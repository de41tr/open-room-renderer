"""UNIT: synthetic music metadata projections; no files, fetches or hardware."""

from dataclasses import replace
from html import escape
from xml.etree import ElementTree as ET

import pytest

from fake_linkplay.queue import (
    DC_NS,
    DIDL_NS,
    MAX_QUEUE_BYTES,
    MAX_XML_DEPTH,
    UPNP_NS,
    DidlMetadata,
    PlaybackQueue,
    QueueError,
    QueueItem,
    parse_didl_metadata,
    parse_queue_context,
    serialize_queue_context,
)


TITLE = "  Été & <雨> https://lyrics.example.test/a?x=1&y=2  "
ARTIST = "音楽 & Friends <duo> https://artist.example.test/name"
ALBUM = "  Collection & <日本語>  "
ARTWORK = "https://art.example.test/cover?size=640&format=png"
URL = "https://media.example.test/direct.ogg"


def didl(fields, *, item_attributes='id="opaque:42&amp;provider"'):
    return (
        f'<DIDL-Lite xmlns="{DIDL_NS}" xmlns:dc="{DC_NS}" '
        f'xmlns:upnp="{UPNP_NS}" xmlns:other="urn:example:unknown">'
        f"<item {item_attributes}>{fields}</item></DIDL-Lite>"
    )


def music_metadata():
    return didl(
        f"<dc:title>{escape(TITLE)}</dc:title>"
        f"<upnp:artist>{escape(ARTIST)}</upnp:artist>"
        f"<upnp:album>{escape(ALBUM)}</upnp:album>"
        f"<upnp:albumArtURI>{escape(ARTWORK)}</upnp:albumArtURI>"
        '<res duration="01:02:03.500">https://metadata.example.test/never-select.ogg</res>'
        "<other:payload>Opaque &amp; &lt;provider&gt; 日本語</other:payload>"
    )


def playlist(metadata="", fields=""):
    return (
        "<PlayList><Tracks><Track1>"
        f"<URL>{escape(URL)}</URL><Id>opaque:42&amp;provider</Id>"
        f"<Metadata>{escape(metadata)}</Metadata>{fields}"
        "</Track1></Tracks></PlayList>"
    )


def test_didl_helper_projects_exact_display_text_and_only_explicit_artwork():
    metadata = music_metadata()
    assert parse_didl_metadata(metadata) == DidlMetadata(
        title=TITLE, artist=ARTIST, album=ALBUM, artwork=ARTWORK, duration=3723.5,
    )
    assert parse_didl_metadata(metadata.encode("utf-8")) == parse_didl_metadata(metadata)
    assert parse_didl_metadata("\ufeff" + metadata) == parse_didl_metadata(metadata)


def test_queue_uses_didl_music_hints_without_replacing_url_identity_or_payload():
    metadata = " \n" + music_metadata() + "\n "
    queue = parse_queue_context(playlist(metadata))
    item = queue.current
    assert (item.title, item.artist, item.album) == (TITLE, ARTIST, ALBUM)
    assert item.artwork == ARTWORK
    assert item.duration == 3723.5
    assert item.url == URL
    assert item.id == "opaque:42&provider"
    assert item.metadata == metadata
    serialized = serialize_queue_context(queue)
    fields = ET.fromstring(serialized).find("Tracks/Track1")
    assert fields.findtext("Artist") == ARTIST
    assert fields.findtext("Album") == ALBUM
    assert fields.findtext("Metadata") == metadata
    assert parse_queue_context(serialized) == queue


@pytest.mark.parametrize("artist,album", [(ARTIST, ALBUM), ("", ""), ("  ", "\t")])
def test_direct_music_fields_override_didl_even_when_explicitly_empty(artist, album):
    fields = f"<Artist>{escape(artist)}</Artist><Album>{escape(album)}</Album>"
    queue = parse_queue_context(playlist(music_metadata(), fields))
    assert queue.current.artist == artist
    assert queue.current.album == album
    assert queue.current.title == TITLE
    assert parse_queue_context(serialize_queue_context(queue)) == queue


