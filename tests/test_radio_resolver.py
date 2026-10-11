"""UNIT/MOCK public station resolution: synthetic data, no WAN or real DNS."""

from __future__ import annotations

import asyncio
import gzip
import json
import socket
from dataclasses import FrozenInstanceError

import pytest
from aiohttp import ClientConnectionError, DummyCookieJar

import fake_linkplay.radio as module
from fake_linkplay.network_policy import NetworkPolicyError, PinnedResolver, ProxyTarget
from fake_linkplay.radio import RadioResolution, RadioResolutionError, resolve_station


STATION = "s12345"
MEDIA = "https://media.example.test/live.aac"
ENDPOINT = "https://opml.radiotime.com/Tune.ashx?id=s12345&formats=mp3,aac&render=json"


def audio(url=MEDIA, **kwargs):
    return {"element": "audio", "url": url, "media_type": "aac", "is_direct": True, **kwargs}


def payload(*items, status="200"):
    return json.dumps({"head": {"status": status}, "body": list(items)}).encode()


async def accept(url):
    return None


def fetched(raw):
    async def fetch(url):
        assert url == ENDPOINT
        return raw
    return fetch


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("radio resolver tests must not perform real network I/O")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.mark.parametrize("status", ["200", 200])
async def test_resolves_canonical_station_to_immutable_audio_without_metadata_changes(status):
    result = await resolve_station(STATION, fetcher=fetched(payload(audio(), status=status)), validator=accept)
    assert result == RadioResolution(MEDIA, STATION, 0, "audio")
    with pytest.raises(FrozenInstanceError):
        result.url = "https://other.example.test/audio"


@pytest.mark.parametrize("station", [
    "", "S12345", "t12345", "s", "s１２３", "s1\n", " s1", "s1 ", "s-1", "s+1",
    "s1&partnerId=synthetic", "s1?id=2", "https://search.example.test/?id=s1", None, 12345,
    "s" + "1" * 33,
])
async def test_invalid_or_unsupported_station_ids_never_fetch(station):
    async def never(url):
        pytest.fail("invalid station IDs must not trigger any fetch or validation")
    with pytest.raises(RadioResolutionError, match="station ID"):
        await resolve_station(station, fetcher=never, validator=never)


@pytest.mark.parametrize("raw", [
    b"", b"not JSON", b"\xff", b"[]", b"null", b"42", b"{}", b'{"head":[]}',
    b'{"head":{"status":"200"}}', b'{"head":{"status":"200"},"body":{}}',
    b'{"head":{"status":"200","fault":"api.methodNotFound"},"body":[]}',
    b'{"head":{"status":"200"},"fault":"api.methodNotFound","body":[]}',
    {"head": {"status": "200"}, "body": []},
])
async def test_malformed_shapes_and_json_fail_closed(raw):
    with pytest.raises(RadioResolutionError):
        await resolve_station(STATION, fetcher=fetched(raw), validator=accept)


@pytest.mark.parametrize("status", ["404", 404, "500", None, True, "ok"])
async def test_service_failure_status_rejects_even_if_http_success_and_audio_present(status):
    with pytest.raises(RadioResolutionError, match="unavailable"):
        await resolve_station(STATION, fetcher=fetched(payload(audio(), status=status)), validator=accept)


@pytest.mark.parametrize("item", [
    None, [], "not a candidate", {}, {"element": "fault", "text": "api.methodNotFound"},
    audio(element="link"), audio(is_direct=False), audio(is_direct="true"),
    audio(media_type="wma"), audio(url=None), audio(url=123),
])
async def test_unsupported_candidates_are_not_returned(item):
    with pytest.raises(RadioResolutionError, match="no supported public audio"):
        await resolve_station(STATION, fetcher=fetched(payload(item)), validator=accept)


