"""UNIT/MOCK source-loss observations cannot control foreign native audio."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.soap import SoapFault
from fake_linkplay.state import PlaybackPhase
from test_native_queue_staging import A, B, C, make_api, start


async def playing(tmp_path, kind="direct"):
    api = make_api(tmp_path, enabled=kind != "direct", urls=(A, B))
    await start(api)
    if kind == "terminal":
        api.mpv.advance()
        await api.sync_mpv_state()
        assert api.state.queue.index == 2 and api._native_stage is None
    return api


def replace_source(api, *, same_url=False):
    url = api.mpv.url if same_url else C
    api.mpv.url, api.mpv.entry_id, api.mpv.idle = url, 999, False
    api.mpv.entries = [{"id": 999, "filename": url}]
    api.mpv.position = 17


def selection(state):
    return {key: value for key, value in asdict(state).items()
            if key not in {"generation", "phase", "last_error", "gain_confirmed"}}


@pytest.mark.parametrize("kind", ["direct", "staged", "terminal"])
@pytest.mark.parametrize("intent", ["play", "pause"])
async def test_coherent_foreign_source_is_error_without_donating_or_mutating(tmp_path, kind, intent):
    api = await playing(tmp_path, kind)
    if intent == "pause":
        await api._transport_command("pause")
    before = selection(api.state)
    generation, native_id, stage = api.state.generation, api._native_current_id, api._native_stage
    replace_source(api, same_url=kind == "terminal")
    calls = list(api.mpv.calls)
    await api.sync_mpv_state()
    assert api.state.phase == PlaybackPhase.ERROR
    assert api.state.generation == generation + 1 and api.state.intent == intent
    assert api._native_output_uncertain and not api.state.gain_confirmed
    assert "source ownership lost" in api.state.last_error
    assert "playback silence not confirmed" in api.state.last_error
    assert selection(api.state) == before
    assert api._native_current_id == native_id and api._native_stage is stage
    if stage is not None:
        assert stage.failed and not api._native_stage_is_current(stage)
    for _ in range(3):
        await api.sync_mpv_state()
    with pytest.raises(SoapFault):
        await api._seek_command(5)
    assert api.mpv.calls == calls
    assert not api._native_cleanup_tasks and api._queue_advance_task is None
    assert api.mpv.url == (B if kind == "terminal" else C) and not api.mpv.idle


@pytest.mark.parametrize("kind", ["direct", "terminal"])
@pytest.mark.parametrize("incomplete", ["no_playlist", "empty_playlist", "bad_id", "duplicate_id", "two_active",
    "path_disagrees", "current_only", "playing_only", "top_parent_only", "entry_parent_only", "invalid_parent", "idle", "eof"])
async def test_partial_or_torn_foreign_observation_does_not_reassign_ownership(tmp_path, kind, incomplete):
    api = await playing(tmp_path, kind)
    before = asdict(api.state)
    replace_source(api, same_url=kind == "terminal")
    snap = await api.mpv.snapshot()
    if incomplete == "no_playlist":
        snap.pop("playlist")
    elif incomplete == "empty_playlist":
        snap["playlist"] = []
    elif incomplete == "bad_id":
        snap["playlist"][0]["id"] = True
    elif incomplete == "duplicate_id":
        snap["playlist"].append({"id": 999, "filename": A})
    elif incomplete == "two_active":
        snap["playlist"].append({"id": 998, "filename": A, "current": True, "playing": True})
    elif incomplete == "path_disagrees":
        snap["playlist"][0]["filename"] = A
    elif incomplete == "current_only":
        snap["playlist"][0].pop("playing")
    elif incomplete == "playing_only":
        snap["playlist"][0].pop("current")
    elif incomplete == "top_parent_only":
        snap["playlist-path"] = A
    elif incomplete == "entry_parent_only":
        snap["playlist"][0]["playlist-path"] = A
    elif incomplete == "invalid_parent":
        snap["playlist-path"] = snap["playlist"][0]["playlist-path"] = False
    elif incomplete == "idle":
        snap["idle-active"] = True
    else:
        snap["eof-reached"] = True
    calls = list(api.mpv.calls)
    api._apply_backend_snapshot(snap)
    assert asdict(api.state) == before
    assert api.mpv.calls == calls and not api._native_output_uncertain


@pytest.mark.parametrize("phase,intent", [(PlaybackPhase.STOPPED, "stop"), (PlaybackPhase.PAUSED, "pause")])
@pytest.mark.parametrize("idle", [False, True])
async def test_unloaded_selection_ignores_foreign_media_but_shows_backend_failure(tmp_path, phase, intent, idle):
    api = make_api(tmp_path, enabled=False, urls=(A,))
    api.state.phase, api.state.intent = phase, intent
    api.state.queue.current.title_is_fallback = True
    api.state.queue.current.duration = api.queue_duration_sec = 0
    replace_source(api)
    api.mpv.idle = idle
    before = asdict(api.state)
    calls = list(api.mpv.calls)
    await api.sync_mpv_state()
    assert asdict(api.state) == before
    assert api.mpv.calls == calls and not api._native_output_uncertain
    api.mpv.fail = True
    await api.sync_mpv_state()
    assert api.state.phase == PlaybackPhase.ERROR and api.state.last_error == "Backend control failed"


@pytest.mark.parametrize("kind", ["direct", "terminal"])
async def test_old_generation_foreign_snapshot_cannot_poison_new_owner(tmp_path, kind):
    api = await playing(tmp_path, kind)
    replace_source(api, same_url=kind == "terminal")
    entered, release = asyncio.Event(), asyncio.Event()
    original = api.mpv.snapshot
    async def delayed():
        snap = await original()
        entered.set()
        await release.wait()
        return snap
    api.mpv.snapshot = delayed
    task = asyncio.create_task(api.sync_mpv_state())
    await entered.wait()
    api._accept_playback_command("stop")
    api.state.mark_stop()
    before = asdict(api.state)
    release.set()
    await task
    assert asdict(api.state) == before and not api._native_output_uncertain


async def test_loss_invalidates_a_late_owned_observation(tmp_path):
    api = await playing(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    original = api.mpv.snapshot
    async def delayed():
        snap = await original()
        entered.set()
        await release.wait()
        return snap
    api.mpv.snapshot = delayed
    task = asyncio.create_task(api.sync_mpv_state())
    await entered.wait()
    replace_source(api)
    api._apply_backend_snapshot(await original())
    before = asdict(api.state)
    release.set()
    await task
    assert api.state.phase == PlaybackPhase.ERROR and asdict(api.state) == before


@pytest.mark.parametrize("kind", ["direct", "terminal"])
@pytest.mark.parametrize("command", ["stop", "play"])
async def test_explicit_owner_recovery_reuses_checked_stop_and_load(tmp_path, kind, command):
    api = await playing(tmp_path, kind)
    replace_source(api, same_url=kind == "terminal")
    await api.sync_mpv_state()
    calls = len(api.mpv.calls)
    selected = api.state.current_url
    await api._execute_soap_side_effect(command.capitalize(), f"<{command.capitalize()}/>")
    assert not api._native_output_uncertain
    if command == "stop":
        assert api.state.phase == PlaybackPhase.STOPPED and api.mpv.idle
        assert api.mpv.calls[calls:] == [("stop", None)]
        assert not api.state.backend_url
    else:
        assert api.state.phase == PlaybackPhase.PLAYING and api.mpv.url == selected
        assert api.state.gain_confirmed
        assert api.mpv.calls[calls:] == [("stop", None), ("load", selected)]


async def test_failed_explicit_stop_cannot_load_or_claim_silence(tmp_path):
    api = await playing(tmp_path)
    replace_source(api)
    await api.sync_mpv_state()
    api.mpv.stop = AsyncMock(side_effect=BackendError("Synthetic Stop rejection"))
    resolver = api._resolve_playback_url = AsyncMock(return_value=A)
    calls = list(api.mpv.calls)
    with pytest.raises(BackendError):
        await api._execute_soap_side_effect("Play", "<Play/>")
    assert api._native_output_uncertain and api.state.phase == PlaybackPhase.ERROR
    assert "silence not confirmed" in api.state.last_error
    resolver.assert_not_awaited()
    assert api.mpv.calls == calls and api.mpv.url == C


@pytest.mark.parametrize("container", [False, True])
async def test_valid_resolved_source_and_container_children_remain_owned(tmp_path, container):
    api = make_api(tmp_path, enabled=False, urls=(A,))
    api._resolve_playback_url = AsyncMock(return_value=B)
    await start(api)
    if container:
        api.mpv.url, api.mpv.entry_id, api.mpv.playlist_path = C, 2, B
        api.mpv.entries = [{"id": 2, "filename": C, "playlist-path": B}]
    api.mpv.position = 5
    await api.sync_mpv_state()
    assert api.state.phase == PlaybackPhase.PLAYING and api.state.curpos == 5
    assert api.state.current_url == A and api.state.backend_url == B
    assert not api._native_output_uncertain
    assert api._native_control_source_matches(await api.mpv.snapshot())


async def test_foreign_loss_cancels_queued_eof_and_stale_unregistered_advance(tmp_path):
    api = await playing(tmp_path)
    generation, queue = api.state.generation, api.state.queue
    task = api._queue_advance_task = asyncio.create_task(api._advance_queue_after_eof(generation, queue))
    replace_source(api)
    calls = list(api.mpv.calls)
    api._apply_backend_snapshot(await api.mpv.snapshot())
    await asyncio.gather(task, return_exceptions=True)
    await api._advance_queue_after_eof(generation, queue)
    assert task.cancelled() and api._queue_advance_task is None
    assert api.state.phase == PlaybackPhase.ERROR and api.state.queue.index == 1
    assert api.mpv.calls == calls


@pytest.mark.parametrize("ended", [False, True])
async def test_foreign_loss_cancels_pending_station_recovery(tmp_path, ended):
    api = make_api(tmp_path, enabled=False, urls=(A, B))
    api.state.queue.current.stream_type = "station"
    api.state.queue.current.duration = 0
    api._activate_queue_item()
    await start(api)
    api._station_recovery_delays = (60,)
    recovery = api._station_recovery
    if ended:
        api.mpv.idle, api.mpv.url = True, ""
        await api.sync_mpv_state()
        assert not api.state.backend_url  # Expected identity now comes from recovery.
    else:
        api._recover_station_after_end("source_stall")
    task = api._station_recovery_task
    await asyncio.sleep(0)
    replace_source(api)
    calls = list(api.mpv.calls)
    await api.sync_mpv_state()
    async with asyncio.timeout(1):
        await asyncio.gather(task, return_exceptions=True)
    assert recovery.invalidated and api._station_recovery is None
    assert task.cancelled() and api._station_recovery_task is None
    assert api.state.phase == PlaybackPhase.ERROR and api.state.intent == "play"
    assert api.mpv.calls == calls and api.mpv.url == C


async def test_foreign_loss_fences_pending_seek_confirmation(tmp_path):
    api = await playing(tmp_path)
    api._seek_timeout = .2
    entered, release = asyncio.Event(), asyncio.Event()
    original = api._wait_for_seek_ready
    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    api._wait_for_seek_ready = delayed
    task = asyncio.create_task(api._seek_command(5))
    await entered.wait()
    assert api._pending_seek_target == 5
    seek_generation = api._pending_seek_generation
    replace_source(api)
    calls = list(api.mpv.calls)
    await api.sync_mpv_state()
    before = asdict(api.state)
    release.set()
    with pytest.raises(BackendError, match="superseded"):
        await task
    assert api._pending_seek_generation > seek_generation and api._pending_seek_target is None
    assert api.state.phase == PlaybackPhase.ERROR and asdict(api.state) == before
    assert api.mpv.calls == calls and api.mpv.url == C


@pytest.mark.parametrize("boundary", ["retry_wake", "resolver"])
async def test_recovery_pre_admission_rejects_foreign_without_observer_poll(tmp_path, boundary):
    api = make_api(tmp_path, enabled=False, urls=(A, B))
    api.state.queue.current.stream_type = "station"
    api.state.queue.current.duration = 0
    api._activate_queue_item()
    await start(api)
    api._station_recovery_delays = (.01,)
    entered, release = asyncio.Event(), asyncio.Event()
    if boundary == "resolver":
        api.state.queue.current.resolved_url = ""
        async def resolve(url):
            entered.set()
            await release.wait()
            return A
        api._resolve_playback_url = resolve
    api.mpv.idle, api.mpv.url = True, ""
    await api.sync_mpv_state()
    task = api._station_recovery_task
    if boundary == "resolver":
        await entered.wait()
    before = selection(api.state)
    replace_source(api)
    calls = list(api.mpv.calls)
    release.set()
    async with asyncio.timeout(1):
        await task
    assert api.state.phase == PlaybackPhase.ERROR and api._native_output_uncertain
    assert selection(api.state) == before and api.mpv.calls == calls and api.mpv.url == C


@pytest.mark.parametrize("same_url", [False, True])
@pytest.mark.parametrize("observer_poll", [False, True])
async def test_real_eof_retirement_blocks_new_active_source_before_advance(tmp_path, same_url, observer_poll):
    api = await playing(tmp_path)
    api.mpv.advance()
    await api.sync_mpv_state()
    task = api._queue_advance_task
    assert task is not None and not task.done() and not api.state.backend_url
    before = selection(api.state)
    replace_source(api)
    if same_url:
        api.mpv.url = api.mpv.entries[0]["filename"] = A
    calls = list(api.mpv.calls)
    if observer_poll:
        await api.sync_mpv_state()
    await asyncio.gather(task, return_exceptions=True)
    assert api.state.phase == PlaybackPhase.ERROR and api._native_output_uncertain
    assert "Automatic playback source ownership unconfirmed" in api.state.last_error
    assert selection(api.state) == before and api.state.queue.index == 1
    assert api.mpv.calls == calls and api.mpv.url == (A if same_url else C)


async def test_eof_advance_rechecks_foreign_after_resolver_yield(tmp_path):
    api = await playing(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    async def resolve(url):
        entered.set()
        await release.wait()
        return url
    api._resolve_playback_url = resolve
    api.mpv.advance()
    await api.sync_mpv_state()
    task = api._queue_advance_task
    await entered.wait()
    assert api.state.phase == PlaybackPhase.LOADING
    replace_source(api)
    calls = list(api.mpv.calls)
    release.set()
    await task
    assert api.state.phase == PlaybackPhase.ERROR and api._native_output_uncertain
    assert api.mpv.calls == calls and api.mpv.url == C


@pytest.mark.parametrize("terminal", ["idle", "eof"])
async def test_terminal_preflight_retains_ordinary_eof_advancement(tmp_path, terminal):
    api = await playing(tmp_path)
    original = api.mpv.snapshot
    async def snapshot():
        snap = await original()
        if terminal == "eof" and api.mpv.url == A:
            snap["eof-reached"] = True
        return snap
    api.mpv.snapshot = snapshot
    if terminal == "idle":
        api.mpv.advance()
    await api.sync_mpv_state()
    task = api._queue_advance_task
    await task
    assert api.state.phase == PlaybackPhase.PLAYING and not api._native_output_uncertain
    assert api.state.queue.index == 2 and api.mpv.url == B


@pytest.mark.parametrize("new_owner", ["stop", "index"])
async def test_queued_advance_fresh_snapshot_cannot_poison_new_owner(tmp_path, new_owner):
    api = await playing(tmp_path)
    api.mpv.advance()
    await api.sync_mpv_state()
    task = api._queue_advance_task
    entered, release = asyncio.Event(), asyncio.Event()
    original = api.mpv.snapshot
    async def delayed():
        replace_source(api)
        snap = await original()
        entered.set()
        await release.wait()
        return snap
    api.mpv.snapshot = delayed
    await entered.wait()
    if new_owner == "stop":
        # Emulate acceptance without cancelling the waiter, exercising its own
        # post-await generation fence independently from task cancellation.
        api.state.accept_command("stop")
        api.state.mark_stop()
    else:
        api.state.queue.index = 2
        api._activate_queue_item()
    before, calls = asdict(api.state), list(api.mpv.calls)
    release.set()
    await task
    assert asdict(api.state) == before and api.mpv.calls == calls
    assert not api._native_output_uncertain


@pytest.mark.parametrize("failure", [BackendError, OSError, TimeoutError])
async def test_eof_preflight_exception_is_visible_without_loading(tmp_path, failure):
    api = await playing(tmp_path)
    api.mpv.advance()
    await api.sync_mpv_state()
    task = api._queue_advance_task
    calls = list(api.mpv.calls)
    api.mpv.snapshot = AsyncMock(side_effect=failure("Synthetic preflight failure"))
    await task
    assert api.state.phase == PlaybackPhase.ERROR and api.state.last_error == "Backend control failed"
    assert api.state.queue.index == 1 and api.mpv.calls == calls


@pytest.mark.parametrize("boundary", ["before_wake", "wake_snapshot", "resolver", "load_snapshot"])
async def test_eof_admission_never_clears_same_generation_terminal_error(tmp_path, boundary):
    api = await playing(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    original = api.mpv.snapshot
    count = 0
    async def delayed_snapshot():
        nonlocal count
        snap = await original()
        count += 1
        if count == (2 if boundary == "load_snapshot" else 1):
            entered.set()
            await release.wait()
        return snap
    async def resolve(url):
        entered.set()
        await release.wait()
        return url
    api.mpv.advance()
    await api.sync_mpv_state()
    task = api._queue_advance_task
    if boundary in {"wake_snapshot", "load_snapshot"}:
        api.mpv.snapshot = delayed_snapshot
    elif boundary == "resolver":
        api._resolve_playback_url = resolve
    if boundary != "before_wake":
        await entered.wait()
    api._mark_backend_failure()
    before, calls = asdict(api.state), list(api.mpv.calls)
    release.set()
    async with asyncio.timeout(1):
        await task
    assert asdict(api.state) == before and api.mpv.calls == calls
    assert api.state.phase == PlaybackPhase.ERROR and api.state.last_error == "Backend control failed"
