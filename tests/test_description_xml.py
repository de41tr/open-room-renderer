from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.mpv_backend import MpvBackend
from fake_linkplay.state import RendererState
from fake_linkplay.upnp_description import (
    avtransport_response_fields,
    description_xml,
    get_control_device_info_response_fields,
    playqueue_response_fields,
    scpd_xml,
    soap_response,
    stream_services_capability,
)


def test_description_contains_media_renderer_and_playqueue() -> None:
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", advertise_ip="127.0.0.1")
    xml = description_xml(cfg)
    assert "urn:schemas-upnp-org:device:MediaRenderer:1" in xml
    assert "urn:schemas-wiimu-com:service:PlayQueue:1" in xml
    assert "/upnp/control/PlayQueue" in xml
    assert "uuid:00000000-0000-4000-8000-000000000000" in xml


def test_wiim_home_profile_description_contains_linkplay_services() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        wiim_home_profile=True,
        discovery_uuid="FF310001BD4CE65900AF9543000000000000",
        friendly_name="LinkPlayA31_542C",
        manufacturer="Linkplay Technology Inc.",
        model_name="MUZO Cobblestone",
    )
    xml = description_xml(cfg)
    assert "<friendlyName>LinkPlayA31_" in xml
    assert "Linkplay Technology Inc." in xml
    assert "urn:schemas-upnp-org:service:ConnectionManager:1" in xml
    assert "/upnp/control/rendertransport1" in xml
    assert "/upnp/control/renderconnmgr1" in xml
    assert "/upnp/rendercontrolSCPD.xml" in xml
    assert "/upnp/PlayQueueSCPD.xml" in xml
    assert "uuid:FF310001BD4CE65900AF9543000000000000" in xml


def test_get_control_device_info_soap_contains_expected_outputs() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        wiim_home_profile=True,
        discovery_uuid="FF310001BD4CE65900AF9543000000000000",
        mdns_instance_name="LinkPlayA31_542C",
        model_name="WiiM Mini",
    )
    result = get_control_device_info_response_fields(cfg, "127.0.0.1")
    xml = soap_response("GetControlDeviceInfo", "rendercontrol1", result)
    assert "<u:GetControlDeviceInfoResponse" in xml
    for key in (
        "MultiType",
        "PlayMode",
        "Router",
        "Ssid",
        "SlaveMask",
        "CurrentVolume",
        "CurrentMute",
        "CurrentChannel",
        "SlaveList",
        "Status",
    ):
        assert f"<{key}>" in xml
    assert "<Router>uuid:FF310001BD4CE65900AF9543000000000000</Router>" in xml
    assert "&quot;upnp_uuid&quot;:&quot;uuid:FF310001BD4CE65900AF9543000000000000&quot;" in xml
    assert "&quot;DeviceName&quot;:&quot;LinkPlayA31_542C&quot;" in xml
    assert "&quot;EQ_support&quot;:&quot;1&quot;" in xml
    assert "&quot;EQVersion&quot;:&quot;4.3&quot;" in xml
    assert "&quot;service_version&quot;:&quot;1.4&quot;" in xml


def test_get_control_device_info_exposes_stream_capability_as_top_level_field() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        wiim_home_profile=True,
        discovery_uuid="FF310001BD4CE65900AF9543000000000000",
        source_experiment_variant="tunein",
        tunein_minimal_capability=True,
        enable_plex_service=True,
        plex_capability_mode="exact",
        enable_stream_service_sentinels=True,
    )
    result = get_control_device_info_response_fields(cfg, "127.0.0.1")
    xml = soap_response("GetControlDeviceInfo", "rendercontrol1", result)
    assert "<StreamCapability>" in xml
    assert "&quot;id&quot;:&quot;Plex&quot;" in xml
    assert "&quot;id&quot;:&quot;Deezer2&quot;" in xml
    assert "&quot;id&quot;:&quot;SoundCloud&quot;" in xml


