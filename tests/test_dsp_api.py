"""UNIT/MOCK truthful HTTP EQ/balance controls, no audio-device access."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import quote

import pytest
from aiohttp.test_utils import make_mocked_request

from fake_linkplay.config import Config
from fake_linkplay.http_api import DEFAULT_EQ_PLUGIN, HttpApi, ParsedCommand
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.state import RendererState


class DspBackend:
    def __init__(self):
        self.calls = []
        self.bands = []
        self.balance = 0.0
        self.error = None
        self.entered = None
        self.release = None

    async def apply(self, kind, value):
        self.calls.append((kind, deepcopy(value)))
        if self.entered is not None:
            self.entered.set()
            await self.release.wait()
        if self.error is not None:
            raise self.error
        if kind == 'peq':
            self.bands = deepcopy(value)
        else:
            self.balance = value

    async def peq(self, bands):
        await self.apply('peq', bands)

    async def channel_balance(self, value):
        await self.apply('balance', value)


def make_api(tmp_path):
    cfg = Config(uuid='00000000-0000-4000-8000-000000000000',
                 state_dir=str(tmp_path), advertise_ip='127.0.0.1')
    return HttpApi(cfg, RendererState.from_config(cfg), DspBackend())


@pytest.fixture
def api(tmp_path):
    return make_api(tmp_path)


def command(name, payload=None):
    return name if payload is None else name + ':' + json.dumps(payload)


def band_command(**fields):
    return command('EQSetLV2Band', {
        'pluginURI': DEFAULT_EQ_PLUGIN,
        'EQBand': [{'param_name': key, 'value': value} for key, value in fields.items()],
    })


async def execute(api, value):
    return await api.execute(ParsedCommand(value), '127.0.0.1')


async def http(api, value):
    return await api.handle_httpapi(make_mocked_request('GET', '/httpapi.asp?command=' + quote(value, safe='')))


def snapshot(api):
    return deepcopy({key: value for key, value in vars(api).items()
                     if key.startswith('eq_') or key == 'channel_balance'})


@pytest.mark.parametrize('raw', ['NaN', 'nan', 'inf', '-inf', 'Infinity', '101', '-101', 'abc', '', '1e9999'])
async def test_invalid_balance_is_400_without_backend_or_disk_change(api, raw):
    before = snapshot(api)
    response = await http(api, 'setChannelBalance:' + raw)
    assert response.status == 400
    assert not api.mpv.calls
    assert snapshot(api) == before
    assert not (Path(api.cfg.state_dir) / 'channel_balance').exists()


@pytest.mark.parametrize('key,value', [
    ('a_gain', float('nan')), ('a_gain', float('inf')), ('a_gain', -float('inf')),
    ('a_gain', 'nan'), ('a_gain', True), ('a_gain', None), ('a_gain', []),
    ('a_gain', 24.001), ('a_gain', -24.001), ('a_freq', 0), ('a_freq', 24001),
    ('a_freq', 'Infinity'), ('a_q', 0), ('a_q', 30.01), ('a_mode', 4),
    ('a_mode', 1.5), ('a_mode', float('nan')), ('a_mode', '3.5'),
    ('z_gain', -2), ('a_unknown', 1), ('a_gain', 10 ** 1000),
])
async def test_invalid_eq_values_never_become_clamped_gain(api, key, value):
    before = snapshot(api)
    response = await http(api, band_command(**{key: value}))
    assert response.status == 400
    assert not api.mpv.calls
    assert snapshot(api) == before


@pytest.mark.parametrize('payload', [
    None, [], 42, 'bad', {}, {'EQBand': {}}, {'EQBand': [None]},
    {'EQBand': [{'param_name': 'a_gain'}]},
    {'EQBand': [{'param_name': 'a_gain', 'value': -2, 'extra': 'unsafe'}]},
    {'EQBand': [{'param_name': 'a_gain', 'value': -2}] * 2},
    {'EQBand': [], 'EQBandL': [{'param_name': 'a_gain', 'value': -2}]},
    {'EQBand': [], 'EQBandR': True},
    {'EQBand': [], 'pluginURI': []},
])
async def test_malformed_eq_schema_rejected_before_mutation(api, payload):
    value = 'EQSetLV2Band:' + json.dumps(payload)
    before = snapshot(api)
    assert (await http(api, value)).status == 400
    assert snapshot(api) == before
    assert not api.mpv.calls


@pytest.mark.parametrize('name', [
    'EQSetLV2SourceBand', 'EQGetLV2SourceBand', 'EQGetLV2SourceBandEx',
    'EQChangeSourceFX', 'EQSourceSave', 'EQv2SourceLoad', 'EQSourceOff',
    'EQSetLV2Band', 'EQv2Load', 'EQv2GetNewList',
])
async def test_unsupported_source_does_not_touch_wifi_dsp(api, name):
    await execute(api, band_command(a_gain=-3))
    before, calls = snapshot(api), list(api.mpv.calls)
    value = command(name, {'source_name': 'bluetooth', 'EQBand': [], 'Name': 'Flat'})
    assert (await http(api, value)).status == 400
    assert snapshot(api) == before
    assert api.mpv.calls == calls


@pytest.mark.parametrize('name', [
    'EQSetLV2Band', 'EQSetLV2SourceBand', 'EQChangeSourceFX', 'EQSourceSave',
    'EQv2Load', 'EQv2SourceLoad', 'EQSetChannelMode',
])
async def test_unsupported_channel_mode_rejected_everywhere(api, name):
    before = snapshot(api)
    assert (await http(api, command(name, {'channelMode': 'Left', 'EQBand': [], 'Name': 'Flat'}))).status == 400
    assert snapshot(api) == before
    assert not api.mpv.calls


async def test_channel_mode_stereo_is_noop_on_active_selection(api):
    await execute(api, band_command(a_gain=-3))
    calls = list(api.mpv.calls)
    assert (await http(api, 'EQSetChannelMode:Left')).status == 400
    assert await execute(api, command('EQSetChannelMode', {'pluginURI': 'other', 'channelMode': 'Stereo'})) == {'status': '0'}
    assert api.eq_active_plugin_uri == DEFAULT_EQ_PLUGIN
    assert api.mpv.calls == calls


@pytest.mark.parametrize('mode', [3, 5])
async def test_zero_gain_pass_filters_reach_backend(api, mode):
    assert (await http(api, band_command(a_freq=1000, a_gain=0, a_q=0.7, a_mode=mode))).status == 200
    assert api.mpv.bands == [{'freq': 1000.0, 'gain': 0.0, 'q': 0.7, 'mode': float(mode)}]
    assert (await execute(api, 'EQGetLV2Band:'))['Name'] == 'Custom'


async def test_finite_numeric_strings_and_complete_canonical_order(api):
    await execute(api, band_command(j_gain='-24', j_freq='24000', j_q='30', j_mode='2'))
    result = await execute(api, 'EQGetLV2Band:')
    assert len(result['EQBand']) == 40
    assert result['EQBand'][0] == {'param_name': 'a_freq', 'value': 30.0}
    assert result['EQBand'][-1] == {'param_name': 'j_mode', 'value': 2.0}
    assert api.mpv.bands == [{'freq': 24000.0, 'gain': -24.0, 'q': 30.0, 'mode': 2.0}]


@pytest.mark.parametrize('value', [
    'EQGetLV2Band:other',
    'EQGetLV2SourceBand:{"source_name":"wifi","pluginURI":"other"}',
    'EQGetLV2SourceBandEx:{"source_name":"wifi","pluginURI":"other"}',
    'EQv2GetList:other', 'EQv2GetNewList:{"pluginURI":"other"}',
])
async def test_eq_getters_are_pure_and_return_defensive_copies(api, value):
    await execute(api, band_command(a_gain=-3))
    before, calls = snapshot(api), list(api.mpv.calls)
    result = await execute(api, value)
    if 'EQBand' in result:
        assert result['EQStat'] == 'Off'
        result['EQBand'][0]['value'] = 12345
    assert snapshot(api) == before
    assert api.mpv.calls == calls
    assert (await execute(api, 'EQGetLV2Band:'))['EQStat'] == 'On'


@pytest.mark.parametrize('value', [
    band_command(a_gain=-9),
    'EQSetLV2SourceBand:{"source_name":"wifi","pluginURI":"other","EQBand":[]}',
    'EQChangeFX:other', 'EQChangeSourceFX:{"source_name":"wifi","pluginURI":"other"}',
    'EQv2Load:{"Name":"Flat"}', 'EQv2SourceLoad:{"source_name":"wifi","Name":"Flat"}',
    'EQSourceOff:{"source_name":"wifi","pluginURI":"other"}', 'EQOff', 'EQOn',
    'setChannelBalance:-0.75',
])
async def test_backend_rejection_rolls_back_all_frontend_state_and_persistence(api, value):
    await execute(api, band_command(a_gain=-3))
    await execute(api, 'EQSave:Saved')
    await execute(api, 'setChannelBalance:0.25')
    before = snapshot(api)
    files = {path.name: path.read_bytes() for path in Path(api.cfg.state_dir).iterdir() if path.is_file()}
    api.mpv.error = BackendError('Synthetic rejection')
    response = await http(api, value)
    assert response.status == 502
    assert snapshot(api) == before
    assert api.cfg.channel_balance == '0.25'
    assert {path.name: path.read_bytes() for path in Path(api.cfg.state_dir).iterdir() if path.is_file()} == files
    assert (await execute(api, 'EQGetLV2Band:'))['Name'] == 'Saved'


async def test_error_map_from_backend_is_not_confirmation(api):
    async def reject(value):
        return {'error': 'invalid parameter'}
    api.mpv.peq = reject
    before = snapshot(api)
    assert (await http(api, band_command(a_gain=-3))).status == 502
    assert snapshot(api) == before


async def test_uncertain_backend_rollback_does_not_claim_old_dsp_active(api):
    await execute(api, band_command(a_gain=-3))
    api.mpv._dsp_configuration_confirmed = False
    api.mpv.error = BackendError('Restoration unconfirmed')
    assert (await http(api, band_command(a_gain=-6))).status == 502
    assert (await http(api, 'EQGetLV2Band:')).status == 502
    assert (await http(api, 'getChannelBalance')).status == 502
    api.mpv.error = None
    await execute(api, band_command(a_gain=-4))
    assert (await http(api, 'EQGetLV2Band:')).status == 200


@pytest.mark.parametrize('reject', [False, True])
async def test_readers_wait_for_backend_acceptance_and_never_see_candidate(api, reject):
    await execute(api, band_command(a_gain=-3))
    api.mpv.entered, api.mpv.release = asyncio.Event(), asyncio.Event()
    if reject:
        api.mpv.error = BackendError('Synthetic rejection')
    setter = asyncio.create_task(http(api, band_command(a_gain=-6)))
    await api.mpv.entered.wait()
    getter = asyncio.create_task(execute(api, 'EQGetLV2Band:'))
    await asyncio.sleep(0)
    assert not getter.done()
    api.mpv.release.set()
    response, readback = await asyncio.gather(setter, getter)
    assert response.status == (502 if reject else 200)
    assert {'param_name': 'a_gain', 'value': -3.0 if reject else -6.0} in readback['EQBand']


@pytest.mark.parametrize('reject', [False, True])
async def test_cancelled_request_settles_backend_before_unlocking_readers(api, reject):
    await execute(api, band_command(a_gain=-3))
    api.mpv.entered, api.mpv.release = asyncio.Event(), asyncio.Event()
    if reject:
        api.mpv.error = BackendError('Synthetic rejection')
    setter = asyncio.create_task(execute(api, band_command(a_gain=-6)))
    await api.mpv.entered.wait()
    setter.cancel()
    getter = asyncio.create_task(execute(api, 'EQGetLV2Band:'))
    await asyncio.sleep(0)
    setter.cancel()
    await asyncio.sleep(0)
    assert not setter.done()
    assert not getter.done()
    api.mpv.release.set()
    with pytest.raises(asyncio.CancelledError):
        await setter
    readback = await getter
    assert {'param_name': 'a_gain', 'value': -3.0 if reject else -6.0} in readback['EQBand']
    assert api.mpv.bands[0]['gain'] == (-3.0 if reject else -6.0)


async def test_concurrent_setters_merge_in_confirmed_order(api):
    api.mpv.entered, api.mpv.release = asyncio.Event(), asyncio.Event()
    first = asyncio.create_task(execute(api, band_command(a_gain=-3)))
    await api.mpv.entered.wait()
    second = asyncio.create_task(execute(api, band_command(b_gain=-4)))
    third = asyncio.create_task(execute(api, 'setChannelBalance:0.2'))
    await asyncio.sleep(0)
    assert len(api.mpv.calls) == 1
    api.mpv.release.set()
    await asyncio.gather(first, second, third)
    assert [band['gain'] for band in api.mpv.bands] == [-3.0, -4.0]
    assert api.mpv.balance == 0.2
    assert await execute(api, 'getChannelBalance') == '0.2'


async def test_balance_storage_failure_reports_error_and_preserves_confirmed_setting(api, monkeypatch):
    await execute(api, 'setChannelBalance:0.2')
    def fail(*args):
        raise OSError('Synthetic storage failure')
    monkeypatch.setattr('fake_linkplay.http_api.atomic_write_text', fail)
    assert (await http(api, 'setChannelBalance:-0.4')).status == 502
    assert await execute(api, 'getChannelBalance') == '-0.4'
    assert api.mpv.balance == -0.4
    assert api.cfg.channel_balance == '-0.4'
    assert (Path(api.cfg.state_dir) / 'channel_balance').read_text().strip() == '0.2'


@pytest.mark.parametrize('value', ['EQSave:New', 'EQv2Delete:{"Name":"Saved"}',
                                    'EQv2Rename:{"Name":"Saved","newName":"New"}',
                                    'EQSourceSave:{"Name":"New"}'])
async def test_library_storage_failure_rolls_back_only_unsaved_metadata(api, monkeypatch, value):
    await execute(api, band_command(a_gain=-3))
    await execute(api, 'EQSave:Saved')
    before, calls = snapshot(api), list(api.mpv.calls)
    path = Path(api.cfg.state_dir) / 'eq_presets.json'
    previous = path.read_bytes()
    def fail(*args):
        raise OSError('Synthetic storage failure')
    monkeypatch.setattr('fake_linkplay.http_api.atomic_write_text', fail)
    assert (await http(api, value)).status == 502
    assert snapshot(api) == before
    assert api.mpv.calls == calls
    assert path.read_bytes() == previous


async def test_delete_and_rename_keep_active_name_honest(api):
    await execute(api, band_command(a_gain=-3))
    await execute(api, 'EQSave:Saved')
    await execute(api, 'EQv2Rename:{"Name":"Saved","newName":"Renamed"}')
    assert (await execute(api, 'EQGetLV2Band:'))['Name'] == 'Renamed'
    await execute(api, 'EQv2Delete:{"Name":"Renamed"}')
    assert (await execute(api, 'EQGetLV2Band:'))['Name'] == 'Custom'
    before, calls = snapshot(api), list(api.mpv.calls)
    assert (await http(api, 'EQv2Load:{"Name":"Renamed"}')).status == 400
    assert snapshot(api) == before
    assert api.mpv.calls == calls


async def test_source_save_never_changes_active_context_without_application(api):
    await execute(api, band_command(a_gain=-3))
    before_bands, calls = deepcopy(api.mpv.bands), list(api.mpv.calls)
    await execute(api, 'EQSourceSave:{"Name":"Other","source_name":"wifi","pluginURI":"other"}')
    assert api.eq_active_plugin_uri == DEFAULT_EQ_PLUGIN
    assert api.eq_active_source_name == ''
    assert api.mpv.bands == before_bands
    assert api.mpv.calls == calls
    assert (await execute(api, 'EQGetLV2Band:'))['Name'] == 'Custom'


async def test_legacy_valid_library_migrates_without_restoring_unapplied_name(tmp_path):
    path = tmp_path / 'eq_presets.json'
    path.write_text(json.dumps({'current': 'Saved', 'presets': {
        'Saved': {DEFAULT_EQ_PLUGIN: [{'param_name': 'a_gain', 'value': -3}]}}}))
    api = make_api(tmp_path)
    assert not api.mpv.calls
    assert api.eq_current_preset == ''
    assert (await execute(api, 'EQGetLV2Band:'))['Name'] == 'Flat'
    assert len(api.eq_presets['Saved'][DEFAULT_EQ_PLUGIN]) == 40
    await execute(api, 'EQv2Load:{"Name":"Saved"}')
    assert (await execute(api, 'EQGetLV2Band:'))['Name'] == 'Saved'
    assert api.mpv.bands[0]['gain'] == -3
    await execute(api, 'EQSave:Again')
    stored = json.loads(path.read_text())
    assert stored['version'] == 1
    assert 'current' not in stored
    assert set(stored['presets']) == {'Saved', 'Again'}


@pytest.mark.parametrize('payload', [
    {'version': 2, 'presets': {}}, {'version': True, 'presets': {}}, [],
    {'presets': {'Unsafe': {DEFAULT_EQ_PLUGIN: [{'param_name': 'a_gain', 'value': float('nan')}]}}},
    {'presets': {'Unsafe': {DEFAULT_EQ_PLUGIN: [{'param_name': 'a_gain', 'value': 'nan'}]}}},
    {'presets': {'Unsafe': {DEFAULT_EQ_PLUGIN: [{'param_name': 'a_gain', 'value': 25}]}}},
    {'presets': {'Unsafe': {DEFAULT_EQ_PLUGIN: [{'param_name': 'z_gain', 'value': 10}]}}},
    {'presets': {'Unsafe': {DEFAULT_EQ_PLUGIN: [None]}}},
])
async def test_corrupt_preset_document_cannot_inject_gain_or_active_name(tmp_path, payload):
    path = tmp_path / 'eq_presets.json'
    path.write_text(json.dumps(payload))
    previous = path.read_bytes()
    api = make_api(tmp_path)
    assert api.eq_presets == {}
    assert api.eq_current_preset == ''
    assert (await execute(api, 'EQGetLV2Band:'))['Name'] == 'Flat'
    assert (await http(api, 'EQv2Load:{"Name":"Unsafe"}')).status == 400
    assert not api.mpv.calls
    assert path.read_bytes() == previous


async def test_fresh_eq_on_is_confirmed_bypass(api):
    assert await execute(api, 'EQOn') == {'status': '0'}
    assert api.mpv.calls == [('peq', [])]


async def test_missing_active_source_plugin_never_reuses_another_cached_curve(api):
    await execute(api, 'EQSetLV2SourceBand:{"source_name":"wifi","pluginURI":"cached","EQBand":[{"param_name":"a_gain","value":-3}]}')
    await execute(api, 'EQSourceOff:{"source_name":"wifi","pluginURI":"missing"}')
    await execute(api, 'EQOn')
    assert api.mpv.bands == []
    readback = await execute(api, 'EQGetLV2SourceBand:{"source_name":"wifi"}')
    assert readback['pluginURI'] == 'missing'
    assert readback['Name'] == 'Flat'
    assert api.eq_current_preset == 'Flat'


async def test_saving_same_name_preserves_other_plugin_entries(api):
    await execute(api, band_command(a_gain=-3))
    await execute(api, 'EQSave:Shared')
    original = deepcopy(api.eq_presets['Shared'][DEFAULT_EQ_PLUGIN])
    await execute(api, 'EQChangeFX:other')
    assert api.eq_current_preset == 'Flat'
    await execute(api, 'EQSave:Shared')
    assert set(api.eq_presets['Shared']) == {DEFAULT_EQ_PLUGIN, 'other'}
    assert api.eq_presets['Shared'][DEFAULT_EQ_PLUGIN] == original


async def test_preset_lists_only_offer_loadable_plugin_entries(api):
    await execute(api, band_command(a_gain=-3))
    await execute(api, 'EQSave:Saved')
    assert (await execute(api, 'EQv2GetList:other'))['custom'] == []
    assert [item['Name'] for item in (await execute(api, 'EQv2GetList:' + DEFAULT_EQ_PLUGIN))['custom']] == ['Saved']


async def test_global_getter_cannot_label_different_source_cache_active(api):
    await execute(api, band_command(a_gain=-3))
    await execute(api, command('EQSetLV2SourceBand', {'source_name': 'wifi', 'EQBand': [{'param_name': 'a_gain', 'value': -6}]}))
    result = await execute(api, 'EQGetLV2Band:{"source_name":"wifi"}')
    assert result['EQStat'] == 'Off'
    assert {'param_name': 'a_gain', 'value': -3.0} in result['EQBand']
    assert api.mpv.bands[0]['gain'] == -6


@pytest.mark.parametrize('raw', ['nan', 'inf', '-inf', '101', '-101', 'bad', '', None, True])
def test_invalid_startup_balance_fails_closed_without_backend_use(tmp_path, raw):
    cfg = Config(uuid='00000000-0000-4000-8000-000000000000', state_dir=str(tmp_path), channel_balance=raw)
    backend = DspBackend()
    with pytest.raises(ValueError):
        HttpApi(cfg, RendererState.from_config(cfg), backend)
    assert not backend.calls


@pytest.mark.parametrize('raw,expected', [('30', '0.3'), ('-25', '-0.25'), ('0.2500', '0.25'), ('0', '0.0')])
def test_startup_balance_is_canonical_before_backend_application(tmp_path, raw, expected):
    cfg = Config(uuid='00000000-0000-4000-8000-000000000000', state_dir=str(tmp_path), channel_balance=raw)
    api = HttpApi(cfg, RendererState.from_config(cfg), DspBackend())
    assert cfg.channel_balance == expected
    assert api.channel_balance == "0.0"
    assert not api.mpv.calls


async def test_startup_balance_only_becomes_actual_after_confirmation(tmp_path):
    cfg = Config(uuid='00000000-0000-4000-8000-000000000000', state_dir=str(tmp_path), channel_balance='50')
    api = HttpApi(cfg, RendererState.from_config(cfg), DspBackend())
    assert cfg.channel_balance == '0.5'
    assert await execute(api, 'getChannelBalance') == '0.0'
    await api.initialize_channel_balance()
    assert api.mpv.balance == 0.5
    assert await execute(api, 'getChannelBalance') == '0.5'
    assert (tmp_path / 'channel_balance').read_text() == '0.5\n'


async def test_failed_startup_balance_then_eq_never_advertises_unapplied_balance(tmp_path):
    cfg = Config(uuid='00000000-0000-4000-8000-000000000000', state_dir=str(tmp_path), channel_balance='0.5')
    api = HttpApi(cfg, RendererState.from_config(cfg), DspBackend())
    api.mpv._dsp_configuration_confirmed = False
    api.mpv.error = BackendError('Startup balance rejected')
    with pytest.raises(BackendError):
        await api.initialize_channel_balance()
    assert api.channel_balance == '0.0'
    assert api.cfg.channel_balance == '0.5'  # Desired configuration remains available to retry.
    assert api.mpv.balance == 0.0
    assert (await http(api, 'getChannelBalance')).status == 502
    assert not (tmp_path / 'channel_balance').exists()
    api.mpv.error = None
    await execute(api, band_command(a_gain=-3))
    assert await execute(api, 'getChannelBalance') == '0.0'
    assert api.mpv.balance == 0.0


async def test_zero_startup_balance_still_reconciles_backend(api):
    api.mpv.balance = 0.75  # Simulate an external backend retaining an old setting.
    await api.initialize_channel_balance()
    assert api.mpv.calls == [('balance', 0.0)]
    assert api.mpv.balance == 0.0
    assert await execute(api, 'getChannelBalance') == '0.0'


@pytest.mark.parametrize('value,names,active_name', [
    ('EQSave:New', {'Saved', 'New'}, 'New'),
    ('EQSourceSave:{"Name":"New"}', {'Saved', 'New'}, 'New'),
    ('EQv2Delete:{"Name":"Saved"}', set(), 'Custom'),
    ('EQv2Rename:{"Name":"Saved","newName":"New"}', {'New'}, 'New'),
])
async def test_directory_fsync_failure_keeps_committed_library_visible(api, monkeypatch, value, names, active_name):
    import os
    import stat
    await execute(api, band_command(a_gain=-3))
    await execute(api, 'EQSave:Saved')
    path = Path(api.cfg.state_dir) / 'eq_presets.json'
    original_fsync = os.fsync
    def fail_directory_fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError('Synthetic directory fsync failure after replace')
        return original_fsync(descriptor)
    monkeypatch.setattr('fake_linkplay.persistence.os.fsync', fail_directory_fsync)
    assert (await http(api, value)).status == 502
    stored = json.loads(path.read_text())
    assert set(stored['presets']) == set(api.eq_presets) == names
    assert stored['presets'] == api.eq_presets
    assert (await execute(api, 'EQGetLV2Band:'))['Name'] == active_name
    assert api.mpv.bands[0]['gain'] == -3


async def test_real_pre_replace_library_failure_preserves_old_memory_and_disk(api, monkeypatch):
    await execute(api, band_command(a_gain=-3))
    await execute(api, 'EQSave:Saved')
    path = Path(api.cfg.state_dir) / 'eq_presets.json'
    previous, before = path.read_bytes(), snapshot(api)
    def fail_replace(*args):
        raise OSError('Synthetic replace failure')
    monkeypatch.setattr('fake_linkplay.persistence.os.replace', fail_replace)
    assert (await http(api, 'EQv2Delete:{"Name":"Saved"}')).status == 502
    assert path.read_bytes() == previous
    assert snapshot(api) == before


async def test_rename_normalizes_names_and_rejects_collision_without_library_loss(api):
    await execute(api, band_command(a_gain=-3))
    await execute(api, 'EQSave:A')
    await execute(api, 'EQSave:B')
    path = Path(api.cfg.state_dir) / 'eq_presets.json'
    previous, before = path.read_bytes(), snapshot(api)
    assert (await http(api, 'EQv2Rename:{"Name":"A","newName":" B "}')).status == 400
    assert snapshot(api) == before
    assert path.read_bytes() == previous
    assert set(make_api(Path(api.cfg.state_dir)).eq_presets) == {'A', 'B'}
    await execute(api, 'EQv2Rename:{"Name":" B ","newName":" C "}')
    assert set(api.eq_presets) == {'A', 'C'}
    assert (await execute(api, 'EQGetLV2Band:'))['Name'] == 'C'
    assert set(make_api(Path(api.cfg.state_dir)).eq_presets) == {'A', 'C'}


async def test_saved_library_size_boundary_matches_reader_without_losing_prior_data(api):
    path = Path(api.cfg.state_dir) / 'eq_presets.json'
    prototype = {'version': 1, 'presets': {'N': {DEFAULT_EQ_PLUGIN: api._default_eq_bands()}}}
    base_size = len((json.dumps(prototype, indent=2, sort_keys=True, allow_nan=False) + '\n').encode('utf-8'))
    name = 'N' + 'x' * (1024 * 1024 - base_size)
    await execute(api, 'EQSave:' + name)
    previous = path.read_bytes()
    assert len(previous) == 1024 * 1024
    assert set(make_api(Path(api.cfg.state_dir)).eq_presets) == {name}
    with pytest.raises(ValueError, match='size limit'):
        await execute(api, 'EQSave:WouldOverflow')
    assert set(api.eq_presets) == {name}
    assert path.read_bytes() == previous
    assert set(make_api(Path(api.cfg.state_dir)).eq_presets) == {name}
