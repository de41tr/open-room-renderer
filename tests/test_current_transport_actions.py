"""UNIT/INTEGRATION-MOCK dynamic transport availability; no hardware claims."""
import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET

import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi, ParsedCommand
from fake_linkplay.queue import PlaybackQueue, QueueItem
from fake_linkplay.soap import SERVICE_TYPES, SOAP_NS, SoapFault
from fake_linkplay.state import PlaybackPhase, RendererState
from fake_linkplay.upnp_description import avtransport_response_fields, scpd_xml
from test_gena_lifecycle import eventually, local_events
from test_playback_generations import StatefulBackend
from test_soap_faults import request


@pytest.fixture
def api(tmp_path):
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path), advertise_ip="127.0.0.1")
    result = HttpApi(cfg, RendererState.from_config(cfg), StatefulBackend())
    result._wait_for_mpv_audio_ready = AsyncMock(return_value=True)
    return result


def select(api, phase="PLAYING", count=1, index=1, *, station=False, loaded=True):
    api.state.queue = PlaybackQueue(items=[QueueItem(
        url=f"https://media.example.test/{number}.wav", stream_type="station" if station else "",
    ) for number in range(count)], index=index if count else 0)
    api._activate_queue_item()
    api.state.phase = PlaybackPhase(phase)
    api.state.intent = {"PLAYING": "play", "PAUSED": "pause"}.get(phase, "stop")
    if loaded and count:
        api.state.backend_url = api.state.backend_original_url = api.state.current_url
        api.mpv.url, api.mpv.idle = api.state.current_url, False
        api.mpv.paused = phase == "PAUSED"


def event_actions(body):
    outer = ET.fromstring(body)
    last_change = next(node.text for node in outer.iter() if node.tag.endswith("LastChange"))
    inner = ET.fromstring(last_change)
    return next(node.attrib["val"] for node in inner.iter() if node.tag.endswith("CurrentTransportActions"))


@pytest.mark.parametrize("phase,count,index,station,loaded,expected", [
    ("STOPPED", 0, 0, False, False, "Stop"),
    ("STOPPED", 1, 1, False, False, "Play,Stop,Pause"),
    ("PLAYING", 1, 1, False, True, "Play,Stop,Pause,Seek,Previous"),
    ("PAUSED", 1, 1, False, True, "Play,Stop,Pause,Seek,Previous"),
    ("PLAYING", 1, 1, True, True, "Play,Stop,Pause"),
    ("PAUSED", 1, 1, True, True, "Play,Stop,Pause"),
    ("PLAYING", 3, 1, False, True, "Play,Stop,Pause,Seek,Next"),
    ("PLAYING", 3, 2, False, True, "Play,Stop,Pause,Seek,Next,Previous"),
    ("PLAYING", 3, 3, False, True, "Play,Stop,Pause,Seek,Previous"),
    ("STOPPED", 3, 1, False, False, "Play,Stop,Pause,Next"),
    ("STOPPED", 3, 2, False, False, "Play,Stop,Pause,Next,Previous"),
    ("STOPPED", 3, 3, False, False, "Play,Stop,Pause,Previous"),
    ("PAUSED", 3, 2, False, True, "Play,Stop,Pause,Seek"),
    ("PAUSED", 1, 1, False, False, "Play,Stop,Pause"),
    ("PLAYING", 3, 2, False, False, "Play,Stop,Pause,Next,Previous"),
    ("LOADING", 1, 1, False, False, "Play,Stop,Pause"),
    ("BUFFERING", 1, 1, False, True, "Play,Stop,Pause"),
    ("ERROR", 3, 2, False, True, "Play,Stop"),
])
def test_one_projection_matches_soap_and_event_serializers(api, phase, count, index, station, loaded, expected):
    select(api, phase, count, index, station=station, loaded=loaded)
    before = asdict(api.state)
    assert api._current_transport_actions() == expected
    fields = avtransport_response_fields("GetCurrentTransportActions", api.state,
                                         current_transport_actions=api._current_transport_actions())
    assert ET.fromstring(fields).text == expected
    for service in ("AVTransport", "rendertransport1", "AVTransport1"):
        assert event_actions(api._event_body(service)) == expected
    assert asdict(api.state) == before
    assert not api.mpv.calls


