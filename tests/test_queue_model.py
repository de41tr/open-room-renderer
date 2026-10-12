"""UNIT: synthetic queue state/XML only; no hardware or network claims."""

from dataclasses import replace
from html import escape
from xml.etree import ElementTree as ET

import pytest

from fake_linkplay.queue import (
    MAX_QUEUE_BYTES,
    MAX_QUEUE_TRACKS,
    PlaybackQueue,
    QueueError,
    QueueItem,
    parse_queue_context,
    serialize_queue_context,
)


def playlist(tracks=None, *, info="", fields="", list_name="Sample queue"):
    track_xml = "" if tracks is None else f"<Tracks>{tracks}</Tracks>"
    return (f"<PlayList><ListName>{escape(list_name)}</ListName>{fields}"
            f"<ListInfo>{info}</ListInfo>{track_xml}</PlayList>")


def track(number, *, fields="", url=None):
    url = f"https://media.example.test/audio-{number}.ogg" if url is None else url
    return f"<Track{number}><URL>{escape(url)}</URL>{fields}</Track{number}>"


def test_two_track_queue_preserves_numbering_and_requested_start():
    context = playlist(track(2) + track(1), info="<TrackNumber>2</TrackNumber><StartPlayIndex>2</StartPlayIndex>")
    queue = parse_queue_context(context)
    assert queue.name == queue.list_name == "Sample queue"
    assert queue.count == 2
    assert queue.index == 2
    assert [item.url for item in queue.items] == [
        "https://media.example.test/audio-1.ogg", "https://media.example.test/audio-2.ogg",
    ]
    assert queue.current is queue.items[1]
    assert queue.loop == 4


def test_defaults_and_names_are_explicit():
    queue = parse_queue_context(playlist(track(1)))
    assert queue.index == 1
    assert parse_queue_context("<PlayList/>").name == "CurrentQueue"
    context = playlist(fields="<QueueName>Control identity</QueueName>")
    assert parse_queue_context(context).name == "Control identity"
    assert parse_queue_context(context, name="Server identity").name == "Server identity"
    queue.require_name("Sample queue")
    for name in ("", "Another queue", None):
        with pytest.raises(QueueError, match="Unknown queue name"):
            queue.require_name(name)


@pytest.mark.parametrize("name", ["", "  ", None, 2])
def test_empty_or_invalid_model_queue_name_is_rejected(name):
    with pytest.raises(QueueError, match="Queue name"):
        PlaybackQueue(name=name)


def test_explicit_empty_wire_name_is_not_silently_replaced():
    with pytest.raises(QueueError, match="Queue name"):
        parse_queue_context(playlist(fields="<QueueName/>"))
    with pytest.raises(QueueError, match="Queue name"):
        parse_queue_context(playlist(), name="")


@pytest.mark.parametrize("has_tracks", [False, True])
def test_provider_header_stays_unresolved_without_promoting_search_url(has_tracks):
    info = ("<SourceName>ExampleRadio</SourceName><StreamId>station:202&amp;x</StreamId>"
            "<SearchUrl>https://directory.example.test/station/202</SearchUrl>"
            "<TrackNumber>1</TrackNumber><StartPlayIndex>1</StartPlayIndex>"
            "<ShowName>Jazz &amp; récits</ShowName><PicUrl>https://art.example.test/202.jpg</PicUrl>"
            "<Quality>lossless</Quality>")
    queue = parse_queue_context(playlist("" if has_tracks else None, info=info))
    assert queue.items == []
    assert queue.count == queue.index == 0
    assert queue.current is None
    assert queue.source == "ExampleRadio"
    assert queue.stream_id == "station:202&x"
    assert queue.search_url == "https://directory.example.test/station/202"
    assert queue.show_name == "Jazz & récits"
    assert queue.artwork == "https://art.example.test/202.jpg"
    assert queue.quality == "lossless"
    assert parse_queue_context(serialize_queue_context(queue)) == queue


def test_real_tracks_take_precedence_over_search_and_embedded_urls():
    metadata = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
                '<item><res>https://metadata.example.test/never-play.ogg</res></item></DIDL-Lite>')
    fields = (f"<Metadata>{escape(metadata)}</Metadata>"
              "<Extension><URL>https://extension.example.test/never-play.ogg</URL></Extension>")
    info = "<SourceName>ExampleRadio</SourceName><SearchUrl>https://directory.example.test/search</SearchUrl>"
    queue = parse_queue_context(playlist(track(1, fields=fields) + track(2), info=info))
    assert queue.count == 2
    assert queue.current.url == "https://media.example.test/audio-1.ogg"
    assert queue.items[0].metadata == metadata


