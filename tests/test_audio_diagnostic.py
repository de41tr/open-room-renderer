"""Synthetic IPC only: no native socket, player, audio device or HTTP request."""
from __future__ import annotations

import asyncio
import errno
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from fake_linkplay import audio_diagnostic, cli, config
from fake_linkplay.mpv_backend import BackendError, MpvBackend

SECRET = "synthetic-private-path-or-token"


class FakeWriter:
    def __init__(self):
        self.commands = []
        self.transport = Mock()
        self.drain = AsyncMock()
        self.wait_closed = AsyncMock()
        self.closed = False

    def write(self, data):
        self.commands.append(json.loads(data))

    def close(self):
        self.closed = True


@pytest.fixture
def probe(monkeypatch):
    backend = MpvBackend(f"/{SECRET}.sock", auto_spawn=False)
    identity = Mock(return_value=(7, 8))
    monkeypatch.setattr(MpvBackend, "_socket_identity", identity)
    writer = FakeWriter()
    responses = [
        {"request_id": 1, "error": "success", "data": "alsa/dmix:default"},
        {"request_id": 2, "error": "success", "data": "alsa"},
    ]

    async def read():
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response if isinstance(response, bytes) else json.dumps(response).encode() + b"\n"

    reader = SimpleNamespace(readline=AsyncMock(side_effect=read))
    connect = AsyncMock(return_value=(reader, writer))
    monkeypatch.setattr(asyncio, "open_unix_connection", connect)
    return SimpleNamespace(backend=backend, identity=identity, writer=writer,
                           responses=responses, reader=reader, connect=connect)


@pytest.mark.asyncio
async def test_only_two_fixed_reads_close_transport_and_leave_lifecycle_untouched(probe, monkeypatch, caplog):
    forbidden = Mock(side_effect=AssertionError("mutating operation reached"))
    for name in ("start", "command", "get_property", "stop_process", "_reap_owned_process"):
        monkeypatch.setattr(MpvBackend, name, forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(os, "unlink", forbidden)
    monkeypatch.setattr(os, "kill", forbidden)
    before = dict(vars(probe.backend))
    result = await probe.backend.diagnose_audio_output()
    assert result == {
        "connection": "connected", "socket_path_check": "unchanged_at_checks",
        "audio_device": {"status": "value", "value": "alsa/dmix:default"},
        "current_ao": {"status": "value", "value": "alsa"},
    }
    assert vars(probe.backend) == before
    assert probe.writer.commands == [
        {"command": ["get_property", "audio-device"], "request_id": 1},
        {"command": ["get_property", "current-ao"], "request_id": 2},
    ]
    probe.connect.assert_awaited_once_with(f"/{SECRET}.sock", limit=8192)
    assert probe.writer.closed
    probe.writer.transport.abort.assert_called_once_with()
    forbidden.assert_not_called()
    assert not caplog.records


@pytest.mark.asyncio
@pytest.mark.parametrize("error,status", [("property unavailable", "unavailable"),
                                         ("property not found", "unsupported")])
async def test_property_unavailable_is_distinct_from_missing_socket(probe, error, status):
    probe.responses[1] = {"request_id": 2, "error": error, "data": SECRET}
    result = await probe.backend.diagnose_audio_output()
    assert result["connection"] == "connected"
    assert result["current_ao"] == {"status": status}
    assert SECRET not in json.dumps(result)


@pytest.mark.asyncio
async def test_missing_socket_does_not_connect_or_retry(probe):
    probe.identity.return_value = None
    result = await probe.backend.diagnose_audio_output()
    assert result["connection"] == "socket_missing"
    assert result["audio_device"] == result["current_ao"] == {"status": "not_read"}
    probe.connect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,status", [
    (FileNotFoundError(SECRET), "socket_missing"),
    (ConnectionRefusedError(SECRET), "transport_error"),
    (PermissionError(errno.EPERM, SECRET), "transport_error"),
])
async def test_connect_failures_private_single_attempt(probe, failure, status, caplog):
    probe.connect.side_effect = failure
    result = await probe.backend.diagnose_audio_output()
    assert result["connection"] == status
    assert SECRET not in json.dumps(result)
    probe.connect.assert_awaited_once()
    assert not caplog.records


@pytest.mark.asyncio
async def test_non_socket_is_refused_without_connect_or_delete(probe, monkeypatch):
    probe.identity.side_effect = BackendError(SECRET)
    unlink = Mock(side_effect=AssertionError("socket deletion reached"))
    monkeypatch.setattr(os, "unlink", unlink)
    result = await probe.backend.diagnose_audio_output()
    assert result["connection"] == "socket_invalid"
    probe.connect.assert_not_awaited()
    unlink.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    b"not json " + SECRET.encode(), b"\xff\n", b"[]\n", b"{}\n",
    b'{"request_id":1,"error":"success","data":"auto"}',
    b'{"request_id":1,"request_id":1,"error":"success","data":"auto"}\n',
    {"request_id": True, "error": "success", "data": "auto"},
    {"request_id": 1, "data": SECRET},
    {"request_id": 1, "error": "success", "data": None},
    {"request_id": 1, "error": "success", "data": {"secret": SECRET}},
    {"request_id": 1, "error": "success", "data": "null\n" + SECRET},
    {"request_id": 1, "error": "success", "data": "alsa/" + "x" * 1024},
    b"[" * 2000 + b"]" * 2000,
])
async def test_malformed_responses_are_private_and_stop_without_retry(probe, response, caplog):
    probe.responses[:] = [response]
    result = await probe.backend.diagnose_audio_output()
    assert result["audio_device"] == {"status": "malformed_response"}
    assert result["current_ao"] == {"status": "not_read"}
    assert len(probe.writer.commands) == 1
    assert SECRET not in json.dumps(result)
    assert probe.writer.closed and not caplog.records


