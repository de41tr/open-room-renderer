"""Offline one-page catalogue admission; no provider or native-app claim."""
import asyncio
from dataclasses import replace
from html import escape

import pytest

from fake_linkplay import tidal_playback as playback
from fake_linkplay.tidal_account import TidalDeviceAccountBridge
from fake_linkplay.queue import parse_queue_context, serialize_queue_context
from fake_linkplay.tidal_page import parse_page_request
from test_tidal_playback import ACCESS, REFRESH, SESSION, METADATA, Response, harness, no_live_http_or_dns, stream
from test_tidal_queue_playback import queue_api
from test_soap_faults import api, request as soap_request


ALBUM = "https://api.tidal.com/v1/albums/123/tracks"
PLAYLIST = "https://api.tidal.com/v1/playlists/1234-abcd/items"
FAVORITES = "https://api.tidal.com/v1/users/42/favorites/tracks"
NEW_TRACKS = "https://api.tidal.com/v1/featured/new/tracks"


def page(*, playlist=False, ids=(42, 43, 42), total=80):
    items = [{"id": value, "type": "Track", "allowStreaming": True} for value in ids]
    if playlist:
        items = [{"type": "track", "item": value} for value in items]
    return {"offset": 20, "limit": 3, "totalNumberOfItems": total, "items": items}


def favorites_page(**kwargs):
    value = page(**kwargs)
    value["items"] = [{"item": item, "created": "2024-01-01T00:00:00Z"} for item in value["items"]]
    return value


@pytest.mark.asyncio
async def test_rich_favorites_page_has_its_own_bounded_response_budget(harness):
    store, responses, requests, _, dns, _ = harness
    body = favorites_page(ids=range(100, 150), total=50)
    body.update(offset=0, limit=50)
    for entry in body["items"]:
        track = entry["item"]
        track.update(title="A synthetic live recording with the orchestra and guest performers",
                     version="Extended studio session", duration=432,
                     copyright="Synthetic independent recording rights statement. " * 4,
                     artists=[{"id": 1000 + i, "name": "Synthetic guest performer with a long credited ensemble name",
                               "picture": "00000000-0000-4000-8000-000000000000"} for i in range(8)],
                     album={"id": 200, "title": "Synthetic concert collection",
                            "cover": "00000000-0000-4000-8000-000000000000"},
                     url=f"https://tidal.com/browse/track/{track['id']}",
                     audioModes=["STEREO"], mediaMetadata={"tags": ["LOSSLESS"]})
    response = Response(body)
    assert 64 * 1024 < len(response.body) <= 512 * 1024
    responses[:] = [response]
    result = await playback.TidalPlaybackResolver(store).resolve_page(FAVORITES + "?offset=0&limit=50")
    assert result.track_ids == tuple(str(value) for value in range(100, 150))
    assert len(requests) == 1 and requests[0][1] == FAVORITES and dns == []
    assert not requests[0][2]["allow_redirects"] and response.closed
    assert playback.MAX_RESPONSE_BYTES == 64 * 1024


@pytest.mark.asyncio
async def test_own_favorites_page_keeps_requested_order_slots_and_query(harness, caplog):
    store, responses, requests, _, dns, _ = harness
    responses[:] = [Response(favorites_page())]
    url = FAVORITES + "?offset=020&limit=3&countryCode=DE&order=DATE&orderDirection=DESC"
    result = await playback.TidalPlaybackResolver(store).resolve_page(url)
    assert result.catalogue_url == url and result.track_ids == ("42", "43", "42")
    assert playback.TidalPlaybackResolver(store).session_matches(result.session_fingerprint)
    assert len(requests) == 1 and requests[0][1] == FAVORITES and dns == []
    assert requests[0][2]["params"] == {"offset": "020", "limit": "3", "countryCode": "DE",
                                         "order": "DATE", "orderDirection": "DESC"}
    assert not requests[0][2]["allow_redirects"]
    assert ACCESS not in repr(result) + caplog.text and REFRESH not in repr(result) + caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("after_refresh", [False, True])
