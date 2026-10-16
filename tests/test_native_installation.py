"""Native profile checks: no privilege changes, service starts, or network installs."""
from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from fake_linkplay.config import Config, load_config

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def installer(monkeypatch):
    module = load_script("install-native")
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(module.sys, "maxsize", 2**63 - 1)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/mpv")
    monkeypatch.setattr(module, "run", lambda *args: "mpv v0.37.0")
    # Unit boundaries below must not depend on the host's optional venv package.
    # The absent-prerequisite test overrides these two availability lookups.
    find_spec = module.importlib.util.find_spec

    def available_venv_spec(name, package=None):
        if name in {"venv", "ensurepip"}:
            return importlib.machinery.ModuleSpec(name, loader=None)
        return find_spec(name, package)

    monkeypatch.setattr(module.importlib.util, "find_spec", available_venv_spec)
    return module


@pytest.fixture
def runner(tmp_path, monkeypatch):
    module = load_script("run-profile")
    root = tmp_path / "r"
    (root / "configs").mkdir(parents=True)
    for name in ("config.example.yaml", "config.app-compat.yaml"):
        (root / "configs" / name).write_text((ROOT / "configs" / name).read_text())
    monkeypatch.setattr(module, "ROOT", root)
    for name in ("ORR_CONFIG", "APP_COMPAT_PORT80", "APP_COMPAT_FRESH", "APP_COMPAT_NAME",
                 "WIIM_HOME_ACOUSTIC_CAPABILITY_MODE", "WIIM_HOME_AUDIO_OUTPUT_CAPABILITY_MODE"):
        # Register restoration even when originally absent: prepare() may set
        # the app capability defaults directly through os.environ.setdefault.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    return module


def test_native_environment_version_and_architecture(installer, monkeypatch):
    info = installer.environment()
    assert info["mpv"] == "mpv v0.37.0"
    assert info["machine"] == "aarch64"
    assert "hardware/systemd untested" in info["validation"]
    monkeypatch.setattr(installer.platform, "machine", lambda: "armv7l")
    assert "ARMv7 untested" in installer.environment()["validation"]
    monkeypatch.setattr(installer.platform, "machine", lambda: "mips")
    with pytest.raises(ValueError, match="no installation profile"):
        installer.environment()


@pytest.mark.parametrize("version", ["mpv 0.34.1", "mpv 0.35.1", "mpv v0.36.0", "unknown player"])
def test_native_rejects_old_or_unknown_mpv(installer, monkeypatch, version):
    monkeypatch.setattr(installer, "run", lambda *args: version)
    with pytest.raises(ValueError, match=r"mpv >= 0\.37"):
        installer.environment()


@pytest.mark.parametrize("version", ["mpv 0.37.0", "mpv v0.37.0", "mpv 0.40.0"])
def test_native_accepts_supported_mpv_floor(installer, monkeypatch, version):
    monkeypatch.setattr(installer, "run", lambda *args: version)
    assert installer.environment()["mpv"] == version


def test_native_rejects_old_python_and_missing_mpv(installer, monkeypatch):
    monkeypatch.setattr(installer.sys, "version_info", (3, 10, 12))
    with pytest.raises(ValueError, match="Python 3.11"):
        installer.environment()
    monkeypatch.setattr(installer.sys, "version_info", (3, 11, 0))
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="Missing mpv"):
        installer.environment()
    assert installer.environment(require_mpv=False)["mpv"] is None


def test_native_optional_alsa_gate(installer, monkeypatch):
    monkeypatch.setattr(installer.os, "access", lambda *args: False)
    assert installer.environment()["alsa_pcm_accessible"] is False
    with pytest.raises(ValueError, match="No accessible ALSA"):
        installer.environment(require_alsa=True)


@pytest.mark.parametrize("port,soap_port", [(8080, 59152), (8081, 8081), (59152, 59152), (65535, 65535)])
def test_native_default_split_and_custom_single_port_contract(installer, tmp_path, port, soap_port):
    # The default has a separate SOAP listener; --port keeps custom installs
    # on one explicitly selected port, including two renderers on one host.
    from fake_linkplay.config import Config
    config = Config(**installer.user_config(tmp_path, port))
    assert config.http_api_port == port and config.upnp_port == soap_port
    assert config.wiim_home_profile and config.enable_mdns and config.enable_ssdp
    assert config.mirror_http_on_upnp_port and not config.mirror_port_80


def test_native_rejects_privileged_port_and_unsafe_paths(installer, tmp_path):
    with pytest.raises(ValueError, match="unprivileged port"):
        installer.user_config(tmp_path, 80)
    with pytest.raises(ValueError, match="too long"):
        installer.checked_prefix("/tmp/" + "x" * 110)
    with pytest.raises(ValueError, match="systemd expansion"):
        installer.checked_prefix("/tmp/$HOME")
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="symlinks"):
        installer.checked_prefix(str(link))


def test_stage_dry_run_does_not_write(installer, tmp_path):
    prefix = tmp_path / "native"
    installer.install(prefix, port=8080, stage_only=True, dry_run=True, require_alsa=False)
    assert not prefix.exists()


