"""REAL_BACKEND/null: native child-local cache policy; no Unix IPC/hardware claim."""
from __future__ import annotations
import asyncio
import ctypes as ct
import json
import locale
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import wave

import pytest
from aiohttp import ClientSession, ClientTimeout, web

from fake_linkplay.config import Config
from fake_linkplay.mpv_backend import LIVE_STATION_BACK_BYTES
from fake_linkplay.http_api import HttpApi, ParsedCommand
from fake_linkplay.queue import PlaybackQueue, QueueItem
from fake_linkplay.state import RendererState, PlaybackPhase
from test_libmpv_integration import _write_tone, _eventually, _Node, _NodeList, _decode_node
from test_libmpv_recovery import _StationBackend
from test_libmpv_formats import _generate_media
from test_libmpv_live_metadata import ChangingICY, TITLES
from test_metadata_surfaces import (MetadataRenderer, assert_surfaces, property_fields,
    ARTIST, ALBUM, ARTWORK, OPAQUE_ID)

KEYS = ('demuxer-max-back-bytes', 'demuxer-donate-buffer', 'demuxer-max-bytes', 'demuxer-readahead-secs', 'cache', 'cache-secs')


class _ChildHistoryBackend(_StationBackend):
    async def command(self, args, timeout=5.0):
        if isinstance(args, list) and args[0] in {'expand-properties', 'expand-text'}:
            hook = getattr(self, '_before_history_cap', None)
            if hook is not None and args[:2] == ['expand-properties', 'set'] and (
                    'file-local-options/demuxer-max-back-bytes' in args[2]):
                self._before_history_cap = None
                await hook()
            # The exact production command arrays go through libmpv, including
            # its own property expansion and returned native result.
            strings = [str(arg).encode() for arg in args]
            values = (_Node * len(args))()
            for i, value in enumerate(strings):
                values[i].format, values[i].u.string = 1, value
            items = _NodeList(len(args), values, None)
            command, result = _Node(), _Node()
            command.format, command.u.list = 7, ct.pointer(items)
            code = self._lib.mpv_command_node(self._handle, ct.byref(command), ct.byref(result))
            try:
                return {'error': self._lib.mpv_error_string(code).decode(),
                        'data': _decode_node(result) if code == 0 else None}
            finally:
                self._lib.mpv_free_node_contents(ct.byref(result))
        return await super().command(args, timeout)


