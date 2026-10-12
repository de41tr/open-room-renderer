"""UNIT/MOCK: shuffle policy admission, wire surfaces and controller ownership."""
import asyncio
from dataclasses import asdict
from unittest.mock import AsyncMock, Mock
from xml.etree import ElementTree as ET

import pytest

from fake_linkplay.http_api import HttpApi, parse_command
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.queue import PlaybackQueue, QueueItem, parse_queue_context, serialize_queue_context
from fake_linkplay.soap import SoapFault
from fake_linkplay.state import PlaybackPhase, RendererState
from fake_linkplay.upnp_description import avtransport_response_fields
from test_queue_playback import api
from test_repeat_queue_playback import end, loads, mode, properties
from test_shuffle_policy import ReverseRandom
from test_soap_faults import request


URLS = tuple(f"https://media.example.test/{index}.wav" for index in range(1, 5))


async def setup(api, value=3, index=2, *, playing=True, duplicate=False):
    api._shuffle_rng = ReverseRandom()
    queue = PlaybackQueue(name="Shuffle", loop=value, index=index,
        items=[QueueItem(URLS[0] if duplicate else url, title=f"Track {position}", duration=30)
               for position, url in enumerate(URLS, 1)])
    await api._replace_queue(queue)
    if playing:
        await api._execute_soap_side_effect("Play", "")
    return queue


async def navigate(api, direction="next", surface="http"):
    if surface == "http":
        return await api.execute(parse_command(f"setPlayerCmd:{direction}"), "192.0.2.1")
    return await api._execute_soap_side_effect("Next" if direction == "next" else "Previous", "")


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [2, 3])
async def test_shuffle_wire_status_xml_soap_and_lastchange_agree(api, value):
    queue = await setup(api, value, playing=False)
    assert api.state.as_player_status()["loop"] == api.state.as_status_ex("192.0.2.1")["loop"] == str(value)
    assert api._playqueue_response_fields("GetQueueLoopMode") == f"<LoopMode>{value}</LoopMode>"
    assert f"<LoopMode>{value}</LoopMode>" in avtransport_response_fields("GetPositionInfo", api.state)
    response = await api.soap(request("GetTransportSettings", "<InstanceID>0</InstanceID>"))
    assert response.status == 200 and ET.fromstring(response.text).findtext(".//PlayMode") == "SHUFFLE"
    avt, event_queue = properties(api, "AVTransport"), properties(api, "PlayQueue")
    assert '<CurrentPlayMode val="SHUFFLE"/>' in avt["LastChange"]
    assert event_queue["LoopMode"] == str(value)
    assert f'<LoopMode val="{value}"/>' in event_queue["LastChange"]
    parsed = parse_queue_context(event_queue["QueueContext"])
    assert parsed.loop == value and parsed.index == 2 and parsed._shuffle is None
    assert [item.url for item in parsed.items] == list(URLS)
    traversal = queue._shuffle
    await api._execute_soap_side_effect("SetPlayMode", "<NewPlayMode>SHUFFLE</NewPlayMode>")
    assert queue.loop == 3 and queue._shuffle is traversal
    for candidate in (2, 3):
        await api._execute_soap_side_effect("SetQueueLoopMode", f"<LoopMode>{candidate}</LoopMode>")
        assert queue.loop == candidate and queue._shuffle is traversal


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate", [False, True])
async def test_http_history_and_soap_numeric_selection_are_distinct(api, duplicate):
    queue = await setup(api, duplicate=duplicate)
    assert queue._shuffle.path == (2, 4, 3, 1)
    canonical = list(queue.items)
    await navigate(api)
    assert queue.index == 4
    traversal, draws = queue._shuffle, api._shuffle_rng.calls
    await navigate(api, "prev")
    assert queue.index == 2
    await navigate(api)
    assert queue.index == 4 and queue._shuffle == traversal and api._shuffle_rng.calls == draws
    await navigate(api, "prev", "soap")
    assert queue.index == 3 and queue._shuffle.path == (3, 4, 2, 1) and queue._shuffle.cursor == 0
    assert api._shuffle_rng.calls == draws + 1
    assert queue.items == canonical and all(a is b for a, b in zip(queue.items, canonical))
    assert api.state.title == "Track 3"
    assert parse_queue_context(serialize_queue_context(queue)).index == 3
    await api._play_queue_index(1)
    assert queue.index == 1 and queue._shuffle.path[0] == 1 and queue._shuffle.cursor == 0


