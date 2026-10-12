"""An earlier backend failure outranks a later readiness result."""
import asyncio

import pytest

from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.state import PlaybackPhase
from test_native_queue_staging import A, make_api, start


@pytest.mark.parametrize("ready", [False, True])
async def test_terminal_backend_error_survives_readiness_result_and_owned_cleanup(tmp_path, ready):
    api = make_api(tmp_path, enabled=False, urls=(A,))
    entered, release = asyncio.Event(), asyncio.Event()
    async def readiness(*_):
        entered.set()
        await release.wait()
        return ready
    api._wait_for_mpv_audio_ready = readiness
    task = asyncio.create_task(start(api))
    await entered.wait()
    api._apply_backend_snapshot({"backend-error": True})
    generation, calls = api.state.generation, list(api.mpv.calls)
    assert api.state.phase == PlaybackPhase.ERROR and api.state.last_error == "Backend control failed"
    release.set()
    with pytest.raises(BackendError):
        await task
    assert api.state.phase == PlaybackPhase.ERROR and api.state.last_error == "Backend control failed"
    assert api.state.generation == generation and not api.state.gain_confirmed
    assert api.mpv.calls[len(calls):] == [("stop", None)] and api.mpv.idle
    assert api._admitted_load_generation is None


async def test_terminal_backend_error_during_owned_cleanup_keeps_original_cause(tmp_path):
    api = make_api(tmp_path, enabled=False, urls=(A,))
    entered, release = asyncio.Event(), asyncio.Event()
    async def unready(*_):
        return False
    original_stop = api.mpv.stop
    async def stop():
        await original_stop()
        entered.set()
        await release.wait()
    api._wait_for_mpv_audio_ready = unready
    api.mpv.stop = stop
    task = asyncio.create_task(start(api))
    await entered.wait()
    api._apply_backend_snapshot({"backend-error": True})
    release.set()
    with pytest.raises(BackendError):
        await task
    assert api.state.phase == PlaybackPhase.ERROR and api.state.last_error == "Backend control failed"
    assert api.mpv.idle and not api.state.backend_url and not api.state.gain_confirmed
    assert api._admitted_load_generation is None
