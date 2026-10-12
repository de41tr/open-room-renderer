"""REAL_BACKEND/null: CLI queue and authored-file acceptance in one short child.

Actual CLI subprocesses reach the production loopback HttpApi, then the existing
real libmpv ctypes command-node test adapter. Three distinct generated WAVs prove
native identity and advancing decoded PCM, with HTTP/SOAP/received GENA agreement.
This optional lane does not certify production Unix IPC, physical audio, shuffle
traversal or endurance; those have separate tests. No external media is fetched.
"""
from __future__ import annotations

import asyncio
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
    receipts, requests, children = [], [], []
    evidence = {"lane": "REAL_BACKEND/null + real CLI/loopback HTTP/SOAP/received GENA",
                "command_transport": "test ctypes command-node adapter, not Unix IPC",
                "mpv_version": backend.read_property("mpv-version"),
                "observer_interval": api._monitor_interval,
                "source_sha256": hashes, "decoded": [], "cases": {}}

    async def media(request):
        requests.append(request.raw_path)
        return web.Response(body=payloads[request.match_info["name"]], content_type="audio/wav")

    async def callback(request):
        properties = {node.tag.rsplit("}", 1)[-1]: node.text or ""
                      for prop in ET.fromstring(await request.text()) for node in prop}
        receipts.append((request.headers["SID"], properties))
        return web.Response()

    app = api.app()
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
            for service in ("AVTransport", "PlayQueue"):
                async with client.request("SUBSCRIBE", server.make_url("/upnp/event/" + service), headers={
                    "NT": "upnp:event", "CALLBACK": f'<{server.make_url("/fixture-events")}>',
                    "TIMEOUT": "Second-60",
                }) as response:
                    assert response.status == 200
                    subscriptions[service] = response.headers["SID"]

            async def soap(action, service="AVTransport", fields="<InstanceID>0</InstanceID>"):
                namespace = ("urn:schemas-wiimu-com:service:PlayQueue:1" if service == "PlayQueue"
                             else "urn:schemas-upnp-org:service:AVTransport:1")
                body = (f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                        f'<u:{action} xmlns:u="{namespace}">{fields}</u:{action}></s:Body></s:Envelope>')
                async with client.post(server.make_url("/upnp/control/" + service), data=body, headers={
                    "SOAPACTION": f'"{namespace}#{action}"', "Content-Type": "text/xml",
                }) as response:
                    text = await response.text()
                    assert response.status == 200, (action, response.status, text)
                    return _values(text)

            async def wire(name, index, phase, mode, loop, marker):
                url = urls[index - 1]
                async with client.get(server.make_url("/httpapi.asp"), params={"command": "getPlayerStatus"}) as response:
                    assert response.status == 200
                    status = await response.json()
                assert (status["plicurr"], status["plicount"], status["loop"], status["uri"],
                        status["playback_phase"], status["transport_error"]) == (
                            str(index), "3", str(loop), url, phase, ""), status
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

                for service in subscriptions:
                    await _eventually("received " + service + " CLI selection agreement", lambda: any(
                        sid == subscriptions[service] and agrees(service, properties)
                        for sid, properties in receipts[marker:]))

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
            assert len({entry["native_id"] for entry in evidence["decoded"]}) == 3
            assert len(_loads(backend.commands)) == 3, "Each requested selection loads exactly once"
            assert playlist.read_bytes() == authored_bytes, "Restoring must not rewrite the authored file"
            evidence["cases"]["restore_play"] = {"selected_native_source": 3, "authored_bytes_unchanged": True}
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
    assert evidence["cleanup"]["no_pending_tasks"] and len(evidence["cases"]) == 4
    assert evidence["cleanup"]["cli_children_reaped"] == 9
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 4 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")

    async def main():
        async with asyncio.timeout(18):
            print(json.dumps(await _exercise(sys.argv[2], Path(sys.argv[3])), sort_keys=True))

    asyncio.run(main())
