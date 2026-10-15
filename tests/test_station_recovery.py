"""UNIT/MOCK station recovery: source retry never outranks owner transport intent."""
import asyncio
from dataclasses import asdict, replace
from unittest.mock import AsyncMock

import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi, ParsedCommand
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.queue import PlaybackQueue, QueueItem
from fake_linkplay.state import PlaybackPhase, RendererState
from test_playback_generations import StatefulBackend


STATION = "https://radio.example.test/live"
NEXT = "https://media.example.test/next.wav"


@pytest.fixture
async def api(tmp_path):
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path))
    result = HttpApi(cfg, RendererState.from_config(cfg), StatefulBackend())
    result._wait_for_mpv_audio_ready = AsyncMock(return_value=True)
    result._station_recovery_delays = (0, 0, 0)
    yield result
    result._invalidate_station_recovery()
    tasks = [task for task in (result._station_recovery_task, result._queue_advance_task) if task]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def play_station(api, *, stream_type="station", duration=0):
    queue = PlaybackQueue(name="Recovery fixture", items=[
        QueueItem(STATION, title="Synthetic station", source="PublicRadio", id="synthetic-station",
                  artwork="https://radio.example.test/art.png", metadata="Synthetic metadata",
                  stream_type=stream_type, duration=duration),
        QueueItem(NEXT, title="Next finite item", duration=30),
    ], index=1)
    await api._replace_queue(queue)
    await api._play_queue_index(1, queue.name)
    return queue


async def eof(api):
    api.mpv.idle = True
    await api.sync_mpv_state()
    return api._station_recovery_task


def loads(api):
    return [value for command, value in api.mpv.calls if command == "load"]


async def finish(task):
    async with asyncio.timeout(1):
        return await task


@pytest.mark.asyncio
async def test_station_reconnects_same_source_preserves_selection_and_owner_state(api, caplog):
    queue = await play_station(api)
    api.state.volume, api.state.mute = 23, True
    api.state.artist, api.state.album = "Synthetic artist", "Synthetic album"
    before = asdict(api.state)
    with caplog.at_level("INFO"):
        task = await eof(api)
        assert task is not None
        assert api.state.phase == PlaybackPhase.BUFFERING
        assert api.state.last_error == "Station source idle; retry pending"
        await finish(task)
    assert loads(api) == [STATION, STATION]
    assert api.state.phase == PlaybackPhase.PLAYING
    assert api.state.last_error == ""
    assert asdict(api.state) == before
    assert api.state.queue is queue
    assert api._queue_advance_task is None
    assert "category=unexpected_idle" in caplog.text
    assert "outcome=recovered" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("observation,category", [
    ({"eof-reached": True, "idle-active": False}, "source_eof"),
    ({"eof-reached": False, "idle-active": True}, "unexpected_idle"),
])
async def test_eof_and_unexpected_idle_are_distinct_recovery_categories(api, caplog, observation, category):
    await play_station(api)
    api.mpv.idle = True
    with caplog.at_level("INFO"):
        api._apply_backend_snapshot(observation)
        await finish(api._station_recovery_task)
    assert api._station_recovery.category == category
    assert f"category={category}" in caplog.text
    assert loads(api) == [STATION, STATION]


@pytest.mark.asyncio
async def test_source_unready_exhausts_exact_finite_budget_and_never_advances(api, caplog):
    await play_station(api)
    generation = api.state.generation
    async def unavailable(*args):
        api.mpv.idle = True
        return False
    api._wait_for_mpv_audio_ready = unavailable
    with caplog.at_level("INFO"):
        await finish(await eof(api))
    assert loads(api) == [STATION] * 4
    assert api.state.generation == generation
    assert api.state.phase == PlaybackPhase.ERROR
    assert api.state.last_error == "Station source unavailable: recovery exhausted"
    assert api.state.queue.index == 1
    assert api._queue_advance_task is None
    assert "outcome=exhausted" in caplog.text
    for _ in range(10):
        await api.sync_mpv_state()
    assert loads(api) == [STATION] * 4
    assert api._station_recovery_task is None


