"""Small Radio-Browser adapter; public directory data, never an OEM catalog.

Only discovered HTTPS mirrors are queried. Station URLs are re-resolved by UUID
at playback and admitted by the public-address policy; search never opens them.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import random
import re
import socket
from urllib.parse import urlencode

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, TCPConnector

from .network_policy import PinnedResolver, resolve_proxy_target

MAX_RESULTS = 20
MAX_BYTES = 256 * 1024
DEADLINE = 12
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_MIRROR = re.compile(r"[a-z0-9-]+\.api\.radio-browser\.info\Z")
_REFERENCE = "https://www.radio-browser.info/history/"
_FORMATS = {"MP3", "AAC", "AAC+", "OGG"}


class CatalogError(ValueError):
    """Fixed-message provider failure; raw responses/URLs never reach the UI."""


class CatalogPlaybackUnavailable(ValueError):
    """Source admission failure, before any player ownership change."""
    code, status = "radio_proxy_unavailable", 503

    def __init__(self) -> None:
        super().__init__("The local radio proxy could not start. Check the renderer startup log "
                         "and restart it after resolving the local listener error.")


def station_reference(station_id: str) -> str:
    if not isinstance(station_id, str) or not _UUID.fullmatch(station_id):
        raise CatalogError("Invalid radio station")
    return _REFERENCE + station_id


def station_from_reference(url: str) -> str:
    if isinstance(url, str) and url.startswith(_REFERENCE) and _UUID.fullmatch(url[len(_REFERENCE):]):
        return url[len(_REFERENCE):]
    return ""


def _text(value: object, limit: int = 256) -> str:
    if not isinstance(value, str) or re.search(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff\ufffe\uffff]", value):
        return ""
    return value.strip().encode("utf-8")[:limit].decode("utf-8", errors="ignore")


@dataclass(frozen=True)
class CatalogStation:
    uuid: str
    name: str
    country: str
    codec: str
    bitrate: int
    url: str

    @property
    def reference(self) -> str:
        return station_reference(self.uuid)


def _stations(raw: bytes) -> list[CatalogStation]:
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise CatalogError("Radio directory response is too large")
    try:
        values = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise CatalogError("Radio directory returned invalid data") from None
    if not isinstance(values, list) or len(values) > MAX_RESULTS:
        raise CatalogError("Radio directory returned invalid data")
    result, seen = [], set()
    for item in values:
        if not isinstance(item, dict):
            continue
        uuid, name = item.get("stationuuid"), _text(item.get("name"))
        codec, url = item.get("codec"), item.get("url_resolved")
        if (not isinstance(uuid, str) or not _UUID.fullmatch(uuid) or uuid in seen or not name
                or not isinstance(codec, str) or codec not in _FORMATS
                or item.get("hls") != 0 or item.get("lastcheckok") != 1
                or not isinstance(url, str) or not url.startswith(("http://", "https://"))
                or len(url.encode("utf-8", errors="replace")) > 8192):
            continue
        # Full DNS/address validation is deferred until selected for playback.
        # No URL or external artwork is exposed to the browser by search.
        country = item.get("countrycode", "")
        country = country if isinstance(country, str) and re.fullmatch(r"[A-Z]{2}", country) else ""
        bitrate = item.get("bitrate")
        bitrate = bitrate if type(bitrate) is int and 0 <= bitrate <= 10000 else 0
        result.append(CatalogStation(uuid, name, country, codec, bitrate, url))
        seen.add(uuid)
    return result


async def _mirrors() -> list[str]:
    """Official DNS discovery, reverse names, random ordering; never a user host."""
    loop = asyncio.get_running_loop()
    target = await resolve_proxy_target("https://all.api.radio-browser.info/")
    addresses = list(target.addresses)
    random.SystemRandom().shuffle(addresses)
    async def reverse(address: str) -> str:
        try:
            host, _ = await loop.getnameinfo((address, 443), socket.NI_NAMEREQD)
            return host.lower().rstrip(".")
        except OSError:
            return ""
    names = await asyncio.gather(*(reverse(address) for address in addresses[:8]))
    return list(dict.fromkeys(name for name in names if _MIRROR.fullmatch(name)))[:3]


async def _fetch(url: str) -> bytes:
    target = await resolve_proxy_target(url)
    connector = TCPConnector(resolver=PinnedResolver(target), use_dns_cache=False, limit=1)
    async with ClientSession(connector=connector, trust_env=False, cookie_jar=DummyCookieJar(),
            auto_decompress=False, timeout=ClientTimeout(total=4, sock_connect=3, sock_read=3),
            headers={"User-Agent": "OpenRoomRenderer/0.1 (+https://github.com/de41tr/open-room-renderer)",
                     "Accept": "application/json", "Accept-Encoding": "identity"}) as session:
        async with session.get(target.url, allow_redirects=False) as response:
            if (response.status != 200 or response.headers.get("Content-Encoding", "identity").lower() != "identity"
                    or response.content_length is not None and response.content_length > MAX_BYTES):
                raise CatalogError("Radio directory is unavailable")
            result = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                if len(result) + len(chunk) > MAX_BYTES:
                    raise CatalogError("Radio directory response is too large")
                result.extend(chunk)
            return bytes(result)


async def _query(path: str) -> tuple[list[CatalogStation], str]:
    for host in await _mirrors():
        try:
            return _stations(await _fetch(f"https://{host}{path}")), host
        except (ValueError, ClientError, OSError, TimeoutError):
            continue
    raise CatalogError("Radio directory is unavailable. Try again later.")


async def search_stations(name: str, country: str = "") -> list[CatalogStation]:
    if (not isinstance(name, str) or not 2 <= len(name.strip()) <= 80 or _text(name, 320) != name.strip()
            or not isinstance(country, str) or country and not re.fullmatch(r"[A-Z]{2}", country)):
        raise CatalogError("Enter 2–80 characters and an optional two-letter country code")
    try:
        async with asyncio.timeout(DEADLINE):
            result, _ = await _query("/json/stations/search?" + urlencode({
                "name": name.strip(), "countrycode": country, "limit": MAX_RESULTS,
                "hidebroken": "true", "order": "clickcount", "reverse": "true"}))
            return result
    except (TimeoutError, ClientError, OSError, ValueError) as exc:
        if isinstance(exc, CatalogError):
            raise
        raise CatalogError("Radio directory is unavailable. Try again later.") from None


async def resolve_station(station_id: str, *, count_click: bool = False) -> CatalogStation:
    station_reference(station_id)
    try:
        async with asyncio.timeout(DEADLINE):
            stations, mirror = await _query("/json/stations/byuuid/" + station_id)
            station = next((item for item in stations if item.uuid == station_id), None)
            if station is None:
                raise CatalogError("Radio station has no supported direct MP3/AAC/Ogg stream")
            await resolve_proxy_target(station.url)

    except (TimeoutError, ClientError, OSError, ValueError) as exc:
        if isinstance(exc, CatalogError):
            raise
        raise CatalogError("Radio station is unavailable") from None

    # Only an explicit initial selection asks for a popularity click. Search,
    # saving, and automatic station recovery never increment it. A slow or bad
    # acknowledgement cannot invalidate an already resolved playable station.
    if count_click:
        try:
            async with asyncio.timeout(1):
                await _fetch(f"https://{mirror}/json/url/{station_id}")
        except (ValueError, ClientError, OSError, TimeoutError):
            pass
    return station
