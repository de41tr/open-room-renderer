"""One leased, read-only event observation of the existing owned Spotify child.

Raw upstream data exists only in bounded local memory. Only fixed Reason members
reach the existing schema-1 recorder; no logs, metadata or exception text escape.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import socket
import struct

from .diagnostics import Reason, safe
from .spotify_diagnostic import MAX_BODY_BYTES, _json_object
from .spotify_playback import _owned_connection

MAX_WIRE_BYTES = 1024 * 1024
MAX_FRAMES = 1000
# Pinned 4dbc099f: daemon/controls.go fetchTrack -> player.NewStream.
# SpotifyId.String is its canonical URI. Match the WHOLE wrapper, never search
# arbitrary raw text for a phrase or retain the identifier in the result.
_LOAD_ERROR = re.compile(
    r"failed creating stream for spotify:(?:track|episode):[A-Za-z0-9]{22}: "
    r"(?P<error>media is restricted|failed retrieving audio key: "
    r"failed retrieving aes key with code (?P<code>0|[1-9][0-9]{0,4}))")


def playback_error_reason(data: object) -> Reason:
    if (type(data) is not dict or data.get("stage") != "load"
            or data.get("kind") != "restricted" or data.get("unplayable") is not True):
        return Reason.SPOTIFY_ERROR_OTHER
    message = data.get("message")
    match = _LOAD_ERROR.fullmatch(message) if type(message) is str and len(message) <= 256 else None
    if match is None:
        return Reason.SPOTIFY_RESTRICTED_UNKNOWN
    if match["error"] == "media is restricted":
        return Reason.SPOTIFY_MEDIA_RESTRICTED
    code = int(match["code"])
    if code > 65535:  # audio.KeyProviderError.Code is uint16, not arbitrary text.
        return Reason.SPOTIFY_RESTRICTED_UNKNOWN
    return Reason.SPOTIFY_KEY_REFUSED_1 if code == 1 else Reason.SPOTIFY_KEY_REFUSED_OTHER


class _End(Exception):
    def __init__(self, reason):
        self.reason = reason


async def _connect(port, pid, validate):
    def connect():
        # Reuse the exact reverse TCP tuple + child socket-inode ownership proof
        # used by existing controls. No HTTP bytes precede that proof.
        with _owned_connection("127.0.0.1", port, pid) as connection:
            validate()
            sock = socket.fromfd(connection.sock.fileno(), socket.AF_INET, socket.SOCK_STREAM)
            sock.setblocking(False)
            return sock

    pending = asyncio.create_task(asyncio.to_thread(connect))
    try:
        sock = await asyncio.shield(pending)
    except asyncio.CancelledError:
        # Cancellation of to_thread cannot cancel its bounded connect operation.
        # Close a late result instead of leaking or adopting the connection.
        def discard(task):
            try:
                task.result().close()
            except (Exception, asyncio.CancelledError):
                pass
        pending.add_done_callback(discard)
        raise
    try:
        return await asyncio.open_connection(sock=sock, limit=8192)
    except BaseException:
        sock.close()
        raise


class _Events:
    """Minimal RFC6455 receive path: no compression, redirects or application POSTs."""
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.size = self.frames = 0

    async def read(self, size):
        self.size += size
        if self.size > MAX_WIRE_BYTES:
            raise _End(Reason.SPOTIFY_EVENTS_LIMIT)
        return await self.reader.readexactly(size)

    async def handshake(self, port):
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        self.writer.write((f"GET /events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Key: {key}\r\n\r\n").encode("ascii"))
        await self.writer.drain()
        raw = await self.reader.readuntil(b"\r\n\r\n")
        if len(raw) > 8192:
            raise ValueError
        headers = {}
        lines = raw.decode("ascii").split("\r\n")
        for line in lines[1:-2]:
            name, value = line.split(":", 1)
            name = name.lower()
            if name in headers:
                raise ValueError
            headers[name] = value.strip()
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if (lines[0] != "HTTP/1.1 101 Switching Protocols"
                or headers.get("upgrade", "").lower() != "websocket"
                or "upgrade" not in [v.strip().lower() for v in headers.get("connection", "").split(",")]
                or headers.get("sec-websocket-accept") != accept
                or "sec-websocket-extensions" in headers or "sec-websocket-protocol" in headers):
            raise ValueError

    async def message(self):
        body = bytearray()
        opened = False
        while True:
            if self.frames >= MAX_FRAMES:
                raise _End(Reason.SPOTIFY_EVENTS_LIMIT)
            self.frames += 1
            a, b = await self.read(2)
            final, op = bool(a & 128), a & 15
            if a & 112 or b & 128:
                raise ValueError
            size = b & 127
            if size == 126:
                size = struct.unpack("!H", await self.read(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", await self.read(8))[0]
            if size > MAX_BODY_BYTES or len(body) + size > MAX_BODY_BYTES:
                raise ValueError
            payload = await self.read(size)
            if op in (8, 9, 10):
                if not final or size > 125:
                    raise ValueError
                if op == 8:
                    raise _End(Reason.SPOTIFY_EVENTS_DISCONNECTED)
                if op == 9:
                    mask = os.urandom(4)  # Protocol pong only; no playback API.
                    self.writer.write(bytes([138, 128 | size]) + mask +
                        bytes(c ^ mask[i % 4] for i, c in enumerate(payload)))
                    await self.writer.drain()
                continue
            if op == 1 and not opened:
                opened = True
            elif op != 0 or not opened:
                raise ValueError
            body.extend(payload)
            if final:
                return _json_object(bytes(body))


async def watch(source, recorder, test_id):
    """At most one connection per source/lease. Failure never changes playback."""
    writer = None
    child = source.process

    def current():
        source._validate_child()
        if (source.process is not child or source.api.spotify_source is not source
                or not recorder.active() or recorder.window["test_id"] != test_id):
            raise ValueError

    def emit(reason):
        current()
        safe(source.api, "background", reason, test_id)

    try:
        current()
        async with asyncio.timeout(max(0, recorder.deadline - asyncio.get_running_loop().time())):
            reader, writer = await _connect(source.profile["port"], str(child.pid), source._validate_child)
            current()
            events = _Events(reader, writer)
            async with asyncio.timeout(4):
                await events.handshake(source.profile["port"])
            emit(Reason.SPOTIFY_EVENTS_CONNECTED)
            while True:
                event = await events.message()
                current()
                if event.get("type") == "playback_error":
                    emit(playback_error_reason(event.get("data")))
                # Do not retain a raw event while waiting for the next one.
                del event
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        reason = exc.reason if isinstance(exc, _End) else Reason.SPOTIFY_EVENTS_UNAVAILABLE
        try:
            emit(reason)
        except Exception:
            pass
    finally:
        if writer is not None:
            try:
                writer.close()
                async with asyncio.timeout(.25):
                    await writer.wait_closed()
            except Exception:
                pass
