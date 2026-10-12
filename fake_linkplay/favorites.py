"""ORR-owned local favorites, separate from provider/OEM preset-write APIs.

Only this service writes the versioned library. A single immutable snapshot
feeds status, legacy recall, and the JSON API; slots are deliberately transient.
Library edits never fetch media or change playback. Like the existing control
API, these routes are unauthenticated and intended for a trusted local network.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
import json
import logging
from pathlib import Path
import posixpath
import re
from threading import RLock
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from aiohttp import web

from .capabilities import configured_presets
from .config import Config
from .persistence import AtomicWriteCommittedError, atomic_write_text
from .queue import PlaybackQueue, QueueItem
from .radio import RadioResolutionError, public_station_reference

LOG = logging.getLogger(__name__)

MAX_FAVORITES = 100
MAX_FAVORITES_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 32 * 1024
MAX_URL_BYTES = 8192
_ID = re.compile(r"[0-9a-f]{32}\Z")
_FIELDS = {"id", "name", "url", "title", "artist", "album", "artwork"}
_DISPLAY_FIELDS = {"title", "artist", "album", "artwork"}


class FavoriteError(ValueError):
    """Safe, fixed-message error suitable for the owned JSON API."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.status = code, status


@dataclass(frozen=True)
class Favorite:
    id: str
    name: str
    url: str
    title: str = ""
    artist: str = ""
    album: str = ""
    artwork: str = ""


def _text(value: object, field: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        raise FavoriteError("invalid_favorite", f"{field} must be a nonempty string" if required else f"{field} must be a string")
    # XML-safe display strings also remain usable in the legacy queue surfaces.
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", value):
        raise FavoriteError("invalid_favorite", f"{field} contains invalid text")
    if len(value.encode("utf-8")) > limit:
        raise FavoriteError("invalid_favorite", f"{field} exceeds its size limit")
    return value


def _url(value: object, field: str = "url") -> str:
    value = _text(value, field, MAX_URL_BYTES, required=True)
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.netloc or not parts.hostname
                or parts.username is not None or parts.password is not None
                or "@" in parts.netloc or parts.port == 0
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
                or "\\" in value or any(char in value for char in "<>")):
            raise ValueError
        # Reject the renderer's transient audio-proxy endpoint. Save the
        # canonical queue URL instead; never copy a backend-generated proxy.
        normalized_path = posixpath.normpath("/" + unquote(parts.path).lstrip("/")).rstrip("/")
        if normalized_path == "/proxy/audio":
            raise ValueError
    except ValueError:
        raise FavoriteError("invalid_favorite", f"{field} must be a direct HTTP(S) reference without URL usernames/passwords or a renderer proxy") from None
    return value  # No strip, decoding, quoting, query sorting, or normalization.


def _record(payload: object, *, persisted: bool = False) -> Favorite:
    required = {"id", "name", "url"} if persisted else {"name", "url"}
    allowed = _FIELDS if persisted else _FIELDS - {"id"}
    if not isinstance(payload, dict) or not required <= payload.keys() or not payload.keys() <= allowed:
        raise FavoriteError("invalid_favorite", "Unsupported or missing favorite fields")
    identifier = payload.get("id") if persisted else uuid4().hex
    if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
        raise FavoriteError("invalid_favorite", "Invalid favorite ID")
    name = _text(payload["name"], "name", 256, required=True)
    url = _url(payload["url"])
    fields = {field: _text(payload.get(field, ""), field, MAX_URL_BYTES if field == "artwork" else 1024)
              for field in _DISPLAY_FIELDS}
    if fields["artwork"]:
        _url(fields["artwork"], "artwork")
    favorite = Favorite(identifier, name, url, **fields)
    PlaybackQueue(items=[QueueItem(url=url, title=favorite.title or name,
        artist=favorite.artist, album=favorite.album, artwork=favorite.artwork)], index=1).validate()
    return favorite


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _decode(encoded: bytes) -> object:
    try:
        return json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise FavoriteError("invalid_json", "Invalid JSON document") from None


