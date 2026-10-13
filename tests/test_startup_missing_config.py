"""Leaf-only missing-config policy, without a loop, state, sockets or services."""
from __future__ import annotations

from dataclasses import asdict
import errno
import importlib.util
from pathlib import Path
import socket
import subprocess
import sys
from unittest.mock import Mock

import pytest

from fake_linkplay import cli, config


ROOT = Path(__file__).resolve().parents[1]
SECRET = "synthetic-private-config-target"
NOT_FOUND = "Configuration file not found. Check --config or ORR_CONFIG."
UNREADABLE = "Configuration file could not be read. Check the path and read permissions."
INTENT = (
    "bind_address: 127.0.0.1\n"
    "auto_spawn_mpv: false\n"
    "enable_ssdp: false\n"
    "default_volume: 0\n"
    "default_mute: true\n"
)


def _step_before_first_await(coroutine):
    """Exercise the real CLI error boundary without asyncio's socketpair."""
    try:
        coroutine.send(None)
    finally:
        coroutine.close()
    raise AssertionError("startup unexpectedly reached an await")


@pytest.fixture(autouse=True)
def no_startup_effects(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    forbidden = Mock(side_effect=AssertionError("startup side effect reached"))
    monkeypatch.setattr(config, "_load_config_state", forbidden)
    monkeypatch.setattr(config, "detect_lan_ip", forbidden)
    monkeypatch.setattr(cli, "setup_logging", forbidden)
    monkeypatch.setattr(cli.RendererState, "from_config", forbidden)
    for name in ("MpvBackend", "HttpApi", "SsdpServer", "MdnsAdvertiser"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(cli.web, "AppRunner", forbidden)
    monkeypatch.setattr(cli.web, "TCPSite", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "socketpair", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(cli.asyncio, "run", _step_before_first_await)
    yield forbidden
    forbidden.assert_not_called()
    assert not (tmp_path / ".state").exists()


def _assert_intent(cfg):
    assert cfg.bind_address == "127.0.0.1"
    assert cfg.auto_spawn_mpv is False
    assert cfg.enable_ssdp is False
    assert cfg.default_volume == 0
    assert cfg.default_mute is True
    assert cfg.uuid is None


def _assert_private_error(caught, message):
    assert str(caught.value) == message
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    assert SECRET not in str(caught.value)


def _dangling_config(tmp_path):
    path = tmp_path / "configs/config.example.yaml"
    path.parent.mkdir()
    path.symlink_to(tmp_path / f"{SECRET}.yaml")
    return path


@pytest.mark.parametrize("symlink", [False, True], ids=["regular", "symlink"])
@pytest.mark.parametrize("missing_ok", [False, True])
def test_readable_config_preserves_intent_without_inspection(tmp_path, monkeypatch, symlink, missing_ok):
    target = tmp_path / f"{SECRET}.yaml"
    target.write_text(INTENT)
    path = target
    if symlink:
        path = tmp_path / "selected.yaml"
        path.symlink_to(target)
    inspect = Mock(side_effect=AssertionError("successful read must not inspect the leaf"))
    with monkeypatch.context() as guarded:
        guarded.setattr(Path, "lstat", inspect)
        _assert_intent(config.read_startup_config(path, missing_ok=missing_ok))
    inspect.assert_not_called()
    assert target.read_text() == INTENT


@pytest.mark.parametrize("missing_parents", [False, True], ids=["missing-leaf", "missing-parents"])
def test_only_confirmed_absence_keeps_implicit_defaults(tmp_path, monkeypatch, missing_parents):
    path = tmp_path / "absent.yaml"
    if missing_parents:
        path = tmp_path / "absent/parents/config.yaml"
    real_read = Path.read_text
    real_lstat = Path.lstat
    calls = []

    def read(selected, **kwargs):
        calls.append(("read", selected))
        return real_read(selected, **kwargs)

    def inspect(selected):
        calls.append(("lstat", selected))
        return real_lstat(selected)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "lstat", inspect)
    assert asdict(config.read_startup_config(path, missing_ok=True)) == asdict(config.Config())
    assert calls == [("read", path), ("lstat", path)]


def test_dangling_selected_leaf_is_not_replaced_with_defaults(tmp_path):
    path = _dangling_config(tmp_path)
    before = path.lstat()
    with pytest.raises(config.StartupConfigError) as caught:
        config.read_startup_config(path, missing_ok=True)
    _assert_private_error(caught, UNREADABLE)
    assert path.is_symlink()
    assert path.lstat() == before
    assert not path.readlink().exists()


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO], ids=["permission", "io"])
def test_failed_leaf_inspection_is_private_and_does_not_retry(tmp_path, monkeypatch, error_number):
    path = tmp_path / f"{SECRET}.yaml"
    calls = []

    def read(selected, **kwargs):
        assert kwargs == {"encoding": "utf-8"}
        calls.append(("read", selected))
        raise FileNotFoundError(errno.ENOENT, SECRET, str(selected))

    def inspect(selected):
        calls.append(("lstat", selected))
        raise OSError(error_number, SECRET, str(selected))

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "lstat", inspect)
    with pytest.raises(config.StartupConfigError) as caught:
        config.read_startup_config(path, missing_ok=True)
    _assert_private_error(caught, UNREADABLE)
    assert calls == [("read", path), ("lstat", path)]


