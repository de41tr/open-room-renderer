"""Synthetic bounded lease and real loopback admission; no provider/audio proof."""
import asyncio
import json
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi, ParsedCommand
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.state import RendererState
from test_soap_faults import FakeBackend
from fake_linkplay.tidal_queue import TidalQueueSelection, parse_tidal_queue
from test_tidal_queue_playback import configured, explicit_context
from test_web_ui import serving, state as web_state
from test_web_tidal import Manager as SyntheticLogin


PUBLIC_FIELDS = {
    "capability_version", "available", "state", "lease_id", "remaining_seconds",
    "volume_percent", "volume_confirmed", "error",
    "playback_position_seconds", "observed_progress_seconds", "decoded_audio_observed",
}
TERMINAL = {"stopped", "expired", "superseded", "failed"}
ERRORS = {
    None, "test_unavailable", "test_busy", "source_not_idle", "volume_unconfirmed",
    "playback_failed", "volume_limit_exceeded", "source_changed", "stop_unconfirmed",
    "lease_mismatch", "test_closed",
}
START = {
    "track_id": "42", "duration_seconds": 60, "volume_percent": 1,
    "confirm_experimental_playback": True,
}
PREFIX = "/orr/v1/tidal/test"


class LeaseBackend(FakeBackend):
    """In-memory native identity/gain; never starts mpv or opens a media URL."""

    def __init__(self):
        super().__init__()
        self.actual_volume = 77
        self.gain_confirmed = True
        self.idle = True
        self.entry_id = 0
        self.position = 0
        self.audio_info = None
        self.unreadable_volume = False
        self.ignore_volume_write = False
        self.stop_error = False

    async def volume(self, value):
        result = await self._call("volume", value)
        if not self.ignore_volume_write:
            self.actual_volume = value
        return result

    async def get_property(self, name):
        if name == "volume":
            if self.unreadable_volume:
                raise BackendError("SYNTHETIC_PRIVATE_READBACK_FAILURE")
            return self.actual_volume
        if name == "mute":
            return False
        return (await self.snapshot()).get(name)

    async def checked_command(self, args):
        if len(args) == 2 and args[0] == "get_property":
            return {"error": "success", "data": await self.get_property(args[1])}
        raise AssertionError(f"Unexpected raw backend mutation: {args!r}")

    async def loadfile(self, url, options=None, *, start_paused=False, current=None):
        if current is not None and not await current():
            raise BackendError("Playback command superseded before load")
        result = await super().loadfile(url, options, start_paused=start_paused)
        self.idle = False
        self.entry_id += 1
        return {**result, "data": {"playlist_entry_id": self.entry_id}}

    async def pause(self):
        self.paused = True
        return await self._call("pause")

    async def seek(self, value):
        self.position = value
        return await self._call("seek", value)

    async def stop(self):
        result = await self._call("stop")
        if self.stop_error:
            raise BackendError("SYNTHETIC_PRIVATE_STOP_FAILURE")
        self.idle, self.url, self.paused = True, "", False
        return result

    async def snapshot(self):
        return {
            "idle-active": self.idle, "pause": self.paused,
            "playback-time": self.position, "duration": 180, "path": self.url,
            "audio-info": self.audio_info,
            "playlist-path": None, "core-idle": self.idle,
            "eof-reached": False, "paused-for-cache": False,
            "track-list": [] if self.idle else [{"type": "audio", "selected": True}],
            "playlist": [] if self.idle else [{
                "id": self.entry_id, "filename": self.url,
                "current": True, "playing": True,
            }],
        }


@pytest.fixture
async def api(tmp_path):
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path),
        advertise_ip="127.0.0.1", default_volume=77,
    )
    result = HttpApi(cfg, RendererState.from_config(cfg), LeaseBackend())
    result._wait_for_mpv_audio_ready = AsyncMock(return_value=True)
    resolver = await configured(result, tmp_path)
    resolver.session_available = lambda: resolver.store.load() == resolver.record
    result.tidal_login = SyntheticLogin()
    result.tidal_test._poll_interval = .005
    try:
        yield result
    finally:
        await result.tidal_test.close()
        await result._eventing.close()


