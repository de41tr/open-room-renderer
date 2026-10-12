"""Explicit multicast discovery -> two real tracks -> HTTP/SOAP/GENA/Unix IPC.

Run ORR_TEST_DISCOVERY_PLAYBACK=1 PYTHONPATH=. .venv/bin/pytest -q -s
    tests/test_discovery_playback_native.py on native Linux with mpv installed.

The actual production SSDP and custom mDNS startup selects IPv4 loopback. The
client obtains HTTP/control/event URLs from real multicast discovery and the
advertised description. Production MpvBackend talks to real mpv Unix JSON IPC;
no backend method is replaced. Audio consists of generated WAVs with ao=null.
Explicit opt-in failures never silently become skips or another transport.
This does not claim physical LAN, Android-client, audible output, or Pi tests.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import hashlib
from html import escape
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import sys
import tempfile
from typing import Any
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from aiohttp import ClientSession, ClientTimeout, web
import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.state import RendererState
from native_discovery_support import discover, live_discovery
from test_mpv_unix_integration import (
    _DURATION, _eventually, _external_player, _property, _write_tone,
)

pytestmark = pytest.mark.skipif(os.environ.get("ORR_TEST_DISCOVERY_PLAYBACK") != "1",
    reason="Native discovery/playback requires ORR_TEST_DISCOVERY_PLAYBACK=1")


@pytest.fixture
def native_workspace():
    assert sys.platform == "linux", "Explicit discovery/playback lane requires Linux"
    executable = shutil.which("mpv")
    assert executable is not None, "ORR_TEST_DISCOVERY_PLAYBACK=1 requires a real mpv executable"
    with tempfile.TemporaryDirectory(prefix="orr-discovery-") as directory:
        yield Path(directory), str(Path(executable).resolve())


async def cleanup_runner(runner: web.AppRunner) -> None:
    await asyncio.wait_for(runner.cleanup(), 8)


def xml_fields(body: str) -> dict[str, str]:
    return {node.tag.rsplit("}", 1)[-1]: node.text or "" for node in ET.fromstring(body).iter()}


def event_fields(body: str) -> dict[str, str]:
    props = {node.tag.rsplit("}", 1)[-1]: node.text or "" for prop in ET.fromstring(body) for node in prop}
    if "LastChange" in props:
        props.update({"last:" + node.tag.rsplit("}", 1)[-1]: node.attrib["val"]
                      for node in ET.fromstring(props["LastChange"]).iter() if "val" in node.attrib})
    return props


@pytest.mark.asyncio
async def test_discovered_renderer_real_controls_queue_and_delivered_events(native_workspace):
    directory, executable = native_workspace
    tracks = (directory / "discovered-440.wav", directory / "discovered-660.wav")
    for path, frequency in zip(tracks, (440, 660)):
        _write_tone(path, frequency)
    digests = [hashlib.sha256(path.read_bytes()).hexdigest() for path in tracks]
    assert len(set(digests)) == 2
    receipts: dict[str, list[tuple[dict[str, str], dict[str, str]]]] = {
        name: [] for name in ("AVTransport", "RenderingControl", "PlayQueue")}
    subscribers = []
    sockets = []
    session = None
    monitor = None
    event_tasks = []
    evidence: dict[str, Any] = {"lane": "PRODUCTION_DISCOVERY_UNIX_IPC/null",
        "architecture": platform.machine(), "discovery_interface": "127.0.0.1",
        "ssdp": "multicast M-SEARCH with unicast response", "mdns": "custom multicast QM/PTR/SRV/TXT/A"}

    async with asyncio.timeout(60), _external_player(directory, executable) as (backend, process):
        await backend.initialize_gain()
        async with AsyncExitStack() as stack:
            def listener() -> socket.socket:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sockets.append(sock)
                stack.callback(sock.close)
                sock.bind(("127.0.0.1", 0))
                sock.listen(socket.SOMAXCONN)
                sock.setblocking(False)
                return sock

            http_socket = listener()
            port = http_socket.getsockname()[1]
            cfg = Config(uuid="00000000-0000-4000-8000-000000000003",
                friendly_name="Synthetic native acceptance", advertise_ip="127.0.0.1",
                bind_address="127.0.0.1", discovery_interface="127.0.0.1",
                http_api_port=port, upnp_port=port, state_dir=str(directory / "state"),
                default_volume=25, wiim_home_profile=True, enable_mdns=True, auto_spawn_mpv=False)
            api = HttpApi(cfg, RendererState.from_config(cfg), backend)
            runner = web.AppRunner(api.app(), shutdown_timeout=1)
            stack.push_async_callback(cleanup_runner, runner)
            await runner.setup()
            await web.SockSite(runner, http_socket).start()
            monitor = api._state_monitor_task
            assert monitor is not None and not monitor.done()

            async def callback(request):
                service = request.match_info["service"]
                assert request.remote == "127.0.0.1"
                assert request.headers["NT"] == "upnp:event"
                assert request.headers["NTS"] == "upnp:propchange"
                receipts[service].append((dict(request.headers), event_fields(await request.text())))
                return web.Response()

            callback_app = web.Application()
            callback_app.router.add_route("NOTIFY", "/events/{service}", callback)
            callback_socket = listener()
            callback_base = f"http://127.0.0.1:{callback_socket.getsockname()[1]}"
            callback_runner = web.AppRunner(callback_app, shutdown_timeout=1)
            stack.push_async_callback(cleanup_runner, callback_runner)
            await callback_runner.setup()
            await web.SockSite(callback_runner, callback_socket).start()
            await stack.enter_async_context(live_discovery([cfg]))
            session = await stack.enter_async_context(ClientSession(timeout=ClientTimeout(total=8), trust_env=False))
            found = await discover([cfg], session)
            renderer = next(iter(found.values()))
            assert renderer.udn == f"uuid:{cfg.upnp_uuid}"

            async def command(value: str) -> Any:
                async with session.get(renderer.base + "/httpapi.asp", params={"command": value}) as response:
                    text = await response.text()
                    assert response.status == 200, (value, response.status, text)
                    return json.loads(text) if response.content_type == "application/json" else text

            async def soap(action: str, fields: str = "", service: str = "AVTransport") -> dict[str, str]:
                namespace, control, _ = renderer.services[service]
                envelope = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                    f'<u:{action} xmlns:u="{namespace}">{fields}</u:{action}></s:Body></s:Envelope>')
                async with session.post(urljoin(renderer.base, control), data=envelope.encode(),
                        headers={"SOAPACTION": f'"{namespace}#{action}"', "Content-Type": "text/xml"}) as response:
                    text = await response.text()
                    assert response.status == 200, (service, action, response.status, text)
                    return xml_fields(text)

            async def event(service: str, expected: dict[str, str], after: int = 0) -> dict[str, str]:
                async def matching():
                    return next((props for _, props in reversed(receipts[service][after:])
                                 if all(props.get(key) == value for key, value in expected.items())), None)
                result = await _eventually(f"delivered {service} GENA {expected}", matching)
                assert result is not None
                return result

            async def consistency(status: str, index: int, track: int, volume: int = 25, mute: bool = False):
                body = await command("getPlayerStatus")
                assert body["status"] == status and body["transport_error"] == "", body
                assert body["plicurr"] == str(index) and body["plicount"] == "2", body
                assert body["vol"] == str(volume) and body["mute"] == str(int(mute)), body
                transport = await soap("GetTransportInfo", "<InstanceID>0</InstanceID>")
                expected = {"play": "PLAYING", "pause": "PAUSED_PLAYBACK", "stop": "STOPPED"}[status]
                assert transport["CurrentTransportState"] == expected, transport
                assert transport["CurrentTransportStatus"] == "OK", transport
                position = await soap("GetPositionInfo", "<InstanceID>0</InstanceID>")
                assert position["Track"] == str(index) and position["TrackURI"] == str(tracks[track]), position
                queue = await soap("GetQueueIndex", service="PlayQueue")
                assert queue["CurrentIndex"] == str(index) and queue["Count"] == "2", queue
                gain_fields = "<InstanceID>0</InstanceID><Channel>Master</Channel>"
                assert (await soap("GetVolume", gain_fields, "RenderingControl"))["CurrentVolume"] == str(volume)
                assert (await soap("GetMute", gain_fields, "RenderingControl"))["CurrentMute"] == str(int(mute))
                assert await _property(backend, "volume") == pytest.approx(volume)
                assert await _property(backend, "mute") is mute
                assert await _property(backend, "idle-active") is (status == "stop")
                if status != "stop":
                    assert await _property(backend, "path") == str(tracks[track])
                    assert await _property(backend, "pause") is (status == "pause")
                    assert await _property(backend, "current-ao") == "null"
                return body

            # Complete initial native gain readback before subscriptions.
            assert (await command("getPlayerStatus"))["status"] == "stop"
            for service in receipts:
                _, _, event_url = renderer.services[service]
                async with session.request("SUBSCRIBE", urljoin(renderer.base, event_url), headers={
                    "NT": "upnp:event", "CALLBACK": f"<{callback_base}/events/{service}>",
                    "TIMEOUT": "Second-120"}) as response:
                    assert response.status == 200, await response.text()
                    subscribers.append((service, response.headers["SID"]))
                await event(service, {})
                assert receipts[service][0][0]["SEQ"] == "0"
            await event("AVTransport", {"TransportState": "STOPPED", "last:TransportState": "STOPPED"})
            await event("RenderingControl", {"Volume": "25", "Mute": "0", "last:Volume": "25", "last:Mute": "0"})

            queue = ('<PlayList><QueueName>Discovered synthetic pair</QueueName>'
                     '<ListInfo><SourceName>UPnPServer</SourceName><TrackNumber>2</TrackNumber></ListInfo><Tracks>'
                     + ''.join(f'<Track{i}><Title>Tone {i}</Title><URL>{escape(str(path))}</URL>'
                               f'<Duration>{_DURATION}</Duration></Track{i}>' for i, path in enumerate(tracks, 1))
                     + '</Tracks></PlayList>')
            await soap("CreateQueue", f"<QueueContext>{escape(queue)}</QueueContext>", "PlayQueue")
            await soap("PlayQueueWithIndex", "<QueueName>Discovered synthetic pair</QueueName><Index>1</Index>", "PlayQueue")
            await event("AVTransport", {"TransportState": "PLAYING", "TrackURI": str(tracks[0]),
                                        "last:TransportState": "PLAYING", "last:CurrentTrack": "1"})
            await event("PlayQueue", {"CurrentIndex": "1", "Count": "2", "TrackURI": str(tracks[0])})
            await consistency("play", 1, 0)
            assert any(track.get("type") == "audio" and track.get("selected")
                       for track in await _property(backend, "track-list"))
            before = await _property(backend, "playback-time")
            async def progressing():
                return await _property(backend, "playback-time") > before + 0.2
            await _eventually("native decoded time progresses", progressing)

            mark = len(receipts["AVTransport"])
            assert await command("setPlayerCmd:pause") == "OK"
            await event("AVTransport", {"TransportState": "PAUSED_PLAYBACK", "last:TransportState": "PAUSED_PLAYBACK"}, mark)
            await consistency("pause", 1, 0)
            await asyncio.sleep(0.15)
            before = await _property(backend, "playback-time")
            await asyncio.sleep(0.25)
            assert abs(await _property(backend, "playback-time") - before) < 0.04

            mark = len(receipts["AVTransport"])
            await soap("Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")
            await event("AVTransport", {"TransportState": "PLAYING", "last:TransportState": "PLAYING"}, mark)
            await consistency("play", 1, 0)
            await _eventually("resumed native decoded time progresses", progressing)

            transport_mark, queue_mark = len(receipts["AVTransport"]), len(receipts["PlayQueue"])
            previous_resource = await _property(backend, "path")
            await soap("Next", "<InstanceID>0</InstanceID>")
            await event("AVTransport", {"TransportState": "PLAYING", "TrackURI": str(tracks[1]),
                                        "last:CurrentTrackURI": str(tracks[1]), "last:CurrentTrack": "2"}, transport_mark)
            await event("PlayQueue", {"CurrentIndex": "2", "Count": "2", "TrackURI": str(tracks[1]),
                                      "last:CurrentIndex": "2"}, queue_mark)
            await consistency("play", 2, 1)
            assert await _property(backend, "path") != previous_resource

            mark = len(receipts["RenderingControl"])
            assert await command("setPlayerCmd:vol:37") == "OK"
            await event("RenderingControl", {"Volume": "37", "last:Volume": "37"}, mark)
            await consistency("play", 2, 1, volume=37)
            for muted in (True, False):
                mark = len(receipts["RenderingControl"])
                await soap("SetMute", "<InstanceID>0</InstanceID><Channel>Master</Channel>"
                           f"<DesiredMute>{int(muted)}</DesiredMute>", "RenderingControl")
                await event("RenderingControl", {"Mute": str(int(muted)), "last:Mute": str(int(muted))}, mark)
                await consistency("play", 2, 1, volume=37, mute=muted)

            mark = len(receipts["AVTransport"])
            assert await command("setPlayerCmd:stop") == "OK"
            await event("AVTransport", {"TransportState": "STOPPED", "last:TransportState": "STOPPED"}, mark)
            assert (await consistency("stop", 2, 1, volume=37))["curpos"] == "0"
            evidence["mpv_version"] = await _property(backend, "mpv-version")
            evidence["track_sha256"] = digests
            evidence["controls"] = ["play", "pause", "resume", "Next", "volume", "mute", "unmute", "stop"]
            evidence["http_soap_native_and_gena_agree"] = True
            evidence["next_changed_native_resource"] = True
            evidence["events_received"] = {service: len(items) for service, items in receipts.items()}
            for service, sid in subscribers:
                delivered = receipts[service]
                assert all(headers["SID"] == sid for headers, _ in delivered)
                assert [int(headers["SEQ"]) for headers, _ in delivered] == list(range(len(delivered)))
                subscriber = api.event_subscribers[sid]
                if subscriber.task:
                    event_tasks.append(subscriber.task)
                _, _, event_url = renderer.services[service]
                async with session.request("UNSUBSCRIBE", urljoin(renderer.base, event_url), headers={"SID": sid}) as response:
                    assert response.status == 200
            assert not api.event_subscribers
        assert session.closed and monitor.done() and all(task.done() for task in event_tasks)
        assert all(sock.fileno() == -1 for sock in sockets)
        assert process.returncode is None, "Renderer shutdown must preserve externally owned mpv"
    assert process.returncode is not None
    evidence["cleanup"] = {"subscriptions_removed": True, "event_tasks_done": True,
        "monitor_done": True, "listeners_closed": True, "mpv_child_reaped": True}
    print(json.dumps(evidence, sort_keys=True))
