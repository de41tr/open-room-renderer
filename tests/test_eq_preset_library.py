"""UNIT ownership boundaries for a saved library, distinct from active DSP."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from fake_linkplay.eq_presets import DEFAULT_EQ_PLUGIN, EqPresetLibrary, merge_eq_bands
from fake_linkplay.persistence import AtomicWriteCommittedError


def cut(gain=-3):
    return merge_eq_bands(None, [{'param_name': 'a_gain', 'value': gain}])


def test_standalone_library_roundtrip_detaches_curves_and_never_writes_active_state(tmp_path):
    library = EqPresetLibrary()
    bands = cut()
    expected = deepcopy(bands)
    library.save('Saved', DEFAULT_EQ_PLUGIN, bands)
    bands[1]['value'] = -12
    assert library.presets['Saved'][DEFAULT_EQ_PLUGIN] == expected
    selected = library.bands('Saved', DEFAULT_EQ_PLUGIN)
    selected[1]['value'] = -6
    assert library.presets['Saved'][DEFAULT_EQ_PLUGIN] == expected

    active_path = tmp_path / 'active_dsp.json'
    active_path.write_bytes(b'Synthetic active state owned by the caller\n')
    active_before = active_path.read_bytes()
    library.persist(tmp_path)
    encoded = (tmp_path / 'eq_presets.json').read_text()
    assert encoded == json.dumps({'version': 1, 'presets': library.presets},
                                 indent=2, sort_keys=True, allow_nan=False) + '\n'
    restored = EqPresetLibrary()
    restored.load(tmp_path)
    assert restored.presets == library.presets
    assert restored.presets is not library.presets
    assert restored.names(DEFAULT_EQ_PLUGIN) == ['Saved']
    restored.rename('Saved', 'Renamed')
    assert restored.display_name(DEFAULT_EQ_PLUGIN, expected, 'Renamed') == 'Renamed'
    restored.delete('Renamed')
    assert restored.display_name(DEFAULT_EQ_PLUGIN, expected, 'Renamed') == 'Custom'
    assert library.names(DEFAULT_EQ_PLUGIN) == ['Saved']
    assert active_path.read_bytes() == active_before
    assert {path.name for path in tmp_path.iterdir()} == {'active_dsp.json', 'eq_presets.json'}


def test_library_load_publishes_only_a_completely_valid_mapping(tmp_path):
    library = EqPresetLibrary()
    library.save('Existing', DEFAULT_EQ_PLUGIN, cut())
    previous = library.presets
    before = deepcopy(previous)
    path = tmp_path / 'eq_presets.json'
    path.write_text(json.dumps({'presets': {
        'Valid first': {DEFAULT_EQ_PLUGIN: [{'param_name': 'a_gain', 'value': -6}]},
        'Invalid later': {DEFAULT_EQ_PLUGIN: [{'param_name': 'a_gain', 'value': 25}]},
    }}))
    encoded = path.read_bytes()
    with pytest.raises(ValueError, match='outside supported range'):
        library.load(tmp_path)
    assert library.presets is previous
    assert library.presets == before
    assert path.read_bytes() == encoded


@pytest.mark.parametrize('error_type', [OSError, AtomicWriteCommittedError])
def test_library_writer_propagates_exact_commit_boundary_without_rollback(tmp_path, error_type):
    library = EqPresetLibrary()
    library.save('Saved', DEFAULT_EQ_PLUGIN, cut())
    before = deepcopy(library.presets)
    error = error_type('Synthetic storage failure')
    writes = []

    def write_text(path, encoded):
        writes.append((path, encoded))
        raise error

    with pytest.raises(error_type) as raised:
        library.persist(tmp_path, write_text=write_text)
    assert raised.value is error
    assert library.presets == before
    assert len(writes) == 1
    assert writes[0][0] == tmp_path / 'eq_presets.json'
    assert json.loads(writes[0][1]) == {'version': 1, 'presets': before}
    assert not list(tmp_path.iterdir())


async def test_api_compatibility_mapping_is_library_authority_even_after_rollback(tmp_path, monkeypatch):
    from test_dsp_api import band_command, execute, make_api

    api = make_api(tmp_path)
    library = api._eq_library
    replacement = {'Saved': {DEFAULT_EQ_PLUGIN: cut()}}
    api.eq_presets = replacement
    assert api.eq_presets is library.presets is replacement
    library.save('Other', DEFAULT_EQ_PLUGIN, cut(-6))
    assert set(api.eq_presets) == {'Saved', 'Other'}
    await execute(api, band_command(a_gain=-3))
    await execute(api, 'EQSave:Saved')
    before = deepcopy(library.presets)
    library_path = tmp_path / 'eq_presets.json'
    active_path = tmp_path / 'active_dsp.json'
    before_library, before_active = library_path.read_bytes(), active_path.read_bytes()
    calls = list(api.mpv.calls)

    def fail_storage(*args):
        raise OSError('Synthetic precommit failure')

    monkeypatch.setattr('fake_linkplay.http_api.atomic_write_text', fail_storage)
    with pytest.raises(OSError, match='precommit failure'):
        await execute(api, 'EQv2Rename:{"Name":"Saved","newName":"Lost"}')
    assert api._eq_library is library
    assert api.eq_presets is library.presets
    assert library.presets == before
    assert api.eq_current_preset == 'Saved'
    assert api.eq_names_by_plugin[DEFAULT_EQ_PLUGIN] == 'Saved'
    assert api.mpv.calls == calls
    assert library_path.read_bytes() == before_library
    assert active_path.read_bytes() == before_active


async def test_api_records_library_commit_before_active_metadata_write(tmp_path, monkeypatch):
    from test_dsp_api import band_command, execute, make_api

    api = make_api(tmp_path)
    await execute(api, band_command(a_gain=-3))
    await execute(api, 'EQSave:Saved')
    library = api._eq_library
    active_path = tmp_path / 'active_dsp.json'
    previous_active = active_path.read_bytes()
    calls = list(api.mpv.calls)
    from fake_linkplay.http_api import atomic_write_text
    writes = []

    def write_text(path, encoded):
        writes.append((path.name, api._dsp_library_committed))
        if path.name == 'active_dsp.json':
            assert api.eq_presets is library.presets
            assert set(library.presets) == {'Renamed'}
            raise OSError('Synthetic active metadata failure')
        atomic_write_text(path, encoded)

    monkeypatch.setattr('fake_linkplay.http_api.atomic_write_text', write_text)
    with pytest.raises(OSError, match='active metadata failure'):
        await execute(api, 'EQv2Rename:{"Name":"Saved","newName":"Renamed"}')
    assert writes == [('eq_presets.json', False), ('active_dsp.json', True)]
    assert api.eq_presets is library.presets
    assert set(library.presets) == {'Renamed'}
    assert json.loads((tmp_path / 'eq_presets.json').read_text())['presets'] == library.presets
    assert active_path.read_bytes() == previous_active
    assert api.mpv.calls == calls
