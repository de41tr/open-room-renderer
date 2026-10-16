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

crypto = pytest.mark.skipif(importlib.util.find_spec("cryptography") is None,
                           reason="optional TIDAL cryptography dependency")
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
