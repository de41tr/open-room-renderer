"""mpv JSON IPC backend and audio DSP filter generation.

The renderer delegates actual playback to mpv. This wrapper owns process
startup, Unix-socket JSON IPC, and construction of the lavfi filter graph used
for balance and PEQ.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import errno
import re
from dataclasses import dataclass
from typing import Any

LOG = logging.getLogger(__name__)


def redact_sensitive(value: object) -> str:
    """Redact tokens and private network addresses before logging mpv URLs."""

    text = str(value)
    patterns = [
        (r"(?i)(X-Plex-Token=)[^&\s\"'<>]+", r"\1***"),
        (r"(?i)(access_token=)[^&\s\"'<>]+", r"\1***"),
        (r"(?i)(token=)[^&\s\"'<>]+", r"\1***"),
        (r"\b(?:10|127)\.(?:\d{1,3}\.){2}\d{1,3}\b", "[private-ip]"),
        (r"\b192\.168\.\d{1,3}\.\d{1,3}\b", "[private-ip]"),
        (r"\b172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}\b", "[private-ip]"),
    ]
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text


class BackendError(RuntimeError):
    """The backend did not acknowledge a requested operation."""


@dataclass
class MpvBackend:
    """Async mpv JSON IPC wrapper.

    The backend owns optional mpv process startup and sends one-shot JSON IPC
    commands over a Unix socket. Audio DSP settings are kept locally and
    reapplied as a single named lavfi filter graph.
    """

    socket_path: str
    auto_spawn: bool = True
    process: asyncio.subprocess.Process | None = None
    _request_id: int = 0
    _channel_balance: float = 0.0
    _peq_bands: list[dict[str, float]] | None = None

    async def start(self) -> None:
        """Ensure the mpv IPC socket exists, spawning mpv when configured.

        Side Effects:
            May remove a stale socket and start an ``mpv`` subprocess.
        """

        if os.path.exists(self.socket_path) and await self._socket_accepts_connections():
            return
        if os.path.exists(self.socket_path):
            LOG.warning("removing stale mpv IPC socket: %s", self.socket_path)
            try:
                os.unlink(self.socket_path)
            except OSError as exc:
                LOG.warning("could not remove stale mpv socket: %s", exc)
        if not self.auto_spawn:
            LOG.warning("mpv IPC socket missing and auto_spawn_mpv=false: %s", self.socket_path)
            return
        try:
            self.process = await asyncio.create_subprocess_exec(
                "mpv",
                "--no-video",
                "--idle=yes",
                f"--input-ipc-server={self.socket_path}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            LOG.error("mpv is not installed or not in PATH")
            return
        for _ in range(50):
            if os.path.exists(self.socket_path):
                LOG.info("mpv IPC ready at %s", self.socket_path)
                return
            await asyncio.sleep(0.1)
        LOG.warning("mpv did not create IPC socket within timeout")

    async def _socket_accepts_connections(self) -> bool:
        """Return whether an existing socket is backed by a live mpv process."""

        try:
            reader, writer = await asyncio.open_unix_connection(self.socket_path)
            writer.close()
            await writer.wait_closed()
            return True
        except OSError:
            return False

    async def stop_process(self) -> None:
        """Terminate the auto-spawned mpv process if this backend started one."""

        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except asyncio.TimeoutError:
                self.process.kill()

    async def command(self, args: list[Any], timeout: float = 5.0) -> dict[str, Any]:
        """Send an mpv command and wait for its matching response.

        Args:
            args: mpv JSON IPC command array.
            timeout: Maximum seconds to wait for the matching request ID.

        Returns:
            mpv response object, or a small ``{"error": ...}`` dictionary.
        """

        if not os.path.exists(self.socket_path):
            await self.start()
        if not os.path.exists(self.socket_path):
            return {"error": "mpv IPC socket unavailable"}
        self._request_id += 1
        request_id = self._request_id
        payload = {"command": args, "request_id": request_id}
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.open_unix_connection(self.socket_path)
            writer.write((json.dumps(payload) + "\n").encode())
            await writer.drain()

            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = max(0.0, deadline - asyncio.get_running_loop().time())
                if remaining <= 0:
                    raise asyncio.TimeoutError(f"timed out waiting for mpv response id={request_id}")
                line = await asyncio.wait_for(reader.readline(), remaining)
                if not line:
                    return {"error": "mpv IPC connection closed"}
                response = json.loads(line.decode() or "{}")
                if response.get("request_id") == request_id:
                    return response
                LOG.debug("ignoring mpv IPC event while waiting for id=%s: %s", request_id, response)
        except Exception as exc:
            LOG.warning("mpv IPC command failed %s: %s", args, exc)
            if isinstance(exc, OSError) and exc.errno in {errno.ECONNREFUSED, errno.ENOENT}:
                try:
                    os.unlink(self.socket_path)
                except OSError:
                    pass
                await self.start()
            return {"error": str(exc)}
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    async def command_no_wait(self, args: list[Any]) -> dict[str, Any]:
        """Send an mpv command without waiting for the response body."""

        if not os.path.exists(self.socket_path):
            await self.start()
        if not os.path.exists(self.socket_path):
            return {"error": "mpv IPC socket unavailable"}
        self._request_id += 1
        payload = {"command": args, "request_id": self._request_id}
        writer: asyncio.StreamWriter | None = None
        try:
            _, writer = await asyncio.open_unix_connection(self.socket_path)
            writer.write((json.dumps(payload) + "\n").encode())
            await writer.drain()
            return {"error": "success"}
        except Exception as exc:
            LOG.warning("mpv IPC fire-and-forget command failed %s: %s", args, exc)
            if isinstance(exc, OSError) and exc.errno in {errno.ECONNREFUSED, errno.ENOENT}:
                try:
                    os.unlink(self.socket_path)
                except OSError:
                    pass
                await self.start()
            return {"error": str(exc)}
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    async def loadfile(self, url: str, options: dict[str, str] | None = None) -> dict[str, Any]:
        """Load ``url`` into mpv and force playback out of pause state."""

        LOG.info("mpv loadfile replace url=%s options=%s", redact_sensitive(url), options or {})
        command: list[Any] = ["loadfile", url, "replace"]
        if options:
            command.extend([-1, options])
        response = await self.command(command, timeout=3.0)
        if response.get("error") not in {None, "success"}:
            LOG.warning("mpv loadfile returned error url=%s response=%s", redact_sensitive(url), response)
            return response
        pause_response = await self.command(["set_property", "pause", False], timeout=3.0)
        if pause_response.get("error") not in {None, "success"}:
            LOG.warning("mpv unpause after loadfile returned error url=%s response=%s", redact_sensitive(url), pause_response)
            return pause_response
        return response

    async def checked_command(self, args: list[Any]) -> dict[str, Any]:
        """Wait for an acknowledgement; never equate socket write with success."""
        response = await self.command(args, timeout=2.0)
        if response.get("error") != "success":
            raise BackendError("Backend operation failed")
        return response

    async def pause(self) -> None:
        """Pause playback."""

        await self.checked_command(["set_property", "pause", True])

    async def resume(self) -> None:
        """Resume playback."""

        await self.checked_command(["set_property", "pause", False])

    async def stop(self) -> None:
        """Stop the current mpv item."""

        await self.checked_command(["stop"])

    async def seek(self, seconds: int) -> None:
        """Seek to an absolute position in seconds."""

        await self.checked_command(["seek", seconds, "absolute"])

    async def volume(self, value: int) -> None:
        """Set mpv volume."""

        await self.checked_command(["set_property", "volume", value])

    async def mute(self, value: bool) -> None:
        """Set mpv mute state."""

        await self.checked_command(["set_property", "mute", value])

    async def channel_balance(self, value: float) -> None:
        """Store stereo balance and reapply the DSP filter graph."""

        self._channel_balance = max(-1.0, min(1.0, value))
        await self.apply_audio_filters()

    async def peq(self, bands: list[dict[str, float]]) -> None:
        """Store parametric EQ bands and reapply the DSP filter graph."""

        self._peq_bands = bands
        await self.apply_audio_filters()

    async def apply_audio_filters(self) -> None:
        """Replace the named mpv audio filter with current balance/PEQ graph."""

        filters = self._audio_filter_graph()
        LOG.info("mpv audio filter graph: %s", filters or "(none)")
        remove_response = await self.command(["af", "remove", "@fake_linkplay_dsp"], timeout=2.0)
        if remove_response.get("error") not in {None, "success"}:
            LOG.debug("mpv audio filter remove returned response=%s", remove_response)
        if filters:
            add_response = await self.command(["af", "add", f"@fake_linkplay_dsp:lavfi=[{filters}]"], timeout=2.0)
            if add_response.get("error") not in {None, "success"}:
                LOG.warning("mpv audio filter add returned response=%s graph=%s", add_response, filters)

    def _audio_filter_graph(self) -> str:
        """Build the lavfi graph string for active PEQ and balance settings."""

        filters: list[str] = []
        for band in self._peq_bands or []:
            gain = float(band.get("gain", 0.0))
            freq = max(10.0, float(band.get("freq", 1000.0)))
            q = max(0.1, float(band.get("q", 1.0)))
            mode = int(float(band.get("mode", 1.0)))
            if mode == 1 and abs(gain) >= 0.01:
                filters.append(f"equalizer=f={freq:.3f}:width_type=q:width={q:.3f}:g={gain:.3f}")
            elif mode == 0 and abs(gain) >= 0.01:
                filters.append(f"bass=f={freq:.3f}:width_type=q:width={q:.3f}:g={gain:.3f}")
            elif mode == 2 and abs(gain) >= 0.01:
                filters.append(f"treble=f={freq:.3f}:width_type=q:width={q:.3f}:g={gain:.3f}")
            elif mode == 3:
                filters.append(f"lowpass=f={freq:.3f}:width_type=q:width={q:.3f}")
            elif mode == 5:
                filters.append(f"highpass=f={freq:.3f}:width_type=q:width={q:.3f}")
        if abs(self._channel_balance) >= 0.001:
            left = 1.0 - max(0.0, self._channel_balance)
            right = 1.0 + min(0.0, self._channel_balance)
            filters.append(f"pan=stereo|c0={left:.3f}*c0|c1={right:.3f}*c1")
        return ",".join(filters)

    async def get_property(self, name: str) -> Any:
        """Read a single mpv property, returning ``None`` on mpv errors."""

        response = await self.command(["get_property", name], timeout=0.5)
        return response.get("data")

    async def snapshot(self) -> dict[str, Any]:
        """Read the small set of mpv properties used for renderer state sync."""

        # Failure of a required control property is different from unavailable
        # media metadata while idle. Preserve that distinction for the owner.
        idle = await self.command(["get_property", "idle-active"], timeout=0.5)
        if idle.get("error") != "success":
            return {"backend-error": True}
        pause = await self.command(["get_property", "pause"], timeout=0.5)
        if pause.get("error") != "success":
            return {"backend-error": True}
        data: dict[str, Any] = {"idle-active": idle.get("data"), "pause": pause.get("data")}
        for key in (
            "playback-time",
            "duration",
            "media-title",
            "filename",
            "path",
            "stream-open-filename",
            "core-idle",
            "eof-reached",
            "paused-for-cache",
            "cache-buffering-state",
            "demuxer-cache-state",
            "track-list",
        ):
            data[key] = await self.get_property(key)
        return data
