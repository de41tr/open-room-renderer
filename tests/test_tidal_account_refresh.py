"""Offline GetUserInfo renewal through the existing single refresh owner."""
import asyncio
from dataclasses import asdict, replace
import json
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from fake_linkplay import tidal_account, tidal_refresh
from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.state import RendererState
from fake_linkplay.tidal_account import TidalAccountUnavailable, TidalDeviceAccountBridge
from fake_linkplay.tidal_codec import decode_account_field
from fake_linkplay.tidal_refresh import TidalSessionRefreshManager
from fake_linkplay.tidal_session import TidalSession, TidalSessionStore
from test_tidal_account import UUID
from test_tidal_refresh import TOKEN, config, responses
from test_soap_faults import FakeBackend, fault_code, request as soap_request


IDENTITY = {"userId": 42, "countryCode": "DE"}


@pytest.fixture
async def shared_account(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail("account refresh tests must never perform live provider requests")
    monkeypatch.setattr(tidal_refresh.aiohttp, "ClientSession", no_network)
    clock = [1000.0]
    monkeypatch.setattr(tidal_account.time, "time", lambda: clock[0])
    store = TidalSessionStore(tmp_path / "private-account")
    store.save(TidalSession("synthetic-old-access", "synthetic-old-refresh", 2000,
                            "42", "DE", "synthetic-client", "tidalapi-device-v0.8.11"))
    bridge = TidalDeviceAccountBridge(store)
    await bridge.set_shared(True)
    return store, bridge, clock


def bind(store, bridge, request):
    owner = TidalSessionRefreshManager(store, config_factory=config, request_json=request,
                                       on_committed=bridge._session_refreshed)
    bridge._bind_refresh_owner(owner)
    return owner


@pytest.mark.asyncio
@pytest.mark.parametrize("read_at", [1700, 1999.5, 2001])
async def test_expired_shared_account_refreshes_and_projects_with_fresh_time(shared_account, read_at):
    store, bridge, clock = shared_account
    calls = []
    clock[0] = read_at

    async def request(url, **kwargs):
        calls.append((url, kwargs))
        if "data" in kwargs:
            return TOKEN
        clock[0] += 10
        return IDENTITY

    bind(store, bridge, request)
    result = await bridge.get_user_info(UUID, version="2", now=read_at)
    assert decode_account_field(UUID, result["access_token"]) == TOKEN["access_token"]
    assert decode_account_field(UUID, result["clientId"]) == "synthetic-client"
    assert result["expires_in"] == 3590 and result["user"] == IDENTITY | {"username": ""}
    assert "refresh_token" not in result and TOKEN["refresh_token"] not in json.dumps(result)
    assert store.load().refresh_token == TOKEN["refresh_token"] and bridge.shared
    assert len(calls) == 2
    await bridge.get_user_info(UUID, version="2")
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["fresh", "unshared", "version", "uuid", "time", "unknown_expiry"])
async def test_invalid_or_unneeded_account_projection_never_refreshes(shared_account, case):
    store, bridge, clock = shared_account
    request, calls = responses()
    bind(store, bridge, request)
    clock[0] = 1000 if case == "fresh" else 2001
    if case == "unshared":
        await bridge.set_shared(False)
    if case == "unknown_expiry":
        # A replaced record invalidates the prior exact-record opt-in too.
        store.save(replace(store.load(), expires_at=None))
    kwargs = {"version": "1" if case == "version" else "2"}
    if case == "time":
        kwargs["now"] = float("nan")
    if case == "fresh":
        await bridge.get_user_info(UUID, **kwargs)
    else:
        with pytest.raises(TidalAccountUnavailable):
            await bridge.get_user_info("short" if case == "uuid" else UUID, **kwargs)
    assert not calls and not (store.directory / "refresh-attempt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["unshare", "record"])
async def test_revocation_or_record_replacement_during_refresh_denies_export(shared_account, change):
    store, bridge, clock = shared_account
    clock[0] = 2001
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(url, **kwargs):
        if "data" in kwargs:
            entered.set()
            await release.wait()
            return TOKEN
        return IDENTITY

    bind(store, bridge, request)
    running = asyncio.create_task(bridge.get_user_info(UUID, version="2"))
    await entered.wait()
    waiting = asyncio.create_task(bridge.get_user_info(UUID, version="2"))
    await asyncio.sleep(0)
    assert not waiting.done() and bridge.adoption_available
    if change == "unshare":
        # This completes while the refresh is still blocked: no account lock
        # is held across network I/O, and the commit callback cannot re-share.
        await asyncio.wait_for(bridge.set_shared(False), timeout=.2)
    else:
        # Simulate an uncooperative same-user file replacement, outside leases.
        replacement = replace(store.load(), access_token="synthetic-replaced", user_id="43")
        (store.directory / "session.json").write_text(json.dumps({"schema": 2, **asdict(replacement)}))
    release.set()
    for task in (running, waiting):
        with pytest.raises(TidalAccountUnavailable):
            await task
    assert not bridge.sharing_requested and not bridge.adoption_available


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "timeout"])
async def test_cancel_or_timeout_keeps_marker_and_blocks_repeat(shared_account, monkeypatch, stop):
    store, bridge, clock = shared_account
    clock[0] = 2001
    entered = asyncio.Event()
    calls, deadlines = [], []

    async def request(url, **kwargs):
        calls.append(url)
        entered.set()
        await asyncio.Event().wait()

    bind(store, bridge, request)
    if stop == "timeout":
        def timeout(seconds):
            deadlines.append(seconds)
            return asyncio.timeout(.01)
        monkeypatch.setattr(tidal_refresh, "asyncio", SimpleNamespace(timeout=timeout, Event=asyncio.Event))
    running = asyncio.create_task(bridge.get_user_info(UUID, version="2"))
    await entered.wait()
    if stop == "cancel":
        running.cancel()
    with pytest.raises(asyncio.CancelledError if stop == "cancel" else TidalAccountUnavailable):
        await running
    assert store.refresh_outcome(store.load()) == "uncertain"
    if stop == "timeout":
        assert deadlines == [10]
    # A fresh owner instance still consults the durable record marker.
    bind(store, bridge, request)
    with pytest.raises(TidalAccountUnavailable):
        await bridge.get_user_info(UUID, version="2")
    assert len(calls) == 1 and store.load().access_token == "synthetic-old-access"


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_concurrent_account_reads_cannot_double_refresh(shared_account, explicit):
    store, bridge, clock = shared_account
    clock[0] = 2001
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def request(url, **kwargs):
        calls.append(url)
        if "data" in kwargs:
            entered.set()
            await release.wait()
            return TOKEN
        return IDENTITY

    owner = bind(store, bridge, request)
    if explicit:
        clock[0] = 1000  # A manual refresh can rotate an otherwise current token.
    first = asyncio.create_task(owner.refresh() if explicit else bridge.get_user_info(UUID, version="2"))
    await entered.wait()
    second = asyncio.create_task(bridge.get_user_info(UUID, version="2"))
    await asyncio.sleep(0)
    assert not second.done() and bridge.adoption_available
    release.set()
    assert (await first)["state" if explicit else "token_type"] == ("refreshed" if explicit else "Bearer")
    result = await second
    assert decode_account_field(UUID, result["access_token"]) == TOKEN["access_token"]
    await bridge.get_user_info(UUID, version="2")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_refresh_owner_cannot_bind_another_store_or_callback(shared_account):
    store, bridge, _ = shared_account
    request, calls = responses()
    same_path_other_store = TidalSessionStore(store.directory)
    for owner in (
        TidalSessionRefreshManager(same_path_other_store, on_committed=bridge._session_refreshed,
                                   config_factory=config, request_json=request),
        TidalSessionRefreshManager(store, config_factory=config, request_json=request),
        SimpleNamespace(_store=store, _on_committed=bridge._session_refreshed),
    ):
        with pytest.raises(TidalAccountUnavailable):
            bridge._bind_refresh_owner(owner)
    assert not calls and bridge._refresh is None