async def _exercise(library, root, extension):
    baseline = asyncio.all_tasks()
    root.mkdir(parents=True, exist_ok=True)
    tracks = (root / 'first.wav', root / 'second.wav')
    for path, hz in zip(tracks, (440, 660)):
        _write_tone(path, hz)
        with wave.open(str(path), 'rb') as f:
            params, frames = f.getparams(), f.readframes(4 * 16000)
        with wave.open(str(path), 'wb') as f:
            f.setparams(params)
            f.writeframes(frames)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    listener.listen(socket.SOMAXCONN)
    listener.setblocking(False)
    base = f'http://127.0.0.1:{listener.getsockname()[1]}'
    container = base + '/station.' + extension
    children = [base + '/first.wav', base + '/second.wav']
    backend = _ChildHistoryBackend(library, tracks, container)
    backend._allowed_media.update(children)
    cfg = Config(uuid='00000000-0000-4000-8000-000000000000', state_dir=str(root / 'state'),
                 auto_spawn_mpv=False, enable_ssdp=False, enable_mdns=False)
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)
    data = (b'#EXTM3U\n#EXTINF:-1,First\nfirst.wav\n#EXTINF:-1,Second\nsecond.wav\n' if extension == 'm3u'
            else b'[playlist]\nNumberOfEntries=2\nFile1=first.wav\nLength1=-1\nFile2=second.wav\nLength2=-1\nVersion=2\n')
    requests = []
    async def serve(request):
        requests.append(request.path)
        if request.path == '/station.' + extension:
            return web.Response(body=data, content_type='audio/x-mpegurl' if extension == 'm3u' else 'audio/x-scpls')
        return web.Response(body=tracks[children.index(base + request.path)].read_bytes(), content_type='audio/wav')
    app = api.app()
    app.router.add_get('/{name:.*}', serve)
    runner = web.AppRunner(app, shutdown_timeout=1)
    evidence = {'type': extension, 'mpv': backend.read_property('mpv-version'), 'children': []}
    def options():
        return {key: backend.read_property('options/' + key) for key in KEYS}
    defaults = options()
    try:
        await runner.setup()
        await web.SockSite(runner, listener).start()
        queue = PlaybackQueue(items=[QueueItem(container, stream_type='station')], index=1)
        await api._replace_queue(queue)
        await api._play_queue_index(1, queue.name)
        selected = api.state.queue.current
        old_identity = None
        for i, child in enumerate(children):
            await _eventually('native child ' + str(i), lambda: backend.read_property('path') == child, timeout=7)
            before = backend.read_property('playback-time')
            await _eventually('child decoded progress', lambda: backend.read_property('playback-time') > before + .25)
            await _eventually('native child policy', lambda: backend.read_property('options/demuxer-max-back-bytes') == LIVE_STATION_BACK_BYTES)
            assert options() == {**defaults, 'demuxer-max-back-bytes': LIVE_STATION_BACK_BYTES, 'demuxer-donate-buffer': False}
            assert backend.read_property('option-info/demuxer-max-back-bytes/set-locally') is True
            assert backend.read_property('option-info/demuxer-donate-buffer/set-locally') is True
            entries = backend.read_property('playlist')
            active = next(entry for entry in entries if entry.get('current') and entry.get('playing'))
            identity = (entries.index(active), active['id'], backend.read_property('pid'))
            if old_identity is not None:
                # Old child command, delayed until natural advance, is rejected
                # by mpv itself, without modifying the current child.
                assert not await backend.apply_station_child_history(*old_identity)
            old_identity = identity
            assert backend.read_property('playlist-path') == container
            assert api.state.current_url == api.state.backend_original_url == container
            assert api.state.queue.current is selected and selected.url == container and selected.duration == 0
            assert backend.loads == [container]
            if i == 0:
                await api.execute(ParsedCommand('pause'), '127.0.0.1')
                assert backend.read_property('pause') is True
                position = backend.read_property('playback-time')
                await asyncio.sleep(.2)
                assert abs(backend.read_property('playback-time') - position) < .05
                await api.execute(ParsedCommand('resume'), '127.0.0.1')
                assert backend.read_property('pause') is False
            evidence['children'].append({'id': active['id'], 'options': options(),
                                         'set_locally': True, 'decoded': True})
        await api._play_url(children[0])
        assert options() == defaults
        assert backend.read_property('option-info/demuxer-max-back-bytes/set-locally') is False
        assert backend.read_property('option-info/demuxer-donate-buffer/set-locally') is False
        # Deterministic stale-command vs foreign/new finite source, including
        # reuse of a URL formerly underneath the station container.
        assert not await backend.apply_station_child_history(*old_identity)
        assert options() == defaults
        # Wrong core ID must fail closed too.
        entry = backend.read_property('playlist')[0]
        assert not await backend.apply_station_child_history(0, entry['id'], backend.read_property('pid') + 1)
        assert options() == defaults
        await api.execute(ParsedCommand('stop'), '127.0.0.1')
        assert not await backend.apply_station_child_history(*old_identity)
        assert options() == defaults
        evidence.update(defaults_restored=True, stale_guards_rejected=True, pause_resume=True,
                        canonical_preserved=True, success=True)
    finally:
        await runner.cleanup()
        listener.close()
        backend.close()
        await asyncio.sleep(0)
        pending = [task for task in asyncio.all_tasks() - baseline if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        evidence['cleanup'] = {'handle_closed': backend._handle is None,
                               'listener_closed': listener.fileno() == -1, 'no_pending_tasks': not pending}
    evidence['requests'] = requests
    return evidence


@pytest.mark.skipif(not os.environ.get('ORR_TEST_LIBMPV_PATH'), reason='Native playlist cache test requires libmpv')
@pytest.mark.parametrize('extension', ['m3u', 'pls'])
def test_native_station_playlist_children_have_file_local_history(tmp_path, extension):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'PYTHONPATH': str(root) + os.pathsep + str(root / 'tests')}
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                             os.environ['ORR_TEST_LIBMPV_PATH'], str(tmp_path), extension],
                            env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert evidence['success']
    assert all(evidence['cleanup'].values())


async def _process_guard(library, directory, old_pid):
    tracks = (directory / 'same-child.wav', directory / 'unused.wav')
    for path in tracks:
        _write_tone(path, 440)
    backend = _ChildHistoryBackend(library, tracks, str(tracks[0]))
    try:
        await backend.loadfile(str(tracks[0]))
        await _eventually('native replacement file', lambda: backend.read_property('path') == str(tracks[0]))
        entry = backend.read_property('playlist')[0]
        pid = backend.read_property('pid')
        default = backend.read_property('options/demuxer-max-back-bytes')
        applied = await backend.apply_station_child_history(0, entry['id'], pid if old_pid is None else old_pid)
        value = backend.read_property('options/demuxer-max-back-bytes')
        local = backend.read_property('option-info/demuxer-max-back-bytes/set-locally')
        donation = backend.read_property('options/demuxer-donate-buffer')
        donation_local = backend.read_property('option-info/demuxer-donate-buffer/set-locally')
        assert (applied and value == LIVE_STATION_BACK_BYTES and local is True and donation is False and donation_local is True) if old_pid is None else (
            pid != old_pid and not applied and value == default and local is False and donation is True and donation_local is False)
        return {'pid': pid, 'id': entry['id'], 'path': str(tracks[0]),
                'applied': applied, 'value': value, 'set_locally': local,
                'donation': donation, 'donation_set_locally': donation_local}
    finally:
        backend.close()