def test_direct_music_fields_work_without_didl():
    fields = f"<Title>{escape(TITLE)}</Title><Artist>{escape(ARTIST)}</Artist><Album>{escape(ALBUM)}</Album>"
    queue = parse_queue_context(playlist("opaque provider text & < >", fields))
    assert (queue.current.title, queue.current.artist, queue.current.album) == (TITLE, ARTIST, ALBUM)
    assert parse_queue_context(serialize_queue_context(queue)) == queue


def test_new_fields_preserve_existing_positional_queue_item_arguments():
    item = QueueItem(URL, "Title", "Source", "opaque", "Metadata", ARTWORK,
                     "track", "audio/ogg", 12.5, "https://media.example.test/resolved.ogg")
    assert item.resolved_url == "https://media.example.test/resolved.ogg"
    assert item.artist == item.album == ""


def test_music_fields_and_raw_metadata_roundtrip_crlf_and_xml_sensitive_text():
    queue = PlaybackQueue(items=[QueueItem(
        url=URL, title="Title\r\n& <雨>", artist="Artist\r& <雨>",
        album="Album\r\nwith\ttabs & < >", metadata="opaque\r\n& <雨>\rpayload",
    )], index=1)
    assert parse_queue_context(serialize_queue_context(queue)) == queue


@pytest.mark.parametrize("field", ["artist", "album"])
@pytest.mark.parametrize("value", [None, 42, "invalid\x00text", "invalid\ud800text"])
def test_music_fields_receive_existing_model_text_validation(field, value):
    queue = PlaybackQueue(items=[QueueItem(URL)], index=1)
    queue.items[0] = replace(queue.current, **{field: value})
    with pytest.raises(QueueError, match="text"):
        serialize_queue_context(queue)


@pytest.mark.parametrize("field", ["Artist", "Album"])
@pytest.mark.parametrize("payload", ["<nested>Not scalar</nested>", None])
def test_direct_music_fields_reject_nested_or_duplicate_values(field, payload):
    fields = (f"<{field}>{payload}</{field}>" if payload is not None
              else f"<{field}>First</{field}><{field}>Second</{field}>")
    with pytest.raises(QueueError):
        parse_queue_context(playlist(fields=fields))


def test_didl_prefix_spelling_does_not_matter_but_namespace_does():
    metadata = (
        f'<d:DIDL-Lite xmlns:d="{DIDL_NS}" xmlns:t="{DC_NS}" xmlns:m="{UPNP_NS}">'
        '<d:item><t:title>Title</t:title><m:artist>Artist</m:artist>'
        '<m:album>Album</m:album><m:albumArtURI>art:opaque</m:albumArtURI>'
        '<d:res duration="12.5">resource:opaque</d:res></d:item></d:DIDL-Lite>'
    )
    assert parse_didl_metadata(metadata) == DidlMetadata("Title", "Artist", "Album", "art:opaque", 12.5)


@pytest.mark.parametrize("fields", [
    "<title>Wrong namespace</title><artist>Wrong namespace</artist><album>Wrong namespace</album>",
    "<other:title>Wrong</other:title><other:artist>Wrong</other:artist><other:album>Wrong</other:album>",
    "<other:extension><dc:title>Nested</dc:title><upnp:artist>Nested</upnp:artist>"
    "<upnp:album>Nested</upnp:album><upnp:albumArtURI>https://art.example.test/ignored</upnp:albumArtURI>"
    '<res duration="99">https://media.example.test/ignored</res></other:extension>',
    "<dc:title><other:child>Nested</other:child></dc:title>"
    "<upnp:artist>Partial<other:child/>text</upnp:artist><upnp:album><other:child/></upnp:album>"
    '<upnp:albumArtURI><other:child/></upnp:albumArtURI><res duration="99"><other:child/></res>',
    '<other:res duration="99">https://media.example.test/ignored</other:res>'
    "<other:albumArtURI>https://art.example.test/ignored</other:albumArtURI>",
    "<URL>https://media.example.test/ignored</URL><PicUrl>https://art.example.test/ignored</PicUrl>",
])
def test_helper_does_not_search_unknown_namespaces_nested_fields_or_url_text(fields):
    assert parse_didl_metadata(didl(fields)) == DidlMetadata()


