"""Real local HTTP, synthetic manager; no provider traffic or grants."""
import pytest
from test_web_ui import api, serving, state


class Manager:
    def __init__(self):
        self.starts = 0
        self.cancels = 0
        self.closed = False
    async def status(self):
        return {"state": "idle", "active": False, "session_status": "missing",
                "access_token": "SYNTHETIC_SECRET"}
    async def start(self):
        self.starts += 1
        return {"state": "pending", "active": True, "verification_uri": "https://link.tidal.com/SYNTHETIC",
                "user_code": "SYNTHETIC", "device_code": "SYNTHETIC_SECRET"}
    async def cancel(self):
        self.cancels += 1
        return {"state": "cancelled", "active": False}
    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_explicit_login_and_cancel_reuse_existing_browser_admission(api):
    manager = Manager()
    api.tidal_login = manager
    async with serving(api) as (session, base):
        boot = await state(session, base)
        for path in ("/ui/tidal", "/ui/tidal.js"):
            async with session.get(base + path) as response:
                assert response.status == 200
                assert response.headers["Cache-Control"] == "no-store"
        async with session.get(base + "/orr/v1/tidal/status") as response:
            assert "SYNTHETIC_SECRET" not in await response.text()
            assert (await response.json())["session_status"] == "missing"
        headers = {"Origin": base, "X-ORR-CSRF": boot["csrf_token"]}
        async with session.post(base + "/orr/v1/tidal/start", json={"confirm_experimental_login": True}, headers=headers) as response:
            assert response.status == 200
            value = await response.json()
            assert value["user_code"] == "SYNTHETIC" and "device_code" not in value
        async with session.post(base + "/orr/v1/tidal/cancel", json={"cancel": True}, headers=headers) as response:
            assert response.status == 200
        assert manager.starts == manager.cancels == 1
        assert not api.mpv.calls
    assert manager.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("headers,payload", [
    ({}, {"confirm_experimental_login": True}),
    ({"Origin": "https://other.example"}, {"confirm_experimental_login": True}),
    (None, {"confirm_experimental_login": False}),
    (None, {"confirm_experimental_login": 1}),
    (None, {"confirm_experimental_login": True, "url": "https://evil.example"}),
])
async def test_unadmitted_or_unconfirmed_requests_never_start_provider(api, headers, payload):
    manager = Manager()
    api.tidal_login = manager
    async with serving(api) as (session, base):
        boot = await state(session, base)
        if headers is None:
            headers = {"Origin": base, "X-ORR-CSRF": boot["csrf_token"]}
        async with session.post(base + "/orr/v1/tidal/start", json=payload, headers=headers) as response:
            assert response.status in {400, 403}
        assert manager.starts == 0


@pytest.mark.asyncio
async def test_account_sharing_is_explicit_revocable_and_never_in_status(api, tmp_path):
    import json
    import time
    from xml.etree import ElementTree as ET
    from fake_linkplay.tidal_account import TidalDeviceAccountBridge
    from fake_linkplay.tidal_session import TidalSession, TidalSessionStore
    from test_soap_faults import request, fault_code

    store = TidalSessionStore(tmp_path / "private-account")
    store.save(TidalSession("SYNTHETIC_ACCESS_SECRET", "SYNTHETIC_REFRESH_SECRET",
                            time.time() + 600, "42", "DE", client_id="synthetic-device-client",
                            auth_flow="tidalapi-device-v0.8.11"))
    api.tidal_account = TidalDeviceAccountBridge(store)
    api.tidal_login = Manager()
    account_request = lambda: request("GetUserInfo", "<AccountSource>Tidal</AccountSource><Version>2</Version>", service="PlayQueue")
    assert fault_code(await api.soap(account_request())) == "501"
    async with serving(api) as (session, base):
        boot = await state(session, base)
        headers = {"Origin": base, "X-ORR-CSRF": boot["csrf_token"]}
        async with session.post(base + "/orr/v1/tidal/share", json={"confirm_lan_account_sharing": True}) as response:
            assert response.status == 403
        assert not api.tidal_account.shared
        async with session.post(base + "/orr/v1/tidal/share", json={"confirm_lan_account_sharing": True}, headers=headers) as response:
            value = await response.json()
            assert response.status == 200 and value["account_shared"] is True
            assert "SECRET" not in json.dumps(value)
        response = await api.soap(account_request())
        assert response.status == 200
        payload = json.loads(next(n.text for n in ET.fromstring(response.text).iter() if n.tag == "Result"))
        assert "clientId" in payload and "redirectURI" not in payload and "refresh_token" not in payload
        async with session.get(base + "/orr/v1/tidal/status") as response:
            assert "SECRET" not in await response.text()
        assert "Tidal" not in api.state.as_status_ex("127.0.0.1")["supported_services"]
        async with session.post(base + "/orr/v1/tidal/unshare", json={"stop_lan_account_sharing": True}, headers=headers) as response:
            assert (await response.json())["account_shared"] is False
        assert fault_code(await api.soap(account_request())) == "501"
        assert store.load().access_token == "SYNTHETIC_ACCESS_SECRET"
        await api.tidal_account.set_shared(True)
        store.clear()
        async with session.get(base + "/orr/v1/tidal/status") as response:
            assert (await response.json())["account_shared"] is False
        assert not api.mpv.calls


