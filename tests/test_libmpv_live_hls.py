"""REAL_BACKEND/libmpv/null: rolling synthetic HLS, outage and owner fencing.

No Unix IPC, WAN, provider accounts, DRM, adaptive variants or physical output.
The fixture is an HTTP publisher, never an HLS player; libmpv does all fetching,
demuxing, decoding and playback. Production HttpApi/backend behavior is inherited.
"""
from __future__ import annotations

import asyncio
from bisect import bisect_right
from contextlib import asynccontextmanager
import hashlib
import json
import locale
import os
from pathlib import Path
import socket
import subprocess
import sys
from typing import Any

import pytest
from aiohttp import ClientSession, ClientTimeout, web

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.queue import PlaybackQueue, QueueItem, serialize_queue_context
from fake_linkplay.state import PlaybackPhase, RendererState
from libmpv_stability_support import Events, SERVICES
from test_libmpv_integration import _eventually, _write_tone
from test_libmpv_formats import _decoder_progress
from test_libmpv_recovery import _StationBackend
from test_metadata_surfaces import MetadataRenderer


def generate(ffmpeg: str, directory: Path) -> tuple[Path, list[tuple[float, str]]]:
    """Generate a bounded seed once; publisher only exposes clock-ready entries."""
    wav = directory / 'quiet.wav'
    _write_tone(wav, 440)
    manifest = directory / 'seed.m3u8'
    env = os.environ.copy()
    env.pop('LD_LIBRARY_PATH', None)  # System FFmpeg uses its own official libraries.
    result = subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
        '-stream_loop', '-1', '-i', str(wav), '-t', '140', '-map_metadata', '-1',
        '-threads', '1', '-c:a', 'aac', '-b:a', '48k', '-hls_time', '1', '-hls_list_size', '0',
        '-hls_segment_filename', str(directory / 'seg-%04d.ts'), str(manifest)],
        env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    lines = manifest.read_text().splitlines()
    entries = [(float(line.split(':')[1].rstrip(',')), lines[i + 1])
               for i, line in enumerate(lines) if line.startswith('#EXTINF:')]
    assert len(entries) > 100 and all(name.startswith('seg-') and '/' not in name for _, name in entries)
    return wav, entries


class RollingHLS:
    def __init__(self, directory: Path, entries: list[tuple[float, str]]):
        self.directory, self.entries = directory, entries
        self.ends = []
        for duration, _ in entries:
            self.ends.append((self.ends[-1] if self.ends else 0) + duration)
        self.started = asyncio.get_running_loop().time()
        self.outage = ''
        self.requests: list[dict[str, Any]] = []
        self.loads = lambda: 0
        self.names = {name for _, name in entries}

    async def handle(self, request: web.Request) -> web.Response:
        assert request.remote == '127.0.0.1'
        elapsed = asyncio.get_running_loop().time() - self.started
        name = request.match_info['name']
        playlist = name == 'live.m3u8'
        status = 503 if self.outage in {'all', 'playlist' if playlist else 'segment'} else 200
        entry: dict[str, Any] = {'at': round(elapsed, 3), 'name': name, 'status': status,
                                 'loads': self.loads()}
        self.requests.append(entry)
        if status == 503:
            return web.Response(status=503, text='Bounded synthetic outage')
        if playlist:
            # Real media timestamps advance monotonically. Keep a five-segment
            # sliding window with a four-second initial availability lead.
            end = bisect_right(self.ends, elapsed + 4.1)
            assert end < len(self.entries), 'Fixture duration exhausted'
            start = max(0, end - 5)
            entry['sequence'] = start
            body = '#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n'
            body += f'#EXT-X-MEDIA-SEQUENCE:{start}\n'
            body += ''.join(f'#EXTINF:{duration:.6f},\n{filename}\n'
                            for duration, filename in self.entries[start:end])
            assert '#EXT-X-ENDLIST' not in body and '#EXT-X-KEY' not in body
            return web.Response(text=body, content_type='application/vnd.apple.mpegurl',
                                headers={'Cache-Control': 'no-store'})
        if name not in self.names:
            raise web.HTTPNotFound()
        return web.Response(body=(self.directory / name).read_bytes(), content_type='video/mp2t')


