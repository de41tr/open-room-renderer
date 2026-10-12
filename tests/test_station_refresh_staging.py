"""UNIT/MOCK intersection of typed station refresh and native queue ownership."""
import asyncio
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

from fake_linkplay.http_api import ParsedCommand
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.queue import PlaybackQueue, QueueItem, parse_queue_context
from fake_linkplay.radio import RadioResolution
from fake_linkplay.state import PlaybackPhase
from test_native_queue_staging import A, B, C, make_api, mutations, start
from test_station_recovery import eof, finish
from test_station_url_refresh import FIRST, FRESH, REFERENCE


@pytest.fixture
async def api(tmp_path):
    result = make_api(tmp_path, urls=(REFERENCE, A, B), durations=(0, 30, 30))
    result._station_recovery_delays = (0, 0, 0)
    result._resolve_station_id = AsyncMock(return_value=RadioResolution(FIRST, "s12345"))
    yield result
    tasks = [task for task in (result._station_recovery_task, result._queue_advance_task)
             if task is not None]
    tasks.extend(result._native_cleanup_tasks)
    result._invalidate_station_recovery()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("selection", ["reference", "header"])
async def test_typed_live_current_never_stages_finite_successor_and_refresh_retains_identity(api, selection):
    if selection == "header":
        queue = PlaybackQueue(name="Synthetic station", source="newTuneIn", stream_id="s12345")
        await api._materialize_radio_queue(queue)
        queue.items.extend([QueueItem(A, duration=30), QueueItem(B, duration=30)])
        await api._play_queue_index(1, queue.name)
    else:
        await start(api)
    selected, generation = api.state.queue.current, api.state.generation
    canonical = FIRST if selection == "header" else REFERENCE
    assert selected.stream_type == "station" and selected.duration == 0
    assert selected._radio_station == (canonical, "s12345")
    assert api.mpv.duration == 30  # A finite native hint cannot override resolver provenance.
    assert api._native_stage is None and mutations(api, "append") == []
    assert api._resolve_station_id.await_count == 1 and selected._radio_resolution_attempted
    api._resolve_station_id.return_value = RadioResolution(FRESH, "s12345")
    await finish(await eof(api))
    assert mutations(api, "load") == [FIRST, FRESH]
    assert api._resolve_station_id.await_count == 2
    assert api.state.generation == generation and api.state.queue.current is selected
    assert selected.url == api.state.current_url == api.state.backend_original_url == canonical
    assert selected.resolved_url == api.state.resolved_url == api.state.backend_url == FRESH
    assert parse_queue_context(api.queue_context).current.url == FRESH
    assert api._station_recovery.backend_url == FRESH and api._station_recovery.attempts == 1
    assert api.state.totlen == 0 and api._native_stage is None and not mutations(api, "append")
    # Moving explicitly to a finite item still enables ordinary one-item lookahead.
    await api._play_queue_index(2, api.state.queue.name)
    generation = api.state.generation
    assert api._station_recovery is None and mutations(api, "append") == [B]
    api.mpv.advance()
    await api.sync_mpv_state()
    assert api.state.queue.index == 3 and api.state.generation == generation
    assert api.state.current_url == api.state.backend_original_url == api.state.backend_url == B
    assert mutations(api, "load") == [FIRST, FRESH, A]


async def test_typed_station_successor_is_not_unwrapped_into_a_finite_native_append(api):
    api.state.queue = PlaybackQueue(items=[QueueItem(A, duration=30), QueueItem(B)], index=1)
    api._activate_queue_item()
    async def resolve(url):
        return RadioResolution(FIRST, "s12345") if url == B else url
    api._resolve_playback_url = resolve
    await start(api)
    assert mutations(api, "load") == [A] and not mutations(api, "append")
    assert api._native_stage is None and api.state.queue.index == 1
    # Ordinary loading unwraps the same typed result and owns its provenance.
    await api._play_queue_index(2, api.state.queue.name)
    assert mutations(api, "load") == [A, FIRST] and api.state.current_url == B
    assert api.state.backend_original_url == B and api.state.backend_url == FIRST
    assert api.state.queue.current._radio_station == (B, "s12345")
    assert api._station_recovery is not None


async def test_finite_successor_resolution_keeps_canonical_and_actual_native_identity(api):
    api.state.queue = PlaybackQueue(items=[QueueItem(A, duration=30), QueueItem(B, duration=30)], index=1)
    api._activate_queue_item()
    async def resolve(url):
        return C if url == B else url
    api._resolve_playback_url = resolve
    await start(api)
    generation = api.state.generation
    assert mutations(api, "append") == [C]
    api.mpv.advance()
    await api.sync_mpv_state()
    assert api.state.queue.index == 2 and api.state.generation == generation
    assert api.state.current_url == api.state.queue.current.url == api.state.backend_original_url == B
    assert api.state.backend_url == api.state.resolved_url == api.state.queue.current.resolved_url == C
    assert api.state.queue.current._radio_station is None and api._station_recovery is None
    assert mutations(api, "load") == [A]


