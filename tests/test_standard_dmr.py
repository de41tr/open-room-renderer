"""INTEGRATION + MOCK audio: unchanged standard DMR client over real HTTP.

Strict UpnpFactory binds the published description/SCPDs. DmrDevice controls and
AiohttpNotifyServer parse complete subscriptions and LastChange without polling
or schema/client hooks. No media fetch, decoder, physical LAN or app claim.
The optional client is already supplied by the pywiim development extra.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import timedelta
from html import escape
import socket
from xml.etree import ElementTree as ET

import pytest
from aiohttp import web

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.soap import SERVICE_TYPES
from fake_linkplay.state import RendererState
from fake_linkplay.upnp_description import description_xml
from test_playback_generations import StatefulBackend

pytest.importorskip("async_upnp_client", reason="standard DMR integration requires the optional UPnP client")
from async_upnp_client.aiohttp import AiohttpNotifyServer, AiohttpRequester
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.profiles.dlna import DmrDevice, PlayMode, TransportState


class GainBackend(StatefulBackend):
    """Audio is fake; gain methods acknowledge the same commands as transport."""

    async def volume(self, value):
        self.calls.append(("volume", value))

    async def mute(self, value):
        self.calls.append(("mute", value))


async def eventually(check):
    async with asyncio.timeout(3):
        while not check():
            await asyncio.sleep(0.005)


@pytest.mark.parametrize("profile", [False, True], ids=["neutral", "compatibility"])
def test_description_preserves_aliases_and_selects_canonical_services(profile):
    ns = {"d": "urn:schemas-upnp-org:device-1-0"}
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", wiim_home_profile=profile)
    services = ET.fromstring(description_xml(cfg)).findall(".//d:service", ns)
    selected = {node.findtext("d:serviceType", namespaces=ns): node for node in services}
    ids = {node.findtext("d:serviceId", namespaces=ns) for node in services}
    for name, alias in [("AVTransport", "rendertransport1"), ("RenderingControl", "rendercontrol1"),
                        ("ConnectionManager", "renderconnmgr1")]:
        node = selected[SERVICE_TYPES[name]]
        assert node.findtext("d:serviceId", namespaces=ns) == f"urn:upnp-org:serviceId:{name}"
        assert node.findtext("d:controlURL", namespaces=ns) == f"/upnp/control/{name}"
        assert node.findtext("d:eventSubURL", namespaces=ns) == f"/upnp/event/{name}"
        assert f"urn:upnp-org:serviceId:{name}1" in ids
        legacy = next(item for item in services
                      if item.findtext("d:serviceId", namespaces=ns) == f"urn:upnp-org:serviceId:{name}1")
        assert legacy.findtext("d:controlURL", namespaces=ns) == f"/upnp/control/{alias}"
        assert legacy.findtext("d:eventSubURL", namespaces=ns) == f"/upnp/event/{alias}"


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [False, True], ids=["neutral", "compatibility"])
async def test_standard_dmr_controls_and_full_parsed_events_without_polling(tmp_path, profile):
    baseline_tasks = asyncio.all_tasks()
    requests = []
    events = []
    backend = GainBackend()
    async with AsyncExitStack() as resources:
        listener = resources.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        listener.bind(("127.0.0.1", 0))
        listener.listen(socket.SOMAXCONN)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path),
                     advertise_ip="127.0.0.1", bind_address="127.0.0.1", http_api_port=port,
                     upnp_port=port, wiim_home_profile=profile, auto_spawn_mpv=False,
                     enable_ssdp=False, enable_mdns=False)
        api = HttpApi(cfg, RendererState.from_config(cfg), backend)

        @web.middleware
        async def record(request, handler):
            response = await handler(request)
            requests.append((request.method, request.path, request.headers.get("SOAPAction", ""),
                             await request.text(), response.status))
            return response

        app = api.app()
        app.middlewares.append(record)
        runner = web.AppRunner(app, shutdown_timeout=1)
        await runner.setup()
        resources.push_async_callback(runner.cleanup)
        await web.SockSite(runner, listener).start()
        requester = AiohttpRequester(timeout=3)
        # Strict defaults. No filters, monkeypatches, extension arguments or hooks.
        device = await UpnpFactory(requester).async_create_device(base + "/description.xml")
        assert DmrDevice.is_profile_device(device)
        notify = AiohttpNotifyServer(requester, source=("127.0.0.1", 0))
        await notify.async_start_server()
        resources.push_async_callback(notify.async_stop_server)
        dmr = DmrDevice(device, notify.event_handler)
        dmr.on_event = lambda service, variables: events.append(
            (service.service_id, {variable.name: variable.value for variable in variables}))
        resources.push_async_callback(dmr.async_unsubscribe_services)
        await dmr.async_subscribe_services()
        assert dmr.is_subscribed
        assert {subscriber.service for subscriber in api.event_subscribers.values()} == {
            "AVTransport", "RenderingControl", "ConnectionManager"}
        await eventually(lambda: DmrDevice.SERVICE_IDS <= {service for service, _ in events})
        assert dmr.transport_state == TransportState.STOPPED
        assert dmr.volume_level == .5 and dmr.is_volume_muted is False
        cm = device.service(SERVICE_TYPES["ConnectionManager"])
        assert {name: cm.state_variable(name).value for name in (
            "SourceProtocolInfo", "SinkProtocolInfo", "CurrentConnectionIDs")} == {
                "SourceProtocolInfo": "", "SinkProtocolInfo": "http-get:*:audio/*:*", "CurrentConnectionIDs": ""}

        uri = base + "/synthetic-never-fetched.wav?key=a%26b"
        title = "Synthetic & 日本語"
        metadata = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
                    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
                    'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
                    '<item id="1" parentID="0" restricted="1">'
                    f'<dc:title>{escape(title)}</dc:title>'
                    '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
                    f'<res protocolInfo="http-get:*:audio/wav:*">{escape(uri)}</res></item></DIDL-Lite>')
        await dmr.async_set_transport_uri(uri, title, meta_data=metadata)
        await eventually(lambda: dmr.can_play and dmr.media_title == title)
        await dmr.async_play()
        await eventually(lambda: dmr.transport_state == TransportState.PLAYING and dmr.can_seek_rel_time)
        assert backend.url == uri and not backend.paused
        assert device.service(SERVICE_TYPES["AVTransport"]).state_variable("CurrentTrackURI").value == uri
        await dmr.async_seek_rel_time(timedelta(seconds=5))
        await eventually(lambda: dmr.media_position == 5)
        assert backend.position == 5
        await dmr.async_pause()
        await eventually(lambda: dmr.transport_state == TransportState.PAUSED_PLAYBACK)
        assert backend.paused
        await dmr.async_play()
        await eventually(lambda: dmr.transport_state == TransportState.PLAYING)
        assert not backend.paused
        await dmr.async_set_volume_level(.3)
        await dmr.async_mute_volume(True)
        await eventually(lambda: dmr.volume_level == .3 and dmr.is_volume_muted is True)
        assert ("volume", 30) in backend.calls and ("mute", True) in backend.calls
        await dmr.async_set_play_mode(PlayMode.REPEAT_ALL)
        await eventually(lambda: dmr.play_mode == PlayMode.REPEAT_ALL)

        # Queue creation is setup through an existing public service; navigation
        # itself uses only standard DMR Next/Previous and their evented guards.
        second = base + "/synthetic-second-never-fetched.wav"
        queue = ("<PlayList><QueueName>Standard DMR</QueueName><Tracks>"
                 f"<Track1><URL>{escape(uri)}</URL></Track1>"
                 f"<Track2><URL>{escape(second)}</URL></Track2></Tracks></PlayList>")
        pq = device.service(SERVICE_TYPES["PlayQueue"])
        await pq.action("CreateQueue").async_call(QueueContext=queue)
        await pq.action("PlayQueueWithIndex").async_call(QueueName="Standard DMR", Index=1)
        await eventually(lambda: dmr.media_track_number == 1 and dmr.can_next)
        await dmr.async_next()
        await eventually(lambda: dmr.media_track_number == 2 and dmr.can_previous)
        assert backend.url == second
        await dmr.async_previous()
        await eventually(lambda: dmr.media_track_number == 1)
        assert backend.url == uri
        await dmr.async_stop()
        await eventually(lambda: dmr.transport_state == TransportState.STOPPED)
        assert backend.idle

        # Event-derived observations above must precede every ordinary read.
        standard = [entry for entry in requests if entry[2] and not entry[2].startswith(f'"{SERVICE_TYPES["PlayQueue"]}#')]
        assert {entry[2].split("#")[-1].strip('"') for entry in standard} == {
            "SetAVTransportURI", "Play", "Pause", "Seek", "SetVolume", "SetMute", "SetPlayMode", "Next", "Previous", "Stop"}
        assert all(entry[4] == 200 and "ControlSource" not in entry[3] for entry in standard)
        assert all(entry[1] in {"/upnp/control/AVTransport", "/upnp/control/RenderingControl"} for entry in standard)
        assert await cm.action("GetProtocolInfo").async_call() == {"Source": "", "Sink": "http-get:*:audio/*:*"}
        assert await cm.action("GetCurrentConnectionIDs").async_call() == {"ConnectionIDs": ""}
        await dmr.async_update()
        assert dmr.transport_state == TransportState.STOPPED and dmr.volume_level == .3
        sids = set(api.event_subscribers)
        await dmr.async_subscribe_services()  # Actual renewal of all three leases.
        assert set(api.event_subscribers) == sids
        await dmr.async_unsubscribe_services()
        assert not dmr.is_subscribed and not api.event_subscribers
    await asyncio.sleep(0)
    assert api._state_monitor_task is None and not api.event_subscribers
    assert not [task for task in asyncio.all_tasks() - baseline_tasks if not task.done()]
