"""UNIT/MOCK confirmed seek, owner races and range-coordinate regressions.

HTTP/SOAP responses use the real handlers; all audio observations are synthetic.
The optional native suites separately exercise real mpv/null controls.
"""
import asyncio
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi, ParsedCommand
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.queue import PlaybackQueue, QueueItem
from fake_linkplay.soap import SERVICE_TYPES, SOAP_NS, SoapFault
from fake_linkplay.state import PlaybackPhase, RendererState


SOURCE = "https://media.example.test/file-128k.mp3"


class SeekBackend:
    def __init__(self):
        self.url = SOURCE
        self.position = 50
        self.duration = 300
        self.paused = False
        self.idle = False
        self.eof = False
        self.failure = False
        self.ignore_seek = False
        self.reads = 0
        self.calls = []
        self.after_seek = None
        self.snapshot_hook = None
        self.seek_hook = None

    async def snapshot(self):
        self.reads += 1
        if self.snapshot_hook:
            await self.snapshot_hook(self.reads)
        if self.failure:
            return {"backend-error": True}
        return {"path": self.url, "playback-time": self.position, "duration": self.duration,
                "idle-active": self.idle, "eof-reached": self.eof, "pause": self.paused,
                "core-idle": self.idle or self.paused, "paused-for-cache": False,
                "track-list": [{"type": "audio", "selected": True}] if not self.idle else []}

    async def seek(self, target):
        self.calls.append(("seek", target))
        if self.seek_hook:
            await self.seek_hook(target)
        if not self.ignore_seek:
            self.position = target
        if self.after_seek:
            self.after_seek()
        return {"error": "success"}

    async def loadfile(self, url, options=None):
        self.calls.append(("load", url, deepcopy(options)))
        self.url, self.idle, self.eof, self.paused = url, False, False, False
        self.position = int((options or {}).get("start", 0))
        return {"error": "success"}

    async def pause(self):
        self.calls.append(("pause",))
        self.paused = True

    async def stop(self):
        self.calls.append(("stop",))
        self.idle, self.url = True, ""

    async def resume(self):
        self.calls.append(("resume",))
        self.paused = False


@pytest.fixture
def api(tmp_path):
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path))
    result = HttpApi(cfg, RendererState.from_config(cfg), SeekBackend())
    result.state.queue = PlaybackQueue(items=[QueueItem(url=SOURCE, duration=300)], index=1)
    result._activate_queue_item()
    result.state.mark_playing(SOURCE)
    result.state.backend_url = result.state.backend_original_url = SOURCE
    result.state.resolved_url = SOURCE
    result.state.curpos = result.mpv.position
    result._seek_timeout = 0.05
    result._seek_poll_interval = 0.002
    result._wait_for_mpv_audio_ready = AsyncMock(return_value=True)
    return result


async def invoke(api, surface, target=10):
    if surface == "http":
        return await api.execute(ParsedCommand("seek", (str(target),)), "192.0.2.1")
    if surface == "previous":
        return await api._execute_soap_side_effect("Previous", "<Previous/>")
    hours, remainder = divmod(target, 3600)
    minutes, seconds = divmod(remainder, 60)
    return await api._execute_soap_side_effect(
        "Seek", f"<Seek><Unit>REL_TIME</Unit><Target>{hours:02}:{minutes:02}:{seconds:02}</Target></Seek>")


def http_request(target):
    return SimpleNamespace(query={"command": f"setPlayerCmd:seek:{target}"}, remote="192.0.2.1",
                           host="renderer.test", path_qs="/httpapi.asp", headers={})


def soap_request(target):
    namespace = SERVICE_TYPES["AVTransport"]
    args = f"<Unit>REL_TIME</Unit><Target>00:00:{target:02}</Target>"
    body = (f'<s:Envelope xmlns:s="{SOAP_NS}"><s:Body>'
            f'<u:Seek xmlns:u="{namespace}">{args}</u:Seek></s:Body></s:Envelope>')
    return SimpleNamespace(headers={"SOAPACTION": f'"{namespace}#Seek"'},
                           match_info={"service": "AVTransport"}, text=AsyncMock(return_value=body))