async def test_favorites_owner_must_match_before_refresh_and_after_refresh(harness, after_refresh):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    store, _, requests, clients, dns, _ = harness
    refresh = SimpleNamespace(refresh_owned=AsyncMock())
    resolver = playback.TidalPlaybackResolver(store, refresh_manager=refresh)
    if after_refresh:
        resolver._session_owned = AsyncMock(return_value=replace(SESSION, user_id="43"))
        url = FAVORITES
    else:
        store.save(replace(SESSION, expires_at=999))
        url = FAVORITES.replace("/users/42/", "/users/43/")
    with pytest.raises(playback.TidalPlaybackError) as error:
        await resolver.resolve_page(url)
    assert error.value.code == "invalid_page_selection"
    assert requests == clients == dns == []
    refresh.refresh_owned.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unwrapped", "null_item", "video", "outer_video", "ineligible"])
async def test_favorites_reject_bad_or_ineligible_slots_without_filtering(harness, kind):
    store, responses, requests, *_ = harness
    body = favorites_page()
    if kind == "unwrapped":
        body["items"][1] = body["items"][1]["item"]
    elif kind == "null_item":
        body["items"][1]["item"] = None
    elif kind == "video":
        body["items"][1]["item"]["type"] = "Video"
    elif kind == "outer_video":
        body["items"][1]["type"] = "video"
        del body["items"][1]["item"]["type"]
    else:
        del body["items"][1]["item"]["allowStreaming"]
    responses[:] = [Response(body)]
    with pytest.raises(playback.TidalPlaybackError) as error:
        await playback.TidalPlaybackResolver(store).resolve_page(FAVORITES + "?offset=20&limit=3")
    assert error.value.code == "unsupported_page" and len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("playlist", [False, True])
async def test_only_requested_page_retains_all_slots_query_and_private_session_binding(harness, playlist, caplog):
    store, responses, requests, clients, dns, _ = harness
    value = page(playlist=playlist)
    response = Response(value)
    responses[:] = [response]
    url = (PLAYLIST if playlist else ALBUM) + "?offset=020&limit=3&countryCode=DE&orderDirection=ASC"
    before = (store.directory / "session.json").read_bytes()
    resolver = playback.TidalPlaybackResolver(store)
    result = await resolver.resolve_page(url)
    assert result.catalogue_url == url and result.track_ids == ("42", "43", "42")
    assert result.session_expires_at == SESSION.expires_at
    assert resolver.session_matches(result.session_fingerprint)
    assert requests == [("GET", PLAYLIST if playlist else ALBUM, {
        "params": {"offset": "020", "limit": "3", "countryCode": "DE", "orderDirection": "ASC"},
        "allow_redirects": False,
        "headers": {"Authorization": "Bearer " + ACCESS, "Accept": "application/json", "Accept-Encoding": "identity"},
    })]
    assert len(clients) == 1 and not clients[0]["trust_env"] and not clients[0]["auto_decompress"]
    assert clients[0]["timeout"].total == 15 and response.closed and dns == []
    assert (store.directory / "session.json").read_bytes() == before
    assert ACCESS not in repr(result) + caplog.text and REFRESH not in repr(result) + caplog.text


@pytest.mark.asyncio
async def test_short_terminal_page_does_not_claim_total_or_request_more(harness):
    store, responses, requests, *_ = harness
    responses[:] = [Response(page(ids=(42, 43), total=22))]
    result = await playback.TidalPlaybackResolver(store).resolve_page(ALBUM + "?offset=20&limit=3")
    assert result.track_ids == ("42", "43") and len(requests) == 1
    assert not hasattr(result, "total")


