"""UNIT/MOCK state-generation scenarios; no actual sound or Android execution."""
import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET

import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi, ParsedCommand
from fake_linkplay.mpv_backend import BackendError, MpvBackend
from fake_linkplay.state import PlaybackPhase, RendererState
from fake_linkplay.upnp_description import avtransport_response_fields


class StatefulBackend:
    def __init__(self):
        self.url = ""
        self.paused = False
        self.idle = True
        self.position = 0
        self.buffering = False
        self.fail = False
        self.calls = []
        self.load_started = asyncio.Event()
        self.release_load = None

    async def loadfile(self, url, options=None):
        self.calls.append(("load", url))
        self.load_started.set()
        if self.release_load:
            await self.release_load.wait()
        if self.fail:
            raise BackendError("synthetic load failure")
        self.url, self.idle, self.paused = url, False, False
        return {"error": "success"}

    async def pause(self):
        self.calls.append(("pause", None))
        self.paused = True

    async def resume(self):
        self.calls.append(("resume", None))
        self.paused = False

    async def stop(self):
        self.calls.append(("stop", None))
        self.idle = True
        self.url = ""

    async def seek(self, seconds):
        self.calls.append(("seek", seconds))
        self.position = seconds

    async def snapshot(self):
        if self.fail:
            return {"backend-error": True}
        return {"path": self.url, "idle-active": self.idle, "pause": self.paused,
                "paused-for-cache": self.buffering, "playback-time": self.position,
                "duration": 30, "media-title": self.url.rsplit("/", 1)[-1],
                "track-list": [] if self.idle else [{"type": "audio", "selected": True}]}


@pytest.fixture
def api(tmp_path):
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path))
    result = HttpApi(cfg, RendererState.from_config(cfg), StatefulBackend())
    result._wait_for_mpv_audio_ready = AsyncMock(return_value=True)
    return result


def assert_consistent(api, phase, wire, upnp):
    assert api.state.phase == phase
    assert api.state.as_player_status()["status"] == wire
    assert api.state.as_status_ex("192.0.2.1")["status"] == wire
    assert f"<CurrentTransportState>{upnp}</CurrentTransportState>" in avtransport_response_fields("GetTransportInfo", api.state)
    event = ET.fromstring(api._event_body("AVTransport"))
    last_change = next(node.text for node in event.iter() if node.tag.rsplit("}", 1)[-1] == "LastChange")
    assert f'TransportState val="{upnp}"' in last_change


@pytest.mark.asyncio
async def test_play_pause_resume_stop_share_one_state(api):
    await api._play_url("https://media.example.test/one.wav")
    assert_consistent(api, PlaybackPhase.PLAYING, "play", "PLAYING")
    await api.execute(ParsedCommand("pause"), "192.0.2.1")
    assert_consistent(api, PlaybackPhase.PAUSED, "pause", "PAUSED_PLAYBACK")
    await api._execute_soap_side_effect("Play", "<Play/>")
    assert_consistent(api, PlaybackPhase.PLAYING, "play", "PLAYING")
    assert [c[0] for c in api.mpv.calls].count("load") == 1
    await api.execute(ParsedCommand("stop"), "192.0.2.1")
    assert_consistent(api, PlaybackPhase.STOPPED, "stop", "STOPPED")


@pytest.mark.asyncio
async def test_eof_backend_exit_and_buffering_are_observed(api):
    await api._play_url("https://media.example.test/one.wav")
    api.mpv.buffering = True
    await api.sync_mpv_state()
    assert_consistent(api, PlaybackPhase.BUFFERING, "transition", "TRANSITIONING")
    api.mpv.buffering = False
    await api.sync_mpv_state()
    assert_consistent(api, PlaybackPhase.PLAYING, "play", "PLAYING")
    api.mpv.idle = True
    await api.sync_mpv_state()
    assert_consistent(api, PlaybackPhase.STOPPED, "stop", "STOPPED")
    api.mpv.fail = True
    await api.sync_mpv_state()
    assert_consistent(api, PlaybackPhase.ERROR, "stop", "STOPPED")
    assert "ERROR_OCCURRED" in avtransport_response_fields("GetInfoEx", api.state)
    assert "ERROR_OCCURRED" in api._event_body("AVTransport")


