"""UNIT/MOCK: every status surface advertises the same enabled local services."""
import json
from xml.etree import ElementTree as ET
from unittest.mock import AsyncMock

import pytest

from fake_linkplay.capabilities import service_capability
from fake_linkplay.config import Config, ExperimentalSource
from fake_linkplay.http_api import HttpApi, ParsedCommand
from fake_linkplay.state import RendererState
from fake_linkplay.upnp_description import avtransport_response_fields, control_device_info_json, description_xml
from test_playback_generations import StatefulBackend
from test_soap_faults import request


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["neutral", "app", "experimental", "sentinels"])
async def test_service_capability_is_identical_on_all_protocol_surfaces(tmp_path, profile):
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path), advertise_ip="192.0.2.1",
        wiim_home_profile=profile == "app", source_experiment_variant="tunein" if profile in {"app", "sentinels"} else "none",
        enable_stream_service_sentinels=profile == "sentinels")
    if profile == "experimental":
        cfg.source_experiment_variant = "new_source"
        cfg.experimental_sources = [ExperimentalSource("synthetic-radio", "Synthetic radio", url="https://media.example.test/stream.mp3")]
    state = RendererState.from_config(cfg)
    api = HttpApi(cfg, state, StatefulBackend())
    expected = service_capability(cfg)
    status = await api.execute(ParsedCommand("getStatusEx"), "192.0.2.1")
    assert status["streamServicesCapability"] == expected
    assert json.loads(status["StreamCapability"]) == expected
    assert state.as_status_ex("192.0.2.1")["streamServicesCapability"] == expected
    assert await api.execute(ParsedCommand("getStreamServicesCapability"), "192.0.2.1") == expected
    control = json.loads(control_device_info_json(cfg, "192.0.2.1", state))
    assert control["streamServicesCapability"] == expected
    assert json.loads(control["StreamCapability"]) == expected
    for action in ["GetInfoEx", "GetPositionInfo", "GetMediaInfo"]:
        fields = ET.fromstring("<Root>" + avtransport_response_fields(action, state) + "</Root>")
        assert json.loads(fields.findtext("StreamCapability")) == expected
    response = await api.soap(request("StreamServicesCapability", service="RenderingControl"))
    assert response.status == 200
    root = ET.fromstring(response.text)
    field = next(node for node in root.iter() if node.tag == "StreamCapability")
    assert json.loads(field.text) == expected
    active = {item["id"] for item in expected["streamServices"] if item["active"]}
    assert not active.intersection({"Samba", "Roon", "Spotify", "Tidal", "Deezer2", "SoundCloud", "Squeezelite"})
    assert active == ({"newTuneIn"} if profile in {"app", "sentinels"} else {"synthetic-radio"} if profile == "experimental" else set())


def test_neutral_and_app_profile_do_not_invent_oem_firmware_or_model():
    for profile in [False, True]:
        cfg = Config(uuid="00000000-0000-4000-8000-000000000000", wiim_home_profile=profile, advertise_ip="192.0.2.1")
        state = RendererState.from_config(cfg)
        http = state.as_status_ex("192.0.2.1")
        control = json.loads(control_device_info_json(cfg, "192.0.2.1", state))
        xml = description_xml(cfg)
        assert http["project"] == control["project"] == "Open_Room_Renderer"
        assert http["firmware"] == control["firmware"] == "0.1.0"
        assert "WiiM_Pro" not in xml and "MUZO Cobblestone" not in xml
        assert "Linux Room Renderer" in xml and "Independent" in xml
        assert http["internet"] == control["internet"] == "0"
        assert "<InternetAccess>0</InternetAccess>" in avtransport_response_fields("GetInfoEx", state)


@pytest.mark.asyncio
async def test_unsupported_hardware_and_service_toggles_are_not_successful(tmp_path):
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path))
    api = HttpApi(cfg, RendererState.from_config(cfg), StatefulBackend())
    for name in ["getUnknownCapability", "SetStreamServicesCapability:Active:Roon"]:
        assert (await api.execute(ParsedCommand(name), "192.0.2.1"))["error"] == "Failed"
    for name in ["getSpdifOutMaxCap", "getCoaxOutMaxCap"]:
        result = await api.execute(ParsedCommand(name), "192.0.2.1")
        assert result["ver"] == result["sampleRate"] == result["bitDepth"] == "0"
