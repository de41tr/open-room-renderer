"""Optional REAL_BACKEND/digital-PCM restart acceptance for active EQ.

Requires ORR_TEST_LIBMPV_PATH and uses the existing test-only ctypes transport.
Each restart closes one libmpv handle, then creates fresh Config, RendererState,
HttpApi and backend objects from the owned state directory. The first generated
quiet WAV is decoded only after complete graph restoration, or through the
backend's deferred-load restoration. Output is a local float PCM file, never a
physical device. This does not claim CLI/systemd/process restart or UNIX IPC,
hardware audio, audibility, arbitrary filter support, or HTTP play readiness.
"""
from __future__ import annotations

import asyncio
import json
import locale
import os
from pathlib import Path
import subprocess
import sys

import pytest

from fake_linkplay.config import load_config
from fake_linkplay.http_api import HttpApi, ParsedCommand
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.persistence import load_active_dsp
from fake_linkplay.state import RendererState
from test_libmpv_dsp import _measure, _write_signal
from test_libmpv_integration import _LibmpvBackend, _eventually


async def _exercise_restore(library: str, directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    track = directory / 'quiet.wav'
    source = _write_signal(track, (1000, 4000))
    config_path = directory / 'renderer.yaml'
    config_path.write_text(f'state_dir: {directory / "state"}\ndefault_volume: 25\ndefault_mute: false\n')
    evidence = {'lane': 'REAL_BACKEND/digital-PCM-DSP-restore', 'captures': {},
                'output': 'local float PCM, no hardware',
                'control_transport': 'HttpApi -> MpvBackend -> test ctypes -> libmpv',
                'restart_scope': 'fresh config/state/API/backend and libmpv handle, not CLI/systemd'}
    original = None
    handles = []
    async def fresh(name):
        cfg = load_config(config_path)
        state = RendererState.from_config(cfg)
        sink = directory / (name + '.pcm')
        assert not sink.exists()
        backend = _LibmpvBackend(library, (track, track), pcm_output=sink,
                                 initial_volume=state.volume, initial_mute=state.mute)
        handles.append(backend)
        api = HttpApi(cfg, state, backend)
        return api, backend, sink
    async def command(api, value):
        return await api.execute(ParsedCommand(value), '127.0.0.1')
    async def capture(name, backend, sink):
        result = await backend.loadfile(str(track))
        assert result['error'] == 'success'
        await _eventually(name, lambda: sink.exists() and sink.stat().st_size == len(source) * 4
                          and backend.read_property('idle-active') is True, timeout=5)
        assert backend.read_property('volume') == 25
        measured = _measure(sink.read_bytes(), source)
        evidence['captures'][name] = {'af': backend.read_property('af'), **measured}
        return measured
    try:
        api, backend, sink = await fresh('original')
        await api.initialize_channel_balance()
        await command(api, 'setPlayerCmd:vol:25')
        await command(api, 'setPlayerCmd:mute:0')
        await command(api, 'EQSetLV2SourceBand:' + json.dumps({
            'source_name': 'wifi', 'pluginURI': 'synthetic.restore', 'EQBand': [
                {'param_name': 'a_freq', 'value': 1000},
                {'param_name': 'a_gain', 'value': -12},
                {'param_name': 'a_q', 'value': 1},
            ]}))
        await command(api, 'setChannelBalance:0.5')
        await command(api, 'EQSave:Quiet saved curve')
        original = await capture('original', backend, sink)
        graph = backend.read_property('af')
        stored = load_active_dsp(api.cfg.state_dir)
        backend.close()
        expected = 10 ** (-12 / 20) / 2
        assert original['channels'][0]['normalized_rms_ratio'] == pytest.approx(expected, rel=0.02)
        for name, deferred in [('restored', False), ('deferred_restore', True)]:
            api, backend, sink = await fresh(name)
            assert api.eq_current_preset == ''
            assert backend.read_property('af') == []
            with pytest.raises(BackendError):
                await command(api, 'EQGetLV2SourceBand:')
            if not deferred:
                await api.initialize_channel_balance()
                assert backend.read_property('af') == graph
            measured = await capture(name, backend, sink)
            assert backend.read_property('af') == graph
            assert measured['channels'][0]['normalized_rms_ratio'] == pytest.approx(expected, rel=0.02)
            assert measured['channels'][1]['normalized_rms_ratio'] > 0.8
            reported = await command(api, 'EQGetLV2SourceBand:')
            assert reported['Name'] == 'Quiet saved curve'
            assert reported['pluginURI'] == 'synthetic.restore'
            assert reported['source_name'] == 'wifi'
            assert reported['EQStat'] == 'On'
            assert reported['EQBand'] == stored['bands']
            assert await command(api, 'getChannelBalance') == '0.5'
            if deferred:
                await command(api, 'EQOff')
            backend.close()
        api, backend, sink = await fresh('restored_bypass')
        await api.initialize_channel_balance()
        measured = await capture('restored_bypass', backend, sink)
        assert measured['channels'][0]['normalized_rms_ratio'] == pytest.approx(0.5, rel=1e-4)
        assert measured['channels'][1]['normalized_rms_ratio'] == pytest.approx(1, rel=1e-4)
        reported = await command(api, 'EQGetLV2SourceBand:')
        assert reported['EQStat'] == 'Off'
        assert reported['Name'] == 'Quiet saved curve'
        assert reported['EQBand'] == stored['bands']
        assert 'equalizer=' not in backend.read_property('af')[0]['params']['graph']
    finally:
        for backend in handles:
            backend.close()
        assert all(backend._handle is None for backend in handles)
    evidence['cleanup'] = {'handles_closed': len(handles)}
    return evidence


@pytest.mark.skipif(not os.environ.get('ORR_TEST_LIBMPV_PATH'),
                    reason='REAL_BACKEND/digital-PCM DSP restore requires ORR_TEST_LIBMPV_PATH')
def test_real_libmpv_active_dsp_restores_before_first_decoded_samples(tmp_path):
    library = Path(os.environ['ORR_TEST_LIBMPV_PATH']).expanduser().resolve()
    assert library.is_file()
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env['PYTHONPATH'] = str(root) + os.pathsep + env.get('PYTHONPATH', '')
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--probe', str(library), str(tmp_path)],
                            cwd=root, env=env, text=True, capture_output=True, timeout=30, check=False)
    assert result.returncode == 0, f'Digital restore failed\n{result.stdout}\n{result.stderr}'
    evidence = json.loads(result.stdout)
    assert set(evidence['captures']) == {'original', 'restored', 'deferred_restore', 'restored_bypass'}
    assert evidence['cleanup'] == {'handles_closed': 4}
    print(json.dumps(evidence, sort_keys=True))


if __name__ == '__main__':
    assert len(sys.argv) == 4 and sys.argv[1] == '--probe'
    locale.setlocale(locale.LC_NUMERIC, 'C')
    print(json.dumps(asyncio.run(_exercise_restore(sys.argv[2], Path(sys.argv[3]))), sort_keys=True))
