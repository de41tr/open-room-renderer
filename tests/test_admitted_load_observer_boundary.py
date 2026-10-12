"""Admitted readiness/cleanup is distinct from post-ready source ownership."""
import asyncio
from dataclasses import asdict

import pytest

from fake_linkplay.http_api import SourcePlaybackError
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.state import PlaybackPhase
from test_native_queue_staging import A, B, C, make_api, start


def nested_child(api):
    api.mpv.url, api.mpv.entry_id = C, 999
    api.mpv.playlist_path = B
    api.mpv.entries = [{"id": 999, "filename": C, "playlist-path": B}]


@pytest.mark.parametrize("retry", [False, True])
async def test_admitted_nested_descendant_keeps_owned_unready_cleanup(tmp_path, retry):
    api = make_api(tmp_path, enabled=False, urls=(A,))
    if retry:
        api.state.queue.current.stream_type = "station"
        api.state.queue.current.duration = 0
        api._activate_queue_item()
        await start(api)
        generation = api.state.generation
        assert api._native_ready_generation == generation
        recovery = api._station_recovery
        api.mpv.idle, api.mpv.url = True, ""
    else:
        recovery = None
    entered, release = asyncio.Event(), asyncio.Event()
    async def unready(*_):
        nested_child(api)
        entered.set()
        await release.wait()
        return False
    api._wait_for_mpv_audio_ready = unready
    task = asyncio.create_task(api._load_playback_url(A, generation, recovery=recovery)
                               if retry else start(api))
    await entered.wait()
    generation = api.state.generation
    before, calls = asdict(api.state), list(api.mpv.calls)
    assert api._admitted_load_generation == generation
    await api.sync_mpv_state()
    assert asdict(api.state) == before and api.mpv.calls == calls
    assert not api._native_output_uncertain
    release.set()
    with pytest.raises(SourcePlaybackError):
        await task
    assert api.mpv.calls[len(calls):] == [("stop", None)]
    assert api.mpv.idle and api._admitted_load_generation is None
    assert api.state.generation == generation
    assert not api._native_output_uncertain


async def test_admitted_backend_failure_is_visible_and_cleanup_preserves_error(tmp_path):
    api = make_api(tmp_path, enabled=False, urls=(A,))
    entered, release = asyncio.Event(), asyncio.Event()
    async def unready(*_):
        entered.set()
        await release.wait()
        raise BackendError("Synthetic readiness failure")
    api._wait_for_mpv_audio_ready = unready
    task = asyncio.create_task(start(api))
    await entered.wait()
    api._apply_backend_snapshot({"backend-error": True})
    assert api.state.phase == PlaybackPhase.ERROR and api.state.last_error == "Backend control failed"
    release.set()
    with pytest.raises(BackendError):
        await task
    assert api.state.last_error == "Backend control failed" and api.mpv.idle
    assert api._admitted_load_generation is None


async def test_readiness_retirement_restores_post_ready_foreign_observation(tmp_path):
    api = make_api(tmp_path, enabled=False, urls=(A,))
    await start(api)
    assert api._admitted_load_generation is None
    nested_child(api)
    await api.sync_mpv_state()
    assert api.state.phase == PlaybackPhase.ERROR and api._native_output_uncertain


async def test_old_load_exit_cannot_clear_newer_admitted_transaction(tmp_path):
    api = make_api(tmp_path, enabled=False, urls=(A,))
    first_entered, second_entered = asyncio.Event(), asyncio.Event()
    first_release, second_release = asyncio.Event(), asyncio.Event()
    async def readiness(generation, url):
        if url == A:
            first_entered.set()
            await first_release.wait()
        else:
            second_entered.set()
            await second_release.wait()
        return True
    api._wait_for_mpv_audio_ready = readiness
    old = asyncio.create_task(start(api))
    await first_entered.wait()
    new = asyncio.create_task(api._play_url(B))
    await second_entered.wait()
    newer_generation = api.state.generation
    first_release.set()
    with pytest.raises(BackendError):
        await old
    assert api._admitted_load_generation == newer_generation
    second_release.set()
    await new
    assert api._admitted_load_generation is None and api.state.phase == PlaybackPhase.PLAYING
    assert api.mpv.url == B