@pytest.mark.asyncio
async def test_private_play_requires_confirmation_but_no_lan_account_sharing(api, tmp_path):
    from unittest.mock import AsyncMock
    from test_tidal_queue_playback import configured
    await configured(api, tmp_path)
    api.tidal_login = Manager()
    api._play_tidal_track = AsyncMock(wraps=api._play_tidal_track)
    await api.tidal_account.set_shared(False)
    async with serving(api) as (session, base):
        boot = await state(session, base)
        headers = {"Origin": base, "X-ORR-CSRF": boot["csrf_token"]}
        async with session.post(base + "/orr/v1/tidal/play", json={"track_id": "42", "confirm_experimental_playback": False}, headers=headers) as response:
            assert response.status == 400
        api._play_tidal_track.assert_not_awaited()
        async with session.post(base + "/orr/v1/tidal/play", json={"track_id": "42", "confirm_experimental_playback": True}, headers=headers) as response:
            assert response.status == 200
            assert (await response.json())["playback_requested"] is True
        api._play_tidal_track.assert_awaited_once_with("42")
        assert api.tidal_playback.calls == [("42", "HIGH")]
        assert [name for name, _ in api.mpv.calls].count("load") == 1
        assert api.state.status == "play"


@pytest.mark.asyncio
@pytest.mark.parametrize("error,outcome", [
    (None, "saved"), ("refresh_result_uncertain", "uncertain"), ("refresh_rejected", "rejected"),
])
async def test_explicit_refresh_reuses_admission_and_does_not_export_session(api, error, outcome):
    class Refresh:
        calls = 0
        async def refresh(self):
            self.calls += 1
            return {'state': 'refreshed' if error is None else 'error',
                    'session_saved': error is None, 'refresh_outcome': outcome, 'error': error,
                    'sharing_warning': False, 'access_token': 'SYNTHETIC_SECRET',
                    'refresh_token': 'SYNTHETIC_SECRET', 'user_id': 'SYNTHETIC_SECRET'}
    manager = Manager()
    refresh = Refresh()
    api.tidal_login, api.tidal_refresh = manager, refresh
    async with serving(api) as (session, base):
        boot = await state(session, base)
        async with session.post(base + '/orr/v1/tidal/refresh', json={'confirm_session_refresh': True}) as response:
            assert response.status == 403
        assert refresh.calls == 0
        headers = {'Origin': base, 'X-ORR-CSRF': boot['csrf_token']}
        async with session.post(base + '/orr/v1/tidal/refresh', json={'confirm_session_refresh': True}, headers=headers) as response:
            assert response.status == 200
            body = await response.text()
            assert 'SYNTHETIC_SECRET' not in body
            value = await response.json()
            assert value['refresh_outcome'] == outcome
            assert value.get('error') == error
        async with session.get(base + '/orr/v1/tidal/status') as response:
            assert response.status == 200
        assert refresh.calls == 1 and manager.starts == 0 and not api.mpv.calls