def test_stage_load_restart_distinct_identity_and_safe_uninstall(installer, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    for prefix in (first, second):
        installer.install(prefix, port=8080, stage_only=True, dry_run=False, require_alsa=False)
        receipt = json.loads((prefix / "installation-receipt.json").read_text())
        assert receipt["mode"] == "staged-only"
        assert receipt["source"]["source_sha256"]
        assert not (prefix / "venv").exists()
        assets = prefix / "source/fake_linkplay/web_assets"
        assert {path.name for path in assets.iterdir()} == {"index.html", "style.css", "controller.js", "app.js", "logo.png", "tidal.html", "tidal.js"}
        manifest = json.loads((prefix / installer.MANIFEST).read_text())
        entries = {entry["path"]: entry for entry in manifest["files"]}
        for name in ("index.html", "style.css", "controller.js", "app.js", "logo.png", "tidal.html", "tidal.js"):
            body = (ROOT / "fake_linkplay/web_assets" / name).read_bytes()
            assert (assets / name).read_bytes() == body
            assert entries[f"source/fake_linkplay/web_assets/{name}"]["sha256"] == hashlib.sha256(body).hexdigest()
    config = load_config(first / "config.yaml")
    assert config.http_api_port == 8080 and config.upnp_port == 59152
    assert config.wiim_home_profile and config.enable_mdns and config.enable_ssdp
    assert not config.mirror_port_80
    assert config.uuid == load_config(first / "config.yaml").uuid
    assert config.uuid != load_config(second / "config.yaml").uuid
    assert config.mpv_socket != load_config(second / "config.yaml").mpv_socket
    assert "NoNewPrivileges=yes" in (first / "open-room-renderer.service").read_text()
    assert "sudo" not in (first / "open-room-renderer.service").read_text()
    foreign = first / "source/user-notes.txt"
    foreign.write_text("keep this")
    edited = first / "config.yaml"
    edited.write_text(edited.read_text() + "\n# User edit\n")
    edited_asset = first / "source/fake_linkplay/web_assets/style.css"
    edited_asset.write_text(edited_asset.read_text() + "\n/* User edit */\n")
    installer.uninstall(first, dry_run=True)
    assert (first / "source/pyproject.toml").exists()
    installer.uninstall(first, dry_run=False)
    assert not (first / "source/pyproject.toml").exists()
    assert foreign.read_text() == "keep this"
    assert edited.is_file()
    assert edited_asset.read_text().endswith("/* User edit */\n")
    assert not (first / "source/fake_linkplay/web_assets/index.html").exists()
    assert (first / "state/uuid").read_text().strip() == config.uuid
    assert (first / installer.MANIFEST).is_file()  # retained with modified owned files
    assert (first / "uninstall.py").is_file()
    installer.uninstall(second, dry_run=False)
    assert (second / "state/uuid").is_file()
    assert not (second / "source/fake_linkplay/web_assets").exists()


def test_native_refuses_existing_prefix(installer, tmp_path):
    with pytest.raises(ValueError, match="already exists"):
        installer.install(tmp_path, port=8080, stage_only=True, dry_run=False, require_alsa=False)


@pytest.fixture
def native_receiver(installer, tmp_path, monkeypatch):
    from fake_linkplay import spotify_receiver as receiver

    prefix, root = tmp_path / "r", tmp_path / "receiver"
    installer.install(prefix, port=8080, stage_only=True, dry_run=False, require_alsa=False)
    launcher = prefix / "venv/bin/open-room-renderer"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("inert fixture; never executed\n")
    installer.write_manifest(prefix)
    root.mkdir(mode=0o700)
    profile = {"format": 1, "directory": str(root), "renderer_prefix": str(prefix),
               "renderer_config": str(prefix / "config.yaml"),
               "renderer_unit": str(prefix / receiver.RENDERER_SERVICE),
               "client_dir": str(tmp_path / "client")}
    receiver._write(root / "receiver.json", profile)
    for name, body in receiver._unit_parts(root, profile).items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_text(body)
        target.chmod(0o600)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    links = receiver._install_links(root, profile)
    for link, target in links.items():
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    receiver._write(root / "installation.json", {"format": 1, "state": "INSTALLED",
                    "links": {str(p): str(t) for p, t in links.items()}})
    return prefix, root, links


def test_native_receiver_removal_requires_receiver_uninstall_first(native_receiver, monkeypatch):
    from fake_linkplay import spotify_receiver as receiver

    prefix, root, links = native_receiver
    before = {str(p.relative_to(prefix)): p.read_bytes() for p in prefix.rglob("*") if p.is_file()}
    # -I -S excludes both checkout imports and installed packages. The copied
    # uninstaller must protect the independent command even without mpv running.
    command = [sys.executable, "-I", "-S", str(prefix / "uninstall.py"), "--prefix", str(prefix), "--uninstall"]
    assert not (prefix / "run/mpv.sock").exists()
    for extra in (["--dry-run"], []):
        result = subprocess.run(command + extra, text=True, capture_output=True, timeout=10)
        assert result.returncode == 2 and "uninstall' command first" in result.stderr
        assert {str(p.relative_to(prefix)): p.read_bytes() for p in prefix.rglob("*") if p.is_file()} == before
        assert all(receiver._exact_link(p, t) for p, t in links.items())

    calls = []
    class StoppedServices:
        def __init__(self, *_args): pass
        def validate(self): return {}
        def stop_all(self, _before): calls.append("confirmed stopped")
        def command(self, *args): calls.append(args)
    monkeypatch.setattr(receiver, "UserServices", StoppedServices)
    assert receiver.uninstall(str(root))["installation"] == "UNINSTALLED"
    assert calls == ["confirmed stopped", ("daemon-reload",)]
    assert not any(os.path.lexists(p) for p in links)
    result = subprocess.run(command, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert not (prefix / "venv/bin/open-room-renderer").exists()
    assert not (prefix / "source").exists()
    assert (prefix / "config.yaml").is_file() and (root / "receiver.json").is_file()


def test_native_receiver_does_not_block_unrelated_prefix(installer, native_receiver, tmp_path, monkeypatch):
    prefix, _root, links = native_receiver
    old = tmp_path / "r-old"
    old.mkdir()
    (old / "owned").write_text("old native installation")
    installer.write_manifest(old)
    # The explicit XDG location, not an unrelated default-home unit, is used.
    home = tmp_path / "home"
    default_unit = home / ".config/systemd/user/open-room-renderer-spotify.service"
    default_unit.parent.mkdir(parents=True)
    default_unit.symlink_to(tmp_path / "missing")
    monkeypatch.setenv("HOME", str(home))
    installer.uninstall(old, dry_run=False)
    assert not old.exists()
    assert (prefix / "venv/bin/open-room-renderer").is_file()
    assert all(p.is_symlink() and os.readlink(p) == str(t) for p, t in links.items())
    default_unit.unlink()
    default_unit.symlink_to(links[next(iter(links))])
    monkeypatch.delenv("XDG_CONFIG_HOME")
    with pytest.raises(ValueError, match="uninstall.*first"):
        installer.uninstall(prefix, dry_run=False)


@pytest.mark.parametrize("damage", ["only-dropin", "dangling-unit", "changed-link", "replaced-link", "broken-metadata"])
def test_native_receiver_incomplete_links_never_authorize_removal(installer, native_receiver, damage):
    prefix, root, links = native_receiver
    spotify, dropin = links
    if damage == "only-dropin":
        spotify.unlink()
    else:
        dropin.unlink()  # The remaining Spotify link alone must suffice.
        if damage == "dangling-unit":
            links[spotify].unlink()
        elif damage == "changed-link":
            spotify.unlink()
            spotify.symlink_to(root / "changed.service")
        elif damage == "replaced-link":
            spotify.unlink()
            spotify.write_text("replacement, preserve for manual review")
        else:
            (root / "receiver.json").write_text("{")
    before = {str(p.relative_to(prefix)): p.read_bytes() for p in prefix.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="uninstall.*first"):
        installer.uninstall(prefix, dry_run=False)
    assert {str(p.relative_to(prefix)): p.read_bytes() for p in prefix.rglob("*") if p.is_file()} == before


def test_uninstall_rejects_escape_before_any_deletion(installer, tmp_path):
    prefix = tmp_path / "p"
    prefix.mkdir()
    safe = prefix / "safe"
    safe.write_text("keep")
    outside = tmp_path / "outside"
    outside.write_text("keep")
    data = {"format": 1, "files": [installer.file_entry(safe, prefix), {"path": "../outside", "sha256": "anything"}], "directories": []}
    (prefix / installer.MANIFEST).write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Unsafe path"):
        installer.uninstall(prefix, dry_run=False)
    assert safe.read_text() == outside.read_text() == "keep"


def test_uninstall_rejects_replaced_parent_and_unlinks_owned_symlink_only(installer, tmp_path):
    prefix = tmp_path / "p"
    prefix.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_text("keep")
    (prefix / "link").symlink_to(outside, target_is_directory=True)
    data = {"format": 1, "files": [{"path": "link/file", "sha256": "anything"}], "directories": []}
    (prefix / installer.MANIFEST).write_text(json.dumps(data))
    with pytest.raises(ValueError, match="traverses a symlink"):
        installer.uninstall(prefix, dry_run=False)
    installer.write_manifest(prefix)
    installer.uninstall(prefix, dry_run=False)
    assert (outside / "file").read_text() == "keep"
    assert not prefix.exists()


def test_manifest_excludes_only_root_user_data_without_traversing_it(installer, tmp_path, monkeypatch):
    prefix = tmp_path / "p"
    protected = ("config.yaml", "state/nested/settings.json", "migrated-profile/nested/config.yaml")
    owned = ("config.yaml.backup", "stateful/file", "migrated-profile-old/file",
             "source/config.yaml", "source/state/file", "source/migrated-profile/file")
    for relative in (*protected, *owned):
        path = prefix / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic data")
    for name in ("state", "migrated-profile"):
        (prefix / name / "empty").mkdir()
    scandir = installer.os.scandir

    def no_user_data_traversal(path):
        assert Path(path) not in (prefix / "state", prefix / "migrated-profile")
        return scandir(path)

    monkeypatch.setattr(installer.os, "scandir", no_user_data_traversal)
    installer.write_manifest(prefix)
    data = json.loads((prefix / installer.MANIFEST).read_text())
    assert {entry["path"] for entry in data["files"]} == set(owned)
    assert set(data["directories"]) == {
        "stateful", "migrated-profile-old", "source", "source/state", "source/migrated-profile"}


@pytest.mark.parametrize("changed", [False, True])
def test_old_manifest_cannot_own_user_data_or_retain_cleanup_tools(installer, tmp_path, capsys, changed):
    prefix = tmp_path / "p"
    protected = ("config.yaml", "state/nested/settings.json", "migrated-profile/nested/config.yaml")
    owned = ("source/config.yaml", "source/state/file", "source/migrated-profile/file",
             "installation-receipt.json", "uninstall.py")
    entries = []
    for relative in (*protected, *owned):
        path = prefix / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original synthetic data")
        entries.append(installer.file_entry(path, prefix))
        if changed and relative in protected:
            path.write_text("changed synthetic user data")
    directories = ("state", "state/nested", "state/empty", "migrated-profile",
                   "migrated-profile/nested", "migrated-profile/empty", "source",
                   "source/state", "source/migrated-profile")
    for relative in directories:
        (prefix / relative).mkdir(exist_ok=True)
    # Old manifests include the exact hashes of configuration and saved state.
    manifest = prefix / installer.MANIFEST
    manifest.write_text(json.dumps({"format": 1, "files": entries, "directories": directories}))
    before = {relative: (prefix / relative).read_bytes() for relative in (*protected, *owned, installer.MANIFEST)}
    installer.uninstall(prefix, dry_run=True)
    assert {relative: (prefix / relative).read_bytes() for relative in before} == before
    assert all((prefix / relative).is_dir() for relative in directories)
    output = capsys.readouterr().out
    assert "Preserved modified file:" not in output
    for relative in protected:
        assert f"Would remove {prefix / relative}" not in output

    installer.uninstall(prefix, dry_run=False)
    assert {relative: (prefix / relative).read_bytes() for relative in protected} == {
        relative: before[relative] for relative in protected}
    assert all((prefix / relative).is_dir() for relative in directories if not relative.startswith("source"))
    assert not (prefix / "source").exists()
    assert not any((prefix / relative).exists() for relative in (*owned, installer.MANIFEST))
    assert "Preserved modified file:" not in capsys.readouterr().out


@pytest.mark.parametrize("name", ["config.yaml", "state", "migrated-profile"])
@pytest.mark.parametrize("kind", ["regular_file", "symlink", "broken_symlink"])
def test_user_data_names_are_protected_regardless_of_file_kind(installer, tmp_path, name, kind):
    prefix = tmp_path / "p"
    prefix.mkdir()
    path = prefix / name
    outside = tmp_path / "outside"
    if kind == "regular_file":
        path.write_text("user data")
    else:
        if kind == "symlink":
            outside.mkdir()
            (outside / "data").write_text("outside user data")
        path.symlink_to(outside, target_is_directory=True)
    entry = installer.file_entry(path, prefix)
    installer.write_manifest(prefix)
    manifest = prefix / installer.MANIFEST
    data = json.loads(manifest.read_text())
    assert data["files"] == data["directories"] == []
    # Simulate an old or forged ownership claim; the link itself must survive.
    data["files"] = [entry]
    data["directories"] = [name]
    manifest.write_text(json.dumps(data))
    installer.uninstall(prefix, dry_run=False)
    assert installer.file_entry(path, prefix) == entry
    assert not manifest.exists()
    if kind == "symlink":
        assert (outside / "data").read_text() == "outside user data"


@pytest.mark.parametrize("section", ["files", "directories"])
@pytest.mark.parametrize("unsafe", ["state/../safe", "migrated-profile/./file", "config.yaml/../safe"])
def test_protected_paths_do_not_bypass_manifest_validation(installer, tmp_path, section, unsafe):
    prefix = tmp_path / "p"
    prefix.mkdir()
    safe = prefix / "safe"
    safe.write_text("keep until all paths validate")
    data = {"format": 1, "files": [installer.file_entry(safe, prefix)], "directories": []}
    data[section].append({"path": unsafe, "sha256": "anything"} if section == "files" else unsafe)
    manifest = prefix / installer.MANIFEST
    manifest.write_text(json.dumps(data))
    before = manifest.read_bytes()
    with pytest.raises(ValueError, match="Unsafe path"):
        installer.uninstall(prefix, dry_run=False)
    assert safe.read_text() == "keep until all paths validate"
    assert manifest.read_bytes() == before


@pytest.mark.parametrize("section", ["files", "directories"])
@pytest.mark.parametrize("name", ["state", "migrated-profile"])
def test_protected_subtrees_still_reject_symlink_traversal_before_deletion(installer, tmp_path, section, name):
    prefix = tmp_path / "p"
    (prefix / name).mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_text("outside user data")
    (prefix / name / "nested").symlink_to(outside, target_is_directory=True)
    safe = prefix / "safe"
    safe.write_text("owned file")
    data = {"format": 1, "files": [installer.file_entry(safe, prefix)], "directories": []}
    unsafe = f"{name}/nested/file"
    data[section].append({"path": unsafe, "sha256": "anything"} if section == "files" else unsafe)
    (prefix / installer.MANIFEST).write_text(json.dumps(data))
    with pytest.raises(ValueError, match="traverses a symlink"):
        installer.uninstall(prefix, dry_run=False)
    assert safe.read_text() == "owned file"
    assert (outside / "file").read_text() == "outside user data"


@pytest.mark.parametrize("identity", ["00000000-0000-4000-8000-000000000001", "A" * 36])
def test_portable_runner_upgrade_keeps_existing_neutral_discovery(runner, identity):
    state = runner.ROOT / ".state"
    state.mkdir()
    saved = identity + "\n"
    (state / "uuid").write_text(saved)
    for _ in range(2):
        cfg = load_config(runner.prepare("portable"))
        assert cfg.upnp_uuid == saved.strip() and not cfg.wiim_home_profile
        assert cfg.http_api_port == cfg.upnp_port == 8080
        assert not cfg.enable_mdns and not cfg.mirror_http_on_upnp_port
        assert cfg.linkplay_instance_name == Config(uuid=identity, wiim_home_profile=False).linkplay_instance_name
        assert (state / "uuid").read_text() == saved


def test_portable_and_compat_runners_keep_identity(runner, monkeypatch):
    portable_path = runner.prepare("portable")
    portable = load_config(portable_path)
    assert portable.http_api_port == 8080 and portable.upnp_port == 59152
    assert portable.wiim_home_profile and portable.enable_mdns and portable.enable_ssdp
    restarted = load_config(runner.prepare("portable"))
    assert portable.uuid == restarted.uuid
    assert restarted.wiim_home_profile and restarted.enable_mdns and restarted.upnp_port == 59152
    assert portable.linkplay_instance_name == restarted.linkplay_instance_name
    compat = load_config(runner.prepare("app-compat"))
    assert compat.http_api_port == 8080 and compat.upnp_port == 59152
    assert not compat.mirror_port_80
    assert compat.wiim_home_profile
    assert compat.uuid == load_config(runner.prepare("app-compat")).uuid
    assert compat.uuid != portable.uuid
    monkeypatch.setenv("APP_COMPAT_PORT80", "1")
    port80 = load_config(runner.prepare("app-compat"))
    assert port80.http_api_port == 80 and port80.upnp_port == 59152
    assert port80.uuid == compat.uuid
    monkeypatch.setenv("APP_COMPAT_FRESH", "1")
    assert load_config(runner.prepare("app-compat")).uuid != compat.uuid


def test_runner_preserves_explicit_custom_yaml(runner, monkeypatch):
    path = runner.ROOT / "custom.yaml"
    text = "http_api_port: 8888\nuuid: synthetic-custom-id\n"
    path.write_text(text)
    monkeypatch.setenv("ORR_CONFIG", str(path))
    assert runner.prepare("app-compat") == path
    assert path.read_text() == text


def test_runner_missing_mpv_and_wrong_ipc(runner, monkeypatch):
    path = runner.prepare("portable")
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="Missing mpv"):
        runner.preflight(path)
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/mpv")
    config = yaml.safe_load(path.read_text())
    # Use a short IPC path to isolate the wrong-path check from sockaddr_un size.
    config["mpv_socket"] = str(runner.ROOT / "ipc")
    path.write_text(yaml.safe_dump(config))
    Path(config["mpv_socket"]).write_text("foreign file")
    with pytest.raises(ValueError, match="not a socket"):
        runner.preflight(path)


def test_runner_port80_permission_error_never_elevates(runner, monkeypatch):
    monkeypatch.setenv("APP_COMPAT_PORT80", "1")
    path = runner.prepare("app-compat")
    config = yaml.safe_load(path.read_text())
    config["mpv_socket"] = "/tmp/orr-test-unused.sock"
    path.write_text(yaml.safe_dump(config))
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/mpv")
    class DeniedSocket:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def bind(self, address):
            raise PermissionError("permission denied")
    monkeypatch.setattr(runner.socket, "socket", lambda *args: DeniedSocket())
    with pytest.raises(ValueError, match="No sudo is invoked"):
        runner.preflight(path)


def test_public_yaml_examples_parse_without_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert not (ROOT / "examples/config.example.toml").exists()
    portable = load_config(ROOT / "examples/config.example.yaml")
    assert portable.http_api_port == 8080
    data = yaml.safe_load((ROOT / "examples/systemd/config.port80.yaml").read_text())
    data["state_dir"] = str(tmp_path / "system-state")
    config = tmp_path / "system.yaml"
    config.write_text(yaml.safe_dump(data))
    assert load_config(config).http_api_port == 80
    text = (ROOT / "examples/systemd/system-port80.service.example").read_text()
    assert "User=open-room-renderer" in text
    assert "CapabilityBoundingSet=CAP_NET_BIND_SERVICE" in text
    assert "AmbientCapabilities=CAP_NET_BIND_SERVICE" in text


def test_shell_helpers_no_longer_elevate_or_default_to_port80():
    for name in ("run.sh", "run-dev.sh", "run-app-compat.sh", "acceptance-open-source-client.sh"):
        text = (ROOT / "scripts" / name).read_text()
        assert "exec sudo" not in text
        assert "sudo -E" not in text
        assert "${APP_COMPAT_FRESH:-1}" not in text
        assert "${API_PORT:-80}" not in text
        subprocess.run(["bash", "-n", str(ROOT / "scripts" / name)], check=True)
    setup = (ROOT / "scripts/dev-setup.sh").read_text()
    assert '${PYTHON:-python3}' in setup
    assert "sys.version_info >= (3, 11)" in setup


@pytest.mark.parametrize("output,returncode", [
    ('[{"name": "example-package", "version": "1.0"}]', 0),
    ('[{"name": "example-package", "version": "1.0"}]', 1),
    ("invalid JSON", 0),
])
def test_native_package_inventory_keeps_warnings_separate(
        tmp_path, monkeypatch, capfd, output, returncode):
    installer = load_script("install-native")
    monkeypatch.setattr(installer, "environment", lambda **kwargs: {"alsa_pcm_accessible": False})
    prefix = tmp_path / "native"
    real_run = subprocess.run
    installs = []
    builds = []
    # The CI checkout exposes same-version egg-info through this inherited path.
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    monkeypatch.setenv("PYYAML_FORCE_LIBYAML", "1")

    def skip_package_build(args, **kwargs):
        if args[1:4] == ["-I", "-m", "venv"]:
            python = prefix / "venv/bin/python"
            python.parent.mkdir(parents=True)
            python.write_text(
                f"#!{sys.executable}\nimport sys\n"
                "sys.stderr.write('WARNING: temporary pip cache is unavailable\\n')\n"
                f"sys.stdout.write({output!r})\nsys.exit({returncode})\n")
            python.chmod(0o700)
        elif args[1:5] == ["-I", "-m", "pip", "install"]:
            installs.append((args, kwargs))
        elif args[1:5] == ["-I", "-m", "pip", "wheel"]:
            builds.append(args)
            (prefix / "wheels/open_room_renderer-0.1.0-py3-none-any.whl").touch()
        elif args[1:3] == ["-I", "-c"]:
            assert "not yaml.__with_libyaml__" in args[3]
        elif len(args) > 2 and args[2] == str(prefix / "venv/bin/open-room-renderer"):
            assert args[1] == "-I"
        else:
            if args[0] == str(prefix / "venv/bin/python"):
                assert args[1:5] == ["-I", "-m", "pip", "list"]
            return real_run(args, **kwargs)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(installer.subprocess, "run", skip_package_build)
    succeeded = returncode == 0 and output != "invalid JSON"
    assert installer.main(["--prefix", str(prefix)]) == (0 if succeeded else 2)
    assert len(installs) == 4
    bootstrap, pure, yaml, application = installs
    assert "--only-binary=:all:" in bootstrap[0]
    assert "--no-build-isolation" not in bootstrap[0]
    assert "--no-build-isolation" in pure[0]
    for (_, kwargs), names in ((bootstrap, {"setuptools", "wheel"}),
                               (pure, {"pyaes", "ratelimit"})):
        assert {line.split("==")[0] for line in kwargs["input"].splitlines()} == names
        assert all(" --hash=sha256:" in line for line in kwargs["input"].splitlines())
    for command, _ in (bootstrap, pure):
        assert "--require-hashes" in command and "--no-deps" in command
    assert yaml[1]["env"]["PYYAML_FORCE_LIBYAML"] == "0"
    assert os.environ["PYYAML_FORCE_LIBYAML"] == "1"
    assert all("env" not in kwargs for _, kwargs in (bootstrap, pure, application))
    assert yaml[1]["input"] == installer.PURE_YAML
    assert {"--no-binary=PyYAML", "--no-cache-dir", "--no-build-isolation",
            "--no-deps", "--require-hashes"} <= set(yaml[0])
    # The shortcut must not suppress isolation for arbitrary/native dependencies.
    assert "--no-build-isolation" not in application[0]
    assert application[0][-1] == str(prefix / "wheels/open_room_renderer-0.1.0-py3-none-any.whl")
    assert len(builds) == 1 and builds[0][-1] == str(prefix / "source")
    assert "--no-build-isolation" in builds[0] and "--no-deps" in builds[0]
    captured = capfd.readouterr()
    assert "WARNING: temporary pip cache is unavailable" in captured.err
    receipt = json.loads((prefix / "installation-receipt.json").read_text())
    assert receipt["mode"] == ("installed" if succeeded else "installation-incomplete")
    if succeeded:
        assert receipt["packages"] == json.loads(output)
    else:
        assert "Installation error:" in captured.err
        assert "packages" not in receipt
    manifest = json.loads((prefix / installer.MANIFEST).read_text())
    assert {"venv/bin/python", "installation-receipt.json", "uninstall.py"} <= {
        entry["path"] for entry in manifest["files"]}


@pytest.mark.parametrize("failed_report", ["pip-build-tools-report.json", "pip-pure-build-report.json", "pip-yaml-report.json", "wheel"])
def test_native_build_failure_does_not_fall_back_or_claim_installation(
        installer, tmp_path, monkeypatch, failed_report):
    prefix = tmp_path / "partial-build"
    installs = []

    def fail_build(args, **kwargs):
        if args[1:4] == ["-I", "-m", "pip"]:
            installs.append(args)
            if str(prefix / failed_report) in args or args[4] == failed_report:
                raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(installer.subprocess, "run", fail_build)
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(prefix, port=8080, stage_only=False, dry_run=False, require_alsa=False)
    assert str(prefix / failed_report) in installs[-1] or installs[-1][4] == failed_report
    assert all(str(prefix / "pip-install-report.json") not in args for args in installs)
    receipt = json.loads((prefix / "installation-receipt.json").read_text())
    assert receipt["mode"] == "installation-incomplete"
    assert "packages" not in receipt
    assert (prefix / installer.MANIFEST).is_file()


def test_incomplete_install_has_safe_manifest(installer, tmp_path, monkeypatch):
    prefix = tmp_path / "partial"
    def failed_venv(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])
    monkeypatch.setattr(installer.subprocess, "run", failed_venv)
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(prefix, port=8080, stage_only=False, dry_run=False, require_alsa=False)
    receipt = json.loads((prefix / "installation-receipt.json").read_text())
    assert receipt["mode"] == "installation-incomplete"
    assert receipt["source"]["source_sha256"] == installer.source_receipt(prefix / "source")["source_sha256"]
    assert (prefix / installer.MANIFEST).is_file()
    config = (prefix / "config.yaml").read_bytes()
    installer.uninstall(prefix, dry_run=False)
    assert {path.name for path in prefix.iterdir()} == {"config.yaml", "state"}
    assert (prefix / "config.yaml").read_bytes() == config
    assert not any((prefix / "state").iterdir())