@pytest.mark.parametrize("url", [
    "http://api.tidal.com/v1/albums/123/tracks", ALBUM + "/", ALBUM + "/playbackinfopostpaywall",
    "https://api.tidal.com/v1/albums/0/tracks", "https://api.tidal.com/v1/albums/01/tracks",
    "https://api.tidal.com/v1/tracks/42", "https://api.tidal.com/v1/artists/42/albums",
    "https://api.tidal.com/v1/playlists/" + "a" * 129 + "/items",
    "https://api.tidal.com/v1/playlists/abc/tracks", ALBUM + "?offset=-1",
    ALBUM + "?offset=1000000000", ALBUM + "?limit=0", ALBUM + "?limit=51",
    ALBUM + "?limit=3&limit=4", ALBUM + "?access_token=private",
    "https://api.tidal.com@foreign.example.test/v1/albums/123/tracks",
    FAVORITES.replace("/42/", "/0/"), FAVORITES.replace("/42/", "/042/"),
    FAVORITES.replace("/42/", "/9223372036854775808/"), FAVORITES + "/",
    FAVORITES.replace("/tracks", "/albums"), FAVORITES + "?limit=51",
    NEW_TRACKS + "/", NEW_TRACKS.replace("/new/", "/top/"),
])
@pytest.mark.asyncio
async def test_invalid_page_request_never_opens_http(harness, url):
    store, _, requests, *_ = harness
    with pytest.raises(playback.TidalPlaybackError) as error:
        await playback.TidalPlaybackResolver(store).resolve_page(url)
    assert error.value.code == "invalid_page_selection" and requests == []


def test_missing_paging_parameters_have_bounded_single_page_defaults():
    request = parse_page_request(ALBUM)
    assert request.offset == 0 and request.limit == 50
    assert request.catalogue_url == ALBUM


@pytest.mark.parametrize("field,value", [
    ("offset", True), ("offset", 21), ("offset", 20.0), ("offset", None),
    ("limit", True), ("limit", 4), ("limit", "3"),
    ("totalNumberOfItems", False), ("totalNumberOfItems", 19),
    ("totalNumberOfItems", 1_000_000_001), ("totalNumberOfItems", "80"),
    ("items", []), ("items", {}), ("items", [{"id": 42, "allowStreaming": True}]),
])
@pytest.mark.asyncio
async def test_inconsistent_provider_page_is_rejected_without_retry(harness, field, value):
    store, responses, requests, *_ = harness
    body = page()
    body[field] = value
    responses[:] = [Response(body)]
    with pytest.raises(playback.TidalPlaybackError) as error:
        await playback.TidalPlaybackResolver(store).resolve_page(ALBUM + "?offset=20&limit=3")
    assert error.value.code == "invalid_page_response" and len(requests) == 1


@pytest.mark.parametrize("playlist", [False, True])
@pytest.mark.parametrize("eligibility", ["missing", None, False, 0, 1, "true"])
@pytest.mark.asyncio
async def test_every_raw_entry_must_be_explicitly_streamable_no_filtering(harness, playlist, eligibility):
    store, responses, requests, *_ = harness
    body = page(playlist=playlist)
    entry = body["items"][2]["item"] if playlist else body["items"][2]
    if eligibility == "missing":
        del entry["allowStreaming"]
    else:
        entry["allowStreaming"] = eligibility
    responses[:] = [Response(body)]
    url = (PLAYLIST if playlist else ALBUM) + "?offset=20&limit=3"
    with pytest.raises(playback.TidalPlaybackError) as error:
        await playback.TidalPlaybackResolver(store).resolve_page(url)
    assert error.value.code == "unsupported_page" and len(requests) == 1


@pytest.mark.parametrize("kind", ["album_video", "playlist_video", "nested_video", "missing_type", "null_type", "unwrapped"])
@pytest.mark.asyncio
async def test_mixed_or_ambiguous_media_is_unsupported_even_when_streamable(harness, kind):
    store, responses, _, *_ = harness
    playlist = kind != "album_video"
    body = page(playlist=playlist)
    if kind == "album_video":
        body["items"][1]["type"] = "Video"
    elif kind == "playlist_video":
        body["items"][1]["type"] = "video"
    elif kind == "nested_video":
        body["items"][1]["item"]["type"] = "Video"
    elif kind == "missing_type":
        del body["items"][1]["type"]
    elif kind == "null_type":
        body["items"][1]["type"] = None
    else:
        body["items"][1] = body["items"][1]["item"]
    responses[:] = [Response(body)]
    with pytest.raises(playback.TidalPlaybackError) as error:
        await playback.TidalPlaybackResolver(store).resolve_page((PLAYLIST if playlist else ALBUM) + "?offset=20&limit=3")
    assert error.value.code == "unsupported_page"


