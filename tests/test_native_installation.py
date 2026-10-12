"""Native profile checks: no privilege changes, service starts, or network installs."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from fake_linkplay.config import load_config

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
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/mpv")
    monkeypatch.setattr(module, "run", lambda *args: "mpv v0.37.0")
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


@pytest.mark.parametrize("version", ["mpv 0.34.1", "unknown player"])
def test_native_rejects_old_or_unknown_mpv(installer, monkeypatch, version):
    monkeypatch.setattr(installer, "run", lambda *args: version)
    with pytest.raises(ValueError, match="mpv >= 0.35"):
        installer.environment()


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
    config = load_config(first / "config.yaml")
    assert config.http_api_port == config.upnp_port == 8080
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
    installer.uninstall(first, dry_run=True)
    assert (first / "source/pyproject.toml").exists()
    installer.uninstall(first, dry_run=False)
    assert not (first / "source/pyproject.toml").exists()
    assert foreign.read_text() == "keep this"
    assert edited.is_file()
    assert (first / "state/uuid").read_text().strip() == config.uuid
    assert (first / installer.MANIFEST).is_file()  # retained with modified owned files
    assert (first / "uninstall.py").is_file()
    installer.uninstall(second, dry_run=False)
    assert (second / "state/uuid").is_file()


def test_native_refuses_existing_prefix(installer, tmp_path):
    with pytest.raises(ValueError, match="already exists"):
        installer.install(tmp_path, port=8080, stage_only=True, dry_run=False, require_alsa=False)


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


def test_portable_and_compat_runners_keep_identity(runner, monkeypatch):
    portable_path = runner.prepare("portable")
    portable = load_config(portable_path)
    assert portable.http_api_port == portable.upnp_port == 8080
    assert not portable.wiim_home_profile
    assert portable.uuid == load_config(runner.prepare("portable")).uuid
    compat = load_config(runner.prepare("app-compat"))
    assert compat.http_api_port == compat.upnp_port == 8080
    assert not compat.mirror_port_80
    assert compat.wiim_home_profile
    assert compat.uuid == load_config(runner.prepare("app-compat")).uuid
    assert compat.uuid != portable.uuid
    monkeypatch.setenv("APP_COMPAT_PORT80", "1")
    port80 = load_config(runner.prepare("app-compat"))
    assert port80.http_api_port == port80.upnp_port == 80
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
    installer.uninstall(prefix, dry_run=False)
    assert not prefix.exists()


def test_generated_unit_quotes_spaces_and_specifiers(installer):
    text = installer.user_unit(Path("/tmp/test name%value"))
    assert 'WorkingDirectory=/tmp/test name%%value' in text
    assert 'ExecStart="/tmp/test name%%value/venv/bin/open-room-renderer"' in text


def test_generated_user_unit_passes_systemd_static_verification(tmp_path):
    installer = load_script("install-native")
    import shutil
    analyzer = shutil.which("systemd-analyze")
    if not analyzer:
        pytest.skip("systemd-analyze not installed; no boot/service-runtime claim")
    prefix = tmp_path / "space and%specifier"
    (prefix / "venv/bin").mkdir(parents=True)
    (prefix / "venv/bin/open-room-renderer").symlink_to(sys.executable)
    unit = tmp_path / "open-room-renderer.service"
    unit.write_text(installer.user_unit(prefix))
    runtime = tmp_path / "xdg"
    runtime.mkdir(mode=0o700)
    result = subprocess.run([analyzer, "--user", "verify", str(unit)], env={**os.environ, "XDG_RUNTIME_DIR": str(runtime)}, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_native_requires_venv_support_before_writing(installer, monkeypatch, tmp_path):
    monkeypatch.setattr(installer.importlib.util, "find_spec", lambda name: None)
    prefix = tmp_path / "missing-venv"
    with pytest.raises(ValueError, match="venv/ensurepip"):
        installer.install(prefix, port=8080, stage_only=False, dry_run=True, require_alsa=False)
    assert not prefix.exists()


def test_core_and_cli_defaults_high_port_with_explicit_port80_preserved(tmp_path):
    from fake_linkplay.cli import build_parser
    from fake_linkplay.config import Config
    assert Config().http_api_port == Config().upnp_port == 8080
    assert Config(http_api_port=80, upnp_port=80).http_api_port == 80
    path = tmp_path / "minimal.yaml"
    path.write_text(f"state_dir: {tmp_path / 'state'}\n")
    assert load_config(path).http_api_port == load_config(path).upnp_port == 8080
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