@pytest.mark.asyncio
async def test_failed_load_never_publishes_playing(api):
    api.mpv.fail = True
    with pytest.raises(BackendError):
        await api._play_url("https://media.example.test/one.wav")
    assert_consistent(api, PlaybackPhase.ERROR, "stop", "STOPPED")


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_command", ["pause", "stop"])
async def test_owner_command_cancels_delayed_resolution(api, owner_command):
    started, release = asyncio.Event(), asyncio.Event()
    async def resolve(url):
        started.set()
        await release.wait()
        return url
    api._resolve_playback_url = resolve
    task = asyncio.create_task(api._play_url("https://media.example.test/one.wav"))
    await started.wait()
    assert api.state.phase == PlaybackPhase.LOADING
    await api.execute(ParsedCommand(owner_command), "192.0.2.1")
    release.set()
    with pytest.raises(BackendError):
        await task
    assert not any(c[0] == "load" for c in api.mpv.calls)
    assert api.state.phase == (PlaybackPhase.PAUSED if owner_command == "pause" else PlaybackPhase.STOPPED)


@pytest.mark.asyncio
async def test_stop_while_load_inflight_runs_after_load_and_wins(api):
    api.mpv.release_load = asyncio.Event()
    load = asyncio.create_task(api._play_url("https://media.example.test/one.wav"))
    await api.mpv.load_started.wait()
    stop = asyncio.create_task(api.execute(ParsedCommand("stop"), "192.0.2.1"))
    await asyncio.sleep(0)
    api.mpv.release_load.set()
    with pytest.raises(BackendError):
        await load
    assert await stop == "OK"
    assert api.mpv.idle
    assert_consistent(api, PlaybackPhase.STOPPED, "stop", "STOPPED")


@pytest.mark.asyncio
async def test_new_source_wins_over_old_resolver(api):
    started, release = asyncio.Event(), asyncio.Event()
    first, second = "https://media.example.test/one.wav", "https://media.example.test/two.wav"
    async def resolve(url):
        if url == first:
            started.set()
            await release.wait()
        return url
    api._resolve_playback_url = resolve
    old = asyncio.create_task(api._play_url(first))
    await started.wait()
    await api._play_url(second)
    release.set()
    with pytest.raises(BackendError):
        await old
    assert api.state.current_url == second
    assert api.mpv.url == second
    assert api.mpv.calls == [("load", second)]


@pytest.mark.asyncio
async def test_stale_snapshot_cannot_change_new_generation(api):
    await api._play_url("https://media.example.test/one.wav")
    original_snapshot = api.mpv.snapshot
    started, release = asyncio.Event(), asyncio.Event()
    async def delayed_snapshot():
        result = await original_snapshot()
        result["media-title"], result["playback-time"] = "Old title", 23
        started.set()
        await release.wait()
        return result
    api.mpv.snapshot = delayed_snapshot
    old = asyncio.create_task(api.sync_mpv_state())
    await started.wait()
    api._accept_playback_command("stop")
    api.state.mark_stop()
    api.state.title = "New title"
    release.set()
    await old
    assert api.state.title == "New title"
    assert api.state.curpos == 0
    assert api.state.phase == PlaybackPhase.STOPPED


@pytest.mark.asyncio
async def test_wrong_source_snapshot_cannot_overwrite_current_item(api):
    await api._play_url("https://media.example.test/one.wav")
    before = asdict(api.state)
    api.mpv.snapshot = AsyncMock(return_value={"path": "https://media.example.test/old.wav", "media-title": "Wrong", "playback-time": 25, "pause": True})
    await api.sync_mpv_state()
    assert asdict(api.state) == before


@pytest.mark.asyncio
async def test_late_readiness_cannot_override_pause(api):
    started, release = asyncio.Event(), asyncio.Event()
    async def readiness(*args):
        started.set()
        await release.wait()
        return True
    api._wait_for_mpv_audio_ready = readiness
    load = asyncio.create_task(api._play_url("https://media.example.test/one.wav"))
    await started.wait()
    assert api.state.phase == PlaybackPhase.BUFFERING
    await api.execute(ParsedCommand("pause"), "192.0.2.1")
    release.set()
    with pytest.raises(BackendError):
        await load
    assert_consistent(api, PlaybackPhase.PAUSED, "pause", "PAUSED_PLAYBACK")


