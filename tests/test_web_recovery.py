"""Read-only Web recovery projection; UNIT/MOCK, no native IPC or browser QA."""
import asyncio
from copy import deepcopy
import json
from unittest.mock import AsyncMock

import pytest

from fake_linkplay.state import PlaybackPhase
from fake_linkplay.web_ui import WebCommand
from test_station_recovery import api, eof, finish, loads, play_station, STATION, NEXT


PENDING_ERRORS = (
    "Station source ended; retry pending", "Station source stalled; retry pending",
    "Station source idle; retry pending", "Station source unavailable; retry pending",
)


def snapshot_without_work(api):
    """Snapshot reads cannot drive, reset or acknowledge engine recovery."""
    calls = deepcopy(api.mpv.calls)
    state = deepcopy(api.state)
    recovery, task = api._station_recovery, api._station_recovery_task
    attempts = recovery.attempts if recovery else None
    snapshot = api.web_api.snapshot()
    assert api.mpv.calls == calls and api.state == state
    assert api._station_recovery is recovery and api._station_recovery_task is task
    assert (recovery.attempts if recovery else None) == attempts
    return snapshot


@pytest.mark.parametrize("reason", PENDING_ERRORS)
@pytest.mark.parametrize("phase", [PlaybackPhase.LOADING, PlaybackPhase.BUFFERING])
@pytest.mark.parametrize("attempts", [0, 1, 2, 3])
async def test_pending_recovery_is_current_allowlisted_and_read_only(api, reason, phase, attempts):
    await play_station(api)
    await eof(api)
    api.state.phase, api.state.last_error = phase, reason
    api._station_recovery.attempts = attempts
    view = snapshot_without_work(api)
    recovery = view["transport"]["recovery"]
    assert view["transport"]["status"] == "PENDING"
    assert recovery["kind"] == "station_retry"
    assert recovery["attempts_used"] == attempts and recovery["attempt_limit"] == 3
    assert f"{attempts} of 3 automatic retry attempts used" in recovery["message"]
    assert loads(api) == [STATION]


async def test_actual_pending_attempt_success_hides_spent_counts(api):
    await play_station(api)
    entered, release = asyncio.Event(), asyncio.Event()

    async def readiness(*_):
        entered.set()
        await release.wait()
        return True

    api._wait_for_mpv_audio_ready = readiness
    task = await eof(api)
    assert snapshot_without_work(api)["transport"]["recovery"]["attempts_used"] == 0
    await entered.wait()
    assert snapshot_without_work(api)["transport"]["recovery"]["attempts_used"] == 1
    release.set()
    await finish(task)
    assert api._station_recovery.attempts == 1
    assert loads(api) == [STATION, STATION]
    assert snapshot_without_work(api)["transport"]["recovery"] is None
    # Generic later buffering is not another automatic recovery attempt.
    api.mpv.buffering = True
    await api.sync_mpv_state()
    assert snapshot_without_work(api)["transport"]["recovery"] is None


async def test_exhausted_budget_keeps_engine_and_explicit_play_policy(api):
    await play_station(api)
    generation = api.state.generation

    async def unavailable(*_):
        api.mpv.idle = True
        return False

    api._wait_for_mpv_audio_ready = unavailable
    await finish(await eof(api))
    assert loads(api) == [STATION] * 4
    for _ in range(4):
        view = snapshot_without_work(api)
        assert view["transport"]["status"] == "ERROR"
        recovery = view["transport"]["recovery"]
        assert set(recovery) == {"kind", "message"}
        assert recovery["kind"] == "station_exhausted"
        assert "Choose Play" in recovery["message"] and "another queue item" in recovery["message"]
        assert view["actions"]["play"] and view["actions"]["stop"] and view["actions"]["queue_select"]
        assert not view["actions"]["next"] and not view["actions"]["pause"]
    assert api.state.generation == generation and api.state.queue.index == 1
    assert api._queue_advance_task is None and loads(api) == [STATION] * 4
    api._wait_for_mpv_audio_ready = AsyncMock(return_value=True)
    await api._web_dispatch(WebCommand(action="play", expected_generation=generation))
    assert loads(api) == [STATION] * 5
    assert api._station_recovery.attempts == 0
    assert snapshot_without_work(api)["transport"]["recovery"] is None


@pytest.mark.parametrize("reason", ["Backend control failed",
    "Backend control failed; playback silence not confirmed"])
