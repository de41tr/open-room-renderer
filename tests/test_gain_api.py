"""UNIT/MOCK HTTP/SOAP gain persistence after backend confirmation."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import make_mocked_request
from fake_linkplay.config import Config, load_config
from fake_linkplay.http_api import HttpApi, ParsedCommand
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.persistence import load_gain
from fake_linkplay.state import RendererState
from test_soap_faults import FakeBackend, request, fault_code


@pytest.fixture
def api(tmp_path):
    cfg = Config(uuid='00000000-0000-4000-8000-000000000000', state_dir=str(tmp_path), advertise_ip='127.0.0.1')
    return HttpApi(cfg, RendererState.from_config(cfg), FakeBackend())


@pytest.mark.asyncio
async def test_http_and_soap_share_confirmed_persistent_gain(api):
    assert await api.execute(ParsedCommand('volume', ('25',)), '127.0.0.1') == 'OK'
    assert load_gain(api.cfg.state_dir) == (25, False)
    response = await api.soap(request('SetMute', '<Channel>Master</Channel><DesiredMute>1</DesiredMute>', 'RenderingControl'))
    assert response.status == 200
    assert load_gain(api.cfg.state_dir) == (25, True)
    assert await api.execute(ParsedCommand('volume', ('50',)), '127.0.0.1') == 'OK'
    assert api.state.mute is True
    assert load_gain(api.cfg.state_dir) == (50, True)
    response = await api.soap(request('SetVolume', '<Channel>Master</Channel><DesiredVolume>12</DesiredVolume>', 'RenderingControl'))
    assert response.status == 200
    assert load_gain(api.cfg.state_dir) == (12, True)


@pytest.mark.asyncio
@pytest.mark.parametrize('command,arg', [('volume','101'), ('volume','-1'), ('volume','nan'), ('volume',''), ('mute','2'), ('mute','false'), ('mute','')])
async def test_http_invalid_gain_never_changes_backend_or_persistence(api, command, arg):
    response = await api.handle_httpapi(make_mocked_request('GET', f'/httpapi.asp?command=setPlayerCmd:{"vol" if command == "volume" else "mute"}:{arg}'))
    assert response.status == 400
    assert not api.mpv.calls
    assert load_gain(api.cfg.state_dir) is None
    assert (api.state.volume, api.state.mute) == (50, False)


@pytest.mark.asyncio
async def test_backend_failure_does_not_persist_proposed_gain(api):
    await api.execute(ParsedCommand('volume', ('25',)), '127.0.0.1')
    api.mpv.volume = AsyncMock(side_effect=BackendError('Synthetic rejection'))
    with pytest.raises(BackendError):
        await api.execute(ParsedCommand('volume', ('80',)), '127.0.0.1')
    assert api.state.volume == 25
    assert load_gain(api.cfg.state_dir) == (25, False)


@pytest.mark.asyncio
async def test_persistence_failure_is_not_success_but_keeps_confirmed_readback(api, monkeypatch):
    await api.execute(ParsedCommand('volume', ('25',)), '127.0.0.1')
    def fail(*args):
        raise OSError('Synthetic disk failure')
    monkeypatch.setattr('fake_linkplay.http_api.save_gain', fail)
    response = await api.soap(request('SetVolume', '<Channel>Master</Channel><DesiredVolume>12</DesiredVolume>', 'RenderingControl'))
    assert fault_code(response) == '501'
    assert api.state.volume == 12
    assert load_gain(api.cfg.state_dir) == (25, False)


@pytest.mark.asyncio
async def test_concurrent_gain_updates_persist_in_confirmed_order(api):
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow_volume(value):
        entered.set()
        await release.wait()
    api.mpv.volume = slow_volume
    first = asyncio.create_task(api.execute(ParsedCommand('volume', ('25',)), '127.0.0.1'))
    await entered.wait()
    second = asyncio.create_task(api.execute(ParsedCommand('mute', ('1',)), '127.0.0.1'))
    await asyncio.sleep(0)
    assert not second.done()
    release.set()
    await asyncio.gather(first, second)
    assert load_gain(api.cfg.state_dir) == (25, True)


@pytest.mark.asyncio
async def test_unknown_gain_faults_reads_until_pair_is_confirmed(api):
    original = api.mpv.volume
    api.mpv.volume = AsyncMock(side_effect=BackendError('Synthetic lost confirmation'))
    with pytest.raises(BackendError):
        await api.execute(ParsedCommand('volume', ('25',)), '127.0.0.1')
    assert api.state.phase.value == 'ERROR'
    assert api.state.gain_confirmed is False
    assert api.state.as_player_status()['gain_confirmed'] is False
    assert 'actual output unknown' in api.state.last_error
    for action in ('GetVolume', 'GetMute'):
        response = await api.soap(request(action, '<Channel>Master</Channel>', 'RenderingControl'))
        assert fault_code(response) == '501'
    api.mpv.volume = original
    await api.execute(ParsedCommand('volume', ('25',)), '127.0.0.1')
    assert api.state.gain_confirmed is True
    assert 'actual output unknown' not in api.state.last_error
    assert (await api.soap(request('GetVolume', '<Channel>Master</Channel>', 'RenderingControl'))).status == 200


@pytest.mark.asyncio
async def test_cancelled_gain_confirmation_marks_output_unknown(api):
    entered = asyncio.Event()
    async def uncertain(value):
        entered.set()
        await asyncio.Future()
    api.mpv.mute = uncertain
    task = asyncio.create_task(api.execute(ParsedCommand('mute', ('1',)), '127.0.0.1'))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert api.state.gain_confirmed is False
    assert api.state.phase.value == 'ERROR'
    assert load_gain(api.cfg.state_dir) is None


@pytest.mark.asyncio
async def test_storage_failure_does_not_invalidate_confirmed_gain(api, monkeypatch):
    def fail(*args):
        raise OSError('Synthetic storage failure')
    monkeypatch.setattr('fake_linkplay.http_api.save_gain', fail)
    with pytest.raises(OSError):
        await api.execute(ParsedCommand('volume', ('25',)), '127.0.0.1')
    assert api.state.gain_confirmed is True
    assert api.state.phase.value == 'STOPPED'
    assert (await api.soap(request('GetVolume', '<Channel>Master</Channel>', 'RenderingControl'))).status == 200


@pytest.mark.asyncio
async def test_failed_resume_does_not_report_known_gain(api):
    api.state.mark_playing('https://media.example.test/one.wav')
    api.state.mark_pause()
    api.state.backend_url = api.state.backend_original_url = api.state.current_url
    api.mpv.resume = AsyncMock(side_effect=BackendError('Synthetic gain failure before resume'))
    with pytest.raises(BackendError):
        await api._transport_command('resume')
    assert api.state.gain_confirmed is False
    assert fault_code(await api.soap(request('GetMute', '<Channel>Master</Channel>', 'RenderingControl'))) == '501'


@pytest.mark.asyncio
async def test_cancelled_load_does_not_report_known_gain(api):
    entered = asyncio.Event()
    async def uncertain(*args, **kwargs):
        entered.set()
        await asyncio.Future()
    api.mpv.loadfile = uncertain
    task = asyncio.create_task(api._play_url('https://media.example.test/one.wav'))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert api.state.gain_confirmed is False
    assert fault_code(await api.soap(request('GetVolume', '<Channel>Master</Channel>', 'RenderingControl'))) == '501'


@pytest.mark.asyncio
async def test_unknown_gain_is_not_reasserted_in_gena_notifications(api):
    from xml.etree import ElementTree as ET
    api.state.gain_confirmed = False
    properties = ET.fromstring(api._event_body('RenderingControl'))
    fields = {node.tag.rsplit('}', 1)[-1]: node.text for prop in properties for node in prop}
    assert 'Volume' not in fields and 'Mute' not in fields
    inner = ET.fromstring(fields['LastChange'])
    assert not any(node.tag.rsplit('}', 1)[-1] in {'Volume', 'Mute'} for node in inner.iter())
    await api.execute(ParsedCommand('volume', ('25',)), '127.0.0.1')
    properties = ET.fromstring(api._event_body('RenderingControl'))
    fields = {node.tag.rsplit('}', 1)[-1]: node.text for prop in properties for node in prop}
    assert fields['Volume'] == '25' and fields['Mute'] == '0'


@pytest.mark.asyncio
async def test_overlapping_volume_success_cannot_hide_resume_confirmation_failure(api):
    volume_started, resume_started = asyncio.Event(), asyncio.Event()
    release_volume, release_resume = asyncio.Event(), asyncio.Event()
    async def volume(value):
        volume_started.set()
        await release_volume.wait()
    async def resume():
        resume_started.set()
        await release_resume.wait()
        raise BackendError('Synthetic failed resume pair')
    api.mpv.volume, api.mpv.resume = volume, resume
    api.state.mark_playing('https://media.example.test/one.wav')
    api.state.mark_pause()
    api.state.backend_url = api.state.backend_original_url = api.state.current_url
    setting = asyncio.create_task(api.execute(ParsedCommand('volume', ('20',)), '127.0.0.1'))
    await volume_started.wait()
    playing = asyncio.create_task(api._transport_command('resume'))
    await resume_started.wait()
    release_volume.set()
    assert await setting == 'OK'
    assert api.state.gain_confirmed is False
    assert fault_code(await api.soap(request('GetMute', '<Channel>Master</Channel>', 'RenderingControl'))) == '501'
    release_resume.set()
    with pytest.raises(BackendError):
        await playing
    assert api.state.gain_confirmed is False
    assert api._gain_operations == 0


def test_uninitialized_real_backend_does_not_claim_known_gain(tmp_path):
    from fake_linkplay.mpv_backend import MpvBackend
    cfg = Config(uuid='00000000-0000-4000-8000-000000000000', state_dir=str(tmp_path))
    backend = MpvBackend('/synthetic-not-used.sock', auto_spawn=False)
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)
    assert backend.gain_confirmed is False
    assert api.state.gain_confirmed is False
    assert api.state.as_player_status()['gain_confirmed'] is False