@pytest.mark.asyncio
async def test_error_eof_and_invalid_driver_do_not_become_unavailable(probe):
    for response, expected, connection in [
        ({"request_id": 2, "error": SECRET}, "request_failed", "connected"),
        (b"", "transport_error", "transport_error"),
        ({"request_id": 2, "error": "success", "data": "alsa/" + SECRET},
         "malformed_response", "connected"),
    ]:
        probe.responses[:] = [{"request_id": 1, "error": "success", "data": "auto"}, response]
        result = await probe.backend.diagnose_audio_output()
        assert result["audio_device"] == {"status": "value", "value": "auto"}
        assert result["current_ao"] == {"status": expected}
        assert result["connection"] == connection
        assert SECRET not in json.dumps(result)


@pytest.mark.asyncio
async def test_events_and_other_reply_ids_are_bounded_and_not_output(probe):
    probe.responses.insert(0, {"event": "log-message", "text": SECRET})
    probe.responses.insert(1, {"request_id": 99, "error": "success", "data": SECRET})
    result = await probe.backend.diagnose_audio_output()
    assert result["current_ao"]["value"] == "alsa"
    assert SECRET not in json.dumps(result)
    probe.responses[:] = [{"event": "log-message", "text": SECRET}] * 20
    result = await probe.backend.diagnose_audio_output()
    assert result["audio_device"] == {"status": "response_limit"}
    assert len(probe.responses) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("responses", [
    [b"x" * 8193],
    [ValueError(SECRET)],  # StreamReader.readline over its configured limit
    [{"event": "log-message", "text": "x" * 7000}] * 5,
])
async def test_line_and_total_reply_limits(probe, responses):
    probe.responses[:] = responses
    result = await probe.backend.diagnose_audio_output()
    assert result["audio_device"]["status"] in {"response_limit", "malformed_response"}
    assert result["current_ao"] == {"status": "not_read"}
    assert probe.writer.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("when,expected_commands", [(1, 0), (2, 1), (3, 2)])
async def test_path_replacement_is_reported_without_new_connection(probe, when, expected_commands):
    probe.identity.side_effect = [(7, 8)] * when + [(7, 9)]
    result = await probe.backend.diagnose_audio_output()
    assert result["socket_path_check"] == "changed"
    assert len(probe.writer.commands) == expected_commands
    probe.connect.assert_awaited_once()
    assert probe.writer.closed


@pytest.mark.asyncio
async def test_one_deadline_includes_both_reads_and_close(probe):
    started = asyncio.get_running_loop().time()
    deadlines = []
    original_close = probe.backend._close_writer

    async def close(writer, deadline):
        deadlines.append(deadline)
        await original_close(writer, deadline)

    async def read():
        if len(probe.writer.commands) == 1:
            await asyncio.sleep(0.025)
            return b'{"request_id":1,"error":"success","data":"auto"}\n'
        await asyncio.sleep(10)

    probe.reader.readline.side_effect = read
    probe.writer.wait_closed.side_effect = lambda: asyncio.sleep(10)
    probe.backend._close_writer = close
    result = await probe.backend.diagnose_audio_output(timeout=0.05)
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 0.2
    assert result["audio_device"] == {"status": "value", "value": "auto"}
    assert result["current_ao"] == {"status": "timed_out"}
    assert result["connection"] == "timed_out"
    assert len(deadlines) == 1 and started <= deadlines[0] - 0.05 <= started + 0.01
    assert probe.writer.closed
    probe.writer.transport.abort.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("delay_on_check,expected_commands", [(1, 0), (2, 0), (3, 1), (4, 2)])
async def test_synchronous_path_overrun_never_sends_later_command_or_reports_complete(
    probe, delay_on_check, expected_commands,
):
    calls = 0

    def inspect():
        nonlocal calls
        calls += 1
        if calls == delay_on_check:
            time.sleep(0.025)
        return (7, 8)

    probe.identity.side_effect = inspect
    result = await probe.backend.diagnose_audio_output(timeout=0.015)
    assert result["connection"] == "timed_out"
    assert len(probe.writer.commands) == expected_commands
    if delay_on_check == 1:
        probe.connect.assert_not_awaited()
    else:
        assert probe.writer.closed
        probe.writer.transport.abort.assert_called_once()


