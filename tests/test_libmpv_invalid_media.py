"""One bounded malformed-media failure and explicit recovery on real libmpv/null.

Only generated local bytes are loaded. HttpApi HTTP/SOAP and received GENA are
real; the existing ctypes adapter replaces only production Unix IPC. Readiness
keeps its production 10-second deadline. No backend fault or device is injected.
"""
from __future__ import annotations

import asyncio
import ctypes as ct
from html import escape
import hashlib
import json
import locale
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest
from aiohttp import ClientSession, ClientTimeout, web

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.state import PlaybackPhase, RendererState
from libmpv_stability_support import event_fields, fields
from test_libmpv_integration import _eventually, _write_tone
from test_libmpv_load_cleanup import _wait_for_cleanup_snapshot
from test_libmpv_load_options import _NamedLoadBackend


# Stable public API layouts, not mpv internals:
# https://github.com/mpv-player/mpv/blob/v0.37.0/libmpv/client.h
# https://github.com/mpv-player/mpv/blob/v0.40.0/include/mpv/client.h
class _Event(ct.Structure):
    _fields_ = [("event_id", ct.c_int), ("error", ct.c_int),
                ("reply_userdata", ct.c_uint64), ("data", ct.c_void_p)]


class _EndFile(ct.Structure):
    _fields_ = [("reason", ct.c_int), ("error", ct.c_int),
                ("playlist_entry_id", ct.c_int64), ("playlist_insert_id", ct.c_int64),
                ("playlist_insert_num_entries", ct.c_int)]


class _ObservedBackend(_NamedLoadBackend):
    """Pass every command through unchanged; retain actual native failure evidence."""

    def __init__(self, library, tracks):
        super().__init__(library, tracks)
        self.loads, self.load_replies = [], []
        self._lib.mpv_wait_event.restype = ct.POINTER(_Event)
        self._lib.mpv_wait_event.argtypes = [ct.c_void_p, ct.c_double]

    async def command(self, args, timeout=5.0):
        loading = (args.get("name") if isinstance(args, dict) else args[0]) == "loadfile"
        if loading:
            self.loads.append(args["url"] if isinstance(args, dict) else args[1])
        result = await super().command(args, timeout)
        if loading:
            self.load_replies.append(result)
        return result

    def events(self):
        result = []
        for _ in range(1024):
            event = self._lib.mpv_wait_event(self._handle, 0).contents
            if event.event_id == 0:
                return result
            assert event.event_id not in {1, 24}, "Native shutdown or event overflow"
            item = {"event_id": event.event_id}
            if event.event_id == 6:  # MPV_EVENT_START_FILE
                assert event.data
                item["entry_id"] = ct.cast(event.data, ct.POINTER(ct.c_int64)).contents.value
            elif event.event_id == 7:  # MPV_EVENT_END_FILE
                assert event.data
                ended = ct.cast(event.data, ct.POINTER(_EndFile)).contents
                item.update(reason=ended.reason, error=ended.error, entry_id=ended.playlist_entry_id,
                            error_text=self._lib.mpv_error_string(ended.error).decode())
            result.append(item)  # Copy before the next mpv_wait_event invalidates data.
        raise AssertionError("Native event queue did not drain")


