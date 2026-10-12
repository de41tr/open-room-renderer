"""One source, one handle, paced synthetic ADTS, and bounded aggregate evidence.

Media generation follows https://ffmpeg.org/ffmpeg-filters.html#sine and
https://ffmpeg.org/ffmpeg-formats.html#adts. No provider or physical audio.
"""
from __future__ import annotations

import asyncio
from collections import Counter
import ctypes as ct
from datetime import datetime, timezone
import gc
from html import escape
import json
import math
from pathlib import Path
import platform
import socket
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.queue import PlaybackQueue, QueueItem, serialize_queue_context
from fake_linkplay.state import PlaybackPhase, RendererState
from libmpv_stability_support import (
    CLEAN_BUDGET, LIVE_BUDGET, SERVICES, CountingBackend, Events, fields,
    resources, within_budget,
)
from test_libmpv_integration import _eventually


FRAME_SECONDS = 1024 / 16000


def adts_frames(data: bytes) -> tuple[bytes, ...]:
    """Accept only the fixed generated AAC-LC/16 kHz/mono/one-block format."""
    frames, offset = [], 0
    assert 0 < len(data) <= 64 * 1024, "Seed must be bounded"
    while offset < len(data):
        header = data[offset:offset + 7]
        assert len(header) == 7 and header[0] == 255 and header[1] == 241
        assert header[2] >> 6 == 1 and (header[2] >> 2) & 15 == 8
        assert ((header[2] & 1) << 2 | header[3] >> 6) == 1
        assert header[6] & 3 == 0, "One AAC raw block per ADTS frame required"
        length = ((header[3] & 3) << 11) | (header[4] << 3) | (header[5] >> 5)
        assert 7 < length <= len(data) - offset
        frames.append(data[offset:offset + length])
        offset += length
    assert len(frames) >= 16
    return tuple(frames)



class PacedStation:
    def __init__(self, frames: tuple[bytes, ...]) -> None:
        self.frames = frames
        self.requests = self.active = self.sent_frames = self.sent_bytes = 0
        self.disconnects = 0
        self.stopping = False
        self.max_lateness = 0.0
        self.closed = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def handle(self, request: web.Request) -> web.StreamResponse:
        assert request.remote == '127.0.0.1'
        self.requests += 1
        assert self.requests == 1, 'An uninterrupted station must not reconnect'
        assert request.headers.get('Range') in (None, 'bytes=0-'), 'No seeks in the uninterrupted baseline'
        self.active += 1
        self.task = asyncio.current_task()
        response = web.StreamResponse(headers={'Content-Type': 'audio/aac', 'Cache-Control': 'no-store'})
        await response.prepare(request)
        clock = asyncio.get_running_loop().time
        started = clock()
        try:
            while not self.stopping:
                # Two-second bounded startup lead helps decoder probing. Then
                # every complete frame is paced at exactly its media duration.
                due = started + max(0.0, self.sent_frames * FRAME_SECONDS - 2)
                await asyncio.sleep(max(0.0, due - clock()))
                self.max_lateness = max(self.max_lateness, clock() - due)
                frame = self.frames[self.sent_frames % len(self.frames)]
                await response.write(frame)
                self.sent_bytes += len(frame)
                self.sent_frames += 1
        except (ConnectionError, asyncio.CancelledError):
            if not self.stopping:
                self.disconnects += 1
            raise
        finally:
            self.active -= 1
            self.closed.set()
        return response


class NativeEvent(ct.Structure):
    _fields_ = [('event_id', ct.c_int), ('error', ct.c_int),
                ('reply_userdata', ct.c_uint64), ('data', ct.c_void_p)]


