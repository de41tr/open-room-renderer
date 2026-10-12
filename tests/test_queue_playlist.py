"""UNIT/MOCK current-queue projection and real HTTP admission; no media I/O."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
import pytest

from fake_linkplay.playlist_files import MAX_PLAYLIST_BYTES, decode_playlist
from fake_linkplay.queue import PlaybackQueue, QueueItem
from fake_linkplay.queue_client import MODES
from fake_linkplay.queue_playlist import PATH, QueuePlaylistApi, encode_current_playlist
from fake_linkplay.radio import RadioResolution
from fake_linkplay.state import PlaybackPhase
from test_native_queue_staging import make_api, start
from test_queue_client_renderer import rich_queue
from test_shuffle_queue_playback import navigate, setup as shuffled_queue
from test_station_recovery import api, eof, finish
from test_station_url_refresh import FIRST, FRESH, REFERENCE

CANONICAL = "https://opml.radiotime.com/Tune.ashx?id=s12345&formats=mp3,aac&render=json"
URL = "https://media.example.test/音声/a%2fb?token=synthetic-secret&a=1+2#片"
EXPECTED_ERROR = {"error": "playlist_unavailable",
                  "message": "Current queue cannot be saved as a URL playlist"}


def snapshot(api):
    """Record both values and owned identities, including staging/recovery/RNG."""
    queue = api.state.queue
    return (asdict(api.state), id(queue), tuple(map(id, queue.items)), id(queue._shuffle),
            id(api._native_stage), id(api._station_recovery), id(api._station_recovery_task),
            repr(api._station_recovery), api.queue_context, api.queue_resolved_url,
            list(api.mpv.calls), getattr(api._shuffle_rng, "calls", None))


async def capture(api):
    before = snapshot(api)
    result = await QueuePlaylistApi(lambda: api.state.queue).handle(make_mocked_request("GET", PATH))
    assert snapshot(api) == before
    assert result.status == 200 and result.headers["Cache-Control"] == "no-store"
    return decode_playlist(result.body)


@pytest.mark.parametrize("mode,loop", MODES.items())
def test_exact_projection_retains_order_duplicates_index_and_all_modes(mode, loop):
    queue = rich_queue(loop=loop)
    queue.name = "Café 夜のラジオ 🎵"
    queue.items[0].url = queue.items[-1].url = URL
    queue.items[0].resolved_url = "http://127.0.0.1:8080/proxy/audio?private-resolved"
    before = asdict(queue)
    encoded = encode_current_playlist(queue)
    assert decode_playlist(encoded) == {"version": 1, "name": queue.name,
        "index": 2, "mode": mode, "urls": [URL, queue.items[1].url, URL]}
    assert asdict(queue) == before
    assert b"private-" not in encoded and b"_radio_" not in encoded
    # The returned bytes own no queue references, even after later mutation.
    queue.items.clear()
    queue.name = "Changed after capture"
    assert len(decode_playlist(encoded)["urls"]) == 3
    assert decode_playlist(encoded)["name"] == "Café 夜のラジオ 🎵"


@pytest.mark.parametrize("field,value", [
    ("items", None), ("items", (QueueItem(URL),)), ("items", [None]),
    ("items", [QueueItem(URL)] * 1001), ("index", 0), ("index", 4),
    ("index", True), ("index", 1.0), ("loop", True), ("loop", 5),
    ("loop", "normal"), ("name", ""), ("name", " spaced "),
    ("name", "bad\rname"), ("name", "bad\ud800name"),
])
async def test_invalid_mutable_structure_is_fixed_all_or_nothing_conflict(field, value):
    queue = rich_queue()
    setattr(queue, field, value)
    response = await QueuePlaylistApi(lambda: queue).handle(make_mocked_request("GET", PATH))
    assert response.status == 409 and response.headers["Cache-Control"] == "no-store"
    assert json.loads(response.body) == EXPECTED_ERROR


@pytest.mark.parametrize("url", [
    "file:///synthetic-secret", "https://user:synthetic-secret@media.example.test/a",
    "https://media.example.test/a b", "https://media.example.test:0/a", "https:///a",
    "http://127.0.0.1:8080/%70roxy/audio?synthetic-secret", "not-a-url-synthetic-secret",
])
async def test_invalid_last_item_does_not_return_valid_prefix(url):
    queue = rich_queue()
    queue.items[-1].url = url
    response = await QueuePlaylistApi(lambda: queue).handle(make_mocked_request("GET", PATH))
    assert response.status == 409 and b"synthetic-secret" not in response.body
    assert queue.items[-1].url == url


@pytest.mark.parametrize("queue", [PlaybackQueue(), PlaybackQueue(source="newTuneIn", stream_id="s12345")])
async def test_empty_and_unresolved_provider_queues_are_not_exported(queue):
    response = await QueuePlaylistApi(lambda: queue).handle(make_mocked_request("GET", PATH))
    assert response.status == 409


def test_exact_item_limit_and_independent_json_and_escaped_soap_limits():
    queue = PlaybackQueue(items=[QueueItem(URL)] * 1000, index=1000)
    assert len(decode_playlist(encode_current_playlist(queue))["urls"]) == 1000
    for suffix in ("x" * MAX_PLAYLIST_BYTES, "&" * 120000):
        queue = PlaybackQueue(items=[QueueItem("https://media.example.test/?q=" + suffix)], index=1)
        with pytest.raises(ValueError):
            encode_current_playlist(queue)


@pytest.mark.parametrize("provenance,stream_type,duration", [
    ((), "station", 0), ((FIRST,), "station", 0), ((FIRST, "s12345", "extra"), "station", 0),
    ([FIRST, "s12345"], "station", 0), ({"url": FIRST, "station_id": "s12345"}, "station", 0),
    ("s12345", "station", 0), (False, "station", 0), ((FRESH, "s12345"), "station", 0),
    ((None, "s12345"), "station", 0), ((FIRST, "s12345"), "track", 0),
    ((FIRST, "s12345"), "", 0), ((FIRST, "s12345"), "station", 30),
    *(( (FIRST, "s12345"), "station", duration) for duration in (float("nan"), False, None, "0", 0j)),
    *(( (FIRST, station), "station", 0) for station in
      (None, 12345, "", "S12345", "t12345", "s１２３", "s1\n", "s1&account=private", "s" + "1" * 33)),
])
async def test_malformed_inactive_radio_provenance_rejects_the_entire_snapshot(provenance, stream_type, duration):
    queue = PlaybackQueue(items=[QueueItem(URL), QueueItem(FIRST)], index=1)
    item = queue.items[-1]
    item._radio_station, item.stream_type, item.duration = provenance, stream_type, duration
    response = await QueuePlaylistApi(lambda: queue).handle(make_mocked_request("GET", PATH))
    assert response.status == 409 and FIRST.encode() not in response.body
    assert item._radio_station is provenance


async def test_unproven_station_rejected_but_unresolved_explicit_public_reference_is_ordinary():
    queue = PlaybackQueue(items=[QueueItem(URL), QueueItem(REFERENCE)], index=1)
    queue.items[-1].stream_type = "station"
    queue.items[-1].source, queue.items[-1].id = "newTuneIn", "s12345"
    queue.items[-1].metadata = '{"_radio_station": ["temporary", "s12345"]}'
    response = await QueuePlaylistApi(lambda: queue).handle(make_mocked_request("GET", PATH))
    assert response.status == 409
    queue.items[-1].stream_type = ""
    assert decode_playlist(encode_current_playlist(queue))["urls"] == [URL, REFERENCE]


@pytest.mark.parametrize("transport", ["playing", "pause", "stop"])
async def test_snapshot_during_transport_does_not_change_any_playback_ownership(api, transport, monkeypatch):
    await shuffled_queue(api)
    await navigate(api)
    if transport != "playing":
        await api._transport_command(transport)
    async def forbidden(*args, **kwargs):
        pytest.fail("snapshot must not await backend, resolver or synchronization")
    monkeypatch.setattr(api, "sync_mpv_state", forbidden)
    monkeypatch.setattr(api, "_resolve_playback_url", forbidden)
    monkeypatch.setattr(api.mpv, "snapshot", forbidden)
    payload = await capture(api)
    assert payload["mode"] == "shuffle-once" and payload["index"] == 4
    assert api.state.phase == {"playing": PlaybackPhase.PLAYING,
                               "pause": PlaybackPhase.PAUSED, "stop": PlaybackPhase.STOPPED}[transport]


async def test_snapshot_preserves_real_staged_successor_identity_and_backend_queue(tmp_path):
    api = make_api(tmp_path, urls=(URL, URL, FIRST))
    await start(api)
    assert api._native_stage is not None
    native = [entry.copy() for entry in api.mpv.entries]
    assert (await capture(api))["urls"] == [URL, URL, FIRST]
    assert api.mpv.entries == native


async def test_header_radio_before_play_during_recovery_and_after_refresh_never_leaks_temporary(api):
    api._resolve_station_id = AsyncMock(return_value=RadioResolution(FIRST, "s12345"))
    queue = PlaybackQueue(name="Radio", source="newTuneIn", stream_id="s12345")
    await api._materialize_radio_queue(queue)
    item = queue.current
    assert item.url == FIRST and not item._radio_resolution_attempted
    assert (await capture(api))["urls"] == [CANONICAL]
    assert api._resolve_station_id.await_count == 1 and not api.mpv.calls
    await api._play_queue_index(1, queue.name)
    api._resolve_station_id.return_value = RadioResolution(FRESH, "s12345")
    api._station_recovery_delays = (0.01, 0.01, 0.01)
    task = await eof(api)
    assert task is not None and api.state.phase == PlaybackPhase.BUFFERING
    assert (await capture(api))["urls"] == [CANONICAL]
    assert api._station_recovery_task is task
    await finish(task)
    assert item.resolved_url == FRESH and api._resolve_station_id.await_count == 2
    assert (await capture(api))["urls"] == [CANONICAL]
    assert api._resolve_station_id.await_count == 2 and api.state.queue.current is item


async def test_capture_reads_queue_once_and_detaches_bytes_before_yield():
    state = SimpleNamespace(queue=PlaybackQueue(items=[QueueItem(URL)], index=1))
    original, calls = state.queue, []
    def current():
        calls.append(True)
        asyncio.get_running_loop().call_soon(setattr, state, "queue", PlaybackQueue())
        return state.queue
    response = await QueuePlaylistApi(current).handle(make_mocked_request("GET", PATH))
    assert state.queue is original and calls == [True]
    await asyncio.sleep(0)
    assert state.queue is not original
    original.items.clear()
    assert decode_playlist(response.body)["urls"] == [URL]


@pytest.mark.parametrize("method,suffix,body,headers,status", [
    ("GET", "", None, {}, 200), ("GET", "?ignored=synthetic-secret", None, {}, 400),
    ("GET", "", b"synthetic-secret", {}, 400),
    ("GET", "", b"x" * (MAX_PLAYLIST_BYTES + 1), {}, 400),
    ("POST", "", None, {}, 405), ("HEAD", "", None, {}, 405),
    ("PUT", "", None, {}, 405), ("DELETE", "", None, {}, 405),
    ("OPTIONS", "", None, {}, 405),
])
async def test_real_http_route_rejects_unexpected_inputs_without_snapshot(method, suffix, body, headers, status):
    calls = []
    def current():
        calls.append(True)
        return PlaybackQueue(items=[QueueItem(URL)], index=1)
    app = web.Application()
    app.add_routes(QueuePlaylistApi(current).routes())
    async with TestClient(TestServer(app)) as client:
        response = await client.request(method, PATH + suffix, data=body, headers=headers)
        assert response.status == status
        if status == 200:
            assert decode_playlist(await response.read())["urls"] == [URL]
        else:
            assert b"synthetic-secret" not in await response.read()
        assert calls == ([True] if status == 200 else [])
        if status != 405:
            assert response.headers["Cache-Control"] == "no-store"


async def test_chunked_request_is_rejected_even_when_body_is_empty():
    calls = []
    app = web.Application()
    app.add_routes(QueuePlaylistApi(lambda: calls.append(True)).routes())
    async def chunks():
        yield b""
    async with TestClient(TestServer(app)) as client:
        response = await client.get(PATH, data=chunks())
        assert response.status == 400 and not calls
