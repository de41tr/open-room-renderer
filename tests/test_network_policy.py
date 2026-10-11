"""UNIT tests: synthetic URLs and deterministic DNS, never real network I/O."""

from __future__ import annotations

import socket

import pytest
from aiohttp import TCPConnector
from aiohttp.abc import AbstractResolver

from fake_linkplay.network_policy import (
    NetworkPolicyError,
    PinnedResolver,
    resolve_proxy_target,
    validate_callback_url,
)


class FakeResolver(AbstractResolver):
    """Return supplied records, with no OS DNS or socket access."""

    def __init__(self, addresses: list[str]) -> None:
        self.addresses = addresses
        self.calls: list[tuple[str, int, int]] = []
        self.closed = False

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        self.calls.append((host, port, family))
        return [{"host": address} for address in self.addresses]

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def prohibit_real_dns(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network policy tests must not perform real DNS")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@pytest.mark.parametrize(("url", "peer", "expected"), [
    ("http://192.168.10.25:8080/event", "192.168.10.25", "http://192.168.10.25:8080/event"),
    ("<http://10.2.3.4:1400/notify>", "10.2.3.4", "http://10.2.3.4:1400/notify"),
    ("http://172.16.5.7/event?sid=synthetic", "172.16.5.7", "http://172.16.5.7/event?sid=synthetic"),
    ("HTTP://[fd12:3456:0000:0000:0000:0000:0000:0001]:8080/event", "fd12:3456::1", "http://[fd12:3456::1]:8080/event"),
    ("http://127.0.0.1:1400/notify", "127.0.0.1", "http://127.0.0.1:1400/notify"),
    ("http://[::1]:1400/notify", "0:0:0:0:0:0:0:1", "http://[::1]:1400/notify"),
    ("http://192.168.10.25/notify", "::ffff:192.168.10.25", "http://192.168.10.25/notify"),
    ("http://[::ffff:192.168.10.25]/notify", "192.168.10.25", "http://192.168.10.25/notify"),
    ("http://[::ffff:127.0.0.1]/notify", "127.0.0.1", "http://127.0.0.1/notify"),
    ("http://192.0.2.10:80/event", "192.0.2.10", "http://192.0.2.10/event"),
])
def test_callback_exact_transport_peer_and_normalization(url, peer, expected):
    assert validate_callback_url(url, peer) == expected


@pytest.mark.parametrize(("url", "peer"), [
    ("http://192.168.10.26/event", "192.168.10.25"),
    ("http://10.1.2.3/event", "192.168.10.25"),
    ("http://127.0.0.1/event", "192.168.10.25"),
    ("http://127.0.0.2/event", "127.0.0.1"),
    ("http://[::1]/event", "127.0.0.1"),
    ("http://client.example.test/event", "192.168.10.25"),
    ("http://localhost/event", "127.0.0.1"),
    ("http://192.168.10.25/event", "client.example.test"),
    ("http://192.168.10.25/event", ""),
    ("http://192.168.10.25/event", None),
    ("https://192.168.10.25/event", "192.168.10.25"),
    ("<http://192.168.10.25/a><http://192.168.10.25/b>", "192.168.10.25"),
    ("<http://192.168.10.25/a> <http://192.168.10.25/b>", "192.168.10.25"),
    ("<http://192.168.10.25/a>,<http://192.168.10.25/b>", "192.168.10.25"),
    ("http://192.168.10.25/a http://192.168.10.25/b", "192.168.10.25"),
])
def test_callback_rejects_other_hosts_dns_and_multiple_urls(url, peer):
    with pytest.raises(NetworkPolicyError):
        validate_callback_url(url, peer)


@pytest.mark.parametrize("address", [
    "169.254.169.254", "169.254.12.34", "169.254.170.2", "100.100.100.200",
    "168.63.129.16", "192.0.0.192", "fd00:ec2::254", "fe80::1",
    "224.0.0.1", "ff02::1", "0.0.0.0", "::", "240.0.0.1", "255.255.255.255",
    "fec0::1", "64:ff9b::7f00:1", "2002:7f00:1::1", "2001::1",
])
def test_callback_forbidden_address_classes_even_when_peer_matches(address):
    host = f"[{address}]" if ":" in address else address
    with pytest.raises(NetworkPolicyError):
        validate_callback_url(f"http://{host}/event", address)


