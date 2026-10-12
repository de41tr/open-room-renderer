"""Optional mDNS/DNS-SD discovery support.

mDNS is off by default in the safe example config. The custom responder exists
for compatibility experiments where zeroconf's generic service publication is
not close enough to client expectations.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import socket
import struct
import sys
import unicodedata
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
_DNS_CASE_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def dns_name_key(name: str) -> str:
    """DNS ignores ASCII letter case; UTF-8 label bytes otherwise stay intact."""

    return name.translate(_DNS_CASE_FOLD)


def escape_label(label: str) -> str:
    """Escape literal dots/backslashes when joining DNS presentation labels."""

    return label.replace("\\", "\\\\").replace(".", "\\.")


def instance_label(name: str) -> str:
    """Return a precomposed UTF-8 service label within DNS's 63-byte limit."""

    name = unicodedata.normalize("NFC", name)
    name = "".join(char for char in name if ord(char) >= 32 and ord(char) != 127).strip()
    name = name or "Open Room Renderer"
    encoded = name.encode("utf-8")
    if len(encoded) > 63:
        suffix = "-" + hashlib.sha256(encoded).hexdigest()[:8]
        name = encoded[: 63 - len(suffix)].decode("utf-8", errors="ignore") + suffix
    return name


def encode_name(name: str) -> bytes:
    """Encode an escaped dotted DNS name with UTF-8, bounded wire labels."""

    if name in {"", "."}:
        return b"\x00"
    labels: list[str] = []
    label = ""
    escaped = False
    for char in name:
        if escaped:
            label += char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ".":
            labels.append(label)
            label = ""
        else:
            label += char
    if escaped:
        raise ValueError("unfinished DNS name escape")
    if label:
        labels.append(label)
    wire_labels = [label.encode("utf-8") for label in labels]
    if any(not label or len(label) > 63 for label in wire_labels):
        raise ValueError("DNS labels must be between 1 and 63 bytes")
    wire = b"".join(bytes([len(label)]) + label for label in wire_labels) + b"\x00"
    if len(wire) > 255:
        raise ValueError("DNS name exceeds 255 bytes")
    return wire


