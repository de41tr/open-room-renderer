"""INTEGRATION + MOCK audio: synthetic music metadata over loopback HTTP.

HTTP, SOAP framing, GENA subscriptions/callbacks and XML parsing are real.
The backend only stores source strings; it never fetches media or plays audio.
No proprietary app/device fixtures, live media hosts or cloud accounts are used.
"""

from __future__ import annotations

import asyncio
import errno
from dataclasses import dataclass, field
from html import escape
from typing import Any
from xml.etree import ElementTree as ET

import pytest
import requests
from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.test_utils import TestServer
from yarl import URL

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.queue import DC_NS, DIDL_NS, UPNP_NS
from fake_linkplay.soap import SERVICE_TYPES, SOAP_NS
from fake_linkplay.state import RendererState
from fake_linkplay.upnp_schema import SCPD_NS


FIRST = "https://media.example.test/first.ogg?version=1&format=ogg"
SECOND = "https://media.example.test/second.wav?version=2&format=wav"
TITLE = "  Été & <雨>\nhttps://lyrics.example.test/song?x=1&y=2\t "
ARTIST = " https://artist.example.test/duo & <日本語>\rName "
ALBUM = "  Collection & <2026>\t日本語  "
ARTWORK = "https://art.example.test/cover?size=640&format=png"
OPAQUE_ID = "opaque:42&provider/<日本語>"
METADATA_RES = "https://metadata.example.test/must-never-play.ogg?x=1&y=2"
NS = {"d": DIDL_NS, "dc": DC_NS, "u": UPNP_NS}


def xml_text(value: str) -> str:
    """Fixture encoding independent of renderer helpers, including XML CRs."""
    return escape(value).replace("\r", "&#13;")


def didl(*, title: str = TITLE, artist: str = ARTIST, album: str = ALBUM,
         artwork: str = ARTWORK) -> str:
    return (
        " \r\n\t"
        f'<d:DIDL-Lite xmlns:d="{DIDL_NS}" xmlns:dc="{DC_NS}" '
        f'xmlns:m="{UPNP_NS}" xmlns:x="urn:example:opaque">\r\n'
        f'<d:item id="{xml_text(OPAQUE_ID)}" parentID="opaque-parent">'
        f"<dc:title>{xml_text(title)}</dc:title>"
        + (f"<m:artist>{xml_text(artist)}</m:artist>" if artist else "")
        + (f"<m:album>{xml_text(album)}</m:album>" if album else "")
        + (f"<m:albumArtURI>{xml_text(artwork)}</m:albumArtURI>" if artwork else "")
        + f'<d:res duration="00:02:03.900">{xml_text(METADATA_RES)}</d:res>'
        '<x:providerData>opaque &amp; &lt;payload&gt; 日本語</x:providerData>'
        "</d:item>\r\n</d:DIDL-Lite>\t\r\n "
    )


def playlist(tracks: list[dict[str, str]], *, name: str = "Music & <日本語>",
             source: str = "UPnPServer", artwork: str = "") -> str:
    nodes = []
    for index, fields in enumerate(tracks, 1):
        nodes.append(f"<Track{index}>" + "".join(
            f"<{key}>{xml_text(value)}</{key}>" for key, value in fields.items()
        ) + f"</Track{index}>")
    return (f"<PlayList><QueueName>{xml_text(name)}</QueueName>"
            f"<ListInfo><SourceName>{xml_text(source)}</SourceName><PicUrl>{xml_text(artwork)}</PicUrl>"
            "<StartPlayIndex>1</StartPlayIndex></ListInfo>"
            "<Tracks>" + "".join(nodes) + "</Tracks></PlayList>")


def property_fields(body: str) -> dict[str, str]:
    root = ET.fromstring(body)
    assert root.tag == "{urn:schemas-upnp-org:event-1-0}propertyset"
    return {node.tag.rsplit("}", 1)[-1]: node.text or "" for prop in root for node in prop}


def last_change_fields(properties: dict[str, str]) -> dict[str, str]:
    return {node.tag.rsplit("}", 1)[-1]: node.attrib["val"]
            for node in ET.fromstring(properties["LastChange"]).iter() if "val" in node.attrib}


