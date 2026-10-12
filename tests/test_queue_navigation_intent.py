"""UNIT/MOCK: queue navigation cannot start a stopped or paused owner's audio."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

from fake_linkplay.http_api import parse_command
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.queue import PlaybackQueue, QueueItem
from fake_linkplay.soap import SoapFault
from fake_linkplay.state import PlaybackPhase
from test_native_queue_staging import A, B, C, make_api, mutations, start


async def navigate(api, action, surface="soap"):
    if surface == "soap":
        return await api._execute_soap_side_effect(action, f"<{action}/>")
    command = "setPlayerCmd:next" if action == "Next" else "setPlayerCmd:prev"
    return await api.execute(parse_command(command), "192.0.2.1")


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("surface", ["soap", "http"])
@pytest.mark.parametrize("action,index,target", [("Next", 1, 2), ("Previous", 2, 1)])
@pytest.mark.parametrize("previously_played", [False, True])
async def test_stopped_navigation_selects_without_loading(tmp_path, enabled, surface, action, index, target, previously_played):
    api = make_api(tmp_path, enabled=enabled)
    api.state.queue.index = index
    api._activate_queue_item()
    if previously_played:
        await start(api)
        await api._transport_command("stop")
    before_calls = list(api.mpv.calls)
    generation = api.state.generation
    api._resolve_playback_url = AsyncMock(side_effect=AssertionError("Stopped selection must not resolve"))
    api._playback_offset_sec = 42
    await navigate(api, action, surface)
    for _ in range(3):
        await api.sync_mpv_state()
    assert api.state.queue.index == api.state.plicurr == target
    assert api.state.current_url == (A, B)[target - 1]
    assert api.state.title == f"Track {target}" and api.state.artist == f"Artist {target}"
    assert api.state.curpos == api._playback_offset_sec == 0
    assert api.state.totlen == 30
    assert api.state.phase == PlaybackPhase.STOPPED and api.state.intent == "stop"
    assert api.state.generation > generation
    assert api.state.backend_url == api.state.backend_original_url == ""
    assert api.mpv.idle and api.mpv.calls == before_calls
    api._resolve_playback_url.assert_not_awaited()
    api._resolve_playback_url = AsyncMock(side_effect=lambda url: url)
    await api._execute_soap_side_effect("Play", "<Play/>")
    assert api.state.phase == PlaybackPhase.PLAYING and api.mpv.url == (A, B)[target - 1]
    assert mutations(api, "load")[-1] == (A, B)[target - 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("surface", ["soap", "http"])
@pytest.mark.parametrize("action", ["Next", "Previous"])
@pytest.mark.parametrize("index", [1, 2, 3])
async def test_paused_multi_item_navigation_is_rejected_without_mutation(tmp_path, enabled, surface, action, index):
    api = make_api(tmp_path, enabled=enabled, urls=(A, B, C))
    api.state.queue.index = index
    api._activate_queue_item()
    await start(api)
    await api._transport_command("pause")
    before, calls, stage = asdict(api.state), list(api.mpv.calls), api._native_stage
    with pytest.raises(SoapFault) as failure:
        await navigate(api, action, surface)
    assert failure.value.code == 701
    assert asdict(api.state) == before and api.mpv.calls == calls
    assert api._native_stage is stage and api.mpv.paused


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["Next", "Previous"])
async def test_paused_native_transition_rejection_does_not_reconcile_or_resume(tmp_path, action):
    api = make_api(tmp_path, urls=(A, B, C))
    await start(api)
    await api._transport_command("pause")
    api.mpv.advance()  # Native EOF won just before the confirmed pause applied.
    before, calls, stage = asdict(api.state), list(api.mpv.calls), api._native_stage
    with pytest.raises(SoapFault) as failure:
        await navigate(api, action)
    assert failure.value.code == 701
    assert asdict(api.state) == before and api.mpv.calls == calls
    assert api._native_stage is stage and api.mpv.paused and api.mpv.url == B
    await api.sync_mpv_state()
    assert api.state.queue.index == 2 and api.state.phase == PlaybackPhase.PAUSED
    assert mutations(api, "resume") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action,index", [("Previous", 1), ("Next", 2)])
async def test_stopped_invalid_boundary_has_no_effect(tmp_path, action, index):
    api = make_api(tmp_path)
    api.state.queue.index = index
    api._activate_queue_item()
    before, calls = asdict(api.state), list(api.mpv.calls)
    with pytest.raises(SoapFault) as failure:
        await navigate(api, action)
    assert failure.value.code == 711
    assert asdict(api.state) == before and api.mpv.calls == calls


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("owner", ["stop", "pause"])
async def test_explicit_play_queue_index_still_starts_audio(tmp_path, enabled, owner):
    api = make_api(tmp_path, enabled=enabled)
    await start(api)
    await api._transport_command(owner)
    await api._execute_soap_side_effect("PlayQueueWithIndex", "<PlayQueueWithIndex><Index>2</Index></PlayQueueWithIndex>")
    assert api.state.phase == PlaybackPhase.PLAYING and api.state.intent == "play"
    assert api.mpv.url == B and not api.mpv.paused
    assert mutations(api, "load") == [A, B]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,uncertain", [(PlaybackPhase.ERROR, False), (PlaybackPhase.ERROR, True),
    (PlaybackPhase.PAUSED, True), (PlaybackPhase.STOPPED, True), (PlaybackPhase.PLAYING, True)])
@pytest.mark.parametrize("action", ["Next", "Previous"])
async def test_error_and_output_uncertainty_cannot_be_hidden_by_navigation(tmp_path, phase, uncertain, action):
    api = make_api(tmp_path, urls=(A, B, C))
    api.state.queue.index = 2
    api._activate_queue_item()
    await start(api)
    api.state.phase = phase
    api.state.last_error = "Synthetic unconfirmed transport; preserve evidence"
    api._native_output_uncertain = uncertain
    before, calls, stage = asdict(api.state), list(api.mpv.calls), api._native_stage
    with pytest.raises(BackendError):
        await navigate(api, action)
    assert asdict(api.state) == before and api.mpv.calls == calls
    assert api._native_output_uncertain is uncertain and api._native_stage is stage


@pytest.mark.asyncio
async def test_stopped_navigation_cancels_pending_eof_and_rejects_its_old_generation(tmp_path):
    api = make_api(tmp_path, enabled=False, urls=(A, B, C))
    await start(api)
    old_generation, old_queue = api.state.generation, api.state.queue
    api.mpv.idle = True
    await api.sync_mpv_state()
    pending = api._queue_advance_task
    assert pending is not None and api.state.phase == PlaybackPhase.STOPPED
    assert api.state.intent == "play"  # Natural EOF is not a new owner command.
    await navigate(api, "Next")
    with pytest.raises(asyncio.CancelledError):
        await pending
    await api._advance_queue_after_eof(old_generation, old_queue)
    assert api.state.queue.index == 2 and api.state.phase == PlaybackPhase.STOPPED
    assert mutations(api, "load") == [A] and api.mpv.idle


@pytest.mark.asyncio
async def test_previous_after_natural_queue_end_stays_stopped(tmp_path):
    api = make_api(tmp_path)
    await start(api)
    api.mpv.advance()
    await api.sync_mpv_state()
    api.mpv.advance()
    await api.sync_mpv_state()
    assert api.state.phase == PlaybackPhase.STOPPED and api.state.intent == "play"
    await navigate(api, "Previous")
    assert api.state.queue.index == 1 and api.state.phase == PlaybackPhase.STOPPED
    assert api.state.intent == "stop" and api.mpv.idle
    assert mutations(api, "load") == [A]
    assert api._native_current_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("new_owner", ["stop", "pause", "source", "queue"])
async def test_new_owner_wins_during_required_navigation_stop(tmp_path, enabled, new_owner):
    api = make_api(tmp_path, enabled=enabled)
    await start(api)
    api.state.mark_stop()  # Residual backend/native identity still requires checked Stop.
    entered, release = asyncio.Event(), asyncio.Event()
    original_stop = api.mpv.stop
    first = True
    async def delayed_stop():
        nonlocal first
        if first:
            first = False
            entered.set()
            await release.wait()
        await original_stop()
    api.mpv.stop = delayed_stop
    navigation = asyncio.create_task(navigate(api, "Next"))
    await entered.wait()
    if new_owner == "source":
        newer = asyncio.create_task(api._play_url(C))
    elif new_owner == "queue":
        newer = asyncio.create_task(api._replace_queue(PlaybackQueue(items=[QueueItem(C)], index=1)))
    else:
        newer = asyncio.create_task(api._transport_command(new_owner))
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(BackendError):
        await navigation
    results = await asyncio.gather(newer, return_exceptions=True)
    if new_owner == "pause":
        # A pause arriving while Stop applies can fail to confirm the old source;
        # that new owner's error/intent still must not be overwritten by Next.
        assert results[0] is None or isinstance(results[0], BackendError)
        assert api.state.intent == "pause" and api.state.queue.index == 1
    else:
        assert results == [None]
        assert api.state.intent == ("play" if new_owner == "source" else "stop")
    assert api.state.current_url == (C if new_owner in {"source", "queue"} else A)
    assert B not in mutations(api, "load")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["rejected", "lost_ack", "cancelled"])
async def test_required_navigation_stop_failure_preserves_selection(tmp_path, failure):
    api = make_api(tmp_path, enabled=False)
    await start(api)
    api.state.mark_stop()
    original_stop = api.mpv.stop
    entered = asyncio.Event()
    async def failing_stop():
        entered.set()
        if failure == "lost_ack":
            await original_stop()
        if failure == "cancelled":
            await asyncio.Future()
        raise BackendError("Synthetic Stop failure")
    api.mpv.stop = failing_stop
    navigation = asyncio.create_task(navigate(api, "Next"))
    await entered.wait()
    if failure == "cancelled":
        navigation.cancel()
    with pytest.raises(asyncio.CancelledError if failure == "cancelled" else BackendError):
        await navigation
    assert api.state.queue.index == 1 and api.state.current_url == A
    assert api.state.phase == PlaybackPhase.ERROR and api.state.intent == "stop"
    assert "silence not confirmed" in api.state.last_error
    assert mutations(api, "load") == [A]


@pytest.mark.asyncio
async def test_stopped_selection_does_not_revive_cancelled_station_recovery(tmp_path):
    api = make_api(tmp_path, enabled=False)
    api.state.queue.current.stream_type = "station"
    api.state.queue.current.duration = 0
    api._activate_queue_item()
    api._station_recovery_delays = (30,)
    await start(api)
    recovery = api._station_recovery
    api.mpv.idle = True
    await api.sync_mpv_state()
    pending = api._station_recovery_task
    assert pending is not None
    await asyncio.sleep(0)
    await api._transport_command("stop")
    await navigate(api, "Next")
    await asyncio.gather(pending, return_exceptions=True)
    await api._retry_station(recovery)  # A late callback still carries the retired owner.
    for _ in range(3):
        await api.sync_mpv_state()
    assert recovery.invalidated and api._station_recovery is None
    assert api.state.queue.index == 2 and api.state.current_url == B
    assert api.state.phase == PlaybackPhase.STOPPED and api.state.intent == "stop"
    assert api.mpv.idle and mutations(api, "load") == [A]


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["soap", "http"])
@pytest.mark.parametrize("phase,uncertain", [(PlaybackPhase.ERROR, False), (PlaybackPhase.PAUSED, True)])
async def test_single_item_previous_cannot_seek_through_output_uncertainty(tmp_path, surface, phase, uncertain):
    api = make_api(tmp_path, urls=(A,))
    await start(api)
    await api._transport_command("pause")
    api.state.phase = phase
    api._native_output_uncertain = uncertain
    api.state.last_error = "Synthetic unconfirmed output; preserve evidence"
    api.state.curpos = api.mpv.position = 7
    before, calls = asdict(api.state), list(api.mpv.calls)
    with pytest.raises(BackendError):
        await navigate(api, "Previous", surface)
    assert asdict(api.state) == before and api.mpv.calls == calls
    assert api._native_output_uncertain is uncertain
    assert api.mpv.position == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("surface", ["soap", "http"])
@pytest.mark.parametrize("action,index", [("Next", 1), ("Previous", 2)])
async def test_navigation_rejects_paused_intent_during_seek_confirmation(tmp_path, enabled, surface, action, index):
    api = make_api(tmp_path, enabled=enabled, urls=(A, B, C))
    api.state.queue.index = index
    api._activate_queue_item()
    await start(api)
    await api._transport_command("pause")
    entered, release = asyncio.Event(), asyncio.Event()
    original_wait = api._wait_for_seek_ready
    async def delayed_readback(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_wait(*args, **kwargs)
    api._wait_for_seek_ready = delayed_readback
    seeking = asyncio.create_task(api._seek_command(5))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert api.state.phase == PlaybackPhase.BUFFERING and api.state.intent == "pause"
        before, calls = asdict(api.state), list(api.mpv.calls)
        stage, seek_generation = api._native_stage, api._pending_seek_generation
        with pytest.raises(SoapFault) as failure:
            await navigate(api, action, surface)
        assert failure.value.code == 701
        assert asdict(api.state) == before and api.mpv.calls == calls
        assert api._native_stage is stage and api._pending_seek_generation == seek_generation
        assert api.mpv.paused and api.state.queue.index == index
    finally:
        release.set()
        await seeking
    assert api.state.phase == PlaybackPhase.PAUSED and api.state.curpos == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["soap", "http"])
@pytest.mark.parametrize("action,index,target", [("Next", 1, 2), ("Previous", 2, 1)])
@pytest.mark.parametrize("already_applied", [False, True])
async def test_navigation_during_nonstaged_stop_keeps_stop_intent(tmp_path, surface, action, index, target, already_applied):
    api = make_api(tmp_path, enabled=False)
    api.state.queue.index = index
    api._activate_queue_item()
    await start(api)
    entered, release = asyncio.Event(), asyncio.Event()
    original_stop = api.mpv.stop
    first = True
    async def delayed_stop():
        nonlocal first
        if first:
            first = False
            if already_applied:
                await original_stop()
            entered.set()
            await release.wait()
            if already_applied:
                return
        await original_stop()
    api.mpv.stop = delayed_stop
    stopping = asyncio.create_task(api._transport_command("stop"))
    await asyncio.wait_for(entered.wait(), 2)
    assert api.state.phase == PlaybackPhase.COMMAND_ACCEPTED and api.state.intent == "stop"
    navigation = asyncio.create_task(navigate(api, action, surface))
    await asyncio.sleep(0)
    assert api.state.queue.index == index
    assert mutations(api, "load") == [(A, B)[index - 1]]
    release.set()
    with pytest.raises(BackendError):
        await stopping  # The newer navigation owns the checked Stop/selection.
    await navigation
    assert api.state.queue.index == target and api.state.current_url == (A, B)[target - 1]
    assert api.state.phase == PlaybackPhase.STOPPED and api.state.intent == "stop"
    assert api.state.backend_url == api.state.backend_original_url == ""
    assert api.mpv.idle and mutations(api, "load") == [(A, B)[index - 1]]
    assert mutations(api, "resume") == []


@pytest.mark.asyncio
async def test_play_intent_buffering_station_can_navigate_to_finite_item(tmp_path):
    api = make_api(tmp_path, enabled=False)
    api.state.queue.current.stream_type = "station"
    api.state.queue.current.duration = 0
    api._activate_queue_item()
    await start(api)
    api.mpv.buffering = True
    await api.sync_mpv_state()
    assert api.state.phase == PlaybackPhase.BUFFERING and api.state.intent == "play"
    api.mpv.buffering = False
    await navigate(api, "Next")
    assert api.state.queue.index == 2 and api.mpv.url == B
    assert api.state.phase == PlaybackPhase.PLAYING and api.state.intent == "play"
    assert not api.mpv.paused and mutations(api, "load") == [A, B]
