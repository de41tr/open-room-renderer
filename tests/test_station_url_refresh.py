"""UNIT/MOCK resolver provenance, finite refresh budget and owner fences."""
import asyncio
from dataclasses import asdict
from html import escape
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET

import pytest

from fake_linkplay.http_api import ParsedCommand
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.queue import PlaybackQueue, QueueItem, parse_queue_context, serialize_queue_context
from fake_linkplay.radio import RadioResolution, RadioResolutionError
from fake_linkplay.state import PlaybackPhase
from fake_linkplay.upnp_description import avtransport_response_fields
from test_station_recovery import api, eof, finish, loads
from test_station_stall_recovery import expire


FIRST = "https://media.example.test/expired.mp3"
FRESH = "https://media.example.test/fresh.mp3"
NEXT = "https://media.example.test/next.wav"
REFERENCE = "https://opml.radiotime.com/Tune.ashx?id=s12345&ignored=synthetic"
ARTWORK = "https://media.example.test/station.png"


async def select(api, mode="header"):
    api._resolve_station_id = AsyncMock(return_value=RadioResolution(FIRST, "s12345"))
    if mode == "header":
        queue = PlaybackQueue(name="Synthetic station", source="newTuneIn", stream_id="s12345",
                              list_name="Station title", artwork=ARTWORK,
                              search_url="https://metadata.example.test/opaque")
        await api._materialize_radio_queue(queue)
        await api._play_queue_index(1, queue.name)
    elif mode == "queue":
        queue = PlaybackQueue(name="Recognized reference", items=[
            QueueItem(REFERENCE, title="Station title", stream_type="station", artwork=ARTWORK),
        ], index=1)
        await api._replace_queue(queue)
        await api._play_queue_index(1, queue.name)
    elif mode == "http":
        await api.execute(ParsedCommand("play", (REFERENCE,)), "192.0.2.1")
    else:
        assert mode == "soap"
        await api._execute_soap_side_effect("SetAVTransportURI", f"<CurrentURI>{escape(REFERENCE)}</CurrentURI>")
        await api._execute_soap_side_effect("Play", "<Play/>")
    api._resolve_station_id.assert_awaited_once_with("s12345")
    api._resolve_station_id.return_value = RadioResolution(FRESH, "s12345")
    return api.state.queue


@pytest.mark.parametrize("mode", ["header", "queue", "http", "soap"])
async def test_refresh_preserves_selection_and_publishes_current_resource(api, mode):
    queue = await select(api, mode)
    selected, generation, requested = queue.current, api.state.generation, api.state.current_url
    before = asdict(queue)
    await finish(await eof(api))
    assert loads(api) == [FIRST, FRESH]
    assert api._resolve_station_id.await_count == 2
    assert api._station_recovery.backend_url == FRESH and api._station_recovery.attempts == 1
    assert api.state.queue is queue and queue.current is selected and api.state.generation == generation
    assert selected.url == api.state.current_url == api.state.backend_original_url == requested
    assert selected.resolved_url == api.state.resolved_url == api.queue_resolved_url == api.state.backend_url == FRESH
    assert api.state.phase == PlaybackPhase.PLAYING and not api.state.last_error
    after = asdict(queue)
    before["items"][0]["resolved_url"] = FRESH
    if selected.title_is_fallback:
        # Fresh backend titles remain eligible when no title was supplied.
        before["items"][0]["title"] = after["items"][0]["title"]
    assert after == before
    assert api.state.as_player_status()["stream_url"] == requested
    assert api.state.as_player_status()["resolved_stream_url"] == FRESH
    assert f"<CurrentURI>{FRESH}</CurrentURI>" in avtransport_response_fields("GetMediaInfo", api.state)
    assert f"<TrackURI>{FRESH}</TrackURI>" in avtransport_response_fields("GetPositionInfo", api.state)
    assert parse_queue_context(api._build_queue_context()).current.url == FRESH
    assert parse_queue_context(api.queue_context).current.url == FRESH
    event = ET.fromstring(api._event_body("AVTransport"))
    last_change = next(node.text for node in event.iter() if node.tag.endswith("LastChange"))
    assert f'CurrentTrackURI val="{FRESH}"' in last_change
    queue_event = ET.fromstring(api._event_body("PlayQueue"))
    assert next(node.text for node in queue_event.iter() if node.tag == "TrackURI") == FRESH


