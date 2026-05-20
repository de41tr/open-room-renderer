from fake_linkplay.config import Config
from fake_linkplay.mdns import (
    DNS_CLASS_IN,
    DNS_TYPE_A,
    DNS_TYPE_PTR,
    DNS_TYPE_SRV,
    DnsQuestion,
    CustomMdnsProtocol,
    MdnsAdvertiser,
    encode_name,
)


def test_wiim_home_profile_mdns_uuid_uses_uuid_prefix() -> None:
    cfg = Config(
        advertise_ip="127.0.0.1",
        http_api_port=59152,
        wiim_home_profile=True,
        discovery_uuid="FF310001BD4CE65900AF9543000000000000",
        bootid="00000000-0000-0000-0000-000000000001",
        mdns_instance_name="LinkPlayA31_542C",
    )
    services = list(MdnsAdvertiser(cfg)._services())
    linkplay = services[0]
    assert linkplay[0] == "_linkplay._tcp.local."
    assert linkplay[1] == 59152
    assert linkplay[2] == "LinkPlayA31_542C"
    assert linkplay[3] == {
        "uuid": "uuid:FF310001BD4CE65900AF9543000000000000",
        "bootid": "00000000-0000-0000-0000-000000000001",
        "security": "http",
    }


def test_wiim_home_profile_mdns_uses_simple_ipv4_srv_target() -> None:
    cfg = Config(
        advertise_ip="127.0.0.1",
        wiim_home_profile=True,
        discovery_uuid="FF310001BD4CE65900AF9543000000000000",
    )
    assert MdnsAdvertiser(cfg)._server_name() == "fake-linkplay.local."


def test_custom_mdns_ptr_response_contains_srv_txt_and_a_records() -> None:
    cfg = Config(
        advertise_ip="127.0.0.1",
        http_api_port=59152,
        wiim_home_profile=True,
        discovery_uuid="FF310001BD4CE65900AF9543000000000000",
        bootid="00000000-0000-0000-0000-000000000001",
        mdns_instance_name="LinkPlayA31_542C",
    )
    protocol = CustomMdnsProtocol(cfg)
    response = protocol._response_for(0, DnsQuestion("_linkplay._tcp.local.", DNS_TYPE_PTR, DNS_CLASS_IN))
    assert response is not None
    assert response[:12] == b"\x00\x00\x84\x00\x00\x00\x00\x01\x00\x00\x00\x03"
    assert encode_name("LinkPlayA31_542C._linkplay._tcp.local.") in response
    assert encode_name("fake-linkplay.local.") in response
    assert b"uuid=uuid:FF310001BD4CE65900AF9543000000000000" in response
    assert b"bootid=00000000-0000-0000-0000-000000000001" in response
    assert b"security=http" in response
    assert b"\x7f\x00\x00\x01" in response


def test_custom_mdns_matches_lowercase_followup_queries() -> None:
    cfg = Config(
        advertise_ip="127.0.0.1",
        http_api_port=59152,
        wiim_home_profile=True,
        discovery_uuid="FF310001BD4CE65900AF9543000000000000",
        mdns_instance_name="LinkPlayA31_542C",
    )
    protocol = CustomMdnsProtocol(cfg)
    srv = protocol._response_for(0, DnsQuestion("linkplaya31_542c._linkplay._tcp.local.", DNS_TYPE_SRV, DNS_CLASS_IN))
    address = protocol._response_for(0, DnsQuestion("fake-linkplay.local.", DNS_TYPE_A, DNS_CLASS_IN))
    assert srv is not None
    assert address is not None
    assert encode_name("fake-linkplay.local.") in srv
    assert b"\x7f\x00\x00\x01" in address
