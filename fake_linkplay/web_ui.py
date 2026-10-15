"""Same-origin browser adapter on the existing trusted-LAN HTTP listeners.

This boundary adds browser admission checks, not authentication. Legacy HTTP,
SOAP and favorites routes remain unauthenticated. The renderer alone owns
playback, transactions and background observation; this adapter owns only a
process-local CSRF secret and presentation snapshot sequence.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
import ipaddress
import hashlib
import hmac
from importlib.resources import files
import json
import math
import re
import secrets
import time
from urllib.parse import unquote, urlsplit

from aiohttp import web

from .canonical_target import canonical_target_url
from .device_name import DeviceNameCommittedError, validate_device_name
from .favorites import FavoriteError, FavoritesStore, decode_favorites_document, validate_target_edit
from .eq_presets import EqPresetLibrary, PRESET_LIBRARY_UNAVAILABLE_MESSAGE, normalize_eq_preset_name
from .eq_transfer import EqTransferCurve, decode_eq_transfer, encode_eq_transfer
from .log_safety import redact_media_urls
from .mpv_backend import BackendError
from .persistence import AtomicWriteCommittedError, validate_eq_records
from .queue import PlaybackQueue
from .queue_protocol import MODES, validate_url
from .queue_playlist import encode_current_playlist
from .web_playlists import (PLAYLIST_ACTIONS, WebPlaylistError, availability as playlist_availability,
                            prepare_edit, prepare_source, prepare_urls)
from .radio import RadioResolutionError
from .radio_catalog import CatalogError, CatalogPlaybackUnavailable, CatalogStation, search_stations
from .state import PlaybackPhase, RendererState

PREFIX = "/orr/v1/web"
MAX_REQUEST_BYTES = 32 * 1024
MAX_TEXT_BYTES = 1024
MAX_SAFE_INTEGER = 2 ** 53 - 1
_MODE_NAMES = {number: name for name, number in MODES.items()}
_ASSETS = {"/": ("index.html", "text/html"), "/ui/": ("index.html", "text/html"),
           "/ui/index.html": ("index.html", "text/html"),
           "/ui/style.css": ("style.css", "text/css"),
           "/ui/controller.js": ("controller.js", "text/javascript"),
           "/ui/app.js": ("app.js", "text/javascript"),
           "/ui/favicon.ico": (None, "image/x-icon")}
_HEADERS = {
    "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' http: https:; font-src 'none'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
}
_GENERATION_ACTIONS = frozenset({"play", "pause", "next", "previous", "seek",
                               "queue_select", "queue_mode", "favorite_current", "radio_play"}) | PLAYLIST_ACTIONS
_SCHEMAS = {
    **{name: {"expected_generation"} for name in ("play", "pause", "next", "previous")},
    "device_rename": {"name", "expected_device"},
    "stop": set(), "seek": {"seconds", "expected_generation"},
    "volume": {"value"}, "mute": {"value"}, "balance": {"value"}, "eq": {"enabled"},
    "queue_select": {"queue_name", "index", "expected_generation"},
    "queue_mode": {"queue_name", "mode", "expected_generation"},
    "radio_play": {"id", "expected_generation"}, "favorite_radio": {"id"},
    "favorite_add": {"name", "url"}, "favorite_current": {"name", "expected_generation"},
    "favorite_rename": {"id", "name"}, "favorite_target": {"id", "url", "metadata"},
    "favorite_delete": {"id"}, "favorite_play": {"id"},
    "favorite_restore": {"expected_session", "text", "private_import_confirmed"},
    "playlist_replace": {"expected_generation", "source"},
    "playlist_append": {"expected_generation", "urls"},
    "playlist_remove": {"expected_generation", "index"},
    "playlist_move": {"expected_generation", "from_index", "to_index"},
    "dsp_bands": {"expected_dsp", "bands"},
    "dsp_preset_load": {"expected_dsp", "preset_id"},
    "dsp_preset_save": {"expected_dsp", "name"},
    "dsp_preset_rename": {"expected_dsp", "preset_id", "name"},
    "dsp_preset_delete": {"expected_dsp", "preset_id"},
    "dsp_preset_import": {"expected_dsp", "text", "private_import_confirmed"},
}
FAVORITES_EXPORT_FILENAME = "open-room-renderer-favorites.json"
_FAVORITES_EXPORT_DOMAIN = b"orr:web-favorites-export:v1\0"
_SESSION_TOKEN = re.compile(r"[A-Za-z0-9_-]{32}\Z")
CURVE_EXPORT_FILENAME = "open-room-renderer-eq-curve.json"
_EXPORT_FILENAME = "open-room-renderer-playlist.json"
_EXPORT_DOMAIN = b"orr:web-playlist-export:v1\0"
_EXPORT_SCHEMAS = {
    "prepare": {"action", "expected_session", "expected_generation"},
    "download": {"action", "expected_session", "expected_generation", "expected_export",
                 "private_export_confirmed"},
}
_DSP_TOKEN = re.compile(r"[0-9a-f]{64}\Z")
_BUILTIN_PRESETS = ("Flat", "Bass Boost", "Treble Boost")
_ID = re.compile(r"(?:[0-9a-f]{32}|configured:[1-9][0-9]{0,5})\Z")
_INVALID_TEXT = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff\ufffe\uffff]")


@dataclass(frozen=True)
class WebCommand:
    """Validated typed arguments. No browser-provided legacy command strings."""
    action: str
    expected_generation: int | None = None
    seconds: int | None = None
    value: int | float | bool | None = None
    enabled: bool | None = None
    queue_name: str = ""
    index: int | None = None
    mode: str = ""
    name: str = ""
    url: str = ""
    id: str = ""
    metadata: str = ""
    expected_dsp: str = ""
    expected_device: str = ""
    expected_session: str = ""
    favorites_bytes: bytes | None = field(default=None, repr=False)
    preset_id: str = ""
    bands: list[dict[str, object]] | None = None
    curve: EqTransferCurve | None = field(default=None, repr=False)
    source: PlaybackQueue | None = field(default=None, repr=False)
    urls: list[str] | None = field(default=None, repr=False)
    from_index: int | None = None
    to_index: int | None = None
    prepared_queue: PlaybackQueue | None = field(default=None, repr=False)


@dataclass(frozen=True)
class WebPlaylistExport:
    """An explicit private read, kept outside playback command dispatch."""
    action: str
    expected_session: str
    expected_generation: int
    expected_export: str = ""
    private_export_confirmed: bool = False


@dataclass(frozen=True)
class WebFavoritesExport:
    """A managed-only private read with no playback or active-DSP authority."""
    action: str
    expected_session: str
    expected_export: str = ""
    private_export_confirmed: bool = False


@dataclass(frozen=True)
class WebDspExport:
    """Saved current-context curve read; never a live DSP command."""
    action: str
    expected_dsp: str
    preset_id: str
    expected_export: str = ""
    private_export_confirmed: bool = False


@dataclass(frozen=True)
class WebRuntime:
    """Synchronous renderer-owned availability and uncertainty observations."""
    actions: frozenset[str]
    catalog_proxy_available: bool = True
    transport_unknown: bool = False
    transport_pending: bool = False
    gain_pending: bool = False
    backend_gain_confirmed: bool = True
    queue_mode_locked: bool = False
    playlist_stopped: bool = False
    station_recovery_pending: bool = False
    station_recovery_attempts: int | None = None
    station_recovery_limit: int | None = None


def _playback_recovery(state: RendererState, runtime: WebRuntime) -> dict[str, object] | None:
    """Allowlisted presentation only; never serialize a backend error or source."""
    if state.phase == PlaybackPhase.ERROR and state.last_error in {
            "Backend control failed", "Backend control failed; playback silence not confirmed"}:
        return {"kind": "backend_control", "message":
            "Backend control failed. Playback and silence are unconfirmed. "
            "Choose Stop to request recovery, then check the reported state before starting playback again."}
    attempts, limit = runtime.station_recovery_attempts, runtime.station_recovery_limit
    if (runtime.transport_unknown or type(attempts) is not int or type(limit) is not int
            or not 0 <= attempts <= limit <= MAX_SAFE_INTEGER or limit == 0):
        return None
    if (state.phase == PlaybackPhase.ERROR and attempts == limit
            and state.last_error == "Station source unavailable: recovery exhausted"):
        return {"kind": "station_exhausted", "message":
            "Automatic station recovery is exhausted. Choose Play to try this selection again, "
            "select another queue item, or choose Stop. This does not confirm silence."}
    if (runtime.station_recovery_pending and state.phase in {PlaybackPhase.LOADING, PlaybackPhase.BUFFERING}
            and state.last_error in {"Station source ended; retry pending",
                "Station source stalled; retry pending", "Station source idle; retry pending",
                "Station source unavailable; retry pending"}):
        return {"kind": "station_retry", "attempts_used": attempts, "attempt_limit": limit,
            "message": f"Station recovery pending: {attempts} of {limit} automatic retry attempts used. "
                "Wait for confirmation, or choose Stop."}
    return None


class WebAdmissionError(ValueError):
    def __init__(self, code: str = "invalid_request", status: int = 400) -> None:
        self.code, self.status = code, status


class WebDspCommandError(RuntimeError):
    """Private-safe classification captured before the DSP lock is released."""
    def __init__(self, effect: str) -> None:
        self.effect = effect


def _text(value: object, limit: int = MAX_TEXT_BYTES, *, required: bool = False) -> str:
    if (not isinstance(value, str) or _INVALID_TEXT.search(value)
            or len(value.encode("utf-8")) > limit or required and not value.strip()):
        raise WebAdmissionError()
    return value


def _display(value: object, default: str = "") -> str:
    if not isinstance(value, str) or _INVALID_TEXT.search(value):
        return default
    # Never echo URLs even if a backend put one into a metadata field.
    if (redact_media_urls(value) != value or re.search(
            r"(?i)(?:https?|file|ftp)://|[?][^ ]*=|(?:access_token|auth_token|api_key|password|token|signature)=", value)):
        return default
    return value[:MAX_TEXT_BYTES]


def _dsp_name(value: object, default: str = "") -> str:
    """Keep literal percent names, but hide encoded URL/token metadata too."""
    visible = _display(value, default)
    if visible == default:
        return default
    decoded = visible
    try:
        for _ in range(2):
            decoded = unquote(decoded, errors="strict")
            if _display(decoded) != decoded:
                return default
    except UnicodeError:
        return default
    return visible


def _title(value: object, urls: tuple[str, ...], fallback: str) -> str:
    """Hide URL-derived basenames while retaining real backend/ICY titles.

    title_is_fallback is also retained for live backend metadata by the core;
    it is not proof that a current title came from a URL.
    """
    visible = _display(value)
    if not visible:
        return fallback
    for url in urls:
        if not url:
            continue
        try:
            path = urlsplit(url).path
            forms = {url, url.rsplit("/", 1)[-1], path, path.rsplit("/", 1)[-1]}
            for _ in range(2):
                forms.update(unquote(form, errors="strict") for form in tuple(forms))
        except (ValueError, UnicodeError):
            return fallback
        if value in forms:
            return fallback
    return visible


def _queue_label(name: str) -> str:
    """A bounded display label, never queue ownership or a media reference."""
    visible = _display(name, "Current queue")
    return visible if len(visible.encode("utf-8")) <= 256 else "Current queue"


def safe_artwork(value: object) -> str | None:
    """Artwork may be rendered only without userinfo, query or fragment."""
    if not isinstance(value, str) or not value or len(value) > 8192:
        return None
    try:
        if re.search(r"%(?![0-9a-fA-F]{2})", value):
            return None
        decoded = unquote(value, errors="strict")
        if _INVALID_TEXT.search(decoded) or "\\" in decoded:
            return None
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or "?" in value or "#" in value
                or parts.port == 0 or any(char.isspace() or ord(char) < 32 for char in value)
                or "\\" in value or any(char in value for char in "<>")):
            return None
        value.encode("utf-8")
    except (ValueError, UnicodeError):
        return None
    return value


def _integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise WebAdmissionError()
    return value


def _decode_request(encoded: bytes) -> dict[str, object]:
    """Shared strict JSON boundary for commands and explicit private reads."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError
        return number

    def invalid(_value):
        raise ValueError

    try:
        if type(encoded) is not bytes or len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError
        payload = json.loads(encoded.decode("utf-8"), object_pairs_hook=unique,
                             parse_float=finite, parse_constant=invalid)
        if not isinstance(payload, dict) or not isinstance(payload.get("action"), str):
            raise ValueError
        return payload
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise WebAdmissionError() from None