@pytest.mark.parametrize("track_id", [None, True, 0, -1, "01", "٤٢", "1" * 21, 1.0, {}, "https://foreign.example.test/"])
@pytest.mark.asyncio
async def test_page_ids_are_numeric_bounded_and_never_urls(harness, track_id):
    store, responses, _, *_ = harness
    body = page()
    body["items"][1]["id"] = track_id
    responses[:] = [Response(body)]
    with pytest.raises(playback.TidalPlaybackError) as error:
        await playback.TidalPlaybackResolver(store).resolve_page(ALBUM + "?offset=20&limit=3")
    assert error.value.code == "invalid_page_response"


@pytest.mark.parametrize("failure", ["redirect", "unauthorized", "oversized", "duplicate_json", "expired", "country", "lease"])
@pytest.mark.asyncio
async def test_page_reuses_bounded_reader_and_private_lease_without_refresh(harness, failure, monkeypatch):
    store, responses, requests, _, dns, _ = harness
    expected = {"redirect": "provider_rejected", "unauthorized": "unauthorized",
                "oversized": "invalid_response", "duplicate_json": "invalid_page_response",
                "expired": "session_expired", "country": "invalid_page_selection", "lease": "storage_unavailable"}[failure]
    body = page()
    response = Response(body)
    if failure in {"redirect", "unauthorized"}:
        response = Response({}, status=302 if failure == "redirect" else 401)
    elif failure == "oversized":
        body["ignored"] = "x" * (playback.MAX_PAGE_RESPONSE_BYTES + 16384)
        response = Response(body)
    elif failure == "duplicate_json":
        response = Response(raw=b'{"items": [], "items": []}')
    elif failure == "expired":
        response = Response(body, on_enter=lambda: monkeypatch.setattr(playback.time, "time", lambda: 2001))
    responses[:] = [response]
    url = ALBUM + "?offset=20&limit=3" + ("&countryCode=US" if failure == "country" else "")
    resolver = playback.TidalPlaybackResolver(store)
    if failure == "lease":
        with store.lease():
            with pytest.raises(playback.TidalPlaybackError) as error:
                await resolver.resolve_page(url)
    else:
        with pytest.raises(playback.TidalPlaybackError) as error:
            await resolver.resolve_page(url)
    assert error.value.code == expected
    assert len(requests) == (0 if failure in {"country", "lease"} else 1) and dns == []
    if requests:
        assert requests[0][2]["allow_redirects"] is False and response.closed
    if failure == "oversized":
        assert playback.MAX_PAGE_RESPONSE_BYTES < response.bytes_read <= playback.MAX_PAGE_RESPONSE_BYTES + 8192


# The real page/track resolver is wired into the existing mock playback owner.
def header_context(url, start=2):
    return ("<PlayList><ListName>One loaded page</ListName><ListInfo><SourceName>Tidal</SourceName>"
            f"<SearchUrl>{escape(url)}</SearchUrl><TrackNumber>999</TrackNumber>"
            f"<StartPlayIndex>{start}</StartPlayIndex></ListInfo><Tracks/></PlayList>")


def header_request(url, start=2):
    return soap_request("CreateQueue", "<QueueContext>" + escape(header_context(url, start)) + "</QueueContext>", service="PlayQueue")


async def connect_page_resolver(api, store):
    api.tidal_account = TidalDeviceAccountBridge(store)
    await api.tidal_account.set_shared(True)
    api.tidal_playback = playback.TidalPlaybackResolver(store)


def audio_responses(track_id):
    return [Response({**METADATA, "id": track_id}), Response(stream(trackId=track_id))]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", [ALBUM, PLAYLIST, FAVORITES, NEW_TRACKS])
