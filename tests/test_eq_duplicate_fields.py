"""UNIT/MOCK: ambiguous EQ commands/libraries never choose last-wins gain."""
import json
import pytest
from tests.test_dsp_api import make_api, http, snapshot, execute, band_command
from fake_linkplay.http_api import DEFAULT_EQ_PLUGIN

@pytest.mark.parametrize('payload', [
    '{"EQBand":[{"param_name":"a_gain","value":-12,"value":12}]}',
    '{"EQBand":[{"param_name":"a_gain","param_name":"b_gain","value":-3}]}',
    '{"EQBand":[{"param_name":"a_gain","value":-12}],"EQBand":[{"param_name":"a_gain","value":12}]}',
    '{"pluginURI":"old","pluginURI":"new","EQBand":[{"param_name":"a_gain","value":-3}]}',
    '{"EQBand":[{"param_name":"a_gain","value":-12,"value":-12}]}',
])
async def test_duplicate_command_fields_reject_before_mutation(tmp_path, payload):
    api = make_api(tmp_path)
    before = snapshot(api)
    disk = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    response = await http(api, 'EQSetLV2Band:' + payload)
    assert response.status == 400
    assert snapshot(api) == before
    assert api.mpv.calls == []
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == disk

@pytest.mark.parametrize('level', ['version', 'presets', 'preset', 'plugin', 'value', 'param_name'])
async def test_duplicate_library_fields_reject_whole_library(tmp_path, level):
    bands='[{"param_name":"a_gain","value":-12}]'
    plugin=json.dumps(DEFAULT_EQ_PLUGIN)
    presets='{"Saved":{' + plugin + ':' + bands + '}}'
    document='{"version":1,"presets":' + presets + '}'
    if level == 'version': document=document.replace('"version":1', '"version":2,"version":1')
    elif level == 'presets': document=document.replace('"presets":', '"presets":{},"presets":')
    elif level == 'preset': document=document.replace('"Saved":', '"Saved":{},"Saved":')
    elif level == 'plugin': document=document.replace(plugin+':', plugin+':[],'+plugin+':')
    elif level == 'value': document=document.replace('"value":-12', '"value":-12,"value":12')
    else: document=document.replace('"param_name":"a_gain"', '"param_name":"b_gain","param_name":"a_gain"')
    path=tmp_path/'eq_presets.json';path.write_text(document)
    api=make_api(tmp_path)
    assert api.eq_presets == {}
    assert (await http(api, 'EQv2Load:{"Name":"Saved"}')).status == 400
    assert not api.mpv.calls
    assert path.read_text() == document

async def test_unambiguous_command_library_and_reload_still_work(tmp_path):
    api=make_api(tmp_path)
    await execute(api, band_command(a_gain=-12))
    await execute(api, 'EQSave:Saved')
    fresh = tmp_path / "library-only"
    fresh.mkdir()
    (fresh / "eq_presets.json").write_bytes((tmp_path / "eq_presets.json").read_bytes())
    restored=make_api(fresh)
    await execute(restored, 'EQv2Load:{"Name":"Saved"}')
    assert restored.mpv.bands[0]['gain'] == -12
