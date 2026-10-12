"""Optional REAL_BACKEND/null radio formats, generated and served on loopback.

Opt in with ORR_TEST_LIBMPV_PATH=/absolute/path/to/libmpv.so.2 and run this file.
The official FFmpeg executable must be on PATH (or ORR_TEST_FFMPEG_BINARY must
point to it), with libmp3lame, AAC and HLS support. Missing opt-in dependencies
fail. All media is synthesized at runtime; no recordings or binary fixtures
are committed. FFmpeg is a test dependency, not a renderer dependency.

The real HTTP API, MpvBackend control/readiness/snapshot logic and libmpv
decoder run unchanged. Only command transport uses the test ctypes adapter.
This does not validate UNIX IPC, real radio services, encrypted/authenticated
HLS, physical audio or WAN networking. Native calls have child-process hard
timeouts, output is always null, and every fixture URL is literal loopback.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from html import escape
import json
import locale
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from typing import Any, AsyncIterator
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree as ET

import pytest
from aiohttp import ClientSession, ClientTimeout, web

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.queue import PlaybackQueue, QueueItem, serialize_queue_context
from fake_linkplay.state import PlaybackPhase, RendererState
from test_libmpv_integration import _LibmpvBackend, _eventually, _write_tone


_ICY_TITLE = "Synthetic artist & orchestra - Loopback <tone>"
_OPT_IN = pytest.mark.skipif(
    not os.environ.get("ORR_TEST_LIBMPV_PATH"),
    reason="REAL_BACKEND/null radio formats require explicit ORR_TEST_LIBMPV_PATH",
)


def _generate_media(ffmpeg: str, directory: Path) -> dict[str, Path]:
    wav = directory / "synthetic-tone.wav"
    _write_tone(wav, 440)
    media = {"wav": wav, "mp3": directory / "tone.mp3",
             "aac": directory / "tone.aac", "hls": directory / "station.m3u8"}

    def encode(arguments: list[str]) -> None:
        completed = subprocess.run(
            [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *arguments],
            text=True, capture_output=True, timeout=15, check=False,
        )
        assert completed.returncode == 0, f"Synthetic FFmpeg generation failed: {completed.stderr}"

    encode(["-i", str(wav), "-t", "20", "-map_metadata", "-1", "-threads", "1",
            "-c:a", "libmp3lame", "-b:a", "64k", "-write_xing", "0",
            "-id3v2_version", "0", str(media["mp3"])])
    encode(["-i", str(wav), "-t", "20", "-map_metadata", "-1", "-threads", "1",
            "-c:a", "aac", "-b:a", "64k", "-f", "adts", str(media["aac"])])
    encode(["-i", str(media["aac"]), "-map_metadata", "-1", "-c:a", "copy",
            "-hls_time", "2", "-hls_list_size", "0", "-hls_segment_filename",
            str(directory / "segment-%03d.ts"), str(media["hls"])])
    assert not media["mp3"].read_bytes().startswith(b"ID3")
    assert media["aac"].read_bytes()[:2] == b"\xff\xf1", "Expected untagged ADTS AAC"
    manifest = media["hls"].read_text()
    assert "#EXT-X-ENDLIST" in manifest and "#EXT-X-KEY" not in manifest
    assert all(line.startswith("segment-") and ":" not in line and "/" not in line
               for line in manifest.splitlines() if line and not line.startswith("#"))
    return media


class _RadioBackend(_LibmpvBackend):
    def __init__(self, library: str, media: dict[str, Path], allowed_urls: set[str]) -> None:
        super().__init__(library, (media["wav"], media["wav"]))
        self._allowed_media = set(allowed_urls)
        self.loads: list[str] = []

    async def command(self, args: list[Any], timeout: float = 5.0) -> dict[str, Any]:
        if args[0] == "loadfile":
            assert args[1] in self._allowed_media, "Only exact loopback fixture URLs may be loaded"
            self.loads.append(args[1])
        return await super().command(args, timeout)


class _RadioFixture:
    def __init__(self, base: str, media: dict[str, Path]) -> None:
        self.base = base
        self.media = media
        self.requests: list[dict[str, str]] = []
        self.responses: dict[str, tuple[bytes, str]] = {
            "/_fixture/tone.mp3": (media["mp3"].read_bytes(), "audio/mpeg"),
            "/_fixture/tone.aac": (media["aac"].read_bytes(), "audio/aac"),
            "/_fixture/opaque": (media["mp3"].read_bytes(), "application/octet-stream"),
            "/_fixture/station.m3u8": (media["hls"].read_bytes(), "application/vnd.apple.mpegurl"),
            "/_fixture/station.m3u": (
                b"#EXTM3U\n#EXTINF:-1,Synthetic M3U station\ntone.mp3\n", "audio/x-mpegurl"),
            "/_fixture/station.pls": (
                b"[playlist]\nNumberOfEntries=1\nFile1=tone.aac\nTitle1=Synthetic PLS station\nLength1=-1\nVersion=2\n",
                "audio/x-scpls"),
            "/_fixture/two-entries.m3u": (
                b"#EXTM3U\n#EXTINF:-1,Synthetic first\ntone.mp3\n#EXTINF:-1,Synthetic second\ntone.aac\n",
                "audio/x-mpegurl"),
            "/_fixture/two-entries.pls": (
                b"[playlist]\nNumberOfEntries=2\nFile1=tone.aac\nLength1=-1\nFile2=tone.mp3\nLength2=-1\nVersion=2\n",
                "audio/x-scpls"),
        }
        for segment in sorted(media["hls"].parent.glob("segment-*.ts")):
            self.responses[f"/_fixture/{segment.name}"] = (segment.read_bytes(), "video/mp2t")
        self.allowed_urls = {base + path for path in self.responses} | {
            base + "/_fixture/redirect", base + "/_fixture/loop", base + "/_fixture/icy",
        }
        for url in self.allowed_urls:
            parts = urlsplit(url)
            assert parts.scheme == "http" and parts.hostname == "127.0.0.1"
            assert parts.port == urlsplit(base).port and not parts.query and not parts.fragment

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append({"path": request.path, "raw_path": request.raw_path,
                              "icy_metadata": request.headers.get("Icy-MetaData", "")})
        if request.path == "/_fixture/redirect":
            raise web.HTTPFound(location="/_fixture/tone.mp3")
        if request.path == "/_fixture/loop":
            raise web.HTTPFound(location="/_fixture/loop")
        if request.path == "/_fixture/icy":
            interval = 8192
            audio = self.media["mp3"].read_bytes()
            metadata = f"StreamTitle='{_ICY_TITLE}';".encode("utf-8")
            units = (len(metadata) + 15) // 16
            block = bytes([units]) + metadata.ljust(units * 16, b"\0")
            payload = bytearray()
            for offset in range(0, len(audio), interval):
                chunk = audio[offset:offset + interval]
                payload.extend(chunk)
                if len(chunk) == interval:
                    payload.extend(block)
            return web.Response(body=bytes(payload), headers={
                "Content-Type": "audio/mpeg", "icy-metaint": str(interval),
                "icy-name": "Synthetic loopback station", "icy-br": "64",
            })
        if request.raw_path not in self.responses:
            raise web.HTTPNotFound()
        data, content_type = self.responses[request.raw_path]
        return web.Response(body=data, content_type=content_type)


@asynccontextmanager
async def _renderer(library: str, media: dict[str, Path], directory: Path) -> AsyncIterator[tuple]:
    baseline_tasks = asyncio.all_tasks()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(socket.SOMAXCONN)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    fixture = _RadioFixture(base, media)
    backend: _RadioBackend | None = None
    runner: web.AppRunner | None = None
    session: ClientSession | None = None
    monitor: asyncio.Task | None = None
    try:
        backend = _RadioBackend(library, media, fixture.allowed_urls)
        cfg = Config(
            uuid="00000000-0000-4000-8000-000000000000", advertise_ip="127.0.0.1",
            bind_address="127.0.0.1", http_api_port=port, upnp_port=port,
            state_dir=str(directory / "state"), auto_spawn_mpv=False,
            enable_ssdp=False, enable_mdns=False,
        )
        api = HttpApi(cfg, RendererState.from_config(cfg), backend)
        app = api.app()
        app.router.add_get("/_fixture/{name:.*}", fixture.handle)
        runner = web.AppRunner(app, shutdown_timeout=1)
        await runner.setup()
        await web.SockSite(runner, listener).start()
        monitor = api._state_monitor_task
        assert monitor is not None and not monitor.done()
        async with ClientSession(timeout=ClientTimeout(total=15), trust_env=False) as session:
            yield api, backend, fixture, session
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
        assert not pending, f"Radio format fixture leaked tasks: {pending!r}"
        assert session is None or session.closed
        assert listener.fileno() == -1
        assert runner is None or not runner.sites
        assert backend is None or backend._handle is None
        assert backend is None or backend.process is None
        assert monitor is None or monitor.done()


async def _command(session: ClientSession, base: str, value: str) -> tuple[int, str]:
    async with session.get(base + "/httpapi.asp", params={"command": value}) as response:
        return response.status, await response.text()


async def _queue_soap(session: ClientSession, base: str, action: str, fields: str) -> None:
    namespace = "urn:schemas-wiimu-com:service:PlayQueue:1"
    envelope = (f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                f'<u:{action} xmlns:u="{namespace}">{fields}</u:{action}></s:Body></s:Envelope>')
    async with session.post(base + "/upnp/control/PlayQueue", data=envelope.encode(), headers={
        "SOAPACTION": f'"{namespace}#{action}"', "Content-Type": "text/xml",
    }) as response:
        text = await response.text()
        assert response.status == 200, (action, response.status, text)
        ET.fromstring(text)


async def _decoder_progress(backend: _RadioBackend, path: str, codec: str) -> dict[str, Any]:
    await _eventually("selected decoded audio", lambda: any(
        track.get("type") == "audio" and track.get("selected") is True
        for track in backend.read_property("track-list")))
    assert backend.read_property("current-ao") == "null"
    assert backend.read_property("path") == path
    selected = [track for track in backend.read_property("track-list")
                if track.get("type") == "audio" and track.get("selected") is True]
    assert selected[0]["codec"] == codec, selected
    before = backend.read_property("playback-time")
    await _eventually("advancing decoded radio audio", lambda: backend.read_property("playback-time") > before + 0.25)
    return {"path": path, "codec": selected[0]["codec"], "before": before,
            "after": backend.read_property("playback-time"), "ao": "null"}


async def _exercise(library: str, ffmpeg: str, directory: Path, scenario: str) -> dict[str, Any]:
    media = _generate_media(ffmpeg, directory)
    evidence: dict[str, Any] = {"lane": "REAL_BACKEND/null", "scenario": scenario}
    async with _renderer(library, media, directory) as (api, backend, fixture, session):
        base = fixture.base

        async def play(name: str, codec: str, *, actual_name: str | None = None) -> dict[str, Any]:
            url = base + "/_fixture/" + name
            result = await _command(session, base, "setPlayerCmd:play:" + url)
            assert result == (200, "OK"), (name, result, await backend.snapshot())
            decoded = await _decoder_progress(backend, base + "/_fixture/" + (actual_name or name), codec)
            assert api.state.current_url == url
            assert api.state.queue.current is not None and api.state.queue.current.url == url
            assert api.state.status == "play" and api.state.last_error == ""
            async with session.get(base + "/httpapi.asp", params={"command": "getPlayerStatus"}) as response:
                status = await response.json()
            assert status["status"] == "play" and status["transport_error"] == "", status
            assert status["stream_url"] == url, status
            return {"requested": url, "resolved": api.state.resolved_url,
                    "backend_identity": api.state.backend_url, "decoder": decoded}

        if scenario == "formats":
            evidence["formats"] = {}
            for name, codec in (("tone.mp3", "mp3"), ("tone.aac", "aac"),
                                ("opaque", "mp3"), ("station.m3u8", "aac")):
                evidence["formats"][name] = await play(name, codec)
            await _eventually("HLS decodes across a two-second segment boundary", lambda: (
                backend.read_property("playback-time") > 2.25), timeout=4)
            evidence["formats"]["station.m3u8"]["cross_segment_position"] = backend.read_property("playback-time")
            paths = {item["path"] for item in fixture.requests}
            assert "/_fixture/station.m3u8" in paths
            assert len([path for path in paths if path.endswith(".ts")]) >= 2, paths
            assert backend.loads == [base + "/_fixture/" + name for name in evidence["formats"]]
        elif scenario == "playlists":
            # Prove native playlist expansion independently before HTTP state
            # confirmation. These loads are real commands, not mocked results.
            evidence["native_playlists"] = {}
            async def playlist_properties() -> dict[str, Any]:
                return {name: await backend.get_property(name) for name in (
                    "playlist", "playlist-path", "playlist-pos", "playlist-entry-id",
                    "playlist-current-pos", "playlist-playing-pos", "path")}

            for name, target, codec in (("station.m3u", "tone.mp3", "mp3"),
                                        ("station.pls", "tone.aac", "aac")):
                before_load = await playlist_properties()
                result = await backend.loadfile(base + "/_fixture/" + name)
                assert result["error"] == "success", result
                # Capture immediately after acknowledgement, before yielding
                # to the loopback server that supplies the playlist body.
                acknowledged = await playlist_properties()
                evidence["native_playlists"][name] = await _decoder_progress(
                    backend, base + "/_fixture/" + target, codec)
                playlist_path = backend.read_property("playlist-path")
                assert playlist_path == base + "/_fixture/" + name
                evidence["native_playlists"][name]["playlist_path"] = playlist_path
                evidence["native_playlists"][name]["identity"] = {
                    "before_load": before_load, "acknowledged": acknowledged,
                    "expanded": await playlist_properties(),
                }
                await backend.stop()
            evidence["native_playlist_eof"] = {}
            for name, first, second, first_codec, second_codec in (
                ("two-entries.m3u", "tone.mp3", "tone.aac", "mp3", "aac"),
                ("two-entries.pls", "tone.aac", "tone.mp3", "aac", "mp3"),
            ):
                url = base + "/_fixture/" + name
                assert (await backend.loadfile(url))["error"] == "success"
                await _decoder_progress(backend, base + "/_fixture/" + first, first_codec)
                first_properties = await playlist_properties()
                assert len(first_properties["playlist"]) == 2
                assert first_properties["playlist-pos"] == 0
                await backend.seek(19)
                await _eventually("native embedded playlist advances after EOF", lambda: (
                    backend.read_property("path") == base + "/_fixture/" + second), timeout=5)
                decoded = await _decoder_progress(backend, base + "/_fixture/" + second, second_codec)
                second_properties = await playlist_properties()
                assert second_properties["playlist-path"] == url
                assert second_properties["playlist-pos"] == 1
                assert backend.read_property("idle-active") is False
                assert second_properties["playlist"][1]["playing"] is True
                assert second_properties["playlist"][1]["playlist-path"] == url
                evidence["native_playlist_eof"][name] = {
                    "first": first_properties, "second": second_properties, "decoder": decoded}
                await backend.seek(19)
                await _eventually("whole native playlist reaches EOF", lambda: (
                    backend.read_property("idle-active") is True), timeout=5)
                evidence["native_playlist_eof"][name]["container_eof"] = await playlist_properties()
                await backend.stop()
            print(json.dumps({"native_playlist_diagnostics": evidence["native_playlists"],
                              "native_playlist_eof": evidence["native_playlist_eof"]}, sort_keys=True), file=sys.stderr)
            evidence["playlists"] = {}
            failures = []
            for name, target, codec in (("station.m3u", "tone.mp3", "mp3"),
                                        ("station.pls", "tone.aac", "aac")):
                try:
                    evidence["playlists"][name] = await play(name, codec, actual_name=target)
                    assert await _command(session, base, "setPlayerCmd:pause") == (200, "OK")
                    assert backend.read_property("pause") is True
                    assert api.state.status == "pause"
                    assert await _command(session, base, "setPlayerCmd:resume") == (200, "OK")
                    assert backend.read_property("pause") is False
                    assert api.state.status == "play"
                    assert backend.read_property("playlist-path") == base + "/_fixture/" + name
                    evidence["playlists"][name]["pause_resume"] = True
                except AssertionError as exc:
                    # Exercise both formats before reporting their independent
                    # HTTP failures; never convert a failing format to a skip.
                    failures.append(str(exc))
                finally:
                    await _command(session, base, "setPlayerCmd:stop")
            assert not failures, {"native_playlists": evidence["native_playlists"], "http_failures": failures}
            # Exercise the public client's actual unindexed playlist command,
            # not play_url as a stand-in. Exact raw fixture targets make URI
            # truncation or extra decoding fail rather than select other media.
            from pywiim import WiiMClient
            client_session = ClientSession(timeout=ClientTimeout(total=8), trust_env=False)
            client = WiiMClient(host="127.0.0.1", port=urlsplit(base).port,
                protocol="http", timeout=5, session=client_session,
                capabilities={"retry_count": 1, "supports_metadata": False})
            evidence["client_playlists"] = {}
            try:
                await client.get_status()
                for name, target, codec in (("station.m3u", "tone.mp3", "mp3"),
                                            ("station.pls", "tone.aac", "aac")):
                    for decorated in (False, True):
                        raw = ("/_fixture/client%2F" + name + "?signature=a%26b%3Dc&literal=%252F&end=:3"
                               if decorated else "/_fixture/" + name)
                        url = base + raw
                        fixture.responses[raw] = fixture.responses["/_fixture/" + name]
                        # Relative entries of an encoded-path playlist remain
                        # under /_fixture/ in native URL resolution.
                        backend._allowed_media.add(url)
                        loads_before = len(backend.loads)
                        requests_before = len(fixture.requests)
                        await client.play_playlist(url)
                        decoded = await _decoder_progress(backend, base + "/_fixture/" + target, codec)
                        assert backend.read_property("playlist-path") == url
                        assert backend.loads[loads_before:] == [url]
                        assert any(item["raw_path"] == raw for item in fixture.requests[requests_before:])
                        status = await client.get_player_status()
                        assert status["uri"] == url and status["play_status"] == "play"
                        assert api.state.current_url == api.state.backend_url == url
                        assert api.state.queue.current.url == url and api.state.queue.count == 1
                        await client.pause()
                        queue, generation = api.state.queue, api.state.generation
                        loads = backend.loads[:]
                        rejected = await _command(session, base,
                            "setPlayerCmd:playlist:" + quote(url, safe="") + ":3")
                        assert rejected == (400, "Failed")
                        assert backend.loads == loads and backend.read_property("pause") is True
                        assert backend.read_property("path") == base + "/_fixture/" + target
                        assert api.state.queue is queue and api.state.generation == generation
                        evidence["client_playlists"][raw] = {
                            "requested": url, "decoder": decoded, "unsupported_index_preserved_pause": True}
                        await client.stop()
            finally:
                await client.close()
                assert client_session.closed
            evidence["container_queue_eof"] = {}
            for name, first, second, first_codec, second_codec in (
                ("two-entries.m3u", "tone.mp3", "tone.aac", "mp3", "aac"),
                ("two-entries.pls", "tone.aac", "tone.mp3", "aac", "mp3"),
            ):
                container = base + "/_fixture/" + name
                next_url = base + "/_fixture/opaque"
                queue = PlaybackQueue(name="Synthetic container queue", items=[
                    QueueItem(container), QueueItem(next_url, title="Next canonical item", duration=20),
                ], index=1)
                loads_before = len(backend.loads)
                await _queue_soap(session, base, "CreateQueue",
                                  f"<QueueContext>{escape(serialize_queue_context(queue))}</QueueContext>")
                await _queue_soap(session, base, "PlayQueueWithIndex",
                                  "<QueueName>Synthetic container queue</QueueName><Index>1</Index>")
                await _decoder_progress(backend, base + "/_fixture/" + first, first_codec)
                assert await _command(session, base, "setPlayerCmd:seek:19") == (200, "OK")
                generation = api.state.generation
                await _eventually("HTTP-selected container advances its native entry", lambda: (
                    backend.read_property("path") == base + "/_fixture/" + second), timeout=5)
                await _decoder_progress(backend, base + "/_fixture/" + second, second_codec)
                await _eventually("monitor follows the second native entry title and position", lambda: (
                    api.state.title == backend.read_property("media-title")
                    and api.state.curpos < 3 and api.state.status == "play"), timeout=3)
                assert api.state.generation == generation
                assert api.state.queue.index == 1 and api.state.queue.count == 2
                assert api.state.current_url == api.state.backend_url == container
                assert backend.loads[loads_before:] == [container]
                internal_title = api.state.title
                assert await _command(session, base, "setPlayerCmd:seek:19") == (200, "OK")
                await _eventually("whole container EOF advances the canonical queue", lambda: (
                    api.state.queue.index == 2 and api.state.status == "play"
                    and backend.read_property("path") == next_url), timeout=5)
                decoded = await _decoder_progress(backend, next_url, "mp3")
                assert backend.loads[loads_before:] == [container, next_url]
                assert api.state.current_url == next_url and api.state.title == "Next canonical item"
                evidence["container_queue_eof"][name] = {
                    "native_second_title": internal_title, "canonical_index_during_native_second": 1,
                    "canonical_index_after_container_eof": 2, "next_decoder": decoded,
                    "renderer_loads": backend.loads[loads_before:], "client_status_polls": 0,
                }
                assert await _command(session, base, "setPlayerCmd:stop") == (200, "OK")
        elif scenario == "redirects":
            evidence["redirect"] = await play("redirect", "mp3")
            url = base + "/_fixture/loop"
            started = asyncio.get_running_loop().time()
            assert await _command(session, base, "setPlayerCmd:play:" + url) == (502, "Failed")
            elapsed = asyncio.get_running_loop().time() - started
            assert elapsed < 12, elapsed
            assert api.state.current_url == url and api.state.phase == PlaybackPhase.ERROR
            assert api.state.status != "play" and api.state.last_error
            assert backend.read_property("idle-active") is True
            requests = sum(item["path"] == "/_fixture/loop" for item in fixture.requests)
            assert 1 < requests <= 32, requests
            evidence["redirect_loop"] = {"elapsed": elapsed, "requests": requests,
                                          "error": api.state.last_error}
            # A bad source must not poison subsequent valid playback.
            evidence["after_failure"] = await play("tone.aac", "aac")
        elif scenario == "icy":
            evidence["icy"] = await play("icy", "mp3")
            await _eventually("native ICY title", lambda: backend.read_property("media-title") == _ICY_TITLE)
            await _eventually("renderer ICY title", lambda: api.state.title == _ICY_TITLE)
            assert any(item["path"] == "/_fixture/icy" and item["icy_metadata"] == "1"
                       for item in fixture.requests), fixture.requests
            assert api.state.queue.current.title == _ICY_TITLE
            assert ET.fromstring(api.state.track_metadata).find(
                ".//{http://purl.org/dc/elements/1.1/}title").text == _ICY_TITLE
            assert escape(_ICY_TITLE) in api.state.track_metadata
            evidence["icy"].update(native_title=backend.read_property("media-title"),
                                   renderer_title=api.state.title, xml_roundtrip=True)
        else:
            raise AssertionError(scenario)
        assert await _command(session, base, "setPlayerCmd:stop") == (200, "OK")
        assert backend.read_property("idle-active") is True
        evidence["requests"] = fixture.requests
        evidence["renderer_loads"] = backend.loads
        evidence["version"] = backend.read_property("mpv-version")
    evidence["cleanup"] = {"handle_closed": True, "listener_closed": True, "no_pending_tasks": True}
    return evidence


def _run_probe(tmp_path: Path, scenario: str) -> dict[str, Any]:
    library = Path(os.environ["ORR_TEST_LIBMPV_PATH"]).expanduser().resolve()
    assert library.is_file(), f"Configured libmpv does not exist: {library}"
    ffmpeg = shutil.which(os.environ.get("ORR_TEST_FFMPEG_BINARY", "ffmpeg"))
    assert ffmpeg, "Opt-in radio format tests require official FFmpeg with MP3/AAC/HLS support"
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    for name in tuple(env):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            env.pop(name)
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--probe", str(library), ffmpeg, str(tmp_path), scenario],
        cwd=root, env=env, text=True, capture_output=True, timeout=60, check=False,
    )
    assert completed.returncode == 0, f"REAL_BACKEND radio formats failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    evidence = json.loads(completed.stdout)
    assert evidence["scenario"] == scenario and evidence["lane"] == "REAL_BACKEND/null"
    assert evidence["cleanup"] == {"handle_closed": True, "listener_closed": True, "no_pending_tasks": True}
    print(json.dumps(evidence, sort_keys=True))
    return evidence


@_OPT_IN
def test_local_radio_mp3_aac_octet_stream_and_hls(tmp_path: Path) -> None:
    _run_probe(tmp_path, "formats")


@_OPT_IN
def test_local_radio_m3u_and_pls_source_identity(tmp_path: Path) -> None:
    _run_probe(tmp_path, "playlists")


@_OPT_IN
def test_local_radio_redirect_and_bounded_loop_failure(tmp_path: Path) -> None:
    _run_probe(tmp_path, "redirects")


@_OPT_IN
def test_local_radio_icy_metadata_from_real_decoder(tmp_path: Path) -> None:
    _run_probe(tmp_path, "icy")


if __name__ == "__main__":
    assert len(sys.argv) == 6 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")

    async def main() -> None:
        async with asyncio.timeout(50):
            evidence = await _exercise(sys.argv[2], sys.argv[3], Path(sys.argv[4]), sys.argv[5])
        print(json.dumps(evidence, sort_keys=True))

    asyncio.run(main())
