"""Pure preparation and loopback Web playlist mutation tests; no media fetches."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from fake_linkplay.http_api import ParsedCommand
from fake_linkplay.queue import PlaybackQueue, QueueItem
from fake_linkplay.queue_policy import REPEAT_ALL, SHUFFLE_REPEAT_ALL
from fake_linkplay.state import PlaybackPhase
from fake_linkplay.web_playlists import (WebPlaylistError, availability, edit_reason,
                                        prepare_edit, prepare_source)
from fake_linkplay.web_ui import MAX_REQUEST_BYTES, PREFIX, WebAdmissionError, decode_command
from test_web_ui import api, command, selected, serving, state

URLS = ["https://media.example.test/first.wav?token=FIRST_SECRET",
        "https://media.example.test/second.wav?token=SECOND_SECRET"]


def source(kind="urls", **fields):
    document = {"name": "Authored", "index": 2, "mode": "repeat-all"}
    if kind == "json":
        return {"format": kind, "text": json.dumps({"version": 1, **document, "urls": URLS}), **fields}
    if kind == "m3u":
        return {"format": kind, **document, "text": "#EXTM3U\r\n#EXTINF:30,PRIVATE TITLE\r\n" + "\r\n".join(URLS), **fields}
    return {"format": kind, **document, "urls": list(URLS), **fields}


def payload(action="playlist_replace", generation=0, **fields):
    if action == "playlist_replace" and "source" not in fields:
        fields["source"] = source()
    return {"action": action, "expected_generation": generation, **fields}


def canonical_queue():
    # Identical URL occurrences must retain independent position/metadata.
    items = [QueueItem(url=URLS[0], title=f"Song {i}", artist=f"Artist {i}",
                       album="Album", artwork="https://art.example.test/a.jpg",
                       metadata=f'<metadata keep="{i}">&amp;</metadata>',
                       duration=20 + i, content_type="audio/flac") for i in range(3)]
    items[1].resolved_url = items[1].url
    items[1].title_is_fallback = True
    return PlaybackQueue(name="Ordinary", list_name="Display list", artwork="https://art.example.test/list.jpg",
                         show_name="Display show", quality="lossless", content_type="audio/flac",
                         items=items, index=2, loop=SHUFFLE_REPEAT_ALL)


@pytest.mark.parametrize("kind", ["json", "m3u", "urls"])
def test_pure_source_preserves_exact_urls_and_authored_selection(kind):
    original = source(kind)
    before = deepcopy(original)
    queue = prepare_source(original)
    assert (queue.name, queue.index, queue.loop) == ("Authored", 2, REPEAT_ALL)
    assert [item.url for item in queue.items] == URLS
    assert all(not item.title and not item.metadata and not item.resolved_url for item in queue.items)
    assert original == before


@pytest.mark.parametrize("bad", [
    {}, [], None, {"format": "xml", "text": "<PlayList/>"},
    {"format": "json", "text": "{}", "name": "Extra"},
    {"format": "json", "text": {"version": 1}},
    {"format": "json", "text": '\ufeff{}'},
    {"format": "json", "text": '{"version":1,"version":1,"name":"x","index":1,"mode":"normal","urls":["https://example.test/a"]}'},
    source("json", text='{"version":NaN}'), source("m3u", text="#EXTM3U\n#EXT-X-VERSION:3\n" + URLS[0]),
    source("m3u", text="#EXTM3U\n#EXTINF:1,title"), source("m3u", text=" " + URLS[0]),
    source("m3u", text=URLS[0] + "\r" + URLS[1]), source("m3u", text="file:///private/file.wav"),
    source("urls", name=" "), source("urls", name="line\nbreak"), source("urls", name="界" * 86),
    source("urls", name=" x"), source("urls", urls=[]), source("urls", urls="https://example.test/a"),
    source("urls", urls=["https://user:password@example.test/a"]),
    source("urls", urls=["https://example.test/proxy/audio?url=x"]), source("urls", urls=["relative.wav"]),
    source("urls", urls=["https://example.test/a", None]), source("urls", urls=URLS * 501),
    source("urls", index=True), source("urls", index=0), source("urls", index=3),
    source("urls", mode="shuffle"), source("urls", mode=1), source("urls", extra="PRIVATE_SOURCE"),
])
def test_pure_strict_source_union_rejects_generically(bad):
    with pytest.raises(WebPlaylistError, match="^Invalid browser playlist input$"):
        prepare_source(bad)


@pytest.mark.parametrize("bad", [
    {"action": "playlist_replace", "source": source()},
    payload(expected_generation=True), payload(expected_generation=-1), payload(expected_generation=2**53),
    payload(extra="SECRET"), payload(source={"format": "urls", "urls": URLS}),
    payload("playlist_append", urls=[]), payload("playlist_append", urls=["file:///private"]),
    payload("playlist_append", urls=URLS, source=source()),
    payload("playlist_remove", index=True), payload("playlist_remove", index=0),
    payload("playlist_remove", index=1001), payload("playlist_remove", index="1"),
    payload("playlist_move", from_index=1, to_index=False),
    payload("playlist_move", from_index=1, to_index=2, index=1),
])
def test_typed_command_rejects_before_dispatch(bad):
    with pytest.raises(WebAdmissionError):
        decode_command(json.dumps(bad).encode())


def test_duplicate_outer_or_nested_keys_and_oversized_envelope_reject():
    encoded = json.dumps(payload()).encode()
    with pytest.raises(WebAdmissionError):
        decode_command(encoded[:-1] + b',"expected_generation":0}')
    with pytest.raises(WebAdmissionError):
        decode_command(encoded.replace(b'"format": "urls"', b'"format":"urls","format":"urls"'))
    with pytest.raises(WebAdmissionError):
        decode_command(encoded + b' ' * MAX_REQUEST_BYTES)


@pytest.mark.parametrize("action,fields,expected_titles,expected_index", [
    ("playlist_append", {"urls": URLS}, ["Song 0", "Song 1", "Song 2", "", ""], 2),
    ("playlist_remove", {"index": 1}, ["Song 1", "Song 2"], 1),
    ("playlist_remove", {"index": 2}, ["Song 0", "Song 2"], 2),
    ("playlist_remove", {"index": 3}, ["Song 0", "Song 1"], 2),
    ("playlist_move", {"from_index": 2, "to_index": 3}, ["Song 0", "Song 2", "Song 1"], 3),
    ("playlist_move", {"from_index": 1, "to_index": 3}, ["Song 1", "Song 2", "Song 0"], 1),
    ("playlist_move", {"from_index": 3, "to_index": 1}, ["Song 2", "Song 0", "Song 1"], 3),
    ("playlist_move", {"from_index": 2, "to_index": 2}, ["Song 0", "Song 1", "Song 2"], 2),
])
def test_canonical_edit_preserves_duplicate_occurrence_metadata_and_detaches(action, fields, expected_titles, expected_index):
    original = canonical_queue()
    before = deepcopy(original)
    candidate = prepare_edit(original, action, **fields)
    assert [item.title for item in candidate.items] == expected_titles
    assert candidate.index == expected_index
    assert original == before
    assert candidate is not original and candidate.items is not original.items
    for item in candidate.items:
        if item.title:
            saved = next(entry for entry in original.items if entry.title == item.title)
            assert item == saved and item is not saved
    assert (candidate.name, candidate.list_name, candidate.show_name, candidate.artwork,
            candidate.quality, candidate.content_type, candidate.loop) == (
            original.name, original.list_name, original.show_name, original.artwork,
            original.quality, original.content_type, original.loop)
    candidate.items[0].metadata = "changed"
    assert original == before


def test_remove_selected_final_chooses_previous_and_never_creates_empty_queue():
    queue = canonical_queue()
    queue.index = 3
    assert prepare_edit(queue, "playlist_remove", index=3).index == 2
    queue = PlaybackQueue(items=[QueueItem(url=URLS[0])], index=1)
    with pytest.raises(WebPlaylistError):
        prepare_edit(queue, "playlist_remove", index=1)


@pytest.mark.parametrize("target,field,value", [
    ("queue", "source", "provider"), ("queue", "search_url", "https://example.test/search?token=SECRET"),
    ("queue", "stream_id", "provider-id"), ("queue", "stream_type", "station"),
    ("item", "source", "provider"), ("item", "id", "provider-id"), ("item", "stream_type", "station"),
    ("item", "_radio_station", (URLS[0], "station-id")), ("item", "_radio_resolution_attempted", True),
    ("item", "resolved_url", "https://other.example.test/resolved?token=SECRET"),
    ("item", "url", "file:///private.wav"), ("item", "metadata", "&" * 140000),
])
def test_provider_radio_nonexplicit_and_oversized_queues_are_not_editable(target, field, value):
    queue = canonical_queue()
    setattr(queue if target == "queue" else queue.items[0], field, value)
    before = deepcopy(queue)
    assert edit_reason(queue) == "unsupported_queue"
    with pytest.raises(WebPlaylistError):
        prepare_edit(queue, "playlist_append", urls=URLS)
    assert queue == before


def test_count_limits_and_complete_escaped_result_are_checked():
    queue = PlaybackQueue(items=[QueueItem(url=URLS[0]) for _ in range(1000)], index=1)
    actions, reason = availability(queue, stopped=True)
    assert reason == "supported" and not actions["playlist_append"]
    with pytest.raises(WebPlaylistError):
        prepare_edit(queue, "playlist_append", urls=URLS)
    huge = "https://media.example.test/a?x=" + "a&" * 130000
    with pytest.raises(WebPlaylistError):
        prepare_source(source("urls", urls=[huge], index=1))


def test_capability_checks_each_url_once_without_deepcopy(monkeypatch):
    from fake_linkplay import queue_protocol, web_playlists

    queue = canonical_queue()
    before = deepcopy(queue)
    items = list(queue.items)
    seen = []
    original = queue_protocol.urlsplit

    def inspect_url(value):
        seen.append(value)
        return original(value)

    def no_deepcopy(value):
        pytest.fail("Capability must not deepcopy canonical state or shuffle history")

    monkeypatch.setattr(queue_protocol, "urlsplit", inspect_url)
    monkeypatch.setattr(web_playlists, "deepcopy", no_deepcopy)
    actions, reason = availability(queue, stopped=True)
    assert reason == "supported" and all(actions.values())
    assert seen == [item.url for item in queue.items]
    assert queue == before
    assert all(item is saved for item, saved in zip(queue.items, items))
    assert queue.items[1].resolved_url == queue.items[1].url


@pytest.mark.parametrize("target,field,value", [
    ("queue", "name", " leading"), ("queue", "list_name", "bad\x00"),
    ("queue", "index", True), ("queue", "loop", 99), ("queue", "quality", []),
    ("queue", "source", "provider"), ("queue", "search_url", "https://example.test/search"),
    ("queue", "stream_id", "id"), ("queue", "stream_type", "station"),
    ("item", "url", "https://user:SECRET@example.test/a"),
    ("item", "title", "bad\x00"), ("item", "artist", []), ("item", "album", "bad\ud800"),
    ("item", "metadata", "&" * 140000), ("item", "artwork", "bad\uffff"),
    ("item", "duration", float("inf")), ("item", "title_is_fallback", 1),
    ("item", "source", "provider"), ("item", "id", "id"), ("item", "stream_type", "station"),
    ("item", "_radio_station", (URLS[0], "station")), ("item", "_radio_resolution_attempted", True),
    ("item", "resolved_url", "https://example.test/other"),
])
def test_capability_rechecks_mutable_fields_and_edits_reject_without_changes(target, field, value):
    queue = canonical_queue()
    assert availability(queue, stopped=True)[1] == "supported"
    holder = queue if target == "queue" else queue.items[0]
    saved = getattr(holder, field)
    setattr(holder, field, value)
    before = deepcopy(queue)
    actions, reason = availability(queue, stopped=True)
    assert reason == "unsupported_queue"
    assert actions == {"playlist_replace": True, "playlist_append": False,
                       "playlist_remove": False, "playlist_move": False}
    with pytest.raises(WebPlaylistError):
        prepare_edit(queue, "playlist_move", from_index=1, to_index=2)
    assert queue == before
    setattr(holder, field, saved)
    assert availability(queue, stopped=True)[1] == "supported"


def test_capability_rechecks_mutated_item_list_and_escaped_boundary():
    from fake_linkplay.queue_protocol import MAX_SOAP_BYTES, prepare_replace

    queue = canonical_queue()
    # Build an exact full-SOAP byte bound with double-escaped metadata and a
    # played explicit item. Any extra byte must disable edits on the next poll.
    queue.items[1].resolved_url = ""
    queue.items[0].metadata = "&"
    remaining = MAX_SOAP_BYTES - len(prepare_replace(queue))
    queue.items[0].metadata += "&" * (remaining // 9) + "a" * (remaining % 9)
    assert len(prepare_replace(queue)) == MAX_SOAP_BYTES
    queue.items[1].resolved_url = queue.items[1].url
    before = deepcopy(queue)
    assert availability(queue, stopped=True)[1] == "supported"
    assert prepare_edit(queue, "playlist_move", from_index=1, to_index=2).items[0] == queue.items[1]
    assert queue == before
    queue.items[0].metadata += "a"
    assert availability(queue, stopped=True)[1] == "unsupported_queue"
    with pytest.raises(WebPlaylistError):
        prepare_edit(queue, "playlist_remove", index=1)
    queue.items[0].metadata = ""
    assert availability(queue, stopped=True)[1] == "supported"
    queue.items.extend([QueueItem(url=URLS[0]) for _ in range(998)])
    assert availability(queue, stopped=True)[1] == "unsupported_queue"
    queue.items.pop()
    assert availability(queue, stopped=True)[1] == "supported"


@pytest.mark.parametrize("kind", ["json", "m3u", "urls"])
async def test_http_import_is_stopped_only_acknowledges_without_fetch_and_does_not_echo(api, kind, monkeypatch, caplog):
    selected(api)
    before = api.state.queue
    async def no_fetch(*args, **kwargs):
        pytest.fail("Import must not resolve or load URLs")
    monkeypatch.setattr(api, "_resolve_playback_url", no_fetch)
    monkeypatch.setattr(api.mpv, "loadfile", no_fetch)
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        assert snapshot["actions"]["playlist_replace"]
        code, result = await command(session, base, snapshot["csrf_token"], payload(source=source(kind)))
        assert (code, result) == (200, {"request_acknowledged": True})
        after = await state(session, base)
        assert after["transport"]["phase"] == "STOPPED"
        assert after["generation"] == snapshot["generation"] + 1
        assert after["queue"]["playlist_edit_reason"] == "supported"
        assert after["queue"]["index"] == 2 and after["queue"]["mode"] == "repeat-all"
        assert api.state.queue is not before
        assert [item.url for item in api.state.queue.items] == URLS
        assert not api.mpv.calls
        assert not any(private in json.dumps(after) + json.dumps(result) + caplog.text
                       for private in ["SECRET", "media.example.test", "PRIVATE TITLE", "second.wav"])
        for route in ("/playlist", "/playlist/export", "/queue/playlist"):
            async with session.get(base + PREFIX + route) as response:
                assert response.status == 404


async def test_http_append_remove_move_preserve_canonical_metadata_and_reset_position_shuffle(api):
    api.state.queue = canonical_queue()
    api._activate_queue_item()
    api.state.curpos = 14
    async with serving(api) as (session, base):
        for action, fields, titles, index in [
            ("playlist_move", {"from_index": 2, "to_index": 3}, ["Song 0", "Song 2", "Song 1"], 3),
            ("playlist_append", {"urls": URLS}, ["Song 0", "Song 2", "Song 1", "", ""], 3),
            ("playlist_remove", {"index": 2}, ["Song 0", "Song 1", "", ""], 2),
        ]:
            snapshot = await state(session, base)
            old = api.state.queue
            old_current = deepcopy(old.current)
            code, body = await command(session, base, snapshot["csrf_token"], payload(action, snapshot["generation"], **fields))
            assert (code, body) == (200, {"request_acknowledged": True})
            assert api.state.queue is not old
            assert [item.title for item in api.state.queue.items] == titles
            assert api.state.queue.index == index
            assert api.state.queue.current == old_current and api.state.queue.current is not old.current
            assert api.state.curpos == 0 and api.state.phase == PlaybackPhase.STOPPED
            assert api.state.queue._shuffle is not None
            assert api.state.queue._shuffle is not old._shuffle
        assert not api.mpv.calls


@pytest.mark.parametrize("phase", list(PlaybackPhase))
async def test_http_all_nonstopped_phases_reject_without_backend_work(api, phase):
    selected(api)
    api.state.phase = phase
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        before, generation = api.state.queue, api.state.generation
        code, body = await command(session, base, snapshot["csrf_token"], payload(generation=generation))
        assert (code == 200) == (phase == PlaybackPhase.STOPPED)
        if code != 200:
            assert code == 409 and body["outcome"] == "REJECTED"
            assert api.state.queue is before and api.state.generation == generation
        assert not api.mpv.calls


@pytest.mark.parametrize("target,field,value", [
    ("state", "intent", "play"), ("state", "intent", "pause"),
    ("state", "backend_url", URLS[0]), ("state", "backend_original_url", URLS[0]),
    ("api", "_native_output_uncertain", True), ("api", "_native_waiting_for_audio", True),
    ("api", "_native_current_id", 1), ("api", "_native_owned_entries", ((1, URLS[0]),)),
    ("api", "_admitted_load_generation", 0), ("api", "_pending_seek_target", 0),
    ("api", "_station_recovery_loading", True),
])
async def test_http_stopped_but_pending_or_uncertain_rejects_without_backend_work(api, target, field, value):
    selected(api)
    setattr(api.state if target == "state" else api, field, value)
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        before = api.state.queue
        assert not snapshot["actions"]["playlist_replace"]
        code, body = await command(session, base, snapshot["csrf_token"], payload())
        assert code == 409 and body["outcome"] == "REJECTED"
        assert api.state.queue is before and api.state.generation == 0 and not api.mpv.calls


@pytest.mark.parametrize("holder", ["backend_lock", "native_cleanup", "seek", "recovery", "advance", "native_stage"])
async def test_existing_work_blocks_without_queueing_a_future_mutation(api, holder):
    selected(api)
    gate = asyncio.Event()
    task = None
    async with serving(api) as (session, base):
        if holder == "backend_lock":
            await api._backend_lock.acquire()
        elif holder == "native_stage":
            api._native_stage = SimpleNamespace(failed=False, invalidated=False)
        else:
            task = asyncio.create_task(gate.wait())
            if holder == "native_cleanup":
                api._native_cleanup_tasks.add(task)
            elif holder == "seek":
                api._seek_tasks.add(task)
            elif holder == "recovery":
                api._station_recovery_task = task
            else:
                api._queue_advance_task = task
        snapshot = await state(session, base)
        before = api.state.queue
        assert not snapshot["actions"]["playlist_replace"]
        code, body = await command(session, base, snapshot["csrf_token"], payload())
        assert code == 409 and body["outcome"] == "REJECTED"
        assert api.state.queue is before and not api.mpv.calls
        if holder == "backend_lock":
            api._backend_lock.release()
        elif holder == "native_stage":
            api._native_stage = None
        else:
            gate.set()
            await task
            api._native_cleanup_tasks.discard(task)
            api._seek_tasks.discard(task)
        await asyncio.sleep(0)
        assert api.state.queue is before and api.state.generation == 0
        assert (await state(session, base))["actions"]["playlist_replace"]


async def test_explicit_stop_must_finish_then_new_generation_required_for_replacement(api):
    selected(api)
    await api.execute(ParsedCommand("resume"), "")
    started, release = asyncio.Event(), asyncio.Event()
    original_stop = api.mpv.stop
    async def slow_stop():
        started.set()
        await release.wait()
        return await original_stop()
    api.mpv.stop = slow_stop
    async with serving(api) as (session, base):
        initial = await state(session, base)
        stopping = asyncio.create_task(command(session, base, initial["csrf_token"], {"action": "stop"}))
        await asyncio.wait_for(started.wait(), 1)
        pending = await state(session, base)
        before = api.state.queue
        code, body = await command(session, base, pending["csrf_token"], payload(generation=pending["generation"]))
        assert code == 409 and body["outcome"] == "REJECTED"
        assert api.state.queue is before
        release.set()
        assert (await stopping)[0] == 200
        assert api.state.queue is before
        code, body = await command(session, base, initial["csrf_token"], payload(generation=initial["generation"]))
        assert code == 409 and body["error"] == "stale_generation"
        fresh = await state(session, base)
        calls = list(api.mpv.calls)
        assert (await command(session, base, fresh["csrf_token"], payload(generation=fresh["generation"])))[0] == 200
        assert api.mpv.calls == calls


async def test_repeat_stale_edit_cannot_duplicate_append_or_replace_same_named_queue(api):
    selected(api)
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        proposal = payload("playlist_append", snapshot["generation"], urls=URLS)
        assert (await command(session, base, snapshot["csrf_token"], proposal))[0] == 200
        before = api.state.queue
        for proposal in (proposal, payload(generation=snapshot["generation"], source=source(name=before.name))):
            code, body = await command(session, base, snapshot["csrf_token"], proposal)
            assert code == 409 and body["error"] == "stale_generation" and body["outcome"] == "REJECTED"
        assert api.state.queue is before and api.state.queue.count == 5 and api.state.generation == 1


@pytest.mark.parametrize("provider", [False, True])
async def test_empty_or_provider_queue_can_be_replaced_but_not_edited(api, provider):
    if provider:
        api.state.queue = canonical_queue()
        api.state.queue.source = "Provider"
        api.state.queue.items[0]._radio_station = (URLS[0], "private-station")
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        assert snapshot["queue"]["playlist_edit_reason"] == ("unsupported_queue" if provider else "empty_queue")
        assert snapshot["actions"]["playlist_replace"]
        for action, fields in [("playlist_append", {"urls": URLS}), ("playlist_remove", {"index": 1}),
                               ("playlist_move", {"from_index": 1, "to_index": 1})]:
            assert not snapshot["actions"][action]
            code, body = await command(session, base, snapshot["csrf_token"], payload(action, **fields))
            assert code == 409 and body["outcome"] == "REJECTED"
        assert (await command(session, base, snapshot["csrf_token"], payload()))[0] == 200
        assert not api.mpv.calls


async def test_played_ordinary_queue_remains_editable_after_explicit_stop(api):
    selected(api)
    await api.execute(ParsedCommand("resume"), "")
    await api.execute(ParsedCommand("stop"), "")
    assert api.state.queue.current.resolved_url == api.state.queue.current.url
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        assert snapshot["actions"]["playlist_append"]
        calls = list(api.mpv.calls)
        assert (await command(session, base, snapshot["csrf_token"], payload("playlist_append", snapshot["generation"], urls=URLS)))[0] == 200
        assert api.mpv.calls == calls


async def test_full_web_envelope_limit_is_unchanged_including_json_escaping(api):
    request = payload(source=source("m3u", text="# " + "z" * 100 + "\n" + "\n".join(URLS)))
    body = json.dumps(request, separators=(",", ":")).encode()
    padding = MAX_REQUEST_BYTES - len(body)
    request["source"]["text"] = request["source"]["text"].replace("z" * 100, "z" * (100 + padding))
    body = json.dumps(request, separators=(",", ":")).encode()
    assert len(body) == MAX_REQUEST_BYTES == 32768
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        headers = {"Origin": base, "X-ORR-CSRF": snapshot["csrf_token"], "Content-Type": "application/json"}
        async with session.post(base + PREFIX + "/command", data=body + b" ", headers=headers) as response:
            assert response.status == 413
            assert (await response.json())["outcome"] == "REJECTED"
        assert api.state.generation == 0
        async with session.post(base + PREFIX + "/command", data=body, headers=headers) as response:
            assert response.status == 200
            assert await response.json() == {"request_acknowledged": True}
        assert not api.mpv.calls


async def test_invalid_indices_or_escaped_candidate_bounds_reject_before_effects(api):
    selected(api)
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        before = api.state.queue
        for proposal in [payload("playlist_remove", index=4), payload("playlist_move", from_index=1, to_index=4)]:
            code, body = await command(session, base, snapshot["csrf_token"], proposal)
            assert code == 400 and body["outcome"] == "REJECTED"
            assert api.state.queue is before and api.state.generation == 0
        before.items[0].metadata = "&" * 140000
        code, body = await command(session, base, snapshot["csrf_token"], payload("playlist_append", urls=URLS))
        assert code == 409 and body["outcome"] == "REJECTED"
        assert api.state.queue is before and api.state.generation == 0 and not api.mpv.calls


async def test_after_dispatch_failure_is_unknown_generic_and_does_not_retry(api, monkeypatch, caplog):
    replacement = api._replace_queue
    calls = 0
    async def fail_after_install(queue):
        nonlocal calls
        calls += 1
        await replacement(queue)
        raise RuntimeError("https://private.example.test/path?token=RAW_ERROR_SECRET")
    monkeypatch.setattr(api, "_replace_queue", fail_after_install)
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        code, body = await command(session, base, snapshot["csrf_token"], payload())
        assert code == 502 and body["outcome"] == "UNKNOWN"
        assert body["error"] == "command_failed"
        assert api.state.queue.count == 2 and api.state.generation == 1
        assert calls == 1 and not api.mpv.calls
        assert "SECRET" not in json.dumps(body) + caplog.text


@pytest.mark.parametrize("changes", [
    {"headers": {"X-ORR-CSRF": "not-the-session-token"}},
    {"headers": {"Origin": "https://foreign.example.test"}},
    {"headers": {"Sec-Fetch-Site": "cross-site"}},
    {"headers": {"Host": "foreign.example.test"}},
    {"headers": {"Content-Type": "text/plain"}},
    {"chunked": True},
])
async def test_new_mutations_retain_existing_guarded_browser_boundary(api, changes):
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        before = api.state.queue
        code, body = await command(session, base, snapshot["csrf_token"], payload(), **changes)
        assert code in {403, 411, 415} and body["outcome"] == "REJECTED"
        assert api.state.queue is before and api.state.generation == 0 and not api.mpv.calls


async def test_malformed_import_response_has_no_source_or_raw_error(api, caplog):
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        code, body = await command(session, base, snapshot["csrf_token"], payload(source={
            "format": "json", "text": 'invalid file contents: https://private.example.test/path?token=RAW_INPUT_SECRET'}))
        assert code == 400 and body["outcome"] == "REJECTED"
        assert api.state.generation == 0 and not api.mpv.calls
        assert all(value not in json.dumps(body) + caplog.text for value in
                   ("RAW_INPUT_SECRET", "private.example.test", "invalid file contents"))


def test_all_duplicate_move_positions_follow_the_selected_occurrence():
    for selected_index in range(1, 4):
        for from_index in range(1, 4):
            for to_index in range(1, 4):
                queue = canonical_queue()
                queue.index = selected_index
                before = deepcopy(queue)
                candidate = prepare_edit(queue, "playlist_move", from_index=from_index, to_index=to_index)
                assert candidate.current == before.current
                assert queue == before
