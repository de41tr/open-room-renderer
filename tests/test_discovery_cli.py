"""UNIT/MOCK SSDP client bounds, hostile packets and private CLI diagnostics.

All socket operations here are fake. Actual loopback multicast subprocess
acceptance is a separate, explicit native lane; no physical network is probed.
"""
from __future__ import annotations

import json
import socket

import pytest

from fake_linkplay import cli, discovery_client as client
from fake_linkplay.config import Config
from fake_linkplay.ssdp import response_packet

ADDRESS = "192.0.2.10"
IDENTITY = "uuid:00112233-4455-6677-8899-aabbccddeeff"
LOCATION = f"http://{ADDRESS}:8080/description.xml"
PRIVATE = "https://user:synthetic-private@example.test/?token=private-value"


def packet(*, identity=IDENTITY, location=LOCATION, server="Linux/6.x UPnP/1.0 OpenRoomRenderer/0.1",
           target=client.MEDIA_RENDERER, optional=b""):
    return (f"HTTP/1.1 200 OK\r\nSERVER: {server}\r\nST: {target}\r\n"
            f"USN: {identity}::{target}\r\nLOCATION: {location}\r\n").encode() + optional + b"\r\n"


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now


class FakeSocket:
    def __init__(self, clock, packets=(), *, recv_delay=0.0, failure=None):
        self.clock = clock
        self.packets = iter(packets)
        self.recv_delay = recv_delay
        self.failure = failure
        self.bound = None
        self.options = []
        self.sent = []
        self.timeouts = []
        self.read_sizes = []
        self.closed = 0

    def fail_at(self, stage):
        if self.failure is not None and self.failure[0] == stage:
            raise self.failure[1]

    def bind(self, address):
        self.fail_at("bind")
        self.bound = address

    def setsockopt(self, level, option, value):
        self.fail_at("setsockopt")
        self.options.append((level, option, value))

    def settimeout(self, timeout):
        self.fail_at("settimeout")
        assert 0 < timeout <= client.MAX_TIMEOUT
        self.timeout = timeout
        self.timeouts.append(timeout)

    def sendto(self, data, address):
        self.fail_at("sendto")
        self.sent.append((self.clock.now, data, address))
        return len(data)

    def recvfrom(self, size):
        self.fail_at("recvfrom")
        self.read_sizes.append(size)
        try:
            event = next(self.packets)
        except StopIteration:
            self.clock.now += self.timeout
            raise socket.timeout
        self.clock.now += self.recv_delay
        if isinstance(event, BaseException):
            raise event
        data, peer = event
        return data[:size], peer

    def close(self):
        self.closed += 1


