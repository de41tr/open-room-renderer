"""Private startup diagnostics without native backends, sockets, or listeners."""
from __future__ import annotations

from dataclasses import asdict
import importlib.util
import logging
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from unittest.mock import Mock

import pytest
import yaml

from fake_linkplay import cli, config
from fake_linkplay.state import RendererState
from fake_linkplay.utils import setup_logging

ROOT = Path(__file__).resolve().parents[1]
SECRET = "synthetic-private-token"
SECRET_URL = f"https://user:password@media.example.test/audio?token={SECRET}"
STRUCTURE = config.CONFIG_STRUCTURE_ERROR
YAML_ERROR = config.CONFIG_YAML_ERROR

INVALID_DOCUMENTS = [
    pytest.param(f'friendly_name: "{SECRET_URL}\n', YAML_ERROR, id="yaml-syntax"),
    pytest.param(f'friendly_name: !unknown {SECRET_URL}\n', YAML_ERROR, id="yaml-tag"),
    pytest.param(f'default_volume: !!int {SECRET}\n', YAML_ERROR, id="yaml-int-tag"),
    pytest.param(f'friendly_name: !!bool {SECRET}\n', YAML_ERROR, id="yaml-bool-tag"),
    pytest.param(f'channel_balance: !!float {SECRET}\n', YAML_ERROR, id="yaml-float-tag"),
    pytest.param(f'friendly_name: !!timestamp {SECRET}\n', YAML_ERROR, id="yaml-timestamp-tag"),
    pytest.param('friendly_name: !!timestamp 2026-99-99\n', YAML_ERROR, id="yaml-timestamp-range"),
    pytest.param('default_volume: !!int ""\n', YAML_ERROR, id="yaml-empty-int-tag"),
    pytest.param('default_volume: !!int "+"\n', YAML_ERROR, id="yaml-sign-int-tag"),
    pytest.param('channel_balance: !!float ""\n', YAML_ERROR, id="yaml-empty-float-tag"),
    pytest.param(yaml.safe_dump(SECRET_URL), STRUCTURE, id="scalar"),
    pytest.param(yaml.safe_dump([SECRET_URL]), STRUCTURE, id="sequence"),
    pytest.param("[]\n", STRUCTURE, id="empty-sequence"),
    pytest.param("false\n", STRUCTURE, id="false-scalar"),
    pytest.param("0\n", STRUCTURE, id="zero-scalar"),
    pytest.param(yaml.safe_dump({SECRET_URL: True}), STRUCTURE, id="unknown-setting"),
    pytest.param(yaml.safe_dump({"_implicit_discovery_settings": [SECRET_URL]}), STRUCTURE, id="internal-provenance"),
    pytest.param(yaml.safe_dump({1: SECRET_URL}), STRUCTURE, id="non-string-key"),
    pytest.param(yaml.safe_dump({"podcast_presets": SECRET_URL}), STRUCTURE, id="presets-shape"),
    pytest.param(yaml.safe_dump({"podcast_presets": [{"url": SECRET_URL}]}), STRUCTURE, id="preset-missing-name"),
    pytest.param(yaml.safe_dump({"experimental_sources": [SECRET_URL]}), STRUCTURE, id="source-shape"),
    pytest.param(yaml.safe_dump({"experimental_sources": [{"id": SECRET_URL, "extra": SECRET}]}), STRUCTURE, id="source-fields"),
    pytest.param(yaml.safe_dump({"uuid": [SECRET_URL]}), STRUCTURE, id="uuid-construction"),
    pytest.param(yaml.safe_dump({"discovery_interface": SECRET_URL}), STRUCTURE, id="existing-interface-check"),
    pytest.param(yaml.safe_dump({"native_queue_staging": SECRET_URL}), STRUCTURE, id="existing-staging-check"),
    pytest.param(yaml.safe_dump({"default_mute": SECRET_URL}), STRUCTURE, id="existing-mute-check"),
    pytest.param(yaml.safe_dump({"default_volume": SECRET_URL}), STRUCTURE, id="volume-conversion"),
    pytest.param("default_volume: .inf\n", STRUCTURE, id="volume-overflow"),
    pytest.param(yaml.safe_dump({"log_level": [SECRET_URL]}), STRUCTURE, id="logging-conversion"),
    pytest.param("log_level: BASIC_FORMAT\n", STRUCTURE, id="logging-non-level-attribute"),
]