class SustainedBackend(CountingBackend):
    def __init__(self, library: str, station_url: str) -> None:
        super().__init__(library, (), station_url)
        self._lib.mpv_wait_event.restype = ct.POINTER(NativeEvent)
        self._lib.mpv_wait_event.argtypes = [ct.c_void_p, ct.c_double]
        self.events: Counter[int] = Counter()
        self.last_snapshot_window: tuple[float, float] | None = None

    async def snapshot(self) -> dict[str, Any]:
        # Bracket the actual inherited property reads, not merely their later
        # application. This conservatively includes native read/queue latency.
        clock = asyncio.get_running_loop().time
        started = clock()
        snapshot = await super().snapshot()
        self.last_snapshot_window = (started, clock())
        return snapshot

    def drain_events(self) -> None:
        for _ in range(1024):
            event = self._lib.mpv_wait_event(self._handle, 0).contents
            if event.event_id == 0:
                return
            self.events[event.event_id] += 1
            assert event.error >= 0, 'Native event reported failure'
            assert event.event_id not in {1, 7, 24}, 'Unexpected shutdown, end-file, or event overflow'
        raise AssertionError('Native event queue did not drain')



class ObservedHttpApi(HttpApi):
    """Record production observations without changing their application."""
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.last_observation: dict[str, float] | None = None
        self.last_monitor_observation: dict[str, float] | None = None
        self.monitor_observations = 0

    def _apply_backend_snapshot(self, snapshot: dict) -> None:
        super()._apply_backend_snapshot(snapshot)
        raw = snapshot.get('playback-time')
        if type(raw) not in (int, float) or self.state.phase != PlaybackPhase.PLAYING:
            return
        assert self.mpv.last_snapshot_window is not None
        started, completed = self.mpv.last_snapshot_window
        observation = {'raw_position': raw, 'published_position': self.state.curpos,
                       'read_started': started, 'read_completed': completed,
                       'applied_at': asyncio.get_running_loop().time()}
        self.last_observation = observation
        if asyncio.current_task() is self._state_monitor_task:
            self.last_monitor_observation = observation
            self.monitor_observations += 1


def validate_observed_position(observation: dict[str, float], *, now: float,
                               native: float, monitor_interval: float) -> float:
    """Separate exact integer serialization from age of the real observation.

    Published seconds intentionally floor native playback-time. An asynchronous
    monitor's integer may therefore lag a newer native float by nearly 1 second
    plus the monitor interval and snapshot work. Never add those unlike terms
    into one raw-position tolerance. Read-start freshness also prevents a newly
    applied, long-blocked snapshot from appearing current.
    """
    raw = observation['raw_position']
    assert math.isfinite(raw) and raw >= 0
    assert observation['published_position'] == int(raw), ('Incorrect native position serialization', observation)
    started, completed, applied = (observation[key] for key in ('read_started', 'read_completed', 'applied_at'))
    assert started <= completed <= applied <= now
    age = now - started
    assert 0 <= age <= monitor_interval * 2, ('Stale monitor observation', age, observation)
    assert completed - started <= monitor_interval, ('Slow native snapshot', observation)
    # Compare native floats from their actual observation times. The 0.1-second
    # clock/read allowance is independent of the whole-second publication.
    assert -.1 <= native - raw <= age + .1, ('Stale/jumped native observation', native, age, observation)
    return age

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_progress(directory: Path, value: dict[str, Any]) -> None:
    path = directory / 'progress.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, sort_keys=True) + '\n')
    temporary.replace(path)


def hms_seconds(value: str) -> int:
    hours, minutes, seconds = map(int, value.split(':'))
    return hours * 3600 + minutes * 60 + seconds


