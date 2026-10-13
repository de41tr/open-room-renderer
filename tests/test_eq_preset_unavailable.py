"""UNIT + loopback HTTP/MOCK observed EQ-library failure preservation.

Only synthetic temporary files and backends. No repair, salvage or live reload.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest

from fake_linkplay.config import Config
from fake_linkplay.eq_presets import (DEFAULT_EQ_PLUGIN, EqPresetLibrary,
    EqPresetLibraryUnavailableError, PRESET_LIBRARY_UNAVAILABLE_MESSAGE,
    default_eq_bands)
from fake_linkplay.http_api import HttpApi
from fake_linkplay.persistence import AtomicWriteCommittedError, atomic_write_text
from fake_linkplay.state import RendererState
from fake_linkplay.web_ui import decode_command
from test_dsp_api import execute, http, snapshot
from test_web_ui import WebBackend, command, serving, state


VALID = json.dumps({'version': 1, 'presets': {'Recoverable': {
    DEFAULT_EQ_PLUGIN: [{'param_name': 'a_gain', 'value': -3}],
}}}).encode()
INVALID = json.dumps({'presets': {
    'Recoverable': {DEFAULT_EQ_PLUGIN: [{'param_name': 'a_gain', 'value': -3}]},
    'Invalid': {DEFAULT_EQ_PLUGIN: [{'param_name': 'a_gain', 'value': 25}]},
}}).encode()


def make_api(directory):
    cfg = Config(uuid='00000000-0000-4000-8000-000000000000',
                 state_dir=str(directory), advertise_ip='127.0.0.1')
    return HttpApi(cfg, RendererState.from_config(cfg), WebBackend())


def reject_read(monkeypatch, path, error):
    original = Path.open
    def open_path(self, mode='r', *args, **kwargs):
        if self == path and mode == 'rb':
            raise error
        return original(self, mode, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', open_path)


def no_writes(monkeypatch):
    writes = []
    def forbidden(*args):
        writes.append(args)
        raise AssertionError('A blocked command reached the writer')
    monkeypatch.setattr('fake_linkplay.http_api.atomic_write_text', forbidden)
    return writes


@pytest.mark.parametrize('encoded,error', [
    (b'{', ValueError), (b'[]', ValueError), (b'{}', ValueError),
    (b'{"version":2,"presets":{}}', ValueError),
    (b'{"version":true,"presets":{}}', ValueError),
    (b'\xff', UnicodeError), (b' ' * (1024 * 1024 + 1), ValueError),
    (b'[' * 20000 + b']' * 20000, RecursionError),
    (b'{"presets":{},"presets":{}}', ValueError),
    (b'{"presets":{" A ":{},"A":{}}}', ValueError),
    (INVALID, ValueError),
], ids=['malformed', 'schema-list', 'missing-presets', 'version', 'boolean-version',
        'encoding', 'oversized', 'nesting', 'duplicate-field', 'duplicate-name', 'invalid-record'])
def test_failed_load_latches_without_publishing_partial_mapping_or_writing(tmp_path, encoded, error):
    path = tmp_path / 'eq_presets.json'
    path.write_bytes(encoded)
    library = EqPresetLibrary()
    library.save('Existing', DEFAULT_EQ_PLUGIN, default_eq_bands())
    previous = library.presets
    before = deepcopy(previous)
    with pytest.raises(error):
        library.load(tmp_path)
    assert library.unavailable and library.presets is previous and library.presets == before
    writes = []
    with pytest.raises(EqPresetLibraryUnavailableError) as raised:
        library.persist(tmp_path, write_text=lambda *args: writes.append(args))
    assert str(raised.value) == PRESET_LIBRARY_UNAVAILABLE_MESSAGE
    assert not writes and path.read_bytes() == encoded


@pytest.mark.parametrize('error_type', [PermissionError, OSError, TypeError, UnicodeError])
def test_failed_read_preserves_original_exception_and_fences_before_serializing(tmp_path, monkeypatch, error_type):
    path = tmp_path / 'eq_presets.json'
    path.write_bytes(VALID)
    library = EqPresetLibrary()
    error = error_type('https://private.test/SECRET')
    with monkeypatch.context() as patch:
        reject_read(patch, path, error)
        with pytest.raises(error_type) as raised:
            library.load(tmp_path)
        assert raised.value is error
    # Compatibility assignments cannot clear the latch or reach serialization.
    library.presets = {'unserializable': object()}
    writes = []
    with pytest.raises(EqPresetLibraryUnavailableError):
        library.persist(tmp_path, write_text=lambda *args: writes.append(args))
    assert not writes and path.read_bytes() == VALID


def test_missing_file_is_healthy_but_dangling_file_symlink_is_unavailable(tmp_path):
    missing = tmp_path / 'new' / 'state'
    library = EqPresetLibrary()
    library.load(missing)
    assert not library.unavailable
    library.save('New', DEFAULT_EQ_PLUGIN, default_eq_bands())
    library.persist(missing)
    assert json.loads((missing / 'eq_presets.json').read_text())['presets'] == library.presets

    path = tmp_path / 'eq_presets.json'
    path.symlink_to('missing-synthetic-target')
    failed = EqPresetLibrary()
    with pytest.raises(FileNotFoundError):
        failed.load(tmp_path)
    writes = []
    with pytest.raises(EqPresetLibraryUnavailableError):
        failed.persist(tmp_path, write_text=lambda *args: writes.append(args))
    assert failed.unavailable and not writes and path.is_symlink()
    assert path.readlink() == Path('missing-synthetic-target')
    assert not (tmp_path / 'missing-synthetic-target').exists()


def test_latch_requires_new_instance_even_after_storage_is_repaired(tmp_path):
    path = tmp_path / 'eq_presets.json'
    path.write_bytes(INVALID)
    library = EqPresetLibrary()
    with pytest.raises(ValueError):
        library.load(tmp_path)
    path.write_bytes(VALID)
    library.load(tmp_path)
    assert library.unavailable
    with pytest.raises(EqPresetLibraryUnavailableError):
        library.persist(tmp_path)
    replacement = EqPresetLibrary()
    replacement.load(tmp_path)
    assert not replacement.unavailable and replacement.presets == library.presets
    assert path.read_bytes() == VALID


@pytest.mark.parametrize('version', [None, 1])
def test_healthy_legacy_load_and_write_failures_never_set_unavailability(tmp_path, version):
    payload = json.loads(VALID)
    if version is None:
        del payload['version']
    path = tmp_path / 'eq_presets.json'
    path.write_text(json.dumps(payload))
    library = EqPresetLibrary()
    library.load(tmp_path)
    for kind in (OSError, AtomicWriteCommittedError):
        error = kind('synthetic write failure')
        def fail(*args):
            raise error
        with pytest.raises(kind) as raised:
            library.persist(tmp_path, write_text=fail)
        assert raised.value is error and not library.unavailable
    library.persist(tmp_path)
    assert json.loads(path.read_text()) == {'version': 1, 'presets': library.presets}


LEGACY_WRITES = [
    'EQSave:New', 'EQSourceSave:{"Name":"New","source_name":"wifi"}',
    'EQv2Delete:{"Name":"Absent"}', 'EQv2Delete:{}',
    'EQv2Rename:{"Name":"Existing","newName":"Renamed"}',
]


@pytest.mark.parametrize('unreadable', [False, True])
@pytest.mark.parametrize('legacy_command', LEGACY_WRITES)
async def test_legacy_destructive_commands_preserve_bytes_mapping_metadata_and_active_state(
        tmp_path, monkeypatch, unreadable, legacy_command):
    path = tmp_path / 'eq_presets.json'
    encoded = VALID if unreadable else INVALID
    path.write_bytes(encoded)
    with monkeypatch.context() as patch:
        if unreadable:
            reject_read(patch, path, PermissionError('synthetic unreadable storage'))
            # Failure to read does not prevent atomic replacement in this setup.
            atomic_write_text(path, encoded.decode())
        api = make_api(tmp_path)
    await api.initialize_channel_balance()
    # Also cover compatibility mapping assignment and rename/delete rollback.
    api.eq_presets = {'Existing': {DEFAULT_EQ_PLUGIN: default_eq_bands()}}
    api.eq_current_preset = 'Existing'
    api.eq_names_by_plugin[DEFAULT_EQ_PLUGIN] = 'Existing'
    before, calls = snapshot(api), deepcopy(api.mpv.calls)
    active = api._active_dsp_snapshot()
    generation = api.state.generation
    files = {file.name: file.read_bytes() for file in tmp_path.iterdir() if file.is_file()}
    writes = no_writes(monkeypatch)
    response = await http(api, legacy_command)
    assert response.status == 502 and response.text == 'Failed'
    assert api._eq_library.unavailable and not api._dsp_library_committed
    assert api.eq_presets is api._eq_library.presets and snapshot(api) == before
    assert api._active_dsp_snapshot() == active and api.mpv.calls == calls
    assert api.state.generation == generation and not writes
    assert {file.name: file.read_bytes() for file in tmp_path.iterdir() if file.is_file()} == files
    assert path.read_bytes() == encoded and not api._dsp_command_lock.locked()


async def test_fresh_unavailable_rename_retains_existing_missing_name_400(tmp_path, monkeypatch):
    path = tmp_path / 'eq_presets.json'
    path.write_bytes(INVALID)
    api = make_api(tmp_path)
    writes = no_writes(monkeypatch)
    response = await http(api, 'EQv2Rename:{"Name":"Missing","newName":"New"}')
    assert response.status == 400 and response.text == 'Failed'
    assert not writes and not api.mpv.calls and not api.eq_presets
    assert path.read_bytes() == INVALID


@pytest.mark.parametrize('unreadable', [False, True])
@pytest.mark.parametrize('action', ['dsp_preset_save', 'dsp_preset_rename', 'dsp_preset_delete'])
async def test_web_saved_writes_are_known_rejections_before_proposals_or_effects(
        tmp_path, monkeypatch, unreadable, action):
    path = tmp_path / 'eq_presets.json'
    encoded = VALID if unreadable else INVALID
    path.write_bytes(encoded)
    with monkeypatch.context() as patch:
        if unreadable:
            reject_read(patch, path, PermissionError('https://private.test/SECRET'))
        api = make_api(tmp_path)
    await api.initialize_channel_balance()
    before, calls = snapshot(api), deepcopy(api.mpv.calls)
    active, generation = api._active_dsp_snapshot(), api.state.generation
    files = {file.name: file.read_bytes() for file in tmp_path.iterdir() if file.is_file()}
    writes = no_writes(monkeypatch)
    async with serving(api) as (session, base):
        observed = await state(session, base)
        editor = observed['dsp']['editor']
        assert observed['dsp']['status'] == 'CONFIRMED' and editor['library_unavailable'] is True
        assert {p['kind'] for p in editor['presets']} == {'builtin'}
        body = {'action': action, 'expected_dsp': editor['revision']}
        if action != 'dsp_preset_delete':
            body['name'] = 'New'
        if action != 'dsp_preset_save':
            body['preset_id'] = editor['presets'][0]['id']
        code, result = await command(session, base, observed['csrf_token'], body)
        assert code == 409 and result == {'error': 'preset_library_unavailable',
            'message': PRESET_LIBRARY_UNAVAILABLE_MESSAGE, 'outcome': 'REJECTED'}
        assert (await state(session, base))['dsp']['editor'] == editor
    assert snapshot(api) == before and api._active_dsp_snapshot() == active
    assert api.mpv.calls == calls and api.state.generation == generation and not writes
    assert {file.name: file.read_bytes() for file in tmp_path.iterdir() if file.is_file()} == files
    assert path.read_bytes() == encoded and not api._dsp_command_lock.locked()


async def test_availability_changes_existing_revision_and_waiting_write_is_stale(tmp_path, monkeypatch):
    api = make_api(tmp_path)
    await api.initialize_channel_balance()
    editor = api.web_api.snapshot()['dsp']['editor']
    assert 'library_unavailable' not in editor
    before, generation, calls = snapshot(api), api.state.generation, deepcopy(api.mpv.calls)
    path = tmp_path / 'eq_presets.json'
    path.write_bytes(INVALID)
    writes = no_writes(monkeypatch)
    await api._dsp_command_lock.acquire()
    task = asyncio.create_task(api._web_dispatch(decode_command(json.dumps({
        'action': 'dsp_preset_save', 'name': 'New', 'expected_dsp': editor['revision'],
    }).encode())))
    try:
        await asyncio.sleep(0)
        assert not task.done()
        # Explicit synthetic load failure: no production polling/reload is added.
        api._load_persisted_eq_presets()
    finally:
        api._dsp_command_lock.release()
    from fake_linkplay.web_ui import WebAdmissionError
    with pytest.raises(WebAdmissionError) as raised:
        await task
    assert raised.value.code == 'stale_dsp'
    fresh = api.web_api.snapshot()['dsp']['editor']
    assert fresh['revision'] != editor['revision'] and fresh['library_unavailable']
    assert fresh['revision'] == api.web_api.snapshot()['dsp']['editor']['revision']
    assert snapshot(api) == before and api.state.generation == generation
    assert api.mpv.calls == calls and not writes and path.read_bytes() == INVALID


@pytest.mark.parametrize('source', ['', 'wifi'])
@pytest.mark.parametrize('enabled', [False, True])
async def test_independent_active_restore_and_ordinary_dsp_stay_available(tmp_path, source, enabled):
    from test_active_dsp_persistence import make_api as restored_api, save_cut
    old, old_peer = await save_cut(tmp_path, source=source, enabled=enabled)
    before = old._active_dsp_snapshot()
    path = tmp_path / 'eq_presets.json'
    path.write_bytes(INVALID)
    restored, peer = restored_api(tmp_path)
    await restored.initialize_channel_balance()
    after = restored._active_dsp_snapshot()
    assert {key: value for key, value in after.items() if key != 'name'} == {
        key: value for key, value in before.items() if key != 'name'}
    assert peer.graph == old_peer.graph and restored._eq_library.unavailable
    async with serving(restored) as (session, base):
        current = await state(session, base)
        editor = current['dsp']['editor']
        assert current['dsp']['status'] == 'CONFIRMED' and editor['library_unavailable']
        assert editor['bands'] == before['bands']
        flat = next(preset for preset in editor['presets'] if preset['name'] == 'Flat')
        assert (await command(session, base, current['csrf_token'], {
            'action': 'dsp_preset_load', 'expected_dsp': editor['revision'],
            'preset_id': flat['id'],
        }))[0] == 200
        current = await state(session, base)
        editor = current['dsp']['editor']
        assert editor['bands'] == default_eq_bands()
        bands = [{**band, 'value': -4 if band['param_name'] == 'a_gain' else band['value']}
                 for band in editor['bands']]
        assert (await command(session, base, current['csrf_token'], {
            'action': 'dsp_bands', 'expected_dsp': editor['revision'], 'bands': bands,
        }))[0] == 200
        for action, fields in [('eq', {'enabled': False}), ('balance', {'value': -.25})]:
            assert (await command(session, base, current['csrf_token'], {'action': action, **fields}))[0] == 200
        current = await state(session, base)
        assert current['dsp']['status'] == 'CONFIRMED' and current['dsp']['eq_enabled'] is False
        assert current['dsp']['balance'] == -.25 and current['dsp']['editor']['bands'] == bands
    assert path.read_bytes() == INVALID and restored._eq_library.unavailable


def test_startup_warning_never_echoes_raw_load_exception_or_private_path(tmp_path, monkeypatch, caplog):
    path = tmp_path / 'eq_presets.json'
    path.write_bytes(VALID)
    with monkeypatch.context() as patch:
        reject_read(patch, path, PermissionError('DO_NOT_EXPOSE https://private.test/token=SECRET'))
        api = make_api(tmp_path)
    assert api._eq_library.unavailable
    warnings = [record for record in caplog.records if record.name == 'fake_linkplay.http_api']
    assert len(warnings) == 1 and warnings[0].getMessage() == PRESET_LIBRARY_UNAVAILABLE_MESSAGE
    assert not warnings[0].exc_info and not warnings[0].stack_info
    assert 'SECRET' not in caplog.text and str(tmp_path) not in caplog.text