@dataclass
class MetadataBackend:
    """MOCK audio adapter, with explicit failure injection for gain checks."""

    url: str = ""
    idle: bool = True
    paused: bool = False
    fail_gain: bool = False
    title: str = "Backend fallback title"
    snapshot_path: str | None = None
    loads: list[str] = field(default_factory=list)

    async def loadfile(self, url: str, options: dict[str, str] | None = None, *, start_paused=False) -> None:
        self.loads.append(url)
        self.url, self.idle, self.paused = url, False, start_paused

    async def resume(self, *, current=None):
        if current is not None and not await current():
            raise BackendError("Playback command superseded")
        self.paused = False

    async def stop(self) -> None:
        self.url, self.idle, self.paused = "", True, False

    async def volume(self, value: int) -> None:
        if self.fail_gain:
            raise BackendError("Synthetic missing gain confirmation")

    async def mute(self, value: bool) -> None:
        if self.fail_gain:
            raise BackendError("Synthetic missing gain confirmation")

    async def snapshot(self) -> dict[str, Any]:
        return {"path": self.url if self.snapshot_path is None else self.snapshot_path,
                "idle-active": self.idle, "pause": self.paused,
                "playback-time": 0, "duration": 999, "media-title": self.title,
                "track-list": [] if self.idle else [{"type": "audio", "selected": True}]}


@dataclass
class MetadataRenderer:
    api: HttpApi
    backend: MetadataBackend
    client: ClientSession
    base: str
    events: dict[str, list[dict[str, str]]]

    async def soap(self, action: str, fields: dict[str, str] | None = None,
                   service: str = "AVTransport") -> dict[str, str]:
        namespace = SERVICE_TYPES[service]
        args = "".join(f"<{key}>{xml_text(value)}</{key}>" for key, value in (fields or {}).items())
        body = (f'<s:Envelope xmlns:s="{SOAP_NS}"><s:Body>'
                f'<u:{action} xmlns:u="{namespace}">{args}</u:{action}>'
                '</s:Body></s:Envelope>')
        async with self.client.post(self.base + "/upnp/control/" + service,
                data=body.encode("utf-8"), headers={"SOAPACTION": f'"{namespace}#{action}"',
                                                   "Content-Type": 'text/xml; charset="utf-8"'}) as response:
            text = await response.text()
            assert response.status == 200, text
        root = ET.fromstring(text)
        result = root.find(f"{{{SOAP_NS}}}Body/{{{namespace}}}{action}Response")
        assert result is not None, text
        return {node.tag.rsplit("}", 1)[-1]: node.text or "" for node in result}

    async def http(self, command: str) -> dict[str, Any]:
        async with self.client.get(self.base + "/httpapi.asp", params={"command": command}) as response:
            assert response.status == 200, await response.text()
            return await response.json()

    async def event(self, service: str, *, uri: str, metadata: str = "", index: int = 1,
                    title: str | None = None) -> dict[str, str]:
        async with asyncio.timeout(3):
            while True:
                for props in reversed(self.events[service]):
                    if props.get("TrackURI") != uri:
                        continue
                    if service == "AVTransport" and props.get("TrackMetaData") == metadata:
                        return props
                    if (service == "PlayQueue" and props.get("CurrentIndex") == str(index)
                            and (title is None or props.get("TrackTitle") == title)):
                        return props
                await asyncio.sleep(0.005)


@pytest.fixture
async def renderer(tmp_path, monkeypatch):
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path),
                 advertise_ip="127.0.0.1", bind_address="127.0.0.1", auto_spawn_mpv=False,
                 enable_ssdp=False, enable_mdns=False)
    backend = MetadataBackend()
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)
    events: dict[str, list[dict[str, str]]] = {"AVTransport": [], "PlayQueue": []}

    async def callback(request):
        assert request.headers["NT"] == "upnp:event"
        assert request.headers["NTS"] == "upnp:propchange"
        events[request.match_info["service"]].append(property_fields(await request.text()))
        return web.Response()

    app = api.app()
    app.router.add_route("NOTIFY", "/synthetic/events/{service}", callback)
    server = TestServer(app, host="127.0.0.1")
    original_request = ClientSession._request
    blocked: list[str] = []

    async def loopback_only(session, method, url, *args, **kwargs):
        target = URL(url)
        if target.scheme != "http" or target.host != "127.0.0.1" or target.port != server.port:
            blocked.append(str(target))
            raise AssertionError(f"Unexpected non-fixture HTTP request: {target}")
        return await original_request(session, method, url, *args, **kwargs)

    def no_requests_network(session, method, url, *args, **kwargs):
        blocked.append(str(url))
        raise AssertionError(f"Unexpected requests network access: {url}")

    monkeypatch.setattr(ClientSession, "_request", loopback_only)
    monkeypatch.setattr(requests.Session, "request", no_requests_network)
    try:
        try:
            await server.start_server()
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EPERM, errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL}:
                pytest.skip(f"Loopback TCP unavailable: errno={exc.errno}")
            raise
        cfg.http_api_port = cfg.upnp_port = server.port
        base = str(server.make_url("/")).rstrip("/")
        async with ClientSession(timeout=ClientTimeout(total=5), trust_env=False) as client:
            for service in events:
                async with client.request("SUBSCRIBE", base + "/upnp/event/" + service, headers={
                    "NT": "upnp:event", "CALLBACK": f"<{base}/synthetic/events/{service}>",
                    "TIMEOUT": "Second-60",
                }) as response:
                    assert response.status == 200
            yield MetadataRenderer(api, backend, client, base, events)
    finally:
        await server.close()
    assert api._state_monitor_task is None
    assert not api.event_subscribers
    assert not blocked, f"Test attempted external network access: {blocked}"


