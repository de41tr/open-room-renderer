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
        discovery_uuid="FF0000000000000040008000000000000001",
        bootid="00000000-0000-0000-0000-000000000001",
        mdns_instance_name="LinkPlayA31_000001",
    )
    services = list(MdnsAdvertiser(cfg)._services())
    linkplay = services[0]
    assert linkplay[0] == "_linkplay._tcp.local."
    assert linkplay[1] == 59152
    assert linkplay[2] == "LinkPlayA31_000001"
    assert linkplay[3] == {
        "uuid": "uuid:FF0000000000000040008000000000000001",
        "bootid": "00000000-0000-0000-0000-000000000001",
        "security": "http",
    }


def test_wiim_home_profile_mdns_uses_simple_ipv4_srv_target() -> None:
    cfg = Config(
        advertise_ip="127.0.0.1",
        wiim_home_profile=True,
        discovery_uuid="FF0000000000000040008000000000000001",
    )
    assert MdnsAdvertiser(cfg)._server_name() == cfg.mdns_hostname


def test_custom_mdns_ptr_response_contains_srv_txt_and_a_records() -> None:
    cfg = Config(
        advertise_ip="127.0.0.1",
        http_api_port=59152,
        wiim_home_profile=True,
        discovery_uuid="FF0000000000000040008000000000000001",
        bootid="00000000-0000-0000-0000-000000000001",
        mdns_instance_name="LinkPlayA31_000001",
    )
    protocol = CustomMdnsProtocol(cfg)
    response = protocol._response_for(0, DnsQuestion("_linkplay._tcp.local.", DNS_TYPE_PTR, DNS_CLASS_IN))
    assert response is not None
    assert response[:12] == b"\x00\x00\x84\x00\x00\x00\x00\x01\x00\x00\x00\x03"
    assert encode_name("LinkPlayA31_000001._linkplay._tcp.local.") in response
    assert encode_name(cfg.mdns_hostname) in response
    assert b"uuid=uuid:FF0000000000000040008000000000000001" in response
    assert b"bootid=00000000-0000-0000-0000-000000000001" in response
    assert b"security=http" in response
    assert b"\x7f\x00\x00\x01" in response


def test_custom_mdns_matches_lowercase_followup_queries() -> None:
    cfg = Config(
        advertise_ip="127.0.0.1",
        http_api_port=59152,
        wiim_home_profile=True,
        discovery_uuid="FF0000000000000040008000000000000001",
        mdns_instance_name="LinkPlayA31_000001",
    )
    protocol = CustomMdnsProtocol(cfg)
    srv = protocol._response_for(0, DnsQuestion("linkplaya31_000001._linkplay._tcp.local.", DNS_TYPE_SRV, DNS_CLASS_IN))
    address = protocol._response_for(0, DnsQuestion(cfg.mdns_hostname, DNS_TYPE_A, DNS_CLASS_IN))
    assert srv is not None
    assert address is not None
    assert encode_name(cfg.mdns_hostname) in srv
    assert b"\x7f\x00\x00\x01" in address


def test_custom_responder_host_is_stable_and_unique_across_installations() -> None:
    first = Config(uuid="00000000-0000-4000-8000-000000000001", wiim_home_profile=True)
    second = Config(uuid="00000000-0000-4000-8000-000000000002", wiim_home_profile=True)
    a = CustomMdnsProtocol(first)
    b = CustomMdnsProtocol(second)
    assert a.target != b.target
    assert a.target == CustomMdnsProtocol(first).target
    assert a.target == MdnsAdvertiser(first)._server_name()
    assert len(a.target.split(".")[0].encode()) <= 63
    assert "fake-linkplay.local." not in (a.target, b.target)


def test_custom_responder_encodes_unicode_and_literal_dots_in_one_label() -> None:
    from fake_linkplay.mdns import decode_name, parse_questions
    import struct

    label = "Küche. Ost \\ 🔊"
    cfg = Config(uuid="00000000-0000-4000-8000-000000000001", wiim_home_profile=True, mdns_instance_name=label, advertise_ip="192.0.2.10")
    protocol = CustomMdnsProtocol(cfg)
    encoded = encode_name(protocol.instance)
    assert encoded[0] == len(label.encode("utf-8"))
    assert encoded[1 : 1 + encoded[0]] == label.encode("utf-8")
    decoded, offset = decode_name(encoded, 0)
    assert offset == len(encoded)
    assert decoded == protocol.instance_key
    packet = struct.pack("!HHHHHH", 9, 0, 1, 0, 0, 0) + encoded + struct.pack("!HH", DNS_TYPE_SRV, DNS_CLASS_IN)
    packet_id, questions = parse_questions(packet)
    assert packet_id == 9
    response = protocol._response_for(packet_id, questions[0])
    assert response is not None
    assert encoded in response


