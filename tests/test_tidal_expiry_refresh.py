"""Offline expired-read continuation, durable retry guard and consent boundaries."""
import asyncio
from dataclasses import replace
import os
import stat

import pytest

from fake_linkplay import tidal_playback as playback
from fake_linkplay.tidal_account import TidalDeviceAccountBridge
from fake_linkplay.tidal_refresh import TidalSessionRefreshManager
from fake_linkplay.tidal_session import TidalSessionError, TidalSessionStore, _session_fingerprint
from test_tidal_refresh import TOKEN, config, responses
from test_tidal_playback import (
    SESSION, TRACK_ID, METADATA, Response, harness, no_live_http_or_dns, stream,
)
from test_tidal_page import ALBUM, page


IDENTITY = {"userId": 42, "countryCode": "DE"}


@pytest.mark.asyncio
async def test_expired_explicit_read_rotates_once_retains_consent_and_status_is_offline(harness, monkeypatch):
    store, provider_responses, provider_calls, *_ = harness
    bridge = TidalDeviceAccountBridge(store)
    await bridge.set_shared(True)
    request, refresh_calls = responses(TOKEN, IDENTITY)
    manager = TidalSessionRefreshManager(store, config_factory=config, request_json=request,
                                         on_committed=bridge._session_refreshed)
    resolver = playback.TidalPlaybackResolver(store, refresh_manager=manager)
    monkeypatch.setattr(playback.time, "time", lambda: 2001)
    assert bridge.sharing_requested and not bridge.shared
    assert resolver.session_available() and not refresh_calls and not provider_calls
    candidate = await resolver.resolve_track(TRACK_ID)
    assert store.load().refresh_token == TOKEN["refresh_token"] and bridge.shared
    assert candidate.session_fingerprint != _session_fingerprint(SESSION)
    assert candidate.session_fingerprint == _session_fingerprint(store.load())
    assert len(refresh_calls) == 2 and len(provider_calls) == 2
    provider_responses.extend([Response(METADATA), Response(stream())])
    await resolver.resolve_track(TRACK_ID)
    assert len(refresh_calls) == 2 and len(provider_calls) == 4
    assert not (store.directory / "refresh-attempt").exists()


@pytest.mark.asyncio
async def test_uncertain_refresh_blocks_get_and_second_post_after_restart(harness):
    store, _, provider_calls, *_ = harness
    old = replace(SESSION, expires_at=999)
    store.save(old)
    request, refresh_calls = responses(OSError("synthetic-private-provider-error"))
    manager = TidalSessionRefreshManager(store, config_factory=config, request_json=request)
    resolver = playback.TidalPlaybackResolver(store, refresh_manager=manager)
    with pytest.raises(playback.TidalPlaybackError) as caught:
        await resolver.resolve_track(TRACK_ID)
    assert caught.value.code == "refresh_result_uncertain"
    assert store.load() == old and len(refresh_calls) == 1 and not provider_calls
    restarted_store = TidalSessionStore(store.directory)
    restarted = TidalSessionRefreshManager(restarted_store, config_factory=config, request_json=request)
    resolver = playback.TidalPlaybackResolver(restarted_store, refresh_manager=restarted)
    assert not resolver.session_available()
    with pytest.raises(playback.TidalPlaybackError) as caught:
        await resolver.resolve_track(TRACK_ID)
    assert caught.value.code == "refresh_result_uncertain"
    assert len(refresh_calls) == 1 and not provider_calls


