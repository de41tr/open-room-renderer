"""Optional mDNS/DNS-SD discovery support.

mDNS is off by default in the safe example config. The custom responder exists
for compatibility experiments where zeroconf's generic service publication is
not close enough to client expectations.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from collections.abc import Iterable
from dataclasses import dataclass

from .config import Config

LOG = logging.getLogger(__name__)
MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
DNS_CLASS_IN = 1
DNS_CLASS_CACHE_FLUSH = 0x8001
DNS_TYPE_A = 1
DNS_TYPE_PTR = 12
DNS_TYPE_TXT = 16
DNS_TYPE_SRV = 33
DNS_TYPE_ANY = 255
DNS_QU_MASK = 0x8000


def encode_name(name: str) -> bytes:
    """Encode a dotted DNS name into label-length wire format."""

    labels = name.rstrip(".").split(".")
    return b"".join(bytes([len(label)]) + label.encode("ascii") for label in labels) + b"\x00"


def decode_name(data: bytes, offset: int) -> tuple[str, int]:
    """Decode a DNS name, including compressed pointers.

    Args:
        data: Complete DNS packet.
        offset: Offset where the encoded name starts.

    Returns:
        Lowercase fully-qualified name and the next uncompressed offset.

    Raises:
        ValueError: If the name or compression pointer is malformed.
    """

    labels: list[str] = []
    jumped = False
    next_offset = offset
    seen = 0
    while True:
        if offset >= len(data):
            raise ValueError("dns name offset out of range")
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("dns pointer out of range")
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                next_offset = offset + 2
            offset = pointer
            jumped = True
            seen += 1
            if seen > 20:
                raise ValueError("dns pointer loop")
            continue
        if length == 0:
            offset += 1
            if not jumped:
                next_offset = offset
            break
        offset += 1
        labels.append(data[offset : offset + length].decode("ascii", errors="ignore"))
        offset += length
    return ".".join(labels).lower() + ".", next_offset


@dataclass(frozen=True)
class DnsQuestion:
    """Question record from an mDNS packet."""

    name: str
    qtype: int
    qclass: int

    @property
    def wants_unicast(self) -> bool:
        """Return whether the question requests a unicast response."""

        return bool(self.qclass & DNS_QU_MASK)


def parse_questions(data: bytes) -> tuple[int, list[DnsQuestion]]:
    """Parse the header and question section of an mDNS query packet."""

    if len(data) < 12:
        return 0, []
    packet_id, _flags, qdcount, _ancount, _nscount, _arcount = struct.unpack("!HHHHHH", data[:12])
    offset = 12
    questions: list[DnsQuestion] = []
    for _ in range(qdcount):
        name, offset = decode_name(data, offset)
        if offset + 4 > len(data):
            break
        qtype, qclass = struct.unpack("!HH", data[offset : offset + 4])
        offset += 4
        questions.append(DnsQuestion(name, qtype, qclass))
    return packet_id, questions


def rr(name: str, rtype: int, rclass: int, ttl: int, rdata: bytes) -> bytes:
    """Build a generic DNS resource record."""

    return encode_name(name) + struct.pack("!HHIH", rtype, rclass, ttl, len(rdata)) + rdata


def ptr_rr(name: str, target: str, ttl: int = 120) -> bytes:
    """Build a PTR resource record."""

    return rr(name, DNS_TYPE_PTR, DNS_CLASS_IN, ttl, encode_name(target))


def srv_rr(name: str, target: str, port: int, ttl: int = 120) -> bytes:
    """Build an SRV resource record for a service instance."""

    data = struct.pack("!HHH", 0, 0, port) + encode_name(target)
    return rr(name, DNS_TYPE_SRV, DNS_CLASS_CACHE_FLUSH, ttl, data)


def txt_rr(name: str, props: dict[str, str], ttl: int = 120) -> bytes:
    """Build a TXT resource record from string key/value properties."""

    chunks = []
    for key, value in props.items():
        chunk = f"{key}={value}".encode("utf-8")
        chunks.append(bytes([len(chunk)]) + chunk)
    return rr(name, DNS_TYPE_TXT, DNS_CLASS_CACHE_FLUSH, ttl, b"".join(chunks))


def a_rr(name: str, ip: str, ttl: int = 120) -> bytes:
    """Build an IPv4 A resource record."""

    return rr(name, DNS_TYPE_A, DNS_CLASS_CACHE_FLUSH, ttl, socket.inet_aton(ip))


def build_response(packet_id: int, answers: list[bytes], additionals: list[bytes] | None = None) -> bytes:
    """Build an mDNS response packet from pre-encoded resource records."""

    additionals = additionals or []
    header = struct.pack("!HHHHHH", packet_id, 0x8400, 0, len(answers), 0, len(additionals))
    return header + b"".join(answers) + b"".join(additionals)


class CustomMdnsProtocol(asyncio.DatagramProtocol):
    """Low-level responder for app-compatibility mDNS experiments."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.transport: asyncio.DatagramTransport | None = None
        self.service = "_linkplay._tcp.local."
        self.instance = f"{cfg.linkplay_instance_name}.{self.service}"
        self.instance_key = self.instance.lower()
        self.target = "fake-linkplay.local."
        self.target_key = self.target.lower()
        self.props = {
            "uuid": cfg.wiim_txt_uuid,
            "bootid": cfg.bootid,
            "security": cfg.security,
        }

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Remember the datagram transport provided by asyncio."""

        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Respond to mDNS queries that mention this renderer's service names."""

        try:
            packet_id, questions = parse_questions(data)
        except ValueError as exc:
            LOG.debug("ignoring malformed mDNS packet from %s: %s", addr, exc)
            return
        for question in questions:
            response = self._response_for(packet_id, question)
            if not response or not self.transport:
                continue
            target = addr if question.wants_unicast else (MDNS_GROUP, MDNS_PORT)
            self.transport.sendto(response, target)
            LOG.debug("mDNS response qname=%s qtype=%s target=%s bytes=%s", question.name, question.qtype, target, len(response))

    def _response_for(self, packet_id: int, question: DnsQuestion) -> bytes | None:
        """Build a response for one question, or ``None`` if unsupported."""

        qtype = question.qtype
        qname = question.name
        service_match = qname == self.service and qtype in {DNS_TYPE_PTR, DNS_TYPE_ANY}
        instance_match = qname == self.instance_key and qtype in {DNS_TYPE_SRV, DNS_TYPE_TXT, DNS_TYPE_ANY}
        target_match = qname == self.target_key and qtype in {DNS_TYPE_A, DNS_TYPE_ANY}
        ip = self.cfg.resolved_advertise_ip
        if service_match:
            return build_response(
                packet_id,
                [ptr_rr(self.service, self.instance)],
                [srv_rr(self.instance, self.target, self.cfg.http_api_port), txt_rr(self.instance, self.props), a_rr(self.target, ip)],
            )
        if instance_match:
            answers: list[bytes] = []
            if qtype in {DNS_TYPE_SRV, DNS_TYPE_ANY}:
                answers.append(srv_rr(self.instance, self.target, self.cfg.http_api_port))
            if qtype in {DNS_TYPE_TXT, DNS_TYPE_ANY}:
                answers.append(txt_rr(self.instance, self.props))
            return build_response(packet_id, answers, [a_rr(self.target, ip)])
        if target_match:
            return build_response(packet_id, [a_rr(self.target, ip)])
        return None


