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
@pytest.mark.parametrize("profile", ["neutral", "app", "experimental", "sentinels", "plex", "plex_legacy"])
async def test_service_capability_is_identical_on_all_protocol_surfaces(tmp_path, profile):
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path), advertise_ip="192.0.2.1",
        wiim_home_profile=profile == "app", source_experiment_variant="tunein" if profile in {"app", "sentinels", "plex", "plex_legacy"} else "none",
        enable_stream_service_sentinels=profile == "sentinels",
        enable_plex_service=profile.startswith("plex"), enable_plex_legacy_gate=profile == "plex_legacy")
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
    assert not active.intersection({"Samba", "Roon", "Spotify", "Tidal", "Deezer2", "SoundCloud", "Squeezelite", "Plex", "PLEX"})
    assert active == ({"newTuneIn"} if profile in {"app", "sentinels", "plex", "plex_legacy"} else {"synthetic-radio"} if profile == "experimental" else set())


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


@pytest.mark.parametrize("exact", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_unimplemented_plex_experiment_never_becomes_active(exact, legacy):
    from fake_linkplay.capabilities import capability_fields, PLEX_STREAMS_BIT
    cfg = Config(enable_plex_service=True, enable_plex_legacy_gate=legacy,
        plex_capability_mode="exact" if exact else "normal", source_experiment_variant="tunein")
    fields = capability_fields(cfg)
    plex = next(item for item in fields["streamServicesCapability"]["streamServices"] if item["id"] == "Plex")
    assert plex["active"] is plex["configurable"] is False
    assert plex["experimental"] is True
    assert "Plex" not in fields["supported_services"] and "PLEX" not in fields["supported_services"]
    assert all(item["id"].lower() != "plex" for item in fields["music_services"])
    assert not int(fields["streams"]) & PLEX_STREAMS_BIT
    assert "streams_all" not in fields


@pytest.mark.parametrize("value", [str((1 << 43) | 200), hex((1 << 43) | 200)])
def test_legacy_diagnostic_mask_cannot_reenable_plex(value):
    from fake_linkplay.capabilities import capability_fields, PLEX_STREAMS_BIT
    cfg = Config(source_experiment_variant="tunein", enable_plex_service=True,
        enable_plex_legacy_gate=True, tunein_streams_value=value, tunein_stream_all_value=value)
    fields = capability_fields(cfg)
    assert fields["streams"] == fields["streams_all"] == "200"
    assert not int(fields["streams"]) & PLEX_STREAMS_BIT


@pytest.mark.parametrize("service_id", ["Plex", "PLEX", "plex"])
def test_custom_source_label_cannot_claim_plex_provider_support(service_id):
    from fake_linkplay.capabilities import capability_fields
    cfg = Config(source_experiment_variant="new_source", enable_plex_service=True,
        enable_plex_legacy_gate=True, experimental_sources=[
            ExperimentalSource(service_id, "Synthetic direct source", url="https://media.example.test/song.wav")])
    fields = capability_fields(cfg)
    assert all(not item["active"] for item in fields["streamServicesCapability"]["streamServices"])
    assert fields["music_services"] == [] and fields["supported_services"] == ""
    assert fields["streams"] == "0"


@pytest.mark.parametrize("value", ["0200", "000200"])
def test_legacy_decimal_mask_preserves_tunein_bits(value):
    from fake_linkplay.capabilities import capability_fields
    fields = capability_fields(Config(source_experiment_variant="tunein",
        tunein_streams_value=value, tunein_stream_all_value=value))
    assert fields["streams"] == fields["streams_all"] == "200"


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["new_source", "music_service", "all"])
@pytest.mark.parametrize("service_id,source", [("Plex", ""), ("PLEX", ""), ("plex", ""), ("local", "Plex")])
async def test_plex_source_aliases_are_not_advertised(tmp_path, variant, service_id, source):
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path),
        source_experiment_variant=variant, enable_plex_service=True, experimental_sources=[
            ExperimentalSource(service_id, "Synthetic provider", source=source, url="")])
    state = RendererState.from_config(cfg)
    api = HttpApi(cfg, state, StatefulBackend())
    status = await api.execute(ParsedCommand("getStatusEx"), "192.0.2.1")
    control = json.loads(control_device_info_json(cfg, "192.0.2.1", state))
    for fields in [status, control]:
        for key in ["services", "service_list", "source_list", "sourceList", "sources"]:
            assert fields.get(key, []) == []
        assert fields.get("supported_sources", "") == ""
        assert fields["music_services"] == []