def test_utf8_label_lengths_use_bytes_and_never_cut_a_character() -> None:
    from fake_linkplay.mdns import instance_label

    assert instance_label("é" * 31) == "é" * 31
    for name in ("é" * 32, "🔊" * 40, "界" * 80, "a" * 80):
        shortened = instance_label(name)
        assert len(shortened.encode("utf-8")) <= 63
        assert "�" not in shortened
        assert shortened == instance_label(name)
        assert shortened != instance_label(name + "x")
        assert encode_name(f"{shortened}._linkplay._tcp.local.")[0] == len(shortened.encode("utf-8"))
    assert instance_label("Ku\u0308che") == "Küche"


def test_neutral_profile_has_bounded_neutral_instance_names() -> None:
    cfg = Config(wiim_home_profile=False, uuid="00000000-0000-4000-8000-000000000001", friendly_name="Küche. 🔊" * 40)
    services = list(MdnsAdvertiser(cfg)._services())
    assert [service[0] for service in services] == ["_linkplay._tcp.local.", "_http._tcp.local."]
    for _service, _port, name, _props in services:
        assert len(name.encode("utf-8")) <= 63
        assert "." not in name
        assert "WiiM" not in name


def test_custom_responder_refreshes_renamed_instance_without_changing_identity() -> None:
    cfg = Config(uuid="00000000-0000-4000-8000-000000000001", wiim_home_profile=True, advertise_ip="192.0.2.10")
    protocol = CustomMdnsProtocol(cfg)
    old_instance = protocol.instance
    old_host, old_props = protocol.target, protocol.props.copy()
    cfg.friendly_name = "Büro 🔊"
    cfg.mdns_instance_name = cfg.friendly_name
    response = protocol._response_for(0, DnsQuestion(protocol.service, DNS_TYPE_PTR, DNS_CLASS_IN))
    assert response is not None
    assert "Büro 🔊".encode() in response
    assert encode_name(old_instance) not in response
    assert protocol.target == old_host
    assert protocol.props == old_props
    assert protocol._response_for(0, DnsQuestion(old_instance.lower(), DNS_TYPE_SRV, DNS_CLASS_IN)) is None
    assert protocol._response_for(0, DnsQuestion(protocol.instance_key, DNS_TYPE_SRV, DNS_CLASS_IN)) is not None


def test_invalid_dns_name_encodings_fail_cleanly() -> None:
    from fake_linkplay.mdns import decode_name
    import pytest

    for name in ("a" * 64 + ".local.", "a..local.", "a\\", ".".join(["x" * 63] * 4)):
        with pytest.raises(ValueError):
            encode_name(name)
    for encoded in (b"\x40" + b"a" * 64 + b"\0", b"\x05abc", b"\xc0\x00", b"\x01\xff\0"):
        with pytest.raises(ValueError):
            decode_name(encoded, 0)


def test_ascii_case_folding_does_not_change_non_ascii_dns_labels() -> None:
    from fake_linkplay.mdns import decode_name

    assert decode_name(encode_name("KÜCHE.local."), 0)[0] == "kÜche.local."


def test_custom_responder_answers_mocked_multicast_and_unicast_queries() -> None:
    import struct
    from fake_linkplay.mdns import DNS_QU_MASK, MDNS_GROUP, MDNS_PORT

    class FakeTransport:
        def __init__(self):
            self.sent = []

        def sendto(self, data, address):
            self.sent.append((data, address))

    cfg = Config(uuid="00000000-0000-4000-8000-000000000001", wiim_home_profile=True, advertise_ip="192.0.2.10")
    protocol = CustomMdnsProtocol(cfg)
    transport = FakeTransport()
    protocol.connection_made(transport)
    sender = ("192.0.2.20", MDNS_PORT)
    for qclass, destination in ((DNS_CLASS_IN, (MDNS_GROUP, MDNS_PORT)), (DNS_CLASS_IN | DNS_QU_MASK, sender)):
        query = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + encode_name(protocol.service) + struct.pack("!HH", DNS_TYPE_PTR, qclass)
        protocol.datagram_received(query, sender)
        response, address = transport.sent[-1]
        assert address == destination
        assert encode_name(protocol.instance) in response
        assert encode_name(cfg.mdns_hostname) in response


