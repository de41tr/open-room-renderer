"""UNIT/MOCK repeat controls, EOF ownership and native-staging fences."""
import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from xml.etree import ElementTree as ET

import pytest

from fake_linkplay.http_api import parse_command
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.queue import PlaybackQueue, QueueItem, parse_queue_context
from fake_linkplay.soap import SoapFault
from fake_linkplay.state import PlaybackPhase
from fake_linkplay.upnp_description import avtransport_response_fields
from test_queue_playback import api, create, play, FIRST, SECOND
from test_native_queue_staging import make_api as make_native_api, start as start_native
from test_soap_faults import request, fault_code


def loads(api):
    return [url for name, url in api.mpv.calls if name == "load"]


def properties(api, service):
    return {node.tag: node.text for prop in ET.fromstring(api._event_body(service)) for node in prop}


async def mode(api, value):
    return await api.execute(parse_command(f"setPlayerCmd:loopmode:{value}"), "192.0.2.1")


async def end(api):
    api.mpv.idle = True
    await api.sync_mpv_state()
    task = api._queue_advance_task
    if task is not None:
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("value,index,expected", [(4, 1, 2), (4, 2, 2), (1, 1, 1), (1, 2, 2), (0, 1, 2), (0, 2, 1)])
async def test_only_natural_end_applies_repeat_policy(api, value, index, expected):
    await create(api)
    await mode(api, value)
    await play(api, index)
    await end(api)
    assert api.state.queue.index == expected
    stopped = value == 4 and index == 2
    assert len(loads(api)) == (1 if stopped else 2)
    assert api.state.phase == (PlaybackPhase.STOPPED if stopped else PlaybackPhase.PLAYING)
    assert api.state.current_url == (FIRST if expected == 1 else SECOND)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 1, 4])
async def test_empty_and_single_item_repeat(api, value):
    await mode(api, value)
    api._apply_backend_snapshot({"idle-active": True})
    assert api._queue_advance_task is None
    api.state.queue = PlaybackQueue(items=[QueueItem(FIRST, duration=30)], index=1, loop=value)
    api._activate_queue_item()
    await api._play_queue_index(1)
    await end(api)
    assert loads(api) == [FIRST] * (1 if value == 4 else 2)
    assert api.state.queue.index == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("value,name", [(4, "NORMAL"), (1, "REPEAT_ONE"), (0, "REPEAT_ALL")])
async def test_http_soap_status_queue_and_events_agree(api, value, name):
    await create(api)
    assert await mode(api, value) == "OK"
    assert api.state.as_player_status()["loop"] == api.state.as_status_ex("192.0.2.1")["loop"] == str(value)
    assert api._playqueue_response_fields("GetQueueLoopMode") == f"<LoopMode>{value}</LoopMode>"
    assert f"<LoopMode>{value}</LoopMode>" in avtransport_response_fields("GetPositionInfo", api.state)
    response = await api.soap(request("GetTransportSettings", "<InstanceID>0</InstanceID>"))
    assert response.status == 200
    root = ET.fromstring(response.text)
    assert root.findtext(".//PlayMode") == name
    assert root.findtext(".//RecQualityMode") == "NOT_IMPLEMENTED"
    avt, queue = properties(api, "AVTransport"), properties(api, "PlayQueue")
    assert f'<CurrentPlayMode val="{name}"/>' in avt["LastChange"]
    assert queue["LoopMode"] == str(value)
    assert f'<LoopMode val="{value}"/>' in queue["LastChange"]
    assert parse_queue_context(queue["QueueContext"]).loop == value
    for action, args, service in [
        ("SetPlayMode", f"<NewPlayMode>{name}</NewPlayMode>", "AVTransport"),
        ("SetQueueLoopMode", f"<LoopMode>{value}</LoopMode>", "PlayQueue"),
    ]:
        assert (await api.soap(request(action, args, service))).status == 200
        assert api.state.loop == str(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["", "5", "-1", "01", "4.0", "NORMAL", "SHUFFLE", "4:0", " 4", "4 "])