@pytest.mark.parametrize("condition,expected", [
    ("uncertain", "Play,Stop"),
    ("failed_stage", "Play,Stop"),
    ("wrong_owner", "Play,Stop,Pause"),
    ("opening", "Play,Stop,Pause"),
    ("seek_closed", "Play,Stop,Pause"),
    ("pending_seek", "Play,Stop,Pause,Seek"),
])
def test_runtime_flags_are_live_not_cached(api, condition, expected):
    select(api)
    if condition == "uncertain":
        api._native_output_uncertain = True
    elif condition == "failed_stage":
        api._native_stage = SimpleNamespace(failed=True)
    elif condition == "wrong_owner":
        api.state.backend_original_url = "https://media.example.test/older.wav"
    elif condition == "opening":
        api._native_waiting_for_audio = True
    elif condition == "seek_closed":
        api._seek_closed = True
    elif condition == "pending_seek":
        api.state.phase = PlaybackPhase.BUFFERING
        api._pending_seek_target = 10
    assert api._current_transport_actions() == expected
    assert event_actions(api._event_body("AVTransport")) == expected


@pytest.mark.parametrize("surface", ["http", "soap"])
async def test_empty_pause_rejects_before_generation_or_backend_mutation(api, surface):
    before = asdict(api.state)
    if surface == "soap":
        response = await api.soap(request("Pause"))
        assert response.status == 500 and "<errorCode>701</errorCode>" in response.text
    else:
        with pytest.raises(SoapFault) as exc:
            await api.execute(ParsedCommand("pause"), "192.0.2.1")
        assert exc.value.code == 701
    assert asdict(api.state) == before
    assert not api.mpv.calls
    assert api._current_transport_actions() == "Stop"


async def test_selected_unloaded_pause_policy_remains_available(api):
    select(api, "STOPPED", loaded=False)
    assert "Pause" in api._current_transport_actions().split(",")
    response = await api.soap(request("Pause"))
    assert response.status == 200
    assert api.state.phase == PlaybackPhase.PAUSED
    assert api.mpv.calls == [("stop", None)]
    assert api._current_transport_actions() == "Play,Stop,Pause"


async def test_unknown_duration_finite_seek_remains_supported(api):
    select(api)
    original = api.mpv.snapshot
    async def snapshot():
        return {**await original(), "duration": None}
    api.mpv.snapshot = snapshot
    assert api.state.totlen == api.state.queue.current.duration == 0
    assert "Seek" in api._current_transport_actions().split(",")
    response = await api.soap(request("Seek", "<Unit>REL_TIME</Unit><Target>00:00:05</Target>"))
    assert response.status == 200
    assert api.mpv.calls == [("seek", 5)]
    assert api.state.curpos == 5


async def test_query_observes_backend_end_before_advertising_seek(api):
    select(api)
    api.mpv.idle = True
    response = await api.soap(request("GetCurrentTransportActions"))
    assert response.status == 200
    assert ET.fromstring(response.text).findtext(".//Actions") == "Play,Stop,Pause"
    assert api.state.phase == PlaybackPhase.STOPPED
    assert event_actions(api._event_body("AVTransport")) == "Play,Stop,Pause"


def test_static_scpd_remains_a_supported_operation_contract(api):
    root = ET.fromstring(scpd_xml("AVTransport"))
    ns = {"s": "urn:schemas-upnp-org:service-1-0"}
    names = {node.text for node in root.findall("s:actionList/s:action/s:name", ns)}
    assert {"Play", "Pause", "Stop", "Seek", "Next", "Previous"} <= names
    assert api._current_transport_actions() == "Stop"


