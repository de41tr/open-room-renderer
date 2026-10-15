"""UNIT/MOCK: unsuccessful loads relinquish actual audio without stopping newer owners."""

import asyncio
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi, SourcePlaybackError
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.queue import PlaybackQueue, QueueItem
from fake_linkplay.state import PlaybackPhase, RendererState
from test_playback_generations import StatefulBackend


SOURCE = "https://media.example.test/outer.m3u"
CHILD = "https://media.example.test/inner.wav"
NEXT = "https://media.example.test/next.wav"


@pytest.fixture
async def api(tmp_path):
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path))
    result = HttpApi(cfg, RendererState.from_config(cfg), StatefulBackend())
    result._wait_for_mpv_audio_ready = AsyncMock(return_value=False)
    yield result
    result._invalidate_station_recovery()
    tasks = [task for task in (result._station_recovery_task, result._queue_advance_task) if task]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def calls(api, command):
    return [value for name, value in api.mpv.calls if name == command]


async def unconfirmed_native_audio(api, generation, url):
    # Model nested expansion: mpv really decodes the child, while it has lost
    # the outer container's ancestry and cannot prove that requested source.
    api.mpv.url = CHILD
    snapshot = await api.mpv.snapshot()
    assert snapshot["idle-active"] is False
    assert HttpApi._mpv_snapshot_has_audio(snapshot)
    assert not HttpApi._mpv_snapshot_matches_source(snapshot, url)
    return False


@pytest.mark.asyncio
async def test_unready_active_audio_is_stopped_before_source_failure_returns(api):
    api._wait_for_mpv_audio_ready = lambda *args: unconfirmed_native_audio(api, *args)
    with pytest.raises(SourcePlaybackError, match="Source did not confirm playback"):
        await api._play_url(SOURCE)
    assert api.mpv.idle and api.mpv.url == ""
    assert calls(api, "stop") == [None]
    assert api.state.backend_url == api.state.backend_original_url == ""
    assert api.state.current_url == api.state.queue.current.url == SOURCE
    assert api.state.resolved_url == SOURCE
    assert api.state.phase == PlaybackPhase.ERROR
    assert api.state.last_error == "Source did not confirm playback"
    assert api._station_recovery is None and api._queue_advance_task is None
    await api.sync_mpv_state()
    assert api.state.phase == PlaybackPhase.ERROR


@pytest.mark.asyncio
async def test_cleanup_waits_for_actual_idle_before_clearing_identity(api):
    snapshots = 0
    original = api.mpv.snapshot

    async def snapshot():
        nonlocal snapshots
        snapshots += 1
        if snapshots == 3:
            api.mpv.idle, api.mpv.url = True, ""
        else:
            assert api.state.backend_url == SOURCE
        return await original()

    api.mpv.snapshot = snapshot
    api.mpv.stop = AsyncMock(return_value={"error": "success"})
    with pytest.raises(SourcePlaybackError):
        await api._play_url(SOURCE)
    assert snapshots == 3
    assert api.state.backend_url == api.state.backend_original_url == ""
    assert api.mpv.idle


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["stop_exception", "stop_error_result", "snapshot_error", "snapshot_exception",
                                      "stop_timeout", "snapshot_timeout", "never_idle"])