def test_generated_unit_quotes_spaces_and_specifiers(installer):
    text = installer.user_unit(Path("/tmp/test name%value"))
    assert 'WorkingDirectory=/tmp/test name%%value' in text
    assert 'ExecStart="/tmp/test name%%value/venv/bin/open-room-renderer"' in text


@pytest.mark.parametrize("character", ["'", '"', "\x7f"])
@pytest.mark.parametrize("stage_only,dry_run", [(False, False), (False, True), (True, False), (True, True)])
def test_native_executable_path_rejected_before_effects(installer, tmp_path, monkeypatch, character, stage_only, dry_run):
    prefix = tmp_path / f"native{character}path"
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid executable prefixes must fail before environment probes or installation")
    monkeypatch.setattr(installer, "environment", forbidden)
    with pytest.raises(ValueError, match="executable path.*quotes or DEL"):
        installer.checked_prefix(str(prefix))
    with pytest.raises(ValueError, match="executable path.*quotes or DEL"):
        installer.install(prefix, port=8080, stage_only=stage_only, dry_run=dry_run, require_alsa=False)
    args = ["--prefix", str(prefix)]
    if stage_only:
        args.append("--stage-only")
    if dry_run:
        args.append("--dry-run")
    assert installer.main(args) == 2
    assert not prefix.exists()