async def test_zeroconf_refresh_unregisters_cached_names_and_registers_current_names(monkeypatch) -> None:
    from unittest.mock import AsyncMock
    import zeroconf
    import zeroconf.asyncio

    fake = AsyncMock()
    monkeypatch.setattr(zeroconf, "Zeroconf", lambda: object())
    monkeypatch.setattr(zeroconf.asyncio, "AsyncZeroconf", lambda **_kwargs: fake)
    cfg = Config(wiim_home_profile=False, uuid="00000000-0000-4000-8000-000000000001", enable_mdns=True, advertise_ip="192.0.2.10")
    advertiser = MdnsAdvertiser(cfg)
    await advertiser.start()
    old_infos = list(advertiser._infos)
    assert len(old_infos) == 2
    old_host = old_infos[0].server
    cfg.friendly_name = "Küche 🔊"
    cfg.mdns_instance_name = cfg.friendly_name
    await advertiser.refresh()
    assert fake.async_unregister_service.await_count == 2
    assert [call.args[0] for call in fake.async_unregister_service.await_args_list] == old_infos
    assert fake.async_register_service.await_count == 4
    assert len(advertiser._infos) == 2
    for info in advertiser._infos:
        assert info.name.startswith("Küche 🔊.")
        assert info.server == old_host
    await advertiser.stop()
    assert not advertiser._infos
    assert advertiser._zeroconf is None
    fake.async_close.assert_awaited_once()
    await advertiser.refresh()  # A pending rename after shutdown must not restart.
    assert fake.async_register_service.await_count == 4


async def test_disabled_advertiser_refresh_is_a_noop() -> None:
    advertiser = MdnsAdvertiser(Config(enable_mdns=False))
    await advertiser.start()
    await advertiser.refresh()
    assert advertiser._zeroconf is None


def test_custom_refresh_withdraws_old_name_and_announces_new_name() -> None:
    from unittest.mock import Mock
    from fake_linkplay.mdns import MDNS_GROUP, MDNS_PORT, decode_name
    import struct

    cfg = Config(uuid="00000000-0000-4000-8000-000000000001", wiim_home_profile=True, advertise_ip="192.0.2.10")
    protocol = CustomMdnsProtocol(cfg)
    transport = Mock()
    protocol.connection_made(transport)
    old_instance = protocol.instance
    cfg.mdns_instance_name = "Küche 🔊"
    protocol.refresh()
    assert transport.sendto.call_count == 2
    goodbye, address = transport.sendto.call_args_list[0].args
    assert address == (MDNS_GROUP, MDNS_PORT)
    assert encode_name(old_instance) in goodbye
    offset = 12
    for _ in range(3):
        _name, offset = decode_name(goodbye, offset)
        _rtype, _rclass, ttl, data_length = struct.unpack("!HHIH", goodbye[offset : offset + 10])
        assert ttl == 0
        offset += 10 + data_length
    announcement, address = transport.sendto.call_args_list[1].args
    assert address == (MDNS_GROUP, MDNS_PORT)
    assert encode_name(protocol.instance) in announcement
    assert encode_name(old_instance) not in announcement
    protocol.refresh()
    assert transport.sendto.call_count == 2


async def test_custom_name_logging_omits_private_instance_on_start_and_query(monkeypatch, caplog) -> None:
    """Synthetic socket only: protocol data keeps names; diagnostic logs do not."""
    import asyncio
    import struct
    from unittest.mock import AsyncMock, Mock
    from fake_linkplay.mdns import CustomMdnsResponder, MDNS_PORT

    cfg = Config(uuid="00000000-0000-4000-8000-000000000001", wiim_home_profile=True,
                 advertise_ip="192.0.2.10", mdns_instance_name="https://private.invalid/?token=DO_NOT_LOG")
    transport = Mock()
    protocol = CustomMdnsProtocol(cfg)
    protocol.connection_made(transport)
    caplog.set_level("DEBUG", logger="fake_linkplay.mdns")
    monkeypatch.setattr("fake_linkplay.mdns.socket.socket", Mock(return_value=Mock()))
    monkeypatch.setattr(asyncio.get_running_loop(), "create_datagram_endpoint",
                        AsyncMock(return_value=(transport, protocol)))
    responder = CustomMdnsResponder(cfg)
    await responder.start()
    query = (struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + encode_name(protocol.instance)
             + struct.pack("!HH", DNS_TYPE_SRV, DNS_CLASS_IN))
    protocol.datagram_received(query, ("192.0.2.20", MDNS_PORT))
    assert encode_name(protocol.instance) in transport.sendto.call_args.args[0]
    await responder.stop()
    assert "custom mDNS responder started" in caplog.text
    assert "mDNS response qtype=" in caplog.text
    assert "do_not_log" not in caplog.text.lower()