@pytest.mark.asyncio
async def test_stop_resume_preserves_remainder_but_natural_exhaustion_starts_new_cycle(api):
    queue = await setup(api)
    await navigate(api)
    before, draws = queue._shuffle, api._shuffle_rng.calls
    await api._transport_command("stop")
    await api.execute(parse_command("setPlayerCmd:resume"), "192.0.2.1")
    assert queue._shuffle is before and api._shuffle_rng.calls == draws
    assert loads(api)[-2:] == [URLS[3], URLS[3]]
    await end(api)
    assert queue.index == 3
    await end(api)
    assert queue.index == 1 and not queue._shuffle.exhausted
    await end(api)
    assert queue._shuffle.exhausted and api.state.phase == PlaybackPhase.STOPPED
    before = queue._shuffle
    await api._execute_soap_side_effect("Play", "")
    assert queue.index == 1 and queue._shuffle.path == (1, 4, 3, 2)
    assert queue._shuffle is not before and not queue._shuffle.exhausted
    assert api._shuffle_rng.calls == draws + 1


@pytest.mark.asyncio
async def test_pause_resume_seek_and_metadata_refresh_preserve_history(api):
    queue = await setup(api)
    await navigate(api)
    before, draws = queue._shuffle, api._shuffle_rng.calls
    await api._transport_command("pause")
    await api._execute_soap_side_effect("Play", "")
    # Seeking updates controller observations but has no traversal responsibility.
    await api._seek_command(3)
    queue.current.title = "Refreshed metadata"
    queue.current.resolved_url = "https://media.example.test/refreshed.wav"
    api._activate_queue_item()
    assert queue._shuffle is before and api._shuffle_rng.calls == draws


@pytest.mark.asyncio
async def test_noop_rejected_and_repeat_flag_modes_do_not_redraw(api):
    queue = await setup(api)
    await navigate(api)
    before, draws = queue._shuffle, api._shuffle_rng.calls
    api._schedule_event_notify = Mock()
    await mode(api, 3)
    api._schedule_event_notify.assert_not_called()
    for value in (2, 3, 2):
        await mode(api, value)
        assert queue._shuffle is before and api._shuffle_rng.calls == draws
    for value in ("RANDOM", "5", "03", " 3"):
        with pytest.raises(ValueError):
            await mode(api, value)
    with pytest.raises(SoapFault):
        await api._execute_soap_side_effect("SetPlayMode", "<NewPlayMode>RANDOM</NewPlayMode>")
    with pytest.raises(SoapFault):
        api._set_queue_loop_mode(3, "Wrong queue")
    assert queue._shuffle is before and api._shuffle_rng.calls == draws
    await mode(api, 4)
    assert queue._shuffle is None
    await mode(api, 3)
    assert queue._shuffle.path[0] == queue.index and queue._shuffle.cursor == 0
    assert api._shuffle_rng.calls == draws + 1


@pytest.mark.asyncio
async def test_replacement_same_name_starts_fresh_traversal(api):
    queue = await setup(api)
    await navigate(api)
    replacement = parse_queue_context(serialize_queue_context(queue))
    await api._replace_queue(replacement)
    assert api.state.queue is replacement and replacement.name == queue.name
    assert replacement._shuffle.path[0] == replacement.index == 4
    assert replacement._shuffle.cursor == 0 and replacement._shuffle is not queue._shuffle


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [2, 3])
async def test_stopped_navigation_selects_without_play_and_paused_navigation_rejects(api, value):
    queue = await setup(api, value, playing=False)
    await navigate(api)
    assert queue.index == 4 and api.state.phase == PlaybackPhase.STOPPED and not loads(api)
    await api._execute_soap_side_effect("Play", "")
    await api._transport_command("pause")
    before, calls, draws = asdict(api.state), list(api.mpv.calls), api._shuffle_rng.calls
    for direction in ("next", "prev"):
        with pytest.raises(SoapFault) as error:
            await navigate(api, direction)
        assert error.value.code == 701
    assert asdict(api.state) == before and api.mpv.calls == calls and api._shuffle_rng.calls == draws


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [2, 3])
async def test_repeat_boundary_previous_and_next_replay_without_redraw(api, value):
    queue = await setup(api, value)
    for _ in range(3):
        await navigate(api)
    assert queue.index == 1
    draws = api._shuffle_rng.calls
    if value == 3:
        before, calls = asdict(api.state), list(api.mpv.calls)
        with pytest.raises(SoapFault) as error:
            await navigate(api)
        assert error.value.code == 711
        assert asdict(api.state) == before and api.mpv.calls == calls and api._shuffle_rng.calls == draws
    else:
        await navigate(api)
        assert queue.index == 4 and api._shuffle_rng.calls == draws + 1
        before = queue._shuffle
        await navigate(api, "prev")
        assert queue.index == 1
        await navigate(api)
        assert queue._shuffle == before and api._shuffle_rng.calls == draws + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("initial,replacement,expected", [(3, 4, 3), (4, 3, 4), (2, 1, 3), (3, 2, 4)])