@pytest.mark.parametrize("surface", ["http", "soap", "previous"])
@pytest.mark.parametrize("paused", [False, True])
async def test_success_is_actual_position_and_preserves_pause(api, surface, paused):
    target = 0 if surface == "previous" else 30
    api.mpv.paused = paused
    if paused:
        api.state.mark_pause()
    # Seek readback is intentionally different to the requested integer. This
    # catches optimistic echoing while allowing the documented decoder window.
    api.mpv.after_seek = lambda: setattr(api.mpv, "position", max(0, target - 0.5))
    await invoke(api, surface, target)
    assert api.state.curpos == int(api.mpv.position)
    assert api.state.phase == (PlaybackPhase.PAUSED if paused else PlaybackPhase.PLAYING)
    assert api._pending_seek_target is None
    assert not api._seek_tasks


@pytest.mark.parametrize("surface", ["http", "soap"])
@pytest.mark.parametrize("initial,target", [(100, 10), (10, 100)])
@pytest.mark.parametrize("paused", [False, True])
async def test_acknowledged_ignored_forward_and_backward_seek_fails(api, surface, initial, target, paused):
    api.mpv.position = api.state.curpos = initial
    api.mpv.ignore_seek = True
    api.mpv.paused = paused
    if paused:
        api.state.mark_pause()
    with pytest.raises(BackendError, match="Seek position not confirmed"):
        await invoke(api, surface, target)
    assert api.mpv.calls == [("seek", target)]
    assert api.state.curpos == initial
    assert api.state.phase == PlaybackPhase.ERROR
    assert api._pending_seek_target is None
    assert not api._seek_tasks


async def test_previous_also_rejects_acknowledged_ignored_restart(api):
    api.mpv.ignore_seek = True
    with pytest.raises(BackendError, match="not confirmed"):
        await invoke(api, "previous")
    assert api.state.curpos == 50
    assert api.mpv.calls == [("seek", 0)]


@pytest.mark.parametrize("surface", ["http", "soap"])
async def test_wire_success_waits_for_readback_and_failure_is_not_success(api, surface):
    api._seek_timeout = 1
    started, release = asyncio.Event(), asyncio.Event()
    async def hold_confirmation(read):
        if read == 2:
            started.set()
            await release.wait()
    api.mpv.snapshot_hook = hold_confirmation
    api.mpv.ignore_seek = True
    call = api.handle_httpapi(http_request(10)) if surface == "http" else api.soap(soap_request(10))
    task = asyncio.create_task(call)
    await asyncio.wait_for(started.wait(), 1)
    assert not task.done()
    assert api.state.curpos == 50
    assert api.state.phase == PlaybackPhase.BUFFERING
    api.mpv.position = 10
    release.set()
    response = await task
    assert response.status == 200
    assert api.state.curpos == 10
    api._seek_timeout = 0.02
    response = (await api.handle_httpapi(http_request(30)) if surface == "http"
                else await api.soap(soap_request(30)))
    assert response.status == (502 if surface == "http" else 500)
    if surface == "soap":
        assert "<errorCode>501</errorCode>" in response.text
    assert api.state.curpos == 10


@pytest.mark.parametrize("surface", ["http", "soap"])
@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("terminal", ["eof", "idle", "error"])
async def test_preflight_or_confirmation_terminal_state_cannot_seek_or_resurrect(api, surface, when, terminal):
    def fail():
        setattr(api.mpv, {"eof": "eof", "idle": "idle", "error": "failure"}[terminal], True)
        if terminal == "idle":
            api.mpv.url = ""
    if when == "before":
        fail()
    else:
        api.mpv.after_seek = fail
    with pytest.raises(BackendError):
        await invoke(api, surface, 10)
    assert api.mpv.calls == ([] if when == "before" else [("seek", 10)])
    assert api.state.phase == (PlaybackPhase.ERROR if terminal == "error" else PlaybackPhase.STOPPED)
    assert api.state.curpos != 10
    assert not api._seek_tasks
    assert api._pending_seek_target is None


@pytest.mark.parametrize("terminal", [PlaybackPhase.STOPPED, PlaybackPhase.ERROR])
async def test_terminal_state_observed_during_snapshot_is_not_overwritten(api, terminal):
    async def terminate_during_confirmation(read):
        if read == 2:
            api.state.phase = terminal
            api.state.last_error = "original error" if terminal == PlaybackPhase.ERROR else ""
    api.mpv.snapshot_hook = terminate_during_confirmation
    with pytest.raises(BackendError):
        await invoke(api, "soap", 10)
    assert api.state.phase == terminal
    assert api.state.last_error != "Seek position not confirmed"
    assert api._pending_seek_target is None


