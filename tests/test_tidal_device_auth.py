"""Offline library-login lifecycle checks; no real library or credentials."""
import asyncio
from concurrent.futures import Future
from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
import threading
from types import SimpleNamespace

import pytest

from fake_linkplay.tidal_device_auth import TidalDeviceLoginManager
from fake_linkplay import tidal_device_auth
from fake_linkplay.tidal_session import TidalSession, TidalSessionError, TidalSessionStore


class FakeSession:
    def __init__(self, *, uri="link.tidal.com/ABCDE", checked=True):
        self.polling = Future()
        self.polling.set_running_or_notify_cancel()
        self.login = SimpleNamespace(
            verification_uri_complete=uri, user_code="ABCDE", expires_in=300.0,
            interval=5.0, device_code="synthetic-internal-device-secret",
        )
        self.access_token = "synthetic-access-secret"
        self.refresh_token = "synthetic-refresh-secret"
        self.expiry_time = datetime(2030, 1, 2, 3, 4, 5)
        self.country_code = "ZZ"
        self.config = SimpleNamespace(client_id="synthetic-device-client")
        self.user = SimpleNamespace(id=12345)
        self.request_session = SimpleNamespace(close=self.close)
        self.checked = checked
        self.closed = False
        self.calls = []

    def login_oauth(self):
        self.calls.append(("login", threading.get_ident()))
        return self.login, self.polling

    def check_login(self):
        self.calls.append(("check", threading.get_ident()))
        return self.checked

    def close(self):
        self.closed = True


async def settled(manager):
    async with asyncio.timeout(3):
        while (result := await manager.status())["active"]:
            await asyncio.sleep(0.005)
    return result


def assert_no_secrets(value):
    serialized = json.dumps(value)
    for secret in ("synthetic-access-secret", "synthetic-refresh-secret",
                   "synthetic-internal-device-secret", "12345", "device_code", "access_token"):
        assert secret not in serialized


@pytest.mark.asyncio
async def test_safe_challenge_checked_storage_and_utc_expiry(tmp_path):
    store = TidalSessionStore(tmp_path / "private")
    session = FakeSession()
    manager = TidalDeviceLoginManager(store, session_factory=lambda: session)
    assert (await manager.status())["state"] == "idle"
    assert not store.directory.exists() and not session.calls
    challenge = await manager.start()
    assert challenge["verification_uri"] == "https://link.tidal.com/ABCDE"
    assert challenge["state"] == "pending"
    assert challenge["user_code"] == "ABCDE"
    assert challenge["expires_in"] == 300
    assert_no_secrets(challenge)
    assert (await manager.start())["state"] == "pending"
    assert [name for name, _ in session.calls] == ["login"]
    challenge["user_code"] = "modified copy"
    assert (await manager.status())["user_code"] == "ABCDE"
    session.polling.set_result(True)
    result = await settled(manager)
    assert result == {"state": "connected", "active": False, "session_saved": True,
                      "session_status": "stored"}
    assert_no_secrets(result)
    assert all(thread != threading.get_ident() for _, thread in session.calls)
    saved = store.load()
    assert saved.access_token == session.access_token
    assert saved.refresh_token == session.refresh_token
    assert saved.user_id == "12345" and saved.country_code == "ZZ"
    assert saved.expires_at == datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc).timestamp()
    assert session.closed
    assert (await manager.close())["state"] == "closed"
    assert (await manager.close())["session_saved"] is True
    assert (await manager.start())["state"] == "closed"


@pytest.mark.asyncio
async def test_existing_session_rejected_before_library_or_network(tmp_path):
    store = TidalSessionStore(tmp_path / "private")
    original = TidalSession("synthetic-original")
    store.save(original)
    called = []
    manager = TidalDeviceLoginManager(store, session_factory=lambda: called.append(True))
    result = await manager.start()
    assert result["error"] == "session_exists" and not called
    await settled(manager)
    assert store.load() == original
    await manager.close()