async def exercise(library: str, directory: Path, seconds: int, source_sha256: str) -> dict[str, Any]:
    before_tasks, before = asyncio.all_tasks(), resources()
    loop = asyncio.get_running_loop()
    total_started = loop.time()
    result: dict[str, Any] = {
        'lane': 'REAL_BACKEND/null uninterrupted synthetic HTTP station',
        'requested_baseline_seconds': seconds, 'started_utc': utc_now(),
        'architecture': platform.machine(), 'python': platform.python_version(),
        'source_sha256': source_sha256, 'agreements': 0, 'samples': 0,
        'budgets': {'live_above_warm_baseline': LIVE_BUDGET, 'cleanup_above_initial': CLEAN_BUDGET},
        'scope': 'One libmpv handle; loopback AAC/ADTS; null audio; no Unix IPC, hardware, original app, or WAN certification',
    }
    seed = (directory / 'quiet.aac').read_bytes()
    fixture = PacedStation(adts_frames(seed))
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    runner = backend = api = monitor = session = None
    events, subscriptions = Events(), {}
    try:
        listener.bind(('127.0.0.1', 0))
        listener.listen(socket.SOMAXCONN)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        url = base + '/_synthetic_station.aac'
        backend = SustainedBackend(library, url)
        result['mpv_version'] = backend.read_property('mpv-version')
        cfg = Config(uuid='00000000-0000-4000-8000-000000000000', advertise_ip='127.0.0.1',
                     bind_address='127.0.0.1', http_api_port=port, upnp_port=port,
                     state_dir=str(directory / 'state'), auto_spawn_mpv=False, enable_ssdp=False, enable_mdns=False)
        api = ObservedHttpApi(cfg, RendererState.from_config(cfg), backend)
        assert api._station_stall_timeout == 15.0 and not cfg.native_queue_staging
        app = api.app()
        app.router.add_get('/_synthetic_station.aac', fixture.handle)
        app.router.add_route('NOTIFY', '/_events/{service}', events.callback)
        runner = web.AppRunner(app, shutdown_timeout=1)
        await runner.setup()
        await web.SockSite(runner, listener).start()
        monitor = api._state_monitor_task
        async with ClientSession(timeout=ClientTimeout(total=10), trust_env=False) as session:
            async def command(value: str) -> Any:
                async with session.get(base + '/httpapi.asp', params={'command': value}) as response:
                    body = await response.text()
                    assert response.status == 200
                    return json.loads(body) if response.content_type == 'application/json' else body

            async def soap(action: str, values: str = '', service: str = 'AVTransport') -> dict[str, str]:
                namespace = ('urn:schemas-wiimu-com:service:PlayQueue:1' if service == 'PlayQueue'
                             else f'urn:schemas-upnp-org:service:{service}:1')
                body = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                        f'<u:{action} xmlns:u="{namespace}">{values}</u:{action}></s:Body></s:Envelope>')
                async with session.post(base + '/upnp/control/' + service, data=body.encode(), headers={
                    'SOAPACTION': f'"{namespace}#{action}"', 'Content-Type': 'text/xml'
                }) as response:
                    body = await response.text()
                    assert response.status == 200, (service, action, response.status)
                    return fields(body)

            async def delivered(service: str, expected: dict[str, str]) -> None:
                def matches() -> bool:
                    if not events.receipts[service]:
                        return False
                    sid, props = events.receipts[service][-1]
                    return sid == subscriptions[service] and all(props.get(k) == v for k, v in expected.items())
                await _eventually('acknowledged consistent GENA state', matches)
                assert not events.errors, events.errors

            async def subscribe(renew: bool = False) -> None:
                for service in SERVICES:
                    headers = {'SID': subscriptions[service]} if renew else {
                        'NT': 'upnp:event', 'CALLBACK': f'<{base}/_events/{service}>'}
                    headers['TIMEOUT'] = 'Second-300'
                    async with session.request('SUBSCRIBE', base + '/upnp/event/' + service, headers=headers) as response:
                        assert response.status == 200
                        if renew:
                            assert subscriptions[service] == response.headers['SID']
                        subscriptions[service] = response.headers['SID']
                    await delivered(service, {})

            await command('getPlayerStatus')
            await command('setPlayerCmd:vol:37')
            await subscribe()
            queue = PlaybackQueue(name='Synthetic continuous station', items=[
                QueueItem(url, title='Synthetic quiet tone', stream_type='station', duration=0)], index=1)
            context = serialize_queue_context(queue)
            await soap('CreateQueue', f'<QueueContext>{escape(context)}</QueueContext>', 'PlayQueue')
            await soap('PlayQueueWithIndex', '<QueueName>Synthetic continuous station</QueueName><Index>1</Index>', 'PlayQueue')
            await _eventually('native station decoded progress', lambda: backend.read_property('playback-time') >= 2, timeout=8)
            await asyncio.sleep(3)  # Warmup is excluded from the full baseline.
            selected_queue, selected_item = api.state.queue, api.state.queue.current
            generation = api.state.generation

            async def agreement() -> None:
                body = await command('getPlayerStatus')
                actual_before = backend.read_property('playback-time')
                assert body['status'] == 'play' and body['transport_error'] == ''
                assert (body['plicurr'], body['plicount'], body['vol'], body['mute']) == ('1', '1', '37', '0')
                assert body['stream_url'] == url and abs(float(body['curpos']) - actual_before) <= 1.5
                transport = await soap('GetTransportInfo')
                assert transport['CurrentTransportState'] == 'PLAYING' and transport['CurrentTransportStatus'] == 'OK'
                position = await soap('GetPositionInfo')
                actual_after = backend.read_property('playback-time')
                assert position['Track'] == '1' and position['TrackURI'] == url
                assert abs(hms_seconds(position['RelTime']) - actual_after) <= 1.5
                queue_state = await soap('GetQueueIndex', service='PlayQueue')
                assert queue_state['CurrentIndex'] == queue_state['Count'] == '1'
                gain = '<InstanceID>0</InstanceID><Channel>Master</Channel>'
                assert (await soap('GetVolume', gain, 'RenderingControl'))['CurrentVolume'] == '37'
                assert (await soap('GetMute', gain, 'RenderingControl'))['CurrentMute'] == '0'
                await delivered('AVTransport', {'TransportState': 'PLAYING', 'TrackURI': url,
                    'last:TransportState': 'PLAYING', 'last:TransportStatus': 'OK',
                    'last:CurrentTrack': '1', 'last:CurrentTrackURI': url})
                await delivered('PlayQueue', {'CurrentIndex': '1', 'Count': '1', 'TrackURI': url})
                await delivered('RenderingControl', {'Volume': '37', 'Mute': '0', 'last:Volume': '37', 'last:Mute': '0'})
                result['agreements'] += 1

            await agreement()
            baseline = resources()
            started = last_sample_at = loop.time()
            first_position = last_position = backend.read_property('playback-time')
            result.update(baseline_started_utc=utc_now(), live_baseline=baseline, native_initial_seconds=first_position)
            maximum, minimum_step, max_gap = dict(baseline), math.inf, 0.0
            max_monitor_age = max_snapshot_duration = 0.0
            monitor_count = api.monitor_observations
            assert api.last_monitor_observation is not None
            monitor_position = api.last_monitor_observation['raw_position']
            next_agreement = next_checkpoint = next_renewal = started
            while True:
                await asyncio.sleep(1)
                assert not (directory / 'cancel-requested').exists(), 'Sustained run cancellation requested'
                now = loop.time()
                position = backend.read_property('playback-time')
                elapsed, gap, step = now - started, now - last_sample_at, position - last_position
                assert .25 <= step <= gap + 1.0, ('Native playback stopped or jumped', gap, step)
                assert gap <= 3.0, 'Resource/progress sampler was unresponsive'
                assert abs((position - first_position) - elapsed) <= 2.0, 'Playback did not advance in real time'
                minimum_step, max_gap = min(minimum_step, step), max(max_gap, gap)
                assert backend.loads == backend.source_changes == fixture.requests == fixture.active == 1
                assert fixture.disconnects == 0 and backend.process is None
                assert backend.read_property('path') == url
                assert backend.read_property('current-ao') == 'null'
                assert backend.read_property('audio-codec-name') == 'aac'
                assert backend.read_property('volume') == 37 and backend.read_property('mute') is False
                for property_name in ('pause', 'idle-active', 'eof-reached', 'core-idle', 'paused-for-cache'):
                    assert backend.read_property(property_name) is False, property_name
                assert api.state.phase == PlaybackPhase.PLAYING and api.state.last_error == ''
                assert api.state.generation == generation and api.state.queue is selected_queue
                assert api.state.queue.current is selected_item and api.state.queue.index == 1 and api.queue_is_live
                assert api.last_observation is not None and api.last_monitor_observation is not None
                assert api.state.curpos == api.last_observation['published_position']
                observation_now = loop.time()
                validate_observed_position(api.last_observation, now=observation_now,
                                           native=position, monitor_interval=api._monitor_interval)
                age = validate_observed_position(api.last_monitor_observation, now=observation_now,
                                                native=position, monitor_interval=api._monitor_interval)
                assert api.monitor_observations > monitor_count, 'Monitor did not observe between samples'
                assert api.last_monitor_observation['raw_position'] > monitor_position, 'Monitor position did not advance'
                monitor_count = api.monitor_observations
                monitor_position = api.last_monitor_observation['raw_position']
                max_monitor_age = max(max_monitor_age, age)
                max_snapshot_duration = max(max_snapshot_duration,
                    api.last_monitor_observation['read_completed'] - api.last_monitor_observation['read_started'])
                assert api._station_recovery is not None and api._station_recovery.established
                assert api._station_recovery.attempts == 0 and api._station_recovery_task is None
                assert api._station_stall_timeout == 15.0 and api._native_stage is None
                assert monitor is not None and not monitor.done()
                assert not events.errors and not events.pending_digest
                backend.drain_events()
                sample = resources()
                within_budget(sample, baseline, LIVE_BUDGET)
                maximum = {key: max(maximum[key], value) for key, value in sample.items()}
                result['samples'] += 1
                if now >= next_agreement or elapsed >= seconds:
                    await agreement()
                    next_agreement = now + 10
                if now >= next_renewal:
                    await subscribe(renew=True)
                    next_renewal = now + 120
                if now >= next_checkpoint or elapsed >= seconds:
                    write_progress(directory, {**result, 'status': 'running', 'baseline_elapsed_seconds': round(elapsed, 3),
                        'native_progress_seconds': round(position - first_position, 3), 'live_max': maximum,
                        'loads': backend.loads, 'station_requests': fixture.requests,
                        'native_events': dict(backend.events), 'monitor_observations': monitor_count,
                        'max_monitor_read_age_seconds': round(max_monitor_age, 6), 'updated_utc': utc_now()})
                    next_checkpoint = now + 60
                last_position, last_sample_at = position, now
                if elapsed >= seconds:
                    break
            assert backend.events[6] == backend.events[8] == 1 and backend.events[7] == 0
            result.update(baseline_ended_utc=utc_now(), baseline_elapsed_seconds=round(elapsed, 3),
                native_progress_seconds=round(position - first_position, 3), min_progress_per_sample_seconds=round(minimum_step, 4),
                max_sample_gap_seconds=round(max_gap, 4), native_events=dict(backend.events),
                monitor_observations=monitor_count, max_monitor_read_age_seconds=round(max_monitor_age, 6),
                max_monitor_snapshot_duration_seconds=round(max_snapshot_duration, 6),
                loads=backend.loads, source_changes=backend.source_changes, station_requests=fixture.requests,
                http_disconnects=fixture.disconnects, live_final=sample, live_max=maximum,
                events=dict(events.accepted), seed_bytes=len(seed), seed_frames=len(fixture.frames),
                streamed_bytes=fixture.sent_bytes, streamed_frames=fixture.sent_frames,
                max_frame_lateness_seconds=round(fixture.max_lateness, 4))
    finally:
        fixture.stopping = True
        try:
            if runner is not None:
                await asyncio.wait_for(runner.cleanup(), 8)
        finally:
            listener.close()
            if backend is not None:
                backend.close()
        await asyncio.sleep(.1)
        pending = [task for task in asyncio.all_tasks() - before_tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        assert not pending, f'Sustained fixture leaked {len(pending)} tasks'
        assert session is None or session.closed
        assert listener.fileno() == -1 and (runner is None or not runner.sites)
        assert backend is None or backend._handle is None
        assert monitor is None or monitor.done()
        assert fixture.active == 0 and (fixture.task is None or fixture.task.done())
        assert api is None or (not api.event_subscribers and not api._seek_tasks
            and api._state_monitor_task is None and api._station_recovery_task is None)
    gc.collect()
    after = resources()
    within_budget(after, before, CLEAN_BUDGET)
    result.update(ended_utc=utc_now(), total_elapsed_seconds=round(loop.time() - total_started, 3),
        after_cleanup=after, before_start=before, cleanup={'native_handle_closed': True, 'listener_closed': True,
        'client_closed': True, 'no_pending_tasks': True, 'no_subscriptions': True, 'no_child_processes': True,
        'stream_handler_finished': True})
    write_progress(directory, {**result, 'status': 'complete'})
    return result
