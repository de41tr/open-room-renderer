"""UNIT/MOCK: SOAP framing and effects; no network, audio or Android claim."""
from dataclasses import asdict
from html import escape
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET

import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.mpv_backend import BackendError, MpvBackend
from fake_linkplay.soap import SERVICE_TYPES, SOAP_NS
from fake_linkplay.state import RendererState


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.failure = False
        self.url = ""

    async def _call(self, name, value=None):
        self.calls.append((name, value))
        return {"error": "synthetic rejection" if self.failure else "success"}

    async def volume(self, value):
        return await self._call("volume", value)

    async def mute(self, value):
        return await self._call("mute", value)

    async def pause(self):
        return await self._call("pause")

    async def stop(self):
        return await self._call("stop")

    async def resume(self):
        return await self._call("resume")

    async def seek(self, value):
        return await self._call("seek", value)

    async def loadfile(self, url, options=None):
        self.url = url
        return await self._call("load", url)

    async def snapshot(self):
        return {"idle-active": False, "pause": False, "playback-time": 0,
                "track-list": [{"type": "audio", "selected": True}], "path": self.url}


@pytest.fixture
def api(tmp_path):
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path), advertise_ip="127.0.0.1")
    result = HttpApi(cfg, RendererState.from_config(cfg), FakeBackend())
    result.state.volume = 30
    result._wait_for_mpv_audio_ready = AsyncMock(return_value=True)
    return result


def request(action, args="", service="AVTransport", *, path=None, body=None, header=None):
    namespace = SERVICE_TYPES[service]
    xml = (f'<s:Envelope xmlns:s="{SOAP_NS}"><s:Body>'
           f'<u:{action} xmlns:u="{namespace}">{args}</u:{action}>'
           '</s:Body></s:Envelope>')
    return SimpleNamespace(headers={"SOAPACTION": header or f'"{namespace}#{action}"'},
                           match_info={"service": path or service}, text=AsyncMock(return_value=body or xml))


def fault_code(response):
    assert response.status == 500
    root = ET.fromstring(response.text)
    return root.findtext(".//{urn:schemas-upnp-org:control-1-0}errorCode")


@pytest.mark.asyncio
@pytest.mark.parametrize("action,service", [
    ("UnknownAction", "AVTransport"),
    ("SetChannel", "RenderingControl"), ("SelectPreset", "RenderingControl"),
    ("SetQueuePolicy", "PlayQueue"), ("AppendQueue", "PlayQueue"),
    ("AppendTracksInQueue", "PlayQueue"), ("AppendTracksInQueueEx", "PlayQueue"),
    ("RemoveTracksInQueue", "PlayQueue"), ("GetStatus", "ConnectionManager"),
])
async def test_unsupported_faults_without_mutation(api, action, service):
    before = asdict(api.state)
    assert fault_code(await api.soap(request(action, service=service))) == "401"
    assert asdict(api.state) == before
    assert not api.mpv.calls
    assert api.queue_context == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["wrong_endpoint", "wrong_namespace", "wrong_action", "malformed", "double_action", "dtd", "header_argument", "nested_argument"])
async def test_invalid_framing_cannot_mutate(api, kind):
    req = request("SetVolume", "<DesiredVolume>7</DesiredVolume>", "RenderingControl")
    xml = await req.text()
    if kind == "wrong_endpoint":
        req.match_info["service"] = "AVTransport"
    elif kind == "wrong_namespace":
        req.headers["SOAPACTION"] = '"urn:example:wrong#SetVolume"'
    elif kind == "wrong_action":
        xml = xml.replace("SetVolume", "SetMute")
    elif kind == "malformed":
        xml = "<broken>"
    elif kind == "double_action":
        xml = xml.replace("</s:Body>", '<u:Pause xmlns:u="' + SERVICE_TYPES["AVTransport"] + '"/></s:Body>')
    elif kind == "dtd":
        xml = '<!DOCTYPE foo [<!ENTITY a "7">]>' + xml
    elif kind == "header_argument":
        xml = xml.replace("<DesiredVolume>7</DesiredVolume>", "").replace("<s:Body>", "<s:Header><DesiredVolume>7</DesiredVolume></s:Header><s:Body>")
    else:
        xml = xml.replace("<DesiredVolume>7</DesiredVolume>", "<Extension><DesiredVolume>7</DesiredVolume></Extension>")
    req.text = AsyncMock(return_value=xml)
    assert fault_code(await api.soap(req)) in {"401", "402"}
    assert api.state.volume == 30
    assert not api.mpv.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["RenderingControl", "rendercontrol1"])