def test_rendering_control_scpd_declares_get_control_device_info_outputs() -> None:
    xml = scpd_xml("RenderingControl")
    assert "<name>GetControlDeviceInfo</name>" in xml
    assert "<name>StreamServicesCapability</name>" in xml
    for key in ("MultiType", "Router", "Ssid", "CurrentVolume", "CurrentMute", "SlaveList", "Status"):
        assert f"<name>{key}</name>" in xml
    assert "<name>StreamCapability</name>" in xml


def test_stream_services_capability_soap_response_exposes_top_level_output() -> None:
    capability = stream_services_capability(
        Config(uuid="00000000-0000-4000-8000-000000000000", advertise_ip="127.0.0.1"),
        [{"id": "Plex", "version": "1.4", "active": True}],
        "1.4",
    )
    body = f"<StreamCapability>{capability}</StreamCapability>"
    xml = soap_response("StreamServicesCapability", "rendercontrol1", body)
    assert "<u:StreamServicesCapabilityResponse" in xml
    assert "<StreamCapability>" in xml


def test_avtransport_seen_by_wiim_home_has_structured_responses() -> None:
    media = soap_response("GetMediaInfo", "rendertransport1", avtransport_response_fields("GetMediaInfo"))
    info = soap_response("GetInfoEx", "rendertransport1", avtransport_response_fields("GetInfoEx"))
    assert "<NrTracks>0</NrTracks>" in media
    assert "<CurrentURI>" in media
    assert "<PlayMedium>NONE</PlayMedium>" in media
    assert "<CurrentTransportState>STOPPED</CurrentTransportState>" in info
    assert "<CurrentTransportStatus>OK</CurrentTransportStatus>" in info
    assert "<InternetAccess>1</InternetAccess>" in info
    assert "<CurrentChannel>Master</CurrentChannel>" in info
    assert "<SlaveList>[]</SlaveList>" in info
    assert "<PlayMedium>NETWORK</PlayMedium>" in info
    assert "<TrackSource>URL</TrackSource>" in info
    assert "<TimeStamp>0</TimeStamp>" in info
    assert "<SubNum>2</SubNum>" in info
    assert "<SpotifyActive>0</SpotifyActive>" in info


def test_playqueue_seen_by_wiim_home_has_structured_responses() -> None:
    mapping = soap_response("GetKeyMapping", "PlayQueue1", playqueue_response_fields("GetKeyMapping"))
    user = soap_response("GetUserInfo", "PlayQueue1", playqueue_response_fields("GetUserInfo"))
    browse = soap_response("BrowseQueue", "PlayQueue1", playqueue_response_fields("BrowseQueue"))
    assert "<QueueContext>{}</QueueContext>" in mapping
    assert "&quot;id&quot;:&quot;&quot;" in user
    assert "&quot;userId&quot;:&quot;&quot;" in user
    assert "&quot;anonUserId&quot;:&quot;&quot;" in user
    assert "&quot;version&quot;:&quot;1.0&quot;" in user
    assert "<QueueContext></QueueContext>" in browse
    assert "<NumberReturned>" not in browse
    assert "<TotalMatches>" not in browse


def test_tunein_get_user_info_can_report_available() -> None:
    class DummyMpv(MpvBackend):
        def __init__(self) -> None:
            super().__init__("/tmp/nonexistent", False)

    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", advertise_ip="127.0.0.1")
    api = HttpApi(cfg, RendererState.from_config(cfg), DummyMpv())
    body = "<GetUserInfo><AccountSource>newTuneIn</AccountSource></GetUserInfo>"
    response = soap_response("GetUserInfo", "PlayQueue1", api._playqueue_response_fields("GetUserInfo", body))
    assert "&quot;accountSource&quot;:&quot;newTuneIn&quot;" in response
    assert "&quot;sourceVersion&quot;:&quot;1.1&quot;" in response
    assert "&quot;showName&quot;:&quot;TuneIn&quot;" in response
    assert "&quot;userId&quot;:&quot;anonymous&quot;" in response