async def test_http_navigation_rereads_mode_after_required_stop(api, initial, replacement, expected):
    queue = await setup(api, initial)
    original_stop = api.mpv.stop
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked_stop():
        entered.set()
        await release.wait()
        await original_stop()
    api.mpv.stop = blocked_stop
    pending = asyncio.create_task(navigate(api))
    await asyncio.wait_for(entered.wait(), 1)
    await mode(api, replacement)
    traversal, draws = queue._shuffle, api._shuffle_rng.calls
    release.set()
    await pending
    assert queue.index == expected and api._shuffle_rng.calls == draws
    if replacement in (2, 3):
        assert queue._shuffle.path == traversal.path and queue._shuffle.cursor == 1
    else:
        assert queue._shuffle is None


@pytest.mark.asyncio
@pytest.mark.parametrize("initial,replacement,index,expected", [(3, 1, 2, 2), (1, 3, 2, 4), (2, 3, 1, None), (2, 2, 1, 4)])
async def test_eof_preflight_is_nonconsuming_and_rereads_policy(api, initial, replacement, index, expected):
    queue = await setup(api, initial)
    if index == 1:
        for _ in range(3):
            await navigate(api)
    original_snapshot = api.mpv.snapshot
    api.mpv.idle = True
    api._apply_backend_snapshot(await original_snapshot())
    pending = api._queue_advance_task
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked_snapshot():
        entered.set()
        await release.wait()
        return await original_snapshot()
    api.mpv.snapshot = blocked_snapshot
    draws = api._shuffle_rng.calls
    await asyncio.wait_for(entered.wait(), 1)
    assert api._shuffle_rng.calls == draws
    await mode(api, replacement)
    draws, count = api._shuffle_rng.calls, len(loads(api))
    release.set()
    await pending
    assert queue.index == (index if expected is None else expected)
    assert len(loads(api)) == count + (expected is not None)
    assert api._shuffle_rng.calls == draws + (index == 1 and replacement == 2)
    if expected is None:
        assert queue._shuffle.exhausted


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["rejected", "lost_ack", "cancelled"])
async def test_failed_stop_does_not_consume_cycle_boundary(api, failure):
    queue = await setup(api, 2)
    for _ in range(3):
        await navigate(api)
    traversal, draws = queue._shuffle, api._shuffle_rng.calls
    original_stop = api.mpv.stop
    entered = asyncio.Event()
    async def failing_stop():
        entered.set()
        if failure == "lost_ack":
            await original_stop()
        if failure == "cancelled":
            await asyncio.Future()
        raise BackendError("Synthetic stop failed")
    api.mpv.stop = failing_stop
    pending = asyncio.create_task(navigate(api))
    await asyncio.wait_for(entered.wait(), 1)
    if failure == "cancelled":
        pending.cancel()
    with pytest.raises(asyncio.CancelledError if failure == "cancelled" else BackendError):
        await pending
    assert queue.index == 1 and queue._shuffle is traversal and api._shuffle_rng.calls == draws
    assert api.state.phase == PlaybackPhase.ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize("newer", ["stop", "pause", "queue"])
