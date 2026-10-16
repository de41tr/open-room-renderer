"""Explicit single-track TIDAL resolution, never background provider access.

Only a saved, unexpired tidalapi 0.8.11 device session is accepted. The result
is a private CDN candidate, not evidence that audio played or a full asset was
provided. Callers must keep its URL out of public status, queues and logs, and
use the public-only, DNS-pinned media proxy before playback. No token refresh,
DRM handling, playlist expansion or provider-provided endpoint is supported.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import math
import re
import time
from urllib.parse import urlsplit

import aiohttp

from .network_policy import NetworkPolicyError, resolve_proxy_target
from .tidal_session import TidalSession, TidalSessionError, TidalSessionStore

MAX_RESPONSE_BYTES = 65536
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
    "storage_unavailable": "Private TIDAL storage is unavailable or already in use.",
    "unauthorized": "TIDAL rejected this session; sign in again explicitly.",
    "provider_rejected": "TIDAL rejected the playback request.",
    "request_failed": "The TIDAL playback request failed or timed out.",
    "invalid_response": "TIDAL returned an invalid playback response.",
    "unsupported_manifest": "Only a single unencrypted TIDAL BTS audio candidate is supported.",
    "unsupported_asset": "TIDAL returned a preview or another unsupported asset presentation.",
    "unsafe_destination": "The TIDAL audio candidate has an unsafe or unavailable destination.",
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


def _device_session(session: TidalSession | None) -> TidalSession:
    if (session is None or session.auth_flow != "tidalapi-device-v0.8.11"
            or not session.client_id or not session.country_code
            or type(session.user_id) is not str
            or not re.fullmatch(r"[1-9][0-9]{0,18}", session.user_id)
            or int(session.user_id) > 2**63 - 1
            or not re.fullmatch(r"[\x21-\x7e]{1,16384}", session.access_token)):
        raise TidalPlaybackError("session_required")
    _unexpired(session)
    return session


def _session_fingerprint(session: TidalSession) -> bytes:
    # Include every session field, including credentials and provenance. This
    # transient digest binds a candidate to the exact record that resolved it;
    # it must never enter public status, serialized queues or diagnostics.
    record = json.dumps(asdict(session), sort_keys=True, separators=(",", ":"),
                        ensure_ascii=True, allow_nan=False).encode("ascii")
    return hashlib.sha256(record).digest()


async def _request_json(http, track_id: str, session: TidalSession, *,
                        playback: bool = False, quality: str = "HIGH") -> dict:
    # URLs are constructed here; neither callers nor provider JSON choose an
    # endpoint. The Bearer header is only ever sent to this fixed API origin.
    _unexpired(session)
    url = _API_TRACKS + track_id
    params = {"countryCode": session.country_code}
    if playback:
        url += "/playbackinfopostpaywall"
        params.update(playbackmode="STREAM", audioquality=quality, assetpresentation="FULL")
    async with http.request("GET", url, params=params, allow_redirects=False, headers={
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
            if len(body) > MAX_RESPONSE_BYTES:
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

    def __init__(self, store: TidalSessionStore):
        self._store = store

    def session_matches(self, fingerprint: bytes) -> bool:
        """Recheck the private session immediately before consuming a candidate."""
        if type(fingerprint) is not bytes or len(fingerprint) != 32:
            return False
        try:
            session = _device_session(self._store.load())
            return hmac.compare_digest(fingerprint, _session_fingerprint(session))
        except (ValueError, TypeError, OSError, OverflowError):
            return False

    async def resolve_track(self, track_id: str, quality: str = "HIGH") -> TidalTrackCandidate:
        if (type(track_id) is not str or not re.fullmatch(r"[1-9][0-9]{0,19}", track_id)
                or type(quality) is not str or quality not in _QUALITIES | {"AUTO"}):
            raise TidalPlaybackError("invalid_selection")
        quality = "HIGH" if quality == "AUTO" else quality
        try:
            with self._store.lease() as lease:
                session = _device_session(lease.load())
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
