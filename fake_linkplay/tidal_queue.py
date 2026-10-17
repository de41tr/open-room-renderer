"""Bounded TIDAL selection data, deliberately separate from playable queues.

A catalogue URL or track ID is not an audio URL. Parsing grants no provider
access, performs no I/O and must never dispatch these references to mpv.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from urllib.parse import parse_qsl, urlsplit

from .queue import (MAX_QUEUE_BYTES, MAX_QUEUE_TRACKS, QueueError, _child,
                    _local_name, _matching_integer, _parse_xml, _structure, _text)
from .queue_diagnostics import QueueRejectionReason


class TidalQueueError(ValueError):
    """Invalid selection; messages never include payloads or account data."""


_QUERY_FIELDS = frozenset({
    "countryCode", "offset", "limit", "includeImageLinks", "filter",
    "deviceType", "order", "orderDirection", "query", "types",
})
_QUALITIES = frozenset({"LOW", "HIGH", "LOSSLESS", "HI_RES", "AUTO"})
_LEGACY_QUALITY = {"0": "LOW", "1": "HIGH", "2": "LOSSLESS", "3": "HI_RES", "4": "AUTO"}


def track_id_from_reference(value: str) -> str:
    """Recognize only the exact stable track URL emitted by this renderer."""
    match = re.fullmatch(r"https://api\.tidal\.com/v1/tracks/([1-9][0-9]{0,19})", value)
    return match[1] if match else ""


def validate_catalogue_url(value: str) -> str:
    """Accept only bounded, credential-free TIDAL v1 catalogue references.

This is an admission check, not a general HTTP allowlist. A provider still
needs authorized credentials, response validation and redirect protection.
"""
    if (not isinstance(value, str) or not value or len(value) > 8192
            or any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value):
        raise TidalQueueError("Invalid TIDAL catalogue reference")
    try:
        parts = urlsplit(value)
        if (parts.scheme != "https" or parts.netloc != "api.tidal.com"
                or parts.fragment or len(parts.query) > 2048
                or not re.fullmatch(
                    r"/v1/(?:albums|artists|tracks|playlists|mixes|users|featured|search)"
                    r"(?:/[A-Za-z0-9_-]+)*/?", parts.path)):
            raise ValueError
        query = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True,
                          max_num_fields=16)
        if len({key for key, _ in query}) != len(query):
            raise ValueError
        for key, val in query:
            if key not in _QUERY_FIELDS or any(ord(c) < 32 or ord(c) == 127 for c in val):
                raise ValueError
            if key in {"offset", "limit"} and not re.fullmatch(r"[0-9]{1,9}", val):
                raise ValueError
    except (ValueError, UnicodeError):
        raise TidalQueueError("Invalid TIDAL catalogue reference") from None
    return value


@dataclass(frozen=True, repr=False)
class TidalTrackSelection:
    """Unresolved provider identity; never a backend playback instruction."""

    track_id: str
    catalogue_url: str = ""
    metadata: str = ""


@dataclass(frozen=True, repr=False)
class TidalQueueSelection:
    """Remote selection keeps its one-based start even before materialization."""

    name: str
    catalogue_url: str
    tracks: tuple[TidalTrackSelection, ...] = field(default_factory=tuple)
    start_index: int = 1
    request_quality: str = "AUTO"
    stream_id: str = ""


def parse_tidal_queue(value: str | bytes, *, name: str | None = None) -> TidalQueueSelection:
    """Parse the observed PlayList selection without inventing playable URLs.

Only provider metadata needed for later authorized resolution is retained.
Account/token fields, embedded resources and arbitrary extensions are not
used as authority or fallback media. Existing direct-URL queues are untouched.
"""
    try:
        return _parse_selection(value, name)
    except QueueError:
        raise TidalQueueError("Invalid TIDAL queue structure") from None


def _parse_selection(value: str | bytes, name: str | None) -> TidalQueueSelection:
    root = _parse_xml(value, MAX_QUEUE_BYTES)
    if _local_name(root.tag) != "PlayList":
        raise TidalQueueError("Invalid TIDAL queue structure")
    info, tracks = _child(root, "ListInfo"), _child(root, "Tracks")
    for container in (root, info, tracks):
        _structure(container)
    if _text(info, "SourceName") != "Tidal":
        raise TidalQueueError("Unsupported TIDAL queue source")
    queue_name = name if name is not None else _text(root, "QueueName")
    if queue_name is None:
        queue_name = _text(root, "ListName") or "CurrentQueue"
    if not isinstance(queue_name, str) or not queue_name.strip() or len(queue_name) > 4096:
        raise TidalQueueError("Invalid TIDAL queue name")
    catalogue = _text(info, "SearchUrl") or ""
    if catalogue:
        validate_catalogue_url(catalogue)
    quality = _text(info, "requestQuality")
    if quality is None:
        legacy = _text(info, "Quality") or "4"
        quality = _LEGACY_QUALITY.get(legacy, legacy)
    if quality not in _QUALITIES:
        raise TidalQueueError("Unsupported TIDAL quality")
    start = _matching_integer(
        [_text(root, "Index"), _text(info, "Index"), _text(info, "StartPlayIndex")],
        "index", QueueRejectionReason.INDEX)
    count = _matching_integer(
        [_text(root, "Count"), _text(info, "Count"), _text(info, "TrackNumber")],
        "count", QueueRejectionReason.COUNT)
    start = 1 if start is None else start
    if not 1 <= start <= 1_000_000_000:
        raise TidalQueueError("Invalid TIDAL start index")
    numbered = {}
    if tracks is not None:
        if len(tracks) > MAX_QUEUE_TRACKS:
            raise TidalQueueError("TIDAL queue exceeds track limit")
        for track in tracks:
            match = re.fullmatch(r"Track([1-9][0-9]{0,3})", _local_name(track.tag))
            if not match or int(match[1]) in numbered:
                raise TidalQueueError("Invalid TIDAL track numbering")
            numbered[int(match[1])] = track
    if sorted(numbered) != list(range(1, len(numbered) + 1)):
        raise TidalQueueError("Invalid TIDAL track numbering")
    selections = []
    for number in sorted(numbered):
        track = numbered[number]
        _structure(track)
        if (_text(track, "Source") or "Tidal") != "Tidal":
            raise TidalQueueError("Unsupported TIDAL track source")
        track_id = _text(track, "Id") or ""
        if not re.fullmatch(r"[0-9]{1,20}", track_id):
            raise TidalQueueError("Invalid TIDAL track identity")
        url = _text(track, "URL") or ""
        if url:
            validate_catalogue_url(url)
        selections.append(TidalTrackSelection(track_id, url, _text(track, "Metadata") or ""))
    if selections:
        if (count is not None and count != len(selections)) or start > len(selections):
            raise TidalQueueError("Invalid TIDAL queue count or index")
    elif not catalogue:
        raise TidalQueueError("TIDAL selection requires a catalogue reference or track IDs")
    return TidalQueueSelection(queue_name, catalogue, tuple(selections), start, quality,
                               _text(info, "StreamId") or "")
