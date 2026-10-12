"""Optional REAL_BACKEND/null: changing ICY titles on one paced source.

Generated finite MP3, literal loopback HTTP, real SOAP/GENA and native decoding.
Only command transport is replaced by the existing public libmpv-node fixture.
No production Unix IPC, provider services or physical audio are exercised.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import locale
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
from typing import AsyncIterator
from xml.etree import ElementTree as ET

import pytest

from aiohttp import ClientSession, ClientTimeout, web

from fake_linkplay.config import Config
from fake_linkplay.mpv_backend import LIVE_STATION_BACK_BYTES
from fake_linkplay.http_api import HttpApi
from fake_linkplay.state import RendererState
from test_libmpv_formats import _generate_media, _decoder_progress
from test_libmpv_recovery import _StationBackend
from test_libmpv_integration import _eventually
from test_metadata_surfaces import (MetadataRenderer, assert_surfaces, didl,
    playlist, property_fields, ARTIST, ALBUM, ARTWORK, OPAQUE_ID)

TITLES = ("First artist & duo - One <tone>", "Second artist - Two & <tone>",
          "Third artist - Final <tone> & orchestra")
STATION_TITLE = "Canonical station & <radio>"


class ChangingICY:
    def __init__(self, media: Path):
        self.audio = media.read_bytes() * 2
        self.title = "Bootstrap artist - Header probe tone"
        self.requests = []
        self.sent_titles = []
        self.finished = asyncio.Event()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append({"method": request.method, "icy": request.headers.get("Icy-MetaData"),
                              "range": request.headers.get("Range")})
        assert request.method == "GET"
        assert request.headers.get("Icy-MetaData") == "1"
        headers = {"Content-Type": "audio/mpeg", "icy-metaint": "8192",
            "icy-name": "Synthetic loopback station", "icy-br": "64"}
        response = web.StreamResponse(headers=headers)
        await response.prepare(request)
        # 40 seconds of finite synthesized MP3, delivered at its 64 kb/s rate.
        # The source remains open until completion or owner Stop; never reload
        # or switch decoder resources merely to cause a title change.
        try:
            for offset in range(0, len(self.audio), 8192):
                chunk = self.audio[offset:offset + 8192]
                payload = bytearray(chunk)
                if len(chunk) == 8192:
                    metadata = f"StreamTitle='{self.title}';".encode()
                    units = 8
                    payload.extend(bytes([units]) + metadata.ljust(units * 16, b"\0"))
                    self.sent_titles.append(self.title)
                await response.write(payload)
                await asyncio.sleep(len(chunk) / 8000)
            await response.write_eof()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.finished.set()
        return response


@asynccontextmanager
async def _renderer(library: str, media: dict, directory: Path) -> AsyncIterator[tuple]:
    baseline_tasks = asyncio.all_tasks()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(socket.SOMAXCONN)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    url = base + "/_fixture/changing-icy"
    fixture = ChangingICY(media["mp3"])
    backend = runner = client = monitor = None
    events = {"AVTransport": [], "PlayQueue": []}

    async def callback(request):
        assert request.headers["NT"] == "upnp:event"
        assert request.headers["NTS"] == "upnp:propchange"
        events[request.match_info["service"]].append(property_fields(await request.text()))
        return web.Response()

    try:
        backend = _StationBackend(library, (media["wav"], media["wav"]), url)
        backend._allowed_media = {url}
        cfg = Config(uuid="00000000-0000-4000-8000-000000000000", advertise_ip="127.0.0.1",
            bind_address="127.0.0.1", http_api_port=port, upnp_port=port,
            state_dir=str(directory / "state"), auto_spawn_mpv=False,
            enable_ssdp=False, enable_mdns=False)
        api = HttpApi(cfg, RendererState.from_config(cfg), backend)
        app = api.app()
        app.router.add_get("/_fixture/changing-icy", fixture.handle)
        app.router.add_route("NOTIFY", "/synthetic/events/{service}", callback)
        runner = web.AppRunner(app, shutdown_timeout=1)
        await runner.setup()
        await web.SockSite(runner, listener).start()
        monitor = api._state_monitor_task
        async with ClientSession(timeout=ClientTimeout(total=15), trust_env=False) as client:
            for service in events:
                async with client.request("SUBSCRIBE", base + "/upnp/event/" + service, headers={
                    "NT": "upnp:event", "CALLBACK": f"<{base}/synthetic/events/{service}>",
                    "TIMEOUT": "Second-60"}) as response:
                    assert response.status == 200
            yield MetadataRenderer(api, backend, client, base, events), fixture, url
    finally:
        try:
            if runner is not None:
                await runner.cleanup()
        finally:
            listener.close()
            if backend is not None:
                backend.close()
        await asyncio.sleep(0)
        pending = [task for task in asyncio.all_tasks() - baseline_tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        assert not pending, f"Leaked tasks: {pending!r}"
        assert client is None or client.closed
        assert listener.fileno() == -1
        assert runner is None or not runner.sites
        assert backend is None or backend._handle is None
        assert monitor is None or monitor.done()


async def _exercise(library: str, ffmpeg: str, directory: Path, scenario: str) -> dict:
    media = _generate_media(ffmpeg, directory)
    evidence = {"lane": "REAL_BACKEND/null/changing-ICY", "scenario": scenario, "updates": []}
    async with _renderer(library, media, directory) as (env, fixture, url):
        api, backend = env.api, env.backend
        metadata = ""
        explicit = scenario == "explicit"
        if scenario != "fallback":
            metadata = didl(title=STATION_TITLE if explicit else "").replace(
                ' duration="00:02:03.900"', '')
            if not explicit:
                metadata = metadata.replace("<dc:title></dc:title>", "")
        await env.soap("CreateQueue", {"QueueContext": playlist([{
            "Id": OPAQUE_ID, "URL": url, "Title": STATION_TITLE if explicit else "",
            "Artist": ARTIST, "Album": ALBUM, "PicUrl": ARTWORK,
            "Metadata": metadata, "StreamType": "station", "ContentType": "audio/mpeg",
        }], source="SyntheticRadio")}, "PlayQueue")
        await env.soap("PlayQueueWithIndex", {"Index": "1"}, "PlayQueue")
        generation = api.state.generation
        queue = api.state.queue
        item = queue.current
        assert item.stream_type == "station" and item.duration == 0
        assert backend.read_property("options/demuxer-max-back-bytes") == LIVE_STATION_BACK_BYTES
        assert backend.read_property("options/demuxer-donate-buffer") is False
        native_entry_id = backend.read_property("playlist")[0]["id"]
        last_position = -1
        for title in TITLES:
            lengths = {service: len(events) for service, events in env.events.items()}
            fixture.title = title
            try:
                await _eventually("updated actual ICY title", lambda: backend.read_property("media-title") == title, timeout=6)
            except AssertionError:
                print(json.dumps({"wanted": title, "native_title": backend.read_property("media-title"),
                    "native_metadata": backend.read_property("metadata"), "snapshot": await backend.snapshot(),
                    "requests": fixture.requests, "sent_titles": fixture.sent_titles,
                    "previous_updates": evidence["updates"]}, default=str), file=sys.stderr)
                raise
            expected = STATION_TITLE if explicit else title
            await _eventually("autonomous renderer title", lambda: api.state.title == expected, timeout=3)
            if not explicit:
                # Check genuine callback delivery BEFORE status/SOAP reads can
                # call sync_mpv_state. This proves autonomous monitor behavior.
                await _eventually("autonomous changing PlayQueue event", lambda: any(
                    event.get("TrackTitle") == expected for event in env.events["PlayQueue"][lengths["PlayQueue"]:]), timeout=3)
                if not metadata:
                    await _eventually("autonomous changing AVTransport event", lambda: any(
                        expected in ET.fromstring(event["TrackMetaData"]).itertext()
                        for event in env.events["AVTransport"][lengths["AVTransport"]:] if event.get("TrackMetaData")), timeout=3)
            else:
                # An explicit title must survive two normal monitor passes.
                await asyncio.sleep(2 * api._monitor_interval + 0.05)
            progress = await _decoder_progress(backend, url, "mp3")
            assert progress["after"] > last_position
            last_position = progress["after"]
            assert api.state.generation == generation and api.state.queue is queue and queue.current is item
            assert queue.index == queue.count == 1 and api.state.current_url == url
            assert backend.loads == [url]
            assert backend.read_property("playlist")[0]["id"] == native_entry_id
            assert backend.read_property("eof-reached") is False
            assert not fixture.finished.is_set()
            actual = await assert_surfaces(env, uri=url, title=expected, artist=ARTIST,
                album=ALBUM, artwork=ARTWORK, metadata=metadata or None,
                source="SyntheticRadio", item_id=OPAQUE_ID)
            if not metadata:
                assert ET.fromstring(actual).find(".//{http://purl.org/dc/elements/1.1/}title").text == title
            queue_event = await env.event("PlayQueue", uri=url, title=expected)
            assert ET.fromstring(queue_event["QueueContext"]).findtext("Tracks/Track1/Title") == expected
            assert item.metadata == metadata
            assert item.title_is_fallback is not explicit
            evidence["updates"].append({"native_media_title": title, "native_metadata": backend.read_property("metadata"),
                "renderer_title": expected, "playback_before": progress["before"], "playback_after": progress["after"],
                "event_counts": {service: len(events) for service, events in env.events.items()},
                "opaque_didl_exact": bool(metadata), "generated_didl_follows_title": not bool(metadata),
                "all_http_soap_gena_surfaces": True})
        assert len(fixture.requests) == 1, fixture.requests
        assert set(TITLES).issubset(set(fixture.sent_titles))
        evidence.update(version=backend.read_property("mpv-version"), history_limit=backend.read_property("options/demuxer-max-back-bytes"), same_source_loads=len(backend.loads),
            same_native_playlist_entry=True, icy_http_requests=fixture.requests,
            title_changes_observed_before_client_status_polls=not explicit,
            canonical_identity_preserved=True, final_position=last_position,
            opaque_didl_preserved=bool(metadata), native_title_never_split_into_artist=True,
            station_title_preserved=explicit)
        await env.soap("Stop")
        assert backend.read_property("idle-active") is True
        await _eventually("stream source cleanup", fixture.finished.is_set, timeout=3)
    evidence["cleanup"] = {"handle_closed": True, "listener_closed": True, "no_pending_tasks": True}
    return evidence


@pytest.mark.skipif(not os.environ.get("ORR_TEST_LIBMPV_PATH"),
                    reason="REAL_BACKEND/null changing ICY requires ORR_TEST_LIBMPV_PATH")
@pytest.mark.parametrize("scenario", ["fallback", "explicit", "opaque"])
def test_live_icy_changes_preserve_canonical_metadata(tmp_path: Path, scenario: str) -> None:
    library = Path(os.environ["ORR_TEST_LIBMPV_PATH"]).expanduser().resolve()
    assert library.is_file()
    ffmpeg = shutil.which(os.environ.get("ORR_TEST_FFMPEG_BINARY", "ffmpeg"))
    assert ffmpeg, "Opt-in changing ICY tests require official FFmpeg"
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    for name in tuple(env):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            env.pop(name)
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--probe",
        str(library), ffmpeg, str(tmp_path), scenario], cwd=root, env=env,
        text=True, capture_output=True, timeout=45, check=False)
    assert result.returncode == 0, f"Changing ICY failed\n{result.stdout}\n{result.stderr}"
    evidence = json.loads(result.stdout)
    assert evidence["history_limit"] == LIVE_STATION_BACK_BYTES
    assert evidence["same_source_loads"] == 1 and len(evidence["updates"]) == 3
    assert evidence["cleanup"] == {"handle_closed": True, "listener_closed": True, "no_pending_tasks": True}
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    locale.setlocale(locale.LC_NUMERIC, "C")
    assert len(sys.argv) == 6 and sys.argv[1] == "--probe"
    async def main():
        async with asyncio.timeout(40):
            evidence = await _exercise(sys.argv[2], sys.argv[3], Path(sys.argv[4]), sys.argv[5])
        print(json.dumps(evidence, sort_keys=True))
    asyncio.run(main())