async def test_invalid_http_modes_fail_without_state_or_backend_change(api, raw):
    await create(api)
    before, calls = asdict(api.state), list(api.mpv.calls)
    api._schedule_event_notify = Mock()
    req = SimpleNamespace(query={"command": f"setPlayerCmd:loopmode:{raw}"}, remote="192.0.2.1",
                          host="renderer.example.test", path_qs="/httpapi.asp", headers={})
    assert (await api.handle_httpapi(req)).status == 400
    assert asdict(api.state) == before and api.mpv.calls == calls
    api._schedule_event_notify.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("action,args,service", [
    ("SetPlayMode", "<NewPlayMode>4</NewPlayMode>", "AVTransport"),
    ("SetPlayMode", "<NewPlayMode>REPEAT_TRACK</NewPlayMode>", "AVTransport"),
    ("SetPlayMode", "<PlayMode>REPEAT_ONE</PlayMode>", "AVTransport"),
    ("SetPlayMode", "", "AVTransport"),
    ("SetQueueLoopMode", "<LoopMode>5</LoopMode>", "PlayQueue"),
    ("SetQueueLoopMode", "<QueueLoopMode>1</QueueLoopMode>", "PlayQueue"),
    ("SetQueueLoopMode", "", "PlayQueue"),
])
async def test_strict_soap_modes_fault_without_mutation(api, action, args, service):
    before = asdict(api.state)
    assert fault_code(await api.soap(request(action, args, service))) == "712"
    assert asdict(api.state) == before and not api.mpv.calls


@pytest.mark.asyncio
async def test_optional_queue_name_validates_before_mutation_even_for_noop(api):
    await create(api)
    for action in ("SetQueueLoopMode", "GetQueueLoopMode"):
        before = asdict(api.state)
        assert fault_code(await api.soap(request(action, "<QueueName>Wrong</QueueName><LoopMode>4</LoopMode>", "PlayQueue"))) == "402"
        assert asdict(api.state) == before
        assert (await api.soap(request(action, "<LoopMode>4</LoopMode>", "PlayQueue"))).status == 200


@pytest.mark.asyncio
async def test_setter_changes_only_policy_and_notifies_once(api):
    await create(api)
    await play(api, 1)
    api._pending_seek_target = 17
    api._pending_seek_generation = 9
    api.state.last_error = "Existing status detail"
    recovery, pending = object(), object()
    api._station_recovery = recovery
    api._queue_advance_task = pending
    api._schedule_event_notify = Mock()
    before, calls = asdict(api.state), list(api.mpv.calls)
    await mode(api, 1)
    before["queue"]["loop"] = 1
    assert asdict(api.state) == before and api.mpv.calls == calls
    assert api._station_recovery is recovery and api._queue_advance_task is pending
    assert (api._pending_seek_target, api._pending_seek_generation) == (17, 9)
    api._schedule_event_notify.assert_called_once_with(("AVTransport", "PlayQueue"))
    await mode(api, 1)
    api._schedule_event_notify.assert_called_once()
    api._station_recovery = api._queue_advance_task = None


@pytest.mark.asyncio
@pytest.mark.parametrize("initial,replacement,index,expected", [(4, 1, 1, 1), (1, 4, 1, 2), (0, 4, 2, None), (0, 1, 2, 2)])
async def test_eof_rechecks_mode_after_awaited_ownership_preflight(api, initial, replacement, index, expected):
    await create(api)
    await mode(api, initial)
    await play(api, index)
    api.mpv.idle = True
    original = api.mpv.snapshot
    api._apply_backend_snapshot(await original())
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked_snapshot():
        entered.set()
        await release.wait()
        return await original()
    api.mpv.snapshot = blocked_snapshot
    task = api._queue_advance_task
    await asyncio.wait_for(entered.wait(), 1)
    generation = api.state.generation
    await mode(api, replacement)
    assert api.state.generation == generation and api._queue_advance_task is task and not task.cancelled()
    release.set()
    await asyncio.wait_for(task, 1)
    assert api.state.queue.index == (index if expected is None else expected)
    assert len(loads(api)) == (1 if expected is None else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 1])