@pytest.mark.parametrize("surface", ["http", "soap", "previous"])
@pytest.mark.parametrize("duration", [0, 300])
async def test_known_stations_reject_before_state_or_backend_mutation(api, surface, duration):
    api.state.queue.current.stream_type = "station"
    api.state.queue.current.duration = duration
    api._activate_queue_item()
    before = asdict(api.state)
    with pytest.raises(SoapFault, match="Seek Mode Not Supported"):
        await invoke(api, surface, 10)
    assert asdict(api.state) == before
    assert not api.mpv.calls and api.mpv.reads == 0


@pytest.mark.parametrize("surface", ["http", "soap"])
@pytest.mark.parametrize("ignored", [False, True])
async def test_unknown_duration_files_depend_on_actual_confirmation(api, surface, ignored):
    api.state.queue.current.duration = api.queue_duration_sec = api.state.totlen = 0
    api.mpv.duration = None
    api.mpv.ignore_seek = ignored
    if ignored:
        with pytest.raises(BackendError, match="not confirmed"):
            await invoke(api, surface, 1000)
        assert api.state.curpos == 50
    else:
        await invoke(api, surface, 1000)
        assert api.state.curpos == 1000
    assert api.mpv.calls == [("seek", 1000)]


@pytest.mark.parametrize("surface", ["http", "soap"])
@pytest.mark.parametrize("target", [-1, 300, 301])
async def test_absolute_targets_outside_known_duration_are_not_clamped(api, surface, target):
    before = asdict(api.state)
    with pytest.raises(SoapFault):
        await invoke(api, surface, target)
    assert asdict(api.state) == before
    assert not api.mpv.calls and api.mpv.reads == 0


@pytest.mark.parametrize("target", [-1, 300, "nan", "1.5", "+5", ""])
async def test_http_illegal_targets_return_failure(api, target):
    response = await api.handle_httpapi(http_request(target))
    assert response.status == 400
    assert not api.mpv.calls


@pytest.mark.parametrize("action,position,target", [("SeekBackward", 10, 0), ("SeekForward", 290, 299)])
async def test_relative_buttons_clamp_to_valid_endpoints_using_actual_position(api, action, position, target):
    api.mpv.position = position
    api.state.curpos = 150  # Prove the relative target uses fresh backend readback.
    await api._execute_soap_side_effect(action, f"<{action}/>")
    assert api.mpv.calls == [("seek", target)]
    assert api.state.curpos == target


@pytest.mark.parametrize("surface", ["http", "soap"])
async def test_backend_discovered_duration_also_rejects_out_of_bounds(api, surface):
    api.state.queue.current.duration = api.queue_duration_sec = api.state.totlen = 0
    with pytest.raises(SoapFault) as exc:
        await invoke(api, surface, 300)
    assert exc.value.code == 711
    assert not api.mpv.calls


@pytest.mark.parametrize("path", ["", "https://other.example.test/wrong.mp3"])
async def test_source_identity_is_required_after_ack(api, path):
    api.mpv.after_seek = lambda: setattr(api.mpv, "url", path)
    with pytest.raises(BackendError, match="not confirmed"):
        await invoke(api, "http", 10)
    assert api.state.curpos == 50
    assert api.state.phase == PlaybackPhase.ERROR


