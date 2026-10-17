"""Synthetic authorized-session bridge; no live service or Android claims."""
from dataclasses import replace
import importlib.util
import json
import time
from xml.etree import ElementTree as ET

import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.state import RendererState
from fake_linkplay.tidal_account import TidalAccountBridge, TidalAccountUnavailable, TidalClientIdentity
from fake_linkplay.tidal_codec import decode_account_field
from fake_linkplay.tidal_session import TidalSession, TidalSessionStore
from test_soap_faults import FakeBackend, fault_code, request

crypto = pytest.mark.skipif(importlib.util.find_spec("pyaes") is None,
                           reason="TIDAL pyaes dependency")
UUID = "0123456789abcdef-SYNTHETIC-ONLY"
CLIENT = TidalClientIdentity("synthetic-client", "https://renderer.example.test/tidal/callback")


@pytest.fixture
def account(tmp_path):
    store = TidalSessionStore(tmp_path / "private-tidal")
    store.save(TidalSession("synthetic-access", "synthetic-refresh", 2000, "42", "DE"))
    return store, TidalAccountBridge(store, CLIENT)


@crypto
@pytest.mark.asyncio
async def test_account_projection_is_app_shaped_and_does_not_export_refresh_token(account):
    store, bridge = account
    result = await bridge.get_user_info(UUID, version="2", now=1000)
    assert decode_account_field(UUID, result["access_token"]) == "synthetic-access"
    assert decode_account_field(UUID, result["clientId"]) == "synthetic-client"
    assert result["redirectURI"] == CLIENT.redirect_uri
    assert result["expires_in"] == 1000
    assert result["user"] == {"userId": 42, "username": "", "countryCode": "DE"}
    assert "refresh_token" not in result
    assert "synthetic-refresh" not in json.dumps(result)
    assert store.load().refresh_token == "synthetic-refresh"
    await bridge.clear_local_session()
    with pytest.raises(TidalAccountUnavailable):
        await bridge.get_user_info(UUID, version="2", now=1000)


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"expires_at": None}, {"expires_at": 999}, {"expires_at": 1000},
    {"user_id": None}, {"user_id": "not-a-number"}, {"user_id": "0"},
    {"user_id": str(2**63)}, {"country_code": None},
])
async def test_incomplete_or_expired_session_cannot_become_a_phantom_account(account, changes):
    store, bridge = account
    store.save(replace(store.load(), **changes))
    with pytest.raises(TidalAccountUnavailable) as error:
        await bridge.get_user_info(UUID, version="2", now=1000)
    assert "synthetic-access" not in str(error.value)
    assert "synthetic-refresh" not in str(error.value)


@pytest.mark.asyncio
async def test_account_errors_do_not_expose_broken_private_storage(account):
    store, bridge = account
    (store.directory / "session.json").write_text('{"token":"SYNTHETIC_SECRET_INVALID_JSON"')
    with pytest.raises(TidalAccountUnavailable) as error:
        await bridge.get_user_info(UUID, version="2", now=1000)
    assert "SYNTHETIC_SECRET" not in str(error.value)


@pytest.mark.asyncio
async def test_unsupported_contract_cannot_trigger_a_session_projection(account):
    _, bridge = account
    with pytest.raises(TidalAccountUnavailable):
        await bridge.get_user_info(UUID, version="1", now=1000)


@pytest.mark.parametrize("client,redirect", [
    ("", CLIENT.redirect_uri), ("bad\nclient", CLIENT.redirect_uri),
    (CLIENT.client_id, ""), (CLIENT.client_id, "javascript:alert(1)"),
    (CLIENT.client_id, "http://foreign.example.test/callback"),
    (CLIENT.client_id, "https://user:secret@renderer.example.test/callback"),
    (CLIENT.client_id, "https://renderer.example.test/callback#secret"),
])
def test_missing_client_and_invalid_redirect_do_not_fall_back_to_oem_identity(client, redirect):
    with pytest.raises(TidalAccountUnavailable):
        TidalClientIdentity(client, redirect)


