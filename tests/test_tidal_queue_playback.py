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
    def session_available(self):
        return self.store.load() == self.record
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
@pytest.mark.parametrize("root_version", ["1.4", "1.10"])
async def test_single_tidal_create_queue_starts_existing_player_without_public_signed_url(api, tmp_path, monkeypatch, root_version):
    api.cfg.enable_plex_service = True  # Root 1.4 has immediate CreateQueue playback.
    if root_version != "1.4":
        capability = {**api._stream_services_capability(), "version": root_version}
        monkeypatch.setattr(api, "_stream_services_capability", lambda: capability)
    resolver = await configured(api, tmp_path)
    context = '<PlayList><ListName>CurrentQueue</ListName><ListInfo><SourceName>Tidal</SourceName><TrackNumber>1</TrackNumber><StartPlayIndex>1</StartPlayIndex></ListInfo><Tracks><Track1><Id>42</Id></Track1></Tracks></PlayList>'
    response = await api.soap(request("CreateQueue", '<QueueContext>'+escape(context)+'</QueueContext>', service="PlayQueue"))
    assert response.status == 200
    assert resolver.calls == [("42", "AUTO")]
    loads = [value for name, value in api.mpv.calls if name == "load"]
    assert len(loads) == 1 and "/proxy/audio?url=" in loads[0]
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
    api.cfg.enable_plex_service = True  # Root 1.4 has immediate CreateQueue playback.
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
    api.cfg.enable_plex_service = True  # Root 1.4 has immediate CreateQueue playback.
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
    api.cfg.enable_plex_service = True  # Root 1.4 has immediate CreateQueue playback.
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
    api.cfg.enable_plex_service = True  # Root 1.4 has immediate CreateQueue playback.
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


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_source", [False, True])
async def test_legacy_create_then_play_index_resolves_and_loads_only_once(queue_api, tmp_path, existing_source):
    api = queue_api
    resolver = await configured(api, tmp_path)
    assert api._stream_services_capability()["version"] == "1.0"
    if existing_source:
        await api._play_url("https://media.example.test/existing.wav")
    before = len(api.mpv.calls)
    assert (await create_explicit(api)).status == 200
    assert resolver.calls == []
    assert api.state.status == "stop"
    assert api.state.queue.index == 2 and api.state.queue.current.id == "43"
    assert all(item._tidal_candidate is None and item._tidal_stream_url == "" for item in api.state.queue.items)
    assert not any(name in {"load", "resume"} for name, _ in api.mpv.calls[before:])
    assert [name for name, _ in api.mpv.calls[before:]].count("stop") == int(existing_source)
    assert (await api.soap(request("PlayQueueWithIndex", "<QueueName>Explicit tracks</QueueName><Index>2</Index>",
                                   service="PlayQueue"))).status == 200
    assert resolver.calls == [("43", "LOSSLESS")]
    assert [name for name, _ in api.mpv.calls[before:]].count("load") == 1
    assert [name for name, _ in api.mpv.calls[before:]].count("stop") == int(existing_source)
    assert_public_selection(api, 2, "43")


@pytest.mark.asyncio
async def test_legacy_unavailable_preparation_preserves_old_queue_without_provider_work(queue_api, tmp_path):
    api = queue_api
    await api._play_url("https://media.example.test/existing.wav")
    resolver = await configured(api, tmp_path)
    resolver.session_available = lambda: False
    previous, state, calls = api.state.queue, asdict(api.state), list(api.mpv.calls)
    assert (await create_explicit(api)).status == 500
    assert api.state.queue is previous and asdict(api.state) == state
    assert api.mpv.calls == calls and resolver.calls == []


@pytest.mark.asyncio
async def test_unshare_after_legacy_preparation_prevents_resolution_and_load(queue_api, tmp_path):
    api = queue_api
    resolver = await configured(api, tmp_path)
    assert (await create_explicit(api)).status == 200
    await api.tidal_account.set_shared(False)
    response = await api.soap(request("PlayQueueWithIndex", "<QueueName>Explicit tracks</QueueName><Index>2</Index>",
                                      service="PlayQueue"))
    assert response.status == 500
    assert resolver.calls == [] and api.mpv.calls == []