@pytest.mark.asyncio
async def test_duplicate_idle_preserves_failed_reload_diagnosis_and_budget(api):
    queue = await play_station(api)
    generation = api.state.generation
    failed = asyncio.Event()
    notify = api._schedule_event_notify
    def observe_failure(*args):
        if api.state.last_error == "Station source unavailable; retry pending":
            failed.set()
        notify(*args)
    api._schedule_event_notify = observe_failure
    api._station_recovery_delays = (0, 60, 60)
    async def unavailable(*args):
        api.mpv.idle = True
        return False
    api._wait_for_mpv_audio_ready = unavailable
    task = await eof(api)
    await asyncio.wait_for(failed.wait(), 1)
    for _ in range(3):
        await api.sync_mpv_state()
        assert api.state.last_error == "Station source unavailable; retry pending"
        assert api.state.phase == PlaybackPhase.BUFFERING
        assert api._station_recovery_task is task and not task.done()
        assert api._station_recovery.attempts == 1
        assert api.state.generation == generation and api.state.queue is queue
        assert api.state.queue.index == 1 and api.state.intent == "play"
        assert loads(api) == [STATION, STATION]
    await api._transport_command("stop")
    await asyncio.gather(task, return_exceptions=True)
    assert api.state.phase == PlaybackPhase.STOPPED and api.state.intent == "stop"
    assert api._station_recovery is None and loads(api) == [STATION, STATION]


@pytest.mark.asyncio
async def test_budget_is_not_replenished_by_short_success_or_repeated_eof(api):
    await play_station(api)
    generation = api.state.generation
    for attempt in range(1, 4):
        task = await eof(api)
        for _ in range(12):
            await api.sync_mpv_state()
            assert api._station_recovery_task is task
        await finish(task)
        assert api._station_recovery.attempts == attempt
        assert api.state.phase == PlaybackPhase.PLAYING
    assert await eof(api) is None
    assert api.state.phase == PlaybackPhase.ERROR
    assert api.state.generation == generation
    assert loads(api) == [STATION] * 4


@pytest.mark.asyncio
@pytest.mark.parametrize("poll_return", [False, True])
async def test_matching_audio_return_during_backoff_avoids_needless_reload(api, poll_return):
    await play_station(api)
    # Keep a previously spent retry, so native return cannot reset the budget.
    await finish(await eof(api))
    api._station_recovery_delays = (0.01, 0.01, 0.01)
    task = await eof(api)
    api.mpv.idle = False
    if poll_return:
        await api.sync_mpv_state()
        assert api.state.phase == PlaybackPhase.PLAYING
        assert api.state.backend_url == api.state.backend_original_url == STATION
        assert api.state.last_error == ""
    await finish(task)
    assert loads(api) == [STATION, STATION]
    assert api._station_recovery.attempts == 1
    assert api.state.phase == PlaybackPhase.PLAYING
    assert api.state.last_error == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("observed_path", ["loaded_proxy", "original", "unrelated"])
async def test_native_return_requires_actual_captured_backend_path(api, observed_path):
    proxy = "http://127.0.0.1/fixture-proxy"
    api._backend_playback_url = lambda *_: proxy
    await play_station(api)
    api._station_recovery_delays = (0.01, 0.01, 0.01)
    task = await eof(api)
    api.mpv.url = {"loaded_proxy": proxy, "original": STATION, "unrelated": NEXT}[observed_path]
    api.mpv.idle = False
    await api.sync_mpv_state()
    if observed_path != "loaded_proxy":
        assert api.state.phase == PlaybackPhase.BUFFERING
        assert api.state.backend_url == ""
    await finish(task)
    assert loads(api) == [proxy] * (1 if observed_path == "loaded_proxy" else 2)
    assert api.state.backend_url == proxy
    assert api.state.backend_original_url == STATION
    assert api.state.phase == PlaybackPhase.PLAYING


@pytest.mark.asyncio
@pytest.mark.parametrize("during_retry", [False, True])
@pytest.mark.parametrize("failure", [BackendError, OSError, TimeoutError])
async def test_resolution_failure_is_never_mislabeled_as_backend_control(api, caplog, during_retry, failure):
    if during_retry:
        await play_station(api)
        api.state.queue.current.resolved_url = ""
    api._resolve_playback_url = AsyncMock(side_effect=failure("synthetic resolver failure"))
    with caplog.at_level("INFO"):
        if during_retry:
            await finish(await eof(api))
            assert api.state.last_error == "Station source unavailable: recovery exhausted"
            assert api._station_recovery.attempts == 3
            assert loads(api) == [STATION]
        else:
            with pytest.raises(BackendError, match="Source resolution failed"):
                await play_station(api)
            assert api.state.last_error == "Source resolution failed"
            assert not loads(api)
    assert api.state.phase == PlaybackPhase.ERROR
    assert "category=source_resolution" in caplog.text
    assert "category=backend_control" not in caplog.text