def runner_module():
    spec = importlib.util.spec_from_file_location("startup_run_profile", ROOT / "scripts/run-profile.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_process(tmp_path, path, *, wrapper=False):
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    if wrapper:
        env["ORR_CONFIG"] = str(path)
        argv = [sys.executable, str(ROOT / "scripts/run-profile.py"), "portable"]
    else:
        argv = [sys.executable, "-m", "fake_linkplay.cli", "serve", "--config", str(path)]
    return subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)


def assert_private_failure(result, message):
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == f"Startup error: {message}\n"
    assert SECRET not in result.stderr and SECRET_URL not in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("document,message", INVALID_DOCUMENTS)
def test_real_cli_and_wrapper_report_same_private_config_errors(tmp_path, document, message):
    path = tmp_path / f"{SECRET}.yaml"
    path.write_text(document)
    for wrapper in (False, True):
        assert_private_failure(run_process(tmp_path, path, wrapper=wrapper), message)
    assert not (tmp_path / ".state").exists()
    assert path.read_text() == document


@pytest.mark.parametrize("kind,message", [
    ("missing", "Configuration file not found. Check --config or ORR_CONFIG."),
    ("directory", "Configuration path is a directory. Select a YAML file."),
    ("unreadable", "Configuration file could not be read. Check the path and read permissions."),
    ("encoding", "Configuration text could not be decoded. Save the YAML file as UTF-8."),
])
def test_real_cli_and_wrapper_reject_bad_files_without_private_path(tmp_path, kind, message):
    path = tmp_path / f"{SECRET}.yaml"
    if kind == "directory":
        path.mkdir()
    elif kind == "unreadable":
        if os.geteuid() == 0:
            pytest.skip("POSIX unreadable-file check requires a non-root process")
        path.write_text(f"# {SECRET_URL}\n")
        path.chmod(0)
    elif kind == "encoding":
        path.write_bytes(b"\xff" + SECRET_URL.encode())
    try:
        for wrapper in (False, True):
            assert_private_failure(run_process(tmp_path, path, wrapper=wrapper), message)
    finally:
        if kind == "unreadable":
            path.chmod(0o600)
    assert not (tmp_path / ".state").exists()


@pytest.mark.parametrize("document,message", INVALID_DOCUMENTS)
async def test_bad_config_stops_before_identity_logging_backend_or_listener(
    tmp_path, monkeypatch, document, message,
):
    path = tmp_path / "bad.yaml"
    path.write_text(document)
    monkeypatch.chdir(tmp_path)
    forbidden = Mock(side_effect=AssertionError("startup side effect reached"))
    monkeypatch.setattr(config, "_load_config_state", forbidden)
    monkeypatch.setattr(cli, "setup_logging", forbidden)
    monkeypatch.setattr(cli.RendererState, "from_config", forbidden)
    monkeypatch.setattr(cli, "MpvBackend", forbidden)
    monkeypatch.setattr(cli.web, "AppRunner", forbidden)
    monkeypatch.setattr(cli.web, "TCPSite", forbidden)
    monkeypatch.setattr(cli, "SsdpServer", forbidden)
    monkeypatch.setattr(cli, "MdnsAdvertiser", forbidden)
    with pytest.raises(config.StartupConfigError) as caught:
        await cli.serve(str(path))
    assert str(caught.value) == message
    assert caught.value.__suppress_context__ or caught.value.__context__ is None
    forbidden.assert_not_called()
    assert not (tmp_path / ".state").exists()