@pytest.mark.asyncio
async def test_replace_queue_retains_immediate_playback_for_legacy_root(queue_api, tmp_path):
    api = queue_api
    resolver = await configured(api, tmp_path)
    response = await api.soap(request("ReplaceQueue", "<QueueContext>" + escape(explicit_context()) + "</QueueContext>",
                                      service="PlayQueue"))
    assert response.status == 200 and resolver.calls == [("43", "LOSSLESS")]
    assert [name for name, _ in api.mpv.calls].count("load") == 1
    assert_public_selection(api, 2, "43")


@pytest.mark.asyncio
@pytest.mark.parametrize("track_id,quality", [("0", "LOSSLESS"), ("043", "LOSSLESS"), ("43", "HI_RES")])
async def test_legacy_invalid_selected_identity_or_quality_preserves_old_queue(queue_api, tmp_path, track_id, quality):
    api = queue_api
    await api._play_url("https://media.example.test/existing.wav")
    resolver = await configured(api, tmp_path)
    context = explicit_context().replace("<Id>43</Id>", f"<Id>{track_id}</Id>").replace("LOSSLESS", quality)
    previous, state, calls = api.state.queue, asdict(api.state), list(api.mpv.calls)
    assert (await create_explicit(api, context)).status == 500
    assert api.state.queue is previous and asdict(api.state) == state
    assert resolver.calls == [] and api.mpv.calls == calls


def search_append_context(track_id="45"):
    # WiiM Home 3.6.11 search append: CurrentQueue alias, explicit one-track
    # identity, and a zero XML start; the SOAP StartIndex addresses the old item.
    return ("<PlayList><ListName>CurrentQueue</ListName><ListInfo><SourceName>Tidal</SourceName>"
            "<SearchUrl/><TrackNumber>1</TrackNumber><StartPlayIndex>0</StartPlayIndex>"
            "<requestQuality>LOSSLESS</requestQuality></ListInfo><Tracks><Track1>"
            f"<Source>Tidal</Source><Id>{track_id}</Id><URL/>"
            "<Metadata>&lt;res&gt;https://foreign.example.test/?token=CALLER_SECRET&lt;/res&gt;</Metadata>"
            "</Track1></Tracks></PlayList>")


async def append_search(api, *, context=None, action="playnext", start=2, direction=1, play=1):
    return await api.soap(request("AppendTracksInQueueEx", "<QueueContext>" + escape(
        search_append_context() if context is None else context) + "</QueueContext>"
        f"<Action>{action}</Action><StartIndex>{start}</StartIndex>"
        f"<Direction>{direction}</Direction><Play>{play}</Play>", service="PlayQueue"))