@pytest.mark.parametrize("stage", ["preflight", "mutation", "confirmation"])
@pytest.mark.parametrize("newer", ["stop", "pause", "source", "seek"])
async def test_newer_owner_command_wins_every_seek_wait(api, stage, newer):
    api._seek_timeout = 2
    started, release = asyncio.Event(), asyncio.Event()
    if stage == "mutation":
        async def block_seek(target):
            if target == 10:
                started.set()
                await release.wait()
        api.mpv.seek_hook = block_seek
    else:
        async def block_snapshot(read):
            if read == (1 if stage == "preflight" else 2):
                started.set()
                await release.wait()
        api.mpv.snapshot_hook = block_snapshot
    old = asyncio.create_task(invoke(api, "soap", 10))
    await asyncio.wait_for(started.wait(), 1)
    if newer == "source":
        operation = api._play_url("https://media.example.test/new.mp3")
    elif newer == "seek":
        operation = invoke(api, "http", 200)
    else:
        operation = api.execute(ParsedCommand(newer), "192.0.2.1")
    new = asyncio.create_task(operation)
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(BackendError, match="superseded"):
        await old
    await new
    if newer == "stop":
        assert api.state.phase == PlaybackPhase.STOPPED and api.state.curpos == 0
    elif newer == "pause":
        assert api.state.phase == PlaybackPhase.PAUSED and api.mpv.paused
    elif newer == "source":
        assert api.state.current_url == api.state.backend_url == api.mpv.url == "https://media.example.test/new.mp3"
        assert api.state.phase == PlaybackPhase.PLAYING and api.state.curpos == 0
    else:
        assert api.state.curpos == api.mpv.position == 200
        assert api.state.phase == PlaybackPhase.PLAYING
    assert api.state.last_error == ""
    assert not api._seek_tasks
    assert api._pending_seek_target is None


@pytest.mark.parametrize("stage", ["preflight", "confirmation"])
async def test_slow_snapshot_is_bounded_and_releases_backend_lock(api, stage):
    async def block_snapshot(read):
        if read == (1 if stage == "preflight" else 2):
            await asyncio.Future()
    api.mpv.snapshot_hook = block_snapshot
    with pytest.raises((BackendError, TimeoutError)):
        await asyncio.wait_for(invoke(api, "http", 10), 1)
    assert not api._backend_lock.locked()
    assert not api._seek_tasks
    assert api._pending_seek_target is None
    assert api.state.curpos == 50


@pytest.mark.parametrize("stage", ["preflight", "mutation", "confirmation"])
async def test_final_listener_cleanup_cancels_every_owned_seek_stage(api, stage):
    api._seek_timeout = 10
    api._monitor_interval = 10
    first, second = api._state_monitor_context(None), api._state_monitor_context(None)
    await anext(first)
    await anext(second)
    started = asyncio.Event()
    if stage == "mutation":
        async def block_seek(target):
            started.set()
            await asyncio.Future()
        api.mpv.seek_hook = block_seek
    else:
        async def block_snapshot(read):
            if read == (1 if stage == "preflight" else 2):
                started.set()
                await asyncio.Future()
        api.mpv.snapshot_hook = block_snapshot
    pending = asyncio.create_task(invoke(api, "http", 10))
    await asyncio.wait_for(started.wait(), 1)
    owned = list(api._seek_tasks)
    assert len(owned) == 1
    await first.aclose()
    assert not owned[0].done()
    await asyncio.wait_for(second.aclose(), 1)
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert all(task.done() for task in owned)
    assert not api._seek_tasks
    assert api._pending_seek_target is None
    assert api._state_monitor_task is None
    assert not api._backend_lock.locked()


@pytest.mark.parametrize("surface", ["http", "soap"])
@pytest.mark.parametrize("paused", [False, True])
async def test_opt_in_range_seek_translates_120_then_130_then_reloads_original_110(api, surface, paused):
    api.cfg.range_seek_reload = True
    api._bitrate_bps_from_url_or_headers = lambda *_: 128000
    api.mpv.position = api.state.curpos = 0
    api.mpv.paused = paused
    if paused:
        api.state.mark_pause()
    await invoke(api, surface, 120)
    first_load = api.mpv.calls[0]
    assert first_load[0] == "load" and parse_qs(urlsplit(first_load[1]).query)["start"] == ["120"]
    assert api.state.curpos == 120 and api._playback_offset_sec == 120
    await invoke(api, surface, 130)
    assert ("seek", 10) in api.mpv.calls
    assert api.state.curpos == 130 and api.mpv.position == 10
    await invoke(api, surface, 110)
    loads = [call for call in api.mpv.calls if call[0] == "load"]
    assert loads[-1] == ("load", SOURCE, {"start": "110"})
    assert api._playback_offset_sec == 0
    assert api.state.curpos == api.mpv.position == 110
    assert api.state.backend_url == api.state.backend_original_url == SOURCE
    assert api.state.totlen == 300
    assert api.state.phase == (PlaybackPhase.PAUSED if paused else PlaybackPhase.PLAYING)
    assert api.mpv.paused is paused