@pytest.mark.asyncio
async def test_cancellation_closes_client_only(probe):
    probe.reader.readline.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await probe.backend.diagnose_audio_output()
    assert probe.writer.closed
    probe.writer.transport.abort.assert_called_once()
    assert probe.backend._startup_task is probe.backend._shutdown_task is probe.backend._reap_task is None


@pytest.mark.asyncio
async def test_probe_rejects_backend_that_could_own_a_player(probe):
    probe.backend.auto_spawn = True
    with pytest.raises(ValueError, match="fresh no-spawn"):
        await probe.backend.diagnose_audio_output()
    probe.connect.assert_not_awaited()


def test_cli_disk_config_is_independent_from_live_intent_and_state(probe, tmp_path, monkeypatch, capsys):
    state = tmp_path / SECRET
    state.mkdir()
    sentinel = state / "gain.json"
    sentinel.write_text("not valid saved state")
    path = tmp_path / f"{SECRET}.yaml"
    path.write_text(yaml.safe_dump({"mpv_socket": f"/{SECRET}.sock", "state_dir": str(state),
                                    "mpv_audio_device": "null"}))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    forbidden = Mock(side_effect=AssertionError("saved state, service or HTTP reached"))
    monkeypatch.setattr(config, "_load_config_state", forbidden)
    monkeypatch.setattr(cli, "serve", forbidden)
    monkeypatch.setattr(cli, "api_get", forbidden)
    assert cli.main(["audio", "diagnose", "--config", str(path)]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert not captured.err and SECRET not in captured.out
    assert report["configuration"]["mpv_audio_device"] == "null"
    assert report["interpretation"]["configuration_vs_live_intent"] == "differs"
    assert report["interpretation"]["renderer_ownership"] == "unverified"
    assert report["interpretation"]["observations"] == "sequential_not_atomic"
    assert report["interpretation"]["audible_output"] == "not_verified"
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    forbidden.assert_not_called()


@pytest.mark.parametrize("configured,live,driver,comparison,null_output", [
    (None, "auto", "alsa", "not_compared", "not_observed"),
    ("null", "null", "null", "matches", "deliberately_silent"),
    ("auto", "auto", None, "matches", "not_observed"),
])
def test_cli_null_unset_and_idle_are_honest(probe, tmp_path, capsys, configured, live, driver,
                                           comparison, null_output):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"mpv_audio_device": configured}))
    probe.responses[:] = [
        {"request_id": 1, "error": "success", "data": live},
        {"request_id": 2, "error": "property unavailable" if driver is None else "success", "data": driver},
    ]
    assert cli.main(["audio", "diagnose", "--config", str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["interpretation"]["configuration_vs_live_intent"] == comparison
    assert report["interpretation"]["null_output"] == null_output
    assert report["interpretation"]["physical_device"] == "not_verified"
    assert report["interpretation"]["unavailable_driver"] == "can_be_idle_not_proof_of_playback_failure"


def test_cli_incomplete_and_invalid_config_have_distinct_private_exits(probe, tmp_path, capsys):
    path = tmp_path / f"{SECRET}.yaml"
    path.write_text("{}\n")
    probe.identity.return_value = None
    assert cli.main(["audio", "diagnose", "--config", str(path)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "incomplete" and report["live_mpv"]["connection"] == "socket_missing"
    for source in [f'friendly_name: "{SECRET}\n', "mpv_socket: []\n", "auto_spawn_mpv: maybe\n"]:
        path.write_text(source)
        assert cli.main(["audio", "diagnose", "--config", str(path)]) == 2
        captured = capsys.readouterr()
        assert captured.out == "" and captured.err.startswith("Audio diagnosis failed: ")
        assert SECRET not in captured.err and "Traceback" not in captured.err
    probe.connect.assert_not_awaited()


@pytest.mark.parametrize("argv", [
    ["audio"], ["audio", "diagnose"], ["audio", SECRET],
    ["audio", "diagnose", "--config", SECRET, "--host", SECRET],
    ["--bad", SECRET, "audio", "diagnose"],
])
def test_audio_parser_errors_do_not_echo_arguments(argv, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(argv)
    assert caught.value.code == 2
    captured = capsys.readouterr()
    assert SECRET not in captured.err
    assert captured.err == "Invalid client command arguments. Run open-room-renderer --help.\n"


def test_literal_audio_values_preserve_legacy_parsing(monkeypatch, capsys):
    api = Mock(return_value=0)
    monkeypatch.setattr(cli, "api_get", api)
    assert cli.main(["play", "audio", "--host", "audio"]) == 0
    api.assert_called_once_with("audio", 8080, "setPlayerCmd:play:audio")
    assert cli.build_parser().parse_args(["serve", "--config", "audio"]).config == "audio"
    assert cli.build_parser().parse_args(["favorites", "add", "audio", "http://example.test/a"]).name == "audio"
    with pytest.raises(SystemExit):
        cli.main(["serve", "--config", "audio", "--legacy-error"])
    assert "unrecognized arguments: --legacy-error" in capsys.readouterr().err