@pytest.mark.asyncio
@pytest.mark.parametrize("appended_id", ["45", "43"])
async def test_search_append_plays_inserted_identity_preserves_old_slots_and_laziness(queue_api, tmp_path, appended_id):
    api = queue_api
    resolver = await configured(api, tmp_path)
    assert (await create_explicit(api)).status == 200
    await api._play_queue_index(2)
    old_items = list(api.state.queue.items)
    old_name = api.state.queue.name
    # The native app's same-source search branch is gated by this play medium.
    info = await api.soap(request("GetInfoEx"))
    assert info.status == 200
    assert ET.fromstring(info.text).findtext(".//PlayMedium") == "SONGLIST-NETWORK"
    assert api.state.as_status_ex("127.0.0.1")["dlna_play_medium"] == "SONGLIST-NETWORK"
    assert "SONGLIST-NETWORK" in api._event_body("AVTransport")
    assert api.state.as_meta_info()["PlayMedium"] == "SONGLIST-NETWORK"
    index_response = await api.soap(request("GetQueueIndex", "<QueueName>CurrentQueue</QueueName>",
                                            service="PlayQueue"))
    assert index_response.status == 200
    index = int(ET.fromstring(index_response.text).findtext(".//CurrentIndex"))
    assert index == 2
    resolver.wait = asyncio.Event()
    calls, previous = list(api.mpv.calls), api.state.queue
    pending = asyncio.create_task(append_search(api, context=search_append_context(appended_id), start=index))
    await asyncio.sleep(0)
    assert api.state.queue is previous and api.mpv.calls == calls
    resolver.wait.set()
    assert (await pending).status == 200
    queue = api.state.queue
    assert queue.name == old_name and queue.index == 3 and queue.count == 4
    assert [item.id for item in queue.items] == ["42", "43", appended_id, "44"]
    assert all(queue.items[index] is old for index, old in zip((0, 1, 3), old_items))
    assert queue.current._tidal_track == (appended_id, "LOSSLESS") and queue.current._tidal_require_shared
    assert resolver.calls == [("43", "LOSSLESS"), (appended_id, "LOSSLESS")]
    assert queue.items[0]._tidal_stream_url == queue.items[3]._tidal_stream_url == ""
    assert [name for name, _ in api.mpv.calls[len(calls):]].count("stop") == 1
    assert [name for name, _ in api.mpv.calls[len(calls):]].count("load") == 1
    public = (api._playqueue_response_fields("BrowseQueue") + serialize_queue_context(queue)
              + json.dumps(api.state.as_status_ex("127.0.0.1")) + api.state.track_metadata
              + avtransport_response_fields("GetPositionInfo", api.state)
              + api._event_body("AVTransport") + api._event_body("PlayQueue"))
    for secret in ("SYNTHETIC_SIGNED", "CALLER_SECRET", "audio.example.test", "foreign.example.test"):
        assert secret not in public
    await api._play_queue_index(4)
    assert api.state.queue.current is old_items[2]
    assert resolver.calls[-1] == ("44", "LOSSLESS")
    assert api._stream_services_capability()["version"] == "1.0"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["generation", "same_name_queue", "share", "session", "resolution_failure"])
async def test_search_append_stale_or_failed_resolution_preserves_current_audio(queue_api, tmp_path, change):
    api = queue_api
    resolver = await configured(api, tmp_path)
    assert (await create_explicit(api)).status == 200
    await api._play_queue_index(2)
    resolver.wait = asyncio.Event()
    pending = asyncio.create_task(append_search(api))
    await asyncio.sleep(0)
    if change == "generation":
        api._accept_playback_command("stop")
    elif change == "same_name_queue":
        api.state.queue = replace(api.state.queue)
    elif change == "share":
        await api.tidal_account.set_shared(False)
    elif change == "session":
        resolver.store.save(replace(resolver.record, access_token="replacement-synthetic-access"))
    else:
        resolver.error = TidalPlaybackError("synthetic failure")
    previous, state, calls = api.state.queue, asdict(api.state), list(api.mpv.calls)
    resolver.wait.set()
    assert (await pending).status == 500
    assert api.state.queue is previous and asdict(api.state) == state
    assert api.mpv.calls == calls
    assert len(resolver.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["share", "session", "generation"])
async def test_search_append_rechecks_owner_after_stop_before_queue_insertion(queue_api, tmp_path, change):
    api = queue_api
    resolver = await configured(api, tmp_path)
    assert (await create_explicit(api)).status == 200
    await api._play_queue_index(2)
    previous = api.state.queue
    stop = api.mpv.stop
    async def revoke_at_stop():
        if change == "share":
            await api.tidal_account.set_shared(False)
        elif change == "session":
            resolver.store.save(replace(resolver.record, access_token="replacement-synthetic-access"))
        else:
            api._accept_playback_command("stop")
        return await stop()
    api.mpv.stop = revoke_at_stop
    calls = list(api.mpv.calls)
    assert (await append_search(api)).status == 500
    assert api.state.queue is previous and previous.count == 3
    assert [name for name, _ in api.mpv.calls[len(calls):]] == ["stop"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["action", "direction", "play", "stale_index", "header_only", "source", "xml_start", "queue_alias", "radio_queue", "unowned_queue", "unshared"])