async def test_cleanup_failure_is_explicit_and_does_not_clear_identity(api, monkeypatch, stage):
    timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda delay: timeout(min(delay, 0.04)))

    async def forever():
        await asyncio.Future()

    if stage == "stop_exception":
        api.mpv.stop = AsyncMock(side_effect=BackendError("Lost Stop acknowledgement"))
    elif stage == "stop_error_result":
        api.mpv.stop = AsyncMock(return_value={"error": "synthetic failure"})
    elif stage == "snapshot_error":
        api.mpv.snapshot = AsyncMock(return_value={"backend-error": True, "idle-active": True})
    elif stage == "snapshot_exception":
        api.mpv.snapshot = AsyncMock(side_effect=OSError("Control disconnected"))
    elif stage == "stop_timeout":
        api.mpv.stop = forever
    elif stage == "snapshot_timeout":
        api.mpv.snapshot = forever
    else:
        api.mpv.stop = AsyncMock(return_value={"error": "success"})
    started = asyncio.get_running_loop().time()
    with pytest.raises(BackendError, match="Playback cleanup did not confirm silence") as failure:
        await api._play_url(SOURCE)
    assert not isinstance(failure.value, SourcePlaybackError)
    assert asyncio.get_running_loop().time() - started < 0.5
    assert api.state.phase == PlaybackPhase.ERROR
    assert api.state.last_error == "Backend control failed; playback silence not confirmed"
    assert api.state.backend_url == api.state.backend_original_url == SOURCE
    assert api.state.gain_confirmed is False
    assert not api._backend_lock.locked()


@pytest.mark.asyncio
async def test_lost_stop_acknowledgement_is_not_replaced_by_idle_guess(api):
    async def stop():
        api.mpv.idle, api.mpv.url = True, ""
        raise BackendError("Stop applied, acknowledgement lost")

    api.mpv.stop = stop
    api.mpv.snapshot = AsyncMock(return_value={"idle-active": True})
    with pytest.raises(BackendError, match="cleanup did not confirm silence"):
        await api._play_url(SOURCE)
    api.mpv.snapshot.assert_not_awaited()
    assert api.state.backend_url == SOURCE
    assert "silence not confirmed" in api.state.last_error


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_resolver_failure_before_load_does_not_stop_preexisting_media(api, cancel):
    api._wait_for_mpv_audio_ready = AsyncMock(return_value=True)
    await api._play_url(NEXT)
    before_calls = list(api.mpv.calls)
    entered = asyncio.Event()

    async def resolve(url):
        entered.set()
        if cancel:
            await asyncio.Future()
        raise OSError("Synthetic resolver failure")

    api._resolve_playback_url = resolve
    task = asyncio.create_task(api._play_url(SOURCE))
    await entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else SourcePlaybackError):
        await task
    assert api.mpv.calls == before_calls
    assert api.mpv.url == NEXT and not api.mpv.idle
    assert api.state.backend_url == api.state.backend_original_url == NEXT
    assert api.state.current_url == SOURCE
    assert api.state.phase == PlaybackPhase.ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["load", "readiness"])
@pytest.mark.parametrize("failure", ["cancel", "backend_error"])
async def test_possibly_applied_load_is_cleaned_even_without_acknowledgement(api, stage, failure):
    entered = asyncio.Event()
    original_load = api.mpv.loadfile

    async def fail():
        entered.set()
        if failure == "cancel":
            await asyncio.Future()
        raise BackendError("Acknowledgement lost after apply")

    async def load(url, options=None, *, start_paused=False):
        await original_load(url, options, start_paused=start_paused)
        if stage == "load":
            await fail()

    api.mpv.loadfile = load
    if stage == "readiness":
        api._wait_for_mpv_audio_ready = lambda *_: fail()
    task = asyncio.create_task(api._play_url(SOURCE))
    await entered.wait()
    if failure == "cancel":
        task.cancel()
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else BackendError):
        await task
    assert calls(api, "stop") == [None]
    assert api.mpv.idle and not api.mpv.url
    assert api.state.backend_url == api.state.backend_original_url == ""
    assert api.state.phase == PlaybackPhase.ERROR
    assert not api._backend_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["play", "stop", "pause"])
