"""Offline single-track resolver boundaries; synthetic HTTP, tokens and DNS."""
import asyncio
import base64
from dataclasses import replace
import json
import traceback

import pytest

from fake_linkplay import network_policy
from fake_linkplay import tidal_playback as playback
from fake_linkplay.tidal_session import TidalSession, TidalSessionError, TidalSessionStore

ACCESS = "synthetic-access-secret"
REFRESH = "synthetic-refresh-secret"
SIGNED_URL = "https://audio.example.test/track.flac?token=synthetic-cdn-secret"
TRACK_ID = "1234567"
CATALOGUE_URL = "https://api.tidal.com/v1/tracks/" + TRACK_ID
SESSION = TidalSession(ACCESS, REFRESH, 2000, "42", "DE", "synthetic-client", "tidalapi-device-v0.8.11")
METADATA = {"id": int(TRACK_ID), "title": "A track", "artist": {"name": "An artist"},
            "album": {"title": "An album"}, "duration": 213, "url": "https://ignored.example.test/"}


def manifest(**changes):
    value = {"mimeType": "audio/flac", "codecs": "flac", "encryptionType": "NONE", "urls": [SIGNED_URL]}
    value.update(changes)
    return base64.b64encode(json.dumps(value).encode()).decode()


def stream(**changes):
    value = {"trackId": int(TRACK_ID), "manifestMimeType": "application/vnd.tidal.bts",
             "manifest": manifest(), "audioQuality": "LOSSLESS", "assetPresentation": "FULL"}
    value.update(changes)
    return value


class Response:
    def __init__(self, value=None, *, raw=None, status=200, on_enter=None):
        self.body = json.dumps(value).encode() if raw is None else raw
        self.status = status
        self.content = self
        self.on_enter = on_enter
        self.closed = False
        self.bytes_read = 0

    async def __aenter__(self):
        if self.on_enter:
            self.on_enter()
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def iter_chunked(self, size):
        for start in range(0, len(self.body), size):
            chunk = self.body[start:start + size]
            self.bytes_read += len(chunk)
            yield chunk