async def test_unsupported_search_append_fails_before_resolution_or_mutation(queue_api, tmp_path, invalid):
    api = queue_api
    resolver = await configured(api, tmp_path)
    assert (await create_explicit(api)).status == 200
    await api._play_queue_index(2)
    kwargs = {}
    if invalid in {"action", "direction", "play"}:
        kwargs[invalid] = {"action": "create", "direction": 0, "play": 0}[invalid]
    elif invalid == "stale_index":
        kwargs["start"] = 1
    elif invalid == "header_only":
        kwargs["context"] = ('<PlayList><ListName>CurrentQueue</ListName><ListInfo>'
            '<SourceName>Tidal</SourceName><StartPlayIndex>0</StartPlayIndex>'
            '<SearchUrl>https://api.tidal.com/v1/albums/123/tracks</SearchUrl></ListInfo></PlayList>')
    elif invalid == "source":
        kwargs["context"] = search_append_context().replace("Tidal", "newTuneIn")
    elif invalid == "xml_start":
        kwargs["context"] = search_append_context().replace("<StartPlayIndex>0", "<StartPlayIndex>1")
    elif invalid == "queue_alias":
        kwargs["context"] = search_append_context().replace("CurrentQueue", "OtherQueue")
    elif invalid == "radio_queue":
        api.state.queue.source = "newTuneIn"
    elif invalid == "unowned_queue":
        api.state.queue.items[0]._tidal_track = None
    elif invalid == "unshared":
        await api.tidal_account.set_shared(False)
    previous, state, calls, resolutions = api.state.queue, asdict(api.state), list(api.mpv.calls), list(resolver.calls)
    assert (await append_search(api, **kwargs)).status == 500
    assert api.state.queue is previous and asdict(api.state) == state
    assert api.mpv.calls == calls and resolver.calls == resolutions


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["radio", "unowned_tidal", "wrong_alias", "mutation"])
async def test_current_queue_alias_is_only_a_privately_owned_tidal_index_read(queue_api, tmp_path, kind):
    api = queue_api
    resolver = await configured(api, tmp_path)
    assert (await create_explicit(api)).status == 200
    if kind == "radio":
        api.state.queue.source = "newTuneIn"
    elif kind == "unowned_tidal":
        api.state.queue.current._tidal_track = None
    alias = "UnknownQueue" if kind == "wrong_alias" else "CurrentQueue"
    action = "PlayQueueWithIndex" if kind == "mutation" else "GetQueueIndex"
    previous, state, calls = api.state.queue, asdict(api.state), list(api.mpv.calls)
    response = await api.soap(request(action, f"<QueueName>{alias}</QueueName><Index>2</Index>",
                                      service="PlayQueue"))
    assert response.status == 500
    assert api.state.queue is previous and asdict(api.state) == state
    assert api.mpv.calls == calls and resolver.calls == []
    exact = await api.soap(request("GetQueueIndex", f"<QueueName>{api.state.queue.name}</QueueName>",
                                   service="PlayQueue"))
    assert exact.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("station", [False, True])
async def test_non_tidal_queue_medium_projection_remains_unchanged(queue_api, station):
    from fake_linkplay.queue import PlaybackQueue, QueueItem
    api = queue_api
    item = QueueItem("https://media.example.test/stream", source="newTuneIn" if station else "DLNA",
                     stream_type="station" if station else "")
    api.state.queue = PlaybackQueue(items=[item], index=1)
    api._activate_queue_item()
    assert api.state.play_medium == ("RADIO-NETWORK" if station else "THIRD-DLNA")
