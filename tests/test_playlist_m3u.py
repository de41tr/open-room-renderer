"""Strict M3U parsing is pure; all inputs and URL secrets are synthetic."""
from __future__ import annotations

import pytest

from fake_linkplay.playlist_files import MAX_PLAYLIST_BYTES, MAX_PLAYLIST_URLS
from fake_linkplay.playlist_m3u import M3UError, decode_m3u_urls

URLS = [
    "HTTPS://Media.example.test/音声/a%2fb?token=synthetic-secret&a=1+2&a=&case=%2f%2F#片",
    "http://[::1]:8080/café.m3u8?x=a%2520b#fragment",
    "https://media.example.test/cafe\u0301.wav",
]


def rejects(encoded):
    with pytest.raises(M3UError) as caught:
        decode_m3u_urls(encoded)
    assert str(caught.value) == "Invalid M3U URL list"


def test_exact_urls_keep_order_duplicates_unicode_and_final_unterminated_line():
    urls = [*URLS, URLS[0]]
    encoded = "\n".join(urls).encode("utf-8")
    assert decode_m3u_urls(encoded) == urls
    assert [url.encode("utf-8") for url in decode_m3u_urls(encoded)] == [
        url.encode("utf-8") for url in urls]


def test_bom_crlf_header_simple_metadata_and_only_explicit_comments():
    encoded = ("\ufeff# before the header\r\n\r\n#EXTM3U\r\n"
               "#EXTINF:-1,Untrusted title, with a comma\r\n#\r\n# between record and URL\r\n"
               f"{URLS[0]}\r\n#EXTINF:0,\r\n{URLS[1]}\r\n"
               f"#EXTINF:012.50,Unicode 夜\r\n{URLS[2]}\r\n").encode("utf-8")
    assert decode_m3u_urls(encoded) == URLS
    assert decode_m3u_urls(("# comment\n\n" + URLS[0] + "\n# trailing comment\n").encode()) == URLS[:1]


def test_url_lines_are_not_stripped_or_split_on_other_unicode_separators():
    # These remain invalid URL candidates for explicit_queue, never repaired.
    lines = [" " + URLS[0], URLS[1] + "\t", URLS[2] + "\u0085tail", URLS[0] + "\u2028tail"]
    assert decode_m3u_urls("\n".join(lines).encode()) == lines


def test_rejects_invalid_encodings_embedded_boms_nuls_and_bare_cr():
    for encoded in (
        b"\xffsynthetic-secret", URLS[0].encode("utf-16"),
        URLS[0].encode("utf-16-le"), URLS[0].encode("utf-16-be"),
        ("\ufeff\ufeff" + URLS[0]).encode(), ("\n\ufeff" + URLS[0]).encode(),
        (URLS[0] + "\ufeff").encode(), ("# secret\ufeff\n" + URLS[0]).encode(),
        ("#EXTM3U\n#EXTINF:1,secret\ufeff\n" + URLS[0]).encode(),
        ("# nul\x00\n" + URLS[0]).encode(),
        (URLS[0] + "\r").encode(), (URLS[0] + "\r" + URLS[1]).encode(),
        ("#EXTM3U\r\r\n" + URLS[0]).encode(),
    ):
        rejects(encoded)


def test_header_must_be_exact_unique_and_before_entries():
    for text in (
        "#EXTM3U\n#EXTM3U\n" + URLS[0], URLS[0] + "\n#EXTM3U\n" + URLS[1],
        "#EXTM3U \n" + URLS[0], "#extm3u\n" + URLS[0],
        "#EXTM3U\n#EXTINF:1,title\n#EXTM3U\n" + URLS[0],
    ):
        rejects(text.encode())


def test_unknown_hash_records_including_hls_and_player_options_fail_closed():
    for directive in (
        "#comment", "## comment", "#\tcomment", "#EXT-X-VERSION:3",
        "#EXT-X-STREAM-INF:BANDWIDTH=800000", "#EXT-X-ENDLIST",
        "#EXTVLCOPT:http-referrer=https://synthetic-secret.example.test/",
        "#KODIPROP:inputstream.adaptive.manifest_type=hls", "#PLAYLIST:title", "#EXTGRP:group",
    ):
        rejects(("#EXTM3U\n" + directive + "\n" + URLS[0]).encode())


def test_extinf_requires_simple_duration_header_and_one_paired_url():
    for duration in ("", "+1", "-0", "-1.0", "-2", "1e3", "NaN", "inf", "1.", ".5", " 1", "1 ", "١", '1 tvg-id="secret"'):
        rejects((f"#EXTM3U\n#EXTINF:{duration},title\n" + URLS[0]).encode())
    for text in (
        "#EXTINF:1,title\n" + URLS[0], "#EXTM3U\n#EXTINF:1\n" + URLS[0],
        "#EXTM3U\n#EXTINF:1,title\n#EXTINF:2,second\n" + URLS[0],
        "#EXTM3U\n#EXTINF:1,title", "#EXTM3U\n#EXTINF:1,title\n# last comment\n",
        "#EXTM3U\n" + URLS[0] + "\n#EXTINF:1,orphan",
    ):
        rejects(text.encode())


def test_empty_documents_and_url_count_limit():
    for encoded in (b"", b"\xef\xbb\xbf", b"#EXTM3U\n", b"\n#\n# only a comment\n"):
        rejects(encoded)
    urls = [URLS[0]] * MAX_PLAYLIST_URLS
    assert decode_m3u_urls("\n".join(urls).encode()) == urls
    rejects("\n".join([*urls, URLS[0]]).encode())


def test_input_byte_bound_includes_metadata_and_uses_utf8_bytes():
    tail = ("\n" + URLS[0]).encode()
    encoded = b"# " + b"x" * (MAX_PLAYLIST_BYTES - 2 - len(tail)) + tail
    assert len(encoded) == MAX_PLAYLIST_BYTES
    assert decode_m3u_urls(encoded) == URLS[:1]
    rejects(encoded + b"\n")
    rejects(encoded.replace(b"x", "é".encode(), 1))
    for invalid_type in (None, "text", bytearray(URLS[0].encode())):
        rejects(invalid_type)