@pytest.mark.parametrize("failure", [FileNotFoundError, IsADirectoryError, PermissionError, OSError])
async def test_failed_config_read_is_single_attempt_before_any_state(tmp_path, monkeypatch, failure):
    reader = Mock(side_effect=failure(SECRET_URL))
    monkeypatch.setattr(Path, "read_text", reader)
    forbidden = Mock(side_effect=AssertionError("state or backend reached"))
    monkeypatch.setattr(config, "_load_config_state", forbidden)
    monkeypatch.setattr(cli, "MpvBackend", forbidden)
    with pytest.raises(config.StartupConfigError) as caught:
        await cli.serve(str(tmp_path / f"{SECRET}.yaml"))
    assert SECRET not in str(caught.value)
    assert caught.value.__suppress_context__
    reader.assert_called_once_with(encoding="utf-8")
    forbidden.assert_not_called()


def test_wrapper_validates_before_probe_or_exec(tmp_path, monkeypatch, capsys):
    runner = runner_module()
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump({SECRET_URL: True}))
    monkeypatch.setenv("ORR_CONFIG", str(path))
    monkeypatch.setattr(sys, "argv", ["run-profile.py", "portable"])
    monkeypatch.chdir(tmp_path)
    forbidden = Mock(side_effect=AssertionError("preflight side effect reached"))
    monkeypatch.setattr(runner.shutil, "which", forbidden)
    monkeypatch.setattr(runner.socket, "socket", forbidden)
    monkeypatch.setattr(runner.os, "execv", forbidden)
    monkeypatch.setattr(config, "_load_config_state", forbidden)
    assert runner.main() == 2
    assert capsys.readouterr().err == f"Startup error: {STRUCTURE}\n"
    forbidden.assert_not_called()
    assert not (tmp_path / ".state").exists()


@pytest.mark.parametrize("document", [
    f'friendly_name: "{SECRET_URL}\n',
    f'default_volume: !!int {SECRET}\n',
    f'friendly_name: !!bool {SECRET}\n',
    f'channel_balance: !!float {SECRET}\n',
    f'friendly_name: !!timestamp {SECRET}\n',
    'default_volume: !!int ""\n',
])
def test_wrapper_template_yaml_error_does_not_echo_source(tmp_path, monkeypatch, capsys, document):
    runner = runner_module()
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.delenv("ORR_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/config.example.yaml").write_text(document)
    monkeypatch.setattr(sys, "argv", ["run-profile.py", "portable"])
    assert runner.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"Startup error: {YAML_ERROR}\n"
    assert not (tmp_path / ".state").exists()


def test_wrapper_privileged_port_probe_remains_single_and_private(tmp_path, monkeypatch, capsys):
    runner = runner_module()
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    path = tmp_path / "profile.yaml"
    path.write_text("http_api_port: 80\nupnp_port: 80\nmirror_port_80: true\n"
                    "auto_spawn_mpv: false\nmpv_socket: /tmp/orr-config-test.sock\n")
    monkeypatch.setenv("ORR_CONFIG", str(path))
    monkeypatch.setattr(sys, "argv", ["run-profile.py", "portable"])
    monkeypatch.chdir(tmp_path)
    probe = Mock()
    probe.bind.side_effect = PermissionError(SECRET_URL)
    class FakeSocket:
        def __enter__(self):
            return probe
        def __exit__(self, *_args):
            return False
    factory = Mock(return_value=FakeSocket())
    monkeypatch.setattr(runner.socket, "socket", factory)
    assert runner.main() == 2
    factory.assert_called_once()
    probe.bind.assert_called_once_with(("0.0.0.0", 80))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "No sudo is invoked" in captured.err
    assert SECRET not in captured.err and "Traceback" not in captured.err