def decode_playlist_export(encoded: bytes) -> WebPlaylistExport:
    try:
        payload = _decode_request(encoded)
        action = payload["action"]
        if action not in _EXPORT_SCHEMAS or payload.keys() != _EXPORT_SCHEMAS[action]:
            raise ValueError
        _integer(payload["expected_generation"], 0, MAX_SAFE_INTEGER)
        session = payload["expected_session"]
        if not isinstance(session, str) or not session.isascii():
            raise ValueError
        if action == "download":
            revision = payload["expected_export"]
            if not isinstance(revision, str) or not _DSP_TOKEN.fullmatch(revision):
                raise ValueError
            if payload["private_export_confirmed"] is not True:
                raise ValueError
        return WebPlaylistExport(**payload)
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise WebAdmissionError() from None


def prepare_playlist_export(queue: PlaybackQueue) -> tuple[bytes, PlaybackQueue]:
    """Keep codec bytes intact and require a worst-generation Web round trip.

    The nested JSON text and complete command envelope, rather than just the
    downloaded file, must fit the unchanged browser request limit. No I/O.
    """
    try:
        encoded = encode_current_playlist(queue)
        source = {"format": "json", "text": encoded.decode("utf-8")}
        restored = prepare_source(source)
        envelope = {"action": "playlist_replace", "expected_generation": MAX_SAFE_INTEGER,
                    "source": source}
        size = len(json.dumps(envelope, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")).encode("utf-8"))
        if size > MAX_REQUEST_BYTES:
            raise ValueError
        return encoded, restored
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise WebAdmissionError("export_unavailable", 409) from None


def decode_favorites_export(encoded: bytes) -> WebFavoritesExport:
    payload = _decode_request(encoded)
    action = payload["action"]
    fields = {"action", "expected_session"}
    if action == "download":
        fields |= {"expected_export", "private_export_confirmed"}
    if action not in {"prepare", "download"} or payload.keys() != fields:
        raise WebAdmissionError()
    if not isinstance(payload["expected_session"], str) or not _SESSION_TOKEN.fullmatch(payload["expected_session"]):
        raise WebAdmissionError()
    if action == "download" and (payload["private_export_confirmed"] is not True
            or not isinstance(payload["expected_export"], str)
            or not _DSP_TOKEN.fullmatch(payload["expected_export"])):
        raise WebAdmissionError()
    return WebFavoritesExport(**payload)


def prepare_favorites_export(encoded: bytes) -> bytes:
    """Prove this complete exact snapshot fits the escaped restore command.

    The same strict persisted decoder and Web URL validator gate both sides.
    Session IDs have a fixed 32-character URL-safe representation.
    """
    try:
        envelope = json.dumps({"action": "favorite_restore", "expected_session": "f" * 32,
            "text": encoded.decode("utf-8"), "private_import_confirmed": True},
            ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if decode_command(envelope).favorites_bytes != encoded:
            raise ValueError
        return encoded
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise WebAdmissionError("favorites_export_unavailable", 409) from None


def decode_dsp_export(encoded: bytes) -> WebDspExport:
    payload = _decode_request(encoded)
    action = payload["action"]
    fields = {"action", "expected_dsp", "preset_id"}
    if action == "download":
        fields |= {"expected_export", "private_export_confirmed"}
    if action not in {"prepare", "download"} or payload.keys() != fields:
        raise WebAdmissionError()
    token_fields = ("expected_dsp", "preset_id", "expected_export") if action == "download" else ("expected_dsp", "preset_id")
    for key in token_fields:
        if not isinstance(payload[key], str) or not _DSP_TOKEN.fullmatch(payload[key]):
            raise WebAdmissionError()
    if action == "download" and payload["private_export_confirmed"] is not True:
        raise WebAdmissionError()
    return WebDspExport(**payload)


def prepare_dsp_curve_export(name: str, plugin: str, bands: object) -> bytes:
    """Prove the exact escaped import envelope fits, without library I/O.

    The only changing outer import value is a fixed-size process-bound DSP
    revision. Names/plugin IDs remain private JSON text, never metadata.
    """
    try:
        encoded = encode_eq_transfer(name, plugin, bands)
        envelope = json.dumps({"action": "dsp_preset_import", "expected_dsp": "f" * 64,
            "text": encoded.decode("utf-8"), "private_import_confirmed": True},
            ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        restored = decode_command(envelope).curve
        if restored != decode_eq_transfer(encoded):
            raise ValueError
        return encoded
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise WebAdmissionError("curve_export_unavailable", 409) from None


def decode_command(encoded: bytes) -> WebCommand:
    try:
        payload = _decode_request(encoded)
        action = payload["action"]
        if action not in _SCHEMAS or payload.keys() != _SCHEMAS[action] | {"action"}:
            raise ValueError
        if action == "device_rename":
            if (not isinstance(payload["name"], str) or not isinstance(payload["expected_device"], str)
                    or not _DSP_TOKEN.fullmatch(payload["expected_device"])):
                raise ValueError
            # Typed JSON is already decoded. In particular, %2F and %25 stay
            # literal; only the legacy transport applies URL/hex decoding.
            payload["name"] = validate_device_name(payload["name"].strip())
        if action in _GENERATION_ACTIONS:
            _integer(payload["expected_generation"], 0, MAX_SAFE_INTEGER)
        if action == "playlist_replace":
            payload["source"] = prepare_source(payload["source"])
        if action == "playlist_append":
            payload["urls"] = prepare_urls(payload["urls"])
        if action == "playlist_remove":
            _integer(payload["index"], 1, 1000)
        if action == "playlist_move":
            _integer(payload["from_index"], 1, 1000)
            _integer(payload["to_index"], 1, 1000)
        if action == "seek":
            _integer(payload["seconds"], 0, 2 ** 31 - 1)
        if action == "volume":
            _integer(payload["value"], 0, 100)
        if action == "mute" and type(payload["value"]) is not bool:
            raise ValueError
        if action == "eq" and type(payload["enabled"]) is not bool:
            raise ValueError
        if action == "balance":
            value = payload["value"]
            if type(value) not in {int, float} or not math.isfinite(value) or not -1 <= value <= 1:
                raise ValueError
        if action.startswith("queue_"):
            _text(payload["queue_name"], 256, required=True)
        if action == "queue_select":
            _integer(payload["index"], 1, 1000)
        if action == "queue_mode" and (not isinstance(payload["mode"], str) or payload["mode"] not in MODES):
            raise ValueError
        if action == "favorite_restore":
            if (payload["private_import_confirmed"] is not True or not isinstance(payload["text"], str)
                    or not isinstance(payload["expected_session"], str)
                    or not _SESSION_TOKEN.fullmatch(payload["expected_session"])):
                raise ValueError
            payload["favorites_bytes"] = payload.pop("text").encode("utf-8")
            for record in decode_favorites_document(payload["favorites_bytes"]):
                validate_url(record.url)
                if record.artwork:
                    validate_url(record.artwork)
            del payload["private_import_confirmed"]
        if action == "dsp_preset_import":
            if payload["private_import_confirmed"] is not True or not isinstance(payload["text"], str):
                raise ValueError
            payload["curve"] = decode_eq_transfer(payload.pop("text").encode("utf-8"))
            del payload["private_import_confirmed"]
        if action.startswith("dsp_"):
            if not isinstance(payload["expected_dsp"], str) or not _DSP_TOKEN.fullmatch(payload["expected_dsp"]):
                raise ValueError
            if "preset_id" in payload and (not isinstance(payload["preset_id"], str)
                    or not _DSP_TOKEN.fullmatch(payload["preset_id"])):
                raise ValueError
            if action == "dsp_bands":
                bands = validate_eq_records(payload["bands"], require_complete=True)
                if any(type(item["value"]) not in {int, float} for item in payload["bands"]):
                    raise ValueError
                payload["bands"] = bands
            if "name" in payload:
                name = _text(payload["name"], 256, required=True).strip()
                if (_dsp_name(name) != name or normalize_eq_preset_name(name) != name
                        or name in _BUILTIN_PRESETS):
                    raise ValueError
                payload["name"] = name
        if "name" in payload and action != "device_rename":
            _text(payload["name"], 256, required=True)
        if "id" in payload and (not isinstance(payload["id"], str) or not _ID.fullmatch(payload["id"])):
            raise ValueError
        if "url" in payload:
            _text(payload["url"], 8192, required=True)
            validate_url(payload["url"])
        if action == "favorite_target":
            validate_target_edit(payload["url"], payload["metadata"])
        return WebCommand(**payload)
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise WebAdmissionError() from None


def _duration(value: object) -> int | None:
    if type(value) in {int, float} and math.isfinite(value) and 0 < value <= MAX_SAFE_INTEGER:
        return int(value)
    return None


class WebControlApi:
    """Explicit callbacks only: no HttpApi, backend ownership or new tasks."""
    def __init__(self, *, current_state: Callable[[], RendererState],
                 device_name: Callable[[], str], runtime: Callable[[], WebRuntime],
                 dsp_snapshot: Callable[[], dict[str, object]], favorites: FavoritesStore,
                 dispatch: Callable[[WebCommand], Awaitable[dict[str, object] | None]],
                 allowed_hosts: tuple[str, ...],
                 inspect_output: Callable[[], Awaitable[dict[str, object]]] | None = None,
                 export_dsp_curve: Callable[[WebDspExport], Awaitable[tuple[bytes, bool]]] | None = None) -> None:
        self._current_state = current_state
        self._device_name = device_name
        self._runtime = runtime
        self._dsp_snapshot = dsp_snapshot
        self._favorites = favorites
        self._dispatch = dispatch
        self._inspect_output = inspect_output
        self._export_dsp_curve = export_dsp_curve
        self._output_inspection_busy = False
        self._allowed_hosts = frozenset(host.lower().rstrip(".") for host in allowed_hosts if host and host not in {"0.0.0.0", "::", "*"})
        self._csrf_token = secrets.token_urlsafe(32)
        self._session_id = secrets.token_urlsafe(24)
        self._revision = 0
        self._radio_busy = False
        self._radio_results: dict[str, tuple[float, CatalogStation]] = {}
        # Keyed presentation tokens conceal raw plugin/name identifiers; they
        # are derived views, not a second revision counter or state authority.
        self._dsp_key = secrets.token_bytes(32)
        self._export_key = secrets.token_bytes(32)

    def _dsp_digest(self, kind: str, value: object) -> str:
        encoded = json.dumps([kind, value], sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hmac.new(self._dsp_key, encoded, hashlib.sha256).hexdigest()

    def device_revision(self, name: str | None = None) -> str:
        """Session-bound name identity, including names hidden by redaction."""
        return self._dsp_digest("device_name", self._device_name() if name is None else name)

    def dsp_revision(self, active: dict[str, object], library: EqPresetLibrary) -> str:
        """Include the entire library because rename/delete operate by name."""
        value = [active, library.presets]
        if library.unavailable:
            value.append({"library_unavailable": True})
        return self._dsp_digest("revision", value)

    def dsp_preset_id(self, plugin: str, name: str) -> str:
        return self._dsp_digest("preset", [plugin, name])

    def dsp_editor(self, active: dict[str, object], library: EqPresetLibrary) -> dict[str, object]:
        plugin = active["plugin"]
        saved = set(library.names(plugin))
        names = list(_BUILTIN_PRESETS) + sorted(saved.difference(_BUILTIN_PRESETS))
        editor = {"revision": self.dsp_revision(active, library),
                "source": "wifi" if active["source"] else "global", "channel_mode": "Stereo",
                "bands": [dict(record) for record in active["bands"]],
                "presets": [{"id": self.dsp_preset_id(plugin, name),
                             "name": _dsp_name(name, "Saved preset"),
                             "kind": "saved" if name in saved else "builtin",
                             "shared": name in saved and len(library.presets[name]) > 1}
                            for name in names]}
        if library.unavailable:
            editor["library_unavailable"] = True
        return editor

    def dsp_preset_name(self, preset_id: str, plugin: str, library: EqPresetLibrary) -> str:
        """Resolve only a current-context opaque ID; never accept a raw URI."""
        for name in (*_BUILTIN_PRESETS, *library.names(plugin)):
            if secrets.compare_digest(preset_id, self.dsp_preset_id(plugin, name)):
                return name
        raise WebAdmissionError("stale_dsp", 409)

    def routes(self) -> list[web.RouteDef]:
        return [web.route("*", path, self.handle) for path in _ASSETS] + [
            web.route("*", PREFIX + "/{tail:.*}", self.handle),
            web.route("*", "/ui/{tail:.*}", self.handle)]

    @staticmethod
    def _error(code: str, status: int, outcome: str = "REJECTED", *, committed: bool = False,
               effect: str | None = None) -> web.Response:
        messages = {
            "radio_proxy_unavailable": "The local radio proxy could not start. Check the renderer startup log and restart it after resolving the local listener error.",
            "radio_unavailable": "Radio-Browser is unavailable. Try searching again later.",
            "radio_busy": "A station search is already running. Wait for it to finish.",
            "radio_expired": "This station selection expired. Search again before choosing it.",
            "radio_invalid": "Enter a station name (2–80 characters), a two-letter country code, or both.",
            "stale_output_inspection": "The renderer session changed. Refresh before inspecting again.",
            "output_inspection_busy": "An output inspection is already running. No new inspection was started.",
            "output_inspection_unavailable": "Output inspection is unavailable in this renderer.",
            "output_inspection_failed": "Output inspection could not be completed. No automatic retry was sent.",
            "stale_generation": "Selection changed. Refresh before choosing again.",
            "export_unavailable": "This queue cannot be exported for browser import. For supported larger queues, use playlist save-current FILE in the CLI.",
            "stale_export": "The queue or renderer session changed. Reopen Export and review it again.",
            "stale_device": "The room name changed. Reopen Rename room and review the current name.",
            "device_rename_committed": "The room name was applied, but persistence or discovery is unconfirmed. Readback alone cannot confirm either. No automatic retry was sent.",
            "curve_export_unavailable": "This saved curve cannot be exported for browser import. A legacy unsafe name must be explicitly renamed to a plain safe name first. The curve and complete import request must fit the transfer limits.",
            "curve_context_mismatch": "The file’s plugin does not match this room’s current DSP context. No remapping or import was performed.",
            "curve_library_full": "This curve cannot fit within the 1 MiB saved preset library limit. No import was performed.",
            "stale_dsp": "DSP settings or saved presets changed. Refresh and review before choosing again.",
            "preset_conflict": "That preset name already exists. Choose another name.",
            "preset_library_unavailable": PRESET_LIBRARY_UNAVAILABLE_MESSAGE,
            "forbidden": "Browser request was not admitted.",
            "unavailable": "Requested operation is currently unavailable.",
            "read_failed": "Renderer state could not be read.",
            "stale_favorites_transfer": "The renderer session or managed favorites changed. Close this dialog and review again.",
            "favorites_not_empty": "Managed favorites must be empty before restoring. No favorites were replaced.",
            "favorites_export_unavailable": "This managed library cannot be exported for browser restore. The complete file and escaped restore request must fit the transfer limits.",
            "favorites_unavailable": "Managed favorites could not be loaded. Stop the renderer service, repair or restore its saved favorites library, then restart it.",
            "command_failed": "Command outcome is unknown. Refresh before any explicit retry.",
        }
        payload = {"error": code, "message": messages.get(code, "Browser request was not admitted."),
                   "outcome": outcome}
        if committed:
            payload["committed"] = True
        if effect:
            payload["effect"] = effect
            payload["message"] = {
                "BACKEND_APPLIED_PERSISTENCE_FAILED": "DSP was applied, but saving restart state failed. Refresh before choosing again.",
                "BACKEND_APPLIED_FOLLOWUP_UNKNOWN": "DSP was applied, but the remaining command outcome is unconfirmed. Refresh before choosing again.",
                "LIBRARY_COMMITTED_DURABILITY_UNKNOWN": "The preset library was replaced, but disk durability is unconfirmed. Refresh before choosing again.",
                "LIBRARY_COMMITTED_ACTIVE_PERSISTENCE_FAILED": "The preset library was saved, but saving active DSP metadata failed. Refresh before choosing again.",
            }.get(effect, payload["message"])
        return web.json_response(payload, status=status, headers=_HEADERS)

    def _origin(self, request: web.Request) -> str:
        hosts = request.headers.getall("Host", [])
        if len(hosts) != 1:
            raise WebAdmissionError("forbidden", 403)
        authority = hosts[0]
        if (not authority or len(authority) > 300 or not authority.isascii()
                or any(char.isspace() for char in authority) or any(char in authority for char in "@/%\\?#")):
            raise WebAdmissionError("forbidden", 403)
        pattern = r"\[([0-9A-Fa-f:]+)\](?::([0-9]{1,5}))?" if authority.startswith("[") else r"([A-Za-z0-9.-]+)(?::([0-9]{1,5}))?"
        match = re.fullmatch(pattern, authority)
        if match is None:
            raise WebAdmissionError("forbidden", 403)
        host, port = match.groups()
        if authority.startswith("["):
            try:
                host = str(ipaddress.IPv6Address(host))
            except ValueError:
                raise WebAdmissionError("forbidden", 403) from None
        host = host.lower()
        local = request.transport.get_extra_info("sockname") if request.transport else None
        if not local or len(local) < 2:
            raise WebAdmissionError("forbidden", 403)
        allowed = set(self._allowed_hosts)
        allowed.add(str(local[0]).lower())
        try:
            if ipaddress.ip_address(local[0]).is_loopback:
                allowed.add("localhost")
        except ValueError:
            pass
        default_port = 443 if request.secure else 80
        if host.rstrip(".") not in allowed or (int(port) if port else default_port) != local[1]:
            raise WebAdmissionError("forbidden", 403)
        origin = ("https" if request.secure else "http") + "://" + authority
        origins = request.headers.getall("Origin", [])
        if origins and origins != [origin]:
            raise WebAdmissionError("forbidden", 403)
        sites = request.headers.getall("Sec-Fetch-Site", [])
        # A link from another site may open the inert HTML entry document.
        # Frames, scripts, API reads and every mutation retain the strict
        # same-origin rule. Host and any supplied Origin were checked above.
        document_navigation = (
            request.method == "GET" and request.path in {"/", "/ui/", "/ui/index.html"}
            and request.headers.getall("Sec-Fetch-Mode", []) == ["navigate"]
            and request.headers.getall("Sec-Fetch-Dest", []) == ["document"])
        if (len(sites) > 1 or sites and sites[0] not in {"same-origin", "none"}
                and not (sites[0] in {"same-site", "cross-site"} and document_navigation)):
            raise WebAdmissionError("forbidden", 403)
        return origin

    @staticmethod
    def _bodyless(request: web.Request) -> None:
        if (request.query_string or request.content_length not in {None, 0}
                or request.headers.getall("Transfer-Encoding", [])
                or request.headers.getall("Content-Encoding", [])):
            raise WebAdmissionError()

    async def _body(self, request: web.Request, origin: str) -> bytes:
        if request.query_string or request.headers.getall("Origin", []) != [origin]:
            raise WebAdmissionError("forbidden", 403)
        tokens = request.headers.getall("X-ORR-CSRF", [])
        if len(tokens) != 1 or not tokens[0].isascii() or not secrets.compare_digest(tokens[0], self._csrf_token):
            raise WebAdmissionError("forbidden", 403)
        encodings = request.headers.getall("Content-Encoding", [])
        if len(encodings) > 1 or encodings and encodings[0].lower() != "identity":
            raise WebAdmissionError("unsupported_encoding", 415)
        content_types = request.headers.getall("Content-Type", [])
        if (len(content_types) != 1 or not re.fullmatch(
                r'application/json(?:\s*;\s*charset=(?:utf-8|"utf-8"))?', content_types[0], re.IGNORECASE)):
            raise WebAdmissionError("unsupported_media_type", 415)
        lengths = request.headers.getall("Content-Length", [])
        if len(lengths) != 1 or request.headers.getall("Transfer-Encoding", []):
            raise WebAdmissionError("length_required", 411)
        if not re.fullmatch(r"[0-9]{1,8}", lengths[0]):
            raise WebAdmissionError()
        length = int(lengths[0])
        if length > MAX_REQUEST_BYTES:
            raise WebAdmissionError("request_too_large", 413)
        try:
            async with asyncio.timeout(5):
                encoded = await request.content.readexactly(length)
        except (asyncio.IncompleteReadError, ConnectionError):
            raise WebAdmissionError() from None
        except TimeoutError:
            raise WebAdmissionError("request_timeout", 408) from None
        return encoded

    def _playlist_export(self, request: WebPlaylistExport) -> web.Response:
        """Capture once after body read; no await, cache, dispatch or state sync."""
        state = self._current_state()
        queue = state.queue
        if (not secrets.compare_digest(request.expected_session, self._session_id)
                or request.expected_generation != state.generation):
            raise WebAdmissionError("stale_export", 409)
        encoded, restored = prepare_playlist_export(queue)
        revision = hmac.new(self._export_key, _EXPORT_DOMAIN + encoded, hashlib.sha256).hexdigest()
        if request.action == "download":
            if not secrets.compare_digest(request.expected_export, revision):
                raise WebAdmissionError("stale_export", 409)
            return web.Response(body=encoded, content_type="application/json", headers={
                **_HEADERS, "Content-Length": str(len(encoded)),
                "Content-Disposition": f'attachment; filename="{_EXPORT_FILENAME}"'})
        return web.json_response({"version": 1, "session_id": self._session_id,
            "generation": state.generation, "revision": revision, "name": _queue_label(restored.name),
            "count": restored.count, "index": restored.index, "mode": _MODE_NAMES[restored.loop],
            "size_bytes": len(encoded), "filename": _EXPORT_FILENAME}, headers=_HEADERS)

    def snapshot(self) -> dict[str, object]:
        """Detach one complete projection without awaiting or performing I/O."""
        state = self._current_state()
        runtime = self._runtime()
        queue = state.queue
        item = queue.current
        transport_status = ("ERROR" if state.phase == PlaybackPhase.ERROR or runtime.transport_unknown else
                            "PENDING" if state.phase in {PlaybackPhase.COMMAND_ACCEPTED, PlaybackPhase.LOADING,
                                                         PlaybackPhase.BUFFERING} or runtime.transport_pending else "CONFIRMED")
        gain_status = ("PENDING" if runtime.gain_pending else "CONFIRMED"
                       if state.gain_confirmed and runtime.backend_gain_confirmed else "UNKNOWN")
        title = (_title(state.title, (item.url, item.resolved_url, state.current_url,
                    state.resolved_url, state.backend_url, state.backend_original_url), f"Track {queue.index}")
                 if item else "No selection")
        actions = {name: name in runtime.actions for name in ("play", "pause", "stop", "next", "previous", "seek")}
        can_save = False
        if item is not None:
            try:
                canonical_target_url(item)
                can_save = True
            except (ValueError, RadioResolutionError):
                pass
        visible_name = _queue_label(queue.name)
        actions.update(queue_select=bool(queue.items), queue_mode=not runtime.queue_mode_locked,
                       save_current=can_save)
        playlist_actions, playlist_reason = playlist_availability(queue, stopped=runtime.playlist_stopped)
        actions.update(playlist_actions)
        dsp = self._dsp_snapshot()
        dsp = {**dsp, "name": _dsp_name(dsp.get("name", ""))}
        self._revision += 1
        device_name = self._device_name()
        visible_device_name = _dsp_name(device_name, "Open Room Renderer")
        return {
            "version": 1, "session_id": self._session_id, "revision": self._revision,
            "csrf_token": self._csrf_token, "generation": state.generation,
            "device": {"name": visible_device_name, "name_visible": visible_device_name == device_name,
                       "revision": self.device_revision(device_name)},
            "transport": {"phase": state.phase.value, "intent": state.intent, "status": transport_status,
                "recovery": _playback_recovery(state, runtime),
                "message": "Playback state is unconfirmed. Stop is available for recovery." if transport_status == "ERROR"
                    else "Waiting for renderer confirmation." if transport_status == "PENDING" else ""},
            "media": {"title": title, "artist": _display(state.artist), "album": _display(state.album),
                "artwork": safe_artwork(state.album_art_uri), "is_live": bool(item and item.stream_type == "station"),
                "position_seconds": max(0, int(state.curpos)) if item and transport_status == "CONFIRMED" else None,
                "duration_seconds": _duration(state.totlen)},
            "gain": {"status": gain_status, "volume": state.volume if gain_status == "CONFIRMED" else None,
                "mute": state.mute if gain_status == "CONFIRMED" else None,
                "last_confirmed": {"volume": state.volume, "mute": state.mute} if gain_status == "CONFIRMED" else None},
            "queue": {"name": visible_name, "count": queue.count, "index": queue.index,
                "playlist_edit_reason": playlist_reason,
                "mode": _MODE_NAMES[queue.loop], "items": [
                    {"index": number, "title": _title(entry.title, (entry.url, entry.resolved_url), f"Track {number}"),
                     "artist": _display(entry.artist), "album": _display(entry.album),
                     "duration_seconds": _duration(entry.duration)} for number, entry in enumerate(queue.items, 1)]},
            "dsp": dsp, "actions": actions,
        }

    def favorites_snapshot(self) -> dict[str, object]:
        document = self._favorites.document()
        records = []
        for entry in document["favorites"]:
            try:
                target = urlsplit(entry["url"]).hostname or "Saved target"
            except ValueError:
                target = "Saved target"
            # Saved current selections retain legacy metadata, but their
            # URL-fallback provenance is not persisted in FavoritesStore.
            # Never expose those legacy basename/query projections here.
            title = _title(entry["title"], (entry["url"],), "")
            records.append({"id": entry["id"], "editable": entry["editable"],
                "name": _display(entry["name"], "Favorite"), "title": title,
                "artist": _display(entry["artist"]), "album": _display(entry["album"]),
                "artwork": safe_artwork(entry["artwork"]), "target_label": _display(target, "Saved target")})
        return {"version": 1, "session_id": self._session_id, "favorites": records}

    def _admit(self, command: WebCommand) -> WebCommand:
        """All guards run immediately before dispatch, with no intervening await."""
        if command.action == "favorite_restore" and not secrets.compare_digest(command.expected_session, self._session_id):
            raise WebAdmissionError("stale_favorites_transfer", 409)
        state = self._current_state()
        if command.expected_generation is not None and command.expected_generation != state.generation:
            raise WebAdmissionError("stale_generation", 409)
        if command.action.startswith("queue_"):
            if command.queue_name != _queue_label(state.queue.name):
                raise WebAdmissionError("stale_generation", 409)
            if command.action == "queue_select" and not 1 <= command.index <= state.queue.count:
                raise WebAdmissionError("unavailable", 409)
        runtime = self._runtime()
        if command.action in {"radio_play", "favorite_radio"}:
            found = self._radio_results.get(command.id)
            if found is None or time.monotonic() - found[0] > 900:
                raise WebAdmissionError("radio_expired", 409)
            if command.action == "radio_play" and not runtime.catalog_proxy_available:
                raise WebAdmissionError("radio_proxy_unavailable", 503)
            if command.action == "radio_play" and (runtime.transport_unknown or runtime.transport_pending):
                raise WebAdmissionError("unavailable", 409)
            command = replace(command, name=found[1].name, url=found[1].reference)
        if command.action in {"play", "pause", "next", "previous", "seek"} and command.action not in runtime.actions:
            raise WebAdmissionError("unavailable", 409)
        if command.action == "queue_mode" and runtime.queue_mode_locked:
            raise WebAdmissionError("unavailable", 409)
        if command.action in PLAYLIST_ACTIONS:
            actions, _reason = playlist_availability(state.queue, stopped=runtime.playlist_stopped)
            if not actions[command.action]:
                raise WebAdmissionError("unavailable", 409)
            try:
                queue = command.source if command.action == "playlist_replace" else prepare_edit(
                    state.queue, command.action, urls=command.urls, index=command.index,
                    from_index=command.from_index, to_index=command.to_index)
            except WebPlaylistError:
                raise WebAdmissionError() from None
            command = replace(command, prepared_queue=queue)
        return command

    def _favorites_export(self, encoded: bytes) -> web.Response:
        request = decode_favorites_export(encoded)
        if not secrets.compare_digest(request.expected_session, self._session_id):
            raise WebAdmissionError("stale_favorites_transfer", 409)
        try:
            data = prepare_favorites_export(self._favorites.backup_bytes())
        except FavoriteError as exc:
            if exc.code == "library_unavailable":
                raise WebAdmissionError("favorites_unavailable", 503) from None
            raise
        revision = hmac.new(self._export_key, _FAVORITES_EXPORT_DOMAIN + data, hashlib.sha256).hexdigest()
        if request.action == "download":
            if not secrets.compare_digest(request.expected_export, revision):
                raise WebAdmissionError("stale_favorites_transfer", 409)
            return web.Response(body=data, content_type="application/json", headers={
                **_HEADERS, "Content-Length": str(len(data)),
                "Content-Disposition": f'attachment; filename="{FAVORITES_EXPORT_FILENAME}"'})
        return web.json_response({"version": 1, "session_id": self._session_id,
            "revision": revision, "count": len(decode_favorites_document(data)),
            "size_bytes": len(data), "filename": FAVORITES_EXPORT_FILENAME,
            "scope": "managed_only"}, headers=_HEADERS)

    async def _dsp_export(self, encoded: bytes) -> web.Response:
        request = decode_dsp_export(encoded)
        if self._export_dsp_curve is None:
            raise WebAdmissionError("unavailable", 409)
        data, shared = await self._export_dsp_curve(request)
        revision = self._dsp_digest("curve_export", [request.expected_dsp, request.preset_id,
                                                     data.decode("utf-8")])
        if request.action == "download":
            if not secrets.compare_digest(request.expected_export, revision):
                raise WebAdmissionError("stale_dsp", 409)
            return web.Response(body=data, content_type="application/json", headers={
                **_HEADERS, "Content-Length": str(len(data)),
                "Content-Disposition": f'attachment; filename="{CURVE_EXPORT_FILENAME}"'})
        return web.json_response({"version": 1, "dsp_revision": request.expected_dsp,
            "preset_id": request.preset_id, "revision": revision, "size_bytes": len(data),
            "filename": CURVE_EXPORT_FILENAME, "scope": "current_plugin", "shared": shared,
            "band_count": 40}, headers=_HEADERS)

    async def _output_inspection(self, encoded: bytes) -> web.Response:
        payload = _decode_request(encoded)
        if (payload.keys() != {"action", "expected_session"} or payload["action"] != "inspect"
                or not isinstance(payload["expected_session"], str)
                or not payload["expected_session"].isascii()):
            raise WebAdmissionError()
        if not secrets.compare_digest(payload["expected_session"], self._session_id):
            raise WebAdmissionError("stale_output_inspection", 409)
        if self._inspect_output is None:
            raise WebAdmissionError("output_inspection_unavailable", 503)
        if self._output_inspection_busy:
            raise WebAdmissionError("output_inspection_busy", 409)
        # Synchronous claim on the shared adapter before the first await. Never
        # queue behind another probe or hold the renderer's playback lock.
        self._output_inspection_busy = True
        try:
            summary = await self._inspect_output()
            return web.json_response({**summary, "session_id": self._session_id}, headers=_HEADERS)
        except Exception:
            # No raw exception, backend response or playback UNKNOWN outcome.
            return self._error("output_inspection_failed", 503, "ERROR")
        finally:
            self._output_inspection_busy = False

    async def _radio_search(self, encoded: bytes) -> web.Response:
        payload = _decode_request(encoded)
        if payload.keys() != {"action", "name", "country"} or payload["action"] != "search":
            raise WebAdmissionError("radio_invalid", 400)
        name, country = payload["name"], payload["country"]
        if (not isinstance(name, str) or name.strip() and not 2 <= len(name.strip()) <= 80
                or _INVALID_TEXT.search(name) or not isinstance(country, str)
                or country and not re.fullmatch(r"[A-Z]{2}", country)
                or not name.strip() and not country):
            raise WebAdmissionError("radio_invalid", 400)
        if self._radio_busy:
            raise WebAdmissionError("radio_busy", 409)
        self._radio_busy = True
        try:
            stations = await search_stations(name, country)
            now = time.monotonic()
            self._radio_results = {key: value for key, value in self._radio_results.items()
                                   if now - value[0] <= 900}
            results = []
            for station in stations:
                token = secrets.token_hex(16)
                self._radio_results[token] = (now, station)
                results.append({"id": token, "name": station.name, "country": station.country,
                                "codec": station.codec, "bitrate": station.bitrate})
            while len(self._radio_results) > 100:
                del self._radio_results[next(iter(self._radio_results))]
            return web.json_response({"session_id": self._session_id, "stations": results}, headers=_HEADERS)
        except CatalogError:
            return self._error("radio_unavailable", 503, "ERROR")
        finally:
            self._radio_busy = False

    async def handle(self, request: web.Request) -> web.Response:
        dispatched = False
        try:
            origin = self._origin(request)
            path = request.path
            # Encoded separators, traversal, aliases and query variants are not assets.
            if request.raw_path.split("?", 1)[0] != path:
                raise WebAdmissionError("not_found", 404)
            if path in _ASSETS:
                if request.method != "GET":
                    raise WebAdmissionError("method_not_allowed", 405)
                self._bodyless(request)
                filename, content_type = _ASSETS[path]
                if filename is None:
                    # Explicit empty same-origin favicon. A data: favicon is
                    # blocked by our intentionally unchanged image CSP.
                    return web.Response(status=204, content_type=content_type, headers=_HEADERS)
                body = files("fake_linkplay").joinpath("web_assets", filename).read_bytes()
                return web.Response(body=body, content_type=content_type, charset="utf-8", headers=_HEADERS)
            if path in {PREFIX + "/state", PREFIX + "/favorites"}:
                if request.method != "GET":
                    raise WebAdmissionError("method_not_allowed", 405)
                self._bodyless(request)
                if path.endswith("/state"):
                    value = self.snapshot()
                else:
                    try:
                        value = self.favorites_snapshot()
                    except FavoriteError as exc:
                        # Only this admitted read receives repair guidance;
                        # dispatched mutations retain their UNKNOWN outcome.
                        if exc.code == "library_unavailable":
                            return self._error("favorites_unavailable", 503, "ERROR")
                        raise
                return web.json_response(value, headers=_HEADERS)
            if path not in {PREFIX + "/command", PREFIX + "/playlist/export", PREFIX + "/output/inspect", PREFIX + "/dsp/export", PREFIX + "/favorites/export", PREFIX + "/radio/search"}:
                raise WebAdmissionError("not_found", 404)
            if request.method != "POST":
                raise WebAdmissionError("method_not_allowed", 405)
            encoded = await self._body(request, origin)
            if path == PREFIX + "/radio/search":
                return await self._radio_search(encoded)
            if path == PREFIX + "/favorites/export":
                return self._favorites_export(encoded)
            if path == PREFIX + "/dsp/export":
                return await self._dsp_export(encoded)
            if path == PREFIX + "/output/inspect":
                return await self._output_inspection(encoded)
            if path == PREFIX + "/playlist/export":
                return self._playlist_export(decode_playlist_export(encoded))
            command = self._admit(decode_command(encoded))
            dispatched = True
            result = await self._dispatch(command)
            return web.json_response({"request_acknowledged": True, **(result or {})}, headers=_HEADERS)
        except (WebAdmissionError, CatalogPlaybackUnavailable) as exc:
            return self._error(exc.code, exc.status)
        except WebDspCommandError as exc:
            return self._error("command_failed", 502, "UNKNOWN", effect=exc.effect)
        except DeviceNameCommittedError:
            return self._error("device_rename_committed", 502, "UNKNOWN", committed=True)
        except AtomicWriteCommittedError:
            code = "device_rename_committed" if dispatched and command.action == "device_rename" else "command_failed"
            return self._error(code, 502, "UNKNOWN", committed=True)
        except Exception:
            # Core operations can have effects before raising, including
            # rejected native navigation/seek and persistence-after-commit.
            return self._error("command_failed" if dispatched else "read_failed", 502 if dispatched else 503,
                               "UNKNOWN" if dispatched else "ERROR")