@pytest.mark.skipif(not os.environ.get('ORR_TEST_LIBMPV_PATH'), reason='Native process guard requires libmpv')
def test_native_stale_process_guard_rejects_reused_entry_id(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'PYTHONPATH': str(root) + os.pathsep + str(root / 'tests')}
    records = []
    for old_pid in (None, 'previous'):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
            os.environ['ORR_TEST_LIBMPV_PATH'], str(tmp_path), 'process-guard',
            '' if old_pid is None else str(records[0]['pid'])],
            env=env, text=True, capture_output=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
        records.append(json.loads(result.stdout))
    # Genuine different native player processes reuse exactly the same ID and
    # path. Only the PID part of the atomic guard excludes the second one.
    assert records[0]['pid'] != records[1]['pid']
    assert records[0]['id'] == records[1]['id'] == 1
    assert records[0]['path'] == records[1]['path']
    assert records[0]['applied'] is True and records[1]['applied'] is False
    (tmp_path / 'native-process-guard.json').write_text(json.dumps(records, indent=2))


async def _transition_guard(library, directory):
    tracks = (directory / 'old-station-child.wav', directory / 'foreign-finite.wav')
    for path in tracks:
        _write_tone(path, 440)
    backend = _ChildHistoryBackend(library, tracks, str(tracks[0]))
    try:
        await backend.loadfile(str(tracks[0]))
        await _eventually('old child decoded', lambda: backend.read_property('playback-time') > .1)
        identity = (0, backend.read_property('playlist')[0]['id'], backend.read_property('pid'))
        defaults = {name: backend.read_property('options/' + name) for name in KEYS}
        partial = {}
        async def replace_between_writes():
            partial.update(history=backend.read_property('options/demuxer-max-back-bytes'),
                           donation=backend.read_property('options/demuxer-donate-buffer'),
                           donation_local=backend.read_property('option-info/demuxer-donate-buffer/set-locally'))
            assert partial == {'history': defaults['demuxer-max-back-bytes'],
                               'donation': False, 'donation_local': True}
            await backend.loadfile(str(tracks[1]))
            await _eventually('foreign file decoded', lambda: backend.read_property('path') == str(tracks[1])
                              and backend.read_property('playback-time') > .1)
        backend._before_history_cap = replace_between_writes
        confirmed = await backend.apply_station_child_history(*identity)
        assert confirmed is False
        assert {name: backend.read_property('options/' + name) for name in KEYS} == defaults
        assert backend.read_property('option-info/demuxer-max-back-bytes/set-locally') is False
        assert backend.read_property('option-info/demuxer-donate-buffer/set-locally') is False
        return {'confirmed': confirmed, 'partial_old_entry': partial,
                'foreign_defaults_preserved': True, 'foreign_entry_id': backend.read_property('playlist')[0]['id']}
    finally:
        backend.close()


@pytest.mark.skipif(not os.environ.get('ORR_TEST_LIBMPV_PATH'), reason='Native transition guard requires libmpv')
def test_native_replacement_between_option_writes_is_unmodified(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'PYTHONPATH': str(root) + os.pathsep + str(root / 'tests')}
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
        os.environ['ORR_TEST_LIBMPV_PATH'], str(tmp_path), 'transition-guard'],
        env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert evidence['confirmed'] is False and evidence['foreign_defaults_preserved']
    (tmp_path / 'native-between-writes.json').write_text(json.dumps(evidence, indent=2))