def assert_display_metadata(metadata: str, *, title: str, artist: str, album: str, artwork: str) -> ET.Element:
    root = ET.fromstring(metadata)
    assert root.tag == f"{{{DIDL_NS}}}DIDL-Lite"
    item = root.find("d:item", NS)
    assert item is not None
    assert item.findtext("dc:title", default="", namespaces=NS) == title
    assert item.findtext("u:artist", default="", namespaces=NS) == artist
    assert item.findtext("u:album", default="", namespaces=NS) == album
    assert item.findtext("u:albumArtURI", default="", namespaces=NS) == artwork
    return item


async def assert_surfaces(env: MetadataRenderer, *, uri: str, title: str, artist: str = "",
                          album: str = "", artwork: str = "", metadata: str | None = None,
                          item_id: str | None = None, count: int = 1, index: int = 1,
                          source: str | None = None) -> str:
    meta = await env.http("getMetaInfo")
    assert (meta["title"], meta["artist"], meta["album"]) == (title, artist, album)
    assert meta["uri"] == meta["stream_url"] == uri
    assert meta["albumArtURI"] == meta["picurl"] == meta["cover"] == meta["art"] == artwork
    if source is not None:
        assert meta["source"] == meta["TrackSource"] == source
    for command in ("getPlayerStatus", "getPlayerStatusEx"):
        player = await env.http(command)
        assert (player["Title"], player["Artist"], player["Album"]) == (title, artist, album)
        assert player["uri"] == player["stream_url"] == uri
        assert player["AlbumArtURI"] == player["albumArtURI"] == player["picurl"] == artwork
        assert (player["plicount"], player["plicurr"]) == (str(count), str(index))
        if source is not None:
            assert player["source"] == player["TrackSource"] == source

    info = await env.soap("GetInfoEx")
    projected_metadata = info["TrackMetaData"]
    if metadata is not None:
        assert projected_metadata == metadata
    assert info["TrackURI"] == uri
    assert info["Track"] == str(index)
    position = await env.soap("GetPositionInfo")
    assert position["TrackURI"] == uri
    assert position["Track"] == str(index)
    assert position["TrackMetaData"] == projected_metadata
    media = await env.soap("GetMediaInfo")
    assert media["CurrentURI"] == uri
    assert media["CurrentURIMetaData"] == projected_metadata
    assert media["NrTracks"] == str(count)
    if source is not None:
        assert info["TrackSource"] == position["TrackSource"] == media["TrackSource"] == source

    for action in ("BrowseQueue", "BrowseQueueEx"):
        browse = await env.soap(action, service="PlayQueue")
        queue = ET.fromstring(browse["QueueContext"])
        tracks = queue.find("Tracks")
        assert tracks is not None and len(tracks) == count
        selected = tracks.find(f"Track{index}")
        assert selected is not None
        assert selected.findtext("URL") == uri
        assert selected.findtext("Title") == title
        assert selected.findtext("Artist", default="") == artist
        assert selected.findtext("Album", default="") == album
        assert (selected.findtext("PicUrl") or queue.findtext("ListInfo/PicUrl") or "") == artwork
        if source is not None:
            assert (selected.findtext("Source") or queue.findtext("ListInfo/SourceName") or "") == source
        if metadata is not None:
            assert selected.findtext("Metadata") == metadata
        if item_id is not None:
            assert selected.findtext("Id") == item_id

    transport = await env.event("AVTransport", uri=uri, metadata=projected_metadata)
    change = last_change_fields(transport)
    assert change["CurrentTrackURI"] == uri
    assert change["CurrentTrack"] == str(index)
    assert change["CurrentTrackMetaData"] == projected_metadata
    queue_event = await env.event("PlayQueue", uri=uri, index=index, title=title)
    assert queue_event["TrackTitle"] == title
    assert queue_event["Count"] == str(count)
    queue = ET.fromstring(queue_event["QueueContext"])
    selected = queue.find(f"Tracks/Track{index}")
    assert selected is not None
    assert selected.findtext("URL") == uri
    assert selected.findtext("Artist", default="") == artist
    assert selected.findtext("Album", default="") == album
    assert (selected.findtext("PicUrl") or queue.findtext("ListInfo/PicUrl") or "") == artwork
    if source is not None:
        assert transport["TrackSource"] == queue_event["TrackSource"] == source
        assert (selected.findtext("Source") or queue.findtext("ListInfo/SourceName") or "") == source
    if metadata is not None:
        assert selected.findtext("Metadata") == metadata
    if item_id is not None:
        assert selected.findtext("Id") == item_id
    assert last_change_fields(queue_event)["CurrentIndex"] == str(index)
    return projected_metadata


