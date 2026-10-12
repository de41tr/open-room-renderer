"""Real pywiim -> loopback HTTP -> production mpv Unix IPC, with null audio.

ORR_TEST_PYWIIM_UNIX=1 PYTHONPATH=. .venv/bin/pytest -q -s \
    tests/test_pywiim_unix_integration.py

Opt-in requires Linux, installed pywiim and mpv, and working Unix IPC. Missing
prerequisites fail, never skip or substitute libmpv. Generated local WAVs only;
no discovery, external media, accounts, original app or physical audio claim.
The shared supervisor gives the entire worker/mpv process group a hard deadline.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, contextmanager
import hashlib
from html import escape
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import sys
import tempfile
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web
import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.mpv_backend import MpvBackend
from fake_linkplay.state import RendererState
from test_discovery_playback_native import cleanup_runner, event_fields
from test_mpv_unix_integration import _DURATION, _eventually, _external_player, _property, _write_tone
from tools.native_probe_supervisor import run_probe


@contextmanager
def client_media(directory: Path):
    """Own a strict loopback media fixture and two distinct generated tracks."""
    tracks = (directory / "client-first-440.wav", directory / "client-second-660.wav")
    for path, frequency in zip(tracks, (440, 660)):
        _write_tone(path, frequency)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(socket.SOMAXCONN)
        listener.setblocking(False)
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        sources = (base + "/media/first%2F440.wav?signature=a%26b%3Dc&literal=%252F&name=caf%C3%A9",
                   base + "/media/second-660.wav")
        yield tracks, sources, listener
    assert listener.fileno() == -1


async def exercise_real_client(backend: MpvBackend, directory: Path, tracks, sources, listener) -> dict[str, Any]:
    """Shared controls/readback, also callable by a separately labeled diagnostic."""
    # Deliberately not importorskip: optional dependency absence after opt-in is
    # an acceptance failure, not a green native run with missing client coverage.
    from pywiim import WiiMClient

    digests = [hashlib.sha256(path.read_bytes()).hexdigest() for path in tracks]
    assert len(set(digests)) == 2
    baseline_tasks = asyncio.all_tasks()
    receipts: dict[str, list[tuple[dict[str, str], dict[str, str]]]] = {
        name: [] for name in ("AVTransport", "RenderingControl", "PlayQueue")}
    commands: list[str] = []
    subscribers = {}
    event_tasks = []
    await backend.initialize_gain()
    async with AsyncExitStack() as stack:
        port = listener.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        cfg = Config(uuid="00000000-0000-4000-8000-000000000004",
            advertise_ip="127.0.0.1", bind_address="127.0.0.1",
            http_api_port=port, upnp_port=port, state_dir=str(directory / "state"),
            default_volume=25, auto_spawn_mpv=False, enable_ssdp=False, enable_mdns=False)
        api = HttpApi(cfg, RendererState.from_config(cfg), backend)

        @web.middleware
        async def record_commands(request, handler):
            if request.path == "/httpapi.asp":
                commands.append(request.query.get("command", ""))
            return await handler(request)

        async def callback(request):
            service = request.match_info["service"]
            assert request.remote == "127.0.0.1"
            assert request.headers["NT"] == "upnp:event"
            assert request.headers["NTS"] == "upnp:propchange"
            receipts[service].append((dict(request.headers), event_fields(await request.text())))
            return web.Response()

        # Match raw request targets before aiohttp's route/query decoding. A
        # second URL unquote by the renderer changes both the resource path and
        # synthetic signed-query data, and MUST fail rather than play any file.
        expected_targets = {source.removeprefix(base): path for source, path in zip(sources, tracks)}
        media_requests: list[str] = []
        unexpected_media_requests: list[str] = []

        async def media(request):
            media_requests.append(request.raw_path)
            path = expected_targets.get(request.raw_path)
            if path is None:
                unexpected_media_requests.append(request.raw_path)
                return web.Response(status=400, text="Media target was changed")
            return web.FileResponse(path)

        app = api.app()
        app.middlewares.append(record_commands)
        app.router.add_get("/media/{tail:.*}", media)
        app.router.add_route("NOTIFY", "/fixture-events/{service}", callback)
        runner = web.AppRunner(app, shutdown_timeout=1)
        stack.push_async_callback(cleanup_runner, runner)
        await runner.setup()
        await web.SockSite(runner, listener).start()
        monitor = api._state_monitor_task
        assert monitor is not None and not monitor.done()
        session = await stack.enter_async_context(ClientSession(timeout=ClientTimeout(total=8), trust_env=False))
        client = WiiMClient(host="127.0.0.1", port=port, protocol="http", timeout=5, session=session,
            # Same explicit transport policy as the existing real-client test.
            # No parser, endpoint, status or command-method override is used.
            capabilities={"retry_count": 1, "supports_metadata": False})
        stack.push_async_callback(client.close)
        # Cache the real client's endpoint through a read before the first
        # mutation, so endpoint discovery cannot duplicate that mutation.
        await client.get_status()

        async def delivered(service: str, expected: dict[str, str], after: int = 0):
            async def matching():
                return next((props for headers, props in reversed(receipts[service][after:])
                             if headers["SID"] == subscribers[service]
                             and all(props.get(key) == value for key, value in expected.items())), None)
            return await _eventually(f"delivered {service} {expected}", matching)

        async def agreement(status: str, index: int, count: int, path: str | None,
                            volume: int = 25, mute: bool = False):
            for parsed in (await client.get_player_status(), await client.get_status()):
                assert parsed.get("play_status") == status, parsed
                assert parsed.get("volume") == volume and parsed.get("volume_level") == volume / 100, parsed
                assert parsed.get("mute") is mute, parsed
                assert parsed.get("transport_error") == "", parsed
                if path is not None:
                    assert parsed.get("uri") == str(path), parsed
            # The client's public raw command preserves canonical HTTP fields.
            raw = await client.send_command("getPlayerStatus")
            assert raw["status"] == status and raw["transport_error"] == "", raw
            assert raw["plicurr"] == str(index) and raw["plicount"] == str(count), raw
            assert raw["vol"] == str(volume) and raw["mute"] == str(int(mute)), raw
            if path is not None:
                assert raw["stream_url"] == str(path), raw
            assert await _property(backend, "volume") == pytest.approx(volume)
            assert await _property(backend, "mute") is mute
            assert await _property(backend, "idle-active") is (status == "stop")
            if status != "stop":
                assert await _property(backend, "path") == str(path)
                assert await _property(backend, "pause") is (status == "pause")
                assert await _property(backend, "current-ao") == "null"
                assert any(track.get("type") == "audio" and track.get("selected") is True
                           for track in await _property(backend, "track-list"))
            else:
                assert raw["curpos"] == "0"
            return raw

        async def progress():
            before = await _property(backend, "playback-time")
            async def advancing():
                return await _property(backend, "playback-time") > before + 0.2
            await _eventually("native decoded position advances", advancing)

        await agreement("stop", 0, 0, None)
        for service in receipts:
            async with session.request("SUBSCRIBE", base + "/upnp/event/" + service, headers={
                "NT": "upnp:event", "CALLBACK": f"<{base}/fixture-events/{service}>",
                "TIMEOUT": "Second-120"}) as response:
                assert response.status == 200, await response.text()
                subscribers[service] = response.headers["SID"]
            await delivered(service, {})
            assert receipts[service][0][0]["SEQ"] == "0"

        await client.play_url(str(sources[0]))
        await agreement("play", 1, 1, sources[0])
        await delivered("AVTransport", {"TransportState": "PLAYING", "TrackURI": str(sources[0]),
                                      "last:TransportState": "PLAYING"})
        await progress()
        mark = len(receipts["AVTransport"])
        await client.pause()
        await agreement("pause", 1, 1, sources[0])
        await delivered("AVTransport", {"TransportState": "PAUSED_PLAYBACK",
                                       "last:TransportState": "PAUSED_PLAYBACK"}, mark)
        await asyncio.sleep(0.15)
        before = await _property(backend, "playback-time")
        await asyncio.sleep(0.25)
        assert abs(await _property(backend, "playback-time") - before) < 0.04
        await client.previous_track()
        await agreement("pause", 1, 1, sources[0])
        assert await _property(backend, "playback-time") < 0.2
        mark = len(receipts["AVTransport"])
        await client.resume()
        await agreement("play", 1, 1, sources[0])
        await delivered("AVTransport", {"TransportState": "PLAYING", "last:TransportState": "PLAYING"}, mark)
        await progress()

        # pywiim 2.3.9 has next_track(), but no queue-creation API. Only fixture
        # setup uses SOAP; actual selection/resume/Next use public client methods.
        queue = ('<PlayList><QueueName>Real client pair</QueueName>'
                 '<ListInfo><SourceName>UPnPServer</SourceName><TrackNumber>2</TrackNumber></ListInfo><Tracks>'
                 + ''.join(f'<Track{i}><Title>Synthetic {i}</Title><URL>{escape(str(path))}</URL>'
                           f'<Duration>{_DURATION}</Duration></Track{i}>' for i, path in enumerate(sources, 1))
                 + '</Tracks></PlayList>')
        namespace = "urn:schemas-wiimu-com:service:PlayQueue:1"
        envelope = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                    f'<u:CreateQueue xmlns:u="{namespace}"><QueueContext>{escape(queue)}</QueueContext>'
                    '</u:CreateQueue></s:Body></s:Envelope>')
        async with session.post(base + "/upnp/control/PlayQueue", data=envelope.encode(), headers={
            "SOAPACTION": f'"{namespace}#CreateQueue"', "Content-Type": "text/xml"}) as response:
            assert response.status == 200, await response.text()
        await client.resume()
        await agreement("play", 1, 2, sources[0])
        transport_mark, queue_mark = len(receipts["AVTransport"]), len(receipts["PlayQueue"])
        await client.next_track()
        await agreement("play", 2, 2, sources[1])
        await delivered("AVTransport", {"TransportState": "PLAYING", "TrackURI": str(sources[1]),
                                       "last:CurrentTrack": "2", "last:CurrentTrackURI": str(sources[1])}, transport_mark)
        await delivered("PlayQueue", {"CurrentIndex": "2", "Count": "2", "TrackURI": str(sources[1])}, queue_mark)
        await progress()
        # The public previous_track() sends the exact HTTP "prev" command.
        # It must select the first decoder resource, then Next must restore the
        # second. Native/HTTP/events are checked after each separate mutation.
        for method, index in ((client.previous_track, 1), (client.next_track, 2)):
            transport_mark, queue_mark = len(receipts["AVTransport"]), len(receipts["PlayQueue"])
            await method()
            path = sources[index - 1]
            await agreement("play", index, 2, path)
            await delivered("AVTransport", {"TrackURI": str(path), "last:CurrentTrack": str(index)}, transport_mark)
            await delivered("PlayQueue", {"CurrentIndex": str(index), "Count": "2", "TrackURI": str(path)}, queue_mark)
            await progress()
        mark = len(receipts["RenderingControl"])
        await client.set_volume(0.37)
        await agreement("play", 2, 2, sources[1], volume=37)
        await delivered("RenderingControl", {"Volume": "37", "last:Volume": "37"}, mark)
        for muted in (True, False):
            mark = len(receipts["RenderingControl"])
            await client.set_mute(muted)
            await agreement("play", 2, 2, sources[1], volume=37, mute=muted)
            await delivered("RenderingControl", {"Mute": str(int(muted)), "last:Mute": str(int(muted))}, mark)
        mark = len(receipts["AVTransport"])
        await client.stop()
        await agreement("stop", 2, 2, sources[1], volume=37)
        await delivered("AVTransport", {"TransportState": "STOPPED", "last:TransportState": "STOPPED"}, mark)
        assert "getPlayerStatusEx" in commands
        assert [command for command in commands if command.startswith("setPlayerCmd:")] == [
            "setPlayerCmd:play:" + str(sources[0]), "setPlayerCmd:pause", "setPlayerCmd:prev", "setPlayerCmd:resume",
            "setPlayerCmd:resume", "setPlayerCmd:next", "setPlayerCmd:prev", "setPlayerCmd:next", "setPlayerCmd:vol:37",
            "setPlayerCmd:mute:1", "setPlayerCmd:mute:0", "setPlayerCmd:stop"]
        for service, sid in subscribers.items():
            received = receipts[service]
            assert all(headers["SID"] == sid for headers, _ in received)
            assert [int(headers["SEQ"]) for headers, _ in received] == list(range(len(received)))
            subscriber = api.event_subscribers[sid]
            if subscriber.task is not None:
                event_tasks.append(subscriber.task)
            async with session.request("UNSUBSCRIBE", base + "/upnp/event/" + service,
                                       headers={"SID": sid}) as response:
                assert response.status == 200
        assert not api.event_subscribers
        evidence = {"pywiim_version": version("pywiim"), "mpv_version": await _property(backend, "mpv-version"),
            "architecture": platform.machine(), "track_sha256": digests,
            "controls": ["play_url", "pause", "resume", "next_track", "previous_track", "set_volume", "set_mute", "stop"],
            "parsed_http_native_and_delivered_gena_agree": True,
            "events_received": {service: len(items) for service, items in receipts.items()}}
    await asyncio.sleep(0)
    assert session.closed and not runner.sites
    assert monitor.done() and all(task.done() for task in event_tasks)
    pending = [task for task in asyncio.all_tasks() - baseline_tasks - {asyncio.current_task()} if not task.done()]
    assert not pending, f"Real-client fixture leaked tasks: {pending!r}"
    assert not unexpected_media_requests, unexpected_media_requests
    assert set(media_requests) == set(expected_targets), media_requests
    evidence["exact_raw_media_targets_received"] = sorted(set(media_requests))
    evidence["cleanup"] = {"session_closed": True,
                           "subscriptions_removed": True, "monitor_and_event_tasks_done": True}
    return evidence


def require_unix_ipc() -> None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM):
            pass
    except OSError as exc:
        raise AssertionError(f"Production Unix IPC NOT_RUN: {type(exc).__name__}, errno={exc.errno}") from exc


@pytest.mark.skipif(os.environ.get("ORR_TEST_PYWIIM_UNIX") != "1",
                   reason="Real client + Unix IPC requires ORR_TEST_PYWIIM_UNIX=1")
def test_real_pywiim_controls_production_unix_ipc() -> None:
    assert sys.platform == "linux", "Explicit pywiim Unix IPC lane requires Linux"
    from pywiim import WiiMClient  # noqa: F401: missing optional client must fail after opt-in
    executable = shutil.which(os.environ.get("ORR_TEST_MPV_BINARY", "mpv"))
    assert executable is not None, "ORR_TEST_PYWIIM_UNIX=1 requires a real mpv executable"
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + str(root / "tests")
    for name in tuple(env):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            env.pop(name)
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    result = run_probe([sys.executable, str(Path(__file__).resolve()), "--probe", str(Path(executable).resolve())],
                       cwd=root, env=env, timeout=60)
    assert result.returncode == 0, f"Real pywiim / production Unix IPC failed\n{result.stdout}\n{result.stderr}"
    evidence = json.loads(result.stdout)
    assert evidence["lane"] == "REAL_PYWIIM_PRODUCTION_UNIX_IPC/null"
    assert evidence["fixture_child_reaped"] is True
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 3 and sys.argv[1] == "--probe"
    async def main():
        require_unix_ipc()
        with tempfile.TemporaryDirectory(prefix="orr-pywiim-") as name:
            directory = Path(name)
            with client_media(directory) as media:
                async with asyncio.timeout(45), _external_player(directory, sys.argv[2]) as (backend, process):
                    assert type(backend) is MpvBackend, "Native client gate must use the production backend unchanged"
                    evidence = await exercise_real_client(backend, directory, *media)
                    assert process.returncode is None, "Renderer teardown must preserve externally owned mpv"
                assert process.returncode is not None
            evidence["cleanup"]["listener_closed"] = True
            evidence.update(lane="REAL_PYWIIM_PRODUCTION_UNIX_IPC/null", fixture_child_reaped=True)
            print(json.dumps(evidence, sort_keys=True))
    asyncio.run(main())