@pytest.mark.parametrize("url", [
    "https://service.example.test/service/Audio/notcompatible.enUS.mp3",
    "http://service.example.test/SERVICE/audio/unavailable.mp3?station=s1",
    "https://service.example.test/%73ervice/%41udio/notcompatible.enUS.mp3",
    "https://service.example.test/service//Audio/notcompatible.enUS.mp3",
    "https://service.example.test/service/./Audio/notcompatible.enUS.mp3",
    "file:///etc/passwd", "ftp://media.example.test/audio", "//media.example.test/audio",
    "javascript:alert(1)", "http:///audio", "http://user:secret@media.example.test/audio",
    "http://[malformed/audio", "",
])
async def test_service_error_media_and_non_http_urls_are_skipped_before_validator(url):
    validated = []
    async def validator(candidate):
        validated.append(candidate)
    result = await resolve_station(STATION, fetcher=fetched(payload(audio(url), audio())), validator=validator)
    assert result.url == MEDIA
    assert validated == [MEDIA]


async def test_later_candidate_is_tried_after_validator_rejects_private_destination():
    rejected = "http://192.168.1.1/admin"
    validated = []
    async def validator(url):
        validated.append(url)
        if url == rejected:
            raise NetworkPolicyError("unsafe private detail")
        return ProxyTarget(url, "media.example.test", 443, ("8.8.8.8",))
    result = await resolve_station(STATION, fetcher=fetched(payload(audio(rejected), audio())), validator=validator)
    assert result.url == MEDIA
    assert validated == [rejected, MEDIA]


async def test_false_rejects_candidate_and_normalized_public_target_url_is_returned():
    first = "https://first.example.test/audio"
    normalized = "https://media.example.test/live.aac?normalized=1"
    async def validator(url):
        if url == first:
            return False
        return ProxyTarget(normalized, "media.example.test", 443, ("8.8.8.8",))
    result = await resolve_station(STATION, fetcher=fetched(payload(audio(first), audio())), validator=validator)
    assert result.url == normalized


async def test_default_validator_uses_public_policy_without_lan_allowlist(monkeypatch):
    calls = []
    async def validate(url):
        calls.append(url)
        raise NetworkPolicyError("synthetic private destination")
    monkeypatch.setattr(module, "resolve_proxy_target", validate)
    with pytest.raises(RadioResolutionError, match="no supported public audio"):
        await resolve_station(STATION, fetcher=fetched(payload(audio())))
    assert calls == [MEDIA]


async def test_real_public_policy_rejects_private_literal_without_dns():
    with pytest.raises(RadioResolutionError, match="no supported public audio"):
        await resolve_station(STATION, fetcher=fetched(payload(audio("http://127.0.0.1/admin"))))


async def test_injected_response_size_limit_is_enforced_before_parsing(monkeypatch):
    raw = payload(audio())
    monkeypatch.setattr(module, "_MAX_JSON_BYTES", len(raw))
    assert (await resolve_station(STATION, fetcher=fetched(raw), validator=accept)).url == MEDIA
    with pytest.raises(RadioResolutionError, match="too large"):
        await resolve_station(STATION, fetcher=fetched(raw + b" "), validator=accept)


@pytest.mark.parametrize("stage", ["fetch", "validation"])
async def test_deadline_covers_fetch_and_candidate_validation(monkeypatch, stage):
    monkeypatch.setattr(module, "_DEADLINE_SECONDS", 0.01)
    async def wait_forever(url):
        await asyncio.Event().wait()
    with pytest.raises(RadioResolutionError, match="resolution failed"):
        await resolve_station(
            STATION,
            fetcher=wait_forever if stage == "fetch" else fetched(payload(audio())),
            validator=wait_forever if stage == "validation" else accept,
        )


async def test_cancellation_is_not_converted_to_resolution_error():
    entered = asyncio.Event()
    async def wait_forever(url):
        entered.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(resolve_station(STATION, fetcher=wait_forever, validator=accept))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("error", [OSError("secret upstream detail"), ClientConnectionError("secret upstream detail")])
async def test_transport_errors_are_sanitized(error):
    async def failure(url):
        raise error
    with pytest.raises(RadioResolutionError) as caught:
        await resolve_station(STATION, fetcher=failure, validator=accept)
    assert "secret" not in str(caught.value)