@pytest.mark.parametrize("content", [
    "<Metadata>https://metadata.example.test/a.ogg</Metadata>",
    "<Extension><URL>https://extension.example.test/a.ogg</URL></Extension>",
    "<SearchUrl>https://directory.example.test/a.ogg</SearchUrl>",
    "<URL/>",
])
def test_missing_direct_track_url_is_rejected(content):
    with pytest.raises(QueueError, match="direct URL"):
        parse_queue_context(playlist(f"<Track1>{content}</Track1>"))


def test_namespaced_didl_is_optional_and_metadata_is_exact():
    metadata = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
                '<item id="provider:42&amp;b"><dc:title>Été &amp; &lt;雨&gt;</dc:title>'
                '<upnp:albumArtURI>https://art.example.test/a?x=1&amp;y=2</upnp:albumArtURI>'
                '<res duration="01:02:03.500">https://metadata.example.test/a.ogg</res>'
                '<provider-field>Keep me: ñ &amp; 日本語</provider-field></item></DIDL-Lite>')
    fields = f"<Id>station:42&amp;provider</Id><Source>ProviderExact</Source><Metadata>{escape(metadata)}</Metadata>"
    queue = parse_queue_context(playlist(track(1, fields=fields), info="<SourceName>QueueProvider</SourceName>"))
    item = queue.current
    assert item.id == "station:42&provider"
    assert item.source == "ProviderExact"
    assert item.metadata == metadata
    assert item.title == "Été & <雨>"
    assert item.artwork == "https://art.example.test/a?x=1&y=2"
    assert item.duration == 3723.5
    serialized = serialize_queue_context(queue)
    assert "&lt;DIDL-Lite" in serialized
    assert parse_queue_context(serialized) == queue


@pytest.mark.parametrize("metadata", [
    "arbitrary provider text: & < > ñ 日本語",
    "  \n<not-well-formed",
    '<DIDL-Lite><item><title>Unnamespaced title</title></item></DIDL-Lite>',
    '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///not-a-real-file">]><x>&e;</x>',
])
def test_unknown_or_unsafe_inner_metadata_remains_opaque_text(metadata):
    queue = parse_queue_context(playlist(track(1, fields=f"<Metadata>{escape(metadata)}</Metadata>")))
    assert queue.current.metadata == metadata
    assert queue.current.title == ""
    assert parse_queue_context(serialize_queue_context(queue)).current.metadata == metadata


def test_direct_metadata_fields_override_optional_didl_hints():
    metadata = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                '<item><dc:title>DIDL title</dc:title><res duration="00:03:00"/></item></DIDL-Lite>')
    fields = (f"<Metadata>{escape(metadata)}</Metadata><Title>Actual title</Title>"
              "<PicUrl>https://art.example.test/direct.jpg</PicUrl><Duration>12.5</Duration>"
              "<StreamType>episode</StreamType><ContentType>audio/ogg</ContentType>")
    item = parse_queue_context(playlist(track(1, fields=fields))).current
    assert item.title == "Actual title"
    assert item.artwork == "https://art.example.test/direct.jpg"
    assert item.duration == 12.5
    assert item.stream_type == "episode"
    assert item.content_type == "audio/ogg"


def test_serialization_is_fresh_after_queue_changes():
    queue = parse_queue_context(playlist(track(1) + track(2)))
    queue.name = "New identity & more"
    queue.index = 2
    root = ET.fromstring(serialize_queue_context(queue))
    assert root.findtext("QueueName") == queue.name
    assert root.findtext("ListName") == queue.list_name
    assert root.findtext("Count") == root.findtext("ListInfo/TrackNumber") == "2"
    assert root.findtext("Index") == root.findtext("ListInfo/StartPlayIndex") == "2"
    assert root.findtext("Tracks/Track2/TrackNumber") == "2"
    queue.items.pop()
    queue.index = 1
    root = ET.fromstring(serialize_queue_context(queue))
    assert root.findtext("Count") == root.findtext("ListInfo/TrackNumber") == "1"
    assert root.find("Tracks/Track2") is None
    assert parse_queue_context(serialize_queue_context(queue)) == queue


