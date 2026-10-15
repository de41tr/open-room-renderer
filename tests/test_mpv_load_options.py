"""Version-independent load options and acknowledgement failure handling."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from fake_linkplay.mpv_backend import BackendError, MpvBackend


_URL = "https://media.example.test/tone.wav"
_OPTIONS = {"start": "8", "demuxer": "lavf", "force-seekable": "yes", "cache": "no"}


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [None, {}, _OPTIONS])
async def test_loadfile_options_use_named_arguments_without_version_probe(options):
    backend = MpvBackend("/synthetic-mpv.sock", auto_spawn=False)
    backend._initialize_gain_locked = AsyncMock()
    backend.command = AsyncMock(return_value={"error": "success"})
    assert await backend.loadfile(_URL, options) == {"error": "success"}
    expected = ({"name": "loadfile", "url": _URL, "flags": "replace", "options": options}
                if options else ["loadfile", _URL, "replace"])
    assert backend.command.await_args_list == [
        call(expected, timeout=3.0), call(["set_property", "pause", False], timeout=3.0),
    ]
    backend._initialize_gain_locked.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"error": "invalid parameter"}, {"error": "mpv IPC command timed out"}, {},
])
async def test_failed_option_load_is_never_retried_or_unpaused(response):
    backend = MpvBackend("/synthetic-mpv.sock", auto_spawn=False)
    backend._initialize_gain_locked = AsyncMock()
    backend.command = AsyncMock(return_value=response)
    result = await backend.loadfile(_URL, _OPTIONS)
    assert result["error"] == (response.get("error") or "mpv loadfile acknowledgement missing")
    backend.command.assert_awaited_once_with(
        {"name": "loadfile", "url": _URL, "flags": "replace", "options": _OPTIONS}, timeout=3.0,
    )


@pytest.mark.asyncio
async def test_named_option_load_reports_unpause_failure_without_retry():
    backend = MpvBackend("/synthetic-mpv.sock", auto_spawn=False)
    backend._initialize_gain_locked = AsyncMock()
    backend.command = AsyncMock(side_effect=[{"error": "success"}, {"error": "unpause failed"}])
    assert await backend.loadfile(_URL, _OPTIONS) == {"error": "unpause failed"}
    assert backend.command.await_count == 2


@pytest.mark.asyncio
async def test_named_command_round_trips_json_and_correlates_acknowledgement(monkeypatch):
    backend = MpvBackend("/synthetic-mpv.sock", auto_spawn=False)
    backend.start = AsyncMock()
    sent = []
    writer = SimpleNamespace(
        write=lambda data: sent.append(json.loads(data)), drain=AsyncMock(), close=Mock(),
        wait_closed=AsyncMock(), transport=Mock(),
    )
    responses = [
        b'{"event":"start-file"}\n', b'{"request_id":99,"error":"success"}\n',
        b'{"request_id":1,"error":"success"}\n',
    ]
    reader = SimpleNamespace(readline=AsyncMock(side_effect=responses))
    monkeypatch.setattr(asyncio, "open_unix_connection", AsyncMock(return_value=(reader, writer)))
    command = {"name": "loadfile", "url": _URL, "flags": "replace", "options": _OPTIONS}
    assert await backend.command(command) == {"request_id": 1, "error": "success"}
    assert sent == [{"command": command, "request_id": 1}]
    writer.close.assert_called_once()
    writer.transport.abort.assert_called_once()


@pytest.mark.asyncio
async def test_named_command_transport_failure_keeps_error_and_safe_logging(monkeypatch, caplog):
    backend = MpvBackend("/synthetic-mpv.sock", auto_spawn=False)
    backend.start = AsyncMock()
    monkeypatch.setattr(asyncio, "open_unix_connection", AsyncMock(side_effect=OSError("Synthetic failure")))
    with caplog.at_level(logging.WARNING):
        result = await backend.command({"name": "loadfile", "url": _URL + "?token=private-fixture", "options": _OPTIONS})
    assert result == {"error": "Synthetic failure"}
    assert "command loadfile failed" in caplog.text
    assert "private-fixture" not in caplog.text


@pytest.mark.asyncio
async def test_paused_load_requires_hold_ack_and_never_autounpauses():
    backend = MpvBackend("/synthetic-mpv.sock", auto_spawn=False)
    backend._initialize_gain_locked = AsyncMock()
    backend.command = AsyncMock(return_value={"error": "hold rejected"})
    with pytest.raises(BackendError):
        await backend.loadfile(_URL, start_paused=True)
    backend.command.assert_awaited_once_with(["set_property", "pause", True], timeout=2.0)
    backend.command.reset_mock()
    backend.command.return_value = {"error": "success"}
    assert await backend.loadfile(_URL, start_paused=True) == {"error": "success"}
    assert backend.command.await_args_list == [
        call(["set_property", "pause", True], timeout=2.0),
        call(["loadfile", _URL, "replace"], timeout=3.0)]


@pytest.mark.asyncio
async def test_resume_checks_owner_after_dsp_and_gain_before_unpause():
    backend = MpvBackend("/synthetic-mpv.sock", auto_spawn=False)
    events = []
    backend._dsp_managed = True
    async def dsp(*args):
        events.append("dsp")
    async def gain(*args, **kwargs):
        events.append("gain")
    async def current():
        events.append("owner")
        return False
    backend._apply_audio_filter_graph_locked = dsp
    backend._initialize_gain_locked = gain
    backend.command = AsyncMock(return_value={"error": "success"})
    with pytest.raises(BackendError, match="superseded"):
        await backend.resume(current=current)
    assert events == ["dsp", "gain", "owner"]
    backend.command.assert_not_awaited()
