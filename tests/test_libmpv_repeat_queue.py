"""REAL_BACKEND/null: finite queue repeat through real HTTP, SOAP and GENA.

Three distinct generated two-second WAVs use the existing public libmpv
command-node adapter. Native identity, decoded progress and null output are
real; command transport is test-local ctypes, not production Unix IPC. This
short acceptance does not claim audible output, hardware, timing or endurance.
The normal production observer cadence is retained, with a bounded child.
"""
from __future__ import annotations

import asyncio
import hashlib
from html import escape
import json
import locale
import os
from pathlib import Path
import subprocess
import sys
import wave
from xml.etree import ElementTree as ET

from aiohttp import ClientSession, ClientTimeout, web
import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.state import PlaybackPhase, RendererState
from test_libmpv_integration import _eventually, _write_tone
from test_libmpv_queue_staging import _StagingBackend


DURATION = 2
MODES = {"NORMAL": "4", "REPEAT_ONE": "1", "REPEAT_ALL": "0"}


def _loads(commands):
    return [command for command in commands if (
        command.get("name") == "loadfile" if isinstance(command, dict)
        else command[0] == "loadfile")]


def _load_url(command):
    return command["url"] if isinstance(command, dict) else command[1]


def _values(xml):
    return {node.tag.rsplit("}", 1)[-1]: node.text or "" for node in ET.fromstring(xml).iter()}


def _attributes(xml):
    return {node.tag.rsplit("}", 1)[-1]: node.get("val") for node in ET.fromstring(xml).iter()}


