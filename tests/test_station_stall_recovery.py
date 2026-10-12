"""UNIT/MOCK bounded stall detection shares station recovery's owner fences."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from fake_linkplay.http_api import ParsedCommand
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.state import PlaybackPhase
from test_station_recovery import api, play_station, loads, finish, eof, STATION, NEXT


@pytest.fixture
async def stalled(api):
    await play_station(api)
    now = [0.0]
    api._station_stall_clock = lambda: now[0]
    api.mpv.buffering = True
    api.mpv.position = 9.5
    return api, now


async def expire(api, now):
    await api.sync_mpv_state()
    now[0] += api._station_stall_timeout
    await api.sync_mpv_state()
    return api._station_recovery_task


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", ["paused-for-cache", "core-idle"])
async def test_sustained_station_stall_reloads_after_time_not_poll_count(stalled, signal, caplog):
    api, now = stalled
    original = api.mpv.snapshot
    async def snapshot():
        snap = await original()
        snap["paused-for-cache"] = signal == "paused-for-cache" and api.mpv.buffering
        snap["core-idle"] = signal == "core-idle" and api.mpv.buffering
        return snap
    api.mpv.snapshot = snapshot
    generation = api.state.generation
    for _ in range(30):
        await api.sync_mpv_state()
    assert api.state.phase == PlaybackPhase.BUFFERING
    assert api._station_recovery_task is None
    now[0] = api._station_stall_timeout - .01
    await api.sync_mpv_state()
    assert api._station_recovery_task is None
    now[0] += .01
    with caplog.at_level("INFO"):
        await api.sync_mpv_state()
        task = api._station_recovery_task
        assert task is not None
        await finish(task)
    assert loads(api) == [STATION, STATION]
    assert api.state.generation == generation and api.state.queue.index == 1
    assert api._station_recovery.attempts == 1
    assert "category=source_stall" in caplog.text
    assert api._station_recovery.stall_since is None
    await api.sync_mpv_state()
    assert api._station_recovery.stall_since == now[0]


@pytest.mark.asyncio
async def test_transient_buffering_and_advancing_position_reset_stall_deadline(stalled):
    api, now = stalled
    for index in range(6):
        api.mpv.position += .1
        now[0] += api._station_stall_timeout
        await api.sync_mpv_state()
    assert api._station_recovery_task is None
    api.mpv.buffering = False
    await api.sync_mpv_state()
    assert api._station_recovery.stall_since is None
    now[0] += 100
    api.mpv.buffering = True
    await api.sync_mpv_state()
    assert api._station_recovery_task is None
    assert loads(api) == [STATION]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["path", "missing_path", "idle", "paused", "nan", "inf", "bool", "missing_position"])
async def test_unconfirmed_observation_resets_stall_evidence(stalled, invalid):
    api, now = stalled
    await api.sync_mpv_state()
    now[0] += 14
    snapshot = await api.mpv.snapshot()
    if invalid == "path":
        snapshot["path"] = NEXT
    elif invalid == "missing_path":
        snapshot.pop("path")
    elif invalid == "idle":
        snapshot["idle-active"] = None
    elif invalid == "paused":
        snapshot["pause"] = True
    elif invalid == "missing_position":
        snapshot.pop("playback-time")
    else:
        snapshot["playback-time"] = {"nan": float("nan"), "inf": float("inf"), "bool": True}[invalid]
    api._apply_backend_snapshot(snapshot)
    assert api._station_recovery.stall_since is None
    now[0] += 2
    await api.sync_mpv_state()
    assert api._station_recovery_task is None
    assert loads(api) == [STATION]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_type,duration", [("track", 0), ("", 0), ("track", 30), ("station", 30)])
async def test_finite_and_unmarked_media_never_recover_stalls(api, stream_type, duration):
    await play_station(api, stream_type=stream_type, duration=duration)
    api._station_stall_clock = lambda: 100000
    api._station_stall_timeout = 0
    api.mpv.buffering = True
    for _ in range(8):
        await api.sync_mpv_state()
    assert api._station_recovery is None and api._station_recovery_task is None
    assert loads(api) == [STATION]


@pytest.mark.asyncio
async def test_initial_unestablished_buffering_does_not_start_stall_recovery(stalled):
    api, now = stalled
    api._station_recovery.established = False
    assert await expire(api, now) is None
    assert api._station_recovery.stall_since is None
    assert loads(api) == [STATION]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["pause", "stop", "play"])
@pytest.mark.parametrize("stage", ["timer", "backoff"])
async def test_owner_command_invalidates_stall_or_scheduled_retry(stalled, command, stage):
    api, now = stalled
    api._station_recovery_delays = (60, 60, 60)
    await api.sync_mpv_state()
    task = None
    if stage == "backoff":
        task = await expire(api, now)
        await asyncio.sleep(0)
    api.mpv.buffering = False
    await api.execute(ParsedCommand(command, (NEXT,) if command == "play" else ()), "192.0.2.1")
    if task:
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
    now[0] += 100
    for _ in range(4):
        await api.sync_mpv_state()
    assert loads(api) == [STATION] + ([NEXT] if command == "play" else [])
    assert api._station_recovery is None


@pytest.mark.asyncio
async def test_native_return_during_stall_backoff_avoids_reload(stalled):
    api, now = stalled
    api._station_recovery_delays = (.01, .01, .01)
    task = await expire(api, now)
    api.mpv.buffering = False
    await finish(task)
    assert loads(api) == [STATION]
    assert api._station_recovery.attempts == 0
    assert api.state.phase == PlaybackPhase.PLAYING
    assert api._station_recovery.stall_since is None


@pytest.mark.asyncio
async def test_stalls_and_eof_share_finite_budget_and_final_stall_confirms_silence(stalled, caplog):
    api, now = stalled
    # EOF spends one retry from the same budget.
    await finish(await eof(api))
    for expected in (2, 3):
        await finish(await expire(api, now))
        assert api._station_recovery.attempts == expected
    with caplog.at_level("INFO"):
        await finish(await expire(api, now))
    assert loads(api) == [STATION] * 4
    assert api.mpv.idle is True
    assert api.state.backend_url == api.state.backend_original_url == ""
    assert api.state.phase == PlaybackPhase.ERROR
    assert api.state.last_error == "Station source unavailable: recovery exhausted"
    assert "category=source_stall outcome=exhausted" in caplog.text
    for _ in range(12):
        await api.sync_mpv_state()
    assert api._station_recovery_task is None
    assert loads(api) == [STATION] * 4


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["stop", "snapshot", "foreign_path"])
async def test_exhaustion_cleanup_failure_is_terminal_and_never_stops_foreign_audio(stalled, failure):
    api, now = stalled
    api._station_recovery.attempts = 3
    task = await expire(api, now)
    if failure == "stop":
        api.mpv.stop = AsyncMock(side_effect=BackendError("Synthetic Stop refusal"))
    elif failure == "snapshot":
        api.mpv.snapshot = AsyncMock(return_value={"backend-error": True})
    else:
        api.mpv.url = NEXT
    await finish(task)
    assert api.state.phase == PlaybackPhase.ERROR
    assert api.state.last_error == "Backend control failed; playback silence not confirmed"
    assert loads(api) == [STATION]
    if failure != "stop":
        assert not any(command == "stop" for command, _ in api.mpv.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["pause", "stop", "play"])
@pytest.mark.parametrize("stage", ["snapshot", "stop"])
async def test_owner_command_arriving_during_terminal_stop_runs_after_cleanup(stalled, command, stage):
    api, now = stalled
    api._station_recovery.attempts = 3
    entered, release = asyncio.Event(), asyncio.Event()
    task = await expire(api, now)
    original = getattr(api.mpv, stage)
    async def blocked():
        entered.set()
        await release.wait()
        return await original()
    setattr(api.mpv, stage, blocked)
    await entered.wait()
    owner = asyncio.create_task(api.execute(ParsedCommand(command, (NEXT,) if command == "play" else ()), "192.0.2.1"))
    await asyncio.sleep(0)
    assert not task.done() and not owner.done()
    api.mpv.buffering = False
    release.set()
    await finish(task)
    await finish(owner)
    assert api.state.phase == {"pause": PlaybackPhase.PAUSED, "stop": PlaybackPhase.STOPPED, "play": PlaybackPhase.PLAYING}[command]
    assert api.state.intent == command
    assert api.state.last_error == ""
    assert loads(api) == [STATION] + ([NEXT] if command == "play" else [])


@pytest.mark.asyncio
async def test_native_return_just_before_terminal_cleanup_preserves_audio(stalled):
    api, now = stalled
    api._station_recovery.attempts = 3
    task = await expire(api, now)
    api.mpv.buffering = False
    await finish(task)
    assert api.state.phase == PlaybackPhase.PLAYING
    assert api._station_recovery.attempts == 3
    assert loads(api) == [STATION]
    assert not any(command == "stop" for command, _ in api.mpv.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["lock", "snapshot", "stop"])
async def test_last_listener_shutdown_drains_terminal_stall_cleanup(stalled, stage):
    api, now = stalled
    api._monitor_interval = 60
    context = api._state_monitor_context(None)
    await anext(context)
    api._station_recovery.attempts = 3
    entered, release = asyncio.Event(), asyncio.Event()
    task = await expire(api, now)
    if stage == "lock":
        await api._backend_lock.acquire()
        await asyncio.sleep(0)
        assert api._station_recovery_loading
    else:
        original = getattr(api.mpv, stage)
        async def blocked():
            entered.set()
            await release.wait()
            return await original()
        setattr(api.mpv, stage, blocked)
        await entered.wait()
    closing = asyncio.create_task(context.aclose())
    await asyncio.sleep(0)
    assert api._station_recovery is None
    assert not task.done() and not closing.done()
    release.set()
    if stage == "lock":
        api._backend_lock.release()
    await finish(closing)
    assert task.done() and api._station_recovery_task is None
    assert api._state_monitor_task is None
    assert api.mpv.idle and not api.state.backend_url
    assert loads(api) == [STATION]


@pytest.mark.asyncio
async def test_shutdown_cleanup_failure_retains_unconfirmed_silence_error(stalled):
    api, now = stalled
    api._monitor_interval = 60
    context = api._state_monitor_context(None)
    await anext(context)
    api._station_recovery.attempts = 3
    entered, release = asyncio.Event(), asyncio.Event()
    async def stop():
        entered.set()
        await release.wait()
        raise BackendError("Synthetic Stop refusal during shutdown")
    api.mpv.stop = stop
    task = await expire(api, now)
    await entered.wait()
    closing = asyncio.create_task(context.aclose())
    await asyncio.sleep(0)
    release.set()
    await finish(closing)
    assert task.done() and api._station_recovery_task is None
    assert api.state.phase == PlaybackPhase.ERROR
    assert api.state.last_error == "Backend control failed; playback silence not confirmed"


@pytest.mark.asyncio
async def test_progress_during_backoff_should_require_new_sustained_grace(stalled):
    api, now = stalled
    api._station_recovery_delays = (.03, .03, .03)
    task = await expire(api, now)
    await asyncio.sleep(0)
    now[0] += .01
    api.mpv.position += .1
    await api.sync_mpv_state()
    assert api._station_recovery.stall_since == now[0]
    await finish(task)
    assert loads(api) == [STATION], 'New decoded progress reset the stall grace, so the earlier episode must not cause a reload'


@pytest.mark.asyncio
async def test_progress_before_terminal_snapshot_should_not_stop_new_episode(stalled):
    api, now = stalled
    api._station_recovery.attempts = 3
    task = await expire(api, now)
    now[0] += .01
    api.mpv.position += .1
    await finish(task)
    assert not api.mpv.idle, 'Terminal snapshot has fresh decoded progress; do not Stop before the new grace expires'
    assert api.state.phase == PlaybackPhase.BUFFERING
    assert api._station_recovery.stall_since == now[0]
    assert loads(api) == [STATION]


@pytest.mark.asyncio
async def test_native_return_then_restall_during_backoff_gets_fresh_grace(stalled):
    api, now = stalled
    api._station_recovery_delays = (.03, .03, .03)
    task = await expire(api, now)
    await asyncio.sleep(0)
    now[0] += .01
    api.mpv.buffering = False
    await api.sync_mpv_state()
    assert api._station_recovery.stall_since is None
    now[0] += .01
    api.mpv.buffering = True
    await api.sync_mpv_state()
    assert api._station_recovery.stall_since == now[0]
    await finish(task)
    assert loads(api) == [STATION], 'A new stall episode during backoff must receive the full sustained grace'


@pytest.mark.asyncio
async def test_progress_observed_only_in_retry_preflight_gets_new_grace(stalled):
    api, now = stalled
    task = await expire(api, now)
    now[0] += .01
    api.mpv.position += .1
    await finish(task)
    assert loads(api) == [STATION]
    assert api._station_recovery.attempts == 0
    assert api._station_recovery.stall_since == now[0]
    assert api.state.phase == PlaybackPhase.BUFFERING
    assert api.state.last_error == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["resolution", "readiness"])
async def test_stall_unready_retry_loop_still_exhausts_finite_budget(stalled, failure):
    api, now = stalled
    if failure == "resolution":
        api.state.queue.current.resolved_url = ""
        api._resolve_playback_url = AsyncMock(side_effect=BackendError("Synthetic resolver failure"))
    else:
        api._wait_for_mpv_audio_ready = AsyncMock(return_value=False)
    await finish(await expire(api, now))
    assert api._station_recovery.attempts == 3
    assert api.state.phase == PlaybackPhase.ERROR
    assert api.state.last_error == "Station source unavailable: recovery exhausted"
    assert api.mpv.idle is True
    assert loads(api) == [STATION] * (1 if failure == "resolution" else 4)


@pytest.mark.asyncio
async def test_foreign_source_in_stall_preflight_is_never_loaded_or_stopped(stalled):
    api, now = stalled
    task = await expire(api, now)
    api.mpv.url = NEXT
    await finish(task)
    assert api.state.phase == PlaybackPhase.ERROR
    assert api.state.last_error == "Backend control failed"
    assert loads(api) == [STATION]
    assert not any(command == "stop" for command, _ in api.mpv.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("attempts", [0, 3])
async def test_deferred_progress_gets_full_grace_then_recovers_or_exhausts(stalled, attempts):
    api, now = stalled
    api._station_recovery.attempts = attempts
    task = await expire(api, now)
    now[0] += .01
    api.mpv.position += .1
    await finish(task)
    assert loads(api) == [STATION] and not api.mpv.idle
    assert api._station_recovery.attempts == attempts
    now[0] += api._station_stall_timeout - .1
    await api.sync_mpv_state()
    assert api._station_recovery_task is None
    now[0] += .1
    await api.sync_mpv_state()
    await finish(api._station_recovery_task)
    if attempts == 3:
        assert api.mpv.idle and api.state.phase == PlaybackPhase.ERROR
        assert loads(api) == [STATION]
    else:
        assert loads(api) == [STATION, STATION]
        assert api._station_recovery.attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("attempts", [0, 3])
async def test_source_ends_after_stall_detection_without_requiring_another_grace(stalled, attempts):
    api, now = stalled
    api._station_recovery.attempts = attempts
    task = await expire(api, now)
    api.mpv.idle = True
    api.mpv.position = 0
    await finish(task)
    if attempts == 3:
        assert api.mpv.idle and api.state.phase == PlaybackPhase.ERROR
        assert loads(api) == [STATION]
    else:
        assert loads(api) == [STATION, STATION]
        assert api._station_recovery.attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("returned_audio", [False, True])
async def test_progress_during_stall_resolution_defers_reload_and_restores_phase(stalled, returned_audio, caplog):
    api, now = stalled
    api.state.queue.current.resolved_url = ""
    entered, release = asyncio.Event(), asyncio.Event()
    async def resolve(url):
        entered.set()
        await release.wait()
        return url
    api._resolve_playback_url = resolve
    with caplog.at_level("INFO"):
        task = await expire(api, now)
        await entered.wait()
        now[0] += .1
        api.mpv.position += .1
        api.mpv.buffering = not returned_audio
        release.set()
        await finish(task)
    assert loads(api) == [STATION]
    assert api._station_recovery.attempts == 0
    assert api.state.phase == (PlaybackPhase.PLAYING if returned_audio else PlaybackPhase.BUFFERING)
    assert api.state.last_error == ""
    assert api._station_recovery.stall_since == (None if returned_audio else now[0])
    assert "outcome=recovered" not in caplog.text
    # The monitor can observe the next episode, rather than remaining LOADING.
    api.mpv.buffering = True
    await api.sync_mpv_state()
    now[0] += api._station_stall_timeout
    await api.sync_mpv_state()
    await finish(api._station_recovery_task)
    assert loads(api) == [STATION, STATION]
    assert api._station_recovery.attempts == 1


@pytest.mark.asyncio
async def test_progress_while_waiting_for_load_lock_defers_stall_reload(stalled):
    api, now = stalled
    api.state.queue.current.resolved_url = ""
    entered, release = asyncio.Event(), asyncio.Event()
    async def resolve(url):
        entered.set()
        await release.wait()
        return url
    api._resolve_playback_url = resolve
    task = await expire(api, now)
    await entered.wait()
    await api._backend_lock.acquire()
    release.set()
    await asyncio.sleep(0)
    now[0] += .1
    api.mpv.position += .1
    api._backend_lock.release()
    await finish(task)
    assert loads(api) == [STATION]
    assert api._station_recovery.attempts == 0
    assert api.state.phase == PlaybackPhase.BUFFERING
    assert api._station_recovery.stall_since == now[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('attempts', [0, 3])
async def test_expanded_playlist_child_eof_is_not_terminal_station_eof(stalled, attempts):
    api, now = stalled
    api._station_recovery.attempts = attempts
    task = await expire(api, now)
    original = api.mpv.snapshot
    async def child_eof():
        snap = await original()
        snap.update({'path': NEXT, 'playlist-path': STATION,
                     'playlist': [{'filename': NEXT, 'playlist-path': STATION, 'current': True, 'playing': True}],
                     'eof-reached': True})
        return snap
    api.mpv.snapshot = child_eof
    await finish(task)
    assert loads(api) == [STATION]
    assert not api.mpv.idle
    assert api.state.phase != PlaybackPhase.ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize("attempts", [0, 3])
async def test_foreign_eof_does_not_bypass_stall_source_identity(stalled, attempts):
    api, now = stalled
    api._station_recovery.attempts = attempts
    task = await expire(api, now)
    api.mpv.url = NEXT
    original = api.mpv.snapshot
    async def foreign_eof():
        return dict(await original(), **{'eof-reached': True})
    api.mpv.snapshot = foreign_eof
    await finish(task)
    assert loads(api) == [STATION]
    assert not any(command == 'stop' for command, _ in api.mpv.calls)
    assert api.state.phase == PlaybackPhase.ERROR