def assert_public(value):
    assert set(value) == PUBLIC_FIELDS
    assert value["capability_version"] == 1
    assert type(value["available"]) is bool
    assert value["state"] in {"idle", "starting", "playing", "stopping", *TERMINAL}
    assert value["lease_id"] is None or isinstance(value["lease_id"], str)
    assert type(value["remaining_seconds"]) is int and value["remaining_seconds"] >= 0
    assert type(value["volume_confirmed"]) is bool
    assert value["volume_percent"] == (1 if value["volume_confirmed"] else None)
    assert value["error"] in ERRORS
    assert type(value["decoded_audio_observed"]) is bool
    assert value["playback_position_seconds"] is None or value["playback_position_seconds"] >= 0
    assert value["observed_progress_seconds"] >= 0
    if value["state"] in TERMINAL:
        assert value["remaining_seconds"] == 0
    public = json.dumps(value)
    for private in ("SYNTHETIC", "synthetic-access", "synthetic-refresh", "example.test",
                    "api.tidal.com", "Synthetic track", "Synthetic artist", "Synthetic album"):
        assert private not in public


async def wait_state(api, expected):
    expected = {expected} if isinstance(expected, str) else set(expected)
    async with asyncio.timeout(5):
        while True:
            value = await api.tidal_test.status()
            assert_public(value)
            if value["state"] in expected:
                return value
            await asyncio.sleep(.005)


async def start_playing(api):
    value, code = await api.tidal_test.start("42", 60)
    assert code == 202
    assert_public(value)
    assert value["volume_confirmed"] is True and value["volume_percent"] == 1
    playing = await wait_state(api, "playing")
    assert playing["lease_id"] == value["lease_id"]
    return value["lease_id"]


async def wait_resolution(api):
    async with asyncio.timeout(2):
        while not api.tidal_playback.calls:
            await asyncio.sleep(0)


async def test_idle_gain_is_reduced_before_load_and_never_restored(api):
    initial = await api.tidal_test.status()
    assert_public(initial)
    assert initial["available"] and initial["state"] == "idle"
    assert initial["lease_id"] is None
    lease = await start_playing(api)
    assert api.mpv.calls[0] == ("volume", 1)
    assert api.mpv.actual_volume == 1
    assert api.state.queue.count == api.state.queue.index == 1
    assert api.state.queue.current.id == "42"
    assert api.state.queue.current._tidal_test_lease is api.tidal_test
    assert [name for name, _ in api.mpv.calls].count("load") == 1
    stopped, code = await api.tidal_test.stop(lease)
    assert code == 200 and stopped["state"] == "stopped"
    assert_public(stopped)
    assert api.mpv.idle and api.mpv.actual_volume == 1
    assert api.state.volume == 1
    assert ("volume", 77) not in api.mpv.calls
    calls = list(api.mpv.calls)
    repeated, code = await api.tidal_test.stop(lease)
    assert code == 200 and repeated["state"] == "stopped"
    assert api.mpv.calls == calls


@pytest.mark.parametrize("foreign", ["renderer_playing", "renderer_paused", "native_playing"])
async def test_unrelated_active_source_is_refused_without_mutation(api, foreign):
    if foreign.startswith("renderer"):
        api.state.mark_playing("https://foreign.example.test/already-playing")
        if foreign == "renderer_paused":
            api.state.mark_pause()
    else:
        api.mpv.idle = False
        api.mpv.url, api.mpv.entry_id = "https://foreign.example.test/native", 99
    before = asdict(api.state)
    value, code = await api.tidal_test.start("42", 60)
    assert code == 409 and value["error"] == "source_not_idle"
    assert_public(value)
    assert not api.mpv.calls and not api.tidal_playback.calls
    assert api.mpv.actual_volume == 77 and asdict(api.state) == before


async def test_initial_raw_gain_confirmation_failure_never_loads_or_restores(api):
    api.mpv.ignore_volume_write = True
    value, code = await api.tidal_test.start("42", 60)
    assert code == 503 and value["state"] == "failed"
    assert value["error"] == "volume_unconfirmed"
    assert_public(value)
    assert not value["volume_confirmed"]
    assert not any(name in {"load", "resume"} for name, _ in api.mpv.calls)
    assert not api.tidal_playback.calls
    assert [call for call in api.mpv.calls if call[0] == "volume"] == [("volume", 1)]


async def test_deadline_during_delayed_resolution_fences_late_candidate(api):
    now = [100.0]
    api.tidal_test._clock = lambda: now[0]
    release = api.tidal_playback.wait = asyncio.Event()
    original = api.tidal_playback.resolve_track

    async def late_candidate(*args, **kwargs):
        try:
            return await original(*args, **kwargs)
        except asyncio.CancelledError:
            # Simulate a provider operation that completes after cancellation.
            await release.wait()
            return await original(*args, **kwargs)

    api.tidal_playback.resolve_track = late_candidate
    admitted, code = await api.tidal_test.start("42", 1)
    assert code == 202
    await wait_resolution(api)
    now[0] = 102.0
    await asyncio.sleep(.02)
    release.set()
    final = await wait_state(api, TERMINAL)
    assert final["state"] == "expired" and final["remaining_seconds"] == 0
    assert final["lease_id"] == admitted["lease_id"]
    await asyncio.sleep(.02)
    assert not any(name in {"load", "resume"} for name, _ in api.mpv.calls)
    assert api.mpv.idle


