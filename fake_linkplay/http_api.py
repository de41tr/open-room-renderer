"""HTTP, UPnP-ish, queue, settings, and EQ control surface.

This is the largest module because the prototype grew from observed client
interactions. New contributors should prefer adding small tests around a narrow
behavior before refactoring. Raw request logging is intentionally opt-in and
redacted before it reaches normal logs.
"""

from __future__ import annotations

import asyncio
import json
import ipaddress
import logging
import math
from copy import deepcopy
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC
from datetime import datetime
from html import escape
from html import unescape as html_unescape
from pathlib import Path
from collections.abc import Awaitable, Callable
from urllib.parse import quote
from urllib.parse import unquote, urljoin, urlsplit, parse_qs
from uuid import uuid4

from aiohttp import ClientError
from aiohttp import ClientSession
from aiohttp import ClientTimeout, TCPConnector
from aiohttp import web

from .capabilities import configured_presets, capability_fields, configured_sources, service_capability
from .config import Config
from .log_safety import redact_media_urls
from .mpv_backend import BackendError, MpvBackend
from .network_policy import NetworkPolicyError, PinnedResolver, resolve_proxy_target, validate_callback_url
from .radio import RadioResolution, RadioResolutionError, resolve_station
from .persistence import (AtomicWriteCommittedError, atomic_write_text, encode_active_dsp,
                          load_active_dsp, save_gain, validate_eq_records)
from .queue import PlaybackQueue, QueueError, QueueItem, parse_didl_metadata, parse_queue_context, serialize_queue_context
from .soap import MUTATING_ACTIONS, SERVICE_ALIASES, SoapFault, fault_xml, parse_request
from .state import PlaybackPhase, RendererState
from .upnp_description import add_plex_stream_service, add_stream_service_sentinels, avtransport_response_fields, description_xml, get_control_device_info_response_fields, new_tunein_stream_service, playqueue_response_fields, scpd_xml, soap_response, stream_mask_with_plex, stream_services_capability

LOG = logging.getLogger(__name__)
PLEX_CALLBACK_URL = "com.linkplay.wiimhome://callback?category=oauth&service=plex"
PLEX_CALLBACK_URL_ENCODED = "com.linkplay.wiimhome%3A%2F%2Fcallback%3Fcategory%3Doauth%26service%3Dplex"

DEFAULT_EQ_PLUGIN = "http://moddevices.com/plugins/caps/EqNp"
DEFAULT_EQ_BAND_FREQS = {
    "a": 30.0,
    "b": 62.5,
    "c": 125.0,
    "d": 250.0,
    "e": 500.0,
    "f": 1000.0,
    "g": 2000.0,
    "h": 4000.0,
    "i": 8000.0,
    "j": 16000.0,
}
LOG_BODY_LIMIT = 5000


@dataclass(frozen=True)
class ParsedCommand:
    """Normalized representation of a Linkplay-style HTTP command."""

    name: str
    args: tuple[str, ...] = ()


@dataclass
class EventSubscriber:
    """UPnP event subscription tracked for NOTIFY callbacks."""

    sid: str
    callback_url: str
    service: str
    seq: int = 0
    peer_ip: str = ""
    expires_at: float = float("inf")
    dirty: bool = False
    task: asyncio.Task | None = field(default=None, repr=False, compare=False)


class SourcePlaybackError(BackendError):
    """Source resolution/readiness failed without a backend control failure."""

    def __init__(self, message: str, category: str = "source_unready") -> None:
        super().__init__(message)
        self.category = category


@dataclass
class StationRecovery:
    """One owner selection's finite retry budget, including successful retries."""

    generation: int
    queue: PlaybackQueue
    item: QueueItem
    index: int
    url: str
    backend_url: str
    attempts: int = 0
    invalidated: bool = False
    category: str = "unexpected_idle"


def parse_command(command: str) -> ParsedCommand:
    """Parse the HTTP ``command`` query parameter into a stable command name.

    Args:
        command: Raw command value from ``/httpapi.asp``.

    Returns:
        ``ParsedCommand`` with URL-bearing arguments decoded where needed.
    """

    if command.startswith("setPlayerCmd:play:"):
        return ParsedCommand("play", (unquote(command.removeprefix("setPlayerCmd:play:")),))
    if command.startswith("setPlayerCmd:playlist:"):
        rest = command.removeprefix("setPlayerCmd:playlist:")
        url, _, index = rest.rpartition(":")
        return ParsedCommand("playlist", (unquote(url), index or "0"))
    if command.startswith("setPlayerCmd:m3u:play:"):
        return ParsedCommand("m3u_play", (unquote(command.removeprefix("setPlayerCmd:m3u:play:")),))
    if command.startswith("setPlayerCmd:seek:"):
        return ParsedCommand("seek", (command.rsplit(":", 1)[-1],))
    if command.startswith("setPlayerCmd:vol:"):
        return ParsedCommand("volume", (command.rsplit(":", 1)[-1],))
    if command.startswith("setPlayerCmd:mute:"):
        return ParsedCommand("mute", (command.rsplit(":", 1)[-1],))
    if command in {"setPlayerCmd:pause", "setPlayerCmd:resume", "setPlayerCmd:onepause", "setPlayerCmd:stop"}:
        return ParsedCommand(command.rsplit(":", 1)[-1])
    return ParsedCommand(command)


