"""Owned local synthetic WebSockets -> existing leased renderer buffer, no Spotify."""
import asyncio
import datetime as dt
import json
import os
import struct

import pytest
from aiohttp import web

from fake_linkplay import spotify_source_diagnostic as observer
from fake_linkplay.diagnostics import Diagnostics, Reason
from test_spotify_diagnostic import peer
from test_spotify_eq import started

COMMIT = "c" * 40
PRIVATE = "synthetic-private-account-title-token"
PREFIX = "failed creating stream for spotify:track:" + "Z" * 22 + ": "
KEY = PREFIX + "failed retrieving audio key: failed retrieving aes key with code "


def error(message, **values):
    return {"stage": "load", "kind": "restricted", "unplayable": True,
            "message": message, "uri": PRIVATE, "context_uri": PRIVATE,
            "play_origin": {"device_identifier": PRIVATE}, **values}


@pytest.mark.parametrize("data,reason", [
    (error(PREFIX + "media is restricted"), Reason.SPOTIFY_MEDIA_RESTRICTED),
    (error(KEY + "1"), Reason.SPOTIFY_KEY_REFUSED_1),
    (error(KEY + "0"), Reason.SPOTIFY_KEY_REFUSED_OTHER),
    (error(KEY + "65535"), Reason.SPOTIFY_KEY_REFUSED_OTHER),
    (error(KEY + "65536"), Reason.SPOTIFY_RESTRICTED_UNKNOWN),
    (error(KEY + "01"), Reason.SPOTIFY_RESTRICTED_UNKNOWN),
    (error(KEY + "-1"), Reason.SPOTIFY_RESTRICTED_UNKNOWN),
    (error(KEY + "1\n"), Reason.SPOTIFY_RESTRICTED_UNKNOWN),
    (error(KEY + "1 " + PRIVATE), Reason.SPOTIFY_RESTRICTED_UNKNOWN),
    (error(PRIVATE + KEY + "1"), Reason.SPOTIFY_RESTRICTED_UNKNOWN),
    (error("media is restricted"), Reason.SPOTIFY_RESTRICTED_UNKNOWN),
    (error({"message": KEY + "1"}), Reason.SPOTIFY_RESTRICTED_UNKNOWN),
    (error(None), Reason.SPOTIFY_RESTRICTED_UNKNOWN),
    (error(KEY + "1", unplayable=1), Reason.SPOTIFY_ERROR_OTHER),
    (error(KEY + "1", stage="playback"), Reason.SPOTIFY_ERROR_OTHER),
    (error(KEY + "1", kind="unknown"), Reason.SPOTIFY_ERROR_OTHER),
    (None, Reason.SPOTIFY_ERROR_OTHER),
])
def test_only_whole_pinned_load_errors_export_fixed_reasons(data, reason):
    assert observer.playback_error_reason(data) is reason


async def lease(api, tmp_path, test_id="synthetic-spotify-errors"):
    api.diagnostics = Diagnostics(tmp_path, lambda: api.state, version="0.1.0", commit=COMMIT)
    now = dt.datetime.now(dt.timezone.utc)
    path = tmp_path / "diagnostic-window.json"
    path.write_text(json.dumps({"test_id": test_id, "commit": COMMIT,
        "started_at": now.isoformat(), "expires_at": (now + dt.timedelta(seconds=120)).isoformat()}))
    path.chmod(0o600)
    await api.diagnostics.pump()