def decode_name(data: bytes, offset: int) -> tuple[str, int]:
    """Decode a DNS name, including compressed pointers.

    Args:
        data: Complete DNS packet.
        offset: Offset where the encoded name starts.

    Returns:
        ASCII-case-normalized, escaped name and the next uncompressed offset.

    Raises:
        ValueError: If the name or compression pointer is malformed.
    """

    labels: list[str] = []
    jumped = False
    next_offset = offset
    seen = 0
    wire_length = 1
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
        if length > 63 or offset + 1 + length > len(data):
            raise ValueError("invalid DNS label length")
        offset += 1
        try:
            label = data[offset : offset + length].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("invalid UTF-8 DNS label") from exc
        labels.append(escape_label(label))
        wire_length += length + 1
        if wire_length > 255:
            raise ValueError("DNS name exceeds 255 bytes")
        offset += length
    return dns_name_key(".".join(labels)) + ".", next_offset


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
        if len(chunk) > 255:
            raise ValueError("DNS TXT item exceeds 255 bytes")
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
        self._published_instance = self.instance
        self.target = cfg.mdns_hostname
        self.target_key = dns_name_key(self.target)
        self.props = {
            "uuid": cfg.wiim_txt_uuid,
            "bootid": cfg.bootid,
            "security": cfg.security,
        }

    @property
    def instance(self) -> str:
        """Read the current name so runtime renames take effect on queries."""

        return f"{escape_label(instance_label(self.cfg.linkplay_instance_name))}.{self.service}"

    @property
    def instance_key(self) -> str:
        """Return the current instance name in query-comparison form."""

        return dns_name_key(self.instance)

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

    def refresh(self) -> None:
        """Withdraw the old instance after a rename and announce its new name."""

        if not self.transport or self._published_instance == self.instance:
            return
        old_instance = self._published_instance
        goodbye = build_response(0, [
            ptr_rr(self.service, old_instance, ttl=0),
            srv_rr(old_instance, self.target, self.cfg.http_api_port, ttl=0),
            txt_rr(old_instance, self.props, ttl=0),
        ])
        self.transport.sendto(goodbye, (MDNS_GROUP, MDNS_PORT))
        response = self._response_for(0, DnsQuestion(self.service, DNS_TYPE_PTR, DNS_CLASS_IN))
        if response:
            self.transport.sendto(response, (MDNS_GROUP, MDNS_PORT))
            self._published_instance = self.instance

    def _response_for(self, packet_id: int, question: DnsQuestion) -> bytes | None:
        """Build a response for one question, or ``None`` if unsupported."""

        qtype = question.qtype
        qname = dns_name_key(question.name)
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
        self._protocol: CustomMdnsProtocol | None = None

    async def start(self) -> None:
        """Bind UDP/5353, join the multicast group, and start serving."""

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            interface = self.cfg.discovery_interface or "0.0.0.0"
            if self.cfg.discovery_interface:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
                if sys.platform.startswith("linux"):
                    sock.setsockopt(socket.IPPROTO_IP, getattr(socket, "IP_MULTICAST_ALL", 49), 0)
            sock.bind(("", MDNS_PORT))
            mreq = socket.inet_aton(MDNS_GROUP) + socket.inet_aton(interface)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            loop = asyncio.get_running_loop()
            self._transport, self._protocol = await loop.create_datagram_endpoint(lambda: CustomMdnsProtocol(self.cfg), sock=sock)
        except BaseException:
            sock.close()
            raise
        LOG.info(
            "custom mDNS responder started: %s -> %s:%s",
            f"{self.cfg.linkplay_instance_name}._linkplay._tcp.local.",
            self.cfg.mdns_hostname,
            self.cfg.http_api_port,
        )

    async def refresh(self) -> None:
        """Publish a renamed instance without changing its host or identity."""

        if self._protocol:
            self._protocol.refresh()

    async def stop(self) -> None:
        """Close the custom responder transport."""

        if self._transport:
            self._transport.close()
            await asyncio.sleep(0)
            self._transport = None
            self._protocol = None