async def test_deadline_during_backend_lock_wait_prevents_load(api):
    now = [100.0]
    api.tidal_test._clock = lambda: now[0]
    release = api.tidal_playback.wait = asyncio.Event()
    _, code = await api.tidal_test.start("42", 1)
    assert code == 202
    await wait_resolution(api)
    await api._backend_lock.acquire()
    try:
        release.set()
        await asyncio.sleep(.02)
        assert not any(name == "load" for name, _ in api.mpv.calls)
        now[0] = 102.0
        await asyncio.sleep(.02)
    finally:
        api._backend_lock.release()
    final = await wait_state(api, TERMINAL)
    assert final["state"] == "expired"
    assert not any(name in {"load", "resume"} for name, _ in api.mpv.calls)
    assert api.mpv.idle


async def test_managed_volume_increase_is_rejected_before_backend_write(api):
    await start_playing(api)
    before = list(api.mpv.calls)
    with pytest.raises(BackendError):
        await api.execute(ParsedCommand("volume", ("2",)), "127.0.0.1")
    assert api.mpv.calls == before and api.mpv.actual_volume == 1


@pytest.mark.parametrize("unsafe", ["high", "unreadable"])
async def test_watchdog_stops_owned_source_on_unsafe_raw_gain(api, unsafe):
    await start_playing(api)
    if unsafe == "high":
        api.mpv.actual_volume = 2
    else:
        api.mpv.unreadable_volume = True
    value = await wait_state(api, TERMINAL)
    assert value["state"] == "failed"
    assert value["error"] == ("volume_limit_exceeded" if unsafe == "high" else "volume_unconfirmed")
    assert not value["volume_confirmed"] and value["volume_percent"] is None
    assert ("stop", None) in api.mpv.calls and api.mpv.idle


@pytest.mark.parametrize("new_owner", ["renderer", "native_other_path", "native_same_path"])
async def test_supersession_never_stops_the_new_source(api, new_owner):
    lease = await start_playing(api)
    before = list(api.mpv.calls)
    if new_owner == "renderer":
        api._accept_playback_command("play")
        api.state.mark_playing("https://foreign.example.test/new-owner")
        api.mpv.url = api.state.current_url
    elif new_owner == "native_other_path":
        api.mpv.url = "https://foreign.example.test/native-owner"
    api.mpv.entry_id += 99
    value = await wait_state(api, "superseded")
    assert value["error"] == "source_changed"
    assert not api.mpv.idle and api.mpv.calls == before
    terminal, code = await api.tidal_test.stop(lease)
    assert code == 200 and terminal["state"] == "superseded"
    assert not api.mpv.idle and api.mpv.calls == before


async def test_mismatched_stop_cannot_stop_the_current_test(api):
    lease = await start_playing(api)
    before = list(api.mpv.calls)
    value, code = await api.tidal_test.stop("wrong-lease")
    assert code == 409 and value["error"] == "lease_mismatch"
    assert_public(value)
    assert value["lease_id"] == lease
    assert api.mpv.calls == before and not api.mpv.idle
    assert (await api.tidal_test.status())["state"] == "playing"


async def test_failed_backend_stop_remains_unconfirmed_and_blocks_readmission(api):
    lease = await start_playing(api)
    api.mpv.stop_error = True
    value, code = await api.tidal_test.stop(lease)
    assert code == 503 and value["state"] == "failed"
    assert value["error"] == "stop_unconfirmed" and not value["available"]
    assert_public(value)
    assert not api.mpv.idle
    status = await api.tidal_test.status()
    assert status["error"] == "stop_unconfirmed" and not status["available"]
    before = list(api.mpv.calls)
    repeat, code = await api.tidal_test.stop(lease)
    assert code == 503 and repeat["error"] == "stop_unconfirmed"
    # An explicit retry may retry Stop only for the still-owned native entry.
    assert api.mpv.calls[len(before):] == [("stop", None)]
    before = list(api.mpv.calls)
    _, code = await api.tidal_test.start("42", 60)
    assert code == 409 and api.mpv.calls == before