class CustomMdnsResponder:
    """Owns the multicast socket for the custom mDNS protocol."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        """Bind UDP/5353, join the multicast group, and start serving."""

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        sock.bind(("", MDNS_PORT))
        mreq = socket.inet_aton(MDNS_GROUP) + socket.inet_aton("0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        loop = asyncio.get_running_loop()
        self._transport, _protocol = await loop.create_datagram_endpoint(lambda: CustomMdnsProtocol(self.cfg), sock=sock)
        LOG.info(
            "custom mDNS responder started: %s -> %s:%s",
            f"{self.cfg.linkplay_instance_name}._linkplay._tcp.local.",
            "fake-linkplay.local.",
            self.cfg.http_api_port,
        )

    async def stop(self) -> None:
        """Close the custom responder transport."""

        if self._transport:
            self._transport.close()
            await asyncio.sleep(0)


class MdnsAdvertiser:
    """Optional mDNS advertiser using either custom or zeroconf publishing."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._zeroconf = None
        self._infos = []

    async def start(self) -> None:
        """Start mDNS advertising when enabled in config."""

        if not self.cfg.enable_mdns:
            return
        if self.cfg.wiim_home_profile:
            self._zeroconf = CustomMdnsResponder(self.cfg)
            await self._zeroconf.start()
            return
        try:
            from zeroconf import ServiceInfo, Zeroconf
            from zeroconf.asyncio import AsyncZeroconf
        except Exception as exc:
            LOG.warning("mDNS disabled; zeroconf unavailable: %s", exc)
            return
        ip_bytes = bytes(int(part) for part in self.cfg.resolved_advertise_ip.split("."))
        self._zeroconf = AsyncZeroconf(zc=Zeroconf())
        for service, port, instance, properties in self._services():
            info = ServiceInfo(
                service,
                f"{instance}.{service}",
                addresses=[ip_bytes],
                port=port,
                properties=properties,
                server=self._server_name(),
            )
            await self._zeroconf.async_register_service(info, allow_name_change=True)
            self._infos.append(info)
        LOG.info("mDNS advertisements started")

    async def stop(self) -> None:
        """Unregister mDNS services and close advertiser resources."""

        if self._zeroconf:
            if isinstance(self._zeroconf, CustomMdnsResponder):
                await self._zeroconf.stop()
                return
            for info in self._infos:
                await self._zeroconf.async_unregister_service(info)
            await self._zeroconf.async_close()

    def _server_name(self) -> str:
        """Return the DNS-SD target host name for published services."""

        if self.cfg.wiim_home_profile:
            return "fake-linkplay.local."
        suffix = self.cfg.wiim_uuid[:12] if self.cfg.wiim_home_profile else (self.cfg.uuid or "").replace("-", "")[:12]
        suffix = suffix or "000000000001"
        return f"WiiM-Mini-{suffix}.local."

    def _services(self) -> Iterable[tuple[str, int, str, dict[str, str]]]:
        """Yield DNS-SD service records for the current config profile."""

        if self.cfg.wiim_home_profile:
            # WiiM Home parses Android NsdServiceInfo.toString() with substring
            # offsets. Keep this deliberately small and put security last.
            props = {
                "uuid": self.cfg.wiim_txt_uuid,
                "bootid": self.cfg.bootid,
                "security": self.cfg.security,
            }
            yield "_linkplay._tcp.local.", self.cfg.http_api_port, self.cfg.linkplay_instance_name, props
            yield "_http._tcp.local.", self.cfg.http_api_port, self.cfg.linkplay_instance_name, {"path": "/description.xml"}
            return

        base = {
            "uuid": self.cfg.uuid or "",
            "model": self.cfg.model_name,
            "manufacturer": self.cfg.manufacturer,
            "api_port": str(self.cfg.http_api_port),
            "description": self.cfg.description_url,
        }
        suffix = (self.cfg.uuid or "").replace("-", "")[:6].upper() or "000001"
        yield "_linkplay._tcp.local.", self.cfg.http_api_port, self.cfg.friendly_name, base
        yield "_linkplay._tcp.local.", self.cfg.http_api_port, f"{self.cfg.model_name}-{suffix}", {}
        yield "_linkplay._tcp.local.", self.cfg.http_api_port, f"WiiM Mini-{suffix}", {"uuid": self.cfg.uuid or ""}
        yield "_wiim._tcp.local.", self.cfg.http_api_port, self.cfg.friendly_name, base
        yield "_http._tcp.local.", self.cfg.upnp_port, self.cfg.friendly_name, {"path": "/description.xml"}