@pytest.mark.asyncio
async def test_backend_snapshot_preserves_transport_failure():
    backend = MpvBackend("/unused", False)
    backend.command = AsyncMock(return_value={"error": "socket unavailable"})
    assert await backend.snapshot() == {"backend-error": True}


@pytest.mark.asyncio
async def test_monitor_observes_eof_without_client_poll_and_cleans_up(api):
    api._monitor_interval = 0.005
    first = api._state_monitor_context(None)
    second = api._state_monitor_context(None)
    await anext(first)
    task = api._state_monitor_task
    await anext(second)
    assert api._state_monitor_task is task
    try:
        await api._play_url("https://media.example.test/one.wav")
        api.mpv.idle = True
        async with asyncio.timeout(1):
            while api.state.phase != PlaybackPhase.STOPPED:
                await asyncio.sleep(0.005)
        await first.aclose()
        assert api._state_monitor_task is task
    finally:
        await first.aclose()
        await second.aclose()
    assert task.done()
    assert api._state_monitor_task is None


@pytest.mark.asyncio
async def test_stop_then_http_resume_reloads_selected_media(api):
    url = "https://media.example.test/one.wav"
    await api._play_url(url)
    await api.execute(ParsedCommand("stop"), "192.0.2.1")
    assert api.state.backend_url == ""
    await api.execute(ParsedCommand("resume"), "192.0.2.1")
    assert api.mpv.url == url
    assert [c[0] for c in api.mpv.calls] == ["load", "stop", "load"]
    assert api.state.phase == PlaybackPhase.PLAYING


@pytest.mark.asyncio
async def test_pause_during_replacement_load_tracks_actual_backend_identity(api):
    first, second = "https://media.example.test/one.wav", "https://media.example.test/two.wav"
    await api._play_url(first)
    api.mpv.load_started.clear()
    api.mpv.release_load = asyncio.Event()
    load = asyncio.create_task(api._play_url(second))
    await api.mpv.load_started.wait()
    pause = asyncio.create_task(api.execute(ParsedCommand("pause"), "192.0.2.1"))
    await asyncio.sleep(0)
    api.mpv.release_load.set()
    with pytest.raises(BackendError):
        await load
    await pause
    assert api.state.backend_url == second
    assert api.state.current_url == second
    assert api.mpv.url == second
    assert_consistent(api, PlaybackPhase.PAUSED, "pause", "PAUSED_PLAYBACK")
    api.mpv.position = 9
    await api.sync_mpv_state()
    assert api.state.curpos == 9


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["error", "eof"])
async def test_seek_completion_cannot_resurrect_terminal_state(api, terminal):
    await api._play_url("https://media.example.test/one.wav")
    original_seek = api.mpv.seek
    async def seek_then_end(target):
        await original_seek(target)
        if terminal == "error":
            api.mpv.fail = True
        else:
            api.mpv.idle = True
    api.mpv.seek = seek_then_end
    with pytest.raises(BackendError):
        await api._seek_command(7)
    assert api.state.phase == (PlaybackPhase.ERROR if terminal == "error" else PlaybackPhase.STOPPED)


@pytest.mark.asyncio
async def test_seek_reload_updates_backend_identity_for_later_snapshots(api):
    await api._play_url("https://media.example.test/one.wav")
    api._should_reload_for_large_seek = lambda *_: True
    api._seek_audio_proxy_url = lambda *_: "http://127.0.0.1/fixture-proxy"
    api.queue_duration_sec = 300
    await api._execute_soap_side_effect("Seek", "<Seek><Unit>REL_TIME</Unit><Target>00:02:00</Target></Seek>")
    assert api.state.backend_url == "http://127.0.0.1/fixture-proxy"
    api._pending_seek_target = None
    api.mpv.position = 10
    await api.sync_mpv_state()
    assert api.state.curpos == 130
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_stop_ack_without_actual_stop_is_rejected(api):
    await api._play_url("https://media.example.test/one.wav")
    api.mpv.stop = AsyncMock(return_value={"error": "success"})
    with pytest.raises(BackendError, match="did not stop"):
        await api.execute(ParsedCommand("stop"), "192.0.2.1")
    assert api.state.phase == PlaybackPhase.ERROR
    assert api.state.intent == "stop"
    assert "silence not confirmed" in api.state.last_error
    assert not api.mpv.idle


