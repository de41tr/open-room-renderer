"""URI bytes survive actual client/query decoding; audio transport is MOCK."""
from urllib.parse import quote

import pytest

from fake_linkplay.http_api import parse_command

MEDIA_URI = "https://media.example.test/recording%2Fpart.wav?signature=a%26b%3Dc&literal=%252F&name=caf%C3%A9"


@pytest.mark.parametrize("prefix,tail,name", [
    ("setPlayerCmd:play:", "", "play"),
    ("setPlayerCmd:m3u:play:", "", "m3u_play"),
    ("setPlayerCmd:playlist:", "", "playlist"),
])
@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("uri", [MEDIA_URI, "HTTP://media.example.test/a%3Ab?x=%25", "file:///tmp/tone%25.wav"])
def test_uri_parser_preserves_resource_bytes(prefix, tail, name, wrapped, uri):
    parsed = parse_command(prefix + (quote(uri, safe="") if wrapped else uri) + tail)
    assert parsed.name == name
    assert parsed.args == (uri,)


@pytest.mark.parametrize("uri", [
    "https://media.example.test:8443/list.m3u",
    "http://[::1]:8080/list.pls",
    "https://media.example.test/list.m3u:3",
    "https://media.example.test/a:b.m3u?signature=x:y&end=:3",
    MEDIA_URI + "&end=:3",
])
def test_playlist_literal_colons_are_resource_bytes_not_indexes(uri):
    for value in (uri, quote(uri, safe="")):
        assert parse_command("setPlayerCmd:playlist:" + value).args == (uri,)


@pytest.mark.parametrize("index", ["0", "3", "-1", "bogus", "", "0:1"])
def test_only_fully_quoted_inner_uri_has_distinguishable_legacy_index(index):
    parsed = parse_command("setPlayerCmd:playlist:" + quote(MEDIA_URI, safe="") + ":" + index)
    assert parsed.args == (MEDIA_URI, index)