@pytest.fixture(autouse=True)
def no_live_http_or_dns(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("resolver tests must not perform live HTTP or DNS")
    monkeypatch.setattr(playback.aiohttp, "ClientSession", forbidden)
    monkeypatch.setattr(network_policy, "ThreadedResolver", forbidden)
    monkeypatch.setattr(playback.time, "time", lambda: 1000)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    store = TidalSessionStore(tmp_path / "private")
    store.save(SESSION)
    responses = [Response(METADATA), Response(stream())]
    requests, clients, dns_requests = [], [], []
    addresses = ["1.1.1.1"]

    class HTTP:
        def __init__(self, **kwargs):
            clients.append(kwargs)
            self.closed = False
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            self.closed = True
        def request(self, method, url, **kwargs):
            requests.append((method, url, kwargs))
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

    class DNS:
        async def resolve(self, host, port, family):
            dns_requests.append((host, port))
            return [{"host": address} for address in addresses]
        async def close(self):
            pass

    monkeypatch.setattr(playback.aiohttp, "ClientSession", HTTP)
    monkeypatch.setattr(network_policy, "ThreadedResolver", DNS)
    return store, responses, requests, clients, dns_requests, addresses


@pytest.mark.asyncio
async def test_explicit_resolution_uses_fixed_origins_and_retains_private_candidate(harness, capsys, caplog):
    store, responses, requests, clients, dns, _ = harness
    before = (store.directory / "session.json").read_bytes()
    seen_responses = list(responses)
    result = await playback.TidalPlaybackResolver(store).resolve_track(TRACK_ID, "LOSSLESS")
    assert result.track_id == TRACK_ID and result.catalogue_url == CATALOGUE_URL
    assert (result.title, result.artist, result.album, result.duration) == ("A track", "An artist", "An album", 213.0)
    assert result.stream_url == SIGNED_URL
    assert (result.content_type, result.codec, result.audio_quality) == ("audio/flac", "flac", "LOSSLESS")
    assert result.asset_presentation == "FULL" and result.session_expires_at == SESSION.expires_at
    assert [call[:2] for call in requests] == [
        ("GET", CATALOGUE_URL), ("GET", CATALOGUE_URL + "/playbackinfopostpaywall")]
    assert requests[0][2]["params"] == {"countryCode": "DE"}
    assert requests[1][2]["params"] == {"countryCode": "DE", "playbackmode": "STREAM",
                                        "audioquality": "LOSSLESS", "assetpresentation": "FULL"}
    assert all(not call[2]["allow_redirects"] for call in requests)
    assert all(call[2]["headers"] == {"Authorization": "Bearer " + ACCESS,
               "Accept": "application/json", "Accept-Encoding": "identity"} for call in requests)
    assert len(clients) == 1 and clients[0]["trust_env"] is False and clients[0]["auto_decompress"] is False
    assert clients[0]["timeout"].total == 15
    assert isinstance(clients[0]["cookie_jar"], playback.aiohttp.DummyCookieJar)
    assert dns == [("audio.example.test", 443)]
    assert all(response.closed for response in seen_responses)
    assert (store.directory / "session.json").read_bytes() == before
    visible = repr(result) + str(capsys.readouterr()) + caplog.text
    assert all(secret not in visible for secret in (ACCESS, REFRESH, SIGNED_URL, TRACK_ID, "An artist"))


@pytest.mark.asyncio
async def test_missing_presentation_and_quality_are_not_invented_from_request(harness):
    store, responses, requests, *_ = harness
    value = stream()
    del value["assetPresentation"]
    del value["audioQuality"]
    responses[1] = Response(value)
    result = await playback.TidalPlaybackResolver(store).resolve_track(TRACK_ID, "AUTO")
    assert result.asset_presentation is None and result.audio_quality == ""
    assert requests[1][2]["params"]["audioquality"] == "HIGH"


@pytest.mark.asyncio
@pytest.mark.parametrize("track_id,quality", [
    ("0", "HIGH"), ("0123", "HIGH"), ("12/3", "HIGH"), ("1?token=x", "HIGH"),
    ("1" * 21, "HIGH"), (123, "HIGH"), (True, "HIGH"), ("123", "HI_RES"),
    ("123", "HI_RES_LOSSLESS"), ("123", "high"), ("123", None),
])
async def test_invalid_selection_never_opens_storage_or_network(tmp_path, track_id, quality):
    store = TidalSessionStore(tmp_path / "never-created")
    resolver = playback.TidalPlaybackResolver(store)
    with pytest.raises(playback.TidalPlaybackError) as caught:
        await resolver.resolve_track(track_id, quality)
    assert caught.value.code == "invalid_selection" and not store.directory.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("session,code", [
    (None, "session_required"), (TidalSession(ACCESS, expires_at=2000, user_id="42", country_code="DE"), "session_required"),
    (replace(SESSION, expires_at=1000), "session_expired"),
    (replace(SESSION, expires_at=None), "session_expired"),
    (replace(SESSION, user_id=None), "session_required"),
    (replace(SESSION, country_code=None), "session_required"),
    (replace(SESSION, access_token="synthetic\nunsafe-token"), "session_required"),
])
async def test_only_unexpired_device_provenance_allows_provider_access(tmp_path, session, code):
    store = TidalSessionStore(tmp_path / "private")
    if session is not None:
        store.save(session)
    with pytest.raises(playback.TidalPlaybackError) as caught:
        await playback.TidalPlaybackResolver(store).resolve_track(TRACK_ID)
    assert caught.value.code == code


@pytest.mark.asyncio
async def test_lease_spans_both_requests_and_dns_and_is_released(harness, monkeypatch):
    store, responses, _, _, _, _ = harness
    other = TidalSessionStore(store.directory)
    checks = []

    def conflicting_operation():
        with pytest.raises(TidalSessionError, match="already in progress"):
            other.clear()
        checks.append(True)

    for response in responses:
        response.on_enter = conflicting_operation
    original = playback.resolve_proxy_target

    async def dns(url, allowed):
        conflicting_operation()
        assert allowed == ()
        return await original(url, allowed)

    monkeypatch.setattr(playback, "resolve_proxy_target", dns)
    await playback.TidalPlaybackResolver(store).resolve_track(TRACK_ID)
    assert len(checks) == 3 and other.clear()


@pytest.mark.asyncio
async def test_busy_storage_does_not_make_provider_requests(harness):
    store, _, requests, *_ = harness
    with store.lease():
        with pytest.raises(playback.TidalPlaybackError) as caught:
            await playback.TidalPlaybackResolver(store).resolve_track(TRACK_ID)
    assert caught.value.code == "storage_unavailable" and not requests


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [0, 1, 2])
async def test_expiry_during_resolution_cannot_return_candidate(harness, monkeypatch, phase):
    store, responses, requests, *_ = harness
    if phase < 2:
        responses[phase].on_enter = lambda: monkeypatch.setattr(playback.time, "time", lambda: 2000)
    else:
        original = playback.resolve_proxy_target
        async def dns(url, allowed):
            monkeypatch.setattr(playback.time, "time", lambda: 2000)
            return await original(url, allowed)
        monkeypatch.setattr(playback, "resolve_proxy_target", dns)
    with pytest.raises(playback.TidalPlaybackError) as caught:
        await playback.TidalPlaybackResolver(store).resolve_track(TRACK_ID)
    assert caught.value.code == "session_expired"
    assert len(requests) == (1 if phase == 0 else 2)
    assert store.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(401, "unauthorized"), (403, "provider_rejected"),
                                         (302, "provider_rejected"), (429, "provider_rejected"), (500, "provider_rejected")])
