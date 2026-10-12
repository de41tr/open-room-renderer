"""Optional REAL_BACKEND/null: fixed public resolver returns a new media URL.

Only controlled resolver JSON, generated audio and exact loopback media URLs
are used. No provider/WAN endpoint, credentials, physical audio or production
UNIX IPC is exercised. Each probe is isolated with a whole-process deadline.
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
from fake_linkplay.queue import QueueItem
from fake_linkplay.radio import resolve_station
from fake_linkplay.state import PlaybackPhase, RendererState
from test_libmpv_integration import _eventually, _write_tone
from test_libmpv_recovery import _StationBackend, _BrokenStation


async def _exercise(library: str, directory: Path, selection: str, staging: bool) -> dict[str, Any]:
    baseline = asyncio.all_tasks()
    tracks = (directory / "station.wav", directory / "unused.wav")
    for path, frequency in zip(tracks, (440, 660)):
        _write_tone(path, frequency)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(socket.SOMAXCONN)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    first, fresh = base + "/expired.wav", base + "/fresh.wav"
    reference = "https://opml.radiotime.com/Tune.ashx?id=s12345"
    backend = _StationBackend(library, tracks, first)
    cache_defaults = {key: backend.read_property("options/" + key) for key in
                      ("demuxer-max-bytes", "cache", "cache-secs")}
    backend._allowed_media.add(fresh)
    fixture = _BrokenStation(tracks[0].read_bytes(), backend)
    resolutions, fresh_requests = [], []

    async def fetched(endpoint: str) -> bytes:
        assert endpoint == "https://opml.radiotime.com/Tune.ashx?id=s12345&formats=mp3,aac&render=json"
        target = first if not resolutions else fresh
        resolutions.append(target)
        return json.dumps({"head": {"status": "200"}, "body": [
            {"element": "audio", "is_direct": True, "media_type": "mp3", "url": target},
        ]}).encode()

    async def validate(url: str) -> bool:
        assert url in {first, fresh}
        return True

    async def resolved(station_id: str):
        return await resolve_station(station_id, fetcher=fetched, validator=validate)

    async def healthy(request: web.Request) -> web.Response:
        fresh_requests.append(True)
        return web.Response(body=tracks[0].read_bytes(), content_type="audio/wav")

    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", advertise_ip="127.0.0.1",
                 bind_address="127.0.0.1", http_api_port=port, upnp_port=port,
                 state_dir=str(directory / "state"), auto_spawn_mpv=False,
                 enable_ssdp=False, enable_mdns=False, native_queue_staging=staging)
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)
    api._resolve_station_id = resolved
    app = api.app()
    app.router.add_get("/expired.wav", fixture.handle)
    app.router.add_get("/fresh.wav", healthy)
    runner = web.AppRunner(app, shutdown_timeout=.1)
    monitor = None
    evidence: dict[str, Any] = {"lane": "REAL_BACKEND/null", "selection": selection, "native_queue_staging": staging, "client_status_polls": 0}
    try:
        await runner.setup()
        await web.SockSite(runner, listener).start()
        monitor = api._state_monitor_task
        async with ClientSession(timeout=ClientTimeout(total=15), trust_env=False) as session:
            async def command(value: str) -> None:
                assert value.startswith("setPlayerCmd:")
                async with session.get(base + "/httpapi.asp", params={"command": value}) as response:
                    assert response.status == 200 and await response.text() == "OK"

            async def soap(action: str, fields: str) -> None:
                ns = "urn:schemas-wiimu-com:service:PlayQueue:1"
                envelope = f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><u:{action} xmlns:u="{ns}">{fields}</u:{action}></s:Body></s:Envelope>'
                async with session.post(base + "/upnp/control/PlayQueue", data=envelope.encode(),
                                        headers={"SOAPACTION": f'"{ns}#{action}"', "Content-Type": "text/xml"}) as response:
                    body = await response.text()
                    assert response.status == 200, body

            await command("setPlayerCmd:vol:37")
            await command("setPlayerCmd:mute:1")
            if selection == "header":
                context = '<PlayList><QueueName>Station</QueueName><ListName>Synthetic station</ListName><ListInfo><SourceName>newTuneIn</SourceName><StreamId>s12345</StreamId></ListInfo></PlayList>'
                await soap("CreateQueue", f"<QueueContext>{escape(context)}</QueueContext>")
                await soap("PlayQueueWithIndex", "<QueueName>Station</QueueName><Index>1</Index>")
            else:
                assert selection == "direct"
                await command("setPlayerCmd:play:" + reference)
            # A generated finite successor is present while real native WAV
            # reports a duration. Resolver-proven live radio must never stage it.
            api.state.queue.items.append(QueueItem(str(tracks[1]), duration=2))
            await api.sync_mpv_state()
            assert api._native_stage is None and len(backend.read_property("playlist")) == 1
            assert backend.read_property("options/demuxer-max-back-bytes") == 0
            selected, generation = api.state.queue.current, api.state.generation
            canonical = first if selection == "header" else reference
            before = backend.read_property("playback-time")
            await _eventually("initial decoded audio", lambda: backend.read_property("playback-time") > before + .25)
            evidence["initial_decoded_progress"] = True
            await _eventually("new resolver target after old URL expires", lambda: resolutions == [first, fresh], timeout=6)
            await _eventually("refreshed real audio", lambda: backend.loads == [first, fresh]
                             and api.state.phase == PlaybackPhase.PLAYING, timeout=5)
            assert fixture.mode == "unavailable" and fresh_requests
            assert api._station_recovery.attempts == 1
            assert api._station_recovery.backend_url == fresh
            assert backend.read_property("path") == fresh and backend.read_property("current-ao") == "null"
            before = backend.read_property("playback-time")
            await _eventually("new URL decoded progress", lambda: backend.read_property("playback-time") > before + .25)
            assert api.state.queue.current is selected and api.state.queue.index == 1
            assert api.state.generation == generation and api.state.current_url == selected.url == canonical
            assert api.state.resolved_url == selected.resolved_url == fresh and api.state.totlen == 0
            assert api.state.backend_original_url == canonical
            assert api._native_stage is None and len(backend.read_property("playlist")) == 1
            assert api.state.volume == 37 and api.state.mute is True
            assert backend.read_property("volume") == pytest.approx(37, abs=.01)
            assert backend.read_property("mute") is True
            if api.state.phase == PlaybackPhase.PLAYING:
                assert backend.read_property("options/demuxer-max-back-bytes") == 0
                assert {key: backend.read_property("options/" + key) for key in cache_defaults} == cache_defaults
                evidence["station_history_disabled_forward_cache_unchanged"] = True
            evidence.update(refreshed_decoded_progress=True, resolver_calls=len(resolutions),
                            renderer_loads=len(backend.loads), retry_attempts=1,
                            old_url_requests=len(fixture.requests), new_url_requests=len(fresh_requests),
                            identity_preserved=True, gain_preserved=True)
    finally:
        await runner.cleanup()
        listener.close()
        backend.close()
        await asyncio.sleep(0)
        pending = [task for task in asyncio.all_tasks() - baseline if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        assert not pending, f"Refresh fixture leaked tasks: {pending!r}"
        assert backend._handle is None and listener.fileno() == -1 and not runner.sites
        assert monitor is None or monitor.done()
        assert api._station_recovery_task is None
    evidence["cleanup"] = {"handle_closed": True, "listener_closed": True, "no_pending_tasks": True}
    return evidence


@pytest.mark.skipif(not os.environ.get("ORR_TEST_LIBMPV_PATH"), reason="REAL_BACKEND/null requires explicit ORR_TEST_LIBMPV_PATH")
@pytest.mark.parametrize("selection", ["header", "direct"])
@pytest.mark.parametrize("staging", [False, True])
def test_real_libmpv_station_url_refresh(tmp_path: Path, selection: str, staging: bool) -> None:
    library = Path(os.environ["ORR_TEST_LIBMPV_PATH"]).expanduser().resolve()
    assert library.is_file()
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    for name in tuple(env):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            env.pop(name)
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--probe", str(library), str(tmp_path), selection, "on" if staging else "off"],
                               cwd=root, env=env, text=True, capture_output=True, timeout=25, check=False)
    assert completed.returncode == 0, f"REAL_BACKEND URL refresh failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    evidence = json.loads(completed.stdout)
    assert evidence["selection"] == selection and evidence["resolver_calls"] == 2
    assert evidence["native_queue_staging"] is staging
    assert evidence["client_status_polls"] == 0 and evidence["refreshed_decoded_progress"]
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 6 and sys.argv[1] == "--probe" and sys.argv[5] in {"on", "off"}
    locale.setlocale(locale.LC_NUMERIC, "C")

    async def main() -> None:
        async with asyncio.timeout(20):
            evidence = await _exercise(sys.argv[2], Path(sys.argv[3]), sys.argv[4], sys.argv[5] == "on")
        print(json.dumps(evidence, sort_keys=True))

    asyncio.run(main())