@crypto
@pytest.mark.asyncio
async def test_actual_soap_opt_in_uses_status_uuid_without_enabling_service(tmp_path, account):
    store, bridge = account
    store.save(replace(store.load(), expires_at=time.time() + 600))
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", wiim_home_profile=True,
                 state_dir=str(tmp_path), advertise_ip="127.0.0.1")
    state = RendererState.from_config(cfg)
    api = HttpApi(cfg, state, FakeBackend(), tidal_account=bridge)
    response = await api.soap(request("GetUserInfo", "<AccountSource>Tidal</AccountSource>"
                                     "<Version>2</Version>", service="PlayQueue"))
    assert response.status == 200
    root = ET.fromstring(response.text)
    result = json.loads(next(n.text for n in root.iter() if n.tag == "Result"))
    status_uuid = state.as_status_ex("127.0.0.1")["uuid"]
    assert decode_account_field(status_uuid, result["access_token"]) == "synthetic-access"
    assert "Tidal" not in state.as_status_ex("127.0.0.1")["supported_services"]
    await bridge.clear_local_session()
    response = await api.soap(request("GetUserInfo", "<AccountSource>Tidal</AccountSource>"
                                     "<Version>2</Version>", service="PlayQueue"))
    assert fault_code(response) == "501"
    assert "synthetic" not in response.text


@crypto
@pytest.mark.asyncio
async def test_device_sharing_binds_exact_provenance_session_and_resets_on_restart(tmp_path):
    from fake_linkplay.tidal_account import TidalDeviceAccountBridge
    store = TidalSessionStore(tmp_path / "device-session")
    original = TidalSession("synthetic-access", "synthetic-refresh", time.time() + 600, "42", "DE")
    store.save(original)
    bridge = TidalDeviceAccountBridge(store)
    with pytest.raises(TidalAccountUnavailable):
        await bridge.set_shared(True)
    record = replace(original, client_id="synthetic-device-client", auth_flow="tidalapi-device-v0.8.11")
    store.save(record)
    await bridge.set_shared(True)
    result = await bridge.get_user_info(UUID, version="2")
    assert decode_account_field(UUID, result["clientId"]) == record.client_id
    assert "redirectURI" not in result
    with pytest.raises(TidalAccountUnavailable):
        await TidalDeviceAccountBridge(store).get_user_info(UUID, version="2")
    store.save(replace(record, access_token="synthetic-replacement"))
    assert not bridge.shared
    with pytest.raises(TidalAccountUnavailable):
        await bridge.get_user_info(UUID, version="2")
    await bridge.set_shared(False)
    assert not bridge.shared


@crypto
@pytest.mark.asyncio
@pytest.mark.parametrize('change', [{}, {'user_id': '43'}, {'client_id': 'different-client'}])
async def test_verified_refresh_preserves_only_same_account_sharing(tmp_path, change):
    from fake_linkplay.tidal_account import TidalDeviceAccountBridge
    store = TidalSessionStore(tmp_path / 'refresh-sharing')
    previous = TidalSession('synthetic-old', 'synthetic-refresh', time.time()+600, '42', 'DE',
                            client_id='synthetic-client', auth_flow='tidalapi-device-v0.8.11')
    store.save(previous)
    bridge = TidalDeviceAccountBridge(store)
    await bridge.set_shared(True)
    replacement = replace(previous, access_token='synthetic-new', refresh_token='synthetic-rotated',
                          expires_at=time.time()+1200, **change)
    with store.lease() as lease:
        lease.save(replacement)
        bridge._session_refreshed(previous, replacement)
    assert bridge.shared is (not change)
    if not change:
        result = await bridge.get_user_info(UUID, version='2')
        assert decode_account_field(UUID, result['access_token']) == 'synthetic-new'
        assert 'synthetic-rotated' not in json.dumps(result)
    await bridge.set_shared(False)
    bridge._session_refreshed(replacement, replacement)
    assert not bridge.shared


@pytest.mark.asyncio
@pytest.mark.parametrize('revoke', [False, True])
async def test_expiry_suspends_sharing_until_verified_same_owner_refresh(tmp_path, monkeypatch, revoke):
    from fake_linkplay import tidal_account
    store = TidalSessionStore(tmp_path / 'expired-sharing')
    previous = TidalSession('synthetic-old', 'synthetic-refresh', 2000, '42', 'DE',
                            client_id='synthetic-client', auth_flow='tidalapi-device-v0.8.11')
    store.save(previous)
    bridge = tidal_account.TidalDeviceAccountBridge(store)
    monkeypatch.setattr(tidal_account.time, 'time', lambda: 1000)
    await bridge.set_shared(True)
    monkeypatch.setattr(tidal_account.time, 'time', lambda: 2001)
    assert not bridge.shared
    assert bridge.sharing_requested
    if revoke:
        await bridge.set_shared(False)
        assert not bridge.sharing_requested
    replacement = replace(previous, access_token='synthetic-new', expires_at=3000)
    with store.lease() as lease:
        lease.save(replacement)
        bridge._session_refreshed(previous, replacement)
    assert bridge.shared is (not revoke)