def install_socket(monkeypatch, packets=(), **kwargs):
    clock = Clock()
    sock = FakeSocket(clock, packets, **kwargs)
    created = []
    def create(*args):
        created.append(args)
        assert args == (socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        return sock
    monkeypatch.setattr(client.socket, "socket", create)
    monkeypatch.setattr(client.time, "monotonic", clock.monotonic)
    return sock, clock, created


def received(data=None, source=ADDRESS):
    return (packet() if data is None else data, (source, 1900))


@pytest.mark.parametrize("compatibility", [False, True])
def test_actual_product_packets_and_unicode_optional_header(compatibility):
    cfg = Config(uuid="00112233-4455-6677-8899-aabbccddeeff", friendly_name="夜の部屋 🎵",
                 mdns_instance_name="音楽\u2028LOCATION: http://other.invalid/description.xml",
                 advertise_ip=ADDRESS, http_api_port=8080, upnp_port=8090,
                 wiim_home_profile=compatibility)
    for data in (response_packet(cfg, client.MEDIA_RENDERER),
                 response_packet(cfg, client.MEDIA_RENDERER)[:-2]
                 + "ServiceName: 夜\u2028USN: malicious\u0085SERVER: malicious\r\n\r\n".encode()):
        found = client.parse_candidate(data, ADDRESS)
        assert found == client.Candidate(ADDRESS, 8080 if compatibility else 8090,
                                        cfg.description_url, f"uuid:{cfg.upnp_uuid}")
        assert "夜" not in repr(found) and "ServiceName" not in repr(found)


@pytest.mark.parametrize("identity", [
    IDENTITY, "uuid:" + "ABCDEF0123456789" * 2,
    "uuid:FF00" + "ABCDEF0123456789" * 2,
])
def test_bounded_conventional_and_compact_identities(identity):
    assert client.parse_candidate(packet(identity=identity), ADDRESS).identity == identity


@pytest.mark.parametrize("identity", ["", "uuid:", "uuid:private-name", "uuid:" + "a" * 500,
    "UUID:" + "a" * 32, "uuid:" + "a" * 33, "uuid:" + "g" * 36,
    "uuid:00112233-4455-6677-8899-aabbccddeef", "uuid:" + "a" * 32 + " ",
    "uuid:" + "a" * 32 + "::upnp:rootdevice", "uuid:" + "é" * 32,
    "uuid:" + "a" * 32 + "\x1b[31m", PRIVATE])
def test_hostile_identities_are_not_candidates(identity):
    assert client.parse_candidate(packet(identity=identity), ADDRESS) is None


@pytest.mark.parametrize("location", [
    "https://192.0.2.10:8080/description.xml", "HTTP://192.0.2.10:8080/description.xml",
    "http://192.0.2.11:8080/description.xml", "http://renderer.local:8080/description.xml",
    "http://[::1]:8080/description.xml", "http://192.0.2.10/description.xml",
    "http://192.0.2.10:0/description.xml", "http://192.0.2.10:65536/description.xml",
    "http://192.0.2.10:-1/description.xml", "http://192.0.2.10:+80/description.xml",
    "http://user@192.0.2.10:8080/description.xml", "http://192.0.2.10:8080@evil.test/description.xml",
    LOCATION + "?token=private-value", LOCATION + "#fragment", LOCATION + "?", LOCATION + "#",
    LOCATION + "/", LOCATION + "\x00", LOCATION + "\tinside", LOCATION + "\nextra",
    "http://192.0.2.10:8080/%64escription.xml", "http://192.0.2.10:8080/upnp/description.xml",
    "http://192.0.2.10:8080/../description.xml", "http://192.0.2.10.:8080/description.xml",
    "http://192.000.002.010:8080/description.xml", "http://3221225994:8080/description.xml",
    "http://0xc000020a:8080/description.xml", "http://192.0.2.10\\:8080/description.xml",
    "http://239.255.255.250:8080/description.xml", "http://0.0.0.0:8080/description.xml",
    "http://255.255.255.255:8080/description.xml", "http://192.0.2.10:８０/description.xml",
])
def test_hostile_locations_are_not_candidates(location):
    assert client.parse_candidate(packet(location=location), ADDRESS) is None


@pytest.mark.parametrize("port", [1, 80, 8080, 65535])
def test_valid_description_port_is_not_assumed_primary_api(port):
    found = client.parse_candidate(packet(location=f"http://{ADDRESS}:{port}/description.xml"), ADDRESS)
    assert found.description_port == port and not hasattr(found, "api_port")


@pytest.mark.parametrize("server", ["", "Other/0.1", "OpenRoomRenderer", "OpenRoomRenderer/",
    "NotOpenRoomRenderer/0.1", "OpenRoomRenderer/0.1evil/private", "X/OpenRoomRenderer/0.1",
    "OpenRoomRenderer/" + "a" * 33, "OpenRoomRenderer/0.1" + " x" * 129,
    "OpenRoomRenderer/0.1\u2028malicious", "OpenRoomRenderer/0.1\x1b", "openroomrenderer/0.1"])
def test_only_bounded_product_token_is_a_hint(server):
    assert client.parse_candidate(packet(server=server), ADDRESS) is None


def test_wrong_st_usn_suffix_and_source_are_ignored():
    assert client.parse_candidate(packet(target="upnp:rootdevice"), ADDRESS) is None
    assert client.parse_candidate(packet().replace(
        b"::" + client.MEDIA_RENDERER.encode(), b"::upnp:rootdevice"), ADDRESS) is None
    assert client.parse_candidate(packet(), "192.0.2.11") is None


@pytest.mark.parametrize("required", [b"SERVER", b"ST", b"USN", b"LOCATION"])
def test_missing_or_duplicate_required_headers_rejected_case_insensitively(required):
    lines = packet().split(b"\r\n")
    matching = next(line for line in lines if line.startswith(required + b":"))
    assert client.parse_candidate(b"\r\n".join(line for line in lines if line != matching), ADDRESS) is None
    duplicate = packet()[:-2] + matching.lower() + b"\r\n\r\n"
    assert client.parse_candidate(duplicate, ADDRESS) is None


@pytest.mark.parametrize("mutate", [
    lambda p: p.replace(b"HTTP/1.1 200 OK", b"HTTP/1.0 200 OK"),
    lambda p: p.replace(b"HTTP/1.1 200 OK", b"HTTP/1.1 301 Moved"),
    lambda p: p.replace(b"HTTP/1.1 200 OK", b"HTTP/1.1 200 OK extra"),
    lambda p: p.replace(b"\r\n", b"\n"),
    lambda p: p.replace(b"\r\n", b"\r"),
    lambda p: p.replace(b"SERVER:", b" SERVER:"),
    lambda p: p.replace(b"SERVER:", b"SERVER :"),
    lambda p: p.replace(b"SERVER:", b"SER\x00VER:"),
    lambda p: p.replace(b"SERVER:", b"SERVER\xff:"),
    lambda p: p[:-2], lambda p: p + b"body", lambda p: p + b"\r\n",
    lambda p: b"\r\n" + p,
    lambda p: p[:-2] + b"folded-line\r\n\r\n",
    lambda p: p[:-2] + b"Optional: unsafe\x00value\r\n\r\n",
    lambda p: p[:-2] + b"Optional: unsafe\x7fvalue\r\n\r\n",
    lambda p: p[:-2] + b"Optional: unsafe\nLOCATION: injected\r\n\r\n",
    lambda p: p[:-2] + b"Optional: unsafe\rLOCATION: injected\r\n\r\n",
    lambda p: p[:-2] + b"Optional: a\r\n continuation\r\n\r\n",
    lambda p: p[:-2] + b"x" * 65 + b": value\r\n\r\n",
    lambda p: p[:-2] + b"Optional: " + b"x" * 4096 + b"\r\n\r\n",
])
def test_malformed_status_header_and_packet_framing_is_rejected(mutate):
    assert client.parse_candidate(mutate(packet()), ADDRESS) is None


def test_unicode_line_separator_cannot_inject_required_header():
    data = packet().replace(b"SERVER: Linux/6.x UPnP/1.0 OpenRoomRenderer/0.1\r\n", b"")
    data = data[:-2] + "ServiceName: 夜\u2028SERVER: OpenRoomRenderer/0.1\r\n\r\n".encode()
    assert client.parse_candidate(data, ADDRESS) is None
    # Unknown values are opaque bytes, not UTF-8 prerequisites for real replies.
    assert client.parse_candidate(packet(optional=b"ServiceName: \xff\xfe\r\n"), ADDRESS) is not None


@pytest.mark.parametrize("interface,ttl", [(ADDRESS, 1), ("127.0.0.1", 0), ("127.0.0.2", 0)])
def test_one_ephemeral_explicit_interface_socket_two_sends_and_absolute_default(monkeypatch, interface, ttl):
    sock, clock, created = install_socket(monkeypatch)
    result = client.discover(interface)
    assert created == [(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)]
    assert sock.bound == (interface, 0)
    assert sock.options == [
        (socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface)),
        (socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl),
    ]
    assert sock.sent == [(0, client.SEARCH, client.SSDP_TARGET), (1, client.SEARCH, client.SSDP_TARGET)]
    assert b"MX: 1\r\n" in client.SEARCH
    assert b"ST: " + client.MEDIA_RENDERER.encode() + b"\r\n" in client.SEARCH
    assert clock.now == client.DEFAULT_TIMEOUT and sock.closed == 1
    assert result == {"candidates": [], "verification": "unverified_unauthenticated",
        "status": "no_matching_replies_observed", "truncated": False,
        "limits_reached": [], "oversized_datagrams": 0}