async def test_http_failures_are_fixed_and_never_redirect_or_refresh(harness, status, code):
    store, responses, requests, *_ = harness
    responses[0] = Response(raw=(ACCESS + SIGNED_URL).encode(), status=status)
    response = responses[0]
    with pytest.raises(playback.TidalPlaybackError) as caught:
        await playback.TidalPlaybackResolver(store).resolve_track(TRACK_ID)
    assert caught.value.code == code and len(requests) == 1 and response.bytes_read == 0
    assert ACCESS not in "".join(traceback.format_exception(caught.value))
    assert SIGNED_URL not in str(caught.value)
    assert store.load() == SESSION


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [playback.aiohttp.ClientError(ACCESS + SIGNED_URL),
                                     OSError(ACCESS + SIGNED_URL), TimeoutError(ACCESS + SIGNED_URL)])
async def test_transport_failures_cannot_echo_secrets(harness, failure):
    store, responses, *_ = harness
    responses[0] = failure
    with pytest.raises(playback.TidalPlaybackError) as caught:
        await playback.TidalPlaybackResolver(store).resolve_track(TRACK_ID)
    assert caught.value.code == "request_failed"
    visible = "".join(traceback.format_exception(caught.value))
    assert ACCESS not in visible and SIGNED_URL not in visible
    assert store.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b'[]', b'{"id":1234567,"id":1}', b'{"id":NaN}', b'\xff',
                                 b'{"id":1234567,"artist":{"name":"a","name":"b"}}',
                                 b" " * (playback.MAX_RESPONSE_BYTES + 1)])