@pytest.mark.parametrize("service", ["AVTransport", "rendertransport1", "AVTransport1"])
async def test_loopback_http_soap_and_gena_follow_same_live_actions(local_events, service):
    env = local_events
    status, headers = await env.subscribe(service=service)
    assert status == 200
    await eventually(lambda: len(env.receipts) == 1)
    assert event_actions(env.receipts[-1][1]) == "Stop"

    async def soap(action, args=""):
        namespace = SERVICE_TYPES["AVTransport"]
        body = (f'<s:Envelope xmlns:s="{SOAP_NS}"><s:Body>'
                f'<u:{action} xmlns:u="{namespace}">{args}</u:{action}>'
                '</s:Body></s:Envelope>')
        async with env.client.post(env.base + "/upnp/control/" + service, data=body,
                                   headers={"SOAPACTION": f'"{namespace}#{action}"'}) as response:
            return response.status, await response.text()

    # HTTP failure agrees with SOAP/event availability, without changing state.
    async with env.client.get(env.base + "/httpapi.asp", params={"command": "setPlayerCmd:pause"}) as response:
        assert response.status == 400
    assert env.api.state.phase == PlaybackPhase.STOPPED
    assert not env.api.mpv.calls

    assert (await soap("SetAVTransportURI", "<CurrentURI>https://media.example.test/file.wav</CurrentURI>"))[0] == 200
    await eventually(lambda: event_actions(env.receipts[-1][1]) == "Play,Stop,Pause")
    assert (await soap("Play", "<Speed>1</Speed>"))[0] == 200
    expected = "Play,Stop,Pause,Seek,Previous"
    await eventually(lambda: event_actions(env.receipts[-1][1]) == expected)
    status, body = await soap("GetCurrentTransportActions")
    assert status == 200 and ET.fromstring(body).findtext(".//Actions") == expected
    async with env.client.get(env.base + "/httpapi.asp", params={"command": "getPlayerStatus"}) as response:
        assert response.status == 200 and (await response.json())["status"] == "play"
    async with env.client.get(env.base + "/httpapi.asp", params={"command": "setPlayerCmd:stop"}) as response:
        assert response.status == 200
    await eventually(lambda: event_actions(env.receipts[-1][1]) == "Play,Stop,Pause")
    status, body = await soap("GetCurrentTransportActions")
    assert status == 200 and ET.fromstring(body).findtext(".//Actions") == "Play,Stop,Pause"
    assert await env.unsubscribe(headers["SID"], service=service) == 200


@pytest.mark.parametrize("phase,count,index,station,loaded,action,code", [
    ("STOPPED", 0, 0, False, False, "Play", 701),
    ("STOPPED", 0, 0, False, False, "Next", 711),
    ("STOPPED", 0, 0, False, False, "Previous", 701),
    ("STOPPED", 1, 1, False, False, "Seek", 701),
    ("PLAYING", 1, 1, True, True, "Seek", 710),
    ("PLAYING", 1, 1, True, True, "Previous", 710),
    ("PLAYING", 3, 1, False, True, "Previous", 711),
    ("PLAYING", 3, 3, False, True, "Next", 711),
])
async def test_known_unavailable_actions_are_hidden_and_reject_without_mutation(
    api, phase, count, index, station, loaded, action, code,
):
    select(api, phase, count, index, station=station, loaded=loaded)
    assert action not in api._current_transport_actions().split(",")
    assert action not in event_actions(api._event_body("AVTransport")).split(",")
    before = asdict(api.state)
    args = "<Unit>REL_TIME</Unit><Target>00:00:05</Target>" if action == "Seek" else ""
    response = await api.soap(request(action, args))
    assert response.status == 500 and f"<errorCode>{code}</errorCode>" in response.text
    assert asdict(api.state) == before
    assert not api.mpv.calls


@pytest.mark.parametrize("phase,intent,loaded,expected", [
    ("PLAYING", "pause", True, "Play,Stop,Pause"),
    ("PAUSED", "stop", True, "Play,Stop,Pause"),
    ("STOPPED", "pause", False, "Play,Stop,Pause"),
    ("COMMAND_ACCEPTED", "pause", True, "Play,Stop,Pause"),
    ("LOADING", "pause", False, "Play,Stop,Pause"),
    ("COMMAND_ACCEPTED", "stop", True, "Play,Stop,Pause,Next,Previous"),
    ("COMMAND_ACCEPTED", "play", False, "Play,Stop,Pause,Next,Previous"),
    ("BUFFERING", "play", True, "Play,Stop,Pause,Next,Previous"),
    ("BUFFERING", "play", False, "Play,Stop,Pause,Next,Previous"),
    ("LOADING", "play", False, "Play,Stop,Pause,Next,Previous"),
    ("ERROR", "stop", True, "Play,Stop"),
    ("ERROR", "pause", True, "Play,Stop"),
])
def test_queue_actions_follow_owner_intent_before_transient_phase(api, phase, intent, loaded, expected):
    select(api, phase, count=3, index=2, loaded=loaded)
    api.state.intent = intent
    before = asdict(api.state)
    assert api._current_transport_actions() == expected
    fields = avtransport_response_fields("GetCurrentTransportActions", api.state,
                                         current_transport_actions=api._current_transport_actions())
    assert ET.fromstring(fields).text == expected
    for service in ("AVTransport", "rendertransport1", "AVTransport1"):
        assert event_actions(api._event_body(service)) == expected
    assert asdict(api.state) == before
    assert not api.mpv.calls