class FavoritesStore:
    """One-process, bounded atomic library. Callers never receive owned records."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.path = Path(cfg.state_dir) / "favorites.json"
        self._lock = RLock()
        self._load_failed = False
        try:
            self._records = self._load()
        except (ValueError, OSError):
            # Preserve damaged/unreadable state and lock out managed writes.
            # Config-owned favorites and unrelated playback remain available.
            self._load_failed = True
            self._records = ()
            LOG.error("Managed favorites unavailable; existing state file preserved")

    def _require_available(self) -> None:
        if self._load_failed:
            raise FavoriteError("library_unavailable",
                "Managed favorites could not be loaded. Repair or restore favorites.json and restart; the file has not been overwritten.", 503)

    def _load(self) -> tuple[Favorite, ...]:
        try:
            with self.path.open("rb") as stream:
                encoded = stream.read(MAX_FAVORITES_BYTES + 1)
        except FileNotFoundError:
            return ()
        if len(encoded) > MAX_FAVORITES_BYTES:
            raise ValueError("Persisted favorites exceeds its size limit")
        payload = _decode(encoded)
        if (not isinstance(payload, dict) or set(payload) != {"version", "favorites"}
                or type(payload["version"]) is not int or payload["version"] != 1
                or not isinstance(payload["favorites"], list)
                or len(payload["favorites"]) > MAX_FAVORITES):
            raise ValueError("Unsupported persisted favorites document")
        records = tuple(_record(record, persisted=True) for record in payload["favorites"])
        if len({record.id for record in records}) != len(records):
            raise ValueError("Duplicate persisted favorite ID")
        return records

    def _commit(self, records: tuple[Favorite, ...]) -> None:
        encoded = json.dumps({"version": 1, "favorites": [asdict(record) for record in records]},
                             ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
        if len(encoded.encode("utf-8")) > MAX_FAVORITES_BYTES:
            raise FavoriteError("library_full", "Favorites library exceeds its size limit", 409)
        try:
            atomic_write_text(self.path, encoded)
        except AtomicWriteCommittedError:
            # os.replace already succeeded. Memory must describe the visible
            # file even though directory fsync could not confirm crash safety.
            self._records = records
            raise
        self._records = records

    def effective_presets(self) -> list[dict[str, object]]:
        """Configured prefix stays unchanged; managed entries receive dense slots."""
        with self._lock:
            presets = configured_presets(self.cfg)
            for favorite in self._records:
                number = len(presets) + 1
                presets.append({**asdict(favorite), "source": "Preset", "picurl": favorite.artwork,
                                "number": number, "index": str(number)})
            return presets

    def document(self) -> dict[str, object]:
        with self._lock:
            self._require_available()
            presets = self.effective_presets()
            configured_count = len(presets) - len(self._records)
            records = []
            for position, preset in enumerate(presets, 1):
                editable = position > configured_count
                records.append({
                    "id": preset["id"] if editable else f"configured:{position}",
                    "slot": position, "editable": editable,
                    "name": preset["name"], "url": preset["url"],
                    "title": preset.get("title", ""), "artist": preset.get("artist", ""),
                    "album": preset.get("album", ""), "artwork": preset.get("picurl", ""),
                })
            return {"version": 1, "favorites": records}

    def _entry(self, identifier: str) -> dict[str, object]:
        return next(record for record in self.document()["favorites"] if record["id"] == identifier)

    def add(self, payload: object) -> dict[str, object]:
        with self._lock:
            self._require_available()
            record = _record(payload)
            if len(self._records) >= MAX_FAVORITES:
                raise FavoriteError("library_full", "Favorites library has reached its entry limit", 409)
            self._commit((*self._records, record))
            return self._entry(record.id)

    def add_current(self, name: object, item: QueueItem | None) -> dict[str, object]:
        self._require_available()
        if item is None or not item.url:
            raise FavoriteError("no_current_target", "Current queue has no resolved direct target", 409)
        url = item.url
        provenance = item._radio_station
        if provenance is not None:
            # Only successful local resolution establishes this private tuple.
            # Never persist its temporary target or infer trust from metadata.
            try:
                if (type(provenance) is not tuple or len(provenance) != 2
                        or provenance[0] != item.url or item.stream_type != "station"
                        or type(item.duration) not in (int, float) or item.duration != 0):
                    raise RadioResolutionError("Invalid station provenance")
                url = public_station_reference(provenance[1])
            except RadioResolutionError:
                raise FavoriteError("no_canonical_target",
                    "Current station has no trusted canonical public reference", 409) from None
        return self.add({"name": name, "url": url, "title": item.title,
                         "artist": item.artist, "album": item.album, "artwork": item.artwork})

    def _position(self, identifier: str) -> int:
        if identifier.startswith("configured:"):
            raise FavoriteError("configured_read_only", "Configured favorites are read-only", 403)
        if not _ID.fullmatch(identifier):
            raise FavoriteError("invalid_id", "Invalid favorite ID")
        for index, record in enumerate(self._records):
            if record.id == identifier:
                return index
        raise FavoriteError("not_found", "Unknown favorite ID", 404)

    def rename(self, identifier: str, name: object) -> dict[str, object]:
        with self._lock:
            self._require_available()
            name = _text(name, "name", 256, required=True)
            position = self._position(identifier)
            records = list(self._records)
            if records[position].name != name:
                records[position] = replace(records[position], name=name)
                self._commit(tuple(records))
            return self._entry(identifier)

    def delete(self, identifier: str) -> None:
        with self._lock:
            self._require_available()
            position = self._position(identifier)
            self._commit(self._records[:position] + self._records[position + 1:])


class FavoritesApi:
    """JSON routes for ORR's library; no compatibility claims for OEM writes."""

    def __init__(self, store: FavoritesStore, current_item: Callable[[], QueueItem | None]) -> None:
        self.store, self.current_item = store, current_item

    def routes(self) -> list[web.RouteDef]:
        base = "/orr/v1/favorites"
        return [web.get(base, self.handle), web.post(base, self.handle),
                web.post(base + "/current", self.handle),
                web.patch(base + "/{id}", self.handle), web.delete(base + "/{id}", self.handle)]

    @staticmethod
    async def _body(request: web.Request) -> object:
        # aiohttp normally decompresses before exposing request.content, while
        # Content-Length still describes wire bytes. Never parse a prefix of an
        # inflated document or silently ignore decoded trailing bytes.
        encodings = request.headers.getall("Content-Encoding", [])
        if len(encodings) > 1 or any(value.lower() != "identity" for value in encodings):
            raise FavoriteError("unsupported_content_encoding", "Compressed favorite requests are unsupported", 415)
        if request.content_type != "application/json" or request.charset not in {None, "utf-8", "UTF-8"}:
            raise FavoriteError("unsupported_media_type", "Use application/json with UTF-8", 415)
        length = request.content_length
        if length is None or request.headers.get("Transfer-Encoding"):
            raise FavoriteError("length_required", "A bounded Content-Length is required", 411)
        if length > MAX_REQUEST_BYTES:
            raise FavoriteError("request_too_large", "Favorite request exceeds its size limit", 413)
        try:
            async with asyncio.timeout(10):
                encoded = await request.content.readexactly(length)
        except asyncio.IncompleteReadError:
            raise FavoriteError("invalid_json", "Incomplete JSON document") from None
        except TimeoutError:
            raise FavoriteError("request_timeout", "Favorite request timed out", 408) from None
        return _decode(encoded)

    async def handle(self, request: web.Request) -> web.Response:
        try:
            if request.method in {"GET", "HEAD"}:
                return web.json_response(self.store.document())
            if request.method == "DELETE":
                if request.content_length not in {None, 0} or request.headers.get("Transfer-Encoding"):
                    raise FavoriteError("unexpected_body", "DELETE does not accept a request body")
                identifier = request.match_info["id"]
                self.store.delete(identifier)
                return web.json_response({"deleted": identifier})
            payload = await self._body(request)
            if request.method == "PATCH" or request.path.endswith("/current"):
                if not isinstance(payload, dict) or set(payload) != {"name"}:
                    raise FavoriteError("invalid_favorite", "Only the name field is accepted")
                if request.method == "PATCH":
                    favorite = self.store.rename(request.match_info["id"], payload["name"])
                else:
                    favorite = self.store.add_current(payload["name"], self.current_item())
            else:
                favorite = self.store.add(payload)
            return web.json_response({"favorite": favorite}, status=201 if request.method == "POST" else 200)
        except FavoriteError as exc:
            return web.json_response({"error": exc.code, "message": str(exc)}, status=exc.status)
        except AtomicWriteCommittedError:
            return web.json_response({"error": "durability_unconfirmed", "committed": True,
                "message": "Favorites were replaced but crash durability is unconfirmed. Read the library before retrying."}, status=503)
        except OSError:
            return web.json_response({"error": "storage_unavailable", "committed": False,
                                      "message": "Favorites could not be saved"}, status=503)