async def test_close_stops_owned_output_and_rejects_new_leases(api):
    await start_playing(api)
    await api.tidal_test.close()
    assert api.mpv.idle and ("stop", None) in api.mpv.calls
    value, code = await api.tidal_test.start("42", 60)
    assert code == 409 and value["error"] == "test_closed"
    assert not value["available"]


async def test_loopback_start_status_stop_use_existing_csrf_and_private_schema(api):
    async with serving(api) as (session, base):
        boot = await web_state(session, base)
        headers = {"Origin": base, "X-ORR-CSRF": boot["csrf_token"]}
        async with session.get(base + PREFIX + "/status") as response:
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert_public(await response.json())
        async with session.post(base + PREFIX + "/start", json=START, headers=headers) as response:
            assert response.status == 202
            admitted = await response.json()
            assert_public(admitted)
        # The creating HTTP request has ended, but renderer ownership persists.
        await wait_state(api, "playing")
        async with session.get(base + PREFIX + "/status") as response:
            assert response.status == 200
            value = await response.json()
            assert_public(value)
            assert value["state"] == "playing" and not value["available"]
        async with session.post(base + PREFIX + "/stop", json={
            "lease_id": admitted["lease_id"], "stop": True,
        }, headers=headers) as response:
            assert response.status == 200
            value = await response.json()
            assert_public(value)
            assert value["state"] == "stopped"
        assert api.mpv.idle


@pytest.mark.parametrize("change", [
    {"track_id": 42}, {"track_id": "0"}, {"track_id": "01"},
    {"track_id": ["42", "43"]}, {"tracks": ["42", "43"]},
    {"track_id": "1" * 20}, {"track_id": "٤٢"},
    {"duration_seconds": True}, {"duration_seconds": 0},
    {"duration_seconds": 121}, {"duration_seconds": 1.0},
    {"volume_percent": True}, {"volume_percent": 2}, {"volume_percent": 1.0},
    {"confirm_experimental_playback": False}, {"confirm_experimental_playback": 1},
    {"url": "https://untrusted.example.test/"},
])
async def test_http_start_rejects_nonexact_or_unsafe_fields_without_mutation(api, change):
    async with serving(api) as (session, base):
        boot = await web_state(session, base)
        headers = {"Origin": base, "X-ORR-CSRF": boot["csrf_token"]}
        async with session.post(base + PREFIX + "/start", json={**START, **change}, headers=headers) as response:
            assert response.status == 400
        assert not api.mpv.calls and not api.tidal_playback.calls


@pytest.mark.parametrize("payload", [
    {}, {"lease_id": "x"}, {"stop": True},
    {"lease_id": "x", "stop": 1}, {"lease_id": "x", "stop": False},
    {"lease_id": 1, "stop": True}, {"lease_id": "x", "stop": True, "extra": 1},
])
async def test_http_stop_rejects_nonexact_fields_without_mutation(api, payload):
    async with serving(api) as (session, base):
        boot = await web_state(session, base)
        headers = {"Origin": base, "X-ORR-CSRF": boot["csrf_token"]}
        async with session.post(base + PREFIX + "/stop", json=payload, headers=headers) as response:
            assert response.status == 400
        assert not api.mpv.calls and not api.tidal_playback.calls


@pytest.mark.parametrize("action,payload", [
    ("start", '{"track_id":"42","track_id":"43","duration_seconds":60,"volume_percent":1,"confirm_experimental_playback":true}'),
    ("start", '{"track_id":"42","duration_seconds":60,"volume_percent":1}'),
    ("start", '[]'),
    ("stop", '{"lease_id":"x","stop":true,"stop":true}'),
])
async def test_http_duplicate_or_missing_keys_never_reach_manager(api, action, payload):
    async with serving(api) as (session, base):
        boot = await web_state(session, base)
        headers = {"Origin": base, "X-ORR-CSRF": boot["csrf_token"], "Content-Type": "application/json"}
        async with session.post(base + PREFIX + "/" + action, data=payload, headers=headers) as response:
            assert response.status == 400
        assert not api.mpv.calls and not api.tidal_playback.calls


@pytest.mark.parametrize("admission", ["missing_origin", "foreign_origin", "missing_csrf", "wrong_csrf"])
async def test_http_browser_admission_rejects_untrusted_start_and_stop(api, admission):
    async with serving(api) as (session, base):
        boot = await web_state(session, base)
        headers = {"Origin": base, "X-ORR-CSRF": boot["csrf_token"]}
        if admission == "missing_origin":
            del headers["Origin"]
        elif admission == "foreign_origin":
            headers["Origin"] = "https://foreign.example.test"
        elif admission == "missing_csrf":
            del headers["X-ORR-CSRF"]
        else:
            headers["X-ORR-CSRF"] = "wrong-token"
        for action, payload in (("start", START), ("stop", {"lease_id": "x", "stop": True})):
            async with session.post(base + PREFIX + "/" + action, json=payload, headers=headers) as response:
                assert response.status == 403
        assert not api.mpv.calls and not api.tidal_playback.calls