@pytest.mark.asyncio
async def test_replacement_expiring_before_projection_cannot_use_stale_now(shared_account, monkeypatch):
    store, bridge, clock = shared_account
    clock[0] = 2001
    request, calls = responses(TOKEN, IDENTITY)
    owner = bind(store, bridge, request)
    refresh = owner.refresh_owned

    async def elapsed(*args, **kwargs):
        result = await refresh(*args, **kwargs)
        clock[0] = store.load().expires_at + 1
        return result

    monkeypatch.setattr(owner, "refresh_owned", elapsed)
    with pytest.raises(TidalAccountUnavailable):
        await bridge.get_user_info(UUID, version="2", now=2001)
    assert len(calls) == 2 and not bridge.shared


@pytest.mark.asyncio
@pytest.mark.parametrize("version_arg", ["", "<Version>2</Version>"])
async def test_real_get_user_info_route_uses_refreshed_encoded_device_record(shared_account, tmp_path, version_arg):
    store, bridge, clock = shared_account
    clock[0] = 2001
    request, calls = responses(TOKEN, IDENTITY)
    owner = bind(store, bridge, request)
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path),
                 advertise_ip="127.0.0.1")
    api = HttpApi(cfg, RendererState.from_config(cfg), FakeBackend(),
                  tidal_account=bridge, tidal_refresh=owner)
    response = await api.soap(soap_request("GetUserInfo", "<AccountSource>Tidal</AccountSource>"
                                          + version_arg, service="PlayQueue", path="PlayQueue1"))
    assert response.status == 200 and len(calls) == 2
    root = ET.fromstring(response.text)
    result = json.loads(next(node.text for node in root.iter() if node.tag == "Result"))
    assert decode_account_field(cfg.upnp_uuid, result["access_token"]) == TOKEN["access_token"]
    assert "refresh_token" not in result and TOKEN["refresh_token"] not in response.text
    await api._eventing.close()