@pytest.mark.asyncio
async def test_seek_during_resolution_rejects_without_cancelling_load(api):
    started, release = asyncio.Event(), asyncio.Event()
    async def resolve(url):
        started.set()
        await release.wait()
        return url
    api._resolve_playback_url = resolve
    load = asyncio.create_task(api._play_url("https://media.example.test/two.wav"))
    await started.wait()
    generation = api.state.generation
    from fake_linkplay.soap import SoapFault
    with pytest.raises(SoapFault):
        await api.execute(ParsedCommand("seek", ("20",)), "192.0.2.1")
    assert api.state.generation == generation
    release.set()
    await load
    assert api.state.phase == PlaybackPhase.PLAYING


@pytest.mark.asyncio
async def test_previous_cannot_overwrite_newer_seek(api):
    await api._play_url("https://media.example.test/one.wav")
    original_seek = api.mpv.seek
    started, release = asyncio.Event(), asyncio.Event()
    async def delayed_seek(seconds):
        if seconds == 0:
            started.set()
            await release.wait()
        return await original_seek(seconds)
    api.mpv.seek = delayed_seek
    previous = asyncio.create_task(api._execute_soap_side_effect("Previous", "<Previous/>"))
    await started.wait()
    seek = asyncio.create_task(api.execute(ParsedCommand("seek", ("20",)), "192.0.2.1"))
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(BackendError):
        await previous
    await seek
    assert api.state.curpos == api.mpv.position == 20


@pytest.mark.asyncio
async def test_old_source_snapshot_cannot_turn_rejected_replacement_into_play(api):
    first, second = "https://media.example.test/one.wav", "https://media.example.test/two.wav"
    await api._play_url(first)
    api.mpv.loadfile = AsyncMock(side_effect=BackendError("synthetic refusal"))
    with pytest.raises(BackendError):
        await api._play_url(second)
    before = asdict(api.state)
    await api.sync_mpv_state()
    assert asdict(api.state) == before
    assert api.state.phase == PlaybackPhase.ERROR
    assert api.state.current_url == second


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_interface", ["http", "soap"])
async def test_pause_during_new_resolution_resumes_selected_source(api, resume_interface):
    first, second = "https://media.example.test/one.wav", "https://media.example.test/two.wav"
    await api._play_url(first)
    started, release = asyncio.Event(), asyncio.Event()
    async def resolve(url):
        started.set()
        await release.wait()
        return url
    api._resolve_playback_url = resolve
    load = asyncio.create_task(api._play_url(second))
    await started.wait()
    await api.execute(ParsedCommand("pause"), "192.0.2.1")
    assert api.mpv.idle
    assert api.state.current_url == second
    assert api.state.phase == PlaybackPhase.PAUSED
    release.set()
    with pytest.raises(BackendError):
        await load
    if resume_interface == "http":
        await api.execute(ParsedCommand("resume"), "192.0.2.1")
    else:
        api.queue_url = second
        await api._execute_soap_side_effect("Play", "<Play/>")
    assert api.mpv.url == api.state.backend_original_url == second
    assert api.state.phase == PlaybackPhase.PLAYING


@pytest.mark.asyncio
async def test_old_snapshot_exception_does_not_mark_new_generation_error(api):
    await api._play_url("https://media.example.test/one.wav")
    started, release = asyncio.Event(), asyncio.Event()
    async def failure():
        started.set()
        await release.wait()
        raise OSError("synthetic delayed failure")
    api.mpv.snapshot = failure
    old = asyncio.create_task(api.sync_mpv_state())
    await started.wait()
    api._accept_playback_command("stop")
    api.state.mark_stop()
    release.set()
    await old
    assert api.state.phase == PlaybackPhase.STOPPED


@pytest.mark.asyncio
async def test_replacement_load_resets_actual_offset_even_when_pause_wins(api):
    await api._play_url("https://media.example.test/one.wav")
    api._playback_offset_sec = 120
    api.mpv.load_started.clear()
    api.mpv.release_load = asyncio.Event()
    load = asyncio.create_task(api._play_url("https://media.example.test/two.wav"))
    await api.mpv.load_started.wait()
    pause = asyncio.create_task(api.execute(ParsedCommand("pause"), "192.0.2.1"))
    await asyncio.sleep(0)
    api.mpv.release_load.set()
    with pytest.raises(BackendError):
        await load
    await pause
    api.mpv.position = 5
    await api.sync_mpv_state()
    assert api.state.curpos == 5