def test_model_roundtrip_preserves_empty_fields_and_independent_instances():
    queue = PlaybackQueue(
        name="Identity", list_name="Display", source="Queue source", index=1,
        items=[QueueItem(url="https://media.example.test/a.ogg", metadata="opaque")],
    )
    assert parse_queue_context(serialize_queue_context(queue)) == queue
    empty = PlaybackQueue()
    another = PlaybackQueue()
    assert empty.items is not another.items
    assert parse_queue_context(serialize_queue_context(empty)) == empty


@pytest.mark.parametrize("index", [-1, 0, 3, True, "1", 1.5])
def test_nonempty_queue_rejects_out_of_range_or_nonnumeric_index(index):
    queue = parse_queue_context(playlist(track(1) + track(2)))
    with pytest.raises(QueueError, match="index"):
        queue.validate_index(index)
    assert queue.index == 1


@pytest.mark.parametrize("index", [1, -1, False, "0"])
def test_empty_queue_only_allows_integer_zero(index):
    with pytest.raises(QueueError, match="index"):
        PlaybackQueue(index=index)


@pytest.mark.parametrize("value", ["0", "3", "-1", "1.0", "bad", "", "999999999999999999999"])
def test_invalid_wire_start_index_is_rejected(value):
    with pytest.raises(QueueError, match="index"):
        parse_queue_context(playlist(track(1) + track(2), info=f"<StartPlayIndex>{value}</StartPlayIndex>"))


@pytest.mark.parametrize("value", ["0", "1", "3", "-2", "2.0", "bad", ""])
def test_declared_count_must_be_valid_and_match_materialized_tracks(value):
    with pytest.raises(QueueError, match="count"):
        parse_queue_context(playlist(track(1) + track(2), info=f"<TrackNumber>{value}</TrackNumber>"))


@pytest.mark.parametrize("context", [
    playlist(track(1), info="<TrackNumber>1</TrackNumber>", fields="<Count>2</Count>"),
    playlist(track(1) + track(2), info="<StartPlayIndex>1</StartPlayIndex>", fields="<Index>2</Index>"),
    playlist(info="<TrackNumber>1</TrackNumber><StartPlayIndex>1</StartPlayIndex>"),
    playlist(info="<SourceName>Provider</SourceName><StreamId>x</StreamId><TrackNumber>1</TrackNumber><StartPlayIndex>2</StartPlayIndex>"),
])
def test_inconsistent_counts_indices_or_nonprovider_missing_tracks_rejected(context):
    with pytest.raises(QueueError):
        parse_queue_context(context)


@pytest.mark.parametrize("tracks", [
    track(2), track(1) + track(3), track(1) + track(1),
    "<Track0><URL>https://media.example.test/zero.ogg</URL></Track0>",
    "<Track01><URL>https://media.example.test/zero.ogg</URL></Track01>",
    "<URL>https://media.example.test/a.ogg</URL>",
    track(1, fields="<TrackNumber>2</TrackNumber>"),
])
def test_invalid_track_labels_or_numbers_rejected(tracks):
    with pytest.raises(QueueError):
        parse_queue_context(playlist(tracks))


@pytest.mark.parametrize("context", [
    "", "<PlayList>", "<Other/>", "<QueueContext><PlayList/></QueueContext>",
    "<PlayList><ListName>a</ListName><ListName>b</ListName></PlayList>",
    "<PlayList><Tracks/><Tracks/></PlayList>",
    "<PlayList><ListInfo/><ListInfo/></PlayList>",
    "<PlayList>unexpected text</PlayList>",
    "<PlayList><Tracks>unexpected text</Tracks></PlayList>",
    "<PlayList><ListInfo>unexpected text</ListInfo></PlayList>",
    playlist(track(1, fields="<URL>https://other.example.test/b.ogg</URL>")),
    playlist(track(1, fields="<Metadata><nested/></Metadata>")),
    playlist(track(1), info="<StartPlayIndex><nested>1</nested></StartPlayIndex>"),
])
def test_malformed_or_ambiguous_structure_rejected(context):
    with pytest.raises(QueueError):
        parse_queue_context(context)