@pytest.mark.parametrize("intent", ["play", "pause", "stop"])
def test_uncertainty_overrides_navigation_owner_intent(api, intent):
    select(api, "COMMAND_ACCEPTED", count=3, index=2)
    api.state.intent = intent
    api._native_output_uncertain = True
    assert api._current_transport_actions() == "Play,Stop"


@pytest.mark.parametrize("phase,intent,loaded,navigation", [
    ("PLAYING", "pause", True, False),
    ("COMMAND_ACCEPTED", "pause", True, False),
    ("COMMAND_ACCEPTED", "stop", True, True),
    ("LOADING", "play", False, True),
    ("BUFFERING", "play", False, True),
])
async def test_real_query_and_event_keep_navigation_owner_intent(api, phase, intent, loaded, navigation):
    select(api, phase, count=3, index=2, loaded=loaded)
    api.state.intent = intent
    if phase == "BUFFERING":
        api.mpv.idle, api.mpv.buffering = False, True
    response = await api.soap(request("GetCurrentTransportActions"))
    assert response.status == 200
    actions = ET.fromstring(response.text).findtext(".//Actions")
    assert ("Next" in actions.split(",")) is navigation
    assert ("Previous" in actions.split(",")) is navigation
    assert api.state.intent == intent and api.state.phase == PlaybackPhase(phase)
    assert event_actions(api._event_body("AVTransport")) == actions


@pytest.mark.parametrize("phase,intent", [
    ("PLAYING", "pause"), ("PAUSED", "stop"), ("PAUSED", "play"), ("PLAYING", "stop"),
])
async def test_inconsistent_seek_owner_is_not_advertised(api, phase, intent):
    select(api, phase)
    api.state.intent = intent
    generation = api.state.generation
    assert api._current_transport_actions() == "Play,Stop,Pause"
    response = await api.soap(request("GetCurrentTransportActions"))
    assert response.status == 200
    assert ET.fromstring(response.text).findtext(".//Actions") == "Play,Stop,Pause"
    assert event_actions(api._event_body("AVTransport")) == "Play,Stop,Pause"
    assert api.state.generation == generation and not api.mpv.calls


@pytest.mark.parametrize("paused", [False, True])
async def test_legitimate_pending_seek_keeps_seek_available(api, paused):
    select(api, "PAUSED" if paused else "PLAYING")
    api._seek_timeout, api._seek_poll_interval = 1, .005
    async def seek_without_immediate_position(target):
        api.mpv.calls.append(("seek", target))
    api.mpv.seek = seek_without_immediate_position
    task = asyncio.create_task(api.soap(request("Seek", "<Unit>REL_TIME</Unit><Target>00:00:05</Target>")))
    try:
        await eventually(lambda: api._pending_seek_target == 5)
        assert api.state.phase == PlaybackPhase.BUFFERING
        response = await api.soap(request("GetCurrentTransportActions"))
        assert response.status == 200
        actions = ET.fromstring(response.text).findtext(".//Actions")
        assert actions == "Play,Stop,Pause,Seek"
        assert event_actions(api._event_body("AVTransport")) == actions
        api.mpv.position = 5
        assert (await task).status == 200
        assert api.state.phase == (PlaybackPhase.PAUSED if paused else PlaybackPhase.PLAYING)
        assert api._current_transport_actions() == "Play,Stop,Pause,Seek,Previous"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_pending_target_does_not_make_stop_intent_seekable(api):
    select(api)
    api.state.phase, api.state.intent = PlaybackPhase.BUFFERING, "stop"
    api._pending_seek_target = 5
    assert api._current_transport_actions() == "Play,Stop,Pause"