@pytest.mark.asyncio
async def test_paused_reload_seek_preserves_real_backend_pause(api):
    await api._play_url("https://media.example.test/one.wav")
    await api.execute(ParsedCommand("pause"), "192.0.2.1")
    api._should_reload_for_large_seek = lambda *_: True
    api._seek_audio_proxy_url = lambda *_: "http://127.0.0.1/fixture-proxy"
    api.queue_duration_sec = 300
    await api._execute_soap_side_effect("Seek", "<Seek><Unit>REL_TIME</Unit><Target>00:02:00</Target></Seek>")
    await asyncio.sleep(0)
    assert api.mpv.paused
    assert api.state.phase == PlaybackPhase.PAUSED
    assert api.state.curpos == 120


def test_paused_seek_readiness_does_not_require_advancing_audio(api):
    snapshot = {"pause": True, "idle-active": False, "core-idle": True, "playback-time": 10}
    assert api._snapshot_is_seek_ready(snapshot, 10, intent="pause")


@pytest.mark.asyncio
async def test_resume_during_replacement_resolution_cannot_resume_old_source(api):
    first, second = "https://media.example.test/one.wav", "https://media.example.test/two.wav"
    await api._play_url(first)
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def resolve(url):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
        return url
    api._resolve_playback_url = resolve
    old = asyncio.create_task(api._play_url(second))
    await started.wait()
    await api.execute(ParsedCommand("resume"), "192.0.2.1")
    release.set()
    with pytest.raises(BackendError):
        await old
    assert api.mpv.url == api.state.current_url == api.state.backend_original_url == second
    assert api.state.phase == PlaybackPhase.PLAYING


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [PlaybackPhase.STOPPED, PlaybackPhase.ERROR])
async def test_unready_seek_does_not_replace_terminal_observation(api, terminal):
    from fake_linkplay.soap import SoapFault
    api.state.phase = terminal
    api.state.last_error = "original failure" if terminal == PlaybackPhase.ERROR else ""
    with pytest.raises(SoapFault):
        await api._seek_command(29)
    assert api.state.phase == terminal
    assert api.state.last_error != "Seek position not confirmed"
    assert api._pending_seek_target is None
    assert not api.mpv.calls


@pytest.mark.asyncio
async def test_failed_repause_after_seek_reload_reports_actual_backend_error(api):
    await api._play_url("https://media.example.test/one.wav")
    await api.execute(ParsedCommand("pause"), "192.0.2.1")
    api._should_reload_for_large_seek = lambda *_: True
    api._seek_audio_proxy_url = lambda *_: "http://127.0.0.1/fixture-proxy"
    api.queue_duration_sec = 300
    api.mpv.pause = AsyncMock(side_effect=BackendError("synthetic pause failure"))
    with pytest.raises(BackendError):
        await api._execute_soap_side_effect("Seek", "<Seek><Unit>REL_TIME</Unit><Target>00:02:00</Target></Seek>")
    assert api.state.phase == PlaybackPhase.ERROR
    assert api.state.backend_url == api.mpv.url == "http://127.0.0.1/fixture-proxy"
    assert api._playback_offset_sec == 120


@pytest.mark.asyncio
async def test_resume_while_stop_confirmation_pending_loads_selected_source(api):
    url = "https://media.example.test/one.wav"
    await api._play_url(url)
    started, release = asyncio.Event(), asyncio.Event()
    original_snapshot = api.mpv.snapshot
    calls = 0
    async def delayed_snapshot():
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
        return await original_snapshot()
    api.mpv.snapshot = delayed_snapshot
    stop = asyncio.create_task(api.execute(ParsedCommand("stop"), "192.0.2.1"))
    await started.wait()
    resume = asyncio.create_task(api.execute(ParsedCommand("resume"), "192.0.2.1"))
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(BackendError):
        await stop
    await resume
    assert api.mpv.url == url
    assert api.state.phase == PlaybackPhase.PLAYING
