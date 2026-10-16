"""UNIT/MOCK active DSP restart intent and confirmation; no audio device."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import stat

import pytest

from fake_linkplay.config import Config, load_config
from fake_linkplay.http_api import DEFAULT_EQ_PLUGIN, HttpApi, ParsedCommand
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.persistence import ACTIVE_DSP_MAX_BYTES, load_active_dsp
from fake_linkplay.state import RendererState
from test_backend_dsp import OTHER_FILTER, add, entry, make_backend


async def execute(api, command):
    return await api.execute(ParsedCommand(command), '127.0.0.1')


def make_api(directory, *, balance='0.0'):
    cfg = Config(uuid='00000000-0000-4000-8000-000000000000',
                 state_dir=str(directory), advertise_ip='127.0.0.1', channel_balance=balance)
    backend, peer = make_backend()
    return HttpApi(cfg, RendererState.from_config(cfg), backend), peer


async def save_cut(directory, *, source='', enabled=True):
    api, peer = make_api(directory)
    await api.initialize_channel_balance()
    payload = {'pluginURI': 'synthetic.eq', 'EQBand': [
        {'param_name': 'a_freq', 'value': 1000},
        {'param_name': 'a_gain', 'value': -12},
        {'param_name': 'a_q', 'value': 1},
    ]}
    if source:
        payload['source_name'] = source
    await execute(api, ('EQSetLV2SourceBand:' if source else 'EQSetLV2Band:') + json.dumps(payload))
    await execute(api, 'setChannelBalance:0.5')
    await execute(api, 'EQSave:Quiet room')
    if not enabled:
        await execute(api, 'EQOff')
    return api, peer


@pytest.mark.parametrize('source', ['', 'wifi'])
@pytest.mark.parametrize('enabled', [False, True])
async def test_fresh_config_state_backend_restores_complete_confirmed_graph(tmp_path, source, enabled):
    old, previous_peer = await save_cut(tmp_path / 'state', source=source, enabled=enabled)
    previous = load_active_dsp(old.cfg.state_dir)
    config_path = tmp_path / 'renderer.yaml'
    config_path.write_text(f'state_dir: {old.cfg.state_dir}\nchannel_balance: -1\n')
    cfg = load_config(config_path)
    backend, peer = make_backend()
    restored = HttpApi(cfg, RendererState.from_config(cfg), backend)
    assert restored.eq_current_preset == ''
    assert restored.eq_active_plugin_uri == DEFAULT_EQ_PLUGIN
    assert restored.channel_balance == '0.0'
    assert not peer.calls
    with pytest.raises(BackendError):
        await execute(restored, 'EQGetLV2Band:')
    await restored.initialize_channel_balance()
    assert peer.graph == previous_peer.graph
    assert len([call for call in peer.calls if call[:2] == ['af', 'add']]) == 1
    assert peer.filters[0] == OTHER_FILTER
    assert restored.eq_active_source_name == source
    assert restored.eq_active_plugin_uri == 'synthetic.eq'
    assert restored.eq_enabled is enabled
    assert restored.eq_current_preset == 'Quiet room'
    getter = 'EQGetLV2SourceBand:' if source else 'EQGetLV2Band:'
    result = await execute(restored, getter)
    assert result['Name'] == 'Quiet room'
    assert result['EQStat'] == ('On' if enabled else 'Off')
    assert result['EQBand'] == previous['bands']
    assert await execute(restored, 'getChannelBalance') == '0.5'
    assert load_active_dsp(cfg.state_dir) == previous
    if not enabled:
        assert 'equalizer=' not in peer.graph
        await execute(restored, 'EQOn')
        assert 'g=-12.000' in peer.graph


async def test_absent_state_reconciles_stale_external_graph_to_configured_bypass(tmp_path):
    api, peer = make_api(tmp_path)
    peer.filters.append(entry('volume=0.1'))
    await api.initialize_channel_balance()
    assert peer.graph == ''
    assert peer.filters == [OTHER_FILTER]
    assert load_active_dsp(tmp_path)['bands'] == api._default_eq_bands()


@pytest.mark.parametrize('field,value', [
    ('version', 2), ('version', True), ('enabled', 1), ('enabled', 'false'),
    ('source', 'bluetooth'), ('source', None), ('plugin', ''), ('plugin', []),
    ('name', 1), ('name', 'bad\nname'), ('balance', '0.5'), ('balance', True),
    ('balance', float('nan')), ('balance', float('inf')), ('balance', -1.01),
    ('balance', 0.1234), ('bands', []), ('bands', None),
])
async def test_unknown_or_invalid_active_document_fails_closed(tmp_path, field, value):
    await save_cut(tmp_path)
    path = tmp_path / 'active_dsp.json'
    payload = json.loads(path.read_text())
    payload[field] = value
    path.write_text(json.dumps(payload))
    previous = path.read_bytes()
    with pytest.raises(ValueError):
        make_api(tmp_path)
    assert path.read_bytes() == previous


@pytest.mark.parametrize('encoded', [b'', b'{', b'\xff', b' ' * (ACTIVE_DSP_MAX_BYTES + 1),
                                     b'[' * 2000 + b']' * 2000])
def test_corrupt_oversized_or_deep_active_document_cannot_become_defaults(tmp_path, encoded):
    path = tmp_path / 'active_dsp.json'
    path.write_bytes(encoded)
    with pytest.raises(ValueError):
        make_api(tmp_path)
    assert path.read_bytes() == encoded


@pytest.mark.parametrize('record', [
    {'param_name': 'a_gain', 'value': float('nan')},
    {'param_name': 'a_gain', 'value': 'NaN'},
    {'param_name': 'a_gain', 'value': 25},
    {'param_name': 'a_mode', 'value': 4},
    {'param_name': 'z_gain', 'value': -3},
    {'param_name': 'a_gain', 'value': False},
])
async def test_invalid_persisted_band_cannot_create_a_clamped_curve(tmp_path, record):
    await save_cut(tmp_path)
    payload = load_active_dsp(tmp_path)
    payload['bands'][1] = record
    path = tmp_path / 'active_dsp.json'
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        make_api(tmp_path)


@pytest.mark.parametrize('operation', ['load', 'resume'])
async def test_deferred_startup_cannot_play_until_saved_graph_readback_succeeds(tmp_path, operation):
    await save_cut(tmp_path)
    api, peer = make_api(tmp_path)
    previous_bytes = (tmp_path / 'active_dsp.json').read_bytes()
    graph = api.mpv._audio_filter_graph()
    peer.reply(add(graph), {'error': 'synthetic unavailable'})
    with pytest.raises(BackendError):
        await api.initialize_channel_balance()
    assert api.eq_current_preset == ''
    assert api.channel_balance == '0.0'
    assert peer.graph == ''
    assert (tmp_path / 'active_dsp.json').read_bytes() == previous_bytes
    with pytest.raises(BackendError):
        await execute(api, 'EQGetLV2Band:')
    async def play():
        return await (api.mpv.loadfile('https://media.example.test/quiet.wav')
                      if operation == 'load' else api.mpv.resume())
    peer.calls.clear()
    peer.reply(add(graph), {'error': 'synthetic unavailable'})
    with pytest.raises(BackendError):
        await play()
    assert not peer.loads
    assert ['set_property', 'pause', False] not in peer.calls
    assert ['set_property', 'volume', 50] not in peer.calls
    await play()
    assert peer.graph == graph
    assert await execute(api, 'getChannelBalance') == '0.5'
    assert (await execute(api, 'EQGetLV2Band:'))['Name'] == 'Quiet room'
    if operation == 'load':
        assert peer.loads[0][1][-1] == entry(graph)


async def test_failed_reconfirmation_after_external_reset_hides_active_getters(tmp_path):
    api, peer = await save_cut(tmp_path)
    graph = peer.graph
    peer.filters = [deepcopy(OTHER_FILTER)]
    peer.reply(add(graph), {'error': 'synthetic rejection'})
    with pytest.raises(BackendError):
        await api.mpv.loadfile('https://media.example.test/quiet.wav')
    with pytest.raises(BackendError):
        await execute(api, 'EQGetLV2Band:')
    await api.mpv.loadfile('https://media.example.test/quiet.wav')
    assert (await execute(api, 'EQGetLV2Band:'))['Name'] == 'Quiet room'


@pytest.mark.parametrize('after_replace', [False, True])
async def test_storage_failure_retains_real_confirmed_curve_and_restart_uses_committed_bytes(tmp_path, monkeypatch, after_replace):
    api, peer = await save_cut(tmp_path)
    path = tmp_path / 'active_dsp.json'
    previous = path.read_bytes()
    with monkeypatch.context() as patch:
        if after_replace:
            original = os.fsync
            def fail(descriptor):
                if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise OSError('Synthetic directory durability failure')
                return original(descriptor)
            patch.setattr('fake_linkplay.persistence.os.fsync', fail)
        else:
            def fail(*args):
                raise OSError('Synthetic replacement failure')
            patch.setattr('fake_linkplay.persistence.os.replace', fail)
        with pytest.raises(OSError):
            await execute(api, 'EQSetLV2Band:{"pluginURI":"synthetic.eq","EQBand":[{"param_name":"a_gain","value":-6}]}')
        assert 'g=-6.000' in peer.graph
        result = await execute(api, 'EQGetLV2Band:')
        assert {'param_name': 'a_gain', 'value': -6.0} in result['EQBand']
        assert (path.read_bytes() != previous) is after_replace
    restored, restored_peer = make_api(tmp_path)
    await restored.initialize_channel_balance()
    assert ('g=-6.000' if after_replace else 'g=-12.000') in restored_peer.graph


@pytest.mark.parametrize('value', ['EQv2Rename:{"Name":"Quiet room","newName":"Renamed"}',
                                   'EQv2Delete:{"Name":"Quiet room"}'])
async def test_active_name_metadata_survives_restart_without_audible_graph_update(tmp_path, value):
    api, peer = await save_cut(tmp_path)
    peer.calls.clear()
    await execute(api, value)
    assert peer.calls == []
    expected = 'Renamed' if 'Rename' in value else 'Custom'
    assert load_active_dsp(tmp_path)['name'] == expected
    restored, restored_peer = make_api(tmp_path)
    assert restored.eq_current_preset == ''
    await restored.initialize_channel_balance()
    assert (await execute(restored, 'EQGetLV2Band:'))['Name'] == expected
    assert restored_peer.graph == peer.graph


async def test_inactive_library_save_does_not_persist_proposed_curve_or_selection(tmp_path):
    api, peer = await save_cut(tmp_path)
    path = tmp_path / 'active_dsp.json'
    previous = path.read_bytes()
    peer.calls.clear()
    await execute(api, 'EQSourceSave:{"source_name":"wifi","pluginURI":"other","Name":"Inactive"}')
    assert path.read_bytes() == previous
    assert peer.calls == []
    assert api.eq_current_preset == 'Quiet room'


async def test_saved_name_without_matching_library_never_becomes_active_name_fiction(tmp_path):
    await save_cut(tmp_path)
    (tmp_path / 'eq_presets.json').unlink()
    api, _ = make_api(tmp_path)
    assert api.eq_current_preset == ''
    await api.initialize_channel_balance()
    result = await execute(api, 'EQGetLV2Band:')
    assert result['Name'] == 'Custom'
    assert {'param_name': 'a_gain', 'value': -12.0} in result['EQBand']


async def test_saved_restore_with_legacy_backend_fails_instead_of_balance_only_fallback(tmp_path):
    from test_dsp_api import DspBackend
    await save_cut(tmp_path)
    cfg = Config(state_dir=str(tmp_path))
    backend = DspBackend()
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)
    with pytest.raises(BackendError, match='complete saved DSP'):
        await api.initialize_channel_balance()
    assert backend.calls == []
    assert api.eq_current_preset == ''


async def test_cancelled_restore_holds_reader_until_complete_graph_and_disk_settle(tmp_path):
    await save_cut(tmp_path)
    api, peer = make_api(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    original = peer.command
    async def delayed(args, timeout=5):
        if args[:2] == ['af', 'add']:
            entered.set()
            await release.wait()
        return await original(args, timeout)
    api.mpv.command = delayed
    initialize = asyncio.create_task(api.initialize_channel_balance())
    await entered.wait()
    initialize.cancel()
    getter = asyncio.create_task(execute(api, 'EQGetLV2Band:'))
    await asyncio.sleep(0)
    assert not getter.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await initialize
    assert (await getter)['Name'] == 'Quiet room'
    assert api.mpv.confirmed_dsp is not None
    assert load_active_dsp(tmp_path)['name'] == 'Quiet room'


@pytest.mark.parametrize('extra', [None, {'unexpected': True}])
async def test_active_document_duplicate_or_unknown_fields_are_not_ignored(tmp_path, extra):
    await save_cut(tmp_path)
    path = tmp_path / 'active_dsp.json'
    if extra is None:
        path.write_text(path.read_text().replace('"enabled": true', '"enabled": true, "enabled": false'))
    else:
        payload = json.loads(path.read_text())
        payload.update(extra)
        path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        make_api(tmp_path)


async def test_oversized_active_identifier_rejected_before_backend_or_persistence(tmp_path):
    api, peer = await save_cut(tmp_path)
    path = tmp_path / 'active_dsp.json'
    previous, graph = path.read_bytes(), peer.graph
    peer.calls.clear()
    with pytest.raises(ValueError, match='size limit'):
        await execute(api, 'EQChangeFX:' + 'p' * ACTIVE_DSP_MAX_BYTES)
    assert peer.calls == []
    assert peer.graph == graph
    assert path.read_bytes() == previous
    assert api.eq_active_plugin_uri == 'synthetic.eq'


async def test_library_commit_then_active_metadata_failure_preserves_committed_library(tmp_path, monkeypatch):
    api, peer = await save_cut(tmp_path)
    active_path, library_path = tmp_path / 'active_dsp.json', tmp_path / 'eq_presets.json'
    previous, graph = active_path.read_bytes(), peer.graph
    from fake_linkplay.http_api import atomic_write_text
    def fail_active(path, value):
        if path.name == 'active_dsp.json':
            raise OSError('Synthetic metadata persistence failure')
        atomic_write_text(path, value)
    with monkeypatch.context() as patch:
        patch.setattr('fake_linkplay.http_api.atomic_write_text', fail_active)
        with pytest.raises(OSError):
            await execute(api, 'EQv2Rename:{"Name":"Quiet room","newName":"Renamed"}')
    assert set(api.eq_presets) == set(json.loads(library_path.read_text())['presets']) == {'Renamed'}
    assert (await execute(api, 'EQGetLV2Band:'))['Name'] == 'Renamed'
    assert active_path.read_bytes() == previous
    assert peer.graph == graph
    restored, restored_peer = make_api(tmp_path)
    await restored.initialize_channel_balance()
    assert restored_peer.graph == graph
    # Old committed active-name metadata must not invent a deleted library name.
    assert (await execute(restored, 'EQGetLV2Band:'))['Name'] == 'Custom'


@pytest.mark.parametrize('applied', [False, True])
async def test_complete_graph_candidate_failure_rolls_back_both_settings(applied):
    from test_backend_dsp import band
    backend, peer = make_backend()
    await backend.configure_dsp(-0.25, [band(gain=-12)])
    previous, confirmed = peer.graph, backend.confirmed_dsp
    candidate = backend._build_audio_filter_graph(0.5, [band(gain=-6)])
    peer.reply(add(candidate), {}, apply=applied)
    with pytest.raises(BackendError):
        await backend.configure_dsp(0.5, [band(gain=-6)])
    assert backend.confirmed_dsp == confirmed
    assert peer.graph == previous
    await backend.loadfile('https://media.example.test/quiet.wav')
    assert peer.loads[0][1][-1] == entry(previous)


async def test_play_restore_and_concurrent_gain_dsp_writes_do_not_invert_locks(tmp_path):
    await save_cut(tmp_path)
    api, peer = make_api(tmp_path)
    await asyncio.wait_for(asyncio.gather(
        api.mpv.loadfile('https://media.example.test/quiet.wav'),
        api.mpv.volume(25), api.mpv.mute(True),
        api.mpv.configure_dsp(0.5, api.mpv._peq_bands),
    ), timeout=2)
    assert peer.loads and peer.loads[0][1][-1]['params']['graph'] == peer.graph
    assert 'g=-12.000' in peer.graph
    assert api.mpv._desired_volume == 25 and api.mpv._desired_mute is True


@pytest.mark.parametrize('corrupt', [False, True])
async def test_cli_validates_and_restores_active_graph_before_first_listener(tmp_path, monkeypatch, corrupt):
    from unittest.mock import AsyncMock
    from fake_linkplay import cli
    original, original_peer = await save_cut(tmp_path)
    path = tmp_path / 'active_dsp.json'
    if corrupt:
        path.write_text('{"version":2}')
    backend, peer = make_backend()
    backend.start = AsyncMock()
    backend.stop_process = AsyncMock()
    monkeypatch.setattr(cli, 'load_startup_config', lambda _, **kwargs: original.cfg)
    monkeypatch.setattr(cli, 'setup_logging', lambda _: None)
    monkeypatch.setattr(cli, 'MpvBackend', lambda *args, **kwargs: backend)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, 'add_signal_handler', lambda *args: None)
    monkeypatch.setattr(loop, 'remove_signal_handler', lambda *args: None)
    listeners = []
    class EndOfStartup(Exception):
        pass
    def first_listener(app):
        listeners.append(app)
        assert peer.graph == original_peer.graph
        assert backend.confirmed_dsp is not None
        raise EndOfStartup
    monkeypatch.setattr(cli.web, 'AppRunner', first_listener)
    with pytest.raises(ValueError if corrupt else EndOfStartup):
        await cli.serve('unused-test-config')
    assert len(listeners) == (0 if corrupt else 1)
    backend.stop_process.assert_awaited_once()
    if corrupt:
        assert not any(call[0] in {'af', 'loadfile'} for call in peer.calls)


@pytest.mark.parametrize('operation', ['unmute', 'volume', 'initialize'])
async def test_output_enabling_gain_retries_pending_dsp_before_touching_gain(tmp_path, operation):
    await save_cut(tmp_path)
    api, peer = make_api(tmp_path)
    desired = api.mpv._audio_filter_graph()
    peer.reply(add(desired), {'error': 'unavailable'})
    with pytest.raises(BackendError):
        await api.initialize_channel_balance()
    peer.properties.update(volume=25, mute=True, pause=False)
    peer.calls.clear()
    async def change_gain():
        if operation == 'initialize':
            await api.mpv.initialize_gain()
        else:
            await api.execute(ParsedCommand('mute' if operation == 'unmute' else 'volume',
                                            ('0' if operation == 'unmute' else '50',)), '127.0.0.1')
    peer.reply(add(desired), {'error': 'unavailable'})
    with pytest.raises(BackendError):
        await change_gain()
    assert peer.graph == ''
    assert peer.properties['mute'] is True and peer.properties['volume'] == 25
    assert not any(call[0] == 'set_property' for call in peer.calls)
    peer.calls.clear()
    await change_gain()
    assert peer.graph == desired
    assert peer.calls.index(add(desired)) < next(
        index for index, call in enumerate(peer.calls) if call[0] == 'set_property')
    assert (await execute(api, 'EQGetLV2Band:'))['Name'] == 'Quiet room'


@pytest.mark.parametrize('operation', ['mute', 'zero'])
async def test_emergency_silence_is_available_when_saved_dsp_unavailable(tmp_path, operation):
    await save_cut(tmp_path)
    api, peer = make_api(tmp_path)
    peer.reply(add(api.mpv._audio_filter_graph()), {'error': 'unavailable'})
    with pytest.raises(BackendError):
        await api.initialize_channel_balance()
    peer.calls.clear()
    peer.properties.update(volume=50, mute=False, pause=False)
    await api.execute(ParsedCommand('mute' if operation == 'mute' else 'volume',
                                   ('1' if operation == 'mute' else '0',)), '127.0.0.1')
    assert not any(call[0] == 'af' for call in peer.calls)
    assert peer.properties['mute'] is True if operation == 'mute' else peer.properties['volume'] == 0
    assert api._pending_dsp_restore is not None
    with pytest.raises(BackendError):
        await execute(api, 'EQGetLV2Band:')


@pytest.mark.parametrize('recovery', ['load', 'unmute'])
async def test_successful_replay_clears_runtime_uncertainty_only_for_matching_graph(tmp_path, recovery):
    api, peer = await save_cut(tmp_path)
    graph = peer.graph
    peer.filters = [deepcopy(OTHER_FILTER)]
    peer.reply(add(graph.replace('g=-12.000', 'g=-6.000')), {'error': 'unavailable'})
    with pytest.raises(BackendError):
        await execute(api, 'EQSetLV2Band:{"pluginURI":"synthetic.eq","EQBand":[{"param_name":"a_gain","value":-6}]}')
    assert api._dsp_state_uncertain
    with pytest.raises(BackendError):
        await execute(api, 'EQGetLV2Band:')
    previous = deepcopy(api.mpv._peq_bands)
    # A real but unrelated confirmed graph cannot rehabilitate stale API state.
    await api.mpv.configure_dsp(-0.5, [])
    with pytest.raises(BackendError):
        await execute(api, 'EQGetLV2Band:')
    assert api._dsp_state_uncertain
    await api.mpv.configure_dsp(0.5, previous)
    # Force the desired restore through the same public recovery route.
    peer.filters = [deepcopy(OTHER_FILTER)]
    if recovery == 'load':
        await api.mpv.loadfile('https://media.example.test/quiet.wav')
    else:
        await api.mpv.mute(False)
    assert peer.graph == graph
    assert (await execute(api, 'EQGetLV2Band:'))['Name'] == 'Quiet room'
    assert not api._dsp_state_uncertain
    assert await execute(api, 'getChannelBalance') == '0.5'


@pytest.mark.parametrize('legacy', [b'junk', b'\xff', b'NaN', b'999'])
async def test_complete_active_snapshot_supersedes_malformed_legacy_balance(tmp_path, legacy):
    old, old_peer = await save_cut(tmp_path / 'state')
    (tmp_path / 'state' / 'channel_balance').write_bytes(legacy)
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(f'state_dir: {tmp_path / "state"}\n')
    cfg = load_config(config_path)
    backend, peer = make_backend()
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)
    await api.initialize_channel_balance()
    assert peer.graph == old_peer.graph
    assert await execute(api, 'getChannelBalance') == '0.5'
    # Direct Config construction follows the same precedence rule.
    direct, _ = make_api(tmp_path / 'state', balance='junk')
    await direct.initialize_channel_balance()
    assert await execute(direct, 'getChannelBalance') == '0.5'


@pytest.mark.parametrize('command', [
    'EQSave:Bad\nName',
    'EQv2Rename:{"Name":"Quiet room","newName":"Bad\\nName"}',
    'EQSourceSave:{"Name":"Bad\\nName"}',
])
async def test_invalid_preset_name_rejected_before_library_commit_and_cannot_block_bypass(tmp_path, command):
    api, peer = await save_cut(tmp_path)
    previous = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    graph = peer.graph
    with pytest.raises(ValueError):
        await execute(api, command)
    assert peer.graph == graph
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()} == previous
    assert (await execute(api, 'EQGetLV2Band:'))['Name'] == 'Quiet room'
    await execute(api, 'EQOff')
    assert 'equalizer=' not in peer.graph
