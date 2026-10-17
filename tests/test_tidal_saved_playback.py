"""Offline saved-target round trips through the existing provider/load owner."""
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest

from fake_linkplay.favorites import FavoritesStore
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.queue import PlaybackQueue, QueueItem, serialize_queue_context
from fake_linkplay.queue_playlist import encode_current_playlist
from fake_linkplay.queue_protocol import explicit_queue
from fake_linkplay.tidal_playback import TidalPlaybackError
from fake_linkplay.tidal_queue import TidalQueueSelection, TidalTrackSelection
from fake_linkplay.web_playlists import prepare_source
from test_native_queue_staging import NativeBackend
from test_soap_faults import api
from test_tidal_queue_playback import configured, queue_api


REFERENCE = "https://api.tidal.com/v1/tracks/42"
DIRECT = "https://media.example.test/ordinary.wav"


def assert_private(api, persisted):
    visible = (persisted + serialize_queue_context(api.state.queue)
               + json.dumps(api.web_api.snapshot()) + json.dumps(api.web_api.favorites_snapshot()))
    for secret in ("synthetic-access", "synthetic-refresh", "SYNTHETIC_SIGNED", "audio.example.test"):
        assert secret not in visible


async def test_saved_favorite_reloads_from_disk_and_resolves_lossless_without_lan_sharing(queue_api, tmp_path):
    api = queue_api
    resolver = await configured(api, tmp_path)
    await api.tidal_account.set_shared(False)
    await api._play_tidal_track("42", "HIGH")
    assert api.state.queue.current._tidal_track == ("42", "HIGH")
    assert api.web_api.snapshot()["actions"]["save_current"]
    saved = api.favorites.add_current("Saved track", api.state.queue.current)
    assert saved["url"] == REFERENCE
    persisted = api.favorites.path.read_text()
    assert resolver.calls == [("42", "HIGH")]
    api.favorites_api.store = FavoritesStore(api.cfg)
    response = await api.favorites_api.handle_play(SimpleNamespace(
        content_length=0, headers={}, match_info={"id": saved["id"]}))
    assert response.status == 200
    assert resolver.calls == [("42", "HIGH"), ("42", "LOSSLESS")]
    assert not api.tidal_account.shared
    assert api.state.queue.current._tidal_require_shared is False
    assert api.state.current_url == REFERENCE and "/proxy/audio?url=" in api.mpv.url
    assert api.state.queue.current.resolved_url == api.state.resolved_url == ""
    assert api.favorites.path.read_text() == persisted
    assert_private(api, persisted)


async def test_exported_playlist_restores_order_selection_and_lazy_private_resolution(queue_api, tmp_path):
    api = queue_api
    resolver = await configured(api, tmp_path)
    selection = TidalQueueSelection("Saved tracks", "", tuple(
        TidalTrackSelection(value) for value in ("42", "43", "42")),
        start_index=2, request_quality="HIGH")
    await api._play_tidal_selection(selection)
    encoded = encode_current_playlist(api.state.queue)
    restored = prepare_source({"format": "json", "text": encoded.decode()})
    await api._replace_queue(restored)
    assert resolver.calls == [("43", "HIGH")]
    assert (restored.name, restored.index) == ("Saved tracks", 2)
    assert [item._tidal_track for item in restored.items] == [
        (value, "LOSSLESS") for value in ("42", "43", "42")]
    await api.tidal_account.set_shared(False)
    for index in (2, 3, 1):
        await api._play_queue_index(index, restored.name)
        assert "/proxy/audio?url=" in api.mpv.url
    assert resolver.calls == [("43", "HIGH"), ("43", "LOSSLESS"),
                              ("42", "LOSSLESS"), ("42", "LOSSLESS")]
    assert encode_current_playlist(restored) != encoded  # Current selection is now 1.
    assert_private(api, encoded.decode())


async def test_restored_tidal_successor_never_enters_native_direct_url_staging(queue_api, tmp_path):
    api = queue_api
    resolver = await configured(api, tmp_path)
    api.cfg.native_queue_staging = True
    api.mpv = NativeBackend()
    queue = explicit_queue("Mixed saved queue", [DIRECT, REFERENCE])
    await api._replace_queue(queue)
    assert resolver.calls == []
    await api._play_queue_index(1, queue.name)
    assert not any(name == "append" for name, _ in api.mpv.calls)
    await api._play_queue_index(2, queue.name)
    assert resolver.calls == [("42", "LOSSLESS")]
    assert not any(name in {"append", "load"} and value == REFERENCE for name, value in api.mpv.calls)
    assert "/proxy/audio?url=" in api.mpv.url


async def test_unowned_catalogue_url_cannot_fall_through_to_direct_audio(queue_api, tmp_path):
    api = queue_api
    resolver = await configured(api, tmp_path)
    with pytest.raises(BackendError):
        await api._play_url(REFERENCE)
    assert resolver.calls == [] and not any(name == "load" for name, _ in api.mpv.calls)


@pytest.mark.parametrize("unavailable", ["resolver", "proxy", "session", "resolution"])
async def test_saved_tidal_failure_never_loads_catalogue_endpoint(queue_api, tmp_path, unavailable):
    api = queue_api
    resolver = await configured(api, tmp_path)
    favorite = api.favorites.add({"name": "Saved track", "url": REFERENCE})
    await api._play_url(DIRECT)
    before = list(api.mpv.calls)
    if unavailable == "resolver":
        api.tidal_playback = None
    elif unavailable == "proxy":
        api.catalog_proxy_port = 0
    elif unavailable == "session":
        resolver.store.clear()
    else:
        resolver.error = TidalPlaybackError("request_failed")
    with pytest.raises(BackendError):
        await api._play_preset_snapshot(api.favorites.playback_snapshot(favorite["id"]))
    assert not any(name == "load" for name, _ in api.mpv.calls[len(before):])
    if unavailable in {"resolver", "proxy"}:
        assert api.mpv.calls == before and resolver.calls == []
        assert api.state.current_url == DIRECT


@pytest.mark.parametrize("url", [
    "http://api.tidal.com/v1/tracks/42", "https://API.TIDAL.COM/v1/tracks/42",
    "https://api.tidal.com:443/v1/tracks/42", "https://api.tidal.com./v1/tracks/42",
    "https://api.tidal.com.example.test/v1/tracks/42", "https://api.tidal.com/v1/tracks/42/",
    "https://api.tidal.com/v1/tracks/42?countryCode=DE", "https://api.tidal.com/v1/tracks/42?",
    "https://api.tidal.com/v1/tracks/42#part", "https://api.tidal.com/v1/tracks/42#",
    "https://api.tidal.com/v1/tracks/%34%32", "https://api.tidal.com/v1/tracks/042",
    "https://api.tidal.com/v1/tracks/0", "https://api.tidal.com/v1/tracks/" + "1" * 21,
    "https://api.tidal.com/v1/albums/42", "https://api.tidal.com/v1/tracks/42/playbackinfopostpaywall",
    "https://user:synthetic-secret@api.tidal.com/v1/tracks/42",
])
async def test_near_match_urls_do_not_gain_provider_ownership(queue_api, tmp_path, url):
    api = queue_api
    resolver = await configured(api, tmp_path)
    queue = PlaybackQueue(name="Ordinary URLs", items=[QueueItem(url, title="Ordinary")], index=1)
    before = asdict(queue)
    await api._replace_queue(queue)
    assert asdict(queue) == before and queue.current._tidal_track is None
    assert resolver.calls == [] and not api.mpv.calls