async def test_each_recovery_refreshes_once_with_one_unchanged_finite_budget(api):
    await select(api)
    for count in range(1, 4):
        url = f"https://media.example.test/refreshed-{count}.mp3"
        api._resolve_station_id.return_value = RadioResolution(url, "s12345")
        await finish(await eof(api))
        assert api._station_recovery.attempts == count
        assert api._station_recovery.backend_url == url
        assert api.mpv.url == url
    assert await eof(api) is None
    assert api.state.phase == PlaybackPhase.ERROR
    for _ in range(10):
        await api.sync_mpv_state()
    assert api._resolve_station_id.await_count == 4 and len(loads(api)) == 4
    assert api._queue_advance_task is None


async def test_newly_proven_station_skips_optional_finite_media_proxy(api):
    api.cfg.proxy_on_demand_audio = True
    await select(api, "http")
    assert loads(api) == [FIRST] and not api._proxy_sources
    await finish(await eof(api))
    assert loads(api) == [FIRST, FRESH] and not api._proxy_sources


@pytest.mark.parametrize("failure", [RadioResolutionError, OSError, TimeoutError])
async def test_failed_refresh_spends_existing_budget_without_reloading_expired_url(api, failure):
    await select(api)
    api._resolve_station_id.side_effect = failure("synthetic unavailable")
    await finish(await eof(api))
    assert api._station_recovery.attempts == 3
    assert api._resolve_station_id.await_count == 4
    assert loads(api) == [FIRST] and api.mpv.idle
    assert api.state.phase == PlaybackPhase.ERROR
    assert api.state.last_error == "Station source unavailable: recovery exhausted"
    assert api.state.queue.current.resolved_url == FIRST


async def test_refreshed_but_unready_loads_each_receive_stop_cleanup(api):
    await select(api)
    api._wait_for_mpv_audio_ready = AsyncMock(return_value=False)
    await finish(await eof(api))
    assert loads(api) == [FIRST, FRESH, FRESH, FRESH]
    assert len([1 for name, _ in api.mpv.calls if name == "stop"]) == 3
    assert api._resolve_station_id.await_count == 4 and api._station_recovery.attempts == 3
    assert api.mpv.idle and not api.state.backend_url
    assert api.state.phase == PlaybackPhase.ERROR


async def test_failed_idle_refresh_stops_returned_buffering_source_before_error(api):
    await select(api)
    async def failed_refresh(station_id):
        api.mpv.idle = False
        api.mpv.buffering = True
        raise RadioResolutionError("Synthetic resolver failure after native return")
    api._resolve_station_id.side_effect = failed_refresh
    await finish(await eof(api))
    assert api._station_recovery.attempts == 3
    assert api.state.phase == PlaybackPhase.ERROR
    assert api.state.last_error == "Station source unavailable: recovery exhausted"
    assert loads(api) == [FIRST] and api.mpv.idle
    assert [name for name, _ in api.mpv.calls].count("stop") == 1
    api.mpv.buffering = False
    await api.sync_mpv_state()
    assert api.mpv.idle and api.state.phase == PlaybackPhase.ERROR


@pytest.mark.parametrize("audio_returned", [False, True])
async def test_already_exhausted_active_eof_stops_only_if_audio_has_not_returned(api, audio_returned):
    await select(api)
    api._station_recovery.attempts = 3
    api.mpv.buffering = True
    api._apply_backend_snapshot({**await api.mpv.snapshot(), "eof-reached": True})
    task = api._station_recovery_task
    assert task is not None
    if audio_returned:
        api.mpv.buffering = False
    await finish(task)
    assert loads(api) == [FIRST]
    assert api._resolve_station_id.await_count == 1
    assert api.mpv.idle is (not audio_returned)
    assert api.state.phase == (PlaybackPhase.PLAYING if audio_returned else PlaybackPhase.ERROR)