@crypto
@pytest.mark.asyncio
async def test_remembered_sharing_survives_restart_and_unshare_is_durable(tmp_path):
    from fake_linkplay.tidal_account import TidalDeviceAccountBridge
    store = TidalSessionStore(tmp_path / "remembered-sharing")
    record = TidalSession("synthetic-access", "synthetic-refresh", time.time()+600, "42", "DE",
                          "synthetic-client", "tidalapi-device-v0.8.11")
    store.save(record)
    bridge = TidalDeviceAccountBridge(store)
    assert not bridge.shared and not bridge.sharing_remembered
    assert not (store.directory / "account-sharing").exists()
    await bridge.set_shared(True, remember=True)
    marker = (store.directory / "account-sharing").read_bytes()
    assert (store.directory / "account-sharing").stat().st_mode & 0o777 == 0o600
    assert all(value.encode() not in marker for value in (record.access_token, record.refresh_token, record.client_id))
    restarted = TidalDeviceAccountBridge(store)
    assert restarted.shared and restarted.sharing_remembered
    result = await restarted.get_user_info(UUID, version="2")
    assert decode_account_field(UUID, result["access_token"]) == record.access_token
    await restarted.set_shared(False)
    assert not bridge.shared  # Another live owner must observe durable revocation too.
    assert not (store.directory / "account-sharing").exists()
    assert not TidalDeviceAccountBridge(store).sharing_requested
    assert store.load() == record


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"access_token": "synthetic-replaced"}, {"user_id": "43"}, {"country_code": "US"},
    {"client_id": "synthetic-other-client"}, {"client_id": None, "auth_flow": None},
])
async def test_remembered_sharing_never_applies_to_a_replaced_record(tmp_path, change):
    from fake_linkplay.tidal_account import TidalDeviceAccountBridge
    store = TidalSessionStore(tmp_path / "replacement-sharing")
    record = TidalSession("synthetic-access", "synthetic-refresh", time.time()+600, "42", "DE",
                          "synthetic-client", "tidalapi-device-v0.8.11")
    store.save(record)
    bridge = TidalDeviceAccountBridge(store)
    await bridge.set_shared(True, remember=True)
    store.save(replace(record, **change))
    assert not bridge.shared and not bridge.sharing_remembered
    assert not TidalDeviceAccountBridge(store).sharing_requested


@pytest.mark.asyncio
async def test_forget_clears_remembered_consent_even_if_session_is_already_missing(tmp_path):
    from fake_linkplay.tidal_account import TidalDeviceAccountBridge
    store = TidalSessionStore(tmp_path / "forgotten-sharing")
    record = TidalSession("synthetic-access", "synthetic-refresh", time.time()+600, "42", "DE",
                          "synthetic-client", "tidalapi-device-v0.8.11")
    store.save(record)
    bridge = TidalDeviceAccountBridge(store)
    await bridge.set_shared(True, remember=True)
    (store.directory / "session.json").unlink()
    await bridge.clear_local_session()
    assert not (store.directory / "account-sharing").exists()
    store.save(record)  # Even restoring identical bytes must not restore forgotten consent.
    assert not bridge.sharing_requested and not TidalDeviceAccountBridge(store).sharing_requested


@pytest.mark.asyncio
async def test_failed_durable_unshare_revokes_current_export_but_reports_saved_consent(tmp_path, monkeypatch):
    from fake_linkplay.tidal_account import TidalDeviceAccountBridge
    from fake_linkplay.tidal_session import TidalSessionError
    store = TidalSessionStore(tmp_path / "failed-unshare")
    store.save(TidalSession("synthetic-access", "synthetic-refresh", time.time()+600, "42", "DE",
                            "synthetic-client", "tidalapi-device-v0.8.11"))
    bridge = TidalDeviceAccountBridge(store)
    await bridge.set_shared(True, remember=True)
    def unavailable(*args):
        raise TidalSessionError("Synthetic storage failure")
    monkeypatch.setattr(store, "remember_sharing", unavailable)
    with pytest.raises(TidalAccountUnavailable):
        await bridge.set_shared(False)
    assert not bridge.shared and bridge.sharing_remembered
    assert TidalDeviceAccountBridge(store).sharing_remembered  # Revocation was not durable.
