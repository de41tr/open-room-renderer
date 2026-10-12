"""REAL_BACKEND/null favorites: real JSON, pywiim, legacy HTTP and received GENA.

Two different synthetic WAVs are served only on loopback. The real libmpv
command-node adapter proves source identity, codec and advancing decoded audio;
this short bounded child does not certify production Unix IPC or audible output.
Service/backend recreation checks durable favorites and absence of startup audio.
Controlled radio JSON resolution saves a stable reference and recalls a new local
target after restart; it makes no WAN/provider availability claim.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
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
from fake_linkplay.queue import PlaybackQueue
from fake_linkplay.radio import public_station_reference, resolve_station
from fake_linkplay.state import PlaybackPhase, RendererState
from test_libmpv_integration import _eventually, _write_tone
from test_libmpv_queue_staging import _StagingBackend

BASE = "/orr/v1/favorites"
DISPLAY = {"title": "Été <live> 日本語", "artist": "Synthetic & artist", "album": "Local tones"}


@asynccontextmanager
async def _renderer(library, directory, tracks, urls, receipts, cleanup):
    backend = _StagingBackend(library, tracks)
    backend._allowed_media = set(urls)  # Only these exact loopback fixtures can be loaded.
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000",
                 state_dir=str(directory / "state"), advertise_ip="127.0.0.1",
                 bind_address="127.0.0.1", auto_spawn_mpv=False,
                 enable_ssdp=False, enable_mdns=False)
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)

    async def callback(request):
        properties = {node.tag.rsplit("}", 1)[-1]: node.text or ""
                      for prop in ET.fromstring(await request.text()) for node in prop}
        receipts.append((request.headers["SID"], properties))
        return web.Response()

    app = api.app()
    app.router.add_route("NOTIFY", "/fixture-events", callback)
    server = TestServer(app, shutdown_timeout=1)
    sockets = []
    try:
        await server.start_server()
        cfg.http_api_port = cfg.upnp_port = server.port
        sockets = [sock for site in server.runner.sites for sock in site._server.sockets]
        yield api, backend, server
    finally:
        try:
            await server.close()
        finally:
            backend.close()
        assert backend._handle is None and backend.process is None
        assert api._state_monitor_task is None and not api.event_subscribers
        assert server.closed and not server.runner.sites and all(sock.fileno() == -1 for sock in sockets)
        cleanup.append({"native_handle_destroyed": True, "monitor_stopped": True,
                        "subscriptions_removed": True, "listeners_closed": True})


async def _decoded(backend, url):
    def sample():
        if backend.read_property("path") != url:
            return None
        entries = [entry for entry in backend.read_property("playlist")
                   if entry.get("current") is True and entry.get("playing") is True]
        selected = [track for track in backend.read_property("track-list")
                    if track.get("type") == "audio" and track.get("selected") is True]
        position = backend.read_property("playback-time")
        if (len(entries) != 1 or not selected or selected[0].get("codec") != "pcm_s16le"
                or backend.read_property("current-ao") != "null"
                or backend.read_property("pause") is not False or position <= .05):
            return None
        assert backend.read_property("audio-params")["samplerate"] == 16000
        if backend.read_property("path") != url:
            return None
        return {"url": url, "native_id": entries[0]["id"], "position": position,
                "codec": selected[0]["codec"], "ao": "null"}

    first = await _eventually("favorite decoded at its actual native URL", sample)
    later = await _eventually("same favorite native lifetime makes decoded progress", lambda: (
        current if (current := sample()) and current["native_id"] == first["native_id"]
        and current["position"] > first["position"] + .08 else None))
    return {**later, "first_position": first["position"]}


async def _exercise(library: str, directory: Path) -> dict:
    import pywiim  # Explicit native opt-in must fail, not skip, if the client is missing.

    baseline = asyncio.all_tasks()
    started = asyncio.get_running_loop().time()
    tracks = (directory / "a.wav", directory / "b.wav")
    for track, frequency in zip(tracks, (330, 660)):
        _write_tone(track, frequency)
    payloads = {"/a.wav": tracks[0].read_bytes(), "/b.wav": tracks[1].read_bytes()}
    hashes = [hashlib.sha256(payload).hexdigest() for payload in payloads.values()]
    assert len(set(hashes)) == 2, "Favorite targets must have different source bytes"
    requests, receipts, cleanup = [], [], []

    async def media(request):
        requests.append(request.raw_path)
        return web.Response(body=payloads[request.path], content_type="audio/wav")

    app = web.Application()
    app.router.add_get("/{name}.wav", media)
    media_server = TestServer(app, shutdown_timeout=1)
    media_sockets = []
    try:
        await media_server.start_server()
        media_sockets = [sock for site in media_server.runner.sites for sock in site._server.sockets]
        urls = tuple(str(media_server.make_url(path)) + "?literal=%252F&value=a%26b%3Dc"
                     for path in payloads)
        metadata = {**DISPLAY, "artwork": str(media_server.make_url("/artwork.svg"))}
        reference = public_station_reference("s12345")
        station_target, resolutions = [urls[0]], []

        async def fetch_station(endpoint):
            assert endpoint == reference
            resolutions.append(station_target[0])
            return json.dumps({"head": {"status": "200"}, "body": [{"element": "audio",
                "is_direct": True, "media_type": "mp3", "url": station_target[0]}]}).encode()

        async def validate_station(url):
            assert url in urls  # Controlled injection accepts only our two exact local WAVs.
            return True

        async def resolve(station_id):
            return await resolve_station(station_id, fetcher=fetch_station, validator=validate_station)

        evidence = {"lane": "REAL_BACKEND/null + loopback HTTP/pywiim/received GENA",
                    "command_transport": "test ctypes command-node adapter, not Unix IPC",
                    "source_sha256": hashes, "decoded": [], "cleanup": cleanup}
        async with ClientSession(timeout=ClientTimeout(total=4), trust_env=False) as session:
            async def json_api(server, method="GET", suffix="", payload=None, expected=200):
                async with session.request(method, server.make_url(BASE + suffix),
                                           **({"json": payload} if payload is not None else {})) as response:
                    result = await response.json()
                    assert response.status == expected, (method, suffix, response.status, result)
                    return result

            async def http(server, command):
                async with session.get(server.make_url("/httpapi.asp"), params={"command": command}) as response:
                    text = await response.text()
                    assert response.status == 200, (command, response.status, text)
                    return json.loads(text) if response.content_type == "application/json" else text

            async def listing(server, expected):
                document = await json_api(server)
                assert document["favorites"] == expected
                raw = await http(server, "getPresetInfo")
                assert raw["preset_num"] == str(len(expected))
                assert (await http(server, "getStatusEx"))["preset_key"] == str(len(expected))
                assert raw["presets"] == raw["preset_list"]
                assert [(entry["number"], entry["id"], entry["name"], entry["url"])
                        for entry in raw["presets"]] == [
                            (entry["slot"], entry["id"], entry["name"], entry["url"]) for entry in expected]
                return raw["presets"]

            async def wire(server, url):
                status = await http(server, "getPlayerStatus")
                assert status["playback_phase"] == "PLAYING" and status["transport_error"] == ""
                assert status["uri"] == url and status["source"] == "Preset"
                for field, key in (("Title", "title"), ("Artist", "artist"),
                                   ("Album", "album"), ("AlbumArtURI", "artwork")):
                    assert status[field] == metadata[key], status

            async with _renderer(library, directory, tracks, urls, receipts, cleanup) as (api, backend, server):
                evidence["mpv_version"] = backend.read_property("mpv-version")
                evidence["observer_interval"] = api._monitor_interval
                added = [(await json_api(server, "POST", payload={"name": "Favorite " + str(index),
                          "url": url, **metadata}, expected=201))["favorite"]
                         for index, url in enumerate(urls, 1)]
                assert [entry["slot"] for entry in added] == [1, 2]
                assert not requests and not backend.commands and backend.read_property("idle-active") is True
                presets = await listing(server, added)
                client = pywiim.WiiMClient(host="127.0.0.1", port=server.port, protocol="http",
                    timeout=4, capabilities={"retry_count": 1, "supports_metadata": False,
                                                            "supports_presets": True})
                try:
                    await client.get_status()
                    assert await client.get_max_preset_slots() == 2
                    assert await client.get_presets() == presets
                    async with session.request("SUBSCRIBE", server.make_url("/upnp/event/PlayQueue"), headers={
                        "NT": "upnp:event", "CALLBACK": f'<{server.make_url("/fixture-events")}>',
                        "TIMEOUT": "Second-60",
                    }) as response:
                        assert response.status == 200
                        sid = response.headers["SID"]
                    for slot, url in enumerate(urls, 1):
                        marker = len(receipts)
                        await client.play_preset(slot, index=1)
                        evidence["decoded"].append(await _decoded(backend, url))
                        await wire(server, url)
                        await _eventually("received favorite source and title event", lambda: any(
                            event_sid == sid and properties.get("TrackURI") == url
                            and properties.get("TrackTitle") == metadata["title"]
                            and properties.get("TrackSource") == "Preset"
                            for event_sid, properties in receipts[marker:]))
                    assert evidence["decoded"][0]["native_id"] != evidence["decoded"][1]["native_id"]
                    generation, commands = api.state.generation, list(backend.commands)
                    playlist, queue = backend.read_property("playlist"), api.state.queue
                    renamed = (await json_api(server, "PATCH", "/" + added[1]["id"],
                               {"name": "Renamed 日本語"}))["favorite"]
                    await json_api(server, "DELETE", "/" + added[0]["id"])
                    renamed["slot"] = 1
                    assert await client.get_presets() == await listing(server, [renamed])
                    saved = (await json_api(server, "POST", "/current", {"name": "Saved current"}, 201))["favorite"]
                    assert saved["url"] == urls[1]
                    assert {key: saved[key] for key in metadata} == metadata
                    await listing(server, [renamed, saved])
                    # Removing the currently playing library record cannot
                    # revoke the independent queue or replace native playback.
                    await json_api(server, "DELETE", "/" + renamed["id"])
                    saved["slot"] = 1
                    persisted = [saved]
                    await listing(server, persisted)
                    progress = await _decoded(backend, urls[1])
                    assert progress["native_id"] == evidence["decoded"][-1]["native_id"]
                    assert progress["position"] > evidence["decoded"][-1]["position"]
                    assert api.state.generation == generation and api.state.queue is queue
                    assert backend.commands == commands and backend.read_property("playlist") == playlist
                    await wire(server, urls[1])
                    evidence["edits"] = {"rename_delete_save_did_not_restart": True,
                                         "generation_unchanged": True, "progress": progress,
                                         "canonical_saved_url": saved["url"]}
                finally:
                    await client.close()
                api._resolve_station_id = resolve
                queue = PlaybackQueue(source="newTuneIn", stream_id="s12345",
                                      list_name=metadata["title"], artwork=metadata["artwork"])
                await api._materialize_radio_queue(queue)
                queue.current.artist, queue.current.album = metadata["artist"], metadata["album"]
                await api._play_queue_index(1, queue.name)
                radio_before = await _decoded(backend, urls[0])
                generation, commands = api.state.generation, list(backend.commands)
                radio = (await json_api(server, "POST", "/current", {"name": "Saved public station"}, 201))["favorite"]
                assert radio["url"] == reference and {key: radio[key] for key in metadata} == metadata
                assert api.state.generation == generation and api.state.queue is queue
                assert backend.commands == commands and resolutions == [urls[0]]
                assert (await _decoded(backend, urls[0]))["native_id"] == radio_before["native_id"]
                persisted.append(radio)
                await listing(server, persisted)
                disk = (directory / "state" / "favorites.json").read_bytes()
                stored = json.loads(disk)["favorites"]
                assert stored[0]["url"] == urls[1] and stored[1]["url"] == reference
                assert set(stored[1]) == {"id", "name", "url", *metadata}
                assert urls[0] not in json.dumps(stored[1]) and urls[1] not in json.dumps(stored[1])

            # Recreate both the real native handle and HttpApi from the same
            # state directory, while the independent media server stays up.
            requests_before = list(requests)
            async with _renderer(library, directory, tracks, urls, receipts, cleanup) as (api, backend, server):
                api._resolve_station_id = resolve
                await listing(server, persisted)
                await asyncio.sleep(api._monitor_interval + .05)
                assert (directory / "state" / "favorites.json").read_bytes() == disk
                assert requests == requests_before and backend.commands == []
                assert resolutions == [urls[0]]
                assert backend.read_property("idle-active") is True and backend.read_property("playlist") == []
                assert api.state.phase == PlaybackPhase.STOPPED and api.state.generation == 0
                evidence["restart"] = {"library_restored": True, "no_automatic_audio": True,
                                       "observed_seconds": api._monitor_interval + .05}
                assert await http(server, "MCUKeyShortClick:1:1") == "OK"
                evidence["decoded"].append(await _decoded(backend, urls[1]))
                await wire(server, urls[1])
                await listing(server, persisted)
                station_target[0] = urls[1]
                client = pywiim.WiiMClient(host="127.0.0.1", port=server.port, protocol="http",
                    timeout=4, capabilities={"retry_count": 1, "supports_metadata": False,
                                                            "supports_presets": True})
                try:
                    await client.get_status()
                    assert (await client.get_presets())[1]["url"] == reference
                    await client.play_preset(2, index=1)
                    radio_after = await _decoded(backend, urls[1])
                    assert radio_after["native_id"] != evidence["decoded"][-1]["native_id"]
                    item = api.state.queue.current
                    assert item.url == api.state.current_url == reference
                    assert item.resolved_url == api.state.resolved_url == urls[1]
                    assert item._radio_station == (reference, "s12345")
                    assert item.stream_type == "station" and item.duration == api.state.totlen == 0
                    assert item.title_is_fallback is False and api._station_recovery is not None
                    assert resolutions == list(urls)
                    await wire(server, reference)
                    evidence["radio"] = {"canonical_saved_url": reference, "save_did_not_restart": True,
                        "no_resolution_at_restart": True, "fresh_resolution_after_restart": True,
                        "decoded": [radio_before, radio_after], "station_recovery_armed": True,
                        "authoritative_display_retained": True, "resolver_calls": len(resolutions)}
                finally:
                    await client.close()
            assert all(any(path.startswith("/" + track.name + "?") for path in requests) for track in tracks)
            evidence["media_requests"] = requests
            evidence["received_events"] = len(receipts)
        assert session.closed
    finally:
        await media_server.close()
        assert media_server.closed and not media_server.runner.sites
        assert all(sock.fileno() == -1 for sock in media_sockets)
    await asyncio.sleep(0)
    assert not [task for task in asyncio.all_tasks() - baseline if not task.done()]
    evidence["no_pending_tasks"] = True
    evidence["elapsed_seconds"] = round(asyncio.get_running_loop().time() - started, 3)
    return evidence


@pytest.mark.skipif(not os.environ.get("ORR_TEST_LIBMPV_PATH"), reason="Native favorites require ORR_TEST_LIBMPV_PATH")
def test_real_favorites_client_playback_edits_and_restart(tmp_path):
    library = Path(os.environ["ORR_TEST_LIBMPV_PATH"]).expanduser().resolve()
    assert library.is_file()
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--probe", str(library), str(tmp_path)],
        cwd=root, env={**os.environ, "PYTHONPATH": str(root) + os.pathsep + os.environ.get("PYTHONPATH", "")},
        text=True, capture_output=True, timeout=18, check=False)
    assert completed.returncode == 0, f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    evidence = json.loads(completed.stdout)
    assert len(evidence["decoded"]) == 3 and len(evidence["cleanup"]) == 2 and evidence["no_pending_tasks"]
    assert len(evidence["radio"]["decoded"]) == 2 and evidence["radio"]["resolver_calls"] == 2
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 4 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")

    async def main():
        async with asyncio.timeout(16):
            print(json.dumps(await _exercise(sys.argv[2], Path(sys.argv[3])), sort_keys=True))

    asyncio.run(main())