@pytest.mark.parametrize("document", ["", "# only a comment\n", "null\n", "{}\n", "friendly_name: Minimal renderer\n"])
def test_minimal_yaml_keeps_defaults_and_identity_behavior(tmp_path, monkeypatch, document):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "minimal.yaml"
    path.write_text(document)
    validated = config.read_startup_config(path)
    assert validated.uuid is None
    assert validated.http_api_port == 8080 and validated.auto_spawn_mpv is True
    assert not (tmp_path / ".state").exists()
    startup = config.load_startup_config(path)
    legacy = config.load_config(path)
    assert asdict(startup) == asdict(legacy)
    assert (tmp_path / ".state/uuid").read_text().strip() == startup.uuid


@pytest.mark.parametrize("volume", [50, -7, 150, "37", 37.8, True])
@pytest.mark.parametrize("level", ["INFO", "debug", "not-a-logging-level"])
def test_eager_checks_preserve_existing_gain_and_logging_semantics(tmp_path, monkeypatch, volume, level):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"default_volume": volume, "log_level": level}))
    validated = config.read_startup_config(path)
    legacy = config.Config(default_volume=volume, log_level=level)
    assert asdict(validated) == asdict(legacy)
    assert RendererState.from_config(validated).volume == max(0, min(100, int(volume)))
    basic_config = Mock()
    with monkeypatch.context() as logging_check:
        logging_check.setattr(logging, "basicConfig", basic_config)
        logging_check.setattr(logging, "getLogger", lambda: type("Logger", (), {"handlers": []})())
        setup_logging(validated.log_level)
    assert basic_config.call_args.kwargs["level"] == getattr(logging, level.upper(), logging.INFO)


def test_bare_serve_keeps_missing_example_fallback_but_explicit_path_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    class AtBackend(Exception):
        pass
    backend = Mock(side_effect=AtBackend)
    monkeypatch.setattr(cli, "MpvBackend", backend)
    monkeypatch.setattr(cli, "setup_logging", lambda _: None)
    with pytest.raises(AtBackend):
        cli.main(["serve"])
    assert (tmp_path / ".state/uuid").is_file()
    backend.assert_called_once()
    backend.reset_mock()
    assert cli.main(["serve", "--config", "configs/config.example.yaml"]) == 2
    backend.assert_not_called()
    # Public utility consumers continue to allow missing files.
    assert config.load_config("another-missing-file.yaml").uuid
    assert cli.build_parser().parse_args(["print-description"]).config == "configs/config.example.yaml"
    assert cli.build_parser().parse_args(["print-discovery-info"]).config == "configs/config.example.yaml"