async def test_backend_control_failure_is_distinct_without_recovery_or_silence_claim(api, reason):
    await play_station(api)
    api._mark_backend_failure()
    api.state.last_error = reason
    view = snapshot_without_work(api)
    assert api._station_recovery is None
    assert view["transport"]["status"] == "ERROR"
    assert view["transport"]["recovery"]["kind"] == "backend_control"
    assert "silence are unconfirmed" in view["transport"]["recovery"]["message"]
    assert "attempts_used" not in view["transport"]["recovery"]
    assert view["actions"]["stop"] and not view["actions"]["pause"]
    assert loads(api) == [STATION]


@pytest.mark.parametrize("change", ["generation", "queue", "item", "index", "url", "intent", "invalidated", "task_done", "uncertain"])
async def test_old_or_uncertain_recovery_progress_is_never_projected(api, change):
    await play_station(api)
    task = await eof(api)
    if change == "generation":
        api.state.generation += 1
    elif change == "queue":
        api.state.queue = deepcopy(api.state.queue)
    elif change == "item":
        api.state.queue.items[0] = deepcopy(api.state.queue.items[0])
    elif change == "index":
        api.state.queue.index = 2
    elif change == "url":
        api.state.current_url = NEXT
    elif change == "intent":
        api.state.intent = "stop"
    elif change == "invalidated":
        api._station_recovery.invalidated = True
    elif change == "task_done":
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    else:
        api._native_output_uncertain = True
    assert snapshot_without_work(api)["transport"]["recovery"] is None


@pytest.mark.parametrize("action", ["queue_select", "pause", "stop"])
async def test_explicit_new_owner_removes_recovery_without_old_retry(api, action):
    queue = await play_station(api)
    api._station_recovery_delays = (60, 60, 60)
    task = await eof(api)
    await asyncio.sleep(0)
    assert snapshot_without_work(api)["transport"]["recovery"]["kind"] == "station_retry"
    await api._web_dispatch(WebCommand(action=action, queue_name=queue.name, index=2,
                                      expected_generation=api.state.generation))
    await asyncio.gather(task, return_exceptions=True)
    assert snapshot_without_work(api)["transport"]["recovery"] is None
    assert loads(api) == ([STATION, NEXT] if action == "queue_select" else [STATION])
    assert api.state.queue.index == (2 if action == "queue_select" else 1)


@pytest.mark.parametrize("phase", [PlaybackPhase.ERROR, PlaybackPhase.BUFFERING])
@pytest.mark.parametrize("reason", [
    "Backend control failed: /private/PRIVATE_ERROR",
    "Station source unavailable: recovery exhausted https://private.example.test/?token=PRIVATE_ERROR",
    "Station source unavailable; retry pending PRIVATE_ERROR",
    "https://private.example.test/error?token=PRIVATE_ERROR", "Unrecognized PRIVATE_ERROR",
])
async def test_unknown_or_lookalike_errors_never_serialize_private_cause(api, phase, reason):
    await play_station(api)
    await eof(api)
    api.state.phase, api.state.last_error = phase, reason
    api._station_recovery.url = api.state.current_url = api.state.queue.current.url = (
        "https://private.example.test/source?token=PRIVATE_SOURCE")
    api._station_recovery.backend_url = "file:///private/PRIVATE_BACKEND"
    api._station_recovery.category = "PRIVATE_CATEGORY"
    view = snapshot_without_work(api)
    assert view["transport"]["recovery"] is None
    assert "PRIVATE_" not in json.dumps(view)


async def test_exhausted_explanation_requires_current_selection_and_exact_budget(api):
    await play_station(api)
    api.state.mark_error("Station source unavailable: recovery exhausted")
    api._station_recovery.attempts = 2
    assert snapshot_without_work(api)["transport"]["recovery"] is None
    api._station_recovery.attempts = 3
    assert snapshot_without_work(api)["transport"]["recovery"]["kind"] == "station_exhausted"
    api.state.generation += 1
    assert snapshot_without_work(api)["transport"]["recovery"] is None


@pytest.mark.parametrize("phase,reason", [
    (PlaybackPhase.BUFFERING, "Station source idle; retry pending"),
    (PlaybackPhase.ERROR, "Station source unavailable: recovery exhausted"),
    (PlaybackPhase.ERROR, "Backend control failed; playback silence not confirmed"),
])
async def test_allowlisted_view_never_contains_recovery_sources_or_private_category(api, phase, reason):
    await play_station(api)
    await eof(api)
    api.state.phase, api.state.last_error = phase, reason
    recovery = api._station_recovery
    recovery.attempts = 3
    recovery.url = api.state.current_url = api.state.queue.current.url = (
        "https://private.example.test/source?token=PRIVATE_SOURCE")
    recovery.backend_url = "file:///private/PRIVATE_BACKEND"
    recovery.category = "PRIVATE_CATEGORY"
    view = snapshot_without_work(api)
    assert view["transport"]["recovery"] is not None
    assert "PRIVATE_" not in json.dumps(view)
