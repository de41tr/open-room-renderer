"""UNIT/MOCK repeat modes never reset or bypass the station recovery budget."""
import pytest

from fake_linkplay.state import PlaybackPhase
from test_repeat_queue_playback import mode
from test_station_recovery import api, play_station, eof, finish, loads, STATION


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", [0, 1])
async def test_mode_changes_during_station_backoff_keep_exact_finite_budget(api, initial):
    await play_station(api)
    await mode(api, initial)
    recovery, generation = api._station_recovery, api.state.generation
    for attempt in range(1, 4):
        task = await eof(api)
        assert task is not None
        await mode(api, (initial + attempt) % 2)
        assert api._station_recovery is recovery and api._station_recovery_task is task
        assert api.state.generation == generation and not recovery.invalidated
        await finish(task)
        assert recovery.attempts == attempt
        assert api.state.phase == PlaybackPhase.PLAYING and api.state.queue.index == 1
        assert api._queue_advance_task is None
    assert await eof(api) is None
    assert api.state.phase == PlaybackPhase.ERROR
    assert recovery.attempts == 3 and loads(api) == [STATION] * 4
    for value in (4, 0, 1):
        await mode(api, value)
        await api.sync_mpv_state()
    assert api.state.phase == PlaybackPhase.ERROR and loads(api) == [STATION] * 4


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 1])
async def test_unready_station_exhausts_recovery_instead_of_repeat_advance(api, value):
    await play_station(api)
    await mode(api, value)
    async def unavailable(*args):
        api.mpv.idle = True
        return False
    api._wait_for_mpv_audio_ready = unavailable
    await finish(await eof(api))
    assert api.state.phase == PlaybackPhase.ERROR
    assert loads(api) == [STATION] * 4 and api.state.queue.index == 1
    assert api._queue_advance_task is None and api._station_recovery.attempts == 3
