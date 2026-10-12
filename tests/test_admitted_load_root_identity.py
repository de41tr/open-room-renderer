"""A valid load ACK can disprove ownership of a later parentless root."""
import asyncio
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

from fake_linkplay.http_api import HttpApi, SourcePlaybackError
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.state import PlaybackPhase
from test_native_queue_staging import A, B, C, make_api, start


def acknowledge(api, data=None):
    original = api.mpv.loadfile
    async def load(url, options=None):
        result = await original(url, options)
        return {**result, "data": {"playlist_entry_id": api.mpv.entry_id} if data is None else data}
    api.mpv.loadfile = load


def replace(api, url=C, parent=None):
    api.mpv.url, api.mpv.entry_id = url, 999
    api.mpv.playlist_path = parent
    api.mpv.entries = [{"id": 999, "filename": url}]
    if parent is not None:
        api.mpv.entries[0]["playlist-path"] = parent


@pytest.mark.parametrize("route", ["observer", "readiness", "failure", "cancel"])
@pytest.mark.parametrize("same_url", [False, True])
@pytest.mark.parametrize("unready", [False, True])
async def test_root_replacement_never_confirms_or_stops_foreign_audio(tmp_path, route, same_url, unready):
    api = make_api(tmp_path, enabled=False, urls=(A,))
    acknowledge(api)
    entered, release = asyncio.Event(), asyncio.Event()
    async def readiness(generation, url):
        entered.set()
        await release.wait()
        if route in {"failure", "cancel"}:
            return False
        return await HttpApi._wait_for_mpv_audio_ready(api, generation, url)
    api._wait_for_mpv_audio_ready = readiness
    task = asyncio.create_task(start(api))
    await entered.wait()
    generation = api.state.generation
    assert api._admitted_load_entry_id == 1
    replace(api, A if same_url else C)
    if unready:
        original_snapshot = api.mpv.snapshot
        async def snapshot():
            return {**await original_snapshot(), "track-list": [], "core-idle": True}
        api.mpv.snapshot = snapshot
    calls = list(api.mpv.calls)
    if route == "observer":
        await api.sync_mpv_state()
    if route == "cancel":
        task.cancel()
    else:
        release.set()
    with pytest.raises(asyncio.CancelledError if route == "cancel" else BackendError):
        await task
    assert api.mpv.calls == calls
    assert api.state.phase == PlaybackPhase.ERROR and api.state.generation == generation + 1
    assert api.state.current_url == api.state.backend_url == A and api.state.intent == "play"
    assert api._native_output_uncertain and not api.state.gain_confirmed
    assert "silence not confirmed" in api.state.last_error
    assert api.mpv.url == (A if same_url else C) and not api.mpv.idle
    assert api._admitted_load_generation is None and api._admitted_load_entry_id is None


@pytest.mark.parametrize("parent", [None, A, B])
async def test_owned_unready_root_and_expanded_children_still_get_cleanup(tmp_path, parent):
    api = make_api(tmp_path, enabled=False, urls=(A,))
    acknowledge(api)
    async def unready(*_):
        if parent is not None:
            replace(api, C, parent)
        before = asdict(api.state)
        await api.sync_mpv_state()
        assert asdict(api.state) == before
        return False
    api._wait_for_mpv_audio_ready = unready
    with pytest.raises(SourcePlaybackError):
        await start(api)
    assert api.mpv.calls == [("load", A), ("stop", None)]
    assert api.mpv.idle and not api._native_output_uncertain
    assert api._admitted_load_entry_id is None


@pytest.mark.parametrize("data", [{}, {"playlist_entry_id": None}, {"playlist_entry_id": True},
    {"playlist_entry_id": -1}, {"playlist_entry_id": 1.0}, {"playlist_entry_id": "1"}, []])
async def test_missing_or_invalid_ack_never_borrows_identity_from_snapshot(tmp_path, data):
    api = make_api(tmp_path, enabled=False, urls=(A,))
    acknowledge(api, data)
    async def unready(generation, url):
        assert api._admitted_load_entry_id is None
        snap = await api.mpv.snapshot()
        snap["path"] = C
        snap["playlist"] = [{"id": 999, "filename": C, "current": True, "playing": True}]
        assert not api._reject_replaced_load_root(snap, generation)
        assert api._admitted_load_entry_id is None
        return False
    api._wait_for_mpv_audio_ready = unready
    with pytest.raises(SourcePlaybackError):
        await start(api)
    assert api.mpv.idle and not api._native_output_uncertain