@asynccontextmanager
async def renderer(library: str, directory: Path, wav: Path, entries):
    baseline = asyncio.all_tasks()
    listener = backend = runner = client = monitor = api = None
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(('127.0.0.1', 0))
        listener.listen(socket.SOMAXCONN)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        url = base + '/_hls/live.m3u8'
        fixture = RollingHLS(directory, entries)
        backend = _StationBackend(library, (wav, wav), url)
        fixture.loads = lambda: len(backend.loads)
        cfg = Config(uuid='00000000-0000-4000-8000-000000000000', advertise_ip='127.0.0.1',
            bind_address='127.0.0.1', http_api_port=port, upnp_port=port, state_dir=str(directory / 'state'),
            auto_spawn_mpv=False, enable_ssdp=False, enable_mdns=False)
        api = HttpApi(cfg, RendererState.from_config(cfg), backend)
        assert api._station_stall_timeout == 15.0
        assert api._station_recovery_delays == (0.5, 1.0, 2.0)
        events = Events()
        app = api.app()
        app.router.add_get('/_hls/{name}', fixture.handle)
        app.router.add_route('NOTIFY', '/_events/{service}', events.callback)
        runner = web.AppRunner(app, shutdown_timeout=1)
        await runner.setup()
        await web.SockSite(runner, listener).start()
        monitor = api._state_monitor_task
        async with ClientSession(timeout=ClientTimeout(total=15), trust_env=False) as client:
            env = MetadataRenderer(api, backend, client, base, {})
            for service in SERVICES:
                async with client.request('SUBSCRIBE', base + '/upnp/event/' + service, headers={
                    'NT': 'upnp:event', 'CALLBACK': f'<{base}/_events/{service}>',
                    'TIMEOUT': 'Second-300'}) as response:
                    assert response.status == 200
            yield env, fixture, events, url
    finally:
        try:
            if runner is not None:
                await runner.cleanup()
        finally:
            try:
                if listener is not None:
                    listener.close()
            finally:
                if backend is not None:
                    backend.close()
            await asyncio.sleep(0)
            pending = [task for task in asyncio.all_tasks() - baseline if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            assert not pending, f'Leaked tasks: {pending!r}'
            assert backend is None or (backend._handle is None and backend.process is None)
            assert listener is None or listener.fileno() == -1
            assert runner is None or not runner.sites
            assert client is None or client.closed
            assert monitor is None or monitor.done()
            assert api is None or api._station_recovery_task is None


async def exercise(library: str, ffmpeg: str, directory: Path, scenario: str) -> dict:
    wav, entries = generate(ffmpeg, directory)
    evidence: dict[str, Any] = {'lane': 'REAL_BACKEND/libmpv/null', 'scenario': scenario,
        'playlist': 'rolling five-segment window; advancing sequence; no ENDLIST', 'surfaces': []}
    production = Path(__file__).resolve().parents[1] / 'fake_linkplay'
    digest = hashlib.sha256()
    for path in sorted(production.rglob('*.py')):
        digest.update(path.relative_to(production).as_posix().encode() + b'\0')
        digest.update(path.read_bytes() + b'\0')
    evidence['production_sha256'] = digest.hexdigest()
    evidence['fixture_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    async with renderer(library, directory, wav, entries) as (env, fixture, events, url):
        api, backend = env.api, env.backend
        evidence['mpv_version'] = backend.read_property('mpv-version')
        async def command(value: str):
            async with env.client.get(env.base + '/httpapi.asp', params={'command': value}) as response:
                text = await response.text()
                assert response.status == 200, text
                return json.loads(text) if response.content_type == 'application/json' else text

        async def surface(status: str, uri: str = url, index: str = '1'):
            # Wait on unsolicited GENA BEFORE any read can reconcile state.
            transport = {'play': 'PLAYING', 'transition': 'TRANSITIONING',
                         'pause': 'PAUSED_PLAYBACK', 'stop': 'STOPPED'}[status]
            await _eventually('autonomous honest GENA', lambda: bool(events.receipts['AVTransport'])
                and events.receipts['AVTransport'][-1][1].get('TransportState') == transport)
            props = events.receipts['AVTransport'][-1][1]
            assert props['TrackURI'] == uri and props['last:CurrentTrackURI'] == uri, props
            assert props['last:TransportState'] == transport and props['last:TransportStatus'] == 'OK', props
            await _eventually('canonical queue GENA', lambda: bool(events.receipts['PlayQueue']) and all(
                events.receipts['PlayQueue'][-1][1].get(key) == value
                for key, value in {'TrackURI': uri, 'CurrentIndex': index, 'Count': '1'}.items()))
            await _eventually('confirmed gain GENA', lambda: bool(events.receipts['RenderingControl']) and all(
                events.receipts['RenderingControl'][-1][1].get(key) == value
                for key, value in {'Volume': '37', 'Mute': '1', 'last:Volume': '37', 'last:Mute': '1'}.items()))
            body = await command('getPlayerStatus')
            assert body['status'] == status and body['stream_url'] == uri, body
            assert body['plicurr'] == index and body['plicount'] == '1', body
            assert (body['vol'], body['mute']) == ('37', '1')
            if status != 'transition':
                assert body['transport_error'] == ''
            if uri == url:
                assert body['totlen'] == '0'
            position = await env.soap('GetPositionInfo')
            info = await env.soap('GetTransportInfo')
            assert position['TrackURI'] == uri and position['Track'] == index, position
            assert info['CurrentTransportState'] == transport and info['CurrentTransportStatus'] == 'OK', info
            queue = await env.soap('GetQueueIndex', service='PlayQueue')
            assert queue['CurrentIndex'] == index and queue['Count'] == '1', queue
            gain = {'InstanceID': '0', 'Channel': 'Master'}
            assert (await env.soap('GetVolume', gain, 'RenderingControl'))['CurrentVolume'] == '37'
            assert (await env.soap('GetMute', gain, 'RenderingControl'))['CurrentMute'] == '1'
            if uri == url:
                assert position['TrackDuration'] == '00:00:00'
            assert not events.errors, events.errors
            evidence['surfaces'].append({'status': status, 'transport': transport,
                                         'error': body['transport_error'], 'uri': uri})

        await command('setPlayerCmd:vol:37')
        await command('setPlayerCmd:mute:1')
        queue = PlaybackQueue(name='Synthetic live HLS', items=[QueueItem(url,
            title='Canonical live HLS', stream_type='station', duration=0)], index=1)
        await env.soap('CreateQueue', {'QueueContext': serialize_queue_context(queue)}, 'PlayQueue')
        await env.soap('PlayQueueWithIndex', {'Index': '1'}, 'PlayQueue')
        selected_queue, selected = api.state.queue, api.state.queue.current
        generation = api.state.generation
        evidence['initial_progress'] = await _decoder_progress(backend, url, 'aac')
        await _eventually('genuine rolling HLS reloads', lambda: len({r['sequence'] for r in fixture.requests
            if 'sequence' in r}) >= 3 and backend.read_property('playback-time') > 5, timeout=12)
        assert backend.loads == [url]
        native_entry = backend.read_property('playlist')[0]['id']
        assert selected.stream_type == 'station' and selected.duration == 0
        assert selected_queue.index == selected_queue.count == 1
        await surface('play')
        before = len(fixture.requests)
        fixture.outage = 'segment' if scenario == 'segment-return' else 'all'
        await _eventually('actual HLS outage reached decoder', lambda: api.state.phase == PlaybackPhase.BUFFERING,
                          timeout=12)
        await _eventually('actual outage request', lambda: any(r['status'] == 503 for r in fixture.requests[before:]))
        assert api.state.queue is selected_queue and api.state.queue.current is selected
        assert api.state.generation == generation and api.state.current_url == url and api.state.totlen == 0
        await surface('transition')
        evidence['outage_snapshot'] = await backend.snapshot()
        if scenario == 'segment-return':
            native = evidence['outage_snapshot']
            assert native['idle-active'] is False and native['eof-reached'] is False
            assert native['paused-for-cache'] is True and native['core-idle'] is True
            assert any(r['status'] == 503 and r['name'].endswith('.ts') for r in fixture.requests[before:])
            stalled_position = backend.read_property('playback-time')
            await asyncio.sleep(2)
            assert backend.read_property('playback-time') == pytest.approx(stalled_position, abs=.001)
            fixture.outage = ''
        else:
            await _eventually('renderer recovery backoff', lambda: api._station_recovery_task is not None,
                              timeout=20)
            task = api._station_recovery_task
            if scenario == 'recover':
                await _eventually('failed renderer reload reached unavailable playlist', lambda: any(
                    r['status'] == 503 and r['name'] == 'live.m3u8' and r['loads'] >= 2
                    for r in fixture.requests), timeout=5)
                await _eventually('failed HLS reload returned to backoff', lambda:
                    api.state.last_error == 'Station source unavailable; retry pending', timeout=12)
                fixture.outage = ''
            else:
                assert scenario in {'pause', 'stop', 'newsource'}
                recovery = api._station_recovery
                assert recovery is not None and recovery.attempts == 0
                assert task is not None and not task.done()
                assert api._station_recovery_loading is False and backend.loads == [url]
                evidence['owner_intervention'] = {'phase': 'retry-backoff',
                    'attempts': recovery.attempts, 'renderer_loads': len(backend.loads),
                    'at_seconds': round(asyncio.get_running_loop().time() - fixture.started, 3)}
                value = 'play:' + str(wav) if scenario == 'newsource' else scenario
                await command('setPlayerCmd:' + value)
                owner_loads = len(backend.loads)
                owner_generation = api.state.generation
                assert owner_generation > generation
                owner_requests = len(fixture.requests)
                fixture.outage = ''
                await asyncio.sleep(sum(api._station_recovery_delays) + api._monitor_interval + .5)
                assert len(backend.loads) == owner_loads, backend.loads
                assert len(fixture.requests) == owner_requests, 'Stale native HLS fetch after owner command'
                assert api._station_recovery is None and task.done()
                assert api.state.generation == owner_generation
                if scenario == 'newsource':
                    assert backend.read_property('path') == str(wav)
                    await surface('play', str(wav))
                    evidence['replacement_progress'] = await _decoder_progress(backend, str(wav), 'pcm_s16le')
                else:
                    assert api.state.current_url == url and api.state.queue.current is selected
                    await surface(scenario)
                    assert backend.read_property('idle-active') is True and api.state.intent == scenario
                evidence['owner_fenced_stale_reload'] = True
        if scenario in {'recover', 'segment-return'}:
            await _eventually('HLS decoder recovered after return', lambda: api.state.phase == PlaybackPhase.PLAYING
                and api.state.last_error == '', timeout=22)
            evidence['returned_progress'] = await _decoder_progress(backend, url, 'aac')
            returned_at = len(fixture.requests)
            await _eventually('live playlist keeps rolling after return', lambda: len({r['sequence']
                for r in fixture.requests[returned_at:] if 'sequence' in r}) >= 2, timeout=5)
            evidence['sustained_return_progress'] = await _decoder_progress(backend, url, 'aac')
            if scenario == 'segment-return':
                assert backend.loads == [url]
                assert backend.read_property('playlist')[0]['id'] == native_entry
                assert api._station_recovery.attempts == 0
            else:
                assert backend.loads == [url] * 3 and api._station_recovery.attempts == 2
            assert api.state.queue is selected_queue and api.state.queue.current is selected
            assert api.state.generation == generation and api.state.current_url == url and api.state.totlen == 0
            await surface('play')
        assert backend.read_property('volume') == pytest.approx(37, abs=.01)
        assert backend.read_property('mute') is True
        evidence.update(loads=backend.loads, requests=fixture.requests, gain={'volume': 37, 'mute': True},
                        production_stall_grace=api._station_stall_timeout,
                        production_retry_delays=api._station_recovery_delays)
    evidence['cleanup'] = {'handle_closed': True, 'listener_closed': True, 'no_pending_tasks': True}
    return evidence


@pytest.mark.skipif(not os.environ.get('ORR_TEST_LIBMPV_PATH'), reason='Explicit real libmpv opt-in required')
@pytest.mark.parametrize('scenario', ['recover', 'segment-return', 'pause', 'stop', 'newsource'])
def test_real_live_hls(tmp_path: Path, scenario: str):
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env['PYTHONPATH'] = str(root) + os.pathsep + str(root / 'tests')
    for name in tuple(env):
        if name.lower() in {'http_proxy', 'https_proxy', 'all_proxy'}:
            env.pop(name)
    env['NO_PROXY'] = env['no_proxy'] = '127.0.0.1,localhost'
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--probe',
        env['ORR_TEST_LIBMPV_PATH'], env.get('ORR_TEST_FFMPEG_BINARY', '/usr/bin/ffmpeg'),
        str(tmp_path), scenario], env=env, cwd=root, text=True, capture_output=True, timeout=110)
    assert result.returncode == 0, result.stdout + '\n' + result.stderr
    evidence = json.loads(result.stdout)
    assert evidence['cleanup']['no_pending_tasks']
    print(json.dumps(evidence, sort_keys=True))


if __name__ == '__main__':
    assert len(sys.argv) == 6 and sys.argv[1] == '--probe'
    locale.setlocale(locale.LC_NUMERIC, 'C')
    async def main():
        async with asyncio.timeout(100):
            result = await exercise(sys.argv[2], sys.argv[3], Path(sys.argv[4]), sys.argv[5])
        # Publish aggregate synthetic-request observations, not a request trace.
        requests = result.pop('requests')
        result['request_counts'] = {
            f'{kind}_{status}': sum((entry['name'] == 'live.m3u8') == (kind == 'playlist')
                                   and entry['status'] == status for entry in requests)
            for kind in ('playlist', 'segment') for status in (200, 503)
        }
        sequences = [entry['sequence'] for entry in requests if 'sequence' in entry]
        result['playlist_sequences'] = {'distinct': len(set(sequences)),
                                       'first': min(sequences), 'last': max(sequences)}
        result['failed_playlist_load_attempts'] = sorted({entry['loads'] for entry in requests
            if entry['name'] == 'live.m3u8' and entry['status'] == 503})
        print(json.dumps(result, sort_keys=True))
    asyncio.run(main())
