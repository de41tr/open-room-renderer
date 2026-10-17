"""Explicit single-track TIDAL resolution, never background provider access.

Only a saved tidalapi 0.8.11 device session is accepted. An explicitly wired
refresh owner may renew a recognized expiry once before provider access. The result
is a private CDN candidate, not evidence that audio played or a full asset was
provided. Callers must keep its URL out of public status, queues and logs, and
use the public-only, DNS-pinned media proxy before playback. No 401 retry, DRM
handling, automatic collection paging or provider-provided endpoint is supported.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import hmac
import json
import math
import re
import time
from urllib.parse import urlsplit

import aiohttp

from .network_policy import NetworkPolicyError, resolve_proxy_target
from .tidal_session import TidalSession, TidalSessionError, TidalSessionStore, _session_fingerprint

MAX_RESPONSE_BYTES = 65536
MAX_PAGE_RESPONSE_BYTES = 512 * 1024  # Up to 50 catalogue records with rich metadata.
MAX_MANIFEST_BYTES = 32768
_MAX_URL_BYTES = 8192
_API_TRACKS = "https://api.tidal.com/v1/tracks/"
_BTS_MIME = "application/vnd.tidal.bts"
_QUALITIES = frozenset({"LOW", "HIGH", "LOSSLESS"})
_AUDIO_TYPES = frozenset({"audio/flac", "audio/x-flac", "audio/mp4", "audio/mpeg", "audio/aac", "audio/ogg"})
_MESSAGES = {
    "invalid_selection": "Select one numeric TIDAL track and LOW, HIGH or LOSSLESS quality.",
    "session_required": "An unexpired tidalapi device-login session is required for TIDAL playback.",
    "session_expired": "The TIDAL session has expired or has no verified expiry; sign in again explicitly.",
    "refresh_result_uncertain": "The previous TIDAL refresh outcome is uncertain; explicit local session recovery is required before another login.",
    "refresh_rejected": "TIDAL rejected the refresh grant; explicit local session recovery and a new login are required.",
    "refresh_failed": "TIDAL session refresh could not complete; no playback request was sent.",
    "storage_unavailable": "Private TIDAL storage is unavailable or already in use.",
    "unauthorized": "TIDAL rejected this session; sign in again explicitly.",
    "provider_rejected": "TIDAL rejected the playback request.",
    "request_failed": "The TIDAL playback request failed or timed out.",
    "invalid_response": "TIDAL returned an invalid playback response.",
    "unsupported_manifest": "Only a single unencrypted TIDAL BTS audio candidate is supported.",
    "unsupported_asset": "TIDAL returned a preview or another unsupported asset presentation.",
    "unsafe_destination": "The TIDAL audio candidate has an unsafe or unavailable destination.",
    "invalid_page_selection": "Select one supported bounded TIDAL track page.",
    "invalid_page_response": "TIDAL returned an inconsistent catalogue page.",
    "unsupported_page": "Only nonempty pages of explicitly streamable audio tracks are supported.",
}


class TidalPlaybackError(ValueError):
    """Fixed diagnostics; provider errors, tokens and signed URLs stay private."""

    def __init__(self, code: str):
        self.code = code if code in _MESSAGES else "invalid_response"
        super().__init__(_MESSAGES[self.code])


@dataclass(frozen=True, repr=False)
class TidalTrackCandidate:
    """Private transient resolution, with safe catalogue/display fields only.

    asset_presentation=None means the response did not prove a full asset.
    Even 'FULL' reports the API's assertion, never successful audio playback.
    """

    track_id: str
    catalogue_url: str
    title: str
    artist: str
    album: str
    duration: float
    content_type: str
    codec: str
    stream_url: str
    audio_quality: str
    asset_presentation: str | None
    session_expires_at: float
    session_fingerprint: bytes


def validate_track_selection(track_id: str, quality: str) -> str:
    """Local-only selection validation shared by preparation and resolution."""
    if (type(track_id) is not str or not re.fullmatch(r"[1-9][0-9]{0,19}", track_id)
            or type(quality) is not str or quality not in _QUALITIES | {"AUTO"}):
        raise TidalPlaybackError("invalid_selection")
    return "HIGH" if quality == "AUTO" else quality


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invalid_constant(value: str):
    raise ValueError


def _json_object(body: bytes) -> dict:
    value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object,
                       parse_constant=_invalid_constant)
    if type(value) is not dict:
        raise ValueError
    return value


def _unexpired(session: TidalSession) -> None:
    if session.expires_at is None or session.expires_at <= time.time():
        raise TidalPlaybackError("session_expired")


def _device_session(session: TidalSession | None, *, allow_expired=False) -> TidalSession:
    if (session is None or session.auth_flow != "tidalapi-device-v0.8.11"
            or not session.client_id or not session.country_code
            or type(session.user_id) is not str
            or not re.fullmatch(r"[1-9][0-9]{0,18}", session.user_id)
            or int(session.user_id) > 2**63 - 1
            or not re.fullmatch(r"[\x21-\x7e]{1,16384}", session.access_token)):
        raise TidalPlaybackError("session_required")
    if not allow_expired:
        _unexpired(session)
    return session


async def _request_json(http, track_id: str, session: TidalSession, *,
                        playback: bool = False, quality: str = "HIGH") -> dict:
    # URLs are constructed here; neither callers nor provider JSON choose an
    # endpoint. The Bearer header is only ever sent to this fixed API origin.
    path = "tracks/" + track_id
    params = {"countryCode": session.country_code}
    if playback:
        path += "/playbackinfopostpaywall"
        params.update(playbackmode="STREAM", audioquality=quality, assetpresentation="FULL")
    return await _request_api_json(http, path, session, params)


async def _request_api_json(http, path: str, session: TidalSession, params: dict, *,
                            max_response_bytes: int = MAX_RESPONSE_BYTES) -> dict:
    """Shared bounded fixed-origin reader; paths come only from local admission."""
    _unexpired(session)
    async with http.request("GET", "https://api.tidal.com/v1/" + path,
                            params=params, allow_redirects=False, headers={
        "Authorization": "Bearer " + session.access_token,
        "Accept": "application/json", "Accept-Encoding": "identity",
    }) as response:
        if response.status == 401:
            raise TidalPlaybackError("unauthorized")
        if response.status != 200:
            raise TidalPlaybackError("provider_rejected")
        body = bytearray()
        async for chunk in response.content.iter_chunked(8192):
            body.extend(chunk)
            if len(body) > max_response_bytes:
                raise TidalPlaybackError("invalid_response")
        return _json_object(body)


def _matches_track(value: object, track_id: str) -> bool:
    return ((type(value) is int and value > 0) or
            (type(value) is str and re.fullmatch(r"[1-9][0-9]{0,19}", value))) and str(value) == track_id


def _text(value: object, limit: int = 512) -> str:
    if (type(value) is not str or len(value) > limit
            or any(ord(char) < 32 or 127 <= ord(char) <= 159
                   or 0xD800 <= ord(char) <= 0xDFFF or ord(char) in (0xFFFE, 0xFFFF)
                   for char in value)):
        raise TidalPlaybackError("invalid_response")
    return value


def _metadata(value: dict, track_id: str) -> tuple[str, str, str, float]:
    if not _matches_track(value.get("id"), track_id):
        raise TidalPlaybackError("invalid_response")
    if "allowStreaming" in value and value["allowStreaming"] is not True:
        raise TidalPlaybackError("provider_rejected")
    artist, album = value.get("artist", {}), value.get("album", {})
    duration = value.get("duration", 0)
    if (type(artist) is not dict or type(album) is not dict
            or type(duration) not in (int, float) or not 0 <= duration <= 86400
            or not math.isfinite(duration)):
        raise TidalPlaybackError("invalid_response")
    return (_text(value.get("title", "")), _text(artist.get("name", "")),
            _text(album.get("title", "")), float(duration))


def _has_drm_fields(value: object) -> bool:
    if type(value) is dict:
        for key, item in value.items():
            normalized = key.lower().replace("_", "").replace("-", "")
            if (normalized in {"keyid", "keyids", "encryptionkey", "securitytoken"}
                    or "drm" in normalized or "license" in normalized):
                return True
            if _has_drm_fields(item):
                return True
    elif type(value) is list:
        return any(_has_drm_fields(item) for item in value)
    return False


def _manifest(value: dict, track_id: str) -> tuple[str, str, str, str, str | None]:
    if not _matches_track(value.get("trackId"), track_id):
        raise TidalPlaybackError("invalid_response")
    presentation = value.get("assetPresentation")
    if (("assetPresentation" in value and presentation != "FULL")
            or value.get("playbackMode", "STREAM") != "STREAM"
            or value.get("preview", False) is not False
            or value.get("isPreview", False) is not False):
        raise TidalPlaybackError("unsupported_asset")
    if value.get("manifestMimeType") != _BTS_MIME or _has_drm_fields(value):
        raise TidalPlaybackError("unsupported_manifest")
    encoded = value.get("manifest")
    if (type(encoded) is not str or not encoded
            or len(encoded) > 4 * ((MAX_MANIFEST_BYTES + 2) // 3)):
        raise TidalPlaybackError("unsupported_manifest")
    try:
        decoded = base64.b64decode(encoded, validate=True)
        if len(decoded) > MAX_MANIFEST_BYTES or base64.b64encode(decoded).decode("ascii") != encoded:
            raise ValueError
        manifest = _json_object(decoded)
    except (ValueError, UnicodeError, RecursionError):
        raise TidalPlaybackError("unsupported_manifest") from None
    if (manifest.get("encryptionType") != "NONE" or _has_drm_fields(manifest)
            or type(manifest.get("urls")) is not list or len(manifest["urls"]) != 1
            or manifest.get("mimeType") not in _AUDIO_TYPES):
        raise TidalPlaybackError("unsupported_manifest")
    codec = _text(manifest.get("codecs", ""), 128)
    quality = value.get("audioQuality", "")
    if quality not in _QUALITIES | {"", "HI_RES", "HI_RES_LOSSLESS"}:
        raise TidalPlaybackError("invalid_response")
    return manifest["urls"][0], manifest["mimeType"], codec, quality, presentation


async def _public_candidate_url(value: object) -> str:
    try:
        if type(value) is not str or len(value) > _MAX_URL_BYTES:
            raise ValueError
        parts = urlsplit(value)
        if (parts.scheme != "https" or parts.port not in (None, 443)
                or parts.username is not None or parts.password is not None or parts.fragment
                or parts.path.lower().endswith((".mpd", ".m3u8", ".m3u"))):
            raise ValueError
        # No LAN exception. Actual playback must resolve again and pin every
        # address at use, since this admission lookup cannot prevent rebinding.
        target = await resolve_proxy_target(value, ())
        return target.url
    except (ValueError, UnicodeError, NetworkPolicyError, OSError):
        raise TidalPlaybackError("unsafe_destination") from None


class TidalPlaybackResolver:
    """Constructing a resolver performs no I/O; resolve only on explicit play."""

    def __init__(self, store: TidalSessionStore, *, refresh_manager=None):
        self._store = store
        self._refresh = refresh_manager

    def session_available(self) -> bool:
        """Local-only readiness gate; never refresh or disclose a session."""
        try:
            session = _device_session(self._store.load(), allow_expired=self._refresh is not None)
            if session.expires_at is None:
                return False
            return (session.expires_at > time.time()
                    or self._refresh is not None and type(session.refresh_token) is str
                    and bool(re.fullmatch(r"[\x21-\x7e]{1,16384}", session.refresh_token))
                    and self._store.refresh_outcome(session) is None)
        except (TidalSessionError, TidalPlaybackError, OSError):
            return False

    async def _session_owned(self, lease):
        session = _device_session(lease.load(), allow_expired=True)
        if (self._refresh is not None and session.expires_at is not None
                and session.expires_at <= time.time()):
            result = await self._refresh.refresh_owned(lease, only_if_expired=True)
            if result["state"] not in {"current", "refreshed"}:
                code = result.get("error")
                if code not in {"refresh_rejected", "refresh_result_uncertain", "storage_unavailable"}:
                    code = "refresh_result_uncertain" if result["refresh_outcome"] == "uncertain" else "refresh_failed"
                raise TidalPlaybackError(code)
        return _device_session(lease.load())

    def session_matches(self, fingerprint: bytes) -> bool:
        """Recheck the private session immediately before consuming a candidate."""
        if type(fingerprint) is not bytes or len(fingerprint) != 32:
            return False
        try:
            session = _device_session(self._store.load())
            return hmac.compare_digest(fingerprint, _session_fingerprint(session))
        except (ValueError, TypeError, OSError, OverflowError):
            return False

    async def resolve_page(self, catalogue_url: str):
        """Read exactly one validated page under the existing private store lease."""
        from .tidal_page import TidalPageCandidate, page_track_ids, parse_page_request
        request = parse_page_request(catalogue_url)
        try:
            with self._store.lease() as lease:
                session = _device_session(lease.load(), allow_expired=True)
                params = dict(request.params)
                if (params.get("countryCode", session.country_code) != session.country_code
                        or request.favorites_user_id and request.favorites_user_id != session.user_id):
                    raise TidalPlaybackError("invalid_page_selection")
                session = await self._session_owned(lease)
                if request.favorites_user_id and request.favorites_user_id != session.user_id:
                    raise TidalPlaybackError("invalid_page_selection")
                params["countryCode"] = session.country_code
                async with asyncio.timeout(15):
                    async with aiohttp.ClientSession(
                        trust_env=False, auto_decompress=False,
                        timeout=aiohttp.ClientTimeout(total=15),
                        cookie_jar=aiohttp.DummyCookieJar(),
                    ) as http:
                        value = await _request_api_json(http, request.path, session, params,
                                                        max_response_bytes=MAX_PAGE_RESPONSE_BYTES)
                    track_ids = page_track_ids(value, request)
                    _unexpired(session)
                    return TidalPageCandidate(request.catalogue_url, track_ids,
                                              _session_fingerprint(session), float(session.expires_at))
        except TidalPlaybackError:
            raise
        except TidalSessionError:
            raise TidalPlaybackError("storage_unavailable") from None
        except (aiohttp.ClientError, OSError, TimeoutError):
            raise TidalPlaybackError("request_failed") from None
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise TidalPlaybackError("invalid_page_response") from None

    async def resolve_track(self, track_id: str, quality: str = "HIGH") -> TidalTrackCandidate:
        quality = validate_track_selection(track_id, quality)
        try:
            with self._store.lease() as lease:
                session = await self._session_owned(lease)
                async with asyncio.timeout(15):
                    async with aiohttp.ClientSession(
                        trust_env=False, auto_decompress=False,
                        timeout=aiohttp.ClientTimeout(total=15),
                        cookie_jar=aiohttp.DummyCookieJar(),
                    ) as http:
                        metadata = await _request_json(http, track_id, session)
                        title, artist, album, duration = _metadata(metadata, track_id)
                        playback = await _request_json(http, track_id, session, playback=True, quality=quality)
                    url, content_type, codec, audio_quality, presentation = _manifest(playback, track_id)
                    url = await _public_candidate_url(url)
                    _unexpired(session)
                    return TidalTrackCandidate(track_id, _API_TRACKS + track_id,
                                               title, artist, album, duration, content_type,
                                               codec, url, audio_quality, presentation,
                                               float(session.expires_at), _session_fingerprint(session))
        except TidalPlaybackError:
            raise
        except TidalSessionError:
            raise TidalPlaybackError("storage_unavailable") from None
        except (aiohttp.ClientError, OSError, TimeoutError):
            raise TidalPlaybackError("request_failed") from None
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise TidalPlaybackError("invalid_response") from None
