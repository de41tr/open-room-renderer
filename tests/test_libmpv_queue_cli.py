"""REAL_BACKEND/null: CLI queue and authored-file acceptance in one short child.

Actual CLI subprocesses reach the production loopback HttpApi, then the existing
real libmpv ctypes command-node test adapter. Three distinct generated WAVs prove
native identity and advancing decoded PCM, with HTTP/SOAP/received GENA agreement.
Client-only mode, navigation, mute and paused seek commands use the same path.
Saving that paused queue is a single read-only GET; its private authored file
restores stopped until an explicit play request selects the saved source.
This optional lane does not certify production Unix IPC, physical audio, shuffle
traversal or endurance; those have separate tests. No external media is fetched.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import locale
import os
from pathlib import Path
import subprocess
import sys
from xml.etree import ElementTree as ET

from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.test_utils import TestServer
import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.playlist_files import decode_playlist
from fake_linkplay.queue import parse_queue_context
from fake_linkplay.state import PlaybackPhase, RendererState
from test_libmpv_integration import _eventually, _write_tone
from test_libmpv_queue_staging import _StagingBackend
from test_libmpv_repeat_queue import _attributes, _loads, _values


async def _exercise(library: str, directory: Path) -> dict:
    baseline = asyncio.all_tasks()
    started = asyncio.get_running_loop().time()
    root = Path(__file__).resolve().parents[1]
    tracks = tuple(directory / name for name in ("a.wav", "b.wav", "c.wav"))
    for track, frequency in zip(tracks, (330, 550, 770)):
        _write_tone(track, frequency)
    payloads = {track.name: track.read_bytes() for track in tracks}
    hashes = [hashlib.sha256(payload).hexdigest() for payload in payloads.values()]
    assert len(set(hashes)) == 3, "Selection must reach three different source byte streams"
    backend = _StagingBackend(library, tracks)
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000",
                 state_dir=str(directory / "state"), advertise_ip="127.0.0.1",
                 bind_address="127.0.0.1", native_queue_staging=False,
                 auto_spawn_mpv=False, enable_ssdp=False, enable_mdns=False)
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)
    receipts, requests, children, control_requests = [], [], [], []
    evidence = {"lane": "REAL_BACKEND/null + real CLI/loopback HTTP/SOAP/received GENA",
                "command_transport": "test ctypes command-node adapter, not Unix IPC",
                "mpv_version": backend.read_property("mpv-version"),
                "observer_interval": api._monitor_interval,
                "source_sha256": hashes, "decoded": [], "wire": [], "cases": {}}

    async def media(request):
        requests.append(request.raw_path)
        return web.Response(body=payloads[request.match_info["name"]], content_type="audio/wav")

    async def callback(request):
        properties = {node.tag.rsplit("}", 1)[-1]: node.text or ""
                      for prop in ET.fromstring(await request.text()) for node in prop}
        receipts.append((request.headers["SID"], properties))
        return web.Response()

    @web.middleware
    async def record_control(request, handler):
        if request.path != "/fixture-events":
            control_requests.append({"method": request.method, "path": request.raw_path,
                                     "content_length": request.content_length,
                                     "transfer_encoding": request.headers.get("Transfer-Encoding")})
        return await handler(request)

    app = api.app()
    app.middlewares.append(record_control)
    app.router.add_get("/fixture-media/{name}", media)
    app.router.add_route("NOTIFY", "/fixture-events", callback)
    server = TestServer(app, shutdown_timeout=1)
    sockets = []
    try:
        await server.start_server()
        cfg.http_api_port = cfg.upnp_port = server.port
        sockets = [sock for site in server.runner.sites for sock in site._server.sockets]
        urls = [str(server.make_url("/fixture-media/" + track.name))
                + "?literal=%252F&value=a%26b%3Dc" for track in tracks]
        backend._allowed_media = set(urls)
        cli_env = {**os.environ, "PYTHONPATH": str(root), "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}

        async def cli(*arguments, remote=True):
            command = [sys.executable, "-m", "fake_linkplay.cli", *map(str, arguments)]
            if remote:
                command.extend(("--host", "127.0.0.1", "--port", str(server.port)))
            child = await asyncio.create_subprocess_exec(*command, cwd=root, env=cli_env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            children.append(child)
            try:
                stdout, stderr = await asyncio.wait_for(child.communicate(), timeout=4)
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
            assert child.returncode == 0, (arguments[:2], child.returncode, stdout, stderr)
            assert not stderr, stderr
            # Successful default summaries must not leak the selected source,
            # including its URL-like fallback title or percent-encoded query.
            assert all(url.encode() not in stdout for url in urls)
            assert b"fixture-media" not in stdout and b"literal=" not in stdout
            return json.loads(stdout) if stdout.lstrip().startswith(b"{") else stdout.decode().strip()

        async with ClientSession(timeout=ClientTimeout(total=4), trust_env=False) as client:
            subscriptions = {}
            for service in ("AVTransport", "PlayQueue", "RenderingControl"):
                async with client.request("SUBSCRIBE", server.make_url("/upnp/event/" + service), headers={
                    "NT": "upnp:event", "CALLBACK": f'<{server.make_url("/fixture-events")}>',
                    "TIMEOUT": "Second-60",
                }) as response:
                    assert response.status == 200
                    subscriptions[service] = response.headers["SID"]

            async def soap(action, service="AVTransport", fields="<InstanceID>0</InstanceID>"):
                namespace = ("urn:schemas-wiimu-com:service:PlayQueue:1" if service == "PlayQueue"
                             else f"urn:schemas-upnp-org:service:{service}:1")
                body = (f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                        f'<u:{action} xmlns:u="{namespace}">{fields}</u:{action}></s:Body></s:Envelope>')
                async with client.post(server.make_url("/upnp/control/" + service), data=body, headers={
                    "SOAPACTION": f'"{namespace}#{action}"', "Content-Type": "text/xml",
                }) as response:
                    text = await response.text()
                    assert response.status == 200, (action, response.status, text)
                    return _values(text)

            async def player_status():
                async with client.get(server.make_url("/httpapi.asp"), params={"command": "getPlayerStatus"}) as response:
                    assert response.status == 200
                    return await response.json()

            async def wire(name, index, phase, mode, loop, marker):
                url = urls[index - 1]
                status = await player_status()
                http_phase = "PAUSED" if phase == "PAUSED_PLAYBACK" else phase
                assert (status["plicurr"], status["plicount"], status["loop"], status["uri"],
                        status["playback_phase"], status["transport_error"]) == (
                            str(index), "3", str(loop), url, http_phase, ""), status
                assert (await soap("GetTransportSettings"))["PlayMode"] == mode
                assert (await soap("GetTransportInfo"))["CurrentTransportState"] == phase
                position = await soap("GetPositionInfo")
                assert position["Track"] == str(index) and position["TrackURI"] == url
                assert (await soap("GetQueueLoopMode", "PlayQueue", ""))["LoopMode"] == str(loop)
                context = (await soap("BrowseQueue", "PlayQueue", ""))["QueueContext"]
                queue = parse_queue_context(context)
                assert (queue.name, queue.index, queue.loop) == (name, index, loop)
                assert [item.url for item in queue.items] == urls, "Canonical order/URL bytes changed"

                def agrees(service, properties):
                    change = _attributes(properties.get("LastChange", "<none/>"))
                    if service == "AVTransport":
                        return (change.get("CurrentPlayMode") == mode
                                and change.get("CurrentTrack") == str(index)
                                and change.get("CurrentTrackURI") == url
                                and change.get("TransportState") == phase)
                    if not properties.get("QueueContext"):
                        return False
                    event_queue = parse_queue_context(properties["QueueContext"])
                    return (properties.get("CurrentIndex") == str(index)
                            and properties.get("LoopMode") == str(loop) and properties.get("TrackURI") == url
                            and (event_queue.name, event_queue.index, event_queue.loop) == (name, index, loop)
                            and [item.url for item in event_queue.items] == urls)

                received = {}
                for service in ("AVTransport", "PlayQueue"):
                    properties = await _eventually("received " + service + " CLI selection agreement", lambda: next((
                        properties for sid, properties in receipts[marker:]
                        if sid == subscriptions[service] and agrees(service, properties)), None))
                    if service == "AVTransport":
                        change = _attributes(properties["LastChange"])
                        received[service] = {key: change[key] for key in (
                            "CurrentPlayMode", "CurrentTrack", "CurrentTrackURI", "TransportState", "RelativeTimePosition")}
                    else:
                        received[service] = {key: properties[key] for key in ("CurrentIndex", "LoopMode", "TrackURI")}
                evidence["wire"].append({"index": index, "phase": phase, "mode": mode, "uri": status["uri"],
                                         "soap_position": position["RelTime"], "received": received})

            async def decoded(index):
                url = urls[index - 1]

                def sample():
                    if backend.read_property("path") != url:
                        return None
                    entries = [entry for entry in backend.read_property("playlist")
                               if entry.get("current") is True and entry.get("playing") is True]
                    selected = [track for track in backend.read_property("track-list")
                                if track.get("type") == "audio" and track.get("selected") is True]
                    position = backend.read_property("playback-time")
                    if (len(entries) != 1 or not selected or selected[0].get("codec") != "pcm_s16le"
                            or position <= .05 or backend.read_property("current-ao") != "null"
                            or backend.read_property("pause") is not False):
                        return None
                    assert backend.read_property("audio-params")["samplerate"] == 16000
                    return {"index": index, "path": url, "native_id": entries[0]["id"],
                            "position": position, "codec": "pcm_s16le", "ao": "null"}

                first = await _eventually("CLI-selected native PCM source", sample)
                later = await _eventually("same native source makes decoded progress", lambda: (
                    observed if (observed := sample()) and observed["native_id"] == first["native_id"]
                    and observed["position"] > first["position"] + .08 else None))
                await _eventually("production observer confirms selected native source", lambda: (
                    api.state.phase == PlaybackPhase.PLAYING and api.state.queue.index == index))
                evidence["decoded"].append({**later, "first_position": first["position"]})
                return later

            async def show(name, index, mode):
                summary = await cli("queue", "show", "--name", name)
                assert summary == {"name": name, "count": 3, "index": index, "mode": mode,
                                   "items": [{"index": number} for number in (1, 2, 3)]}

            marker = len(receipts)
            await cli("queue", "load", "--name", "Explicit queue", "--mode", "repeat-all", *urls)
            assert not requests and not _loads(backend.commands)
            assert backend.read_property("idle-active") is True and backend.read_property("playlist") == []
            await show("Explicit queue", 1, "repeat-all")
            await wire("Explicit queue", 1, "STOPPED", "REPEAT_ALL", 0, marker)
            evidence["cases"]["load_stopped"] = {"media_requests": 0, "native_loads": 0,
                                                     "canonical_indices_without_urls": True}

            for index in (1, 2):
                marker = len(receipts)
                await cli("queue", "play-index", str(index), "--name", "Explicit queue")
                await decoded(index)
                await wire("Explicit queue", index, "PLAYING", "REPEAT_ALL", 0, marker)
            assert evidence["decoded"][0]["native_id"] != evidence["decoded"][1]["native_id"]
            evidence["cases"]["play_index"] = {"different_native_sources": [1, 2]}

            # A mode request changes policy while this exact native source
            # keeps decoding; it cannot restart, reload or replace ownership.
            marker, commands, generation = len(receipts), len(backend.commands), api.state.generation
            native_playlist = backend.read_property("playlist")
            previous = evidence["decoded"][-1]
            assert await cli("queue", "mode", "normal") == {"mode_request_acknowledged": True, "mode": "normal"}
            unchanged = await decoded(2)
            assert api.state.generation == generation
            assert backend.commands[commands:] == []
            assert backend.read_property("playlist") == native_playlist
            assert unchanged["native_id"] == previous["native_id"]
            assert unchanged["position"] > previous["position"]
            await wire("Explicit queue", 2, "PLAYING", "NORMAL", 4, marker)
            evidence["cases"]["queue_mode"] = {"native_id": unchanged["native_id"], "generation": generation,
                "before_position": previous["position"], "after_position": unchanged["position"],
                "native_mutations": 0, "native_loads": 0}

            navigation = [unchanged]
            for command, index in (("next", 3), ("previous", 2)):
                marker, loads = len(receipts), len(_loads(backend.commands))
                assert await cli(command) == {"request_acknowledged": True, "command": command}
                selection = await decoded(index)
                assert selection["native_id"] != navigation[-1]["native_id"]
                assert len(_loads(backend.commands)) == loads + 1
                await wire("Explicit queue", index, "PLAYING", "NORMAL", 4, marker)
                navigation.append(selection)
            assert [entry["index"] for entry in navigation] == [2, 3, 2]
            assert navigation[0]["path"] == navigation[2]["path"] != navigation[1]["path"]
            evidence["cases"]["normal_navigation"] = {"indices": [entry["index"] for entry in navigation],
                "native_ids": [entry["native_id"] for entry in navigation], "loads": 2}

            native_playlist = backend.read_property("playlist")
            loads = len(_loads(backend.commands))
            mute_values = []
            for value, expected in (("on", True), ("off", False)):
                marker = len(receipts)
                assert await cli("mute", value) == {"request_acknowledged": True, "command": "mute", "value": value}
                assert backend.read_property("mute") is expected
                status = await player_status()
                assert status["mute"] == str(int(expected)) and status["transport_error"] == ""
                fields = "<InstanceID>0</InstanceID><Channel>Master</Channel>"
                assert (await soap("GetMute", "RenderingControl", fields))["CurrentMute"] == str(int(expected))
                event = await _eventually("received CLI mute " + value, lambda: next((properties
                    for sid, properties in receipts[marker:]
                    if sid == subscriptions["RenderingControl"] and properties.get("Mute") == str(int(expected))), None))
                mute_values.append({"native": backend.read_property("mute"), "http": status["mute"],
                                    "gena": event["Mute"]})
            assert backend.read_property("playlist") == native_playlist
            assert len(_loads(backend.commands)) == loads
            assert backend.read_property("pause") is False and api.state.phase == PlaybackPhase.PLAYING
            evidence["cases"]["mute_on_off"] = {"confirmed_values": mute_values, "native_loads": 0}

            assert await cli("pause") == "OK"
            assert backend.read_property("pause") is True and api.state.intent == "pause"
            marker = len(receipts)
            assert await cli("seek", "8") == {"request_acknowledged": True, "command": "seek", "seconds": 8}
            actual = backend.read_property("playback-time")
            assert abs(actual - 8) < .2, actual
            assert backend.read_property("pause") is True
            assert backend.read_property("duration") == pytest.approx(30, abs=.05)
            assert backend.read_property("path") == urls[1]
            assert backend.read_property("playlist") == native_playlist
            assert len(_loads(backend.commands)) == loads
            assert api.state.intent == "pause" and api.state.phase == PlaybackPhase.PAUSED
            status = await player_status()
            assert abs(int(status["curpos"]) - actual) <= 1
            position = await soap("GetPositionInfo")
            def seconds(value):
                return sum(int(part) * multiplier for part, multiplier in zip(value.split(":"), (3600, 60, 1)))

            assert abs(seconds(position["RelTime"]) - actual) <= 1
            await wire("Explicit queue", 2, "PAUSED_PLAYBACK", "NORMAL", 4, marker)
            event = await _eventually("received paused CLI absolute seek position", lambda: next((properties
                for sid, properties in receipts[marker:]
                if sid == subscriptions["AVTransport"] and properties.get("TransportState") == "PAUSED_PLAYBACK"
                and properties.get("TrackURI") == urls[1]
                and abs(seconds(properties["RelativeTimePosition"]) - actual) <= 1), None))
            await asyncio.sleep(.1)
            settled = backend.read_property("playback-time")
            assert abs(settled - actual) < .03 and backend.read_property("pause") is True
            evidence["cases"]["paused_seek"] = {"target": 8, "native_position": actual,
                "settled_position": settled, "http_position": status["curpos"], "soap_position": position["RelTime"],
                "gena_position": event["RelativeTimePosition"], "gena_phase": event["TransportState"],
                "native_id": native_playlist[0]["id"], "path": backend.read_property("path"), "native_loads": 0}

            # The actual save-current CLI must only read the queue. In
            # particular, it cannot resolve, stop, reload or seek the source.
            saved = directory / "saved-current.json"
            request_marker, commands = len(control_requests), len(backend.commands)
            generation, queue = api.state.generation, api.state.queue
            queue_before = deepcopy(vars(queue))
            media_requests, saved_position = len(requests), backend.read_property("playback-time")
            original_resolver, resolutions = api._resolve_playback_url, []

            async def record_resolution(url):
                resolutions.append(url)
                return await original_resolver(url)

            api._resolve_playback_url = record_resolution
            try:
                await cli("playlist", "save-current", saved)
            finally:
                api._resolve_playback_url = original_resolver
            save_requests = control_requests[request_marker:]
            assert save_requests == [{"method": "GET", "path": "/orr/v1/queue/playlist",
                                      "content_length": None, "transfer_encoding": None}]
            assert not resolutions and len(requests) == media_requests
            assert backend.commands[commands:] == []
            assert api.state.generation == generation and api.state.queue is queue
            assert vars(api.state.queue) == queue_before
            assert api.state.intent == "pause" and api.state.phase == PlaybackPhase.PAUSED
            assert backend.read_property("playlist") == native_playlist
            assert backend.read_property("path") == urls[1] and backend.read_property("pause") is True
            saved_after_position = backend.read_property("playback-time")
            assert abs(saved_after_position - saved_position) < .03
            saved_bytes = saved.read_bytes()
            saved_document = {"version": 1, "name": "Explicit queue", "index": 2,
                              "mode": "normal", "urls": urls}
            assert decode_playlist(saved_bytes) == saved_document
            assert saved_bytes == (json.dumps(saved_document, ensure_ascii=False,
                allow_nan=False, indent=2) + "\n").encode("utf-8")
            assert saved.stat().st_mode & 0o777 == 0o600
            evidence["cases"]["save_current_paused"] = {"requests": save_requests,
                "index": 2, "mode": "normal", "native_id": native_playlist[0]["id"],
                "generation": generation, "before_position": saved_position,
                "after_position": saved_after_position, "canonical_url_bytes_unchanged": True,
                "file_mode": "0600", "pause_preserved": True, "queue_unchanged": True,
                "native_mutations": 0, "resolver_calls": 0, "media_requests": 0, "soap_requests": 0}

            playlist = directory / "authored.json"
            await cli("playlist", "create", playlist, "--name", "Authored queue", "--start-index", "3",
                      "--mode", "shuffle-repeat-all", *urls, remote=False)
            authored_bytes = playlist.read_bytes()
            assert json.loads(authored_bytes) == {"version": 1, "name": "Authored queue", "index": 3,
                                                 "mode": "shuffle-repeat-all", "urls": urls}
            marker, loads = len(receipts), len(_loads(backend.commands))
            await cli("playlist", "restore", playlist)
            assert len(_loads(backend.commands)) == loads, "Default restore must not load any source"
            assert backend.read_property("idle-active") is True and backend.read_property("playlist") == []
            await show("Authored queue", 3, "shuffle-repeat-all")
            await wire("Authored queue", 3, "STOPPED", "SHUFFLE", 2, marker)
            # Observe a complete normal monitor interval: restoring cannot
            # leave a delayed reload/advance from the previous playing source.
            quiet_seconds = api._monitor_interval + .02
            await asyncio.sleep(quiet_seconds)
            assert len(_loads(backend.commands)) == loads
            assert backend.read_property("idle-active") is True and backend.read_property("playlist") == []
            assert api.state.phase == PlaybackPhase.STOPPED
            evidence["cases"]["restore_stopped"] = {"stopped_prior_playback": True, "extra_native_loads": 0,
                                                       "index": 3, "mode": "shuffle-repeat-all",
                                                       "observed_stopped_seconds": quiet_seconds}

            marker = len(receipts)
            await cli("playlist", "restore", playlist, "--play")
            await decoded(3)
            await wire("Authored queue", 3, "PLAYING", "SHUFFLE", 2, marker)
            assert len({entry["native_id"] for entry in evidence["decoded"]}) == 5
            assert len(_loads(backend.commands)) == 5, "Each requested selection loads exactly once"
            assert playlist.read_bytes() == authored_bytes, "Restoring must not rewrite the authored file"
            evidence["cases"]["restore_play"] = {"selected_native_source": 3, "authored_bytes_unchanged": True}

            # Use the unchanged restore client on the newly saved file. The
            # saved selection is retained without loading until explicit play.
            marker, loads = len(receipts), len(_loads(backend.commands))
            await cli("playlist", "restore", saved)
            assert len(_loads(backend.commands)) == loads
            assert backend.read_property("idle-active") is True and backend.read_property("playlist") == []
            await wire("Explicit queue", 2, "STOPPED", "NORMAL", 4, marker)
            await asyncio.sleep(quiet_seconds)
            assert len(_loads(backend.commands)) == loads
            assert backend.read_property("idle-active") is True and backend.read_property("playlist") == []
            assert api.state.phase == PlaybackPhase.STOPPED
            assert saved.read_bytes() == saved_bytes
            marker = len(receipts)
            await cli("playlist", "restore", saved, "--play")
            restored = await decoded(2)
            await wire("Explicit queue", 2, "PLAYING", "NORMAL", 4, marker)
            assert len(_loads(backend.commands)) == loads + 1
            assert restored["native_id"] != native_playlist[0]["id"]
            assert saved.read_bytes() == saved_bytes
            evidence["cases"]["restore_saved_queue"] = {"index": 2, "mode": "normal",
                "observed_stopped_seconds": quiet_seconds, "extra_loads_before_play": 0,
                "explicit_play_loads": 1, "native_id": restored["native_id"],
                "saved_bytes_unchanged": True}
            # A real CLI Stop also leaves no native playlist that can advance.
            assert await cli("stop") == "OK"
            assert backend.read_property("idle-active") is True and backend.read_property("playlist") == []
            evidence["received_event_count"] = len(receipts)
            assert {path.split("?", 1)[0] for path in requests} == {
                "/fixture-media/" + track.name for track in tracks}
    finally:
        try:
            await server.close()
        finally:
            backend.close()
        for child in children:
            if child.returncode is None:
                child.kill()
                await child.wait()
        assert all(child.returncode is not None for child in children)
        assert backend._handle is None and backend.process is None
        assert api._state_monitor_task is None and not api.event_subscribers
        assert server.closed and not server.runner.sites and all(sock.fileno() == -1 for sock in sockets)
    assert not [task for task in asyncio.all_tasks() - baseline if not task.done()]
    evidence["cleanup"] = {"native_handle_destroyed": True, "no_pending_tasks": True,
                           "monitor_stopped": True, "subscriptions_removed": True,
                           "listeners_closed": True, "cli_children_reaped": len(children)}
    evidence["elapsed_seconds"] = round(asyncio.get_running_loop().time() - started, 3)
    return evidence


@pytest.mark.skipif(not os.environ.get("ORR_TEST_LIBMPV_PATH"), reason="Native queue CLI requires ORR_TEST_LIBMPV_PATH")
def test_real_queue_cli_and_authored_playlist(tmp_path):
    library = Path(os.environ["ORR_TEST_LIBMPV_PATH"]).expanduser().resolve()
    assert library.is_file()
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root)}
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--probe", str(library), str(tmp_path)],
        cwd=root, env=env, text=True, capture_output=True, timeout=20, check=False)
    assert completed.returncode == 0, f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    evidence = json.loads(completed.stdout)
    assert evidence["cleanup"]["no_pending_tasks"] and len(evidence["cases"]) == 10
    assert evidence["cleanup"]["cli_children_reaped"] == 19
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 4 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")

    async def main():
        async with asyncio.timeout(18):
            print(json.dumps(await _exercise(sys.argv[2], Path(sys.argv[3])), sort_keys=True))

    asyncio.run(main())