async def test_header_page_uses_local_index_reports_loaded_size_and_ends_there(queue_api, harness, endpoint):
    api = queue_api
    api.cfg.enable_plex_service = True  # Root 1.4 has immediate CreateQueue playback.
    store, responses, requests, *_ = harness
    await connect_page_resolver(api, store)
    url = endpoint + "?offset=20&limit=3&countryCode=DE"
    body = (favorites_page(ids=(42, 43), total=22) if endpoint == FAVORITES
            else page(playlist=endpoint == PLAYLIST, ids=(42, 43), total=22))
    responses[:] = [Response(body), *audio_responses(43)]
    assert (await api.soap(header_request(url))).status == 200
    assert api.state.queue.count == 2 and api.state.queue.index == 2
    assert api.state.queue.search_url == url and api.state.queue.current.id == "43"
    assert [entry.id for entry in api.state.queue.items] == ["42", "43"]
    public = serialize_queue_context(api.state.queue)
    parsed = parse_queue_context(public)
    assert parsed.search_url == url and parsed.count == 2 and parsed.index == 2
    assert "synthetic-cdn-secret" not in public and "audio.example.test" not in public
    assert [call[1] for call in requests] == [
        endpoint,
        "https://api.tidal.com/v1/tracks/43",
        "https://api.tidal.com/v1/tracks/43/playbackinfopostpaywall",
    ]
    calls = list(api.mpv.calls)
    assert (await api.soap(soap_request("Next"))).status == 500
    assert len(requests) == 3 and api.mpv.calls == calls and api.state.queue.index == 2
    responses[:] = audio_responses(42)
    assert (await api.soap(soap_request("Previous"))).status == 200
    assert api.state.queue.index == 1 and api.state.queue.current.id == "42"
    assert [call[1] for call in requests[3:]] == [
        "https://api.tidal.com/v1/tracks/42",
        "https://api.tidal.com/v1/tracks/42/playbackinfopostpaywall",
    ]
    assert api.state.queue.search_url == url and api.state.queue.count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["mixed", "ineligible", "local_index", "request_index", "session_change", "unshared"])
async def test_failed_page_admission_preserves_existing_playing_queue(queue_api, harness, failure):
    api = queue_api
    store, responses, requests, *_ = harness
    await connect_page_resolver(api, store)
    await api._play_url("https://media.example.test/existing.wav")
    previous, calls = api.state.queue, list(api.mpv.calls)
    before = serialize_queue_context(previous)
    body = page(ids=(42, 43), total=22)
    if failure == "mixed":
        body["items"][1]["type"] = "Video"
    elif failure == "ineligible":
        del body["items"][1]["allowStreaming"]
    responses[:] = [Response(body)]
    if failure == "unshared":
        await api.tidal_account.set_shared(False)
    if failure == "session_change":
        resolve = api.tidal_playback.resolve_page
        async def replaced_session(url):
            result = await resolve(url)
            store.save(replace(SESSION, access_token="synthetic-new-session"))
            return result
        api.tidal_playback.resolve_page = replaced_session
    start = 3 if failure == "local_index" else 4 if failure == "request_index" else 2
    assert (await api.soap(header_request(ALBUM + "?offset=20&limit=3", start))).status == 500
    assert api.state.queue is previous and serialize_queue_context(previous) == before
    assert api.mpv.calls == calls and api.state.status == "play"
    assert len(requests) == (0 if failure in {"request_index", "unshared"} else 1)


@pytest.mark.asyncio
async def test_page_and_selected_track_must_use_identical_session_even_if_new_one_is_shared(queue_api, harness):
    api = queue_api
    api.cfg.enable_plex_service = True  # Root 1.4 has immediate CreateQueue playback.
    store, responses, requests, *_ = harness
    await connect_page_resolver(api, store)
    await api._play_url("https://media.example.test/existing.wav")
    previous, calls = api.state.queue, list(api.mpv.calls)
    responses[:] = [Response(page()), *audio_responses(43)]
    resolve = api.tidal_playback.resolve_track
    async def new_shared_session(track_id, quality):
        store.save(replace(SESSION, access_token="synthetic-new-session"))
        await api.tidal_account.set_shared(True)
        return await resolve(track_id, quality)
    api.tidal_playback.resolve_track = new_shared_session
    assert (await api.soap(header_request(ALBUM + "?offset=20&limit=3"))).status == 500
    assert len(requests) == 3 and api.tidal_account.shared
    assert api.state.queue is previous and api.mpv.calls == calls and api.state.status == "play"


