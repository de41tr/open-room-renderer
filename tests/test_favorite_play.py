"""Stable-ID recall: real local HTTP, deterministic ownership and no WAN audio."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import make_mocked_request

from fake_linkplay.config import ExperimentalSource
from fake_linkplay.favorites import FavoriteError, FavoritesStore
from fake_linkplay.http_api import ParsedCommand
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.state import PlaybackPhase
from test_music_presets import FIRST, SECOND, configured, running
from test_native_queue_staging import C, make_api, mutations, start

BASE = "/orr/v1/favorites"
SECRET = "https://media.example.test/private?token=synthetic-secret"


def request(identifier):
    return make_mocked_request("POST", BASE + "/" + identifier + "/play", match_info={"id": identifier})


def test_playback_snapshot_is_detached_and_uses_only_managed_ids(tmp_path):
    store = FavoritesStore(configured(tmp_path))
    lower = store.add({"name": "Lower", "url": FIRST})
    chosen = store.add({"name": "Chosen", "url": SECOND, "artwork": FIRST})
    # A configured experimental source ID must not shadow a managed ID.
    store.cfg.source_experiment_variant = "preset"
    store.cfg.experimental_sources = [ExperimentalSource(chosen["id"], "Other", url=FIRST)]
    before = store.playback_snapshot(chosen["id"])
    assert before["url"] == SECOND and before["number"] == 5
    store.delete(lower["id"])
    assert store.playback_snapshot(chosen["id"])["number"] == 4
    store.rename(chosen["id"], "Renamed")
    store.delete(chosen["id"])
    assert before["name"] == "Chosen" and before["picurl"] == FIRST
    before["url"] = SECRET
    assert store.playback_snapshot("configured:3")["url"] == FIRST
    with pytest.raises(FavoriteError) as error:
        store.playback_snapshot(chosen["id"])
    assert error.value.status == 404


async def test_http_stable_recall_after_compaction_preserves_exact_display(tmp_path):
    async with running(configured(tmp_path)) as (api, backend, server, session):
        lower = api.favorites.add({"name": "Lower", "url": FIRST})
        metadata = {"name": "Café 日本語", "url": SECOND, "title": "Title & 曲",
                    "artist": "Artist", "album": "Album", "artwork": FIRST}
        chosen = api.favorites.add(metadata)
        api.favorites.delete(lower["id"])
        async with session.post(server.make_url(BASE + "/" + chosen["id"] + "/play")) as response:
            assert response.status == 200
            assert await response.json() == {"request_acknowledged": True, "favorite_id": chosen["id"]}
        item = api.state.queue.current
        assert item.url == backend.url == SECOND and item.id == "preset:3"
        assert {field: getattr(item, field) for field in ("title", "artist", "album", "artwork")} == {
            field: metadata[field] for field in ("title", "artist", "album", "artwork")}
        assert item.source == "Preset" and api.state.status == "play"
        assert backend.calls == [("loadfile", SECOND), ("resume", None)]


async def test_invalid_unknown_deleted_and_body_requests_do_not_mutate_playback(tmp_path):
    async with running(configured(tmp_path)) as (api, backend, server, session):
        deleted = api.favorites.add({"name": "Deleted", "url": FIRST})
        api.favorites.delete(deleted["id"])
        await api.execute(ParsedCommand("MCUKeyShortClick:2"), "127.0.0.1")
        before, calls = asdict(api.state), list(backend.calls)
        for identifier, status in (("configured:0", 400), ("configured:01", 400),
                                   ("configured:+1", 400), ("configured:1000000", 400),
                                   ("configured:３", 400), ("A" * 32, 400),
                                   ("configured:3", 404), ("a" * 32, 404), (deleted["id"], 404)):
            async with session.post(server.make_url(BASE + "/" + identifier + "/play")) as response:
                assert response.status == status
        for kwargs in ({"json": {}}, {"data": b"x"}, {"chunked": True}):
            async with session.post(server.make_url(BASE + "/configured:1/play"), **kwargs) as response:
                assert response.status == 400
                assert (await response.json())["error"] == "unexpected_body"
        assert asdict(api.state) == before and backend.calls == calls


async def test_configured_source_and_artwork_survive_corrupt_managed_storage(tmp_path):
    path = tmp_path / "favorites.json"
    path.write_bytes(b"{broken")
    cfg = configured(tmp_path, source_experiment_variant="preset", experimental_sources=[
        ExperimentalSource("radio", "Configured source", source="Independent Radio",
                           url=SECOND, icon=FIRST)])
    async with running(cfg) as (api, backend, server, session):
        async with session.post(server.make_url(BASE + "/configured:3/play")) as response:
            assert response.status == 200
        assert backend.url == SECOND and api.state.queue.current.source == "Independent Radio"
        assert api.state.queue.current.artwork == FIRST and api.state.queue.current.id == "preset:3"
        before, calls = asdict(api.state), list(backend.calls)
        for identifier, status in (("a" * 32, 503), ("configured:4", 404), ("invalid", 400)):
            async with session.post(server.make_url(BASE + "/" + identifier + "/play")) as response:
                assert response.status == status
        assert asdict(api.state) == before and backend.calls == calls
        assert path.read_bytes() == b"{broken"


async def test_edits_during_resolution_cannot_retarget_admitted_snapshot(tmp_path):
    async with running(configured(tmp_path)) as (api, backend, server, session):
        lower = api.favorites.add({"name": "Lower", "url": FIRST})
        chosen = api.favorites.add({"name": "Original name", "url": SECOND, "artwork": FIRST})
        entered, release = asyncio.Event(), asyncio.Event()
        async def resolve(url):
            assert url == SECOND
            entered.set()
            await release.wait()
            return url
        api._resolve_playback_url = resolve
        pending = asyncio.create_task(session.post(server.make_url(BASE + "/" + chosen["id"] + "/play")))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            queue, item = api.state.queue, api.state.queue.current
            api.favorites.rename(chosen["id"], "Changed name")
            api.favorites.delete(lower["id"])
            api.favorites.delete(chosen["id"])
            api.favorites.add({"name": "Replacement", "url": FIRST})
            release.set()
            async with await asyncio.wait_for(pending, 2) as response:
                assert response.status == 200
            assert api.state.queue is queue and queue.current is item
            assert item.url == backend.url == SECOND and item.title == "Original name"
            assert item.id == "preset:4" and item.artwork == FIRST
            async with session.post(server.make_url(BASE + "/" + chosen["id"] + "/play")) as response:
                assert response.status == 404
            assert backend.calls == [("loadfile", SECOND), ("resume", None)]
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("new_owner", ["stop", "play"])
async def test_delayed_favorite_never_overrides_newer_owner(tmp_path, new_owner):
    async with running(configured(tmp_path)) as (api, backend, server, session):
        chosen = api.favorites.add({"name": "Delayed", "url": FIRST})
        entered, release = asyncio.Event(), asyncio.Event()
        async def resolve(url):
            if url == FIRST:
                entered.set()
                await release.wait()
            return url
        api._resolve_playback_url = resolve
        pending = asyncio.create_task(session.post(server.make_url(BASE + "/" + chosen["id"] + "/play")))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if new_owner == "stop":
                await api.execute(ParsedCommand("stop"), "127.0.0.1")
            else:
                async with session.post(server.make_url(BASE + "/configured:2/play")) as response:
                    assert response.status == 200
            before, calls = asdict(api.state), list(backend.calls)
            release.set()
            async with await asyncio.wait_for(pending, 2) as response:
                assert response.status == 502
            assert asdict(api.state) == before and backend.calls == calls
            assert backend.url == (SECOND if new_owner == "play" else "")
            assert api.state.status == ("play" if new_owner == "play" else "stop")
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("failure", ["backend", "resolver", "oserror", "timeout"])
async def test_playback_errors_are_safe_502_and_not_storage_errors(tmp_path, failure):
    async with running(configured(tmp_path)) as (api, backend, server, session):
        if failure == "backend":
            backend.fail_operation = "loadfile"
        else:
            async def fail(url):
                raise {"resolver": BackendError, "oserror": OSError, "timeout": TimeoutError}[failure](SECRET)
            api._resolve_playback_url = fail
        async with session.post(server.make_url(BASE + "/configured:1/play")) as response:
            assert response.status == 502
            assert await response.json() == {"error": "playback_failed",
                                            "message": "Favorite playback could not be confirmed"}
        assert api.state.status != "play" and api.state.transport_status == "ERROR_OCCURRED"
        assert api.state.current_url == FIRST and backend.idle
        assert backend.calls == ([("loadfile", FIRST), ("stop", None)] if failure == "backend" else [])


@pytest.mark.parametrize("failure", ["rejected", "not_idle", "readback"])
async def test_unconfirmed_stop_prevents_favorite_replacement(tmp_path, failure):
    async with running(configured(tmp_path)) as (api, backend, server, session):
        await api.execute(ParsedCommand("MCUKeyShortClick:1"), "127.0.0.1")
        chosen = api.favorites.add({"name": "Replacement", "url": SECOND})
        queue, saved = api.state.queue, api.favorites.document()
        if failure == "rejected":
            backend.stop = AsyncMock(side_effect=BackendError("Synthetic Stop refusal"))
        elif failure == "not_idle":
            backend.stop = AsyncMock()  # Acknowledged, but the old source is still active.
        else:
            backend.snapshot = AsyncMock(return_value={"backend-error": True})
        api._resolve_playback_url = AsyncMock(return_value=SECOND)
        async with session.post(server.make_url(BASE + "/" + chosen["id"] + "/play")) as response:
            assert response.status == 502
        assert api.state.queue is queue and api.state.current_url == FIRST
        assert api.state.phase == PlaybackPhase.ERROR and "silence not confirmed" in api.state.last_error
        assert api.favorites.document() == saved
        api._resolve_playback_url.assert_not_awaited()
        assert [url for name, url in backend.calls if name == "loadfile"] == [FIRST]


@pytest.mark.parametrize("new_owner", ["stop", "play"])
async def test_new_owner_during_retirement_prevents_stale_favorite_publication(tmp_path, new_owner):
    async with running(configured(tmp_path)) as (api, backend, server, session):
        await api.execute(ParsedCommand("MCUKeyShortClick:1"), "127.0.0.1")
        chosen = api.favorites.add({"name": "Stale replacement", "url": SECOND})
        queue, saved = api.state.queue, api.favorites.document()
        entered, release = asyncio.Event(), asyncio.Event()
        original = backend.stop
        async def delayed_stop():
            entered.set()
            await release.wait()
            await original()
        backend.stop = delayed_stop
        api._resolve_playback_url = AsyncMock(side_effect=lambda url: url)
        pending = asyncio.create_task(session.post(server.make_url(BASE + "/" + chosen["id"] + "/play")))
        newer = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert api.state.queue is queue and api.state.current_url == FIRST
            api._resolve_playback_url.assert_not_awaited()
            generation = api.state.generation
            newer = asyncio.create_task(api._transport_command("stop") if new_owner == "stop" else api._play_url(C))
            async with asyncio.timeout(2):
                while api.state.generation == generation:
                    await asyncio.sleep(0)
            release.set()
            async with await asyncio.wait_for(pending, 2) as response:
                assert response.status == 502
            await asyncio.wait_for(newer, 2)
            assert backend.url == (C if new_owner == "play" else "")
            assert api.state.current_url == (C if new_owner == "play" else FIRST)
            assert api.state.phase == (PlaybackPhase.PLAYING if new_owner == "play" else PlaybackPhase.STOPPED)
            assert [url for name, url in backend.calls if name == "loadfile"] == ([FIRST, C] if new_owner == "play" else [FIRST])
            assert api.favorites.document() == saved
        finally:
            release.set()
            for task in (pending, newer):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (pending, newer) if task is not None), return_exceptions=True)


@pytest.mark.parametrize("outcome", ["success", "resolution_failure", "cancel"])
async def test_favorite_recall_retires_native_staging_through_existing_path(tmp_path, outcome):
    api = make_api(tmp_path)
    await start(api)
    assert api._native_stage is not None and len(api.mpv.entries) == 2
    chosen = api.favorites.add({"name": "New favorite", "url": C})
    entered = asyncio.Event()
    if outcome != "success":
        async def resolve(url):
            entered.set()
            if outcome == "cancel":
                await asyncio.Future()
            raise BackendError(SECRET)
        api._resolve_playback_url = resolve
    pending = asyncio.create_task(api.favorites_api.handle_play(request(chosen["id"])))
    if outcome == "cancel":
        await asyncio.wait_for(entered.wait(), 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        response = await asyncio.wait_for(pending, 2)
        assert response.status == (200 if outcome == "success" else 502)
        if outcome == "success":
            assert json.loads(response.text)["favorite_id"] == chosen["id"]
    assert api._native_stage is None and mutations(api, "stop") == [None]
    assert api.state.queue.current.url == C and api.state.queue.current.id == "preset:1"
    if outcome == "success":
        assert api.mpv.url == C and len(api.mpv.entries) == 1
        assert api.state.phase == PlaybackPhase.PLAYING
    else:
        assert api.mpv.idle and api.mpv.entries == [] and api.state.phase == PlaybackPhase.ERROR
