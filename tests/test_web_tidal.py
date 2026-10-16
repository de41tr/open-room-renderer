"""Real local HTTP, synthetic manager; no provider traffic or grants."""
import pytest
from test_web_ui import api, serving, state


class Manager:
    def __init__(self):
        self.starts = 0
        self.cancels = 0
        self.closed = False
    async def status(self):
        return {"state": "idle", "active": False, "access_token": "SYNTHETIC_SECRET"}
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