async def test_http_wrong_method_status_body_and_host_are_rejected(api):
    async with serving(api) as (session, base):
        for action in ("start", "stop"):
            async with session.get(base + PREFIX + "/" + action) as response:
                assert response.status == 405
        async with session.post(base + PREFIX + "/status") as response:
            assert response.status == 405
        async with session.get(base + PREFIX + "/status", data="{}") as response:
            assert response.status == 400
        async with session.get(base + PREFIX + "/status", headers={"Host": "foreign.example.test"}) as response:
            assert response.status == 403
        assert not api.mpv.calls and not api.tidal_playback.calls


@pytest.mark.parametrize("selection", ["single_track", "explicit_queue", "header_page"])
async def test_foreign_pending_resolution_refuses_admission_without_gain_change(api, selection):
    api.tidal_playback.wait = asyncio.Event()
    if selection == "header_page":
        async def pending_page(url):
            api.tidal_playback.calls.append(("page", url))
            await api.tidal_playback.wait.wait()
            raise AssertionError("The pending page must be cancelled")
        api.tidal_playback.resolve_page = pending_page
        play = api._play_tidal_selection(TidalQueueSelection(
            "Page", "https://api.tidal.com/v1/albums/123/tracks?limit=3"), require_shared=True)
    else:
        play = (api._play_tidal_track("43") if selection == "single_track" else
                api._play_tidal_selection(parse_tidal_queue(explicit_context()), require_shared=True))
    foreign = asyncio.create_task(play)
    try:
        await wait_resolution(api)
        value, code = await api.tidal_test.start("42", 60)
        assert code == 409 and value["error"] == "source_not_idle"
        assert not api.mpv.calls and api.mpv.actual_volume == 77
    finally:
        foreign.cancel()
        await asyncio.gather(foreign, return_exceptions=True)
    assert not api._tidal_pending_resolutions


async def test_progress_and_decoding_come_only_from_owned_native_observations(api):
    await start_playing(api)
    async with asyncio.timeout(2):
        while (await api.tidal_test.status())["playback_position_seconds"] is None:
            await asyncio.sleep(.005)
    value = await api.tidal_test.status()
    assert value["observed_progress_seconds"] == 0
    assert not value["decoded_audio_observed"]
    api.mpv.position = 2.5
    api.mpv.audio_info = {"entry_id": api.mpv.entry_id + 99, "codec": "flac"}
    async with asyncio.timeout(2):
        while (await api.tidal_test.status())["playback_position_seconds"] != 2.5:
            await asyncio.sleep(.005)
    value = await api.tidal_test.status()
    assert value["observed_progress_seconds"] == 2.5
    assert not value["decoded_audio_observed"]
    api.mpv.audio_info["entry_id"] = api.mpv.entry_id
    async with asyncio.timeout(2):
        while not (await api.tidal_test.status())["decoded_audio_observed"]:
            await asyncio.sleep(.005)
    assert_public(await api.tidal_test.status())


@pytest.mark.parametrize("boundary", ["loadfile", "resume"])
async def test_deadline_during_backend_preparation_fences_final_mutation(api, boundary):
    now = [100.0]
    api.tidal_test._clock = lambda: now[0]
    entered, release = asyncio.Event(), asyncio.Event()
    original = getattr(api.mpv, boundary)
    if boundary == "loadfile":
        async def preparation(url, options=None, *, start_paused=False, current=None):
            entered.set()
            await release.wait()
            return await original(url, options, start_paused=start_paused, current=current)
    else:
        async def preparation(*, current=None):
            entered.set()
            await release.wait()
            return await original(current=current)
    setattr(api.mpv, boundary, preparation)
    _, code = await api.tidal_test.start("42", 1)
    assert code == 202
    await asyncio.wait_for(entered.wait(), 2)
    now[0] = 102.0
    await asyncio.sleep(.02)
    release.set()
    final = await wait_state(api, TERMINAL)
    assert final["state"] == "expired"
    forbidden = {"load", "resume"} if boundary == "loadfile" else {"resume"}
    assert not any(name in forbidden for name, _ in api.mpv.calls)
    assert api.mpv.idle


