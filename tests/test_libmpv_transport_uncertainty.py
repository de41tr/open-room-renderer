"""Optional REAL_BACKEND/null owner-control failure fence.

Generated local WAVs, real local HTTP/SOAP/GENA and inherited production controls
use the existing libmpv command adapter. Only the post-mutation acknowledgement
failure is injected. This is not production Unix IPC, physical audio or Android
acceptance. Native calls run in a bounded child interpreter.
"""
from __future__ import annotations

import asyncio
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
from fake_linkplay.state import RendererState
from test_libmpv_integration import _eventually, _write_tone
from test_libmpv_load_options import _NamedLoadBackend


async def _exercise(library: str, directory: Path, owner_command: str) -> dict:
    tracks = (directory / "one.wav", directory / "two.wav")
    for track, frequency in zip(tracks, (440, 660)):
        _write_tone(track, frequency)
    backend = _NamedLoadBackend(library, tracks)
    original_command = backend.command
    inject = False
    native_applied = False
    loads = []

    async def command(args, timeout=5.0):
        nonlocal inject, native_applied
        if isinstance(args, dict) and args["name"] == "loadfile":
            loads.append(args["url"])
        elif isinstance(args, list) and args[0] == "loadfile":
            loads.append(args[1])
        result = await original_command(args, timeout)
        target = ["stop"] if owner_command == "stop" else ["set_property", "pause", True]
        if inject and args == target:
            inject = False
            property_name = "idle-active" if owner_command == "stop" else "pause"
            await _eventually("native owner control applied", lambda: backend.read_property(property_name) is True)
            native_applied = backend.read_property(property_name) is True
            return {"error": "mpv IPC command timed out"}
        return result

    backend.command = command
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(directory / "state"), advertise_ip="127.0.0.1")
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)
    api._monitor_interval = .01
    receipts = []

    async def callback(request):
        receipts.append(await request.text())
        return web.Response()

    app = api.app()
    app.router.add_route("NOTIFY", "/fixture-events", callback)
    runner = web.AppRunner(app, shutdown_timeout=1)
    try:
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        async with ClientSession(timeout=ClientTimeout(total=12), trust_env=False) as client:
            async with client.request("SUBSCRIBE", base + "/upnp/event/AVTransport", headers={
                "NT": "upnp:event", "CALLBACK": f"<{base}/fixture-events>", "TIMEOUT": "Second-60",
            }) as response:
                assert response.status == 200

            async def soap(action, fields="", service="AVTransport"):
                namespace = ("urn:schemas-wiimu-com:service:PlayQueue:1" if service == "PlayQueue"
                             else "urn:schemas-upnp-org:service:AVTransport:1")
                body = (f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                        f'<u:{action} xmlns:u="{namespace}">{fields}</u:{action}></s:Body></s:Envelope>')
                async with client.post(base + "/upnp/control/" + service, data=body, headers={
                    "SOAPACTION": f'"{namespace}#{action}"', "Content-Type": "text/xml",
                }) as response:
                    text = await response.text()
                    assert response.status == 200, (response.status, text)
                    return text

            context = ("<PlayList><QueueName>Two</QueueName><ListInfo><TrackNumber>2</TrackNumber>"
                       "<StartPlayIndex>1</StartPlayIndex></ListInfo><Tracks>"
                       f"<Track1><URL>{tracks[0]}</URL></Track1><Track2><URL>{tracks[1]}</URL></Track2>"
                       "</Tracks></PlayList>")
            await soap("CreateQueue", "<QueueContext>" + escape(context) + "</QueueContext>", "PlayQueue")
            await soap("PlayQueueWithIndex", "<QueueName>Two</QueueName><Index>1</Index>", "PlayQueue")
            await _eventually("first track decoded", lambda: backend.read_property("playback-time") > .1)
            assert backend.read_property("current-ao") == "null"
            inject = True
            async with client.get(base + "/httpapi.asp", params={"command": "setPlayerCmd:" + owner_command}) as response:
                assert response.status == 502 and await response.text() == "Failed"
            assert native_applied
            await asyncio.sleep(.15)  # Several independent observer turns; no status polling drives this.
            async with client.get(base + "/httpapi.asp", params={"command": "getPlayerStatus"}) as response:
                status = await response.json()
            assert status["playback_phase"] == "ERROR" and status["plicurr"] == "1", status
            assert api.state.intent == owner_command
            assert loads == [str(tracks[0])], loads
            if owner_command == "stop":
                assert backend.read_property("idle-active") is True
                assert "silence not confirmed" in status["transport_error"]
            else:
                assert backend.read_property("pause") is True
                assert backend.read_property("path") == str(tracks[0])
                assert "state unknown" in status["transport_error"]
            transport = await soap("GetTransportInfo", "<InstanceID>0</InstanceID>")
            position = await soap("GetPositionInfo", "<InstanceID>0</InstanceID>")
            assert "<CurrentTransportState>STOPPED</CurrentTransportState>" in transport
            assert "<CurrentTransportStatus>ERROR_OCCURRED</CurrentTransportStatus>" in transport
            assert "<Track>1</Track>" in position
            await _eventually("delivered GENA failure", lambda: any(
                'TransportStatus val="ERROR_OCCURRED"' in (ET.fromstring(body).find(".//LastChange").text or "")
                for body in receipts))
            await soap("Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")
            await _eventually("explicit Play decoded original selection", lambda: (
                backend.read_property("path") == str(tracks[0])
                and backend.read_property("playback-time") > .1
                and backend.read_property("pause") is False))
            assert api.state.status == "play" and api.state.queue.index == 1
            assert loads == [str(tracks[0]), str(tracks[0])], loads
            return {"lane": "REAL_BACKEND/null + HTTP/SOAP/GENA", "command": owner_command,
                    "native_mutation_before_lost_ack": native_applied, "automatic_reload": False,
                    "explicit_play_recovered_original_selection": True}
    finally:
        await runner.cleanup()
        backend.close()
        assert api._state_monitor_task is None and not api.event_subscribers and backend._handle is None


@pytest.mark.skipif(not os.environ.get("ORR_TEST_LIBMPV_PATH"), reason="Native control failures require ORR_TEST_LIBMPV_PATH")
@pytest.mark.parametrize("command", ["stop", "pause"])
def test_real_owner_control_ack_loss_preserves_fence(tmp_path, command):
    library = Path(os.environ["ORR_TEST_LIBMPV_PATH"]).expanduser().resolve()
    assert library.is_file()
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--probe", str(library), str(tmp_path), command],
        cwd=root, env=env, text=True, capture_output=True, timeout=25, check=False)
    assert completed.returncode == 0, f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    evidence = json.loads(completed.stdout)
    assert evidence["native_mutation_before_lost_ack"] and not evidence["automatic_reload"]
    assert evidence["explicit_play_recovered_original_selection"]
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 5 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")

    async def main():
        async with asyncio.timeout(20):
            print(json.dumps(await _exercise(sys.argv[2], Path(sys.argv[3]), sys.argv[4]), sort_keys=True))

    asyncio.run(main())
