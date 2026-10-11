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
import stat
import socket
import struct
import re
from dataclasses import dataclass, field
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

    # All callers share startup, including callers with shorter deadlines. A
    # cancelled HTTP request must not abandon a child being started for others.
    _startup_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _shutdown_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _reap_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _owned_process: asyncio.subprocess.Process | None = field(default=None, init=False, repr=False)
    _owned_socket_identity: tuple[int, int] | None = field(default=None, init=False, repr=False)
    _spawn_attempted: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _startup_error: str = field(default="mpv did not become ready", init=False, repr=False)
    _startup_timeout: float = field(default=5.0, init=False, repr=False)
    _shutdown_timeout: float = field(default=3.0, init=False, repr=False)

    @staticmethod
    def _observe_task(task: asyncio.Task[None]) -> None:
        """Retrieve background failures even if every request timed out."""
        if not task.cancelled():
            task.exception()

    async def start(self, timeout: float = 5.0) -> None:
        """Connect to mpv or make one owned spawn attempt, within ``timeout``.

        Foreign paths are never removed and failed attempts never respawn on
        each status poll. Restart the renderer to retry spawning; an external socket
        that becomes available can still be used without restarting.
        """
        if self._closed:
            raise BackendError("mpv backend is shut down")
        if timeout <= 0:
            raise BackendError("mpv startup timed out")
        # No await between observing and publishing the task: atomic within the
        # event loop, without holding a lock across subprocess/socket I/O.
        if self._startup_task is None or self._startup_task.done():
            self._startup_task = asyncio.create_task(self._start_once())
            self._startup_task.add_done_callback(self._observe_task)
        try:
            async with asyncio.timeout(timeout):
                await asyncio.shield(self._startup_task)
        except TimeoutError as exc:
            raise BackendError("mpv startup timed out") from exc

    def _socket_identity(self) -> tuple[int, int] | None:
        """Inspect the path without following links or removing foreign files."""
        try:
            info = os.lstat(self.socket_path)
        except FileNotFoundError:
            return None
        if not stat.S_ISSOCK(info.st_mode):
            raise BackendError("mpv IPC path is not a socket (symlinks are not followed)")
        return info.st_dev, info.st_ino

    def _socket_exists(self) -> bool:
        return self._socket_identity() is not None

    async def _start_once(self) -> None:
        ready = False
        try:
            async with asyncio.timeout(self._startup_timeout):
                if self._socket_exists():
                    # A refused preexisting socket is not evidence of ownership.
                    # In particular do not launch mpv, which could replace it.
                    await self._probe_socket()
                    ready = True
                    return
                if not self.auto_spawn:
                    raise BackendError("mpv IPC socket unavailable; auto-spawn is disabled")
                if self._spawn_attempted:
                    raise BackendError(self._startup_error)
                self._spawn_attempted = True
                self.process = await asyncio.create_subprocess_exec(
                    "mpv",
                    "--no-video",
                    "--idle=yes",
                    f"--input-ipc-server={self.socket_path}",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                self._owned_process = self.process
                while True:
                    if self.process.returncode is not None:
                        raise BackendError(f"mpv exited during startup (code {self.process.returncode})")
                    try:
                        if self._socket_exists():
                            identity = self._socket_identity()
                            peer_pid = await self._probe_socket(owned_identity=identity)
                            # A path appearing after spawn alone does not prove
                            # ownership: another server may have raced startup.
                            # Linux SO_PEERCRED binds it to our actual child.
                            if (identity is not None and peer_pid is not None
                                    and peer_pid == self.process.pid
                                    and self._socket_identity() == identity):
                                self._owned_socket_identity = identity
                            ready = True
                            LOG.info("mpv IPC ready at %s", self.socket_path)
                            return
                    except (FileNotFoundError, ConnectionRefusedError):
                        pass
                    await asyncio.sleep(0.05)
        except TimeoutError as exc:
            self._startup_error = "mpv startup timed out waiting for IPC"
            raise BackendError(self._startup_error) from exc
        except FileNotFoundError as exc:
            self._startup_error = "mpv executable not found" if self._spawn_attempted and self.process is None else "mpv IPC socket unavailable"
            raise BackendError(self._startup_error) from exc
        except OSError as exc:
            self._startup_error = f"mpv startup failed: {redact_sensitive(exc)}"
            raise BackendError(self._startup_error) from exc
        except BackendError as exc:
            self._startup_error = str(exc)
            raise
        finally:
            if not ready and self._owned_process is not None:
                await self._reap_owned_process()

    async def _probe_socket(self, *, owned_identity: tuple[int, int] | None = None) -> int | None:
        """Check readiness and, on Linux, identify the connected peer process."""
        _, writer = await asyncio.open_unix_connection(self.socket_path)
        try:
            ipc_socket = writer.get_extra_info("socket")
            credentials = ipc_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            peer_pid = struct.unpack("3i", credentials)[0]
            # Record verified ownership before awaiting transport close, so a
            # shutdown signal during that await can still clean our socket.
            if (owned_identity is not None and self._owned_process is not None
                    and peer_pid == self._owned_process.pid
                    and self._socket_identity() == owned_identity):
                self._owned_socket_identity = owned_identity
            return peer_pid
        except (AttributeError, OSError, TypeError, struct.error):
            # Without trustworthy peer credentials, connection is usable but
            # the path is never ours to remove automatically.
            return None
        finally:
            await self._close_writer(writer, asyncio.get_running_loop().time() + 0.1)

    @staticmethod
    async def _close_writer(writer: asyncio.StreamWriter, deadline: float) -> None:
        """Close promptly, aborting a transport that cannot finish by deadline."""
        writer.close()
        remaining = deadline - asyncio.get_running_loop().time()
        try:
            if remaining <= 0:
                return
            await asyncio.wait_for(writer.wait_closed(), min(remaining, 0.1))
        except (OSError, TimeoutError):
            pass
        finally:
            # close() already sends buffered bytes. abort() prevents a broken
            # peer from retaining a transport after bounded wait_closed().
            writer.transport.abort()

    async def _reap_owned_process(self) -> None:
        process = self._owned_process
        if process is None:
            return
        if self._reap_task is None:
            self._reap_task = asyncio.create_task(self._terminate_and_wait(process))
            self._reap_task.add_done_callback(self._observe_task)
        await asyncio.shield(self._reap_task)

    async def _terminate_and_wait(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), self._shutdown_timeout)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), self._shutdown_timeout)
            except TimeoutError as exc:
                raise BackendError("mpv did not exit after kill") from exc
        # mpv closes its listener without unlinking its pathname on exit. Only
        # remove the peer-verified socket, after reap and if its identity is
        # unchanged. Preserve regular files, links and replacement sockets.
        if self._owned_socket_identity is not None:
            try:
                if self._socket_identity() == self._owned_socket_identity:
                    os.unlink(self.socket_path)
            except (OSError, BackendError) as exc:
                LOG.warning("could not clean up owned mpv IPC socket: %s", redact_sensitive(exc))
            self._owned_socket_identity = None
        # Keep the process handle on wait failure instead of claiming a reap.
        self._owned_process = None
        if self.process is process:
            self.process = None

    async def stop_process(self) -> None:
        """Reap only our child, escalating to kill and awaiting its exit.

        No IPC quit is sent: an externally managed mpv is never terminated.
        Shutdown is idempotent and prevents subsequent request-driven spawning.
        """
        self._closed = True
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._shutdown())
            self._shutdown_task.add_done_callback(self._observe_task)
        try:
            await asyncio.shield(self._shutdown_task)
        except asyncio.CancelledError:
            await asyncio.shield(self._shutdown_task)
            raise

    async def _shutdown(self) -> None:
        if self._startup_task is not None:
            if not self._startup_task.done():
                self._startup_task.cancel()
            await asyncio.gather(self._startup_task, return_exceptions=True)
        await self._reap_owned_process()

    async def command(self, args: list[Any], timeout: float = 5.0) -> dict[str, Any]:
        """Send one JSON IPC command with an end-to-end deadline.

        Startup, connect, drain, event handling, response and close all consume
        the same budget. Errors remain explicit dictionaries for existing API
        callers; checked control wrappers turn them into ``BackendError``.
        """
        return await self._send_command(args, timeout, wait_response=True)

    async def command_no_wait(self, args: list[Any], timeout: float = 5.0) -> dict[str, Any]:
        """Bound connection/write time; success here means sent, not acknowledged."""
        return await self._send_command(args, timeout, wait_response=False)

    async def _send_command(self, args: list[Any], timeout: float, *, wait_response: bool) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout_at(deadline):
                await self.start(timeout=max(0.0, deadline - asyncio.get_running_loop().time()))
                if self._closed:
                    raise BackendError("mpv backend is shut down")
                reader, writer = await asyncio.open_unix_connection(self.socket_path)
                if self._closed:
                    raise BackendError("mpv backend is shut down")
                self._request_id += 1
                request_id = self._request_id
                payload = {"command": args, "request_id": request_id}
                writer.write((json.dumps(payload) + "\n").encode())
                await writer.drain()
                if not wait_response:
                    return {"error": "success"}
                while True:
                    line = await reader.readline()
                    if not line:
                        raise BackendError("mpv IPC connection closed before acknowledgement")
                    try:
                        response = json.loads(line.decode())
                    except (UnicodeDecodeError, ValueError) as exc:
                        raise BackendError("mpv IPC returned invalid JSON") from exc
                    if not isinstance(response, dict):
                        raise BackendError("mpv IPC returned a non-object response")
                    if response.get("request_id") == request_id:
                        if not isinstance(response.get("error"), str):
                            raise BackendError("mpv IPC response is missing an error status")
                        return response
                    LOG.debug("ignoring mpv IPC event while waiting for id=%s", request_id)
        except TimeoutError:
            return {"error": "mpv IPC command timed out"}
        except (BackendError, OSError, ValueError) as exc:
            error = redact_sensitive(exc) or type(exc).__name__
            LOG.warning("mpv IPC command %s failed: %s", args[0] if args else "(empty)", error)
            return {"error": error}
        finally:
            if writer is not None:
                await self._close_writer(writer, deadline)

    async def loadfile(self, url: str, options: dict[str, str] | None = None) -> dict[str, Any]:
        """Load ``url`` into mpv and force playback out of pause state."""

        LOG.info("mpv loadfile replace url=%s options=%s", redact_sensitive(url), options or {})
        command: list[Any] = ["loadfile", url, "replace"]
        if options:
            command.extend([-1, options])
        response = await self.command(command, timeout=3.0)
        if response.get("error") != "success":
            LOG.warning("mpv loadfile returned error url=%s response=%s", redact_sensitive(url), response)
            return {**response, "error": response.get("error") or "mpv loadfile acknowledgement missing"}
        pause_response = await self.command(["set_property", "pause", False], timeout=3.0)
        if pause_response.get("error") != "success":
            LOG.warning("mpv unpause after loadfile returned error url=%s response=%s", redact_sensitive(url), pause_response)
            return {**pause_response, "error": pause_response.get("error") or "mpv unpause acknowledgement missing"}
        return response

    async def checked_command(self, args: list[Any]) -> dict[str, Any]:
        """Wait for an acknowledgement; never equate socket write with success."""
        response = await self.command(args, timeout=2.0)
        if response.get("error") != "success":
            raise BackendError(f"Backend operation failed: {redact_sensitive(response.get('error') or 'missing acknowledgement')}")
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