@pytest.mark.parametrize("boundary", ["load_gain", "load_pause_ack", "resume_gain"])
async def test_real_backend_rechecks_deadline_after_internal_preparation(boundary):
    from test_backend_gain import make_backend

    backend, peer = make_backend(initial_volume=1)
    now = [100.0]
    entered, release = asyncio.Event(), asyncio.Event()
    delayed = (["set_property", "pause", True] if boundary == "load_pause_ack"
               else ["get_property", "mute"])

    async def command(args, timeout=5.0):
        result = await peer.command(args, timeout)
        if args == delayed:
            entered.set()
            await release.wait()
        return result

    async def current():
        return now[0] < 101.0

    backend.command = command
    if boundary == "resume_gain":
        task = asyncio.create_task(backend.resume(current=current))
    else:
        task = asyncio.create_task(backend.loadfile(
            "https://synthetic.example.test/never-opened", start_paused=True, current=current))
    await asyncio.wait_for(entered.wait(), 2)
    now[0] = 102.0
    release.set()
    with pytest.raises(BackendError):
        await task
    assert not peer.loads
    assert not any(call[0] == "loadfile" for call in peer.calls)
    assert ["set_property", "pause", False] not in peer.calls


async def test_preflight_snapshot_fences_competing_renderer_commands(api):
    entered, release = asyncio.Event(), asyncio.Event()
    original = api.mpv.snapshot
    first = True

    async def delayed_snapshot():
        nonlocal first
        if first:
            first = False
            entered.set()
            await release.wait()
        return await original()

    api.mpv.snapshot = delayed_snapshot
    starting = asyncio.create_task(api.tidal_test.start("42", 60))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(BackendError):
            api._accept_playback_command("play")
        with pytest.raises(BackendError):
            await api.execute(ParsedCommand("volume", ("80",)), "127.0.0.1")
        assert not api.mpv.calls and api.mpv.actual_volume == 77
        release.set()
        value, code = await starting
        assert code == 202 and value["volume_percent"] == 1
    finally:
        release.set()
        await asyncio.gather(starting, return_exceptions=True)


@pytest.mark.parametrize("ack", [None, {}, {"playlist_entry_id": None}])
async def test_missing_load_ack_uses_paused_native_root_readback(api, ack):
    original = api.mpv.loadfile

    async def without_identity(url, options=None, *, start_paused=False, current=None):
        await original(url, options, start_paused=start_paused, current=current)
        return {"error": "success", "data": ack}

    api.mpv.loadfile = without_identity
    lease = await start_playing(api)
    assert not api.mpv.paused and not api.mpv.idle
    stopped, code = await api.tidal_test.stop(lease)
    assert code == 200 and stopped["state"] == "stopped"
    assert api.mpv.idle


@pytest.mark.parametrize("malformed", ["missing_playlist", "missing_id", "boolean_id", "string_id", "no_active", "backend_error"])
async def test_missing_ack_with_unprovable_native_root_never_unpauses_or_claims_supersession(api, malformed):
    original_load, original_snapshot = api.mpv.loadfile, api.mpv.snapshot
    loaded = False
    api.tidal_test._io_timeout = .04

    async def without_identity(url, options=None, *, start_paused=False, current=None):
        nonlocal loaded
        await original_load(url, options, start_paused=start_paused, current=current)
        loaded = True
        return {"error": "success"}

    async def uncertain_root():
        snap = await original_snapshot()
        if loaded:
            if malformed == "missing_playlist":
                snap.pop("playlist")
            elif malformed == "missing_id":
                snap["playlist"][0].pop("id")
            elif malformed == "boolean_id":
                snap["playlist"][0]["id"] = True
            elif malformed == "string_id":
                snap["playlist"][0]["id"] = "1"
            elif malformed == "no_active":
                snap["playlist"][0].update(current=False, playing=False)
            else:
                return {"backend-error": True}
        return snap

    api.mpv.loadfile, api.mpv.snapshot = without_identity, uncertain_root
    value, code = await api.tidal_test.start("42", 60)
    assert code == 202
    final = await wait_state(api, TERMINAL)
    assert final["state"] == "failed" and final["error"] == "stop_unconfirmed"
    assert not final["available"]
    assert api.mpv.paused and not api.mpv.idle
    assert not any(name in {"resume", "stop"} for name, _ in api.mpv.calls)
    stopped, code = await api.tidal_test.stop(value["lease_id"])
    assert code == 503 and stopped["error"] == "stop_unconfirmed"


