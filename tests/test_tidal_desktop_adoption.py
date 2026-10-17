"""Loopback protocol integration with synthetic credentials; no original-app claim."""
import asyncio
from dataclasses import replace
import json
import time
from xml.etree import ElementTree as ET

import pytest
from aiohttp.test_utils import TestClient, TestServer

from fake_linkplay.capabilities import configured_presets, configured_sources, service_capability
from fake_linkplay.config import Config, ExperimentalSource
from fake_linkplay.http_api import HttpApi
from fake_linkplay.soap import SERVICE_TYPES, SOAP_NS
from fake_linkplay.state import RendererState
from fake_linkplay.tidal_account import TidalAccountUnavailable, TidalDeviceAccountBridge
from fake_linkplay.tidal_codec import decode_account_field
from fake_linkplay.tidal_playback import TidalPlaybackResolver
from fake_linkplay.tidal_session import TidalSession, TidalSessionStore
from test_soap_faults import FakeBackend


@pytest.mark.parametrize('available', [False, True])
def test_experimental_source_cannot_impersonate_or_duplicate_native_tidal(available):
    cfg = Config(source_experiment_variant='all', experimental_sources=[
        ExperimentalSource('Tidal', 'Synthetic local source', url='https://media.example.test/source.mp3'),
        ExperimentalSource('synthetic-radio', 'Synthetic radio', url='https://media.example.test/radio.mp3'),
    ])
    capability = service_capability(cfg, tidal_available=available)
    services = capability['streamServices']
    assert [item['id'] for item in services] == (['Tidal', 'synthetic-radio'] if available else ['synthetic-radio'])
    assert not any(item.get('experimental') for item in services if item['id'] == 'Tidal')
    assert capability['version'] == '1.0'
    # Reservation is limited to provider discovery, not local URL sources.
    assert [item['id'] for item in configured_sources(cfg)] == ['Tidal', 'synthetic-radio']
    assert [item['id'] for item in configured_presets(cfg)] == ['Tidal', 'synthetic-radio']


async def post(client, action, arguments='', *, service='RenderingControl', path='rendercontrol1'):
    namespace = SERVICE_TYPES[service]
    body = (f'<s:Envelope xmlns:s="{SOAP_NS}"><s:Body>'
            f'<u:{action} xmlns:u="{namespace}">{arguments}</u:{action}>'
            '</s:Body></s:Envelope>')
    response = await client.post(f'/upnp/control/{path}', data=body,
                                 headers={'SOAPACTION': f'"{namespace}#{action}"'})
    text = await response.text()
    assert response.status == 200, (response.status, action)
    return ET.fromstring(text)


@pytest.fixture
async def shared_api(tmp_path):
    store = TidalSessionStore(tmp_path / 'private-tidal')
    session = TidalSession('synthetic-access', 'synthetic-refresh', time.time() + 3600,
                           '42', 'DE', 'synthetic-client', 'tidalapi-device-v0.8.11')
    store.save(session)
    bridge = TidalDeviceAccountBridge(store)
    await bridge.set_shared(True)
    cfg = Config(uuid='00000000-0000-4000-8000-000000000000', state_dir=str(tmp_path),
                 advertise_ip='127.0.0.1', wiim_home_profile=True)
    api = HttpApi(cfg, RendererState.from_config(cfg), FakeBackend(),
                  tidal_account=bridge, tidal_playback=TidalPlaybackResolver(store))
    return api, store, bridge, session


