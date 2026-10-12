"""Optional REAL_BACKEND/null: established HTTP audio stalls without sending EOF.

Generated WAV media, an exact loopback URL and the test-only libmpv adapter are
used. Production monitoring/load/readiness logic is unchanged. This does not
exercise production UNIX IPC, real stations, WAN resilience or physical audio.
The recovery case uses the production 15-second grace period. Other scenarios
shorten only that clock budget so the finite retry/owner tests remain bounded.
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
from test_libmpv_recovery import _StationBackend


class _StalledStation:
    def __init__(self, wav: bytes, backend: _StationBackend) -> None:
        self.wav, self.backend = wav, backend
        self.mode = "stall"
        self.release = asyncio.Event()
        self.requests: list[dict[str, Any]] = []
        self.stalled_transports: list[Any] = []

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append({"mode": self.mode, "renderer_load": len(self.backend.loads)})
        if self.mode == "available":
            return web.Response(body=self.wav, content_type="audio/wav")
        response = web.StreamResponse(headers={
            "Content-Type": "audio/wav", "Content-Length": str(len(self.wav)),
        })
        await response.prepare(request)
        # Five seconds permit real demuxer probing and decoded progress. Hold
        # the socket open with the rest of the declared body still outstanding.
        prefix = 44 + 5 * 16000 * 2 * 2
        await response.write(self.wav[:prefix])
        assert request.transport is not None
        self.stalled_transports.append(request.transport)
        await self.release.wait()
        try:
            await response.write(self.wav[prefix:])
        except ConnectionResetError:
            pass
        return response


async def _exercise(library: str, directory: Path, scenario: str) -> dict[str, Any]:
    baseline = asyncio.all_tasks()
    tracks = (directory / "station.wav", directory / "replacement.wav")
    for path, frequency in zip(tracks, (440, 660)):
        _write_tone(path, frequency)
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
    fixture = _StalledStation(tracks[0].read_bytes(), backend)
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000", advertise_ip="127.0.0.1",
        bind_address="127.0.0.1", http_api_port=port, upnp_port=port,
        state_dir=str(directory / "state"), auto_spawn_mpv=False,
        enable_ssdp=False, enable_mdns=False,
    )
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)
    if scenario != "recover":
        api._station_stall_timeout = 1.0
    app = api.app()
    app.router.add_get("/_fixture_station.wav", fixture.handle)
    runner = web.AppRunner(app, shutdown_timeout=.1)
    monitor = None
    evidence: dict[str, Any] = {"lane": "REAL_BACKEND/null", "scenario": scenario, "client_status_polls": 0}
    try:
        await runner.setup()
        await web.SockSite(runner, listener).start()
        monitor = api._state_monitor_task
        assert monitor is not None and not monitor.done()
        async with ClientSession(timeout=ClientTimeout(total=15), trust_env=False) as session:
            async def command(value: str) -> None:
                assert value.startswith("setPlayerCmd:")
                async with session.get(base + "/httpapi.asp", params={"command": value}) as response:
                    assert response.status == 200 and await response.text() == "OK"

            async def soap(action: str, fields: str) -> None:
                namespace = "urn:schemas-wiimu-com:service:PlayQueue:1"
                envelope = f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><u:{action} xmlns:u="{namespace}">{fields}</u:{action}></s:Body></s:Envelope>'
                async with session.post(base + "/upnp/control/PlayQueue", data=envelope.encode(), headers={"SOAPACTION": f'"{namespace}#{action}"', "Content-Type": "text/xml"}) as response:
                    body = await response.text()
                    assert response.status == 200, (action, response.status, body)

            queue = PlaybackQueue(name="Synthetic no-EOF station", items=[
                QueueItem(str(tracks[1]), title="Unselected finite tone", duration=30),
                QueueItem(station_url, title="Synthetic source", stream_type="track" if scenario == "finite" else "station",
                          duration=30 if scenario == "finite" else 0),
            ], index=2)
            await soap("CreateQueue", f"<QueueContext>{escape(serialize_queue_context(queue))}</QueueContext>")
            await command("setPlayerCmd:vol:37")
            await command("setPlayerCmd:mute:1")
            await soap("PlayQueueWithIndex", "<QueueName>Synthetic no-EOF station</QueueName><Index>2</Index>")
            selected, generation = api.state.queue.current, api.state.generation
            expected_back_cache = 0 if scenario != "finite" else backend.read_property("option-info/demuxer-max-back-bytes/default-value")
            assert backend.read_property("options/demuxer-max-back-bytes") == expected_back_cache
            before = backend.read_property("playback-time")
            await _eventually("initial real decoded progress", lambda: backend.read_property("playback-time") > before + .25)
            evidence["initial_decoded_progress"] = True

            await _eventually("actual no-EOF cache stall", lambda: (
                api.state.phase == PlaybackPhase.BUFFERING
                and backend.read_property("paused-for-cache") is True
                and backend.read_property("core-idle") is True
            ), timeout=8)
            assert backend.read_property("idle-active") is False
            assert backend.read_property("eof-reached") is False
            assert backend.read_property("pause") is False
            assert len(backend.loads) == 1
            assert len(fixture.stalled_transports) == 1 and not fixture.stalled_transports[0].is_closing()
            evidence["stall"] = {"idle": False, "eof": False, "paused_for_cache": True, "core_idle": True, "held_socket_open": True}
            if scenario == "recover":
                stalled_at = asyncio.get_running_loop().time()
                assert api._station_stall_timeout == 15.0
                stalled_position = backend.read_property("playback-time")
                await asyncio.sleep(1)
                assert backend.read_property("playback-time") == pytest.approx(stalled_position, abs=.001)
                assert len(backend.loads) == 1
                fixture.mode = "available"
                await _eventually("monitor-owned reload after production stall grace", lambda: (
                    len(backend.loads) == 2 and api.state.phase == PlaybackPhase.PLAYING
                    and api.state.last_error == ""
                ), timeout=19)
                elapsed = asyncio.get_running_loop().time() - stalled_at
                assert elapsed >= api._station_stall_timeout - 2 * api._monitor_interval
                evidence["stall_to_recovery_seconds"] = round(elapsed, 2)
                assert api._station_recovery.attempts == 1
                before = backend.read_property("playback-time")
                await _eventually("recovered real decoded progress", lambda: backend.read_property("playback-time") > before + .25)
                evidence["recovered_decoded_progress"] = True
            elif scenario == "exhaust":
                await _eventually("exact finite stall budget and terminal silence", lambda: (
                    len(backend.loads) == 4 and api.state.phase == PlaybackPhase.ERROR
                    and backend.read_property("idle-active") is True
                ), timeout=30)
                assert api._station_recovery.attempts == 3
                assert api.state.last_error == "Station source unavailable: recovery exhausted"
                fixture.mode = "available"
                fixture.release.set()
                await asyncio.sleep(2)
                assert backend.read_property("idle-active") is True
                assert len(backend.loads) == 4
                evidence["late_source_return_stayed_silent"] = True
            else:
                if scenario == "transient":
                    fixture.release.set()
                elif scenario in {"pause", "stop", "newsource"}:
                    value = "play:" + str(tracks[1]) if scenario == "newsource" else scenario
                    await command("setPlayerCmd:" + value)
                    fixture.mode = "available"
                    fixture.release.set()
                else:
                    assert scenario == "finite"
                await asyncio.sleep(api._station_stall_timeout + sum(api._station_recovery_delays) + api._monitor_interval)
                assert len(backend.loads) == (2 if scenario == "newsource" else 1)
                if scenario == "transient":
                    assert api.state.phase == PlaybackPhase.PLAYING
                    assert api._station_recovery.attempts == 0
                    assert backend.read_property("paused-for-cache") is False
                    assert backend.read_property("playback-time") > 5.5
                elif scenario == "finite":
                    assert api._station_recovery is None
                    assert api.state.phase == PlaybackPhase.BUFFERING
                elif scenario == "newsource":
                    assert backend.read_property("path") == str(tracks[1])
                    assert api.state.phase == PlaybackPhase.PLAYING
                elif scenario == "pause":
                    assert backend.read_property("pause") is True
                    assert api.state.phase == PlaybackPhase.PAUSED
                else:
                    assert backend.read_property("idle-active") is True
                    assert api.state.phase == PlaybackPhase.STOPPED
                evidence["negative_case_preserved"] = scenario

            if scenario != "newsource":
                assert api.state.queue.current is selected and api.state.queue.index == 2
                assert api.state.current_url == station_url
                if scenario not in {"pause", "stop"}:
                    assert api.state.generation == generation
            assert api.state.volume == 37 and api.state.mute is True
            assert backend.read_property("volume") == pytest.approx(37, abs=.01)
            assert backend.read_property("mute") is True
            if scenario not in {"exhaust", "stop"}:
                assert backend.read_property("current-ao") == "null"
            if api.state.phase == PlaybackPhase.PLAYING and scenario != "newsource":
                assert backend.read_property("options/demuxer-max-back-bytes") == 0
                assert {key: backend.read_property("options/" + key) for key in cache_defaults} == cache_defaults
                evidence["station_history_disabled_forward_cache_unchanged"] = True
            evidence.update(renderer_loads=len(backend.loads), http_requests=fixture.requests,
                            gain_preserved={"volume": 37, "mute": True},
                            stall_grace_seconds=api._station_stall_timeout)
    finally:
        fixture.release.set()
        await runner.cleanup()
        listener.close()
        backend.close()
        await asyncio.sleep(0)
        pending = [task for task in asyncio.all_tasks() - baseline if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        assert not pending, f"No-EOF fixture leaked tasks: {pending!r}"
        assert backend._handle is None and listener.fileno() == -1 and not runner.sites
        assert monitor is None or monitor.done()
        assert api._station_recovery_task is None
    evidence["cleanup"] = {"handle_closed": True, "listener_closed": True, "no_pending_tasks": True}
    return evidence


@pytest.mark.skipif(not os.environ.get("ORR_TEST_LIBMPV_PATH"), reason="REAL_BACKEND/null requires explicit ORR_TEST_LIBMPV_PATH")
@pytest.mark.parametrize("scenario", ["recover", "transient", "pause", "stop", "newsource", "finite", "exhaust"])
def test_real_libmpv_stall_recovery(tmp_path: Path, scenario: str) -> None:
    library = Path(os.environ["ORR_TEST_LIBMPV_PATH"]).expanduser().resolve()
    assert library.is_file()
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    for name in tuple(env):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            env.pop(name)
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--probe", str(library), str(tmp_path), scenario],
        cwd=root, env=env, text=True, capture_output=True, timeout=65, check=False,
    )
    assert completed.returncode == 0, f"REAL_BACKEND no-EOF stall failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    evidence = json.loads(completed.stdout)
    assert evidence["scenario"] == scenario and evidence["client_status_polls"] == 0
    assert evidence["cleanup"] == {"handle_closed": True, "listener_closed": True, "no_pending_tasks": True}
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 5 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")
    async def main() -> None:
        async with asyncio.timeout(60):
            evidence = await _exercise(sys.argv[2], Path(sys.argv[3]), sys.argv[4])
        print(json.dumps(evidence, sort_keys=True))
    asyncio.run(main())
