"""Synthetic explicit-track bridge through real queue/load ownership; no provider/audio proof."""
import asyncio
from dataclasses import asdict, replace
from html import escape
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree as ET

import pytest

from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.tidal_account import TidalDeviceAccountBridge
from fake_linkplay.tidal_session import TidalSession, TidalSessionStore
from fake_linkplay.upnp_description import avtransport_response_fields
from fake_linkplay.queue import parse_queue_context, serialize_queue_context
from fake_linkplay.tidal_playback import TidalPlaybackError
from test_playback_generations import StatefulBackend
from test_native_queue_staging import NativeBackend
from test_soap_faults import api, request


class Resolver:
    def __init__(self):
        self.calls = []
        self.wait = None
        self.error = None
    def session_matches(self, fingerprint):
        return fingerprint == b"synthetic-session-binding" and self.store.load() == self.record
    async def resolve_track(self, track_id, quality="HIGH"):
        self.calls.append((track_id, quality))
        if self.wait is not None:
            await self.wait.wait()
        if self.error is not None:
            raise self.error
        return SimpleNamespace(track_id=track_id, catalogue_url=f"https://api.tidal.com/v1/tracks/{track_id}",
                               title=f"Synthetic track {track_id}", artist=f"Synthetic artist {track_id}",
                               album=f"Synthetic album {track_id}", duration=float(track_id) + 100,
                               session_fingerprint=b"synthetic-session-binding", session_expires_at=time.time()+600,
                               content_type="audio/flac", stream_url=f"https://audio.example.test/media/{track_id}?token=SYNTHETIC_SIGNED")


async def configured(api, tmp_path):
    store = TidalSessionStore(tmp_path / "private-playback")
    store.save(TidalSession("synthetic-access", "synthetic-refresh", time.time()+600, "42", "DE",
                            client_id="synthetic-client", auth_flow="tidalapi-device-v0.8.11"))
    api.tidal_account = TidalDeviceAccountBridge(store)
    await api.tidal_account.set_shared(True)
    api.tidal_playback = Resolver()
    api.tidal_playback.store, api.tidal_playback.record = store, store.load()
    return api.tidal_playback


@pytest.mark.asyncio
async def test_single_tidal_create_queue_starts_existing_player_without_public_signed_url(api, tmp_path):
    resolver = await configured(api, tmp_path)
    context = '<PlayList><ListName>CurrentQueue</ListName><ListInfo><SourceName>Tidal</SourceName><TrackNumber>1</TrackNumber><StartPlayIndex>1</StartPlayIndex></ListInfo><Tracks><Track1><Id>42</Id></Track1></Tracks></PlayList>'
    response = await api.soap(request("CreateQueue", '<QueueContext>'+escape(context)+'</QueueContext>', service="PlayQueue"))
    assert response.status == 200
    assert resolver.calls == [("42", "AUTO")]
    assert any(name == "load" and "/proxy/audio?url=" in value for name, value in api.mpv.calls)
    assert api.state.queue.source == "Tidal" and api.state.status == "play"
    assert api.state.queue.current._tidal_stream_url.endswith("SYNTHETIC_SIGNED")
    assert api._proxy_source_allowed(api.state.queue.current._tidal_stream_url)
    public = json.dumps(api.state.as_status_ex("127.0.0.1")) + serialize_queue_context(api.state.queue)
    public += avtransport_response_fields("GetPositionInfo", api.state)
    assert "SYNTHETIC_SIGNED" not in public and "audio.example.test" not in public
    assert "Tidal" not in api.state.as_status_ex("127.0.0.1")["supported_services"]


@pytest.mark.asyncio
async def test_resolution_cannot_replace_a_newer_command_or_unshared_account(api, tmp_path):
    resolver = await configured(api, tmp_path)
    resolver.wait = asyncio.Event()
    previous = api.state.queue
    task = asyncio.create_task(api._play_tidal_track("42"))
    await asyncio.sleep(0)
    api._accept_playback_command("stop")
    resolver.wait.set()
    with pytest.raises(BackendError):
        await task
    assert api.state.queue is previous and not api.mpv.calls
    await api.tidal_account.set_shared(False)
    with pytest.raises(BackendError):
        await api._play_tidal_track("42", require_shared=True)
    assert len(resolver.calls) == 1


