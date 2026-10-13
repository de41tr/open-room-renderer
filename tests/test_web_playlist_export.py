"""Bounded private Web export: pure/in-memory HTTP tests, no media or sockets."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiohttp.test_utils import make_mocked_request
from multidict import CIMultiDict
import pytest

from fake_linkplay.config import Config
from fake_linkplay.playlist_files import MAX_PLAYLIST_BYTES, decode_playlist
from fake_linkplay.queue import PlaybackQueue, QueueItem
from fake_linkplay.queue_playlist import encode_current_playlist
from fake_linkplay.queue_policy import ShuffleTraversal
from fake_linkplay.queue_protocol import MODES
from fake_linkplay.state import PlaybackPhase, RendererState
from fake_linkplay import web_ui
from fake_linkplay.web_ui import (MAX_REQUEST_BYTES, MAX_SAFE_INTEGER, PREFIX, WebAdmissionError,
    WebControlApi, WebRuntime, decode_command, decode_playlist_export, prepare_playlist_export)

PATH = PREFIX + "/playlist/export"
ORIGIN = "http://renderer.example.test:8099"
FILENAME = "open-room-renderer-playlist.json"
URL = 'https://media.example.test/音声/a%2fb?token=SYNTHETIC_SECRET&a=1+2&quote="q"#片'
TEMPORARY = "https://stream.example.test/temporary?token=TEMPORARY_SECRET"
PUBLIC = "https://opml.radiotime.com/Tune.ashx?id=s12345&formats=mp3,aac&render=json"


def queue():
    return PlaybackQueue(name="Café 夜 🎵", items=[QueueItem(URL),
        QueueItem("https://media.example.test/second"), QueueItem(URL)], index=2)


def forbidden(*args, **kwargs):
    pytest.fail("Private export must not call backend, resolver, runtime, persistence or files")


@pytest.fixture
def control():
    state = RendererState.from_config(Config())
    state.queue = queue()
    state.generation = 7
    calls = []
    def current():
        calls.append(True)
        return state
    api = WebControlApi(current_state=current, device_name=forbidden, runtime=forbidden,
        dsp_snapshot=forbidden, favorites=SimpleNamespace(document=forbidden),
        dispatch=forbidden, allowed_hosts=("renderer.example.test",))
    return api, state, calls


def payload(api, state, action="prepare", **fields):
    result = {"action": action, "expected_session": api._session_id,
              "expected_generation": state.generation}
    if action == "download":
        result.update(expected_export="a" * 64, private_export_confirmed=True)
    return {**result, **fields}


def request(api, body, *, path=PATH, method="POST", headers=None, content=None):
    encoded = body if isinstance(body, bytes) else json.dumps(body).encode()
    values = CIMultiDict({"Host": "renderer.example.test:8099", "Origin": ORIGIN,
        "X-ORR-CSRF": api._csrf_token, "Content-Type": "application/json",
        "Content-Length": str(len(encoded))})
    if headers is not None:
        values = CIMultiDict(headers)
    transport = Mock()
    transport.get_extra_info.side_effect = lambda key, default=None: (
        ("192.0.2.1", 8099) if key == "sockname" else default)
    return make_mocked_request(method, path, headers=values, transport=transport,
        payload=content or SimpleNamespace(readexactly=AsyncMock(return_value=encoded)))


async def send(control, action="prepare", **fields):
    api, state, _ = control
    return await api.handle(request(api, payload(api, state, action, **fields)))


def assert_error(response, status, code):
    assert response.status == status
    messages = {
        "export_unavailable": "This queue cannot be exported for browser import. For supported larger queues, use playlist save-current FILE in the CLI.",
        "stale_export": "The queue or renderer session changed. Reopen Export and review it again.",
    }
    assert json.loads(response.body) == {"error": code, "message": messages.get(
        code, "Browser request was not admitted."), "outcome": "REJECTED"}
    assert b"SECRET" not in response.body
    assert b"https://" not in response.body
    for name, value in web_ui._HEADERS.items():
        assert response.headers[name] == value
    assert "Content-Disposition" not in response.headers
    assert "Access-Control-Allow-Origin" not in response.headers


def import_envelope(encoded, generation=MAX_SAFE_INTEGER):
    return json.dumps({"action": "playlist_replace", "expected_generation": generation,
        "source": {"format": "json", "text": encoded.decode()}}, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()


@pytest.mark.parametrize("mode,loop", MODES.items())
async def test_exact_round_trip_and_fixed_download_headers(control, mode, loop):
    api, state, calls = control
    state.queue.loop = loop
    encoded = encode_current_playlist(state.queue)
    prepared = await send(control)
    assert prepared.status == 200 and calls == [True]
    summary = json.loads(prepared.body)
    assert summary == {"version": 1, "session_id": api._session_id, "generation": 7,
        "revision": hmac.new(api._export_key, b"orr:web-playlist-export:v1\0" + encoded,
                             hashlib.sha256).hexdigest(),
        "name": state.queue.name, "count": 3, "index": 2, "mode": mode,
        "size_bytes": len(encoded), "filename": FILENAME}
    assert b"SECRET" not in prepared.body and b"https://" not in prepared.body
    response = await send(control, "download", expected_export=summary["revision"])
    assert calls == [True, True]
    assert response.status == 200 and response.body == encoded
    assert response.headers["Content-Type"] == "application/json"
    assert response.headers["Content-Length"] == str(len(encoded))
    assert response.headers["Content-Disposition"] == f'attachment; filename="{FILENAME}"'
    for name, value in web_ui._HEADERS.items():
        assert response.headers[name] == value
    assert "Access-Control-Allow-Origin" not in response.headers
    document = decode_playlist(response.body)
    restored = decode_command(import_envelope(response.body)).source
    assert document == {"version": 1, "name": state.queue.name, "index": 2, "mode": mode,
                        "urls": [URL, state.queue.items[1].url, URL]}
    assert [item.url for item in restored.items] == document["urls"]
    assert restored.name == document["name"] and restored.index == 2 and restored.loop == loop


@pytest.mark.parametrize("action", ["prepare", "download"])
@pytest.mark.parametrize("field,value", [
    ("expected_generation", True), ("expected_generation", False), ("expected_generation", -1),
    ("expected_generation", MAX_SAFE_INTEGER + 1), ("expected_generation", 7.0),
    ("expected_generation", "7"), ("expected_generation", None),
    ("expected_session", None), ("expected_session", 7), ("expected_session", "é"),
    ("filename", "PRIVATE_SECRET.json"), ("path", "/private/SECRET"), ("overwrite", True),
    ("url", URL), ("source", {"format": "json", "text": "SECRET"}),
])
async def test_exact_request_variants_reject_extra_or_wrong_types(control, action, field, value):
    assert_error(await send(control, action, **{field: value}), 400, "invalid_request")
    assert not control[2]


@pytest.mark.parametrize("field", ["action", "expected_session", "expected_generation"])
async def test_prepare_requires_each_field(control, field):
    api, state, calls = control
    body = payload(api, state)
    del body[field]
    assert_error(await api.handle(request(api, body)), 400, "invalid_request")
    assert not calls


@pytest.mark.parametrize("value", [False, 1, "true", None, [], {}])
async def test_download_requires_literal_consent(control, value):
    assert_error(await send(control, "download", private_export_confirmed=value), 400, "invalid_request")
    assert not control[2]


@pytest.mark.parametrize("field", ["expected_export", "private_export_confirmed"])
async def test_download_requires_consent_and_revision_fields(control, field):
    api, state, calls = control
    body = payload(api, state, "download")
    del body[field]
    assert_error(await api.handle(request(api, body)), 400, "invalid_request")
    assert not calls


@pytest.mark.parametrize("value", [None, 1, "", "A" * 64, "a" * 63, "a" * 65, "g" * 64, "a" * 64 + "\n"])
async def test_invalid_export_tokens_reject_before_capture(control, value):
    assert_error(await send(control, "download", expected_export=value), 400, "invalid_request")
    assert not control[2]


@pytest.mark.parametrize("extra", [{"expected_export": "a" * 64}, {"private_export_confirmed": True}])
async def test_prepare_cannot_smuggle_download_fields(control, extra):
    assert_error(await send(control, **extra), 400, "invalid_request")
    assert not control[2]


@pytest.mark.parametrize("body", [b"null", b"[]", b"{}", b'{"action":"save"}',
    b'{"action":"prepare","action":"prepare"}',
    b'{"action":"prepare","expected_session":"a","expected_session":"a","expected_generation":7}',
    b'{"action":"prepare","expected_session":"a","expected_generation":NaN}',
    b'{"action":"prepare","expected_session":"a","expected_generation":Infinity}',
    b'{"action":"prepare","expected_session":"a","expected_generation":1e9999}',
    b'{"action":"prepare"}\xff', b'\xef\xbb\xbf{"action":"prepare"}',
    b'{"action":"prepare"} trailing', b"[" * 1200 + b"]" * 1200])
async def test_same_strict_json_boundary(control, body):
    api, _, calls = control
    assert_error(await api.handle(request(api, body)), 400, "invalid_request")
    assert not calls
    with pytest.raises(WebAdmissionError):
        decode_playlist_export(body)


def test_decoder_byte_limit_and_safe_integer_endpoints():
    for generation in (0, MAX_SAFE_INTEGER):
        body = json.dumps({"action": "prepare", "expected_session": "ascii",
                           "expected_generation": generation}).encode()
        assert decode_playlist_export(body).expected_generation == generation
        assert decode_playlist_export(body + b" " * (MAX_REQUEST_BYTES - len(body))).expected_generation == generation
        with pytest.raises(WebAdmissionError):
            decode_playlist_export(body + b" " * (MAX_REQUEST_BYTES + 1 - len(body)))


@pytest.mark.parametrize("action", ["prepare", "download"])
@pytest.mark.parametrize("field,value", [("expected_session", "another-session"),
    ("expected_session", ""), ("expected_generation", 6), ("expected_generation", 8)])
async def test_session_and_generation_checked_before_serialization(control, monkeypatch, action, field, value):
    monkeypatch.setattr(web_ui, "encode_current_playlist", forbidden)
    assert_error(await send(control, action, **{field: value}), 409, "stale_export")
    assert control[2] == [True]


@pytest.mark.parametrize("edit", ["mode", "name", "url", "index", "count", "session", "generation"])
async def test_download_rechecks_every_exported_change(control, edit):
    api, state, _ = control
    summary = json.loads((await send(control)).body)
    if edit == "mode": state.queue.loop = MODES["shuffle-once"]
    elif edit == "name": state.queue.name = "Renamed"
    elif edit == "url": state.queue.items[-1].url += "changed"
    elif edit == "index": state.queue.index = 3
    elif edit == "count": state.queue.items.append(QueueItem(URL))
    elif edit == "session": api._session_id = "new-session"
    elif edit == "generation": state.generation += 1
    assert_error(await send(control, "download", expected_export=summary["revision"],
        expected_session=summary["session_id"], expected_generation=summary["generation"]), 409, "stale_export")


async def test_same_generation_private_names_are_fenced_without_preview_disclosure(control):
    api, state, _ = control
    state.queue.name = "https://private.example.test/name?token=NAME_SECRET"
    summary = json.loads((await send(control)).body)
    assert summary["name"] == "Current queue" and "SECRET" not in json.dumps(summary)
    encoded = (await send(control, "download", expected_export=summary["revision"])).body
    assert b"NAME_SECRET" in encoded  # Exact name is disclosed only in consented bytes.
    state.queue.name += "changed"
    next_summary = json.loads((await send(control)).body)
    assert next_summary["name"] == summary["name"] and next_summary["revision"] != summary["revision"]
    assert_error(await send(control, "download", expected_export=summary["revision"]), 409, "stale_export")


async def test_revisions_are_process_keyed_stable_and_not_plain_hashes(control):
    api, state, _ = control
    one = json.loads((await send(control)).body)
    two = json.loads((await send(control)).body)
    assert one == two
    assert one["revision"] != hashlib.sha256(encode_current_playlist(state.queue)).hexdigest()
    api._export_key = b"another process key"
    three = json.loads((await send(control)).body)
    assert three["revision"] != one["revision"]
    assert_error(await send(control, "download", expected_export=one["revision"]), 409, "stale_export")


async def test_unexported_metadata_and_playback_changes_do_not_invalidate(control):
    api, state, _ = control
    summary = json.loads((await send(control)).body)
    expected = encode_current_playlist(state.queue)
    state.queue.items[0].title = "Edited title"
    state.queue.items[0].metadata = "Changed metadata"
    state.queue.items[0].resolved_url = TEMPORARY
    state.queue._shuffle = ShuffleTraversal((2, 1, 3), 1)
    state.curpos = 27
    state.phase = PlaybackPhase.PLAYING
    response = await send(control, "download", expected_export=summary["revision"])
    assert response.status == 200 and response.body == expected
    assert b"TEMPORARY_SECRET" not in response.body


@pytest.mark.parametrize("phase", list(PlaybackPhase))
async def test_read_only_ownership_no_state_io_or_dispatch_in_any_phase(control, monkeypatch, phase):
    api, state, _ = control
    state.phase = phase
    state.queue._shuffle = ShuffleTraversal((2, 1, 3), 1)
    before = deepcopy(state)
    original_queue, original_items, original_shuffle = state.queue, tuple(state.queue.items), state.queue._shuffle
    monkeypatch.setattr(web_ui, "files", forbidden)
    monkeypatch.setattr("builtins.open", forbidden)
    summary = json.loads((await send(control)).body)
    assert (await send(control, "download", expected_export=summary["revision"])).status == 200
    assert state == before and state.queue is original_queue and state.queue._shuffle is original_shuffle
    assert all(actual is original for actual, original in zip(state.queue.items, original_items))
    assert api._revision == 0
    assert not any(isinstance(value, (bytes, PlaybackQueue)) for key, value in vars(api).items()
                   if key not in {"_dsp_key", "_export_key"})


async def test_capture_after_body_read_has_no_yield_until_response(control):
    api, state, calls = control
    original = state.queue
    read = AsyncMock()
    async def read_body(length):
        state.generation += 1
        return json.dumps(payload(api, state)).encode()
    read.side_effect = read_body
    def capture():
        calls.append(True)
        asyncio.get_running_loop().call_soon(setattr, state, "queue", PlaybackQueue())
        return state
    api._current_state = capture
    response = await api.handle(request(api, payload(api, state), content=SimpleNamespace(readexactly=read)))
    assert response.status == 200 and json.loads(response.body)["generation"] == 8
    assert state.queue is original and calls == [True]
    await asyncio.sleep(0)
    assert state.queue is not original


async def test_stale_after_slow_body_rejected_before_capture_encoding(control, monkeypatch):
    api, state, calls = control
    encoded = json.dumps(payload(api, state)).encode()
    async def read_body(length):
        state.generation += 1
        return encoded
    monkeypatch.setattr(web_ui, "encode_current_playlist", forbidden)
    response = await api.handle(request(api, encoded, content=SimpleNamespace(readexactly=read_body)))
    assert_error(response, 409, "stale_export")
    assert calls == [True]


async def test_polling_does_not_prepare_encode_or_cache_exports(control, monkeypatch):
    api, state, _ = control
    api._device_name = lambda: "Renderer"
    api._runtime = lambda: WebRuntime(actions=frozenset({"stop"}))
    api._dsp_snapshot = lambda: {"name": ""}
    api._favorites = SimpleNamespace(document=lambda: {"favorites": []})
    monkeypatch.setattr(web_ui, "encode_current_playlist", forbidden)
    monkeypatch.setattr(web_ui, "prepare_source", forbidden)
    for _ in range(2):
        snapshot = api.snapshot()
        favorites = api.favorites_snapshot()
        assert "SECRET" not in json.dumps(snapshot) + json.dumps(favorites)
    assert "export" not in snapshot and "export" not in snapshot["queue"]


@pytest.mark.parametrize("changed,status,code", [
    ({"Host": None}, 403, "forbidden"), ({"Host": "foreign.example.test:8099"}, 403, "forbidden"),
    ({"Host": "renderer.example.test:1"}, 403, "forbidden"),
    ({"Host": "user@renderer.example.test:8099"}, 403, "forbidden"),
    ({"Origin": None}, 403, "forbidden"), ({"Origin": "null"}, 403, "forbidden"),
    ({"Origin": ORIGIN + "/"}, 403, "forbidden"),
    ({"Origin": "https://foreign.example.test"}, 403, "forbidden"),
    ({"X-ORR-CSRF": None}, 403, "forbidden"), ({"X-ORR-CSRF": "wrong"}, 403, "forbidden"),
    ({"X-ORR-CSRF": "é"}, 403, "forbidden"),
    ({"Sec-Fetch-Site": "cross-site"}, 403, "forbidden"),
    ({"Sec-Fetch-Site": "same-site"}, 403, "forbidden"),
    ({"Content-Type": None}, 415, "unsupported_media_type"),
    ({"Content-Type": "text/plain"}, 415, "unsupported_media_type"),
    ({"Content-Type": "application/json; charset=ascii"}, 415, "unsupported_media_type"),
    ({"Content-Type": "application/json; extra=value"}, 415, "unsupported_media_type"),
    ({"Content-Encoding": "gzip"}, 415, "unsupported_encoding"),
    ({"Content-Length": None}, 411, "length_required"),
    ({"Transfer-Encoding": "chunked"}, 411, "length_required"),
    ({"Content-Length": "-1"}, 400, "invalid_request"),
    ({"Content-Length": "1.0"}, 400, "invalid_request"),
    ({"Content-Length": "123456789"}, 400, "invalid_request"),
    ({"Content-Length": str(MAX_REQUEST_BYTES + 1)}, 413, "request_too_large"),
])
async def test_existing_admission_statuses_before_body_or_capture(control, changed, status, code):
    api, state, calls = control
    body = payload(api, state)
    headers = CIMultiDict(request(api, body).headers)
    for key, value in changed.items():
        if value is None: headers.popall(key, None)
        else: headers[key] = value
    content = SimpleNamespace(readexactly=AsyncMock(side_effect=AssertionError("must not read")))
    response = await api.handle(request(api, body, headers=headers, content=content))
    assert_error(response, status, code)
    assert not calls and content.readexactly.await_count == 0


@pytest.mark.parametrize("header,value,status,code", [
    ("Host", "renderer.example.test:8099", 403, "forbidden"),
    ("Origin", ORIGIN, 403, "forbidden"), ("X-ORR-CSRF", "same", 403, "forbidden"),
    ("Sec-Fetch-Site", "same-origin", 403, "forbidden"),
    ("Content-Type", "application/json", 415, "unsupported_media_type"),
    ("Content-Encoding", "identity", 415, "unsupported_encoding"),
    ("Content-Length", "100", 411, "length_required"),
])
async def test_duplicate_headers_reuse_exact_guards(control, header, value, status, code):
    api, state, calls = control
    body = payload(api, state)
    headers = CIMultiDict(request(api, body).headers)
    headers[header] = value
    headers.add(header, value)
    response = await api.handle(request(api, body, headers=headers))
    assert_error(response, status, code)
    assert not calls


@pytest.mark.parametrize("method,path,status,code", [
    *((method, PATH, 405, "method_not_allowed") for method in ("GET", "HEAD", "OPTIONS", "PUT", "DELETE", "PATCH")),
    ("POST", PATH + "/", 404, "not_found"), ("POST", PATH + "/extra", 404, "not_found"),
    ("POST", PREFIX + "/playlist/%65xport", 404, "not_found"),
    ("POST", PREFIX + "/playlist%2fexport", 404, "not_found"),
    ("POST", PATH + "?token=QUERY_SECRET", 403, "forbidden"),
])
async def test_method_path_and_query_admission(control, method, path, status, code):
    api, state, calls = control
    response = await api.handle(request(api, payload(api, state), path=path, method=method))
    assert_error(response, status, code)
    assert not calls


@pytest.mark.parametrize("error,status,code", [
    (asyncio.IncompleteReadError(b"PRIVATE_SECRET", 100), 400, "invalid_request"),
    (ConnectionError("PRIVATE_SECRET"), 400, "invalid_request"),
    (TimeoutError("PRIVATE_SECRET"), 408, "request_timeout"),
])
async def test_bounded_body_failures_never_capture(control, monkeypatch, error, status, code):
    api, state, calls = control
    timeout, observed = asyncio.timeout, []
    def record_timeout(seconds):
        observed.append(seconds)
        return timeout(seconds)
    monkeypatch.setattr(asyncio, "timeout", record_timeout)
    content = SimpleNamespace(readexactly=AsyncMock(side_effect=error))
    response = await api.handle(request(api, payload(api, state), content=content))
    assert_error(response, status, code)
    assert observed == [5] and not calls
    content.readexactly.assert_awaited_once_with(len(json.dumps(payload(api, state)).encode()))


async def test_cancelled_body_never_produces_export(control):
    api, state, calls = control
    content = SimpleNamespace(readexactly=AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await api.handle(request(api, payload(api, state), content=content))
    assert not calls


async def test_identity_encoding_and_json_charset_remain_supported(control):
    api, state, _ = control
    body = payload(api, state)
    headers = CIMultiDict(request(api, body).headers)
    headers.update({"Content-Encoding": "IDENTITY", "Content-Type": 'application/json; charset="utf-8"'})
    assert (await api.handle(request(api, body, headers=headers))).status == 200


@pytest.mark.parametrize("unit", ["x", '"', "音"])
def test_full_worst_generation_nested_envelope_exact_limit(unit):
    item = QueueItem("https://media.example.test/" + unit * 100)
    candidate = PlaybackQueue(name="Bound", items=[item], index=1)
    available = MAX_REQUEST_BYTES - len(import_envelope(encode_current_playlist(candidate)))
    item.url += "x" * available
    exact = encode_current_playlist(candidate)
    assert len(exact) < MAX_REQUEST_BYTES
    assert len(import_envelope(exact)) == MAX_REQUEST_BYTES
    assert prepare_playlist_export(candidate)[0] == exact
    assert decode_command(import_envelope(exact)).source.items[0].url == item.url
    item.url += "x"
    oversized = encode_current_playlist(candidate)
    assert len(oversized) < MAX_REQUEST_BYTES
    assert len(import_envelope(oversized)) == MAX_REQUEST_BYTES + 1
    # A short current generation can fit; the gate still reserves the full safe range.
    assert len(import_envelope(oversized, 0)) <= MAX_REQUEST_BYTES
    with pytest.raises(WebAdmissionError) as failure:
        prepare_playlist_export(candidate)
    assert (failure.value.code, failure.value.status) == ("export_unavailable", 409)


def test_quotes_can_double_nested_import_size_without_changing_codec_bytes():
    candidate = PlaybackQueue(items=[QueueItem('https://media.example.test/' + '"' * 8200)], index=1)
    encoded = encode_current_playlist(candidate)
    assert len(encoded) < MAX_REQUEST_BYTES < len(import_envelope(encoded))
    with pytest.raises(WebAdmissionError):
        prepare_playlist_export(candidate)


@pytest.mark.parametrize("name", ["x" * 257, "音" * 86, "line\nbreak", "tab\tname", "del\x7fname"])
def test_cli_valid_names_that_web_import_rejects_remain_unavailable(name):
    candidate = queue()
    candidate.name = name
    assert encode_current_playlist(candidate)
    with pytest.raises(WebAdmissionError) as failure:
        prepare_playlist_export(candidate)
    assert failure.value.code == "export_unavailable"


def test_count_authored_and_soap_bounds_are_not_relaxed():
    candidate = PlaybackQueue(items=[QueueItem("http://e.test")] * 1000, index=1000)
    encoded, restored = prepare_playlist_export(candidate)
    assert len(decode_playlist(encoded)["urls"]) == restored.count == 1000
    candidate.items.append(QueueItem("http://e.test"))
    with pytest.raises(WebAdmissionError):
        prepare_playlist_export(candidate)
    for suffix in ("x" * MAX_PLAYLIST_BYTES, "&" * 120000):
        candidate = PlaybackQueue(items=[QueueItem("https://media.example.test/?q=" + suffix)], index=1)
        with pytest.raises(ValueError):
            encode_current_playlist(candidate)
        with pytest.raises(WebAdmissionError):
            prepare_playlist_export(candidate)


@pytest.mark.parametrize("target,field,value", [
    ("queue", "items", None), ("queue", "items", [None]), ("queue", "index", True),
    ("queue", "index", 4), ("queue", "loop", 5), ("queue", "loop", True),
    ("queue", "name", ""), ("queue", "name", " spaced "),
    ("item", "url", "file:///PRIVATE_SECRET"),
    ("item", "url", "https://user:PRIVATE_SECRET@media.example.test/a"),
    ("item", "url", "http://127.0.0.1:8080/%70roxy/audio?PRIVATE_SECRET"),
    ("item", "url", "https://media.example.test/a b"),
    ("item", "url", "https://media.example.test:0/a"),
    ("item", "url", "https:///PRIVATE_SECRET"),
    ("item", "title", "bad\ud800PRIVATE_SECRET"),
])
async def test_invalid_last_entry_or_structure_is_all_or_nothing(control, target, field, value, caplog):
    api, state, _ = control
    setattr(state.queue if target == "queue" else state.queue.items[-1], field, value)
    assert_error(await send(control), 409, "export_unavailable")
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize("candidate", [PlaybackQueue(), PlaybackQueue(source="newTuneIn", stream_id="s12345")])
async def test_empty_and_unresolved_provider_queues_reject(control, candidate):
    control[1].queue = candidate
    assert_error(await send(control), 409, "export_unavailable")


async def test_provider_fields_do_not_change_existing_encoder_eligibility(control):
    api, state, _ = control
    state.queue.source = "provider-private-identity"
    state.queue.search_url = "https://private.example.test/search?token=SEARCH_SECRET"
    state.queue.items[-1].source = "provider"
    state.queue.items[-1].id = "provider-private-id"
    summary = json.loads((await send(control)).body)
    assert "provider" not in json.dumps(summary) and "SECRET" not in json.dumps(summary)
    response = await send(control, "download", expected_export=summary["revision"])
    assert response.body == encode_current_playlist(state.queue)
    assert b"provider" not in response.body and b"SEARCH_SECRET" not in response.body


@pytest.mark.parametrize("provenance,stream_type,duration", [
    ((), "station", 0), ((TEMPORARY,), "station", 0),
    ((TEMPORARY, "s12345", "extra"), "station", 0),
    ([TEMPORARY, "s12345"], "station", 0), ({"url": TEMPORARY, "station_id": "s12345"}, "station", 0),
    ("s12345", "station", 0), (False, "station", 0),
    ((URL, "s12345"), "station", 0), ((None, "s12345"), "station", 0),
    ((TEMPORARY, "s12345"), "track", 0), ((TEMPORARY, "s12345"), "", 0),
    ((TEMPORARY, "s12345"), "station", 30),
    *(( (TEMPORARY, "s12345"), "station", duration) for duration in (float("nan"), False, None, "0", 0j)),
    *(( (TEMPORARY, station), "station", 0) for station in
      (None, 12345, "", "S12345", "t12345", "s１２３", "s1\n", "s1&account=PRIVATE_SECRET", "s" + "1" * 33)),
])
async def test_complete_inactive_radio_provenance_validation(control, provenance, stream_type, duration, caplog):
    api, state, _ = control
    item = state.queue.items[-1]
    item.url = TEMPORARY
    item._radio_station, item.stream_type, item.duration = provenance, stream_type, duration
    assert_error(await send(control), 409, "export_unavailable")
    assert item._radio_station is provenance and "SECRET" not in caplog.text


async def test_proven_radio_projects_public_reference_never_temporary_stream(control):
    api, state, _ = control
    item = state.queue.items[-1]
    item.url = TEMPORARY
    item.resolved_url = TEMPORARY + "resolved"
    item._radio_station, item.stream_type, item.duration = (TEMPORARY, "s12345"), "station", 0
    summary = json.loads((await send(control)).body)
    response = await send(control, "download", expected_export=summary["revision"])
    assert response.status == 200 and b"TEMPORARY_SECRET" not in response.body
    assert decode_playlist(response.body)["urls"][-1] == PUBLIC
    assert item.url == TEMPORARY and item.resolved_url == TEMPORARY + "resolved"


async def test_unproven_station_never_uses_provider_or_metadata_as_authority(control):
    item = control[1].queue.items[-1]
    item.url, item.source, item.id, item.stream_type = PUBLIC, "newTuneIn", "s12345", "station"
    item.metadata = '{"_radio_station": ["temporary", "s12345"]}'
    assert_error(await send(control), 409, "export_unavailable")
    item.stream_type = ""
    assert (await send(control)).status == 200


async def test_unavailable_download_recomputes_instead_of_returning_cached_preview(control):
    api, state, _ = control
    summary = json.loads((await send(control)).body)
    state.queue.items[-1].url = "file:///PRIVATE_SECRET"
    assert_error(await send(control, "download", expected_export=summary["revision"]), 409, "export_unavailable")


async def test_export_variants_are_not_playback_commands(control):
    api, state, calls = control
    for action in ("prepare", "download"):
        response = await api.handle(request(api, payload(api, state, action), path=PREFIX + "/command"))
        assert_error(response, 400, "invalid_request")
    assert not calls


@pytest.mark.parametrize("error", [ValueError(URL), TypeError(URL), OverflowError(URL), RecursionError(URL)])
async def test_encoder_failure_messages_are_never_disclosed(control, monkeypatch, caplog, error):
    def fail(_queue):
        raise error
    monkeypatch.setattr(web_ui, "encode_current_playlist", fail)
    assert_error(await send(control), 409, "export_unavailable")
    assert "SECRET" not in caplog.text and "media.example.test" not in caplog.text


async def test_download_bytes_are_detached_from_later_queue_changes(control):
    api, state, _ = control
    summary = json.loads((await send(control)).body)
    encoded = encode_current_playlist(state.queue)
    response = await send(control, "download", expected_export=summary["revision"])
    state.queue.items.clear()
    state.queue.name = "Changed after capture"
    assert response.status == 200 and response.body == encoded
    assert len(decode_playlist(response.body)["urls"]) == 3


@pytest.mark.parametrize("length", [0, 1])
async def test_body_is_decoded_only_within_declared_length(control, length):
    api, state, calls = control
    encoded = json.dumps(payload(api, state)).encode()
    headers = CIMultiDict(request(api, encoded).headers)
    headers["Content-Length"] = str(length)
    content = SimpleNamespace(readexactly=AsyncMock(side_effect=lambda size: encoded[:size]))
    assert_error(await api.handle(request(api, encoded, headers=headers, content=content)),
                 400, "invalid_request")
    content.readexactly.assert_awaited_once_with(length)
    assert not calls