@pytest.mark.asyncio
async def test_desktop_initialization_discovers_then_decrypts_shared_account(shared_api):
    api, store, bridge, session = shared_api
    async with TestClient(TestServer(api.app())) as client:
        description = await client.get('/description.xml')
        assert description.status == 200
        discovery = ET.fromstring(await description.text())
        device_ns = {'d': 'urn:schemas-upnp-org:device-1-0'}
        assert discovery.findtext('.//d:UDN', namespaces=device_ns) == 'uuid:' + api.cfg.upnp_uuid
        controls = {node.text for node in discovery.findall('.//d:controlURL', device_ns)}
        assert {'/upnp/control/rendercontrol1', '/upnp/control/PlayQueue1'} <= controls
        device = await post(client, 'GetControlDeviceInfo', '<InstanceID>0</InstanceID>')
        status = json.loads(device.findtext('.//Status'))
        capabilities = await post(client, 'StreamServicesCapability',
                                  '<InstanceID>0</InstanceID><AppVersion>1.0</AppVersion>')
        capability = json.loads(capabilities.findtext('.//StreamCapability'))
        # The desktop filters its local service registry to these exact IDs,
        # then skips GetUserInfo and shows Login if Tidal is absent.
        supported = {item['id'] for item in capability['streamServices']}
        # Distinguish this discovery gate from a broken GetUserInfo response.
        account = await post(client, 'GetUserInfo', '<AccountSource>Tidal</AccountSource>',
                             service='PlayQueue', path='PlayQueue1')
        result = json.loads(account.findtext('.//Result'))
        assert decode_account_field(status['uuid'], result['access_token']) == session.access_token
        assert result['user']['userId'] == 42
        assert 'refresh_token' not in result
        assert 'Tidal' in supported, 'desktop refuses GetUserInfo before account adoption'
        tidal = next(item for item in capability['streamServices'] if item['id'] == 'Tidal')
        assert tidal == {'id': 'Tidal', 'version': '1.0', 'active': True,
                         'configurable': False, 'removable': False}
        # Keep the existing Android queue/index and quality branches unchanged.
        assert capability['version'] == '1.0'
        assert status['streamServicesCapability'] == capability
        for command in ('getStatusEx', 'getDeviceInfo', 'getStreamServicesCapability'):
            response = await client.get('/httpapi.asp', params={'command': command})
            assert response.status == 200
            payload = await response.json()
            assert payload.get('streamServicesCapability', payload) == capability
        for action in ('GetInfoEx', 'GetPositionInfo', 'GetMediaInfo'):
            transport = await post(client, action, '<InstanceID>0</InstanceID>',
                                   service='AVTransport', path='rendertransport1')
            assert json.loads(transport.findtext('.//StreamCapability')) == capability
        # Revocation removes discovery on the next real read; no cached flag
        # may carry the account into a later user or an unshared session.
        await bridge.set_shared(False)
        capabilities = await post(client, 'StreamServicesCapability')
        assert all(item['id'] != 'Tidal' for item in
                   json.loads(capabilities.findtext('.//StreamCapability'))['streamServices'])
        assert not api.mpv.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('unavailable', ['unshared', 'playback', 'proxy', 'other_store', 'malformed_token'])
async def test_device_sharing_alone_does_not_advertise_unusable_playback(shared_api, tmp_path, unavailable):
    api, store, bridge, session = shared_api
    if unavailable == 'unshared':
        await bridge.set_shared(False)
    elif unavailable == 'playback':
        api.tidal_playback = None
    elif unavailable == 'proxy':
        api.catalog_proxy_port = 0
    elif unavailable == 'malformed_token':
        store.save(replace(session, access_token='synthetic malformed token'))
        try:
            await bridge.set_shared(True)
        except TidalAccountUnavailable:
            pass
        assert not api.tidal_playback.session_available()
    else:
        other = TidalSessionStore(tmp_path / 'other-account')
        other.save(session)
        api.tidal_playback = TidalPlaybackResolver(other)
    async with TestClient(TestServer(api.app())) as client:
        response = await post(client, 'StreamServicesCapability')
        capability = json.loads(response.findtext('.//StreamCapability'))
        assert not any(item['id'] == 'Tidal' for item in capability['streamServices'])
        assert not api.mpv.calls


@pytest.mark.asyncio
async def test_expired_remembered_restart_stays_discoverable_until_on_demand_renewal(shared_api, monkeypatch):
    from fake_linkplay import tidal_account
    from fake_linkplay.tidal_refresh import TidalSessionRefreshManager
    from test_tidal_refresh import config, responses, TOKEN

    api, store, bridge, session = shared_api
    await bridge.set_shared(True, remember=True)
    monkeypatch.setattr(tidal_account.time, 'time', lambda: session.expires_at + 1)
    bridge = TidalDeviceAccountBridge(store)
    api.tidal_account = bridge
    assert bridge.sharing_requested and not bridge.shared
    provider, calls = responses(TOKEN, {'userId': 42, 'countryCode': 'DE'})
    entered, release = asyncio.Event(), asyncio.Event()
    async def request(url, **kwargs):
        if 'data' in kwargs:
            entered.set()
            await release.wait()
        return await provider(url, **kwargs)
    owner = TidalSessionRefreshManager(store, config_factory=config, request_json=request,
                                       on_committed=bridge._session_refreshed)
    bridge._bind_refresh_owner(owner)
    api.tidal_refresh = owner
    api.tidal_playback = TidalPlaybackResolver(store, refresh_manager=owner)
    async with TestClient(TestServer(api.app())) as client:
        capability = json.loads((await post(client, 'StreamServicesCapability')).findtext('.//StreamCapability'))
        assert any(item['id'] == 'Tidal' for item in capability['streamServices'])
        assert not calls  # Discovery may not contact the provider or rotate credentials.
        pending_account = asyncio.create_task(post(
            client, 'GetUserInfo', '<AccountSource>Tidal</AccountSource>',
            service='PlayQueue', path='PlayQueue1'))
        await entered.wait()
        try:
            capability = json.loads((await post(client, 'StreamServicesCapability')).findtext('.//StreamCapability'))
            assert any(item['id'] == 'Tidal' for item in capability['streamServices'])
        finally:
            release.set()
        account = await pending_account
        result = json.loads(account.findtext('.//Result'))
        assert decode_account_field(api.cfg.upnp_uuid, result['access_token']) == TOKEN['access_token']
        assert result['expires_in'] == 3600 and len(calls) == 2
        assert 'refresh_token' not in result and not api.mpv.calls
