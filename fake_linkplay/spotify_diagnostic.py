"""Opt-in, read-only observations of an independently installed go-librespot.

Only numeric loopback HTTP endpoints are accepted. This module never starts a
player, authenticates, sends playback commands, or shares renderer/mpv state.
The v0.10.3 status projection is a sidecar claim, not audible-output evidence.
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import math
import sys

import aiohttp

REQUEST_TIMEOUT = 4.0
CONNECT_TIMEOUT = 2.0
READ_TIMEOUT = 2.0
CLOSE_TIMEOUT = 0.25
MAX_WATCH_SECONDS = 30.0
MAX_BODY_BYTES = 65536
MAX_JSON_DEPTH = 32
MAX_EVENT_MESSAGES = 32
_BOOLEAN_FIELDS = ("stopped", "paused", "buffering", "repeat_context", "repeat_track", "shuffle_context")
_EVENT_TYPES = frozenset({"playing", "not_playing", "will_play", "paused", "active", "inactive",
    "metadata", "volume", "seek", "stopped", "repeat_track", "repeat_context", "shuffle_context",
    "playback_ready", "playback_error"})
_INVALID_INPUT = "Invalid Spotify diagnostic arguments. Use numeric loopback, port 1..65535 and watch seconds 0..30."
_REQUEST_FAILED = "Spotify diagnostic failed: local sidecar request was not confirmed."
_CONTRACT_FAILED = "Spotify diagnostic failed: unsupported or malformed sidecar response."
_REDIRECT_FAILED = "Spotify diagnostic failed: sidecar redirects are not permitted."
_TIMEOUT = "Spotify diagnostic failed: observation deadline exceeded."


class SpotifyDiagnosticError(ValueError):
    """Fixed errors that never contain endpoint, account, metadata or raw errors."""


def _endpoint(host: str, port: int) -> str:
    try:
        if not isinstance(host, str) or len(host) > 45 or "%" in host:
            raise ValueError
        address = ipaddress.ip_address(host)
        if (not address.is_loopback or str(address) != host
                or (address.version == 6 and address.ipv4_mapped is not None)):
            raise ValueError
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError
        return f"http://{'[' + host + ']' if address.version == 6 else host}:{port}"
    except (TypeError, ValueError):
        raise SpotifyDiagnosticError(_INVALID_INPUT) from None


def _watch_seconds(value: object) -> float:
    try:
        if isinstance(value, bool) or len(str(value)) > 32:
            raise ValueError
        result = float(value)
        if not math.isfinite(result) or not 0 <= result <= MAX_WATCH_SECONDS:
            raise ValueError
        return result
    except (TypeError, ValueError, OverflowError):
        raise SpotifyDiagnosticError(_INVALID_INPUT) from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ValueError


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError
    return result


def _json_object(encoded: bytes) -> dict[str, object]:
    """Bound container depth before decoding, including ignored private fields."""
    try:
        if len(encoded) > MAX_BODY_BYTES:
            raise ValueError
        depth = 0
        quoted = escaped = False
        for byte in encoded:
            if quoted:
                if escaped:
                    escaped = False
                elif byte == 92:
                    escaped = True
                elif byte == 34:
                    quoted = False
            elif byte == 34:
                quoted = True
            elif byte in (91, 123):
                depth += 1
                if depth > MAX_JSON_DEPTH:
                    raise ValueError
            elif byte in (93, 125):
                depth -= 1
        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_invalid_constant, parse_float=_finite_float)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise SpotifyDiagnosticError(_CONTRACT_FAILED) from None


def _project_status(value: dict[str, object]) -> dict[str, object]:
    """Retain only checked primitive fields; never return account or track data."""
    result: dict[str, object] = {}
    for field in _BOOLEAN_FIELDS:
        if type(value.get(field)) is not bool:
            raise SpotifyDiagnosticError(_CONTRACT_FAILED)
        result[field] = value[field]
    for field in ("volume", "volume_steps"):
        number = value.get(field)
        if type(number) is not int or not 0 <= number <= 0xffffffff:
            raise SpotifyDiagnosticError(_CONTRACT_FAILED)
        result[field] = number
    steps = result["volume_steps"]
    result["volume_fraction"] = result["volume"] / steps if steps > 0 else None
    return result


async def _reject_redirect(_session, _context, params) -> None:
    # ws_connect has no allow_redirects option. The public redirect trace fires
    # before aiohttp follows Location, including during its WebSocket handshake.
    params.response.close()
    raise SpotifyDiagnosticError(_REDIRECT_FAILED)


class SpotifyDiagnosticClient:
    """One bounded observation; no persistent connection, cookies or credentials."""

    def __init__(self, host: str, port: int) -> None:
        self.endpoint = _endpoint(host, port)

    async def _get(self, session: aiohttp.ClientSession, path: str) -> dict[str, object] | None:
        if path not in {"/", "/status"}:
            raise SpotifyDiagnosticError(_REQUEST_FAILED)
        async with session.get(self.endpoint + path, allow_redirects=False) as response:
            if 300 <= response.status < 400:
                raise SpotifyDiagnosticError(_REDIRECT_FAILED)
            if response.status not in ({200, 204} if path == "/status" else {200}):
                raise SpotifyDiagnosticError(_REQUEST_FAILED)
            encodings = response.headers.getall("Content-Encoding", [])
            if encodings and (len(encodings) != 1 or encodings[0].lower() != "identity"):
                raise SpotifyDiagnosticError(_CONTRACT_FAILED)
            if response.content_length is not None and response.content_length > MAX_BODY_BYTES:
                raise SpotifyDiagnosticError(_CONTRACT_FAILED)
            encoded = bytearray()
            async for chunk in response.content.iter_chunked(4096):
                encoded.extend(chunk)
                if len(encoded) > MAX_BODY_BYTES:
                    raise SpotifyDiagnosticError(_CONTRACT_FAILED)
            if response.status == 204:
                if encoded or response.content_length not in (None, 0):
                    raise SpotifyDiagnosticError(_CONTRACT_FAILED)
                return None
            return _json_object(bytes(encoded))

    async def _status(self, session: aiohttp.ClientSession, result: dict[str, object]) -> None:
        result["snapshot_freshness"] = "unknown"
        status = await self._get(session, "/status")
        result["status"] = None if status is None else _project_status(status)
        result["session_observation"] = "NO_SESSION" if status is None else "SESSION_PRESENT"
        result["snapshot_freshness"] = "snapshot_only"

    async def _watch(self, session: aiohttp.ClientSession, result: dict[str, object], seconds: float) -> None:
        events = result["events"]
        ws = None
        deadline = asyncio.timeout(seconds)
        try:
            async with deadline:
                ws = await session.ws_connect(self.endpoint + "/events", max_msg_size=MAX_BODY_BYTES,
                    autoping=False, heartbeat=None, compress=0)
                events["channel"] = "observed"
                # There is no initial event snapshot or replay guarantee.
                await self._status(session, result)
                events["snapshot_refreshes"] += 1
                for _ in range(MAX_EVENT_MESSAGES):
                    message = await ws.receive()
                    if message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                                         aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR}:
                        events.update(channel="disconnected", end_reason="disconnected")
                        result["snapshot_freshness"] = "unknown"
                        return
                    events["messages_received"] += 1
                    if message.type in {aiohttp.WSMsgType.PING, aiohttp.WSMsgType.PONG}:
                        continue
                    if message.type != aiohttp.WSMsgType.TEXT:
                        raise SpotifyDiagnosticError(_CONTRACT_FAILED)
                    event = _json_object(message.data.encode("utf-8"))
                    if not isinstance(event.get("type"), str) or "data" not in event:
                        raise SpotifyDiagnosticError(_CONTRACT_FAILED)
                    if event["type"] not in _EVENT_TYPES:
                        events["unknown_types"] += 1
                    # Known and unknown events are hints only. Never interpret,
                    # print or retain event data, even playback_error messages.
                    await self._status(session, result)
                    events["snapshot_refreshes"] += 1
                events["end_reason"] = "message_limit"
        except TimeoutError:
            if not deadline.expired():
                raise
            events["end_reason"] = "duration_limit"
        finally:
            if ws is not None:
                try:
                    async with asyncio.timeout(CLOSE_TIMEOUT):
                        await ws.close()
                except (TimeoutError, aiohttp.ClientError):
                    pass
        # Refresh even during a quiet/lossy stream. This remains one observation,
        # not proof of continuous freshness or a complete event history.
        await self._status(session, result)
        events["snapshot_refreshes"] += 1

    async def diagnose(self, watch_seconds: float = 0) -> dict[str, object]:
        seconds = _watch_seconds(watch_seconds)
        trace = aiohttp.TraceConfig()
        trace.on_request_redirect.append(_reject_redirect)
        result: dict[str, object] = {
            "provider": "go-librespot", "observation": "sidecar_reported",
            "api_reachable": True, "expected_contract": "go-librespot_v0.10.3_status_projection",
            "bootstrap_ready": False, "session_observation": "UNKNOWN",
            "snapshot_freshness": "unknown", "status": None,
            "audio_output": "unverified", "shared_dsp": "unverified",
            "events": {"channel": "not_requested" if not seconds else "unconfirmed",
                "end_reason": "not_requested", "messages_received": 0,
                "unknown_types": 0, "snapshot_refreshes": 0, "complete_history": False},
        }
        try:
            # Force a fresh connection for each HTTP read. No pooled-connection
            # reuse, ambient proxy/netrc, Set-Cookie replay or decompression.
            # aiohttp may recover an idempotent GET once after a disconnect;
            # there is no application retry/reconnect loop.
            async with asyncio.timeout(3 * REQUEST_TIMEOUT + seconds + CLOSE_TIMEOUT + 0.5):
                async with aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar(),
                        auto_decompress=False, headers={"Accept-Encoding": "identity"},
                        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT,
                            sock_read=READ_TIMEOUT),
                        connector=aiohttp.TCPConnector(force_close=True, use_dns_cache=False, limit=2),
                        trace_configs=[trace]) as session:
                    root = await self._get(session, "/")
                    if root is None or type(root.get("playback_ready")) is not bool:
                        raise SpotifyDiagnosticError(_CONTRACT_FAILED)
                    result["bootstrap_ready"] = root["playback_ready"]
                    await self._status(session, result)
                    if seconds:
                        await self._watch(session, result, seconds)
            return result
        except TimeoutError:
            raise SpotifyDiagnosticError(_TIMEOUT) from None
        except (aiohttp.ClientError, OSError):
            raise SpotifyDiagnosticError(_REQUEST_FAILED) from None


def add_spotify_parser(subparsers) -> None:
    parser = subparsers.add_parser("spotify", help="optional local sidecar diagnostics and offline proposals")
    commands = parser.add_subparsers(dest="spotify_cmd", required=True)
    from .spotify_exclusive import add_prepare_parser
    add_prepare_parser(commands)
    diagnose = commands.add_parser("diagnose", help="observe an already running go-librespot sidecar",
        description="Read-only, sidecar-reported observations. No installation, login, playback or shared DSP.")
    diagnose.add_argument("--host", default="127.0.0.1", help="numeric loopback only (default: 127.0.0.1)")
    diagnose.add_argument("--port", type=int, required=True, help="explicit local API port (upstream has no fixed default)")
    diagnose.add_argument("--watch-seconds", default="0", help="bounded event observation, 0..30 seconds (default: 0)")


def run_spotify_diagnostic(args: argparse.Namespace) -> int:
    try:
        client = SpotifyDiagnosticClient(args.host, args.port)
        result = asyncio.run(client.diagnose(_watch_seconds(args.watch_seconds)))
    except SpotifyDiagnosticError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Spotify diagnostic interrupted.", file=sys.stderr)
        return 130
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0