async def _exercise(library: str, directory: Path) -> dict:
    baseline = asyncio.all_tasks()
    clock = asyncio.get_running_loop().time
    started = clock()
    invalid, valid = directory / "malformed.wav", directory / "recovery-660.wav"
    # Invalid RIFF/WAVE fmt chunk, without paths, URLs or playlist-like text.
    invalid.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\xff" * 64)
    _write_tone(valid, 660)
    backend = _ObservedBackend(library, (invalid, valid))
    listener = runner = session = monitor = api = None
    event_tasks = []
    evidence = {"lane": "REAL_BACKEND/null/malformed-media", "mpv_version": backend.read_property("mpv-version"),
                "fixture_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in (invalid, valid)}}
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(socket.SOMAXCONN)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        cfg = Config(uuid="00000000-0000-4000-8000-000000000000",
                     advertise_ip="127.0.0.1", bind_address="127.0.0.1",
                     http_api_port=port, upnp_port=port, state_dir=str(directory / "state"),
                     auto_spawn_mpv=False, enable_ssdp=False, enable_mdns=False)
        api = HttpApi(cfg, RendererState.from_config(cfg), backend)
        receipts, subscriptions = [], {}

        async def receive(request):
            receipts.append({"sid": request.headers["SID"], "seq": int(request.headers["SEQ"]),
                             "values": event_fields(await request.text())})
            return web.Response()

        app = api.app()
        app.router.add_route("NOTIFY", "/_fixture_events", receive)
        runner = web.AppRunner(app, shutdown_timeout=1)
        await runner.setup()
        await web.SockSite(runner, listener).start()
        monitor = api._state_monitor_task
        assert monitor is not None and not monitor.done()
        async with ClientSession(timeout=ClientTimeout(total=13), trust_env=False) as session:
            async def command(value):
                async with session.get(base + "/httpapi.asp", params={"command": value}) as response:
                    return response.status, await response.text()

            async def soap(action, arguments="", service="AVTransport"):
                namespace = f"urn:schemas-upnp-org:service:{service}:1"
                body = f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><u:{action} xmlns:u="{namespace}"><InstanceID>0</InstanceID>{arguments}</u:{action}></s:Body></s:Envelope>'
                async with session.post(base + "/upnp/control/" + service, data=body.encode(),
                        headers={"SOAPACTION": f'"{namespace}#{action}"', "Content-Type": "text/xml"}) as response:
                    text = await response.text()
                    assert response.status == 200, (action, response.status, text)
                    return fields(text)

            async def delivered(service, expected, after=0):
                return await _eventually("received GENA " + service, lambda: next((item for item in receipts[after:]
                    if item["sid"] == subscriptions[service]
                    and all(item["values"].get(key) == value for key, value in expected.items())), None))

            async def status(phase, source, error):
                code, text = await command("getPlayerStatus")
                assert code == 200
                value = json.loads(text)
                assert value["playback_phase"] == phase and value["stream_url"] == source, value
                assert value["status"] == ("play" if phase == "PLAYING" else "stop"), value
                assert value["transport_error"] == error, value
                assert value["vol"] == "37" and value["mute"] == "1" and value["gain_confirmed"] is True
                return value

            def gain():
                assert backend.read_property("volume") == pytest.approx(37, abs=.01)
                assert backend.read_property("mute") is True
                assert api.state.volume == 37 and api.state.mute is True

            for service in ("AVTransport", "RenderingControl"):
                async with session.request("SUBSCRIBE", base + "/upnp/event/" + service,
                        headers={"NT": "upnp:event", "CALLBACK": f"<{base}/_fixture_events>",
                                 "TIMEOUT": "Second-60"}) as response:
                    assert response.status == 200
                    subscriptions[service] = response.headers["SID"]
            await delivered("AVTransport", {"last:TransportState": "STOPPED"})
            assert await command("setPlayerCmd:vol:37") == (200, "OK")
            assert await command("setPlayerCmd:mute:1") == (200, "OK")
            await delivered("RenderingControl", {"last:Volume": "37", "last:Mute": "1"})
            gain()

            mark = len(receipts)
            load_started = clock()
            # This is the real HTTP failure contract. SOAP read actions below
            # correctly return 200 carrying STOPPED/ERROR_OCCURRED, not a fault.
            assert await command("setPlayerCmd:play:" + str(invalid)) == (502, "Failed")
            failed_after = clock() - load_started
            assert failed_after < 13
            native_events = backend.events()
            starts = [item for item in native_events if item["event_id"] == 6]
            ends = [item for item in native_events if item["event_id"] == 7]
            assert len(starts) == len(ends) == 1, native_events
            # MPV_ERROR_UNKNOWN_FORMAT: unrelated native failures must not pass.
            assert ends[0]["reason"] == 4 and ends[0]["error"] == -17, native_events
            assert starts[0]["entry_id"] == ends[0]["entry_id"]
            assert not any(item["event_id"] == 8 for item in native_events), "Invalid bytes reported FILE_LOADED"
            assert backend.load_replies == [{"error": "success"}], backend.load_replies
            snapshot = await _wait_for_cleanup_snapshot(backend, clock() + .75)
            assert snapshot["playback-time"] is None and snapshot["track-list"] == [], snapshot
            assert api.state.phase == PlaybackPhase.ERROR
            assert api.state.backend_url == api.state.backend_original_url == ""
            assert api.state.current_url == api.state.queue.current.url == str(invalid)
            failure_event = await delivered("AVTransport", {"last:TransportState": "STOPPED",
                "last:TransportStatus": "ERROR_OCCURRED", "last:CurrentTrackURI": str(invalid)}, mark)
            failure_status = await status("ERROR", str(invalid), "Source did not confirm playback")
            assert failure_status["curpos"] == "0"
            transport = await soap("GetTransportInfo")
            assert transport["CurrentTransportState"] == "STOPPED"
            assert transport["CurrentTransportStatus"] == "ERROR_OCCURRED"
            assert (await soap("GetPositionInfo"))["TrackURI"] == str(invalid)
            assert (await soap("GetVolume", "<Channel>Master</Channel>", "RenderingControl"))["CurrentVolume"] == "37"
            assert (await soap("GetMute", "<Channel>Master</Channel>", "RenderingControl"))["CurrentMute"] == "1"
            gain()

            # Beyond every configured retry delay, with all real monitor/event
            # tasks running. No command or timeout is replaced to get this result.
            quiet_seconds = sum(api._station_recovery_delays) + api._monitor_interval
            await asyncio.sleep(quiet_seconds)
            assert backend.loads == [str(invalid)]
            assert backend.events() == [], "Unexpected native activity before explicit selection"
            assert api._station_recovery is None and api._station_recovery_task is None
            assert api.state.phase == PlaybackPhase.ERROR
            assert backend.read_property("idle-active") is True
            assert not any(item["values"].get("last:TransportState") == "PLAYING" for item in receipts[mark:])
            evidence["failure"] = {"http_status": 502, "soap_read_status": 200,
                "transport": transport["CurrentTransportState"], "transport_status": transport["CurrentTransportStatus"],
                "native_end_file": ends[0], "native_idle": snapshot["idle-active"],
                "native_path": snapshot["path"], "native_progress": snapshot["playback-time"],
                "native_playlist": snapshot["playlist"], "received_gena_seq": failure_event["seq"],
                "elapsed_seconds": failed_after, "no_retry_seconds": quiet_seconds}

            mark = len(receipts)
            failed_generation = api.state.generation
            await soap("SetAVTransportURI", f"<CurrentURI>{escape(str(valid))}</CurrentURI><CurrentURIMetaData/>")
            assert backend.loads == [str(invalid)], "Selection alone loaded audio"
            assert backend.read_property("idle-active") is True
            await soap("Play", "<Speed>1</Speed>")
            assert api.state.generation > failed_generation
            assert backend.read_property("path") == str(valid)
            assert backend.read_property("idle-active") is False and backend.read_property("pause") is False
            assert backend.read_property("current-ao") == "null"
            tracks = backend.read_property("track-list")
            assert any(track.get("type") == "audio" and track.get("selected") is True
                       and track.get("codec") == "pcm_s16le" for track in tracks), tracks
            entry = [item for item in backend.read_property("playlist") if item.get("current") and item.get("playing")]
            assert len(entry) == 1 and entry[0]["filename"] == str(valid)
            assert entry[0]["id"] != ends[0]["entry_id"]
            before = backend.read_property("playback-time")
            await _eventually("recovered native PCM progress", lambda: backend.read_property("playback-time") > before + .25)
            after = backend.read_property("playback-time")
            recovered_event = await delivered("AVTransport", {"last:TransportState": "PLAYING",
                "last:TransportStatus": "OK", "last:CurrentTrackURI": str(valid)}, mark)
            await status("PLAYING", str(valid), "")
            recovered_transport = await soap("GetTransportInfo")
            assert recovered_transport["CurrentTransportState"] == "PLAYING"
            assert recovered_transport["CurrentTransportStatus"] == "OK"
            assert (await soap("GetPositionInfo"))["TrackURI"] == str(valid)
            assert api.state.backend_url == api.state.backend_original_url == str(valid)
            assert backend.loads == [str(invalid), str(valid)]
            gain()
            evidence["recovery"] = {"native_entry_id": entry[0]["id"], "native_source": str(valid),
                "before": before, "after": after, "ao": "null", "codec": "pcm_s16le",
                "received_gena_seq": recovered_event["seq"], "volume": 37, "mute": True,
                "renderer_loads": len(backend.loads)}
            await soap("Stop")
            await _wait_for_cleanup_snapshot(backend, clock() + .75)
            event_tasks = [subscriber.task for subscriber in api.event_subscribers.values() if subscriber.task]
    finally:
        try:
            if runner is not None:
                await runner.cleanup()
        finally:
            if listener is not None:
                listener.close()
            backend.close()
        await asyncio.sleep(0)
        pending = [task for task in asyncio.all_tasks() - baseline if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        assert not pending, f"Leaked native failure fixture tasks: {pending!r}"
        assert session is None or session.closed
        assert listener is None or listener.fileno() == -1
        assert runner is None or not runner.sites
        assert backend._handle is None and backend.process is None
        assert monitor is None or monitor.done()
        assert all(task.done() for task in event_tasks)
        assert api is None or not api.event_subscribers and api._station_recovery_task is None
    evidence["cleanup"] = {"handle_closed": True, "listener_closed": True, "session_closed": True,
                           "subscriptions_closed": True, "monitor_and_events_done": True, "no_pending_tasks": True}
    evidence["elapsed_seconds"] = clock() - started
    return evidence


@pytest.mark.skipif(not os.environ.get("ORR_TEST_LIBMPV_PATH"),
                   reason="Native malformed-media acceptance requires ORR_TEST_LIBMPV_PATH")
def test_real_malformed_media_failure_and_explicit_recovery(tmp_path: Path) -> None:
    library = Path(os.environ["ORR_TEST_LIBMPV_PATH"]).expanduser().resolve()
    assert library.is_file(), f"Configured libmpv does not exist: {library}"
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root) + os.pathsep + str(root / "tests")}
    for name in tuple(env):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            env.pop(name)
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--probe", str(library), str(tmp_path)],
        cwd=root, env=env, text=True, capture_output=True, timeout=25, check=False)
    assert completed.returncode == 0, f"Malformed-media native probe failed\n{completed.stdout}\n{completed.stderr}"
    evidence = json.loads(completed.stdout)
    assert all(evidence["cleanup"].values())
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 4 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")

    async def main():
        async with asyncio.timeout(20):
            evidence = await _exercise(sys.argv[2], Path(sys.argv[3]))
        print(json.dumps(evidence, sort_keys=True))

    asyncio.run(main())