def test_non_tunein_get_user_info_reports_neutral_logged_out_shape(tmp_path) -> None:
    class DummyMpv(MpvBackend):
        def __init__(self) -> None:
            super().__init__("/tmp/nonexistent", False)

    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", advertise_ip="127.0.0.1", state_dir=str(tmp_path))
    api = HttpApi(cfg, RendererState.from_config(cfg), DummyMpv())
    body = "<GetUserInfo><AccountSource>Plex</AccountSource></GetUserInfo>"
    response = soap_response("GetUserInfo", "PlayQueue1", api._playqueue_response_fields("GetUserInfo", body))
    assert "&quot;source&quot;:&quot;Plex&quot;" in response
    assert "&quot;msg&quot;:&quot;NOT_LOGIN&quot;" in response
    assert "&quot;userId&quot;:&quot;&quot;" in response
    assert "&quot;userName&quot;:&quot;&quot;" in response
    assert "&quot;showName&quot;:&quot;&quot;" in response
    assert "&quot;subscription&quot;:&quot;0&quot;" in response
    assert "&quot;sourceVersion&quot;:&quot;1.4&quot;" in response
    assert "&quot;capabilityVersion&quot;:&quot;1.4&quot;" in response
    assert "&quot;replayGain&quot;:true" in response
    assert "com.linkplay.wiimhome%3A%2F%2Fcallback%3Fcategory%3Doauth%26service%3Dplex" in response


def test_plex_get_user_info_debug_login_shape(tmp_path) -> None:
    class DummyMpv(MpvBackend):
        def __init__(self) -> None:
            super().__init__("/tmp/nonexistent", False)

    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        plex_user_info_mode="debug_login",
        state_dir=str(tmp_path),
    )
    api = HttpApi(cfg, RendererState.from_config(cfg), DummyMpv())
    body = "<GetUserInfo><AccountSource>Plex</AccountSource></GetUserInfo>"
    response = soap_response("GetUserInfo", "PlayQueue1", api._playqueue_response_fields("GetUserInfo", body))
    assert "&quot;source&quot;:&quot;Plex&quot;" in response
    assert "&quot;msg&quot;:&quot;success&quot;" in response
    assert "&quot;userId&quot;:&quot;debug-user&quot;" in response
    assert "&quot;showName&quot;:&quot;Debug Plex&quot;" in response
    assert "com.linkplay.wiimhome%3A%2F%2Fcallback%3Fcategory%3Doauth%26service%3Dplex" in response


def test_stream_service_sentinel_get_user_info_reports_named_not_login_shape() -> None:
    class DummyMpv(MpvBackend):
        def __init__(self) -> None:
            super().__init__("/tmp/nonexistent", False)

    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        enable_stream_service_sentinels=True,
    )
    api = HttpApi(cfg, RendererState.from_config(cfg), DummyMpv())
    body = "<GetUserInfo><AccountSource>Deezer2</AccountSource></GetUserInfo>"
    response = soap_response("GetUserInfo", "PlayQueue1", api._playqueue_response_fields("GetUserInfo", body))
    assert "&quot;source&quot;:&quot;Deezer2&quot;" in response
    assert "&quot;msg&quot;:&quot;not login&quot;" in response
    assert "&quot;showName&quot;:&quot;Deezer2&quot;" in response
    assert "&quot;sourceVersion&quot;:&quot;1.4&quot;" in response
    assert "&quot;capabilityVersion&quot;:&quot;1.4&quot;" in response


def test_tunein_avtransport_info_exposes_stream_capability() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        source_experiment_variant="tunein",
    )
    state = RendererState.from_config(cfg)
    response = soap_response("GetInfoEx", "rendertransport1", avtransport_response_fields("GetInfoEx", state))
    assert "<StreamCapability>" in response
    assert "newTuneIn" in response


def test_plex_avtransport_info_exposes_same_stream_capability_path() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        source_experiment_variant="tunein",
        tunein_minimal_capability=True,
        enable_plex_service=True,
    )
    state = RendererState.from_config(cfg)
    response = soap_response("GetInfoEx", "rendertransport1", avtransport_response_fields("GetInfoEx", state))
    assert "<StreamCapability>" in response
    assert "newTuneIn" in response
    assert "Plex" in response
    assert "&quot;version&quot;:&quot;1.4&quot;" in response