async def test_superseded_stop_keeps_history_and_new_owner(api, newer):
    queue = await setup(api, 2)
    for _ in range(3):
        await navigate(api)
    traversal, draws = queue._shuffle, api._shuffle_rng.calls
    original_stop = api.mpv.stop
    entered, release = asyncio.Event(), asyncio.Event()
    first = True
    async def blocked_stop():
        nonlocal first
        if first:
            first = False
            entered.set()
            await release.wait()
        await original_stop()
    api.mpv.stop = blocked_stop
    pending = asyncio.create_task(navigate(api))
    await asyncio.wait_for(entered.wait(), 1)
    replacement = PlaybackQueue(name=queue.name, items=[QueueItem(URLS[0])], index=1)
    owner = asyncio.create_task(api._replace_queue(replacement) if newer == "queue" else api._transport_command(newer))
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(BackendError):
        await pending
    await asyncio.gather(owner, return_exceptions=True)
    assert queue._shuffle is traversal and api._shuffle_rng.calls == draws
    assert api.state.queue is (replacement if newer == "queue" else queue)
    assert api.state.intent == ("pause" if newer == "pause" else "stop")


@pytest.mark.asyncio
async def test_failed_load_retains_selected_cursor_and_explicit_play_retries(api):
    queue = await setup(api)
    original_load = api.mpv.loadfile
    api.mpv.loadfile = AsyncMock(side_effect=BackendError("Synthetic failed load"))
    with pytest.raises(BackendError):
        await navigate(api)
    assert queue.index == 4 and queue._shuffle.cursor == 1 and api.state.phase == PlaybackPhase.ERROR
    traversal, draws = queue._shuffle, api._shuffle_rng.calls
    await api.sync_mpv_state()
    api.mpv.loadfile.assert_awaited_once()
    api.mpv.loadfile = original_load
    await api._execute_soap_side_effect("Play", "")
    assert queue.index == 4 and queue._shuffle is traversal and api._shuffle_rng.calls == draws
    assert api.state.phase == PlaybackPhase.PLAYING and loads(api)[-1] == URLS[3]


@pytest.mark.asyncio
async def test_mode_changed_after_admitted_selection_does_not_cancel_load(api):
    queue = await setup(api)
    api.mpv.release_load = asyncio.Event()
    api.mpv.load_started.clear()
    pending = asyncio.create_task(navigate(api))
    await asyncio.wait_for(api.mpv.load_started.wait(), 1)
    assert queue.index == 4
    generation, traversal, draws = api.state.generation, queue._shuffle, api._shuffle_rng.calls
    await mode(api, 2)
    assert api.state.generation == generation and queue._shuffle is traversal and api._shuffle_rng.calls == draws
    api.mpv.release_load.set()
    await pending
    assert api.state.phase == PlaybackPhase.PLAYING and queue.index == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("newer", ["stop", "pause", "same_name_queue", "same_url_foreign"])
async def test_eof_owner_fences_do_not_consume_shuffle(api, newer):
    queue = await setup(api, 2)
    for _ in range(3):
        await navigate(api)
    before, draws, count = queue._shuffle, api._shuffle_rng.calls, len(loads(api))
    api.mpv.idle = True
    await api.sync_mpv_state()
    pending = api._queue_advance_task
    if newer in {"stop", "pause"}:
        await api._transport_command(newer)
    elif newer == "same_name_queue":
        await api._replace_queue(PlaybackQueue(name=queue.name, items=[QueueItem(URLS[0])], index=1))
    else:
        api.mpv.url, api.mpv.idle = queue.current.url, False
        original = api.mpv.snapshot
        async def foreign_snapshot():
            return {**await original(), "playlist": [{"id": 999, "filename": api.mpv.url,
                "current": True, "playing": True}]}
        api.mpv.snapshot = foreign_snapshot
    await asyncio.gather(pending, return_exceptions=True)
    assert queue._shuffle is before and api._shuffle_rng.calls == draws and len(loads(api)) == count


def test_constructor_accepts_injected_rng(api):
    queue = PlaybackQueue(items=[QueueItem(url) for url in URLS], index=2, loop=3)
    state = RendererState.from_config(api.cfg)
    state.queue = queue
    rng = ReverseRandom()
    HttpApi(api.cfg, state, api.mpv, shuffle_rng=rng)
    assert queue._shuffle.path == (2, 4, 3, 1) and rng.calls == 1