@pytest.mark.parametrize("bad", ["idle", "eof", "parent", "torn_parent", "torn_path", "partial", "old_generation"])
async def test_ambiguous_or_out_of_scope_root_sample_cannot_invalidate_owner(tmp_path, bad):
    api = make_api(tmp_path, enabled=False, urls=(A,))
    acknowledge(api)
    async def unready(generation, url):
        snap = await api.mpv.snapshot()
        snap["path"] = C
        snap["playlist"] = [{"id": 999, "filename": C, "current": True, "playing": True}]
        if bad == "idle":
            snap["idle-active"] = True
        elif bad == "eof":
            snap["eof-reached"] = True
        elif bad == "parent":
            snap["playlist-path"] = snap["playlist"][0]["playlist-path"] = B
        elif bad == "torn_parent":
            snap["playlist-path"] = B
        elif bad == "torn_path":
            snap["playlist"][0]["filename"] = A
        elif bad == "partial":
            snap["playlist"][0].pop("playing")
        else:
            generation -= 1
        before = asdict(api.state)
        assert not api._reject_replaced_load_root(snap, generation)
        assert asdict(api.state) == before
        return False
    api._wait_for_mpv_audio_ready = unready
    with pytest.raises(SourcePlaybackError):
        await start(api)
    assert api.mpv.idle


async def test_gain_wrapper_preserves_success_result_and_confirmation_semantics(tmp_path):
    api = make_api(tmp_path, enabled=False, urls=(A,))
    reply = {"error": "success", "data": {"playlist_entry_id": 42}}
    assert await api._gain_reconciling_call(AsyncMock(return_value=reply)()) is reply
    assert api.state.gain_confirmed and not api._gain_uncertain and api._gain_operations == 0
    with pytest.raises(BackendError):
        await api._gain_reconciling_call(AsyncMock(return_value={"error": "Synthetic rejection"})())
    assert not api.state.gain_confirmed and api._gain_uncertain and api._gain_operations == 0
    assert await api._gain_reconciling_call(AsyncMock(return_value=reply)()) is reply
    assert api.state.gain_confirmed and not api._gain_uncertain and api._gain_operations == 0


async def test_foreign_root_during_cleanup_preserves_prior_terminal_error(tmp_path):
    api = make_api(tmp_path, enabled=False, urls=(A,))
    acknowledge(api)
    async def unready(*_):
        api._apply_backend_snapshot({"backend-error": True})
        replace(api)
        return False
    api._wait_for_mpv_audio_ready = unready
    with pytest.raises(BackendError):
        await start(api)
    assert api.state.phase == PlaybackPhase.ERROR and api.state.last_error == "Backend control failed"
    assert api._native_output_uncertain and api.mpv.calls == [("load", A)] and api.mpv.url == C


async def test_stale_load_cannot_clear_newer_acknowledged_root_token(tmp_path):
    api = make_api(tmp_path, enabled=False, urls=(A,))
    acknowledge(api)
    old_entered, new_entered = asyncio.Event(), asyncio.Event()
    old_release, new_release = asyncio.Event(), asyncio.Event()
    async def readiness(generation, url):
        entered, release = (old_entered, old_release) if url == A else (new_entered, new_release)
        entered.set()
        await release.wait()
        return True
    api._wait_for_mpv_audio_ready = readiness
    old = asyncio.create_task(start(api))
    await old_entered.wait()
    assert api._admitted_load_entry_id == 1
    new = asyncio.create_task(api._play_url(B))
    await new_entered.wait()
    assert api._admitted_load_entry_id == 2
    generation = api._admitted_load_generation
    old_release.set()
    with pytest.raises(BackendError):
        await old
    assert api._admitted_load_generation == generation and api._admitted_load_entry_id == 2
    new_release.set()
    await new
    assert api._admitted_load_entry_id is None and api._admitted_load_generation is None
    assert api.state.phase == PlaybackPhase.PLAYING and api.mpv.url == B


@pytest.mark.parametrize("scheme", ["edl", "EDL"])
async def test_internal_edl_root_is_ambiguous_and_keeps_owned_cleanup(tmp_path, scheme):
    api = make_api(tmp_path, enabled=False, urls=(A,))
    acknowledge(api)
    async def unready(generation, url):
        replace(api, f"{scheme}://synthetic-owned-segment")
        before = asdict(api.state)
        await api.sync_mpv_state()
        assert asdict(api.state) == before and not api._native_output_uncertain
        return False
    api._wait_for_mpv_audio_ready = unready
    with pytest.raises(SourcePlaybackError):
        await start(api)
    assert api.mpv.calls == [("load", A), ("stop", None)]
    assert api.mpv.idle and not api._native_output_uncertain
    assert api.state.last_error == "Source did not confirm playback"
