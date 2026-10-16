"""One bounded, explicitly requested TIDAL catalogue page; no paging loop.

Raw streaming eligibility and item types are checked before any filtering.
Reject ambiguous/mixed pages rather than shifting the caller's local index.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import parse_qsl, urlsplit

from .tidal_playback import TidalPlaybackError
from .tidal_queue import TidalQueueError, validate_catalogue_url

MAX_PAGE_ITEMS = 50
MAX_PAGE_TOTAL = 1_000_000_000


@dataclass(frozen=True, repr=False)
class TidalPageRequest:
    catalogue_url: str
    path: str
    params: dict[str, str | int]
    offset: int
    limit: int
    playlist: bool


@dataclass(frozen=True, repr=False)
class TidalPageCandidate:
    catalogue_url: str
    track_ids: tuple[str, ...]
    session_fingerprint: bytes
    session_expires_at: float


def parse_page_request(url: str) -> TidalPageRequest:
    try:
        validate_catalogue_url(url)
        parts = urlsplit(url)
        album = re.fullmatch(r"/v1/albums/[1-9][0-9]{0,19}/tracks", parts.path)
        playlist = re.fullmatch(r"/v1/playlists/[A-Za-z0-9][A-Za-z0-9_-]{0,127}/items", parts.path)
        if not album and not playlist:
            raise ValueError
        params = dict(parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True,
                                max_num_fields=16))
        offset, limit = int(params.get("offset", "0")), int(params.get("limit", "50"))
        if not 0 <= offset < MAX_PAGE_TOTAL or not 1 <= limit <= MAX_PAGE_ITEMS:
            raise ValueError
        params.setdefault("offset", offset)
        params.setdefault("limit", limit)
        return TidalPageRequest(url, parts.path.removeprefix("/v1/"), params,
                                offset, limit, bool(playlist))
    except (TidalQueueError, ValueError, TypeError, UnicodeError):
        raise TidalPlaybackError("invalid_page_selection") from None


def page_track_ids(value: dict, request: TidalPageRequest) -> tuple[str, ...]:
    """Preserve every returned slot; do not filter inaccessible/video items."""
    offset, limit, total = (value.get(key) for key in ("offset", "limit", "totalNumberOfItems"))
    items = value.get("items")
    if (type(offset) is not int or offset != request.offset
            or type(limit) is not int or limit != request.limit
            or type(total) is not int or not offset <= total <= MAX_PAGE_TOTAL
            or type(items) is not list or len(items) != min(limit, total - offset)):
        raise TidalPlaybackError("invalid_page_response")
    if not items:
        raise TidalPlaybackError("unsupported_page")
    result = []
    for entry in items:
        if type(entry) is not dict:
            raise TidalPlaybackError("invalid_page_response")
        # The album /tracks endpoint is audio-typed. A mixed /items endpoint
        # must instead prove each wrapper's type; never infer it from an ID.
        if request.playlist:
            if entry.get("type") != "track" or type(entry.get("item")) is not dict:
                raise TidalPlaybackError("unsupported_page")
            entry = entry["item"]
        if (("type" in entry and entry["type"] != "Track")
                or "item" in entry or entry.get("allowStreaming") is not True):
            raise TidalPlaybackError("unsupported_page")
        track_id = entry.get("id")
        if (type(track_id) not in (str, int)
                or not re.fullmatch(r"[1-9][0-9]{0,19}", str(track_id))):
            raise TidalPlaybackError("invalid_page_response")
        result.append(str(track_id))
    return tuple(result)
