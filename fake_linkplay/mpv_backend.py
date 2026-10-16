"""mpv JSON IPC backend and audio DSP filter generation.

The renderer delegates actual playback to mpv. This wrapper owns process
startup, Unix-socket JSON IPC, and construction of the lavfi filter graph used
for balance and PEQ.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import stat
import socket
import struct
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .audio_device import validate_audio_device
from .ipc_provenance import SocketProvenance
from .log_safety import redact_media_urls

# A finite history preserves mpv's timed ICY transitions. Zero can prune an
# update before playback observes it. Disable donation so unused forward
# capacity cannot silently enlarge this backward-history budget.
LIVE_STATION_BACK_BYTES = 1024 * 1024
LIVE_STATION_HISTORY_OPTIONS = {
    "demuxer-max-back-bytes": str(LIVE_STATION_BACK_BYTES),
    "demuxer-donate-buffer": "no",
}

LOG = logging.getLogger(__name__)


def redact_sensitive(value: object) -> str:
    """Redact tokens and private network addresses before logging mpv URLs."""

    text = redact_media_urls(value)
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
    _dsp_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    # This verifies mpv's configured af list, not that an idle player's graph
    # has been accepted by a decoder or produced measured audio samples.
    _dsp_configuration_confirmed: bool = field(default=False, init=False, repr=False)
    _dsp_managed: bool = field(default=False, init=False, repr=False)
    initial_volume: int = field(default=50, kw_only=True)
    initial_mute: bool = field(default=False, kw_only=True)
    audio_device: str | None = field(default=None, kw_only=True)

    # Desired gain starts with configuration and advances only after confirmed
    # writes. A failed/cancelled write may have reached mpv, so the next load
    # must restore the last confirmed desired values before it can play.
    _desired_volume: int = field(init=False, repr=False)
    _desired_mute: bool = field(init=False, repr=False)
    _gain_initialized: bool = field(default=False, init=False, repr=False)
    _gain_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    # All callers share startup, including callers with shorter deadlines. A
    # cancelled HTTP request must not abandon a child being started for others.
    _startup_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _shutdown_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _reap_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _owned_process: asyncio.subprocess.Process | None = field(default=None, init=False, repr=False)
    _provenance: SocketProvenance = field(init=False, repr=False)
    _owned_socket_identity: tuple[int, int] | None = field(default=None, init=False, repr=False)
    _spawn_attempted: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _startup_error: str = field(default="mpv did not become ready", init=False, repr=False)
    _startup_timeout: float = field(default=5.0, init=False, repr=False)
    _shutdown_timeout: float = field(default=3.0, init=False, repr=False)

    @property
    def gain_confirmed(self) -> bool:
        """Whether this backend has completed its own gain readback path."""
        return self._gain_initialized

    @property
    def confirmed_dsp(self) -> tuple[float, list[dict[str, float]]] | None:
        """A defensive snapshot only after our complete graph readback."""
        if not self._dsp_configuration_confirmed:
            return None
        return self._channel_balance, self.validate_peq_bands(self._peq_bands or [])


    def stage_initial_dsp(self, balance: float, bands: list[dict[str, float]]) -> None:
        """Stage validated startup intent without advertising backend success.

        Called before listeners or playback. Subsequent loads/resumes must
        reconcile this complete graph even when the initial backend was absent.
        """
        if self._dsp_managed or self._dsp_lock.locked():
            raise BackendError("Initial DSP has already been staged")
        candidate_balance = self.validate_channel_balance(balance)
        candidate_bands = self.validate_peq_bands(bands)
        self._channel_balance, self._peq_bands = candidate_balance, candidate_bands
        self._dsp_managed = True
        self._dsp_configuration_confirmed = False


    def __post_init__(self) -> None:
        self.audio_device = validate_audio_device(self.audio_device, auto_spawn=self.auto_spawn)
        self._provenance = SocketProvenance(self.socket_path)
        self._desired_volume = max(0, min(100, int(self.initial_volume)))
        self._desired_mute = bool(self.initial_mute)

    @staticmethod
    def _observe_task(task: asyncio.Task[None]) -> None:
        """Retrieve background failures even if every request timed out."""
        if not task.cancelled():
            task.exception()

    async def start(self, timeout: float = 5.0) -> None:
        """Connect to mpv or make one owned spawn attempt, within ``timeout``.

        Foreign paths are never removed and failed attempts never respawn on
        each status poll. Restart the renderer to retry spawning; an external socket
        that becomes available can still be used without restarting, unless
        explicit audio-device configuration requires our own verified child.
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

    def _owns_audio_device_socket(self) -> bool:
        """Explicit output configuration is meaningful only on our live child."""
        return (self._owned_process is not None
                and self._owned_process.returncode is None
                and self._owned_socket_identity is not None
                and self._socket_identity() == self._owned_socket_identity)

    def _require_audio_device_peer(self, peer_pid: int | None) -> None:
        if self.audio_device is not None and (
                not self._owns_audio_device_socket()
                or peer_pid is None or peer_pid != self._owned_process.pid):
            raise BackendError(
                "Explicit mpv_audio_device requires an owned, peer-verified mpv socket; "
                "choose an unused private socket and restart the renderer"
            )

    async def _start_once(self) -> None:
        ready = False
        guard = self._provenance.startup_guard(self.auto_spawn and self._owned_process is None)
        guard_entered = False
        try:
            recovery_allowed = guard.__enter__()
            guard_entered = True
            async with asyncio.timeout(self._startup_timeout):
                if recovery_allowed and self.auto_spawn and not self._spawn_attempted:
                    if self._provenance.recover_previous_boot():
                        LOG.info("removed peer-proven owned mpv IPC left by a previous boot")
                if self._socket_exists():
                    # A refused preexisting socket is not evidence of ownership.
                    # In particular do not launch mpv, which could replace it.
                    if self.audio_device is not None and not self._owns_audio_device_socket():
                        self._require_audio_device_peer(None)
                    peer_pid = await self._probe_socket()
                    self._require_audio_device_peer(peer_pid)
                    ready = True
                    return
                if not self.auto_spawn:
                    raise BackendError("mpv IPC socket unavailable; auto-spawn is disabled")
                if self._spawn_attempted:
                    raise BackendError(self._startup_error)
                self._spawn_attempted = True
                # Explicit routing opts into isolation: ambient ao/profiles
                # must not silently override it or turn a failed DAC into null.
                audio_options = [] if self.audio_device is None else [
                    "--no-config", "--load-scripts=no", "--audio-fallback-to-null=no",
                    f"--audio-device={self.audio_device}",
                ]
                self.process = await asyncio.create_subprocess_exec(
                    "mpv",
                    "--no-video",
                    "--idle=yes",
                    f"--input-ipc-server={self.socket_path}",
                    *audio_options,
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
                            self._require_audio_device_peer(peer_pid)
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
            try:
                if not ready and self._owned_process is not None:
                    await self._reap_owned_process()
            finally:
                # Keep the cooperating-start lock until failed children have
                # been reaped, not merely until the first startup exception.
                if guard_entered:
                    guard.__exit__(None, None, None)

    async def _probe_socket(self, *, owned_identity: tuple[int, int] | None = None) -> int | None:
        """Check readiness and, on Linux, identify the connected peer process."""
        snapshot = self._provenance.snapshot() if owned_identity is not None else None
        _, writer = await asyncio.open_unix_connection(self.socket_path)
        try:
            peer_pid = self._peer_pid(writer)
            # Record verified ownership before awaiting transport close, so a
            # shutdown signal during that await can still clean our socket.
            if (owned_identity is not None and self._owned_process is not None
                    and peer_pid == self._owned_process.pid
                    and self._socket_identity() == owned_identity):
                self._owned_socket_identity = owned_identity
                if snapshot is not None and tuple(snapshot[:2]) == owned_identity:
                    self._provenance.remember(snapshot)
            return peer_pid
        except (AttributeError, OSError, TypeError, struct.error):
            return None
        finally:
            await self._close_writer(writer, asyncio.get_running_loop().time() + 0.1)

    @staticmethod
    def _peer_pid(writer: asyncio.StreamWriter) -> int | None:
        """Read Linux peer credentials from this exact connected transport."""
        try:
            ipc_socket = writer.get_extra_info("socket")
            credentials = ipc_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            return struct.unpack("3i", credentials)[0]
        except (AttributeError, OSError, TypeError, struct.error):
            # Without trustworthy peer credentials, connection is usable but
            # the path is never ours to remove automatically.
            return None

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
        self._provenance.forget()
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

    async def command(self, args: list[Any] | dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
        """Send one JSON IPC command with an end-to-end deadline.

        Startup, connect, drain, event handling, response and close all consume
        the same budget. Errors remain explicit dictionaries for existing API
        callers; checked control wrappers turn them into ``BackendError``.
        """
        return await self._send_command(args, timeout, wait_response=True)

    async def command_no_wait(self, args: list[Any] | dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
        """Bound connection/write time; success here means sent, not acknowledged."""
        return await self._send_command(args, timeout, wait_response=False)

    async def diagnose_audio_output(self, timeout: float = 2.0) -> dict[str, Any]:
        """Read two output properties on one connection, without startup or logging.

        This deliberately bypasses start/command and all playback/state helpers.
        Only a fresh no-spawn backend may use it. Cleanup only closes this client
        transport; it sends no shutdown command/signal and removes no socket. Values are sequential
        observations, not an atomic snapshot or proof of renderer ownership.
        """
        result: dict[str, Any] = {
            "connection": "not_attempted", "socket_path_check": "not_checked",
            "audio_device": {"status": "not_read"},
            "current_ao": {"status": "not_read"},
        }
        if (self.auto_spawn is not False or self.process is not None
                or self._owned_process is not None or self._startup_task is not None
                or self._closed):
            raise ValueError("Audio diagnosis requires a fresh no-spawn backend")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Audio diagnosis requires a positive finite timeout")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        writer: asyncio.StreamWriter | None = None
        current: str | None = None
        frames = received = 0

        def unique_object(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError
                value[key] = item
            return value

        def check_deadline():
            # Buffered awaits need not yield to asyncio.timeout_at. Never send
            # a later request or accept a late reply after a synchronous overrun.
            if loop.time() >= deadline:
                raise TimeoutError

        def check_path(identity):
            # An unchanged inode at these instants is only an advisory check.
            # In particular, it cannot certify ownership or rule out an ABA race.
            observed = self._socket_identity()
            check_deadline()
            if observed != identity:
                result["socket_path_check"] = "changed"
                return False
            result["socket_path_check"] = "unchanged_at_checks"
            return True

        try:
            async with asyncio.timeout_at(deadline):
                identity = self._socket_identity()
                check_deadline()
                if identity is None:
                    result["connection"] = "socket_missing"
                    return result
                reader, writer = await asyncio.open_unix_connection(self.socket_path, limit=8192)
                result["connection"] = "connected"
                if not check_path(identity):
                    return result
                for request_id, (prop, key) in enumerate((
                    ("audio-device", "audio_device"), ("current-ao", "current_ao"),
                ), 1):
                    current = key
                    check_deadline()
                    writer.write((json.dumps({"command": ["get_property", prop],
                                              "request_id": request_id}) + "\n").encode())
                    await writer.drain()
                    check_deadline()
                    while True:
                        if frames >= 16:
                            result[key] = {"status": "response_limit"}
                            return result
                        line = await reader.readline()
                        check_deadline()
                        if not line:
                            result["connection"] = "transport_error"
                            result[key] = {"status": "transport_error"}
                            return result
                        frames += 1
                        received += len(line)
                        if len(line) > 8192 or received > 32768:
                            result[key] = {"status": "response_limit"}
                            return result
                        if not line.endswith(b"\n"):
                            raise ValueError
                        response = json.loads(line.decode("utf-8"), object_pairs_hook=unique_object)
                        check_deadline()
                        if not isinstance(response, dict):
                            raise ValueError
                        response_id = response.get("request_id")
                        if "request_id" not in response:
                            if not isinstance(response.get("event"), str):
                                raise ValueError
                            continue
                        if type(response_id) is not int:
                            raise ValueError
                        if response_id != request_id:
                            continue
                        error = response.get("error")
                        if not isinstance(error, str):
                            raise ValueError
                        if error in {"property unavailable", "property not found"}:
                            result[key] = {"status": "unavailable" if error == "property unavailable"
                                           else "unsupported"}
                        elif error != "success":
                            result[key] = {"status": "request_failed"}
                            return result
                        else:
                            value = response.get("data")
                            if not isinstance(value, str):
                                raise ValueError
                            if prop == "audio-device":
                                validate_audio_device(value, auto_spawn=True)
                            elif re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is None:
                                raise ValueError
                            result[key] = {"status": "value", "value": value}
                        break
                    current = None
                    if not check_path(identity):
                        return result
        except FileNotFoundError:
            result["connection"] = "socket_missing"
            if writer is not None:
                result["socket_path_check"] = "changed"
        except BackendError:
            # _socket_identity rejects non-sockets and symlinks, without unlink.
            result["connection"] = "socket_invalid"
            if writer is not None:
                result["socket_path_check"] = "changed"
        except TimeoutError:
            result["connection"] = "timed_out"
            if current is not None:
                result[current] = {"status": "timed_out"}
        except OSError:
            result["connection"] = "transport_error"
            if current is not None:
                result[current] = {"status": "transport_error"}
        except (ValueError, UnicodeError, RecursionError):
            if current is not None:
                result[current] = {"status": "malformed_response"}
            else:
                result["connection"] = "socket_invalid"
        finally:
            if writer is not None:
                await self._close_writer(writer, deadline)
        return result

    async def _send_command(self, args: list[Any] | dict[str, Any], timeout: float, *, wait_response: bool) -> dict[str, Any]:
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
                # Opt-in routing may never fall through to a replacement or
                # unverifiable player between readiness and this connection.
                if self.audio_device is not None:
                    self._require_audio_device_peer(self._peer_pid(writer))
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
            name = args.get("name", "(empty)") if isinstance(args, dict) else (args[0] if args else "(empty)")
            LOG.warning("mpv IPC command %s failed: %s", name, error)
            return {"error": error}
        finally:
            if writer is not None:
                await self._close_writer(writer, deadline)

    async def loadfile(
        self, url: str, options: dict[str, str] | None = None, *, start_paused: bool = False,
        current: Callable[[], Awaitable[bool]] | None = None,
    ) -> dict[str, Any]:
        """Confirm DSP/gain, optionally holding the source for admission."""

        # A single lock order (DSP, then gain) prevents a restored cut from
        # disappearing between confirmation and the first decoded sample.
        async with self._dsp_lock:
            if self._dsp_managed:
                await self._apply_audio_filter_graph_locked(self._audio_filter_graph())
            async with self._gain_lock:
                # External mpv may have restarted or changed since the last load.
                await self._initialize_gain_locked(force=True)
                if current is not None:
                    return await self._loadfile_locked(url, options, start_paused=start_paused, current=current)
                return await self._loadfile_locked(url, options, start_paused=start_paused)

    async def _loadfile_locked(
        self, url: str, options: dict[str, str] | None, *, start_paused: bool = False,
        current: Callable[[], Awaitable[bool]] | None = None,
    ) -> dict[str, Any]:
        """Keep source loading serialized with gain initialization and changes."""

        if current is not None and not await current():
            raise BackendError("Playback admission ended before load")
        if start_paused:
            # Retain audio during admission instead of racing the first
            # readiness sample. A global hold also survives playlist expansion.
            await self.checked_command(["set_property", "pause", True])
        if current is not None and not await current():
            raise BackendError("Playback admission ended before load")
        LOG.info("mpv loadfile replace url=%s options=%s", redact_sensitive(url), redact_sensitive(options or {}))
        command: list[Any] | dict[str, Any] = ["loadfile", url, "replace"]
        if options:
            # mpv 0.38 inserted an index before positional options. Named
            # arguments are supported by both older and newer JSON IPC APIs,
            # so no version guess or potentially duplicate load retry is needed.
            command = {"name": "loadfile", "url": url, "flags": "replace", "options": options}
        response = await self.command(command, timeout=3.0)
        if response.get("error") != "success":
            LOG.warning("mpv loadfile returned error url=%s response=%s", redact_sensitive(url), redact_sensitive(response))
            return {**response, "error": response.get("error") or "mpv loadfile acknowledgement missing"}
        if start_paused:
            return response
        pause_response = await self.command(["set_property", "pause", False], timeout=3.0)
        if pause_response.get("error") != "success":
            LOG.warning("mpv unpause after loadfile returned error url=%s response=%s", redact_sensitive(url), redact_sensitive(pause_response))
            return {**pause_response, "error": pause_response.get("error") or "mpv unpause acknowledgement missing"}
        return response

    async def checked_command(self, args: list[Any]) -> dict[str, Any]:
        """Wait for an acknowledgement; never equate socket write with success."""
        response = await self.command(args, timeout=2.0)
        if response.get("error") != "success":
            raise BackendError(f"Backend operation failed: {redact_sensitive(response.get('error') or 'missing acknowledgement')}")
        return response

    async def append_file(self, url: str) -> int:
        """Append one inert playlist entry and return mpv's acknowledged ID.

        No gain, DSP, pause or gapless settings change here. The caller must
        prove a ready owned current item before staging, and confirm identity
        again before adopting the successor. ACK alone is never playback.
        """
        response = await self.checked_command(["loadfile", url, "append"])
        data = response.get("data")
        entry_id = data.get("playlist_entry_id") if isinstance(data, dict) else None
        if type(entry_id) is not int or entry_id < 0:
            raise BackendError("Native append did not return an entry identity")
        return entry_id

    async def pause(self) -> None:
        """Pause playback."""

        await self.checked_command(["set_property", "pause", True])

    async def resume(self, *, current: Callable[[], Awaitable[bool]] | None = None) -> None:
        """Reconcile DSP/gain, then unpause only a still-current admission."""

        async with self._dsp_lock:
            if self._dsp_managed:
                await self._apply_audio_filter_graph_locked(self._audio_filter_graph())
            async with self._gain_lock:
                await self._initialize_gain_locked(force=True)
                if current is not None and not await current():
                    raise BackendError("Playback command superseded before resume")
                await self.checked_command(["set_property", "pause", False])

    async def stop(self) -> None:
        """Stop the current mpv item."""

        await self.checked_command(["stop"])

    async def seek(self, seconds: int) -> None:
        """Seek to an absolute position in seconds."""

        await self.checked_command(["seek", seconds, "absolute"])

    async def volume(self, value: int) -> None:
        """Set and read back mpv volume without changing the desired mute."""

        if type(value) is not int or not 0 <= value <= 100:
            raise BackendError("Volume must be an integer from 0 to 100")
        if value != 0:
            async with self._dsp_lock:
                if self._dsp_managed:
                    await self._apply_audio_filter_graph_locked(self._audio_filter_graph())
                async with self._gain_lock:
                    await self._apply_gain_locked(value, self._desired_mute)
            return
        # Emergency silence must remain possible when a saved cut cannot be
        # restored. The complete pair applies zero before any desired unmute.
        async with self._gain_lock:
            await self._apply_gain_locked(value, self._desired_mute)

    async def mute(self, value: bool) -> None:
        """Set and read back mpv mute without changing the desired volume."""

        if type(value) is not bool:
            raise BackendError("Mute must be a boolean")
        if not value:
            async with self._dsp_lock:
                if self._dsp_managed:
                    await self._apply_audio_filter_graph_locked(self._audio_filter_graph())
                async with self._gain_lock:
                    await self._apply_gain_locked(self._desired_volume, value)
            return
        # Mute is confirmed before the volume write and cannot enable output.
        async with self._gain_lock:
            await self._apply_gain_locked(self._desired_volume, value)

    async def initialize_gain(self) -> None:
        """Apply configured/confirmed gain, including after deferred startup."""

        async with self._dsp_lock:
            if self._dsp_managed:
                await self._apply_audio_filter_graph_locked(self._audio_filter_graph())
            async with self._gain_lock:
                await self._initialize_gain_locked()

    async def _initialize_gain_locked(self, *, force: bool = False) -> None:
        if self._gain_initialized and not force:
            return
        await self._apply_gain_locked(self._desired_volume, self._desired_mute)

    async def _apply_gain_locked(self, volume: int, mute: bool) -> None:
        """Confirm a complete candidate pair before publishing either value."""

        # External mpv can reset either property independently. Every gain
        # operation restores both, even when only one was requested to change.
        # Invalidate before awaiting: partial writes and cancellations are not
        # a confirmed pair and must not advance either cached desired value.
        self._gain_initialized = False
        # Apply a requested mute first; when unmuting, establish volume first.
        values = [("volume", volume), ("mute", mute)]
        if mute:
            values.reverse()
        for name, value in values:
            await self._set_gain_property_locked(name, value)
        self._desired_volume, self._desired_mute = volume, mute
        self._gain_initialized = True

    async def _set_gain_property_locked(self, name: str, value: int | bool) -> None:
        """Acknowledge and verify a gain property within one bounded budget."""

        try:
            async with asyncio.timeout(2.0):
                await self.checked_command(["set_property", name, value])
                response = await self.checked_command(["get_property", name])
            actual = response.get("data")
            correct_type = type(actual) is bool if name == "mute" else type(actual) in (int, float)
            if not correct_type or actual != value:
                raise BackendError(f"Backend {name} readback did not confirm the requested value")
        except TimeoutError as exc:
            raise BackendError(f"Backend {name} confirmation timed out") from exc

    async def channel_balance(self, value: float) -> None:
        """Confirm a candidate balance/PEQ graph before publishing balance."""

        candidate = self.validate_channel_balance(value)
        async with self._dsp_lock:
            self._dsp_managed = True
            bands = self.validate_peq_bands(self._peq_bands if self._peq_bands is not None else [])
            graph = self._build_audio_filter_graph(candidate, bands)
            await self._apply_audio_filter_graph_locked(graph)
            self._channel_balance = candidate

    async def peq(self, bands: list[dict[str, float]]) -> None:
        """Confirm a copied candidate PEQ/balance graph before publishing PEQ."""

        candidate = self.validate_peq_bands(bands)
        async with self._dsp_lock:
            self._dsp_managed = True
            balance = self.validate_channel_balance(self._channel_balance)
            graph = self._build_audio_filter_graph(balance, candidate)
            await self._apply_audio_filter_graph_locked(graph)
            self._peq_bands = candidate

    async def configure_dsp(self, balance: float, bands: list[dict[str, float]]) -> None:
        """Apply a complete validated balance/PEQ candidate as one transaction."""
        candidate_balance = self.validate_channel_balance(balance)
        candidate_bands = self.validate_peq_bands(bands)
        async with self._dsp_lock:
            self._dsp_managed = True
            graph = self._build_audio_filter_graph(candidate_balance, candidate_bands)
            await self._apply_audio_filter_graph_locked(graph)
            self._channel_balance, self._peq_bands = candidate_balance, candidate_bands


    async def apply_audio_filters(self) -> None:
        """Reconfirm the desired graph without changing gain or source state."""

        async with self._dsp_lock:
            await self._apply_audio_filter_graph_locked(self._audio_filter_graph())

    @staticmethod
    def _finite_dsp_number(value: object, name: str, minimum: float, maximum: float) -> float:
        if type(value) not in (int, float):
            raise BackendError(f"{name} must be a finite number from {minimum:g} to {maximum:g}")
        try:
            number = float(value)
        except OverflowError as exc:
            raise BackendError(f"{name} is outside its supported range") from exc
        if not math.isfinite(number) or not minimum <= number <= maximum:
            raise BackendError(f"{name} must be a finite number from {minimum:g} to {maximum:g}")
        return number

    @staticmethod
    def validate_channel_balance(value: object) -> float:
        """Reject malformed, nonfinite or out-of-range balance without writes."""

        return MpvBackend._finite_dsp_number(value, "Balance", -1.0, 1.0)

    @staticmethod
    def validate_peq_bands(bands: object) -> list[dict[str, float]]:
        """Copy up to ten complete bands, with bounded finite numeric values.

        Frequencies are 10..24000 Hz, gain -24..24 dB, and Q 0.1..30.
        Modes are bypass (-1), low shelf (0), peak (1), high shelf (2),
        low pass (3), and high pass (5). A zero gain never bypasses pass filters.
        These input bounds do not promise support at every source sample rate.
        """

        if not isinstance(bands, list) or len(bands) > 10:
            raise BackendError("PEQ must be a list of at most 10 bands")
        result: list[dict[str, float]] = []
        for band in bands:
            if not isinstance(band, dict) or set(band) != {"freq", "gain", "q", "mode"}:
                raise BackendError("Each PEQ band must contain exactly freq, gain, q and mode")
            candidate = {
                "freq": MpvBackend._finite_dsp_number(band["freq"], "PEQ frequency", 10.0, 24000.0),
                "gain": MpvBackend._finite_dsp_number(band["gain"], "PEQ gain", -24.0, 24.0),
                "q": MpvBackend._finite_dsp_number(band["q"], "PEQ Q", 0.1, 30.0),
                "mode": MpvBackend._finite_dsp_number(band["mode"], "PEQ mode", -1.0, 5.0),
            }
            if candidate["mode"] not in {-1, 0, 1, 2, 3, 5}:
                raise BackendError("Unsupported PEQ mode; expected -1, 0, 1, 2, 3 or 5")
            result.append(candidate)
        return result

    async def _checked_dsp_command(self, args: list[Any]) -> dict[str, Any]:
        response = await self.command(args, timeout=2.0)
        if not isinstance(response, dict) or response.get("error") != "success":
            error = response.get("error") if isinstance(response, dict) else None
            raise BackendError(f"Backend DSP operation failed: {redact_sensitive(error or 'missing acknowledgement')}")
        return response

    async def _read_dsp_graph(self) -> str:
        """Read exactly our enabled lavfi entry, leaving other filters alone."""

        response = await self._checked_dsp_command(["get_property", "af"])
        entries = response.get("data")
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise BackendError("Backend DSP readback is not a filter list")
        matches = [entry for entry in entries if entry.get("label") == "fake_linkplay_dsp"]
        if not matches:
            return ""
        if len(matches) != 1:
            raise BackendError("Backend DSP readback contains duplicate named filters")
        entry = matches[0]
        params = entry.get("params")
        if (entry.get("name") != "lavfi" or entry.get("enabled") is not True
                or not isinstance(params, dict) or set(params) != {"graph"}
                or not isinstance(params["graph"], str) or not params["graph"]):
            raise BackendError("Backend DSP readback did not confirm an enabled lavfi graph")
        return params["graph"]

    async def _apply_audio_filter_graph_locked(self, graph: str) -> None:
        """Replace one label transactionally; an idle af read is configuration.

        mpv replaces an existing label on ``af add``. Removing it first loses
        the working chain when construction fails. A pre-read supplies the
        rollback graph and prevents removing an absent label during bypass.
        A failed or cancelled write can still have reached mpv, so settle its
        rollback under the same lock before another candidate is allowed in.
        """

        self._dsp_configuration_confirmed = False
        try:
            async with asyncio.timeout(4.0):
                previous = await self._read_dsp_graph()
                if previous != graph:
                    try:
                        if graph:
                            await self._checked_dsp_command(["af", "add", f"@fake_linkplay_dsp:lavfi=[{graph}]"])
                        else:
                            await self._checked_dsp_command(["af", "remove", "@fake_linkplay_dsp"])
                        if await self._read_dsp_graph() != graph:
                            raise BackendError("Backend DSP readback did not confirm the requested graph")
                    except (BackendError, asyncio.CancelledError) as exc:
                        restored, cancelled = await self._settle_dsp_rollback(previous)
                        if cancelled:
                            raise asyncio.CancelledError
                        if not restored:
                            if isinstance(exc, asyncio.CancelledError):
                                LOG.warning("cancelled DSP update could not confirm previous graph restoration")
                                raise
                            raise BackendError("Backend DSP update failed; previous graph restoration is unconfirmed")
                        raise
        except TimeoutError as exc:
            raise BackendError("Backend DSP confirmation timed out") from exc
        self._dsp_configuration_confirmed = True
        LOG.info("mpv DSP configuration confirmed: %s", graph or "(bypass)")

    async def _settle_dsp_rollback(self, previous: str) -> tuple[bool, bool]:
        """Finish bounded restoration even if the caller cancels repeatedly."""

        task = asyncio.create_task(self._restore_dsp_graph(previous))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        restored = task.result()
        self._dsp_configuration_confirmed = restored and previous == self._audio_filter_graph()
        return restored, cancelled

    async def _restore_dsp_graph(self, previous: str) -> bool:
        """Restore only our label, and explicitly report an unverifiable result."""

        try:
            async with asyncio.timeout(4.0):
                try:
                    actual = await self._read_dsp_graph()
                except BackendError:
                    actual = None
                if actual == previous:
                    return True
                if previous:
                    await self._checked_dsp_command(["af", "add", f"@fake_linkplay_dsp:lavfi=[{previous}]"])
                elif actual:
                    await self._checked_dsp_command(["af", "remove", "@fake_linkplay_dsp"])
                else:
                    # Never remove a label whose presence is unconfirmed.
                    return False
                return await self._read_dsp_graph() == previous
        except (BackendError, TimeoutError):
            return False

    def _audio_filter_graph(self) -> str:
        """Build the validated desired graph, without mutating backend state."""

        balance = self.validate_channel_balance(self._channel_balance)
        bands = self.validate_peq_bands(self._peq_bands if self._peq_bands is not None else [])
        return self._build_audio_filter_graph(balance, bands)

    @staticmethod
    def _build_audio_filter_graph(balance: float, bands: list[dict[str, float]]) -> str:
        """Build one lavfi graph from a validated, private candidate snapshot."""

        filters: list[str] = []
        for band in bands:
            gain, freq, q, mode = band["gain"], band["freq"], band["q"], band["mode"]
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
        if abs(balance) >= 0.001:
            left = 1.0 - max(0.0, balance)
            right = 1.0 + min(0.0, balance)
            filters.append(f"pan=stereo|c0={left:.3f}*c0|c1={right:.3f}*c1")
        return ",".join(filters)

    async def apply_station_child_history(self, position: int, entry_id: int, pid: int) -> bool:
        """Set/read the shared file-local policy for one native active entry.

        A Python lock cannot stop mpv naturally advancing between an observation
        and a write. mpv expands this property name and executes synchronous set
        in the same core command. A changed/missing entry expands to an invalid
        empty name and cannot change another file's option. Only integer identity
        is interpolated; media URLs never enter the expression.
        """
        if any(type(value) is not int or value < 0 for value in (position, entry_id, pid)):
            raise ValueError("Station child identity must contain nonnegative integers")

        def guarded(value: str) -> str:
            for name, expected in reversed((("pid", pid),
                    (f"playlist/{position}/id", entry_id),
                    (f"playlist/{position}/current", "yes"),
                    (f"playlist/{position}/playing", "yes"))):
                value = "${?=" + name + "==" + str(expected) + ":" + value + "}"
            return value

        # Disable donation first: a finite backward limit alone can borrow
        # the forward allocation. Each mutation is independently guarded by
        # exactly the same native identity; a transition between writes safely
        # rejects the later write. Partial application is never confirmed.
        for name in ("demuxer-donate-buffer", "demuxer-max-back-bytes"):
            response = await self.command(["expand-properties", "set",
                guarded("file-local-options/" + name), LIVE_STATION_HISTORY_OPTIONS[name]], timeout=0.5)
            if response.get("error") != "success":
                return False
        # Read both effective values and both file-local flags under the same
        # guard. ACKs alone cannot establish application to the original entry.
        response = await self.command(["expand-text", guarded(
            "${=options/demuxer-max-back-bytes}|${=options/demuxer-donate-buffer}|"
            "${=option-info/demuxer-max-back-bytes/set-locally}|"
            "${=option-info/demuxer-donate-buffer/set-locally}")], timeout=0.5)
        return (response.get("error") == "success"
                and response.get("data") == f"{LIVE_STATION_BACK_BYTES}|no|yes|yes")

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
            "playlist-path",
            "playlist",
            "stream-open-filename",
            "core-idle",
            "eof-reached",
            "paused-for-cache",
            "cache-buffering-state",
            "demuxer-cache-state",
            "track-list",
        ):
            data[key] = await self.get_property(key)
        data["audio-info"] = None
        # These are current decoded-track observations, never URL/catalog
        # hints. Fence the packet bitrate read against native entry AND track
        # identity changes while the separately queried properties arrive.
        entry = self._media_entry(data.get("playlist"))
        track = self._media_audio_track(data.get("track-list"))
        if data["idle-active"] is False and entry is not None and track is not None and entry[1] == data.get("path"):
            bitrate = await self.get_property("audio-bitrate")
            end_track = self._media_audio_track(await self.get_property("track-list"))
            end_entry = self._media_entry(await self.get_property("playlist"))
            if end_entry == entry and end_track == track:
                data["audio-info"] = {"entry_id": entry[0], "codec": track[1],
                    "bitrate_bps": (int(bitrate) if type(bitrate) in {int, float}
                        and math.isfinite(bitrate) and 1 <= bitrate <= 1_000_000_000 else None)}
        return data

    @staticmethod
    def _media_entry(entries: object) -> tuple[int, str] | None:
        if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
            return None
        active = [entry for entry in entries if entry.get("current") is True or entry.get("playing") is True]
        if len(active) != 1:
            return None
        entry = active[0]
        if (entry.get("current") is True and entry.get("playing") is True
                and type(entry.get("id")) is int and entry["id"] >= 0
                and isinstance(entry.get("filename"), str) and entry["filename"]):
            return entry["id"], entry["filename"]
        return None

    @staticmethod
    def _media_audio_track(tracks: object) -> tuple[int, str | None] | None:
        if not isinstance(tracks, list):
            return None
        selected = [track for track in tracks if isinstance(track, dict)
                    and track.get("type") == "audio" and track.get("selected") is True]
        if len(selected) != 1 or type(selected[0].get("id")) is not int:
            return None
        codec = selected[0].get("codec")
        codec = codec if isinstance(codec, str) and re.fullmatch(r"[a-zA-Z0-9_+-]{1,40}", codec) else None
        return selected[0]["id"], codec
