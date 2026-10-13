"""Offline artifacts and inert fixtures only: no account, service or audio tests."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import stat
import subprocess

import pytest

from fake_linkplay import cli
from fake_linkplay import spotify_exclusive as exclusive
from fake_linkplay.native_profile import user_unit

SECRET = "private-account-token-marker"


@pytest.fixture
def profile(tmp_path):
    # Keep below AF_UNIX's pathname limit without opening any socket.
    prefix = tmp_path / "r"
    prefix.mkdir(mode=0o700)
    for name in ("run", "state", "venv", "venv/bin"):
        (prefix / name).mkdir(mode=0o700)
    launcher = prefix / "venv/bin/open-room-renderer"
    launcher.write_text("#!/bin/sh\nexit 99\n")
    launcher.chmod(0o700)
    config = {
        "auto_spawn_mpv": True, "mpv_socket": str(prefix / "run/mpv.sock"),
        "state_dir": str(prefix / "state"), "mpv_audio_device": "alsa/hw:CARD=DAC,DEV=0",
        "http_api_port": 8080, "upnp_port": 8080, "mirror_port_80": False,
    }
    (prefix / "config.yaml").write_text(json.dumps(config))
    (prefix / "installation-receipt.json").write_text(json.dumps({"format": 1, "mode": "installed"}))
    (prefix / exclusive.RENDERER_SERVICE).write_text(user_unit(prefix))
    binary = tmp_path / "go-librespot"
    # Real ELF bytes from an inert system fixture. It is NEVER executed.
    shutil.copyfile("/usr/bin/true", binary)
    binary.chmod(0o700)
    return {
        "renderer_prefix": str(prefix), "renderer_config": str(prefix / "config.yaml"),
        "renderer_unit": str(prefix / exclusive.RENDERER_SERVICE),
        "go_librespot_binary": str(binary), "output_dir": str(tmp_path / "s"), "port": 3678,
    }


def invoke(profile, **changes):
    return exclusive.prepare_exclusive(**(profile | changes))


def config_change(profile, **changes):
    path = Path(profile["renderer_config"])
    raw = json.loads(path.read_text())
    raw.update(changes)
    path.write_text(json.dumps(raw))


def test_deterministic_private_package_and_manual_handover(profile):
    result = invoke(profile)
    root = Path(profile["output_dir"])
    first = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert set(first) == {
        "sidecar/config.yml", exclusive.SPOTIFY_SERVICE,
        exclusive.RENDERER_SERVICE + ".d/90-spotify-exclusive.conf", "MANUAL_HANDOVER.md", "receipt.json",
    }
    config = json.loads(first["sidecar/config.yml"])
    assert config["credentials"] == {"type": "device_auth"}
    assert config["audio_backend"] == "alsa" and config["audio_device"] == "hw:CARD=DAC,DEV=0"
    assert config["server"] == {"enabled": True, "address": "127.0.0.1", "port": 3678,
                                "allow_origin": "", "cert_file": "", "key_file": ""}
    assert not config["zeroconf_enabled"] and not config["mpris_enabled"]
    assert config["metadata"] == {"enabled": False, "context_sweep": False}
    assert not config["cache"]["enabled"] and config["cache"]["dir"] == str(root / "sidecar/cache")
    assert config["initial_volume"] == 0 and config["ignore_last_volume"] is True
    assert result["evidence"] == "OFFLINE_ARTIFACT_ONLY" and result["gates"] == exclusive.GATES
    assert result["audio_bypasses_renderer_eq_balance"] and not result["effective_units_inspected"]
    assert result["expected_upstream"]["binary_version_verified"] is False
    for name, sha in result["files"].items():
        assert hashlib.sha256(first[name]).hexdigest() == sha
    for path in [root, *root.rglob("*")]:
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    unit = first[exclusive.SPOTIFY_SERVICE].decode()
    assert unit.count("After=") == 1 and "After=open-room-renderer.service\n" in unit
    assert "Conflicts=open-room-renderer.service\n" in unit and "[Install]" not in unit
    assert "ExecStop" not in unit and " --config_dir " in unit
    dropin = first[exclusive.RENDERER_SERVICE + ".d/90-spotify-exclusive.conf"].decode()
    for text in (unit, dropin):
        for required in ("KillMode=control-group", "SendSIGKILL=yes", "TimeoutStopSec=45", "Restart=no"):
            assert required in text
    assert "ExecStart" not in dropin and "After=" not in dropin
    guide = first["MANUAL_HANDOVER.md"].decode()
    executable = Path(profile["renderer_prefix"]) / "venv/bin/open-room-renderer"
    assert f"\n{shlex.quote(str(executable))} spotify diagnose --host 127.0.0.1 --port 3678\n" in guide
    for required in ("disable open-room-renderer.service", "empty owned", "physical device release",
                     "defense in depth", "never sudo", "Never automatically fall back", "not legal clearance"):
        assert required.lower() in guide.lower()
    shutil.rmtree(root)  # Test-owned output only, not generator behavior.
    assert invoke(profile) == result
    assert first == {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("prefix", [
    "/opt/renderer", "/opt/renderer with spaces", "/opt/renderer%literal",
    "/opt/renderer;echo & (literal) | < > * ? [a] {b} # ~ ! `literal`",
])
def test_guide_diagnostic_uses_shell_quoted_native_executable(prefix):
    prefix = exclusive._path(prefix)
    guide = exclusive._guide(
        prefix, prefix / "config.yaml", prefix / exclusive.RENDERER_SERVICE,
        Path("/opt/client/go-librespot"), Path("/opt/proposal"), "127.0.0.2", 4567,
    )
    commands = [line for line in guide.splitlines() if " spotify diagnose " in line]
    # Parse the inert guide only; never execute its commands.
    assert len(commands) == 1
    assert shlex.split(commands[0]) == [
        str(prefix / "venv/bin/open-room-renderer"), "spotify", "diagnose",
        "--host", "127.0.0.2", "--port", "4567",
    ]


def test_migrated_profile_is_explicit_and_separate(profile):
    prefix = Path(profile["renderer_prefix"])
    migrated = prefix / "migrated-profile"
    migrated.mkdir(mode=0o700)
    (migrated / "state").mkdir(mode=0o700)
    config = json.loads(Path(profile["renderer_config"]).read_text())
    config["state_dir"] = str(migrated / "state")
    (migrated / "config.yaml").write_text(json.dumps(config))
    unit = migrated / exclusive.RENDERER_SERVICE
    unit.write_text(user_unit(prefix, config=migrated / "config.yaml"))
    invoke(profile, renderer_config=str(migrated / "config.yaml"), renderer_unit=str(unit))


def test_no_network_process_device_or_saved_state_operations(profile, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline generator attempted a forbidden runtime operation")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    original = os.open
    opened = []
    def audited(path, flags, *args, **kwargs):
        opened.append(str(path))
        assert not str(path).startswith("/dev/") and not str(path).endswith(".sock")
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", audited)
    state = Path(profile["renderer_prefix"]) / "state/private-token"
    state.write_text(SECRET)
    assert invoke(profile)["evidence"] == "OFFLINE_ARTIFACT_ONLY"
    assert not any("private-token" in path for path in opened)
    assert state.read_text() == SECRET


@pytest.mark.parametrize("route", [None, "", "auto", "alsa/default", "alsa/dmix", "alsa/plughw:CARD=DAC,DEV=0",
    "pulse", "pipewire", "null", "alsa/hw:0,0", "alsa/hw:CARD=DAC,DEV=00", "alsa/hw:CARD=DAC,DEV=0,SUBDEV=0",
    "alsa/hw:CARD=DAC,DEV=0\nExecStart=" + SECRET, "alsa/hw:CARD=DAC,DEV=-1"])
def test_rejects_nonexclusive_audio_routes(profile, route):
    config_change(profile, mpv_audio_device=route)
    with pytest.raises(exclusive.ExclusiveProfileError):
        invoke(profile)
    assert not Path(profile["output_dir"]).exists()


@pytest.mark.parametrize("change", [
    {"auto_spawn_mpv": False}, {"auto_spawn_mpv": "true"}, {"mpv_socket": "/tmp/mpv.sock"},
    {"state_dir": "/tmp/state"}, {"mirror_port_80": True}, {"http_api_port": 80},
    {"upnp_port": 3678}, {"unknown_secret": SECRET},
    {"podcast_presets": [{"name": "n", "url": "u", "secret": SECRET}]},
    {"experimental_sources": [{"name": "n", "unknown": SECRET}]},
])
def test_rejects_unsupported_renderer_settings(profile, change):
    config_change(profile, **change)
    with pytest.raises(exclusive.ExclusiveProfileError) as error:
        invoke(profile)
    assert SECRET not in str(error.value) and not Path(profile["output_dir"]).exists()


@pytest.mark.parametrize("body", [
    "auto_spawn_mpv: true\nauto_spawn_mpv: false", "server: {x: 1, x: 2}",
    "a: &a [1]\nb: *a", "a: [" * 18 + "]" * 18,
    "!!python/object/apply:os.system [echo bad]", "x: !!int " + SECRET,
    "[1, 2, 3]", "x: 1\n---\ny: 2", "x: " + "a" * (exclusive.MAX_INPUT + 1),
])
def test_rejects_malformed_duplicate_or_complex_yaml_privately(profile, body):
    Path(profile["renderer_config"]).write_text(body)
    with pytest.raises(exclusive.ExclusiveProfileError) as error:
        invoke(profile)
    assert SECRET not in str(error.value) and not Path(profile["output_dir"]).exists()


@pytest.mark.parametrize("changes", [
    {"host": "localhost"}, {"host": "0.0.0.0"}, {"host": "192.0.2.1"}, {"host": "::ffff:127.0.0.1"},
    {"host": "::1"}, {"host": "::1%lo"}, {"host": "127.000.0.1"}, {"host": "http://" + SECRET},
    {"port": 0}, {"port": 80}, {"port": True}, {"port": 65536}, {"port": 8080},
])
def test_rejects_host_and_port_ambiguity(profile, changes):
    with pytest.raises(exclusive.ExclusiveProfileError) as error:
        invoke(profile, **changes)
    assert SECRET not in str(error.value) and not Path(profile["output_dir"]).exists()


def test_accepts_canonical_ipv4_loopback(profile):
    invoke(profile, host="127.0.0.2")
    assert json.loads((Path(profile["output_dir"]) / "sidecar/config.yml").read_text())["server"]["address"] == "127.0.0.2"


@pytest.mark.parametrize("suffix", ["/../escaped", "/$HOME", "/back\\slash", "/x\n" + SECRET, "/trailing "])
def test_rejects_unsafe_output_paths(profile, suffix):
    with pytest.raises(exclusive.ExclusiveProfileError) as error:
        invoke(profile, output_dir=str(Path(profile["output_dir"]).parent) + suffix)
    assert SECRET not in str(error.value) and not Path(profile["output_dir"]).exists()


def test_rejects_existing_output_and_path_collisions(profile):
    output = Path(profile["output_dir"])
    output.mkdir()
    sentinel = output / "keep"
    sentinel.write_text(SECRET)
    with pytest.raises(exclusive.ExclusiveProfileError):
        invoke(profile)
    assert sentinel.read_text() == SECRET and list(output.iterdir()) == [sentinel]
    for path in (profile["renderer_prefix"], str(Path(profile["renderer_prefix"]) / "new"),
                 profile["go_librespot_binary"], str(output.parent)):
        with pytest.raises(exclusive.ExclusiveProfileError):
            invoke(profile, output_dir=path)


@pytest.mark.parametrize("target", ["renderer_config", "renderer_unit", "go_librespot_binary", "output_dir"])
def test_rejects_leaf_symlinks(profile, target):
    path = Path(profile[target])
    other = path.with_name(path.name + "-real")
    if path.exists():
        path.rename(other)
    else:
        other.mkdir()
    path.symlink_to(other)
    with pytest.raises(exclusive.ExclusiveProfileError):
        invoke(profile)
    assert path.is_symlink()


def test_rejects_ancestor_symlink_and_special_input(profile, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    with pytest.raises(exclusive.ExclusiveProfileError):
        invoke(profile, output_dir=str(link / "new"))
    path = Path(profile["renderer_config"])
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(exclusive.ExclusiveProfileError):
        invoke(profile)


@pytest.mark.parametrize("target", [
    "config.yaml", exclusive.RENDERER_SERVICE, "installation-receipt.json",
    "venv/bin/open-room-renderer", "go-librespot",
])
@pytest.mark.parametrize("kind", [
    stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFDIR, stat.S_IFLNK,
])
def test_refuses_existing_special_input_before_open(profile, monkeypatch, target, kind):
    # Synthetic metadata only: never create or open a real device or socket.
    path = (Path(profile["go_librespot_binary"]) if target == "go-librespot"
            else Path(profile["renderer_prefix"]) / target)
    real_stat, real_open = os.stat, os.open
    values = list(real_stat(path))
    values[0] = kind | 0o600
    synthetic = os.stat_result(values)
    checked = []

    def metadata(name, *args, **kwargs):
        if name == path.name and kwargs.get("dir_fd") is not None:
            assert kwargs.get("follow_symlinks") is False
            checked.append(name)
            return synthetic
        return real_stat(name, *args, **kwargs)

    def guarded_open(name, flags, *args, **kwargs):
        if name == path.name and kwargs.get("dir_fd") is not None:
            pytest.fail("non-regular input was opened before rejection")
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "stat", metadata)
    monkeypatch.setattr(os, "open", guarded_open)
    with pytest.raises(exclusive.ExclusiveProfileError) as error:
        invoke(profile)
    assert str(error.value) == "Inputs must be owned regular files, without group/other write access or symlinks."
    assert checked == [path.name]
    assert not Path(profile["output_dir"]).exists()


@pytest.mark.parametrize("binary", [False, True])
@pytest.mark.parametrize("mode", [stat.S_IFIFO | 0o600, stat.S_IFREG | 0o666])
def test_rechecks_opened_input_type_and_permissions(tmp_path, monkeypatch, binary, mode):
    path = tmp_path / "inert-input"
    path.write_bytes(b"\x7fELF")
    path.chmod(0o700)
    real_fstat = os.fstat
    opened = []

    def changed_metadata(fd):
        opened.append(fd)
        values = list(real_fstat(fd))
        values[0] = mode
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", changed_metadata)
    with pytest.raises(exclusive.ExclusiveProfileError, match="regular files"):
        exclusive._read(path, binary=binary)
    assert len(opened) == 1
    with pytest.raises(OSError):
        real_fstat(opened[0])  # The rejected descriptor is still closed.


def test_rejects_custom_units_uninstalled_profiles_and_wrappers(profile):
    unit = Path(profile["renderer_unit"])
    original = unit.read_text()
    unit.write_text(original + "\n# custom\n")
    with pytest.raises(exclusive.ExclusiveProfileError, match="customized"):
        invoke(profile)
    unit.write_text(original)
    receipt = Path(profile["renderer_prefix"]) / "installation-receipt.json"
    receipt.write_text('{"format":1,"mode":"staged-only"}')
    with pytest.raises(exclusive.ExclusiveProfileError, match="completed native"):
        invoke(profile)
    receipt.write_text('{"format":1,"mode":"installed"}')
    Path(profile["go_librespot_binary"]).write_text("#!/bin/sh\n" + SECRET)
    with pytest.raises(exclusive.ExclusiveProfileError, match="ELF"):
        invoke(profile)


def test_rejects_nonprivate_native_directories_and_writable_input(profile):
    prefix = Path(profile["renderer_prefix"])
    prefix.chmod(0o755)
    with pytest.raises(exclusive.ExclusiveProfileError, match="0700"):
        invoke(profile)
    prefix.chmod(0o700)
    Path(profile["renderer_config"]).chmod(0o666)
    with pytest.raises(exclusive.ExclusiveProfileError, match="regular files"):
        invoke(profile)


def test_private_cli_errors_and_dispatch(profile, capsys):
    args = ["spotify", "prepare-exclusive"]
    for name, value in profile.items():
        args += ["--" + name.replace("_", "-"), str(value)]
    assert cli.main(args) == 0
    assert "OFFLINE_ARTIFACT_ONLY" in capsys.readouterr().out
    for extra in (["--token", SECRET], ["--port", "3679"], ["--host", SECRET, "--host", SECRET]):
        with pytest.raises(SystemExit) as error:
            cli.main(args + extra)
        assert error.value.code == 2 and SECRET not in capsys.readouterr().err
    assert cli.main(args) == 2  # Existing destination is never overwritten.
    captured = capsys.readouterr()
    assert not captured.out and "Traceback" not in captured.err


def test_static_systemd_verify_with_inert_binary(profile, tmp_path):
    analyzer = shutil.which("systemd-analyze")
    if not analyzer:
        pytest.skip("static analyzer unavailable; no runtime claim")
    # Exercise command argument escaping for space, percent and quote characters.
    binary = tmp_path / 'bin %literal'
    binary.mkdir()
    fixture = binary / "go-librespot"
    shutil.copyfile("/usr/bin/true", fixture)
    fixture.chmod(0o700)
    output = tmp_path / 'out %"literal'
    invoke(profile, go_librespot_binary=str(fixture), output_dir=str(output))
    text = (output / exclusive.SPOTIFY_SERVICE).read_text()
    assert '%%literal/go-librespot" --config_dir ' in text
    assert 'out %%\\"literal/sidecar"' in text
    shutil.copyfile(profile["renderer_unit"], output / exclusive.RENDERER_SERVICE)
    runtime = tmp_path / "xdg"
    runtime.mkdir(mode=0o700)
    result = subprocess.run([analyzer, "--user", "verify", str(output / exclusive.SPOTIFY_SERVICE),
                             str(output / exclusive.RENDERER_SERVICE)],
                            env={**os.environ, "XDG_RUNTIME_DIR": str(runtime)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("target", ["renderer_prefix", "renderer_config", "renderer_unit", "go_librespot_binary", "output_dir"])
def test_rejects_double_root_linux_path_alias(profile, target):
    # POSIX pathlib preserves // while Linux resolves it to /; never use that
    # discrepancy to bypass prefix/output collision guards.
    changes = {target: "/" + profile[target]}
    if target == "output_dir":
        changes[target] = "/" + profile["renderer_prefix"] + "/unexpected-sidecar"
    with pytest.raises(exclusive.ExclusiveProfileError):
        invoke(profile, **changes)
    assert not (Path(profile["renderer_prefix"]) / "unexpected-sidecar").exists()
    assert not Path(profile["output_dir"]).exists()


@pytest.mark.parametrize("target", ["renderer_prefix", "go_librespot_binary"])
def test_rejects_systemd_unsafe_executable_path_before_read(profile, target):
    with pytest.raises(exclusive.ExclusiveProfileError, match="Executable paths"):
        invoke(profile, **{target: profile[target] + '\"literal'})
    assert not Path(profile["output_dir"]).exists()


def test_native_installer_snapshot_includes_offline_modules():
    import importlib.util
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("exclusive_native_snapshot", root / "scripts/install-native.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    paths = {str(path.relative_to(root)) for path in installer.source_files(root)}
    assert {"fake_linkplay/spotify_exclusive.py", "fake_linkplay/native_profile.py",
            "fake_linkplay/spotify_diagnostic.py", "fake_linkplay/cli.py"} <= paths


@pytest.mark.parametrize("target", ["config.yaml", "installation-receipt.json"])
@pytest.mark.parametrize("tag,value", [(tag, SECRET) for tag in ["bool", "int", "float", "timestamp"]] + [("int", '""'), ("float", '""')])
def test_cli_redacts_malformed_explicit_yaml_scalar_tags(profile, target, tag, value, capsys):
    (Path(profile["renderer_prefix"]) / target).write_text("x: !!" + tag + " " + value)
    args = ["spotify", "prepare-exclusive"]
    for name, value in profile.items():
        args += ["--" + name.replace("_", "-"), str(value)]
    assert cli.main(args) == 2
    captured = capsys.readouterr()
    assert not captured.out and SECRET not in captured.err and "Traceback" not in captured.err
    assert not Path(profile["output_dir"]).exists()