@pytest.mark.asyncio
async def test_windows_omitted_version_adopts_shared_account_without_mutation_or_secret_logs(shared_account, tmp_path, caplog):
    store, bridge, _ = shared_account
    request, calls = responses()
    owner = bind(store, bridge, request)
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path),
                 advertise_ip="127.0.0.1", wiim_home_profile=True, log_wiim_http_details=True)
    api = HttpApi(cfg, RendererState.from_config(cfg), FakeBackend(),
                  tidal_account=bridge, tidal_refresh=owner)
    before = store.load(), (store.directory / "session.json").read_bytes(), asdict(api.state)
    results = []
    with caplog.at_level("INFO", logger="fake_linkplay.http_api"):
        for version_arg in ("", "<Version>2</Version>"):
            # Windows 0.2.10.5 sends just AccountSource; the explicit v2 result
            # must remain identical. All identity/token values here are synthetic.
            req = soap_request("GetUserInfo", "<AccountSource>Tidal</AccountSource>" + version_arg,
                               service="PlayQueue", path="PlayQueue1")
            req.remote, req.method, req.path = "192.0.2.1", "POST", "/upnp/control/PlayQueue1"
            response = await api.soap(req)
            assert response.status == 200
            results.append(json.loads(ET.fromstring(response.text).findtext(".//Result")))
    result = results[0]
    assert result == results[1]
    status_uuid = api.state.as_status_ex("127.0.0.1")["uuid"]
    assert decode_account_field(status_uuid, result["access_token"]) == before[0].access_token
    assert decode_account_field(status_uuid, result["clientId"]) == before[0].client_id
    assert result["user"] == IDENTITY | {"username": ""}
    assert "refresh_token" not in result and "redirectURI" not in result
    assert before == (store.load(), (store.directory / "session.json").read_bytes(), asdict(api.state))
    assert not calls and not api.mpv.calls and api.tidal_login is None
    assert not (store.directory / "refresh-attempt").exists()
    assert "[account payload omitted]" in caplog.text
    for secret in (before[0].access_token, before[0].refresh_token, before[0].client_id,
                   result["access_token"], result["clientId"]):
        assert secret not in caplog.text
    assert "Tidal" not in api.state.as_status_ex("127.0.0.1")["supported_services"]
    await api._eventing.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("version_arg", [
    "<Version/>", "<Version></Version>", "<Version> \t\n </Version>",
    "<Version>1</Version>", "<Version>3</Version>", "<Version>2.0</Version>",
    '<v:Version xmlns:v="urn:synthetic"/>', "<version/>",
])
async def test_explicit_invalid_version_cannot_adopt_or_refresh(shared_account, tmp_path, version_arg):
    store, bridge, clock = shared_account
    clock[0] = 2001
    request, calls = responses()
    owner = bind(store, bridge, request)
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path),
                 advertise_ip="127.0.0.1")
    api = HttpApi(cfg, RendererState.from_config(cfg), FakeBackend(),
                  tidal_account=bridge, tidal_refresh=owner)
    before = store.load()
    response = await api.soap(soap_request("GetUserInfo", "<AccountSource>Tidal</AccountSource>"
                                          + version_arg, service="PlayQueue", path="PlayQueue1"))
    assert fault_code(response) == "501"
    assert not calls and store.load() == before and not api.mpv.calls
    assert not (store.directory / "refresh-attempt").exists()
    assert "synthetic" not in response.text
    await api._eventing.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unshared", "restart", "replaced", "country", "provenance", "expired"])