@pytest.mark.parametrize("url", [
    "", "/audio", "//media.example.test/audio", "file:///audio", "ftp://media.example.test/audio",
    "http:///audio", "http://", "http://media.example.test:/audio",
    "http://media.example.test:0/audio", "http://media.example.test:65536/audio",
    "http://media.example.test:-1/audio", "http://media.example.test:+80/audio",
    "http://media.example.test:no/audio", "http://user@media.example.test/audio",
    "http://user:password@media.example.test/audio", "http://@media.example.test/audio",
    "http://media.example.test/audio#", "http://media.example.test/audio#fragment",
    "http://media.example.test/audio\r\nInjected:yes", "http://media.example.test/\x00audio",
    "http://media.example.test/\x7faudio", "http://media.example.test/\u202eaudio",
    " http://media.example.test/audio", "http://media.example.test/a udio",
    "http://media.example.test/audio\t", "http://media.example.test/%00audio",
    "http://media.example.test/%0D%0Aaudio", "http://media.example.test/%1faudio",
    "http://media.example.test/%7Faudio", "http://media.example.test\\@127.0.0.1/audio",
    "http://[fe80::1%25eth0]/audio", "http://[::1/audio", "http://[v1.invalid]/audio",
    "http://[127.0.0.1]/audio", "http://::1/audio", "http://media.example.test:80:90/audio",
    "http://%31%32%37.0.0.1/audio", "http://127.1/audio", "http://2130706433/audio",
    "http://0x7f000001/audio", "http://0177.0.0.1/audio", "http://0x7f.0.0.1/audio",
    "http://media..example.test/audio", "http://-media.example.test/audio",
    "<http://media.example.test/audio>",
])
async def test_proxy_rejects_malformed_urls_without_dns(url):
    dns = FakeResolver(["8.8.8.8"])
    with pytest.raises(NetworkPolicyError):
        await resolve_proxy_target(url, resolver=dns)
    assert dns.calls == []


@pytest.mark.parametrize("url", [
    "http://user:secret@192.168.1.2/event", "http://192.168.1.2/event#fragment",
    "http://192.168.1.2:/event", "http://192.168.1.2:0/event",
    "http://192.168.1.2:65536/event", "http://192.168.1.2/event\r\n",
    "http://192.168.1.2/%0anotify", "http://192.168.1.2/event\x00",
    "http://192.168.1.2\\@192.168.1.2/event",
])
def test_callback_rejects_unsafe_url_syntax(url):
    with pytest.raises(NetworkPolicyError):
        validate_callback_url(url, "192.168.1.2")


async def test_public_dns_answers_are_all_validated_normalized_and_deduplicated():
    # Public IPs are classification-only fixtures: no packets are sent to them.
    dns = FakeResolver(["8.8.8.8", "2001:4860:4860:0000:0000:0000:0000:8888", "8.8.8.8"])
    target = await resolve_proxy_target("HTTPS://MEDIA.Example.Test:443/audio?part=1&fmt=wav", resolver=dns)
    assert target.url == "https://media.example.test/audio?part=1&fmt=wav"
    assert target.host == "media.example.test"
    assert target.port == 443
    assert target.addresses == ("8.8.8.8", "2001:4860:4860::8888")
    assert dns.calls == [("media.example.test", 443, socket.AF_UNSPEC)]
    assert not dns.closed  # The caller owns an injected resolver.


@pytest.mark.parametrize("url", ["https://8.8.8.8/audio", "https://[::ffff:8.8.8.8]/audio"])
async def test_public_literal_needs_no_dns(url):
    dns = FakeResolver([])
    target = await resolve_proxy_target(url, resolver=dns)
    assert target.url == "https://8.8.8.8/audio"
    assert target.addresses == ("8.8.8.8",)
    assert dns.calls == []


@pytest.mark.parametrize("address", [
    "127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1", "169.254.169.254",
    "169.254.1.2", "fe80::1", "fd00:ec2::254", "100.100.100.200", "168.63.129.16",
    "192.0.0.192", "224.0.0.1", "ff02::1", "0.0.0.0", "::", "240.0.0.1",
    "255.255.255.255", "100.64.0.1", "192.0.2.1", "198.51.100.1", "203.0.113.1",
    "2001:db8::1", "fec0::1", "64:ff9b::7f00:1", "2002:7f00:1::1", "2001::1",
])
@pytest.mark.parametrize("allow_lan", [False, True])
async def test_proxy_forbidden_addresses_stay_forbidden_even_on_allowlist(address, allow_lan):
    dns = FakeResolver([address])
    allowlist = ("media.example.test",) if allow_lan else ()
    with pytest.raises(NetworkPolicyError):
        await resolve_proxy_target("http://media.example.test/audio", allowlist, dns)
    host = f"[{address}]" if ":" in address else address
    with pytest.raises(NetworkPolicyError):
        await resolve_proxy_target(f"http://{host}/audio", (address,) if allow_lan else (), dns)


@pytest.mark.parametrize("address", ["10.2.3.4", "172.16.1.2", "192.168.1.2", "fd12:3456::1", "::ffff:192.168.1.2"])
async def test_proxy_rfc1918_and_ula_require_exact_lan_allowlist(address):
    dns = FakeResolver([address])
    with pytest.raises(NetworkPolicyError):
        await resolve_proxy_target("http://media.example.test/audio", resolver=dns)
    with pytest.raises(NetworkPolicyError):
        await resolve_proxy_target("http://other.media.example.test/audio", ("media.example.test",), dns)
    target = await resolve_proxy_target("http://media.example.test/audio", ("MEDIA.Example.Test.",), dns)
    assert target.addresses == (("192.168.1.2" if address.startswith("::ffff:") else address),)
    host = f"[{address}]" if ":" in address else address
    literal = await resolve_proxy_target(f"http://{host}/audio", (address,), dns)
    assert literal.addresses == target.addresses


