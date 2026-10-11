"""MOCK integration: radio queue SOAP actions resolve IDs, never search URLs.

No provider request, audio device, proprietary app, or account is exercised.
"""
import asyncio
from dataclasses import asdict
from html import escape
from unittest.mock import AsyncMock, Mock
from xml.etree import ElementTree as ET

import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi, ParsedCommand
from fake_linkplay.queue import parse_queue_context
from fake_linkplay.radio import RadioResolution, RadioResolutionError
from fake_linkplay.state import PlaybackPhase, RendererState
from test_playback_generations import StatefulBackend
from test_soap_faults import fault_code, request


DIRECT_URL = "https://media.example.test/station.mp3"
REPLACEMENT_URL = "https://media.example.test/replacement.mp3"
ARTWORK = "https://cdn-profiles.tunein.com/s12345/images/logoq.png?synthetic=1"


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr("fake_linkplay.http_api.ClientSession", Mock(side_effect=AssertionError("Queue metadata must never be fetched")))
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path))
    result = HttpApi(cfg, RendererState.from_config(cfg), StatefulBackend())
    result._wait_for_mpv_audio_ready = AsyncMock(return_value=True)
    result._resolve_station_id = AsyncMock(return_value=RadioResolution(DIRECT_URL, "s12345"))
    result._resolve_playback_url = AsyncMock(side_effect=lambda url: url)
    return result


def header_context(*, source="newTuneIn", stream_id="s12345", search_url="", stream_type="", remote_hints=True):
    stream_field = f"<StreamId>{escape(stream_id)}</StreamId>" if stream_id is not None else ""
    hints = "<TrackNumber>100</TrackNumber><StartPlayIndex>20</StartPlayIndex>" if remote_hints else ""
    return (
        "<PlayList><QueueName>Station queue</QueueName><ListName>Synthetic &amp; Station</ListName>"
        f"<ListInfo><SourceName>{escape(source)}</SourceName>{stream_field}"
        f"<SearchUrl>{escape(search_url)}</SearchUrl><PicUrl>{escape(ARTWORK)}</PicUrl>"
        f"<StreamType>{escape(stream_type)}</StreamType><ContentType>audio</ContentType>"
        f"<ShowName>Provider &amp; show</ShowName>{hints}</ListInfo></PlayList>"
    )


async def create(api, context, action="CreateQueue"):
    return await api.soap(request(action, f"<QueueContext>{escape(context)}</QueueContext>", "PlayQueue"))


async def play(api):
    return await api.soap(request("PlayQueueWithIndex", "<QueueName>Station queue</QueueName><Index>1</Index>", "PlayQueue"))


async def browse(api):
    response = await api.soap(request("BrowseQueue", "<QueueName>Station queue</QueueName>", "PlayQueue"))
    assert response.status == 200
    context = next(node.text for node in ET.fromstring(response.text).iter() if node.tag.rsplit("}", 1)[-1] == "QueueContext")
    return parse_queue_context(context)


def queue_projection(api):
    return {name: value for name, value in vars(api).items() if name.startswith("queue_")}


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["CreateQueue", "ReplaceQueue"])
async def test_stream_id_only_header_materializes_one_real_station(api, action):
    response = await create(api, header_context(), action)
    assert response.status == 200
    api._resolve_station_id.assert_awaited_once_with("s12345")
    assert not api.mpv.calls
    assert api.state.phase == PlaybackPhase.STOPPED
    assert api.state.plicount == api.state.plicurr == 1
    queue = await browse(api)
    assert queue.name == "Station queue"
    assert queue.count == queue.index == 1
    assert queue.stream_id == "s12345"
    assert queue.list_name == "Synthetic & Station"
    assert queue.show_name == "Provider & show"
    assert queue.source == queue.current.source == "newTuneIn"
    assert queue.artwork == queue.current.artwork == ARTWORK
    assert queue.current.url == api.queue_url == api.state.current_url == DIRECT_URL
    assert queue.current.stream_type == "station"
    assert queue.current.duration == api.state.totlen == 0
    assert queue.search_url == ""
    assert api.state.album_art_uri == ARTWORK
    assert "logog.png" not in api.state.track_metadata
    assert "<song:isLive>1</song:isLive>" in api.state.track_metadata

    assert (await play(api)).status == 200
    assert api.mpv.url == DIRECT_URL
    assert api.state.phase == PlaybackPhase.PLAYING
    assert api.state.totlen == 0  # Ignore the backend's synthetic segment duration.
    assert api.state.track_source == "newTuneIn"
    api._resolve_station_id.assert_awaited_once_with("s12345")