@pytest.mark.asyncio
async def test_supplied_transport_didl_survives_http_soap_and_gena_without_resource_rewrite(renderer):
    metadata = didl()
    await renderer.soap("SetAVTransportURI", {"CurrentURI": FIRST, "CurrentURIMetaData": metadata})
    assert renderer.backend.loads == []
    await renderer.soap("Play", {"Speed": "1"})
    assert renderer.backend.loads == [FIRST]
    actual = await assert_surfaces(renderer, uri=FIRST, title=TITLE, artist=ARTIST,
                                  album=ALBUM, artwork=ARTWORK, metadata=metadata)
    item = assert_display_metadata(actual, title=TITLE, artist=ARTIST, album=ALBUM, artwork=ARTWORK)
    assert item.attrib["id"] == OPAQUE_ID
    assert item.findtext("d:res", namespaces=NS) == METADATA_RES
    assert renderer.api.state.queue.current.url == FIRST
    assert renderer.api.state.queue.current.metadata == metadata


@pytest.mark.asyncio
async def test_two_track_next_projects_selected_music_and_clears_absent_fields(renderer):
    first_metadata = didl()
    second_title = " https://title.example.test/ordinary-text?a=1&b=<2> 日本語 "
    second_metadata = didl(title=second_title, artist="", album="", artwork="")
    await renderer.soap("CreateQueue", {"QueueContext": playlist([
        {"Id": OPAQUE_ID, "URL": FIRST, "Metadata": first_metadata},
        {"Id": "opaque:second&<track>", "URL": SECOND, "Metadata": second_metadata},
    ])}, "PlayQueue")
    await renderer.soap("PlayQueueWithIndex", {"QueueName": "Music & <日本語>", "Index": "1"}, "PlayQueue")
    await assert_surfaces(renderer, uri=FIRST, title=TITLE, artist=ARTIST, album=ALBUM,
                          artwork=ARTWORK, metadata=first_metadata, item_id=OPAQUE_ID, count=2)
    await renderer.soap("Next")
    actual = await assert_surfaces(renderer, uri=SECOND, title=second_title, metadata=second_metadata,
                                  item_id="opaque:second&<track>", count=2, index=2)
    assert_display_metadata(actual, title=second_title, artist="", album="", artwork="")
    assert renderer.backend.loads == [FIRST, SECOND]
    assert renderer.api.state.queue.current.id == "opaque:second&<track>"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["UPnPServer", "CustomRadio"])
async def test_generated_didl_uses_real_queue_artist_and_album(renderer, source):
    await renderer.soap("CreateQueue", {"QueueContext": playlist([
        {"Id": OPAQUE_ID, "URL": FIRST, "Title": TITLE, "Artist": ARTIST,
         "Album": ALBUM, "PicUrl": ARTWORK, "Source": source},
    ])}, "PlayQueue")
    await renderer.soap("PlayQueueWithIndex", {"Index": "1"}, "PlayQueue")
    metadata = await assert_surfaces(renderer, uri=FIRST, title=TITLE, artist=ARTIST,
                                     album=ALBUM, artwork=ARTWORK, item_id=OPAQUE_ID)
    item = assert_display_metadata(metadata, title=TITLE, artist=ARTIST, album=ALBUM, artwork=ARTWORK)
    assert item.findtext("d:res", namespaces=NS) == FIRST
    assert item.findtext("dc:creator", default="", namespaces=NS) == ARTIST
    assert item.findtext("u:creator", default="", namespaces=NS) in {"", ARTIST}


