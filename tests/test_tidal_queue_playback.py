"""Synthetic single-track bridge through real queue/load ownership; no provider/audio proof."""
import asyncio
from html import escape
import json
import time
from types import SimpleNamespace

import pytest

from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.tidal_account import TidalDeviceAccountBridge
from fake_linkplay.tidal_session import TidalSession, TidalSessionStore
from fake_linkplay.upnp_description import avtransport_response_fields
from fake_linkplay.queue import serialize_queue_context
from test_soap_faults import api, request


class Resolver:
    def __init__(self):
        self.calls = []
        self.wait = None
    def session_matches(self, fingerprint):
        return fingerprint == b"synthetic-session-binding" and self.store.load() == self.record
    async def resolve_track(self, track_id, quality="HIGH"):
        self.calls.append((track_id, quality))
        if self.wait is not None:
            await self.wait.wait()
        return SimpleNamespace(track_id=track_id, catalogue_url="https://api.tidal.com/v1/tracks/42",
                               title="Synthetic track", artist="Synthetic artist", album="Synthetic album",
                               duration=180.0, session_fingerprint=b"synthetic-session-binding", session_expires_at=time.time()+600, content_type="audio/flac", stream_url="https://audio.example.test/media?token=SYNTHETIC_SIGNED")


async def configured(api, tmp_path):
    store = TidalSessionStore(tmp_path / "private-playback")
    store.save(TidalSession("synthetic-access", "synthetic-refresh", time.time()+600, "42", "DE",
                            client_id="synthetic-client", auth_flow="tidalapi-device-v0.8.11"))
    api.tidal_account = TidalDeviceAccountBridge(store)
    await api.tidal_account.set_shared(True)
    api.tidal_playback = Resolver()
    api.tidal_playback.store, api.tidal_playback.record = store, store.load()
    return api.tidal_playback


@pytest.mark.asyncio
async def test_single_tidal_create_queue_starts_existing_player_without_public_signed_url(api, tmp_path):
    resolver = await configured(api, tmp_path)
    context = '<PlayList><ListName>CurrentQueue</ListName><ListInfo><SourceName>Tidal</SourceName><TrackNumber>1</TrackNumber><StartPlayIndex>1</StartPlayIndex></ListInfo><Tracks><Track1><Id>42</Id></Track1></Tracks></PlayList>'
    response = await api.soap(request("CreateQueue", '<QueueContext>'+escape(context)+'</QueueContext>', service="PlayQueue"))
    assert response.status == 200
    assert resolver.calls == [("42", "AUTO")]
    assert any(name == "load" and "/proxy/audio?url=" in value for name, value in api.mpv.calls)
    assert api.state.queue.source == "Tidal" and api.state.status == "play"
    assert api.state.queue.current._tidal_stream_url.endswith("SYNTHETIC_SIGNED")
    assert api._proxy_source_allowed(api.state.queue.current._tidal_stream_url)
    public = json.dumps(api.state.as_status_ex("127.0.0.1")) + serialize_queue_context(api.state.queue)
    public += avtransport_response_fields("GetPositionInfo", api.state)
    assert "SYNTHETIC_SIGNED" not in public and "audio.example.test" not in public
    assert "Tidal" not in api.state.as_status_ex("127.0.0.1")["supported_services"]


@pytest.mark.asyncio
async def test_resolution_cannot_replace_a_newer_command_or_unshared_account(api, tmp_path):
    resolver = await configured(api, tmp_path)
    resolver.wait = asyncio.Event()
    previous = api.state.queue
    task = asyncio.create_task(api._play_tidal_track("42"))
    await asyncio.sleep(0)
    api._accept_playback_command("stop")
    resolver.wait.set()
    with pytest.raises(BackendError):
        await task
    assert api.state.queue is previous and not api.mpv.calls
    await api.tidal_account.set_shared(False)
    with pytest.raises(BackendError):
        await api._play_tidal_track("42", require_shared=True)
    assert len(resolver.calls) == 1


@pytest.mark.asyncio
async def test_tidal_proxy_ignores_lan_allowlist_and_rejects_redirects(api, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, Mock
    import fake_linkplay.http_api as module
    from fake_linkplay.network_policy import NetworkPolicyError, ProxyTarget
    await configured(api, tmp_path)
    await api.tidal_account.set_shared(False)
    await api._play_tidal_track("42")
    assert not api.tidal_account.shared
    item = api.state.queue.current
    api.cfg.proxy_allowed_lan_hosts = ("127.0.0.1",)
    item._tidal_stream_url = "https://127.0.0.1/private"
    with pytest.raises(NetworkPolicyError):
        await api._open_proxy_source(item._tidal_stream_url, {})
    item._tidal_stream_url = "https://audio.example.test/media?token=SYNTHETIC_SIGNED"
    target = ProxyTarget(item._tidal_stream_url, "audio.example.test", 443, ("1.1.1.1",))
    resolve = AsyncMock(return_value=target)
    monkeypatch.setattr(module, "resolve_proxy_target", resolve)
    monkeypatch.setattr(module, "TCPConnector", lambda **kwargs: None)
    upstream = SimpleNamespace(status=302, headers={"Location": "https://other.example.test/"}, release=Mock())
    client = SimpleNamespace(get=AsyncMock(return_value=upstream), close=AsyncMock())
    factory = Mock(return_value=client)
    monkeypatch.setattr(module, "ClientSession", factory)
    with pytest.raises(NetworkPolicyError):
        await api._open_proxy_source(item._tidal_stream_url, {"User-Agent": "OpenRoomRenderer/0.1"})
    resolve.assert_awaited_once_with(item._tidal_stream_url, ())
    client.get.assert_awaited_once_with(item._tidal_stream_url, allow_redirects=False)
    assert "Authorization" not in factory.call_args.kwargs["headers"]
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_cleared_while_backend_lock_waits_prevents_new_load(api, tmp_path):
    resolver = await configured(api, tmp_path)
    await api.tidal_account.set_shared(False)
    await api._backend_lock.acquire()
    task = asyncio.create_task(api._play_tidal_track("42"))
    await asyncio.sleep(0)
    resolver.store.clear()
    api._backend_lock.release()
    with pytest.raises(BackendError):
        await task
    assert not any(name == "load" for name, _ in api.mpv.calls)