@pytest.mark.asyncio
async def test_restarted_session_status_is_local_sanitized_and_keeps_sharing_explicit(api, tmp_path):
    import time
    from dataclasses import replace
    from fake_linkplay.tidal_account import TidalDeviceAccountBridge
    from fake_linkplay.tidal_device_auth import TidalDeviceLoginManager
    from fake_linkplay.tidal_session import TidalSession, TidalSessionStore

    store = TidalSessionStore(tmp_path / "private-account")
    saved = TidalSession("SYNTHETIC_ACCESS_SECRET", "SYNTHETIC_REFRESH_SECRET",
                         time.time() + 600, "12345", "DE", client_id="synthetic-device-client",
                         auth_flow="tidalapi-device-v0.8.11")
    store.save(saved)
    api.tidal_login = TidalDeviceLoginManager(store, session_factory=lambda: pytest.fail("provider call"))
    api.tidal_account = TidalDeviceAccountBridge(store)
    async with serving(api) as (session, base):
        for record, expected in ((saved, "stored"), (replace(saved, expires_at=1), "expired")):
            store.save(record)
            async with session.get(base + "/orr/v1/tidal/status") as response:
                assert response.status == 200
                assert await response.json() == {"state": "idle", "active": False,
                                                "session_status": expected, "account_shared": False}
        assert not api.mpv.calls


@pytest.mark.asyncio
async def test_session_status_does_not_export_arbitrary_manager_values(api):
    class InvalidStatus(Manager):
        async def status(self):
            return {"state": "idle", "active": False, "session_status": "SYNTHETIC_SECRET"}
    api.tidal_login = InvalidStatus()
    async with serving(api) as (session, base):
        async with session.get(base + "/orr/v1/tidal/status") as response:
            assert await response.json() == {"state": "idle", "active": False, "account_shared": False}


def recovery_account(api, tmp_path):
    """Real local owners with synthetic credentials and no provider transport."""
    import time
    from fake_linkplay.tidal_account import TidalDeviceAccountBridge
    from fake_linkplay.tidal_device_auth import TidalDeviceLoginManager
    from fake_linkplay.tidal_playback import TidalPlaybackResolver
    from fake_linkplay.tidal_session import TidalSession, TidalSessionStore
    from test_tidal_device_auth import FakeSession

    store = TidalSessionStore(tmp_path / "private-recovery")
    saved = TidalSession("SYNTHETIC_OLD_ACCESS", "SYNTHETIC_OLD_REFRESH", time.time() + 600,
                         "42", "DE", "synthetic-client", "tidalapi-device-v0.8.11")
    store.save(saved)
    fake = FakeSession()
    api.tidal_account = TidalDeviceAccountBridge(store)
    api.tidal_login = TidalDeviceLoginManager(store, session_factory=lambda: fake)
    api.tidal_playback = TidalPlaybackResolver(store)
    return store, saved, fake


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [False, True])
async def test_confirmed_forget_recovers_marked_session_without_retry_or_automatic_login(api, tmp_path, rejected):
    import json
    from fake_linkplay.tidal_refresh import TidalSessionRefreshManager
    from test_tidal_device_auth import settled
    from test_tidal_refresh import config, responses, TOKEN, IDENTITY

    store, saved, fake = recovery_account(api, tmp_path)
    await api.tidal_account.set_shared(True)
    with store.lease() as lease:
        lease.mark_refresh_attempt(saved, rejected=rejected)
    marker = (store.directory / "refresh-attempt").read_bytes()
    refresh = TidalSessionRefreshManager(store, config_factory=lambda: pytest.fail("provider configuration"))
    api.tidal_refresh = refresh
    assert (await refresh.refresh())["error"] == ("refresh_rejected" if rejected else "refresh_result_uncertain")
    async with serving(api) as (session, base):
        headers = {"Origin": base, "X-ORR-CSRF": (await state(session, base))["csrf_token"]}
        async with session.post(base + "/orr/v1/tidal/cancel", json={"cancel": True}, headers=headers) as response:
            assert response.status == 200
        assert store.load() == saved and api.tidal_account.shared
        async with session.post(base + "/orr/v1/tidal/disconnect", json={"confirm_forget_local_session": True}, headers=headers) as response:
            assert response.status == 200
            result = await response.json()
            assert result["session_forgotten"] and result["session_status"] == "missing"
            assert not result["account_shared"] and "SYNTHETIC" not in json.dumps(result)
        assert store.load() is None and not api.tidal_account.sharing_requested
        assert (store.directory / "refresh-attempt").read_bytes() == marker
        assert store.refresh_outcome(saved) == ("rejected" if rejected else "uncertain")
        assert not fake.calls and not api.mpv.calls
        # A separately confirmed new login can now succeed; Forget did not start it.
        async with session.post(base + "/orr/v1/tidal/start", json={"confirm_experimental_login": True}, headers=headers) as response:
            assert (await response.json())["state"] == "pending"
        fake.polling.set_result(True)
        await settled(api.tidal_login)
        assert (await api.tidal_login.status())["session_status"] == "stored"
        fresh = store.load()
        assert fresh != saved and store.refresh_outcome(fresh) is None
        assert not api.tidal_account.sharing_requested
        # A genuinely new grant is refreshable; the old grant stays guarded.
        request, calls = responses(TOKEN, IDENTITY)
        cfg = config()
        cfg.client_id = fresh.client_id
        new_refresh = TidalSessionRefreshManager(store, config_factory=lambda: cfg, request_json=request)
        assert (await new_refresh.refresh())["refresh_outcome"] == "saved"
        assert len(calls) == 2 and not api.mpv.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("headers,payload", [
    ({}, {"confirm_forget_local_session": True}),
    ({"Origin": "https://other.example"}, {"confirm_forget_local_session": True}),
    (None, {"confirm_forget_local_session": False}),
    (None, {"confirm_forget_local_session": 1}),
    (None, {"confirm_forget_local_session": True, "path": "/other"}),
])
async def test_forget_requires_exact_confirmation_and_browser_admission(api, tmp_path, headers, payload):
    store, saved, fake = recovery_account(api, tmp_path)
    async with serving(api) as (session, base):
        if headers is None:
            headers = {"Origin": base, "X-ORR-CSRF": (await state(session, base))["csrf_token"]}
        async with session.post(base + "/orr/v1/tidal/disconnect", json=payload, headers=headers) as response:
            assert response.status in {400, 403}
        assert store.load() == saved and not fake.calls and not api.mpv.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["store", "backend", "account", "resolution", "test", "login"])