@pytest.mark.parametrize("entry", ["file", "directory", "symlink"])
def test_entry_appearing_after_failed_read_is_not_reread_or_defaulted(tmp_path, monkeypatch, entry):
    path = tmp_path / f"{SECRET}.yaml"
    real_lstat = Path.lstat
    calls = []

    def read(selected, **kwargs):
        calls.append(("read", selected))
        if entry == "file":
            selected.write_text(INTENT)
        elif entry == "directory":
            selected.mkdir()
        else:
            selected.symlink_to(tmp_path / "absent-target.yaml")
        raise FileNotFoundError(errno.ENOENT, SECRET, str(selected))

    def inspect(selected):
        calls.append(("lstat", selected))
        return real_lstat(selected)

    with monkeypatch.context() as guarded:
        guarded.setattr(Path, "read_text", read)
        guarded.setattr(Path, "lstat", inspect)
        with pytest.raises(config.StartupConfigError) as caught:
            config.read_startup_config(path, missing_ok=True)
    _assert_private_error(caught, UNREADABLE)
    assert calls == [("read", path), ("lstat", path)]
    if entry == "file":
        assert path.read_text() == INTENT
    elif entry == "directory":
        assert path.is_dir()
    else:
        assert path.is_symlink()


@pytest.mark.parametrize("missing_ok", [False, True])
@pytest.mark.parametrize("document", [INTENT, ""])
def test_captured_text_never_constructs_or_inspects_a_path(monkeypatch, missing_ok, document):
    forbidden = Mock(side_effect=AssertionError("captured input accessed a path"))
    monkeypatch.setattr(config, "Path", forbidden)
    cfg = config.read_startup_config(SECRET, missing_ok=missing_ok, captured_text=document)
    if document:
        _assert_intent(cfg)
    else:
        assert asdict(cfg) == asdict(config.Config())
    forbidden.assert_not_called()


@pytest.mark.parametrize("dangling", [False, True], ids=["absent", "dangling"])
def test_explicit_missing_file_keeps_existing_diagnostic_without_inspection(tmp_path, monkeypatch, dangling):
    path = _dangling_config(tmp_path) if dangling else tmp_path / f"{SECRET}.yaml"
    inspect = Mock(side_effect=AssertionError("explicit failure must not inspect the leaf"))
    monkeypatch.setattr(Path, "lstat", inspect)
    with pytest.raises(config.StartupConfigError) as caught:
        config.read_startup_config(path)
    _assert_private_error(caught, NOT_FOUND)
    inspect.assert_not_called()


