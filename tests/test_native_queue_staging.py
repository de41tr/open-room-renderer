"""UNIT/MOCK ownership races for opt-in one-successor native queue staging."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET

import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi, parse_command
from fake_linkplay.mpv_backend import BackendError, MpvBackend
from fake_linkplay.queue import PlaybackQueue, QueueItem
from fake_linkplay.state import PlaybackPhase, RendererState
from test_playback_generations import StatefulBackend

A = "https://media.example.test/one.wav"
B = "https://media.example.test/two.wav"
C = "https://media.example.test/three.wav"


class NativeBackend(StatefulBackend):
    gain_confirmed = True
    confirmed_dsp = (0.0, [])

    def __init__(self):
        super().__init__()
        self.entries = []
        self.serial = 0
        self.entry_id = None
        self.append_started = asyncio.Event()
        self.release_append = None
        self.fail_append = False
        self.omit_id = False
        self.after_append = None
        self.playlist_path = None
        self.duration = 30.0

    async def loadfile(self, url, options=None, *, start_paused=False):
        result = await super().loadfile(url, options, start_paused=start_paused)
        self.serial += 1
        self.entry_id = self.serial
        self.entries = [{"id": self.entry_id, "filename": url}]
        self.playlist_path = None
        return result

    async def append_file(self, url):
        self.calls.append(("append", url))
        self.serial += 1
        entry_id = self.serial
        self.entries.append({"id": entry_id, "filename": url})
        self.append_started.set()
        if self.release_append is not None:
            await self.release_append.wait()
        if self.after_append is not None:
            self.after_append(self)
        if self.fail_append:
            raise BackendError("Synthetic missing acknowledgement after application")
        return entry_id if not self.omit_id else None

    def advance(self):
        current = next(index for index, entry in enumerate(self.entries) if entry["id"] == self.entry_id)
        if current + 1 == len(self.entries):
            self.idle, self.url = True, ""
        else:
            entry = self.entries[current + 1]
            self.entry_id, self.url = entry["id"], entry["filename"]
            self.position = 0
            self.playlist_path = entry.get("playlist-path")

    async def stop(self):
        await super().stop()
        self.entries = []
        self.entry_id = None
        self.playlist_path = None

    async def snapshot(self):
        result = await super().snapshot()
        entries = deepcopy(self.entries)
        for entry in entries:
            if entry["id"] == self.entry_id and not self.idle:
                entry.update(current=True, playing=True)
        return {**result, "playlist": entries, "playlist-path": self.playlist_path, "duration": self.duration}


def make_api(tmp_path, *, enabled=True, urls=(A, B), durations=None):
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path),
                 native_queue_staging=enabled)
    api = HttpApi(cfg, RendererState.from_config(cfg), NativeBackend())
    api._wait_for_mpv_audio_ready = AsyncMock(return_value=True)
    api._schedule_event_notify = lambda *args: None
    api.state.queue = PlaybackQueue(name="Synthetic queue", index=1,
        items=[QueueItem(url=url, title=f"Track {index}", artist=f"Artist {index}",
                         duration=durations[index - 1] if durations else 30)
               for index, url in enumerate(urls, 1)])
    api._activate_queue_item()
    return api


async def start(api):
    await api._play_url(api.state.queue.current.url)


def mutations(api, command):
    return [value for name, value in api.mpv.calls if name == command]


def test_config_default_and_strict_boolean():
    assert Config().native_queue_staging is False
    for invalid in ("true", 1, None, []):
        with pytest.raises(ValueError, match="native_queue_staging"):
            Config(native_queue_staging=invalid)


@pytest.mark.asyncio
async def test_backend_append_requires_native_ack_id_and_changes_no_controls():
    backend = MpvBackend("unused", auto_spawn=False)
    backend.checked_command = AsyncMock(return_value={"error": "success", "data": {"playlist_entry_id": 42}})
    assert await backend.append_file(B) == 42
    backend.checked_command.assert_awaited_once_with(["loadfile", B, "append"])
    for data in ({}, None, {"playlist_entry_id": True}, {"playlist_entry_id": -1}):
        backend.checked_command.return_value = {"error": "success", "data": data}
        with pytest.raises(BackendError, match="identity"):
            await backend.append_file(B)


@pytest.mark.asyncio
async def test_default_off_retains_existing_eof_queue_path(tmp_path):
    api = make_api(tmp_path, enabled=False)
    await start(api)
    assert mutations(api, "append") == []
    api.mpv.advance()
    await api.sync_mpv_state()
    await api._queue_advance_task
    assert api.state.queue.index == 2
    assert mutations(api, "load") == [A, B]


@pytest.mark.asyncio
async def test_ack_does_not_advance_but_owned_native_transition_adopts_metadata(tmp_path):
    api = make_api(tmp_path)
    events = []
    api._schedule_event_notify = lambda *args: events.append({child.tag: child.text for prop in ET.fromstring(api._event_body("PlayQueue")) for child in prop})
    await start(api)
    assert api.state.queue.index == 1 and api.state.title == "Track 1"
    assert mutations(api, "append") == [B]
    assert api._native_stage.entry_id == 2
    api.mpv.advance()
    await api.sync_mpv_state()
    assert api.state.queue.index == 2 and api.state.title == "Track 2"
    assert api.state.current_url == api.state.backend_url == B
    assert api.state.artist == "Artist 2"
    assert mutations(api, "load") == [A] and mutations(api, "stop") == []
    assert api._queue_advance_task is None
    assert any(event.get("CurrentIndex") == "2" and event.get("TrackURI") == B for event in events)
    assert '<CurrentTrack val="2"/>' in ET.fromstring(api._event_body("AVTransport"))[0][0].text


@pytest.mark.asyncio
async def test_only_next_is_staged_and_duplicate_urls_use_entry_identity(tmp_path):
    api = make_api(tmp_path, urls=(A, A, C))
    await start(api)
    assert mutations(api, "append") == [A]
    await api.sync_mpv_state()
    assert api.state.queue.index == 1
    api.mpv.advance()
    await api.sync_mpv_state()
    assert api.state.queue.index == 2
    assert mutations(api, "append") == [A, C]
    assert len(api.mpv.entries) == 3  # Past prefix plus current plus one future.
    api.mpv.advance()
    await api.sync_mpv_state()
    assert api.state.queue.index == 3
    assert mutations(api, "load") == [A]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["gain", "dsp", "live", "duration", "expanded", "foreign_tail"])
async def test_stage_requires_finite_owned_ready_direct_source(tmp_path, reason):
    api = make_api(tmp_path)
    if reason == "gain":
        api.mpv.gain_confirmed = False
    elif reason == "dsp":
        api.mpv.confirmed_dsp = None
    elif reason == "live":
        api.state.queue.current.stream_type = "station"
    elif reason == "duration":
        api.mpv.duration = float("nan")
    original_load = api.mpv.loadfile
    async def load(*args, **kwargs):
        result = await original_load(*args, **kwargs)
        if reason == "expanded":
            api.mpv.playlist_path = A
        elif reason == "foreign_tail":
            api.mpv.entries.append({"id": 99, "filename": C})
        return result
    api.mpv.loadfile = load
    await start(api)
    assert not mutations(api, "append")


@pytest.mark.asyncio
async def test_first_level_expanded_successor_is_one_item_until_container_eof(tmp_path):
    container = "https://media.example.test/list.m3u"
    api = make_api(tmp_path, urls=(A, container, C))
    await start(api)
    api.mpv.entries[-1:] = [{"id": 3, "filename": B, "playlist-path": container},
                            {"id": 4, "filename": C, "playlist-path": container}]
    api.mpv.advance()
    await api.sync_mpv_state()
    assert api.state.queue.index == 2 and api.state.backend_url == container
    assert mutations(api, "append") == [container]
    api.mpv.advance()
    await api.sync_mpv_state()
    assert api.state.queue.index == 2
    api.mpv.advance()
    await api.sync_mpv_state()
    await api._queue_advance_task
    assert api.state.queue.index == 3 and mutations(api, "load") == [A, C]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["wrong_id", "foreign_source", "wrong_parent", "nested_parent", "mixed_prefix"])
async def test_foreign_or_ambiguous_successor_is_never_adopted(tmp_path, kind):
    api = make_api(tmp_path)
    await start(api)
    api.mpv.advance()
    if kind == "wrong_id":
        api.mpv.entry_id = api.mpv.entries[-1]["id"] = 99
    elif kind == "foreign_source":
        api.mpv.url = C
    elif kind in {"wrong_parent", "nested_parent"}:
        api.mpv.entries[-1] = {"id": 3, "filename": C, "playlist-path": C if kind == "wrong_parent" else A}
        api.mpv.entry_id, api.mpv.url, api.mpv.playlist_path = 3, C, B
    else:
        api.mpv.entries[0]["filename"] = C
    await api.sync_mpv_state()
    assert api.state.queue.index == 1
    assert mutations(api, "load") == [A] and mutations(api, "stop") == []


@pytest.mark.asyncio
async def test_successor_error_is_not_retried_as_if_predecessor_eof(tmp_path):
    api = make_api(tmp_path)
    await start(api)
    api.mpv.advance()
    api.mpv.advance()
    await api.sync_mpv_state()
    assert api.state.phase == PlaybackPhase.ERROR and api.state.queue.index == 1
    assert api._queue_advance_task is None and mutations(api, "load") == [A]


@pytest.mark.asyncio
async def test_pause_after_native_transition_adopts_and_preserves_pause(tmp_path):
    api = make_api(tmp_path)
    await start(api)
    api.mpv.advance()
    await api._transport_command("pause")
    assert api.state.queue.index == 2 and api.state.phase == PlaybackPhase.PAUSED
    assert api.mpv.paused and mutations(api, "stop") == []
    await api._transport_command("resume")
    assert api.state.phase == PlaybackPhase.PLAYING and mutations(api, "load") == [A]


@pytest.mark.asyncio
async def test_pause_during_append_ack_at_eof_preserves_new_owner(tmp_path):
    api = make_api(tmp_path)
    api.mpv.release_append = asyncio.Event()
    loading = asyncio.create_task(start(api))
    await api.mpv.append_started.wait()
    api.mpv.advance()
    pausing = asyncio.create_task(api._transport_command("pause"))
    await asyncio.sleep(0)
    api.mpv.release_append.set()
    with pytest.raises(BackendError, match="superseded"):
        await loading
    await pausing
    assert api.state.queue.index == 2 and api.state.phase == PlaybackPhase.PAUSED
    assert api.mpv.paused and mutations(api, "stop") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("new_owner", ["stop", "source"])
async def test_stale_append_cannot_mutate_after_new_stop_or_source(tmp_path, new_owner):
    api = make_api(tmp_path)
    api.mpv.release_append = asyncio.Event()
    loading = asyncio.create_task(start(api))
    await api.mpv.append_started.wait()
    api.mpv.advance()
    newer = asyncio.create_task(api._transport_command("stop") if new_owner == "stop" else api._play_url(C))
    await asyncio.sleep(0)
    api.mpv.release_append.set()
    with pytest.raises(BackendError, match="superseded"):
        await loading
    await newer
    assert api._native_stage is None
    if new_owner == "stop":
        assert api.state.phase == PlaybackPhase.STOPPED and api.mpv.idle
        assert mutations(api, "stop") == [None]
    else:
        assert api.state.phase == PlaybackPhase.PLAYING and api.state.current_url == C
        assert api.mpv.url == C and mutations(api, "stop") == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize("new_owner", [None, "pause", "source"])
async def test_missing_append_ack_after_application_fails_closed_without_stale_cleanup(tmp_path, new_owner):
    api = make_api(tmp_path)
    api.mpv.fail_append = True
    api.mpv.release_append = asyncio.Event()
    loading = asyncio.create_task(start(api))
    await api.mpv.append_started.wait()
    newer = None
    if new_owner is not None:
        newer = asyncio.create_task(api._transport_command("pause") if new_owner == "pause" else api._play_url(C))
        await asyncio.sleep(0)
    api.mpv.release_append.set()
    with pytest.raises(BackendError):
        await loading
    if new_owner == "source":
        await newer
        assert api.state.phase == PlaybackPhase.PLAYING and api.mpv.url == C
        assert mutations(api, "stop") == [None]
    else:
        if newer:
            with pytest.raises(BackendError):
                await newer
        assert api.state.phase == PlaybackPhase.ERROR and api.mpv.idle
        assert mutations(api, "stop") == [None]


@pytest.mark.asyncio
async def test_append_readback_foreign_source_is_reported_without_foreign_stop(tmp_path):
    api = make_api(tmp_path)
    def foreign(backend):
        backend.url, backend.entry_id = C, 99
        backend.entries = [{"id": 99, "filename": C}]
    api.mpv.after_append = foreign
    with pytest.raises(BackendError):
        await start(api)
    assert api.state.phase == PlaybackPhase.ERROR
    assert "silence not confirmed" in api.state.last_error
    assert mutations(api, "stop") == []


@pytest.mark.asyncio
async def test_seek_at_native_transition_uses_actual_successor_duration_and_id(tmp_path):
    api = make_api(tmp_path, durations=(1, 30))
    await start(api)
    api.mpv.advance()
    await api._seek_command(8)
    assert api.state.queue.index == 2 and api.state.curpos == 8
    assert api.state.phase == PlaybackPhase.PLAYING and not api.mpv.paused
    assert mutations(api, "seek") == [8]
    assert mutations(api, "pause") == [None] and mutations(api, "resume") == [None, None]
    assert mutations(api, "load") == [A]


@pytest.mark.asyncio
async def test_pause_survives_seek_at_successor_transition(tmp_path):
    api = make_api(tmp_path)
    await start(api)
    await api._transport_command("pause")
    # Simulates EOF winning just before native pause applied.
    api.mpv.advance()
    await api._seek_command(7)
    assert api.state.queue.index == 2 and api.state.curpos == 7
    assert api.state.phase == PlaybackPhase.PAUSED and api.mpv.paused
    assert mutations(api, "resume") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action, index, url", [("Next", 3, C), ("Previous", 1, A)])
@pytest.mark.parametrize("surface", ["soap", "http"])
async def test_navigation_reconciles_native_successor_before_computing_index(tmp_path, action, index, url, surface):
    api = make_api(tmp_path, urls=(A, B, C))
    await start(api)
    api.mpv.advance()
    if surface == "soap":
        await api._execute_soap_side_effect(action, f"<{action}/>")
    else:
        command = "setPlayerCmd:next" if action == "Next" else "setPlayerCmd:prev"
        assert await api.execute(parse_command(command), "127.0.0.1") == "OK"
    assert api.state.queue.index == index and api.mpv.url == url
    assert mutations(api, "load") == [A, url]
    assert mutations(api, "stop") == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["soap", "http"])
async def test_next_at_native_last_item_keeps_original_play_intent(tmp_path, surface):
    from fake_linkplay.soap import SoapFault
    api = make_api(tmp_path)
    await start(api)
    api.mpv.advance()
    with pytest.raises(SoapFault):
        if surface == "soap":
            await api._execute_soap_side_effect("Next", "<Next/>")
        else:
            await api.execute(parse_command("setPlayerCmd:next"), "127.0.0.1")
    assert api.state.queue.index == 2 and api.state.phase == PlaybackPhase.PLAYING
    assert not api.mpv.paused and mutations(api, "load") == [A]


@pytest.mark.asyncio
@pytest.mark.parametrize("new_owner", [None, "pause", "source"])
async def test_lost_append_ack_after_eof_silences_only_the_unchanged_owned_prefix(tmp_path, new_owner):
    api = make_api(tmp_path)
    api.mpv.fail_append = True
    api.mpv.release_append = asyncio.Event()
    loading = asyncio.create_task(start(api))
    await api.mpv.append_started.wait()
    api.mpv.advance()
    newer = None
    if new_owner:
        newer = asyncio.create_task(api._transport_command("pause") if new_owner == "pause" else api._play_url(C))
        await asyncio.sleep(0)
    api.mpv.release_append.set()
    with pytest.raises(BackendError):
        await loading
    if new_owner == "source":
        await newer
        assert api.mpv.url == C and not api.mpv.idle and mutations(api, "stop") == [None]
    else:
        if newer:
            with pytest.raises(BackendError):
                await newer
        assert api.mpv.idle and api.state.phase == PlaybackPhase.ERROR
        assert api.state.queue.index == 1  # Missing ACK can authorize cleanup, never adoption.
        assert "silence not confirmed" not in api.state.last_error
        assert mutations(api, "stop") == [None]


@pytest.mark.asyncio
async def test_lost_ack_cannot_claim_foreign_replacement_even_at_same_successor_url(tmp_path):
    api = make_api(tmp_path)
    api.mpv.fail_append = True
    def foreign(backend):
        backend.url, backend.entry_id = B, 99
        backend.entries = [{"id": 99, "filename": B}]
    api.mpv.after_append = foreign
    with pytest.raises(BackendError):
        await start(api)
    assert mutations(api, "stop") == [] and not api.mpv.idle
    assert "silence not confirmed" in api.state.last_error


@pytest.mark.asyncio
async def test_invalid_seek_restores_playback_after_identity_fence(tmp_path):
    from fake_linkplay.soap import SoapFault
    api = make_api(tmp_path)
    await start(api)
    with pytest.raises(SoapFault):
        await api._seek_command(30)
    assert not api.mpv.paused and api.state.phase == PlaybackPhase.PLAYING
    assert api.state.queue.index == 1 and api._native_stage.confirmed
    assert mutations(api, "seek") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["gain", "foreign"])
async def test_successor_resolution_rechecks_ready_state_and_native_identity(tmp_path, kind):
    api = make_api(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    async def resolve(url):
        if url == B:
            entered.set()
            await release.wait()
        return url
    api._resolve_playback_url = resolve
    loading = asyncio.create_task(start(api))
    await entered.wait()
    if kind == "gain":
        api.mpv.volume = AsyncMock(side_effect=BackendError("Synthetic gain failure"))
        with pytest.raises(BackendError):
            await api._set_confirmed_gain(volume=15)
    else:
        await api.mpv.loadfile(C)
    release.set()
    if kind == "gain":
        with pytest.raises(BackendError):
            await loading
    else:
        await loading
        assert api.mpv.url == C and not mutations(api, "stop")
    assert mutations(api, "append") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_new_source_resolution_failure_or_cancel_cannot_leave_staged_audio(tmp_path, cancel):
    api = make_api(tmp_path)
    await start(api)
    entered = asyncio.Event()
    async def resolve(url):
        entered.set()
        if cancel:
            await asyncio.Future()
        raise BackendError("Synthetic resolution failure")
    api._resolve_playback_url = resolve
    changing = asyncio.create_task(api._play_url(C))
    await entered.wait()
    if cancel:
        changing.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else BackendError):
        await changing
    assert api.mpv.idle and not api.mpv.entries
    assert api.state.phase == PlaybackPhase.ERROR
    assert mutations(api, "stop") == [None]


@pytest.mark.asyncio
async def test_gain_error_silences_existing_owned_successor_and_preserves_error(tmp_path):
    api = make_api(tmp_path)
    await start(api)
    api.mpv.advance()
    api.mpv.volume = AsyncMock(side_effect=BackendError("Synthetic gain failure"))
    with pytest.raises(BackendError):
        await api._set_confirmed_gain(volume=15)
    await asyncio.gather(*tuple(api._native_cleanup_tasks))
    assert api.mpv.idle and api._native_stage is None
    assert api.state.phase == PlaybackPhase.ERROR and "Gain confirmation failed" in api.state.last_error


@pytest.mark.asyncio
async def test_failed_native_stop_cannot_restore_false_playing_after_invalidation(tmp_path):
    api = make_api(tmp_path)
    await start(api)
    api.mpv.stop = AsyncMock(side_effect=BackendError("Synthetic rejected stop"))
    with pytest.raises(BackendError):
        await api._transport_command("stop")
    api.mpv.advance()
    await api.sync_mpv_state()
    assert api.state.phase == PlaybackPhase.ERROR
    assert "silence not confirmed" in api.state.last_error


@pytest.mark.asyncio
async def test_cancelled_new_source_during_append_silences_retired_owned_queue(tmp_path):
    api = make_api(tmp_path)
    api.mpv.release_append = asyncio.Event()
    loading = asyncio.create_task(start(api))
    await api.mpv.append_started.wait()
    changing = asyncio.create_task(api._play_url(C))
    await asyncio.sleep(0)
    changing.cancel()
    await asyncio.sleep(0)
    api.mpv.advance()
    api.mpv.release_append.set()
    with pytest.raises(BackendError):
        await loading
    with pytest.raises(asyncio.CancelledError):
        await changing
    assert api.mpv.idle and not api.mpv.entries
    assert api.state.phase == PlaybackPhase.ERROR


@pytest.mark.asyncio
async def test_foreign_tail_replacement_is_error_without_wrong_stop(tmp_path):
    api = make_api(tmp_path)
    await start(api)
    api.mpv.entries[-1] = {"id": 99, "filename": B}
    api.mpv.advance()
    await api.sync_mpv_state()
    assert api.state.phase == PlaybackPhase.ERROR and api.state.queue.index == 1
    assert "silence not confirmed" in api.state.last_error
    assert not mutations(api, "stop") and not api.mpv.idle


@pytest.mark.asyncio
async def test_unconfirmed_cleanup_requires_stop_before_next_source_resolution(tmp_path):
    api = make_api(tmp_path)
    await start(api)
    real_stop = api.mpv.stop
    api.mpv.stop = AsyncMock(side_effect=BackendError("Synthetic failed Stop"))
    with pytest.raises(BackendError):
        await api._transport_command("stop")
    assert api._native_output_uncertain and api.state.phase == PlaybackPhase.ERROR
    api.mpv.stop = real_stop
    api._resolve_playback_url = AsyncMock(side_effect=BackendError("Synthetic failed resolver"))
    with pytest.raises(BackendError):
        await api._play_url(C)
    assert api.mpv.idle and not api.mpv.entries and not api._native_output_uncertain


@pytest.mark.asyncio
async def test_gain_failure_while_append_ack_pending_cannot_adopt_around_cleanup(tmp_path):
    api = make_api(tmp_path)
    api.mpv.release_append = asyncio.Event()
    loading = asyncio.create_task(start(api))
    await api.mpv.append_started.wait()
    api.mpv.volume = AsyncMock(side_effect=BackendError("Synthetic gain failure"))
    with pytest.raises(BackendError):
        await api._set_confirmed_gain(volume=15)
    api.mpv.advance()
    api.mpv.release_append.set()
    with pytest.raises(BackendError):
        await loading
    await asyncio.gather(*tuple(api._native_cleanup_tasks))
    assert api.mpv.idle and not api.mpv.entries
    assert api.state.queue.index == 1 and api.state.phase == PlaybackPhase.ERROR
    assert "Gain confirmation failed" in api.state.last_error


@pytest.mark.asyncio
async def test_seek_waits_for_audio_when_successor_was_paused_during_open(tmp_path):
    api = make_api(tmp_path)
    await start(api)
    api.mpv.advance()
    decoded = asyncio.Event()
    snapshot = api.mpv.snapshot
    async def opening():
        value = await snapshot()
        if not decoded.is_set():
            value.update({"track-list": [], "core-idle": True, "playback-time": None})
        return value
    api.mpv.snapshot = opening
    await api._transport_command("pause")
    assert api.state.queue.index == 2 and api._native_waiting_for_audio
    seeking = asyncio.create_task(api._seek_command(7))
    await asyncio.sleep(.01)
    assert mutations(api, "seek") == []
    decoded.set()
    await seeking
    assert mutations(api, "seek") == [7] and api.state.phase == PlaybackPhase.PAUSED


@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", ["rejection", "cancellation"])
async def test_cancelled_retired_native_cleanup_preserves_output_fence(tmp_path, first_failure):
    api = make_api(tmp_path)
    await start(api)
    stop_entered, cleanup_entered = asyncio.Event(), asyncio.Event()
    original_stop, original_snapshot = api.mpv.stop, api.mpv.snapshot

    async def unconfirmed_stop():
        stop_entered.set()
        if first_failure == "cancellation":
            await asyncio.Future()
        raise BackendError("Synthetic rejected Stop")

    async def blocked_cleanup_snapshot():
        cleanup_entered.set()
        await asyncio.Future()

    api.mpv.stop, api.mpv.snapshot = unconfirmed_stop, blocked_cleanup_snapshot
    stopping = asyncio.create_task(api._transport_command("stop"))
    await stop_entered.wait()
    if first_failure == "cancellation":
        stopping.cancel()
    await cleanup_entered.wait()
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert api.state.phase == PlaybackPhase.ERROR and api.state.intent == "stop"
    assert api._native_output_uncertain
    assert api._native_stage is None and api._native_current_id is None
    # The old armed successor may run while confirmation is unavailable.
    api.mpv.advance()
    assert api.mpv.url == B
    api.mpv.stop, api.mpv.snapshot = original_stop, original_snapshot
    api._resolve_playback_url = AsyncMock(side_effect=BackendError("Synthetic resolver failure"))
    with pytest.raises(BackendError):
        await api._play_url(C)
    assert api.mpv.idle and not api.mpv.entries
    assert not api._native_output_uncertain
    assert mutations(api, "load") == [A]


@pytest.mark.asyncio
async def test_newer_failed_resolution_during_unconfirmed_native_stop_is_fenced(tmp_path):
    api = make_api(tmp_path)
    await start(api)
    entered, release = asyncio.Event(), asyncio.Event()
    original_stop = api.mpv.stop
    first = True
    resolved_while_active = []

    async def stop():
        nonlocal first
        if first:
            first = False
            entered.set()
            await release.wait()
            raise BackendError("Synthetic lost Stop acknowledgement")
        await original_stop()

    async def resolve(url):
        resolved_while_active.append(not api.mpv.idle)
        raise BackendError("Synthetic new-source resolver failure")

    api.mpv.stop, api._resolve_playback_url = stop, resolve
    stopping = asyncio.create_task(api._transport_command("stop"))
    await entered.wait()
    playing = asyncio.create_task(api._play_url(C))
    await asyncio.sleep(0)
    release.set()
    for task in (stopping, playing):
        with pytest.raises(BackendError):
            await task
    assert resolved_while_active == [False]
    assert api.mpv.idle and not api.mpv.entries
    assert not api._native_output_uncertain
    assert api.state.phase == PlaybackPhase.ERROR
    assert mutations(api, "load") == [A]


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", ["http", "soap", "internal"])
async def test_pause_superseding_uncertain_native_stop_reestablishes_play_ownership(tmp_path, resume):
    from fake_linkplay.http_api import ParsedCommand

    api = make_api(tmp_path)
    await start(api)
    original_stop = api.mpv.stop
    entered, release = asyncio.Event(), asyncio.Event()

    async def rejected_stop():
        entered.set()
        await release.wait()
        raise BackendError("Synthetic rejected Stop")

    api.mpv.stop = rejected_stop
    stopping = asyncio.create_task(api._transport_command("stop"))
    await entered.wait()
    pausing = asyncio.create_task(api._transport_command("pause"))
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(BackendError):
        await stopping
    await pausing
    assert api.state.phase == PlaybackPhase.PAUSED and api._native_output_uncertain
    api.mpv.stop = original_stop
    if resume == "http":
        await api.execute(ParsedCommand("resume"), "127.0.0.1")
    elif resume == "soap":
        await api._execute_soap_side_effect("Play", "<Play/>")
    else:
        await api._transport_command("resume")
    assert not api._native_output_uncertain
    assert mutations(api, "load") == [A, A]
    api.mpv.advance()
    await api.sync_mpv_state()
    assert api.state.current_url == api.mpv.url == B
    assert api.state.queue.index == 2 and api.state.phase == PlaybackPhase.PLAYING