async def test_range_workaround_is_off_by_default(api):
    assert api.cfg.range_seek_reload is False
    await invoke(api, "soap", 120)
    assert api.mpv.calls == [("seek", 120)]
    assert api._playback_offset_sec == 0


@pytest.mark.parametrize("raw", [100, -3, float("nan"), float("inf"), None, "10", True])
def test_readiness_never_accepts_outside_two_sided_finite_window(api, raw):
    snapshot = {"playback-time": raw, "pause": False, "idle-active": False,
                "track-list": [{"type": "audio", "selected": True}]}
    assert not api._snapshot_is_seek_ready(snapshot, 10)


@pytest.mark.parametrize("flags", [{"eof-reached": True}, {"paused-for-cache": True},
                                    {"backend-error": True}, {"idle-active": True},
                                    {"pause": True}, {"cache-buffering-state": 40}])
def test_target_position_alone_is_not_confirmation(api, flags):
    snapshot = {"playback-time": 10, "pause": False, "idle-active": False,
                "track-list": [{"type": "audio", "selected": True}], **flags}
    assert not api._snapshot_is_seek_ready(snapshot, 10)


async def test_range_segment_duration_keeps_original_absolute_bounds(api):
    api.cfg.range_seek_reload = True
    api._bitrate_bps_from_url_or_headers = lambda *_: 128000
    api.state.queue.current.duration = api.queue_duration_sec = 0
    api.mpv.position = api.state.curpos = 0
    original_load = api.mpv.loadfile
    async def load_segment(url, options=None):
        await original_load(url, options)
        start = int(parse_qs(urlsplit(url).query).get("start", [0])[0])
        api.mpv.duration = 300 - start
    api.mpv.loadfile = load_segment
    await invoke(api, "http", 120)
    assert api.mpv.duration == 180
    assert api.state.totlen == 300
    await invoke(api, "soap", 130)
    assert api.state.totlen == 300
    await invoke(api, "http", 250)
    assert api.state.curpos == 250 and api.state.totlen == 300
    await invoke(api, "soap", 110)
    assert api.state.curpos == 110 and api.state.totlen == 300


async def test_waiting_for_preflight_backend_lock_is_bounded(api):
    await api._backend_lock.acquire()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(invoke(api, "http", 10), 1)
        assert not api.mpv.calls and api.mpv.reads == 0
        assert not api._seek_tasks
    finally:
        api._backend_lock.release()


async def test_unacknowledged_seek_mutation_is_bounded_and_marks_uncertainty(api):
    async def block_seek(target):
        await asyncio.Future()
    api.mpv.seek_hook = block_seek
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(invoke(api, "http", 10), 1)
    assert api.state.phase == PlaybackPhase.ERROR
    assert "before confirmation" in api.state.last_error
    assert not api._backend_lock.locked()
    assert not api._seek_tasks


@pytest.mark.parametrize("field,value", [("backend_url", "https://other.example.test/file.mp3"),
                                         ("_playback_offset_sec", 30)])
async def test_backend_identity_or_offset_change_during_readback_is_fenced(api, field, value):
    async def change_identity(read):
        if read == 2:
            setattr(api if field.startswith("_") else api.state, field, value)
    api.mpv.snapshot_hook = change_identity
    with pytest.raises(BackendError):
        await invoke(api, "http", 10)
    assert api.state.curpos == 50


@pytest.mark.parametrize("status,headers,requested", [
    (200, {"Content-Range": "bytes 20-29/30"}, "bytes=20-"),
    (206, {}, "bytes=20-"),
    (206, {"Content-Range": "bytes 0-9/30"}, "bytes=20-"),
    (206, {"Content-Range": "bytes 20-30/30"}, "bytes=20-"),
    (206, {"Content-Range": "bytes 20-19/30"}, "bytes=20-"),
    (206, {"Content-Range": "bytes 20-29/*"}, "bytes=20-"),
    (206, {"Content-Range": "bytes 20-29/30", "Content-Encoding": "gzip"}, "bytes=20-"),
    (206, {"Content-Range": "bytes 20-29/30", "Content-Length": "30"}, "bytes=20-"),
    (206, {"Content-Range": "bytes 20-29/30"}, "bytes=20-25"),
])
def test_shifted_range_requires_matching_partial_identity_bytes(api, status, headers, requested):
    assert api._seek_proxy_content_range(status, headers, requested, 20) is None