@pytest.mark.parametrize("delayed", ["backend_lock", "snapshot"])
async def test_stop_timeout_after_new_renderer_owner_preserves_new_state(api, delayed):
    api.tidal_test._poll_interval = 1
    lease = await start_playing(api)
    api.tidal_test._io_timeout = .04
    entered, release = asyncio.Event(), asyncio.Event()
    if delayed == "backend_lock":
        await api._backend_lock.acquire()
    else:
        original = api.mpv.snapshot

        async def slow_snapshot():
            entered.set()
            await release.wait()
            return await original()

        api.mpv.snapshot = slow_snapshot
    stopping = asyncio.create_task(api.tidal_test.stop(lease))
    try:
        if delayed == "snapshot":
            await asyncio.wait_for(entered.wait(), 2)
        else:
            await wait_state(api, "stopping")
        api._accept_playback_command("play")
        api.state.mark_playing("https://foreign.example.test/newer-renderer")
        api.mpv.url, api.mpv.entry_id = api.state.current_url, 99
        before = (api.state.generation, api.state.phase, api.state.current_url,
                  api.state.last_error, api.state.intent)
        value, code = await asyncio.wait_for(stopping, 2)
        assert code == 200 and value["state"] == "superseded"
        assert (api.state.generation, api.state.phase, api.state.current_url,
                api.state.last_error, api.state.intent) == before
        assert not any(name == "stop" for name, _ in api.mpv.calls)
        assert not api.mpv.idle
    finally:
        release.set()
        if delayed == "backend_lock":
            api._backend_lock.release()
        await asyncio.gather(stopping, return_exceptions=True)


@pytest.mark.parametrize("missing", ["reported_unavailable", "cleared_session"])
async def test_unavailable_session_refuses_before_gain_mutation(api, missing):
    if missing == "reported_unavailable":
        api.tidal_playback.session_available = lambda: False
    else:
        api.tidal_playback.store.clear()
    status = await api.tidal_test.status()
    assert not status["available"]
    value, code = await api.tidal_test.start("42", 60)
    assert code == 409 and value["error"] == "test_unavailable"
    assert not api.mpv.calls and api.mpv.actual_volume == 77
    assert not api.tidal_playback.calls


async def test_native_replacement_during_inner_load_preparation_is_never_stopped(api):
    original = api.mpv.loadfile
    entered, release = asyncio.Event(), asyncio.Event()

    async def preparing(url, options=None, *, start_paused=False, current=None):
        entered.set()
        await release.wait()
        return await original(url, options, start_paused=start_paused, current=current)

    api.mpv.loadfile = preparing
    _, code = await api.tidal_test.start("42", 60)
    assert code == 202
    await asyncio.wait_for(entered.wait(), 2)
    api.mpv.idle = False
    api.mpv.url, api.mpv.entry_id = "https://foreign.example.test/native-during-prepare", 99
    release.set()
    final = await wait_state(api, "superseded")
    assert final["error"] == "source_changed"
    assert api.mpv.calls == [("volume", 1)]
    assert not api.mpv.idle and api.mpv.entry_id == 99


async def test_cancelled_start_caller_does_not_cancel_renderer_deadline(api):
    entered, release = asyncio.Event(), asyncio.Event()
    original = api.mpv.volume
    now = [100.0]
    api.tidal_test._clock = lambda: now[0]

    async def delayed_gain(value):
        await original(value)
        entered.set()
        await release.wait()

    api.mpv.volume = delayed_gain
    caller = asyncio.create_task(api.tidal_test.start("42", 1))
    await asyncio.wait_for(entered.wait(), 2)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()
    playing = await wait_state(api, "playing")
    assert playing["lease_id"] is not None and api.mpv.actual_volume == 1
    now[0] = 102.0
    expired = await wait_state(api, "expired")
    assert expired["lease_id"] == playing["lease_id"] and api.mpv.idle
    assert ("stop", None) in api.mpv.calls