@pytest.mark.parametrize("timeout", [.001, .1, 1, 3, 5])
def test_short_and_maximum_timeouts_preserve_absolute_deadline(monkeypatch, timeout):
    sock, clock, _ = install_socket(monkeypatch)
    client.discover(ADDRESS, timeout)
    assert clock.now == pytest.approx(timeout)
    assert len(sock.sent) == 2 and sock.sent[1][0] == min(1, timeout / 2)
    assert sock.closed == 1


def test_absolute_deadline_is_checked_under_continuous_invalid_traffic(monkeypatch):
    sock, clock, _ = install_socket(monkeypatch, [received(b"invalid")] * 1000, recv_delay=.03)
    result = client.discover(ADDRESS, .2)
    assert .2 <= clock.now < .23
    assert len(sock.read_sizes) == 7 and len(sock.sent) == 2
    assert result["limits_reached"] == [] and not result["truncated"]
    assert sock.closed == 1


def test_duplicate_flood_consumes_receive_limit_and_bounds_dedup(monkeypatch):
    sock, clock, _ = install_socket(monkeypatch, [received()] * 1000)
    result = client.discover(ADDRESS)
    assert len(result["candidates"]) == 1
    assert len(sock.read_sizes) == 256 and sock.read_sizes == [4097] * 256
    assert result["truncated"] and result["limits_reached"] == ["datagram_limit"]
    assert len(sock.sent) == 1 and clock.now == 0 and sock.closed == 1