@pytest.mark.parametrize("declaration", [
    '<!DOCTYPE PlayList [<!ENTITY example "x">]>',
    '<!DOCTYPE PlayList SYSTEM "https://never-fetch.example.test/external.dtd">',
    '<!ENTITY example SYSTEM "file:///not-a-real-file">',
])
def test_dtds_and_entity_declarations_are_rejected(declaration):
    with pytest.raises(QueueError, match="DTDs and entities"):
        parse_queue_context(declaration + "<PlayList/>")


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-be", "utf-16-le", "utf-32"])
def test_non_utf8_encoding_cannot_bypass_dtd_checks(encoding):
    malicious = '<!DOCTYPE PlayList [<!ENTITY e "x">]><PlayList><ListName>&e;</ListName></PlayList>'
    with pytest.raises(QueueError):
        parse_queue_context(malicious.encode(encoding))


def test_limits_apply_to_utf8_bytes_tracks_declared_counts_and_depth():
    context = playlist(list_name="日本語")
    size = len(context.encode("utf-8"))
    assert parse_queue_context(context.encode("utf-8"), max_bytes=size).list_name == "日本語"
    with pytest.raises(QueueError, match="byte limit"):
        parse_queue_context(context, max_bytes=size - 1)
    with pytest.raises(QueueError, match="byte limit"):
        parse_queue_context(" " * (MAX_QUEUE_BYTES + 1))
    with pytest.raises(QueueError, match="track limit"):
        parse_queue_context(playlist(track(1) + track(2)), max_tracks=1)
    with pytest.raises(QueueError, match="track limit"):
        parse_queue_context(playlist(info=f"<TrackNumber>{MAX_QUEUE_TRACKS + 1}</TrackNumber>"))
    with pytest.raises(QueueError, match="depth limit"):
        parse_queue_context("<PlayList>" + "<Extension>" * 40 + "</Extension>" * 40 + "</PlayList>")


@pytest.mark.parametrize("kwargs", [{"max_bytes": 0}, {"max_bytes": True}, {"max_tracks": 0}, {"max_tracks": MAX_QUEUE_TRACKS + 1}])
def test_invalid_parser_limits_are_rejected(kwargs):
    with pytest.raises(QueueError, match="limit"):
        parse_queue_context("<PlayList/>", **kwargs)


@pytest.mark.parametrize("value", ["5", "-1", "bad"])
def test_unsupported_loop_modes_are_rejected(value):
    with pytest.raises(QueueError, match="loop mode"):
        parse_queue_context(playlist(fields=f"<LoopMode>{value}</LoopMode>"))


@pytest.mark.parametrize("duration", ["nan", "inf", "-1", "bad", "01:61:00"])
def test_invalid_direct_duration_is_rejected(duration):
    with pytest.raises(QueueError, match="duration"):
        parse_queue_context(playlist(track(1, fields=f"<Duration>{duration}</Duration>")))


def test_invalid_mutable_model_cannot_be_serialized():
    queue = PlaybackQueue(items=[QueueItem(url="https://media.example.test/a.ogg")], index=1)
    queue.index = 0
    with pytest.raises(QueueError, match="index"):
        serialize_queue_context(queue)
    queue.index = 1
    queue.loop = 5
    with pytest.raises(QueueError, match="loop"):
        serialize_queue_context(queue)
    queue.loop = 4
    queue.items[0] = replace(queue.items[0], metadata="invalid\x00text")
    with pytest.raises(QueueError, match="XML characters"):
        serialize_queue_context(queue)


def test_namespaced_playlist_uses_direct_fields_only():
    queue = parse_queue_context(
        '<p:PlayList xmlns:p="urn:example:queue"><p:ListName>Namespaced</p:ListName>'
        '<p:Tracks><p:Track1><p:URL>https://media.example.test/a.ogg</p:URL>'
        '</p:Track1></p:Tracks></p:PlayList>'
    )
    assert queue.name == "Namespaced"
    assert queue.current.url == "https://media.example.test/a.ogg"


def test_carriage_returns_and_xml_sensitive_text_roundtrip_as_text():
    queue = PlaybackQueue(
        name="Queue & <雨>", list_name="A\r\nB", index=1,
        items=[QueueItem(url="https://media.example.test/a?x=1&y=2",
                         metadata="opaque\r\ntext\rwith\ttabs & < &amp;")],
    )
    assert parse_queue_context(serialize_queue_context(queue)) == queue
