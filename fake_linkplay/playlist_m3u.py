"""Pure decoder for a strict offline M3U URL-list subset, not M3U/HLS playback.

Only LF/CRLF terminators are removed. URL validation belongs to explicit_queue;
this parser never normalizes URLs, resolves relative references or fetches media.
Unsupported directives fail closed rather than being silently discarded.
"""
from __future__ import annotations

import re

from .playlist_files import MAX_PLAYLIST_BYTES, MAX_PLAYLIST_URLS, PlaylistError

_DURATION = re.compile(r"(?:-1|[0-9]+(?:\.[0-9]+)?)")
_INVALID_M3U = "Invalid M3U URL list"


class M3UError(PlaylistError):
    """Unsupported M3U input; messages never include supplied content."""


def decode_m3u_urls(encoded: bytes) -> list[str]:
    """Decode 1–1000 ordered URL lines with optional simple EXTINF records.

    A single leading UTF-8 BOM is optional. A header, if present, must occur
    once before entries. EXTINF requires that header and one subsequent URL;
    its title and duration are discarded. Only empty lines, '#' and '# ' human
    comments are otherwise ignored. A valid final URL needs no final newline.
    The input has an independent 1 MiB bound, including comments and metadata.
    """
    if type(encoded) is not bytes or len(encoded) > MAX_PLAYLIST_BYTES:
        raise M3UError(_INVALID_M3U)
    try:
        text = encoded.decode("utf-8")
    except UnicodeError:
        raise M3UError(_INVALID_M3U) from None
    if text.startswith("\ufeff"):
        text = text[1:]
    # No embedded BOMs, NULs (including BOM-less UTF-16), or bare CRs.
    text = text.replace("\r\n", "\n")
    if any(character in text for character in ("\ufeff", "\x00", "\r")):
        raise M3UError(_INVALID_M3U)
    header = False
    pending = False
    urls = []
    for line in text.split("\n"):
        if not line or line == "#" or line.startswith("# "):
            continue
        if line == "#EXTM3U":
            if header or urls or pending:
                raise M3UError(_INVALID_M3U)
            header = True
        elif line.startswith("#EXTINF:"):
            duration, comma, _title = line[len("#EXTINF:"):].partition(",")
            if not header or pending or not comma or not _DURATION.fullmatch(duration):
                raise M3UError(_INVALID_M3U)
            pending = True
        elif line.startswith("#"):
            raise M3UError(_INVALID_M3U)
        else:
            urls.append(line)
            if len(urls) > MAX_PLAYLIST_URLS:
                raise M3UError(_INVALID_M3U)
            pending = False
    if pending or not urls:
        raise M3UError(_INVALID_M3U)
    return urls
