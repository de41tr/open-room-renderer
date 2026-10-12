"""Owned favorites: durable library, real HTTP/client, no audio/media network."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import json
import gzip
import os

import pytest

from fake_linkplay import favorites, persistence
from fake_linkplay.capabilities import configured_presets
from fake_linkplay.config import Config, ExperimentalSource
from fake_linkplay.favorites import (FavoriteError, FavoritesStore, MAX_FAVORITES,
                                     MAX_FAVORITES_BYTES, MAX_REQUEST_BYTES)
from fake_linkplay.http_api import HttpApi, ParsedCommand
from fake_linkplay.persistence import AtomicWriteCommittedError
from fake_linkplay.queue import PlaybackQueue, QueueItem
from fake_linkplay.state import RendererState
from test_music_presets import FIRST, SECOND, FakeAudioBackend, configured, running

BASE = "/orr/v1/favorites"
EXACT = "https://media.example.test/音楽/caf%C3%A9%2Fone.m3u?signature=a%26b%3Dc&literal=%252F&x=one+two"
LABEL = "Été & 日本語 🎵"


def test_managed_store_restart_prefix_order_stable_ids_and_exact_text(tmp_path):
    cfg = configured(tmp_path, source_experiment_variant="preset", experimental_sources=[
        ExperimentalSource("demo", "Demo", url=FIRST)])
    original = deepcopy(configured_presets(cfg))
    store = FavoritesStore(cfg)
    assert not store.path.exists()
    first = store.add({"name": LABEL, "url": EXACT, "title": "原曲 <live>",
                       "artist": "A & B", "album": "Été", "artwork": SECOND})
    second = store.add({"name": "Second managed", "url": SECOND})
    assert first["slot"] == 4 and second["slot"] == 5
    assert first["editable"] is True
    assert store.effective_presets()[:3] == original
    assert [entry["editable"] for entry in store.document()["favorites"]] == [False] * 3 + [True] * 2
    assert configured_presets(cfg) == original
    assert store.rename(first["id"], "Renamed")["id"] == first["id"]
    store.delete(first["id"])
    remaining = store.document()["favorites"][-1]
    assert remaining["id"] == second["id"] and remaining["slot"] == 4
    assert FavoritesStore(cfg).document() == store.document()
    document = store.document()
    document["favorites"][-1]["url"] = "https://wrong.example.test/"
    assert store.document()["favorites"][-1]["url"] == SECOND
    assert json.loads(store.path.read_text())["version"] == 1


def test_unicode_and_url_bytes_survive_persistence_without_normalization(tmp_path):
    cfg = Config(state_dir=str(tmp_path))
    record = FavoritesStore(cfg).add({"name": LABEL, "url": EXACT})
    restored = FavoritesStore(cfg).document()["favorites"][0]
    assert restored == record
    assert restored["url"].encode() == EXACT.encode()
    assert LABEL in (tmp_path / "favorites.json").read_text()


def test_concurrent_mutations_keep_all_acknowledged_writes_and_valid_restart(tmp_path):
    cfg = configured(tmp_path)
    store = FavoritesStore(cfg)
    with ThreadPoolExecutor(max_workers=8) as executor:
        added = list(executor.map(lambda index: store.add({"name": f"N{index}", "url": FIRST}), range(24)))
        renamed = list(executor.map(lambda entry: store.rename(entry["id"], "Renamed " + entry["name"]), added))
        list(executor.map(lambda entry: store.delete(entry["id"]), added[::2]))
    assert len({record["id"] for record in added}) == 24
    assert all(record["name"].startswith("Renamed ") for record in renamed)
    actual = store.document()["favorites"][2:]
    assert {record["id"] for record in actual} == {record["id"] for record in added[1::2]}
    assert [record["slot"] for record in actual] == list(range(3, 15))
    assert FavoritesStore(cfg).document() == store.document()


@pytest.mark.parametrize("operation", ["add", "rename", "delete"])
@pytest.mark.parametrize("after_replace", [False, True])
def test_atomic_failure_publishes_only_committed_snapshot(tmp_path, monkeypatch, operation, after_replace):
    cfg = configured(tmp_path)
    store = FavoritesStore(cfg)
    original = store.add({"name": LABEL, "url": EXACT})
    before = store.document()
    fsync = os.fsync
    calls = 0
    def fail_fsync(descriptor):
        nonlocal calls
        calls += 1
        if calls == (2 if after_replace else 1):
            raise OSError("synthetic filesystem failure with private contents")
        return fsync(descriptor)
    monkeypatch.setattr(persistence.os, "fsync", fail_fsync)
    with pytest.raises(AtomicWriteCommittedError if after_replace else OSError):
        if operation == "add":
            store.add({"name": "New", "url": SECOND})
        elif operation == "rename":
            store.rename(original["id"], "New label")
        else:
            store.delete(original["id"])
    assert FavoritesStore(cfg).document() == store.document()
    assert (store.document() != before) is after_replace
    assert not list(tmp_path.glob(".favorites.json.*"))


def test_replace_failure_keeps_disk_and_memory_and_removes_temporary(tmp_path, monkeypatch):
    store = FavoritesStore(configured(tmp_path))
    original = store.add({"name": "Original", "url": FIRST})
    before = store.path.read_bytes()
    def fail(*_args):
        raise OSError("replace failure")
    monkeypatch.setattr(persistence.os, "replace", fail)
    with pytest.raises(OSError):
        store.delete(original["id"])
    assert store.path.read_bytes() == before
    assert store.document()["favorites"][-1] == original
    assert not list(tmp_path.glob(".favorites.json.*"))


VALID = {"id": "a" * 32, "name": "Station", "url": FIRST}
BAD_DOCUMENTS = [
    b'{"version":1,"version":1,"favorites":[]}',
    b'{"version":1,"favorites":[{"id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","name":"A","name":"B","url":"https://media.example.test/a"}]}',
    b'{"version":true,"favorites":[]}', b'{"version":2,"favorites":[]}',
    b'{"version":1,"favorites":[],"ignored":true}', b'[]', b'null',
    b'{"version":1,"favorites":null}', b'{"version":1,"favorites":[true]}',
    b'{"version":1,"favorites":[NaN]}', b'{"version":1,', b'\xff',
    json.dumps({"version": 1, "favorites": [VALID, VALID]}).encode(),
    json.dumps({"version": 1, "favorites": [{**VALID, "id": "BAD"}]}).encode(),
    json.dumps({"version": 1, "favorites": [{**VALID, "name": "bad\ud800"}]}).encode(),
    json.dumps({"version": 1, "favorites": [{**VALID, "name": "bad\x00"}]}).encode(),
    json.dumps({"version": 1, "favorites": [{**VALID, "artist": 10}]}).encode(),
    json.dumps({"version": 1, "favorites": [{**VALID, "metadata": "account"}]}).encode(),
    json.dumps({"version": 1, "favorites": [VALID] * (MAX_FAVORITES + 1)}).encode(),
    b' ' * (MAX_FAVORITES_BYTES + 1),
]


@pytest.mark.parametrize("encoded", BAD_DOCUMENTS, ids=range(len(BAD_DOCUMENTS)))
def test_corrupt_startup_refuses_and_never_silently_replaces(tmp_path, encoded):
    path = tmp_path / "favorites.json"
    path.write_bytes(encoded)
    cfg = configured(tmp_path)
    api = HttpApi(cfg, RendererState.from_config(cfg), FakeAudioBackend())
    assert api.state.effective_presets() == configured_presets(cfg)
    assert api.state.as_status_ex("127.0.0.1")["preset_key"] == "2"
    for action in (api.favorites.document,
                   lambda: api.favorites.add({"name": "New", "url": FIRST}),
                   lambda: api.favorites.rename("a" * 32, "Changed"),
                   lambda: api.favorites.delete("a" * 32)):
        with pytest.raises(FavoriteError) as raised:
            action()
        assert raised.value.status == 503
    assert path.read_bytes() == encoded


@pytest.mark.parametrize("payload", [
    {}, [], None, True, {"name": "N"}, {"url": FIRST},
    {"name": "N", "url": FIRST, "source": "account"},
    {"name": "N", "url": FIRST, "id": "a" * 32},
    {"name": "", "url": FIRST}, {"name": " \n", "url": FIRST},
    {"name": 1, "url": FIRST}, {"name": "N", "url": True},
    {"name": "x" * 257, "url": FIRST}, {"name": "é" * 129, "url": FIRST},
    {"name": "N", "url": FIRST, "title": "x" * 1025},
    {"name": "N", "url": FIRST, "artist": None},
    {"name": "N", "url": FIRST, "album": "invalid\x00"},
    {"name": "N", "url": FIRST, "artwork": "javascript:bad"},
    *({"name": "N", "url": value} for value in ["", " ", "relative", "file:///tmp/a.wav",
       "ftp://media.example.test/a", "https:///missing", "https://user:secret@media.example.test/a",
       "https://@media.example.test/a", "https://media.example.test:70000/a",
       "https://media.example.test:0/a", "https://media.example.test/a b",
       "https://media.example.test/\x00", "https://media.example.test/\ud800",
       "http://127.0.0.1:8080/proxy/audio?url=opaque",
       "http://127.0.0.1:8080/%70roxy/audio?url=opaque",
       "http://127.0.0.1:8080/a/../proxy/audio?url=opaque",
       "http://127.0.0.1:8080/a/%2e%2e/proxy%2faudio?url=opaque",
       "https://media.example.test/" + "a" * 8192]),
])
def test_invalid_add_rejected_without_creating_store(tmp_path, payload):
    store = FavoritesStore(configured(tmp_path))
    before = store.document()
    with pytest.raises(FavoriteError):
        store.add(payload)
    assert store.document() == before and not store.path.exists()


def test_entry_and_serialized_size_limits_do_not_discard_previous_library(tmp_path, monkeypatch):
    store = FavoritesStore(configured(tmp_path))
    monkeypatch.setattr(favorites, "MAX_FAVORITES", 2)
    for index in range(2):
        store.add({"name": str(index), "url": FIRST})
    before = store.path.read_bytes()
    with pytest.raises(FavoriteError, match="entry limit"):
        store.add({"name": "Overflow", "url": FIRST})
    monkeypatch.setattr(favorites, "MAX_FAVORITES_BYTES", len(before))
    with pytest.raises(FavoriteError, match="size limit"):
        store.rename(store.document()["favorites"][-1]["id"], "Longer name")
    assert store.path.read_bytes() == before


@pytest.mark.asyncio
async def test_http_mutations_leave_every_playback_field_and_call_unchanged(tmp_path):
    async with running(configured(tmp_path)) as (api, backend, server, session):
        await api.execute(ParsedCommand("MCUKeyShortClick:1"), "127.0.0.1")
        before, calls = asdict(api.state), list(backend.calls)
        queue, item = api.state.queue, api.state.queue.current
        async with session.post(server.make_url(BASE), json={"name": LABEL, "url": EXACT}) as response:
            assert response.status == 201
            record = (await response.json())["favorite"]
            assert not any(name.lower().startswith("access-control-") for name in response.headers)
        async with session.patch(server.make_url(BASE + "/" + record["id"]), json={"name": "Rename"}) as response:
            assert response.status == 200
        async with session.delete(server.make_url(BASE + "/" + record["id"])) as response:
            assert response.status == 200
        assert asdict(api.state) == before and backend.calls == calls
        assert api.state.queue is queue and api.state.queue.current is item
        assert api.state.as_status_ex("127.0.0.1")["preset_key"] == "2"


@pytest.mark.asyncio
async def test_save_current_uses_canonical_container_and_only_display_fields(tmp_path):
    async with running(configured(tmp_path)) as (api, backend, server, session):
        item = QueueItem(url=EXACT, title=LABEL, artist="Artist", album="Album", artwork=SECOND,
                         id="provider-account-id", metadata="secret provider account state",
                         source="Provider", resolved_url="https://media.example.test/temporary-child")
        api.state.queue = PlaybackQueue(items=[item], index=1)
        api.state.current_url = EXACT
        api.state.resolved_url = item.resolved_url
        api.state.backend_url = "http://127.0.0.1:8080/proxy/audio?url=temporary"
        api.state.backend_original_url = "https://media.example.test/expanded-child"
        before = asdict(api.state)
        async with session.post(server.make_url(BASE + "/current"), json={"name": "Saved container"}) as response:
            assert response.status == 201
            saved = (await response.json())["favorite"]
        assert {key: saved[key] for key in ("url", "title", "artist", "album", "artwork")} == {
            "url": EXACT, "title": LABEL, "artist": "Artist", "album": "Album", "artwork": SECOND}
        assert asdict(api.state) == before and not backend.calls
        stored = (tmp_path / "favorites.json").read_text()
        assert "temporary" not in stored and "expanded-child" not in stored and "provider" not in stored


@pytest.mark.asyncio
async def test_save_current_missing_and_unresolved_provider_have_no_side_effects(tmp_path):
    async with running(configured(tmp_path)) as (api, backend, server, session):
        for queue in (PlaybackQueue(), PlaybackQueue(source="Plex", search_url="opaque-account-search")):
            api.state.queue = queue
            async with session.post(server.make_url(BASE + "/current"), json={"name": "Saved"}) as response:
                assert response.status == 409
        item = QueueItem(url=FIRST)
        item._radio_station = (FIRST, "s1234")
        api.state.queue = PlaybackQueue(items=[item], index=1)
        async with session.post(server.make_url(BASE + "/current"), json={"name": "Saved"}) as response:
            assert response.status == 409
        assert not (tmp_path / "favorites.json").exists() and not backend.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("body,headers,status", [
    (b'{"name":"A","name":"B","url":"https://media.example.test/a"}', {"Content-Type": "application/json"}, 400),
    (b'null', {"Content-Type": "application/json"}, 400),
    (b'{"name":"N","url":NaN}', {"Content-Type": "application/json"}, 400),
    (b'\xff', {"Content-Type": "application/json"}, 400),
    (b'{}', {"Content-Type": "text/plain"}, 415),
    (b'{}', {"Content-Type": "application/json; charset=latin1"}, 415),
    (b' ' * (MAX_REQUEST_BYTES + 1), {"Content-Type": "application/json"}, 413),
])
async def test_http_json_body_is_bounded_and_strict(tmp_path, body, headers, status):
    async with running(configured(tmp_path)) as (api, backend, server, session):
        async with session.post(server.make_url(BASE), data=body, headers=headers) as response:
            assert response.status == status
            assert "error" in await response.json()
        assert not api.favorites.path.exists() and not backend.calls


@pytest.mark.asyncio
async def test_chunked_body_is_rejected_and_configured_edits_are_read_only(tmp_path):
    async with running(configured(tmp_path)) as (api, backend, server, session):
        async def body():
            yield b'{}'
        async with session.post(server.make_url(BASE), data=body(), headers={"Content-Type": "application/json"}) as response:
            assert response.status == 411
        for method, data in ((session.patch, {"name": "Changed"}), (session.delete, None)):
            async with method(server.make_url(BASE + "/configured:1"), **({"json": data} if data else {})) as response:
                assert response.status == 403
        async with session.patch(server.make_url(BASE + "/" + "a" * 32), json={"name": "Unknown"}) as response:
            assert response.status == 404
        async with session.delete(server.make_url(BASE + "/invalid")) as response:
            assert response.status == 400
        assert not api.favorites.path.exists() and not backend.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_http_persistence_error_reports_commit_outcome_and_memory_matches_file(tmp_path, monkeypatch, committed):
    async with running(configured(tmp_path)) as (api, backend, server, session):
        original = favorites.atomic_write_text
        def fail(path, encoded):
            if committed:
                original(path, encoded)
                raise AtomicWriteCommittedError("synthetic private failure")
            raise OSError("synthetic private failure")
        monkeypatch.setattr(favorites, "atomic_write_text", fail)
        async with session.post(server.make_url(BASE), json={"name": LABEL, "url": EXACT}) as response:
            assert response.status == 503
            result = await response.json()
            assert result["committed"] is committed
            assert "private failure" not in json.dumps(result)
        assert api.favorites.document() == FavoritesStore(api.cfg).document()
        assert api.state.as_status_ex("127.0.0.1")["preset_key"] == str(2 + committed)
        assert not backend.calls


@pytest.mark.asyncio
async def test_concurrent_http_adds_and_real_pywiim_listing_play_rename_delete(tmp_path):
    pywiim = pytest.importorskip("pywiim")
    async with running(configured(tmp_path)) as (api, backend, server, session):
        async def add(index):
            async with session.post(server.make_url(BASE), json={"name": f"Managed {index}", "url": EXACT}) as response:
                assert response.status == 201
                return (await response.json())["favorite"]
        added = await asyncio.gather(*(add(index) for index in range(8)))
        assert len({record["id"] for record in added}) == 8
        assert sorted(record["slot"] for record in added) == list(range(3, 11))
        client = pywiim.WiiMClient(host="127.0.0.1", port=server.port, protocol="http", timeout=5,
                                 session=session, capabilities={"retry_count": 1, "supports_metadata": False,
                                                               "supports_presets": True})
        try:
            await client.get_status()
            assert await client.get_max_preset_slots() == 10
            presets = await client.get_presets()
            assert [record["url"] for record in presets] == [FIRST, SECOND] + [EXACT] * 8
            await client.play_preset(4, index=1)
            assert backend.url == EXACT
            assert api.state.queue.current.id == "preset:4"
            before, calls = asdict(api.state), list(backend.calls)
            selected = next(record for record in added if record["slot"] == 4)
            lower = next(record for record in added if record["slot"] == 3)
            async with session.patch(server.make_url(BASE + "/" + selected["id"]), json={"name": LABEL}) as response:
                assert response.status == 200
            async with session.delete(server.make_url(BASE + "/" + lower["id"])) as response:
                assert response.status == 200
            assert asdict(api.state) == before and backend.calls == calls
            assert api.state.as_status_ex("127.0.0.1")["preset_key"] == "9"
            presets = await client.get_presets()
            assert len(presets) == 9 and presets[2]["name"] == LABEL
            assert presets[2]["id"] == selected["id"]
            await client.play_preset(3)
            assert backend.url == EXACT and api.state.title == LABEL
            assert FavoritesStore(api.cfg).document() == api.favorites.document()
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_corrupt_library_preserves_configured_playback_but_managed_http_is_unavailable(tmp_path):
    path = tmp_path / "favorites.json"
    path.write_bytes(b'{"version":1,"favorites":')
    before = path.read_bytes()
    async with running(configured(tmp_path)) as (api, backend, server, session):
        async with session.get(server.make_url(BASE)) as response:
            assert response.status == 503
            assert (await response.json())["error"] == "library_unavailable"
        async with session.post(server.make_url(BASE), json={"name": "New", "url": FIRST}) as response:
            assert response.status == 503
        await api.execute(ParsedCommand("MCUKeyShortClick:2"), "127.0.0.1")
        assert backend.url == SECOND
        assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_compressed_request_cannot_hide_invalid_trailing_json(tmp_path):
    body = json.dumps({"name": "Compressed", "url": FIRST}).encode() + b" " * 100000 + b" NOT JSON"
    encoded = gzip.compress(body)
    assert len(encoded) < MAX_REQUEST_BYTES < len(body)
    async with running(configured(tmp_path)) as (api, backend, server, session):
        async with session.post(server.make_url(BASE), data=encoded,
                headers={"Content-Type": "application/json", "Content-Encoding": "gzip"}) as response:
            assert response.status == 415
            assert (await response.json())["error"] == "unsupported_content_encoding"
        assert not api.favorites.path.exists() and not backend.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("encodings", [("identity", "gzip"), ("gzip", "identity"), ("identity", "identity")])
async def test_duplicate_content_encoding_headers_cannot_bypass_body_validation(tmp_path, encodings):
    body = json.dumps({"name": "Compressed", "url": FIRST}).encode() + b" " * 100000 + b" NOT JSON"
    async with running(configured(tmp_path)) as (api, backend, server, session):
        async with session.post(server.make_url(BASE), data=gzip.compress(body),
                headers=[("Content-Type", "application/json"),
                         *(("Content-Encoding", value) for value in encodings)]) as response:
            assert response.status == 415
            assert (await response.json())["error"] == "unsupported_content_encoding"
        assert not api.favorites.path.exists() and not backend.calls