@pytest.mark.parametrize("wait_at", ["resolver", "load"])
@pytest.mark.parametrize("owner", ["pause", "stop", "newsource", "gain"])
async def test_owner_or_gain_failure_during_typed_refresh_prevents_stale_staging(api, wait_at, owner):
    await start(api)
    generation = api.state.generation
    entered, release = asyncio.Event(), asyncio.Event()
    if wait_at == "resolver":
        async def resolve(station_id):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return RadioResolution(FRESH, station_id)
        api._resolve_station_id.side_effect = resolve
    else:
        api._resolve_station_id.return_value = RadioResolution(FRESH, "s12345")
        api.mpv.load_started = entered
        api.mpv.release_load = release
    recovery = await eof(api)
    await asyncio.wait_for(entered.wait(), 1)
    if owner == "gain":
        api.mpv.volume = AsyncMock(side_effect=BackendError("Synthetic gain failure"))
        with pytest.raises(BackendError):
            await api._set_confirmed_gain(volume=15)
        changing = None
    else:
        parsed = ParsedCommand("play", (C,)) if owner == "newsource" else ParsedCommand(owner)
        changing = asyncio.create_task(api.execute(parsed, "192.0.2.1"))
        await asyncio.sleep(0)
    release.set()
    await asyncio.gather(recovery, return_exceptions=True)
    if changing is not None:
        await finish(changing)
    assert api._station_recovery is None and api._native_stage is None
    assert not mutations(api, "append")
    assert (FRESH in mutations(api, "load")) is (wait_at == "load")
    if owner == "newsource":
        assert api.state.current_url == api.state.backend_original_url == api.state.backend_url == C
        assert api.state.phase == PlaybackPhase.PLAYING and api.mpv.url == C
    elif owner == "stop":
        assert api.state.phase == PlaybackPhase.STOPPED and api.mpv.idle
        assert not api.state.backend_url
    elif owner == "pause":
        assert api.state.phase == PlaybackPhase.PAUSED
        if wait_at == "load":
            assert api.mpv.paused
            assert api.state.backend_original_url == REFERENCE and api.state.backend_url == FRESH
        else:
            assert api.mpv.idle and not api.state.backend_url
    else:
        assert api.state.generation == generation and api.state.phase == PlaybackPhase.ERROR
        assert "Gain confirmation failed" in api.state.last_error
        if wait_at == "load":
            assert api.mpv.idle and not api.state.backend_url
    if owner != "gain":
        assert api.state.generation > generation
    before, calls = asdict(api.state), list(api.mpv.calls)
    await asyncio.sleep(0)
    assert asdict(api.state) == before and api.mpv.calls == calls


@pytest.mark.parametrize("wait_at", ["resolver", "append"])
@pytest.mark.parametrize("owner", ["pause", "stop", "newsource", "gain"])
async def test_owner_or_gain_failure_after_station_to_finite_selection_preserves_staging_fence(api, wait_at, owner):
    await start(api)
    assert api._station_recovery is not None
    entered, release = asyncio.Event(), asyncio.Event()
    if wait_at == "resolver":
        async def resolve(url):
            if url == B:
                entered.set()
                await release.wait()
            return url
        api._resolve_playback_url = resolve
    else:
        api.mpv.append_started = entered
        api.mpv.release_append = release
    loading = asyncio.create_task(api._play_queue_index(2, api.state.queue.name))
    await asyncio.wait_for(entered.wait(), 1)
    assert api._station_recovery is None
    if owner == "gain":
        api.mpv.volume = AsyncMock(side_effect=BackendError("Synthetic gain failure"))
        with pytest.raises(BackendError):
            await api._set_confirmed_gain(volume=15)
        changing = None
    else:
        parsed = ParsedCommand("play", (C,)) if owner == "newsource" else ParsedCommand(owner)
        changing = asyncio.create_task(api.execute(parsed, "192.0.2.1"))
        await asyncio.sleep(0)
    if wait_at == "append":
        api.mpv.advance()
    release.set()
    with pytest.raises(BackendError):
        await loading
    if changing is not None:
        await finish(changing)
    await asyncio.gather(*tuple(api._native_cleanup_tasks))
    assert api._station_recovery is None
    if owner == "newsource":
        assert api.state.current_url == api.state.backend_original_url == api.state.backend_url == C
        assert api.mpv.url == C and api.state.phase == PlaybackPhase.PLAYING
    elif owner == "pause":
        assert api.state.phase == PlaybackPhase.PAUSED and api.mpv.paused
        expected = B if wait_at == "append" else A
        assert api.state.current_url == api.state.backend_url == expected
    elif owner == "stop":
        assert api.state.phase == PlaybackPhase.STOPPED and api.mpv.idle
    else:
        assert api.state.phase == PlaybackPhase.ERROR and "Gain confirmation failed" in api.state.last_error
        if wait_at == "append":
            assert api.mpv.idle and not api.mpv.entries
    if wait_at == "resolver":
        assert not mutations(api, "append")
