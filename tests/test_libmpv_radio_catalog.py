"""Opt-in real libmpv/null catalog playback; synthetic directory and local audio.

Production HTTP, selected-source proxy, forced demuxers and queue owner are real.
The existing ctypes transport substitutes only Unix IPC. No provider/hardware
claim. Run with ORR_TEST_LIBMPV_PATH and ffmpeg available; the child is bounded.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import locale
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import quote, urlsplit
from unittest.mock import patch

from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.test_utils import TestServer
import pytest

from fake_linkplay import radio_catalog
from fake_linkplay.config import Config
from fake_linkplay.cli import _start_catalog_proxy
from fake_linkplay.http_api import HttpApi
from fake_linkplay.network_policy import ProxyTarget, resolve_proxy_target
from fake_linkplay.state import RendererState
from fake_linkplay.web_ui import PREFIX
from test_libmpv_queue_staging import _StagingBackend
from test_libmpv_integration import _write_tone, _eventually
from test_libmpv_live_metadata import ChangingICY, TITLES
from test_libmpv_formats import _decoder_progress
from tools.native_probe_supervisor import run_probe


async def exercise(library: str, directory: Path):
    tone = directory / "tone.wav"
    _write_tone(tone, 440)
    for suffix, codec in (("mp3", "libmp3lame"), ("aac", "aac")):
        options = (["-b:a", "64k", "-write_xing", "0", "-id3v2_version", "0"] if suffix == "mp3" else [])
        subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-i", str(tone), "-map_metadata", "-1",
                        "-c:a", codec, *options, str(directory / ("tone." + suffix))],
                        check=True, timeout=15, capture_output=True)
    for index, duration in ((1, 4), (2, 6)):
        subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                        f"sine=frequency={index * 440}:sample_rate=48000:duration={duration}",
                        "-c:a", "libvorbis", "-q:a", "3", "-metadata", f"title=Synthetic song {index}",
                        "-fflags", "+bitexact", "-serial_offset", str(index), str(directory / f"part{index}.ogg")],
                       check=True, timeout=15, capture_output=True)
    chained = b"".join((directory / f"part{index}.ogg").read_bytes() for index in (1, 2))
    icy = ChangingICY(directory / "tone.mp3")
    requests = []
    async def media(request):
        requests.append(request.path)
        if request.path == "/icy.mp3":
            return await icy.handle(request)
        if request.path == "/chained.ogg":
            response = web.StreamResponse(headers={"Content-Type": "audio/ogg", "Accept-Ranges": "none"})
            await response.prepare(request)
            for offset in range(0, len(chained), 1024):
                await response.write(chained[offset:offset + 1024]); await asyncio.sleep(.015)
            await response.write_eof()
            return response
        if request.path == "/bad.m3u":
            return web.Response(text=f"#EXTM3U\nhttp://127.0.0.1:{media_server.port}/private\n", content_type="audio/x-mpegurl")
        return web.FileResponse(directory / ("tone.aac" if request.path.endswith("aac") else "tone.mp3"))
    media_app = web.Application(); media_app.router.add_get("/{path}", media)
    media_server = TestServer(media_app)
    backend = _StagingBackend(library, ())
    server = None
    proxy_runners = []
    try:
        await media_server.start_server()
        cfg = Config(uuid="00000000-0000-4000-8000-000000000000", advertise_ip="127.0.0.1",
                     state_dir=str(directory / "state"), bind_address="127.0.0.2",
                     proxy_on_demand_audio=False, range_seek_reload=False)
        api = HttpApi(cfg, RendererState.from_config(cfg), backend)
        server = TestServer(api.app(), host="127.0.0.2"); await server.start_server(); cfg.http_api_port = server.port
        await _start_catalog_proxy(api, proxy_runners)
        assert api.catalog_proxy_port and api.catalog_proxy_port != server.port
        origin = str(server.make_url("/")).rstrip("/")
        station = radio_catalog.CatalogStation("00000000-0000-4000-8000-000000000000", "Synthetic radio", "DE", "MP3", 128,
                    f"http://media.example.test:{media_server.port}/tone.mp3")
        async def query(_):
            # Keep production provider normalization and codec admission in this
            # native path; only directory transport and DNS are synthetic.
            return radio_catalog._stations(json.dumps([{
                "stationuuid": station.uuid, "name": station.name, "countrycode": station.country,
                "codec": station.codec, "bitrate": station.bitrate, "url_resolved": station.url,
                "hls": 0, "lastcheckok": 1}]).encode()), "synthetic.api.radio-browser.info"
        async def fetch(_):
            return b'{}'  # The optional provider click acknowledgement is unused.
        async def resolve(url, allowed_lan_hosts=()):
            parsed = urlsplit(url)
            if parsed.hostname == "media.example.test" and parsed.port == media_server.port:
                assert not allowed_lan_hosts
                return ProxyTarget(url, parsed.hostname, parsed.port, ("127.0.0.1",))
            return await resolve_proxy_target(url, allowed_lan_hosts)
        for path in ("tone.mp3", "tone.aac", "chained.ogg", "icy.mp3", "bad.m3u"):
            url = f"http://media.example.test:{media_server.port}/{path}"
            backend._allowed_media.add(f"http://127.0.0.1:{api.catalog_proxy_port}/proxy/audio?url={quote(url, safe='')}")
        with patch.object(radio_catalog, "_query", query), patch.object(radio_catalog, "_fetch", fetch), \
             patch.object(radio_catalog, "resolve_proxy_target", resolve), \
             patch("fake_linkplay.http_api.resolve_proxy_target", resolve):
            async with ClientSession(timeout=ClientTimeout(total=15), trust_env=False) as session:
                async def snapshot():
                    async with session.get(origin + PREFIX + "/state") as response:
                        assert response.status == 200; return await response.json()
                token = (await snapshot())["csrf_token"]
                async def post(path, payload, expected=200):
                    async with session.post(origin + PREFIX + path, json=payload,
                            headers={"Origin": origin, "X-ORR-CSRF": token}) as response:
                        body = await response.json(); assert response.status == expected, body; return body
                result = await post("/radio/search", {"action": "search", "name": "Synthetic", "country": "DE"})
                selection = result["stations"][0]["id"]
                saved = await post("/command", {"action": "favorite_radio", "id": selection})
                assert not requests and backend.read_property("idle-active")
                ordinary_url = str(media_server.make_url("/tone.mp3"))
                backend._allowed_media.add(ordinary_url)
                ordinary = api.favorites.add({"name": "Prior ordinary source", "url": ordinary_url})
                saved_before = api.favorites.document()
                failed_replacements = []
                retired_before_lookup = []
                async def unavailable(_, **kwargs):
                    retired_before_lookup.append(backend.read_property("idle-active") and not backend.read_property("playlist"))
                    raise radio_catalog.CatalogError("Synthetic directory outage")
                for paused in (False, True):
                    await post("/command", {"action": "favorite_play", "id": ordinary["id"]})
                    await _decoder_progress(backend, ordinary_url, "mp3")
                    if paused:
                        await post("/command", {"action": "pause", "expected_generation": api.state.generation})
                        assert backend.read_property("pause")
                    with patch("fake_linkplay.http_api.resolve_catalog_station", unavailable):
                        async with session.post(origin + "/orr/v1/favorites/" + saved["favorite_id"] + "/play") as response:
                            assert response.status == 502
                            assert (await response.json())["error"] == "playback_failed"
                    assert backend.read_property("idle-active") and not backend.read_property("playlist")
                    assert retired_before_lookup[-1]
                    assert api.state.backend_url == api.state.backend_original_url == ""
                    assert api.state.current_url == api.state.queue.current.url == station.reference
                    view = await snapshot()
                    assert view["transport"]["phase"] == "ERROR" and view["actions"]["stop"]
                    assert not view["actions"]["pause"]
                    assert api.favorites.document() == saved_before
                    failed_replacements.append({"prior": "paused" if paused else "playing", "native_idle": True,
                                                "native_playlist_empty": True, "favorites_preserved": True})
                evidence = []
                for codec, path, decoder in (("MP3", "tone.mp3", "mp3"), ("AAC", "tone.aac", "aac"),
                                             ("OGG", "chained.ogg", "vorbis")):
                    station = replace(station, codec=codec, url=f"http://media.example.test:{media_server.port}/{path}")
                    await post("/command", {"action": "favorite_play", "id": saved["favorite_id"]})
                    progress = await _decoder_progress(backend, api.state.backend_url, decoder)
                    entry = {"codec": codec, "native_audio": True, "output": "null"}
                    if codec == "OGG":
                        native_id = backend.read_property("playlist")[0]["id"]
                        assert api.state.queue.current.content_type == "audio/ogg"
                        assert backend.read_property("options/demuxer-lavf-format") == "ogg"
                        assert backend.read_property("options/ytdl") is False
                        assert backend.read_property("media-title") == "Synthetic song 1"
                        await _eventually("catalog chained Vorbis title and progress", lambda:
                            backend.read_property("media-title") == "Synthetic song 2"
                            and backend.read_property("playback-time") > 4.7, timeout=9)
                        second = await _decoder_progress(backend, api.state.backend_url, "vorbis")
                        assert len(backend.read_property("playlist")) == 1
                        assert backend.read_property("playlist")[0]["id"] == native_id
                        assert requests.count("/chained.ogg") == 1
                        assert api.state.current_url == api.state.queue.current.url == station.reference
                        assert api.state.queue.current.title == api.state.title == "Synthetic radio"
                        entry.update({"chained_vorbis": True, "first_position": progress["after"],
                                      "second_position": second["after"], "title_changed": True,
                                      "single_source_request": True, "native_entry_unchanged": True})
                    evidence.append(entry)
                    await post("/command", {"action": "stop"})
                    assert backend.read_property("idle-active")
                station = replace(station, codec="MP3", url=station.url.replace("chained.ogg", "icy.mp3"))
                await post("/command", {"action": "favorite_play", "id": saved["favorite_id"]})
                queue, item, generation = api.state.queue, api.state.queue.current, api.state.generation
                native_id = backend.read_property("playlist")[0]["id"]
                saved_before = api.favorites.document()
                def loads():
                    return [command for command in backend.commands
                        if (command.get("name") if isinstance(command, dict) else command[0]) == "loadfile"]
                load_count = len(loads())
                titles = []
                for title in TITLES[:2]:
                    icy.title = title
                    await _eventually("catalog-proxied ICY title", lambda: backend.read_property("media-title") == title, timeout=6)
                    progress = await _decoder_progress(backend, api.state.backend_url, "mp3")
                    assert api.state.queue is queue and queue.current is item and api.state.generation == generation
                    assert api.state.current_url == item.url == station.reference
                    assert item.title == api.state.title == "Synthetic radio"
                    assert api.favorites.document() == saved_before
                    assert backend.read_property("playlist")[0]["id"] == native_id
                    assert len(loads()) == load_count
                    titles.append({"title": title, "position": progress["after"]})
                assert len(icy.requests) == 1 and icy.requests[0]["icy"] == "1"
                assert set(TITLES[:2]).issubset(icy.sent_titles)
                await post("/command", {"action": "stop"})
                await _eventually("catalog ICY source cleanup", icy.finished.is_set, timeout=3)
                station = replace(station, url=station.url.replace("icy.mp3", "bad.m3u"))
                for codec in ("MP3", "OGG"):
                    station = replace(station, codec=codec)
                    before = requests.count("/bad.m3u")
                    await post("/command", {"action": "favorite_play", "id": saved["favorite_id"]}, expected=502)
                    assert requests.count("/bad.m3u") == before + 1 and "/private" not in requests
                    assert backend.read_property("idle-active")
                assert api.favorites.playback_snapshot(saved["favorite_id"])["url"] == station.reference
                return {"decoded": evidence, "favorite_reresolved": True, "nested_playlist_not_fetched": True,
                        "failed_favorite_replacements": failed_replacements,
                        "specific_bind_ephemeral_proxy": True, "hostile_playlist_formats": ["MP3", "OGG"], "icy_titles": titles,
                        "icy_single_source_load": True, "icy_canonical_favorite_preserved": True}
    finally:
        if server is not None:
            await server.close()
        for runner in proxy_runners:
            await runner.cleanup()
        backend.close(); await media_server.close()


def test_real_libmpv_radio_catalog(tmp_path):
    library = os.environ.get("ORR_TEST_LIBMPV_PATH")
    if not library:
        pytest.skip("Real libmpv requires ORR_TEST_LIBMPV_PATH")
    assert shutil.which("ffmpeg"), "Explicit catalog decoder lane requires ffmpeg"
    result = run_probe([sys.executable, str(Path(__file__).resolve()), "--probe", library, str(tmp_path)],
                       timeout=60, cwd=Path(__file__).resolve().parents[1], env=os.environ.copy())
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout.strip().splitlines()[-1])
    assert evidence["nested_playlist_not_fetched"] and evidence["favorite_reresolved"]
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    locale.setlocale(locale.LC_NUMERIC, "C")
    async def main():
        async with asyncio.timeout(50):
            return await exercise(sys.argv[2], Path(sys.argv[3]))
    print(json.dumps(asyncio.run(main()), sort_keys=True))