@pytest.mark.parametrize("character", ["'", '"', "\x7f"])
@pytest.mark.parametrize("standalone", [False, True])
def test_old_executable_path_can_still_be_uninstalled(installer, tmp_path, monkeypatch, character, standalone):
    prefix = tmp_path / f"old{character}path"
    prefix.mkdir()
    (prefix / "state").mkdir()
    (prefix / "state/uuid").write_text("keep identity")
    (prefix / "config.yaml").write_text("keep config")
    (prefix / "owned").write_text("remove owned file")
    copied = prefix / "uninstall.py"
    copied.write_text((ROOT / "scripts/install-native.py").read_text())
    installer.write_manifest(prefix)
    if standalone:
        # The copied tool has no checkout-relative native_profile helper.
        monkeypatch.setattr(sys, "dont_write_bytecode", True)
        spec = importlib.util.spec_from_file_location("standalone_uninstaller", copied)
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
    def forbidden(*args, **kwargs):
        raise AssertionError("uninstall must not probe or execute renderer binaries")
    monkeypatch.setattr(installer, "environment", forbidden)
    before = {path.name: path.read_bytes() for path in prefix.iterdir() if path.is_file()}
    args = ["--prefix", str(prefix), "--uninstall"]
    assert installer.main([*args, "--dry-run"]) == 0
    assert {path.name: path.read_bytes() for path in prefix.iterdir() if path.is_file()} == before
    assert installer.main(args) == 0
    assert {path.name for path in prefix.iterdir()} == {"config.yaml", "state"}
    assert (prefix / "config.yaml").read_text() == "keep config"
    assert (prefix / "state/uuid").read_text() == "keep identity"


