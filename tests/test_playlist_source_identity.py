"""UNIT/MOCK provenance checks for mpv-expanded playlist containers."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.mpv_backend import BackendError, MpvBackend
from fake_linkplay.queue import PlaybackQueue, QueueItem
from fake_linkplay.state import PlaybackPhase, RendererState


CONTAINER = "https://radio.example.test/station.m3u"
CHILD = "https://radio.example.test/tone.mp3"
OTHER = "https://radio.example.test/other.pls"


def expanded_snapshot() -> dict:
    return {
        "path": CHILD, "playlist-path": CONTAINER,
        "playlist": [{"filename": CHILD, "playlist-path": CONTAINER,
                      "id": 2, "current": True, "playing": True}],
        "idle-active": False, "pause": False, "eof-reached": False,
        "playback-time": 7, "duration": 20,
        "media-title": "Synthetic child title",
        "track-list": [{"type": "audio", "selected": True}],
    }


@pytest.fixture
def api(tmp_path):
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path))
    backend = AsyncMock(spec=MpvBackend)
    result = HttpApi(cfg, RendererState.from_config(cfg), backend)
    result.state.queue = PlaybackQueue(items=[QueueItem(CONTAINER), QueueItem(OTHER)], index=1)
    result.state.current_url = result.state.backend_url = result.state.backend_original_url = CONTAINER
    result.state.phase, result.state.intent = PlaybackPhase.PLAYING, "play"
    result._schedule_event_notify = lambda *args: None
    return result


def test_source_identity_accepts_exact_direct_or_consistent_active_container_child():
    matches = HttpApi._mpv_snapshot_matches_source
    assert matches({"path": CHILD}, CHILD)
    assert matches(expanded_snapshot(), CONTAINER)
    snapshot = expanded_snapshot()
    snapshot["playlist"].insert(0, {"filename": "https://radio.example.test/previous.aac",
                                   "playlist-path": CONTAINER, "id": 1})
    assert matches(snapshot, CONTAINER), "Inactive siblings do not change container ownership"


def test_source_identity_rejects_missing_forged_or_inconsistent_playlist_provenance():
    cases = []
    for key, value in (("path", None), ("path", ""), ("playlist-path", OTHER),
                       ("playlist-path", None), ("playlist", None), ("playlist", []),
                       ("playlist", ["invalid-entry"])):
        snapshot = expanded_snapshot()
        snapshot[key] = value
        cases.append(snapshot)
    for key, value in (("filename", OTHER), ("playlist-path", OTHER), ("playlist-path", None),
                       ("current", False), ("playing", False), ("current", 1), ("playing", "yes")):
        snapshot = expanded_snapshot()
        snapshot["playlist"][0][key] = value
        cases.append(snapshot)
    snapshot = expanded_snapshot()
    snapshot["playlist"].append(deepcopy(snapshot["playlist"][0]))
    cases.append(snapshot)
    snapshot = expanded_snapshot()
    snapshot["playlist"].append({"filename": OTHER, "current": True})
    cases.append(snapshot)
    for snapshot in cases:
        assert not HttpApi._mpv_snapshot_matches_source(snapshot, CONTAINER), snapshot
    assert not HttpApi._mpv_snapshot_matches_source(expanded_snapshot(), OTHER)
    assert not HttpApi._mpv_snapshot_matches_source(expanded_snapshot(), "")


@pytest.mark.asyncio
async def test_active_container_sync_tracks_child_but_does_not_advance_canonical_queue_on_entry_eof(api):
    original_item = api.state.queue.current
    snapshot = expanded_snapshot()
    api._apply_backend_snapshot(snapshot)
    assert api.state.phase == PlaybackPhase.PLAYING and api.state.curpos == 7
    assert api.state.title == snapshot["media-title"]
    assert api.state.backend_url == api.state.current_url == CONTAINER
    snapshot["eof-reached"] = True
    api._apply_backend_snapshot(snapshot)
    assert api.state.phase == PlaybackPhase.PLAYING
    assert api.state.queue.index == 1 and api.state.queue.current is original_item
    assert api._queue_advance_task is None
    # Only terminal idle advances the top-level queue, even after libmpv has
    # cleared its active child and top-level playlist-path properties.
    api._advance_queue_after_eof = AsyncMock()
    snapshot.update({"idle-active": True, "path": None, "playlist-path": None})
    for entry in snapshot["playlist"]:
        entry.pop("current", None)
        entry.pop("playing", None)
    api._apply_backend_snapshot(snapshot)
    task = api._queue_advance_task
    assert task is not None
    await task
    api._advance_queue_after_eof.assert_awaited_once()


@pytest.mark.asyncio
async def test_old_container_observation_cannot_update_new_source_or_complete_its_readiness(api):
    snapshot = expanded_snapshot()
    api.state.current_url = api.state.backend_url = OTHER
    api.state.curpos, api.state.title = 3, "New source title"
    api._apply_backend_snapshot(snapshot)
    assert (api.state.curpos, api.state.title) == (3, "New source title")
    api.mpv.snapshot.return_value = snapshot
    assert not await api._wait_for_mpv_audio_ready(api.state.generation, OTHER, timeout=0.01)


@pytest.mark.asyncio
async def test_generation_change_discards_even_valid_playlist_snapshot(api):
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed_snapshot():
        started.set()
        await release.wait()
        return expanded_snapshot()

    api.mpv.snapshot.side_effect = delayed_snapshot
    task = asyncio.create_task(api.sync_mpv_state())
    await started.wait()
    api.state.accept_command("play")
    api.state.current_url = api.state.backend_url = OTHER
    api.state.curpos, api.state.title = 3, "New generation title"
    release.set()
    await task
    assert (api.state.curpos, api.state.title) == (3, "New generation title")


@pytest.mark.asyncio
async def test_readiness_requires_actual_source_identity_and_preserves_generation_fence(api):
    api.mpv.snapshot.return_value = expanded_snapshot()
    assert await api._wait_for_mpv_audio_ready(api.state.generation, CONTAINER, timeout=0.01)
    missing_identity = expanded_snapshot()
    missing_identity.pop("path")
    api.mpv.snapshot.return_value = missing_identity
    assert not await api._wait_for_mpv_audio_ready(api.state.generation, CONTAINER, timeout=0.01)

    async def superseded_snapshot():
        api.state.accept_command("stop")
        return expanded_snapshot()

    api.mpv.snapshot.side_effect = superseded_snapshot
    assert not await api._wait_for_mpv_audio_ready(api.state.generation, CONTAINER, timeout=0.01)


@pytest.mark.asyncio
async def test_pending_seek_rejects_another_entry_even_if_container_and_target_match(api):
    # Repeated URLs are legitimate playlist entries; a stable path alone does
    # not prove the same entry is confirming the seek.
    for second_path in ("https://radio.example.test/second.aac", CHILD):
        first, second = expanded_snapshot(), expanded_snapshot()
        second["path"] = second["playlist"][0]["filename"] = second_path
        second["playlist"][0]["id"] = 3
        second["playback-time"] = 10
        reads = 0

        async def observations():
            nonlocal reads
            reads += 1
            return first if reads == 1 else second

        api.mpv.snapshot.side_effect = observations
        api.mpv.seek.return_value = {"error": "success"}
        api.state.phase, api.state.intent = PlaybackPhase.PLAYING, "play"
        api.state.curpos = 7
        api._seek_timeout, api._seek_poll_interval = 0.03, 0.001
        with pytest.raises(BackendError):
            await api._seek_command(10)
        assert api.state.phase == PlaybackPhase.ERROR
        assert api.state.curpos == 7, "A different native entry cannot confirm or publish the requested seek"
        assert api.state.current_url == api.state.backend_url == CONTAINER
        assert api.state.queue.index == 1 and not api._seek_tasks


@pytest.mark.parametrize("position,duration", [(None, None), (20, 20.088)])
def test_loading_next_embedded_entry_reports_buffering_until_selected_audio(api, position, duration):
    snapshot = expanded_snapshot()
    snapshot.update({"core-idle": True, "playback-time": position, "duration": duration, "track-list": []})
    api._apply_backend_snapshot(snapshot)
    assert api.state.phase == PlaybackPhase.BUFFERING
    assert api.state.queue.index == 1 and api._queue_advance_task is None
    snapshot.update({"core-idle": False, "playback-time": 0,
                     "track-list": [{"type": "audio", "selected": True}]})
    api._apply_backend_snapshot(snapshot)
    assert api.state.phase == PlaybackPhase.PLAYING


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["pause", "resume"])
async def test_transport_control_cannot_confirm_foreign_container(api, command):
    snapshot = expanded_snapshot()
    snapshot["playlist-path"] = snapshot["playlist"][0]["playlist-path"] = OTHER
    snapshot["pause"] = command == "pause"
    api.mpv.snapshot.return_value = snapshot
    getattr(api.mpv, command).return_value = {"error": "success"}
    with pytest.raises(BackendError, match="source not confirmed"):
        await api._transport_command(command)
    assert api.state.phase == PlaybackPhase.ERROR
    assert api.state.transport_status == "ERROR_OCCURRED"


@pytest.mark.asyncio
@pytest.mark.parametrize("command,key,value", [("pause", "pause", None), ("resume", "pause", None),
    ("resume", "idle-active", None), ("stop", "idle-active", None)])
async def test_transport_control_requires_actual_boolean_confirmation(api, command, key, value):
    snapshot = expanded_snapshot()
    snapshot.update({"pause": command == "pause", "idle-active": command == "stop", key: value})
    api.mpv.snapshot.return_value = snapshot
    getattr(api.mpv, command).return_value = {"error": "success"}
    with pytest.raises(BackendError):
        await api._transport_command(command)


@pytest.mark.parametrize("tracks", [1, "invalid", {"type": "audio"}])
def test_malformed_track_list_cannot_claim_selected_audio_or_raise(api, tracks):
    snapshot = expanded_snapshot()
    snapshot.update({"track-list": tracks, "core-idle": True})
    api._apply_backend_snapshot(snapshot)
    assert api.state.phase == PlaybackPhase.BUFFERING