async def test_forget_refuses_busy_owner_without_waiting_or_changing_session(api, tmp_path, owner):
    import asyncio
    from contextlib import ExitStack
    from fake_linkplay.web_ui import WebAdmissionError
    from test_tidal_device_auth import settled

    store, saved, fake = recovery_account(api, tmp_path)
    with ExitStack() as stack:
        if owner == "store":
            stack.enter_context(store.lease())
        elif owner in {"backend", "account"}:
            lock = api._backend_lock if owner == "backend" else api.tidal_account._lock
            await lock.acquire()
            stack.callback(lock.release)
        elif owner == "resolution":
            api._tidal_pending_resolutions.add(asyncio.current_task())
            stack.callback(api._tidal_pending_resolutions.clear)
        elif owner == "test":
            api.tidal_test._live = True
            stack.callback(setattr, api.tidal_test, "_live", False)
        else:
            store.clear()
            assert (await api.tidal_login.start())["state"] == "pending"
            await api.tidal_login.cancel()  # Cancel still owns storage until its worker drains.
        with pytest.raises(WebAdmissionError) as caught:
            await asyncio.wait_for(api._disconnect_tidal_session(), .25)
        assert caught.value.code in {"disconnect_busy", "disconnect_storage_unavailable"}
        assert store.load() == (None if owner == "login" else saved)
        assert not api.mpv.calls
        if owner == "login":
            fake.polling.set_result(True)
            await settled(api.tidal_login)
            assert store.load() is None
    await api.tidal_login.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["PLAYING", "PAUSED", "LOADING", "BUFFERING", "COMMAND_ACCEPTED", "ERROR"])
async def test_forget_requires_tidal_stop_and_never_sends_stop_itself(api, tmp_path, phase):
    from fake_linkplay.queue import PlaybackQueue, QueueItem
    from fake_linkplay.state import PlaybackPhase
    from fake_linkplay.web_ui import WebAdmissionError
    store, saved, fake = recovery_account(api, tmp_path)
    item = QueueItem(url="https://api.tidal.com/v1/tracks/42")
    item._tidal_track = ("42", "LOSSLESS")
    api.state.queue = PlaybackQueue(items=[item], index=1, source="Tidal")
    api.state.phase = PlaybackPhase(phase)
    with pytest.raises(WebAdmissionError) as caught:
        await api._disconnect_tidal_session()
    assert caught.value.code == "disconnect_stop_first"
    assert store.load() == saved and not api.mpv.calls and not fake.calls