@pytest.mark.parametrize("explicit", [False, True])
def test_real_process_accepts_minimal_yaml_before_stubbed_service_boundary(tmp_path, explicit):
    path = tmp_path / "configs/config.example.yaml"
    path.parent.mkdir()
    path.write_text("{}\n")
    source = textwrap.dedent('''
        import sys
        from pathlib import Path
        from fake_linkplay import cli
        class ServiceBoundary(Exception):
            pass
        def backend(*args, **kwargs):
            assert Path('.state/uuid').is_file()
            assert kwargs == {'initial_volume': 50, 'initial_mute': False, 'audio_device': None}
            raise ServiceBoundary
        cli.MpvBackend = backend
        try:
            cli.main(sys.argv[1:])
        except ServiceBoundary:
            print('configuration accepted before backend/listeners')
        else:
            raise AssertionError('expected service boundary')
    ''')
    args = ["serve", "--config", str(path)] if explicit else ["serve"]
    result = subprocess.run([sys.executable, "-c", source, *args], cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and result.stderr == ""
    assert result.stdout == "configuration accepted before backend/listeners\n"


def test_runtime_failures_are_not_misreported_as_configuration_errors(monkeypatch):
    async def fail(_path):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(cli, "serve", fail)
    with pytest.raises(RuntimeError, match=SECRET):
        cli.main(["serve", "--config", "valid.yaml"])


def test_public_loader_keeps_legacy_construction_without_startup_checks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "legacy.yaml"
    path.write_text(yaml.safe_dump({"default_volume": SECRET, "log_level": [SECRET]}))
    legacy = config.load_config(path)
    assert legacy.default_volume == SECRET and legacy.log_level == [SECRET]
    assert (tmp_path / ".state/uuid").is_file()
    with pytest.raises(config.StartupConfigError, match="structure is invalid"):
        config.read_startup_config(path)


def test_wrapper_does_not_recursively_copy_unrelated_yaml_values(tmp_path):
    runner = runner_module()
    path = tmp_path / "profile.yaml"
    path.write_text("friendly_name: &name [*name]\nauto_spawn_mpv: false\n"
                    "mpv_socket: /tmp/orr-config-test.sock\n")
    data = runner.preflight(path)
    assert data["friendly_name"][0] is data["friendly_name"]
    assert data["auto_spawn_mpv"] is False


def test_real_wrapper_accepts_minimal_yaml_before_stubbed_exec(tmp_path):
    path = tmp_path / "minimal.yaml"
    path.write_text("{}\n")
    source = textwrap.dedent('''
        import importlib.util
        import os
        from pathlib import Path
        import sys
        spec = importlib.util.spec_from_file_location('run_profile', sys.argv[1])
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        runner.ROOT = Path.cwd()
        runner.shutil.which = lambda name: '/synthetic/mpv'
        def forbidden_probe(*args):
            raise AssertionError('default high ports must not be probed')
        runner.socket.socket = forbidden_probe
        class ExecBoundary(Exception):
            pass
        def execv(executable, arguments):
            assert arguments == [executable, 'serve', '--config', os.environ['ORR_CONFIG']]
            assert not Path('.state').exists()
            raise ExecBoundary
        runner.os.execv = execv
        sys.argv = ['run-profile.py', 'portable']
        try:
            runner.main()
        except ExecBoundary:
            print('configuration accepted before exec')
        else:
            raise AssertionError('expected exec boundary')
    ''')
    result = subprocess.run([sys.executable, "-c", source, str(ROOT / "scripts/run-profile.py")],
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(ROOT), "ORR_CONFIG": str(path)},
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and result.stderr == ""
    assert result.stdout.endswith("configuration accepted before exec\n")
    assert not (tmp_path / ".state").exists()


def test_invalid_volume_is_not_masked_by_valid_saved_gain(tmp_path, monkeypatch):
    from fake_linkplay.persistence import save_gain
    state = tmp_path / "state"
    save_gain(state, 25, True)
    before = (state / "gain.json").read_bytes()
    path = tmp_path / "renderer.yaml"
    path.write_text(yaml.safe_dump({"state_dir": str(state), "default_volume": SECRET_URL}))
    # The startup reader rejects malformed YAML settings without even reading
    # existing gain. The valid saved gain and absent identity stay untouched.
    with monkeypatch.context() as guarded:
        forbidden = Mock(side_effect=AssertionError("persisted state reached"))
        guarded.setattr(config, "_load_config_state", forbidden)
        with pytest.raises(config.StartupConfigError, match="structure is invalid"):
            config.load_startup_config(path)
        forbidden.assert_not_called()
    assert (state / "gain.json").read_bytes() == before
    assert list(state.iterdir()) == [state / "gain.json"]
    # Preserve the public loader contract; its saved override still masks this
    # legacy input. Operators must correct the malformed setting for serve.
    assert config.load_config(path).default_volume == 25
    path.write_text(yaml.safe_dump({"state_dir": str(state), "default_volume": "37"}))
    restored = config.load_startup_config(path)
    assert (restored.default_volume, restored.default_mute) == (25, True)
    assert (state / "gain.json").read_bytes() == before