async def _child_icy(library, ffmpeg, directory, extension):
    media = _generate_media(ffmpeg, directory)
    fixture = ChangingICY(media['mp3'])
    baseline = asyncio.all_tasks()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    listener.listen(socket.SOMAXCONN)
    listener.setblocking(False)
    base = f'http://127.0.0.1:{listener.getsockname()[1]}'
    source, child = base + '/station.' + extension, base + '/changing-icy'
    backend = _ChildHistoryBackend(library, (media['wav'], media['wav']), source)
    backend._allowed_media.add(child)
    cfg = Config(uuid='00000000-0000-4000-8000-000000000000', state_dir=str(directory / 'state'),
                 auto_spawn_mpv=False, enable_ssdp=False, enable_mdns=False)
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)
    events = {'AVTransport': [], 'PlayQueue': []}
    async def callback(request):
        events[request.match_info['service']].append(property_fields(await request.text()))
        return web.Response()
    async def playlist(request):
        data = '#EXTM3U\nchanging-icy\n' if extension == 'm3u' else (
            '[playlist]\nNumberOfEntries=1\nFile1=changing-icy\nVersion=2\n')
        return web.Response(text=data, content_type='audio/x-mpegurl' if extension == 'm3u' else 'audio/x-scpls')
    app = api.app()
    app.router.add_get('/station.' + extension, playlist)
    app.router.add_get('/changing-icy', fixture.handle)
    app.router.add_route('NOTIFY', '/events/{service}', callback)
    runner = web.AppRunner(app, shutdown_timeout=1)
    updates = []
    try:
        await runner.setup()
        await web.SockSite(runner, listener).start()
        async with ClientSession(timeout=ClientTimeout(total=15), trust_env=False) as session:
            env = MetadataRenderer(api, backend, session, base, events)
            for service in events:
                async with session.request('SUBSCRIBE', base + '/upnp/event/' + service, headers={
                    'NT': 'upnp:event', 'CALLBACK': f'<{base}/events/{service}>', 'TIMEOUT': 'Second-60'}) as response:
                    assert response.status == 200
            item = QueueItem(source, stream_type='station', source='SyntheticRadio', id=OPAQUE_ID,
                             artist=ARTIST, album=ALBUM, artwork=ARTWORK)
            queue = PlaybackQueue(items=[item], index=1)
            await api._replace_queue(queue)
            await api._play_queue_index(1, queue.name)
            assert backend.read_property('options/demuxer-max-back-bytes') == LIVE_STATION_BACK_BYTES
            assert backend.read_property('options/demuxer-donate-buffer') is False
            for title in TITLES:
                seen = len(events['PlayQueue'])
                fixture.title = title
                await _eventually('native child ICY title', lambda: backend.read_property('media-title') == title, timeout=6)
                await _eventually('autonomous canonical child title', lambda: api.state.title == title)
                await _eventually('autonomous child title event', lambda: any(
                    event.get('TrackTitle') == title for event in events['PlayQueue'][seen:]))
                assert backend.read_property('path') == child
                assert api.state.current_url == api.state.backend_original_url == source
                assert api.state.queue is queue and queue.current is item
                await assert_surfaces(env, uri=source, title=title, artist=ARTIST, album=ALBUM,
                    artwork=ARTWORK, source='SyntheticRadio', item_id=OPAQUE_ID)
                updates.append(title)
            assert backend.loads == [source] and len(fixture.requests) == 1
            await env.soap('Stop')
            await _eventually('ICY stopped', fixture.finished.is_set)
    finally:
        await runner.cleanup()
        listener.close()
        backend.close()
        await asyncio.sleep(0)
        pending = [task for task in asyncio.all_tasks() - baseline if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        assert not pending and backend._handle is None and listener.fileno() == -1
    return {'extension': extension, 'updates': updates, 'canonical_preserved': True,
            'http_soap_gena_verified': True, 'cleanup': True}


@pytest.mark.skipif(not os.environ.get('ORR_TEST_LIBMPV_PATH'), reason='Native child ICY requires libmpv')
@pytest.mark.parametrize('extension', ['m3u', 'pls'])
def test_native_station_child_finite_history_preserves_icy(tmp_path, extension):
    ffmpeg = shutil.which(os.environ.get('ORR_TEST_FFMPEG_BINARY', 'ffmpeg'))
    assert ffmpeg, 'Opt-in child ICY test requires official FFmpeg'
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'PYTHONPATH': str(root) + os.pathsep + str(root / 'tests')}
    for name in tuple(env):
        if name.lower() in {'http_proxy', 'https_proxy', 'all_proxy'}:
            env.pop(name)
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
        os.environ['ORR_TEST_LIBMPV_PATH'], str(tmp_path), 'child-icy', ffmpeg, extension],
        env=env, text=True, capture_output=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert len(evidence['updates']) == 3 and evidence['http_soap_gena_verified']
    (tmp_path / 'native-child-icy.json').write_text(json.dumps(evidence, indent=2))


if __name__ == '__main__':
    locale.setlocale(locale.LC_NUMERIC, 'C')
    if sys.argv[3] == 'process-guard':
        result = _process_guard(sys.argv[1], Path(sys.argv[2]), int(sys.argv[4]) if sys.argv[4] else None)
    elif sys.argv[3] == 'child-icy':
        result = _child_icy(sys.argv[1], sys.argv[4], Path(sys.argv[2]), sys.argv[5])
    elif sys.argv[3] == 'transition-guard':
        result = _transition_guard(sys.argv[1], Path(sys.argv[2]))
    else:
        result = _exercise(sys.argv[1], Path(sys.argv[2]), sys.argv[3])
    print(json.dumps(asyncio.run(result)))