@pytest.mark.asyncio
async def test_forget_only_retires_tidal_cache_and_preserves_foreign_playback(api, tmp_path):
    from copy import deepcopy
    from fake_linkplay.queue import PlaybackQueue, QueueItem
    from fake_linkplay.state import PlaybackPhase
    store, saved, fake = recovery_account(api, tmp_path)
    foreign = QueueItem(url="https://radio.example.test/live")
    tidal = QueueItem(url="https://api.tidal.com/v1/tracks/42")
    tidal._tidal_track = ("42", "LOSSLESS")
    tidal._tidal_stream_url = "https://audio.example.test/file?token=SYNTHETIC_SIGNED"
    tidal._tidal_candidate = object()
    tidal._tidal_session_fingerprint = b"synthetic-binding"
    api.state.queue = PlaybackQueue(items=[foreign, tidal], index=1, source="Radio")
    api.state.queue._tidal_prepared_page = object()
    api.state.phase, api.state.intent = PlaybackPhase.PLAYING, "play"
    api.state.current_url = api.state.backend_url = api.state.backend_original_url = foreign.url
    api._proxy_sources = {foreign.url: 1, tidal._tidal_stream_url: 2}
    before = (api.state.generation, api.state.phase, api.state.intent, api.state.current_url,
              api.state.backend_url, api.state.backend_original_url, deepcopy(foreign))
    assert (await api._disconnect_tidal_session())["session_forgotten"]
    assert before == (api.state.generation, api.state.phase, api.state.intent, api.state.current_url,
                      api.state.backend_url, api.state.backend_original_url, foreign)
    assert not tidal._tidal_stream_url and tidal._tidal_candidate is None
    assert not tidal._tidal_session_fingerprint and api.state.queue._tidal_prepared_page is None
    assert api._proxy_sources == {foreign.url: 1}
    assert tidal.url == "https://api.tidal.com/v1/tracks/42"
    assert store.load() is None and not api.mpv.calls and not fake.calls


@pytest.mark.asyncio
async def test_forget_refuses_unsafe_session_file_and_preserves_external_target(api, tmp_path):
    from fake_linkplay.web_ui import WebAdmissionError
    store, saved, fake = recovery_account(api, tmp_path)
    target = tmp_path / "other"
    target.write_text("SYNTHETIC_PRIVATE_OTHER")
    target.chmod(0o600)
    path = store.directory / "session.json"
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(WebAdmissionError) as caught:
        await api._disconnect_tidal_session()
    assert caught.value.code == "disconnect_storage_unavailable"
    assert path.is_symlink() and target.read_text() == "SYNTHETIC_PRIVATE_OTHER"
    assert not api.mpv.calls and not fake.calls


@pytest.mark.asyncio
async def test_forget_refuses_previous_tidal_backend_during_foreign_source_selection(api, tmp_path):
    from fake_linkplay.queue import PlaybackQueue, QueueItem
    from fake_linkplay.state import PlaybackPhase
    from fake_linkplay.web_ui import WebAdmissionError
    store, saved, fake = recovery_account(api, tmp_path)
    api.state.queue = PlaybackQueue(items=[QueueItem(url="https://radio.example.test/live")], index=1)
    api.state.phase, api.state.intent = PlaybackPhase.LOADING, "play"
    api.state.backend_original_url = "https://api.tidal.com/v1/tracks/42"
    with pytest.raises(WebAdmissionError) as caught:
        await api._disconnect_tidal_session()
    assert caught.value.code == "disconnect_stop_first"
    assert store.load() == saved and not api.mpv.calls and not fake.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["backend", "account"])
async def test_forget_refuses_awakened_lock_waiter_even_when_lock_reports_unlocked(api, tmp_path, owner):
    import asyncio
    from fake_linkplay.web_ui import WebAdmissionError
    store, saved, fake = recovery_account(api, tmp_path)
    lock = api._backend_lock if owner == "backend" else api.tidal_account._lock
    release = asyncio.Event()
    await lock.acquire()

    async def queued_owner():
        async with lock:
            await release.wait()

    waiter = asyncio.create_task(queued_owner())
    await asyncio.sleep(0)
    lock.release()
    assert not lock.locked()  # The queued task still owns FIFO priority.
    try:
        with pytest.raises(WebAdmissionError) as caught:
            # Directly await in this turn so the FIFO waiter has not acquired yet.
            await api._disconnect_tidal_session()
        assert caught.value.code == "disconnect_busy"
        assert store.load() == saved and not fake.calls and not api.mpv.calls
    finally:
        release.set()
        await waiter
    # No cancelled acquire or timeout leaks; a fresh explicit call succeeds.
    assert (await api._disconnect_tidal_session())["session_forgotten"]
    assert store.load() is None