@pytest.mark.asyncio
@pytest.mark.parametrize("interface", ["soap", "http"])
async def test_direct_source_replacement_clears_previous_artist_album_and_artwork(renderer, interface):
    await renderer.soap("SetAVTransportURI", {"CurrentURI": FIRST, "CurrentURIMetaData": didl()})
    await renderer.soap("Play")
    if interface == "soap":
        await renderer.soap("SetAVTransportURI", {"CurrentURI": SECOND})
        await renderer.soap("Play")
    else:
        async with renderer.client.get(renderer.base + "/httpapi.asp",
                params={"command": "setPlayerCmd:play:" + SECOND}) as response:
            assert response.status == 200, await response.text()
    title = (await renderer.http("getMetaInfo"))["title"]
    assert title != TITLE
    metadata = await assert_surfaces(renderer, uri=SECOND, title=title)
    item = assert_display_metadata(metadata, title=title, artist="", album="", artwork="")
    assert item.findtext("dc:creator", default="", namespaces=NS) == ""
    assert item.findtext("u:creator", default="", namespaces=NS) == ""
    assert renderer.backend.loads == [FIRST, SECOND]


@pytest.mark.asyncio
async def test_info_extensions_expose_unknown_gain_and_declare_boolean_contract(renderer):
    async with renderer.client.get(renderer.base + "/upnp/AVTransport.xml") as response:
        assert response.status == 200
        scpd = ET.fromstring(await response.text())
    ns = {"s": SCPD_NS}
    actions = {node.findtext("s:name", namespaces=ns): node
               for node in scpd.findall("s:actionList/s:action", ns)}
    variables = {node.findtext("s:name", namespaces=ns): node
                 for node in scpd.findall("s:serviceStateTable/s:stateVariable", ns)}
    for action in ("GetInfoEx", "GetPositionInfo"):
        argument = next(node for node in actions[action].findall("s:argumentList/s:argument", ns)
                        if node.findtext("s:name", namespaces=ns) == "GainConfirmed")
        assert argument.findtext("s:direction", namespaces=ns) == "out"
        variable = variables[argument.findtext("s:relatedStateVariable", namespaces=ns)]
        assert variable.findtext("s:dataType", namespaces=ns) == "boolean"
        assert (await renderer.soap(action))["GainConfirmed"] == "1"

    renderer.backend.fail_gain = True
    async with renderer.client.get(renderer.base + "/httpapi.asp",
            params={"command": "setPlayerCmd:vol:25"}) as response:
        assert response.status == 502
    assert (await renderer.http("getPlayerStatus"))["gain_confirmed"] is False
    for action in ("GetInfoEx", "GetPositionInfo"):
        assert (await renderer.soap(action))["GainConfirmed"] == "0"
    renderer.backend.fail_gain = False
    async with renderer.client.get(renderer.base + "/httpapi.asp",
            params={"command": "setPlayerCmd:vol:25"}) as response:
        assert response.status == 200
    for action in ("GetInfoEx", "GetPositionInfo"):
        assert (await renderer.soap(action))["GainConfirmed"] == "1"


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [
    " opaque provider & <not-XML> 日本語\r\n\t ",
    "https://metadata.example.test/ordinary-provider-text?x=1&y=<2>",
    " \t\r\n ",
])
async def test_nonempty_opaque_transport_metadata_remains_exact_and_does_not_invent_display_fields(renderer, metadata):
    await renderer.soap("SetAVTransportURI", {"CurrentURI": FIRST, "CurrentURIMetaData": metadata})
    await renderer.soap("Play")
    title = renderer.backend.title
    await assert_surfaces(renderer, uri=FIRST, title=title, metadata=metadata)
    assert renderer.api.state.queue.current.metadata == metadata
    assert renderer.backend.loads == [FIRST]