def test_invalid_flood_consumes_receive_limit(monkeypatch):
    sock, _, _ = install_socket(monkeypatch, [received(b"invalid")] * 1000)
    result = client.discover(ADDRESS)
    assert result["candidates"] == [] and result["truncated"]
    assert result["status"] == "no_matching_replies_observed"
    assert len(sock.read_sizes) == 256 and sock.closed == 1


def test_result_limit_stops_at_64_with_honest_truncation(monkeypatch):
    packets = [received(packet(identity=f"uuid:{index:032x}")) for index in range(1000)]
    sock, _, _ = install_socket(monkeypatch, packets)
    result = client.discover(ADDRESS)
    assert len(result["candidates"]) == len(sock.read_sizes) == 64
    assert result["limits_reached"] == ["candidate_limit"] and result["truncated"]
    assert sock.closed == 1


def test_simultaneous_caps_report_both_limits(monkeypatch):
    packets = [received()] * (256 - 64 + 1)
    packets += [received(packet(identity=f"uuid:{index:032x}")) for index in range(63)]
    sock, _, _ = install_socket(monkeypatch, packets)
    result = client.discover(ADDRESS)
    assert len(result["candidates"]) == 64 and len(sock.read_sizes) == 256
    assert result["limits_reached"] == ["candidate_limit", "datagram_limit"]
    assert result["truncated"] and sock.closed == 1


def test_conflicting_endpoints_and_identities_are_retained_duplicates_are_not(monkeypatch):
    other = packet(location=f"http://{ADDRESS}:8090/description.xml")
    other_address = packet(location="http://192.0.2.11:8080/description.xml")
    other_identity = packet(identity="uuid:" + "a" * 32)
    sock, _, _ = install_socket(monkeypatch, [received(), received(), received(other),
        received(other_address, "192.0.2.11"), received(other_identity),
        received(packet(identity=IDENTITY.upper().replace("UUID:", "uuid:")))])
    result = client.discover(ADDRESS)
    assert len(result["candidates"]) == 4 and sock.closed == 1
    assert len({item["identity"] for item in result["candidates"]}) == 2
    assert len({item["description_url"] for item in result["candidates"]}) == 3


def test_oversize_sentinel_rejects_whole_datagram_including_valid_prefix(monkeypatch):
    prefix = packet()
    data = prefix + b"x" * 8192
    sock, _, _ = install_socket(monkeypatch, [received(data), received()])
    result = client.discover(ADDRESS)
    assert result["oversized_datagrams"] == 1 and len(result["candidates"]) == 1
    assert set(sock.read_sizes) == {4097}
    exact = packet(optional=b"Ignored: " + b"x" * (4096 - len(packet()) - 11) + b"\r\n")
    assert len(exact) == 4096 and client.parse_candidate(exact, ADDRESS) is not None
    assert client.parse_candidate(exact + b"x", ADDRESS) is None


@pytest.mark.parametrize("stage", ["bind", "setsockopt", "settimeout", "sendto", "recvfrom"])
@pytest.mark.parametrize("exception", [OSError(PRIVATE), KeyboardInterrupt(PRIVATE)])
def test_setup_io_and_interrupt_always_close_with_fixed_private_error(monkeypatch, capsys, stage, exception):
    sock, _, _ = install_socket(monkeypatch, failure=(stage, exception))
    code = cli.main(["discover", "--interface", ADDRESS])
    captured = capsys.readouterr()
    assert code == (130 if isinstance(exception, KeyboardInterrupt) else 1)
    assert sock.closed == 1 and captured.out == ""
    assert captured.err == ("Discovery interrupted.\n" if code == 130 else client._NETWORK_ERROR + "\n")


def test_socket_creation_error_is_private(monkeypatch, capsys):
    def fail(*_args):
        raise OSError(PRIVATE)
    monkeypatch.setattr(client.socket, "socket", fail)
    assert cli.main(["discover", "--interface", ADDRESS]) == 1
    assert capsys.readouterr().err == client._NETWORK_ERROR + "\n"