@pytest.mark.parametrize("channel", ["Master", "Single"])
@pytest.mark.parametrize("control_source", ["", "<ControlSource>WiiMApp</ControlSource>"])
async def test_supported_volume_accepts_optional_client_extensions(api, path, channel, control_source):
    response = await api.soap(request("SetVolume", f"<InstanceID>0</InstanceID><Channel>{channel}</Channel><DesiredVolume>7</DesiredVolume>{control_source}", "RenderingControl", path=path))
    assert response.status == 200
    assert api.mpv.calls == [("volume", 7)]
    assert api.state.volume == 7
    assert ET.fromstring(response.text).find(f".//{{{SERVICE_TYPES['RenderingControl']}}}SetVolumeResponse") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("action,args,service", [
    ("SetVolume", "<DesiredVolume>7</DesiredVolume>", "RenderingControl"),
    ("SetMute", "<DesiredMute>1</DesiredMute>", "RenderingControl"),
    ("Pause", "", "AVTransport"), ("Stop", "", "AVTransport"),
    ("Seek", "<Unit>REL_TIME</Unit><Target>00:00:07</Target>", "AVTransport"),
])
async def test_backend_rejection_does_not_commit_success_state(api, action, args, service):
    api.mpv.failure = True
    api.state.mark_playing("https://media.example.test/one.wav")
    api.state.curpos = 0
    before = asdict(api.state)
    assert fault_code(await api.soap(request(action, args, service))) == "501"
    after = asdict(api.state)
    # A rejected owner command still invalidates older in-flight observations.
    after.pop("generation")
    before.pop("generation")
    if action in {"SetVolume", "SetMute"}:
        assert after["phase"].value == "ERROR"
        assert after["gain_confirmed"] is False
        assert "actual output unknown" in after["last_error"]
        for field in ("phase", "gain_confirmed", "last_error"):
            after.pop(field)
            before.pop(field)
    if action in {"Pause", "Stop"}:
        assert after["phase"].value == "ERROR"
        assert after["intent"] == action.lower()
        assert "confirmation failed" in after["last_error"]
        for field in ("phase", "intent", "last_error"):
            after.pop(field)
            before.pop(field)
    assert after == before


@pytest.mark.asyncio
async def test_backend_exception_is_sanitized(api):
    api.mpv.volume = AsyncMock(side_effect=BackendError("secret detail must not escape"))
    response = await api.soap(request("SetVolume", "<DesiredVolume>7</DesiredVolume>", "RenderingControl"))
    assert fault_code(response) == "501"
    assert "secret" not in response.text
    assert api.state.volume == 30


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ["", "<DesiredVolume>nan</DesiredVolume>", "<DesiredVolume>101</DesiredVolume>", "<DesiredVolume>-1</DesiredVolume>", "<DesiredVolume>7</DesiredVolume><DesiredVolume>8</DesiredVolume>"])
async def test_invalid_volume_has_no_effect(api, args):
    assert fault_code(await api.soap(request("SetVolume", args, "RenderingControl"))) == "402"
    assert not api.mpv.calls


@pytest.mark.asyncio
async def test_select_then_play_loads_exactly_once(api):
    url = "https://media.example.test/one.wav?x=1&y=2"
    assert (await api.soap(request("SetAVTransportURI", f"<CurrentURI>{escape(url)}</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>"))).status == 200
    assert not api.mpv.calls
    assert (await api.soap(request("Play", "<Speed>1</Speed>"))).status == 200
    assert api.mpv.calls == [("load", url)]
    assert api.state.status == "play"


@pytest.mark.asyncio
async def test_load_failure_is_not_soap_success(api):
    api.queue_url = "https://media.example.test/one.wav"
    api.mpv.failure = True
    assert fault_code(await api.soap(request("Play", "<Speed>1</Speed>"))) == "501"
    assert api.state.status != "play"


@pytest.mark.asyncio
@pytest.mark.parametrize("method,args", [("pause", []), ("resume", []), ("stop", []), ("volume", [7]), ("mute", [True]), ("seek", [7])])
async def test_production_backend_requires_acknowledgement(method, args):
    backend = MpvBackend("/unused", False)
    backend.command = AsyncMock(return_value={"error": "synthetic rejection"})
    with pytest.raises(BackendError):
        await getattr(backend, method)(*args)
    backend.command.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("target,seconds", [
    ("0:0:5", 5), ("00:00:05", 5), ("1:2:3", 3723),
    ("001:02:03", 3723), ("0:59:59", 3599),
])
async def test_seek_accepts_padded_or_unpadded_ascii_time(api, target, seconds):
    # Isolate lexical adaptation; confirmed seek/state fences have separate tests.
    api._seek_command = AsyncMock()
    response = await api.soap(request("Seek", f"<Unit>REL_TIME</Unit><Target>{target}</Target>"))
    assert response.status == 200
    api._seek_command.assert_awaited_once_with(seconds)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [
    "0:60:0", "0:0:60", "0:000:5", "0:0:005", "0::5", "0:5", "5",
    "-1:0:5", "+0:0:5", "0:-1:5", "0:0:+5", "0:0:5.0", "0:0:5:0",
    "0:\n0:5", "0:0:5\x01", "0:0:\t5", "0:０:5", "0:0:٥", "０:0:5",
])
async def test_invalid_seek_time_is_rejected_before_any_mutation(api, target):
    before = asdict(api.state)
    api._seek_command = AsyncMock()
    response = await api.soap(request("Seek", f"<Unit>REL_TIME</Unit><Target>{target}</Target>"))
    assert fault_code(response) == "402"
    api._seek_command.assert_not_awaited()
    assert asdict(api.state) == before and not api.mpv.calls