def test_native_executable_path_keeps_supported_characters(installer, tmp_path):
    prefix = tmp_path / "space %;`*?[]()#&é"
    assert installer.checked_prefix(str(prefix)) == prefix
    installer.install(prefix, port=8080, stage_only=True, dry_run=False, require_alsa=False)
    assert (prefix / "open-room-renderer.service").read_text() == installer.user_unit(prefix)


@pytest.mark.parametrize("quoted_config", [False, True])
def test_generated_user_unit_passes_systemd_static_verification(tmp_path, quoted_config):
    installer = load_script("install-native")
    import shutil
    analyzer = shutil.which("systemd-analyze")
    if not analyzer:
        pytest.skip("systemd-analyze not installed; no boot/service-runtime claim")
    prefix = tmp_path / "space and%specifier"
    (prefix / "venv/bin").mkdir(parents=True)
    (prefix / "venv/bin/open-room-renderer").symlink_to(sys.executable)
    unit = tmp_path / "open-room-renderer.service"
    if quoted_config:
        from fake_linkplay.native_profile import systemd_quote, user_unit
        config = tmp_path / 'config \'"%name.yaml'
        text = user_unit(prefix, config=config)
        assert f" --config {systemd_quote(config)}\n" in text
        unit.write_text(text)
    else:
        unit.write_text(installer.user_unit(prefix))
    runtime = tmp_path / "xdg"
    runtime.mkdir(mode=0o700)
    result = subprocess.run([analyzer, "--user", "verify", str(unit)], env={**os.environ, "XDG_RUNTIME_DIR": str(runtime)}, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("missing_module", ["venv", "ensurepip"])
def test_native_requires_venv_support_before_writing(installer, monkeypatch, tmp_path, missing_module):
    find_spec = installer.importlib.util.find_spec

    def missing_venv_spec(name, package=None):
        return None if name == missing_module else find_spec(name, package)

    monkeypatch.setattr(installer.importlib.util, "find_spec", missing_venv_spec)
    prefix = tmp_path / "missing-venv"
    with pytest.raises(ValueError, match="venv/ensurepip"):
        installer.install(prefix, port=8080, stage_only=False, dry_run=True, require_alsa=False)
    assert not prefix.exists()


def test_core_and_cli_defaults_high_port_with_explicit_port80_preserved(tmp_path):
    from fake_linkplay.cli import build_parser
    from fake_linkplay.config import Config
    assert Config().http_api_port == 8080 and Config().upnp_port == 59152
    assert Config(http_api_port=80, upnp_port=80).http_api_port == 80
    path = tmp_path / "minimal.yaml"
    path.write_text(f"state_dir: {tmp_path / 'state'}\n")
    assert load_config(path).http_api_port == 8080 and load_config(path).upnp_port == 59152
    path.write_text(path.read_text() + "http_api_port: 80\nupnp_port: 80\n")
    explicit = load_config(path)
    assert explicit.http_api_port == explicit.upnp_port == 80
    parser = build_parser()
    for command in (["status"], ["pause"], ["resume"], ["stop"], ["play", "https://example.invalid/audio"], ["volume", "25"]):
        assert parser.parse_args(command).port == 8080
        assert parser.parse_args([*command, "--port", "80"]).port == 80


def test_custom_missing_port_uses_high_port_without_low_port_probe(runner, monkeypatch):
    path = runner.ROOT / "custom.yaml"
    path.write_text("auto_spawn_mpv: false\nmpv_socket: /tmp/orr-unused-external.sock\n")
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    def forbidden_probe(*args):
        raise AssertionError("The high-port profile must not probe privileged ports")
    monkeypatch.setattr(runner.socket, "socket", forbidden_probe)
    assert runner.preflight(path)["auto_spawn_mpv"] is False


def test_prefix_rejects_trailing_whitespace(installer):
    for value in ("/tmp/trailing ", "/tmp/trailing /"):
        with pytest.raises(ValueError, match="end with whitespace"):
            installer.checked_prefix(value)


def test_stage_only_does_not_execute_or_reject_existing_mpv(installer, monkeypatch):
    def unexpected_mpv(*args):
        raise AssertionError("Staging must not require or execute the local mpv")
    monkeypatch.setattr(installer, "run", unexpected_mpv)
    assert installer.environment(require_mpv=False)["mpv"] is None


def test_32bit_userspace_on_arm64_kernel_is_not_claimed_as_ci(installer, monkeypatch):
    monkeypatch.setattr(installer.sys, "maxsize", 2**31 - 1)
    info = installer.environment()
    assert info["machine"] == "aarch64"
    assert info["python_bits"] == 32
    assert "32-bit ARM" in info["validation"]
    assert "untested" in info["validation"]
    assert "CI" not in info["validation"]


def test_staged_web_assets_are_allowlisted_and_all_affect_source_receipt(installer, tmp_path):
    prefix = tmp_path / "staged"
    installer.install(prefix, port=8080, stage_only=True, dry_run=False, require_alsa=False)
    snapshot = prefix / "source"
    assets = snapshot / "fake_linkplay/web_assets"
    original = installer.source_receipt(snapshot)["source_sha256"]
    for name in ("index.html", "style.css", "controller.js", "app.js", "logo.png", "tidal.html", "tidal.js"):
        path = assets / name
        body = path.read_bytes()
        path.write_bytes(body + b"\nChanged asset\n")
        assert installer.source_receipt(snapshot)["source_sha256"] != original
        path.write_bytes(body)
    for name in ("unexpected.js", "private.json", "private.txt"):
        (assets / name).write_text("not installation input")
    assert installer.source_receipt(snapshot)["source_sha256"] == original
    selected = installer.source_files(snapshot)
    assert {path.name for path in selected if path.parent == assets} == {
        "index.html", "style.css", "controller.js", "app.js", "logo.png", "tidal.html", "tidal.js"}


@pytest.mark.parametrize("replacement", ["missing", "file_symlink", "directory_symlink"])
def test_source_assets_reject_missing_files_and_symlinks(installer, tmp_path, replacement):
    prefix = tmp_path / "staged"
    installer.install(prefix, port=8080, stage_only=True, dry_run=False, require_alsa=False)
    snapshot = prefix / "source"
    assets = snapshot / "fake_linkplay/web_assets"
    if replacement == "directory_symlink":
        outside = tmp_path / "outside-assets"
        assets.rename(outside)
        assets.symlink_to(outside, target_is_directory=True)
    else:
        target = assets / "app.js"
        target.unlink()
        if replacement == "file_symlink":
            outside = tmp_path / "outside.js"
            outside.write_text("not an owned source")
            target.symlink_to(outside)
    with pytest.raises(ValueError, match="Missing regular|must not traverse symlinks"):
        installer.source_files(snapshot)
    with pytest.raises(ValueError, match="Missing regular|must not traverse symlinks"):
        installer.source_receipt(snapshot)
