"""Saved-entry read policy: synthetic files only, no event loop, sockets, or audio."""
from __future__ import annotations

import errno
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import traceback
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fake_linkplay import cli, config, http_api, persistence
from fake_linkplay.startup_state import StartupStateError, _STATE_ERRORS
from fake_linkplay.state import RendererState

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = "synthetic-private-saved-target"
SETTINGS = ("gain.json", "active_dsp.json", "channel_balance", "device_name")
DSP = {
    "version": 1, "plugin": "synthetic.eq", "source": "", "enabled": True,
    "name": "Synthetic quiet curve", "balance": 0.375,
    "bands": [{"param_name": f"{letter}_{suffix}", "value": value}
              for letter in "abcdefghij"
              for suffix, value in (("freq", 1000), ("gain", -6), ("q", 1), ("mode", 0))],
}
SAVED = {
    "gain.json": b'{"version":1,"volume":7,"mute":true}\n',
    "active_dsp.json": (json.dumps(DSP) + "\n").encode(),
    "channel_balance": b"0.75\n",
    "device_name": b"Synthetic saved room\n",
}


@pytest.fixture(autouse=True)
def no_resources(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("No backend, socket, or subprocess may start"))
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    for name in ("MpvBackend", "MdnsAdvertiser", "SsdpServer"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(cli.web, "AppRunner", forbidden)
    monkeypatch.setattr(cli.web, "TCPSite", forbidden)
    yield forbidden
    forbidden.assert_not_called()


def read_setting(path):
    if path.name == "gain.json":
        return persistence.load_gain(path.parent)
    if path.name == "active_dsp.json":
        return persistence.load_active_dsp(path.parent)
    return config._read_optional_persistent_text(path)


def make_config(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir()
    path = tmp_path / "renderer.yaml"
    path.write_text(json.dumps({
        "state_dir": str(directory), "uuid": "00000000-0000-4000-8000-000000000000",
        "auto_spawn_mpv": False, "enable_ssdp": False, "advertise_ip": "127.0.0.1",
        "mpv_socket": "/tmp/orr-synthetic-saved-state.sock",
        "default_volume": 50, "default_mute": False, "channel_balance": "0.0",
    }))
    return path, directory


def snapshot(directory):
    return {p.name: (p.lstat().st_ino, p.lstat().st_mode,
                     os.readlink(p) if p.is_symlink() else p.read_bytes())
            for p in directory.iterdir()}


def assert_saved_config(cfg, name):
    if name == "gain.json":
        assert (cfg.default_volume, cfg.default_mute) == (7, True)
    elif name == "active_dsp.json":
        assert cfg.channel_balance == "0.375"
    elif name == "channel_balance":
        assert cfg.channel_balance == "0.75"
    else:
        assert cfg.friendly_name == cfg.mdns_instance_name == "Synthetic saved room"


@pytest.mark.parametrize("name", SETTINGS)
@pytest.mark.parametrize("missing_parents", [False, True])
def test_only_genuinely_missing_entries_allow_absence(tmp_path, name, missing_parents):
    directory = tmp_path / "missing" / "parents" if missing_parents else tmp_path
    assert read_setting(directory / name) is None
    assert list(tmp_path.iterdir()) == []


def test_genuinely_missing_settings_keep_configured_defaults(tmp_path):
    path, directory = make_config(tmp_path)
    cfg = config.load_startup_config(path)
    assert (cfg.default_volume, cfg.default_mute, cfg.channel_balance) == (50, False, "0.0")
    assert cfg.friendly_name == "Open Room Renderer"
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("name", SETTINGS)
@pytest.mark.parametrize("symlink", [False, True])
def test_readable_files_and_links_keep_saved_values_and_exact_storage(tmp_path, name, symlink):
    path, directory = make_config(tmp_path)
    saved = directory / name
    target = tmp_path / PRIVATE
    if symlink:
        target.write_bytes(SAVED[name])
        saved.symlink_to(target)
    else:
        saved.write_bytes(SAVED[name])
    before = snapshot(directory)
    expected = {"gain.json": (7, True), "active_dsp.json": DSP}.get(
        name, SAVED[name].decode(),
    )
    assert read_setting(saved) == expected
    for loader in (config.load_startup_config, config.load_config):
        assert_saved_config(loader(path), name)
    assert snapshot(directory) == before
    if symlink:
        assert target.read_bytes() == SAVED[name]


@pytest.mark.parametrize("name", SETTINGS)
def test_malformed_state_still_fails_privately_without_rewriting(tmp_path, name):
    path, directory = make_config(tmp_path)
    saved = directory / name
    saved.write_bytes(b"\xff" + PRIVATE.encode())
    before = snapshot(directory)
    with pytest.raises(ValueError):
        read_setting(saved)
    with pytest.raises(StartupStateError) as caught:
        config.load_startup_config(path)
    assert str(caught.value) == _STATE_ERRORS[name.removesuffix(".json")]
    assert snapshot(directory) == before


@pytest.mark.parametrize("name", SETTINGS)
def test_unreadable_state_still_fails_without_rewriting(tmp_path, name):
    if os.geteuid() == 0:
        pytest.skip("POSIX permission check needs a non-root process")
    path, directory = make_config(tmp_path)
    saved = directory / name
    saved.write_bytes(SAVED[name])
    before = snapshot(directory)
    saved.chmod(0)
    try:
        with pytest.raises(PermissionError):
            read_setting(saved)
        with pytest.raises(StartupStateError) as caught:
            config.load_startup_config(path)
        assert str(caught.value) == _STATE_ERRORS[name.removesuffix(".json")]
        assert saved.stat().st_mode & 0o777 == 0
    finally:
        saved.chmod(before[name][1] & 0o777)
    assert snapshot(directory) == before


@pytest.mark.parametrize("name", SETTINGS)
@pytest.mark.parametrize("target_kind", ["missing", "missing-parent", "dangling-chain"])
def test_dangling_saved_entries_fail_closed_without_changing_link(tmp_path, name, target_kind):
    path, directory = make_config(tmp_path)
    target = tmp_path / PRIVATE
    if target_kind == "missing-parent":
        target = target / "target"
    elif target_kind == "dangling-chain":
        target.symlink_to(tmp_path / "absent-target")
    saved = directory / name
    saved.symlink_to(target)
    before = snapshot(directory)
    with pytest.raises(FileNotFoundError):
        read_setting(saved)
    with pytest.raises(FileNotFoundError):
        config.load_config(path)
    with pytest.raises(StartupStateError) as caught:
        config.load_startup_config(path)
    assert str(caught.value) == _STATE_ERRORS[name.removesuffix(".json")]
    assert caught.value.__suppress_context__
    assert PRIVATE not in "".join(traceback.format_exception(caught.value))
    assert snapshot(directory) == before


@pytest.mark.parametrize("name", SETTINGS)
@pytest.mark.parametrize("code", [errno.EACCES, errno.EIO])
def test_uncertain_entry_inspection_propagates_instead_of_defaults(tmp_path, monkeypatch, name, code):
    path, directory = make_config(tmp_path)
    saved = directory / name
    saved.symlink_to(tmp_path / PRIVATE)
    before = snapshot(directory)
    original = Path.lstat
    error = OSError(code, PRIVATE, str(saved))

    def inspect(self):
        if self == saved:
            raise error
        return original(self)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", inspect)
        with pytest.raises(OSError) as caught:
            read_setting(saved)
        assert caught.value is error
        with pytest.raises(StartupStateError) as caught:
            config.load_startup_config(path)
        assert str(caught.value) == _STATE_ERRORS[name.removesuffix(".json")]
        assert PRIVATE not in "".join(traceback.format_exception(caught.value))
    assert snapshot(directory) == before


@pytest.mark.parametrize("name", SETTINGS)
def test_entry_present_after_missing_read_preserves_original_error(tmp_path, monkeypatch, name):
    saved = tmp_path / name
    saved.write_bytes(SAVED[name])
    before = snapshot(tmp_path)
    original = Path.open
    error = FileNotFoundError(errno.ENOENT, PRIVATE, str(saved))

    def fail_read(self, *args, **kwargs):
        if self == saved:
            raise error
        return original(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", fail_read)
        with pytest.raises(FileNotFoundError) as caught:
            read_setting(saved)
        assert caught.value is error
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("name", SETTINGS)
@pytest.mark.parametrize("code", [errno.EACCES, errno.EIO])
def test_ordinary_read_errors_do_not_add_inspection_or_retry(tmp_path, monkeypatch, name, code):
    saved = tmp_path / name
    error = OSError(code, PRIVATE, str(saved))
    read = Mock(side_effect=error)
    inspect = Mock(side_effect=AssertionError("Only a missing read needs inspection"))
    monkeypatch.setattr(Path, "open", read)
    monkeypatch.setattr(Path, "lstat", inspect)
    with pytest.raises(OSError) as caught:
        read_setting(saved)
    assert caught.value is error
    read.assert_called_once()
    inspect.assert_not_called()


def run_before_first_await(coroutine):
    """Run synchronous startup validation without an event loop or its sockets."""
    try:
        coroutine.send(None)
    finally:
        coroutine.close()
    raise AssertionError("Startup unexpectedly reached an await")


@pytest.mark.parametrize("name", SETTINGS)
@pytest.mark.parametrize("through_wrapper", [False, True])
def test_cli_and_profile_wrapper_stop_privately_before_backend(
    tmp_path, monkeypatch, capsys, no_resources, name, through_wrapper,
):
    path, directory = make_config(tmp_path)
    saved, target = directory / name, tmp_path / PRIVATE
    saved.symlink_to(target)
    before = snapshot(directory)
    monkeypatch.setattr(cli.asyncio, "run", run_before_first_await)
    if through_wrapper:
        spec = importlib.util.spec_from_file_location("saved_state_profile", ROOT / "scripts/run-profile.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        monkeypatch.setenv("ORR_CONFIG", str(path))
        monkeypatch.setattr(sys, "argv", [str(ROOT / "scripts/run-profile.py"), "portable"])
        # Keep real prepare/preflight and CLI dispatch; replace only the process
        # handoff. Selected ports are unprivileged so preflight needs no socket.
        def dispatch(executable, argv):
            raise SystemExit(cli.main(argv[1:]))
        monkeypatch.setattr(runner.os, "execv", dispatch)
        monkeypatch.chdir(tmp_path)  # Also restores cwd after wrapper changes it.
        with pytest.raises(SystemExit) as caught:
            runner.main()
        assert caught.value.code == 2
    else:
        assert cli.main(["serve", "--config", str(path)]) == 2
    assert capsys.readouterr().err == f"Startup error: {_STATE_ERRORS[name.removesuffix('.json')]}\n"
    assert snapshot(directory) == before
    # Restoring the target cannot wake a stopped startup or cause a saved write.
    target.write_bytes(SAVED[name])
    assert snapshot(directory) == before
    assert target.read_bytes() == SAVED[name]
    no_resources.assert_not_called()
    # An explicit new load sees the restored value through the same saved link.
    assert_saved_config(config.load_startup_config(path), name)
    assert snapshot(directory) == before


@pytest.mark.parametrize("startup", [False, True])
def test_second_api_dsp_read_rejects_newly_dangling_target_before_staging(
    tmp_path, startup,
):
    path, directory = make_config(tmp_path)
    saved, target = directory / "active_dsp.json", tmp_path / PRIVATE
    target.write_bytes(SAVED["active_dsp.json"])
    saved.symlink_to(target)
    before = snapshot(directory)
    cfg = config.load_startup_config(path)
    assert cfg.channel_balance == "0.375"
    target.unlink()  # Synthetic external change between the two real readers.
    stage = Mock(side_effect=AssertionError("No default or saved graph may be staged"))
    with pytest.raises(StartupStateError if startup else FileNotFoundError) as caught:
        http_api.HttpApi(cfg, RendererState.from_config(cfg),
                         SimpleNamespace(stage_initial_dsp=stage), _startup=startup)
    if startup:
        assert str(caught.value) == _STATE_ERRORS["active_dsp"]
        assert PRIVATE not in "".join(traceback.format_exception(caught.value))
    stage.assert_not_called()
    assert snapshot(directory) == before
    target.write_bytes(SAVED["active_dsp.json"])
    assert snapshot(directory) == before
    assert target.read_bytes() == SAVED["active_dsp.json"]
    stage.assert_not_called()


def test_valid_active_dsp_still_overrides_dangling_legacy_balance(tmp_path):
    path, directory = make_config(tmp_path)
    (directory / "active_dsp.json").write_bytes(SAVED["active_dsp.json"])
    (directory / "channel_balance").symlink_to(tmp_path / PRIVATE)
    before = snapshot(directory)
    for loader in (config.load_startup_config, config.load_config):
        assert loader(path).channel_balance == "0.375"
    assert snapshot(directory) == before