class HttpApi:
    """aiohttp control surface for HTTP API, UPnP stubs, and app events.

    The class owns app-facing queue state, EQ presets, service capability
    experiments, and event subscribers. It delegates actual playback and DSP to
    ``MpvBackend`` and serializes visible state through ``RendererState``.
    """

    def __init__(self, cfg: Config, state: RendererState, mpv: MpvBackend, *, on_rename: Callable[[], Awaitable[None]] | None = None) -> None:
        """Create an API handler bound to config, renderer state, and mpv."""

        self.cfg = cfg
        self.state = state
        if state.phase == PlaybackPhase.ERROR or getattr(mpv, "gain_confirmed", True) is False:
            state.gain_confirmed = False
        self.mpv = mpv
        self.on_rename = on_rename
        self.queue_url = ""
        self.queue_title = ""
        self.queue_name = "CurrentQueue"
        self.queue_context = ""
        self.queue_resolved_url = ""
        self.queue_source = ""
        self.queue_search_url = ""
        self.queue_pic_url = ""
        self.queue_quality = ""
        self.queue_stream_type = ""
        self.queue_content_type = ""
        self.queue_is_live = True
        self.queue_duration_sec = 0
        self._backend_lock = asyncio.Lock()
        self._gain_command_lock = asyncio.Lock()
        self._gain_operations = 0
        self._gain_uncertain = not self.state.gain_confirmed
        self._proxy_sources: dict[str, int] = {}
        self._proxy_semaphore = asyncio.Semaphore(4)
        self._state_monitor_task: asyncio.Task | None = None
        self._app_users = 0
        self._monitor_interval = 0.5
        self._queue_advance_task: asyncio.Task | None = None
        self._station_recovery: StationRecovery | None = None
        self._station_recovery_task: asyncio.Task | None = None
        self._station_recovery_loading = False
        self._station_recovery_delays = (0.5, 1.0, 2.0)
        self._playback_offset_sec = 0
        self._pending_seek_target: int | None = None
        self._pending_seek_generation = 0
        self._seek_tasks: set[asyncio.Task] = set()
        self._seek_timeout = 5.0
        self._seek_poll_interval = 0.05
        self._seek_closed = False
        self._dsp_command_lock = asyncio.Lock()
        self._dsp_backend_confirmed = False
        self._dsp_backend_attempted = False
        self._dsp_state_uncertain = False
        self._dsp_active_confirmed = False
        self._dsp_library_committed = False
        self.eq_bands_by_plugin: dict[str, list[dict[str, object]]] = {}
        self.eq_source_bands_by_plugin: dict[str, dict[str, list[dict[str, object]]]] = {}
        self.eq_names_by_plugin: dict[str, str] = {}
        self.eq_channel_by_plugin: dict[str, str] = {}
        self.eq_active_plugin_uri = DEFAULT_EQ_PLUGIN
        self.eq_active_source_name = ""
        self.eq_enabled = True
        self.eq_presets: dict[str, dict[str, list[dict[str, object]]]] = {}
        self.eq_current_preset = ""
        self._load_persisted_eq_presets()
        self.event_subscribers: dict[str, EventSubscriber] = {}
        self._event_clock = time.monotonic
        self._event_retry_base = 0.5
        self._event_semaphore = asyncio.Semaphore(8)
        self._event_limit_total = 64
        self._event_limit_peer = 8
        self._pending_dsp_restore = load_active_dsp(cfg.state_dir)
        desired_balance = (self._pending_dsp_restore["balance"]
                           if self._pending_dsp_restore is not None else cfg.channel_balance)
        self.cfg.channel_balance = self._normalize_channel_balance(str(desired_balance))
        # Configuration is desired state until the startup transaction confirms it.
        self.channel_balance = "0.0"
        # Loading the library never activates it. A separate, strictly validated
        # owned snapshot supplies restart intent, published only after readback.
        stage_dsp = getattr(self.mpv, "stage_initial_dsp", None)
        if callable(stage_dsp):
            if self._pending_dsp_restore is None:
                self._pending_dsp_restore = self._active_dsp_snapshot()
                self._pending_dsp_restore["balance"] = float(self.cfg.channel_balance)
            desired = self._pending_dsp_restore
            stage_dsp(desired["balance"], self._snapshot_eq_filters(desired))
        if self._pending_dsp_restore is not None:
            self._dsp_state_uncertain = True
        self.device_name_override = ""
        self.plex_client_id = self._load_or_create_plex_client_id()
        self.plex_user_info = self._load_plex_user_info()

    def app(self) -> web.Application:
        """Build the aiohttp application and route table."""

        app = web.Application()
        app.cleanup_ctx.append(self._state_monitor_context)
        app.add_routes(
            [
                web.get("/httpapi.asp", self.handle_httpapi),
                web.get("/description.xml", self.description),
                web.get("/upnp/AVTransport.xml", self.avtransport_xml),
                web.get("/upnp/rendertransportSCPD.xml", self.avtransport_xml),
                web.get("/upnp/RenderingControl.xml", self.rendering_xml),
                web.get("/upnp/rendercontrolSCPD.xml", self.rendering_xml),
                web.get("/upnp/ConnectionManager.xml", self.connmanager_xml),
                web.get("/upnp/renderconnmgrSCPD.xml", self.connmanager_xml),
                web.get("/upnp/PlayQueue.xml", self.playqueue_xml),
                web.get("/upnp/PlayQueueSCPD.xml", self.playqueue_xml),
                web.get("/icons/{name}", self.icon),
                web.get("/proxy/audio", self.proxy_audio),
                web.get("/deezer", self.deezer_root),
                web.get("/deezer/{tail:.*}", self.deezer_api),
                web.get("/upnp/control/{service}", self.soap),
                web.post("/upnp/control/{service}", self.soap),
                web.post("/upnp/transport", self.soap),
                web.post("/upnp/rendering", self.soap),
                web.route("SUBSCRIBE", "/upnp/event/{service}", self.event_subscribe),
                web.route("UNSUBSCRIBE", "/upnp/event/{service}", self.event_unsubscribe),
            ]
        )
        return app

    async def _state_monitor_context(self, app):
        """Share one backend observer between all configured HTTP listeners."""
        self._app_users += 1
        if self._state_monitor_task is None:
            self._seek_closed = False
            self._state_monitor_task = asyncio.create_task(self._monitor_backend())
        try:
            yield
        finally:
            self._app_users -= 1
            if self._app_users == 0 and self._state_monitor_task is not None:
                # Invalidate before any cleanup await. An in-flight load keeps
                # the backend lock until its actual identity has been recorded.
                self._invalidate_station_recovery()
                self._seek_closed = True
                self._pending_seek_generation += 1
                self._pending_seek_target = None
                seek_tasks = list(self._seek_tasks)
                for seek_task in seek_tasks:
                    seek_task.cancel()
                if seek_tasks:
                    await asyncio.gather(*seek_tasks, return_exceptions=True)
                recovery_task = self._station_recovery_task
                event_tasks = [subscriber.task for subscriber in self.event_subscribers.values() if subscriber.task and not subscriber.task.done()]
                self.event_subscribers.clear()
                for event_task in event_tasks:
                    event_task.cancel()
                if event_tasks:
                    await asyncio.gather(*event_tasks, return_exceptions=True)
                task, self._state_monitor_task = self._state_monitor_task, None
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                if recovery_task is not None:
                    await asyncio.gather(recovery_task, return_exceptions=True)
                    if self._station_recovery_task is recovery_task:
                        self._station_recovery_task = None
                if self._queue_advance_task is not None:
                    task, self._queue_advance_task = self._queue_advance_task, None
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    async def _monitor_backend(self) -> None:
        """Observe EOF/failure even when no client is polling status."""
        while True:
            await asyncio.sleep(self._monitor_interval)
            self._prune_expired_subscribers()
            if self.state.phase in {PlaybackPhase.COMMAND_ACCEPTED, PlaybackPhase.LOADING, PlaybackPhase.STOPPED, PlaybackPhase.ERROR}:
                continue
            try:
                await self.sync_mpv_state()
            except (BackendError, OSError, TimeoutError):
                self._mark_backend_failure()

    @property
    def _play_generation(self) -> int:
        return self.state.generation

    @_play_generation.setter
    def _play_generation(self, value: int) -> None:
        self.state.generation = value

    def _accept_playback_command(self, intent: str) -> int:
        self._invalidate_station_recovery()
        task = self._queue_advance_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            self._queue_advance_task = None
            task.cancel()
        self._pending_seek_generation += 1
        self._pending_seek_target = None
        self._proxy_sources.clear()
        return self.state.accept_command(intent)

    def _invalidate_station_recovery(self) -> None:
        recovery, self._station_recovery = self._station_recovery, None
        if recovery is not None:
            recovery.invalidated = True
        task = self._station_recovery_task
        if (task is not None and task is not asyncio.current_task() and not task.done()
                and not self._station_recovery_loading):
            task.cancel()

    def _arm_station_recovery(self) -> None:
        item = self.state.queue.current
        if item and item.stream_type == "station" and item.duration == 0:
            self._station_recovery = StationRecovery(
                self.state.generation, self.state.queue, item, self.state.queue.index, item.url, self.state.backend_url)

    def _station_recovery_is_current(self, recovery: StationRecovery) -> bool:
        return (not recovery.invalidated and self._station_recovery is recovery
                and recovery.generation == self.state.generation and self.state.intent == "play"
                and self.state.queue is recovery.queue and self.state.queue.index == recovery.index
                and self.state.queue.current is recovery.item
                and recovery.item.stream_type == "station" and recovery.item.duration == 0
                and recovery.item.url == self.state.current_url == recovery.url)

    def _station_source_has_audio(self, recovery: StationRecovery, snapshot: dict) -> bool:
        """Require the captured actual source, not a stale/foreign active path."""
        return (self._mpv_snapshot_matches_source(snapshot, recovery.backend_url)
                and snapshot.get("idle-active") is False and snapshot.get("pause") is False
                and snapshot.get("eof-reached") is not True and snapshot.get("paused-for-cache") is not True
                and self._mpv_snapshot_has_audio(snapshot))

    def _mark_backend_failure(self) -> None:
        self._invalidate_station_recovery()
        self.state.gain_confirmed = False
        self.state.mark_error("Backend control failed")
        LOG.warning("playback failure category=backend_control generation=%s", self.state.generation)
        self._schedule_event_notify()

    def _recover_station_after_end(self, category: str) -> None:
        recovery = self._station_recovery
        if recovery is None or not self._station_recovery_is_current(recovery):
            self.state.phase = PlaybackPhase.STOPPED
            return
        self.state.phase = PlaybackPhase.BUFFERING
        self.state.last_error = ("Station source ended; retry pending" if category == "source_eof"
                                 else "Station source idle; retry pending")
        if self._station_recovery_task is None or self._station_recovery_task.done():
            recovery.category = category
            if recovery.attempts >= len(self._station_recovery_delays):
                self._exhaust_station_recovery(recovery)
            else:
                self._station_recovery_task = asyncio.create_task(self._retry_station(recovery))

    def _exhaust_station_recovery(self, recovery: StationRecovery) -> None:
        self.state.mark_error("Station source unavailable: recovery exhausted")
        LOG.warning("station recovery category=%s outcome=exhausted generation=%s attempts=%s",
                    recovery.category, recovery.generation, recovery.attempts)
        self._schedule_event_notify()

    async def _retry_station(self, recovery: StationRecovery) -> None:
        try:
            while self._station_recovery_is_current(recovery):
                if recovery.attempts >= len(self._station_recovery_delays):
                    self._exhaust_station_recovery(recovery)
                    return
                await asyncio.sleep(self._station_recovery_delays[recovery.attempts])
                if not self._station_recovery_is_current(recovery):
                    return
                try:
                    async with self._backend_lock:
                        if not self._station_recovery_is_current(recovery):
                            return
                        snapshot = await self.mpv.snapshot()
                    if not self._station_recovery_is_current(recovery):
                        return
                    if snapshot.get("backend-error"):
                        raise BackendError("Backend control failed before recovery")
                    if self._station_source_has_audio(recovery, snapshot):
                        self._apply_backend_snapshot(snapshot)
                        LOG.info("station recovery category=%s outcome=already_active generation=%s attempts=%s",
                                 recovery.category, recovery.generation, recovery.attempts)
                        self._schedule_event_notify()
                        return
                    recovery.attempts += 1
                    LOG.info("station recovery category=%s generation=%s attempt=%s",
                             recovery.category, recovery.generation, recovery.attempts)
                    await self._load_playback_url(recovery.url, recovery.generation, recovery=recovery)
                except SourcePlaybackError:
                    if not self._station_recovery_is_current(recovery):
                        return
                    self.state.phase = PlaybackPhase.BUFFERING
                    self.state.last_error = "Station source unavailable; retry pending"
                    self._schedule_event_notify()
                    continue
                except (BackendError, OSError, TimeoutError):
                    # A failed control channel is not evidence to reload a source.
                    if self._station_recovery_is_current(recovery):
                        self._mark_backend_failure()
                    return
                if self._station_recovery_is_current(recovery):
                    self.state.last_error = ""
                    LOG.info("station recovery category=%s outcome=recovered generation=%s attempts=%s",
                             recovery.category, recovery.generation, recovery.attempts)
                    self._schedule_event_notify()
                return
        finally:
            if self._station_recovery_task is asyncio.current_task():
                self._station_recovery_task = None

    async def sync_mpv_state(self) -> None:
        """Apply an observation only to the generation that requested it."""
        generation = self.state.generation
        try:
            async with self._backend_lock:
                snap = await self.mpv.snapshot()
        except (BackendError, OSError, TimeoutError):
            if generation == self.state.generation:
                self._mark_backend_failure()
            return
        if generation != self.state.generation:
            return
        self._apply_backend_snapshot(snap)

    def _apply_backend_snapshot(self, snap: dict) -> None:
        """One source of truth for all HTTP, SOAP and event serializers."""
        old_phase = self.state.phase
        if self.state.phase in {PlaybackPhase.COMMAND_ACCEPTED, PlaybackPhase.LOADING}:
            return
        if snap.get("backend-error"):
            self._mark_backend_failure()
            return
        if self.state.phase == PlaybackPhase.ERROR:
            return
        observed_url = snap.get("path")
        recovery = self._station_recovery
        expected_url = self.state.backend_url
        if not expected_url and recovery and self._station_recovery_is_current(recovery):
            expected_url = recovery.backend_url
        if expected_url and observed_url and not self._mpv_snapshot_matches_source(snap, expected_url):
            return
        tracks = snap.get("track-list")
        selected_audio = isinstance(tracks, list) and any(
            isinstance(track, dict) and track.get("type") == "audio" and track.get("selected", True)
            for track in tracks)
        if self.state.phase not in {PlaybackPhase.COMMAND_ACCEPTED, PlaybackPhase.LOADING}:
            # An expanded playlist is one canonical queue resource. Native
            # entry EOF can advance within it; only whole-player idle ends
            # that resource. Direct files retain their existing EOF behavior.
            expanded_playlist = bool(expected_url and observed_url and observed_url != expected_url)
            if snap.get("idle-active") is True or (snap.get("eof-reached") is True and not expanded_playlist):
                if not (self.state.intent == "pause" and not self.state.backend_url):
                    self.state.phase = PlaybackPhase.STOPPED
                    self.state.curpos = 0
                    self.state.backend_url = ""
                    self.state.backend_original_url = ""
                    selected = self.state.queue.current
                    station = bool(selected and selected.stream_type == "station" and selected.duration == 0)
                    if station and old_phase in {PlaybackPhase.PLAYING, PlaybackPhase.BUFFERING} and self.state.intent == "play":
                        self._recover_station_after_end("source_eof" if snap.get("eof-reached") is True else "unexpected_idle")
                    elif not station and old_phase in {PlaybackPhase.PLAYING, PlaybackPhase.BUFFERING} and self.state.intent == "play" and self.state.queue.index < self.state.queue.count:
                        if self._queue_advance_task is None or self._queue_advance_task.done():
                            self._queue_advance_task = asyncio.create_task(self._advance_queue_after_eof(self.state.generation, self.state.queue))
            elif self.state.intent == "stop":
                # A late active backend snapshot cannot undo an owner stop.
                pass
            elif snap.get("pause") is True:
                self.state.phase = PlaybackPhase.PAUSED
            elif (snap.get("paused-for-cache") is True
                  or (snap.get("core-idle") is True and not selected_audio)):
                self.state.phase = PlaybackPhase.BUFFERING
            elif self._mpv_snapshot_has_audio(snap):
                self.state.phase = PlaybackPhase.PLAYING
                recovery = self._station_recovery
                if (recovery and self._station_recovery_is_current(recovery)
                        and self._station_source_has_audio(recovery, snap)):
                    self.state.backend_url = recovery.backend_url
                    self.state.backend_original_url = recovery.url
                    self.state.last_error = ""
        if isinstance(snap.get("playback-time"), (int, float)) and self.state.phase != PlaybackPhase.STOPPED:
            playback_time = self._adjusted_playback_time(snap)
            # Always publish observations, never a requested seek position.
            self.state.curpos = playback_time
        if self._pending_seek_target is not None and self.state.phase in {PlaybackPhase.PLAYING, PlaybackPhase.PAUSED}:
            self.state.mark_transitioning()
        if self.queue_is_live:
            self.state.totlen = 0
        elif self.queue_duration_sec > 0:
            self.state.totlen = self.queue_duration_sec
        elif (type(snap.get("duration")) in {int, float} and math.isfinite(snap["duration"])
                and snap["duration"] > 0):
            self.state.totlen = int(snap["duration"]) + self._playback_offset_sec
        selected = self.state.queue.current
        if (snap.get("media-title") and self.state.intent != "stop"
                and (not selected or not selected.title or selected.title_is_fallback)):
            title = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", "\ufffd", str(snap["media-title"]))
            changed = self.state.title != title
            self.state.title = title
            if selected:
                selected.title = title
                selected.title_is_fallback = True
                self.queue_title = title
                if not selected.metadata:
                    self.state.track_metadata = self._didl_metadata(
                        selected.resolved_url or selected.url, title, self.queue_source, self.queue_pic_url,
                        self.queue_is_live, artist=selected.artist, album=selected.album, item_id=selected.id or None)
                    if selected.duration > 0:
                        self.state.track_metadata = self._metadata_with_duration(self.state.track_metadata, int(selected.duration))
                self.queue_context = serialize_queue_context(self.state.queue)
            if changed:
                self._schedule_event_notify()
        if self.state.phase != old_phase:
            self._schedule_event_notify()

    async def _transport_command(self, command: str) -> None:
        """Serialize mutations and verify the actual backend acknowledgement."""
        previous_phase, previous_intent = self.state.phase, self.state.intent
        intent = {"pause": "pause", "resume": "play", "stop": "stop"}[command]
        generation = self._accept_playback_command(intent)
        self.state.phase = PlaybackPhase.COMMAND_ACCEPTED
        try:
            async with self._backend_lock:
                if generation != self.state.generation:
                    raise BackendError("Playback command superseded")
                pause_unloaded = command == "pause" and self.state.backend_original_url != self.state.current_url
                effect = "stop" if pause_unloaded else command
                operation = getattr(self.mpv, effect)()
                if effect == "resume":
                    await self._gain_reconciling_call(operation)
                else:
                    await self._backend_call(operation)
                snapshot = await self.mpv.snapshot()
                if generation != self.state.generation:
                    raise BackendError("Playback command superseded")
                if snapshot.get("backend-error"):
                    self.state.mark_error()
                    raise BackendError("Backend unavailable after command")
                if effect == "stop" and snapshot.get("idle-active") is not True:
                    raise BackendError("Backend did not stop")
                if effect == "pause" and snapshot.get("pause") is not True:
                    raise BackendError("Backend did not pause")
                if command == "resume" and (snapshot.get("pause") is not False or snapshot.get("idle-active") is not False):
                    raise BackendError("Backend did not resume")
                if effect in {"pause", "resume"} and not self._mpv_snapshot_matches_source(snapshot, self.state.backend_url):
                    self.state.mark_error("Playback source changed before control confirmation")
                    self._schedule_event_notify()
                    raise BackendError("Playback source not confirmed after control")
                if effect == "stop":
                    self.state.backend_url = ""
                    self.state.backend_original_url = ""
            if generation != self.state.generation:
                raise BackendError("Playback command superseded")
            if command == "stop":
                self.state.mark_stop()
            elif command == "pause":
                self.state.mark_pause()
            else:
                self.state.phase = PlaybackPhase.PLAYING
                self._arm_station_recovery()
            self._apply_backend_snapshot(snapshot)
        except (BackendError, OSError, TimeoutError):
            if generation == self.state.generation and self.state.phase != PlaybackPhase.ERROR:
                if previous_phase in {PlaybackPhase.COMMAND_ACCEPTED, PlaybackPhase.LOADING, PlaybackPhase.BUFFERING}:
                    self.state.mark_error()
                else:
                    self.state.phase, self.state.intent = previous_phase, previous_intent
            raise
        self._schedule_event_notify()

    async def _seek_command(self, seconds: int, *, relative: bool = False) -> None:
        """Acknowledge only a bounded, source-fenced actual-position readback.

        Unknown-duration files are allowed; explicitly marked stations are not.
        The caller owns completion, so HTTP, SOAP and Previous have the same
        success semantics and shutdown can cancel every confirmation task.
        """
        item = self.state.queue.current
        if (item and item.stream_type == "station") or self.queue_stream_type == "station":
            raise SoapFault(710, "Seek Mode Not Supported")
        if (self._seek_closed or self.state.phase not in {PlaybackPhase.PLAYING, PlaybackPhase.PAUSED}
                and not (self.state.phase == PlaybackPhase.BUFFERING and self._pending_seek_target is not None)):
            raise SoapFault(701, "Transition Not Available")
        if type(seconds) is not int:
            raise SoapFault(402, "Invalid Args")
        if not relative:
            self._bounded_seek_target(seconds, self._seek_duration(), relative=False)
        source_url = self.state.current_url
        intent = self.state.intent
        generation = self._accept_playback_command(intent)
        seek_generation = self._pending_seek_generation
        task = asyncio.create_task(self._perform_seek(
            seconds, relative, source_url, intent, generation, seek_generation))
        self._seek_tasks.add(task)
        try:
            await task
        finally:
            self._seek_tasks.discard(task)

    def _seek_duration(self, snapshot: dict | None = None) -> float:
        """Use full metadata or fresh duration, never a truncated display bound."""
        item = self.state.queue.current
        duration = (item.duration if item else 0) or self.queue_duration_sec
        if duration:
            return float(duration)
        if snapshot is not None:
            raw = snapshot.get("duration")
            if type(raw) in {int, float} and math.isfinite(raw) and raw > 0:
                return float(raw + self._playback_offset_sec)
            return float(self.state.totlen)
        # A display length is integral, so its apparent end may still be a
        # valid target (e.g. second300 of a300.8-second file). Read it fresh.
        return 0.0

    @staticmethod
    def _bounded_seek_target(seconds: int, duration: float, *, relative: bool) -> int:
        # Absolute targets are rejected, never silently moved. Relative buttons
        # clamp to the last whole second before a known end (or zero).
        if relative:
            seconds = max(0, seconds)
            if duration > 0:
                seconds = min(seconds, max(0, math.ceil(duration) - 1))
        if seconds < 0 or (duration > 0 and seconds >= duration):
            raise SoapFault(711, "Illegal Seek Target")
        return seconds

    async def _perform_seek(
        self, seconds: int, relative: bool, source_url: str, intent: str,
        generation: int, seek_generation: int,
    ) -> None:
        """Own preflight, mutation and confirmation for one accepted command."""
        mutation_started = False

        def current() -> bool:
            return (not self._seek_closed and generation == self.state.generation
                    and seek_generation == self._pending_seek_generation
                    and self.state.current_url == source_url and self.state.intent == intent)

        try:
            async with asyncio.timeout(self._seek_timeout), self._backend_lock:
                if not current():
                    raise BackendError("Playback command superseded")
                snapshot = await self.mpv.snapshot()
                if not current():
                    raise BackendError("Playback command superseded")
                if self.state.phase in {PlaybackPhase.STOPPED, PlaybackPhase.ERROR}:
                    raise BackendError("Playback ended before seek")
                self._apply_backend_snapshot(snapshot)
                if self.state.phase in {PlaybackPhase.STOPPED, PlaybackPhase.ERROR}:
                    raise BackendError("Playback ended before seek")
                if not self.state.backend_url or not self._mpv_snapshot_matches_source(snapshot, self.state.backend_url):
                    raise BackendError("Source not confirmed before seek")
                if self.state.phase not in {PlaybackPhase.PLAYING, PlaybackPhase.PAUSED}:
                    raise SoapFault(701, "Transition Not Available")
                seek_source = self._seek_source_token(snapshot)
                previous_pos = self._adjusted_playback_time(snapshot)
                target = self._bounded_seek_target(
                    previous_pos + seconds if relative else seconds,
                    self._seek_duration(snapshot), relative=relative)
                direct_url = self.queue_resolved_url or self.state.resolved_url or source_url
                # A segment starts at offset, so earlier targets require the
                # original resource. Later absolute targets use local seconds.
                before_segment = target < self._playback_offset_sec
                range_reload = not before_segment and self._should_reload_for_large_seek(target, previous_pos)
                mutation_started = True
                if before_segment or range_reload:
                    seek_source = None  # A reload intentionally establishes a new resource.
                    if before_segment:
                        playback_url = self._backend_playback_url(source_url, direct_url)
                        options = dict(self._mpv_playback_options(source_url, direct_url), start=str(target))
                        offset = 0
                    else:
                        playback_url = self._seek_audio_proxy_url(direct_url, target)
                        options = self._mpv_seek_reload_options()
                        offset = target
                    await self._gain_reconciling_call(self.mpv.loadfile(playback_url, options))
                    # Preserve actual identity even when a newer owner command
                    # arrived during this locked, acknowledged backend mutation.
                    self.state.backend_url = playback_url
                    self.state.backend_original_url = source_url
                    self._playback_offset_sec = offset
                    if not current():
                        raise BackendError("Playback command superseded")
                    if intent == "pause":
                        await self._backend_call(self.mpv.pause())
                else:
                    await self._backend_call(self.mpv.seek(target - self._playback_offset_sec))
                if not current():
                    raise BackendError("Playback command superseded")
                if self.state.phase in {PlaybackPhase.STOPPED, PlaybackPhase.ERROR}:
                    raise BackendError("Playback ended during seek")
                backend_url, offset = self.state.backend_url, self._playback_offset_sec
                self._pending_seek_target = target
                self.state.mark_transitioning()
            self._schedule_event_notify(("rendertransport1", "AVTransport"))
            snapshot = await self._wait_for_seek_ready(current, target, backend_url, offset, intent, seek_source)
            if (not current() or self.state.backend_url != backend_url or self._playback_offset_sec != offset
                    or self.state.phase in {PlaybackPhase.STOPPED, PlaybackPhase.ERROR}):
                raise BackendError("Playback command superseded or ended")
            self._pending_seek_target = None
            self._apply_backend_snapshot(snapshot)
            LOG.info("Seek confirmed target=%s actual=%s status=%s", target, self.state.curpos, self.state.status)
            self._schedule_event_notify(("rendertransport1", "AVTransport"))
        except (TimeoutError, asyncio.CancelledError):
            if current() and mutation_started and self.state.phase not in {PlaybackPhase.STOPPED, PlaybackPhase.ERROR}:
                self.state.mark_error("Seek operation interrupted before confirmation")
                self._schedule_event_notify(("rendertransport1", "AVTransport"))
            raise
        except (BackendError, OSError):
            if current() and self.state.phase not in {PlaybackPhase.STOPPED, PlaybackPhase.ERROR}:
                if mutation_started:
                    self.state.mark_error("Seek position not confirmed")
                    self._schedule_event_notify(("rendertransport1", "AVTransport"))
            raise
        finally:
            if generation == self.state.generation and seek_generation == self._pending_seek_generation:
                self._pending_seek_target = None

    @staticmethod
    def _seek_source_token(snapshot: dict) -> tuple[object, object]:
        """Fence both the actual file and repeated URLs in a native playlist."""
        playlist = snapshot.get("playlist")
        active = [entry for entry in playlist if isinstance(entry, dict)
                  and (entry.get("current") is True or entry.get("playing") is True)] if isinstance(playlist, list) else []
        entry_id = active[0].get("id") if len(active) == 1 else None
        return snapshot.get("path"), entry_id

    @staticmethod
    def _seek_proxy_content_range(status: int, headers: object, requested: str, offset: int) -> str | None:
        """Verify shifted bytes and expose segment-local coordinates to mpv.

        This only confirms the HTTP byte range. Bitrate-derived time offsets
        remain an opt-in approximation, especially for VBR/container media.
        """
        if status != 206 or headers.get("Content-Encoding", "identity").lower() != "identity":
            return None
        response = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", headers.get("Content-Range", ""))
        request = re.fullmatch(r"bytes=([0-9]+)-([0-9]*)", requested)
        if response is None or request is None:
            return None
        start, end, total = map(int, response.groups())
        if start != int(request[1]) or start < offset or not start <= end < total:
            return None
        if request[2] and end > int(request[2]):
            return None
        length = headers.get("Content-Length")
        if length is not None and (not length.isdecimal() or int(length) != end - start + 1):
            return None
        return f"bytes {start - offset}-{end - offset}/{total - offset}"

    async def handle_httpapi(self, request: web.Request) -> web.Response:
        """Handle ``/httpapi.asp`` commands and serialize text/JSON results."""

        command = request.query.get("command", "")
        parsed = parse_command(command)
        LOG.info(
            "HTTP API remote=%s host=%s path=%s ua=%s command=%s parsed=%s",
            request.remote,
            request.host,
            self._redact_sensitive_text(request.path_qs),
            request.headers.get("User-Agent", ""),
            self._redact_sensitive_text(command),
            self._redact_sensitive_text(str(parsed)),
        )
        forced_failure_commands = {"GetAcousticCapability"}
        if os.environ.get("WIIM_HOME_AUDIO_OUTPUT_CAPABILITY_MODE") == "fail":
            forced_failure_commands.update({"getSpdifOutMaxCap", "getCoaxOutMaxCap", "GetFadeFeature", "getSpdifOutSwitchDelayMs"})
        force_failure = parsed.name in forced_failure_commands or (
            (
                parsed.name.startswith("getAudioOutputVrmsSupportList")
                or parsed.name.startswith("getAuxVoltageSupportList")
            )
            and os.environ.get("WIIM_HOME_AUDIO_OUTPUT_CAPABILITY_MODE") == "fail"
        )
        if force_failure and os.environ.get("WIIM_HOME_ACOUSTIC_CAPABILITY_MODE") == "fail":
            LOG.info("HTTP API forced failure for %s", parsed.name)
            self._log_http_detail(request, "", 404, "Failed")
            return web.Response(status=404, text="Failed", content_type="text/plain")
        try:
            payload = await self.execute(parsed, request.remote or self.cfg.resolved_advertise_ip)
        except (BackendError, OSError, TimeoutError):
            LOG.warning("backend command failed")
            return web.Response(status=502, text="Failed", content_type="text/plain")
        except (SoapFault, ValueError, TypeError):
            return web.Response(status=400, text="Failed", content_type="text/plain")
        except Exception:
            LOG.exception("command failed")
            return web.Response(status=500, text="Failed", content_type="text/plain")
        if isinstance(payload, dict):
            LOG.info("HTTP API response=json keys=%s", ",".join(payload.keys()))
            self._log_http_detail(request, "", 200, json.dumps(payload, separators=(",", ":")))
            return web.json_response(payload, status=400 if payload.get("error") == "Failed" else 200)
        LOG.info("HTTP API response=text")
        self._log_http_detail(request, "", 200, str(payload))
        return web.Response(text=str(payload), content_type="text/plain")

    async def execute(self, parsed: ParsedCommand, remote: str) -> dict[str, object] | str:
        """Execute a parsed HTTP command.

        Args:
            parsed: Normalized command from ``parse_command``.
            remote: Client address used where a command needs peer context.

        Returns:
            Either a JSON-serializable mapping or a plain text response.

        Side Effects:
            May mutate renderer state, persist settings, schedule UPnP events,
            or issue mpv playback/DSP commands.
        """

        name = parsed.name
        if name.startswith("EQ") or name == "getChannelBalance" or name.startswith("setChannelBalance:"):
            return await self._run_dsp_command(name)
        if name in {"getStatusEx", "getStatus"}:
            await self.sync_mpv_state()
            return self._with_experimental_sources(self.state.as_status_ex(self.cfg.resolved_advertise_ip))
        if name.startswith("getStatus:ip:"):
            await self.sync_mpv_state()
            return self._with_experimental_sources(self.state.as_status_ex(self.cfg.resolved_advertise_ip))
        if name in {"getPlayerStatus", "getPlayerStatusEx"}:
            await self.sync_mpv_state()
            return self.state.as_player_status()
        if name == "getMetaInfo":
            return self.state.as_meta_info()
        if name == "getPresetInfo":
            presets = configured_presets(self.cfg)
            # Keep the legacy alias while exposing the real client's field.
            return {"preset_num": str(len(presets)), "preset_list": presets, "presets": presets}
        if name.startswith("MCUKeyShortClick:"):
            match = re.fullmatch(r"MCUKeyShortClick:([1-9][0-9]{0,5})(?::([1-9][0-9]{0,5}))?", name)
            if not match:
                raise ValueError("Invalid preset selection")
            number = int(match.group(1))
            # A configured favorite represents one direct target, not a stored
            # multi-track provider playlist. Reject unsupported indexes honestly.
            if match.group(2) is not None and int(match.group(2)) != 1:
                raise ValueError("Preset contains one direct target")
            presets = configured_presets(self.cfg)
            if number > len(presets):
                raise ValueError("Unknown preset slot")
            selected = presets[number - 1]
            queue = PlaybackQueue(items=[QueueItem(
                url=selected["url"], title=selected["name"], source=selected["source"],
                artwork=selected.get("picurl", ""), id=f"preset:{number}",
            )], index=1)
            queue.validate()
            self.state.queue = queue
            self._activate_queue_item()
            await self._play_url(queue.current.url)
            self._schedule_event_notify()
            return "OK"
        if name == "getPowerState":
            return {"power": "on", "status": "on"}
        if name == "getDeviceInfo":
            return self._with_experimental_sources(self.state.as_status_ex(self.cfg.resolved_advertise_ip))
        if name == "getNewVersion":
            return {"status": "0", "version": self.state.as_status_ex(self.cfg.resolved_advertise_ip)["firmware"]}
        if name == "getDebugInfo":
            return {"status": "OK", "debug": "fake-linkplay"}
        if name.startswith("SetStreamServicesCapability:Active:") or name.startswith("SetStreamServicesCapability:Deactive:"):
            return {"error": "Failed", "command": name}
        if self._is_stream_services_capability_command(name):
            return self._stream_services_capability()
        if name == "getMvRemoteUpdateStart":
            return {"error": "Failed", "command": name}
        if name in {"getMvRemoteUpdateStartCheck", "getMvRemoteUpdateStatus"}:
            return {"status": "0", "update": "none", "progress": "0"}
        if name in {"setTimezone", "setTuneinLocation"} or name.startswith("setTimezone:") or name.startswith("setTuneinLocation:"):
            return {"error": "Failed", "command": name}
        if name.startswith("setDeviceName:") or name.startswith("setHexDeviceName:"):
            requested_name = self._decode_device_name_command(name)
            if not requested_name or len(requested_name) > 128 or re.search(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff\ufffe\uffff]", requested_name):
                raise ValueError("Invalid device name")
            if requested_name:
                self.device_name_override = requested_name
                self.cfg.friendly_name = requested_name
                self.cfg.mdns_instance_name = requested_name
                self._persist_runtime_value("device_name", requested_name)
                if self.on_rename is not None:
                    await self.on_rename()
                LOG.info("Persisted WiiM Home device rename request name=%s", requested_name)
                self._schedule_event_notify()
            return "OK"
        if name == "getbtstatus":
            return {"status": "0", "bt_status": "0", "connect": "0"}
        if name == "getbthistory":
            return {"status": "0", "history": [], "list": []}
        if name == "GetAcousticCapability":
            if os.environ.get("WIIM_HOME_ACOUSTIC_CAPABILITY_MODE") == "peq":
                return {
                    "Version": "4.3",
                    "EQBlock": {"Version": "4.3"},
                    "GEQ": {"Version": "4.3"},
                    "PEQ": {"Version": "4.3", "Filters": ["LS", "PK", "HS", "LP", "HP", "OFF"]},
                    "RC": {"Version": "0", "LevelMatch": None},
                    "SoundMode": {"Version": "1.0", "Modes": [], "DefaultMode": ""},
                    "OutputDelay": {
                        "Version": "1.0",
                        "PerOutputDelay": False,
                        "EnableMicroDelay": False,
                        "MinDelayUs": 0,
                        "MaxDelayUs": 0,
                        "StepDelayUs": 1,
                    },
                    "SubLPF": {"Version": "0"},
                }
            return {
                "Version": "1.0",
                "GEQ": {"Version": "0"},
                "PEQ": {"Version": "0", "Filters": []},
                "SubLPF": {"Version": "0"},
                "EQBlock": {"Version": "0", "Blocks": []},
                "HeadphoneEQ": {"version": "0"},
                "DynamicBass": {"Version": "0", "Min": 0, "Max": 0, "Step": 1},
                "Evaluation": {"Version": "0"},
                "SoundMode": {"version": "0", "Modes": [], "DefaultMode": ""},
                "OutputDelay": {
                    "version": "0",
                    "PerOutputDelay": False,
                    "EnableMicroDelay": False,
                    "MinDelayUs": 0,
                    "MaxDelayUs": 0,
                    "StepDelayUs": 1000,
                },
                "RC": {"Version": "0", "LevelMatch": None},
            }
        if name == "getLMPFilterCapability":
            return {"version": "0"}
        if name == "getDigitalInputAudioTypeSupport":
            return "0x0"
        if name == "getModeRename":
            return {}
        if name == "getAudioInputEnable":
            return {
                "ver": "1.0",
                "audioInput": [
                    {"mode": "wifi", "enable": 1},
                ],
            }
        if name == "getAudioInputStreamState":
            return {"status": "0", "state": "idle", "source": ""}
        if name.startswith("getAudioOutputVrmsSupportList"):
            return {"ver": "1.1", "voltage": []}
        if name.startswith("getAuxVoltageSupportList"):
            return {"ver": "1.1", "voltage": []}
        if name in {"getSpdifOutMaxCap", "getCoaxOutMaxCap"}:
            return {"ver": "0", "sampleRate": "0", "bitDepth": "0", "cfgType": "", "multiList": []}
        if name == "getSpdifOutSwitchDelayMs":
            return {"status": "0", "delayMs": "0", "delay": "0"}
        if name == "GetFadeFeature":
            return {"FadeFeature": 0}
        if name == "getTotalAudioInsertState":
            return {"status": "0", "insert": "0", "state": "0"}
        if name == "getEndTrackEnable":
            return {"status": "0", "enable": "0"}
        if name == "getShutdown":
            return {"status": "0", "enable": "0", "shutdown": "0"}
        if name == "get_bt_latency":
            return {"status": "0", "latency": "0"}
        if name == "checkBackupSettingState":
            return {"status": "0", "state": "0"}
        if name == "getMvRemoteSilenceUpdateTime":
            return {"status": "0", "time": "0"}
        if name == "getNewAudioOutputHardwareMode":
            return {"status": "0", "mode": "0", "list": []}
        if name == "getDeviceNameChangeable":
            return {"status": "0", "changeable": "1"}
        if name == "getPowerModeTime":
            return {"status": "0", "time": "0", "mode": "0"}
        if name == "getSubLPF":
            return {"status": "0", "enable": "0", "freq": "0"}
        if name == "getTriggeroutStatus":
            return {"status": "0", "enable": "0", "status": "0"}
        if name == "getLowPriorityPromptDisable":
            return {"status": "0", "disable": "0"}
        if name == "Button_Enable_GET":
            return {"status": "0", "enable": "0"}
        if name == "Reload_Button_GET":
            return {"status": "0", "reload": "0"}
        if name == "LED_SWITCH_STATUS_GET":
            return {"status": "0", "enable": "0", "status": "0"}
        if name == "VOL_LED_SWITCH_GET":
            return {"status": "0", "enable": "0", "status": "0"}
        if name == "audio_cast:get_speaker_list":
            return {"status": "0", "speakers": [], "list": []}
        if name == "getStaticIpInfo":
            return {
                "wlanStaticIpEnable": 0,
                "wlanStaticIp": "0.0.0.0/0",
                "ethStaticIpEnable": 0,
                "ethStaticIp": "0.0.0.0/0",
            }
        if name == "getRoutineCapability":
            return {"status": "0", "support": "0", "routines": []}
        if name == "getAllRoutines":
            return {"status": "0", "routines": []}
        if name == "getAlexaProfile":
            return {"status": 0, "login": 0, "loggedIn": False, "profile": {}}
        if name in {"getAvsDevInfo", "getAlexaCountry", "alexaLanguageListGet", "getAsrStatus"}:
            return {"status": 0, "enable": 0, "login": 0}
        if name.startswith("get") and ("Capability" in name or "Support" in name):
            return {"error": "Failed", "command": name}
        if name in {"play", "playlist", "m3u_play"}:
            url = parsed.args[0]
            self.queue_url = url
            self.queue_title = url.rsplit("/", 1)[-1] or "Network stream"
            self.queue_source = "CustomRadio"
            self.queue_search_url = url
            self.queue_pic_url = ""
            self.state.track_source = self.queue_source
            self.state.play_medium = "STATION-NETWORK"
            self.state.album_art_uri = ""
            self.state.track_metadata = self._didl_metadata(url, self.queue_title, self.queue_source)
            self.state.queue = PlaybackQueue(items=[QueueItem(url=url,
                source=self.queue_source)], index=1)
            self._activate_queue_item()
            await self._play_url(url)
            self._schedule_event_notify()
            return "OK"
        if name in {"pause", "resume", "onepause", "stop"}:
            command = "resume" if name == "onepause" else name
            if command == "resume" and not self.state.current_url:
                raise BackendError("No media selected")
            if command == "resume" and (not self.state.backend_url or self.state.intent == "stop" or self.state.backend_original_url != self.state.current_url or self.state.phase in {PlaybackPhase.STOPPED, PlaybackPhase.ERROR}):
                await self._play_url(self.state.current_url)
            else:
                await self._transport_command(command)
            return "OK"
        if name == "seek":
            if not parsed.args or not re.fullmatch(r"-?[0-9]+", parsed.args[0]):
                raise ValueError("Invalid seek target")
            await self._seek_command(int(parsed.args[0]))
            return "OK"
        if name == "volume":
            if not parsed.args or not re.fullmatch(r"[0-9]{1,3}", parsed.args[0]):
                raise ValueError("Invalid volume")
            value = int(parsed.args[0])
            if not 0 <= value <= 100:
                raise ValueError("Invalid volume")
            await self._set_confirmed_gain(volume=value)
            return "OK"
        if name == "mute":
            if not parsed.args or parsed.args[0] not in {"0", "1"}:
                raise ValueError("Invalid mute")
            await self._set_confirmed_gain(mute=parsed.args[0] == "1")
            return "OK"
        return {"error": "Failed", "command": name}

    async def _gain_reconciling_call(self, operation) -> None:
        """Do not advertise known gain while any reconciling operation is pending."""
        self._gain_operations += 1
        self.state.gain_confirmed = False
        try:
            await self._backend_call(operation)
        except BaseException:
            self._gain_uncertain = True
            raise
        else:
            self._gain_uncertain = False
        finally:
            self._gain_operations -= 1
            self.state.gain_confirmed = self._gain_operations == 0 and not self._gain_uncertain

    async def _set_confirmed_gain(self, *, volume: int | None = None, mute: bool | None = None) -> None:
        """Publish and persist only backend-confirmed gain, in command order."""
        async with self._gain_command_lock:
            try:
                if volume is not None:
                    await self._gain_reconciling_call(self.mpv.volume(volume))
                if mute is not None:
                    await self._gain_reconciling_call(self.mpv.mute(mute))
            except (BackendError, OSError, TimeoutError, asyncio.CancelledError):
                # A write may have reached the backend before acknowledgement
                # was lost. ERROR does not assert silence or successful rollback.
                self.state.gain_confirmed = False
                self._invalidate_station_recovery()
                self.state.mark_error("Gain confirmation failed; actual output unknown; showing last confirmed settings")
                self._schedule_event_notify()
                raise
            if volume is not None:
                self.state.volume = volume
            if mute is not None:
                self.state.mute = mute
            if self.state.last_error.startswith("Gain confirmation failed;"):
                self.state.last_error = "Gain confirmed; transport requires a new command"
            # The backend changed even if storage subsequently fails. Keep its
            # confirmed readback visible, notify clients, and report the failed
            # persistence instead of promising that restart will retain it.
            self._schedule_event_notify(("rendercontrol1", "RenderingControl"))
            save_gain(self.cfg.state_dir, self.state.volume, self.state.mute)
            self.cfg.default_volume, self.cfg.default_mute = self.state.volume, self.state.mute

    async def initialize_channel_balance(self) -> None:
        """Restore the complete startup DSP graph before publishing listeners.

        The legacy method name remains for callers. Minimal test backends with
        no complete-graph hook can initialize balance only when no active state
        was saved; a saved curve is never silently replaced by that fallback.
        """
        if self._pending_dsp_restore is not None:
            await self._run_dsp_command("__initializeDSP")
        else:
            await self._run_dsp_command("setChannelBalance:" + self.cfg.channel_balance)

    def _active_dsp_snapshot(self) -> dict[str, object]:
        """Only the active context is restart state; inactive caches stay local."""
        return {
            "version": 1, "plugin": self.eq_active_plugin_uri,
            "source": self.eq_active_source_name, "enabled": self.eq_enabled,
            "name": self._eq_display_name(self.eq_active_plugin_uri, self._active_eq_records()),
            "balance": float(self.channel_balance), "bands": deepcopy(self._active_eq_records()),
        }

    @classmethod
    def _snapshot_eq_filters(cls, snapshot: dict[str, object]) -> list[dict[str, float]]:
        if not snapshot["enabled"]:
            return []
        return [band for band in cls._eq_filter_bands(snapshot["bands"])
                if band["mode"] in {3.0, 5.0} or abs(band["gain"]) >= 0.01]

    def _publish_restored_dsp(self) -> None:
        """Advance frontend caches only after the staged complete graph exists."""
        desired = self._pending_dsp_restore
        if desired is None:
            return
        plugin, source = desired["plugin"], desired["source"]
        self.eq_active_plugin_uri, self.eq_active_source_name = plugin, source
        plugins = self.eq_source_bands_by_plugin.setdefault(source, {}) if source else self.eq_bands_by_plugin
        plugins[plugin] = deepcopy(desired["bands"])
        self.eq_names_by_plugin[plugin] = desired["name"]
        self.eq_channel_by_plugin[plugin] = "Stereo"
        self.eq_enabled = desired["enabled"]
        self.channel_balance = self._normalize_channel_balance(str(desired["balance"]))
        self.cfg.channel_balance = self.channel_balance
        self.eq_current_preset = self._eq_display_name(plugin, plugins[plugin])
        self._pending_dsp_restore = None
        self._dsp_active_confirmed = True
        self._dsp_state_uncertain = False

    def _refresh_restored_dsp(self) -> None:
        """Reconcile only matching complete backend truth, without new I/O."""
        desired = self._pending_dsp_restore
        actual = getattr(self.mpv, "confirmed_dsp", None)
        if desired is not None:
            if actual == (desired["balance"], self._snapshot_eq_filters(desired)):
                self._publish_restored_dsp()
        elif self._dsp_state_uncertain and self._dsp_active_confirmed:
            # A failed runtime update may have rolled back onto a graph reset
            # by external mpv. Load/resume or a checked gain change can later
            # restore our old desired graph; an unrelated confirmed candidate
            # must never clear the API's uncertainty about that old state.
            expected = self._active_dsp_snapshot()
            if actual == (expected["balance"], self._snapshot_eq_filters(expected)):
                self._dsp_state_uncertain = False

    def _persist_active_dsp(self) -> None:
        """Never persist a library-only proposal or a still-unconfirmed restore."""
        if self._dsp_active_confirmed and not self._dsp_state_uncertain and self._pending_dsp_restore is None:
            self._require_dsp_readback()
            atomic_write_text(Path(self.cfg.state_dir) / "active_dsp.json", encode_active_dsp(self._active_dsp_snapshot()))

    async def _run_dsp_command(self, name: str) -> dict[str, object] | str:
        """Serialize DSP reads/writes and hide unconfirmed proposals from clients."""
        async with self._dsp_command_lock:
            self._refresh_restored_dsp()
            fields = (
                "eq_bands_by_plugin", "eq_source_bands_by_plugin", "eq_names_by_plugin",
                "eq_channel_by_plugin", "eq_active_plugin_uri", "eq_active_source_name",
                "eq_enabled", "eq_presets", "eq_current_preset", "channel_balance",
            )
            before = {field: deepcopy(getattr(self, field)) for field in fields}
            old_balance = self.cfg.channel_balance
            self._dsp_backend_confirmed = False
            self._dsp_backend_attempted = False
            self._dsp_library_committed = False

            async def transaction() -> dict[str, object] | str:
                try:
                    return await self._execute_dsp_command(name)
                except BaseException as exc:
                    if self._dsp_backend_attempted and not self._dsp_backend_confirmed:
                        self._dsp_state_uncertain = not getattr(self.mpv, "_dsp_configuration_confirmed", True)
                    # Preserve actual confirmed DSP on a subsequent disk error.
                    # Library-only edits roll back only before file replacement;
                    # a failed directory fsync leaves committed bytes visible.
                    if (not self._dsp_backend_confirmed and not self._dsp_library_committed
                            and not isinstance(exc, AtomicWriteCommittedError)):
                        for field, value in before.items():
                            setattr(self, field, value)
                        self.cfg.channel_balance = old_balance
                    raise

            task = asyncio.create_task(transaction())
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # HTTP disconnects cannot release serialization while a checked
                # backend write is unresolved. Finish confirmation/rollback first.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except BaseException:
                        break
                if not task.cancelled():
                    task.exception()
                raise

    async def _execute_dsp_command(self, name: str) -> dict[str, object] | str:
        """Run one EQ/balance command while its transaction lock is held."""
        if name == "__initializeDSP":
            desired = self._pending_dsp_restore
            if desired is None:
                return "OK"
            configure = getattr(self.mpv, "configure_dsp", None)
            if not callable(configure):
                raise BackendError("Backend cannot restore the complete saved DSP state")
            self._dsp_backend_attempted = True
            await self._backend_call(configure(desired["balance"], self._snapshot_eq_filters(desired)))
            self._dsp_backend_confirmed = True
            self._publish_restored_dsp()
            self._persist_active_dsp()
            return "OK"
        if self._pending_dsp_restore is not None and not name.startswith(("EQv2GetList:", "EQv2GetNewList:")):
            raise BackendError("Saved DSP restore is not yet confirmed")
        if name == "getChannelBalance":
            self._require_dsp_readback()
            return self.channel_balance
        if name.startswith("setChannelBalance:"):
            self.channel_balance = self._normalize_channel_balance(name.split(":", 1)[1])
            encode_active_dsp(self._active_dsp_snapshot())
            self._dsp_backend_attempted = True
            await self._backend_call(self.mpv.channel_balance(float(self.channel_balance)))
            self._dsp_backend_confirmed = True
            self._dsp_state_uncertain = False
            self._dsp_active_confirmed = True
            self.cfg.channel_balance = self.channel_balance
            self._persist_active_dsp()
            atomic_write_text(Path(self.cfg.state_dir) / "channel_balance", self.channel_balance + "\n")
            return "OK"
        if name.startswith("EQSetLV2SourceBand:") or name.startswith("EQSetLV2Band:"):
            result = self._set_eq_bands(name, source_specific=name.startswith("EQSetLV2SourceBand:"))
            await self._apply_eq_to_mpv()
            return result
        if name.startswith("EQGetLV2SourceBandEx:") or name.startswith("EQGetLV2SourceBand:"):
            return self._get_eq_bands(name, source_specific=True)
        if name.startswith("EQGetLV2Band"):
            return self._get_eq_bands(name)
        if name.startswith("EQChangeSourceFX:"):
            payload = self._eq_payload(name, "EQChangeSourceFX:")
            self.eq_active_source_name = str(payload.get("source_name") or payload.get("source") or self.eq_active_source_name or "wifi")
            plugin_uri = str(payload.get("pluginURI") or payload.get("plugin") or self.eq_active_plugin_uri or DEFAULT_EQ_PLUGIN)
            self.eq_active_plugin_uri = plugin_uri
            self.eq_enabled = True
            self._ensure_eq_plugin(plugin_uri, source_specific=True, source_name=self.eq_active_source_name)
            await self._apply_eq_to_mpv()
            return {"status": "OK"}
        if name.startswith("EQChangeFX:"):
            plugin_uri = unquote(name.removeprefix("EQChangeFX:")).strip() or DEFAULT_EQ_PLUGIN
            self.eq_active_plugin_uri = plugin_uri
            self.eq_active_source_name = ""
            self.eq_enabled = True
            self._ensure_eq_plugin(plugin_uri)
            await self._apply_eq_to_mpv()
            return {"status": "OK"}
        if name.startswith("EQv2GetList:") or name.startswith("EQv2GetNewList:"):
            return self._get_eq_preset_list(name)
        if name.startswith("EQSourceSave:"):
            self._save_eq_preset_from_json(name, "EQSourceSave:")
            self._persist_eq_presets()
            return {"status": "OK"}
        if name.startswith("EQSave:"):
            preset_name = self._normalize_eq_preset_name(unquote(name.removeprefix("EQSave:")))
            plugin = self.eq_active_plugin_uri
            if self.eq_active_source_name:
                bands = self.eq_source_bands_by_plugin.get(self.eq_active_source_name, {}).get(plugin, self._default_eq_bands())
            else:
                bands = self.eq_bands_by_plugin.get(plugin, self._default_eq_bands())
            self.eq_presets.setdefault(preset_name, {})[plugin] = deepcopy(bands)
            self.eq_current_preset = preset_name
            self.eq_names_by_plugin[plugin] = preset_name
            self._persist_eq_presets()
            return {"status": "0"}
        if name.startswith("EQv2SourceLoad:") or name.startswith("EQv2Load:"):
            source_specific = name.startswith("EQv2SourceLoad:")
            self._load_eq_preset(name, "EQv2SourceLoad:" if source_specific else "EQv2Load:", source_specific=source_specific)
            self.eq_enabled = True
            await self._apply_eq_to_mpv()
            return self._get_eq_bands(self._eq_get_command_for_active(source_specific), source_specific)
        if name.startswith("EQSourceOff:"):
            payload = self._eq_payload(name, "EQSourceOff:")
            self.eq_active_source_name = str(payload.get("source_name") or payload.get("source") or self.eq_active_source_name or "wifi")
            self.eq_active_plugin_uri = str(payload.get("pluginURI") or self.eq_active_plugin_uri or DEFAULT_EQ_PLUGIN)
            self.eq_enabled = False
            await self._apply_eq_to_mpv()
            return {"status": "OK"}
        if name.startswith("EQv2Delete:"):
            self._delete_eq_preset(name)
            self._persist_eq_presets()
            return {"status": "0"}
        if name.startswith("EQv2Rename:"):
            self._rename_eq_preset(name)
            self._persist_eq_presets()
            return {"status": "0"}
        if name in {"EQOn", "EQOff"}:
            self.eq_enabled = name == "EQOn"
            await self._apply_eq_to_mpv()
            return {"status": "0"}
        if name.startswith("EQSetChannelMode:"):
            self._set_eq_channel_mode(name)
            return {"status": "0"}
        return {"error": "Failed", "command": name}

    async def _apply_eq_to_mpv(self) -> None:
        """Translate active EQ state into mpv PEQ bands."""

        encode_active_dsp(self._active_dsp_snapshot())
        bands: list[dict[str, float]] = []
        if self.eq_enabled:
            for raw_band in self._active_eq_bands():
                gain = raw_band["gain"]
                if raw_band["mode"] in {3.0, 5.0} or abs(gain) >= 0.01:
                    bands.append(raw_band)
        self._dsp_backend_attempted = True
        await self._backend_call(self.mpv.peq(bands))
        self._dsp_backend_confirmed = True
        self._dsp_state_uncertain = False
        self._dsp_active_confirmed = True
        self.eq_current_preset = self._eq_display_name(self.eq_active_plugin_uri, self._active_eq_records())
        self._persist_active_dsp()

    def _active_eq_bands(self) -> list[dict[str, float]]:
        """Read exactly the active context; never substitute another cached plugin."""
        return self._eq_filter_bands(self._active_eq_records())

    def _active_eq_records(self) -> list[dict[str, object]]:
        plugin_uri = self.eq_active_plugin_uri or DEFAULT_EQ_PLUGIN
        if self.eq_active_source_name:
            if self.eq_active_source_name != "wifi":
                raise ValueError("Only wifi EQ is supported")
            plugins = self.eq_source_bands_by_plugin.get(self.eq_active_source_name, {})
        else:
            plugins = self.eq_bands_by_plugin
        return plugins.get(plugin_uri, self._default_eq_bands())

    def _ensure_eq_plugin(self, plugin_uri: str, source_specific: bool = False, source_name: str = "wifi") -> None:
        """Ensure an EQ plugin has a default 10-band parameter set."""

        if source_specific:
            source_plugins = self.eq_source_bands_by_plugin.setdefault(source_name, {})
            source_plugins.setdefault(plugin_uri, self.eq_bands_by_plugin.get(plugin_uri, self._default_eq_bands()))
        else:
            self.eq_bands_by_plugin.setdefault(plugin_uri, self._default_eq_bands())

    @staticmethod
    def _eq_filter_bands(bands: list[dict[str, object]]) -> list[dict[str, float]]:
        """Convert strictly validated canonical parameter records to filters."""
        records = HttpApi._validated_eq_records(bands, require_complete=True)
        values = {str(item["param_name"]): item["value"] for item in records}
        filters = [
            {suffix: float(values[f"{letter}_{suffix}"]) for suffix in ("freq", "gain", "q", "mode")}
            for letter in DEFAULT_EQ_BAND_FREQS
            if values[f"{letter}_mode"] != -1.0
        ]
        try:
            return MpvBackend.validate_peq_bands(filters)
        except BackendError as exc:
            raise ValueError("Invalid EQ parameters") from exc

    def _persist_runtime_value(self, name: str, value: str) -> None:
        """Persist a small runtime setting under the configured state dir."""

        try:
            state_dir = Path(self.cfg.state_dir)
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / name).write_text(value.strip() + "\n")
        except OSError as exc:
            LOG.warning("could not persist runtime value %s: %s", name, exc)

    @staticmethod
    def _decode_device_name_command(command: str) -> str:
        """Decode plain or hex-encoded device rename commands."""

        if command.startswith("setHexDeviceName:"):
            raw_hex = command.removeprefix("setHexDeviceName:").strip()
            try:
                return bytes.fromhex(raw_hex).decode("utf-8").strip()
            except ValueError:
                LOG.warning("invalid setHexDeviceName payload: %s", raw_hex)
                return ""
            except UnicodeDecodeError:
                LOG.warning("invalid UTF-8 setHexDeviceName payload: %s", raw_hex)
                return ""
        return unquote(command.removeprefix("setDeviceName:")).strip()

    @staticmethod
    def _normalize_channel_balance(raw: str) -> str:
        """Accept finite balance or percentage input, without unsafe clamping."""
        value = float(unquote(raw))
        if not math.isfinite(value) or abs(value) > 100.0:
            raise ValueError("Invalid channel balance")
        if abs(value) > 1.0:
            value /= 100.0
        try:
            value = MpvBackend.validate_channel_balance(value)
        except BackendError as exc:
            raise ValueError("Invalid channel balance") from exc
        return f"{value:.3f}".rstrip("0").rstrip(".") if value else "0.0"

    def _set_eq_bands(self, command: str, source_specific: bool = False) -> dict[str, object]:
        """Stage a canonical band update for checked backend application."""
        prefix = "EQSetLV2SourceBand:" if source_specific else "EQSetLV2Band:"
        payload = self._eq_payload(command, prefix)
        bands = payload.get("EQBand")
        if not isinstance(bands, list):
            raise ValueError("EQBand must be a list")
        plugin_uri = str(payload.get("pluginURI") or DEFAULT_EQ_PLUGIN)
        source_name = str(payload.get("source_name") or payload.get("source") or "wifi")
        if source_specific:
            source_plugins = self.eq_source_bands_by_plugin.setdefault(source_name, {})
            normalized = self._merge_eq_bands(source_plugins.get(plugin_uri), bands)
            source_plugins[plugin_uri] = normalized
        else:
            normalized = self._merge_eq_bands(self.eq_bands_by_plugin.get(plugin_uri), bands)
            self.eq_bands_by_plugin[plugin_uri] = normalized
        self.eq_active_plugin_uri = plugin_uri
        self.eq_active_source_name = source_name if source_specific else ""
        self.eq_channel_by_plugin[plugin_uri] = "Stereo"
        preset_name = str(payload.get("Name") or "")
        if not preset_name:
            preset_name = "Flat" if self._is_flat_eq_bands(normalized) else "Custom"
        self.eq_current_preset = preset_name
        self.eq_names_by_plugin[plugin_uri] = preset_name
        LOG.info("EQSetLV2Band staged source=%s plugin=%s bands=%s", self.eq_active_source_name, plugin_uri, len(normalized))
        return {"status": "OK"}

    def _get_eq_bands(self, command: str, source_specific: bool = False) -> dict[str, object]:
        """Return EQ band state for the requested or active plugin."""

        self._require_dsp_readback()
        plugin_uri = ""
        source_name = ""
        prefix = "EQGetLV2SourceBandEx:" if command.startswith("EQGetLV2SourceBandEx:") else "EQGetLV2SourceBand:" if source_specific else "EQGetLV2Band:"
        if command.startswith(prefix):
            raw = unquote(command.removeprefix(prefix))
            if raw.startswith("{"):
                payload = self._eq_payload(command, prefix)
                plugin_uri = str(payload.get("pluginURI") or "")
                source_name = str(payload.get("source_name") or payload.get("source") or "")
            else:
                plugin_uri = raw
        if not plugin_uri:
            plugin_uri = self.eq_active_plugin_uri or DEFAULT_EQ_PLUGIN
        if source_specific:
            source_name = source_name or self.eq_active_source_name or "wifi"
            if source_name != "wifi":
                raise ValueError("Only wifi EQ is supported")
            bands = self.eq_source_bands_by_plugin.get(source_name, {}).get(plugin_uri, self._default_eq_bands())
        else:
            source_name = ""
            bands = self.eq_bands_by_plugin.get(plugin_uri, self._default_eq_bands())
        payload: dict[str, object] = {
            "status": "OK",
            "EQStat": "On" if self.eq_enabled and plugin_uri == self.eq_active_plugin_uri and source_name == self.eq_active_source_name else "Off",
            "Name": self._eq_display_name(plugin_uri, bands),
            "pluginURI": plugin_uri,
            "channelMode": self.eq_channel_by_plugin.get(plugin_uri, "Stereo"),
            "EQBand": deepcopy(bands),
            "EQBandL": [],
            "EQBandR": [],
        }
        if source_specific:
            payload["source_name"] = source_name
        else:
            payload["source_name"] = ""
        return payload

    def _get_eq_preset_list(self, command: str) -> dict[str, object]:
        """Return built-in and user-saved EQ presets for the active plugin."""

        raw = unquote(command.split(":", 1)[1]) if ":" in command else ""
        if raw.startswith("{"):
            payload = self._eq_payload(command, command.split(":", 1)[0] + ":")
            plugin_uri = str(payload.get("pluginURI") or payload.get("plugin") or self.eq_active_plugin_uri or DEFAULT_EQ_PLUGIN)
        else:
            plugin_uri = raw or self.eq_active_plugin_uri or DEFAULT_EQ_PLUGIN
        names = [self._normalize_eq_preset_name(name) for name in sorted(self.eq_presets)
                 if plugin_uri in self.eq_presets[name]]
        if plugin_uri.endswith("/Eq4p") or plugin_uri.endswith("/Eq10HP"):
            return {"status": "OK", "preset": ["Flat", "Bass Boost", "Treble Boost"], "custom": names, "pluginURI": plugin_uri}
        presets = [self._eq_list_item(name, "preset") for name in ("Flat", "Bass Boost", "Treble Boost")]
        custom = [self._eq_list_item(name, "custom") for name in names]
        return {"status": "OK", "preset": presets, "custom": custom, "pluginURI": plugin_uri}

    def _save_eq_preset_from_json(self, command: str, prefix: str) -> None:
        """Save cached bands without silently switching the active DSP context."""
        payload = self._eq_payload(command, prefix)
        name = self._normalize_eq_preset_name(str(payload.get("Name") or payload.get("name") or "Custom"))
        plugin_uri = str(payload.get("pluginURI") or self.eq_active_plugin_uri or DEFAULT_EQ_PLUGIN)
        source_name = str(payload.get("source_name") or payload.get("source") or self.eq_active_source_name or "")
        if source_name:
            bands = self.eq_source_bands_by_plugin.get(source_name, {}).get(plugin_uri, self._default_eq_bands())
        else:
            bands = self.eq_bands_by_plugin.get(plugin_uri, self._default_eq_bands())
        self.eq_presets.setdefault(name, {})[plugin_uri] = deepcopy(bands)
        if plugin_uri == self.eq_active_plugin_uri and source_name == self.eq_active_source_name:
            self.eq_current_preset = name
            self.eq_names_by_plugin[plugin_uri] = name
        LOG.info("EQSourceSave stored preset=%s plugin=%s source=%s", name, plugin_uri, source_name)

    def _load_eq_preset(self, command: str, prefix: str, source_specific: bool = False) -> None:
        """Load a saved or built-in EQ preset into the active plugin context."""

        payload = self._eq_payload(command, prefix)
        name = self._normalize_eq_preset_name(str(payload.get("Name") or payload.get("name") or self.eq_current_preset or "Flat"))
        plugin_uri = str(payload.get("pluginURI") or DEFAULT_EQ_PLUGIN)
        source_name = str(payload.get("source_name") or payload.get("source") or self.eq_active_source_name or "wifi")
        self.eq_active_plugin_uri = plugin_uri
        if source_specific:
            self.eq_active_source_name = source_name
        else:
            self.eq_active_source_name = ""
        self.eq_current_preset = name
        if name in self.eq_presets and plugin_uri in self.eq_presets[name]:
            bands = [dict(band) for band in self.eq_presets[name][plugin_uri]]
        elif name == "Flat":
            bands = self._default_eq_bands()
        elif name == "Bass Boost":
            bands = self._preset_eq_bands({"a_gain": 5.0, "b_gain": 3.0})
        elif name == "Treble Boost":
            bands = self._preset_eq_bands({"i_gain": 3.0, "j_gain": 5.0})
        else:
            raise ValueError("Unknown EQ preset for plugin")
        bands = self._merge_eq_bands(None, bands)
        if source_specific:
            self.eq_source_bands_by_plugin.setdefault(source_name, {})[plugin_uri] = bands
        else:
            self.eq_bands_by_plugin[plugin_uri] = bands
        self.eq_names_by_plugin[plugin_uri] = name
        self.eq_channel_by_plugin[plugin_uri] = str(payload.get("channelMode") or self.eq_channel_by_plugin.get(plugin_uri) or "Stereo")

    def _eq_get_command_for_active(self, source_specific: bool = False) -> str:
        if source_specific:
            payload = {
                "source_name": self.eq_active_source_name or "wifi",
                "pluginURI": self.eq_active_plugin_uri or DEFAULT_EQ_PLUGIN,
            }
            return "EQGetLV2SourceBand:" + json.dumps(payload, separators=(",", ":"))
        return "EQGetLV2Band:" + (self.eq_active_plugin_uri or DEFAULT_EQ_PLUGIN)

    def _eq_list_item(self, name: str, item_type: str) -> dict[str, object]:
        return {
            "Name": name,
            "Type": item_type,
            "channelMode": "Stereo",
            "UpdateAt": "0",
        }

    @staticmethod
    def _normalize_eq_preset_name(name: str) -> str:
        if any(ord(char) < 32 for char in name):
            raise ValueError("Invalid EQ preset name")
        name = name.strip()
        if name.startswith("{") and name.endswith("}") and "Name=" in name:
            match = re.search(r"(?:^|\{|,\s*)Name=([^,}]+)", name)
            if match:
                return match.group(1).strip() or "Custom"
        return name or "Custom"

    def _load_persisted_eq_presets(self) -> None:
        """Load a validated preset library, never claim it is already applied."""
        try:
            path = Path(self.cfg.state_dir) / "eq_presets.json"
            with path.open("rb") as stream:
                encoded = stream.read(1024 * 1024 + 1)
            if len(encoded) > 1024 * 1024:
                raise ValueError("EQ preset document is too large")
            payload = json.loads(encoded, parse_constant=self._reject_eq_constant)
            if (not isinstance(payload, dict) or type(payload.get("version", 1)) is not int
                    or payload.get("version", 1) != 1):
                raise ValueError("Unsupported EQ preset document")
            presets = payload.get("presets")
            if not isinstance(presets, dict):
                raise ValueError("Invalid EQ preset library")
            loaded: dict[str, dict[str, list[dict[str, object]]]] = {}
            for name, plugins in presets.items():
                if not isinstance(name, str) or not isinstance(plugins, dict):
                    raise ValueError("Invalid EQ preset")
                clean_name = self._normalize_eq_preset_name(name)
                if clean_name in loaded:
                    raise ValueError("Duplicate EQ preset name")
                loaded[clean_name] = {}
                for plugin_uri, bands in plugins.items():
                    if not isinstance(plugin_uri, str) or not plugin_uri:
                        raise ValueError("Invalid EQ plugin")
                    loaded[clean_name][plugin_uri] = self._merge_eq_bands(None, bands)
            self.eq_presets = loaded
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
            LOG.warning("could not load EQ preset library: %s", exc)

    def _persist_eq_presets(self) -> None:
        # This document is a library, not a claim that a preset is active after
        # restart. Active DSP replay requires separate backend confirmation.
        payload = {"version": 1, "presets": self.eq_presets}
        encoded = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
        if len(encoded.encode("utf-8")) > 1024 * 1024:
            raise ValueError("EQ preset library exceeds its persisted size limit")
        atomic_write_text(Path(self.cfg.state_dir) / "eq_presets.json", encoded)
        self._dsp_library_committed = True
        self._persist_active_dsp()

    def _delete_eq_preset(self, command: str) -> None:
        payload = self._eq_payload(command, "EQv2Delete:")
        raw_name = str(payload.get("Name") or payload.get("name") or "")
        name = self._normalize_eq_preset_name(raw_name) if raw_name else ""
        if name:
            self.eq_presets.pop(name, None)
            if self.eq_current_preset == name:
                self.eq_current_preset = ""
            for plugin, preset_name in list(self.eq_names_by_plugin.items()):
                if preset_name == name:
                    del self.eq_names_by_plugin[plugin]

    def _rename_eq_preset(self, command: str) -> None:
        payload = self._eq_payload(command, "EQv2Rename:")
        raw_old = str(payload.get("Name") or payload.get("name") or "")
        raw_new = str(payload.get("newName") or payload.get("NewName") or "")
        if not raw_old.strip() or not raw_new.strip():
            raise ValueError("EQ rename requires both preset names")
        old_name = self._normalize_eq_preset_name(raw_old)
        new_name = self._normalize_eq_preset_name(raw_new)
        if old_name not in self.eq_presets:
            raise ValueError("Unknown EQ preset")
        if new_name != old_name and new_name in self.eq_presets:
            raise ValueError("EQ preset name already exists")
        self.eq_presets[new_name] = self.eq_presets.pop(old_name)
        if self.eq_current_preset == old_name:
            self.eq_current_preset = new_name
        for plugin, preset_name in list(self.eq_names_by_plugin.items()):
            if preset_name == old_name:
                self.eq_names_by_plugin[plugin] = new_name

    def _set_eq_channel_mode(self, command: str) -> None:
        raw = unquote(command.removeprefix("EQSetChannelMode:"))
        payload = self._eq_payload(command, "EQSetChannelMode:") if raw.startswith("{") else {}
        plugin_uri = str(payload.get("pluginURI") or DEFAULT_EQ_PLUGIN)
        channel_mode = str(payload.get("channelMode") or (raw if not payload else "Stereo") or "Stereo")
        if channel_mode != "Stereo":
            raise ValueError("Only Stereo EQ is supported")
        self.eq_channel_by_plugin[plugin_uri] = channel_mode

    @staticmethod
    def _json_payload(command: str, prefix: str) -> dict[str, object]:
        raw = unquote(command.removeprefix(prefix))
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _default_eq_bands() -> list[dict[str, object]]:
        bands: list[dict[str, object]] = []
        for letter, freq in DEFAULT_EQ_BAND_FREQS.items():
            bands.extend(
                [
                    {"param_name": f"{letter}_freq", "value": freq},
                    {"param_name": f"{letter}_gain", "value": 0.0},
                    {"param_name": f"{letter}_q", "value": 0.25},
                    {"param_name": f"{letter}_mode", "value": 1},
                ]
            )
        return bands

    def _preset_eq_bands(self, updates: dict[str, float]) -> list[dict[str, object]]:
        bands = self._default_eq_bands()
        for item in bands:
            param_name = item.get("param_name")
            if isinstance(param_name, str) and param_name in updates:
                item["value"] = updates[param_name]
        return bands

    @staticmethod
    def _is_flat_eq_bands(bands: list[dict[str, object]]) -> bool:
        for item in bands:
            param_name = item.get("param_name")
            if not isinstance(param_name, str):
                continue
            try:
                value = float(item.get("value", 0.0))
            except (TypeError, ValueError):
                continue
            if param_name.endswith("_gain") and abs(value) >= 0.01:
                return False
            if param_name.endswith("_mode") and value not in {-1.0, 1.0}:
                return False
        return True

    def _merge_eq_bands(self, current: list[dict[str, object]] | None, updates: list[object]) -> list[dict[str, object]]:
        """Validate partial changes and retain exactly forty canonical records."""
        base = self._validated_eq_records(current if current is not None else self._default_eq_bands(), require_complete=True)
        changed = self._validated_eq_records(updates)
        merged = {str(item["param_name"]): item for item in base}
        merged.update({str(item["param_name"]): item for item in changed})
        return [merged[f"{letter}_{suffix}"] for letter in DEFAULT_EQ_BAND_FREQS
                for suffix in ("freq", "gain", "q", "mode")]

    @staticmethod
    def _validated_eq_records(bands: object, *, require_complete: bool = False) -> list[dict[str, object]]:
        return validate_eq_records(bands, require_complete=require_complete)

    @staticmethod
    def _reject_eq_constant(value: str) -> object:
        raise ValueError("Non-finite EQ JSON value")

    @classmethod
    def _eq_payload(cls, command: str, prefix: str) -> dict[str, object]:
        raw = unquote(command.removeprefix(prefix))
        if len(raw) > 128 * 1024:
            raise ValueError("EQ request is too large")
        try:
            payload = json.loads(raw, parse_constant=cls._reject_eq_constant)
        except RecursionError as exc:
            raise ValueError("EQ request nesting is too deep") from exc
        if not isinstance(payload, dict):
            raise ValueError("EQ request must be an object")
        for field in ("source_name", "source"):
            if field in payload and payload[field] != "wifi":
                raise ValueError("Only wifi EQ is supported")
        if "channelMode" in payload and payload["channelMode"] != "Stereo":
            raise ValueError("Only Stereo EQ is supported")
        for field in ("pluginURI", "plugin", "Name", "name", "newName", "NewName"):
            if field in payload and not isinstance(payload[field], str):
                raise ValueError("EQ identifier must be text")
        for field in ("EQBandL", "EQBandR"):
            if field in payload and payload[field] != []:
                raise ValueError("Per-channel EQ is unsupported")
        return payload

    def _require_dsp_readback(self) -> None:
        self._refresh_restored_dsp()
        if self._dsp_state_uncertain or getattr(self.mpv, "confirmed_dsp", False) is None:
            raise BackendError("Backend DSP state is unconfirmed")

    def _eq_display_name(self, plugin: str, bands: list[dict[str, object]]) -> str:
        name = self.eq_names_by_plugin.get(plugin, "")
        if name and self.eq_presets.get(name, {}).get(plugin) == bands:
            return name
        if name in {"Bass Boost", "Treble Boost"}:
            expected = self._preset_eq_bands({"a_gain": 5.0, "b_gain": 3.0} if name == "Bass Boost" else {"i_gain": 3.0, "j_gain": 5.0})
            if bands == expected:
                return name
        return "Flat" if self._is_flat_eq_bands(bands) else "Custom"

    def _enabled_experimental_sources(self) -> list[dict[str, object]]:
        """Return enabled configured source experiments as app-facing records."""

        return configured_sources(self.cfg)

    def _experimental_preset_payloads(self) -> list[dict[str, object]]:
        presets: list[dict[str, object]] = []
        for index, source in enumerate(self._enabled_experimental_sources(), start=1):
            presets.append(
                {
                    "id": source["id"],
                    "name": source["name"],
                    "title": source["name"],
                    "url": source["url"],
                    "source": source["source"],
                    "type": source["type"],
                    "icon": source["icon"],
                    "index": str(index),
                }
            )
        return presets

    def _with_experimental_sources(self, payload: dict[str, object]) -> dict[str, object]:
        """Add configured source/service experiment fields to a status payload."""

        payload = dict(payload)
        payload.update(capability_fields(self.cfg))
        variant = self.cfg.source_experiment_variant
        sources = self._enabled_experimental_sources()
        if variant == "tunein":
            payload = dict(payload)
            payload.update(self._tunein_capability_fields())
            return payload
        if variant == "none" or not sources:
            return payload
        payload = dict(payload)
        if variant in {"new_source", "music_service", "all"}:
            payload["source_list"] = sources
            payload["sourceList"] = sources
            payload["sources"] = sources
            payload["service_list"] = sources
            payload["services"] = sources
            payload["streamServicesCapability"] = self._stream_services_capability()
            payload["supported_sources"] = ",".join(str(item["source"]) for item in sources)
        if variant in {"radioparadise_replacement", "all"}:
            first = sources[0]
            payload["RadioParadise"] = first["name"]
            payload["radio_paradise_name"] = first["name"]
            payload["radio_paradise_url"] = first["url"]
        if variant in {"linkplay_radio_replacement", "all"}:
            first = sources[0]
            payload["LinkplayRadio"] = first["name"]
            payload["linkplay_radio_name"] = first["name"]
            payload["linkplay_radio_url"] = first["url"]
        return payload

    @staticmethod
    def _is_stream_services_capability_command(command: str) -> bool:
        lowered = command.lower()
        return (
            "streamservice" in lowered
            or "stream_service" in lowered
            or "musicservice" in lowered
            or "music_service" in lowered
        ) and ("capability" in lowered or command.lower().startswith("get"))

    def _stream_services_capability(self) -> dict[str, object]:
        """Build the StreamServicesCapability response advertised to clients."""

        return service_capability(self.cfg)

    def _tunein_capability_fields(self) -> dict[str, object]:
        return capability_fields(self.cfg)

    async def _play_url(self, url: str) -> None:
        """Resolve/load once and confirm only this generation's actual audio."""
        if not self.state.queue.current or self.state.queue.current.url != url:
            self.state.queue = PlaybackQueue(items=[QueueItem(url=url,
                source=self.queue_source or self.state.track_source, metadata=self.state.track_metadata,
                artwork=self.state.album_art_uri)], index=1)
        generation = self._accept_playback_command("play")
        self.state.phase = PlaybackPhase.COMMAND_ACCEPTED
        self.state.current_url = url
        self.state.resolved_url = ""
        self.state.curpos = 0
        self._schedule_event_notify()
        await self._load_playback_url(url, generation)

    async def _load_playback_url(self, url: str, generation: int, *, recovery: StationRecovery | None = None) -> None:
        """Share load/readiness with retries without accepting a new owner command."""
        def current() -> bool:
            return (generation == self.state.generation
                    and (recovery is None or self._station_recovery_is_current(recovery)))

        queue, item, index = self.state.queue, self.state.queue.current, self.state.queue.index
        load_attempted = False
        load_identity: tuple[str, str] | None = None

        def owns_selection() -> bool:
            # Retry eligibility can be revoked by shutdown/control failure
            # without transferring ownership of an already-applied load.
            return (generation == self.state.generation
                    and self.state.intent == "play" and self.state.current_url == url
                    and self.state.queue is queue and queue.current is item and queue.index == index
                    and (item is None or item.url == url))

        def owns_load() -> bool:
            # Native playlist ancestry may be unprovable. Ownership comes from
            # our serialized mutation, never a permissive native path match.
            identity = (self.state.backend_url, self.state.backend_original_url)
            return (load_attempted and owns_selection()
                    and identity in {load_identity, ("", "")})

        async def silence_failed_load() -> None:
            if not owns_load():
                return
            # Include lock acquisition, Stop acknowledgement and idle readback
            # in one finite budget. Stay in the caller task, including when its
            # cancellation interrupted a load that may already have applied.
            async with asyncio.timeout(3.0), self._backend_lock:
                if not owns_load():
                    return
                if recovery is not None:
                    self._station_recovery_loading = True
                try:
                    await self._backend_call(self.mpv.stop())
                    # Once Stop is issued, retain the lock until its actual
                    # result is known. A newer owner may select/accept now but
                    # cannot mutate the backend until this lock is released.
                    while True:
                        snapshot = await self.mpv.snapshot()
                        if snapshot.get("backend-error"):
                            raise BackendError("Backend unavailable during playback cleanup")
                        if snapshot.get("idle-active") is True:
                            self.state.backend_url = ""
                            self.state.backend_original_url = ""
                            return
                        await asyncio.sleep(0.05)
                finally:
                    if recovery is not None:
                        self._station_recovery_loading = False

        try:
            if not current():
                raise BackendError("Playback command superseded")
            self.state.phase = PlaybackPhase.LOADING
            selected = self.state.queue.current
            if selected and selected.url == url and selected.resolved_url:
                direct_url = selected.resolved_url
            else:
                try:
                    direct_url = await self._resolve_playback_url(url)
                except (BackendError, OSError, TimeoutError):
                    raise SourcePlaybackError("Source resolution failed", "source_resolution") from None
            if not current():
                raise BackendError("Playback command superseded")
            playback_url = self._backend_playback_url(url, direct_url)
            mpv_options = self._mpv_playback_options(url, direct_url)
            async with self._backend_lock:
                if not current():
                    raise BackendError("Playback command superseded")
                if recovery is not None:
                    self._station_recovery_loading = True
                try:
                    load_identity = (self.state.backend_url, self.state.backend_original_url)
                    load_attempted = True
                    await self._gain_reconciling_call(self.mpv.loadfile(playback_url, mpv_options))
                    # Record actual backend identity even if a newer owner
                    # command arrived during the bounded, locked mutation.
                    self.state.backend_url = playback_url
                    self.state.backend_original_url = url
                    load_identity = (playback_url, url)
                    self._playback_offset_sec = 0
                    if self.state.current_url == url:
                        self.state.resolved_url = direct_url
                finally:
                    if recovery is not None:
                        self._station_recovery_loading = False
            if not current():
                raise BackendError("Playback command superseded")
            self._playback_offset_sec = 0
            self.state.resolved_url = direct_url
            self.state.backend_url = playback_url
            selected = self.state.queue.current
            if selected and selected.url == url:
                selected.resolved_url = direct_url
                if not selected.metadata:
                    self.state.track_metadata = self._didl_metadata(
                        direct_url, self.state.title, self.queue_source, self.queue_pic_url,
                        self.queue_is_live, artist=selected.artist, album=selected.album,
                        item_id=selected.id or None)
                    if selected.duration > 0:
                        self.state.track_metadata = self._metadata_with_duration(self.state.track_metadata, int(selected.duration))
            self.state.phase = PlaybackPhase.BUFFERING
            self._schedule_event_notify()
            ready = await self._wait_for_mpv_audio_ready(generation, playback_url)
            if not current():
                raise BackendError("Playback command superseded")
            if not ready:
                raise SourcePlaybackError("Source did not confirm playback")
            # An idle observation during readiness may have cleared identity.
            # Audio confirmation belongs to this still-current loaded source.
            self.state.backend_url = playback_url
            self.state.backend_original_url = url
            if recovery is None:
                self._arm_station_recovery()
            self.state.mark_playing(url)
            await self.sync_mpv_state()
            if not current():
                raise BackendError("Playback command superseded")
            if recovery is not None and not self.state.backend_url:
                raise SourcePlaybackError("Station source ended during recovery", "source_eof")
            if self.state.phase not in {PlaybackPhase.PLAYING, PlaybackPhase.BUFFERING}:
                raise BackendError("Backend no longer playing")
        except (BackendError, OSError, TimeoutError, asyncio.CancelledError) as exc:
            try:
                await silence_failed_load()
            except (BackendError, OSError, TimeoutError, asyncio.CancelledError) as cleanup_error:
                if owns_selection():
                    self._mark_backend_failure()
                    self.state.last_error = "Backend control failed; playback silence not confirmed"
                if isinstance(exc, asyncio.CancelledError):
                    raise exc
                if isinstance(cleanup_error, asyncio.CancelledError):
                    raise
                raise BackendError("Playback cleanup did not confirm silence") from cleanup_error
            if owns_selection():
                if isinstance(exc, SourcePlaybackError):
                    LOG.warning("playback failure category=%s generation=%s", exc.category, generation)
                    if recovery is None:
                        self.state.mark_error(str(exc))
                        self._schedule_event_notify()
                elif isinstance(exc, asyncio.CancelledError) or not current():
                    # An observer may already have diagnosed backend failure
                    # and cancelled this retry. Cleanup must retain that cause.
                    if self.state.phase != PlaybackPhase.ERROR:
                        self.state.mark_error("Playback cancelled before confirmation")
                        self._schedule_event_notify()
                elif self.state.phase != PlaybackPhase.ERROR:
                    self._mark_backend_failure()
            raise

    def _backend_playback_url(self, original_url: str, direct_url: str) -> str:
        # WiiM Home can hand us service profile URLs rather than final media
        # URLs. For on-demand audio we optionally proxy the resolved media so
        # range seeks can be translated into fresh upstream HTTP requests.
        if (
            self.cfg.proxy_on_demand_audio
            and not self.queue_is_live
            and direct_url.startswith(("http://", "https://"))
            and "api.radiotime.com/" not in direct_url
        ):
            proxied = self._local_audio_proxy_url(direct_url)
            LOG.info("using local proxy for on-demand playback direct=%s proxy=%s", self._redact_sensitive_text(direct_url), self._redact_sensitive_text(proxied))
            return proxied
        return direct_url

    def _mpv_playback_options(self, original_url: str, direct_url: str) -> dict[str, str]:
        """Return per-load mpv options for normal playback.

        Args are kept because experimental branches may need to choose options
        based on the service URL shape.
        """

        return {}

    @staticmethod
    def _mpv_seek_reload_options() -> dict[str, str]:
        return {
            "demuxer": "lavf",
            "force-seekable": "yes",
            "cache": "no",
        }

    def _should_reload_for_large_seek(self, target: int, previous_pos: int) -> bool:
        """Return whether a seek should reload via byte-range proxy."""

        direct_url = self.queue_resolved_url or self.state.resolved_url
        if not self.cfg.range_seek_reload:
            return False
        if self.queue_is_live or self.cfg.proxy_on_demand_audio:
            return False
        if not direct_url.startswith(("http://", "https://")):
            return False
        if abs(target - previous_pos) < 60:
            return False
        return self._bitrate_bps_from_url_or_headers(direct_url, {}) > 0

    def _local_audio_proxy_url(self, url: str) -> str:
        self._proxy_sources[url] = self.state.generation
        return f"http://127.0.0.1:{self.cfg.http_api_port}/proxy/audio?url={quote(url, safe='')}"

    def _seek_audio_proxy_url(self, url: str, start_seconds: int) -> str:
        return f"{self._local_audio_proxy_url(url)}&start={max(0, int(start_seconds))}"

    @staticmethod
    def _radio_station_from_url(url: str, *, allow_opml: bool = True) -> str:
        """Extract a supported ID; never forward opaque provider parameters."""
        try:
            parts = urlsplit(url)
            if parts.scheme not in {"http", "https"} or parts.username or parts.password:
                return ""
            if parts.port not in {None, 80, 443}:
                return ""
            if parts.hostname == "api.radiotime.com":
                match = re.fullmatch(r"/profiles/(s[0-9]{1,32})/media", parts.path)
                return match[1] if match else ""
            if allow_opml and parts.hostname == "opml.radiotime.com" and parts.path == "/Tune.ashx":
                ids = parse_qs(parts.query).get("id", [])
                if len(ids) == 1 and re.fullmatch(r"s[0-9]{1,32}", ids[0]):
                    return ids[0]
        except ValueError:
            pass
        return ""

    async def _resolve_station_id(self, station_id: str) -> RadioResolution:
        return await resolve_station(station_id)

    async def _resolve_playback_url(self, url: str) -> str:
        """Resolve recognized public station references, never arbitrary JSON."""
        station_id = self._radio_station_from_url(url)
        if station_id:
            try:
                return (await self._resolve_station_id(station_id)).url
            except RadioResolutionError:
                raise BackendError("Public station resolution failed") from None
        try:
            host = urlsplit(url).hostname
        except ValueError:
            host = None
        if host in {"api.radiotime.com", "opml.radiotime.com"}:
            raise BackendError("Unsupported public radio reference")
        return url

    async def _materialize_radio_queue(self, queue: PlaybackQueue) -> None:
        """Resolve a header-only station under the current command generation."""
        station_id = queue.stream_id or self._radio_station_from_url(queue.search_url, allow_opml=False)
        if (queue.source != "newTuneIn" or not re.fullmatch(r"s[0-9]{1,32}", station_id)
                or queue.stream_type not in {"", "station"}):
            raise SoapFault(402, "Invalid Args")
        if self.state.backend_url or self.state.status in {"play", "pause", "transition"}:
            await self._transport_command("stop")
        generation = self._accept_playback_command("stop")
        self.state.mark_stop()
        self._schedule_event_notify()
        try:
            result = await self._resolve_station_id(station_id)
        except (RadioResolutionError, OSError, TimeoutError):
            if generation == self.state.generation:
                self.state.mark_error("Public station resolution failed")
                self._schedule_event_notify()
            raise BackendError("Public station resolution failed") from None
        if generation != self.state.generation:
            raise BackendError("Radio queue command superseded")
        queue.stream_id = station_id
        queue.items = [QueueItem(
            url=result.url, title=queue.list_name or queue.show_name or station_id,
            source=queue.source, id=station_id, artwork=queue.artwork,
            stream_type="station", content_type=queue.content_type or result.content_type,
            duration=0,
        )]
        queue.index = 1
        await self._replace_queue(queue)

    @staticmethod
    def _json_duration_seconds(text: str) -> int:
        """Extract the largest plausible duration value from JSON text."""

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return 0
        candidates: list[int] = []

        def add(value: object) -> None:
            if isinstance(value, (int, float)) and value > 0:
                candidates.append(HttpApi._normalize_duration_seconds(value))
            elif isinstance(value, str):
                stripped = value.strip()
                if re.fullmatch(r"\d+(\.\d+)?", stripped):
                    candidates.append(HttpApi._normalize_duration_seconds(float(stripped)))
                elif re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", stripped):
                    candidates.append(HttpApi._hms_to_seconds(stripped))

        def walk(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key.lower() in {"duration", "durationsec", "durationsecs", "durationseconds", "seconds", "totaltime", "totalseconds"}:
                        add(child)
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(payload)
        return max(candidates) if candidates else 0

    @staticmethod
    def _mpv_snapshot_matches_source(snapshot: dict, expected_url: str) -> bool:
        """Bind a native playlist child to the exact loaded container.

        mpv expands M3U/PLS internally and changes ``path`` to the child.
        Both its container property and sole active entry must agree. A title,
        URL suffix, or membership among inactive entries cannot prove identity.
        Callers still fence the snapshot by their playback generation.
        """
        observed = snapshot.get("path")
        if not isinstance(expected_url, str) or not expected_url or not isinstance(observed, str) or not observed:
            return False
        if observed == expected_url:
            return True
        if snapshot.get("playlist-path") != expected_url:
            return False
        entries = snapshot.get("playlist")
        if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
            return False
        active = [entry for entry in entries if entry.get("current") is True or entry.get("playing") is True]
        return (len(active) == 1 and active[0].get("current") is True and active[0].get("playing") is True
                and active[0].get("filename") == observed and active[0].get("playlist-path") == expected_url)


    async def _wait_for_mpv_audio_ready(self, generation: int, playback_url: str, timeout: float = 10.0) -> bool:
        """Poll mpv until the loaded item exposes a selected audio track."""

        deadline = asyncio.get_running_loop().time() + timeout
        last_snapshot: dict[str, object] = {}
        while asyncio.get_running_loop().time() < deadline:
            if generation != self._play_generation:
                return False
            await asyncio.sleep(0.5)
            async with self._backend_lock:
                snapshot = await self.mpv.snapshot()
            if generation != self.state.generation:
                return False
            last_snapshot = snapshot
            if snapshot.get("backend-error"):
                raise BackendError("Backend control failed during readiness")
            if not self._mpv_snapshot_matches_source(snapshot, playback_url):
                continue
            if snapshot.get("idle-active") is not True and self._mpv_snapshot_has_audio(snapshot):
                LOG.info("mpv audio-ready playback=%s snapshot=%s", self._redact_sensitive_text(playback_url), self._redact_sensitive_text(self._compact_snapshot(snapshot)))
                return True
        LOG.warning("mpv audio-ready timeout playback=%s snapshot=%s", self._redact_sensitive_text(playback_url), self._redact_sensitive_text(self._compact_snapshot(last_snapshot)))
        return False

    @staticmethod
    def _mpv_snapshot_has_audio(snapshot: dict[str, object]) -> bool:
        """Return whether an mpv snapshot looks like active audio playback."""

        track_list = snapshot.get("track-list")
        if isinstance(track_list, list):
            for track in track_list:
                if isinstance(track, dict) and track.get("type") == "audio" and track.get("selected", True):
                    return True
        if snapshot.get("idle-active") is False or snapshot.get("core-idle") is False:
            if isinstance(snapshot.get("playback-time"), (int, float)):
                return True
            duration = snapshot.get("duration")
            if isinstance(duration, (int, float)) and duration > 0:
                return True
        return False


    async def _wait_for_seek_ready(
        self, current: Callable[[], bool], target: int, backend_url: str, offset: int, intent: str,
        seek_source: tuple[object, object] | None,
    ) -> dict:
        """Read actual position with a deadline covering lock and snapshot waits."""
        try:
            async with asyncio.timeout(self._seek_timeout):
                while True:
                    if (not current() or self.state.backend_url != backend_url
                            or self._playback_offset_sec != offset
                            or self.state.phase in {PlaybackPhase.STOPPED, PlaybackPhase.ERROR}):
                        raise BackendError("Playback command superseded or ended")
                    async with self._backend_lock:
                        if not current():
                            raise BackendError("Playback command superseded")
                        snapshot = await self.mpv.snapshot()
                    if (not current() or self.state.backend_url != backend_url
                            or self._playback_offset_sec != offset):
                        raise BackendError("Playback command superseded")
                    if self.state.phase in {PlaybackPhase.STOPPED, PlaybackPhase.ERROR}:
                        raise BackendError("Playback ended during seek")
                    # An unrelated or missing path cannot confirm this source,
                    # including a terminal observation from a different file.
                    if snapshot.get("backend-error"):
                        self._mark_backend_failure()
                        raise BackendError("Backend control failed during seek")
                    if self._mpv_snapshot_matches_source(snapshot, backend_url):
                        if seek_source is not None and self._seek_source_token(snapshot) != seek_source:
                            raise BackendError("Source changed during seek")
                        self._apply_backend_snapshot(snapshot)
                        if self.state.phase in {PlaybackPhase.STOPPED, PlaybackPhase.ERROR}:
                            raise BackendError("Playback ended during seek")
                        if self._snapshot_is_seek_ready(snapshot, target, intent=intent):
                            return snapshot
                    elif snapshot.get("idle-active") is True and not snapshot.get("path"):
                        # mpv clears path after EOF/stop; this is terminal too.
                        self._apply_backend_snapshot(snapshot)
                        raise BackendError("Playback ended during seek")
                    await asyncio.sleep(self._seek_poll_interval)
        except TimeoutError:
            raise BackendError("Seek position not confirmed") from None

    def _snapshot_is_seek_ready(
        self, snapshot: dict[str, object], target: int, *, intent: str = "play",
    ) -> bool:
        """Require a two-sided actual-position window and preserved pause.

        One second covers decoder rounding on either side. Time alone never
        expands this window: an unchanged ignored backward seek must not
        become successful merely because confirmation waited longer.
        """
        raw = snapshot.get("playback-time")
        if type(raw) not in {int, float} or not math.isfinite(raw):
            return False
        if (snapshot.get("backend-error") or snapshot.get("idle-active") is not False
                or snapshot.get("eof-reached") is True or snapshot.get("paused-for-cache") is True):
            return False
        if snapshot.get("pause") is not (intent == "pause"):
            return False
        position = raw + self._playback_offset_sec
        if not target - 1.0 <= position <= target + 1.0:
            return False
        if intent == "pause":
            return True
        buffering = snapshot.get("cache-buffering-state")
        return (snapshot.get("core-idle") is not True
                and not (isinstance(buffering, (int, float)) and 0 <= buffering < 100)
                and self._mpv_snapshot_has_audio(snapshot))

    def _adjusted_playback_time(self, snapshot: dict[str, object]) -> int:
        """Return mpv playback time adjusted for proxy reload offsets."""

        playback_time = snapshot.get("playback-time")
        if type(playback_time) not in {int, float} or not math.isfinite(playback_time):
            return self.state.curpos
        return max(0, int(playback_time) + self._playback_offset_sec)



    @staticmethod
    def _compact_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
        """Return a compact mpv snapshot suitable for diagnostic logs."""

        compact: dict[str, object] = {}
        for key in (
            "playback-time",
            "duration",
            "media-title",
            "filename",
            "path",
            "stream-open-filename",
            "core-idle",
            "idle-active",
            "pause",
            "cache-buffering-state",
            "demuxer-cache-state",
        ):
            value = snapshot.get(key)
            if value not in (None, ""):
                compact[key] = value
        track_list = snapshot.get("track-list")
        if isinstance(track_list, list):
            compact["track-list"] = [
                {k: v for k, v in track.items() if k in {"type", "selected", "codec", "lang", "title"}}
                for track in track_list
                if isinstance(track, dict)
            ]
        return compact

    @staticmethod
    def _redacted_headers(request: web.Request) -> dict[str, str]:
        """Return request headers with credentials removed or masked."""

        blocked = {"authorization", "cookie", "proxy-authorization"}
        redacted = {}
        for key, value in request.headers.items():
            lowered = key.lower()
            if lowered in blocked:
                continue
            if lowered in {"x-plex-token", "x-plex-client-identifier"}:
                redacted[key] = "***"
            else:
                redacted[key] = HttpApi._redact_sensitive_text(value)
        return redacted

    @classmethod
    def _redacted_query(cls, request: web.Request) -> dict[str, str]:
        """Return query parameters with sensitive values masked."""

        return {key: cls._redact_sensitive_text(value) for key, value in request.query.items()}

    @classmethod
    def _preview(cls, text: str) -> str:
        """Return a redacted, bounded-size preview for logs."""

        text = cls._redact_sensitive_text(text)
        if len(text) <= LOG_BODY_LIMIT:
            return text
        return text[:LOG_BODY_LIMIT] + f"...<truncated {len(text) - LOG_BODY_LIMIT} chars>"

    @staticmethod
    def _redact_sensitive_text(text: object) -> str:
        """Mask credentials, local identifiers, and private addresses in logs."""

        value = redact_media_urls(text)
        # Detail logging is essential for WiiM Home reverse engineering, but
        # captures can contain real cloud-service tokens and login material.
        # Keep this list intentionally broad; false-positive redaction is fine.
        patterns = [
            (r"(?i)(X-Plex-Token=)[^&\s\"'<>]+", r"\1***"),
            (r'(?i)("X-Plex-Token"\s*:\s*")[^"]+', r"\1***"),
            (r"(?i)('X-Plex-Token'\s*:\s*')[^']+", r"\1***"),
            (r"(?i)(x-plex-token:\s*)[^\r\n]+", r"\1***"),
            (r"(?is)(<UserName>).*?(</UserName>)", r"\1***\2"),
            (r"(?is)(<PassWord>).*?(</PassWord>)", r"\1***\2"),
            (r"(?is)(<Password>).*?(</Password>)", r"\1***\2"),
            (r"(?is)(<Token>).*?(</Token>)", r"\1***\2"),
            (r"(?is)(<RefreshToken>).*?(</RefreshToken>)", r"\1***\2"),
            (r"(?is)(<Code>).*?(</Code>)", r"\1***\2"),
            (r"(?is)(<CodeVerifier>).*?(</CodeVerifier>)", r"\1***\2"),
            (r'(?i)("userName"\s*:\s*")[^"]+', r"\1***"),
            (r'(?i)("password"\s*:\s*")[^"]+', r"\1***"),
            (r'(?i)("authToken"\s*:\s*")[^"]+', r"\1***"),
            (r"(?i)(accessToken[\"'=:\s]+)[^,&\"'\s<>}]+", r"\1***"),
            (r"(?i)(refreshToken[\"'=:\s]+)[^,&\"'\s<>}]+", r"\1***"),
            (r"(?i)(token[\"'=:\s]+)[^,&\"'\s<>}]+", r"\1***"),
            (r"\b(?:10|127)\.(?:\d{1,3}\.){2}\d{1,3}\b", "[private-ip]"),
            (r"\b192\.168\.\d{1,3}\.\d{1,3}\b", "[private-ip]"),
            (r"\b172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}\b", "[private-ip]"),
            (r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b", "[mac]"),
            (r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", "[uuid]"),
        ]
        for pattern, repl in patterns:
            value = re.sub(pattern, repl, value)
        return value

    def _log_http_detail(self, request: web.Request, request_body: str, status: int, response_body: str) -> None:
        """Emit opt-in detailed HTTP diagnostics with redacted payloads."""

        if not self.cfg.log_wiim_http_details:
            return
        LOG.info(
            "WIIM_HTTP_DETAIL %s",
            json.dumps(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "remote": request.remote,
                    "method": request.method,
                    "path": request.path,
                    "query": self._redacted_query(request),
                    "headers": self._redacted_headers(request),
                    "request_body": self._preview(request_body),
                    "response_status": status,
                    "response_body": self._preview(response_body),
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )

    @staticmethod
    def _xml_text(body: str, tag: str, *, strip: bool = True) -> str:
        """Extract unescaped text from a simple XML tag."""

        match = re.search(rf"<(?:\w+:)?{re.escape(tag)}[^>]*>(.*?)</(?:\w+:)?{re.escape(tag)}>", body, re.IGNORECASE | re.DOTALL)
        return html_unescape(match.group(1).strip() if strip else match.group(1)) if match else ""

    @staticmethod
    def _first_stream_url(text: str) -> str:
        """Find the most likely playable URL in JSON, XML, or plain text."""

        expanded = text
        for _ in range(3):
            unescaped = html_unescape(expanded)
            if unescaped == expanded:
                break
            expanded = unescaped
        candidates = HttpApi._json_stream_candidates(expanded)
        patterns = [
            r"<URL>(.*?)</URL>",
            r"<res[^>]*>(https?://.*?)</res>",
            r"(https?://[^\s<\"']+)",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, expanded, re.IGNORECASE | re.DOTALL):
                candidate = html_unescape(match.group(1).strip())
                if candidate not in candidates:
                    candidates.append(candidate)
        if not candidates:
            return ""
        candidates.sort(key=HttpApi._stream_candidate_rank)
        return candidates[0]

    @staticmethod
    def _json_stream_candidates(text: str) -> list[str]:
        """Collect playable-looking URLs from nested JSON structures."""

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []
        candidates: list[str] = []

        def add(value: object) -> None:
            if isinstance(value, str) and value.startswith(("http://", "https://")) and value not in candidates:
                candidates.append(value)

        def walk(value: object) -> None:
            if isinstance(value, dict):
                play = value.get("Play")
                if isinstance(play, dict) and play.get("CanPlay") is not False:
                    add(play.get("PlayUrl"))
                    add(play.get("Url"))
                    add(play.get("URL"))
                for key in ("PlayUrl", "StreamUrl", "Stream", "Url", "URL", "url", "media", "playlist", "File1"):
                    add(value.get(key))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(payload)
        return candidates

    @staticmethod
    def _stream_candidate_rank(url: str) -> tuple[int, int]:
        """Rank extracted URLs so media/playlist candidates beat artwork."""

        lower = url.lower()
        if re.search(r"\.(png|jpe?g|webp|gif|svg)(\?|$)", lower) or "/images/" in lower:
            return (90, len(url))
        if re.search(r"\.(m3u8?|pls|asx)(\?|$)", lower):
            return (0, len(url))
        if re.search(r"\.(mp3|aac|m4a|ogg|opus|flac|wav)(\?|$)", lower):
            return (1, len(url))
        if any(marker in lower for marker in ("/media?", "/stream", "streamtheworld", "icecast", "shoutcast")):
            return (2, len(url))
        if "api.radiotime.com/" in lower:
            return (5, len(url))
        return (20, len(url))

    def _didl_metadata(self, url: str, title: str, source: str = "", art_url: str = "", is_live: bool = True, *, artist: str = "", album: str = "", item_id: str | None = None) -> str:
        """Build minimal DIDL-Lite metadata for the current queue item."""

        # Source names/URL shapes do not establish an actual media format.
        protocol = "*"
        origin_source = source
        return (
            f'<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
            f'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            f'xmlns:song="www.wiimu.com/song/" '
            f'xmlns:custom="www.linkplay.com/custom/" '
            f'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
            f"<item id=\"{self._event_attribute(title if item_id is None else item_id)}\">"
            f"<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
            f"<song:isLive>{'1' if is_live else '0'}</song:isLive>"
            f"<song:originSource>{escape(origin_source)}</song:originSource>"
            f'<res protocolInfo="http-get:*:{protocol}:*" duration="0">{escape(url)}</res>'
            f"<dc:title>{escape(title)}</dc:title>"
            f"<upnp:artist>{escape(artist)}</upnp:artist>"
            f"<dc:creator>{escape(artist)}</dc:creator>"
            f"<upnp:album>{escape(album)}</upnp:album>"
            f"<upnp:albumArtURI>{escape(art_url)}</upnp:albumArtURI>"
            f"<song:image>{escape(art_url)}</song:image>"
            f"</item>"
            f"</DIDL-Lite>"
        ).replace("\r", "&#13;")

    def _metadata_art_url(self, metadata: str) -> str:
        """Extract the best artwork URL from DIDL-like metadata."""

        return self._best_art_url(self._xml_text(metadata, "image") or self._xml_text(metadata, "albumArtURI") or self._xml_text(metadata, "PicUrl"))

    @staticmethod
    def _best_art_url(art_url: str) -> str:
        """Prefer larger TuneIn artwork when the URL follows known patterns."""

        if "cdn-profiles.tunein.com/" not in art_url:
            return art_url
        return re.sub(r"/logoq(\.[a-zA-Z0-9]+)(\?|$)", r"/logog\1\2", art_url, count=1)

    def _metadata_with_art(self, metadata: str, art_url: str, is_live: bool | None = None) -> str:
        """Insert or replace artwork and live-state tags in metadata."""

        if not metadata:
            return metadata

        def upsert_tag(text: str, tag: str, value: str) -> str:
            escaped_value = escape(value)
            pattern = rf"<(?:\w+:)?{re.escape(tag)}[^>]*>.*?</(?:\w+:)?{re.escape(tag)}>"
            replacement = f"<{tag}>{escaped_value}</{tag}>"
            if re.search(pattern, text, re.IGNORECASE | re.DOTALL):
                return re.sub(pattern, replacement, text, count=1, flags=re.IGNORECASE | re.DOTALL)
            insert_at = re.search(r"</(?:\w+:)?item>", text, re.IGNORECASE)
            if insert_at:
                return text[: insert_at.start()] + replacement + text[insert_at.start() :]
            return text + replacement

        updated = metadata
        if art_url:
            updated = upsert_tag(updated, "upnp:albumArtURI", art_url)
            updated = upsert_tag(updated, "song:image", art_url)
        if is_live is not None:
            updated = upsert_tag(updated, "song:isLive", "1" if is_live else "0")
        return updated

    @staticmethod
    def _metadata_with_url(metadata: str, url: str) -> str:
        """Replace the first DIDL ``res`` URL with the resolved media URL."""

        if not metadata or not url:
            return metadata

        def replace(match: re.Match[str]) -> str:
            return f"{match.group(1)}{escape(url)}{match.group(3)}"

        return re.sub(r"(<res\b[^>]*>)(.*?)(</res>)", replace, metadata, count=1, flags=re.IGNORECASE | re.DOTALL)

    @staticmethod
    def _metadata_with_duration(metadata: str, duration_sec: int) -> str:
        """Insert or update DIDL duration metadata in seconds."""

        if not metadata:
            return metadata
        value = str(max(0, duration_sec))
        if re.search(r"<res\b[^>]*\bduration=", metadata, re.IGNORECASE):
            return re.sub(r'(<res\b[^>]*\bduration=")[^"]*(")', rf"\g<1>{value}\2", metadata, count=1, flags=re.IGNORECASE)
        return re.sub(r"(<res\b)", rf'\1 duration="{value}"', metadata, count=1, flags=re.IGNORECASE)

    @staticmethod
    def _hms_to_seconds(value: str) -> int:
        """Parse HH:MM:SS, MM:SS, or numeric seconds into an integer."""

        parts = value.strip().split(":")
        try:
            if len(parts) == 3:
                hours, minutes, seconds = (int(part) for part in parts)
            elif len(parts) == 2:
                hours = 0
                minutes, seconds = (int(part) for part in parts)
            else:
                return max(0, int(float(value)))
        except ValueError:
            return 0
        return max(0, hours * 3600 + minutes * 60 + seconds)

    def _proxy_source_allowed(self, url: str) -> bool:
        if self.state.intent == "stop":
            return False
        selected = self.state.queue.current
        allowed = {self.state.current_url, self.state.resolved_url}
        if selected:
            allowed.update((selected.url, selected.resolved_url))
        return bool(url) and (url in allowed or self._proxy_sources.get(url) == self.state.generation)

    async def _open_proxy_source(self, original_url: str, headers: dict[str, str]):
        """Validate and pin every redirect hop; never forward client credentials."""
        url = original_url
        seen: set[str] = set()
        for _ in range(6):
            if not self._proxy_source_allowed(original_url):
                raise NetworkPolicyError("Media selection changed")
            target = await resolve_proxy_target(url, self.cfg.proxy_allowed_lan_hosts)
            if target.url in seen:
                raise NetworkPolicyError("Redirect loop")
            seen.add(target.url)
            if not self._proxy_source_allowed(original_url):
                raise NetworkPolicyError("Media selection changed")
            connector = TCPConnector(resolver=PinnedResolver(target), use_dns_cache=False, limit=1)
            session = ClientSession(timeout=ClientTimeout(total=None, sock_connect=10, sock_read=30),
                headers=headers, connector=connector, trust_env=False, auto_decompress=False)
            upstream = None
            try:
                upstream = await session.get(target.url, allow_redirects=False)
                if upstream.status not in {301, 302, 303, 307, 308}:
                    return session, upstream
                location = upstream.headers.get("Location", "")
                if not location:
                    raise NetworkPolicyError("Redirect has no destination")
                url = urljoin(target.url, location)
            except BaseException:
                if upstream is not None:
                    upstream.release()
                await session.close()
                raise
            upstream.release()
            await session.close()
        raise NetworkPolicyError("Too many redirects")

    async def proxy_audio(self, request: web.Request) -> web.StreamResponse:
        """Serve only selected media to the local backend, with SSRF-safe hops."""
        if not (self.cfg.proxy_on_demand_audio or self.cfg.range_seek_reload):
            return web.Response(status=404)
        try:
            if not ipaddress.ip_address(request.remote or "").is_loopback:
                return web.Response(status=403, text="Local backend only")
        except ValueError:
            return web.Response(status=403)
        url = request.query.get("url", "")
        if not self._proxy_source_allowed(url):
            return web.Response(status=403, text="Media is not selected")
        range_header = request.headers.get("Range", "")
        start = request.query.get("start", "0")
        if not re.fullmatch(r"[0-9]{1,12}", start) or (range_header and not re.fullmatch(r"bytes=(?:[0-9]{1,18}-[0-9]{0,18}|-[0-9]{1,18})", range_header)):
            return web.Response(status=400, text="Invalid range")
        base_offset = self._byte_offset_for_seconds(url, int(start))
        if base_offset and range_header.startswith("bytes=-"):
            return web.Response(status=400, text="Unsupported shifted suffix range")
        upstream_range = self._shift_range_header(range_header, base_offset)
        if not upstream_range and base_offset:
            upstream_range = f"bytes={base_offset}-"
        upstream_headers = {"User-Agent": "OpenRoomRenderer/0.1"}
        if upstream_range:
            upstream_headers["Range"] = upstream_range
        if self._proxy_semaphore.locked():
            return web.Response(status=503, text="Media proxy busy")
        session = upstream = response = None
        try:
            async with self._proxy_semaphore:
                async with asyncio.timeout(20):
                    session, upstream = await self._open_proxy_source(url, upstream_headers)
                if upstream.status not in {200, 206}:
                    return web.Response(status=502, text="Upstream media unavailable")
                headers = {"Content-Type": upstream.headers.get("Content-Type", "application/octet-stream"),
                    "Cache-Control": "no-store", "Accept-Ranges": upstream.headers.get("Accept-Ranges", "bytes")}
                for header in ("Content-Length", "Content-Range", "Content-Encoding"):
                    if upstream.headers.get(header):
                        headers[header] = upstream.headers[header]
                if base_offset:
                    shifted_range = self._seek_proxy_content_range(
                        upstream.status, upstream.headers, upstream_range, base_offset)
                    if shifted_range is None:
                        return web.Response(status=502, text="Upstream seek range was not confirmed")
                    headers["Content-Range"] = shifted_range
                response = web.StreamResponse(status=upstream.status, headers=headers)
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    if not self._proxy_source_allowed(url):
                        response.force_close()
                        return response
                    await response.write(chunk)
                await response.write_eof()
                return response
        except NetworkPolicyError:
            return web.Response(status=403, text="Media destination refused")
        except (ClientError, TimeoutError, OSError):
            LOG.warning("audio proxy request failed")
            if response is not None and response.prepared:
                response.force_close()
                return response
            return web.Response(status=502, text="Media proxy failed")
        finally:
            if upstream is not None:
                upstream.release()
            if session is not None:
                await session.close()

    async def deezer_root(self, request: web.Request) -> web.Response:
        """Return the synthetic Deezer experiment root payload."""

        payload = self._deezer_payload("root")
        self._log_http_detail(request, "", 200, json.dumps(payload, separators=(",", ":")))
        return web.json_response(payload)

    async def deezer_api(self, request: web.Request) -> web.Response:
        """Return a synthetic Deezer experiment section payload."""

        section = request.match_info.get("tail", "")
        payload = self._deezer_payload(section)
        self._log_http_detail(request, "", 200, json.dumps(payload, separators=(",", ":")))
        return web.json_response(payload)

    def _deezer_payload(self, section: str) -> dict[str, object]:
        """Build synthetic, DRM-free data for service-shape experiments."""

        base = f"http://{self.cfg.resolved_advertise_ip}:{self.cfg.http_api_port}/deezer"
        art = f"http://{self.cfg.resolved_advertise_ip}:{self.cfg.http_api_port}/icons/deezer.svg"
        test_url = self.cfg.podcast_presets[0].url if self.cfg.podcast_presets else "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"
        test_track = {
            "id": "fake-deezer-track-1",
            "type": "track",
            "title": "DRM-free test stream",
            "name": "DRM-free test stream",
            "readable": True,
            "explicit_lyrics": False,
            "duration": 0,
            "rank": 1,
            "link": test_url,
            "url": test_url,
            "stream": test_url,
            "streamUrl": test_url,
            "playUrl": test_url,
            "trackUrl": test_url,
            "searchUrl": test_url,
            "source": "Deezer2",
            "sourceName": "Deezer2",
            "picture": art,
            "picture_small": art,
            "picture_medium": art,
            "picture_big": art,
            "album": {"id": "fake-deezer-album", "title": "Local test content", "cover": art, "cover_big": art},
            "artist": {"id": "fake-deezer-artist", "name": "Fake Linkplay"},
        }
        collection = {
            "id": "fake-deezer-collection",
            "type": "playlist",
            "title": "Local DRM-free tests",
            "name": "Local DRM-free tests",
            "readable": True,
            "link": f"{base}/collection",
            "url": f"{base}/collection",
            "trackUrl": f"{base}/collection",
            "searchUrl": f"{base}/collection",
            "source": "Deezer2",
            "sourceName": "Deezer2",
            "picture": art,
            "picture_small": art,
            "picture_medium": art,
            "picture_big": art,
            "tracks": {"data": [test_track]},
            "children": [test_track],
        }
        if section in {"collection", "search"}:
            data = [test_track]
            title = "Collection"
        else:
            data = [collection, test_track]
            title = "Deezer"
        return {
            "status": "success",
            "msg": "success",
            "source": "Deezer2",
            "accountSource": "Deezer2",
            "title": title,
            "total": len(data),
            "data": data,
            "results": data,
            "items": data,
            "children": data,
            "body": [{"type": "children", "text": title, "children": data}],
            "collection": f"{base}/collection",
            "search": f"{base}/search",
            "next": "",
        }

    @staticmethod
    def _parse_nonnegative_int(value: str) -> int:
        """Parse a non-negative integer from loose string input."""

        try:
            return max(0, int(float(value)))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _byte_offset_for_seconds(cls, url: str, seconds: int) -> int:
        """Estimate byte offset for constant-bitrate HTTP audio."""

        if seconds <= 0:
            return 0
        bitrate_bps = cls._bitrate_bps_from_url_or_headers(url, {})
        if bitrate_bps <= 0:
            return 0
        return max(0, int(seconds * bitrate_bps / 8))

    @staticmethod
    def _shift_range_header(range_header: str, base_offset: int) -> str:
        """Add a base byte offset to an HTTP Range header."""

        if base_offset <= 0 or not range_header:
            return range_header
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header.strip())
        if not match:
            return range_header
        start = base_offset + int(match.group(1))
        end_raw = match.group(2)
        if end_raw:
            return f"bytes={start}-{base_offset + int(end_raw)}"
        return f"bytes={start}-"

    async def description(self, request: web.Request) -> web.Response:
        """Serve the UPnP device description XML."""

        LOG.info("HTTP description remote=%s host=%s path=%s ua=%s", request.remote, request.host, request.path_qs, request.headers.get("User-Agent", ""))
        return web.Response(text=description_xml(self.cfg), content_type="application/xml")

    async def avtransport_xml(self, request: web.Request) -> web.Response:
        """Serve the AVTransport SCPD XML."""

        LOG.info("HTTP SCPD remote=%s host=%s path=%s", request.remote, request.host, request.path_qs)
        return web.Response(text=scpd_xml("AVTransport"), content_type="application/xml")

    async def rendering_xml(self, request: web.Request) -> web.Response:
        """Serve the RenderingControl SCPD XML."""

        LOG.info("HTTP SCPD remote=%s host=%s path=%s", request.remote, request.host, request.path_qs)
        return web.Response(text=scpd_xml("RenderingControl"), content_type="application/xml")

    async def connmanager_xml(self, request: web.Request) -> web.Response:
        """Serve the ConnectionManager SCPD XML."""

        LOG.info("HTTP SCPD remote=%s host=%s path=%s", request.remote, request.host, request.path_qs)
        return web.Response(text=scpd_xml("ConnectionManager"), content_type="application/xml")

    async def playqueue_xml(self, request: web.Request) -> web.Response:
        """Serve the PlayQueue SCPD XML."""

        LOG.info("HTTP SCPD remote=%s host=%s path=%s", request.remote, request.host, request.path_qs)
        return web.Response(text=scpd_xml("PlayQueue", enable_plex=self.cfg.enable_plex_service), content_type="application/xml")

    async def icon(self, request: web.Request) -> web.Response:
        """Serve a generated placeholder SVG icon for source experiments."""

        name = request.match_info.get("name", "source")
        label = "ARD" if "ard" in name.lower() else "SRC"
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">'
            '<rect width="256" height="256" rx="32" fill="#1d4ed8"/>'
            '<circle cx="190" cy="66" r="34" fill="#f97316"/>'
            f'<text x="128" y="148" text-anchor="middle" font-family="Arial, sans-serif" font-size="56" font-weight="700" fill="#ffffff">{escape(label)}</text>'
            '</svg>'
        )
        self._log_http_detail(request, "", 200, svg)
        return web.Response(text=svg, content_type="image/svg+xml")

    async def soap(self, request: web.Request) -> web.Response:
        """Handle UPnP SOAP actions and return small XML response envelopes."""

        body = await request.text()
        try:
            service, action, action_body = parse_request(
                request.match_info.get("service") or {"/upnp/transport": "AVTransport", "/upnp/rendering": "RenderingControl"}.get(getattr(request, "path", ""), ""),
                request.headers.get("SOAPACTION", ""), body,
            )
            if action in MUTATING_ACTIONS[service]:
                await self._execute_soap_side_effect(action, action_body)
                result = self._playqueue_response_fields(action, action_body) if action == "UserLogin" else ""
                if action in {"CreateQueue", "ReplaceQueue"}:
                    result = f"<QueueName>{escape(self.state.queue.name)}</QueueName>"
            elif action == "GetControlDeviceInfo":
                result = get_control_device_info_response_fields(self.cfg, self.cfg.resolved_advertise_ip, self.state)
            elif action == "StreamServicesCapability":
                capability = json.dumps(self._stream_services_capability(), separators=(",", ":"))
                result = f"<StreamCapability>{escape(capability)}</StreamCapability>"
            elif service == "RenderingControl":
                result = self._rendering_response_fields(action)
            elif service == "AVTransport":
                if action in {"GetInfoEx", "GetPositionInfo", "GetMediaInfo", "GetTransportInfo"}:
                    await self.sync_mpv_state()
                result = avtransport_response_fields(action, self.state)
            elif service == "PlayQueue":
                queue_name = self._xml_text(action_body, "QueueName")
                if queue_name and queue_name != self.state.queue.name:
                    raise SoapFault(402, "Invalid Args")
                result = self._playqueue_response_fields(action, action_body)
            elif action == "GetProtocolInfo":
                result = "<Source></Source><Sink>http-get:*:audio/*:*</Sink>"
            elif action == "GetCurrentConnectionIDs":
                result = "<ConnectionIDs></ConnectionIDs>"
            else:
                raise SoapFault(401, "Invalid Action")
            if result is None:
                raise SoapFault(401, "Invalid Action")
            response_text = soap_response(action, service, result)
        except SoapFault as exc:
            return web.Response(status=500, text=fault_xml(exc), content_type="text/xml")
        except (BackendError, OSError, TimeoutError):
            return web.Response(status=500, text=fault_xml(SoapFault(501, "Action Failed")), content_type="text/xml")
        except (ValueError, TypeError, OverflowError):
            return web.Response(status=500, text=fault_xml(SoapFault(402, "Invalid Args")), content_type="text/xml")
        self._log_http_detail(request, body, 200, response_text)
        return web.Response(text=response_text, content_type="text/xml")

    def _playqueue_response_fields(self, action: str, body: str = "") -> str | None:
        """Return PlayQueue SOAP response fields for supported actions."""

        if action in {"BrowseQueue", "BrowseQueueEx"}:
            # WiiM Home polls BrowseQueue heavily and uses this queue context to
            # decide whether the now-playing controls are actionable. Returning
            # an empty queue after CreateQueue makes play/pause appear broken.
            queue_context = self._build_queue_context() or self.queue_context
            if queue_context:
                return f"<QueueContext>{escape(queue_context)}</QueueContext>"
            return playqueue_response_fields(action)
        if action == "UserLogin":
            account_source = self._xml_text(body, "AccountSource")
            if account_source == "Plex":
                user_info = self._current_plex_user_info() or self._plex_not_login_user_info()
                result = json.dumps(user_info, separators=(",", ":"))
                return f"<Result>{escape(result)}</Result>"
            if account_source == "Deezer2" and self.cfg.enable_stream_service_sentinels:
                raise SoapFault(401, "Invalid Action")
            return playqueue_response_fields(action)
        if action == "GetUserInfo":
            account_source = self._xml_text(body, "AccountSource")
            if account_source in {"newTuneIn", "tunein"}:
                result = json.dumps(
                    {
                        "sourceVersion": "1.1",
                        "capabilityVersion": "1.0",
                        "userId": "anonymous",
                        "userName": "anonymous",
                        "showName": "TuneIn",
                        "expires_in": 0,
                        "accountSource": account_source,
                    },
                    separators=(",", ":"),
                )
                return f"<Result>{escape(result)}</Result>"
            if account_source == "Plex":
                # Plex browsing is performed by WiiM Home itself after this
                # login gate. The renderer can provide a token/profile, but it
                # cannot force the app to prefer Plex Relay over an unreachable
                # local PMS connection returned by plex.tv/resources.
                current_plex_user_info = self._current_plex_user_info()
                if current_plex_user_info:
                    user_info = current_plex_user_info
                elif self.cfg.plex_user_info_mode == "debug_login":
                    user_info = {
                        "source": "Plex",
                        "msg": "success",
                        "userId": "debug-user",
                        "userName": "debug-user",
                        "showName": "Debug Plex",
                        "subscription": "0",
                        "accessToken": "",
                        "refreshToken": "",
                        "uuid": self.cfg.upnp_uuid,
                        "sourceVersion": "1.4",
                        "capabilityVersion": "1.4",
                        "replayGain": True,
                        "redirectUrl": PLEX_CALLBACK_URL_ENCODED,
                    }
                else:
                    user_info = self._plex_not_login_user_info()
                result = json.dumps(user_info, separators=(",", ":"))
                return f"<Result>{escape(result)}</Result>"
            if self.cfg.enable_stream_service_sentinels and account_source in {"Deezer2", "SoundCloud"}:
                user_info = self._stream_service_user_info(account_source)
                result = json.dumps(user_info, separators=(",", ":"))
                return f"<Result>{escape(result)}</Result>"
            result = json.dumps(
                {"id": account_source or "", "userId": "", "showName": "", "anonUserId": "", "version": "1.0"},
                separators=(",", ":"),
            )
            return f"<Result>{escape(result)}</Result>"
        if action == "GetKeyMapping":
            return "<QueueContext>&lt;KeyMapping&gt;&lt;/KeyMapping&gt;</QueueContext>"
        if action == "GetQueueIndex":
            return "\n".join(
                [
                    f"<Count>{self.state.plicount}</Count>",
                    f"<CurrentIndex>{self.state.plicurr}</CurrentIndex>",
                    f"<PreloadingIndex>{self.state.plicurr}</PreloadingIndex>",
                    "<CurrentPage>1</CurrentPage>",
                    f"<TrackNums>{self.state.plicount}</TrackNums>",
                ]
            )
        if action == "GetQueueLoopMode":
            return f"<LoopMode>{escape(str(self.state.loop))}</LoopMode>"
        if action in {"SetQueueLoopMode", "SetQueuePolicy", "ReplaceQueue"}:
            return ""
        return playqueue_response_fields(action)

    def _stream_service_user_info(self, account_source: str) -> dict[str, object]:
        """Build synthetic user-info payloads for service experiments."""

        return {
            "source": account_source,
            "accountSource": account_source,
            "msg": "not login",
            "userId": "",
            "userName": "",
            "showName": account_source,
            "anonUserId": "",
            "subscription": "0",
            "sourceVersion": "1.4",
            "capabilityVersion": "1.4",
            "replayGain": True,
            "redirectUrl": "",
        }

    def _plex_not_login_user_info(self) -> dict[str, object]:
        """Return the Plex user-info shape for a logged-out account."""

        return {
            "source": "Plex",
            "accountSource": "Plex",
            "msg": "NOT_LOGIN",
            "userId": "",
            "userName": "",
            "showName": "",
            "subscription": "0",
            "accessToken": "",
            "refreshToken": "",
            "uuid": self.cfg.upnp_uuid,
            "sourceVersion": "1.4",
            "capabilityVersion": "1.4",
            "replayGain": True,
            "redirectUrl": PLEX_CALLBACK_URL_ENCODED,
        }

    def _plex_logged_in_user_info(self, token: str, pin_payload: dict[str, object]) -> dict[str, object]:
        """Build a Plex logged-in user-info payload from PIN auth data."""

        user_id = str(pin_payload.get("userID") or pin_payload.get("userId") or pin_payload.get("id") or "plex-user")
        username = str(pin_payload.get("username") or pin_payload.get("login") or pin_payload.get("title") or "Plex User")
        subscription = "1" if self._plex_payload_has_pass(pin_payload) else str(pin_payload.get("subscription") or "0")
        return {
            "source": "Plex",
            "accountSource": "Plex",
            "msg": "success",
            "userId": user_id,
            "userName": username,
            "showName": username,
            "email": str(pin_payload.get("email") or ""),
            "accessToken": token,
            "refreshToken": "",
            "expires": 4102444800,
            "subscription": subscription,
            "subscriptionDescription": str(pin_payload.get("subscriptionDescription") or ""),
            "country": str(pin_payload.get("country") or "US"),
            "uuid": self.cfg.upnp_uuid,
            "clientIdentifier": self.plex_client_id,
            "sourceVersion": "1.4",
            "capabilityVersion": "1.4",
            "replayGain": True,
            "redirectUrl": PLEX_CALLBACK_URL_ENCODED,
        }

    def _load_or_create_plex_client_id(self) -> str:
        """Return a persisted Plex client identifier for local experiments."""

        path = Path(self.cfg.state_dir) / "plex_client_id"
        try:
            if path.exists():
                existing = re.sub(r"[^A-Za-z0-9_.:-]", "", path.read_text().strip())
                if existing:
                    return existing
            value = f"fake-linkplay-{self.cfg.upnp_uuid or self.cfg.uuid or uuid4()}"
            self._persist_text_secure(path, value)
            return value
        except OSError as exc:
            LOG.warning("could not load Plex client id: %s", exc)
            return f"fake-linkplay-{self.cfg.upnp_uuid or uuid4()}"

    def _load_plex_user_info(self) -> dict[str, object] | None:
        """Load persisted Plex user information if it contains a token."""

        path = Path(self.cfg.state_dir) / "plex_user_info.json"
        try:
            if not path.exists():
                return None
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("could not load Plex user info: %s", exc)
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("msg") == "success" and payload.get("userId") and payload.get("accessToken"):
            return payload
        return None

    def _current_plex_user_info(self) -> dict[str, object] | None:
        """Return cached Plex user info, loading it lazily if needed."""

        if self.plex_user_info is None:
            self.plex_user_info = self._load_plex_user_info()
        return self.plex_user_info

    def _persist_plex_user_info(self, user_info: dict[str, object]) -> None:
        """Persist Plex user information with owner-readable permissions."""

        path = Path(self.cfg.state_dir) / "plex_user_info.json"
        try:
            self._persist_text_secure(path, json.dumps(user_info, indent=2, sort_keys=True) + "\n")
        except OSError as exc:
            LOG.warning("could not persist Plex user info: %s", exc)

    @staticmethod
    def _persist_text_secure(path: Path, value: str) -> None:
        """Write text to a local state file and chmod it to 0600."""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        path.chmod(0o600)

    def _rendering_response_fields(self, action: str) -> str | None:
        """Return RenderingControl SOAP response fields."""

        if action == "GetVolume":
            if not self.state.gain_confirmed:
                raise SoapFault(501, "Action Failed")
            return f"<CurrentVolume>{escape(str(self.state.volume))}</CurrentVolume>"
        if action == "GetMute":
            if not self.state.gain_confirmed:
                raise SoapFault(501, "Action Failed")
            return f"<CurrentMute>{'1' if self.state.mute else '0'}</CurrentMute>"
        if action == "GetChannel":
            return "<CurrentChannel>Master</CurrentChannel>"
        if action == "ListPresets":
            return "<CurrentPresetNameList></CurrentPresetNameList>"
        if action in {"SelectPreset", "SetChannel"}:
            return ""
        return None

    def _build_queue_context(self) -> str:
        """Build the Linkplay-style QueueContext XML for the current item."""

        if self.state.queue.items:
            return serialize_queue_context(self.state.queue)
        url = self.queue_url or self.state.current_url
        if not url:
            return ""
        title = self.queue_title or self.state.title or "Network stream"
        source = self.queue_source or self.state.track_source or self._xml_text(self.queue_context, "SourceName") or self._xml_text(self.queue_context, "Source") or "radio"
        search_url = self.queue_search_url or self._xml_text(self.queue_context, "SearchUrl") or url
        pic_url = self.queue_pic_url or self._xml_text(self.queue_context, "PicUrl")
        quality = self.queue_quality or self._xml_text(self.queue_context, "Quality") or "0"
        stream_type = self.queue_stream_type or self._xml_text(self.queue_context, "StreamType") or "station"
        content_type = self.queue_content_type or self._xml_text(self.queue_context, "ContentType") or "audio"
        metadata = self.state.track_metadata or self._didl_metadata(url, title, source, pic_url, self.queue_is_live)
        track_url = self.queue_resolved_url or url
        metadata = self._metadata_with_url(metadata, track_url)
        return "\n".join(
            [
                '<?xml version="1.0" ?>',
                "<PlayList>",
                f"  <QueueName>{escape(self.queue_name)}</QueueName>",
                f"  <ListName>{escape(title)}</ListName>",
                "  <ListInfo>",
                "    <Radio>1</Radio>",
                f"    <SourceName>{escape(source)}</SourceName>",
                f"    <TrackSource>{escape(source)}</TrackSource>",
                f"    <StartPlayIndex>{self.state.plicurr}</StartPlayIndex>",
                f"    <LastPlayIndex>{self.state.plicurr}</LastPlayIndex>",
                f"    <TrackNumber>{self.state.plicount}</TrackNumber>",
                f"    <SearchUrl>{escape(search_url)}</SearchUrl>",
                f"    <PicUrl>{escape(pic_url)}</PicUrl>",
                f"    <Quality>{escape(quality)}</Quality>",
                "  </ListInfo>",
                "  <Tracks>",
                "    <Track1>",
                f"      <TrackNumber>{self.state.plicurr}</TrackNumber>",
                f"      <Title>{escape(title)}</Title>",
                f"      <Artist>{escape(self.state.artist)}</Artist>",
                f"      <Album>{escape(self.state.album)}</Album>",
                f"      <Source>{escape(source)}</Source>",
                f"      <SourceName>{escape(source)}</SourceName>",
                f"      <TrackSource>{escape(source)}</TrackSource>",
                f"      <URL>{escape(track_url)}</URL>",
                f"      <SearchUrl>{escape(search_url)}</SearchUrl>",
                f"      <PicUrl>{escape(pic_url)}</PicUrl>",
                f"      <Metadata>{escape(metadata)}</Metadata>",
                "      <Radio>1</Radio>",
                f"      <StreamType>{escape(stream_type)}</StreamType>",
                f"      <ContentType>{escape(content_type)}</ContentType>",
                "    </Track1>",
                "  </Tracks>",
                "</PlayList>",
            ]
        )

    def _activate_queue_item(self) -> None:
        """Project the selected canonical queue item into existing serializers."""
        queue = self.state.queue
        item = queue.current
        self.queue_name = queue.name
        self.queue_url = item.url if item else ""
        self.queue_resolved_url = item.resolved_url if item else ""
        self.queue_title = (item.title or queue.list_name or item.url.rsplit("/", 1)[-1]) if item else ""
        if item and not item.title:
            item.title = self.queue_title
            item.title_is_fallback = True
        self.queue_source = (item.source or queue.source) if item else queue.source
        self.queue_search_url = queue.search_url
        self.queue_pic_url = (item.artwork or queue.artwork) if item else queue.artwork
        self.queue_quality = queue.quality
        self.queue_stream_type = item.stream_type if item else ""
        self.queue_content_type = item.content_type if item else ""
        self.queue_duration_sec = int(item.duration) if item else 0
        self.queue_is_live = bool(item and item.stream_type == "station")
        self.state.current_url = self.queue_url
        self.state.resolved_url = self.queue_resolved_url
        self.state.title = self.queue_title
        self.state.artist = item.artist if item else ""
        self.state.album = item.album if item else ""
        self.state.album_art_uri = self.queue_pic_url
        self.state.track_source = self.queue_source
        self.state.play_medium = "RADIO-NETWORK" if self.queue_is_live else "THIRD-DLNA"
        self.state.track_metadata = (item.metadata or self._didl_metadata(item.resolved_url or item.url, self.queue_title, self.queue_source, self.queue_pic_url, self.queue_is_live, artist=item.artist, album=item.album, item_id=item.id or None)) if item else ""
        if item and not item.metadata and item.duration > 0:
            self.state.track_metadata = self._metadata_with_duration(self.state.track_metadata, int(item.duration))
        self.state.curpos = 0
        self.state.totlen = self.queue_duration_sec
        self.queue_context = serialize_queue_context(queue)

    async def _advance_queue_after_eof(self, generation: int, queue: PlaybackQueue) -> None:
        """Auto-advance only while the original owner intent is still current."""
        if generation != self.state.generation or self.state.intent != "play" or self.state.queue is not queue:
            return
        try:
            await self._play_queue_index(queue.index + 1, queue.name)
        except (BackendError, SoapFault, OSError, TimeoutError):
            # The command path already reports current-generation backend errors.
            # Superseded auto-advance must not overwrite newer owner state.
            return

    async def _replace_queue(self, queue: PlaybackQueue) -> None:
        queue.validate()
        if self.state.backend_url or self.state.status in {"play", "pause", "transition"}:
            await self._transport_command("stop")
        self._accept_playback_command("stop")
        self.state.mark_stop()
        self.state.queue = queue
        self._activate_queue_item()
        self._schedule_event_notify()

    async def _play_queue_index(self, index: int, queue_name: str = "") -> None:
        queue = self.state.queue
        try:
            if queue_name:
                queue.require_name(queue_name)
            queue.validate_index(index)
            if not queue.items:
                raise QueueError("Queue is empty")
        except QueueError:
            raise SoapFault(711, "Illegal Seek Target") from None
        if self.state.backend_url or self.state.status in {"play", "pause", "transition"}:
            await self._transport_command("stop")
        # Stop may have yielded to a newer CreateQueue/owner command.
        if self.state.queue is not queue:
            raise BackendError("Queue command superseded")
        queue.index = index
        self._activate_queue_item()
        await self._play_url(queue.current.url)
        self.queue_context = serialize_queue_context(queue)
        self._schedule_event_notify()

    @staticmethod
    async def _backend_call(operation):
        """Check adapters returning mpv results as well as exception-based ones."""
        result = await operation
        if isinstance(result, dict) and result.get("error") not in {None, "success"}:
            raise BackendError("Backend operation failed")
        return result

    async def _execute_soap_side_effect(self, action: str, body: str) -> None:
        """Apply state or mpv side effects implied by a SOAP action."""

        required = {
            "SetVolume": "DesiredVolume", "SetMute": "DesiredMute",
            "SetAVTransportURI": "CurrentURI", "CreateQueue": "QueueContext",
            "ReplaceQueue": "QueueContext", "PlayQueueWithIndex": "Index",
            "Seek": "Target",
        }
        if action in required and not self._xml_text(body, required[action]).strip():
            raise SoapFault(402, "Invalid Args")
        if action == "Play":
            speed = self._xml_text(body, "Speed")
            if speed and speed != "1":
                raise SoapFault(717, "Play Speed Not Supported")
            if not (self.queue_url or self.state.current_url):
                raise SoapFault(701, "Transition Not Available")
        if action == "Seek" and self._xml_text(body, "Unit") not in {"", "REL_TIME"}:
            raise SoapFault(710, "Seek Mode Not Supported")
        if action == "Seek" and not re.fullmatch(r"\d+:[0-5]\d:[0-5]\d", self._xml_text(body, "Target")):
            raise SoapFault(402, "Invalid Args")
        if action == "UserLogin":
            account_source = self._xml_text(body, "AccountSource")
            if account_source == "Plex" and self.cfg.enable_plex_service:
                pin_id = self._xml_text(body, "Code")
                if pin_id:
                    user_info = await self._plex_user_info_from_pin(pin_id)
                    if user_info:
                        self.plex_user_info = user_info
                        self._persist_plex_user_info(user_info)
                        LOG.info("SOAP UserLogin Plex stored userId=%s clientIdentifier=%s", user_info.get("userId"), self.plex_client_id)
                    else:
                        raise SoapFault(501, "Action Failed")
                else:
                    raise SoapFault(402, "Invalid Args")
            else:
                raise SoapFault(401, "Invalid Action")
            return
        if action == "SetVolume":
            raw = self._xml_text(body, "DesiredVolume")
            if raw:
                if not raw.isdecimal() or not 0 <= int(raw) <= 100:
                    raise SoapFault(402, "Invalid Args")
                value = int(raw)
                await self._set_confirmed_gain(volume=value)
                LOG.info("SOAP SetVolume applied value=%s", value)
            return
        if action == "SetMute":
            raw = self._xml_text(body, "DesiredMute")
            if raw:
                if raw.strip().lower() not in {"0", "1", "true", "false"}:
                    raise SoapFault(402, "Invalid Args")
                value = raw.strip().lower() in {"1", "true"}
                await self._set_confirmed_gain(mute=value)
                LOG.info("SOAP SetMute applied value=%s", value)
            return
        if action == "SetChannel":
            raise SoapFault(401, "Invalid Action")
        if action == "SetAVTransportURI":
            url = self._xml_text(body, "CurrentURI")
            if url:
                if self.state.backend_url or self.state.status in {"play", "pause", "transition"}:
                    await self._transport_command("stop")
                else:
                    self._accept_playback_command("stop")
                self.state.backend_url = ""
                self.state.resolved_url = ""
                self.state.mark_stop()
                self.queue_url = url
                self.state.current_url = url
                metadata = self._xml_text(body, "CurrentURIMetaData", strip=False)
                hints = parse_didl_metadata(metadata)
                title = hints.title or url.rsplit("/", 1)[-1] or "Network stream"
                self.state.track_source = "DLNA"
                self.state.queue = PlaybackQueue(items=[QueueItem(
                    url=url, title=title, source="DLNA", metadata=metadata,
                    artwork=hints.artwork, artist=hints.artist, album=hints.album,
                    duration=hints.duration, title_is_fallback=not bool(hints.title),
                )], index=1)
                self._activate_queue_item()
                LOG.info("SOAP SetAVTransportURI stored url=%s", self._redact_sensitive_text(url))
                self._schedule_event_notify(("rendertransport1", "AVTransport"))
            return
        if action == "Play":
            url = self.queue_url or self.state.current_url
            if self.state.status == "pause" and self.state.backend_url and self.state.current_url == url:
                await self._transport_command("resume")
            elif self.state.status != "play" or self.state.current_url != url:
                await self._play_url(url)
            self._schedule_event_notify()
            return
        if action in {"Pause", "Stop"}:
            await self._transport_command(action.lower())
            return
        if action == "Next":
            await self._play_queue_index(self.state.queue.index + 1)
            return
        if action == "Previous":
            if self.state.queue.count > 1:
                await self._play_queue_index(self.state.queue.index - 1)
                return
            if not self.state.current_url or self.state.status not in {"play", "pause"}:
                raise SoapFault(701, "Transition Not Available")
            await self._seek_command(0)
            LOG.info("SOAP Previous restarted current track")
            self._schedule_event_notify(("rendertransport1", "AVTransport"))
            return
        if action == "SetPlayMode":
            mode = self._xml_text(body, "NewPlayMode") or self._xml_text(body, "PlayMode")
            if mode != "NORMAL":
                raise SoapFault(712, "Play Mode Not Supported")
            self.state.loop = "0"
            self.state.queue.loop = 0
            return
        if action in {"Seek", "SeekForward", "SeekBackward"}:
            if action == "SeekForward":
                await self._seek_command(30, relative=True)
            elif action == "SeekBackward":
                await self._seek_command(-30, relative=True)
            else:
                unit = self._xml_text(body, "Unit")
                if unit and unit != "REL_TIME":
                    raise SoapFault(710, "Seek Mode Not Supported")
                await self._seek_command(self._hms_to_seconds(self._xml_text(body, "Target")))
            return
        if action in {"CreateQueue", "ReplaceQueue"}:
            context = self._xml_text(body, "QueueContext")
            try:
                queue = parse_queue_context(context, name=self._xml_text(body, "QueueName") or None)
            except QueueError:
                raise SoapFault(402, "Invalid Args") from None
            if queue.items or not (queue.source or queue.stream_id or queue.search_url):
                await self._replace_queue(queue)
                return
            await self._materialize_radio_queue(queue)
            return
        if action == "PlayQueueWithIndex":
            raw_index = self._xml_text(body, "Index")
            if not re.fullmatch(r"[0-9]+", raw_index):
                raise SoapFault(402, "Invalid Args")
            await self._play_queue_index(int(raw_index), self._xml_text(body, "QueueName"))
            return
        if action == "SetQueueLoopMode":
            mode = self._xml_text(body, "LoopMode") or self._xml_text(body, "QueueLoopMode")
            if mode != "0":
                raise SoapFault(712, "Play Mode Not Supported")
            self.state.loop = "0"
            self.state.queue.loop = 0
            return
        if action in {"SetQueuePolicy", "AppendQueue", "AppendTracksInQueue", "AppendTracksInQueueEx", "RemoveTracksInQueue"}:
            raise SoapFault(401, "Invalid Action")

    async def _plex_user_info_from_pin(self, pin_id: str) -> dict[str, object] | None:
        """Poll Plex PIN auth and convert a successful result to user info."""

        url = f"https://plex.tv/api/v2/pins/{quote(pin_id.strip())}"
        headers = self._plex_headers()
        timeout = ClientTimeout(total=20, sock_connect=8, sock_read=8)
        try:
            async with ClientSession(timeout=timeout, headers=headers) as session:
                for attempt in range(4):
                    async with session.get(url) as response:
                        text = await response.text()
                        if response.status >= 400:
                            LOG.warning("Plex PIN poll failed status=%s attempt=%s body=%s", response.status, attempt + 1, self._preview(text))
                            return None
                        try:
                            payload = json.loads(text)
                        except json.JSONDecodeError:
                            LOG.warning("Plex PIN poll returned non-JSON body=%s", self._preview(text))
                            return None
                    if isinstance(payload, dict):
                        token = str(payload.get("authToken") or payload.get("auth_token") or "")
                        if token:
                            profile = await self._plex_user_profile(session, token)
                            if profile:
                                payload = {**payload, **profile}
                            return self._plex_logged_in_user_info(token, payload)
                    await asyncio.sleep(1)
        except (ClientError, TimeoutError, OSError) as exc:
            LOG.warning("Plex PIN poll error pin=%s error=%s", pin_id, exc)
        return None

    async def _plex_user_profile(self, session: ClientSession, token: str) -> dict[str, object]:
        """Fetch optional Plex profile details for a valid token."""

        headers = {"X-Plex-Token": token}
        try:
            async with session.get("https://plex.tv/api/v2/user", headers=headers) as response:
                text = await response.text()
                if response.status >= 400:
                    LOG.warning("Plex user profile failed status=%s body=%s", response.status, self._preview(text))
                    return {}
                payload = json.loads(text)
                if isinstance(payload, dict):
                    return payload
        except (ClientError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            LOG.warning("Plex user profile error: %s", exc)
        return {}

    @staticmethod
    def _plex_payload_has_pass(payload: dict[str, object]) -> bool:
        """Return whether Plex profile data indicates Plex Pass."""

        roles = payload.get("roles")
        if isinstance(roles, list) and any(str(role).lower() == "plexpass" for role in roles):
            return True
        description = str(payload.get("subscriptionDescription") or "").lower()
        return "plex pass" in description

    def _plex_headers(self) -> dict[str, str]:
        """Return headers used for Plex PIN/profile requests."""

        return {
            "Accept": "application/json",
            "X-Plex-Client-Identifier": self.plex_client_id,
            "X-Plex-Product": "Fake Linkplay Renderer",
            "X-Plex-Version": "0.1.0",
            "X-Plex-Platform": "Linux",
            "X-Plex-Device-Name": self.cfg.friendly_name,
        }

    @staticmethod
    def _loop_mode_value(mode: str) -> str:
        """Normalize app loop mode names to renderer loop codes."""

        normalized = mode.strip().upper()
        if normalized in {"0", "NORMAL", "NONE"}:
            return "0"
        if normalized in {"1", "REPEAT_ONE", "REPEAT_TRACK", "SINGLE"}:
            return "1"
        if normalized in {"2", "REPEAT_ALL", "REPEAT"}:
            return "2"
        if normalized in {"3", "SHUFFLE", "RANDOM"}:
            return "3"
        return mode.strip() or "0"

    @staticmethod
    def _queue_item_is_episode(url: str, stream_type: str, content_type: str, queue_context: str) -> bool:
        """Infer whether a queue item is on-demand audio rather than live."""

        haystack = " ".join([url, stream_type, content_type, queue_context]).lower()
        if re.search(r"/profiles/t\d+/media", haystack):
            return True
        return any(marker in haystack for marker in ("<streamtype>episode", "streamtype&gt;episode", "podcast", "on-demand", "ondemand"))

    @staticmethod
    def _metadata_duration_seconds(metadata: str) -> int:
        """Extract duration seconds from a DIDL ``res`` element."""

        if not metadata:
            return 0
        match = re.search(r"<res\b[^>]*\bduration=\"([^\"]+)\"", metadata, re.IGNORECASE)
        if not match:
            return 0
        return HttpApi._hms_to_seconds(match.group(1))

    async def _probe_audio_duration_seconds(self, url: str) -> int:
        """Best-effort duration probe using HTTP headers and range requests."""

        try:
            timeout = ClientTimeout(total=8)
            async with ClientSession(timeout=timeout, headers={"User-Agent": "Linkplay/4.8"}) as session:
                async with session.head(url, allow_redirects=True) as response:
                    duration = self._duration_from_headers(url, response.headers)
                    if duration > 0:
                        LOG.info("audio duration from HEAD url=%s duration=%s", self._redact_sensitive_text(url), duration)
                        return duration
                async with session.get(url, headers={"Range": "bytes=0-0"}, allow_redirects=True) as response:
                    duration = self._duration_from_headers(url, response.headers)
                    if duration > 0:
                        LOG.info("audio duration from range probe url=%s duration=%s", self._redact_sensitive_text(url), duration)
                        return duration
        except (ClientError, TimeoutError, OSError) as exc:
            LOG.info("could not probe audio duration url=%s error=%s", self._redact_sensitive_text(url), self._redact_sensitive_text(exc))
        return 0

    @classmethod
    def _duration_from_headers(cls, url: str, headers: object) -> int:
        """Estimate duration from explicit headers or byte length/bitrate."""

        get = getattr(headers, "get", None)
        if not callable(get):
            return 0
        for key in ("X-Duration", "Duration", "X-Content-Duration", "X-Playback-Duration"):
            duration = cls._duration_value_seconds(get(key))
            if duration > 0:
                return duration
        total_bytes = cls._content_length_from_headers(headers)
        bitrate_bps = cls._bitrate_bps_from_url_or_headers(url, headers)
        if total_bytes <= 0 or bitrate_bps <= 0:
            return 0
        return max(1, int(total_bytes * 8 / bitrate_bps))

    @classmethod
    def _content_length_from_headers(cls, headers: object) -> int:
        """Return total byte length from Content-Range or Content-Length."""

        get = getattr(headers, "get", None)
        if not callable(get):
            return 0
        content_range = str(get("Content-Range") or "")
        match = re.search(r"/(\d+)\s*$", content_range)
        if match:
            return int(match.group(1))
        content_length = str(get("Content-Length") or "")
        return int(content_length) if re.fullmatch(r"\d+", content_length) else 0

    @classmethod
    def _bitrate_bps_from_url_or_headers(cls, url: str, headers: object) -> int:
        """Infer bitrate from ICY headers or common URL filename patterns."""

        get = getattr(headers, "get", None)
        if callable(get):
            icy_br = str(get("icy-br") or get("Icy-Br") or "")
            if re.fullmatch(r"\d{2,4}", icy_br):
                return int(icy_br) * 1000
        lowered = unquote(url).lower()
        matches = re.findall(r"(?:mp3|aac|m4a|opus|ogg|audio|_|-)(\d{2,4})k(?:d|bps)?", lowered)
        for value in matches:
            bitrate_kbps = int(value)
            if 32 <= bitrate_kbps <= 512:
                return bitrate_kbps * 1000
        return 0

    @classmethod
    def _duration_value_seconds(cls, value: object) -> int:
        """Normalize one loose duration value into seconds."""

        if isinstance(value, (int, float)) and value > 0:
            return cls._normalize_duration_seconds(value)
        if not isinstance(value, str):
            return 0
        stripped = value.strip()
        if re.fullmatch(r"\d+(\.\d+)?", stripped):
            return cls._normalize_duration_seconds(float(stripped))
        if re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", stripped):
            return cls._hms_to_seconds(stripped)
        return 0

    @staticmethod
    def _normalize_duration_seconds(value: float) -> int:
        """Normalize seconds or millisecond-like duration values."""

        seconds = int(value)
        if seconds > 24 * 60 * 60:
            as_milliseconds = seconds // 1000
            if 0 < as_milliseconds <= 24 * 60 * 60:
                return as_milliseconds
        return seconds

    @staticmethod
    def _event_service(value: str) -> str | None:
        service = SERVICE_ALIASES.get(value.lower())
        return service if service in {"AVTransport", "RenderingControl", "PlayQueue"} else None

    @staticmethod
    def _event_lease(value: str) -> int:
        value = value or "Second-1800"
        if value.lower() == "second-infinite":
            return 1800
        match = re.fullmatch(r"Second-([0-9]{1,10})", value, re.IGNORECASE)
        if not match:
            raise ValueError("Invalid subscription timeout")
        return max(30, min(1800, int(match[1])))

    def _remove_subscriber(self, sid: str) -> None:
        subscriber = self.event_subscribers.pop(sid, None)
        if subscriber and subscriber.task and subscriber.task is not asyncio.current_task():
            subscriber.task.cancel()

    def _prune_expired_subscribers(self) -> None:
        now = self._event_clock()
        for sid, subscriber in list(self.event_subscribers.items()):
            if subscriber.expires_at <= now:
                self._remove_subscriber(sid)

    async def event_subscribe(self, request: web.Request) -> web.Response:
        """Validate new subscriptions or renew a same-peer service lease."""
        service = self._event_service(request.match_info.get("service", ""))
        if service is None:
            return web.Response(status=404)
        self._prune_expired_subscribers()
        peer = request.remote or ""
        sid = request.headers.get("SID", "")
        callback = request.headers.get("CALLBACK", "")
        nt = request.headers.get("NT", "")
        try:
            lease = self._event_lease(request.headers.get("TIMEOUT", ""))
        except ValueError:
            return web.Response(status=400)
        if sid:
            if callback or nt:
                return web.Response(status=400)
            subscriber = self.event_subscribers.get(sid)
            if subscriber is None or subscriber.peer_ip != peer or subscriber.service != service:
                return web.Response(status=412)
            subscriber.expires_at = self._event_clock() + lease
        else:
            if nt.lower() != "upnp:event" or not callback:
                return web.Response(status=400)
            try:
                callback = validate_callback_url(callback, peer)
            except NetworkPolicyError:
                return web.Response(status=412)
            subscriber = next((item for item in self.event_subscribers.values()
                if item.peer_ip == peer and item.service == service and item.callback_url == callback), None)
            if subscriber is None:
                if len(self.event_subscribers) >= self._event_limit_total or sum(item.peer_ip == peer for item in self.event_subscribers.values()) >= self._event_limit_peer:
                    return web.Response(status=503)
                sid = f"uuid:{uuid4()}"
                subscriber = EventSubscriber(sid=sid, callback_url=callback, service=service,
                    peer_ip=peer, expires_at=self._event_clock() + lease)
                self.event_subscribers[sid] = subscriber
            else:
                sid = subscriber.sid
                subscriber.expires_at = self._event_clock() + lease
            self._schedule_subscriber_notify(subscriber)
        return web.Response(status=200, body=b"", headers={"SID": sid,
            "TIMEOUT": f"Second-{lease}", "SERVER": "Linux UPnP/1.0 OpenRoomRenderer/0.1"})

    async def event_unsubscribe(self, request: web.Request) -> web.Response:
        service = self._event_service(request.match_info.get("service", ""))
        if service is None:
            return web.Response(status=404)
        self._prune_expired_subscribers()
        sid = request.headers.get("SID", "")
        if not sid or request.headers.get("CALLBACK") or request.headers.get("NT"):
            return web.Response(status=400)
        subscriber = self.event_subscribers.get(sid)
        if subscriber is None or subscriber.peer_ip != (request.remote or "") or subscriber.service != service:
            return web.Response(status=412)
        self._remove_subscriber(sid)
        return web.Response(status=200)

    @staticmethod
    def _callback_url(header: str) -> str:
        """Extract the URL from a UPnP Callback header."""

        match = re.search(r"<([^>]+)>", header)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _default_gateway_for_ip(ip: str) -> str:
        """Return a conventional .1 gateway for an IPv4 address string."""

        parts = ip.split(".")
        if len(parts) == 4 and all(part.isdigit() for part in parts):
            return ".".join(parts[:3] + ["1"])
        return "0.0.0.0"

    def _drop_duplicate_subscribers(self, callback_url: str, service: str, keep_sid: str | None = None) -> None:
        """Remove duplicate event subscriptions for the same callback/service."""

        service_key = service.lower()
        duplicate_sids = [
            sid
            for sid, subscriber in self.event_subscribers.items()
            if sid != keep_sid and subscriber.callback_url == callback_url and subscriber.service.lower() == service_key
        ]
        for sid in duplicate_sids:
            self.event_subscribers.pop(sid, None)
            LOG.info("dropped duplicate event subscriber service=%s sid=%s callback=%s", service, sid, callback_url)

    def _transport_state(self) -> str:
        """Map internal transport state to UPnP AVTransport state names."""

        return self.state.transport_state

    def _schedule_event_notify(self, services: tuple[str, ...] | None = None) -> None:
        """Coalesce changes while keeping one ordered delivery task per SID."""
        self._prune_expired_subscribers()
        selected = {self._event_service(service) for service in services} if services else set()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for subscriber in list(self.event_subscribers.values()):
            if selected and self._event_service(subscriber.service) not in selected:
                continue
            self._schedule_subscriber_notify(subscriber)

    def _schedule_subscriber_notify(self, subscriber: EventSubscriber) -> None:
        subscriber.dirty = True
        if subscriber.task is None or subscriber.task.done():
            subscriber.task = asyncio.create_task(self._notify_event_subscriber(subscriber))

    def _subscriber_active(self, subscriber: EventSubscriber) -> bool:
        return self.event_subscribers.get(subscriber.sid) is subscriber and subscriber.expires_at > self._event_clock()

    async def _notify_event_subscriber(self, subscriber: EventSubscriber) -> None:
        """Retry transient failures within the lease, without redirects or reordering."""
        failures = 0
        try:
            while self._subscriber_active(subscriber) and (subscriber.dirty or failures):
                if not failures:
                    subscriber.dirty = False
                    body = self._event_body(subscriber.service)
                headers = {"CONTENT-TYPE": 'text/xml; charset="utf-8"', "NT": "upnp:event",
                    "NTS": "upnp:propchange", "SID": subscriber.sid, "SEQ": str(subscriber.seq)}
                success = False
                try:
                    async with self._event_semaphore:
                        if not self._subscriber_active(subscriber):
                            break
                        remaining = subscriber.expires_at - self._event_clock()
                        timeout = ClientTimeout(total=min(1.5, remaining))
                        async with ClientSession(timeout=timeout, trust_env=False) as session:
                            async with session.request("NOTIFY", subscriber.callback_url, data=body.encode("utf-8"), headers=headers, allow_redirects=False) as response:
                                await response.content.read(4096)
                                success = 200 <= response.status < 300
                                if response.status in {410, 412}:
                                    self._remove_subscriber(subscriber.sid)
                                LOG.debug("GENA delivery service=%s status=%s seq=%s", subscriber.service, response.status, subscriber.seq)
                except (ClientError, TimeoutError, OSError):
                    pass
                if not self._subscriber_active(subscriber):
                    break
                if success:
                    subscriber.seq = 1 if subscriber.seq == 0xFFFFFFFF else subscriber.seq + 1
                    failures = 0
                else:
                    failures += 1
                    delay = min(30.0, self._event_retry_base * 2 ** min(failures - 1, 8))
                    await asyncio.sleep(min(delay, max(0, subscriber.expires_at - self._event_clock())))
        finally:
            if subscriber.expires_at <= self._event_clock():
                self.event_subscribers.pop(subscriber.sid, None)
            if subscriber.task is asyncio.current_task():
                subscriber.task = None

    def _event_body(self, service: str) -> str:
        """Build the XML propertyset body for a UPnP event service."""

        service_key = service.lower()
        device_name = self.cfg.linkplay_instance_name if self.cfg.wiim_home_profile else self.cfg.friendly_name
        if service_key in {"rendercontrol1", "renderingcontrol"}:
            if not self.state.gain_confirmed:
                return self._event_propertyset({
                    "LastChange": '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/RCS/"><InstanceID val="0"/></Event>',
                    "DeviceName": device_name, "GroupName": device_name,
                })
            last_change = (
                '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/RCS/">'
                '<InstanceID val="0">'
                f'<Volume channel="Master" val="{self._event_attribute(str(self.state.volume))}"/>'
                f'<Mute channel="Master" val="{"1" if self.state.mute else "0"}"/>'
                "</InstanceID></Event>"
            )
            return self._event_propertyset(
                {
                    "LastChange": last_change,
                    "Volume": str(self.state.volume),
                    "Mute": "1" if self.state.mute else "0",
                    "DeviceName": device_name,
                    "GroupName": device_name,
                }
            )
        if service_key in {"playqueue1", "playqueue"}:
            queue_context = self._build_queue_context() or self.queue_context
            last_change = (
                '<Event xmlns="urn:schemas-wiimu-com:metadata-1-0/PlayQueue/"><InstanceID val="0">\r\n'
                f'<CurrentPlayListName val="{self._event_attribute(self.state.queue.name)}"/>\r\n'
                f'<CurrentIndex val="{self.state.queue.index}"/>\r\n'
                f'<LoopMode val="{self.state.queue.loop}"/>\r\n'
                '</InstanceID></Event>'
            )
            return self._event_propertyset(
                {
                    "LastChange": last_change,
                    "QueueContext": queue_context,
                    "CurrentPlayListName": self.state.queue.name,
                    "CurrentIndex": str(self.state.queue.index),
                    "LoopMode": str(self.state.queue.loop),
                    "Count": str(self.state.queue.count),
                    "TrackSource": self.state.track_source,
                    "TrackURI": self.state.resolved_url or self.state.current_url,
                    "TrackTitle": self.state.title,
                    "DeviceName": device_name,
                    "GroupName": device_name,
                }
            )

        transport_state = self._transport_state()
        metadata = self.state.track_metadata if self.state.current_url else ""
        if self.state.current_url and not metadata:
            metadata = self._didl_metadata(self.state.current_url, self.state.title or "Network stream", self.state.track_source, self.state.album_art_uri, artist=self.state.artist, album=self.state.album)
        track_uri = self.state.resolved_url or self.state.current_url
        track_duration = self._seconds_to_hms(self.state.totlen)
        rel_time = self._seconds_to_hms(self.state.curpos)
        last_change = (
            '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/">'
            '<InstanceID val="0">'
            f'<TransportState val="{escape(transport_state)}"/>'
            f'<TransportStatus val="{self.state.transport_status}"/>'
            f'<CurrentTrack val="{self.state.plicurr}"/>'
            f'<CurrentTrackDuration val="{self._event_attribute(track_duration)}"/>'
            f'<CurrentTrackURI val="{self._event_attribute(track_uri)}"/>'
            f'<CurrentTrackMetaData val="{self._event_attribute(metadata)}"/>'
            f'<RelativeTimePosition val="{self._event_attribute(rel_time)}"/>'
            f'<A_ARG_TYPE_SeekTarget val="{self._event_attribute(rel_time)}"/>'
            '<CurrentTransportActions val="Play,Stop,Pause,Seek,Next,Previous"/>'
            f'<PlaybackStorageMedium val="{escape(self.state.play_medium or "NETWORK")}"/>'
            "</InstanceID></Event>"
        )
        return self._event_propertyset(
            {
                "LastChange": last_change,
                "TransportState": transport_state,
                "CurrentTransportState": transport_state,
                "CurrentTrackDuration": track_duration,
                "RelativeTimePosition": rel_time,
                "A_ARG_TYPE_SeekTarget": rel_time,
                "TrackURI": track_uri,
                "TrackMetaData": metadata,
                "TrackSource": self.state.track_source,
                "PlayMedium": self.state.play_medium,
                "DeviceName": device_name,
                "GroupName": device_name,
            }
        )

    @staticmethod
    def _event_attribute(value: str) -> str:
        """Keep XML attribute whitespace distinct through both XML layers."""
        return escape(value, quote=True).replace("\r", "&#13;").replace("\n", "&#10;").replace("\t", "&#9;")

    @staticmethod
    def _event_propertyset(properties: dict[str, str]) -> str:
        """Wrap event property values in a UPnP event propertyset."""

        items = ["<?xml version=\"1.0\" encoding=\"utf-8\"?>", '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">']
        for key, value in properties.items():
            # XML 1.0 cannot represent these code points, even as references.
            # Preserve ordinary Unicode/music text; replace only invalid XML.
            value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", "\ufffd", value)
            if key == "LastChange":
                value = value.replace(" />", "/>").replace("/><", "/>\r\n<")
            encoded = escape(value).replace("\r", "&#13;")
            items.append(f"<e:property><{key}>{encoded}</{key}></e:property>")
        items.append("</e:propertyset>")
        return "".join(items)

    @staticmethod
    def _seconds_to_hms(value: int) -> str:
        """Format seconds as HH:MM:SS for UPnP transport fields."""

        total = max(0, int(value))
        hours = total // 3600
        minutes = (total % 3600) // 60
        seconds = total % 60
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