@pytest.mark.asyncio
async def test_third_attempt_is_allowed_to_finish_despite_duplicate_idle(api):
    await play_station(api)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def readiness(*args):
        nonlocal calls
        calls += 1
        if calls < 3:
            api.mpv.idle = True
            return False
        entered.set()
        await release.wait()
        return True

    api._wait_for_mpv_audio_ready = readiness
    task = await eof(api)
    await entered.wait()
    for _ in range(8):
        api._apply_backend_snapshot({"idle-active": True})
    assert api.state.phase == PlaybackPhase.BUFFERING
    assert not task.done()
    # The third attempt can still become ready after those idle observations.
    release.set()
    await finish(task)
    assert api.state.phase == PlaybackPhase.PLAYING
    assert loads(api) == [STATION] * 4


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["pause", "stop"])
async def test_owner_command_during_backoff_cancels_retry(api, command):
    await play_station(api)
    api._station_recovery_delays = (60, 60, 60)
    task = await eof(api)
    await asyncio.sleep(0)
    await api.execute(ParsedCommand(command), "192.0.2.1")
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert loads(api) == [STATION]
    assert api.state.intent == command
    assert api._station_recovery is None
    for _ in range(3):
        await api.sync_mpv_state()
    assert loads(api) == [STATION]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["pause", "stop"])
async def test_owner_command_during_load_waits_for_identity_then_wins(api, command):
    await play_station(api)
    api.mpv.load_started.clear()
    api.mpv.release_load = asyncio.Event()
    task = await eof(api)
    await api.mpv.load_started.wait()
    owner = asyncio.create_task(api.execute(ParsedCommand(command), "192.0.2.1"))
    await asyncio.sleep(0)
    assert not task.done()
    assert not owner.done()
    assert api.state.intent == command
    api.mpv.release_load.set()
    await finish(task)
    await finish(owner)
    assert loads(api) == [STATION, STATION]
    assert api.state.phase == (PlaybackPhase.PAUSED if command == "pause" else PlaybackPhase.STOPPED)
    if command == "pause":
        assert api.state.backend_url == api.mpv.url == STATION
        assert api.state.backend_original_url == STATION
        assert api.mpv.paused
    else:
        assert api.mpv.idle
        assert api.state.backend_url == ""


@pytest.mark.asyncio
async def test_two_newer_owner_commands_do_not_cancel_locked_recovery_load(api):
    await play_station(api)
    api.mpv.load_started.clear()
    api.mpv.release_load = asyncio.Event()
    task = await eof(api)
    await api.mpv.load_started.wait()
    pause = asyncio.create_task(api.execute(ParsedCommand("pause"), "192.0.2.1"))
    await asyncio.sleep(0)
    stop = asyncio.create_task(api.execute(ParsedCommand("stop"), "192.0.2.1"))
    await asyncio.sleep(0)
    assert not task.done()
    api.mpv.release_load.set()
    await finish(task)
    with pytest.raises(BackendError):
        await finish(pause)
    await finish(stop)
    assert api.mpv.idle
    assert api.state.phase == PlaybackPhase.STOPPED


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["pause", "stop"])
async def test_owner_command_during_readiness_cancels_without_late_restart(api, command):
    await play_station(api)
    entered = asyncio.Event()

    async def readiness(*args):
        entered.set()
        await asyncio.Future()

    api._wait_for_mpv_audio_ready = readiness
    task = await eof(api)
    await entered.wait()
    await api.execute(ParsedCommand(command), "192.0.2.1")
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert loads(api) == [STATION, STATION]
    assert api.state.phase == (PlaybackPhase.PAUSED if command == "pause" else PlaybackPhase.STOPPED)


@pytest.mark.asyncio
async def test_source_replacement_during_recovery_resolution_cancels_old_work(api):
    await play_station(api)
    api.state.queue.current.resolved_url = ""
    entered = asyncio.Event()

    async def resolve(url):
        if url == STATION:
            entered.set()
            await asyncio.Future()
        return url

    api._resolve_playback_url = resolve
    task = await eof(api)
    await entered.wait()
    await api.execute(ParsedCommand("play", (NEXT,)), "192.0.2.1")
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert loads(api) == [STATION, NEXT]
    assert api.state.current_url == api.mpv.url == NEXT
    assert api.state.phase == PlaybackPhase.PLAYING


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["source", "queue", "same_url_queue", "queue_index"])
async def test_replacement_invalidates_backoff_even_if_url_is_the_same(api, operation):
    queue = await play_station(api)
    api._station_recovery_delays = (60, 60, 60)
    task = await eof(api)
    await asyncio.sleep(0)
    if operation == "source":
        await api.execute(ParsedCommand("play", (NEXT,)), "192.0.2.1")
    elif operation == "queue_index":
        await api._play_queue_index(2, queue.name)
    else:
        replacement = PlaybackQueue(name="Replacement", items=[
            QueueItem(STATION if operation == "same_url_queue" else NEXT, stream_type="station")
        ], index=1)
        await api._replace_queue(replacement)
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert loads(api) == ([STATION, NEXT] if operation in {"source", "queue_index"} else [STATION])
    assert api._station_recovery is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["load", "readiness"])
