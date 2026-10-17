"""One bounded, explicitly requested TIDAL catalogue page; no paging loop.

Raw streaming eligibility and item types are checked before any filtering.
Reject ambiguous/mixed pages rather than shifting the caller's local index.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import parse_qsl, urlsplit

from .tidal_playback import TidalPlaybackError
from .tidal_queue import TidalQueueError, _GENRE_TRACKS_PATH, validate_catalogue_url

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
    favorites_user_id: str = ""
    collection: str = ""


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
        favorites = re.fullmatch(r"/v1/users/([1-9][0-9]{0,18})/favorites/tracks", parts.path)
        collection = ""
        if (parts.path in {"/v1/featured/new/tracks", "/v1/featured/recommended/tracks", "/v1/featured/top/tracks",
                           "/v1/rising/new/tracks"}
                or re.fullmatch(r"/v1/artists/[1-9][0-9]{0,19}/(?:toptracks|radio)", parts.path)
                or re.fullmatch(r"/v1/tracks/[1-9][0-9]{0,19}/radio", parts.path)
                or re.fullmatch(_GENRE_TRACKS_PATH, parts.path)):
            collection = "tracks"
        elif re.fullmatch(r"/v1/mixes/[A-Za-z0-9][A-Za-z0-9_-]{0,127}/items", parts.path):
            collection = "mixed"
        if not album and not playlist and not favorites and not collection:
            raise ValueError
        if favorites and int(favorites[1]) > 2**63 - 1:
            raise ValueError
        params = dict(parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True,
                                max_num_fields=16))
        offset, limit = int(params.get("offset", "0")), int(params.get("limit", "50"))
        if not 0 <= offset < MAX_PAGE_TOTAL or not 1 <= limit <= MAX_PAGE_ITEMS:
            raise ValueError
        params.setdefault("offset", offset)
        params.setdefault("limit", limit)
        return TidalPageRequest(url, parts.path.removeprefix("/v1/"), params,
                                offset, limit, bool(playlist), favorites[1] if favorites else "",
                                collection)
    except (TidalQueueError, ValueError, TypeError, UnicodeError):
        raise TidalPlaybackError("invalid_page_selection") from None


def _collection_page(value: dict) -> dict:
    """Use only the first declared page, never merge or search shelf modules."""
    if value.get("rows") is None:
        return value
    rows = value["rows"]
    if "items" in value or type(rows) is not list or not rows or type(rows[0]) is not dict:
        raise TidalPlaybackError("invalid_page_response")
    modules = rows[0].get("modules")
    if type(modules) is not list or not modules or type(modules[0]) is not dict:
        raise TidalPlaybackError("invalid_page_response")
    page = modules[0].get("pagedList")
    if type(page) is not dict or "rows" in page:
        raise TidalPlaybackError("invalid_page_response")
    return page


def _collection_track(entry: dict, *, mixed: bool) -> dict:
    """Require positive audio evidence for mixed slots and generic wrappers."""
    if "type" in entry and (type(entry["type"]) is not str or entry["type"].lower() != "track"):
        raise TidalPlaybackError("unsupported_page")
    if "playlist" in entry or "mixType" in entry or "item" in entry and "value" in entry:
        raise TidalPlaybackError("unsupported_page")
    key = "item" if "item" in entry else "value" if "value" in entry else ""
    inner = entry[key] if key else entry
    if type(inner) is not dict or any(key in inner for key in ("item", "value", "playlist", "mixType")):
        raise TidalPlaybackError("unsupported_page")
    if inner.get("uuid") is not None and inner["uuid"] != str(inner.get("id")):
        raise TidalPlaybackError("unsupported_page")
    track_url = False
    if "url" in inner:
        match = (re.fullmatch(r"https?://(?:www\.|listen\.)?tidal\.com/"
                              r"(?:browse/)?track/([1-9][0-9]{0,19})/?", inner["url"])
                 if type(inner["url"]) is str else None)
        track_url = match is not None and match[1] == str(inner.get("id"))
        if not track_url:
            raise TidalPlaybackError("unsupported_page")
    if (mixed or key) and "type" not in entry and inner.get("type") != "Track" and not track_url:
        raise TidalPlaybackError("unsupported_page")
    return inner


def page_track_ids(value: dict, request: TidalPageRequest) -> tuple[str, ...]:
    """Preserve every returned slot; do not filter inaccessible/video items."""
    if request.collection:
        value = _collection_page(value)
    offset, limit, total = (value.get(key) for key in ("offset", "limit", "totalNumberOfItems"))
    items = value.get("items")
    if (type(offset) is not int or offset != request.offset
            or type(limit) is not int or limit != request.limit
            or type(total) is not int or not offset <= total <= MAX_PAGE_TOTAL
            or type(items) is not list or len(items) > min(limit, total - offset)
            or not items and total > offset):
        raise TidalPlaybackError("invalid_page_response")
    if not items:
        raise TidalPlaybackError("unsupported_page")
    # A page may be short before the reported end. Preserve its actual slots;
    # neither fill gaps nor fetch another page to satisfy the requested limit.
    result = []
    for entry in items:
        if type(entry) is not dict:
            raise TidalPlaybackError("invalid_page_response")
        # The album and featured /tracks endpoints are audio-typed. A mixed /items endpoint
        # must instead prove each wrapper's type; never infer it from an ID.
        raw_new_track = (request.path == "featured/new/tracks" and "item" not in entry
                         and all(entry.get(key) is None for key in ("value", "playlist", "mixType")))
        # Preserve the existing raw New Tracks contract: optional URL/UUID
        # decoration is not new identity authority for this audio-typed route.
        if request.collection and not raw_new_track:
            entry = _collection_track(entry, mixed=request.collection == "mixed")
        elif request.playlist:
            if entry.get("type") != "track" or type(entry.get("item")) is not dict:
                raise TidalPlaybackError("unsupported_page")
            entry = entry["item"]
        elif request.favorites_user_id:
            # /favorites/tracks is audio-typed, but unlike /tracks its entries
            # wrap each track in item (with optional favorite-created metadata).
            if (("type" in entry and (type(entry["type"]) is not str
                                     or entry["type"].lower() != "track"))
                    or type(entry.get("item")) is not dict):
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
