"""Synthetic secret markers only; verify diagnostics do not expose URL data."""
from __future__ import annotations

import logging
from urllib.parse import quote

import pytest

from fake_linkplay.http_api import HttpApi, ParsedCommand
from fake_linkplay.log_safety import redact_media_urls
from fake_linkplay.mpv_backend import redact_sensitive
from fake_linkplay.utils import RedactingFormatter
from test_music_presets import configured, running


URL = ("https://synthetic-user:synthetic-password@media.example.test/station.m3u"
       "?account=synthetic-account&signature=synthetic-signature&unfamiliar=synthetic-value"
       "#synthetic-fragment")
MARKERS = ("synthetic-user", "synthetic-password", "synthetic-account", "synthetic-signature",
           "synthetic-value", "synthetic-fragment")


@pytest.mark.parametrize("encode", [lambda value: value, lambda value: quote(value, safe=""),
                                     lambda value: quote(quote(value, safe=""), safe="")])
@pytest.mark.parametrize("sanitize", [redact_media_urls, redact_sensitive, HttpApi._redact_sensitive_text])
def test_raw_and_encoded_url_credentials_and_arbitrary_query_values_are_hidden(encode, sanitize):
    encoded = encode(URL)
    text = sanitize("command=setPlayerCmd:play:" + encoded)
    assert all(marker not in text for marker in MARKERS)
    assert ("media.example.test/station.m3u" in text and "?[redacted]" in text) or "[redacted-media-url]" in text
    assert encoded == encode(URL)  # Logging never mutates a playback argument.


def test_xml_url_queries_and_invalid_authority_are_safe():
    assert "synthetic-account" not in redact_media_urls(URL.replace("&", "&amp;"))
    assert redact_media_urls("http://[invalid?secret=synthetic-value") == "[invalid-media-url]"


def test_final_formatter_sanitizes_access_request_line_and_exception_traceback():
    try:
        raise RuntimeError("Could not open " + URL)
    except RuntimeError:
        import sys
        record = logging.LogRecord("aiohttp.access", logging.ERROR, __file__, 1,
                                   'GET /httpapi.asp?command=%s HTTP/1.1',
                                   (quote(URL, safe=""),), sys.exc_info())
    rendered = RedactingFormatter("%(name)s %(message)s").format(record)
    assert all(marker not in rendered for marker in MARKERS)
    assert "Traceback" in rendered
    assert "RuntimeError" in rendered


@pytest.mark.asyncio
async def test_actual_readiness_logs_are_sanitized_but_backend_gets_original_url(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="fake_linkplay.http_api")
    async with running(configured(tmp_path)) as (api, backend, server, session):
        await api.execute(ParsedCommand("play", (URL,)), "127.0.0.1")
        assert backend.url == URL
        messages = "\n".join(record.getMessage() for record in caplog.records
                             if record.name.startswith("fake_linkplay."))
        assert "audio-ready" in messages
        assert all(marker not in messages for marker in MARKERS)


@pytest.mark.parametrize("value", [
    "https://user:SENSITIVE%2Fpassword@media.example.test/song",
    "https://media.example.test/song?account=abc%26SENSITIVE%3Dxyz",
    "https://media.example.test/song?account=foo'SENSITIVE",
    "https://media.example.test/song?account=SENSITIVE%2526name%253Dvalue",
])
def test_encoded_delimiters_and_legal_apostrophe_cannot_escape_redaction(value):
    for candidate in (value, quote(value, safe=""), quote(quote(value, safe=""), safe="")):
        sanitized = redact_media_urls(candidate)
        assert "SENSITIVE" not in sanitized
        assert redact_media_urls(sanitized) == sanitized


@pytest.mark.parametrize("encode", [quote,
    lambda value: value.replace("://", ":%2F%2F"),
    lambda value: value.replace("/", "\\/"),
    lambda value: value.replace("/", "\\u002f").replace(":", "\\u003a"),
])
def test_partial_wrapper_and_json_slash_encodings_are_sanitized(encode):
    sanitized = redact_media_urls(encode(URL))
    assert all(marker not in sanitized for marker in MARKERS)
    assert "[redacted-media-url]" in sanitized
    assert redact_media_urls(sanitized) == sanitized


@pytest.mark.parametrize("value", [
    "https://SENSITIVE&sol;x:password@media.example.test/song",
    "https://SENSITIVE&quest;x:password@media.example.test/song",
])
def test_literal_html_entities_in_userinfo_do_not_become_url_delimiters(value):
    result = redact_media_urls(value)
    assert "SENSITIVE" not in result
    assert redact_media_urls(result) == result


def test_path_entities_are_not_repeatedly_decoded_by_formatting():
    value = "https://media.example.test/foo&amp;amp;bar"
    assert redact_media_urls(value) == value
    assert redact_media_urls(redact_media_urls(value)) == value


@pytest.mark.parametrize("sanitize", [redact_sensitive, HttpApi._redact_sensitive_text])
def test_private_address_placeholder_survives_repeat_sanitization(sanitize):
    result = sanitize("http://192.168.1.2/song.mp3?account=SENSITIVE")
    assert "[private-ip]/song.mp3?[redacted]" in result
    assert sanitize(result) == result
    assert redact_media_urls(result) == result


@pytest.mark.parametrize("value", [
    quote("https://SENSITIVE%2Fuser:password@host/song?account=SENSITIVE", safe="/%"),
    r"https:\/\/host/song\u003faccount=SENSITIVE",
    r"https:\/\/SENSITIVE\u0040host/song",
])
def test_ambiguous_encoded_wrappers_are_masked_as_a_whole(value):
    assert redact_media_urls(value) == "[redacted-media-url]"