@pytest.mark.parametrize("failure,message", [
    (IsADirectoryError, "Configuration path is a directory. Select a YAML file."),
    (PermissionError, UNREADABLE),
    (OSError, UNREADABLE),
    (UnicodeError, "Configuration text could not be decoded. Save the YAML file as UTF-8."),
])
def test_other_read_errors_keep_existing_diagnostics_without_inspection(monkeypatch, failure, message):
    reader = Mock(side_effect=failure(SECRET))
    inspect = Mock(side_effect=AssertionError("non-missing failure must not inspect the leaf"))
    monkeypatch.setattr(Path, "read_text", reader)
    monkeypatch.setattr(Path, "lstat", inspect)
    with pytest.raises(config.StartupConfigError) as caught:
        config.read_startup_config(SECRET, missing_ok=True)
    _assert_private_error(caught, message)
    reader.assert_called_once_with(encoding="utf-8")
    inspect.assert_not_called()


@pytest.mark.parametrize("mode", ["implicit", "explicit"])
def test_cli_dangling_leaf_fails_privately_before_any_startup_effect(tmp_path, capsys, mode):
    path = _dangling_config(tmp_path)
    args = ["serve"] if mode == "implicit" else ["serve", "--config", str(path)]
    assert cli.main(args) == 2
    captured = capsys.readouterr()
    expected = UNREADABLE if mode == "implicit" else NOT_FOUND
    assert captured.out == ""
    assert captured.err == f"Startup error: {expected}\n"
    assert SECRET not in captured.err and "Traceback" not in captured.err
    assert path.is_symlink()


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
def test_cli_leaf_inspection_errors_use_fixed_stderr_before_hydration(tmp_path, monkeypatch, capsys, error_number):
    inspect = Mock(side_effect=OSError(error_number, SECRET, str(tmp_path / SECRET)))
    monkeypatch.setattr(Path, "lstat", inspect)
    assert cli.main(["serve"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"Startup error: {UNREADABLE}\n"
    inspect.assert_called_once_with()


@pytest.mark.parametrize("kind", ["absent", "regular", "symlink"])
@pytest.mark.parametrize("explicit", [False, True])
def test_cli_config_selection_keeps_intent_and_true_absence_rules(tmp_path, monkeypatch, kind, explicit):
    default = tmp_path / "configs/config.example.yaml"
    default.parent.mkdir()
    path = tmp_path / "chosen.yaml" if explicit else default
    if kind != "absent":
        if kind == "regular":
            path.write_text(INTENT)
        else:
            target = tmp_path / f"{SECRET}.yaml"
            target.write_text(INTENT)
            path.symlink_to(target)
    # ORR_CONFIG belongs to the profile wrapper; direct serve keeps its own selection.
    monkeypatch.setenv("ORR_CONFIG", str(tmp_path / "unselected.yaml"))
    selected = []

    class AtHydration(Exception):
        pass

    def hydrate(cfg, **kwargs):
        assert kwargs == {"_startup": True}
        selected.append(cfg)
        raise AtHydration

    monkeypatch.setattr(config, "_load_config_state", hydrate)
    args = ["serve", "--config", str(path)] if explicit else ["serve"]
    if explicit and kind == "absent":
        assert cli.main(args) == 2
        assert selected == []
    else:
        with pytest.raises(AtHydration):
            cli.main(args)
        assert len(selected) == 1
        if kind == "absent":
            assert asdict(selected[0]) == asdict(config.Config())
        else:
            _assert_intent(selected[0])


def _runner():
    spec = importlib.util.spec_from_file_location("missing_config_run_profile", ROOT / "scripts/run-profile.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


@pytest.mark.parametrize("profile", ["portable", "app-compat"])
@pytest.mark.parametrize("dangling", [False, True], ids=["absent", "dangling"])
def test_orr_config_wrapper_remains_explicit_and_fails_before_probes(tmp_path, monkeypatch, capsys, no_startup_effects, profile, dangling):
    runner = _runner()
    path = _dangling_config(tmp_path) if dangling else tmp_path / f"{SECRET}.yaml"
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setenv("ORR_CONFIG", str(path))
    monkeypatch.setattr(sys, "argv", ["run-profile.py", profile])
    monkeypatch.setattr(runner.shutil, "which", no_startup_effects)
    monkeypatch.setattr(runner.os, "execv", no_startup_effects)
    assert runner.prepare(profile) == path
    assert runner.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"Startup error: {NOT_FOUND}\n"


@pytest.mark.parametrize("profile", ["portable", "app-compat"])
@pytest.mark.parametrize("selection", ["regular", "symlink", "generated"])
def test_wrapper_readable_and_generated_profiles_keep_explicit_exec(
    tmp_path, monkeypatch, capsys, no_startup_effects, profile, selection,
):
    runner = _runner()
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["run-profile.py", profile])
    monkeypatch.setattr(runner.shutil, "which", no_startup_effects)
    monkeypatch.delenv("ORR_CONFIG", raising=False)
    for name in ("APP_COMPAT_PORT80", "APP_COMPAT_FRESH", "APP_COMPAT_NAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WIIM_HOME_ACOUSTIC_CAPABILITY_MODE", "peq")
    monkeypatch.setenv("WIIM_HOME_AUDIO_OUTPUT_CAPABILITY_MODE", "fail")
    source = INTENT + f"state_dir: wrapper-state\nmpv_socket: {tmp_path / 'ipc.sock'}\n"
    if selection == "generated":
        template = tmp_path / "configs" / (
            "config.app-compat.yaml" if profile == "app-compat" else "config.example.yaml"
        )
        template.parent.mkdir()
        template.write_text(source)
        selected = tmp_path / "wrapper-state/runner-config.yaml"
    else:
        selected = tmp_path / "selected.yaml"
        if selection == "regular":
            selected.write_text(source)
        else:
            target = tmp_path / f"{SECRET}.yaml"
            target.write_text(source)
            selected.symlink_to(target)
        monkeypatch.setenv("ORR_CONFIG", str(selected))
    calls = []

    class AtExec(Exception):
        pass

    def execv(executable, arguments):
        calls.append((executable, arguments))
        _assert_intent(config.read_startup_config(selected))
        raise AtExec

    monkeypatch.setattr(runner.os, "execv", execv)
    with pytest.raises(AtExec):
        runner.main()
    executable = str(tmp_path / ".venv/bin/open-room-renderer")
    assert calls == [(executable, [executable, "serve", "--config", str(selected)])]
    assert capsys.readouterr().err == ""
    if selection == "generated":
        assert list((tmp_path / "wrapper-state").iterdir()) == [selected]


@pytest.mark.parametrize("command", ["loader", "print-description", "print-discovery-info"])
@pytest.mark.parametrize("dangling", [False, True], ids=["absent", "dangling"])
def test_legacy_loader_and_print_commands_keep_missing_fallback_without_state(tmp_path, monkeypatch, capsys, command, dangling):
    path = _dangling_config(tmp_path) if dangling else tmp_path / "configs/config.example.yaml"
    hydrated = []

    def hydrate(cfg, **kwargs):
        assert kwargs == {}
        assert asdict(cfg) == asdict(config.Config())
        hydrated.append(cfg)
        cfg.uuid = "00000000-0000-4000-8000-000000000000"
        cfg.advertise_ip = "192.0.2.1"
        return cfg

    monkeypatch.setattr(config, "_load_config_state", hydrate)
    startup_reader = Mock(side_effect=AssertionError("legacy path used startup reader"))
    monkeypatch.setattr(config, "read_startup_config", startup_reader)
    if command == "loader":
        assert config.load_config(path) is hydrated[0]
    else:
        assert cli.main([command]) == 0
        assert capsys.readouterr().err == ""
    assert len(hydrated) == 1
    startup_reader.assert_not_called()