@pytest.mark.parametrize("addresses", [
    ["8.8.8.8", "192.168.1.2"], ["192.168.1.2", "8.8.8.8"],
    ["8.8.8.8", "::1"], ["8.8.8.8", "169.254.169.254"],
    ["8.8.8.8", "invalid.example.test"], ["8.8.8.8", "fe80::1%eth0"], [],
])
async def test_one_bad_dns_answer_rejects_entire_target(addresses):
    with pytest.raises(NetworkPolicyError):
        await resolve_proxy_target("http://media.example.test/audio", resolver=FakeResolver(addresses))


async def test_lan_allowlist_can_mix_public_and_private_but_not_loopback():
    target = await resolve_proxy_target(
        "http://media.example.test/audio", ("media.example.test",), FakeResolver(["8.8.8.8", "192.168.1.2"]),
    )
    assert target.addresses == ("8.8.8.8", "192.168.1.2")
    with pytest.raises(NetworkPolicyError):
        await resolve_proxy_target(
            "http://media.example.test/audio", ("media.example.test",), FakeResolver(["192.168.1.2", "127.0.0.1"]),
        )


@pytest.mark.parametrize("allowlist", [
    "media.example.test", ("*.example.test",), ("http://media.example.test",),
    ("media.example.test:8000",), ("192.168.0.0/16",), ("",),
])
async def test_allowlist_is_exact_hosts_only(allowlist):
    dns = FakeResolver(["192.168.1.2"])
    with pytest.raises(NetworkPolicyError):
        await resolve_proxy_target("http://media.example.test/audio", allowlist, dns)
    assert dns.calls == []


async def test_dns_failure_becomes_policy_error():
    class FailingResolver(FakeResolver):
        async def resolve(self, *args, **kwargs):
            raise socket.gaierror("synthetic DNS failure")

    with pytest.raises(NetworkPolicyError, match="safely resolved"):
        await resolve_proxy_target("https://media.example.test/audio", resolver=FailingResolver([]))


async def test_default_resolver_is_closed_after_success_and_failure(monkeypatch):
    for addresses in (["8.8.8.8"], [], ["not-an-ip"]):
        dns = FakeResolver(addresses)
        monkeypatch.setattr("fake_linkplay.network_policy.ThreadedResolver", lambda: dns)
        if addresses == ["8.8.8.8"]:
            await resolve_proxy_target("https://media.example.test/audio")
        else:
            with pytest.raises(NetworkPolicyError):
                await resolve_proxy_target("https://media.example.test/audio")
        assert dns.closed


async def test_pinned_resolver_ignores_dns_rebinding_and_other_destinations():
    dns = FakeResolver(["8.8.8.8", "2001:4860:4860::8888"])
    target = await resolve_proxy_target("https://media.example.test:8443/audio", resolver=dns)
    dns.addresses[:] = ["127.0.0.1", "169.254.169.254"]
    pinned = PinnedResolver(target)
    results = await pinned.resolve(target.host, target.port, socket.AF_UNSPEC)
    assert [result["host"] for result in results] == ["8.8.8.8", "2001:4860:4860::8888"]
    assert all(result["hostname"] == "media.example.test" and result["port"] == 8443 for result in results)
    assert [result["family"] for result in results] == [socket.AF_INET, socket.AF_INET6]
    assert len(await pinned.resolve(target.host, target.port, socket.AF_INET)) == 1
    assert len(await pinned.resolve(target.host, target.port, socket.AF_INET6)) == 1
    with pytest.raises(OSError, match="different destination"):
        await pinned.resolve("other.example.test", target.port)
    with pytest.raises(OSError, match="different destination"):
        await pinned.resolve(target.host, 80)
    with pytest.raises(OSError, match="address family"):
        await pinned.resolve(target.host, target.port, socket.AF_UNIX)
    await pinned.close()
    assert len(dns.calls) == 1


async def test_aiohttp_connector_consumes_pinned_addresses_without_second_dns_lookup():
    dns = FakeResolver(["8.8.8.8", "2001:4860:4860::8888"])
    target = await resolve_proxy_target("https://media.example.test/audio", resolver=dns)
    dns.addresses[:] = ["127.0.0.1"]
    # This executes aiohttp's resolver integration, but never opens a socket.
    async with TCPConnector(resolver=PinnedResolver(target), use_dns_cache=False) as connector:
        records = await connector._resolve_host(target.host, target.port)
    assert tuple(record["host"] for record in records) == target.addresses
    assert len(dns.calls) == 1