async def test_loopback_disconnect_before_start_response_keeps_renderer_watchdog(api):
    entered, release = asyncio.Event(), asyncio.Event()
    original = api.mpv.volume
    now = [100.0]
    api.tidal_test._clock = lambda: now[0]

    async def delayed_gain(value):
        await original(value)
        entered.set()
        await release.wait()

    api.mpv.volume = delayed_gain
    async with serving(api) as (session, base):
        boot = await web_state(session, base)
        port = int(base.rsplit(":", 1)[-1])
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        payload = json.dumps({**START, "duration_seconds": 1}).encode()
        request = (
            f"POST {PREFIX}/start HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Origin: {base}\r\nX-ORR-CSRF: {boot['csrf_token']}\r\n"
            "Content-Type: application/json\r\nConnection: close\r\n"
            f"Content-Length: {len(payload)}\r\n\r\n"
        ).encode() + payload
        try:
            writer.write(request)
            await writer.drain()
            await asyncio.wait_for(entered.wait(), 2)
            writer.close()
            await writer.wait_closed()
        finally:
            release.set()
            writer.close()
            await writer.wait_closed()
        playing = await wait_state(api, "playing")
        assert playing["lease_id"] is not None
        now[0] = 102.0
        async with asyncio.timeout(2):
            while True:
                async with session.get(base + PREFIX + "/status") as response:
                    assert response.status == 200
                    final = await response.json()
                if final["state"] == "expired":
                    break
                await asyncio.sleep(.005)
        assert_public(final)
        assert final["lease_id"] == playing["lease_id"]
        assert api.mpv.idle and ("stop", None) in api.mpv.calls


@pytest.mark.parametrize("control", ["pause", "resume", "seek", "play_current"])
async def test_same_source_controls_cannot_escape_the_bounded_lease(api, control):
    lease = await start_playing(api)
    before, generation = list(api.mpv.calls), api.state.generation
    with pytest.raises(BackendError):
        if control in {"pause", "resume"}:
            await api._transport_command(control)
        elif control == "seek":
            await api._seek_command(7)
        else:
            await api._play_url(api.state.current_url)
    assert api.mpv.calls == before and api.state.generation == generation
    value = await api.tidal_test.status()
    assert value["state"] == "playing" and value["lease_id"] == lease
    assert not api.mpv.idle


async def test_new_url_stops_exact_old_lease_then_deadline_leaves_new_source_alone(api):
    now = [100.0]
    api.tidal_test._clock = lambda: now[0]
    await start_playing(api)
    new_url = "https://media.example.test/new-unrelated-source.wav"
    old_loads = sum(name == "load" for name, _ in api.mpv.calls)
    await api._play_url(new_url)
    calls = list(api.mpv.calls)
    assert calls.count(("stop", None)) == 1
    assert sum(name == "load" for name, _ in calls) == old_loads + 1
    stop_index = calls.index(("stop", None))
    assert any(name == "load" for name, _ in calls[stop_index + 1:])
    assert api.state.current_url == new_url and api.mpv.url == new_url
    now[0] = 162.0
    await asyncio.sleep(.03)
    assert api.mpv.calls == calls and not api.mpv.idle
    assert api.state.current_url == new_url and api.state.status == "play"


async def test_terminal_status_does_not_reassert_old_one_percent_after_gain_change(api):
    lease = await start_playing(api)
    stopped, code = await api.tidal_test.stop(lease)
    assert code == 200 and stopped["state"] == "stopped"
    await api.execute(ParsedCommand("volume", ("77",)), "127.0.0.1")
    value = await api.tidal_test.status()
    assert_public(value)
    assert value["state"] == "stopped"
    assert value["volume_percent"] is None and not value["volume_confirmed"]
    assert api.mpv.actual_volume == 77


async def test_unrelated_media_controls_keep_existing_pause_resume_seek_behavior(api):
    await api._play_url("https://media.example.test/unrelated.wav")
    assert api.state.status == "play"
    await api._transport_command("pause")
    assert api.state.status == "pause" and api.mpv.paused
    await api._transport_command("resume")
    assert api.state.status == "play" and not api.mpv.paused
    await api._seek_command(7)
    assert api.mpv.position == 7 and api.state.curpos == 7
    assert ("pause", None) in api.mpv.calls and ("seek", 7) in api.mpv.calls


async def test_confirmed_stop_releases_guard_for_later_explicit_normal_play(api):
    lease = await start_playing(api)
    item = api.state.queue.current
    assert item._tidal_test_lease is api.tidal_test
    stopped, code = await api.tidal_test.stop(lease)
    assert code == 200 and stopped["state"] == "stopped"
    assert item._tidal_test_lease is None
    before = len([call for call in api.mpv.calls if call[0] == "load"])
    await api._play_url(item.url)
    assert api.state.status == "play"
    assert len([call for call in api.mpv.calls if call[0] == "load"]) == before + 1
    assert not api.tidal_test._live


async def test_unconfirmed_stop_retains_item_admission_guard(api):
    lease = await start_playing(api)
    item = api.state.queue.current
    api.mpv.stop_error = True
    stopped, code = await api.tidal_test.stop(lease)
    assert code == 503 and stopped["error"] == "stop_unconfirmed"
    assert item._tidal_test_lease is api.tidal_test