async def test_initial_header_load_failure_before_ack_refreshes_on_owner_play(api):
    api._resolve_station_id = AsyncMock(return_value=RadioResolution(FIRST, "s12345"))
    queue = PlaybackQueue(name="Initial failure", source="newTuneIn", stream_id="s12345")
    await api._materialize_radio_queue(queue)
    original_load = api.mpv.loadfile
    async def fail_load(url, options=None, *, start_paused=False):
        api.mpv.calls.append(("load", url))
        raise BackendError("Synthetic failure before acknowledgement")
    api.mpv.loadfile = fail_load
    with pytest.raises(BackendError):
        await api._play_queue_index(1, queue.name)
    assert api._resolve_station_id.await_count == 1
    assert not queue.current.resolved_url and queue.current._radio_resolution_attempted
    assert api._station_recovery is None and api._station_recovery_task is None
    api.mpv.loadfile = original_load
    api._resolve_station_id.return_value = RadioResolution(FRESH, "s12345")
    await api._play_queue_index(1, queue.name)
    assert api._resolve_station_id.await_count == 2
    assert loads(api) == [FIRST, FRESH] and api.state.phase == PlaybackPhase.PLAYING


async def test_cancelled_header_play_before_load_does_not_consume_resolution(api):
    api._resolve_station_id = AsyncMock(return_value=RadioResolution(FIRST, "s12345"))
    queue = PlaybackQueue(name="Deferred first play", source="newTuneIn", stream_id="s12345")
    await api._materialize_radio_queue(queue)
    await api._backend_lock.acquire()
    pending = asyncio.create_task(api._play_queue_index(1, queue.name))
    await asyncio.sleep(0)
    stopped = asyncio.create_task(api.execute(ParsedCommand("stop"), "192.0.2.1"))
    await asyncio.sleep(0)
    api._backend_lock.release()
    await stopped
    with pytest.raises(BackendError, match="superseded"):
        await pending
    assert not queue.current._radio_resolution_attempted and not loads(api)
    await api._play_queue_index(1, queue.name)
    assert api._resolve_station_id.await_count == 1 and loads(api) == [FIRST]


@pytest.mark.parametrize("command", ["pause", "stop", "newsource"])
@pytest.mark.parametrize("late_failure", [False, True])
async def test_owner_command_wins_through_resolver_await_even_if_cancellation_is_ignored(api, command, late_failure):
    await select(api)
    entered, release = asyncio.Event(), asyncio.Event()
    async def resolve(station_id):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        if late_failure:
            raise RadioResolutionError("Synthetic late failure")
        return RadioResolution(FRESH, station_id)
    api._resolve_station_id.side_effect = resolve
    task = await eof(api)
    await entered.wait()
    parsed = ParsedCommand("play", (NEXT,)) if command == "newsource" else ParsedCommand(command)
    await api.execute(parsed, "192.0.2.1")
    before, calls = asdict(api.state), list(api.mpv.calls)
    release.set()
    await asyncio.gather(task, return_exceptions=True)
    assert asdict(api.state) == before and api.mpv.calls == calls
    assert FRESH not in loads(api)
    assert api._station_recovery is None and api._station_recovery_task is None


@pytest.mark.parametrize("category", ["idle", "stall"])
@pytest.mark.parametrize("late_failure", [False, True])
async def test_source_progress_during_refresh_defers_load_without_spending_attempt(api, category, late_failure):
    await select(api)
    recovery = api._station_recovery
    recovery.attempts = 2  # Last allowed attempt must not become terminal failure.
    entered, release = asyncio.Event(), asyncio.Event()
    async def resolve(station_id):
        entered.set()
        await release.wait()
        if late_failure:
            raise RadioResolutionError("Synthetic late failure")
        return RadioResolution(FRESH, station_id)
    api._resolve_station_id.side_effect = resolve
    now = [0.0]
    api._station_stall_clock = lambda: now[0]
    if category == "stall":
        api.mpv.buffering = True
        api.mpv.position = 9.5
        task = await expire(api, now)
    else:
        task = await eof(api)
    await entered.wait()
    api.mpv.idle = False
    api.mpv.buffering = False
    api.mpv.position += .5
    release.set()
    await finish(task)
    assert loads(api) == [FIRST] and recovery.attempts == 2
    assert api.state.phase == PlaybackPhase.PLAYING and not api.state.last_error
    assert api.state.resolved_url == api.state.queue.current.resolved_url == FIRST
    assert recovery.backend_url == FIRST


