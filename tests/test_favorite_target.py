"""Managed target repairs: local library, real HTTP and detached playback."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import gzip
import json
import os

import pytest
from fake_linkplay import favorites, persistence
from fake_linkplay.capabilities import configured_presets
from fake_linkplay.favorites import FavoriteError, FavoritesStore, MAX_REQUEST_BYTES
from fake_linkplay.persistence import AtomicWriteCommittedError
from test_music_presets import FIRST, SECOND, configured, running
from test_native_queue_staging import make_api, start

BASE = "/orr/v1/favorites"
EXACT = "https://media.example.test/音楽/caf%C3%A9%2Fone.wav?signature=a%26b%3Dc&literal=%252F&x=one+two"
DISPLAY = {"title": "Été 曲", "artist": "Artist & another", "album": "Album", "artwork": SECOND}


def test_target_keep_clear_order_identity_restart_and_noop(tmp_path, monkeypatch):
    cfg = configured(tmp_path)
    store = FavoritesStore(cfg)
    first = store.add({"name": "First", "url": FIRST})
    chosen = store.add({"name": "Library name", "url": FIRST, **DISPLAY})
    last = store.add({"name": "Last", "url": SECOND})
    before = store.document()
    snapshot = store.playback_snapshot(chosen["id"])
    changed = store.set_url(chosen["id"], EXACT, "keep")
    assert changed == {**chosen, "url": EXACT}
    assert store.document()["favorites"] == [*before["favorites"][:3], changed, last]
    assert store.effective_presets()[:2] == configured_presets(cfg)
    assert snapshot["url"] == FIRST and snapshot["title"] == DISPLAY["title"]
    assert FavoritesStore(cfg).document() == store.document()
    assert store.path.read_text().count(EXACT) == 1
    assert set(json.loads(store.path.read_bytes())["favorites"][1]) == {
        "id", "name", "url", "title", "artist", "album", "artwork"}

    original = favorites.atomic_write_text
    monkeypatch.setattr(favorites, "atomic_write_text", lambda *_: pytest.fail("no-op wrote the library"))
    assert store.set_url(chosen["id"], EXACT, "keep") == changed
    monkeypatch.setattr(favorites, "atomic_write_text", original)
    cleared = store.set_url(chosen["id"], EXACT, "clear")
    assert cleared == {**changed, **dict.fromkeys(DISPLAY, "")}
    assert (cleared["id"], cleared["slot"], cleared["name"]) == (chosen["id"], 4, "Library name")
    assert store.document()["favorites"][2] == first
    assert FavoritesStore(cfg).document() == store.document()
    monkeypatch.setattr(favorites, "atomic_write_text", lambda *_: pytest.fail("no-op wrote the library"))
    assert store.set_url(chosen["id"], EXACT, "clear") == cleared
    assert store.set_url(chosen["id"], EXACT, "keep") == cleared


def test_concurrent_rename_and_target_edits_share_the_store_lock(tmp_path):
    store = FavoritesStore(configured(tmp_path))
    chosen = store.add({"name": "Original", "url": FIRST, **DISPLAY})
    with ThreadPoolExecutor(max_workers=2) as pool:
        rename = pool.submit(store.rename, chosen["id"], "Renamed")
        edit = pool.submit(store.set_url, chosen["id"], SECOND, "clear")
        rename.result(timeout=2)
        edit.result(timeout=2)
    assert store.document()["favorites"][-1] == {
        **chosen, "name": "Renamed", "url": SECOND, **dict.fromkeys(DISPLAY, "")}
    assert FavoritesStore(store.cfg).document() == store.document()


def test_target_edit_respects_serialized_library_limit(tmp_path, monkeypatch):
    store = FavoritesStore(configured(tmp_path))
    chosen = store.add({"name": "Library name", "url": FIRST})
    before = store.path.read_bytes()
    monkeypatch.setattr(favorites, "MAX_FAVORITES_BYTES", len(before))
    with pytest.raises(FavoriteError) as caught:
        store.set_url(chosen["id"], EXACT, "keep")
    assert caught.value.code == "library_full" and caught.value.status == 409
    assert store.path.read_bytes() == before and store.document()["favorites"][-1] == chosen


@pytest.mark.parametrize("url", [
    None, True, [], {}, 3, "", " ", "relative", "file:///tmp/a.wav", "ftp://media.example.test/a",
    "https:///missing", "https://user:secret@media.example.test/a", "https://@media.example.test/a",
    "https://media.example.test:70000/a", "https://media.example.test:0/a",
    "https://media.example.test/a b", "https://media.example.test/\x00",
    "https://media.example.test/\ud800", "https://media.example.test/a\\b",
    "https://media.example.test/a<b", "https://media.example.test/a\n",
    "http://127.0.0.1:8080/proxy/audio?url=opaque", "http://127.0.0.1:8080/%70roxy/audio?url=opaque",
    "http://127.0.0.1:8080/a/%2e%2e/proxy%2faudio?url=opaque", "https://media.example.test/" + "a" * 8192,
])
def test_invalid_target_is_private_and_never_mutates(tmp_path, url):
    store = FavoritesStore(configured(tmp_path))
    chosen = store.add({"name": "Library name", "url": FIRST, **DISPLAY})
    before = store.path.read_bytes()
    with pytest.raises(FavoriteError) as caught:
        store.set_url(chosen["id"], url, "clear")
    assert caught.value.status == 400 and "secret" not in str(caught.value)
    assert store.path.read_bytes() == before and store.document()["favorites"][-1] == chosen


def test_invalid_policy_unknown_configured_and_corrupt_library(tmp_path):
    cfg = configured(tmp_path)
    store = FavoritesStore(cfg)
    chosen = store.add({"name": "Library name", "url": FIRST, **DISPLAY})
    before = store.path.read_bytes()
    for policy in (None, True, 1, [], {}, "", "KEEP", "clear ", "secret"):
        with pytest.raises(FavoriteError) as caught:
            store.set_url(chosen["id"], SECOND, policy)
        assert caught.value.status == 400 and "secret" not in str(caught.value)
    for identifier, status in (("configured:1", 403), ("a" * 32, 404), ("invalid", 400)):
        with pytest.raises(FavoriteError) as caught:
            store.set_url(identifier, SECOND, "keep")
        assert caught.value.status == status
    assert store.path.read_bytes() == before and store.document()["favorites"][-1] == chosen
    store.path.write_bytes(b'{"version":1,"favorites": BROKEN')
    corrupt = FavoritesStore(cfg)
    with pytest.raises(FavoriteError) as caught:
        corrupt.set_url(chosen["id"], SECOND, "clear")
    assert caught.value.code == "library_unavailable" and caught.value.status == 503
    assert corrupt.path.read_bytes() == b'{"version":1,"favorites": BROKEN'


@pytest.mark.parametrize("failure", ["file_fsync", "replace", "directory_fsync"])
def test_atomic_target_failure_preserves_truthful_visible_state(tmp_path, monkeypatch, failure):
    store = FavoritesStore(configured(tmp_path))
    chosen = store.add({"name": "Library name", "url": FIRST, **DISPLAY})
    before = store.path.read_bytes()
    real_fsync = os.fsync
    calls = 0
    def fsync(descriptor):
        nonlocal calls
        calls += 1
        if calls == (2 if failure == "directory_fsync" else 1):
            raise OSError("private supplied data must not leak")
        real_fsync(descriptor)
    def replace(*_args):
        raise OSError("private supplied data must not leak")
    if failure == "replace":
        monkeypatch.setattr(persistence.os, "replace", replace)
    else:
        monkeypatch.setattr(persistence.os, "fsync", fsync)
    committed = failure == "directory_fsync"
    with pytest.raises(AtomicWriteCommittedError if committed else OSError):
        store.set_url(chosen["id"], EXACT, "clear")
    assert FavoritesStore(store.cfg).document() == store.document()
    expected = {**chosen, "url": EXACT, **dict.fromkeys(DISPLAY, "")} if committed else chosen
    assert store.document()["favorites"][-1] == expected
    assert (store.path.read_bytes() != before) is committed
    assert not list(tmp_path.glob(".favorites.json.*"))


async def test_http_target_keeps_current_playback_until_explicit_recall(tmp_path):
    async with running(configured(tmp_path)) as (api, backend, server, session):
        chosen = api.favorites.add({"name": "Library name", "url": FIRST, **DISPLAY})
        path = BASE + "/" + chosen["id"]
        async with session.post(server.make_url(path + "/play")) as response:
            assert response.status == 200
        state, calls = asdict(api.state), list(backend.calls)
        queue, item = api.state.queue, api.state.queue.current
        for policy in ("keep", "clear"):
            async with session.put(server.make_url(path + "/target"), json={"url": EXACT, "metadata": policy}) as response:
                assert response.status == 200
                expected = {**chosen, "url": EXACT, **(dict.fromkeys(DISPLAY, "") if policy == "clear" else {})}
                assert await response.json() == {"favorite": expected}
                assert not any(key.lower().startswith("access-control-") for key in response.headers)
            assert api.state.queue is queue and queue.current is item
            assert asdict(api.state) == state and backend.calls == calls and backend.url == FIRST
        assert FavoritesStore(api.cfg).document() == api.favorites.document()
        async with session.post(server.make_url(path + "/play")) as response:
            assert response.status == 200
        assert backend.url == EXACT and api.state.queue.current.url == EXACT
        assert api.state.queue.current.title == "Library name"
        assert all(getattr(api.state.queue.current, key) == "" for key in ("artist", "album", "artwork"))
        assert backend.calls == [("loadfile", FIRST), ("stop", None), ("loadfile", EXACT)]


async def test_target_edit_cannot_retarget_an_admitted_snapshot_awaiting_resolution(tmp_path):
    async with running(configured(tmp_path)) as (api, backend, server, session):
        chosen = api.favorites.add({"name": "Library name", "url": FIRST, **DISPLAY})
        path = BASE + "/" + chosen["id"]
        entered, release = asyncio.Event(), asyncio.Event()
        resolved = []
        async def resolve(url):
            resolved.append(url)
            if url == FIRST:
                entered.set()
                await release.wait()
            return url
        api._resolve_playback_url = resolve
        pending = asyncio.create_task(session.post(server.make_url(path + "/play")))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            before, calls = asdict(api.state), list(backend.calls)
            queue, item = api.state.queue, api.state.queue.current
            async with session.put(server.make_url(path + "/target"), json={"url": SECOND, "metadata": "clear"}) as response:
                assert response.status == 200
            assert asdict(api.state) == before and backend.calls == calls and resolved == [FIRST]
            assert api.state.queue is queue and queue.current is item
            release.set()
            async with await asyncio.wait_for(pending, 2) as response:
                assert response.status == 200
            assert item.url == backend.url == FIRST
            assert {field: getattr(item, field) for field in DISPLAY} == DISPLAY
            async with session.post(server.make_url(path + "/play")) as response:
                assert response.status == 200
            assert backend.url == SECOND and resolved == [FIRST, SECOND]
            assert api.state.queue.current.title == "Library name"
            assert backend.calls == [("loadfile", FIRST), ("stop", None), ("loadfile", SECOND)]
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_target_update_does_not_touch_native_staging_or_recovery(tmp_path):
    api = make_api(tmp_path)
    await start(api)
    chosen = api.favorites.add({"name": "Library name", "url": FIRST, **DISPLAY})
    stage = api._native_stage
    assert stage is not None
    # Arm a concrete recovery object on the current selection; library writes
    # must leave this ownership bookkeeping untouched as well as native staging.
    api.state.queue.current.stream_type = "station"
    api.state.queue.current.duration = 0
    api._arm_station_recovery()
    recovery = api._station_recovery
    assert recovery is not None
    before, calls = asdict(api.state), deepcopy(api.mpv.calls)
    entries, stage_before = deepcopy(api.mpv.entries), asdict(stage)
    recovery_before = asdict(recovery)
    api.favorites.set_url(chosen["id"], SECOND, "clear")
    assert asdict(api.state) == before and api.mpv.calls == calls and api.mpv.entries == entries
    assert api._native_stage is stage and asdict(stage) == stage_before
    assert api._station_recovery is recovery and asdict(recovery) == recovery_before


async def test_target_route_exact_body_methods_and_existing_contracts(tmp_path):
    async with running(configured(tmp_path)) as (api, backend, server, session):
        chosen = api.favorites.add({"name": "Library name", "url": FIRST, **DISPLAY})
        path = BASE + "/" + chosen["id"]
        before, state = api.favorites.path.read_bytes(), asdict(api.state)
        for payload in ({}, {"url": SECOND}, {"metadata": "keep"}, {"url": SECOND, "metadata": "keep", "name": "Changed"},
                        {"url": SECOND, "metadata": None}, {"url": SECOND, "metadata": []},
                        {"url": True, "metadata": "keep"}, [], None):
            async with session.put(server.make_url(path + "/target"), data=json.dumps(payload),
                                   headers={"Content-Type": "application/json"}) as response:
                assert response.status == 400 and "error" in await response.json()
        for method in ("GET", "HEAD", "POST", "PATCH", "DELETE", "OPTIONS"):
            async with session.request(method, server.make_url(path + "/target")) as response:
                assert response.status == 405
        async with session.patch(server.make_url(path), json={"url": SECOND, "metadata": "keep"}) as response:
            assert response.status == 400
        async with session.post(server.make_url(BASE + "/current"), json={"url": SECOND, "metadata": "keep"}) as response:
            assert response.status == 400
        assert api.favorites.path.read_bytes() == before and asdict(api.state) == state and not backend.calls


async def test_target_body_limits_compression_duplicates_and_identity_errors(tmp_path):
    async with running(configured(tmp_path)) as (api, backend, server, session):
        chosen = api.favorites.add({"name": "Library name", "url": FIRST})
        target = server.make_url(BASE + "/" + chosen["id"] + "/target")
        body = json.dumps({"url": SECOND, "metadata": "keep"}).encode()
        before = api.favorites.path.read_bytes()
        cases = [(b'{"url":"a","url":"b","metadata":"clear"}', {}, 400),
                 (b'{"url":NaN,"metadata":"clear"}', {}, 400), (b'\xff', {}, 400),
                 (b' ' * (MAX_REQUEST_BYTES + 1), {}, 413),
                 (body, {"Content-Type": "text/plain"}, 415),
                 (body, {"Content-Type": "application/json; charset=latin1"}, 415),
                 (gzip.compress(body), {"Content-Encoding": "gzip"}, 415)]
        for encoded, headers, status in cases:
            async with session.put(target, data=encoded, headers={"Content-Type": "application/json", **headers}) as response:
                assert response.status == status and "error" in await response.json()
        async def chunks():
            yield body
        async with session.put(target, data=chunks(), headers={"Content-Type": "application/json"}) as response:
            assert response.status == 411
        for encodings in (("identity", "identity"), ("identity", "gzip")):
            async with session.put(target, data=body,
                    headers=[("Content-Type", "application/json"),
                             *(("Content-Encoding", value) for value in encodings)]) as response:
                assert response.status == 415
        for identifier, status in (("configured:1", 403), ("a" * 32, 404), ("invalid", 400)):
            async with session.put(server.make_url(BASE + "/" + identifier + "/target"),
                                   json={"url": SECOND, "metadata": "keep"}) as response:
                assert response.status == status
        assert api.favorites.path.read_bytes() == before and not backend.calls


@pytest.mark.parametrize("committed", [False, True])
async def test_target_http_commit_errors_do_not_claim_success_or_leak(tmp_path, monkeypatch, committed):
    async with running(configured(tmp_path)) as (api, backend, server, session):
        chosen = api.favorites.add({"name": "Private library label", "url": FIRST, **DISPLAY})
        original = favorites.atomic_write_text
        def fail(path, encoded):
            if committed:
                original(path, encoded)
                raise AtomicWriteCommittedError("private supplied URL " + EXACT)
            raise OSError("private supplied URL " + EXACT)
        monkeypatch.setattr(favorites, "atomic_write_text", fail)
        async with session.put(server.make_url(BASE + "/" + chosen["id"] + "/target"),
                               json={"url": EXACT, "metadata": "clear"}) as response:
            assert response.status == 503
            result = await response.json()
            assert result["committed"] is committed
            assert result["error"] == ("durability_unconfirmed" if committed else "storage_unavailable")
            assert "private" not in json.dumps(result) and EXACT not in json.dumps(result)
        assert FavoritesStore(api.cfg).document() == api.favorites.document()
        assert api.favorites.document()["favorites"][-1]["url"] == (EXACT if committed else FIRST)
        assert not backend.calls


async def test_target_http_corrupt_library_is_preserved(tmp_path):
    (tmp_path / "favorites.json").write_bytes(b'CORRUPT')
    async with running(configured(tmp_path)) as (api, backend, server, session):
        async with session.put(server.make_url(BASE + "/" + "a" * 32 + "/target"),
                               json={"url": SECOND, "metadata": "clear"}) as response:
            assert response.status == 503
            assert (await response.json())["error"] == "library_unavailable"
        assert (tmp_path / "favorites.json").read_bytes() == b'CORRUPT' and not backend.calls