async def test_old_cleanup_waiting_for_lock_cannot_stop_newer_owner(api, owner):
    entered, fail = asyncio.Event(), asyncio.Event()

    async def readiness(*_):
        entered.set()
        await fail.wait()
        return False

    api._wait_for_mpv_audio_ready = readiness
    old = asyncio.create_task(api._play_url(SOURCE))
    await entered.wait()
    await api._backend_lock.acquire()
    try:
        fail.set()
        await asyncio.sleep(0)  # Old cleanup is queued on the held lock.
        api._wait_for_mpv_audio_ready = AsyncMock(return_value=True)
        operation = api._play_url(NEXT) if owner == "play" else api._transport_command(owner)
        new = asyncio.create_task(operation)
        await asyncio.sleep(0)
    finally:
        api._backend_lock.release()
    with pytest.raises(SourcePlaybackError):
        await old
    await new
    if owner == "play":
        assert api.mpv.url == NEXT and api.state.phase == PlaybackPhase.PLAYING
        assert not calls(api, "stop")
    elif owner == "pause":
        assert api.mpv.url == SOURCE and api.mpv.paused
        assert api.state.phase == PlaybackPhase.PAUSED and not calls(api, "stop")
    else:
        assert api.mpv.idle and api.state.phase == PlaybackPhase.STOPPED
        assert calls(api, "stop") == [None]
    assert not api.state.last_error


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["play", "stop", "pause"])
async def test_issued_cleanup_records_actual_idle_without_overwriting_new_owner(api, owner):
    entered, release = asyncio.Event(), asyncio.Event()
    original_stop, original_load = api.mpv.stop, api.mpv.loadfile
    first = True
    owner_saw_idle_identity = []

    async def stop():
        nonlocal first
        if first:
            first = False
            entered.set()
            await release.wait()
        else:
            owner_saw_idle_identity.append((api.state.backend_url, api.state.backend_original_url))
        await original_stop()

    async def load(url, options=None, *, start_paused=False):
        if url == NEXT:
            owner_saw_idle_identity.append((api.state.backend_url, api.state.backend_original_url))
        await original_load(url, options, start_paused=start_paused)

    api.mpv.stop, api.mpv.loadfile = stop, load
    old = asyncio.create_task(api._play_url(SOURCE))
    await entered.wait()
    api._wait_for_mpv_audio_ready = AsyncMock(return_value=True)
    operation = api._play_url(NEXT) if owner == "play" else api._transport_command(owner)
    new = asyncio.create_task(operation)
    await asyncio.sleep(0)
    assert api.state.intent == owner
    assert api.state.phase in {PlaybackPhase.LOADING, PlaybackPhase.COMMAND_ACCEPTED}
    release.set()
    with pytest.raises(SourcePlaybackError):
        await old
    await new
    assert owner_saw_idle_identity == [("", "")]
    assert api.state.intent == owner and not api.state.last_error
    assert api.state.phase == {"play": PlaybackPhase.PLAYING, "pause": PlaybackPhase.PAUSED,
                               "stop": PlaybackPhase.STOPPED}[owner]
    assert calls(api, "stop") == [None] * (1 if owner == "play" else 2)
    assert api.state.backend_url == (NEXT if owner == "play" else "")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["queue", "item", "index", "url", "item_url", "intent", "backend_identity"])
async def test_cleanup_rechecks_captured_selection_and_actual_identity(api, change):
    entered, release = asyncio.Event(), asyncio.Event()

    async def readiness(*_):
        entered.set()
        await release.wait()
        return False

    api._wait_for_mpv_audio_ready = readiness
    task = asyncio.create_task(api._play_url(SOURCE))
    await entered.wait()
    if change == "queue":
        api.state.queue = PlaybackQueue(items=[QueueItem(SOURCE)], index=1)
    elif change == "item":
        api.state.queue.items[0] = QueueItem(SOURCE)
    elif change == "index":
        api.state.queue.items.append(QueueItem(NEXT))
        api.state.queue.index = 2
    elif change == "url":
        api.state.current_url = NEXT
    elif change == "item_url":
        api.state.queue.current.url = NEXT
    elif change == "intent":
        api.state.intent = "pause"
    else:
        api.state.backend_url = api.state.backend_original_url = NEXT
    before = asdict(api.state)
    release.set()
    with pytest.raises(SourcePlaybackError):
        await task
    assert not calls(api, "stop")
    if change != "backend_identity":
        assert asdict(api.state) == before
    assert api.state.backend_url == (NEXT if change == "backend_identity" else SOURCE)


