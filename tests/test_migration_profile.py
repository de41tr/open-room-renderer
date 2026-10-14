"""Disposable offline profile preparation: no network, subprocess or identity creation."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys

import pytest
import yaml

from fake_linkplay import migration_profile as migration
from fake_linkplay.config import load_startup_config, read_startup_config

ROOT = Path(__file__).resolve().parents[1]


def _installer():
    spec = importlib.util.spec_from_file_location("offline_test_installer", ROOT / "scripts/install-native.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _native(prefix):
    prefix.mkdir(mode=0o700)
    for name in ("state", "run"):
        (prefix / name).mkdir(mode=0o700)
    data = (json.dumps(_installer().user_config(prefix, 8080)) + "\n").encode()
    (prefix / "config.yaml").write_bytes(data)
    receipt = {"format": 1, "mode": "staged-only", "environment": {},
               "source": {"source_sha256": "a" * 64},
               "initial_config_sha256": hashlib.sha256(data).hexdigest()}
    (prefix / "installation-receipt.json").write_text(json.dumps(receipt))
    return prefix


@pytest.fixture
def pair(tmp_path):
    old, new = _native(tmp_path / "old"), _native(tmp_path / "new")
    (old / "state/uuid").write_bytes(b"uuid:1234-ABCD\n")
    return old, new


def _prepare(pair, **kwargs):
    old, new = pair
    args = dict(source=old, source_config=old / "config.yaml", destination=new,
                offline_acknowledged=True)
    args.update(kwargs)
    return migration.prepare_profile(**args)


def _edit_config(prefix, **values):
    path = prefix / "config.yaml"
    raw = json.loads(path.read_text())
    raw.update(values)
    path.write_text(json.dumps(raw))


def _snapshot(prefix):
    return {str(p.relative_to(prefix)): p.read_bytes() for p in prefix.rglob("*") if p.is_file()}


def test_prepares_separate_exact_state_and_only_two_rebased_settings(pair, monkeypatch):
    old, new = pair
    expected = {name: (b"opaque\x00\xff" + name.encode()) for name in migration.STATE_FILES}
    expected["uuid"] = b"uuid:1234-ABCD\n"
    for name, value in expected.items():
        (old / "state" / name).write_bytes(value)
    for name in migration.SKIP_FILES:
        (old / "state" / name).write_bytes(b"secret not read")
    _edit_config(old, http_api_port=9090, mpv_audio_device="alsa/plughw:CARD=Test,DEV=0",
                 proxy_allowed_lan_hosts=["example.invalid"],
                 podcast_presets=[{"name": "old-prefix-label", "url": "https://example.invalid/old?a=secret"}],
                 native_queue_staging=True, friendly_name="Room name")
    raw = json.loads((old / "config.yaml").read_text())
    before, fresh = _snapshot(old), (new / "config.yaml").read_bytes()
    real_open = migration.os.open
    def guarded_open(path, *args, **kwargs):
        assert str(path) not in migration.SKIP_FILES
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(migration.os, "open", guarded_open)
    assert _prepare(pair) == {"status": "prepared", "state_files": 7, "skipped_plex_files": 2, "activated": False}
    profile = new / migration.PROFILE
    raw["state_dir"], raw["mpv_socket"] = str(profile / "state"), str(new / "run/mpv.sock")
    assert yaml.safe_load((profile / "config.yaml").read_text()) == raw
    assert {p.name: p.read_bytes() for p in (profile / "state").iterdir()} == expected
    assert (new / "config.yaml").read_bytes() == fresh and list((new / "state").iterdir()) == []
    assert _snapshot(old) == before
    assert str(profile / "config.yaml") in (profile / "open-room-renderer.service").read_text()
    assert str(new / "venv/bin/open-room-renderer") in (profile / "open-room-renderer.service").read_text()
    for path in profile.rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    assert stat.S_IMODE(profile.stat().st_mode) == 0o700
    assert read_startup_config(profile / "config.yaml").mpv_audio_device == raw["mpv_audio_device"]


def test_dry_run_never_writes_or_loads_runtime_state(pair, monkeypatch):
    before = [_snapshot(p) for p in pair]
    def forbidden(*args, **kwargs):
        raise AssertionError("must not create identity, start processes or connect")
    from fake_linkplay import config
    monkeypatch.setattr(config, "load_config", forbidden)
    monkeypatch.setattr(config, "load_or_create_uuid", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    assert _prepare(pair, dry_run=True)["status"] == "dry-run"
    assert not (pair[1] / migration.PROFILE).exists()
    assert [_snapshot(p) for p in pair] == before


def test_legacy_native_migration_pins_effective_discovery_and_reloads_same_identity(pair):
    old, new = pair
    raw = json.loads((old / "config.yaml").read_text())
    raw.pop("wiim_home_profile")
    raw.pop("mirror_http_on_upnp_port")
    raw["upnp_port"] = 8080
    (old / "config.yaml").write_text(json.dumps(raw))
    saved = b"00000000-0000-4000-8000-000000000001\n"
    (old / "state/uuid").write_bytes(saved)
    before = _snapshot(old)
    original = load_startup_config(old / "config.yaml")
    _prepare(pair)
    output = new / migration.PROFILE
    migrated = yaml.safe_load((output / "config.yaml").read_text())
    assert migrated["wiim_home_profile"] is False
    assert migrated["mirror_http_on_upnp_port"] is False
    assert migrated["upnp_port"] == 8080 and migrated["enable_mdns"] is True
    for _ in range(2):
        cfg = load_startup_config(output / "config.yaml")
        assert cfg.upnp_uuid == original.upnp_uuid == saved.decode().strip()
        assert cfg.mdns_hostname == original.mdns_hostname
        assert cfg.wiim_mac == original.wiim_mac
        assert not cfg.wiim_home_profile and cfg.enable_mdns and cfg.upnp_port == 8080
    assert (output / "state/uuid").read_bytes() == saved
    assert _snapshot(old) == before


@pytest.mark.parametrize("character", ["'", '"', "\x7f"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_destination_executable_path_rejected_before_reads_or_writes(pair, monkeypatch, character, dry_run):
    old, new = pair
    destination = new.with_name(f"new{character}path")
    new.rename(destination)
    before = [_snapshot(p) for p in (old, destination)]
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid destination executable must fail before opening either tree")
    monkeypatch.setattr(migration, "_Tree", forbidden)
    with pytest.raises(migration.PreparationError, match="Destination executable path.*quotes or DEL"):
        _prepare((old, destination), dry_run=dry_run)
    assert [_snapshot(p) for p in (old, destination)] == before
    assert not (destination / migration.PROFILE).exists()


@pytest.mark.parametrize("character", ["'", '"', "\x7f"])
def test_source_and_config_names_need_not_be_executable_paths(pair, character):
    old, new = pair
    source = old.with_name(f"old{character}path")
    old.rename(source)
    _edit_config(source, state_dir=str(source / "state"), mpv_socket=str(source / "run/mpv.sock"))
    config = source / f"config{character}name.yaml"
    (source / "config.yaml").rename(config)
    before = _snapshot(source)
    assert _prepare((source, new), source_config=config)["status"] == "prepared"
    assert _snapshot(source) == before
    assert (new / migration.PROFILE / "state/uuid").read_bytes() == (source / "state/uuid").read_bytes()


def test_relocated_snapshot_uses_explicit_original_prefix(pair, tmp_path):
    old, new = pair
    original = tmp_path / "original"
    _edit_config(old, state_dir=str(original / "state"), mpv_socket=str(original / "run/mpv.sock"))
    with pytest.raises(migration.PreparationError):
        _prepare(pair)
    _prepare(pair, original_prefix=original)
    assert (new / "migrated-profile/state/uuid").read_bytes() == (old / "state/uuid").read_bytes()


@pytest.mark.parametrize("case", ["ack", "missing_uuid", "empty_uuid", "relative_state", "external_state", "external_socket",
                                  "external_mpv", "overlap", "source_public", "state_public", "destination_state",
                                  "source_run", "destination_run", "existing_profile", "changed_destination",
                                  "incomplete_source", "incomplete_destination", "no_digest", "unexpected_state"])
def test_refuses_unsafe_or_not_fresh_preflight_without_output(pair, tmp_path, case):
    old, new = pair
    kwargs = {}
    if case == "ack": kwargs["offline_acknowledged"] = False
    if case == "missing_uuid": (old / "state/uuid").unlink()
    if case == "empty_uuid": (old / "state/uuid").write_text(" \n")
    if case == "relative_state": _edit_config(old, state_dir="state")
    if case == "external_state": _edit_config(old, state_dir=str(tmp_path / "elsewhere"))
    if case == "external_socket": _edit_config(old, mpv_socket="/tmp/shared.sock")
    if case == "external_mpv": _edit_config(old, auto_spawn_mpv=False)
    if case == "overlap": kwargs["destination"] = old
    if case == "source_public": old.chmod(0o755)
    if case == "state_public": (old / "state").chmod(0o755)
    if case == "destination_state": (new / "state/device_name").write_text("used")
    if case == "source_run": (old / "run/leftover").write_text("runtime")
    if case == "destination_run": (new / "run/leftover").write_text("runtime")
    if case == "existing_profile": (new / migration.PROFILE).mkdir()
    if case == "changed_destination": _edit_config(new, friendly_name="edited")
    if case.startswith("incomplete_"):
        prefix = old if case.endswith("source") else new
        receipt = json.loads((prefix / "installation-receipt.json").read_text())
        receipt["mode"] = "installation-incomplete"
        (prefix / "installation-receipt.json").write_text(json.dumps(receipt))
    if case == "no_digest":
        receipt = json.loads((new / "installation-receipt.json").read_text())
        del receipt["initial_config_sha256"]
        (new / "installation-receipt.json").write_text(json.dumps(receipt))
    if case == "unexpected_state": (old / "state/queue.json").write_text("[]")
    with pytest.raises((migration.PreparationError, OSError)):
        _prepare(pair, **kwargs)
    assert not (new / migration.PROFILE / "config.yaml").exists()


@pytest.mark.parametrize("target", ["config.yaml", "installation-receipt.json", "state/uuid", "state/plex_client_id"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory", "writable"])
def test_refuses_unsafe_input_files_without_opening_special_targets(pair, tmp_path, target, kind):
    old, new = pair
    path = old / target
    path.unlink(missing_ok=True)
    outside = tmp_path / "outside"
    outside.write_text("never follow")
    if kind == "symlink": path.symlink_to(outside)
    if kind == "hardlink": os.link(outside, path)
    if kind == "fifo": os.mkfifo(path)
    if kind == "directory": path.mkdir()
    if kind == "writable":
        path.write_text("unsafe")
        path.chmod(0o666)
    with pytest.raises((migration.PreparationError, OSError)):
        _prepare(pair)
    assert not (new / migration.PROFILE).exists()


def test_refuses_symlink_ancestor(pair, tmp_path):
    old, _ = pair
    link = tmp_path / "alias"
    link.symlink_to(old, target_is_directory=True)
    with pytest.raises(OSError):
        _prepare(pair, source=link, source_config=link / "config.yaml")


@pytest.mark.skipif(os.environ.get("ORR_TEST_MIGRATION_SOCKET") != "1",
                    reason="Real Unix-socket refusal proof runs in the existing native CI lane")
def test_refuses_observed_runtime_socket_without_contact(pair):
    old, new = pair
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.bind(str(old / "run/mpv.sock"))
        with pytest.raises(migration.PreparationError, match="runtime entry"):
            _prepare(pair)
    assert not (new / migration.PROFILE).exists()


@pytest.mark.parametrize("body", ["state_dir: a\nstate_dir: b\n", "a: &x value\nb: *x\n", "a: {<<: {uuid: value}}\n",
                                "uuid: value\nuuid: other\n", "!!python/object:os.system {}", "default_volume: !!int private-secret",
                                "podcast_presets: [{name: one, name: two, url: secret}]", "uuid: .nan"])
def test_rejects_ambiguous_yaml_without_diagnostic_values(pair, body, capsys):
    (pair[0] / "config.yaml").write_text(body)
    assert migration.main(["--source", str(pair[0]), "--source-config", str(pair[0] / "config.yaml"),
                           "--destination", str(pair[1]), "--acknowledge-offline"]) == 2
    result = capsys.readouterr()
    assert "private-secret" not in result.err and "https://" not in result.err
    assert not (pair[1] / migration.PROFILE).exists()


def test_requires_bounded_regular_input(pair, monkeypatch):
    monkeypatch.setattr(migration, "MAX_FILE_BYTES", 3)
    with pytest.raises(migration.PreparationError):
        _prepare(pair)


def test_configured_identity_without_state_uuid_is_preserved(pair):
    old, new = pair
    (old / "state/uuid").unlink()
    _edit_config(old, uuid="explicit-id")
    _prepare(pair)
    assert not (new / "migrated-profile/state/uuid").exists()
    assert yaml.safe_load((new / "migrated-profile/config.yaml").read_text())["uuid"] == "explicit-id"


@pytest.mark.parametrize("when", ["capture", "staging"])
def test_observed_source_change_aborts_without_runnable_config(pair, monkeypatch, when):
    old, new = pair
    if when == "capture":
        original = migration._Tree.read
        def changing_read(tree, relative, **kwargs):
            data = original(tree, relative, **kwargs)
            if tree.root == old and relative == "state/uuid":
                (old / "state/uuid").write_text("changed")
            return data
        monkeypatch.setattr(migration._Tree, "read", changing_read)
    else:
        original = migration._write
        def changing_write(directory, name, data):
            original(directory, name, data)
            if name == ".config.pending":
                (old / "state/uuid").write_text("changed")
        monkeypatch.setattr(migration, "_write", changing_write)
    with pytest.raises(migration.PreparationError):
        _prepare(pair)
    assert not (new / "migrated-profile/config.yaml").exists()


@pytest.mark.parametrize("failure", ["state", "unit", "pending", "link", "after_link"])
def test_publication_failures_distinguish_partial_from_committed(pair, monkeypatch, failure):
    new = pair[1]
    original_write, original_link, original_unlink = migration._write, migration.os.link, migration.os.unlink
    def fail_write(directory, name, data):
        if ((failure == "state" and name == "uuid") or (failure == "unit" and name.endswith(".service"))
                or (failure == "pending" and name == ".config.pending")):
            raise OSError("private error")
        original_write(directory, name, data)
    def fail_link(*args, **kwargs):
        if failure == "link": raise OSError("private error")
        return original_link(*args, **kwargs)
    def fail_unlink(path, **kwargs):
        if failure == "after_link" and path == ".config.pending": raise OSError("private error")
        return original_unlink(path, **kwargs)
    monkeypatch.setattr(migration, "_write", fail_write)
    monkeypatch.setattr(migration.os, "link", fail_link)
    monkeypatch.setattr(migration.os, "unlink", fail_unlink)
    expected = migration.ProfileCommittedError if failure == "after_link" else migration.PreparationError
    with pytest.raises(expected): _prepare(pair)
    assert (new / "migrated-profile/config.yaml").exists() == (failure == "after_link")
    with pytest.raises(migration.PreparationError, match="already exists"):
        _prepare(pair)


def test_config_publication_is_exclusive_and_never_replaces_other_writer(pair, monkeypatch):
    original = migration.os.link
    def race(src, dst, **kwargs):
        migration._write(kwargs["dst_dir_fd"], dst, b"other writer")
        return original(src, dst, **kwargs)
    monkeypatch.setattr(migration.os, "link", race)
    with pytest.raises(migration.PreparationError): _prepare(pair)
    assert (pair[1] / "migrated-profile/config.yaml").read_bytes() == b"other writer"


def test_installer_records_digest_as_receipt_metadata(pair, tmp_path):
    prefix = tmp_path / "stage"
    installer = _installer()
    installer.install(prefix, port=8080, stage_only=True, dry_run=False, require_alsa=False)
    receipt = json.loads((prefix / "installation-receipt.json").read_text())
    assert receipt["initial_config_sha256"] == hashlib.sha256((prefix / "config.yaml").read_bytes()).hexdigest()


def test_captured_config_validation_does_not_reopen_or_create_state(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError("no path reads")
    monkeypatch.setattr(Path, "read_text", forbidden)
    cfg = read_startup_config(tmp_path / "absent", captured_text='uuid: existing\nstate_dir: /unused\n')
    assert cfg.uuid == "EXISTING" and not (tmp_path / "absent").exists()


def test_scientific_yaml_numbers_preserve_parsed_type(pair):
    old, new = pair
    text = (old / "config.yaml").read_text()
    raw = json.loads(text)
    raw["default_volume"] = 1e-6
    (old / "config.yaml").write_text(yaml.safe_dump(raw))
    _prepare(pair)
    result = yaml.safe_load((new / "migrated-profile/config.yaml").read_text())
    assert type(result["default_volume"]) is float and result["default_volume"] == 1e-6


def test_double_slash_path_cannot_bypass_disjoint_prefix_check(pair):
    old, new = pair
    with pytest.raises(migration.PreparationError, match="normalized"):
        _prepare(pair, destination="/" + str(old / "nested"))
    assert not (old / "nested").exists()


@pytest.mark.parametrize("failure_call", range(1, 9))
def test_fsync_failures_have_exact_config_commit_boundary(pair, monkeypatch, failure_call):
    original = migration.os.fsync
    calls = 0
    def fail(descriptor):
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise OSError("synthetic private sync failure")
        return original(descriptor)
    monkeypatch.setattr(migration.os, "fsync", fail)
    expected = migration.ProfileCommittedError if failure_call >= 7 else migration.PreparationError
    with pytest.raises(expected):
        _prepare(pair)
    assert (pair[1] / "migrated-profile/config.yaml").exists() == (failure_call >= 7)
    assert (pair[0] / "state/uuid").read_bytes() == b"uuid:1234-ABCD\n"


def test_main_io_error_never_echoes_path_or_secret(pair, monkeypatch, capsys):
    def denied(*args, **kwargs):
        raise PermissionError("https://private.invalid/?token=secret")
    monkeypatch.setattr(migration._Tree, "read", denied)
    result = migration.main(["--source", str(pair[0]), "--source-config", str(pair[0] / "config.yaml"),
                             "--destination", str(pair[1]), "--acknowledge-offline"])
    assert result == 2
    output = capsys.readouterr()
    assert "private.invalid" not in output.err and "token" not in output.err


def test_help_points_to_installed_offline_workflow():
    from fake_linkplay.cli import build_parser
    assert "fake_linkplay.migration_profile" in build_parser().format_help()


def test_context_cleanup_failure_after_commit_is_not_reported_as_uncommitted(pair, monkeypatch):
    original = migration.os.close
    profile = pair[1] / "migrated-profile/config.yaml"
    failed = False
    def close(descriptor):
        nonlocal failed
        original(descriptor)
        if profile.exists() and not failed:
            failed = True
            raise OSError("synthetic close failure")
    monkeypatch.setattr(migration.os, "close", close)
    with pytest.raises(migration.ProfileCommittedError, match="committed"):
        _prepare(pair)
    assert profile.exists()


def test_previously_migrated_source_config_and_state_are_supported(pair, tmp_path):
    old, intermediate = pair
    _prepare(pair)
    fresh = _native(tmp_path / "next")
    migration.prepare_profile(source=intermediate, source_config=intermediate / "migrated-profile/config.yaml",
                              destination=fresh, offline_acknowledged=True)
    assert (fresh / "migrated-profile/state/uuid").read_bytes() == (old / "state/uuid").read_bytes()


@pytest.mark.parametrize("packages", [[], [{"name": "open-room-renderer", "version": "0.1.0"}]])
def test_complete_installed_receipt_mode_is_accepted(pair, packages):
    for prefix in pair:
        path = prefix / "installation-receipt.json"
        receipt = json.loads(path.read_text())
        receipt.update(mode="installed", packages=packages)
        path.write_text(json.dumps(receipt))
    assert _prepare(pair, dry_run=True)["status"] == "dry-run"


def test_installed_receipt_missing_package_record_is_refused(pair):
    path = pair[0] / "installation-receipt.json"
    receipt = json.loads(path.read_text())
    receipt["mode"] = "installed"
    path.write_text(json.dumps(receipt))
    with pytest.raises(migration.PreparationError, match="complete native receipt"):
        _prepare(pair)