@pytest.mark.parametrize("replacement", ["source", "same_url_queue", "queue_index"])
async def test_replacement_wins_over_active_recovery_load_or_readiness(api, stage, replacement):
    queue = await play_station(api)
    entered = asyncio.Event()
    original_readiness = api._wait_for_mpv_audio_ready
    if stage == "load":
        api.mpv.load_started.clear()
        api.mpv.release_load = asyncio.Event()
    else:
        async def readiness(*args):
            entered.set()
            await asyncio.Future()
        api._wait_for_mpv_audio_ready = readiness
    task = await eof(api)
    await (api.mpv.load_started.wait() if stage == "load" else entered.wait())
    api._wait_for_mpv_audio_ready = original_readiness
    if replacement == "source":
        operation = api.execute(ParsedCommand("play", (NEXT,)), "192.0.2.1")
    elif replacement == "queue_index":
        operation = api._play_queue_index(2, queue.name)
    else:
        new_queue = PlaybackQueue(name="Same source, new owner", items=[QueueItem(STATION, stream_type="station")], index=1)
        operation = api._replace_queue(new_queue)
    owner = asyncio.create_task(operation)
    await asyncio.sleep(0)
    if stage == "load":
        assert not task.done()
        api.mpv.release_load.set()
    await finish(owner)
    await asyncio.gather(task, return_exceptions=True)
    assert loads(api) == [STATION, STATION] + ([] if replacement == "same_url_queue" else [NEXT])
    if replacement == "same_url_queue":
        assert api.state.queue is new_queue
        assert api.state.phase == PlaybackPhase.STOPPED
        assert api.mpv.idle
    else:
        assert api.state.current_url == api.mpv.url == NEXT
        assert api.state.phase == PlaybackPhase.PLAYING
    assert api._station_recovery is None


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_change", ["queue", "item", "index", "url", "stream_type", "duration"])
async def test_captured_identity_is_rechecked_before_retry(api, identity_change):
    queue = await play_station(api)
    api._station_recovery_delays = (0.01, 0.01, 0.01)
    task = await eof(api)
    if identity_change == "queue":
        api.state.queue = PlaybackQueue(name=queue.name, items=list(queue.items), index=1)
    elif identity_change == "item":
        queue.items[0] = replace(queue.items[0])
    elif identity_change == "index":
        queue.index = 2
    elif identity_change == "url":
        queue.current.url = NEXT
    elif identity_change == "stream_type":
        queue.current.stream_type = "track"
    else:
        queue.current.duration = 30
    await finish(task)
    assert loads(api) == [STATION]


@pytest.mark.asyncio
async def test_failed_stop_keeps_owner_fence_and_does_not_rearm_retry(api):
    await play_station(api)
    api._station_recovery_delays = (60, 60, 60)
    task = await eof(api)
    # A later active observation may precede an owner command while retry sleeps.
    api.state.phase = PlaybackPhase.PLAYING
    api.mpv.stop = AsyncMock(side_effect=BackendError("synthetic stop failure"))
    with pytest.raises(BackendError):
        await api.execute(ParsedCommand("stop"), "192.0.2.1")
    await asyncio.gather(task, return_exceptions=True)
    assert api.state.intent == "stop"
    assert api.state.phase == PlaybackPhase.ERROR
    assert "silence not confirmed" in api.state.last_error
    for _ in range(8):
        await api.sync_mpv_state()
    assert loads(api) == [STATION]
    assert api._station_recovery is None
    assert api._queue_advance_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_type,duration", [("track", 0), ("", 0), ("track", 30), ("station", 30)])