async def test_progress_during_backend_lock_wait_retains_original_source(api):
    await select(api)
    original = api._resolve_station_id
    entered, release = asyncio.Event(), asyncio.Event()
    async def resolve(station_id):
        entered.set()
        await release.wait()
        return RadioResolution(FRESH, station_id)
    api._resolve_station_id = resolve
    task = await eof(api)
    await entered.wait()
    await api._backend_lock.acquire()
    release.set()
    await asyncio.sleep(0)
    api.mpv.idle = False
    api.mpv.position += .5
    api._backend_lock.release()
    await finish(task)
    assert loads(api) == [FIRST] and api._station_recovery.attempts == 0
    api._resolve_station_id = original
    await finish(await eof(api))
    assert loads(api) == [FIRST, FRESH] and api._station_recovery.attempts == 1


@pytest.mark.parametrize("mode", ["provider_labels", "roundtrip", "changed_url"])
async def test_only_local_matching_resolver_provenance_can_refresh(api, mode):
    if mode == "provider_labels":
        queue = PlaybackQueue(name="Untrusted metadata", source="newTuneIn", stream_id="s12345",
                              search_url=REFERENCE, items=[QueueItem(FIRST, source="newTuneIn", id="s12345",
                              stream_type="station", metadata="_radio_station=s12345")], index=1)
        wire = serialize_queue_context(queue).replace("</Track1>", "<_radio_station>s12345</_radio_station></Track1>")
        queue = parse_queue_context(wire)
        api._resolve_station_id = AsyncMock(side_effect=AssertionError("No trusted resolver provenance"))
    else:
        queue = await select(api)
        if mode == "roundtrip":
            queue = parse_queue_context(serialize_queue_context(queue))
            assert queue.current._radio_station is None
        else:
            queue.current.url = FIRST + "?replacement=1"
            queue.current.resolved_url = ""
        api._resolve_station_id.reset_mock()
        api._resolve_station_id.side_effect = AssertionError("Unproven source must not refresh")
    await api._replace_queue(queue)
    await api._play_queue_index(1, queue.name)
    await finish(await eof(api))
    api._resolve_station_id.assert_not_awaited()
    assert api.state.phase == PlaybackPhase.PLAYING


@pytest.mark.parametrize("stream_type,duration", [("track", 0), ("station", 30), ("", 30)])
async def test_explicit_finite_or_nonstation_reference_never_arms_or_refreshes(api, stream_type, duration):
    api._resolve_station_id = AsyncMock(return_value=RadioResolution(FIRST, "s12345"))
    queue = PlaybackQueue(items=[QueueItem(REFERENCE, stream_type=stream_type, duration=duration)], index=1)
    await api._replace_queue(queue)
    await api._play_queue_index(1, queue.name)
    assert api._station_recovery is None
    assert await eof(api) is None
    await api._play_queue_index(1, queue.name)
    api._resolve_station_id.assert_awaited_once_with("s12345")
    assert loads(api) == [FIRST, FIRST]


async def test_explicit_play_after_stop_refreshes_known_station_without_resetting_metadata(api):
    queue = await select(api)
    queue.current.metadata = "Opaque station metadata"
    api._activate_queue_item()
    selected, before = queue.current, asdict(queue.current)
    await api.execute(ParsedCommand("stop"), "192.0.2.1")
    await api.execute(ParsedCommand("resume"), "192.0.2.1")
    assert loads(api) == [FIRST, FRESH]
    assert queue.current is selected and api.state.track_metadata == "Opaque station metadata"
    before["resolved_url"] = FRESH
    assert asdict(selected) == before
    assert api._station_recovery.attempts == 0
