"""Explicit refresh checks using synthetic configuration and HTTP responses only."""
import asyncio
from dataclasses import replace
import http.client
import json
from types import SimpleNamespace

import pytest

from fake_linkplay import tidal_refresh
from fake_linkplay.tidal_refresh import TidalSessionRefreshManager
from fake_linkplay.tidal_session import TidalSession, TidalSessionError, TidalSessionStore


OLD = TidalSession("synthetic-old-access", "synthetic-old-refresh", 1,
                   "12345", "ZZ", "synthetic-client", "tidalapi-device-v0.8.11")
TOKEN = {"access_token": "synthetic-new-access", "refresh_token": "synthetic-new-refresh",
         "token_type": "Bearer", "expires_in": 3600}
IDENTITY = {"userId": 12345, "countryCode": "ZZ"}


def config():
    return SimpleNamespace(client_id=OLD.client_id, client_secret="synthetic-client-secret",
                           api_oauth2_token=tidal_refresh.TOKEN_URL,
                           api_v1_location="https://api.tidal.com/v1/")


def private_store(tmp_path, record=OLD):
    store = TidalSessionStore(tmp_path / "private")
    store.save(record)
    return store


def responses(*values):
    calls = []

    async def request(url, **kwargs):
        calls.append((url, kwargs))
        value = values[len(calls) - 1]
        if isinstance(value, Exception):
            raise value
        return value

    return request, calls


def safe(result):
    encoded = json.dumps(result)
    for private in ("synthetic-", "12345", "access_token", "refresh_token", "client_id"):
        assert private not in encoded


