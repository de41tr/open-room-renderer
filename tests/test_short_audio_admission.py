"""Paused admission: positive audio evidence, owner races, and immediate EOF."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from fake_linkplay.http_api import HttpApi, SourcePlaybackError
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.state import PlaybackPhase
from test_admitted_load_root_identity import acknowledge, replace
from test_native_queue_staging import A, B, make_api, start


@pytest.mark.parametrize("evidence", ["missing_track", "unselected", "missing_selection", "idle", "eof", "unpaused", "foreign"])
async def test_time_or_duration_never_substitutes_for_paused_selected_audio(tmp_path, evidence):
    api = make_api(tmp_path, enabled=False, urls=(A,))
    acknowledge(api)
    async def readiness(*_):
        snapshot = await api.mpv.snapshot()
        if evidence == "missing_track":
            snapshot["track-list"] = []
        elif evidence == "unselected":
            snapshot["track-list"][0]["selected"] = False
        elif evidence == "missing_selection":
            snapshot["track-list"][0].pop("selected")
        else:
            key, value = {"idle": ("idle-active", True), "eof": ("eof-reached", True),
                          "unpaused": ("pause", False), "foreign": ("path", B)}[evidence]
            snapshot[key] = value
        assert snapshot["duration"] > 0 and isinstance(snapshot["playback-time"], (float, int))
        assert not api._mpv_snapshot_ready_while_paused(snapshot, A)
        return False
    api._wait_for_mpv_audio_ready = readiness
    with pytest.raises(SourcePlaybackError):
        await start(api)
    assert api.mpv.calls == [("load", A), ("stop", None)]
    assert api.state.phase == PlaybackPhase.ERROR


@pytest.mark.parametrize("when", ["preparation", "acknowledgement"])
async def test_same_url_root_replacement_around_resume_is_never_stopped_as_owned(tmp_path, when):
    api = make_api(tmp_path, enabled=False, urls=(A,))
    acknowledge(api)
    api._wait_for_mpv_audio_ready = AsyncMock(return_value=True)
    original = api.mpv.resume
    async def resume(*, current=None):
        if when == "preparation":
            replace(api, A)
        await original(current=current)
        if when == "acknowledgement":
            replace(api, A)
    api.mpv.resume = resume
    with pytest.raises(BackendError):
        await start(api)
    assert api.mpv.entry_id == 999 and not api.mpv.idle
    assert [name for name, _ in api.mpv.calls] == (["load"] if when == "preparation" else ["load", "resume"])
    assert api.state.phase == PlaybackPhase.ERROR and api._native_output_uncertain
    assert not api.state.gain_confirmed and "silence not confirmed" in api.state.last_error


async def test_stop_during_resume_preparation_prevents_old_unpause(tmp_path):
    api = make_api(tmp_path, enabled=False, urls=(A,))
    acknowledge(api)
    api._wait_for_mpv_audio_ready = AsyncMock(return_value=True)
    entered, release = asyncio.Event(), asyncio.Event()
    original = api.mpv.resume
    async def resume(*, current=None):
        entered.set()
        await release.wait()
        await original(current=current)
    api.mpv.resume = resume
    play = asyncio.create_task(start(api))
    await entered.wait()
    stop = asyncio.create_task(api._transport_command("stop"))
    await asyncio.sleep(0)
    assert api.state.intent == "stop"
    release.set()
    with pytest.raises(BackendError):
        await play
    await stop
    assert api.mpv.calls == [("load", A), ("stop", None)]
    assert api.state.phase == PlaybackPhase.STOPPED and api.mpv.idle


async def test_proven_audio_ending_after_resume_ack_is_success_and_advances_once(tmp_path):
    api = make_api(tmp_path, enabled=False, urls=(A, B))
    acknowledge(api)
    api._wait_for_mpv_audio_ready = AsyncMock(return_value=True)
    api._advance_queue_after_eof = AsyncMock()
    original = api.mpv.resume
    async def resume(*, current=None):
        await original(current=current)
        api.mpv.idle, api.mpv.url = True, ""
    api.mpv.resume = resume
    await start(api)
    assert api.state.phase == PlaybackPhase.STOPPED and api.state.intent == "play"
    assert api.state.backend_url == api.state.backend_original_url == api.state.last_error == ""
    assert api.mpv.calls == [("load", A), ("resume", None)]
    await api._queue_advance_task
    api._advance_queue_after_eof.assert_awaited_once_with(api.state.generation, api.state.queue)


async def test_same_url_reload_waits_for_acknowledged_playing_root_audio(tmp_path):
    api = make_api(tmp_path, enabled=False, urls=(A,))
    acknowledge(api)
    await start(api)
    await api._transport_command("stop")
    api.mpv.calls.clear()
    api._wait_for_mpv_audio_ready = HttpApi._wait_for_mpv_audio_ready.__get__(api)
    original = api.mpv.snapshot
    samples = []
    async def snapshot():
        result = await original()
        if api._admitted_load_generation == api.state.generation and api.mpv.paused:
            samples.append(result)
            if len(samples) == 1:
                # Native Stop/reload of the same URL can expose retiring
                # audio metadata while the new ACK is only current, not playing.
                result["playlist"][0].pop("playing")
            elif len(samples) == 2:
                result["track-list"] = []
                result["playback-time"] = None
        return result
    api.mpv.snapshot = snapshot
    await start(api)
    assert len(samples) >= 4  # Two non-ready reads, real readiness, final owner check.
    assert api.mpv.calls == [("load", A), ("resume", None)]
    assert api.state.phase == PlaybackPhase.PLAYING and not api.state.last_error
    assert api.mpv.entry_id == 2 and not api._native_output_uncertain


@pytest.mark.parametrize("parent,expected", [(A, True), (B, False), (None, False)])
async def test_acknowledged_container_readiness_requires_exact_first_level_parent(tmp_path, parent, expected):
    api = make_api(tmp_path, enabled=False, urls=(A,))
    acknowledge(api)
    async def readiness(generation, url):
        assert api._admitted_load_entry_id == 1
        replace(api, B, parent)
        snapshot = await api.mpv.snapshot()
        assert api._mpv_snapshot_ready_while_paused(snapshot, A) is expected
        return False
    api._wait_for_mpv_audio_ready = readiness
    with pytest.raises(SourcePlaybackError):
        await start(api)
    # Admission rejection cannot manufacture an ACK for the expanded child.
    assert api._admitted_load_entry_id is None