async def test_unknown_or_finite_media_eof_keeps_normal_queue_advance(api, stream_type, duration):
    await play_station(api, stream_type=stream_type, duration=duration)
    # This legacy default must never make a finite/unknown item recoverable.
    api.queue_is_live = True
    assert await eof(api) is None
    await finish(api._queue_advance_task)
    assert loads(api) == [STATION, NEXT]
    assert api.state.queue.index == 2
    assert api.state.phase == PlaybackPhase.PLAYING


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["snapshot", "snapshot_exception", "load", "readiness"])
async def test_backend_control_failure_is_terminal_without_source_retries(api, failure, caplog):
    await play_station(api)
    if failure == "snapshot":
        api.mpv.fail = True
    elif failure == "snapshot_exception":
        api.mpv.snapshot = AsyncMock(side_effect=OSError("synthetic IPC disconnect"))
    elif failure == "load":
        api.mpv.loadfile = AsyncMock(side_effect=BackendError("synthetic load refusal"))
    else:
        api._wait_for_mpv_audio_ready = AsyncMock(side_effect=BackendError("synthetic IPC timeout"))
    with caplog.at_level("INFO"):
        task = await eof(api)
        if task:
            await finish(task)
    assert api.state.phase == PlaybackPhase.ERROR
    assert api.state.last_error == "Backend control failed"
    assert api._station_recovery is None
    assert api._queue_advance_task is None
    assert "category=backend_control" in caplog.text
    assert "outcome=exhausted" not in caplog.text
    assert len(loads(api)) == (2 if failure == "readiness" else 1)
    if failure == "load":
        assert api.mpv.loadfile.await_count == 1


@pytest.mark.asyncio
async def test_real_readiness_helper_distinguishes_ipc_failure_from_source_timeout(api):
    api.mpv.snapshot = AsyncMock(return_value={"backend-error": True})
    with pytest.raises(BackendError, match="control failed"):
        await HttpApi._wait_for_mpv_audio_ready(api, api.state.generation, STATION, timeout=0.1)


@pytest.mark.asyncio
async def test_backend_error_observed_during_backoff_cancels_pending_retry(api):
    await play_station(api)
    api._station_recovery_delays = (60, 60, 60)
    task = await eof(api)
    await asyncio.sleep(0)
    api.mpv.fail = True
    await api.sync_mpv_state()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert api.state.last_error == "Backend control failed"
    assert loads(api) == [STATION]


@pytest.mark.asyncio
async def test_buffering_and_owner_pause_never_schedule_source_reload(api):
    await play_station(api)
    api.mpv.buffering = True
    for _ in range(12):
        await api.sync_mpv_state()
    assert api.state.phase == PlaybackPhase.BUFFERING
    assert api._station_recovery_task is None
    api.mpv.buffering = False
    await api.sync_mpv_state()
    await api.execute(ParsedCommand("pause"), "192.0.2.1")
    api.mpv.buffering = True
    for _ in range(12):
        await api.sync_mpv_state()
    assert api.state.phase == PlaybackPhase.PAUSED
    assert loads(api) == [STATION]
    assert api._station_recovery_task is None


@pytest.mark.asyncio
async def test_explicit_owner_resume_gets_new_retry_budget(api):
    await play_station(api)
    for _ in range(3):
        await finish(await eof(api))
    await eof(api)
    assert api.state.phase == PlaybackPhase.ERROR
    await api.execute(ParsedCommand("resume"), "192.0.2.1")
    assert api._station_recovery.attempts == 0
    await finish(await eof(api))
    assert api.state.phase == PlaybackPhase.PLAYING
    assert api._station_recovery.attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["backoff", "load", "readiness"])
async def test_last_listener_shutdown_invalidates_and_drains_recovery(api, stage):
    api._monitor_interval = 60
    first, second = api._state_monitor_context(None), api._state_monitor_context(None)
    await anext(first)
    await anext(second)
    await play_station(api)
    entered = asyncio.Event()
    if stage == "backoff":
        api._station_recovery_delays = (60, 60, 60)
    elif stage == "load":
        api.mpv.load_started.clear()
        api.mpv.release_load = asyncio.Event()
    else:
        async def readiness(*args):
            entered.set()
            await asyncio.Future()
        api._wait_for_mpv_audio_ready = readiness
    task = await eof(api)
    if stage == "load":
        await api.mpv.load_started.wait()
    elif stage == "readiness":
        await entered.wait()
    else:
        await asyncio.sleep(0)
    await first.aclose()
    assert api._station_recovery_task is task and not task.done()
    closing = asyncio.create_task(second.aclose())
    await asyncio.sleep(0)
    assert api._station_recovery is None
    if stage == "load":
        assert not task.done()
        api.mpv.release_load.set()
    await finish(closing)
    assert task.done()
    assert api._station_recovery_task is None
    assert api._state_monitor_task is None
    assert loads(api) == [STATION] * (1 if stage == "backoff" else 2)
    assert api.mpv.idle
    assert api.state.backend_url == api.state.backend_original_url == ""
    assert [command for command, _ in api.mpv.calls if command == "stop"] == (
        [] if stage == "backoff" else ["stop"])