async def start_station_retry(api):
    api._wait_for_mpv_audio_ready = AsyncMock(return_value=True)
    api._station_recovery_delays = (0, 0, 0)
    api.state.queue = PlaybackQueue(items=[QueueItem(SOURCE, stream_type="station", duration=0)], index=1)
    await api._play_url(SOURCE)
    recovery = api._station_recovery
    api.mpv.idle, api.mpv.url = True, ""
    api._wait_for_mpv_audio_ready = lambda *args: unconfirmed_native_audio(api, *args)
    await api.sync_mpv_state()
    return recovery, api._station_recovery_task


@pytest.mark.asyncio
async def test_each_unready_station_attempt_is_silenced_with_same_finite_budget(api):
    recovery, task = await start_station_retry(api)
    await task
    assert calls(api, "load") == [SOURCE] * 4
    assert calls(api, "stop") == [None] * 3
    assert api.mpv.idle
    assert api.state.backend_url == api.state.backend_original_url == ""
    assert api.state.phase == PlaybackPhase.ERROR
    assert api.state.last_error == "Station source unavailable: recovery exhausted"
    assert api._station_recovery is recovery and recovery.attempts == 3
    assert api._station_recovery_task is None and api._queue_advance_task is None
    for _ in range(3):
        await api.sync_mpv_state()
    assert calls(api, "load") == [SOURCE] * 4


@pytest.mark.asyncio
async def test_failed_station_cleanup_aborts_source_retries_as_backend_failure(api):
    recovery, task = await start_station_retry(api)
    api.mpv.stop = AsyncMock(side_effect=BackendError("Stop refused"))
    await task
    assert calls(api, "load") == [SOURCE] * 2
    assert recovery.attempts == 1 and recovery.invalidated
    assert api._station_recovery is None and api._station_recovery_task is None
    assert api.state.phase == PlaybackPhase.ERROR
    assert "silence not confirmed" in api.state.last_error
    assert api.state.backend_url == SOURCE and not api.mpv.idle


@pytest.mark.asyncio
async def test_two_new_owner_commands_do_not_cancel_issued_recovery_cleanup(api):
    entered, release = asyncio.Event(), asyncio.Event()
    original_stop = api.mpv.stop

    async def stop():
        if not entered.is_set():
            entered.set()
            await release.wait()
        await original_stop()

    recovery, task = await start_station_retry(api)
    api.mpv.stop = stop
    await entered.wait()
    pause = asyncio.create_task(api._transport_command("pause"))
    await asyncio.sleep(0)
    stop_task = asyncio.create_task(api._transport_command("stop"))
    await asyncio.sleep(0)
    assert not task.done() and api._station_recovery_loading
    release.set()
    await task
    with pytest.raises(BackendError, match="superseded"):
        await pause
    await stop_task
    assert recovery.attempts == 1 and recovery.invalidated
    assert api.state.phase == PlaybackPhase.STOPPED and api.state.intent == "stop"
    assert api.mpv.idle and api.state.backend_url == api.state.backend_original_url == ""
    assert api._station_recovery_task is None and not api._station_recovery_loading


@pytest.mark.asyncio
async def test_cleanup_deadline_includes_waiting_for_backend_lock(api, monkeypatch):
    timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda delay: timeout(min(delay, 0.04)))
    entered, fail = asyncio.Event(), asyncio.Event()

    async def readiness(*_):
        entered.set()
        await fail.wait()
        return False

    api._wait_for_mpv_audio_ready = readiness
    task = asyncio.create_task(api._play_url(SOURCE))
    await entered.wait()
    await api._backend_lock.acquire()
    try:
        fail.set()
        with pytest.raises(BackendError, match="cleanup did not confirm silence"):
            await task
    finally:
        api._backend_lock.release()
    assert not calls(api, "stop")
    assert api.state.backend_url == SOURCE and not api.mpv.idle
    assert "silence not confirmed" in api.state.last_error


