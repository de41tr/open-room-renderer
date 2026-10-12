"""Resolve public station IDs through the unauthenticated RadioTime endpoint.

This narrow adapter supports the observed direct MP3/AAC station response. It
does not accept search URLs, account credentials, partner keys, or episode IDs.
Callers retain ownership of queue metadata and of safely opening the returned
media URL: DNS validation here is not a connection-time pin for later playback.
"""

from __future__ import annotations

import asyncio
import json
import posixpath
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, TCPConnector

from .network_policy import NetworkPolicyError, PinnedResolver, ProxyTarget, resolve_proxy_target


_ENDPOINT = "https://opml.radiotime.com/Tune.ashx"
_MAX_JSON_BYTES = 256 * 1024
_MAX_CANDIDATES = 16
_DEADLINE_SECONDS = 10
_STATION_ID = re.compile(r"s[0-9]{1,32}\Z")


class RadioResolutionError(ValueError):
    """The station could not be resolved to supported, public audio."""


@dataclass(frozen=True)
class RadioResolution:
    url: str
    station_id: str
    duration: int = 0
    content_type: str = "audio"


Fetcher = Callable[[str], Awaitable[bytes]]
Validator = Callable[[str], Awaitable[ProxyTarget | bool | None]]


async def _fetch_json(url: str) -> bytes:
    """Fetch one validated, pinned endpoint with no redirects or ambient auth."""
    target = await resolve_proxy_target(url)
    connector = TCPConnector(resolver=PinnedResolver(target), use_dns_cache=False, limit=1)
    async with ClientSession(
        connector=connector,
        timeout=ClientTimeout(total=_DEADLINE_SECONDS, sock_connect=5, sock_read=5),
        trust_env=False,
        cookie_jar=DummyCookieJar(),
        auto_decompress=False,
        headers={
            "User-Agent": "OpenRoomRenderer/0.1",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        },
    ) as session:
        async with session.get(target.url, allow_redirects=False) as response:
            if response.status != 200:
                raise RadioResolutionError("Radio service returned an unsuccessful response")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise RadioResolutionError("Radio service returned unsupported content encoding")
            if response.content_length is not None and response.content_length > _MAX_JSON_BYTES:
                raise RadioResolutionError("Radio service response is too large")
            result = bytearray()
            async for chunk in response.content.iter_chunked(16 * 1024):
                if len(result) + len(chunk) > _MAX_JSON_BYTES:
                    raise RadioResolutionError("Radio service response is too large")
                result.extend(chunk)
            return bytes(result)


def _audio_candidates(raw: bytes) -> list[str]:
    if not isinstance(raw, bytes):
        raise RadioResolutionError("Radio service did not return JSON bytes")
    if len(raw) > _MAX_JSON_BYTES:
        raise RadioResolutionError("Radio service response is too large")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RadioResolutionError("Radio service returned invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("head"), dict):
        raise RadioResolutionError("Radio service returned an invalid response")
    head = payload["head"]
    if head.get("status") not in ("200", 200) or head.get("fault") or payload.get("fault"):
        raise RadioResolutionError("Radio station is unavailable")
    body = payload.get("body")
    if not isinstance(body, list):
        raise RadioResolutionError("Radio service returned an invalid response")
    return [
        item["url"] for item in body
        if isinstance(item, dict)
        and item.get("element") == "audio"
        and item.get("is_direct") is True
        and item.get("media_type") in ("mp3", "aac")
        and isinstance(item.get("url"), str)
    ]


def _supported_media_url(url: str) -> bool:
    """Discard non-media schemes and the service's spoken error placeholders."""
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            return False
        if parts.username is not None or parts.password is not None:
            return False
        # Decode repeatedly to recognize encoded forms without contacting the
        # destination. Public-address and full URL checks follow separately.
        path = parts.path
        for _ in range(3):
            path = unquote(path)
        path = posixpath.normpath(re.sub(r"/+", "/", path)).casefold()
        return "/service/audio/" not in path
    except (ValueError, UnicodeError):
        return False


def public_station_reference(station_id: str) -> str:
    """Return the stable public reference for a supported station, without I/O."""
    if not isinstance(station_id, str) or not _STATION_ID.fullmatch(station_id):
        raise RadioResolutionError("Unsupported radio station ID")
    return f"{_ENDPOINT}?id={station_id}&formats=mp3,aac&render=json"


async def resolve_station(
    station_id: str, *, fetcher: Fetcher | None = None, validator: Validator | None = None,
) -> RadioResolution:
    """Resolve a lowercase ``s`` + 1–32 ASCII digits station ID.

    The optional async ``fetcher(url)`` returns raw JSON bytes and replaces
    endpoint I/O for deterministic tests. ``validator(url)`` replaces public
    destination validation: it may return a ProxyTarget (normalized URL), None,
    or True on success; False or a validation exception rejects the candidate.
    Both hooks remain bounded by the same deadline as production resolution.
    Neither hook receives queue metadata, source search URLs, or credentials.
    """
    endpoint = public_station_reference(station_id)
    fetch = fetcher if fetcher is not None else _fetch_json
    validate = validator if validator is not None else resolve_proxy_target
    try:
        async with asyncio.timeout(_DEADLINE_SECONDS):
            for url in _audio_candidates(await fetch(endpoint))[:_MAX_CANDIDATES]:
                if not _supported_media_url(url):
                    continue
                try:
                    target = await validate(url)
                except (NetworkPolicyError, ClientError, OSError, ValueError):
                    continue
                if target is False:
                    continue
                return RadioResolution(
                    url=target.url if isinstance(target, ProxyTarget) else url,
                    station_id=station_id,
                )
            raise RadioResolutionError("Radio station has no supported public audio")
    except RadioResolutionError:
        raise
    except (TimeoutError, ClientError, OSError, ValueError) as exc:
        raise RadioResolutionError("Radio station resolution failed") from exc