@pytest.mark.asyncio
@pytest.mark.parametrize("search_url", [
    "https://media.example.test/decoy.mp3?token=synthetic&station=s99999",
    "http://api.radiotime.com/profiles/s99999/media?partnerId=synthetic&latlon=0,0",
    "https://example.test/opaque?<untrusted>&query=https://elsewhere.example.test/track.mp3",
])
async def test_explicit_station_id_keeps_search_url_opaque(api, search_url):
    assert (await create(api, header_context(search_url=search_url))).status == 200
    api._resolve_station_id.assert_awaited_once_with("s12345")
    assert api.queue_search_url == search_url
    assert (await browse(api)).search_url == search_url
    assert (await play(api)).status == 200
    assert api.mpv.url == DIRECT_URL
    assert all(call.args == (DIRECT_URL,) for call in api._resolve_playback_url.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_profile_fallback_extracts_only_station_id(api, scheme):
    search_url = f"{scheme}://api.radiotime.com/profiles/s12345/media?partnerId=synthetic&id=s99999&redirect=https://example.test/decoy"
    assert (await create(api, header_context(stream_id=None, search_url=search_url))).status == 200
    api._resolve_station_id.assert_awaited_once_with("s12345")
    api._resolve_playback_url.assert_not_awaited()
    assert api.state.queue.current.url == DIRECT_URL
    assert api.state.queue.stream_id == "s12345"
    assert api.state.queue.search_url == search_url


@pytest.mark.asyncio
@pytest.mark.parametrize("source,stream_id,stream_type", [
    ("Plex", "s12345", ""),
    ("tunein", "s12345", ""),
    ("NewTuneIn", "s12345", ""),
    ("", "s12345", ""),
    ("newTuneIn", "t12345", "episode"),
    ("newTuneIn", "s12345", "episode"),
    ("newTuneIn", "S12345", ""),
    ("newTuneIn", "s12/345", ""),
    ("newTuneIn", "s" + "1" * 33, ""),
])
async def test_unsupported_headers_fault_without_mutating_active_playback(api, source, stream_id, stream_type):
    await api.execute(ParsedCommand("play", (REPLACEMENT_URL,)), "192.0.2.1")
    before, projection, calls = asdict(api.state), queue_projection(api), list(api.mpv.calls)
    context = header_context(source=source, stream_id=stream_id, stream_type=stream_type,
                             search_url="http://api.radiotime.com/profiles/s12345/media", remote_hints=bool(source))
    assert fault_code(await create(api, context)) == "402"
    assert asdict(api.state) == before
    assert queue_projection(api) == projection
    assert api.mpv.calls == calls
    api._resolve_station_id.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("search_url", [
    "https://api.radiotime.com.example.test/profiles/s12345/media",
    "https://example.test/profiles/s12345/media",
    "https://api.radiotime.com@media.example.test/profiles/s12345/media",
    "https://user@api.radiotime.com/profiles/s12345/media",
    "https://api.radiotime.com:8443/profiles/s12345/media",
    "https://api.radiotime.com/profiles/s12345/media/extra",
    "https://api.radiotime.com/profiles/%7312345/media",
    "https://api.radiotime.com/profiles/t12345/media?id=s12345",
    "https://api.radiotime.com/search?url=/profiles/s12345/media",
    "https://opml.radiotime.com/Tune.ashx?id=s12345",
    "file:///profiles/s12345/media",
])
async def test_unrecognized_search_url_is_not_a_playable_fallback(api, search_url):
    before = asdict(api.state)
    assert fault_code(await create(api, header_context(stream_id=None, search_url=search_url))) == "402"
    assert asdict(api.state) == before
    assert api.queue_context == ""
    assert not api.mpv.calls
    api._resolve_station_id.assert_not_awaited()
    api._resolve_playback_url.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RadioResolutionError("Synthetic unavailable station"), TimeoutError(), OSError("Synthetic failure")])
async def test_station_resolution_failure_returns_fault_without_fake_success(api, error):
    api._resolve_station_id.side_effect = error
    assert fault_code(await create(api, header_context())) == "501"
    assert api.state.phase == PlaybackPhase.ERROR
    assert api.state.status != "play"
    assert api.state.plicount == api.state.plicurr == 0
    assert api.state.current_url == api.queue_url == ""
    assert api.state.totlen == 0
    assert not api.mpv.calls
    api._resolve_playback_url.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["http", "soap"])
@pytest.mark.parametrize("resolution_fails", [False, True])
async def test_new_source_supersedes_blocked_station_resolution(api, replacement, resolution_fails):
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_resolve(station_id):
        assert station_id == "s12345"
        entered.set()
        await release.wait()
        if resolution_fails:
            raise RadioResolutionError("Synthetic late failure")
        return RadioResolution(DIRECT_URL, station_id)

    api._resolve_station_id.side_effect = blocked_resolve
    pending = asyncio.create_task(create(api, header_context()))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        if replacement == "http":
            assert await api.execute(ParsedCommand("play", (REPLACEMENT_URL,)), "192.0.2.1") == "OK"
        else:
            response = await api.soap(request("SetAVTransportURI", f"<CurrentURI>{REPLACEMENT_URL}</CurrentURI>"))
            assert response.status == 200
            assert (await api.soap(request("Play"))).status == 200
        before, projection, calls = asdict(api.state), queue_projection(api), list(api.mpv.calls)
        release.set()
        assert fault_code(await asyncio.wait_for(pending, timeout=5)) == "501"
        assert asdict(api.state) == before
        assert queue_projection(api) == projection
        assert api.mpv.calls == calls
        assert api.state.queue.current.url == api.mpv.url == REPLACEMENT_URL
        assert api.state.phase == PlaybackPhase.PLAYING
        assert DIRECT_URL not in [value for command, value in api.mpv.calls if command == "load"]
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('stream_id', [None, ''])
async def test_header_without_station_id_or_fallback_is_rejected(api, stream_id):
    before = asdict(api.state)
    response = await create(api, header_context(stream_id=stream_id, remote_hints=False))
    assert fault_code(response) == '402'
    assert asdict(api.state) == before
    api._resolve_station_id.assert_not_awaited()
