from fake_linkplay.config import Config
from fake_linkplay.ssdp import SsdpServer, STS, response_packet


def test_ssdp_supports_wiimu_search_target() -> None:
    assert "ssdp:wiimudevice" in STS


def test_wiimu_response_points_to_description() -> None:
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", advertise_ip="127.0.0.1")
    packet = response_packet(cfg, "ssdp:wiimudevice").decode()
    assert "ST: ssdp:wiimudevice" in packet
    assert "USN: uuid:00000000-0000-4000-8000-000000000000::ssdp:wiimudevice" in packet
    assert "LOCATION: http://127.0.0.1:80/description.xml" in packet


def test_wiim_home_profile_ssdp_uses_discovery_uuid() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        wiim_home_profile=True,
        discovery_uuid="FF310001BD4CE65900AF9543000000000000",
    )
    packet = response_packet(cfg, "ssdp:wiimudevice").decode()
    assert "USN: uuid:FF310001BD4CE65900AF9543000000000000::ssdp:wiimudevice" in packet


def test_wiim_home_profile_media_renderer_response_has_linkplay_headers() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        http_api_port=59152,
        upnp_port=49152,
        wiim_home_profile=True,
        discovery_uuid="FF310001BD4CE65900AF9543000000000000",
        mdns_instance_name="LinkPlayA31_542C",
    )
    packet = response_packet(cfg, "urn:schemas-upnp-org:device:MediaRenderer:1").decode()
    assert "LOCATION: http://127.0.0.1:59152/description.xml" in packet
    assert "SERVER: Linux/2.6.21 http" in packet
    assert "Bootid.upnp.org: 000000000000000000000000000000000000" in packet
    assert "ServiceName: LinkPlayA31_542C" in packet
    assert "DEVICE-SOURCE: SSDP" in packet


def test_wiimu_search_target_also_returns_media_renderer_target() -> None:
    server = SsdpServer(Config())
    assert server._targets_for_search("ssdp:wiimudevice") == (
        "ssdp:wiimudevice",
        "urn:schemas-upnp-org:device:MediaRenderer:1",
    )