@pytest.mark.parametrize("newer", ["stop", "pause", "error", "foreign"])
async def test_repeat_does_not_override_owner_or_failed_source(api, value, newer):
    await create(api)
    await mode(api, value)
    await play(api, 2)
    api.mpv.idle = True
    await api.sync_mpv_state()
    task = api._queue_advance_task
    if newer in {"stop", "pause"}:
        await api._transport_command(newer)
    elif newer == "error":
        api.state.mark_error("Synthetic failure")
    else:
        api.mpv.url, api.mpv.idle = "https://media.example.test/foreign.wav", False
        original = api.mpv.snapshot
        async def foreign_snapshot():
            return {**await original(), "playlist": [{"id": 999, "filename": api.mpv.url,
                    "current": True, "playing": True}]}
        api.mpv.snapshot = foreign_snapshot
    await asyncio.gather(task, return_exceptions=True)
    assert loads(api) == [SECOND]
    assert api.state.queue.index == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 1])
async def test_manual_navigation_stays_numeric_nonwrapping_and_stopped(api, value):
    await create(api)
    await mode(api, value)
    for action in ("Previous", "Next", "Next"):
        if action == "Previous" or api.state.queue.index == 2:
            before = asdict(api.state)
            with pytest.raises(SoapFault):
                await api._execute_soap_side_effect(action, "<Action/>")
            assert asdict(api.state) == before
        else:
            await api._execute_soap_side_effect(action, "<Action/>")
            assert api.state.queue.index == 2
    assert not loads(api) and api.state.phase == PlaybackPhase.STOPPED
    await play(api, 1)
    await api._transport_command("pause")
    before = asdict(api.state)
    with pytest.raises(SoapFault) as exc:
        await api._execute_soap_side_effect("Next", "<Action/>")
    assert exc.value.code == 701 and asdict(api.state) == before
    await api._transport_command("resume")
    await api._execute_soap_side_effect("Next", "<Action/>")
    assert api.state.queue.index == 2  # Repeat-one affects EOF only.
    await api._execute_soap_side_effect("Previous", "<Action/>")
    assert api.state.queue.index == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("_native_stage", object()), ("_native_current_id", 7),
    ("_native_owned_entries", ((7, FIRST),)), ("_native_cleanup_tasks", {object()}),
    ("_native_output_uncertain", True)])
async def test_native_state_blocks_mode_change_but_allows_same_mode(api, field, value):
    setattr(api, field, value)
    api._schedule_event_notify = Mock()
    before = asdict(api.state)
    with pytest.raises(SoapFault) as exc:
        await mode(api, 1)
    assert exc.value.code == 701
    assert asdict(api.state) == before and getattr(api, field) is value and not api.mpv.calls
    assert await mode(api, 4) == "OK"
    assert asdict(api.state) == before
    api._schedule_event_notify.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 1, 2, 3])
async def test_native_repeat_requires_confirmed_stop_and_never_stages(tmp_path, value):
    api = make_native_api(tmp_path)
    await start_native(api)
    assert api._native_stage is not None
    before, calls, stage = asdict(api.state), list(api.mpv.calls), api._native_stage
    with pytest.raises(SoapFault):
        await mode(api, value)
    assert asdict(api.state) == before and api.mpv.calls == calls and api._native_stage is stage
    await api._transport_command("stop")
    await mode(api, value)
    before_appends = [entry for entry in api.mpv.calls if entry[0] == "append"]
    await start_native(api)
    assert [entry for entry in api.mpv.calls if entry[0] == "append"] == before_appends
    assert api._native_stage is None
    api.mpv.advance()
    await api.sync_mpv_state()
    await api._queue_advance_task
    assert api.state.queue.index == (1 if value == 1 else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["resolve", "final_snapshot"])