@pytest.mark.parametrize("metadata", [
    "", "  \n ", "arbitrary provider text: & < > ñ 日本語",
    "https://artist.example.test/ordinary-title", "<not-well-formed",
    '<DIDL-Lite><item><title>No namespace</title></item></DIDL-Lite>',
    f'<DIDL-Lite xmlns="{DIDL_NS}"/>',
    f'<other xmlns="{DIDL_NS}"><item><title>Not DIDL</title></item></other>',
    '<!DOCTYPE x [<!ENTITY e "expanded">]><x>&e;</x>',
    '<!DOCTYPE x SYSTEM "https://never-fetch.example.test/external.dtd"><x/>',
    '<!ENTITY e SYSTEM "file:///not-a-real-file"><x>&e;</x>',
    didl("<dc:title>&unknown;</dc:title>"),
])
def test_unrecognized_or_unsafe_metadata_is_harmless_opaque_text(metadata):
    assert parse_didl_metadata(metadata) == DidlMetadata()
    queue = parse_queue_context(playlist(metadata))
    assert queue.current.metadata == metadata
    assert queue.current.artist == queue.current.album == ""
    assert parse_queue_context(serialize_queue_context(queue)) == queue


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-be", "utf-16-le", "utf-32"])
def test_non_utf8_bytes_cannot_bypass_metadata_dtd_checks(encoding):
    unsafe = '<!DOCTYPE x [<!ENTITY e "expanded">]>' + didl("<dc:title>&e;</dc:title>")
    assert parse_didl_metadata(unsafe.encode(encoding)) == DidlMetadata()


@pytest.mark.parametrize("duration", ["nan", "inf", "-1", "bad", "01:61:00", "9" * 5000 + ":00:00"])
def test_invalid_optional_duration_does_not_discard_valid_display_hints(duration):
    metadata = didl(f'<upnp:artist>Artist</upnp:artist><res duration="{duration}"/>')
    assert parse_didl_metadata(metadata) == DidlMetadata(artist="Artist")


def test_didl_limits_count_utf8_bytes_and_enforce_existing_depth_bound():
    metadata = music_metadata()
    size = len(metadata.encode("utf-8"))
    assert parse_didl_metadata(metadata, max_bytes=size).artist == ARTIST
    assert parse_didl_metadata(metadata, max_bytes=size - 1) == DidlMetadata()
    assert parse_didl_metadata(" " * (MAX_QUEUE_BYTES + 1)) == DidlMetadata()
    for extension_depth, expected in [(MAX_XML_DEPTH - 2, "Artist"), (MAX_XML_DEPTH - 1, "")]:
        fields = ("<upnp:artist>Artist</upnp:artist>" + "<other:extension>" * extension_depth
                  + "</other:extension>" * extension_depth)
        assert parse_didl_metadata(didl(fields)).artist == expected


@pytest.mark.parametrize("max_bytes", [0, -1, True, "100"])
def test_didl_helper_rejects_invalid_configured_limits(max_bytes):
    with pytest.raises(QueueError, match="limit"):
        parse_didl_metadata(music_metadata(), max_bytes=max_bytes)


def test_helper_uses_first_direct_item_and_field_without_merging_other_items():
    metadata = music_metadata().replace(
        "</item></DIDL-Lite>",
        "<upnp:artist>Second artist</upnp:artist></item>"
        "<item><upnp:album>Other item album</upnp:album></item></DIDL-Lite>",
    )
    assert parse_didl_metadata(metadata).artist == ARTIST
    assert parse_didl_metadata(metadata).album == ALBUM
