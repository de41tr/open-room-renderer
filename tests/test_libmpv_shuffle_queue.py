"""REAL_BACKEND/null: one short actual Shuffle acceptance, with received GENA.

Four distinct generated WAVs, production observer cadence, loopback HTTP/SOAP
and the existing libmpv command-node adapter prove playback, not merely policy.
This does not exercise production Unix IPC, physical audio or endurance. Each
native run has a hard subprocess bound; no external media or FFmpeg is needed.
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
from fake_linkplay.queue import parse_queue_context
from fake_linkplay.state import PlaybackPhase, RendererState
from test_libmpv_integration import _eventually, _write_tone
from test_libmpv_queue_staging import _StagingBackend
from test_libmpv_repeat_queue import _attributes, _load_url, _loads, _values


DURATION = 1.2
ORDER = (2, 4, 1, 3)


class _DeterministicRandom:
    """Force a non-numeric cycle, then exercise the boundary-repeat guard."""

    def __init__(self):
        self.calls = 0
        self.boundary_draws = 0

    def shuffle(self, values):
        self.calls += 1
        if len(values) == 4:
            # The initial cycle ends at 3. Deliberately draw 3 first again;
            # production must prevent that immediate boundary repetition.
            self.boundary_draws += 1
            values[:] = (3, 4, 1, 2)
        else:
            values.sort(key=ORDER.index)


async def _exercise(library: str, directory: Path) -> dict:
    baseline = asyncio.all_tasks()
    started = asyncio.get_running_loop().time()
    tracks = tuple(directory / name for name in ("a.wav", "b.wav", "c.wav", "d.wav"))
    for track, frequency in zip(tracks, (330, 440, 660, 880)):
        _write_tone(track, frequency)
        with wave.open(str(track), "rb") as source:
            parameters = source.getparams()
            samples = source.readframes(round(parameters.framerate * DURATION))
        with wave.open(str(track), "wb") as output:
            output.setparams(parameters)
            output.writeframes(samples)
    hashes = [hashlib.sha256(track.read_bytes()).hexdigest() for track in tracks]
    assert len(set(hashes)) == 4, "Shuffle must select four distinct source byte streams"
    backend = _StagingBackend(library, tracks)
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000",
                 state_dir=str(directory / "state"), advertise_ip="127.0.0.1",
                 bind_address="127.0.0.1", native_queue_staging=False,
                 auto_spawn_mpv=False, enable_ssdp=False, enable_mdns=False)
    rng = _DeterministicRandom()
    api = HttpApi(cfg, RendererState.from_config(cfg), backend, shuffle_rng=rng)
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
        port = site._server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
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

            def native(index):
                if backend.read_property("path") != str(tracks[index - 1]):
                    return None
                entries = [entry for entry in backend.read_property("playlist")
                           if entry.get("current") is True and entry.get("playing") is True]
                if len(entries) != 1:
                    return None
                position = backend.read_property("playback-time")
                if (position <= .06 or backend.read_property("pause") is not False
                        or backend.read_property("current-ao") != "null"):
                    return None
                assert backend.read_property("duration") == pytest.approx(DURATION, abs=.01)
                assert backend.read_property("audio-params")["samplerate"] == 16000
                assert any(track.get("type") == "audio" and track.get("selected") is True
                           for track in backend.read_property("track-list"))
                return {"index": index, "native_id": entries[0]["id"], "position": position,
                        "path": str(tracks[index - 1]), "ao": "null"}

            async def decoded(index):
                # Natural transitions are driven solely by the production
                # observer. Neither public status reads nor explicit sync calls
                # are allowed to drive advancement before this independent check.
                first = await _eventually("selected native source decoded", lambda: native(index))
                later = await _eventually("same native lifetime made decoded progress", lambda: (
                    observed if (observed := native(index))
                    and observed["native_id"] == first["native_id"]
                    and observed["position"] > first["position"] + .04 else None))
                await _eventually("autonomous observer confirmed playback", lambda: (
                    api.state.phase == PlaybackPhase.PLAYING and api.state.queue.index == index
                    and (api._queue_advance_task is None or api._queue_advance_task.done())))
                return {**later, "first_position": first["position"]}

            async def wire(index, phase, code, event_start):
                path = str(tracks[index - 1])
                status = await http("getPlayerStatus")
                assert status["loop"] == str(code) and status["plicurr"] == str(index), status
                assert status["plicount"] == "4" and status["playback_phase"] == phase, status
                assert status["transport_error"] == "" and status["uri"] == path, status
                assert status["Title"] == f"Tone {index}" and status["Artist"] == f"Artist {index}", status
                assert status["source"] == f"Source {index}", status
                assert (await soap("GetTransportSettings"))["PlayMode"] == "SHUFFLE"
                assert (await soap("GetQueueLoopMode", "", "PlayQueue"))["LoopMode"] == str(code)
                position = await soap("GetPositionInfo")
                assert position["Track"] == str(index) and position["TrackURI"] == path, position
                assert f"Tone {index}" in position["TrackMetaData"] and f"Artist {index}" in position["TrackMetaData"]
                assert (await soap("GetTransportInfo"))["CurrentTransportState"] == phase
                context = (await soap("BrowseQueue", "<QueueName>Shuffle</QueueName>", "PlayQueue"))["QueueContext"]
                queue = parse_queue_context(context)
                assert queue.index == index and queue.loop == code
                assert [item.url for item in queue.items] == list(map(str, tracks)), "Canonical queue order changed"
                assert [item.title for item in queue.items] == [f"Tone {i}" for i in range(1, 5)]

                def avt_agrees(props):
                    change = _attributes(props.get("LastChange", "<none/>"))
                    return (change.get("CurrentPlayMode") == "SHUFFLE" and change.get("CurrentTrack") == str(index)
                            and change.get("TransportState") == phase and change.get("CurrentTrackURI") == path
                            and f"Tone {index}" in change.get("CurrentTrackMetaData", "")
                            and f"Artist {index}" in change.get("CurrentTrackMetaData", "")
                            and props.get("TrackSource") == f"Source {index}")

                def queue_agrees(props):
                    change = _attributes(props.get("LastChange", "<none/>"))
                    context = props.get("QueueContext", "")
                    if not context:
                        return False
                    queue = parse_queue_context(context)
                    return (props.get("LoopMode") == str(code) and props.get("CurrentIndex") == str(index)
                            and props.get("TrackURI") == path and props.get("TrackTitle") == f"Tone {index}"
                            and queue.index == index and queue.loop == code
                            and queue.current.artist == f"Artist {index}"
                            and [item.url for item in queue.items] == list(map(str, tracks))
                            and props.get("TrackSource") == f"Source {index}"
                            and change.get("LoopMode") == str(code) and change.get("CurrentIndex") == str(index))

                for service, agrees in (("AVTransport", avt_agrees), ("PlayQueue", queue_agrees)):
                    await _eventually("received " + service + " shuffle/metadata agreement", lambda: any(
                        sid == subscriptions[service] and agrees(props) for sid, props in receipts[event_start:]))

            context = ("<PlayList><QueueName>Shuffle</QueueName><ListInfo><TrackNumber>4</TrackNumber>"
                       "<StartPlayIndex>2</StartPlayIndex></ListInfo><Tracks>" + "".join(
                           f"<Track{i}><URL>{track}</URL><Title>Tone {i}</Title><Artist>Artist {i}</Artist>"
                           f"<Source>Source {i}</Source><Album>Synthetic</Album><Duration>{DURATION}</Duration></Track{i}>"
                           for i, track in enumerate(tracks, 1)) + "</Tracks></PlayList>")
            await soap("CreateQueue", "<QueueContext>" + escape(context) + "</QueueContext>", "PlayQueue")
            await soap("SetPlayMode", "<InstanceID>0</InstanceID><NewPlayMode>SHUFFLE</NewPlayMode>")
            marker = len(receipts)
            await soap("Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")
            history = [await decoded(2)]
            await wire(2, "PLAYING", 3, marker)
            draws = rng.calls
            for command, index in (("next", 4), ("prev", 2), ("next", 4)):
                marker = len(receipts)
                assert await http("setPlayerCmd:" + command) == "OK"
                history.append(await decoded(index))
                await wire(index, "PLAYING", 3, marker)
            assert rng.calls == draws, "Retracing HTTP history must not redraw a permutation"
            assert len({item["native_id"] for item in history}) == 4
            evidence["cases"]["http_history"] = {"order": [item["index"] for item in history], "decoded": history}

            # SOAP stays numerical and nonwrapping, even in Shuffle. Verify it
            # independently while stopped, including both public boundaries.
            await soap("Stop")
            commands = len(backend.commands)
            await soap("Next", fault=711)
            for action, index in (("Previous", 3), ("Previous", 2), ("Next", 3), ("Next", 4),
                                  ("Previous", 3), ("Previous", 2), ("Previous", 1)):
                await soap(action)
                assert (await soap("GetPositionInfo"))["Track"] == str(index)
            await soap("Previous", fault=711)
            assert backend.commands[commands:] == [], "Stopped SOAP navigation must stay silent"
            evidence["cases"]["soap_numeric"] = {"nonwrapping": True, "stopped_native_mutations": 0}

            async def select_two():
                await soap("PlayQueueWithIndex", "<QueueName>Shuffle</QueueName><Index>2</Index>", "PlayQueue")

            marker, loads_before = len(receipts), len(_loads(backend.commands))
            await select_two()
            once = []
            for index in ORDER:
                once.append(await decoded(index))
                await wire(index, "PLAYING", 3, marker)
                marker = len(receipts)
            await _eventually("shuffle-once naturally stopped after one permutation", lambda: (
                backend.read_property("idle-active") is True and api.state.phase == PlaybackPhase.STOPPED))
            await wire(3, "STOPPED", 3, marker)
            # A second observer tick cannot silently restart the finished cycle.
            await asyncio.sleep(api._monitor_interval + .02)
            assert [_load_url(command) for command in _loads(backend.commands)[loads_before:]] == [
                str(tracks[index - 1]) for index in ORDER]
            assert backend.read_property("idle-active") is True
            evidence["cases"]["shuffle_once"] = {"decoded": once, "order": list(ORDER), "natural_terminal_stop": True}

            await soap("Stop")
            generation, commands = api.state.generation, len(backend.commands)
            assert await http("setPlayerCmd:loopmode:2") == "OK"
            assert api.state.generation == generation and backend.commands[commands:] == []
            marker, loads_before = len(receipts), len(_loads(backend.commands))
            await select_two()
            repeated = []
            expected = (*ORDER, 4)
            for index in expected:
                repeated.append(await decoded(index))
                await wire(index, "PLAYING", 2, marker)
                marker = len(receipts)
            assert rng.boundary_draws == 1
            assert sorted(expected[:4]) == [1, 2, 3, 4]
            assert expected[3] != expected[4], "Repeat boundary must not repeat the current item"
            assert len({item["native_id"] for item in repeated}) == 5
            assert [_load_url(command) for command in _loads(backend.commands)[loads_before:]] == [
                str(tracks[index - 1]) for index in expected]
            evidence["cases"]["shuffle_repeat"] = {"decoded": repeated, "order": list(expected),
                "complete_cycles": 1, "next_cycle_first_index": 4, "forced_same_first_draw_corrected": True}

            await soap("Stop")
            await wire(4, "STOPPED", 2, marker)
            commands = len(backend.commands)
            stopped_for = DURATION + api._monitor_interval + .05
            await asyncio.sleep(stopped_for)
            assert backend.read_property("idle-active") is True and backend.read_property("playlist") == []
            assert backend.commands[commands:] == [] and api.state.phase == PlaybackPhase.STOPPED
            assert api.state.queue.index == 4
            evidence["cases"]["owner_stop"] = {"observed_seconds": stopped_for, "reloads": 0,
                                                   "native_idle_and_empty_playlist": True}
            assert all((command.get("flags") if isinstance(command, dict) else command[2]) == "replace"
                       for command in _loads(backend.commands))
            evidence["native_load_urls"] = [_load_url(command) for command in _loads(backend.commands)]
            evidence["received_event_count"] = len(receipts)
    finally:
        await runner.cleanup()
        backend.close()
        assert api._state_monitor_task is None and not api.event_subscribers and backend._handle is None
    assert not site._server.is_serving() and not site._server.sockets, "The loopback listener must be closed"
    assert not [task for task in asyncio.all_tasks() - baseline if not task.done()]
    evidence["cleanup"] = {"native_handle_destroyed": True, "no_pending_tasks": True,
                           "monitor_stopped": True, "subscriptions_removed": True, "listener_closed": True}
    evidence["elapsed_seconds"] = round(asyncio.get_running_loop().time() - started, 3)
    return evidence


@pytest.mark.skipif(not os.environ.get("ORR_TEST_LIBMPV_PATH"), reason="Native Shuffle requires ORR_TEST_LIBMPV_PATH")
def test_real_finite_queue_shuffle_modes(tmp_path):
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
