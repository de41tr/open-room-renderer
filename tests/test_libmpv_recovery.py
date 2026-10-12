"""Optional REAL_BACKEND/null station recovery against a loopback fault fixture.

Opt in with ORR_TEST_LIBMPV_PATH=/absolute/path/to/libmpv.so.2. This reuses the
public-API ctypes adapter from test_libmpv_integration; production UNIX IPC is
not exercised. Only generated, low-amplitude WAV files and the exact fixture
URL are allowed. No WAN media, audio device, or client status polling is used.
Each scenario runs in a disposable interpreter with a whole-process timeout.
"""

from __future__ import annotations

import asyncio
from html import escape
import json
import locale
import os
from pathlib import Path
import socket
import subprocess
import sys
from typing import Any

import pytest
from aiohttp import ClientSession, ClientTimeout, web

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.queue import PlaybackQueue, QueueItem, serialize_queue_context
from fake_linkplay.state import PlaybackPhase, RendererState
from test_libmpv_integration import _eventually, _write_tone
from test_libmpv_load_options import _NamedLoadBackend


class _StationBackend(_NamedLoadBackend):
    """Count renderer load commands separately from native HTTP reconnects."""

    def __init__(self, library: str, tracks: tuple[Path, Path], station_url: str) -> None:
        super().__init__(library, tracks)
        self._allowed_media.add(station_url)
        self.loads: list[str] = []

    async def command(self, args: list[Any] | dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
        operation = args.get("name") if isinstance(args, dict) else args[0]
        if operation == "loadfile":
            url = args["url"] if isinstance(args, dict) else args[1]
            assert url in self._allowed_media
            self.loads.append(url)
        return await super().command(args, timeout)


class _BrokenStation:
    def __init__(self, wav: bytes, backend: _StationBackend) -> None:
        self.wav = wav
        self.backend = backend
        self.mode = "disconnect"
        self.requests: list[dict[str, Any]] = []

    async def handle(self, request: web.Request) -> web.StreamResponse:
        mode = self.mode
        self.requests.append({"mode": mode, "renderer_load": len(self.backend.loads)})
        if mode == "unavailable":
            return web.Response(status=503, text="Synthetic station temporarily unavailable")
        if mode == "available":
            return web.Response(body=self.wav, content_type="audio/wav")

        # Deliberately cut the connection short of the declared 30-second body.
        # Two seconds of real PCM let readiness and decoded progress be observed
        # before the player consumes its buffer and actually reaches EOF/idle.
        self.mode = "unavailable"
        response = web.StreamResponse(headers={
            "Content-Type": "audio/wav", "Content-Length": str(len(self.wav)),
            "Connection": "close",
        })
        await response.prepare(request)
        await response.write(self.wav[:44 + 2 * 16000 * 2 * 2])
        assert request.transport is not None
        request.transport.close()
        return response


async def _exercise(library: str, directory: Path, scenario: str) -> dict[str, Any]:
    baseline_tasks = asyncio.all_tasks()
    tracks = (directory / "station-440.wav", directory / "unused-660.wav")
    for path, frequency in zip(tracks, (440, 660)):
        _write_tone(path, frequency)
    listener: socket.socket | None = None
    backend: _StationBackend | None = None
    runner: web.AppRunner | None = None
    session: ClientSession | None = None
    api: HttpApi | None = None
    monitor: asyncio.Task | None = None
    recovery_task: asyncio.Task | None = None
    evidence: dict[str, Any] = {"lane": "REAL_BACKEND/null", "scenario": scenario}
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(socket.SOMAXCONN)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        station_url = base + "/_fixture_station.wav"
        backend = _StationBackend(library, tracks, station_url)
        cache_defaults = {key: backend.read_property("options/" + key) for key in
                          ("demuxer-max-bytes", "cache", "cache-secs")}
        assert backend._allowed_media == {str(path.resolve()) for path in tracks} | {station_url}
        fixture = _BrokenStation(tracks[0].read_bytes(), backend)
        cfg = Config(
            uuid="00000000-0000-4000-8000-000000000000",
            advertise_ip="127.0.0.1", bind_address="127.0.0.1",
            http_api_port=port, upnp_port=port, state_dir=str(directory / "state"),
            auto_spawn_mpv=False, enable_ssdp=False, enable_mdns=False,
        )
        api = HttpApi(cfg, RendererState.from_config(cfg), backend)
        app = api.app()
        app.router.add_get("/_fixture_station.wav", fixture.handle)
        runner = web.AppRunner(app, shutdown_timeout=1)
        await runner.setup()
        await web.SockSite(runner, listener).start()
        monitor = api._state_monitor_task
        assert monitor is not None and not monitor.done()

        async with ClientSession(timeout=ClientTimeout(total=15), trust_env=False) as session:
            async def command(value: str) -> None:
                # Only mutations, never getPlayerStatus or another reconciliation
                # endpoint: recovery must be driven by the renderer's monitor.
                assert value.startswith("setPlayerCmd:")
                async with session.get(base + "/httpapi.asp", params={"command": value}) as response:
                    body = await response.text()
                    assert response.status == 200 and body == "OK", (value, response.status, body)

            async def soap(action: str, fields: str) -> None:
                namespace = "urn:schemas-wiimu-com:service:PlayQueue:1"
                envelope = f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><u:{action} xmlns:u="{namespace}">{fields}</u:{action}></s:Body></s:Envelope>'
                async with session.post(base + "/upnp/control/PlayQueue", data=envelope.encode(), headers={"SOAPACTION": f'"{namespace}#{action}"', "Content-Type": "text/xml"}) as response:
                    body = await response.text()
                    assert response.status == 200, (action, response.status, body)

            queue = PlaybackQueue(name="Synthetic station recovery", items=[
                QueueItem(url=str(tracks[1]), title="Unselected finite tone", duration=30),
                QueueItem(url=station_url, title="Synthetic station", stream_type="station", duration=0),
            ], index=2)
            await soap("CreateQueue", f"<QueueContext>{escape(serialize_queue_context(queue))}</QueueContext>")
            await command("setPlayerCmd:vol:37")
            await command("setPlayerCmd:mute:1")
            await soap("PlayQueueWithIndex", "<QueueName>Synthetic station recovery</QueueName><Index>2</Index>")
            selected = api.state.queue.current
            assert selected is not None and selected.stream_type == "station" and selected.duration == 0
            assert backend.read_property("current-ao") == "null"
            assert backend.read_property("options/demuxer-max-back-bytes") == 0
            assert backend.read_property("path") == station_url
            before = backend.read_property("playback-time")
            await _eventually("initial decoded station progress", lambda: backend.read_property("playback-time") > before + 0.25)
            evidence["initial_progress"] = {"before": before, "after": backend.read_property("playback-time")}
            assert len(backend.loads) == 1

            await _eventually("monitor-owned station backoff", lambda: (
                api.state.phase == PlaybackPhase.BUFFERING
                and api._station_recovery_task is not None
                and api._station_recovery is not None
                and api._station_recovery.attempts == 0
            ), timeout=5)
            recovery_task = api._station_recovery_task
            assert backend.read_property("idle-active") is True
            assert fixture.mode == "unavailable"
            generation = api.state.generation

            if scenario == "recover":
                await _eventually("real HTTP 503 on renderer reload", lambda: any(
                    item == {"mode": "unavailable", "renderer_load": 2} for item in fixture.requests
                ), timeout=4)
                # Wait for the real readiness timeout, rather than supplying a
                # fake backend failure or turning an in-flight native retry good.
                await _eventually("failed reload returning to backoff", lambda: (
                    api._station_recovery is not None
                    and api._station_recovery.attempts == 1
                    and api.state.last_error == "Station source unavailable; retry pending"
                ), timeout=12)
                fixture.mode = "available"
                await _eventually("renderer reload recovering real audio", lambda: (
                    len(backend.loads) == 3 and api.state.phase == PlaybackPhase.PLAYING
                    and api.state.last_error == ""
                ), timeout=5)
                assert api.state.generation == generation
                assert backend.read_property("path") == station_url
                assert backend.read_property("current-ao") == "null"
                assert any(track.get("type") == "audio" and track.get("selected") is True
                           for track in backend.read_property("track-list"))
                before = backend.read_property("playback-time")
                await _eventually("recovered decoded progress", lambda: backend.read_property("playback-time") > before + 0.25)
                evidence["recovered_progress"] = {"before": before, "after": backend.read_property("playback-time")}
                assert api._station_recovery is not None and api._station_recovery.attempts == 2
            elif scenario in {"stop", "pause"}:
                await command(f"setPlayerCmd:{scenario}")
                fixture.mode = "available"
                # Observe beyond every configured retry delay, with the source
                # healthy again, so a stale timer cannot hide behind a 503.
                await asyncio.sleep(sum(api._station_recovery_delays) + api._monitor_interval)
                assert len(backend.loads) == 1, backend.loads
                assert backend.read_property("idle-active") is True
                assert api.state.status == scenario
                assert api.state.intent == scenario
                assert api._station_recovery is None
                assert recovery_task is not None and recovery_task.done()
                evidence["owner_command_prevented_reload"] = scenario
            else:
                assert scenario == "cleanup"
                # Leave a live backoff for AppRunner cleanup to invalidate.
                assert recovery_task is not None and not recovery_task.done()

            assert api.state.queue.index == 2 and api.state.queue.current is selected
            assert api.state.current_url == station_url and api.state.totlen == 0
            assert api.state.volume == 37 and api.state.mute is True
            assert backend.read_property("volume") == pytest.approx(37, abs=0.01)
            assert backend.read_property("mute") is True
            assert backend.loads == [station_url] * len(backend.loads)
            if api.state.phase == PlaybackPhase.PLAYING:
                assert backend.read_property("options/demuxer-max-back-bytes") == 0
                assert {key: backend.read_property("options/" + key) for key in cache_defaults} == cache_defaults
                evidence["station_history_disabled_forward_cache_unchanged"] = True
            evidence.update(renderer_loads=len(backend.loads), http_requests=fixture.requests,
                            preserved={"volume": 37, "mute": True, "index": 2},
                            client_status_polls=0)
    finally:
        try:
            if runner is not None:
                await runner.cleanup()
        finally:
            if listener is not None:
                listener.close()
            if backend is not None:
                backend.close()
        await asyncio.sleep(0)
        pending = [task for task in asyncio.all_tasks() - baseline_tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        assert not pending, f"REAL_BACKEND recovery fixture leaked tasks: {pending!r}"
        assert session is None or session.closed
        assert listener is None or listener.fileno() == -1
        assert runner is None or not runner.sites
        assert backend is None or backend._handle is None
        assert backend is None or backend.process is None
        assert monitor is None or monitor.done()
        assert recovery_task is None or recovery_task.done()
        assert api is None or api._station_recovery_task is None
    evidence["cleanup"] = {"handle_closed": True, "listener_closed": True, "no_pending_tasks": True}
    return evidence


@pytest.mark.skipif(not os.environ.get("ORR_TEST_LIBMPV_PATH"), reason="REAL_BACKEND/null requires explicit ORR_TEST_LIBMPV_PATH")
@pytest.mark.parametrize("scenario", ["recover", "stop", "pause", "cleanup"])
def test_real_libmpv_station_recovery(tmp_path: Path, scenario: str) -> None:
    library = Path(os.environ["ORR_TEST_LIBMPV_PATH"]).expanduser().resolve()
    assert library.is_file(), f"Configured libmpv does not exist: {library}"
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    # Keep even proxy-configured developer machines on the literal loopback URL.
    for name in tuple(env):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            env.pop(name)
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--probe", str(library), str(tmp_path), scenario],
        cwd=root, env=env, text=True, capture_output=True, timeout=40, check=False,
    )
    assert completed.returncode == 0, f"REAL_BACKEND recovery failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    evidence = json.loads(completed.stdout)
    assert evidence["scenario"] == scenario and evidence["client_status_polls"] == 0
    assert evidence["cleanup"] == {"handle_closed": True, "listener_closed": True, "no_pending_tasks": True}
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 5 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")

    async def main() -> None:
        async with asyncio.timeout(30):
            evidence = await _exercise(sys.argv[2], Path(sys.argv[3]), sys.argv[4])
        print(json.dumps(evidence, sort_keys=True))

    asyncio.run(main())