@pytest.mark.asyncio
async def test_concurrent_owner_is_not_reentered_and_later_read_uses_rotated_session(harness):
    store, _, provider_calls, *_ = harness
    store.save(replace(SESSION, expires_at=999))
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def request(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            entered.set()
            await release.wait()
            return TOKEN
        return IDENTITY

    manager = TidalSessionRefreshManager(store, config_factory=config, request_json=request)
    resolver = playback.TidalPlaybackResolver(store, refresh_manager=manager)
    running = asyncio.create_task(manager.refresh())
    await entered.wait()
    with pytest.raises(playback.TidalPlaybackError) as caught:
        await resolver.resolve_track(TRACK_ID)
    assert caught.value.code == "storage_unavailable" and not provider_calls
    release.set()
    assert (await running)["state"] == "refreshed"
    await resolver.resolve_track(TRACK_ID)
    assert len(calls) == 2 and len(provider_calls) == 2


@pytest.mark.asyncio
async def test_unshare_during_refresh_cannot_be_reenabled_by_commit(harness, monkeypatch):
    store, _, _, *_ = harness
    bridge = TidalDeviceAccountBridge(store)
    await bridge.set_shared(True)
    monkeypatch.setattr(playback.time, "time", lambda: 2001)
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(url, **kwargs):
        if "data" in kwargs:
            entered.set()
            await release.wait()
            return TOKEN
        return IDENTITY

    manager = TidalSessionRefreshManager(store, config_factory=config, request_json=request,
                                         on_committed=bridge._session_refreshed)
    running = asyncio.create_task(manager.refresh())
    await entered.wait()
    await bridge.set_shared(False)
    release.set()
    assert (await running)["state"] == "refreshed"
    assert not bridge.sharing_requested and not bridge.shared


@pytest.mark.asyncio
async def test_invalid_grant_stays_rejected_across_restart_without_provider_error_text(harness):
    store, provider_responses, calls, *_ = harness
    provider_responses[:] = [Response({"error": "invalid_grant",
                                      "error_description": "synthetic-private-rejection"}, status=400)]
    result = await TidalSessionRefreshManager(store, config_factory=config).refresh()
    assert result["error"] == "refresh_rejected" and result["refresh_outcome"] == "rejected"
    assert "synthetic" not in str(result) and store.load() == SESSION
    result = await TidalSessionRefreshManager(TidalSessionStore(store.directory),
                                               config_factory=config).refresh()
    assert result["error"] == "refresh_rejected" and result["refresh_outcome"] == "rejected"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_one_expired_page_refreshes_but_wrong_country_never_starts_refresh(harness):
    store, provider_responses, calls, *_ = harness
    store.save(replace(SESSION, expires_at=999))
    request, refresh_calls = responses(TOKEN, IDENTITY)
    manager = TidalSessionRefreshManager(store, config_factory=config, request_json=request)
    resolver = playback.TidalPlaybackResolver(store, refresh_manager=manager)
    with pytest.raises(playback.TidalPlaybackError) as caught:
        await resolver.resolve_page(ALBUM + "?countryCode=US")
    assert caught.value.code == "invalid_page_selection" and not refresh_calls and not calls
    provider_responses[:] = [Response(page())]
    candidate = await resolver.resolve_page(ALBUM + "?offset=20&limit=3")
    assert candidate.track_ids == ("42", "43", "42") and len(calls) == 1 and len(refresh_calls) == 2


@pytest.mark.asyncio
async def test_401_with_refresh_owner_never_attempts_refresh(harness):
    store, provider_responses, calls, *_ = harness
    provider_responses[:] = [Response({"error": "synthetic-error"}, status=401)]
    request, refresh_calls = responses()
    manager = TidalSessionRefreshManager(store, config_factory=config, request_json=request)
    with pytest.raises(playback.TidalPlaybackError) as caught:
        await playback.TidalPlaybackResolver(store, refresh_manager=manager).resolve_track(TRACK_ID)
    assert caught.value.code == "unauthorized" and len(calls) == 1 and not refresh_calls
    assert not (store.directory / "refresh-attempt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_identical_success_is_saved_and_marker_cleanup_failure_stays_safe(harness, monkeypatch, cleanup_failure):
    from fake_linkplay.tidal_session import _TidalSessionLease
    store, _, _, *_ = harness
    value = {**TOKEN, "access_token": SESSION.access_token, "refresh_token": SESSION.refresh_token,
             "expires_in": 1000}
    request, calls = responses(value, IDENTITY)
    if cleanup_failure:
        def fail_cleanup(self):
            raise TidalSessionError("synthetic-cleanup-error")
        monkeypatch.setattr(_TidalSessionLease, "clear_refresh_attempt", fail_cleanup)
    result = await TidalSessionRefreshManager(store, config_factory=config, request_json=request).refresh()
    assert result["session_saved"] and result["refresh_outcome"] == "saved" and store.load() == SESSION
    assert len(calls) == 2 and result["state"] == ("error" if cleanup_failure else "refreshed")
    assert store.refresh_outcome(SESSION) == ("uncertain" if cleanup_failure else None)


def test_refresh_marker_is_private_full_record_bound_and_contains_no_credentials(tmp_path):
    store = TidalSessionStore(tmp_path / "private")
    store.save(SESSION)
    with store.lease() as lease:
        lease.mark_refresh_attempt(SESSION)
        assert lease.refresh_outcome(SESSION) == "uncertain"
    marker = store.directory / "refresh-attempt"
    value = marker.read_bytes()
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600 and len(value) == 75
    assert SESSION.access_token.encode() not in value and SESSION.refresh_token.encode() not in value
    for change in ({"access_token": "synthetic-new"}, {"refresh_token": "synthetic-new"},
                   {"expires_at": 3000}, {"user_id": "43"}, {"country_code": "US"},
                   {"client_id": "other-client"}, {"client_id": None, "auth_flow": None}):
        assert store.refresh_outcome(replace(SESSION, **change)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "permissions"])
async def test_unsafe_refresh_marker_blocks_before_post(tmp_path, unsafe):
    store = TidalSessionStore(tmp_path / "private")
    store.save(SESSION)
    marker = store.directory / "refresh-attempt"
    other = tmp_path / "other"
    other.write_bytes(b"synthetic-private-other")
    other.chmod(0o600)
    if unsafe == "symlink":
        marker.symlink_to(other)
    elif unsafe == "hardlink":
        os.link(other, marker)
    else:
        marker.write_bytes(b"synthetic-private-marker")
        marker.chmod(0o644)
    request, calls = responses()
    result = await TidalSessionRefreshManager(store, config_factory=config,
                                              request_json=request).refresh()
    assert result["error"] == "storage_unavailable" and not calls
    assert store.load() == SESSION and other.read_bytes() == b"synthetic-private-other"


@pytest.mark.asyncio
async def test_near_expiry_refreshes_before_a_two_request_track_read(harness, monkeypatch):
    store, provider_responses, provider_calls, *_ = harness
    clock = [1999.0]
    monkeypatch.setattr(playback.time, "time", lambda: clock[0])
    # Metadata arriving after the original expiry must not strand the playback
    # request or require a second click. Renewal precedes either provider GET.
    provider_responses[0].on_enter = lambda: clock.__setitem__(0, 2001.0)
    request, refresh_calls = responses(TOKEN, IDENTITY)
    owner = TidalSessionRefreshManager(store, config_factory=config, request_json=request)
    candidate = await playback.TidalPlaybackResolver(store, refresh_manager=owner).resolve_track(TRACK_ID)
    assert candidate.session_fingerprint == _session_fingerprint(store.load())
    assert len(refresh_calls) == 2 and len(provider_calls) == 2
    assert all(call[2]["headers"]["Authorization"] == "Bearer " + TOKEN["access_token"]
               for call in provider_calls)
