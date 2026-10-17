"""One renderer-owned, explicitly confirmed low-gain TIDAL test lease.

No provider credentials or URLs leave this module's public status. This is a
best-effort software bound, not a hardware safety interlock.
"""
from __future__ import annotations

import asyncio
import inspect
import math
import secrets
import time

from .mpv_backend import BackendError
from .state import PlaybackPhase


class TidalTestLease:
    def __init__(self, api):
        self.api = api
        self._clock = time.monotonic
        self._poll_interval = .25
        self._io_timeout = 3.0
        self._closed = False
        self._start_lock = asyncio.Lock()
        self._finish_lock = asyncio.Lock()
        self._reservation = None
        self._play_task = None
        self._watch_task = None
        self._deadline_task = None
        self._start_tasks = set()
        self._live = False
        self._state = "idle"
        self._error = None
        self._lease_id = None
        self._deadline = 0.0
        self._queue = None
        self._generation = None
        self._backend_url = ""
        self._entry_id = None
        self._volume_confirmed = False
        self._position = None
        self._first_position = None
        self._progress = 0.0
        self._decoded = False

    def _tasks_pending(self):
        return any(task is not None and not task.done() for task in
                   (self._play_task, self._watch_task, self._deadline_task))

    def _hooks(self):
        a = self.api
        try:
            return (a.tidal_playback is not None and a.catalog_proxy_port != 0
                    and callable(getattr(a.tidal_playback, "session_available", None))
                    and a.tidal_playback.session_available() is True
                    and all(callable(getattr(a.mpv, name, None)) for name in
                            ("snapshot", "checked_command", "volume", "stop", "loadfile", "resume"))
                    and "current" in inspect.signature(a.mpv.loadfile).parameters
                    and "current" in inspect.signature(a.mpv.resume).parameters)
        except (TypeError, ValueError):
            return False

    def _renderer_idle(self):
        a, s = self.api, self.api.state
        return (s.phase == PlaybackPhase.STOPPED and s.intent == "stop"
                and not s.backend_url and not s.backend_original_url
                and a.spotify_source is None and not a._tidal_pending_resolutions
                and not a._native_output_uncertain
                and a._native_stage is None and a._native_current_id is None
                and not a._native_owned_entries and not a._native_cleanup_tasks
                and a._admitted_load_generation is None and a._pending_seek_target is None
                and not a._station_recovery_loading
                and not any(not t.done() for t in a._seek_tasks)
                and not any(t is not None and not t.done() for t in
                            (a._queue_advance_task, a._station_recovery_task)))

    @staticmethod
    def _idle(snapshot):
        return (isinstance(snapshot, dict) and not snapshot.get("backend-error")
                and snapshot.get("idle-active") is True
                and snapshot.get("playlist") == [] and snapshot.get("path") in (None, ""))

    def owns_selection(self):
        return (self._lease_id is not None and self.api.spotify_source is None
                and self.api.state.generation == self._generation
                and self.api.state.queue is self._queue)

    def admitted(self):
        return self._live and not self._closed and self._clock() < self._deadline and self.owns_selection()

    def check(self):
        if not self.admitted():
            raise BackendError("Bounded TIDAL test admission ended")

    def command_allowed(self):
        if self._reservation is not None and asyncio.current_task() is not self._reservation:
            raise BackendError("Bounded TIDAL test admission in progress")

    def command_accepted(self, generation):
        if not self._live:
            return
        if asyncio.current_task() in (self._play_task, self._reservation):
            self._generation = generation
        else:
            self._superseded()

    def bind(self, queue):
        self._queue = queue
        self._generation = self.api.state.generation
        self.check()

    def controls_owned_source(self):
        return self.owns_selection() and (self._live or self._state == "stopping"
                                           or self._error == "stop_unconfirmed")

    def guard_same_source_control(self):
        self.command_allowed()
        if self.controls_owned_source() and asyncio.current_task() is not self._play_task:
            raise BackendError("Bounded TIDAL test permits only Stop transport control")

    def guard_gain(self, volume):
        self.command_allowed()
        if (volume is not None and volume > 1 and self.owns_selection()
                and (self._live or self._state == "stopping" or self._error == "stop_unconfirmed")):
            raise BackendError("Bounded TIDAL test volume limit")

    def _superseded(self):
        if self.owns_selection():
            self.api._mark_source_ownership_lost()
        self._live = False
        self._state, self._error = "superseded", "source_changed"
        self._volume_confirmed = False

    async def _snapshot(self):
        value = await self.api.mpv.snapshot()
        if not isinstance(value, dict):
            raise BackendError("Bounded TIDAL test observation unavailable")
        return value

    async def _raw_volume(self):
        reply = await self.api.mpv.checked_command(["get_property", "volume"])
        value = reply.get("data") if isinstance(reply, dict) and reply.get("error") == "success" else None
        if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 100:
            raise BackendError("Bounded TIDAL test gain unknown")
        return value

    def _result(self, available=False, error=None):
        return {"capability_version": 1, "available": bool(available), "state": self._state,
                "lease_id": self._lease_id,
                "remaining_seconds": max(0, math.ceil(self._deadline - self._clock())) if self._live else 0,
                "volume_percent": 1 if self._volume_confirmed else None,
                "volume_confirmed": self._volume_confirmed,
                "playback_position_seconds": self._position,
                "observed_progress_seconds": self._progress,
                "decoded_audio_observed": self._decoded,
                "error": self._error if error is None else error}

    async def status(self):
        available = False
        hooks_ready = renderer_idle = None
        if not self._live:
            self._volume_confirmed = False
        if (not self._closed and not self._live and not self._tasks_pending() and self._state != "stopping"
                and self._error != "stop_unconfirmed" and (hooks_ready := self._hooks())
                and not self.api._backend_lock.locked() and not self._start_lock.locked()
                and (renderer_idle := self._renderer_idle())):
            generation, queue = self.api.state.generation, self.api.state.queue
            try:
                async with asyncio.timeout(self._io_timeout), self.api._backend_lock:
                    snap = await self._snapshot()
                    raw = await self._raw_volume()
                    available = (self._idle(snap) and self._renderer_idle()
                                 and generation == self.api.state.generation and queue is self.api.state.queue)
                    self._volume_confirmed = available and raw == 1
            except (BackendError, OSError, TimeoutError):
                pass
        # Report only predicates actually reached above. These are response-only
        # readiness hints; a real lease error, including one raised while awaiting
        # the backend observation, always takes precedence.
        error = self._error
        if error is None:
            if hooks_ready is False:
                error = "test_unavailable"
            elif renderer_idle is False:
                error = "source_not_idle"
        return self._result(available, error=error)

    async def start(self, track_id, duration_seconds):
        # The caller/request may disappear; the renderer retains both admission
        # and its watchdog, and consumes the independent task's result.
        task = asyncio.create_task(self._start(track_id, duration_seconds))
        self._start_tasks.add(task)
        task.add_done_callback(self._start_tasks.discard)
        return await asyncio.shield(task)

    async def _start(self, track_id, duration_seconds):
        async with self._start_lock:
            if self._closed:
                return self._result(error="test_closed"), 409
            if self._live or self._tasks_pending() or self._state == "stopping" or self._error == "stop_unconfirmed":
                return self._result(error="test_busy"), 409
            a = self.api
            if not self._hooks():
                return self._result(error="test_unavailable"), 409
            if a._backend_lock.locked() or a._gain_command_lock.locked() or not self._renderer_idle():
                return self._result(error="source_not_idle"), 409
            generation, queue = a.state.generation, a.state.queue
            self._reservation = asyncio.current_task()
            admitted = False
            try:
                async with asyncio.timeout(self._io_timeout), a._backend_lock:
                    snap = await self._snapshot()
                    await self._raw_volume()
                    if not self._hooks():
                        return self._result(error="test_unavailable"), 409
                    if (not self._idle(snap) or not self._renderer_idle()
                            or generation != a.state.generation or queue is not a.state.queue):
                        return self._result(error="source_not_idle"), 409
                    self._queue, self._generation = queue, generation
                    self._lease_id = secrets.token_urlsafe(24)
                    admitted = True
                    self._deadline = self._clock() + duration_seconds
                    self._live, self._state, self._error = True, "starting", None
                    self._backend_url, self._entry_id = "", None
                    self._volume_confirmed = self._decoded = False
                    self._position = self._first_position = None
                    self._progress = 0.0
                    # Register BEFORE the first mutation and provider resolution.
                    self._watch_task = asyncio.create_task(self._watch())
                    self._deadline_task = asyncio.create_task(self._expire())
                    await a._set_confirmed_gain(volume=1)
                    raw = await self._raw_volume()
                    self.check()
                    if raw != 1 or not a.state.gain_confirmed:
                        raise BackendError("Bounded TIDAL test gain unknown")
                    self._volume_confirmed = True
                self._play_task = asyncio.create_task(self._play(track_id))
                return self._result(), 202
            except (BackendError, OSError, TimeoutError):
                if admitted:
                    self._live = False
                    self._state, self._error = "failed", "volume_unconfirmed"
                    self._volume_confirmed = False
                    return self._result(), 503
                return self._result(error="test_unavailable"), 409
            finally:
                self._reservation = None

    async def _play(self, track_id):
        try:
            self.check()
            await self.api._play_tidal_track(track_id, _test_lease=self)
            self.check()
            self._state = "playing"
        except (BackendError, OSError, TimeoutError, asyncio.CancelledError):
            if self._live:
                await self._finish("failed", "playback_failed")

    async def before_load(self):
        self.check()
        raw = await self._raw_volume()
        self.check()
        snap = await self._snapshot()
        self.check()
        if raw != 1:
            self._volume_confirmed = False
            raise BackendError("Bounded TIDAL test gain unknown")
        if not self._idle(snap):
            self._superseded()
            raise BackendError("Bounded TIDAL test source changed")
        return True

    async def record_load(self, url, entry_id):
        self._backend_url = url
        if type(entry_id) is int and entry_id >= 0:
            self._entry_id = entry_id
            return
        # Some existing adapters discard loadfile's result node. While the
        # renderer mutation lock is still held and audio is paused, capture
        # exactly one matching native root; never manufacture it from metadata.
        async with asyncio.timeout(self._io_timeout):
            while True:
                snap = await self._snapshot()
                if not self.owns_selection():
                    self._superseded()
                    raise BackendError("Bounded TIDAL test source changed")
                if snap.get("backend-error"):
                    raise BackendError("Bounded TIDAL test native ownership unknown")
                active = self.api._native_active(snap)
                if (self.api._native_source_observation_coherent(snap) and active is not None
                        and snap.get("pause") is True and snap.get("path") == url
                        and active.get("filename") == url and not snap.get("playlist-path")
                        and type(active.get("id")) is int and active["id"] >= 0):
                    self._entry_id = active["id"]
                    return
                if self._foreign_native(snap):
                    self._superseded()
                    raise BackendError("Bounded TIDAL test source changed")
                await asyncio.sleep(.02)

    def _owned_native(self, snap):
        entries = snap.get("playlist")
        if not isinstance(entries, list):
            return False
        current = [entry for entry in entries if isinstance(entry, dict)
                   and (entry.get("current") is True or entry.get("playing") is True)]
        return (len(current) == 1 and type(self._entry_id) is int
                and current[0].get("id") == self._entry_id
                and current[0].get("filename") == self._backend_url
                and (snap.get("path") == self._backend_url
                     or snap.get("pause") is True and snap.get("path") in (None, "")))

    def _foreign_native(self, snap):
        return (not snap.get("backend-error") and self.api._native_source_observation_coherent(snap)
                and not self._owned_native(snap)
                and (self._entry_id is not None or snap.get("path") != self._backend_url))

    async def cleanup_failed_load(self):
        await self._finish("failed", "playback_failed")
        if self._error == "stop_unconfirmed":
            raise BackendError("Bounded TIDAL test stop unconfirmed")

    async def before_resume(self):
        self.check()
        raw = await self._raw_volume()
        self.check()
        if raw != 1:
            self._volume_confirmed = False
            raise BackendError("Bounded TIDAL test gain unknown")
        return True

    def _observe(self, snap):
        position = snap.get("playback-time")
        if type(position) in {int, float} and math.isfinite(position) and position >= 0:
            self._position = position
            if self._first_position is None:
                self._first_position = position
            self._progress = max(self._progress, position - self._first_position)
        info = snap.get("audio-info")
        self._decoded |= (isinstance(info, dict) and info.get("entry_id") == self._entry_id
                          and isinstance(info.get("codec"), str) and bool(info["codec"]))

    async def _expire(self):
        # Separate deadline task: a slow native observation cannot postpone it.
        while self._live and self._clock() < self._deadline:
            await asyncio.sleep(min(self._poll_interval, max(0, self._deadline - self._clock())))
        if self._live:
            await self._finish("expired")

    async def _watch(self):
        while self._live:
            await asyncio.sleep(min(self._poll_interval, max(0, self._deadline - self._clock())))
            if not self._live:
                break
            if not self.owns_selection():
                self._superseded()
                break
            if self._clock() >= self._deadline:
                await self._finish("expired")
                break
            try:
                async with asyncio.timeout(self._io_timeout):
                    raw = await self._raw_volume()
                    if not self.owns_selection():
                        self._superseded()
                        break
                    if not self._live:
                        break
                    if raw > 1:
                        self._volume_confirmed = False
                        await self._finish("failed", "volume_limit_exceeded")
                        break
                    self._volume_confirmed = raw == 1
                    if self._backend_url and self._state == "playing":
                        snap = await self._snapshot()
                        if not self.owns_selection():
                            self._superseded()
                            break
                        if not self._live:
                            break
                        if snap.get("backend-error"):
                            raise BackendError("Bounded TIDAL test observation unavailable")
                        if self._idle(snap):
                            await self._finish("stopped")
                            break
                        if not self._owned_native(snap):
                            if self._foreign_native(snap):
                                self._superseded()
                                break
                            raise BackendError("Bounded TIDAL test ownership unknown")
                        self._observe(snap)
            except (BackendError, OSError, TimeoutError):
                self._volume_confirmed = False
                await self._finish("failed", "volume_unconfirmed")
                break

    async def _finish(self, state, error=None):
        async with self._finish_lock:
            if not self.owns_selection():
                self._superseded()
                return
            if not self._live and self._state not in {"stopping", "failed"}:
                return
            self._live = False  # Fence late resolver, load-lock and resume first.
            self._state = "stopping"
            try:
                async with asyncio.timeout(self._io_timeout), self.api._backend_lock:
                    if not self.owns_selection():
                        self._superseded()
                        return
                    snap = await self._snapshot()
                    if not self.owns_selection():
                        self._superseded()
                        return
                    if snap.get("backend-error"):
                        raise BackendError("Bounded TIDAL stop observation unavailable")
                    if not self._idle(snap):
                        if not self._backend_url or not self._owned_native(snap):
                            if self._foreign_native(snap):
                                self._superseded()
                                return
                            raise BackendError("Bounded TIDAL test ownership unknown")
                        await self.api._backend_call(self.api.mpv.stop())
                        while True:
                            snap = await self._snapshot()
                            if not self.owns_selection():
                                self._superseded()
                                return
                            if self._idle(snap):
                                break
                            if snap.get("backend-error") or self._foreign_native(snap):
                                raise BackendError("Bounded TIDAL stop unconfirmed")
                            await asyncio.sleep(.02)
                    self.api.state.backend_url = self.api.state.backend_original_url = ""
                    self.api.state.mark_stop()
                    item = self._queue.current
                    if item is not None and item._tidal_test_lease is self:
                        # A later explicit normal Play is a new owner. Keep the
                        # guard attached whenever silence remains unconfirmed.
                        item._tidal_test_lease = None
                    self.api._schedule_event_notify()
                self._state, self._error = state, error
                self._volume_confirmed = False
            except (BackendError, OSError, TimeoutError, asyncio.CancelledError):
                if not self.owns_selection():
                    self._superseded()
                    return
                self._state, self._error = "failed", "stop_unconfirmed"
                self._volume_confirmed = False
                self.api.state.mark_error("Bounded TIDAL test stop unconfirmed; actual output unknown")

    async def stop(self, lease_id):
        if lease_id != self._lease_id or self._lease_id is None:
            return self._result(error="lease_mismatch"), 409
        if self._live or self._state == "stopping" or self._error == "stop_unconfirmed":
            task = asyncio.create_task(self._finish("stopped"))
            self._start_tasks.add(task)
            task.add_done_callback(self._start_tasks.discard)
            await asyncio.shield(task)
        return self._result(), 503 if self._error == "stop_unconfirmed" else 200

    async def close(self):
        self._closed = True
        if self._start_tasks:
            await asyncio.gather(*tuple(self._start_tasks), return_exceptions=True)
        if self._live or self._error == "stop_unconfirmed":
            await self._finish("stopped")
        for task in (self._play_task, self._watch_task, self._deadline_task):
            if task is not None and not task.done():
                task.cancel()
        tasks = [t for t in (self._play_task, self._watch_task, self._deadline_task) if t is not None]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self._io_timeout)
            # A cancelled provider may take longer to unwind. Closed admission
            # fences it permanently; shutdown must not wait unboundedly for it.
            for task in pending:
                task.cancel()
