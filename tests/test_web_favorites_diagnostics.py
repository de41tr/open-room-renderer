"""Saved-favorites diagnostics: loopback HTTP and a synthetic audio backend.

No browser JavaScript, native audio, provider accounts, or external requests.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fake_linkplay.capabilities import configured_presets
from fake_linkplay.config import Config, PodcastPreset
from fake_linkplay.favorites import FavoriteError
from fake_linkplay.http_api import HttpApi
from fake_linkplay.state import RendererState
from fake_linkplay.web_ui import PREFIX
from test_web_ui import WebBackend, api, command, selected, serving, state


UNAVAILABLE = {
    "error": "favorites_unavailable",
    "message": ("Managed favorites could not be loaded. Stop the renderer service, "
                "repair or restore its saved favorites library, then restart it."),
    "outcome": "ERROR",
}
READ_FAILED = {
    "error": "read_failed",
    "message": "Renderer state could not be read.",
    "outcome": "ERROR",
}
COMMAND_FAILED = {
    "error": "command_failed",
    "message": "Command outcome is unknown. Refresh before any explicit retry.",
    "outcome": "UNKNOWN",
}
SAVED = {
    "id": "a" * 32,
    "name": "PRIVATE_SAVED_NAME",
    "url": "https://private.example.test/saved.wav?token=PRIVATE_SAVED_TOKEN",
    "title": "PRIVATE_SAVED_TITLE",
}
CONFIGURED_URL = "https://media.example.test/configured.wav"


def saved_bytes(records: list[dict[str, object]]) -> bytes:
    return json.dumps({"version": 1, "favorites": records}).encode("utf-8")


@pytest.fixture(params=["malformed-json", "invalid-second-record", "unreadable"])
def unavailable_api(tmp_path, monkeypatch, request):
    path = tmp_path / "favorites.json"
    encoded = saved_bytes([SAVED])
    if request.param == "malformed-json":
        encoded = encoded[:-2]
    elif request.param == "invalid-second-record":
        encoded = saved_bytes([SAVED, {**SAVED, "id": "invalid"}])
    path.write_bytes(encoded)
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000",
                 state_dir=str(tmp_path), advertise_ip="127.0.0.1", default_volume=50,
                 podcast_presets=[PodcastPreset("Configured station", CONFIGURED_URL)])
    # Do not rely on chmod: elevated test runners can still read mode-000 files.
    original_open = Path.open

    def unreadable(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("PRIVATE_READ_ERROR " + str(path))
        return original_open(candidate, *args, **kwargs)

    with monkeypatch.context() as startup:
        if request.param == "unreadable":
            startup.setattr(Path, "open", unreadable)
        result = HttpApi(cfg, RendererState.from_config(cfg), WebBackend())
    assert path.read_bytes() == encoded
    return result, encoded


async def favorites(session, base):
    async with session.get(base + PREFIX + "/favorites") as response:
        return response.status, await response.json()


async def test_unavailable_startup_is_exact_safe_error_without_partial_library(unavailable_api):
    api, encoded = unavailable_api
    assert api.state.effective_presets() == configured_presets(api.cfg)
    with pytest.raises(FavoriteError) as raised:
        api.favorites.document()
    assert raised.value.code == "library_unavailable"
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        assert snapshot["session_id"]
        assert "PRIVATE_" not in json.dumps(snapshot)
        assert await favorites(session, base) == (503, UNAVAILABLE)
        # The complete response has no session ID, configured prefix, partial
        # valid record, raw error, saved label, URL, or repair-file path.
        assert await favorites(session, base) == (503, UNAVAILABLE)
        assert not api.mpv.calls
    assert api.favorites.path.read_bytes() == encoded


@pytest.mark.parametrize("kind,status", [
    ("host", 403), ("origin", 403), ("cross-site", 403), ("same-site", 403),
    ("method", 405), ("query", 400), ("body", 400),
    ("content-encoding", 400), ("chunked", 400),
])
async def test_favorites_guards_reject_before_reading_library(api, monkeypatch, kind, status):
    reads = []

    def fail_read():
        reads.append(True)
        raise FavoriteError("library_unavailable", "PRIVATE_LIBRARY_ERROR", 503)

    monkeypatch.setattr(api.favorites, "document", fail_read)
    async with serving(api) as (session, base):
        method, path, headers, kwargs = "GET", PREFIX + "/favorites", {}, {}
        if kind == "host":
            headers["Host"] = "attacker.example.test:" + base.rsplit(":", 1)[-1]
        elif kind == "origin":
            headers["Origin"] = "https://attacker.example.test"
        elif kind in {"cross-site", "same-site"}:
            headers["Sec-Fetch-Site"] = kind
        elif kind == "method":
            method = "POST"
        elif kind == "query":
            path += "?value=PRIVATE_QUERY"
        elif kind == "body":
            kwargs["data"] = b"PRIVATE_BODY"
        elif kind == "content-encoding":
            headers["Content-Encoding"] = "identity"
        elif kind == "chunked":
            kwargs.update(chunked=True, data=b"PRIVATE_BODY")
        async with session.request(method, base + path, headers=headers, **kwargs) as response:
            body = await response.json()
            assert response.status == status
            assert body["outcome"] == "REJECTED"
            assert body["error"] != "favorites_unavailable"
            assert "session_id" not in body and "favorites" not in body
            assert "PRIVATE_" not in json.dumps(body)
        assert reads == []
        assert not api.mpv.calls


class CodedReadError(RuntimeError):
    code = "library_unavailable"
    status = 503


def read_error(kind):
    if kind == "library-unavailable":
        return FavoriteError("library_unavailable", "PRIVATE_LIBRARY_ERROR", 503)
    if kind == "other-favorite":
        return FavoriteError("library_full", "PRIVATE_LIBRARY_ERROR", 503)
    if kind == "not-found":
        return FavoriteError("not_found", "PRIVATE_LIBRARY_ERROR", 404)
    return {"runtime": RuntimeError, "oserror": OSError,
            "lookalike-code": CodedReadError}[kind]("PRIVATE_LIBRARY_ERROR")


@pytest.mark.parametrize("kind", ["other-favorite", "not-found", "runtime", "oserror", "lookalike-code"])
async def test_other_favorites_read_errors_remain_generic(api, monkeypatch, kind):
    def fail_read():
        raise read_error(kind)

    monkeypatch.setattr(api.favorites, "document", fail_read)
    async with serving(api) as (session, base):
        assert await favorites(session, base) == (503, READ_FAILED)
        assert not api.mpv.calls


@pytest.mark.parametrize("kind", ["library-unavailable", "other-favorite", "runtime", "oserror"])
async def test_state_read_errors_remain_generic_even_for_unavailable_favorites(api, monkeypatch, kind):
    def fail_read():
        raise read_error(kind)

    monkeypatch.setattr(api.web_api, "snapshot", fail_read)
    async with serving(api) as (session, base):
        async with session.get(base + PREFIX + "/state") as response:
            assert response.status == 503
            assert await response.json() == READ_FAILED
        assert not api.mpv.calls


async def test_unavailable_favorite_add_stays_dispatched_unknown_and_preserves_file(unavailable_api):
    api, encoded = unavailable_api
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        assert await command(session, base, snapshot["csrf_token"], {
            "action": "favorite_add", "name": "New saved station",
            "url": "https://media.example.test/new.wav?token=PRIVATE_NEW_TOKEN",
        }) == (502, COMMAND_FAILED)
        assert await favorites(session, base) == (503, UNAVAILABLE)
        assert not api.mpv.calls
    assert api.favorites.path.read_bytes() == encoded


@pytest.mark.parametrize("kind", ["other-favorite", "runtime", "oserror"])
async def test_other_favorite_add_errors_stay_dispatched_unknown(api, monkeypatch, kind):
    def fail_add(payload):
        raise read_error(kind)

    monkeypatch.setattr(api.favorites, "add", fail_add)
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        assert await command(session, base, snapshot["csrf_token"], {
            "action": "favorite_add", "name": "New saved station",
            "url": "https://media.example.test/new.wav",
        }) == (502, COMMAND_FAILED)
        assert not api.mpv.calls
    assert not api.favorites.path.exists()


async def test_unavailable_library_keeps_playback_and_configured_legacy_recall(unavailable_api):
    api, encoded = unavailable_api
    selected(api, count=1)
    ordinary_url = api.state.queue.current.url
    async with serving(api) as (session, base):
        before = await state(session, base)
        csrf = before["csrf_token"]
        assert await command(session, base, csrf, {
            "action": "play", "expected_generation": before["generation"],
        }) == (200, {"request_acknowledged": True})
        assert api.mpv.url == ordinary_url
        assert (await state(session, base))["transport"]["phase"] == "PLAYING"
        assert await favorites(session, base) == (503, UNAVAILABLE)
        assert await command(session, base, csrf, {"action": "stop"}) == (
            200, {"request_acknowledged": True})
        async with session.get(base + "/httpapi.asp", params={"command": "MCUKeyShortClick:1"}) as response:
            assert response.status == 200
        assert api.mpv.url == CONFIGURED_URL
        assert api.state.queue.current.id == "preset:1"
        assert (await state(session, base))["transport"]["phase"] == "PLAYING"
        assert await favorites(session, base) == (503, UNAVAILABLE)
    assert api.favorites.path.read_bytes() == encoded


async def test_repair_stays_latched_until_restart_then_saved_favorites_recover(unavailable_api):
    api, _ = unavailable_api
    repaired = saved_bytes([SAVED])
    async with serving(api) as (session, base):
        before = await state(session, base)
        assert await favorites(session, base) == (503, UNAVAILABLE)
        # Even restored readability or valid replacement bytes do not silently
        # turn an already-failed store into an editable empty/partial library.
        api.favorites.path.write_bytes(repaired)
        assert await favorites(session, base) == (503, UNAVAILABLE)
        assert await command(session, base, before["csrf_token"], {
            "action": "favorite_add", "name": "Do not overwrite repair",
            "url": "https://media.example.test/new.wav",
        }) == (502, COMMAND_FAILED)
        assert api.favorites.path.read_bytes() == repaired
    restarted = HttpApi(api.cfg, RendererState.from_config(api.cfg), WebBackend())
    async with serving(restarted) as (session, base):
        fresh = await state(session, base)
        code, document = await favorites(session, base)
        assert code == 200
        assert set(document) == {"version", "session_id", "favorites"}
        assert document["session_id"] == fresh["session_id"] != before["session_id"]
        assert [entry["id"] for entry in document["favorites"]] == ["configured:1", SAVED["id"]]
        restored = document["favorites"][1]
        assert restored["name"] == SAVED["name"]
        assert restored["title"] == SAVED["title"]
        assert restored["editable"] is True
        assert "url" not in restored and "PRIVATE_SAVED_TOKEN" not in json.dumps(document)
        assert restarted.favorites.document()["favorites"][1]["url"] == SAVED["url"]
        assert not restarted.mpv.calls
    assert api.favorites.path.read_bytes() == repaired
