"""Pure assertion tests for the opt-in same-host acceptance tool."""
import copy

import pytest

from tools.acceptance_snapcast import assert_measurement_state, assert_owned_status, assert_reconnected_status


def state():
    return {'streams': [{'id': 'private-run', 'status': 'playing'}], 'groups': [
        {'id': 'original-group', 'muted': True, 'stream_id': 'private-run', 'clients': [
            {'id': cid, 'connected': True, 'config': {'volume': {'muted': False, 'percent': 50}}}
            for cid in ('a', 'b')]}]}


def checked(value, source_status='playing'):
    assert_measurement_state(value, 'private-run', ['a', 'b'], {frozenset(['a', 'b']): True}, 50, source_status)


def test_mute_requires_connected_playing_source_and_expected_group_state():
    checked(state())


@pytest.mark.parametrize('fault', ['idle', 'disconnected', 'group_unmuted', 'gain_changed',
                                   'client_muted', 'missing_member', 'foreign_stream', 'foreign_client'])
def test_silent_output_alone_cannot_pass_mute(fault):
    value = copy.deepcopy(state())
    group = value['groups'][0]
    if fault == 'idle':
        value['streams'][0]['status'] = 'idle'
    elif fault == 'disconnected':
        group['clients'][0]['connected'] = False
    elif fault == 'group_unmuted':
        group['muted'] = False
    elif fault == 'gain_changed':
        group['clients'][0]['config']['volume']['percent'] = 100
    elif fault == 'client_muted':
        group['clients'][0]['config']['volume']['muted'] = True
    elif fault == 'missing_member':
        group['clients'].pop()
    elif fault == 'foreign_stream':
        value['streams'][0]['id'] = 'other-run'
    elif fault == 'foreign_client':
        group['clients'][0]['id'] = 'other-run'
    with pytest.raises(AssertionError):
        checked(value)


def test_stop_deliberately_requires_drained_source():
    value = state()
    value['streams'][0]['status'] = 'idle'
    checked(value, 'idle')
    with pytest.raises(AssertionError):
        checked(state(), 'idle')


def test_ownership_rejects_additional_stream_or_peer():
    for key, extra in [('streams', {'id': 'other-run', 'status': 'playing'}),
                       ('groups', {'id': 'other', 'stream_id': 'other-run', 'clients': []})]:
        value = state()
        value[key].append(extra)
        with pytest.raises(AssertionError):
            assert_owned_status(value, 'private-run', ['a', 'b'])


@pytest.mark.parametrize('fault', [None, 'group_id', 'percent', 'muted'])
def test_reconnection_requires_original_group_and_retained_volume(fault):
    value = state()
    group = value['groups'][0]
    group['muted'] = False
    if fault == 'group_id':
        group['id'] = 'replacement-group'
    elif fault == 'percent':
        group['clients'][0]['config']['volume']['percent'] = 100
    elif fault == 'muted':
        group['clients'][0]['config']['volume']['muted'] = True
    if fault is None:
        assert_reconnected_status(value, 'private-run', ['a', 'b'], 'original-group', 50)
    else:
        with pytest.raises(AssertionError):
            assert_reconnected_status(value, 'private-run', ['a', 'b'], 'original-group', 50)


@pytest.mark.asyncio
@pytest.mark.parametrize('owned_alive', [True, False])
async def test_recycled_rpc_port_never_receives_a_mutation(tmp_path, monkeypatch, owned_alive):
    import argparse
    import asyncio
    import json
    from tools import acceptance_snapcast as tool

    requests = []
    async def foreign(reader, writer):
        try:
            raw = await reader.readline()
            if raw:
                request = json.loads(raw)
                requests.append(request['method'])
                response = {'id': request['id'], 'jsonrpc': '2.0', 'result': {'server': state()}}
                writer.write(json.dumps(response).encode() + b'\n')
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    listener = await asyncio.start_server(foreign, '127.0.0.1', 0)
    rpc_port = listener.sockets[0].getsockname()[1]
    ports = iter([tool.freeport(), rpc_port, tool.freeport()])
    monkeypatch.setattr(tool, 'freeport', lambda: next(ports))
    class OwnedServer:
        returncode = None if owned_alive else 1
        def poll(self):
            return self.returncode
        def terminate(self):
            self.returncode = -15
        def wait(self, **_kwargs):
            return self.returncode
    monkeypatch.setattr(tool.subprocess, 'Popen', lambda *_args, **_kwargs: OwnedServer())
    args = argparse.Namespace(snapserver='unused', snapclient='unused', mpv='unused', orr_ipc=False)
    try:
        with pytest.raises(AssertionError):
            await asyncio.wait_for(tool.exercise(tmp_path / 'private', args), timeout=3)
    finally:
        listener.close()
        await listener.wait_closed()
        await asyncio.sleep(0)
    assert requests == (['Server.GetStatus'] if owned_alive else [])