@pytest.mark.asyncio
async def test_newer_command_supersedes_pending_page_before_any_track_resolution(queue_api, harness):
    api = queue_api
    store, responses, requests, *_ = harness
    await connect_page_resolver(api, store)
    responses[:] = [Response(page())]
    resolve = api.tidal_playback.resolve_page
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed_page(url):
        result = await resolve(url)
        entered.set()
        await release.wait()
        return result
    api.tidal_playback.resolve_page = delayed_page
    previous = api.state.queue
    creating = asyncio.create_task(api.soap(header_request(ALBUM + "?offset=20&limit=3")))
    await entered.wait()
    assert creating in api._tidal_pending_resolutions
    api._accept_playback_command("stop")
    release.set()
    assert (await creating).status == 500
    assert len(requests) == 1 and api.mpv.calls == [] and api.state.queue is previous
    assert not api._tidal_pending_resolutions


@pytest.mark.asyncio
async def test_natural_eof_at_loaded_page_end_never_schedules_more_media(queue_api, harness):
    api = queue_api
    api.cfg.enable_plex_service = True  # Root 1.4 has immediate CreateQueue playback.
    store, responses, requests, *_ = harness
    await connect_page_resolver(api, store)
    responses[:] = [Response(page(ids=(42, 43), total=22)), *audio_responses(43)]
    assert (await api.soap(header_request(ALBUM + "?offset=20&limit=3"))).status == 200
    api.mpv.idle, api.mpv.url = True, ""
    await api.sync_mpv_state()
    assert api.state.status == "stop" and api.state.queue.index == api.state.queue.count == 2
    assert api._queue_advance_task is None and len(requests) == 3
    assert len([call for call in api.mpv.calls if call[0] == "load"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("favorites", [False, True])
async def test_legacy_header_create_materializes_only_page_until_play_index(queue_api, harness, favorites):
    api = queue_api
    store, responses, requests, *_ = harness
    await connect_page_resolver(api, store)
    endpoint = FAVORITES if favorites else ALBUM
    url = endpoint + "?offset=20&limit=3&order=DATE&orderDirection=DESC"
    responses[:] = [Response((favorites_page if favorites else page)(ids=(42, 43), total=22))]
    assert (await api.soap(header_request(url))).status == 200
    assert [call[1] for call in requests] == [endpoint]
    assert api.mpv.calls == [] and api.state.status == "stop"
    assert api.state.queue.count == 2 and api.state.queue.index == 2
    assert api.state.queue.search_url == url and api.state.queue._tidal_prepared_page is not None
    public = serialize_queue_context(api.state.queue)
    assert "session_fingerprint" not in public and "synthetic-cdn-secret" not in public
    responses[:] = audio_responses(43)
    assert (await api.soap(soap_request("PlayQueueWithIndex", "<Index>2</Index>", service="PlayQueue"))).status == 200
    assert [call[1] for call in requests] == [endpoint, "https://api.tidal.com/v1/tracks/43",
                                            "https://api.tidal.com/v1/tracks/43/playbackinfopostpaywall"]
    assert [name for name, _ in api.mpv.calls].count("load") == 1
    assert not any(name == "stop" for name, _ in api.mpv.calls)
    assert api.state.queue._tidal_prepared_page is None


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["before_play", "during_resolution"])
async def test_legacy_prepared_page_rejects_session_change_before_first_load(queue_api, harness, when):
    api = queue_api
    store, responses, requests, *_ = harness
    await connect_page_resolver(api, store)
    responses[:] = [Response(page())]
    assert (await api.soap(header_request(ALBUM + "?offset=20&limit=3"))).status == 200
    async def change_session():
        store.save(replace(SESSION, access_token="synthetic-new-session"))
        await api.tidal_account.set_shared(True)
    if when == "before_play":
        await change_session()
    else:
        resolve = api.tidal_playback.resolve_track
        async def changed_session(track_id, quality):
            await change_session()
            return await resolve(track_id, quality)
        api.tidal_playback.resolve_track = changed_session
        responses[:] = audio_responses(43)
    assert (await api.soap(soap_request("PlayQueueWithIndex", "<Index>2</Index>", service="PlayQueue"))).status == 500
    assert len(requests) == (1 if when == "before_play" else 3)
    assert api.mpv.calls == [] and api.state.queue._tidal_prepared_page is not None