async def _exercise(library: str, directory: Path) -> dict:
    baseline = asyncio.all_tasks()
    started = asyncio.get_running_loop().time()
    tracks = tuple(directory / name for name in ("a.wav", "b.wav", "c.wav"))
    for track, frequency in zip(tracks, (330, 440, 660)):
        _write_tone(track, frequency)
        # Reuse the established generator while keeping this acceptance short.
        with wave.open(str(track), "rb") as source:
            parameters = source.getparams()
            samples = source.readframes(parameters.framerate * DURATION)
        with wave.open(str(track), "wb") as output:
            output.setparams(parameters)
            output.writeframes(samples)
    hashes = [hashlib.sha256(track.read_bytes()).hexdigest() for track in tracks]
    assert len(set(hashes)) == 3, "The native queue must contain distinct source bytes"
    backend = _StagingBackend(library, tracks)
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000",
                 state_dir=str(directory / "state"), advertise_ip="127.0.0.1",
                 bind_address="127.0.0.1", native_queue_staging=False,
                 auto_spawn_mpv=False, enable_ssdp=False, enable_mdns=False)
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)
    receipts = []
    evidence = {"lane": "REAL_BACKEND/null + loopback HTTP/SOAP/received GENA",
                "command_transport": "test ctypes command-node adapter, not Unix IPC",
                "mpv_version": backend.read_property("mpv-version"),
                "native_queue_staging": False, "observer_interval": api._monitor_interval,
                "source_duration_seconds": DURATION, "source_sha256": hashes, "cases": {}}

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
        async with ClientSession(timeout=ClientTimeout(total=5), trust_env=False) as client:
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
                    values = _values(text)
                    if fault:
                        assert values["errorCode"] == str(fault), text
                    return values

            async def http(command, expected=200):
                async with client.get(base + "/httpapi.asp", params={"command": command}) as response:
                    text = await response.text()
                    assert response.status == expected, (command, response.status, text)
                    return json.loads(text) if response.content_type == "application/json" else text

            def native(index, previous_id=None):
                if backend.read_property("path") != str(tracks[index - 1]):
                    return None
                entries = [entry for entry in backend.read_property("playlist")
                           if entry.get("current") is True and entry.get("playing") is True]
                if len(entries) != 1 or entries[0]["id"] == previous_id:
                    return None
                position = backend.read_property("playback-time")
                if (position <= .1 or backend.read_property("pause") is not False
                        or backend.read_property("current-ao") != "null"):
                    return None
                assert backend.read_property("duration") == pytest.approx(DURATION, abs=.01)
                assert backend.read_property("audio-params")["samplerate"] == 16000
                assert any(track.get("type") == "audio" and track.get("selected") is True
                           for track in backend.read_property("track-list"))
                return {"index": index, "native_id": entries[0]["id"], "position": position,
                        "path": str(tracks[index - 1]), "ao": "null"}

            async def decoded(index, previous_id=None):
                # No renderer status read or explicit sync drives these natural
                # transitions. Only independent actual native properties do.
                first = await _eventually("selected native source decoded", lambda: native(index, previous_id), timeout=4)
                later = await _eventually("same native lifetime made decoded progress", lambda: (
                    observed if (observed := native(index, previous_id))
                    and observed["native_id"] == first["native_id"]
                    and observed["position"] > first["position"] + .04 else None))
                await _eventually("autonomous observer confirmed selected playback", lambda: (
                    api.state.phase == PlaybackPhase.PLAYING and api.state.queue.index == index
                    and (api._queue_advance_task is None or api._queue_advance_task.done())))
                return {**later, "first_position": first["position"]}

            async def wire(index, phase, mode, event_start):
                code, path = MODES[mode], str(tracks[index - 1])
                status = await http("getPlayerStatus")
                assert status["loop"] == code and status["plicurr"] == str(index), status
                assert status["playback_phase"] == phase and status["transport_error"] == "", status
                assert status["uri"] == path and status["source"] == f"Source {index}", status
                assert status["Title"] == f"Tone {index}" and status["Artist"] == f"Artist {index}", status
                settings = await soap("GetTransportSettings")
                assert settings["PlayMode"] == mode and settings["RecQualityMode"] == "NOT_IMPLEMENTED", settings
                loop = await soap("GetQueueLoopMode", "<QueueName>Repeat</QueueName>", "PlayQueue")
                assert loop["LoopMode"] == code, loop
                position = await soap("GetPositionInfo")
                assert position["Track"] == str(index) and position["TrackURI"] == path, position
                assert position["TrackDuration"] == "00:00:02" and status["totlen"] == "2", position
                relative = sum(int(part) * factor for part, factor in zip(position["RelTime"].split(":"), (3600, 60, 1)))
                assert abs(relative - int(status["curpos"])) <= 1, (position, status)
                if phase == "PLAYING":
                    assert abs(relative - backend.read_property("playback-time")) <= 1, position
                else:
                    assert relative == 0, position
                assert f"Tone {index}" in position["TrackMetaData"] and f"Artist {index}" in position["TrackMetaData"], position
                transport = await soap("GetTransportInfo")
                assert transport["CurrentTransportState"] == phase and transport["CurrentTransportStatus"] == "OK", transport

                def avt_agrees(props):
                    change = _attributes(props.get("LastChange", "<none/>"))
                    return (change.get("CurrentPlayMode") == mode and change.get("CurrentTrack") == str(index)
                            and change.get("TransportState") == phase and change.get("CurrentTrackURI") == path
                            and props.get("TrackSource") == f"Source {index}")

                def queue_agrees(props):
                    change = _attributes(props.get("LastChange", "<none/>"))
                    return (props.get("LoopMode") == code and props.get("CurrentIndex") == str(index)
                            and props.get("TrackURI") == path and props.get("TrackTitle") == f"Tone {index}"
                            and props.get("TrackSource") == f"Source {index}"
                            and change.get("LoopMode") == code and change.get("CurrentIndex") == str(index))

                for service, agrees in (("AVTransport", avt_agrees), ("PlayQueue", queue_agrees)):
                    await _eventually("received " + service + " mode/selection agreement", lambda: any(
                        sid == subscriptions[service] and agrees(props) for sid, props in receipts[event_start:]))
                return status

            async def set_mode(mode, surface):
                generation, commands = api.state.generation, len(backend.commands)
                playlist = backend.read_property("playlist")
                marker = len(receipts)
                if surface == "soap":
                    await soap("SetPlayMode", f"<InstanceID>0</InstanceID><NewPlayMode>{mode}</NewPlayMode>")
                elif surface == "queue":
                    await soap("SetQueueLoopMode", f"<QueueName>Repeat</QueueName><LoopMode>{MODES[mode]}</LoopMode>", "PlayQueue")
                else:
                    assert await http("setPlayerCmd:loopmode:" + MODES[mode]) == "OK"
                assert api.state.generation == generation, "A policy setter must not replace transport ownership"
                assert backend.commands[commands:] == [], backend.commands[commands:]
                assert backend.read_property("playlist") == playlist, "A policy setter must not restart native playback"
                assert api._native_stage is None
                return marker

            context = ("<PlayList><QueueName>Repeat</QueueName><ListInfo><TrackNumber>3</TrackNumber>"
                       "<StartPlayIndex>1</StartPlayIndex></ListInfo><Tracks>" + "".join(
                           f"<Track{i}><URL>{track}</URL><Title>Tone {i}</Title><Artist>Artist {i}</Artist>"
                           f"<Source>Source {i}</Source><Album>Synthetic</Album><Duration>{DURATION}</Duration></Track{i}>"
                           for i, track in enumerate(tracks, 1)) + "</Tracks></PlayList>")
            await soap("CreateQueue", "<QueueContext>" + escape(context) + "</QueueContext>", "PlayQueue")
            await set_mode("NORMAL", "queue")
            marker = len(receipts)
            await soap("Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")
            normal = []
            for index in (1, 2, 3):
                normal.append(await decoded(index))
                await wire(index, "PLAYING", "NORMAL", marker)
                marker = len(receipts)
            await _eventually("normal queue naturally stopped after C", lambda: (
                backend.read_property("idle-active") is True and api.state.phase == PlaybackPhase.STOPPED), timeout=4)
            await wire(3, "STOPPED", "NORMAL", marker)
            await asyncio.sleep(api._monitor_interval + .05)
            assert [_load_url(command) for command in _loads(backend.commands)] == list(map(str, tracks))
            assert backend.read_property("idle-active") is True
            evidence["cases"]["normal"] = {"decoded": normal, "order": [1, 2, 3], "natural_terminal_stop": True}

            await soap("Stop")
            await set_mode("REPEAT_ALL", "soap")
            marker, loads_before = len(receipts), len(_loads(backend.commands))
            await soap("PlayQueueWithIndex", "<QueueName>Repeat</QueueName><Index>3</Index>", "PlayQueue")
            end = await decoded(3)
            await wire(3, "PLAYING", "REPEAT_ALL", marker)
            marker = len(receipts)
            wrapped = await decoded(1)
            await wire(1, "PLAYING", "REPEAT_ALL", marker)
            assert [_load_url(command) for command in _loads(backend.commands)[loads_before:]] == [str(tracks[2]), str(tracks[0])]
            assert end["native_id"] != wrapped["native_id"]
            commands, generation = len(backend.commands), api.state.generation
            await soap("Previous", fault=711)
            assert api.state.queue.index == 1 and api.state.generation == generation
            assert backend.commands[commands:] == [], backend.commands[commands:]
            evidence["cases"]["repeat_all"] = {"decoded": [end, wrapped], "order": [3, 1], "manual_previous_does_not_wrap": True}

            # Change policy while actual A keeps playing: no native mutation,
            # generation bump or replacement. Then explicitly select B.
            marker = await set_mode("REPEAT_ONE", "http")
            unchanged = await decoded(1)
            assert unchanged["native_id"] == wrapped["native_id"]
            await wire(1, "PLAYING", "REPEAT_ONE", marker)
            marker, loads_before = len(receipts), len(_loads(backend.commands))
            await soap("PlayQueueWithIndex", "<QueueName>Repeat</QueueName><Index>2</Index>", "PlayQueue")
            first = await decoded(2)
            await wire(2, "PLAYING", "REPEAT_ONE", marker)
            marker = len(receipts)
            repeated = await decoded(2, first["native_id"])
            await wire(2, "PLAYING", "REPEAT_ONE", marker)
            assert [_load_url(command) for command in _loads(backend.commands)[loads_before:]] == [str(tracks[1]), str(tracks[1])]
            assert repeated["native_id"] != first["native_id"]
            evidence["cases"]["repeat_one"] = {"decoded": [first, repeated], "order": [2, 2],
                "fresh_native_id_and_replace_load": True, "playing_setter_preserves_native_id_and_generation": True}

            marker = len(receipts)
            assert await http("setPlayerCmd:next") == "OK"
            manual = await decoded(3)
            await wire(3, "PLAYING", "REPEAT_ONE", marker)
            commands, generation = len(backend.commands), api.state.generation
            await soap("Next", fault=711)
            assert await http("setPlayerCmd:next", 400) == "Failed"
            assert api.state.queue.index == 3 and api.state.generation == generation
            assert backend.commands[commands:] == [], backend.commands[commands:]
            evidence["cases"]["manual_navigation"] = {"decoded": manual, "repeat_one_next_selects": 3,
                                                       "manual_next_does_not_wrap": True}

            marker = len(receipts)
            await soap("Stop")
            await wire(3, "STOPPED", "REPEAT_ONE", marker)
            commands = len(backend.commands)
            # Stay stopped beyond the original media duration and a normal
            # observer tick, so silence cannot pass merely before C's EOF.
            stopped_for = DURATION + api._monitor_interval + .05
            await asyncio.sleep(stopped_for)
            assert backend.read_property("idle-active") is True and backend.read_property("playlist") == []
            assert backend.commands[commands:] == [], backend.commands[commands:]
            assert api.state.phase == PlaybackPhase.STOPPED and api.state.queue.index == 3
            evidence["cases"]["explicit_stop"] = {"observed_seconds": stopped_for, "reloads": 0,
                                                     "native_idle_and_empty_playlist": True}
            assert all((command.get("flags") if isinstance(command, dict) else command[2]) == "replace"
                       for command in _loads(backend.commands)), backend.commands
            evidence["native_load_urls"] = [_load_url(command) for command in _loads(backend.commands)]
            evidence["received_event_count"] = len(receipts)
    finally:
        await runner.cleanup()
        backend.close()
        assert api._state_monitor_task is None and not api.event_subscribers and backend._handle is None
    assert not [task for task in asyncio.all_tasks() - baseline if not task.done()]
    evidence["cleanup"] = {"native_handle_destroyed": True, "no_pending_tasks": True,
                           "monitor_stopped": True, "subscriptions_removed": True}
    evidence["elapsed_seconds"] = round(asyncio.get_running_loop().time() - started, 3)
    return evidence


@pytest.mark.skipif(not os.environ.get("ORR_TEST_LIBMPV_PATH"), reason="Native repeat requires ORR_TEST_LIBMPV_PATH")
def test_real_finite_queue_repeat_modes(tmp_path):
    library = Path(os.environ["ORR_TEST_LIBMPV_PATH"]).expanduser().resolve()
    assert library.is_file()
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--probe", str(library), str(tmp_path)],
        cwd=root, env=env, text=True, capture_output=True, timeout=25, check=False)
    assert completed.returncode == 0, f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    evidence = json.loads(completed.stdout)
    assert evidence["cleanup"]["no_pending_tasks"] and len(evidence["cases"]) == 5
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 4 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")

    async def main():
        async with asyncio.timeout(23):
            print(json.dumps(await _exercise(sys.argv[2], Path(sys.argv[3])), sort_keys=True))

    asyncio.run(main())