def test_ipc_diagnostics_never_include_media_errors_or_unknown_property_names():
    from tools.acceptance_snapcast import ipc_summary
    secret = 'https://private.example/audio?token=SECRET'
    assert ipc_summary(['loadfile', secret, 'replace'], {'error': secret}, .125) == {
        'command': 'loadfile', 'seconds': .125, 'outcome': 'rejected'}
    assert ipc_summary(['get_property', secret], {'error': 'mpv IPC command timed out'}, .5) == {
        'command': 'get_property:other', 'seconds': .5, 'outcome': 'timeout'}
    assert ipc_summary({'name': 'loadfile', 'url': secret}, {'error': 'success'}, .001) == {
        'command': 'loadfile', 'seconds': .001, 'outcome': 'success'}


def native_state():
    return {'path': 'synthetic.wav', 'idle-active': False, 'pause': False,
            'eof-reached': False, 'paused-for-cache': False, 'playback-time': 3.0}


@pytest.mark.parametrize('fault', ['backend-error', 'idle-active', 'pause', 'eof-reached',
                                   'paused-for-cache', 'path', 'playback-time', 'mute'])
def test_upstream_mute_requires_actual_active_native_source(fault):
    from tools.acceptance_snapcast import assert_upstream_active
    value = native_state()
    mute = True
    if fault in {'backend-error', 'idle-active', 'pause', 'eof-reached', 'paused-for-cache'}:
        value[fault] = True
    elif fault == 'path':
        value[fault] = 'another-source.wav'
    elif fault == 'playback-time':
        value[fault] = float('nan')
    else:
        mute = False
    with pytest.raises(AssertionError):
        assert_upstream_active(value, mute, True, 'synthetic.wav')


def test_upstream_mute_accepts_documented_snapcast_silence_classifications():
    from tools.acceptance_snapcast import assert_upstream_active
    assert assert_upstream_active(native_state(), True, True, 'synthetic.wav') == 3.0
    for status in ('playing', 'idle'):
        value = state()
        value['streams'][0]['status'] = status
        checked(value, ('playing', 'idle'))
    # Downstream group mute still cannot use source idleness as its proof.
    with pytest.raises(AssertionError):
        checked(value)


@pytest.mark.parametrize('frequency', [440, 733])
def test_tone_fit_preserves_gain_across_a_clock_correction(frequency):
    import math
    import statistics
    from tools.acceptance_snapcast import RATE, tone_amplitudes
    count = 7 * RATE // 10
    values = [round(500 * math.sin(2 * math.pi * frequency * (index + (858 if index > count // 2 else 0)) / RATE))
              for index in range(count)]
    amplitudes = tone_amplitudes(values, frequency)
    assert abs(statistics.median(amplitudes) - 500) < 1
    assert sum(abs(value - 500) < 7 for value in amplitudes) / len(amplitudes) >= .8


@pytest.mark.parametrize('corruption', ['swapped_frequency', 'wrong_gain', 'silence', 'noise'])
def test_tone_fit_rejects_corrupted_source_identity(corruption):
    import math
    import random
    from tools.acceptance_snapcast import RATE, tone_amplitudes
    random.seed(0)
    values = [round(500 * math.sin(2 * math.pi * (733 if corruption == 'swapped_frequency' else 440) * i / RATE))
              for i in range(7 * RATE // 10)]
    if corruption == 'wrong_gain':
        values = [value // 2 for value in values]
    elif corruption == 'silence':
        values = [0] * len(values)
    elif corruption == 'noise':
        values = [random.randrange(-500, 501) for value in values]
    assert sum(abs(value - 500) < 7 for value in tone_amplitudes(values, 440)) / 7 < .8
