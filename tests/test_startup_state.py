"""Bounded persisted-state startup diagnostics; synthetic data and fake backend only."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import traceback
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from fake_linkplay import cli, config, http_api, persistence
from fake_linkplay.state import RendererState
from fake_linkplay.startup_state import StartupStateError, _STATE_ERRORS

ROOT = Path(__file__).resolve().parents[1]
SECRET = "synthetic-private-state-token"
SECRET_URL = f"https://user:password@media.example.test/audio?token={SECRET}"
UUID = "00000000-0000-4000-8000-000000000000"


def active_document():
    return {
        "version": 1, "plugin": "synthetic.eq", "source": "", "enabled": True,
        "name": "Synthetic preset", "balance": 0.25,
        "bands": [{"param_name": f"{letter}_{suffix}", "value": value}
                  for letter in "abcdefghij"
                  for suffix, value in (("freq", 1000), ("gain", -6), ("q", 1), ("mode", 0))],
    }


def private_dsp_bytes():
    payload = active_document()
    payload["bands"][0]["value"] = SECRET_URL
    return json.dumps(payload).encode()


def config_file(tmp_path, *, persisted_identity=False):
    directory = tmp_path / SECRET
    directory.mkdir()
    path = tmp_path / "renderer.yaml"
    document = {"state_dir": str(directory), "auto_spawn_mpv": False,
                "mpv_socket": "/tmp/orr-synthetic-startup.sock", "enable_ssdp": False}
    if not persisted_identity:
        document["uuid"] = UUID
    path.write_text(yaml.safe_dump(document))
    return path, directory


@pytest.fixture
def wrapper(tmp_path):
    # Execute the real wrapper, whose relative entrypoint is installed here with
    # the current test interpreter. No checkout venv, mpv, or bind probe is used.
    root = tmp_path / "wrapper"
    (root / "scripts").mkdir(parents=True)
    (root / ".venv/bin").mkdir(parents=True)
    script = root / "scripts/run-profile.py"
    shutil.copyfile(ROOT / "scripts/run-profile.py", script)
    executable = root / ".venv/bin/open-room-renderer"
    executable.write_text(f"#!{sys.executable}\nfrom fake_linkplay.cli import main\nraise SystemExit(main())\n")
    executable.chmod(0o700)
    return script


@pytest.mark.parametrize("name,encoded,stage", [
    ("active_dsp.json", private_dsp_bytes(), "active_dsp"),
    ("active_dsp.json", ("{" + SECRET_URL).encode(), "active_dsp"),
    ("active_dsp.json", b'[' * 1100, "active_dsp"),
    ("gain.json", b'{"version":1,"volume":25,"mute":true,"mute":false}', "gain"),
    ("gain.json", b'[' * 1100, "gain"),
    ("gain.json", b'\xff' + SECRET_URL.encode(), "gain"),
    ("uuid", b"", "identity"),
    ("uuid", b'\xff' + SECRET_URL.encode(), "identity"),
    ("device_name", b'\xff' + SECRET_URL.encode(), "device_name"),
    ("channel_balance", b'\xff' + SECRET_URL.encode(), "channel_balance"),
])
def test_real_cli_and_wrapper_keep_state_failures_private(tmp_path, wrapper, name, encoded, stage):
    path, directory = config_file(tmp_path, persisted_identity=name == "uuid")
    saved = directory / name
    saved.write_bytes(encoded)
    env = {**os.environ, "PYTHONPATH": str(ROOT), "ORR_CONFIG": str(path)}
    for argv in ([sys.executable, "-m", "fake_linkplay.cli", "serve", "--config", str(path)],
                 [sys.executable, str(wrapper), "portable"]):
        result = subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == 2, result.stderr
        assert result.stderr == f"Startup error: {_STATE_ERRORS[stage]}\n"
        assert SECRET not in result.stderr and str(directory) not in result.stderr
        assert "Traceback" not in result.stderr
        assert saved.read_bytes() == encoded
        assert set(directory.iterdir()) == {saved}


@pytest.mark.parametrize("name,stage", [
    ("uuid", "identity"), ("gain.json", "gain"), ("device_name", "device_name"),
    ("active_dsp.json", "active_dsp"), ("channel_balance", "channel_balance"),
])
def test_real_cli_unreadable_state_stops_without_retry_or_reset(tmp_path, name, stage):
    if os.geteuid() == 0:
        pytest.skip("POSIX permissions require a non-root process")
    path, directory = config_file(tmp_path, persisted_identity=name == "uuid")
    saved = directory / name
    saved.write_text(SECRET_URL)
    saved.chmod(0)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "fake_linkplay.cli", "serve", "--config", str(path)],
            cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(ROOT)},
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 2
        assert result.stderr == f"Startup error: {_STATE_ERRORS[stage]}\n"
        assert saved.stat().st_mode & 0o777 == 0
    finally:
        saved.chmod(0o600)
    assert saved.read_text() == SECRET_URL


@pytest.mark.parametrize("stage", list(_STATE_ERRORS))
@pytest.mark.parametrize("failure", [ValueError, OSError, UnicodeError, PermissionError])
def test_initial_known_readers_have_only_fixed_guidance(tmp_path, monkeypatch, stage, failure):
    path, directory = config_file(tmp_path, persisted_identity=stage == "identity")
    reader = Mock(side_effect=failure(SECRET_URL))
    if stage == "identity":
        monkeypatch.setattr(config, "load_or_create_uuid", reader)
    elif stage == "directory":
        monkeypatch.setattr(config, "ensure_dir", reader)
    elif stage in {"gain", "active_dsp"}:
        monkeypatch.setattr(config, f"load_{stage}", reader)
    else:
        (directory / stage).write_text("0.0")
        original = config._read_optional_persistent_text
        def read_text(p):
            return reader() if p.name == stage else original(p)
        monkeypatch.setattr(config, "_read_optional_persistent_text", read_text)
    backend = Mock(side_effect=AssertionError("Backend must not be constructed"))
    monkeypatch.setattr(cli, "MpvBackend", backend)
    with pytest.raises(StartupStateError) as caught:
        asyncio.run(cli.serve(str(path)))
    assert str(caught.value) == _STATE_ERRORS[stage]
    assert caught.value.__suppress_context__
    assert SECRET not in "".join(traceback.format_exception(caught.value))
    reader.assert_called_once()
    backend.assert_not_called()


@pytest.mark.parametrize("failure", [RuntimeError, AssertionError, KeyError, AttributeError,
                                     TypeError, asyncio.CancelledError, KeyboardInterrupt])
def test_initial_programmer_errors_and_cancellation_propagate(tmp_path, monkeypatch, failure):
    path, _ = config_file(tmp_path)
    error = failure(SECRET_URL)
    reader = Mock(side_effect=error)
    monkeypatch.setattr(config, "load_gain", reader)
    with pytest.raises(failure) as caught:
        config.load_startup_config(path)
    assert caught.value is error
    reader.assert_called_once()


def fake_backend(monkeypatch):
    backend = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), initialize_gain=AsyncMock(),
                              stop_process=AsyncMock(), stage_initial_dsp=Mock())
    monkeypatch.setattr(cli, "MpvBackend", Mock(return_value=backend))
    monkeypatch.setattr(cli, "setup_logging", lambda _: None)
    listener = Mock(side_effect=AssertionError("Listener must not start"))
    monkeypatch.setattr(cli.web, "AppRunner", listener)
    return backend, listener


def test_cli_second_real_dsp_read_is_private_and_cleans_acquired_backend(tmp_path, monkeypatch, capsys):
    path, directory = config_file(tmp_path)
    saved = directory / "active_dsp.json"
    saved.write_text(json.dumps(active_document()))
    backend, listener = fake_backend(monkeypatch)
    # Simulate state replacement after the initial validation and before the
    # independent constructor read, without changing either production reader.
    backend.start.side_effect = lambda: saved.write_bytes(private_dsp_bytes())
    assert cli.main(["serve", "--config", str(path)]) == 2
    assert capsys.readouterr().err == f"Startup error: {_STATE_ERRORS['active_dsp']}\n"
    backend.start.assert_awaited_once()
    backend.stop.assert_awaited_once()
    backend.initialize_gain.assert_awaited_once()
    backend.stop_process.assert_awaited_once()
    backend.stage_initial_dsp.assert_not_called()
    listener.assert_not_called()
    assert saved.read_bytes() == private_dsp_bytes()


@pytest.mark.parametrize("failure", [ValueError, OSError, UnicodeError,
                                     RuntimeError, AssertionError, KeyError, AttributeError,
                                     TypeError, asyncio.CancelledError])
def test_second_reader_limits_catches_and_always_cleans_up(tmp_path, monkeypatch, failure):
    path, _ = config_file(tmp_path)
    backend, listener = fake_backend(monkeypatch)
    error = failure(SECRET_URL)
    reader = Mock(side_effect=error)
    monkeypatch.setattr(http_api, "load_active_dsp", reader)
    expected = StartupStateError if issubclass(failure, (ValueError, OSError)) else failure
    with pytest.raises(expected) as caught:
        asyncio.run(cli.serve(str(path)))
    if expected is StartupStateError:
        assert str(caught.value) == _STATE_ERRORS["active_dsp"]
        assert SECRET not in "".join(traceback.format_exception(caught.value))
    else:
        assert caught.value is error
    reader.assert_called_once()
    backend.stop_process.assert_awaited_once()
    backend.stage_initial_dsp.assert_not_called()
    listener.assert_not_called()


@pytest.mark.parametrize("component", ["FavoritesStore", "EventSubscriptionManager"])
def test_startup_does_not_wrap_unrelated_constructor_failures(tmp_path, monkeypatch, component):
    path, _ = config_file(tmp_path)
    backend, listener = fake_backend(monkeypatch)
    error = ValueError(SECRET_URL)
    monkeypatch.setattr(http_api, component, Mock(side_effect=error))
    with pytest.raises(ValueError) as caught:
        asyncio.run(cli.serve(str(path)))
    assert caught.value is error
    backend.stop_process.assert_awaited_once()
    listener.assert_not_called()


@pytest.mark.parametrize("public_api", ["load_config", "load_persistent_runtime_state", "load_active_dsp", "HttpApi"])
def test_public_apis_keep_original_dsp_exception_and_chain(tmp_path, public_api):
    path, directory = config_file(tmp_path)
    (directory / "active_dsp.json").write_bytes(private_dsp_bytes())
    cfg = config.read_startup_config(path)
    readers = {
        "load_config": lambda: config.load_config(path),
        "load_persistent_runtime_state": lambda: config.load_persistent_runtime_state(cfg),
        "load_active_dsp": lambda: persistence.load_active_dsp(directory),
        "HttpApi": lambda: http_api.HttpApi(cfg, RendererState.from_config(cfg), SimpleNamespace()),
    }
    with pytest.raises(ValueError) as caught:
        readers[public_api]()
    assert type(caught.value) is ValueError
    assert str(caught.value) == "EQ parameter must be a finite number"
    assert SECRET_URL in str(caught.value.__cause__)


def test_public_gain_reader_keeps_original_error(tmp_path):
    (tmp_path / "gain.json").write_text('{"version":1,"volume":25,"mute":true,"mute":false}')
    with pytest.raises(ValueError, match="^Invalid persisted gain document$") as caught:
        persistence.load_gain(tmp_path)
    assert type(caught.value) is ValueError


def test_valid_state_precedence_and_public_startup_results_match(tmp_path):
    path, directory = config_file(tmp_path)
    (directory / "gain.json").write_text('{"version":1,"volume":25,"mute":true}')
    (directory / "device_name").write_text("  Synthetic room\x00\n")
    (directory / "channel_balance").write_bytes(b"\xff")
    (directory / "active_dsp.json").write_text(json.dumps(active_document()))
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    startup = config.load_startup_config(path)
    public = config.load_config(path)
    assert asdict(startup) == asdict(public)
    assert startup.default_volume == 25 and startup.default_mute is True
    assert startup.friendly_name == startup.mdns_instance_name == "Synthetic room"
    assert startup.channel_balance == "0.25"
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}


def test_published_identity_is_preserved_when_later_gain_read_fails(tmp_path):
    path, directory = config_file(tmp_path, persisted_identity=True)
    (directory / "gain.json").write_text("invalid")
    with pytest.raises(StartupStateError, match="Saved gain"):
        config.load_startup_config(path)
    identity = (directory / "uuid").read_bytes()
    with pytest.raises(StartupStateError):
        config.load_startup_config(path)
    assert (directory / "uuid").read_bytes() == identity
    assert (directory / "gain.json").read_text() == "invalid"


INITIAL_BALANCE_ERROR = (
    "Initial channel balance is invalid. Check channel_balance in configuration or saved state."
)


def balance_config_file(tmp_path, *, raw="0.0", saved=None, active=False,
                        persisted_identity=False):
    path, directory = config_file(tmp_path, persisted_identity=persisted_identity)
    document = yaml.safe_load(path.read_text())
    document["channel_balance"] = raw
    path.write_text(yaml.safe_dump(document))
    (directory / "plex_client_id").write_text("synthetic-plex-client")
    if saved is not None:
        (directory / "channel_balance").write_bytes(saved)
    if active:
        (directory / "active_dsp.json").write_text(json.dumps(active_document()))
    return path, directory


@pytest.mark.parametrize("source", ["configuration", "saved"])
@pytest.mark.parametrize("persisted_identity", [False, True])
def test_initial_balance_cli_subprocess_is_private_and_releases_backend(
    tmp_path, source, persisted_identity,
):
    path, directory = balance_config_file(
        tmp_path, raw=SECRET_URL if source == "configuration" else "0.0",
        saved=SECRET_URL.encode() if source == "saved" else None,
        persisted_identity=persisted_identity,
    )
    config_bytes = path.read_bytes()
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    # Exercise the real CLI handler in a child with a fully stubbed backend.
    # Even an accidental IPC call is rejected locally; no socket is contacted.
    program = '''
import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from fake_linkplay import cli
backend = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(),
    initialize_gain=AsyncMock(), stop_process=AsyncMock(), stage_initial_dsp=Mock())
cli.MpvBackend = Mock(return_value=backend)
cli.setup_logging = lambda _: None
listener = Mock(side_effect=AssertionError("Listener must not start"))
cli.web.AppRunner = listener
ipc = AsyncMock(side_effect=AssertionError("IPC must not be contacted"))
asyncio.open_unix_connection = ipc
try:
    result = cli.main(["serve", "--config", sys.argv[1]])
finally:
    print(json.dumps({name: getattr(backend, name).await_count
        for name in ("start", "stop", "initialize_gain", "stop_process")} | {
        "stage_initial_dsp": backend.stage_initial_dsp.call_count,
        "listener": listener.call_count, "ipc": ipc.await_count}))
raise SystemExit(result)
'''
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-c", program, str(path)], cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 2, result.stderr
        assert result.stderr == f"Startup error: {INITIAL_BALANCE_ERROR}\n"
        assert not any(value in result.stderr for value in (
            SECRET, SECRET_URL, "user:password", str(directory), "Traceback",
        ))
        assert json.loads(result.stdout) == {
            "start": 1, "stop": 1, "initialize_gain": 1, "stop_process": 1,
            "stage_initial_dsp": 0, "listener": 0, "ipc": 0,
        }
        after = {p.name: p.read_bytes() for p in directory.iterdir()}
        assert all(after[name] == data for name, data in before.items())
        assert set(after) == set(before) | ({"uuid"} if persisted_identity else set())
        if persisted_identity:
            assert after["uuid"].strip()
        before = after  # A published identity survives the same failed retry.
        assert path.read_bytes() == config_bytes


@pytest.mark.parametrize("source", ["configuration", "saved"])
@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "101", "-101", "bad"])
def test_initial_balance_known_invalid_values_keep_fixed_cli_guidance(
    tmp_path, monkeypatch, capsys, source, raw,
):
    path, directory = balance_config_file(
        tmp_path, raw=raw if source == "configuration" else "0.0",
        saved=raw.encode() if source == "saved" else None,
    )
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    backend, listener = fake_backend(monkeypatch)
    assert cli.main(["serve", "--config", str(path)]) == 2
    assert capsys.readouterr().err == f"Startup error: {INITIAL_BALANCE_ERROR}\n"
    for name in ("start", "stop", "initialize_gain", "stop_process"):
        getattr(backend, name).assert_awaited_once()
    backend.stage_initial_dsp.assert_not_called()
    listener.assert_not_called()
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}


@pytest.mark.parametrize("startup", [False, True])
@pytest.mark.parametrize("raw,expected", [
    ("-.125", "-0.125"), ("25", "0.25"), ("%2D25", "-0.25"),
    ("-100", "-1"), ("100", "1"), ("1", "1"), ("1.0001", "0.01"),
    ("0.0001", "0"), ("-0.0001", "-0"), ("-0", "0.0"),
])
@pytest.mark.parametrize("source", ["configuration", "saved"])
def test_initial_balance_preserves_normalization_without_changing_saved_bytes(
    tmp_path, startup, raw, expected, source,
):
    path, directory = balance_config_file(
        tmp_path, raw=raw if source == "configuration" else SECRET_URL,
        saved=raw.encode() if source == "saved" else None,
    )
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    loader = config.load_startup_config if startup else config.load_config
    cfg = loader(path)
    backend = SimpleNamespace(stage_initial_dsp=Mock())
    api = http_api.HttpApi(cfg, RendererState.from_config(cfg), backend, _startup=startup)
    assert cfg.channel_balance == expected
    assert api.channel_balance == "0.0"
    backend.stage_initial_dsp.assert_called_once()
    assert backend.stage_initial_dsp.call_args.args[0] == float(expected)
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}


@pytest.mark.parametrize("startup", [False, True])
@pytest.mark.parametrize("saved,active,expected", [
    (b"75", False, "0.75"), (SECRET_URL.encode(), True, "0.25"),
    (b"", False, "0.0"), (b"\x00\x00", False, "0.0"),
])
def test_initial_balance_preserves_source_precedence(tmp_path, startup, saved, active, expected):
    path, directory = balance_config_file(tmp_path, raw=SECRET_URL, saved=saved, active=active)
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    public, startup_cfg = config.load_config(path), config.load_startup_config(path)
    assert asdict(public) == asdict(startup_cfg)
    cfg = startup_cfg if startup else public
    backend = SimpleNamespace(stage_initial_dsp=Mock())
    api = http_api.HttpApi(cfg, RendererState.from_config(cfg), backend, _startup=startup)
    assert cfg.channel_balance == expected
    assert api.channel_balance == "0.0"
    backend.stage_initial_dsp.assert_called_once()
    assert backend.stage_initial_dsp.call_args.args[0] == float(expected)
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}


@pytest.mark.parametrize("startup", [False, True])
@pytest.mark.parametrize("failure", [ValueError, OSError, RuntimeError, AssertionError,
                                     KeyError, AttributeError, TypeError,
                                     asyncio.CancelledError, KeyboardInterrupt])
def test_initial_balance_boundary_only_translates_startup_value_errors(
    tmp_path, monkeypatch, startup, failure,
):
    path, _ = balance_config_file(tmp_path)
    cfg = config.load_config(path)
    error = failure(SECRET_URL)
    error.__cause__ = RuntimeError(SECRET)
    normalize = Mock(side_effect=error)
    monkeypatch.setattr(http_api.HttpApi, "_normalize_channel_balance", normalize)
    backend = SimpleNamespace(stage_initial_dsp=Mock())
    expected = StartupStateError if startup and failure is ValueError else failure
    with pytest.raises(expected) as caught:
        http_api.HttpApi(cfg, RendererState.from_config(cfg), backend, _startup=startup)
    normalize.assert_called_once_with("0.0")
    if expected is StartupStateError:
        assert str(caught.value) == INITIAL_BALANCE_ERROR
        assert caught.value.__suppress_context__
        assert caught.value.__cause__ is None
        assert SECRET not in "".join(traceback.format_exception(caught.value))
    else:
        assert caught.value is error
        assert str(caught.value.__cause__) == SECRET
    backend.stage_initial_dsp.assert_not_called()


@pytest.mark.parametrize("source", ["configuration", "saved"])
def test_initial_balance_public_loader_and_constructor_keep_legacy_behavior(tmp_path, source):
    path, _ = balance_config_file(
        tmp_path, raw=SECRET_URL if source == "configuration" else "0.0",
        saved=SECRET_URL.encode() if source == "saved" else None,
    )
    # Neither loader adds schema validation or eagerly normalizes balance.
    public, startup = config.load_config(path), config.load_startup_config(path)
    assert asdict(public) == asdict(startup)
    assert public.channel_balance == SECRET_URL
    with pytest.raises(ValueError) as caught:
        http_api.HttpApi(public, RendererState.from_config(public), SimpleNamespace())
    assert type(caught.value) is ValueError
    assert SECRET_URL in str(caught.value)
    assert not caught.value.__suppress_context__


@pytest.mark.parametrize("failure", [ValueError, OSError, TypeError, asyncio.CancelledError])
def test_initial_balance_unrelated_staging_failures_propagate_and_cleanup(
    tmp_path, monkeypatch, failure,
):
    path, _ = balance_config_file(tmp_path)
    backend, listener = fake_backend(monkeypatch)
    error = failure(SECRET_URL)
    backend.stage_initial_dsp.side_effect = error
    with pytest.raises(failure) as caught:
        asyncio.run(cli.serve(str(path)))
    assert caught.value is error
    for name in ("start", "stop", "initialize_gain", "stop_process"):
        getattr(backend, name).assert_awaited_once()
    backend.stage_initial_dsp.assert_called_once()
    listener.assert_not_called()


@pytest.mark.parametrize("failure", [OSError, TypeError, RuntimeError, asyncio.CancelledError])
def test_initial_balance_normalizer_other_errors_keep_cli_cleanup(tmp_path, monkeypatch, failure):
    path, _ = balance_config_file(tmp_path)
    backend, listener = fake_backend(monkeypatch)
    error = failure(SECRET_URL)
    normalize = Mock(side_effect=error)
    monkeypatch.setattr(http_api.HttpApi, "_normalize_channel_balance", normalize)
    with pytest.raises(failure) as caught:
        asyncio.run(cli.serve(str(path)))
    assert caught.value is error
    normalize.assert_called_once_with("0.0")
    for name in ("start", "stop", "initialize_gain", "stop_process"):
        getattr(backend, name).assert_awaited_once()
    backend.stage_initial_dsp.assert_not_called()
    listener.assert_not_called()