@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", ["unready", "cancel"])
async def test_interrupted_cleanup_reports_uncertainty_without_detached_task(api, first_failure):
    waiting, cleaning = asyncio.Event(), asyncio.Event()
    baseline = asyncio.all_tasks()

    async def readiness(*_):
        waiting.set()
        if first_failure == "cancel":
            await asyncio.Future()
        return False

    async def stop():
        cleaning.set()
        await asyncio.Future()

    api._wait_for_mpv_audio_ready, api.mpv.stop = readiness, stop
    task = asyncio.create_task(api._play_url(SOURCE))
    await waiting.wait()
    if first_failure == "cancel":
        task.cancel()
    await cleaning.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert api.state.phase == PlaybackPhase.ERROR
    assert "silence not confirmed" in api.state.last_error
    assert api.state.backend_url == SOURCE and not api.mpv.idle
    assert not api._backend_lock.locked()
    assert not [pending for pending in asyncio.all_tasks() - baseline if not pending.done()]


@pytest.mark.asyncio
async def test_station_retry_can_succeed_after_confirmed_failed_attempt_cleanup(api):
    recovery, task = await start_station_retry(api)
    attempts = 0

    async def readiness(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return await unconfirmed_native_audio(api, *args)
        return True

    api._wait_for_mpv_audio_ready = readiness
    await task
    assert calls(api, "load") == [SOURCE] * 3
    assert calls(api, "stop") == [None]
    assert api.state.phase == PlaybackPhase.PLAYING and not api.state.last_error
    assert api.state.backend_url == api.state.backend_original_url == api.mpv.url == SOURCE
    assert api._station_recovery is recovery and recovery.attempts == 2
    # Only one attempt remains. A successful retry cannot replenish its budget.
    api.mpv.idle, api.mpv.url = True, ""
    api._wait_for_mpv_audio_ready = lambda *args: unconfirmed_native_audio(api, *args)
    await api.sync_mpv_state()
    await api._station_recovery_task
    assert recovery.attempts == 3
    assert calls(api, "load") == [SOURCE] * 4 and calls(api, "stop") == [None] * 2
    assert api.mpv.idle and api.state.phase == PlaybackPhase.ERROR
    assert api.state.last_error == "Station source unavailable: recovery exhausted"


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_backend_error_observer_during_recovery_readiness_still_cleans_owned_load(api, cleanup_fails):
    entered = asyncio.Event()

    async def readiness(*_):
        entered.set()
        await asyncio.Future()

    recovery, task = await start_station_retry(api)
    api._wait_for_mpv_audio_ready = readiness
    await entered.wait()
    generation = api.state.generation
    assert not api.mpv.idle and api.state.backend_url == SOURCE
    snapshot = api.mpv.snapshot
    observed = False

    async def transient_backend_error():
        nonlocal observed
        if not observed:
            observed = True
            return {"backend-error": True}
        return await snapshot()

    if cleanup_fails:
        async def stop():
            api.mpv.calls.append(("stop", None))
            raise BackendError("Stop acknowledgement unavailable")
        api.mpv.stop = stop
    api.mpv.snapshot = transient_backend_error
    await api.sync_mpv_state()
    assert recovery.invalidated and api._station_recovery is None
    assert api.state.generation == generation
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() and api._station_recovery_task is None
    assert calls(api, "load") == [SOURCE] * 2 and recovery.attempts == 1
    assert calls(api, "stop") == [None]
    assert api.state.phase == PlaybackPhase.ERROR
    if cleanup_fails:
        assert api.state.last_error == "Backend control failed; playback silence not confirmed"
        assert not api.mpv.idle and api.state.backend_url == SOURCE
    else:
        assert api.state.last_error == "Backend control failed"
        assert api.mpv.idle and api.state.backend_url == api.state.backend_original_url == ""
    assert not api._backend_lock.locked() and not api._station_recovery_loading
