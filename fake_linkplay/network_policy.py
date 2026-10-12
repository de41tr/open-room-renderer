"""Small, fail-closed destination rules for audio proxying and GENA callbacks.

Each proxy hop must be validated separately, then fetched with a fresh connector
using ``PinnedResolver(target)`` and automatic redirects disabled. Retaining the
original hostname in ``target.url`` preserves HTTP Host and TLS certificate/SNI
checks while the resolver prevents a second DNS lookup at connection time.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from aiohttp.abc import AbstractResolver
from aiohttp.resolver import ThreadedResolver
from yarl import URL


class NetworkPolicyError(ValueError):
    """A URL or its resolved destination is unsafe or malformed."""


@dataclass(frozen=True)
class ProxyTarget:
    """One validated URL and the only addresses its connector may contact."""

    url: str
    host: str
    port: int
    addresses: tuple[str, ...]


_IP = ipaddress.IPv4Address | ipaddress.IPv6Address
_LAN_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
))
# Link-local metadata addresses are also denied by their address class. These
# additional platform endpoints must not become accessible via a LAN exception.
_METADATA_ADDRESSES = frozenset(ipaddress.ip_address(value) for value in (
    "169.254.169.254", "169.254.170.2", "100.100.100.200",
    "168.63.129.16", "192.0.0.192", "fd00:ec2::254",
))
_CONTROL_ESCAPE = re.compile(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", re.IGNORECASE)
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_NUMERIC_HOST = re.compile(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*\Z", re.IGNORECASE)


def _ip(value: str) -> _IP:
    # Scoped addresses can change routing and are unnecessary for these rules.
    if not isinstance(value, str) or "%" in value:
        raise ValueError("not an unscoped IP literal")
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def _canonical_host(host: str) -> str:
    if not isinstance(host, str) or not host or any(char.isspace() for char in host):
        raise NetworkPolicyError("missing or invalid host")
    try:
        return str(_ip(host))
    except ValueError:
        pass
    try:
        host = host.encode("idna").decode("ascii").lower().removesuffix(".")
    except UnicodeError as exc:
        raise NetworkPolicyError("invalid host") from exc
    # Do not let legacy numeric IP spellings bypass aiohttp's DNS resolver.
    if _NUMERIC_HOST.fullmatch(host):
        raise NetworkPolicyError("ambiguous numeric host")
    if len(host) > 253 or not all(_DNS_LABEL.fullmatch(label) for label in host.split(".")):
        raise NetworkPolicyError("invalid host")
    return host


def _parse_url(raw_url: str, schemes: set[str]) -> tuple[str, str, int]:
    if not isinstance(raw_url, str) or not raw_url:
        raise NetworkPolicyError("missing URL")
    if any(char.isspace() or unicodedata.category(char) in {"Cc", "Cf"} for char in raw_url):
        raise NetworkPolicyError("URL contains whitespace or controls")
    if _CONTROL_ESCAPE.search(raw_url) or any(char in raw_url for char in "\\<>#"):
        raise NetworkPolicyError("URL contains forbidden characters")
    try:
        parts = urlsplit(raw_url)
        if parts.scheme not in schemes or not parts.netloc:
            raise NetworkPolicyError("unsupported or missing URL scheme/host")
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise NetworkPolicyError("URL credentials are not allowed")
        # Explicitly reject empty, signed, nondecimal, or out-of-range ports and
        # malformed brackets rather than relying on permissive URL parsers.
        pattern = r"\[([^\]]+)\](?::([0-9]+))?" if parts.netloc.startswith("[") else r"([^:]+)(?::([0-9]+))?"
        authority = re.fullmatch(pattern, parts.netloc)
        if authority is None:
            raise NetworkPolicyError("invalid URL authority")
        raw_host, raw_port = authority.groups()
        if parts.netloc.startswith("["):
            if not isinstance(ipaddress.ip_address(raw_host), ipaddress.IPv6Address):
                raise NetworkPolicyError("brackets require an IPv6 literal")
        host = _canonical_host(raw_host)
        port = int(raw_port) if raw_port is not None else (443 if parts.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise NetworkPolicyError("invalid URL port")
        normalized = URL(raw_url).with_host(host)
        return str(normalized), host, port
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, NetworkPolicyError):
            raise
        raise NetworkPolicyError("malformed URL") from exc


def _always_blocked(address: _IP) -> bool:
    return (
        address in _METADATA_ADDRESSES
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        or (
            isinstance(address, ipaddress.IPv6Address)
            and (address.is_site_local or address.sixtofour is not None or address.teredo is not None)
        )
    )


def validate_callback_url(raw_url: str, peer_ip: str) -> str:
    """Accept one HTTP callback to the transport peer's exact literal IP.

    ``raw_url`` may be a bare URL or a single ``<URL>`` Callback header. Pass
    the entire header, not a first-URL regex extraction. ``peer_ip`` must come
    from the socket transport, never Forwarded/X-Forwarded-For headers. Normal
    LAN peers and same-peer loopback clients are supported; DNS is never used.
    """
    if isinstance(raw_url, str) and raw_url.startswith("<") and raw_url.endswith(">"):
        raw_url = raw_url[1:-1]
    normalized, host, _ = _parse_url(raw_url, {"http"})
    try:
        destination, peer = _ip(host), _ip(peer_ip)
    except ValueError as exc:
        raise NetworkPolicyError("callback and transport peer must use literal IPs") from exc
    if destination != peer:
        raise NetworkPolicyError("callback must target the transport peer")
    if _always_blocked(destination) and not destination.is_loopback:
        raise NetworkPolicyError("callback address is forbidden")
    # Equality above also ensures loopback callbacks only belong to local peers.
    return normalized


async def resolve_proxy_target(
    url: str,
    allowed_lan_hosts: Iterable[str] = (),
    resolver: AbstractResolver | None = None,
) -> ProxyTarget:
    """Validate every DNS answer and pin a single HTTP(S) proxy destination.

    Public unicast addresses are allowed by default. Exact allowlisted hosts
    may additionally resolve to RFC1918/ULA addresses, never loopback, metadata,
    link-local, multicast, unspecified, or reserved addresses. Allowlist entries
    are hostnames or bare IP literals, without ports, schemes, or wildcards.
    An injected resolver follows aiohttp's AbstractResolver interface and stays
    owned by its caller. No connection is made by this function.
    """
    normalized, host, port = _parse_url(url, {"http", "https"})
    if isinstance(allowed_lan_hosts, str):
        raise NetworkPolicyError("LAN allowlist must be a collection of exact hosts")
    allowed = {_canonical_host(value) for value in allowed_lan_hosts}
    try:
        literal = _ip(host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [literal]
    else:
        dns = resolver if resolver is not None else ThreadedResolver()
        try:
            answers = await dns.resolve(host, port, family=socket.AF_UNSPEC)
            addresses = [_ip(answer["host"]) for answer in answers]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise NetworkPolicyError("proxy host could not be safely resolved") from exc
        finally:
            if resolver is None:
                await dns.close()
    if not addresses:
        raise NetworkPolicyError("proxy host has no addresses")
    for address in addresses:
        lan = any(address.version == network.version and address in network for network in _LAN_NETWORKS)
        if _always_blocked(address) or address.is_loopback or not (address.is_global or (host in allowed and lan)):
            raise NetworkPolicyError("proxy destination address is forbidden")
    return ProxyTarget(normalized, host, port, tuple(dict.fromkeys(str(address) for address in addresses)))


class PinnedResolver(AbstractResolver):
    """Connection-time resolver with no DNS fallback and one destination only.

    Use ``TCPConnector(resolver=PinnedResolver(target), use_dns_cache=False)``
    in a fresh session with ``trust_env=False`` and ``allow_redirects=False``.
    Revalidate and create a new resolver for each redirect destination. Literal
    IP URLs may bypass aiohttp's resolver, but were already validated above.
    """

    def __init__(self, target: ProxyTarget) -> None:
        self._target = target

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET,
    ) -> list[dict[str, Any]]:
        if host != self._target.host or port != self._target.port:
            raise OSError("pinned resolver cannot resolve a different destination")
        results = []
        for value in self._target.addresses:
            address = _ip(value)
            address_family = socket.AF_INET if address.version == 4 else socket.AF_INET6
            if family not in (socket.AF_UNSPEC, address_family):
                continue
            results.append({
                "hostname": self._target.host,
                "host": value,
                "port": port,
                "family": address_family,
                "proto": socket.IPPROTO_TCP,
                "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
            })
        if not results:
            raise OSError("no pinned addresses for requested address family")
        return results

    async def close(self) -> None:
        """Nothing to close: this resolver never owns network resources."""