@pytest.mark.asyncio
async def test_real_owned_events_reach_existing_lease_buffer_without_raw_data(tmp_path, monkeypatch, caplog):
    source, api, backend = await started(tmp_path, monkeypatch)
    source.child.pid = os.getpid()  # The synthetic TCP listener really belongs to this PID.
    async def handler(request):
        assert request.path == "/events"
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        for message in (PREFIX + "media is restricted", KEY + "1", KEY + "42", PRIVATE):
            await ws.send_json({"type": "playback_error", "data": error(message)})
        await ws.send_json({"type": "metadata", "data": {"title": PRIVATE}})
        await ws.close()
        return ws

    try:
        async with peer(handler) as (port, requests):
            source.profile["port"] = port
            await source.observe()
            assert source._diagnostic_task is None and requests == []  # Default off.
            await lease(api, tmp_path)
            await source.observe()  # The existing renderer monitor's entry point.
            await asyncio.wait_for(source._diagnostic_task, 3)
            await api.diagnostics.pump()
            saved = tmp_path / "diagnostic-buffer.json"
            raw = saved.read_text()
            data = json.loads(raw)
            assert saved.stat().st_mode & 0o777 == 0o600
            assert PRIVATE not in raw + caplog.text and "spotify:track:" not in raw
            assert "failed retrieving" not in raw and "media is restricted" not in raw
            assert [e["reason"] for e in data["events"]] == [
                "spotify_events_connected", "spotify_media_restricted", "spotify_key_refused_code_1",
                "spotify_key_refused_other_code", "spotify_load_restricted_unknown",
                "spotify_events_disconnected"]
            assert all(e["protocol"] == "backend" and e["endpoint"] == "unknown"
                       and e["action"] == "unknown" and e["stage"] == "playback"
                       and e["outcome"] == "unknown" and e["http_code"] is None
                       and e["soap_code"] is None for e in data["events"])
            assert [(method, path) for method, path, _ in requests] == [("GET", "/events")]
            await source.observe()
            assert len(requests) == 1  # Disconnection never reconnects this lease.
            assert not source.failed and backend.stops == 0
    finally:
        await source.close()


@pytest.mark.asyncio
async def test_stop_cancels_quiet_observer_and_releases_owned_source(tmp_path, monkeypatch):
    source, api, backend = await started(tmp_path, monkeypatch)
    source.child.pid = os.getpid()
    connected = asyncio.Event()
    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        connected.set()
        await ws.receive()
        return ws
    async with peer(handler) as (port, requests):
        source.profile["port"] = port
        await lease(api, tmp_path)
        await source.observe()
        await asyncio.wait_for(connected.wait(), 3)
        await asyncio.wait_for(source.close(), 2)
        assert source._diagnostic_task.done() and backend.stops == 1
        assert api.spotify_source is None and source.child.terminated
        assert len(requests) == 1


@pytest.mark.asyncio
async def test_unowned_listener_receives_no_http_bytes(tmp_path, monkeypatch):
    source, api, backend = await started(tmp_path, monkeypatch)
    # Fake child PID is not the process that owns this real TCP listener.
    source.child.pid = 2147483647
    async def handler(request):
        pytest.fail("Unowned listener must not receive an HTTP request")
    try:
        async with peer(handler) as (port, requests):
            source.profile["port"] = port
            await lease(api, tmp_path)
            await source.observe()
            await asyncio.wait_for(source._diagnostic_task, 3)
            assert requests == [] and not source.failed and backend.stops == 0
            assert [e["reason"] for e in api.diagnostics.events] == ["spotify_events_unavailable"]
    finally:
        await source.close()


@pytest.mark.asyncio
async def test_flood_limit_and_new_lease_fence(tmp_path, monkeypatch):
    source, api, backend = await started(tmp_path, monkeypatch)
    source.child.pid = os.getpid()
    monkeypatch.setattr(observer, "MAX_FRAMES", 4)
    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        for _ in range(10):
            await ws.send_json({"type": "playback_error", "data": error(KEY + "1")})
        await ws.receive()
        return ws
    try:
        async with peer(handler) as (port, requests):
            source.profile["port"] = port
            await lease(api, tmp_path)
            await source.observe()
            await asyncio.wait_for(source._diagnostic_task, 3)
            assert len(api.diagnostics.events) == 6
            assert api.diagnostics.events[-1]["reason"] == "spotify_events_limit"
            before = list(api.diagnostics.events)
            api.diagnostics.background(Reason.SPOTIFY_MEDIA_RESTRICTED, "different-window")
            assert api.diagnostics.events == before
            await source.observe()
            assert len(requests) == 1
            api.diagnostics.deadline = 0
            await source.observe()
            assert not api.diagnostics.active() and len(requests) == 1
            await lease(api, tmp_path, "synthetic-next-window")
            await source.observe()
            await asyncio.wait_for(source._diagnostic_task, 3)
            assert len(requests) == 2 and not source.failed
    finally:
        await source.close()