@pytest.mark.asyncio
async def test_tidal_proxy_ignores_lan_allowlist_and_rejects_redirects(api, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, Mock
    import fake_linkplay.http_api as module
    from fake_linkplay.network_policy import NetworkPolicyError, ProxyTarget
    await configured(api, tmp_path)
    await api.tidal_account.set_shared(False)
    await api._play_tidal_track("42")
    assert not api.tidal_account.shared
    item = api.state.queue.current
    api.cfg.proxy_allowed_lan_hosts = ("127.0.0.1",)
    item._tidal_stream_url = "https://127.0.0.1/private"
    with pytest.raises(NetworkPolicyError):
        await api._open_proxy_source(item._tidal_stream_url, {})
    item._tidal_stream_url = "https://audio.example.test/media?token=SYNTHETIC_SIGNED"
    target = ProxyTarget(item._tidal_stream_url, "audio.example.test", 443, ("1.1.1.1",))
    resolve = AsyncMock(return_value=target)
    monkeypatch.setattr(module, "resolve_proxy_target", resolve)
    monkeypatch.setattr(module, "TCPConnector", lambda **kwargs: None)
    upstream = SimpleNamespace(status=302, headers={"Location": "https://other.example.test/"}, release=Mock())
    client = SimpleNamespace(get=AsyncMock(return_value=upstream), close=AsyncMock())
    factory = Mock(return_value=client)
    monkeypatch.setattr(module, "ClientSession", factory)
    with pytest.raises(NetworkPolicyError):
        await api._open_proxy_source(item._tidal_stream_url, {"User-Agent": "OpenRoomRenderer/0.1"})
    resolve.assert_awaited_once_with(item._tidal_stream_url, ())
    client.get.assert_awaited_once_with(item._tidal_stream_url, allow_redirects=False)
    assert "Authorization" not in factory.call_args.kwargs["headers"]
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_cleared_while_backend_lock_waits_prevents_new_load(api, tmp_path):
    resolver = await configured(api, tmp_path)
    await api.tidal_account.set_shared(False)
    await api._backend_lock.acquire()
    task = asyncio.create_task(api._play_tidal_track("42"))
    await asyncio.sleep(0)
    resolver.store.clear()
    api._backend_lock.release()
    with pytest.raises(BackendError):
        await task
    assert not any(name == "load" for name, _ in api.mpv.calls)


@pytest.fixture
def queue_api(api):
    api.mpv = StatefulBackend()
    return api


def explicit_context(*, start=2, count=3):
    # Valid catalogue hints and opaque metadata do not choose media or display text.
    tracks = "".join(
        f"<Track{index}><Id>{track_id}</Id><Title>Untrusted title</Title>"
        "<URL>https://api.tidal.com/v1/albums/999/tracks</URL>"
        "<Metadata>&lt;res&gt;https://foreign.example.test/?token=CALLER_SECRET&lt;/res&gt;</Metadata>"
        f"</Track{index}>" for index, track_id in enumerate(("42", "43", "44"), 1))
    return ("<PlayList><ListName>Explicit tracks</ListName><ListInfo><SourceName>Tidal</SourceName>"
            f"<TrackNumber>{count}</TrackNumber><StartPlayIndex>{start}</StartPlayIndex>"
            f"<requestQuality>LOSSLESS</requestQuality></ListInfo><Tracks>{tracks}</Tracks></PlayList>")


async def create_explicit(api, context=None):
    return await api.soap(request("CreateQueue", "<QueueContext>" + escape(
        context if context is not None else explicit_context()) + "</QueueContext>", service="PlayQueue"))


def assert_public_selection(api, index, track_id):
    assert api.state.queue.index == api.state.plicurr == index
    assert api.state.queue.count == api.state.plicount == 3
    assert api.state.current_url == f"https://api.tidal.com/v1/tracks/{track_id}"
    assert parse_qs(urlsplit(api.mpv.url).query)["url"] == [
        f"https://audio.example.test/media/{track_id}?token=SYNTHETIC_SIGNED"]
    assert (api.state.title, api.state.artist, api.state.album, api.state.totlen) == (
        f"Synthetic track {track_id}", f"Synthetic artist {track_id}",
        f"Synthetic album {track_id}", int(track_id) + 100)
    assert api.state.queue.current.content_type == api.queue_content_type == "audio/flac"
    assert api.state.queue.current.resolved_url == api.state.resolved_url == api.queue_resolved_url == ""
    browse = api._playqueue_response_fields("BrowseQueue")
    queue = parse_queue_context(ET.fromstring("<Root>" + browse + "</Root>").findtext("QueueContext"))
    assert queue.count == 3 and queue.index == index and queue.quality == "LOSSLESS"
    assert [item.url for item in queue.items] == [f"https://api.tidal.com/v1/tracks/{value}" for value in (42, 43, 44)]
    public = (browse + serialize_queue_context(api.state.queue) + api.state.track_metadata
              + json.dumps(api.state.as_status_ex("127.0.0.1"))
              + avtransport_response_fields("GetPositionInfo", api.state)
              + api._event_body("AVTransport") + api._event_body("PlayQueue"))
    for private in ("SYNTHETIC_SIGNED", "audio.example.test", "CALLER_SECRET", "foreign.example.test", "Untrusted title"):
        assert private not in public


@pytest.mark.asyncio
@pytest.mark.parametrize("native_staging", [False, True])
async def test_explicit_queue_resolves_start_before_replacing_and_lazily_navigates(queue_api, tmp_path, native_staging):
    api = queue_api
    api.cfg.native_queue_staging = native_staging
    if native_staging:
        api.mpv = NativeBackend()
    await api._play_url("https://media.example.test/existing.wav")
    resolver = await configured(api, tmp_path)
    resolver.wait = asyncio.Event()
    previous, calls = api.state.queue, list(api.mpv.calls)
    create = asyncio.create_task(create_explicit(api))
    await asyncio.sleep(0)
    assert resolver.calls == [("43", "LOSSLESS")]
    assert api.state.queue is previous and api.mpv.calls == calls
    resolver.wait.set()
    assert (await create).status == 200
    assert_public_selection(api, 2, "43")
    for item in (api.state.queue.items[0], api.state.queue.items[2]):
        assert item.title == f"TIDAL track {item.id}"
        assert item.title_is_fallback
        assert (item.artist, item.album, item.content_type, item.metadata) == ("", "", "", "")
        assert item.duration == 0 and item._tidal_stream_url == ""
    for action, args, service, index, track_id in (
        ("Next", "", "AVTransport", 3, "44"),
        ("Previous", "", "AVTransport", 2, "43"),
        ("PlayQueueWithIndex", "<QueueName>Explicit tracks</QueueName><Index>1</Index>", "PlayQueue", 1, "42"),
    ):
        assert (await api.soap(request(action, args, service=service))).status == 200
        assert_public_selection(api, index, track_id)
        assert resolver.calls[-1] == (track_id, "LOSSLESS")
    assert resolver.calls == [(value, "LOSSLESS") for value in ("43", "44", "43", "42")]
    assert len([value for name, value in api.mpv.calls if name == "load"]) == 5
    assert not any(name == "append" for name, _ in api.mpv.calls)
    assert "Tidal" not in api.state.as_status_ex("127.0.0.1")["supported_services"]


@pytest.mark.asyncio
@pytest.mark.parametrize("context", [
    explicit_context(start=0), explicit_context(start=4), explicit_context(count=2),
    explicit_context().replace("<Id>43</Id>", "<Id>invalid</Id>"),
    explicit_context().replace("https://api.tidal.com/v1/albums/999/tracks", "https://foreign.example.test/audio"),
    "<PlayList><ListInfo><SourceName>Tidal</SourceName>"
    "<SearchUrl>https://api.tidal.com/v1/artists/999/albums</SearchUrl>"
    "<StartPlayIndex>2</StartPlayIndex></ListInfo><Tracks/></PlayList>",
])
async def test_invalid_or_unsupported_header_selection_preserves_playing_queue_without_resolution(queue_api, tmp_path, context):
    api = queue_api
    await api._play_url("https://media.example.test/existing.wav")
    resolver = await configured(api, tmp_path)
    previous, state, calls = api.state.queue, asdict(api.state), list(api.mpv.calls)
    assert (await create_explicit(api, context)).status == 500
    assert api.state.queue is previous and asdict(api.state) == state
    assert api.mpv.calls == calls and resolver.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("sharing", ["adopted_unshared", "disabled", "session_replaced"])
async def test_unshared_explicit_selection_preserves_playing_queue_without_resolution(queue_api, tmp_path, sharing):
    api = queue_api
    await api._play_url("https://media.example.test/existing.wav")
    resolver = await configured(api, tmp_path)
    if sharing == "adopted_unshared":
        api.tidal_account = TidalDeviceAccountBridge(resolver.store)
    elif sharing == "disabled":
        await api.tidal_account.set_shared(False)
    else:
        resolver.store.save(replace(resolver.record, access_token="replacement-synthetic-access"))
    previous, state, calls = api.state.queue, asdict(api.state), list(api.mpv.calls)
    assert (await create_explicit(api)).status == 500
    assert api.state.queue is previous and asdict(api.state) == state
    assert api.mpv.calls == calls and resolver.calls == []


@pytest.mark.asyncio
async def test_failed_start_resolution_preserves_existing_queue(queue_api, tmp_path):
    api = queue_api
    await api._play_url("https://media.example.test/existing.wav")
    resolver = await configured(api, tmp_path)
    resolver.error = TidalPlaybackError("provider_rejected")
    previous, state, calls = api.state.queue, asdict(api.state), list(api.mpv.calls)
    assert (await create_explicit(api)).status == 500
    assert resolver.calls == [("43", "LOSSLESS")]
    assert api.state.queue is previous and asdict(api.state) == state and api.mpv.calls == calls


@pytest.mark.asyncio
async def test_stop_supersedes_lazy_resolution_without_applying_metadata_or_loading(queue_api, tmp_path):
    api = queue_api
    resolver = await configured(api, tmp_path)
    assert (await create_explicit(api)).status == 200
    resolver.wait = asyncio.Event()
    advance = asyncio.create_task(api._play_queue_index(3))
    await asyncio.sleep(0)
    assert resolver.calls[-1] == ("44", "LOSSLESS")
    await api._transport_command("stop")
    state, calls = asdict(api.state), list(api.mpv.calls)
    resolver.wait.set()
    with pytest.raises(BackendError):
        await advance
    assert asdict(api.state) == state and api.mpv.calls == calls
    assert api.state.queue.current.title == "TIDAL track 44"
    assert api.state.queue.current._tidal_stream_url == ""


@pytest.mark.asyncio
async def test_session_replaced_while_lazy_selection_waits_for_backend_prevents_load(queue_api, tmp_path):
    api = queue_api
    resolver = await configured(api, tmp_path)
    assert (await create_explicit(api)).status == 200
    resolved = asyncio.Event()
    resolve = resolver.resolve_track

    async def hold_backend(track_id, quality):
        candidate = await resolve(track_id, quality)
        await api._backend_lock.acquire()
        resolved.set()
        return candidate

    resolver.resolve_track = hold_backend
    advance = asyncio.create_task(api._play_queue_index(3))
    await resolved.wait()
    resolver.store.save(replace(resolver.record, access_token="replacement-synthetic-access"))
    api._backend_lock.release()
    with pytest.raises(BackendError):
        await advance
    assert resolver.calls == [("43", "LOSSLESS"), ("44", "LOSSLESS")]
    assert len([value for name, value in api.mpv.calls if name == "load"]) == 1