async def test_json_is_bounded_utf8_unique_and_an_object(harness, raw):
    store, responses, requests, *_ = harness
    responses[0] = Response(raw=raw)
    with pytest.raises(playback.TidalPlaybackError) as caught:
        await playback.TidalPlaybackResolver(store).resolve_track(TRACK_ID)
    assert caught.value.code == "invalid_response" and len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"id": 7}, {"id": True}, {"title": "unsafe\ntext"}, {"title": "x" * 513},
    {"title": "\ud800"}, {"duration": -1}, {"duration": True}, {"artist": []},
    {"album": {"title": "\x00"}},
])
async def test_identity_and_display_metadata_fail_closed(harness, changes):
    store, responses, requests, *_ = harness
    responses[0] = Response(dict(METADATA, **changes))
    with pytest.raises(playback.TidalPlaybackError) as caught:
        await playback.TidalPlaybackResolver(store).resolve_track(TRACK_ID)
    assert caught.value.code == "invalid_response" and len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changes,code", [
    ({"trackId": 7}, "invalid_response"),
    ({"assetPresentation": "PREVIEW"}, "unsupported_asset"),
    ({"assetPresentation": None}, "unsupported_asset"),
    ({"playbackMode": "PREVIEW"}, "unsupported_asset"),
    ({"isPreview": True}, "unsupported_asset"),
    ({"preview": True}, "unsupported_asset"),
    ({"manifestMimeType": "application/dash+xml"}, "unsupported_manifest"),
    ({"manifestMimeType": "application/vnd.tidal.bts; charset=utf-8"}, "unsupported_manifest"),
    ({"keyId": "synthetic-key"}, "unsupported_manifest"),
    ({"drm": {"licenseUrl": "https://ignored.example.test"}}, "unsupported_manifest"),
    ({"manifest": "%%%"}, "unsupported_manifest"),
    ({"manifest": "e30=\n"}, "unsupported_manifest"),
    ({"manifest": "e31="}, "unsupported_manifest"),
    ({"manifest": base64.b64encode(b'\xff').decode()}, "unsupported_manifest"),
    ({"manifest": base64.b64encode(b'{"encryptionType":"NONE","encryptionType":"AES"}').decode()}, "unsupported_manifest"),
    ({"manifest": "A" * 50000}, "unsupported_manifest"),
    ({"manifest": manifest(encryptionType="OLD_AES")}, "unsupported_manifest"),
    ({"manifest": manifest(encryptionType=None)}, "unsupported_manifest"),
    ({"manifest": manifest(keyId=None)}, "unsupported_manifest"),
    ({"manifest": manifest(extra={"drmToken": "synthetic-secret"})}, "unsupported_manifest"),
    ({"manifest": manifest(urls=[SIGNED_URL, SIGNED_URL])}, "unsupported_manifest"),
    ({"manifest": manifest(urls=[])}, "unsupported_manifest"),
    ({"manifest": manifest(mimeType="application/dash+xml")}, "unsupported_manifest"),
])
async def test_unsupported_response_cannot_become_audio_candidate(harness, changes, code):
    store, responses, _, _, dns, _ = harness
    responses[1] = Response(stream(**changes))
    with pytest.raises(playback.TidalPlaybackError) as caught:
        await playback.TidalPlaybackResolver(store).resolve_track(TRACK_ID)
    assert caught.value.code == code and not dns


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://audio.example.test/track.flac", "https://audio.example.test:444/track.flac",
    "https://user@audio.example.test/track.flac", "https://audio.example.test/track.flac#fragment",
    "https://127.0.0.1/track.flac", "https://10.0.0.1/track.flac", "https://169.254.169.254/track.flac",
    "https://[::1]/track.flac", "https://2130706433/track.flac", "https://0x7f000001/track.flac",
    "https://audio.example.test/track.mpd", "https://audio.example.test/track.m3u8",
    "https://audio.example.test/track\n.flac", "https://audio.example.test/track%0A.flac",
    "https://audio.example.test:0/track.flac", "https://audio.example.test\\@127.0.0.1/track.flac",
    "https://audio.example.test/" + "x" * 8192, None,
])
async def test_unsafe_or_playlist_urls_never_return(harness, url):
    store, responses, *_ = harness
    responses[1] = Response(stream(manifest=manifest(urls=[url])))
    with pytest.raises(playback.TidalPlaybackError) as caught:
        await playback.TidalPlaybackResolver(store).resolve_track(TRACK_ID)
    assert caught.value.code == "unsafe_destination"


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["10.0.0.1", "127.0.0.1", "169.254.169.254", "::1", "192.168.1.1", "100.64.0.1"])
async def test_any_private_dns_answer_rejects_entire_candidate(harness, address):
    store, _, _, _, _, addresses = harness
    addresses.append(address)
    with pytest.raises(playback.TidalPlaybackError) as caught:
        await playback.TidalPlaybackResolver(store).resolve_track(TRACK_ID)
    assert caught.value.code == "unsafe_destination"


@pytest.mark.asyncio
async def test_cancellation_releases_storage_and_does_not_refresh(harness, monkeypatch):
    store, _, requests, *_ = harness
    entered = asyncio.Event()
    async def dns(url, allowed):
        entered.set()
        await asyncio.Future()
    monkeypatch.setattr(playback, "resolve_proxy_target", dns)
    task = asyncio.create_task(playback.TidalPlaybackResolver(store).resolve_track(TRACK_ID))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(requests) == 2 and store.clear()


@pytest.mark.asyncio
async def test_catalogue_streaming_denial_prevents_playback_request(harness):
    store, responses, requests, _, dns, _ = harness
    responses[0] = Response(dict(METADATA, allowStreaming=False))
    with pytest.raises(playback.TidalPlaybackError) as caught:
        await playback.TidalPlaybackResolver(store).resolve_track(TRACK_ID)
    assert caught.value.code == "provider_rejected"
    assert len(requests) == 1 and not dns


@pytest.mark.asyncio
async def test_candidate_fingerprint_rejects_replaced_or_deleted_session(harness):
    store, _, requests, *_ = harness
    resolver = playback.TidalPlaybackResolver(store)
    candidate = await resolver.resolve_track(TRACK_ID)
    fingerprint = candidate.session_fingerprint
    assert type(fingerprint) is bytes and len(fingerprint) == 32
    assert resolver.session_matches(fingerprint)
    assert repr(fingerprint) not in repr(candidate)
    store.save(replace(SESSION, access_token="synthetic-replacement-access"))
    assert not resolver.session_matches(fingerprint)
    store.save(SESSION)
    assert resolver.session_matches(fingerprint)
    store.clear()
    assert not resolver.session_matches(fingerprint)
    assert len(requests) == 2