async def test_mode_changed_while_staging_preflight_awaits_prevents_append(tmp_path, when):
    api = make_native_api(tmp_path)
    original = api._resolve_playback_url
    if when == "resolve":
        async def resolve(url):
            if url == SECOND:
                await mode(api, 1)
            return await original(url)
        api._resolve_playback_url = resolve
    else:
        original_snapshot = api.mpv.snapshot
        resolved_successor = False
        async def snapshot():
            nonlocal resolved_successor
            result = await original_snapshot()
            if resolved_successor:
                resolved_successor = False
                await mode(api, 1)
            return result
        async def resolve(url):
            nonlocal resolved_successor
            result = await original(url)
            if url == SECOND:
                resolved_successor = True
            return result
        api.mpv.snapshot = snapshot
        api._resolve_playback_url = resolve
    await start_native(api)
    assert api.state.loop == "1"
    assert not [entry for entry in api.mpv.calls if entry[0] == "append"]
    assert api._native_stage is None


@pytest.mark.asyncio
async def test_failed_native_stop_keeps_policy_fenced(tmp_path):
    api = make_native_api(tmp_path)
    await start_native(api)
    api.mpv.stop = AsyncMock(side_effect=BackendError("Synthetic rejected stop"))
    with pytest.raises(BackendError):
        await api._transport_command("stop")
    assert api._native_output_uncertain
    before = asdict(api.state)
    with pytest.raises(SoapFault):
        await mode(api, 0)
    assert asdict(api.state) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [PlaybackPhase.STOPPED, PlaybackPhase.PAUSED, PlaybackPhase.ERROR])
async def test_policy_update_preserves_inactive_transport_and_error(api, phase):
    await create(api)
    api.state.phase = phase
    api.state.intent = "pause" if phase == PlaybackPhase.PAUSED else "stop"
    api.state.last_error = "Synthetic retained error" if phase == PlaybackPhase.ERROR else ""
    before = asdict(api.state)
    await mode(api, 1)
    before["queue"]["loop"] = 1
    assert asdict(api.state) == before and not api.mpv.calls


@pytest.mark.asyncio
async def test_empty_queue_browse_and_event_context_follow_mode(api):
    await create(api, '<PlayList><QueueName>Empty</QueueName><Count>0</Count><Index>0</Index><Tracks/></PlayList>')
    for value in (1, 0, 4):
        await mode(api, value)
        response = ET.fromstring("<Root>" + api._playqueue_response_fields("BrowseQueue") + "</Root>")
        contexts = [response.findtext("QueueContext"), properties(api, "PlayQueue")["QueueContext"]]
        for context in contexts:
            queue = parse_queue_context(context)
            assert queue.loop == value and queue.count == queue.index == 0
    assert not api.mpv.calls


@pytest.mark.asyncio
async def test_mode_change_while_load_pending_does_not_cancel_load(api):
    await create(api)
    api.mpv.release_load = asyncio.Event()
    pending = asyncio.create_task(play(api, 1))
    await asyncio.wait_for(api.mpv.load_started.wait(), 1)
    generation = api.state.generation
    await mode(api, 1)
    assert api.state.generation == generation and not pending.done()
    api.mpv.release_load.set()
    await pending
    assert api.state.phase == PlaybackPhase.PLAYING and loads(api) == [FIRST]
    await end(api)
    assert loads(api) == [FIRST, FIRST]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 1])
async def test_failed_repeat_load_is_terminal(api, value):
    await create(api)
    await mode(api, value)
    await play(api, 2)
    api.mpv.loadfile = AsyncMock(side_effect=BackendError("Synthetic repeat load failure"))
    await end(api)
    assert api.state.phase == PlaybackPhase.ERROR
    api.mpv.loadfile.assert_awaited_once()
    for _ in range(3):
        await api.sync_mpv_state()
    api.mpv.loadfile.assert_awaited_once()


@pytest.mark.asyncio
async def test_native_guard_maps_to_http_failure_without_changes(tmp_path):
    api = make_native_api(tmp_path)
    await start_native(api)
    req = SimpleNamespace(query={"command": "setPlayerCmd:loopmode:1"}, remote="192.0.2.1",
                          host="renderer.example.test", path_qs="/httpapi.asp", headers={})
    before, calls = asdict(api.state), list(api.mpv.calls)
    assert (await api.handle_httpapi(req)).status == 400
    assert asdict(api.state) == before and api.mpv.calls == calls
