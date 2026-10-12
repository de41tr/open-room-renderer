"""Bounded, SSDP-only address hints for explicitly selected IPv4 interfaces.

Replies are unauthenticated self-descriptions, never verified renderer identity
or permission to contact/control an endpoint. This client does not read config,
resolve names, fetch descriptions, join multicast groups, or keep a listener.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import ipaddress
import json
import math
import re
import socket
import sys
import time

SSDP_TARGET = ("239.255.255.250", 1900)
MEDIA_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"
DEFAULT_TIMEOUT = 3.0
MAX_TIMEOUT = 5.0
MAX_DATAGRAMS = 256
MAX_PACKET_BYTES = 4096
MAX_CANDIDATES = 64
SEARCH = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
          'MAN: "ssdp:discover"\r\nMX: 1\r\n'
          f"ST: {MEDIA_RENDERER}\r\n\r\n").encode("ascii")
_REQUIRED = {b"server", b"st", b"usn", b"location"}
_HEADER_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}")
_PRODUCT = re.compile(r"(?:^| )OpenRoomRenderer/[0-9A-Za-z][0-9A-Za-z._-]{0,31}(?: |$)")
_IDENTITY = re.compile(r"uuid:(?:[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}"
                       r"|[0-9A-Fa-f]{32}|[0-9A-Fa-f]{36})")
_LOCATION = re.compile(r"http://([0-9.]{7,15}):([0-9]{1,5})/description\.xml")
_INVALID_INPUT = "Invalid discovery input. Use an explicit local IPv4 address and a timeout above 0 and at most 5 seconds."
_NETWORK_ERROR = "Discovery failed: could not use the selected IPv4 interface or complete SSDP discovery."


class DiscoveryError(ValueError):
    """Fixed, private-safe discovery error, never raw arguments or packets."""


def local_ipv4(value: str) -> str:
    """Validate a literal unicast address; binding establishes local ownership."""
    try:
        address = ipaddress.IPv4Address(value)
        if (not isinstance(value, str) or str(address) != value or address.is_unspecified
                or address.is_multicast or value == "255.255.255.255"):
            raise ValueError
    except (ValueError, TypeError, ipaddress.AddressValueError):
        raise DiscoveryError(_INVALID_INPUT) from None
    return value


def discovery_timeout(value: str | float) -> float:
    """Require a finite positive duration within the absolute deadline cap."""
    try:
        timeout = float(value)
        if isinstance(value, bool) or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise DiscoveryError(_INVALID_INPUT) from None
    return timeout


@dataclass(frozen=True)
class Candidate:
    """An unverified advertised identity and its source-matched description URL."""

    address: str
    description_port: int
    description_url: str
    identity: str


def parse_candidate(packet: bytes, source: str) -> Candidate | None:
    """Accept only tightly bounded OpenRoomRenderer MediaRenderer responses.

    Split CRLF bytes before decoding required fields. Optional values can contain
    Unicode (including friendly-name ServiceName), but cannot introduce headers
    through Unicode line separators. Arbitrary optional/server text is not output.
    """
    if len(packet) > MAX_PACKET_BYTES or not packet.endswith(b"\r\n\r\n"):
        return None
    lines = packet[:-4].split(b"\r\n")
    if not lines or lines[0] != b"HTTP/1.1 200 OK":
        return None
    headers: dict[bytes, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(b":")
        if (not separator or _HEADER_NAME.fullmatch(name) is None
                or any(byte < 32 and byte != 9 or byte == 127 for byte in value)):
            return None
        name = name.lower()
        if name not in _REQUIRED:
            continue
        if name in headers:
            return None
        value = value.strip(b" \t")
        if any(byte < 32 or byte > 126 for byte in value):
            return None
        headers[name] = value.decode("ascii")
    if headers.keys() != _REQUIRED:
        return None
    if (headers[b"st"] != MEDIA_RENDERER or len(headers[b"server"]) > 256
            or _PRODUCT.search(headers[b"server"]) is None):
        return None
    identity, separator, target = headers[b"usn"].partition("::")
    if not separator or target != MEDIA_RENDERER or _IDENTITY.fullmatch(identity) is None:
        return None
    match = _LOCATION.fullmatch(headers[b"location"])
    if match is None:
        return None
    address, port_text = match.groups()
    try:
        address = local_ipv4(address)
        port = int(port_text)
        if source != address or not 1 <= port <= 65535:
            return None
    except DiscoveryError:
        return None
    # Reconstruct from validated fields; no opaque packet text reaches stdout.
    return Candidate(address, port, f"http://{address}:{port}/description.xml", identity)


def discover(interface: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, object]:
    """Collect bounded hints until the absolute deadline or a resource cap.

    A one-byte oversize sentinel prevents a truncated packet from being accepted.
    Every datagram, including invalid/duplicate traffic, consumes the receive cap.
    Endpoint conflicts for the same identity are retained as separate candidates.
    """
    interface = local_ipv4(interface)
    timeout = discovery_timeout(timeout)
    started = time.monotonic()
    deadline = started + timeout
    second_send = started + min(1.0, timeout / 2)
    candidates: dict[tuple[str, str, int], Candidate] = {}
    limits: list[str] = []
    oversized = received = sent = 0
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.bind((interface, 0))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL,
                        0 if ipaddress.IPv4Address(interface).is_loopback else 1)
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            if sent == 0 or sent == 1 and now >= second_send:
                sock.settimeout(deadline - now)
                sock.sendto(SEARCH, SSDP_TARGET)
                sent += 1
                continue
            wake_at = min(deadline, second_send) if sent == 1 else deadline
            sock.settimeout(wake_at - now)
            try:
                packet, peer = sock.recvfrom(MAX_PACKET_BYTES + 1)
            except socket.timeout:
                continue
            received += 1
            if len(packet) > MAX_PACKET_BYTES:
                oversized += 1
            else:
                candidate = parse_candidate(packet, peer[0])
                if candidate is not None:
                    key = (candidate.identity.lower(), candidate.address, candidate.description_port)
                    candidates.setdefault(key, candidate)
            if len(candidates) >= MAX_CANDIDATES:
                limits.append("candidate_limit")
            if received >= MAX_DATAGRAMS:
                limits.append("datagram_limit")
            if limits:
                break
    finally:
        sock.close()
    return {
        "candidates": [asdict(candidate) for candidate in candidates.values()],
        "verification": "unverified_unauthenticated",
        "status": "candidates_observed" if candidates else "no_matching_replies_observed",
        "truncated": bool(limits),
        "limits_reached": limits,
        "oversized_datagrams": oversized,
    }


def add_discovery_parser(subparsers) -> None:
    parser = subparsers.add_parser("discover", help="find unverified renderer address hints using SSDP",
        description="Bounded SSDP-only discovery on an explicit local IPv4 interface. "
                    "Results are unauthenticated and unverified; nothing is fetched or controlled.")
    parser.add_argument("--interface", required=True, metavar="LOCAL_IPV4")
    parser.add_argument("--timeout", default=DEFAULT_TIMEOUT, metavar="SECONDS",
                        help="absolute discovery deadline, above 0 and at most 5 seconds (default: 3)")


def run_discovery(args: argparse.Namespace) -> int:
    try:
        result = discover(args.interface, args.timeout)
    except DiscoveryError as error:
        print(str(error), file=sys.stderr)
        return 2
    except OSError:
        print(_NETWORK_ERROR, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Discovery interrupted.", file=sys.stderr)
        return 130
    print(json.dumps(result, sort_keys=True))
    return 0