@pytest.mark.parametrize("interface", ["", "0.0.0.0", "239.255.255.250", "255.255.255.255",
    "localhost", "renderer.local", "::1", "127.1", "2130706433", "127.000.0.1",
    "127.0.0.1 ", "127.0.0.1\n", PRIVATE])
def test_invalid_interface_is_rejected_without_network(monkeypatch, capsys, interface):
    monkeypatch.setattr(client.socket, "socket", lambda *_a: pytest.fail("invalid interface reached socket"))
    assert cli.main(["discover", "--interface", interface]) == 2
    assert capsys.readouterr().err == client._INVALID_INPUT + "\n"


@pytest.mark.parametrize("timeout", ["0", "-0", "-1", "5.000001", "nan", "NaN", "inf", "-inf", "1e999",
    "", PRIVATE])
def test_invalid_timeout_is_rejected_without_network(monkeypatch, capsys, timeout):
    monkeypatch.setattr(client.socket, "socket", lambda *_a: pytest.fail("invalid timeout reached socket"))
    assert cli.main(["discover", "--interface", ADDRESS, "--timeout=" + timeout]) == 2
    assert capsys.readouterr().err == client._INVALID_INPUT + "\n"


@pytest.mark.parametrize("arguments", [
    ["discover"], ["discover", "--interface"], ["discover", "--interface", ADDRESS, "--timeout"],
    ["discover", "--config", PRIVATE], ["discover", "--interface", ADDRESS, "--unexpected", PRIVATE],
    ["--unexpected", PRIVATE, "discover", "--interface", ADDRESS],
    ["--timeout", PRIVATE, "discover", "--interface", ADDRESS],
    [PRIVATE, "discover", "--interface", ADDRESS],
])
def test_malformed_discovery_arguments_are_private_including_leading_flags(monkeypatch, capsys, arguments):
    monkeypatch.setattr(client.socket, "socket", lambda *_a: pytest.fail("invalid command reached socket"))
    with pytest.raises(SystemExit) as error:
        cli.main(arguments)
    assert error.value.code == 2
    assert capsys.readouterr().err == "Invalid client command arguments. Run open-room-renderer --help.\n"


@pytest.mark.parametrize("arguments,diagnostic", [
    (["volume", "discover"], "invalid int value: 'discover'"),
    (["status", "--port", "discover"], "invalid int value: 'discover'"),
    (["serve", "--config", "discover", "--unexpected"], "unrecognized arguments: --unexpected"),
    (["play", "discover", "--port", "invalid"], "invalid int value: 'invalid'"),
    (["favorites", "rename", "bad-id", "discover"], "expected a 32-character lowercase hexadecimal favorite ID"),
])
def test_literal_discover_legacy_argument_does_not_change_old_diagnostics(capsys, arguments, diagnostic):
    with pytest.raises(SystemExit) as error:
        cli.main(arguments)
    assert error.value.code == 2 and diagnostic in capsys.readouterr().err


def test_cli_output_has_only_bounded_hints_no_http_dns_config_or_optional_text(monkeypatch, capsys):
    data = packet(optional=("ServiceName: " + PRIVATE + " 夜\r\n").encode(),
                  server="IgnoredPrivateLabel/1 OpenRoomRenderer/0.1")
    sock, _, _ = install_socket(monkeypatch, [received(data)])
    def forbidden(*_args, **_kwargs):
        pytest.fail("discovery called HTTP, DNS, config, service or control code")
    monkeypatch.setattr(client.socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(client.socket, "gethostbyname", forbidden)
    monkeypatch.setattr(cli, "load_config", forbidden)
    monkeypatch.setattr(cli, "serve", forbidden)
    monkeypatch.setattr(cli, "api_get", forbidden)
    monkeypatch.setattr(cli.requests, "Session", forbidden)
    monkeypatch.setattr(cli.requests, "get", forbidden)
    monkeypatch.setattr(cli.requests, "request", forbidden)
    assert cli.main(["discover", "--interface", ADDRESS]) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["verification"] == "unverified_unauthenticated"
    assert result["status"] == "candidates_observed"
    assert result["candidates"] == [{"address": ADDRESS, "description_port": 8080,
                                    "description_url": LOCATION, "identity": IDENTITY}]
    assert captured.err == "" and PRIVATE not in captured.out
    assert "IgnoredPrivateLabel" not in captured.out and "夜" not in captured.out
    assert sock.closed == 1