async def test_omitted_version_preserves_device_sharing_and_session_gates(shared_account, tmp_path, case):
    store, bridge, clock = shared_account
    if case == "unshared":
        await bridge.set_shared(False)
    elif case == "restart":
        bridge = TidalDeviceAccountBridge(store)
    elif case == "replaced":
        store.save(replace(store.load(), access_token="synthetic-replacement"))
    elif case == "country":
        store.save(replace(store.load(), country_code=None))
    elif case == "provenance":
        store.save(replace(store.load(), client_id=None, auth_flow=None))
    else:
        clock[0] = 2001  # No refresh owner: an expired token must not escape.
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path),
                 advertise_ip="127.0.0.1")
    api = HttpApi(cfg, RendererState.from_config(cfg), FakeBackend(), tidal_account=bridge)
    before = store.load()
    response = await api.soap(soap_request("GetUserInfo", "<AccountSource>Tidal</AccountSource>",
                                          service="PlayQueue", path="PlayQueue1"))
    assert fault_code(response) == "501"
    assert store.load() == before and not api.mpv.calls and api.tidal_login is None
    assert not (store.directory / "refresh-attempt").exists()
    assert "synthetic" not in response.text
    await api._eventing.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["Qobuz", "Deezer2", "tidal", ""])
async def test_omitted_version_never_exports_tidal_account_for_other_sources(shared_account, tmp_path, source):
    store, bridge, clock = shared_account
    clock[0] = 2001
    request, calls = responses()
    owner = bind(store, bridge, request)
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path),
                 advertise_ip="127.0.0.1")
    api = HttpApi(cfg, RendererState.from_config(cfg), FakeBackend(),
                  tidal_account=bridge, tidal_refresh=owner)
    before = store.load()
    response = await api.soap(soap_request("GetUserInfo", f"<AccountSource>{source}</AccountSource>",
                                          service="PlayQueue", path="PlayQueue1"))
    assert response.status == 200
    result = json.loads(ET.fromstring(response.text).findtext(".//Result"))
    assert result["id"] == source and result["userId"] == "" and "access_token" not in result
    assert not calls and store.load() == before and not api.mpv.calls
    assert not (store.directory / "refresh-attempt").exists()
    await api._eventing.close()


@pytest.mark.asyncio
async def test_production_runtime_binds_the_single_store_owner(shared_account, tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path),
                 advertise_ip="127.0.0.1")
    api = HttpApi(cfg, RendererState.from_config(cfg), FakeBackend(), _startup=True)
    assert api.tidal_account._refresh is api.tidal_refresh
    assert api.tidal_playback._refresh is api.tidal_refresh
    assert api.tidal_refresh._store is api.tidal_account._store
    assert not api.tidal_account.shared and not api.tidal_account.sharing_requested
    await api.tidal_login.close()
    await api._eventing.close()


@pytest.mark.asyncio
async def test_injected_other_store_account_keeps_working_without_wrong_refresh_owner(shared_account, tmp_path, monkeypatch):
    from pathlib import Path
    _, bridge, _ = shared_account
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path),
                 advertise_ip="127.0.0.1")
    api = HttpApi(cfg, RendererState.from_config(cfg), FakeBackend(), tidal_account=bridge, _startup=True)
    assert api.tidal_account is bridge and bridge._refresh is None
    assert api.tidal_refresh._store is not bridge._store
    result = await bridge.get_user_info(UUID, version="2")
    assert decode_account_field(UUID, result["access_token"]) == "synthetic-old-access"
    await api.tidal_login.close()
    await api._eventing.close()