@pytest.mark.asyncio
async def test_expiry_cancels_quiet_connection_without_late_or_replayed_events(tmp_path, monkeypatch):
    source, api, _ = await started(tmp_path, monkeypatch)
    source.child.pid = os.getpid()
    connected = asyncio.Event()
    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        connected.set()
        await ws.receive()
        return ws
    try:
        async with peer(handler) as (port, requests):
            source.profile["port"] = port
            await lease(api, tmp_path)
            await source.observe()
            await asyncio.wait_for(connected.wait(), 3)
            api.diagnostics.deadline = 0
            before = list(api.diagnostics.events)
            await source.observe()
            await asyncio.gather(source._diagnostic_task, return_exceptions=True)
            api.diagnostics.background(Reason.SPOTIFY_KEY_REFUSED_1, "synthetic-spotify-errors")
            assert api.diagnostics.events == before
            await source.observe()
            assert len(requests) == 1 and not source.failed
    finally:
        await source.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["redirect", "malformed", "oversized_frame"])
async def test_untrusted_wire_fails_privately_without_reconnect_or_playback(tmp_path, monkeypatch, caplog, response):
    source, api, backend = await started(tmp_path, monkeypatch)
    source.child.pid = os.getpid()
    async def handler(request):
        if response == "redirect":
            return web.Response(status=302, headers={"Location": "http://example.invalid/" + PRIVATE})
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if response == "malformed":
            await ws.send_str('{"type":"playback_error","data":"' + PRIVATE)
        else:
            request.transport.write(b"\x81\x7f" + struct.pack("!Q", 2**40))
        await ws.receive()
        return ws
    try:
        async with peer(handler) as (port, requests):
            source.profile["port"] = port
            await lease(api, tmp_path)
            await source.observe()
            await asyncio.wait_for(source._diagnostic_task, 3)
            await source.observe()
            assert len(requests) == 1 and requests[0][:2] == ("GET", "/events")
            assert api.diagnostics.events[-1]["reason"] == "spotify_events_unavailable"
            assert PRIVATE not in api.diagnostics.snapshot().decode() + caplog.text
            assert not source.failed and backend.stops == 0
    finally:
        await source.close()


@pytest.mark.asyncio
async def test_fragmented_message_and_protocol_ping_remain_bounded():
    from unittest.mock import AsyncMock, Mock
    reader = asyncio.StreamReader()
    writer = Mock(drain=AsyncMock())
    body = json.dumps({"type": "playback_error", "data": error(KEY + "1")}).encode()
    split = len(body) // 2
    def frame(op, data):
        return bytes([op, 126]) + struct.pack("!H", len(data)) + data
    reader.feed_data(frame(1, body[:split]) + b"\x89\x00" + frame(128, body[split:]))
    events = observer._Events(reader, writer)
    result = await events.message()
    assert observer.playback_error_reason(result["data"]) is Reason.SPOTIFY_KEY_REFUSED_1
    assert events.frames == 3 and events.size < observer.MAX_WIRE_BYTES
    assert writer.write.call_args[0][0][:2] == b"\x8a\x80"  # Masked empty protocol pong.


@pytest.mark.asyncio
async def test_cancelled_connection_closes_late_socket(monkeypatch):
    from unittest.mock import Mock
    ready = asyncio.Event()
    sock = Mock()
    async def late_connect(_):
        await ready.wait()
        return sock
    monkeypatch.setattr(observer.asyncio, "to_thread", late_connect)
    task = asyncio.create_task(observer._connect(1, "1", lambda: None))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    ready.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    sock.close.assert_called_once()