@pytest.mark.asyncio
async def test_missing_didl_title_follows_only_matching_backend_title_without_rewriting_metadata(renderer):
    metadata = didl(title="").replace("<dc:title></dc:title>", "")
    await renderer.soap("SetAVTransportURI", {"CurrentURI": FIRST, "CurrentURIMetaData": metadata})
    assert renderer.api.state.title == FIRST.rsplit("/", 1)[-1]
    assert renderer.api.state.queue.current.title_is_fallback
    renderer.backend.title = " Matching backend & <日本語>\r\nfirst "
    await renderer.soap("Play")
    await assert_surfaces(renderer, uri=FIRST, title=renderer.backend.title, artist=ARTIST,
                          album=ALBUM, artwork=ARTWORK, metadata=metadata, source="DLNA")
    confirmed_title = renderer.backend.title

    # A stale observation from another source must not rename the selected item.
    renderer.backend.snapshot_path = SECOND
    renderer.backend.title = "Foreign source title that must be ignored"
    player = await renderer.http("getPlayerStatus")
    assert player["Title"] == confirmed_title
    assert renderer.api.state.queue.current.title == confirmed_title
    assert renderer.api.state.track_metadata == metadata

    renderer.backend.snapshot_path = None
    renderer.backend.title = " Matching backend & <日本語>\tsecond "
    assert (await renderer.http("getPlayerStatus"))["Title"] == renderer.backend.title
    await assert_surfaces(renderer, uri=FIRST, title=renderer.backend.title, artist=ARTIST,
                          album=ALBUM, artwork=ARTWORK, metadata=metadata, source="DLNA")
    assert renderer.api.state.queue.current.title == renderer.backend.title
    assert renderer.api.state.queue.current.metadata == metadata
    assert ET.fromstring(metadata).find("d:item/dc:title", NS) is None
    assert renderer.backend.loads == [FIRST]


@pytest.mark.asyncio
async def test_generated_didl_keeps_listinfo_artwork_and_source_through_load_and_title_refresh(renderer):
    source = " Library & <日本語>\rCollection "
    await renderer.soap("CreateQueue", {"QueueContext": playlist([
        {"Id": OPAQUE_ID, "URL": FIRST, "Title": "", "Artist": ARTIST,
         "Album": ALBUM, "Source": "", "PicUrl": ""},
    ], source=source, artwork=ARTWORK)}, "PlayQueue")

    async def assert_inherited(title):
        metadata = await assert_surfaces(renderer, uri=FIRST, title=title, artist=ARTIST,
                                         album=ALBUM, artwork=ARTWORK, item_id=OPAQUE_ID,
                                         source=source)
        item = assert_display_metadata(metadata, title=title, artist=ARTIST, album=ALBUM, artwork=ARTWORK)
        assert item.findtext("{www.wiimu.com/song/}originSource") == source
        assert item.findtext("{www.wiimu.com/song/}image") == ARTWORK
        assert item.attrib["id"] == OPAQUE_ID
        assert item.find("d:res", NS).attrib["protocolInfo"] == "http-get:*:*:*"
        assert renderer.api.state.queue.current.source == ""
        assert renderer.api.state.queue.current.artwork == ""

    await assert_inherited(FIRST.rsplit("/", 1)[-1])
    await renderer.soap("PlayQueueWithIndex", {"Index": "1"}, "PlayQueue")
    await assert_inherited(renderer.backend.title)
    renderer.backend.title = "Refreshed inherited source & <雨>\r\ntrack"
    await renderer.http("getPlayerStatus")
    await assert_inherited(renderer.backend.title)
    assert renderer.backend.loads == [FIRST]


@pytest.mark.asyncio
async def test_generated_opaque_id_keeps_attribute_whitespace_without_guessing_source_or_mime(renderer):
    opaque_id = ' opaque:\tfirst\nsecond\rthird & <日本語> "quoted" '
    # A provider-looking path is still a synthetic .test URL, not format evidence.
    uri = "https://media.example.test/radioparadise.com/unknown-format"
    await renderer.soap("CreateQueue", {"QueueContext": playlist([
        {"Id": opaque_id, "URL": uri},
    ], source="")}, "PlayQueue")
    await renderer.soap("PlayQueueWithIndex", {"Index": "1"}, "PlayQueue")
    for title in (renderer.backend.title, "A later backend title & <雨>"):
        renderer.backend.title = title
        await renderer.http("getPlayerStatus")
        metadata = await assert_surfaces(renderer, uri=uri, title=title, item_id=opaque_id, source="")
        item = assert_display_metadata(metadata, title=title, artist="", album="", artwork="")
        assert item.attrib["id"] == opaque_id
        assert item.findtext("dc:creator", default="", namespaces=NS) == ""
        assert item.findtext("{www.wiimu.com/song/}originSource", default="") == ""
        assert item.find("d:res", NS).attrib["protocolInfo"] == "http-get:*:*:*"
        assert renderer.api.state.queue.current.id == opaque_id
    assert renderer.backend.loads == [uri]