@pytest.fixture
def transport(monkeypatch):
    """Record production transport choices without opening any sockets."""
    class Response:
        status = 200
        headers = {}
        content_length = None
        chunks = [payload(audio())]
        read_chunks = 0
        @property
        def content(self):
            return self
        async def iter_chunked(self, size):
            for chunk in self.chunks:
                self.read_chunks += 1
                yield chunk
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
    response = Response()
    calls = {"resolve": [], "get": [], "connector": [], "session": []}
    async def resolve(url):
        calls["resolve"].append(url)
        return ProxyTarget(url, "opml.radiotime.com", 443, ("8.8.8.8",))
    def connector(**kwargs):
        calls["connector"].append(kwargs)
        return object()
    class Session:
        def __init__(self, **kwargs):
            calls["session"].append(kwargs)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        def get(self, url, **kwargs):
            calls["get"].append((url, kwargs))
            return response
    monkeypatch.setattr(module, "resolve_proxy_target", resolve)
    monkeypatch.setattr(module, "TCPConnector", connector)
    monkeypatch.setattr(module, "ClientSession", Session)
    return response, calls


async def test_production_fetch_is_pinned_bounded_and_does_not_forward_auth(transport):
    response, calls = transport
    assert (await resolve_station(STATION, validator=accept)).url == MEDIA
    assert calls["resolve"] == [ENDPOINT]
    assert calls["get"] == [(ENDPOINT, {"allow_redirects": False})]
    connection = calls["connector"][0]
    assert isinstance(connection["resolver"], PinnedResolver)
    assert connection["use_dns_cache"] is False and connection["limit"] == 1
    answers = await connection["resolver"].resolve("opml.radiotime.com", 443)
    assert [answer["host"] for answer in answers] == ["8.8.8.8"]
    with pytest.raises(OSError):
        await connection["resolver"].resolve("other.example.test", 443)
    session = calls["session"][0]
    assert session["trust_env"] is False and session["auto_decompress"] is False
    assert isinstance(session["cookie_jar"], DummyCookieJar)
    assert session["timeout"].total == module._DEADLINE_SECONDS
    assert session["headers"] == {
        "User-Agent": "OpenRoomRenderer/0.1", "Accept": "application/json", "Accept-Encoding": "identity",
    }


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 401, 404, 500])
async def test_production_fetch_never_follows_redirects_or_accepts_http_errors(transport, status):
    response, calls = transport
    response.status = status
    response.headers = {"Location": "http://127.0.0.1/admin"}
    with pytest.raises(RadioResolutionError):
        await resolve_station(STATION, validator=accept)
    assert len(calls["get"]) == 1 and response.read_chunks == 0


async def test_production_fetch_rejects_content_length_before_read(transport, monkeypatch):
    response, _ = transport
    monkeypatch.setattr(module, "_MAX_JSON_BYTES", 10)
    response.content_length = 11
    with pytest.raises(RadioResolutionError, match="too large"):
        await resolve_station(STATION, validator=accept)
    assert response.read_chunks == 0


async def test_production_fetch_enforces_streamed_limit_with_unknown_length(transport, monkeypatch):
    response, _ = transport
    monkeypatch.setattr(module, "_MAX_JSON_BYTES", 10)
    response.chunks = [b"12345", b"67890", b"1", b"must not be read"]
    with pytest.raises(RadioResolutionError, match="too large"):
        await resolve_station(STATION, validator=accept)
    assert response.read_chunks == 3


async def test_production_fetch_rejects_compression_before_decompression(transport):
    response, _ = transport
    response.headers = {"Content-Encoding": "gzip"}
    response.chunks = [gzip.compress(b" " * (module._MAX_JSON_BYTES + 1))]
    with pytest.raises(RadioResolutionError, match="content encoding"):
        await resolve_station(STATION, validator=accept)
    assert response.read_chunks == 0


async def test_candidate_validation_has_a_fixed_attempt_limit():
    validated = []
    async def reject(url):
        validated.append(url)
        return False
    entries = [audio(f"https://stream{i}.example.test/live.aac") for i in range(100)]
    with pytest.raises(RadioResolutionError):
        await resolve_station(STATION, fetcher=fetched(payload(*entries)), validator=reject)
    assert len(validated) == module._MAX_CANDIDATES == 16