@pytest.mark.asyncio
async def test_expired_remembered_account_restores_suspended_without_startup_refresh(shared_account):
    store, bridge, clock = shared_account
    await bridge.set_shared(True, remember=True)
    clock[0] = 2001
    restarted = TidalDeviceAccountBridge(store)
    request, calls = responses(TOKEN, IDENTITY)
    bind(store, restarted, request)
    assert not calls and not restarted.shared and restarted.sharing_requested and restarted.sharing_remembered
    result = await restarted.get_user_info(UUID, version="2")
    assert decode_account_field(UUID, result["access_token"]) == TOKEN["access_token"]
    assert len(calls) == 2 and restarted.shared and restarted.sharing_remembered
    assert TidalDeviceAccountBridge(store).shared
    await restarted.get_user_info(UUID, version="2")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_unshare_during_refresh_stays_revoked_after_restart(shared_account):
    store, bridge, clock = shared_account
    await bridge.set_shared(True, remember=True)
    clock[0] = 2001
    entered, release = asyncio.Event(), asyncio.Event()
    async def request(url, **kwargs):
        if "data" in kwargs:
            entered.set()
            await release.wait()
            return TOKEN
        return IDENTITY
    bind(store, bridge, request)
    running = asyncio.create_task(bridge.get_user_info(UUID, version="2"))
    await entered.wait()
    await asyncio.wait_for(bridge.set_shared(False), timeout=.2)
    assert not TidalDeviceAccountBridge(store).sharing_requested
    release.set()
    with pytest.raises(TidalAccountUnavailable):
        await running
    assert not bridge.sharing_requested and not bridge.sharing_remembered
    assert not TidalDeviceAccountBridge(store).sharing_requested
    assert store.load().access_token == TOKEN["access_token"]


@pytest.mark.asyncio
async def test_external_unshare_cannot_be_resurrected_by_refresh_callback(shared_account):
    store, bridge, clock = shared_account
    await bridge.set_shared(True, remember=True)
    clock[0] = 2001
    entered, release = asyncio.Event(), asyncio.Event()
    async def request(url, **kwargs):
        if "data" in kwargs:
            entered.set()
            await release.wait()
            return TOKEN
        return IDENTITY
    bind(store, bridge, request)
    running = asyncio.create_task(bridge.get_user_info(UUID, version="2"))
    await entered.wait()
    await TidalDeviceAccountBridge(store).set_shared(False)
    release.set()
    with pytest.raises(TidalAccountUnavailable):
        await running
    assert not bridge.sharing_remembered and not TidalDeviceAccountBridge(store).shared


@pytest.mark.asyncio
async def test_interruption_between_session_commit_and_consent_rotation_fails_closed(shared_account):
    store, bridge, _ = shared_account
    await bridge.set_shared(True, remember=True)
    replacement = replace(store.load(), access_token="synthetic-new", expires_at=3000)
    with store.lease() as lease:
        lease.save(replacement)  # Simulate process exit before its consent callback.
    restarted = TidalDeviceAccountBridge(store)
    assert not restarted.shared and not restarted.sharing_remembered
    assert not bridge.shared


@pytest.mark.asyncio
async def test_failed_consent_rotation_reports_warning_and_restart_stays_unshared(shared_account, monkeypatch):
    from fake_linkplay import tidal_session
    store, bridge, clock = shared_account
    await bridge.set_shared(True, remember=True)
    clock[0] = 2001
    request, calls = responses(TOKEN, IDENTITY)
    owner = bind(store, bridge, request)
    original_write = tidal_session.TidalSessionStore._write_at
    def failed_consent(directory, target, data):
        original_write(directory, target, data)
        if target == "account-sharing":
            raise OSError("Synthetic post-replace consent failure")
    monkeypatch.setattr(tidal_session.TidalSessionStore, "_write_at", staticmethod(failed_consent))
    result = await owner.refresh()
    assert result["refresh_outcome"] == "saved" and result["sharing_warning"]
    assert len(calls) == 2 and store.load().access_token == TOKEN["access_token"]
    assert not bridge.shared and not bridge.sharing_remembered
    assert not TidalDeviceAccountBridge(store).sharing_requested