class MdnsAdvertiser:
    """Optional mDNS advertiser using either custom or zeroconf publishing."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._zeroconf = None
        self._infos = []
        self._refresh_lock = asyncio.Lock()

    async def start(self) -> None:
        """Start mDNS advertising when enabled in config."""

        if not self.cfg.enable_mdns:
            return
        if self.cfg.wiim_home_profile:
            self._zeroconf = CustomMdnsResponder(self.cfg)
            await self._zeroconf.start()
            return
        try:
            from zeroconf import IPVersion, Zeroconf
            from zeroconf.asyncio import AsyncZeroconf
        except Exception as exc:
            LOG.warning("mDNS disabled; zeroconf unavailable: %s", exc)
            if self.cfg.discovery_interface is not None:
                raise
            return
        options = ({"interfaces": [self.cfg.discovery_interface], "ip_version": IPVersion.V4Only}
                   if self.cfg.discovery_interface else {})
        self._zeroconf = AsyncZeroconf(zc=Zeroconf(**options))
        try:
            if self.cfg.discovery_interface:
                await self._configure_selected_interface()
            await self._register_services()
        except BaseException:
            await self.stop()
            raise
        LOG.info("mDNS advertisements started")

    async def _configure_selected_interface(self) -> None:
        """Fail closed unless this owned Zeroconf instance has isolated sockets.

        The explicit-interface adapter depends on engine.readers/senders and
        wrapper.transport.get_extra_info('socket'), present in zeroconf 0.132
        and 0.151.5. Probe these capabilities at runtime rather than assuming
        future internal shapes. The default interface mode never uses them.
        This selects multicast interfaces, not a firewall or DHCP rebinder.
        """

        assert self.cfg.discovery_interface and self._zeroconf is not None
        try:
            zc = self._zeroconf.zeroconf
            await asyncio.wait_for(zc.async_wait_for_start(), 5)
            readers = tuple(zc.engine.readers)
            senders = tuple(zc.engine.senders)
            if not readers or not senders:
                raise RuntimeError("zeroconf has no active discovery interface")

            def owned_socket(wrapper):
                sock = wrapper.transport.get_extra_info("socket")
                if sock is None or sock.family != socket.AF_INET or sock.fileno() < 0:
                    raise RuntimeError("zeroconf discovery socket is not active IPv4")
                return sock

            address = socket.inet_aton(self.cfg.discovery_interface)
            for sender in senders:
                sock = owned_socket(sender)
                if (sock.getsockname()[0] != self.cfg.discovery_interface
                        or sock.getsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, 4) != address):
                    raise RuntimeError("zeroconf sender does not use discovery_interface")
            for reader in readers:
                sock = owned_socket(reader)
                if sys.platform.startswith("linux"):
                    option = getattr(socket, "IP_MULTICAST_ALL", 49)
                    sock.setsockopt(socket.IPPROTO_IP, option, 0)
                    if sock.getsockopt(socket.IPPROTO_IP, option) != 0:
                        raise RuntimeError("zeroconf reader does not isolate multicast membership")
        except (AttributeError, TypeError, OSError, TimeoutError) as exc:
            raise RuntimeError("zeroconf cannot safely select discovery_interface") from exc

    async def _register_services(self) -> None:
        """Register a snapshot of the current config using the existing host."""

        from zeroconf import ServiceInfo

        assert self._zeroconf is not None
        ip_bytes = socket.inet_aton(self.cfg.resolved_advertise_ip)
        infos = [
            ServiceInfo(
                service,
                f"{instance}.{service}",
                addresses=[ip_bytes],
                port=port,
                properties=properties,
                server=self._server_name(),
            )
            for service, port, instance, properties in self._services()
        ]
        for info in infos:
            await self._zeroconf.async_register_service(info, allow_name_change=True)
            self._infos.append(info)

    async def refresh(self) -> None:
        """Refresh a runtime rename; callers should invoke after updating cfg.

        Bind this async method to the HTTP rename callback after ``start``.
        It is a no-op when disabled or stopped and serializes repeated renames.
        """

        async with self._refresh_lock:
            if self._zeroconf is None:
                return
            if isinstance(self._zeroconf, CustomMdnsResponder):
                await self._zeroconf.refresh()
                return
            for info in list(self._infos):
                await self._zeroconf.async_unregister_service(info)
                self._infos.remove(info)
            await self._register_services()

    async def stop(self) -> None:
        """Unregister mDNS services and close advertiser resources."""

        async with self._refresh_lock:
            if self._zeroconf:
                if isinstance(self._zeroconf, CustomMdnsResponder):
                    await self._zeroconf.stop()
                else:
                    for info in self._infos:
                        await self._zeroconf.async_unregister_service(info)
                    await self._zeroconf.async_close()
                self._infos.clear()
                self._zeroconf = None

    def _server_name(self) -> str:
        """Return the DNS-SD target host name for published services."""

        return self.cfg.mdns_hostname

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
            yield "_linkplay._tcp.local.", self.cfg.http_api_port, instance_label(self.cfg.linkplay_instance_name), props
            yield "_http._tcp.local.", self.cfg.http_api_port, instance_label(self.cfg.linkplay_instance_name), {"path": "/description.xml"}
            return

        base = {
            "uuid": self.cfg.upnp_uuid,
            "model": self.cfg.model_name,
            "manufacturer": self.cfg.manufacturer,
            "api_port": str(self.cfg.http_api_port),
            "description": self.cfg.description_url,
        }
        # The generic publisher treats dots as separators rather than literal
        # instance-label characters. Keep this label valid for that API too.
        name = instance_label(self.cfg.linkplay_instance_name.replace(".", "-").replace("\\", "-"))
        yield "_linkplay._tcp.local.", self.cfg.http_api_port, name, base
        yield "_http._tcp.local.", self.cfg.upnp_port, name, {"path": "/description.xml"}
