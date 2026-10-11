from fake_linkplay.config import Config
from fake_linkplay.ssdp import SERVER, SsdpServer, STS, response_packet


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
        discovery_uuid="FF0000000000000040008000000000000001",
    )
    packet = response_packet(cfg, "ssdp:wiimudevice").decode()
    assert "USN: uuid:FF0000000000000040008000000000000001::ssdp:wiimudevice" in packet


def test_wiim_home_profile_media_renderer_response_has_linkplay_headers() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        http_api_port=59152,
        upnp_port=49152,
        wiim_home_profile=True,
        discovery_uuid="FF0000000000000040008000000000000001",
        mdns_instance_name="LinkPlayA31_000001",
    )
    packet = response_packet(cfg, "urn:schemas-upnp-org:device:MediaRenderer:1").decode()
    assert "LOCATION: http://127.0.0.1:59152/description.xml" in packet
    assert f"SERVER: {SERVER}" in packet
    assert "Bootid.upnp.org: 000000000000000000000000000000000000" in packet
    assert "ServiceName: ##hexStr##" + "LinkPlayA31_000001".encode().hex().upper() in packet
    assert "DEVICE-SOURCE: SSDP" in packet


def test_wiimu_search_target_also_returns_media_renderer_target() -> None:
    server = SsdpServer(Config())
    assert server._targets_for_search("ssdp:wiimudevice") == (
        "ssdp:wiimudevice",
        "urn:schemas-upnp-org:device:MediaRenderer:1",
    )


def test_rootdevice_usn_has_required_suffix_for_responses_and_notifies() -> None:
    from fake_linkplay.ssdp import notify_packet

    cfg = Config(uuid="00000000-0000-4000-8000-000000000001", advertise_ip="192.0.2.10")
    expected = "USN: uuid:00000000-0000-4000-8000-000000000001::upnp:rootdevice\r\n"
    assert expected in response_packet(cfg, "upnp:rootdevice").decode()
    assert expected in notify_packet(cfg, "upnp:rootdevice").decode()


def test_own_uuid_search_is_case_insensitive_and_has_no_duplicate_usn_suffix() -> None:
    from fake_linkplay.ssdp import notify_packet

    cfg = Config(discovery_uuid="uuid:ff0000000000000040008000000000000001", wiim_home_profile=True, advertise_ip="192.0.2.10")
    server = SsdpServer(cfg)
    own_uuid = f"uuid:{cfg.upnp_uuid}"
    assert server._targets_for_search(f" {own_uuid.swapcase()} ") == (own_uuid,)
    for packet in (response_packet(cfg, own_uuid), notify_packet(cfg, own_uuid)):
        assert f"USN: {own_uuid}\r\n".encode() in packet
        assert f"USN: {own_uuid}::".encode() not in packet
    assert server._targets_for_search("uuid:00000000-0000-4000-8000-000000000002") == ()


def test_all_search_includes_own_uuid_and_all_description_services() -> None:
    from xml.etree import ElementTree
    from fake_linkplay.upnp_description import description_xml

    cfg = Config(uuid="00000000-0000-4000-8000-000000000001", advertise_ip="192.0.2.10")
    server = SsdpServer(cfg)
    targets = server._targets_for_search("ssdp:all")
    assert f"uuid:{cfg.upnp_uuid}" in targets
    xml = ElementTree.fromstring(description_xml(cfg))
    for element in xml.findall(".//{urn:schemas-upnp-org:device-1-0}serviceType"):
        assert element.text in targets
        assert server._targets_for_search(element.text) == (element.text,)
    assert "urn:schemas-upnp-org:service:ConnectionManager:1" in targets
    assert len(targets) == len(set(targets))


def test_missing_or_unsupported_search_target_is_ignored() -> None:
    server = SsdpServer(Config(uuid="00000000-0000-4000-8000-000000000001"))
    for target in ("", " ", "urn:example:unsupported", "ssdp:all:extra"):
        assert server._targets_for_search(target) == ()


def test_compatibility_service_name_is_marked_utf8_hex() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000001",
        advertise_ip="192.0.2.10",
        wiim_home_profile=True,
        mdns_instance_name="Küche 🔊",
    )
    packet = response_packet(cfg, "urn:schemas-upnp-org:device:MediaRenderer:1").decode("ascii")
    name_header = next(line for line in packet.splitlines() if line.startswith("ServiceName:"))
    assert name_header == "ServiceName: ##hexStr##4BC3BC63686520F09F948A"
    assert bytes.fromhex(name_header.split("##hexStr##", 1)[1]).decode("utf-8") == cfg.linkplay_instance_name
    assert f"SERVER: {SERVER}" in packet
    assert "Linux/2.6.21" not in packet


def test_neutral_response_has_independent_server_identity() -> None:
    cfg = Config(uuid="00000000-0000-4000-8000-000000000001", advertise_ip="192.0.2.10")
    packet = response_packet(cfg, "urn:schemas-upnp-org:device:MediaRenderer:1").decode()
    assert "OpenRoomRenderer/0.1" in packet
    assert "ServiceName:" not in packet
    assert "WiiM" not in packet
    assert "Linkplay/4.8" not in packet