@pytest.mark.asyncio
@pytest.mark.parametrize("lifetime", [20, 120])
async def test_short_lived_replacement_is_used_instead_of_repeated_immediate_rotation(shared_account, lifetime):
    store, bridge, clock = shared_account
    clock[0] = 2001
    token = {**TOKEN, "expires_in": lifetime}
    request, calls = responses(token, IDENTITY, token, IDENTITY)
    bind(store, bridge, request)
    for _ in range(3):
        result = await bridge.get_user_info(UUID, version="2")
        assert decode_account_field(UUID, result["access_token"]) == TOKEN["access_token"]
        assert result["expires_in"] == lifetime and bridge.adoption_available
    assert len(calls) == 2
    # The narrow guard does not suppress the next real renewal.
    clock[0] += lifetime * .9
    await bridge.get_user_info(UUID, version="2")
    assert len(calls) == 4 and store.load().expires_at > clock[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [None, "uncertain", "rejected", "missing_grant", "unshared", "replaced"])
async def test_adoption_readiness_keeps_expired_consent_visible_without_network(shared_account, blocked):
    store, bridge, clock = shared_account
    if blocked == "missing_grant":
        store.save(replace(store.load(), refresh_token=None))
    await bridge.set_shared(True, remember=True)
    clock[0] = 2001
    bridge = TidalDeviceAccountBridge(store)
    request, calls = responses()
    bind(store, bridge, request)
    if blocked in {"uncertain", "rejected"}:
        with store.lease() as lease:
            lease.mark_refresh_attempt(store.load(), rejected=blocked == "rejected")
    elif blocked == "unshared":
        await bridge.set_shared(False)
    elif blocked == "replaced":
        store.save(replace(store.load(), user_id="43"))
    before = {p.name: p.read_bytes() for p in store.directory.iterdir()}
    assert bridge.adoption_available is (blocked is None)
    assert before == {p.name: p.read_bytes() for p in store.directory.iterdir()}
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["uncertain", "rejected"])
async def test_failed_manual_rotation_blocks_fresh_token_waiters_and_provider_reads(shared_account, outcome):
    from fake_linkplay.tidal_playback import TidalPlaybackError, TidalPlaybackResolver
    from fake_linkplay.tidal_session import _session_fingerprint
    store, bridge, _ = shared_account
    await bridge.set_shared(True, remember=True)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def request(url, **kwargs):
        calls.append(url)
        entered.set()
        await release.wait()
        if outcome == "rejected":
            raise tidal_refresh._Failure("refresh_rejected")
        raise TimeoutError("Synthetic uncertain provider rotation")

    owner = bind(store, bridge, request)
    resolver = TidalPlaybackResolver(store, refresh_manager=owner)
    running = asyncio.create_task(owner.refresh())
    await entered.wait()
    waiting = asyncio.create_task(bridge.get_user_info(UUID, version="2"))
    await asyncio.sleep(0)
    assert not waiting.done() and bridge.adoption_available and resolver.session_available()
    release.set()
    assert (await running)["refresh_outcome"] == outcome
    with pytest.raises(TidalAccountUnavailable):
        await waiting
    assert not bridge.adoption_available and not bridge.shared and not resolver.session_available()
    assert bridge.sharing_requested  # Explicit consent remains suspended, not silently discarded.
    assert not resolver.session_matches(_session_fingerprint(store.load()))
    with pytest.raises(TidalAccountUnavailable):
        await bridge.get_user_info(UUID, version="2")
    with pytest.raises(TidalPlaybackError) as failure:
        await resolver.resolve_track("42")
    assert failure.value.code == ("refresh_rejected" if outcome == "rejected" else "refresh_result_uncertain")
    with store.lease() as lease:
        assert (await owner.refresh_owned(lease, only_if_expired=True))["refresh_outcome"] == outcome
    restarted_store = TidalSessionStore(store.directory)
    restarted = TidalDeviceAccountBridge(restarted_store)
    restarted_owner = bind(restarted_store, restarted, request)
    assert restarted.sharing_requested and not restarted.adoption_available
    assert not TidalPlaybackResolver(restarted_store, refresh_manager=restarted_owner).session_available()
    with pytest.raises(TidalAccountUnavailable):
        await restarted.get_user_info(UUID, version="2")
    assert len(calls) == 1
