"""Real owned child -> private f32le FIFO -> existing ORR EQ -> PCM file.

The child generates quiet synthetic stereo tones, NOT Spotify/account audio.
The existing libmpv test adapter replaces only IPC. No hardware, discovery,
provider authentication, native ARM execution or audibility is claimed.
"""
from array import array
import asyncio
import ctypes as ct
import json
import locale
import math
import os
from pathlib import Path
import subprocess
import sys

import pytest

from fake_linkplay import spotify_eq as eq
from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi, ParsedCommand
from fake_linkplay.state import RendererState
from test_libmpv_integration import _LibmpvBackend, _Node, _eventually, _write_tone
from test_libmpv_load_options import _NamedLoadBackend


class PcmBackend(_NamedLoadBackend):
    def __init__(self, library, track, sink, root):
        _LibmpvBackend.__init__(self, library, (track, track), pcm_output=sink, initial_volume=25)
        self._lib.mpv_command_node.restype = ct.c_int
        self._lib.mpv_command_node.argtypes = [ct.c_void_p, ct.POINTER(_Node), ct.POINTER(_Node)]
        self.root = root

    async def command(self, args, timeout=5.0):
        if isinstance(args, dict):
            path = Path(args['url'])
            assert path.name == 'audio.fifo' and path.parent.parent == self.root and path.is_fifo()
            self._allowed_media.add(str(path))
        return await super().command(args, timeout)


class SyntheticSource(eq.SpotifyEqSource):
    async def _spawn(self, binary, directory):
        # No Spotify binary, app/API, login, external media or hardware involved.
        script = '''import math, struct, sys, time
with open(sys.argv[1], 'wb', buffering=0) as output:
    for block in range(600):
        data = b''.join(struct.pack('<ff', .001*math.sin(2*math.pi*1000*i/44100), .001*math.sin(2*math.pi*4000*i/44100)) for i in range(block*882,(block+1)*882))
        output.write(data)
        time.sleep(.02)
time.sleep(20)
'''
        return await asyncio.create_subprocess_exec(sys.executable, '-c', script, str(self.fifo),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)

    async def _read_provider(self):
        self._validate_child()
        return {'status': {'stopped': False, 'paused': False, 'buffering': False}, 'track': None}


async def exercise(library: str, root: Path):
    root.mkdir(mode=0o700)
    track, sink = root / 'own.wav', root / 'captured.pcm'
    _write_tone(track, 660)
    p = {'format': 1, 'client_dir': str(root / 'unused-client'), 'client_sha256': 'a'*64,
         'name': 'Synthetic source', 'bitrate': 160, 'port': 3678, 'device_id': 'b'*40}
    # Only client provenance is substituted: the daemon is intentionally never run.
    eq.verify_client = lambda _: (Path(sys.executable), {'binary_sha256': p['client_sha256']})
    backend = PcmBackend(library, track, sink, root)
    cfg = Config(uuid='00000000-0000-4000-8000-000000000000', default_volume=25,
                 state_dir=str(root / 'state'), enable_mdns=False, enable_ssdp=False)
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)
    source = SyntheticSource(root, p)
    try:
        await api.initialize_channel_balance()
        await api.execute(ParsedCommand('EQSetLV2SourceBand:' + json.dumps({
            'source_name': 'wifi', 'pluginURI': 'synthetic.spotify.pcm', 'EQBand': [
                {'param_name': 'a_freq', 'value': 1000}, {'param_name': 'a_gain', 'value': -12},
                {'param_name': 'a_q', 'value': 1}]})), '127.0.0.1')
        await source.start(api)
        runtime, child = source.runtime, source.process
        await _eventually('cut PCM samples', lambda: sink.exists() and sink.stat().st_size >= 16000*2*4*.75, 7)
        graph = backend.read_property('af')
        assert graph and 'g=-12.000' in graph[0]['params']['graph']
        assert backend.read_property('options/demuxer-rawaudio-rate') == 44100
        assert backend.read_property('audio-params')['samplerate'] == 44100
        # The same existing EQ control used by the UI changes this running FIFO.
        boundary = sink.stat().st_size / (16000*2*4)
        await api.execute(ParsedCommand('EQOff'), '127.0.0.1')
        assert backend.read_property('af') == []
        await _eventually('bypass PCM samples', lambda: sink.stat().st_size >= 16000*2*4*(boundary+1.8), 7)
        await source.close()
        assert child.returncode is not None and not runtime.exists() and source.keepalive is None
        assert api.spotify_source is None and backend.read_property('idle-active') is True
        assert api.state.current_url == ''
        samples = array('f'); samples.frombytes(sink.read_bytes())
        def ratios(start, end):
            return [math.sqrt(sum(x*x for x in samples[int(start*16000)*2+c:int(end*16000)*2:2]) /
                              int((end-start)*16000)) / (.001/math.sqrt(2)) / ((25/100)**3)
                    for c in (0, 1)]
        cut, bypass = ratios(.2,.5), ratios(boundary+1.2,boundary+1.5)
        assert cut[0] == pytest.approx(10**(-12/20), rel=.035)
        assert cut[1] > .8
        assert bypass == pytest.approx([1,1], rel=.035)
        assert max(abs(x) for x in samples) < .00002
        # Return uses the original backend; the PCM input options are file-local.
        reply = await backend.loadfile(str(track))
        assert reply['error'] == 'success'
        await _eventually('normal source return', lambda: backend.read_property('path') == str(track)
            or backend.read_property('idle-active') is True)
        assert backend.read_property('option-info/demuxer-max-bytes/set-locally') is False
        await backend.stop()
        return {'lane': 'REAL_SOFTWARE_TESTED/synthetic-owned-child-FIFO-existing-EQ',
                'input': '44100 Hz stereo f32le, synthetic peak 0.001', 'output': '16000 Hz float PCM file',
                'cut_normalized_rms': cut, 'bypass_normalized_rms': bypass,
                'producer_reaped_before_reader_stop': child.returncode is not None,
                'private_runtime_removed': not runtime.exists(), 'ordinary_source_return': True,
                'spotify_account': 'NOT_TESTED', 'DAC': 'NOT_TESTED', 'production_unix_IPC': 'NOT_TESTED'}
    finally:
        await source.close()
        backend.close()