@pytest.mark.parametrize("requested,content_range,result", [
    ("bytes=20-", "bytes 20-29/30", "bytes 0-9/10"),
    ("bytes=22-25", "bytes 22-25/30", "bytes 2-5/10"),
])
def test_shifted_range_response_uses_segment_coordinates(api, requested, content_range, result):
    assert api._seek_proxy_content_range(206, {"Content-Range": content_range}, requested, 20) == result


@pytest.mark.parametrize("relative", [False, True])
async def test_fractional_backend_duration_keeps_last_valid_whole_second(api, relative):
    api.state.queue.current.duration = api.queue_duration_sec = 0
    api.state.totlen = 300  # Integer display must not truncate seekability.
    api.mpv.duration = 300.8
    api.mpv.position = 280
    if relative:
        await api._execute_soap_side_effect("SeekForward", "<SeekForward/>")
    else:
        await invoke(api, "http", 300)
    assert api.mpv.calls == [("seek", 300)]
    assert api.state.curpos == 300


async def test_seek_acknowledgement_loss_cannot_leave_false_confirmed_playback(api):
    async def apply_then_fail(target):
        api.mpv.position = target
        raise BackendError("Synthetic acknowledgement lost")
    api.mpv.seek = apply_then_fail
    with pytest.raises(BackendError):
        await invoke(api, "http", 10)
    assert api.state.phase == PlaybackPhase.ERROR
    assert api.state.curpos == 50
    assert api.state.last_error == "Seek position not confirmed"
    assert not api._seek_tasks


@pytest.mark.parametrize("resume", ["http", "soap"])
async def test_uncertain_range_load_recovers_via_public_resume_by_reloading_source(api, resume):
    api.cfg.range_seek_reload = True
    api._bitrate_bps_from_url_or_headers = lambda *_: 128000
    api.mpv.position = 0
    await invoke(api, "http", 120)
    original_load = api.mpv.loadfile
    async def load_then_lose_ack(url, options=None):
        await original_load(url, options)
        raise BackendError("Synthetic acknowledgement lost after applied load")
    api.mpv.loadfile = load_then_lose_ack
    with pytest.raises(BackendError):
        await invoke(api, "http", 110)
    assert api.state.phase == PlaybackPhase.ERROR
    assert api.mpv.url == SOURCE
    # Backend identity is deliberately treated as uncertain, not fabricated.
    api.mpv.loadfile = original_load
    if resume == "http":
        await api.execute(ParsedCommand("resume"), "192.0.2.1")
    else:
        await api._execute_soap_side_effect("Play", "<Play/>")
    assert api.state.backend_url == api.mpv.url == SOURCE
    assert api._playback_offset_sec == 0
    assert api.state.phase == PlaybackPhase.PLAYING
    api.cfg.range_seek_reload = False
    await invoke(api, "http", 130)
    assert api.state.curpos == api.mpv.position == 130


@pytest.mark.parametrize("surface", ["http", "soap"])
async def test_elapsed_time_cannot_confirm_ignored_two_second_backward_seek(api, surface):
    api.mpv.position = api.state.curpos = 12
    api.mpv.ignore_seek = True
    api._seek_timeout = 1.1  # The former elapsed upper window falsely passed after1s.
    api._seek_poll_interval = 0.01
    with pytest.raises(BackendError, match="not confirmed"):
        await invoke(api, surface, 10)
    assert api.state.curpos == api.mpv.position == 12
    assert api.state.phase == PlaybackPhase.ERROR


async def test_same_url_different_native_playlist_entry_cannot_confirm_seek(api):
    snapshot = api.mpv.snapshot
    async def with_native_entry():
        result = await snapshot()
        result["playlist"] = [{"filename": SOURCE, "id": 1 if not api.mpv.calls else 2,
                               "current": True, "playing": True}]
        return result
    api.mpv.snapshot = with_native_entry
    with pytest.raises(BackendError, match="Source changed"):
        await invoke(api, "http", 10)
    assert api.mpv.calls == [("seek", 10)]
    assert api.state.curpos == 50
    assert api.state.phase == PlaybackPhase.ERROR
