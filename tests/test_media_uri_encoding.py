"""URI bytes survive actual client/query decoding; audio transport is MOCK."""
from urllib.parse import quote

import pytest

from fake_linkplay.http_api import parse_command

MEDIA_URI = "https://media.example.test/recording%2Fpart.wav?signature=a%26b%3Dc&literal=%252F&name=caf%C3%A9"


@pytest.mark.parametrize("prefix,tail,name", [
    ("setPlayerCmd:play:", "", "play"),
    ("setPlayerCmd:m3u:play:", "", "m3u_play"),
    ("setPlayerCmd:playlist:", ":2", "playlist"),
])
@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("uri", [MEDIA_URI, "HTTP://media.example.test/a%3Ab?x=%25", "file:///tmp/tone%25.wav"])
def test_uri_parser_preserves_resource_bytes(prefix, tail, name, wrapped, uri):
    parsed = parse_command(prefix + (quote(uri, safe="") if wrapped else uri) + tail)
    assert parsed.name == name
    assert parsed.args == ((uri, "2") if name == "playlist" else (uri,))
