"""Real loopback-only multicast clients for explicit native acceptance lanes.

No monkeypatches, unicast M-SEARCH substitute, interface enumeration, or host
network configuration are used. The independent wire readers deliberately do
not reuse the renderer's SSDP/DNS packet builders or DNS name decoder.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import socket
import struct
import sys
from typing import AsyncIterator
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET

from aiohttp import ClientSession

from fake_linkplay.config import Config
from fake_linkplay.mdns import MdnsAdvertiser
from fake_linkplay.ssdp import SsdpServer

LOOPBACK = "127.0.0.1"
SSDP = ("239.255.255.250", 1900)
MDNS = ("224.0.0.251", 5353)
LINKPLAY = "_linkplay._tcp.local."
ROOT = "{urn:schemas-upnp-org:device-1-0}"


def multicast_socket(group: str, port: int, *, listen: bool) -> socket.socket:
    """Confine both sending and joined-group reception to the IPv4 loopback."""
    assert sys.platform == "linux", "Explicit discovery lane requires Linux"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(LOOPBACK))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 0)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        sock.setsockopt(socket.IPPROTO_IP, getattr(socket, "IP_MULTICAST_ALL", 49), 0)
        # On this Linux-only lane, binding the multicast group rejects unicast
        # packets sent to the same port. recvfrom's source address alone cannot
        # establish whether the destination was multicast.
        sock.bind((group if listen else LOOPBACK, port if listen else 0))
        if listen:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            socket.inet_aton(group) + socket.inet_aton(LOOPBACK))
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


def ssdp_headers(packet: bytes) -> tuple[str, dict[str, str]]:
    lines = packet.decode("utf-8", errors="strict").split("\r\n")
    return lines[0], {line.split(":", 1)[0].lower(): line.split(":", 1)[1].strip()
                      for line in lines[1:] if ":" in line}


def _dns_name(packet: bytes, offset: int, seen: frozenset[int] = frozenset()) -> tuple[str, int]:
    labels: list[str] = []
    assert offset not in seen and len(seen) < 20, "DNS compression loop"
    seen = seen | {offset}
    while True:
        assert offset < len(packet), "Truncated DNS name"
        length = packet[offset]
        offset += 1
        if length == 0:
            return ".".join(labels) + ".", offset
        if length & 0xC0 == 0xC0:
            assert offset < len(packet), "Truncated DNS pointer"
            suffix, _ = _dns_name(packet, ((length & 0x3F) << 8) | packet[offset], seen)
            return ".".join([*labels, suffix]), offset + 1
        assert length <= 63 and offset + length <= len(packet), "Invalid DNS label"
        labels.append(packet[offset:offset + length].decode("utf-8"))
        offset += length


def dns_records(packet: bytes) -> list[tuple[str, int, object]]:
    """Read PTR, SRV, TXT and A records from a real DNS response."""
    assert len(packet) >= 12, "Truncated DNS packet"
    _, flags, questions, answers, authorities, additionals = struct.unpack("!6H", packet[:12])
    if not flags & 0x8000:
        return []  # Our multicast query is looped back too.
    assert flags & 0xF == 0, "DNS response error"
    offset = 12
    for _ in range(questions):
        _, offset = _dns_name(packet, offset)
        offset += 4
    records = []
    for _ in range(answers + authorities + additionals):
        name, offset = _dns_name(packet, offset)
        assert offset + 10 <= len(packet), "Truncated DNS record"
        kind, _, ttl, length = struct.unpack("!HHIH", packet[offset:offset + 10])
        offset += 10
        end = offset + length
        assert end <= len(packet), "Truncated DNS data"
        value: object = packet[offset:end]
        if kind == 12:
            value, _ = _dns_name(packet, offset)
        elif kind == 33:
            assert length >= 7
            _, _, port = struct.unpack("!HHH", packet[offset:offset + 6])
            target, _ = _dns_name(packet, offset + 6)
            value = (target.lower(), port)
        elif kind == 16:
            props = {}
            cursor = offset
            while cursor < end:
                size = packet[cursor]
                cursor += 1
                assert cursor + size <= end, "Truncated DNS TXT"
                item = packet[cursor:cursor + size].decode("utf-8")
                key, sep, text = item.partition("=")
                if sep:
                    props[key] = text
                cursor += size
            value = props
        elif kind == 1:
            assert length == 4
            value = socket.inet_ntoa(packet[offset:end])
        if ttl:
            records.append((name.lower(), kind, value))
        offset = end
    return records


def _ptr_query() -> bytes:
    name = b"".join(bytes([len(label)]) + label.encode("ascii") for label in LINKPLAY.rstrip(".").split(".")) + b"\0"
    # QM query: multicast request and multicast response, not QU/unicast.
    return struct.pack("!6H", 0, 0, 1, 0, 0, 0) + name + struct.pack("!HH", 12, 1)


@dataclass
class DiscoveredRenderer:
    udn: str
    location: str
    base: str
    mdns_host: str
    services: dict[str, tuple[str, str, str]]


@asynccontextmanager
async def live_discovery(configs: list[Config]) -> AsyncIterator[tuple[list[SsdpServer], list[MdnsAdvertiser]]]:
    """Start and stop real production discovery objects on the chosen interface."""
    ssdp = [SsdpServer(cfg) for cfg in configs]
    mdns = [MdnsAdvertiser(cfg) for cfg in configs]
    notifications = multicast_socket(*SSDP, listen=True)
    try:
        for cfg, s, m in zip(configs, ssdp, mdns):
            assert cfg.discovery_interface == LOOPBACK and cfg.enable_mdns
            assert cfg.wiim_home_profile, "This native lane covers custom mDNS"
            await s.start()
            assert s.sock is not None and len(s.tasks or []) == 2
            await m.start()
            assert m._zeroconf is not None
        expected = {f"uuid:{cfg.upnp_uuid}::upnp:rootdevice": cfg.description_url for cfg in configs}
        seen = {}
        async with asyncio.timeout(5):
            while set(seen) != set(expected):
                packet, peer = await asyncio.get_running_loop().sock_recvfrom(notifications, 65535)
                assert peer[0] == LOOPBACK
                line, headers = ssdp_headers(packet)
                if line == "NOTIFY * HTTP/1.1" and headers.get("usn") in expected:
                    assert headers["nt"] == "upnp:rootdevice" and headers["nts"] == "ssdp:alive"
                    assert headers["location"] == expected[headers["usn"]]
                    seen[headers["usn"]] = headers["location"]
        yield ssdp, mdns
        for server in ssdp:
            for task in server.tasks or []:
                assert not task.done(), f"SSDP background task stopped: {task}"
    finally:
        notifications.close()
        outcomes = await asyncio.gather(
            *(asyncio.wait_for(item.stop(), 5) for item in [*reversed(mdns), *reversed(ssdp)]),
            return_exceptions=True,
        )
        assert not any(isinstance(result, BaseException) for result in outcomes), outcomes
        assert notifications.fileno() == -1
        assert all(s.sock is None and all(t.done() for t in s.tasks or []) for s in ssdp)
        assert all(m._zeroconf is None for m in mdns)


async def discover(configs: list[Config], session: ClientSession, *, timeout: float = 8) -> dict[str, DiscoveredRenderer]:
    """Multicast M-SEARCH/PTR -> advertised locations -> parsed HTTP descriptions."""
    expected = {f"uuid:{cfg.upnp_uuid}": cfg for cfg in configs}
    loop = asyncio.get_running_loop()
    search = multicast_socket(*SSDP, listen=False)
    mdns: socket.socket | None = None
    locations: dict[str, str] = {}
    records: dict[tuple[str, int], object] = {}
    ptr_instances: set[str] = set()
    try:
        mdns = multicast_socket(*MDNS, listen=True)
        request = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
                   'MAN: "ssdp:discover"\r\nMX: 1\r\nST: upnp:rootdevice\r\n\r\n').encode("ascii")
        async with asyncio.timeout(timeout):
            await loop.sock_sendto(search, request, SSDP)
            while set(locations) != set(expected):
                packet, peer = await loop.sock_recvfrom(search, 65535)
                assert peer[0] == LOOPBACK
                line, headers = ssdp_headers(packet)
                if line != "HTTP/1.1 200 OK":
                    continue
                udn, suffix = headers["usn"].rsplit("::", 1)
                if udn in expected:
                    assert suffix == headers["st"] == "upnp:rootdevice"
                    locations[udn] = headers["location"]
            await loop.sock_sendto(mdns, _ptr_query(), MDNS)
            while True:
                packet, peer = await loop.sock_recvfrom(mdns, 65535)
                assert peer[0] == LOOPBACK
                for name, kind, value in dns_records(packet):
                    records[(name, kind)] = value
                    if name == LINKPLAY and kind == 12:
                        ptr_instances.add(value.lower())
                identities = {value.get("uuid") for (name, kind), value in records.items() if kind == 16}
                if identities >= set(expected):
                    break
        found = {}
        for udn, cfg in expected.items():
            location = locations[udn]
            parsed = urlsplit(location)
            assert parsed.scheme == "http" and parsed.hostname == LOOPBACK and parsed.port
            assert parsed.path == "/description.xml" and location == cfg.description_url
            async with session.get(location) as response:
                assert response.status == 200
                root = ET.fromstring(await response.text())
            assert root.findtext(f"{ROOT}device/{ROOT}UDN") == udn
            base = root.findtext(f"{ROOT}URLBase")
            assert base and urljoin(base, "description.xml") == location
            services = {}
            for service in root.findall(f"{ROOT}device/{ROOT}serviceList/{ROOT}service"):
                name = service.findtext(f"{ROOT}serviceId").rsplit(":", 1)[-1]
                services[name] = tuple(service.findtext(f"{ROOT}{field}") or ""
                                       for field in ("serviceType", "controlURL", "eventSubURL"))
            instance = next(name for (name, kind), value in records.items()
                            if kind == 16 and value.get("uuid") == udn)
            assert instance in ptr_instances, "TXT identity must have been discovered through PTR"
            host, port = records[(instance, 33)]
            assert port == parsed.port and records[(host, 1)] == parsed.hostname
            assert host == cfg.mdns_hostname.lower()
            found[udn] = DiscoveredRenderer(udn, location, base.rstrip("/"), host, services)
        assert len({item.location for item in found.values()}) == len(configs)
        assert len({item.mdns_host for item in found.values()}) == len(configs)
        return found
    finally:
        search.close()
        if mdns is not None:
            mdns.close()
            assert mdns.fileno() == -1
        assert search.fileno() == -1
