"""Opt-in go-librespot PCM input to the existing ORR mpv/DSP owner.

The pinned upstream pipe driver produces f32le, 44100 Hz, stereo. It never
opens ALSA here. A private, non-addressable FIFO is the only admitted input;
normal URL admission cannot start this source. No service or account setup is
performed by preparing a profile. Explicit startup activation enables the
upstream local Spotify-app discovery flow, without persisting credentials.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import ipaddress
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import sys
import tempfile

from .config import StartupConfigError
from . import diagnostics as diagnostic
from .mpv_backend import BackendError
from .queue import PlaybackQueue, QueueItem
from .spotify_client import verify_client
from .spotify_exclusive import ExclusiveProfileError, _directory, _path, _private_directory, _read
from .spotify_playback import control, now_playing
from .spotify_receiver import _device_name, _lock, _preferred_bitrate, _write
from .startup_state import StartupStateError
from .state import PlaybackPhase

SOURCE_URL = "orr-spotify://local"
# File-local options. All defaults return on the next ordinary ORR source.
PCM_OPTIONS = {
    "demuxer": "rawaudio", "demuxer-rawaudio-format": "floatle",
    "demuxer-rawaudio-rate": "44100", "demuxer-rawaudio-channels": "stereo",
    "cache": "no", "demuxer-readahead-secs": "0", "demuxer-max-bytes": "65536",
    "demuxer-max-back-bytes": "0", "audio-buffer": "0.1", "ytdl": "no",
    "force-media-title": "Spotify Connect",
}


def _profile(root: Path) -> dict:
    _private_directory(root)
    value = json.loads(_read(root / "spotify-eq.json"))
    if (type(value) is not dict or set(value) != {
            "format", "client_dir", "client_sha256", "name", "bitrate", "port", "device_id"}
            or value["format"] != 1 or type(value["format"]) is not int
            or not re.fullmatch(r"[0-9a-f]{40}", str(value["device_id"]))
            or not re.fullmatch(r"[0-9a-f]{64}", str(value["client_sha256"]))
            or type(value["port"]) is not int or not 1024 <= value["port"] <= 65535):
        raise ExclusiveProfileError("Use an intact private Spotify EQ profile.")
    _path(value["client_dir"])
    _device_name(value["name"])
    _preferred_bitrate(value["bitrate"])
    return value


def setup(directory: str, client_dir: str, name: str, bitrate: int, port: int) -> dict:
    root, client = _path(directory), _path(client_dir)
    if root == client or root in client.parents or client in root.parents:
        raise ExclusiveProfileError("Use separate private client and Spotify EQ directories.")
    _device_name(name)
    _preferred_bitrate(bitrate)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ExclusiveProfileError("Use an unprivileged loopback control port.")
    _, receipt = verify_client(client)
    # Do not overwrite/adopt a previous profile or an existing receiver's state.
    with _directory(root.parent) as parent:
        os.mkdir(root.name, mode=0o700, dir_fd=parent)
    value = {"format": 1, "client_dir": str(client), "client_sha256": receipt["binary_sha256"],
             "name": name, "bitrate": bitrate, "port": port, "device_id": secrets.token_hex(20)}
    _write(root / "spotify-eq.json", value)
    return {"spotify_eq": "PREPARED", "client": "go-librespot_v0.10.3",
            "audio_path": "private_PCM_to_existing_ORR_mpv_DSP", "activated": False,
            "credentials_persisted": False, "gates": ["ACCOUNT_PENDING", "HARDWARE_PENDING", "PROVIDER_POLICY_PENDING"]}


def _config(profile: dict, runtime: Path) -> dict:
    return {
        "device_name": profile["name"], "device_id": profile["device_id"], "device_type": "speaker",
        "bitrate": profile["bitrate"], "audio_backend": "pipe",
        "audio_output_pipe": str(runtime / "audio.fifo"), "audio_output_pipe_format": "f32le",
        "audio_output_pipe_wait_for_reader": False,
        "external_volume": False, "volume_steps": 100, "initial_volume": 0,
        "ignore_last_volume": True, "disable_autoplay": True, "crossfade_duration": 0,
        "credentials": {"type": "zeroconf", "zeroconf": {"persist_credentials": False}},
        "zeroconf_enabled": True, "zeroconf_backend": "builtin", "zeroconf_port": 0,
        "mpris_enabled": False,
        "server": {"enabled": True, "address": "127.0.0.1", "port": profile["port"],
                   "allow_origin": "", "cert_file": "", "key_file": ""},
        "cache": {"enabled": False, "dir": str(runtime / "cache"), "size_limit": "100MB"},
        "metadata": {"enabled": False, "context_sweep": False}, "log_level": "error",
    }


class SpotifyEqSource:
    """One renderer invocation; only its own child and FIFO are disposable."""

    def __init__(self, root: Path, profile: dict):
        self.root, self.profile = root, profile
        self.api = None
        self.process = None
        self.runtime = None
        self.fifo: Path | None = None
        self.fifo_identity = None
        self.keepalive = None
        self.active = False
        self.stopping = False
        self.starting = True
        self.failed = False
        self._lock = asyncio.Lock()
        self._stop_task = None
        self._entry_id = None
        self._observing = False
        self._provider = None
        self._diagnostic_task = None
        self._diagnostic_test_id = None

    def _diagnostic_tick(self) -> None:
        recorder = getattr(self.api, "diagnostics", None)
        enabled = (self.active and not self.starting and not self.stopping
                   and recorder is not None and diagnostic.safe(self.api, "active"))
        if not enabled:
            if self._diagnostic_task is not None:
                self._diagnostic_task.cancel()
            return
        if self._diagnostic_task is not None and not self._diagnostic_task.done():
            if self._diagnostic_test_id != recorder.window["test_id"]:
                self._diagnostic_task.cancel()
            return
        if self._diagnostic_test_id == recorder.window["test_id"]:
            return  # No reconnect, including after an unconfirmed observation.
        from .spotify_source_diagnostic import watch
        self._diagnostic_test_id = recorder.window["test_id"]
        self._diagnostic_task = asyncio.create_task(watch(self, recorder, self._diagnostic_test_id))

    def _validate_child(self) -> None:
        if not self.active or self.stopping or self.process is None or self.process.returncode is not None:
            raise ExclusiveProfileError("The owned Spotify process is no longer active.")

    def _check_fifo(self) -> None:
        if self.fifo is None or self.keepalive is None:
            raise BackendError("Spotify PCM input is unavailable")
        info = self.fifo.lstat()
        held = os.fstat(self.keepalive)
        if (not stat.S_ISFIFO(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid() or (info.st_dev, info.st_ino) != self.fifo_identity
                or (held.st_dev, held.st_ino) != self.fifo_identity):
            raise BackendError("Spotify PCM input ownership changed")

    def _owns_snapshot(self, snapshot: dict) -> bool:
        if snapshot.get("path") != str(self.fifo):
            return False
        entries = snapshot.get("playlist")
        if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
            return False
        item = entries[0]
        if (item.get("filename") != str(self.fifo) or item.get("current") is not True
                or item.get("playing") is not True or type(item.get("id")) is not int):
            return False
        if self._entry_id is None:
            self._entry_id = item["id"]
        return item["id"] == self._entry_id

    async def _current_reader(self, generation: int, *, during_control: bool = False) -> bool:
        def current():
            return (self.active and not self.stopping and not self.failed and self.api is not None
                    and generation == self.api.state.generation
                    and (during_control or not self._lock.locked()))
        if not current():
            return False
        snapshot = await self.api.mpv.snapshot()
        return current() and not snapshot.get("backend-error") and self._owns_snapshot(snapshot)

    async def _spawn(self, binary: Path, directory: Path):
        # No account/environment tokens, user configuration or upstream logs are
        # inherited. The verified client needs only its native system libraries.
        env = {"PATH": "/usr/bin:/bin", "HOME": str(directory), "LC_ALL": "C",
               "XDG_CONFIG_HOME": str(directory), "XDG_CACHE_HOME": str(directory / "cache")}
        return await asyncio.create_subprocess_exec(
            str(binary), "--config_dir", str(directory), env=env, cwd=directory,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True, umask=0o077,
        )

    async def _read_provider(self) -> dict:
        self._validate_child()
        child = self.process
        result = await asyncio.to_thread(now_playing, "127.0.0.1", self.profile["port"],
                                         str(child.pid), self._validate_child)
        self._validate_child()
        if self.process is not child:
            raise BackendError("Spotify observation superseded")
        return result

    async def start(self, api) -> None:
        if api.spotify_source is not None:
            raise BackendError("Stop the current Spotify source before starting another")
        generation = api.state.generation
        self.api = api
        api.spotify_source = self
        self.active = True  # Competing source commands must wait for confirmed Stop.
        async with self._lock:
            binary, receipt = verify_client(self.profile["client_dir"])
            if receipt["binary_sha256"] != self.profile["client_sha256"]:
                raise ExclusiveProfileError("The pinned Spotify client changed.")
            directory = Path(tempfile.mkdtemp(prefix="pcm-", dir=self.root))
            self.runtime = directory
            self.fifo = directory / "audio.fifo"
            os.mkfifo(self.fifo, mode=0o600)
            self.keepalive = os.open(self.fifo, os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
            info = os.fstat(self.keepalive)
            self.fifo_identity = info.st_dev, info.st_ino
            self._check_fifo()
            _write(directory / "config.yml", _config(self.profile, directory))
            async with api._backend_lock:
                await api.mpv.start()
                before = await api.mpv.snapshot()
                if before.get("backend-error") or before.get("idle-active") is not True:
                    raise BackendError("Spotify requires an idle, owned renderer")
                if self.stopping or api.spotify_source is not self or generation != api.state.generation:
                    raise BackendError("Spotify startup was superseded")
                generation = api._accept_playback_command("play")
                api.state.queue = PlaybackQueue(items=[QueueItem(
                    SOURCE_URL, title="Spotify Connect", source="Spotify", stream_type="live")], index=1)
                api._activate_queue_item()
                api.state.play_medium = "NETWORK"
                api.state.phase = PlaybackPhase.BUFFERING
                # This existing method confirms the complete existing gain and
                # EQ/balance graph before the FIFO can yield its first sample.
                reply = await api._gain_reconciling_call(api.mpv.loadfile(str(self.fifo), dict(PCM_OPTIONS)))
                if reply.get("error") != "success":
                    raise BackendError("Spotify PCM input could not be loaded")
                api.state.backend_url = str(self.fifo)
                api.state.backend_original_url = SOURCE_URL
                deadline = asyncio.get_running_loop().time() + 3
                while True:
                    snapshot = await api.mpv.snapshot()
                    if snapshot.get("backend-error"):
                        raise BackendError("Spotify PCM source ownership was not confirmed")
                    if self._owns_snapshot(snapshot):
                        break
                    if asyncio.get_running_loop().time() >= deadline:
                        raise BackendError("Spotify PCM source ownership was not confirmed")
                    await asyncio.sleep(0.025)
            if self.stopping or api.spotify_source is not self or generation != api.state.generation:
                raise BackendError("Spotify startup was superseded")
            self.process = await self._spawn(binary, directory)
            # NO_SESSION is healthy discovery readiness, never authentication or
            # playback evidence. An unrelated loopback listener is never queried.
            deadline = asyncio.get_running_loop().time() + 10
            while True:
                self._validate_child()
                try:
                    self._provider = await self._read_provider()
                    break
                except (ExclusiveProfileError, OSError):
                    if asyncio.get_running_loop().time() >= deadline:
                        raise BackendError("Spotify's owned control listener did not become ready") from None
                    await asyncio.sleep(0.1)
            self.starting = False
            api._schedule_event_notify()

    async def observe(self) -> None:
        api = self.api
        self._diagnostic_tick()
        if (not self.active or self.starting or self.stopping or self.failed
                or self._observing or self._lock.locked() or api is None):
            return
        self._observing = True
        generation = api.state.generation
        try:
            self._check_fifo()
            provider = await self._read_provider()
            async with api._backend_lock:
                snapshot = await api.mpv.snapshot()
                status = provider.get("status")
                # An app Resume only restarts the producer. If ORR previously
                # paused its reader, reconcile that one owned reader as well.
                # Never race a newer Web control, Stop, or source generation.
                if (self.active and not self.stopping and not self._lock.locked()
                        and generation == api.state.generation and not snapshot.get("backend-error")
                        and self._owns_snapshot(snapshot) and snapshot.get("pause") is True
                        and status and not status["paused"] and not status["stopped"]):
                    await api._gain_reconciling_call(api.mpv.resume(
                        current=lambda: self._current_reader(generation)))
                    snapshot = await api.mpv.snapshot()
            if not self.active or self.stopping or generation != api.state.generation:
                return
            if snapshot.get("backend-error") or not self._owns_snapshot(snapshot):
                raise BackendError("Spotify PCM source ownership was lost")
            self._provider = provider
            status, track = provider.get("status"), provider.get("track")
            api.state.stream_title = ""
            api.state.title = "Spotify Connect"
            api.state.artist = api.state.album = ""
            api.state.curpos = api.state.totlen = 0
            if isinstance(track, dict):
                # Bounded display metadata only; never account/URI/artwork fields.
                def text(value):
                    return "".join(c for c in value if c.isprintable())[:512] if isinstance(value, str) else ""
                api.state.title = text(track.get("title")) or "Spotify Connect"
                api.state.artist = text(", ".join(track.get("artists") or []))
                api.state.album = text(track.get("album"))
                for field, target in (("position_ms", "curpos"), ("duration_ms", "totlen")):
                    if type(track.get(field)) is int:
                        setattr(api.state, target, track[field] // 1000)
            # A provider's PLAYING flag alone is never audio confirmation.
            audio = (api._mpv_snapshot_has_audio(snapshot) and snapshot.get("core-idle") is False
                     and snapshot.get("pause") is False and snapshot.get("paused-for-cache") is not True)
            if audio and status and not status["stopped"] and not status["paused"] and not status["buffering"]:
                api.state.phase, api.state.intent = PlaybackPhase.PLAYING, "play"
            elif status and status["paused"] and (snapshot.get("pause") is True or not audio):
                api.state.phase, api.state.intent = PlaybackPhase.PAUSED, "pause"
            else:
                api.state.phase = PlaybackPhase.BUFFERING
            api.state.last_error = ""
            api._schedule_event_notify()
        except (ExclusiveProfileError, BackendError, OSError, ValueError):
            if self.active and not self.stopping and generation == api.state.generation:
                self.failed = True
                api.state.mark_error("Spotify source unavailable; Stop before changing sources")
                api._schedule_event_notify()
        finally:
            self._observing = False

    def actions(self) -> str:
        if self.failed:
            return "Stop"
        status = self._provider.get("status") if self._provider else None
        return "Play,Pause,Stop" if status and not status["stopped"] else "Stop"

    async def transport(self, command: str) -> None:
        if command == "stop":
            await self.close()
            return
        if command not in {"pause", "resume"} or self.failed:
            raise BackendError("Use the Spotify app for this source control")
        async with self._lock:
            self._validate_child()
            api = self.api
            generation = api._accept_playback_command("pause" if command == "pause" else "play")
            try:
                # Resume the reader first: the upstream pipe writer can be
                # backpressured while holding its output mutex. Pause the reader
                # only after upstream acknowledges Pause, avoiding that deadlock.
                if command == "resume":
                    async with api._backend_lock:
                        if not self._owns_snapshot(await api.mpv.snapshot()):
                            raise BackendError("Spotify PCM ownership changed before control")
                        await api._gain_reconciling_call(api.mpv.resume(
                            current=lambda: self._current_reader(generation, during_control=True)))
                result = await asyncio.to_thread(control, "127.0.0.1", self.profile["port"],
                    str(self.process.pid), command, None, self._validate_child)
                status = (result.get("provider_after") or {}).get("status")
                if (result.get("request_outcome") != "ACKNOWLEDGED" or result.get("error")
                        or not status or status["paused"] != (command == "pause")):
                    raise BackendError("Spotify transport was not confirmed")
                if command == "pause":
                    async with api._backend_lock:
                        if not self._owns_snapshot(await api.mpv.snapshot()):
                            raise BackendError("Spotify PCM ownership changed before control")
                        await api.mpv.pause()
                if generation != api.state.generation:
                    raise BackendError("Spotify transport superseded")
                api.state.phase = PlaybackPhase.PAUSED if command == "pause" else PlaybackPhase.BUFFERING
                api._schedule_event_notify()
            except (ExclusiveProfileError, BackendError, OSError, asyncio.CancelledError):
                self.failed = True
                api.state.mark_error("Spotify transport unconfirmed; Stop before changing sources")
                raise BackendError("Spotify transport unconfirmed") from None

    async def _reap_producer(self) -> None:
        # go-librespot has no child-process launch in this pinned mode.
        child = self.process
        if child is not None and child.returncode is None:
            try:
                child.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(child.wait(), 3)
            except TimeoutError:
                try:
                    child.kill()
                except ProcessLookupError:
                    pass
                await asyncio.wait_for(child.wait(), 3)
        if child is not None and child.returncode is None:
            raise BackendError("Spotify producer exit was not confirmed")

    async def _stop(self) -> None:
        api = self.api
        if api is not None:
            api._accept_playback_command("stop")
            api.state.mark_transitioning()
        try:
            # Reap even if a provider control still holds the lifecycle lock.
            # Its owned connection then closes; shutdown cannot leave a live
            # producer merely because the control used its whole HTTP deadline.
            await self._reap_producer()
            if self._diagnostic_task is not None:
                await asyncio.gather(self._diagnostic_task, return_exceptions=True)
            async with self._lock:
                # Startup might have been awaiting child creation when close()
                # set stopping. Capture/reap that child before reader release.
                await self._reap_producer()
                if api is not None and self.fifo is not None:
                    async with api._backend_lock:
                        snapshot = await api.mpv.snapshot()
                        if snapshot.get("backend-error"):
                            raise BackendError("Spotify output release is unknown")
                        if snapshot.get("idle-active") is not True:
                            if not self._owns_snapshot(snapshot):
                                raise BackendError("Spotify output ownership changed; stop the renderer")
                            await api.mpv.stop()
                            snapshot = await api.mpv.snapshot()
                        if snapshot.get("idle-active") is not True:
                            raise BackendError("Spotify output release was not confirmed")
                if self.keepalive is not None:
                    os.close(self.keepalive)
                    self.keepalive = None
                if self.runtime is not None:
                    _private_directory(self.runtime)
                    shutil.rmtree(self.runtime)
                    self.runtime = None
                self.active = False
                if api is not None:
                    api.spotify_source = None
                    api.state.backend_url = api.state.backend_original_url = ""
                    if api.state.current_url == SOURCE_URL:
                        api.state.queue = PlaybackQueue()
                        api._activate_queue_item()
                    api.state.mark_stop()
                    api._schedule_event_notify()
        except (OSError, BackendError, TimeoutError):
            self.failed = True
            if api is not None:
                api.state.mark_error("Spotify stop unconfirmed; no new source may start")
            raise BackendError("Spotify stop unconfirmed; stop the renderer") from None

    async def close(self) -> None:
        if not self.active:
            return
        if self._stop_task is None:
            self.stopping = True
            if self._diagnostic_task is not None:
                self._diagnostic_task.cancel()
            # Do not wait behind a blocked provider control before interrupting
            # our own producer. Closing its socket releases that control's lock;
            # only _stop's reaping + native idle proof release source ownership.
            if self.process is not None and self.process.returncode is None:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass
            self._stop_task = asyncio.create_task(self._stop())
        # A cancelled HTTP waiter must not abandon child reaping or enable a
        # replacement while cleanup is still running. Failed stops stay blocked.
        await asyncio.shield(self._stop_task)


def prepare_activation(cfg, source, resources):
    """Own the opt-in profile/listeners before any renderer state is touched.

    The existing unit continues to execute `serve --config ...`. This function
    never changes that config, services, accounts, or another process. Reserved
    sockets become the ordinary HTTP listeners, with no close/rebind gap.
    """
    try:
        if sys.platform != "linux" or os.geteuid() == 0:
            raise ExclusiveProfileError("Use Spotify EQ as a normal Linux user, never root.")
        if source is None:
            root = _path(cfg.spotify_eq_profile)
            source = SpotifyEqSource(root, _profile(root))
        elif cfg.spotify_eq_profile is not None and _path(cfg.spotify_eq_profile) != source.root:
            raise ExclusiveProfileError("The explicit and configured Spotify EQ profiles differ.")
        if (cfg.auto_spawn_mpv is not True or type(cfg.mpv_audio_device) is not str
                or not re.fullmatch(r"alsa/(?:plug)?hw:CARD=[A-Za-z0-9_-]{1,64},DEV=(?:0|[1-9][0-9]{0,2})", cfg.mpv_audio_device)
                or source.profile["port"] in {cfg.http_api_port, cfg.upnp_port, 80 if cfg.mirror_port_80 else None}):
            raise ExclusiveProfileError("Spotify EQ requires an owned mpv, explicit ALSA hw/plughw output, and distinct control ports.")
        # This first opt-in supports the existing literal IPv4 service profile.
        address = str(ipaddress.IPv4Address(cfg.bind_address))
        _private_directory(_path(cfg.mpv_socket).parent)
        binary, receipt = verify_client(source.profile["client_dir"])
        if receipt["binary_sha256"] != source.profile["client_sha256"]:
            raise ExclusiveProfileError("The pinned Spotify client changed.")
        sockets = {}
        ports = [cfg.http_api_port, cfg.upnp_port] + ([80] if cfg.mirror_port_80 else [])
        for port in dict.fromkeys(ports):
            if type(port) is not int or not 1 <= port <= 65535:
                raise ExclusiveProfileError("Spotify EQ requires explicit valid renderer listener ports.")
            held = resources.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            held.bind((address, port))
            # Listen now so a second SO_REUSEADDR bind cannot share the pending
            # reservation. No handler or account is active before _serve.
            held.listen(socket.SOMAXCONN)
            held.setblocking(False)
            sockets[port] = held
        resources.enter_context(_lock(source.root, "run.lock"))
        return source, sockets
    except ExclusiveProfileError as exc:
        raise StartupConfigError(str(exc)) from None
    except (OSError, ValueError, KeyError, TypeError):
        raise StartupConfigError(
            "Spotify EQ preflight failed: check the private profile/client, explicit output, "
            "socket directory and free listener ports. No renderer state or existing process was changed."
        ) from None


def add_eq_parser(commands) -> None:
    parser = commands.add_parser("eq", help="opt-in Spotify Connect through the existing ORR EQ")
    parser.add_argument("--directory", required=True, help="private separate Spotify EQ profile")
    actions = parser.add_subparsers(dest="spotify_eq_cmd", required=True)
    prepare = actions.add_parser("setup", help="prepare a private profile without activation/account access")
    prepare.add_argument("--client-dir", required=True)
    prepare.add_argument("--name", default="Open Room Renderer Spotify")
    prepare.add_argument("--bitrate", type=int, choices=(96, 160, 320), default=160)
    prepare.add_argument("--port", type=int, default=3678, help="loopback-only upstream control port")
    run = actions.add_parser("run", help="explicit foreground renderer + local-app Spotify discovery; no services")
    run.add_argument("--renderer-config", required=True, help="existing explicit renderer YAML; never rewritten")


def run_eq(args: argparse.Namespace) -> int:
    try:
        if sys.platform != "linux" or os.geteuid() == 0:
            raise ExclusiveProfileError("Use Spotify EQ as a normal Linux user, never root.")
        if args.spotify_eq_cmd == "setup":
            result = setup(args.directory, args.client_dir, args.name, args.bitrate, args.port)
            print(json.dumps(result, sort_keys=True))
            return 0
        root = _path(args.directory)
        profile = _profile(root)
        config_path = _path(args.renderer_config)
        _read(config_path)
        # serve performs its profile/listener reservation before any state load.
        # The same path is used by the existing unit's optional YAML setting.
        from .cli import StartupListenerError, serve
        try:
            asyncio.run(serve(str(config_path), spotify_source=SpotifyEqSource(root, profile)))
        except StartupListenerError:
            raise ExclusiveProfileError("A renderer listener is already in use; stop the separate renderer before Spotify EQ activation.") from None
        return 0
    except (ExclusiveProfileError, BackendError, StartupConfigError, StartupStateError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print("Spotify EQ setup or runtime could not be confirmed; no service was changed.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
