"""UNIT: byte-preserving metadata edits, including existing regex quirks."""

import re

import pytest

from fake_linkplay.http_api import HttpApi
from fake_linkplay import metadata_formatting


@pytest.fixture(params=["http_api", "module"])
def formatters(request):
    if request.param == "module":
        return (metadata_formatting.metadata_with_art, metadata_formatting.metadata_with_url,
                metadata_formatting.metadata_with_duration)
    api = HttpApi.__new__(HttpApi)
    return api._metadata_with_art, api._metadata_with_url, api._metadata_with_duration


@pytest.mark.parametrize(("metadata", "art", "live", "expected"), [
    ("", "art:opaque", True, ""),
    ("opaque\r\n\t雨", "", None, "opaque\r\n\t雨"),
    ("<item><unknown/></item>", "art:opaque", False,
     "<item><unknown/><upnp:albumArtURI>art:opaque</upnp:albumArtURI>"
     "<song:image>art:opaque</song:image><song:isLive>0</song:isLive></item>"),
    ("malformed <item", "art:opaque", True,
     "malformed <item<upnp:albumArtURI>art:opaque</upnp:albumArtURI>"
     "<song:image>art:opaque</song:image><song:isLive>1</song:isLive>"),
    ("<item><UPNP:albumArtURI x='1'>first\nold</UPNP:albumArtURI>"
     "<upnp:albumArtURI>second</upnp:albumArtURI><song:image>old</song:image>"
     "<song:isLive>1</song:isLive></item>", "art:雨&<>'\"\r\n\t", False,
     "<item><upnp:albumArtURI>art:雨&amp;&lt;&gt;&#x27;&quot;\r\n\t</upnp:albumArtURI>"
     "<upnp:albumArtURI>second</upnp:albumArtURI>"
     "<song:image>art:雨&amp;&lt;&gt;&#x27;&quot;\r\n\t</song:image>"
     "<song:isLive>0</song:isLive></item>"),
    ("<d:item><m:albumArtURI>old</m:albumArtURI></d:item>", "art:%2F%26", None,
     "<d:item><m:albumArtURI>old</m:albumArtURI>"
     "<upnp:albumArtURI>art:%2F%26</upnp:albumArtURI>"
     "<song:image>art:%2F%26</song:image></d:item>"),
    ("<item><x:upnp:albumArtURI>old</x:upnp:albumArtURI></item>", "art:opaque", None,
     "<item><upnp:albumArtURI>art:opaque</upnp:albumArtURI>"
     "<song:image>art:opaque</song:image></item>"),
    ("<item/>", r"art:\n", None,
     r"<item/><upnp:albumArtURI>art:\n</upnp:albumArtURI><song:image>art:\n</song:image>"),
    ("<item><upnp:albumArtURI>old</upnp:albumArtURI></item>", r"art:\n", None,
     "<item><upnp:albumArtURI>art:\n</upnp:albumArtURI>"
     r"<song:image>art:\n</song:image></item>"),
])
def test_artwork_preserves_exact_text_and_existing_tag_matching(formatters, metadata, art, live, expected):
    with_art, _, _ = formatters
    assert with_art(metadata, art, live) == expected


@pytest.mark.parametrize(("art", "error"), [(r"art:\1", re.error), (r"art:\Q", re.error),
                                              (r"art:\g<missing>", re.error)])
def test_artwork_preserves_existing_replacement_string_errors(formatters, art, error):
    with_art, _, _ = formatters
    with pytest.raises(error):
        with_art("<upnp:albumArtURI>old</upnp:albumArtURI>", art)


@pytest.mark.parametrize(("metadata", "url", "expected"), [
    ("", "opaque:雨", ""),
    ("<res>old</res>", "", "<res>old</res>"),
    ("<res><broken", "opaque:雨", "<res><broken"),
    ("<d:res>old</d:res><res/>", "opaque:雨", "<d:res>old</d:res><res/>"),
    (" \r\n<RES duration='9'>old\r\ntext</RES><res>second</res>\t",
     "opaque:%2F雨&<>'\"\r\n\t" + r"\1\g<missing>",
     " \r\n<RES duration='9'>opaque:%2F雨&amp;&lt;&gt;&#x27;&quot;\r\n\t"
     + r"\1\g&lt;missing&gt;</RES><res>second</res>" + "\t"),
])
def test_resource_url_preserves_exact_text_and_first_unprefixed_tag(formatters, metadata, url, expected):
    _, with_url, _ = formatters
    assert with_url(metadata, url) == expected


@pytest.mark.parametrize(("metadata", "duration", "expected"), [
    ("", -1, ""),
    ("opaque\r\n\t雨", 7, "opaque\r\n\t雨"),
    ('<res duration="12">old</res><res duration="13">second</res>', -3,
     '<res duration="0">old</res><res duration="13">second</res>'),
    ("<res duration='12'>old</res>", 7, "<res duration='12'>old</res>"),
    ('<res duration ="12">old</res>', 7, '<res duration="7" duration ="12">old</res>'),
    ('<res>first</res><res duration="12">second</res>', 7,
     '<res>first</res><res duration="7">second</res>'),
    ('\r\n<RES DURATION="01:02:03">雨</RES>\t', 7,
     '\r\n<RES DURATION="7">雨</RES>\t'),
    ("<d:res>old</d:res><res/>", 7, '<d:res>old</d:res><res duration="7"/>'),
    ("<res", 7, '<res duration="7"'),
])
def test_duration_preserves_existing_attribute_and_malformed_tag_rules(formatters, metadata, duration, expected):
    _, _, with_duration = formatters
    assert with_duration(metadata, duration) == expected


def test_false_metadata_short_circuits_argument_validation(formatters):
    with_art, with_url, with_duration = formatters
    assert with_art(None, object()) is None
    assert with_url(None, object()) is None
    assert with_duration(None, object()) is None
    with pytest.raises(TypeError):
        with_duration("<res/>", "7")
