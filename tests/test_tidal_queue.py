"""UNIT: provider references, not login, playback or application acceptance."""
from html import escape

import pytest

from fake_linkplay.queue import QueueError, parse_queue_context
from fake_linkplay.tidal_queue import TidalQueueError, parse_tidal_queue, validate_catalogue_url


CATALOGUE = "https://api.tidal.com/v1/albums/123/tracks?countryCode=DE&offset=0&limit=50"


def selection(*, tracks="", fields="", catalogue=CATALOGUE):
    return ("<PlayList><ListName>Synthetic selection</ListName><ListInfo>"
            "<SourceName>Tidal</SourceName>"
            f"<SearchUrl>{escape(catalogue)}</SearchUrl>{fields}</ListInfo>"
            f"<Tracks>{tracks}</Tracks></PlayList>")


def test_remote_selection_keeps_start_without_faking_playable_items():
    queue = parse_tidal_queue(selection(fields="<TrackNumber>0</TrackNumber>"
                                       "<StartPlayIndex>5</StartPlayIndex>"
                                       "<requestQuality>LOSSLESS</requestQuality>"))
    assert queue.tracks == ()
    assert queue.start_index == 5
    assert queue.catalogue_url == CATALOGUE
    assert queue.request_quality == "LOSSLESS"
    assert "Synthetic selection" not in repr(queue)
    assert CATALOGUE not in repr(queue)


def test_track_identity_can_be_resolved_without_an_audio_url():
    xml = selection(catalogue="", tracks="<Track1><Id>42</Id><Source>Tidal</Source>"
                    "<URL/><Metadata>opaque &amp; untouched</Metadata></Track1>",
                    fields="<TrackNumber>1</TrackNumber><Quality>2</Quality>")
    queue = parse_tidal_queue(xml)
    assert queue.tracks[0].track_id == "42"
    assert queue.tracks[0].catalogue_url == ""
    assert queue.tracks[0].metadata == "opaque & untouched"
    assert queue.request_quality == "LOSSLESS"
    assert "opaque" not in repr(queue.tracks[0])
    # This must not relax direct-URL queue admission for the existing player.
    with pytest.raises(QueueError, match="direct URL"):
        parse_queue_context(xml)


def test_catalogue_metadata_is_preserved_but_never_used_as_media_fallback():
    xml = selection(tracks=f"<Track1><Id>42</Id><URL>{escape(CATALOGUE)}</URL>"
                    "<Metadata>&lt;res&gt;https://foreign.example.test/audio&lt;/res&gt;</Metadata>"
                    "</Track1>")
    queue = parse_tidal_queue(xml)
    assert queue.tracks[0].catalogue_url == CATALOGUE
    assert "foreign.example.test" in queue.tracks[0].metadata
    assert not hasattr(queue.tracks[0], "resolved_url")


@pytest.mark.parametrize("url", [
    "http://api.tidal.com/v1/tracks/42",
    "https://api.tidal.com.evil.example/v1/tracks/42",
    "https://api.tidal.com@evil.example/v1/tracks/42",
    "https://user:password@api.tidal.com/v1/tracks/42",
    "https://api.tidal.com:444/v1/tracks/42",
    "https://api.tidal.com/v1/tracks/42#private",
    "https://api.tidal.com/v1/tracks/../users/1",
    "https://api.tidal.com/v1/tracks/%2e%2e/users/1",
    "https://api.tidal.com/v1/tracks/42?access_token=synthetic-secret",
    "https://api.tidal.com/v1/tracks/42?token=synthetic-secret",
    "https://api.tidal.com/v1/tracks/42?limit=1&limit=2",
    "https://api.tidal.com/v1/tracks/42?offset=-1",
    "https://api.tidal.com/v1/tracks/42?query=%0d%0aAuthorization",
    "https://api.tidal.com/v1/tracks/42\n",
    "https://api.tidal.com\\@foreign.example/v1/tracks/42",
    "https://api.tidal.com/v1/oauth2/token",
])
def test_catalogue_boundary_rejects_foreign_or_credential_bearing_targets(url):
    with pytest.raises(TidalQueueError) as error:
        validate_catalogue_url(url)
    assert url not in str(error.value)
    assert "synthetic-secret" not in str(error.value)


@pytest.mark.parametrize("fields,tracks", [
    ("<StartPlayIndex>0</StartPlayIndex>", ""),
    ("<StartPlayIndex>1</StartPlayIndex><Index>2</Index>", ""),
    ("<requestQuality>UNKNOWN</requestQuality>", ""),
    ("<TrackNumber>2</TrackNumber>", "<Track1><Id>42</Id></Track1>"),
    ("<StartPlayIndex>2</StartPlayIndex>", "<Track1><Id>42</Id></Track1>"),
    ("", "<Track2><Id>42</Id></Track2>"),
    ("", "<Track1><Id>42</Id></Track1><Track1><Id>43</Id></Track1>"),
    ("", "<Track1><Id>42</Id><Source>Other</Source></Track1>"),
    ("", "<Track1><Id>42</Id><Id>43</Id></Track1>"),
    ("", "<Track1><Id>42</Id><URL>https://foreign.example/audio</URL></Track1>"),
    ("", "<Track1><Metadata>&lt;Id&gt;42&lt;/Id&gt;</Metadata></Track1>"),
])
def test_malformed_or_mixed_provider_selection_is_rejected(fields, tracks):
    with pytest.raises(TidalQueueError):
        parse_tidal_queue(selection(fields=fields, tracks=tracks))


def test_dtd_depth_size_and_duplicate_containers_retain_existing_xml_limits():
    for xml in [
        '<!DOCTYPE x [<!ENTITY e "hello">]>' + selection(),
        selection().replace("<ListName>", "<ListInfo/><ListName>"),
        selection().replace("<Tracks>", "<Tracks>" + "<x>" * 33)
                   .replace("</Tracks>", "</x>" * 33 + "</Tracks>"),
        selection(fields="<Extension>" + "x" * (1024 * 1024) + "</Extension>"),
    ]:
        with pytest.raises(TidalQueueError):
            parse_tidal_queue(xml)


def test_explicit_empty_selection_or_wrong_source_does_not_become_a_queue():
    with pytest.raises(TidalQueueError):
        parse_tidal_queue(selection(catalogue=""))
    with pytest.raises(TidalQueueError):
        parse_tidal_queue(selection().replace("<SourceName>Tidal", "<SourceName>newTuneIn"))