@pytest.mark.asyncio
async def test_status_after_restart_tracks_only_local_session_without_library_or_writes(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(tidal_device_auth, "time", SimpleNamespace(
        time=lambda: now[0], monotonic=lambda: 100.0,
    ))
    monkeypatch.setattr(tidal_device_auth, "_library_session", lambda: pytest.fail("status initialized tidalapi"))
    store = TidalSessionStore(tmp_path / "private")
    manager = TidalDeviceLoginManager(store)
    assert await manager.status() == {"state": "idle", "active": False,
                                      "session_saved": False, "session_status": "missing"}
    assert not store.directory.exists()
    saved = TidalSession("synthetic-access-secret", "synthetic-refresh-secret", 2000.0,
                         "12345", "ZZ", client_id="synthetic-device-client",
                         auth_flow="tidalapi-device-v0.8.11")
    store.save(saved)
    before = (store.directory / "session.json").read_bytes()
    for moment, expected in ((1000.0, "stored"), (2000.0, "expired"), (2001.0, "expired")):
        now[0] = moment
        # A fresh process has no remembered login attempt or sharing consent.
        result = await TidalDeviceLoginManager(store).status()
        assert result == {"state": "idle", "active": False,
                          "session_saved": False, "session_status": expected}
        assert_no_secrets(result)
        assert "synthetic-device-client" not in json.dumps(result)
        assert str(store.directory) not in json.dumps(result)
        assert (store.directory / "session.json").read_bytes() == before
    store.save(replace(saved, expires_at=3000.0))
    assert (await manager.status())["session_status"] == "stored"
    store.clear()
    assert (await manager.status())["session_status"] == "missing"


@pytest.mark.asyncio
@pytest.mark.parametrize("change,expected", [
    ({"client_id": None, "auth_flow": None}, "unsupported"),
    ({"refresh_token": None}, "unsupported"),
    ({"expires_at": None}, "unsupported"),
    ({"user_id": "01"}, "unsupported"),
    ({"country_code": None}, "unsupported"),
    ({"access_token": "synthetic token with spaces"}, "unsupported"),
    ("corrupt", "unavailable"),
    ("permissions", "unavailable"),
])
async def test_status_rejects_incomplete_legacy_or_unreadable_records_safely(tmp_path, change, expected):
    store = TidalSessionStore(tmp_path / "private")
    saved = TidalSession("synthetic-access-secret", "synthetic-refresh-secret", 2000000000.0,
                         "12345", "ZZ", client_id="synthetic-device-client",
                         auth_flow="tidalapi-device-v0.8.11")
    store.save(replace(saved, **change) if isinstance(change, dict) else saved)
    path = store.directory / "session.json"
    if change == "corrupt":
        path.write_text('{"synthetic-access-secret": "broken"}')
    elif change == "permissions":
        path.chmod(0o644)
    manager = TidalDeviceLoginManager(store, session_factory=lambda: pytest.fail("status started login"))
    assert await manager.status() == {"state": "idle", "active": False,
                                      "session_saved": False, "session_status": expected}


@pytest.mark.asyncio
async def test_cancel_retains_lease_until_worker_finishes_and_discards_success(tmp_path):
    store = TidalSessionStore(tmp_path / "private")
    session = FakeSession()
    manager = TidalDeviceLoginManager(store, session_factory=lambda: session)
    await manager.start()
    cancelled = await manager.cancel()
    assert cancelled == {"state": "cancelling", "active": True, "session_saved": False}
    assert (await manager.start())["state"] == "cancelling"
    with pytest.raises(TidalSessionError):
        store.save(TidalSession("synthetic-other"))
    session.polling.set_result(True)
    assert (await settled(manager))["state"] == "cancelled"
    assert [name for name, _ in session.calls] == ["login"]
    assert store.load() is None and session.closed
    with store.lease():
        pass
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("uri", [
    "http://link.tidal.com/ABCDE", "https://link.tidal.com.evil.test/ABCDE",
    "https://login.tidal.com/ABCDE", "https://link.tidal.com/ABCDE?access_token=secret",
    "https://link.tidal.com/ABCDE#secret", "https://name@link.tidal.com/ABCDE",
    "https://link.tidal.com:443/ABCDE", "https://link.tidal.com/OTHER",
])
async def test_rejects_unsafe_uri_before_exposing_challenge_but_drains_worker(tmp_path, uri):
    store = TidalSessionStore(tmp_path / "private")
    session = FakeSession(uri=uri)
    manager = TidalDeviceLoginManager(store, session_factory=lambda: session)
    result = await manager.start()
    assert result["state"] == "error" and result["error"] == "invalid_challenge"
    assert "verification_uri" not in result and "user_code" not in result
    assert_no_secrets(result)
    with pytest.raises(TidalSessionError):
        store.save(TidalSession("synthetic-other"))
    session.polling.set_result(True)
    await settled(manager)
    assert store.load() is None
    await manager.close()


@pytest.mark.asyncio
async def test_close_while_initial_request_runs_never_releases_ownership_early(tmp_path):
    store = TidalSessionStore(tmp_path / "private")
    entered, release = threading.Event(), threading.Event()
    session = FakeSession()

    def factory():
        entered.set()
        release.wait(3)
        return session

    manager = TidalDeviceLoginManager(store, session_factory=factory)
    starting = asyncio.create_task(manager.start())
    await asyncio.to_thread(entered.wait, 2)
    try:
        assert (await manager.close())["active"] is True
        assert (await starting)["state"] == "cancelling"
        with pytest.raises(TidalSessionError):
            store.save(TidalSession("synthetic-other"))
    finally:
        release.set()
    assert (await settled(manager))["state"] == "closed"
    assert store.load() is None and session.calls == []


@pytest.mark.asyncio
async def test_cancelled_start_request_cannot_adopt_later(tmp_path):
    entered, release = threading.Event(), threading.Event()
    store = TidalSessionStore(tmp_path / "private")
    session = FakeSession()

    def login():
        entered.set()
        release.wait(3)
        return session.login, session.polling

    session.login_oauth = login
    manager = TidalDeviceLoginManager(store, session_factory=lambda: session)
    starting = asyncio.create_task(manager.start())
    await asyncio.to_thread(entered.wait, 2)
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    release.set()
    session.polling.set_result(True)
    assert (await settled(manager))["state"] == "cancelled"
    assert store.load() is None
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["declined", "exception", "expired"])
async def test_failed_login_is_redacted_and_does_not_save(tmp_path, caplog, outcome):
    store = TidalSessionStore(tmp_path / "private")
    session = FakeSession(checked=False)
    manager = TidalDeviceLoginManager(store, session_factory=lambda: session)
    with caplog.at_level(logging.DEBUG):
        await manager.start()
        logging.getLogger("tidalapi.session").error("synthetic-access-secret")
        logging.getLogger("tidalapi.request").debug("synthetic-internal-device-secret")
        if outcome == "exception":
            session.polling.set_exception(RuntimeError("synthetic-refresh-secret"))
        elif outcome == "expired":
            session.polling.set_exception(TimeoutError("synthetic-refresh-secret"))
        else:
            session.polling.set_result(True)
        result = await settled(manager)
    assert result["state"] == ("expired" if outcome == "expired" else "error")
    assert_no_secrets(result)
    assert not caplog.text and store.load() is None
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("expiry_time", datetime(1970, 1, 1)),
    ("user_id", "not-numeric"), ("user_id", 0), ("user_id", -1),
    ("user_id", True), ("user_id", 2**63), ("user_id", "01"),
])
async def test_completed_login_rejects_expired_token_or_invalid_user_id(tmp_path, field, value):
    store = TidalSessionStore(tmp_path / "private")
    session = FakeSession()
    if field == "user_id":
        session.user.id = value
    else:
        session.expiry_time = value
    manager = TidalDeviceLoginManager(store, session_factory=lambda: session)
    await manager.start()
    session.polling.set_result(True)
    result = await settled(manager)
    assert result["error"] == "login_failed" and store.load() is None
    await manager.close()


@pytest.mark.asyncio
async def test_failed_attempt_has_minimum_retry_cooldown(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(tidal_device_auth, "time", SimpleNamespace(
        monotonic=lambda: now[0], time=lambda: 1700000000.0,
    ))
    sessions = []

    def factory():
        session = FakeSession(checked=False)
        sessions.append(session)
        return session

    manager = TidalDeviceLoginManager(TidalSessionStore(tmp_path / "private"), session_factory=factory)
    await manager.start()
    sessions[0].polling.set_result(True)
    assert (await settled(manager))["retry_after"] == 5
    assert (await manager.start())["state"] == "error"
    assert len(sessions) == 1
    now[0] += 4.9
    assert (await manager.start())["retry_after"] == 1 and len(sessions) == 1
    now[0] += 0.2
    assert (await manager.start())["state"] == "pending" and len(sessions) == 2
    await manager.cancel()
    sessions[1].polling.set_result(True)
    await settled(manager)
    await manager.close()