def test_real_owned_pcm_runs_through_existing_eq_and_returns(tmp_path):
    library = os.environ.get('ORR_TEST_LIBMPV_PATH')
    if not library:
        pytest.skip('set ORR_TEST_LIBMPV_PATH for real digital-only FIFO acceptance')
    script = '''import asyncio,json,locale,sys
from pathlib import Path
from test_libmpv_spotify_eq import exercise
locale.setlocale(locale.LC_NUMERIC, 'C')
print(json.dumps(asyncio.run(exercise(sys.argv[1],Path(sys.argv[2]))), sort_keys=True))
'''
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(Path(__file__).parent), str(Path(__file__).parents[1])]))
    result = subprocess.run([sys.executable, '-c', script, library, str(tmp_path/'acceptance')],
                            env=env, capture_output=True, text=True, timeout=35)
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout)


def test_real_unix_pcm_source_stop_returns_to_normal_renderer(tmp_path):
    if os.environ.get('ORR_TEST_UNIX_IPC') != '1':
        pytest.skip('set ORR_TEST_UNIX_IPC for production JSON IPC/null-output acceptance')
    import shutil
    from tools.acceptance_cli_lifecycle import write_null_wrapper
    native = shutil.which('mpv')
    assert native, 'This explicit lane requires native mpv; never silently skip it'
    (tmp_path / 'bin').mkdir()
    write_null_wrapper(tmp_path, Path(native))
    script = '''import asyncio,json,locale,sys
from pathlib import Path
from test_libmpv_spotify_eq import unix_exercise
locale.setlocale(locale.LC_NUMERIC, 'C')
print(json.dumps(asyncio.run(unix_exercise(Path(sys.argv[1]))), sort_keys=True))
'''
    env = dict(os.environ, PATH=str(tmp_path / 'bin')+os.pathsep+os.environ['PATH'],
               PYTHONPATH=os.pathsep.join([str(Path(__file__).parent), str(Path(__file__).parents[1])]))
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path / 'ipc')],
                            env=env, capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout)


async def unix_exercise(root):
    from fake_linkplay.mpv_backend import MpvBackend
    from fake_linkplay.queue import PlaybackQueue, QueueItem
    from fake_linkplay.state import PlaybackPhase
    root.mkdir(mode=0o700)
    track = root / 'own.wav'; _write_tone(track, 660)
    p = {'format': 1, 'client_dir': str(root / 'unused-client'), 'client_sha256': 'a'*64,
         'name': 'Synthetic source', 'bitrate': 160, 'port': 3678, 'device_id': 'b'*40}
    eq.verify_client = lambda _: (Path(sys.executable), {'binary_sha256': p['client_sha256']})
    backend = MpvBackend(str(root / 'mpv.sock'), auto_spawn=True, initial_volume=25)
    cfg = Config(uuid='00000000-0000-4000-8000-000000000000', default_volume=25,
                 state_dir=str(root/'state'), enable_mdns=False, enable_ssdp=False)
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)
    source = SyntheticSource(root, p)
    try:
        await backend.start()
        await api.initialize_channel_balance()
        await source.start(api)
        child, runtime = source.process, source.runtime
        deadline = asyncio.get_running_loop().time()+4
        while api.state.phase != PlaybackPhase.PLAYING:
            assert asyncio.get_running_loop().time() < deadline
            await source.observe()
            await asyncio.sleep(.05)
        await api.execute(ParsedCommand('setChannelBalance:0.5'), '127.0.0.1')
        assert 'pan' in str(await backend.get_property('af'))
        queue = PlaybackQueue(items=[QueueItem(str(track), title='Own synthetic fallback')], index=1)
        await api._replace_queue(queue)
        assert child.returncode is not None and not runtime.exists()
        assert await backend.get_property('idle-active') is True
        await api._play_url(str(track))
        assert api.state.phase == PlaybackPhase.PLAYING
        assert await backend.get_property('current-ao') == 'null'
        assert await backend.get_property('option-info/demuxer-max-bytes/set-locally') is False
        await api.execute(ParsedCommand('stop'), '127.0.0.1')
        return {'lane': 'REAL_SOFTWARE_TESTED/production-Unix-IPC/synthetic-FIFO',
                'existing_balance_filter': True, 'producer_reaped': True, 'ordinary_source_return': True,
                'output': 'null, no DAC', 'Spotify': 'NOT_STARTED'}
    finally:
        await source.close()
        await backend.stop_process()
