"""INTEGRATION/MOCK protocol handshake; synthetic discovery, no Android app."""
import asyncio
import json
from unittest.mock import AsyncMock
from html import escape
from xml.etree import ElementTree as ET

import pytest
from aiohttp import ClientSession, web

from fake_linkplay.config import load_config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.mdns import MdnsAdvertiser
from fake_linkplay.ssdp import response_packet
from fake_linkplay.state import RendererState
from test_playback_generations import StatefulBackend


class HandshakeBackend(StatefulBackend):
    async def volume(self, value):
        self.calls.append(("volume", value))

    async def mute(self, value):
        self.calls.append(("mute", value))


@pytest.mark.asyncio
async def test_discovery_contract_description_handshake_and_two_track_controls(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"state_dir: {tmp_path / 'state'}\nwiim_home_profile: true\nadvertise_ip: 127.0.0.1\nhttp_api_port: 0\nupnp_port: 0\nsource_experiment_variant: none\n")
    cfg = load_config(config_path)
    backend = HandshakeBackend()
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)
    runner = web.AppRunner(api.app())
    notifications = []
    async def callback(request):
        notifications.append((request.headers.get("SID"), await request.text()))
        return web.Response()
    callback_app = web.Application()
    callback_app.router.add_route("NOTIFY", "/events", callback)
    callback_runner = web.AppRunner(callback_app)
    await runner.setup()
    await callback_runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    callback_site = web.TCPSite(callback_runner, "127.0.0.1", 0)
    try:
        await site.start()
        await callback_site.start()
        cfg.http_api_port = cfg.upnp_port = site._server.sockets[0].getsockname()[1]
        base = cfg.api_base_url
        discovery = response_packet(cfg, "urn:schemas-upnp-org:device:MediaRenderer:1").decode()
        assert f"LOCATION: {base}/description.xml" in discovery
        assert f"USN: uuid:{cfg.upnp_uuid}" in discovery
        assert cfg.upnp_uuid.startswith("FF")
        mdns = list(MdnsAdvertiser(cfg)._services())[0]
        assert mdns[1] == cfg.http_api_port
        assert mdns[3]["uuid"] == "uuid:" + cfg.upnp_uuid
        async with ClientSession() as client:
            callback_url = f"http://127.0.0.1:{callback_site._server.sockets[0].getsockname()[1]}/events"
            subscriptions = {}
            for service in ("AVTransport", "RenderingControl", "PlayQueue"):
                async with client.request("SUBSCRIBE", base + "/upnp/event/" + service, headers={"NT": "upnp:event", "CALLBACK": f"<{callback_url}>", "TIMEOUT": "Second-60"}) as response:
                    assert response.status == 200
                    subscriptions[service] = response.headers["SID"]
            async def event_matches(service, predicate):
                async with asyncio.timeout(2):
                    while True:
                        for sid, body in reversed(notifications):
                            if sid == subscriptions[service]:
                                props = {node.tag: node.text or "" for prop in ET.fromstring(body) for node in prop}
                                if predicate(props):
                                    return props
                        await asyncio.sleep(0.005)
            async with client.get(base + "/description.xml") as response:
                assert response.status == 200
                descriptor = ET.fromstring(await response.text())
            ns = {"d": "urn:schemas-upnp-org:device-1-0", "s": "urn:schemas-upnp-org:service-1-0"}
            assert descriptor.findtext(".//d:UDN", namespaces=ns) == "uuid:" + cfg.upnp_uuid
            services = descriptor.findall(".//d:service", ns)
            for service in services:
                path = service.findtext("d:SCPDURL", namespaces=ns)
                async with client.get(base + path) as response:
                    assert response.status == 200
                    schema = ET.fromstring(await response.text())
                actions = {a.text for a in schema.findall("s:actionList/s:action/s:name", ns)}
                assert "GetStatus" not in actions
                assert actions

            async def soap(action, fields="", service="AVTransport", path=None):
                namespace = "urn:schemas-wiimu-com:service:PlayQueue:1" if service == "PlayQueue" else f"urn:schemas-upnp-org:service:{service}:1"
                xml = f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><u:{action} xmlns:u="{namespace}">{fields}</u:{action}></s:Body></s:Envelope>'
                async with client.post(base + (path or f"/upnp/control/{service}"), data=xml.encode(), headers={"SOAPACTION": f'"{namespace}#{action}"', "Content-Type": "text/xml"}) as response:
                    text = await response.text()
                    assert response.status == 200, (action, text)
                    return {node.tag: node.text for node in ET.fromstring(text).iter() if len(node) == 0}

            control = await soap("GetControlDeviceInfo", "<InstanceID>0</InstanceID>", "RenderingControl", "/upnp/control/rendercontrol1")
            info = json.loads(control["Status"])
            assert info["uuid"] == cfg.upnp_uuid
            assert info["preset_key"] == api.state.as_status_ex(cfg.resolved_advertise_ip)["preset_key"] == "0"
            assert control["MultiType"] == "1" and control["SlaveMask"] == "0"
            assert info["streamServicesCapability"]["streamServices"] == []
            assert (await soap("GetInfoEx", "<InstanceID>0</InstanceID>", path="/upnp/transport"))["CurrentTransportState"] == "STOPPED"
            first, second = "https://media.example.test/one.wav", "https://media.example.test/two.wav"
            queue = f"<PlayList><QueueName>Handshake queue</QueueName><ListInfo><SourceName>UPnPServer</SourceName></ListInfo><Tracks><Track1><URL>{first}</URL></Track1><Track2><URL>{second}</URL></Track2></Tracks></PlayList>"
            assert (await soap("CreateQueue", f"<QueueContext>{escape(queue)}</QueueContext>", "PlayQueue"))["QueueName"] == "Handshake queue"
            await soap("PlayQueueWithIndex", "<QueueName>Handshake queue</QueueName><Index>1</Index>", "PlayQueue")
            assert backend.url == first
            await soap("Pause")
            assert backend.paused
            await soap("Play")
            assert not backend.paused
            await soap("Next")
            assert backend.url == second
            position = await soap("GetPositionInfo")
            assert position["Track"] == "2" and position["TrackURI"] == second
            queue_event = await event_matches("PlayQueue", lambda props: props.get("CurrentIndex") == "2")
            assert queue_event["TrackURI"] == second and queue_event["Count"] == "2"
            transport_event = await event_matches("AVTransport", lambda props: '<CurrentTrack val="2"/>' in props.get("LastChange", ""))
            assert second in transport_event["LastChange"]
            await soap("SetVolume", "<InstanceID>0</InstanceID><Channel>Single</Channel><DesiredVolume>7</DesiredVolume><ControlSource>WiiMApp</ControlSource>", "RenderingControl", "/upnp/rendering")
            await soap("SetMute", "<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredMute>1</DesiredMute>", "RenderingControl")
            assert api.state.volume == 7 and api.state.mute
            await event_matches("RenderingControl", lambda props: props.get("Volume") == "7" and props.get("Mute") == "1")
            await soap("Stop")
            assert backend.idle
            await event_matches("AVTransport", lambda props: 'TransportState val="STOPPED"' in props.get("LastChange", "") and second in props.get("LastChange", ""))
            async with client.get(base + "/httpapi.asp", params={"command": "getPlayerStatusEx"}) as response:
                status = await response.json()
            assert status["status"] == "stop" and status["plicurr"] == "2"
            identity = cfg.upnp_uuid
            api.on_rename = AsyncMock()
            async with client.get(base + "/httpapi.asp", params={"command": "setDeviceName:Café Room"}) as response:
                assert response.status == 200 and await response.text() == "OK"
            api.on_rename.assert_awaited_once()
            assert cfg.upnp_uuid == identity
            assert list(MdnsAdvertiser(cfg)._services())[0][2] == "Café Room"
            renamed = await soap("GetControlDeviceInfo", service="RenderingControl")
            assert json.loads(renamed["Status"])["DeviceName"] == "Café Room"
            for service, sid in subscriptions.items():
                async with client.request("UNSUBSCRIBE", base + "/upnp/event/" + service, headers={"SID": sid}) as response:
                    assert response.status == 200
    finally:
        await runner.cleanup()
        await callback_runner.cleanup()
    assert api._state_monitor_task is None
