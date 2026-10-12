"""REAL_BACKEND/null queue navigation preserves the owner's transport intent.

Only generated local WAVs can reach the existing libmpv command adapter. The
production renderer, loopback HTTP/SOAP/GENA, native decoder and properties are
real. This does not validate production Unix IPC, audible output or hardware.
Each staging configuration runs in a child with a hard process timeout.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from html import escape
import json
import locale
import os
from pathlib import Path
import subprocess
import sys
from xml.etree import ElementTree as ET

from aiohttp import ClientSession, ClientTimeout, web
import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.state import PlaybackPhase, RendererState
from test_libmpv_integration import _eventually, _write_tone
from test_libmpv_queue_staging import _StagingBackend


def _loads(commands):
    return [command for command in commands if (
        command.get("name") == "loadfile" if isinstance(command, dict)
        else command[0] == "loadfile")]


async def _exercise(library: str, directory: Path, staging: bool) -> dict:
    baseline = asyncio.all_tasks()
    tracks = tuple(directory / name for name in ("one.wav", "two.wav", "three.wav"))
    for track, frequency in zip(tracks, (330, 440, 660)):
        _write_tone(track, frequency)
    backend = _StagingBackend(library, tracks)
    native_command = backend.command
    lose_stop_ack = False
    native_stop_applied = False

    async def command(args, timeout=5.0):
        nonlocal lose_stop_ack, native_stop_applied
        result = await native_command(args, timeout)
        if lose_stop_ack and args == ["stop"]:
            lose_stop_ack = False
            await _eventually("actual native stop before lost ack", lambda: backend.read_property("idle-active") is True)
            native_stop_applied = True
            return {"error": "synthetic acknowledgement lost after real native stop"}
        return result

    backend.command = command
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000",
                 state_dir=str(directory / "state"), advertise_ip="127.0.0.1",
                 bind_address="127.0.0.1", native_queue_staging=staging,
                 auto_spawn_mpv=False, enable_ssdp=False, enable_mdns=False)
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)
    receipts = []
    evidence = {"lane": "REAL_BACKEND/null + loopback HTTP/SOAP/GENA",
                "mpv_version": backend.read_property("mpv-version"),
                "native_queue_staging": staging, "cases": {}}

    async def callback(request):
        properties = {node.tag.rsplit("}", 1)[-1]: node.text or ""
                      for prop in ET.fromstring(await request.text()) for node in prop}
        receipts.append((request.headers["SID"], properties))
        return web.Response()

    app = api.app()
    app.router.add_route("NOTIFY", "/fixture-events", callback)
    runner = web.AppRunner(app, shutdown_timeout=1)
    try:
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        async with ClientSession(timeout=ClientTimeout(total=10), trust_env=False) as client:
            subscriptions = {}
            for service in ("AVTransport", "PlayQueue"):
                async with client.request("SUBSCRIBE", base + "/upnp/event/" + service, headers={
                    "NT": "upnp:event", "CALLBACK": f"<{base}/fixture-events>", "TIMEOUT": "Second-60",
                }) as response:
                    assert response.status == 200
                    subscriptions[service] = response.headers["SID"]

            async def soap(action, fields="<InstanceID>0</InstanceID>", service="AVTransport", fault=None):
                namespace = ("urn:schemas-wiimu-com:service:PlayQueue:1" if service == "PlayQueue"
                             else "urn:schemas-upnp-org:service:AVTransport:1")
                body = (f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                        f'<u:{action} xmlns:u="{namespace}">{fields}</u:{action}></s:Body></s:Envelope>')
                async with client.post(base + "/upnp/control/" + service, data=body, headers={
                    "SOAPACTION": f'"{namespace}#{action}"', "Content-Type": "text/xml",
                }) as response:
                    text = await response.text()
                    assert response.status == (500 if fault else 200), (action, response.status, text)
                    ET.fromstring(text)
                    if fault:
                        assert f"<errorCode>{fault}</errorCode>" in text, text
                    return text

            async def http(value, expected=200):
                async with client.get(base + "/httpapi.asp", params={"command": value}) as response:
                    body = await response.text()
                    assert response.status == expected, (value, response.status, body)
                    return json.loads(body) if response.content_type == "application/json" else body

            async def wire_selection(index, phase):
                status = await http("getPlayerStatus")
                assert status["plicurr"] == str(index) and status["playback_phase"] == phase, status
                assert status["transport_error"] == "", status
                position = await soap("GetPositionInfo")
                transport = await soap("GetTransportInfo")
                expected_transport = {"STOPPED": "STOPPED", "PAUSED": "PAUSED_PLAYBACK", "PLAYING": "PLAYING"}[phase]
                assert f"<Track>{index}</Track>" in position and str(tracks[index - 1]) in position, position
                assert f"Tone {index}" in position and f"Artist {index}" in position, position
                assert f"<CurrentTransportState>{expected_transport}</CurrentTransportState>" in transport, transport
                assert "<CurrentTransportStatus>OK</CurrentTransportStatus>" in transport, transport
                assert api.state.queue.index == index and api.state.title == f"Tone {index}"
                assert api.state.artist == f"Artist {index}"
                return status

            async def stopped_events(index, start):
                await _eventually("GENA stopped selection", lambda: any(
                    sid == subscriptions["AVTransport"]
                    and 'TransportState val="STOPPED"' in props.get("LastChange", "")
                    and f'CurrentTrack val="{index}"' in props.get("LastChange", "")
                    and props.get("TrackURI") == str(tracks[index - 1])
                    for sid, props in receipts[start:]))
                await _eventually("GENA queue selection", lambda: any(
                    sid == subscriptions["PlayQueue"] and props.get("CurrentIndex") == str(index)
                    and props.get("TrackTitle") == f"Tone {index}"
                    for sid, props in receipts[start:]))
                assert not any(sid == subscriptions["AVTransport"] and
                    ('TransportState val="PLAYING"' in props.get("LastChange", "") or
                     'TransportState val="TRANSITIONING"' in props.get("LastChange", ""))
                    for sid, props in receipts[start:])

            async def played(index):
                await _eventually("selected native media actually decoding", lambda: (
                    backend.read_property("path") == str(tracks[index - 1])
                    and backend.read_property("pause") is False
                    and backend.read_property("playback-time") > .05
                    and backend.read_property("current-ao") == "null"))
                await wire_selection(index, "PLAYING")

            context = ("<PlayList><QueueName>Intent</QueueName><ListInfo><TrackNumber>3</TrackNumber>"
                       "<StartPlayIndex>2</StartPlayIndex></ListInfo><Tracks>" + "".join(
                           f"<Track{i}><URL>{track}</URL><Title>Tone {i}</Title><Artist>Artist {i}</Artist>"
                           f"<Album>Synthetic</Album><Duration>30</Duration></Track{i}>"
                           for i, track in enumerate(tracks, 1)) + "</Tracks></PlayList>")
            await soap("CreateQueue", "<QueueContext>" + escape(context) + "</QueueContext>", "PlayQueue")
            await wire_selection(2, "STOPPED")
            assert backend.read_property("idle-active") is True
            assert backend.read_property("playlist") == []

            # Initial and previously played STOPPED queues must never load or
            # unpause. Check both actions, both public control surfaces and
            # independent native, HTTP, SOAP and event readbacks after the reply.
            for history in ("initial", "after_stop"):
                if history == "after_stop":
                    await soap("Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")
                    await played(2)
                    stop_marker = len(receipts)
                    await soap("Stop")
                    await _eventually("GENA stopped after explicit Stop", lambda: any(
                        sid == subscriptions["AVTransport"] and
                        'TransportState val="STOPPED"' in props.get("LastChange", "")
                        for sid, props in receipts[stop_marker:]))
                movements = (("Next", 3, "soap"), ("Previous", 2, "soap"),
                             ("Previous", 1, "http"), ("Next", 2, "http"))
                for action, index, surface in movements:
                    marker, event_marker = len(backend.commands), len(receipts)
                    if surface == "soap":
                        await soap(action)
                    else:
                        assert await http("setPlayerCmd:" + ("next" if action == "Next" else "prev")) == "OK"
                    await asyncio.sleep(.06)
                    await wire_selection(index, "STOPPED")
                    await stopped_events(index, event_marker)
                    assert backend.read_property("idle-active") is True
                    assert backend.read_property("playlist") == []
                    assert backend.commands[marker:] == [], backend.commands[marker:]
                evidence["cases"]["stopped_" + history] = {
                    "next_previous_http_and_soap": True, "native_mutations": [],
                    "metadata_and_gena_updated": True, "idle_and_empty_native_playlist": True}

            # A positive assertion prevents a no-op backend from satisfying
            # silence checks: explicit Play loads the silently selected item.
            marker = len(backend.commands)
            await soap("Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")
            await played(2)
            assert _loads(backend.commands[marker:])[0][1:3] == [str(tracks[1]), "replace"]
            if staging:
                assert api._native_stage is not None and api._native_stage.confirmed
                assert len(backend.read_property("playlist")) == 2
            evidence["cases"]["explicit_play_selected_item"] = {
                "index": 2, "native_path": str(tracks[1]), "null_ao": True,
                "confirmed_successor_staged": staging}

            await soap("Pause")
            await asyncio.sleep(.08)
            await wire_selection(2, "PAUSED")
            frozen = {"queue": asdict(api.state.queue), "generation": api.state.generation,
                      "intent": api.state.intent, "metadata": api.state.track_metadata,
                      "backend_url": api.state.backend_url, "context": api.queue_context,
                      "stage_id": id(api._native_stage), "playlist": backend.read_property("playlist")}
            marker = len(backend.commands)
            before = backend.read_property("playback-time")
            for action in ("Next", "Previous"):
                await soap(action, fault=701)
                # The legacy HTTP surface maps this unsupported transition to
                # 400, while retaining the same rejection-before-mutation path.
                assert await http("setPlayerCmd:" + ("next" if action == "Next" else "prev"), 400) == "Failed"
                await asyncio.sleep(.06)
                await wire_selection(2, "PAUSED")
            assert frozen == {"queue": asdict(api.state.queue), "generation": api.state.generation,
                              "intent": api.state.intent, "metadata": api.state.track_metadata,
                              "backend_url": api.state.backend_url, "context": api.queue_context,
                              "stage_id": id(api._native_stage), "playlist": backend.read_property("playlist")}
            assert backend.commands[marker:] == [], backend.commands[marker:]
            assert backend.read_property("path") == str(tracks[1]) and backend.read_property("pause") is True
            after = backend.read_property("playback-time")
            assert abs(after - before) < .03, (before, after)
            evidence["cases"]["paused_navigation"] = {
                "soap_fault": 701, "http_status": 400, "native_mutations": [],
                "queue_metadata_generation_stage_playlist_unchanged": True,
                "native_position_before": before, "native_position_after": after}

            marker = len(backend.commands)
            await soap("PlayQueueWithIndex", "<QueueName>Intent</QueueName><Index>1</Index>", "PlayQueue")
            await played(1)
            assert _loads(backend.commands[marker:])[0][1:3] == [str(tracks[0]), "replace"]
            evidence["cases"]["explicit_play_queue_from_pause"] = {"index": 1, "plays": True}

            for action, index in (("Next", 2), ("Previous", 1)):
                marker = len(backend.commands)
                await soap(action)
                await played(index)
                assert _loads(backend.commands[marker:])[0][1:3] == [str(tracks[index - 1]), "replace"]
            evidence["cases"]["playing_navigation"] = {"next_previous_loads_and_plays": True}

            # A real Stop with a deliberately lost acknowledgement establishes
            # the production ERROR fence. Navigation must not reinterpret that
            # state as a harmless stopped selection or start another item.
            lose_stop_ack = True
            await soap("Stop", fault=501)
            assert native_stop_applied
            await asyncio.sleep(.08)
            assert api.state.phase == PlaybackPhase.ERROR
            original = (api.state.queue.index, api.state.generation, api.state.current_url, api.state.track_metadata)
            marker = len(backend.commands)
            for action in ("Next", "Previous"):
                await soap(action, fault=501)
                assert await http("setPlayerCmd:" + ("next" if action == "Next" else "prev"), 502) == "Failed"
            await asyncio.sleep(.06)
            assert original == (api.state.queue.index, api.state.generation, api.state.current_url, api.state.track_metadata)
            assert backend.commands[marker:] == [], backend.commands[marker:]
            assert api.state.phase == PlaybackPhase.ERROR and backend.read_property("idle-active") is True
            evidence["cases"]["error_navigation"] = {"real_stop_then_lost_ack": True,
                "soap_fault": 501, "http_status": 502, "native_mutations": [], "selection_unchanged": True}
            evidence["native_mutation_commands"] = backend.commands
    finally:
        await runner.cleanup()
        backend.close()
        assert api._state_monitor_task is None and not api.event_subscribers and backend._handle is None
    assert not [task for task in asyncio.all_tasks() - baseline if not task.done()]
    evidence["cleanup"] = {"native_handle_destroyed": True, "no_pending_tasks": True,
                           "monitor_stopped": True, "subscriptions_removed": True}
    return evidence


@pytest.mark.skipif(not os.environ.get("ORR_TEST_LIBMPV_PATH"), reason="Native navigation requires ORR_TEST_LIBMPV_PATH")
@pytest.mark.parametrize("staging", [False, True], ids=["staging-off", "staging-on"])
def test_real_queue_navigation_preserves_transport_intent(tmp_path, staging):
    library = Path(os.environ["ORR_TEST_LIBMPV_PATH"]).expanduser().resolve()
    assert library.is_file()
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--probe", str(library), str(tmp_path), str(int(staging))],
        cwd=root, env=env, text=True, capture_output=True, timeout=35, check=False)
    assert completed.returncode == 0, f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    evidence = json.loads(completed.stdout)
    assert evidence["native_queue_staging"] is staging and evidence["cleanup"]["no_pending_tasks"]
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 5 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")

    async def main():
        async with asyncio.timeout(30):
            print(json.dumps(await _exercise(sys.argv[2], Path(sys.argv[3]), bool(int(sys.argv[4]))), sort_keys=True))

    asyncio.run(main())