@pytest.mark.asyncio
@pytest.mark.parametrize("rotated", [True, False])
async def test_refresh_commits_complete_rotation_and_hook_under_single_lease(tmp_path, rotated):
    store = private_store(tmp_path)
    value = dict(TOKEN)
    if not rotated:
        del value["refresh_token"]
    request, calls = responses(value, IDENTITY)
    committed = []

    def on_committed(old, new):
        assert store.load() == new
        with pytest.raises(TidalSessionError):
            store.clear()
        committed.append((old, new))

    manager = TidalSessionRefreshManager(store, config_factory=config, request_json=request,
                                         on_committed=on_committed)
    assert not calls and store.load() == OLD  # Construction is offline.
    result = await manager.refresh()
    assert result == {"state": "refreshed", "session_saved": True,
                      "refresh_outcome": "saved", "sharing_warning": False}
    safe(result)
    saved = store.load()
    assert committed == [(OLD, saved)]
    assert saved.access_token == TOKEN["access_token"]
    assert saved.refresh_token == (TOKEN["refresh_token"] if rotated else OLD.refresh_token)
    assert saved.user_id == OLD.user_id and saved.country_code == OLD.country_code
    assert saved.client_id == OLD.client_id and saved.auth_flow == OLD.auth_flow
    assert calls == [
        (tidal_refresh.TOKEN_URL, {"data": {"grant_type": "refresh_token",
          "refresh_token": OLD.refresh_token, "client_id": OLD.client_id,
          "client_secret": "synthetic-client-secret"}}),
        (tidal_refresh.SESSIONS_URL, {"token": TOKEN["access_token"]}),
    ]
    with store.lease():
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"access_token": ""}, {"access_token": "secret\r\nHeader: bad"},
    {"refresh_token": None}, {"refresh_token": ""}, {"refresh_token": 42},
    {"token_type": "Basic"}, {"expires_in": True}, {"expires_in": 0},
    {"expires_in": float("inf")}, {"expires_in": 10**1000},
])
async def test_invalid_token_response_cannot_drop_old_refresh_or_reach_identity(tmp_path, changes):
    store = private_store(tmp_path)
    request, calls = responses({**TOKEN, **changes})
    result = await TidalSessionRefreshManager(store, config_factory=config,
                                              request_json=request).refresh()
    assert result["error"] == "invalid_response" and result["refresh_outcome"] == "uncertain"
    assert len(calls) == 1 and store.load() == OLD
    safe(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("token,identity", [
    ({**TOKEN, "user": {"userId": 9}}, IDENTITY),
    ({**TOKEN, "user": {"countryCode": "US"}}, IDENTITY),
    ({**TOKEN, "client_id": "different-client"}, IDENTITY),
    (TOKEN, {**IDENTITY, "userId": 9}),
    (TOKEN, {**IDENTITY, "countryCode": "US"}),
    (TOKEN, {**IDENTITY, "clientId": "different-client"}),
    (TOKEN, {}), (TOKEN, None),
])
async def test_changed_or_unverified_identity_never_commits(tmp_path, token, identity):
    store = private_store(tmp_path)
    request, calls = responses(token, identity)
    committed = []
    result = await TidalSessionRefreshManager(
        store, config_factory=config, request_json=request,
        on_committed=lambda *args: committed.append(args),
    ).refresh()
    assert result["error"] == "identity_mismatch" and not committed
    assert store.load() == OLD and len(calls) <= 2
    safe(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("record,error", [
    (replace(OLD, client_id=None, auth_flow=None), "session_unavailable"),
    (replace(OLD, refresh_token=None), "session_unavailable"),
    (replace(OLD, user_id=None), "session_unavailable"),
    (replace(OLD, client_id="different-client"), "client_mismatch"),
])
async def test_ineligible_session_never_sends_credentials(tmp_path, record, error):
    store = private_store(tmp_path, record)
    request, calls = responses()
    result = await TidalSessionRefreshManager(store, config_factory=config,
                                              request_json=request).refresh()
    assert result["error"] == error and result["refresh_outcome"] == "not_started"
    assert not calls and store.load() == record


@pytest.mark.asyncio
async def test_concurrent_refresh_and_logout_fail_and_cancel_never_commits(tmp_path):
    store = private_store(tmp_path)
    entered = asyncio.Event()
    calls = []

    async def request(url, **kwargs):
        calls.append(url)
        entered.set()
        await asyncio.Event().wait()

    manager = TidalSessionRefreshManager(store, config_factory=config, request_json=request)
    running = asyncio.create_task(manager.refresh())
    await entered.wait()
    result = await manager.refresh()
    assert result["error"] == "storage_unavailable"
    assert result["refresh_outcome"] == "not_started"
    with pytest.raises(TidalSessionError):
        store.clear()
    with pytest.raises(TidalSessionError):
        store.save(replace(OLD, access_token="synthetic-replacement"))
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert store.load() == OLD and len(calls) == 1
    with store.lease():
        pass


@pytest.mark.asyncio
async def test_total_deadline_discards_late_identity_and_does_not_retry(tmp_path, monkeypatch):
    store = private_store(tmp_path)
    request, calls = responses(TOKEN, IDENTITY)
    # Expire the explicit commit guard without making a real timed request.
    clock = iter([100.0, 131.0])
    monkeypatch.setattr(tidal_refresh, "time", SimpleNamespace(
        time=lambda: 1000.0, monotonic=lambda: next(clock)))
    result = await TidalSessionRefreshManager(store, config_factory=config,
                                              request_json=request).refresh()
    assert result["error"] == "refresh_failed" and result["refresh_outcome"] == "uncertain"
    assert len(calls) == 2 and store.load() == OLD


@pytest.mark.asyncio
async def test_provider_failure_is_safe_and_never_retried(tmp_path, capsys, caplog):
    store = private_store(tmp_path)
    request, calls = responses(RuntimeError("synthetic-provider-access-and-refresh-secret"))
    result = await TidalSessionRefreshManager(store, config_factory=config,
                                              request_json=request).refresh()
    assert result["error"] == "refresh_failed" and result["refresh_outcome"] == "uncertain"
    assert len(calls) == 1 and store.load() == OLD
    safe(result)
    assert capsys.readouterr() == ("", "") and not caplog.records


@pytest.mark.asyncio
async def test_callback_failure_reports_saved_session_and_never_retries(tmp_path):
    store = private_store(tmp_path)
    request, calls = responses(TOKEN, IDENTITY)

    def failed_hook(*args):
        raise RuntimeError("synthetic-hook-secret")

    result = await TidalSessionRefreshManager(
        store, config_factory=config, request_json=request, on_committed=failed_hook,
    ).refresh()
    assert result == {"state": "refreshed", "session_saved": True,
                      "refresh_outcome": "saved", "sharing_warning": True}
    assert store.load().refresh_token == TOKEN["refresh_token"] and len(calls) == 2
    safe(result)


@pytest.mark.asyncio
async def test_failure_after_atomic_replace_is_reported_as_uncertain(tmp_path, monkeypatch):
    store = private_store(tmp_path)
    request, calls = responses(TOKEN, IDENTITY)
    original_save = TidalSessionStore._save_at

    def failed_fsync(directory, session):
        original_save(directory, session)
        raise TidalSessionError("synthetic-storage-secret")

    monkeypatch.setattr(TidalSessionStore, "_save_at", staticmethod(failed_fsync))
    result = await TidalSessionRefreshManager(store, config_factory=config,
                                              request_json=request).refresh()
    assert result["error"] == "commit_failed" and result["refresh_outcome"] == "uncertain"
    assert store.load().refresh_token == TOKEN["refresh_token"] and len(calls) == 2
    safe(result)


@pytest.mark.asyncio
async def test_http_wire_debug_blocks_before_any_request(tmp_path, monkeypatch):
    store = private_store(tmp_path)
    request, calls = responses()
    monkeypatch.setattr(http.client.HTTPConnection, "debuglevel", 1)
    result = await TidalSessionRefreshManager(store, config_factory=config,
                                              request_json=request).refresh()
    assert result["error"] == "unsafe_debug" and result["refresh_outcome"] == "not_started"
    assert not calls and store.load() == OLD


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status,error", [
    (b'{"safe":true}', 200, None),
    (b'{"token":"one","token":"two"}', 200, "invalid_response"),
    (b"x" * 65537, 200, "invalid_response"),
    (b"synthetic-provider-error-secret", 302, "refresh_failed"),
])
async def test_transport_bounded_private_no_redirect_json(tmp_path, monkeypatch, body, status, error):
    calls = []

    class Context:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Response(Context):
        async def iter_chunked(self, size):
            for start in range(0, len(body), size):
                yield body[start:start + size]

    response = Response()
    response.status = status
    response.content = response

    class Http(Context):
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False and kwargs["auto_decompress"] is False
            assert kwargs["timeout"].total == 15
            assert isinstance(kwargs["cookie_jar"], tidal_refresh.aiohttp.DummyCookieJar)

        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return response

    monkeypatch.setattr(tidal_refresh.aiohttp, "ClientSession", Http)
    if error:
        with pytest.raises(tidal_refresh._Failure) as caught:
            await tidal_refresh._request_json(tidal_refresh.SESSIONS_URL, token="synthetic-token")
        assert caught.value.args == (error,)
    else:
        assert await tidal_refresh._request_json(
            tidal_refresh.SESSIONS_URL, token="synthetic-token") == {"safe": True}
    assert calls == [("GET", tidal_refresh.SESSIONS_URL, {
        "data": None, "headers": {"Accept": "application/json", "Accept-Encoding": "identity",
                                  "Authorization": "Bearer synthetic-token"},
        "allow_redirects": False, "ssl": True,
    })]
