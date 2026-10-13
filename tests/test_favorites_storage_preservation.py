"""Synthetic saved-favorites storage refusal, store and loopback HTTP surfaces.

Only disposable files, links, and a mock backend; no native or external service.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from fake_linkplay import favorites as favorites_module
from fake_linkplay.capabilities import configured_presets
from fake_linkplay.config import Config, PodcastPreset
from fake_linkplay.favorites import FavoriteError, FavoritesStore
from fake_linkplay.http_api import HttpApi
from fake_linkplay.state import RendererState
from test_web_favorites_diagnostics import COMMAND_FAILED, UNAVAILABLE, favorites
from test_web_ui import WebBackend, command, selected, serving, state


BASE = "/orr/v1/favorites"
SAVED_ID = "a" * 32
URL = "https://media.example.test/new.wav"
SAVED_BYTES = (b'{"version":1,"favorites":[{"id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
               b'"name":"PRIVATE_SAVED_NAME","url":"https://media.example.test/saved.wav?token=PRIVATE_TOKEN"}]}\n')
LEGACY_UNAVAILABLE = {
    "error": "library_unavailable",
    "message": ("Managed favorites could not be loaded. Repair or restore favorites.json "
                "and restart; the file has not been overwritten."),
}
OPERATIONS = ["add", "current", "rename", "target", "delete"]


def config(path):
    return Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(path),
                  advertise_ip="127.0.0.1", default_volume=50,
                  podcast_presets=[PodcastPreset("Configured", "https://media.example.test/configured.wav")])


@pytest.fixture(params=["dangling", "dangling-restored", "existing-entry-read-missing",
                        "absence-check-denied", "absence-check-io"])
def unavailable_storage(tmp_path, monkeypatch, request):
    path = tmp_path / "favorites.json"
    target = tmp_path / "PRIVATE_target.json"
    is_link = request.param.startswith("dangling")
    if is_link:
        path.symlink_to(target.name)
        link_bytes = os.readlink(os.fsencode(path))
        link_identity = path.lstat().st_ino
    else:
        path.write_bytes(SAVED_BYTES)
    original_open, original_lstat = Path.open, Path.lstat

    def read_missing(candidate, *args, **kwargs):
        if candidate == path:
            raise FileNotFoundError("PRIVATE_READ_ERROR " + str(path))
        return original_open(candidate, *args, **kwargs)

    def uncertain_absence(candidate, *args, **kwargs):
        if candidate == path:
            error = PermissionError if request.param == "absence-check-denied" else OSError
            raise error("PRIVATE_STAT_ERROR " + str(path))
        return original_lstat(candidate, *args, **kwargs)

    with monkeypatch.context() as startup:
        if not is_link:
            startup.setattr(Path, "open", read_missing)
        if request.param.startswith("absence-check-"):
            startup.setattr(Path, "lstat", uncertain_absence)
        cfg = config(tmp_path)
        api = HttpApi(cfg, RendererState.from_config(cfg), WebBackend())
    if request.param == "dangling-restored":
        # A later external repair does not clear an observed failure latch.
        target.write_bytes(SAVED_BYTES)
    selected(api, count=1)
    writes = []

    def unexpected_write(*args, **kwargs):
        writes.append((args, kwargs))
        raise AssertionError("Unavailable storage must never reach the writer")

    monkeypatch.setattr(favorites_module, "atomic_write_text", unexpected_write)

    def assert_preserved():
        assert writes == []
        if is_link:
            assert path.is_symlink()
            assert path.lstat().st_ino == link_identity
            assert os.readlink(os.fsencode(path)) == link_bytes
            if request.param == "dangling-restored":
                assert target.read_bytes() == SAVED_BYTES
                assert path.read_bytes() == SAVED_BYTES
            else:
                assert not target.exists()
                assert not path.exists()
        else:
            assert path.read_bytes() == SAVED_BYTES
        assert not list(tmp_path.glob(".favorites.json.*"))
        assert not api.mpv.calls

    return api, assert_preserved


def mutate_store(store, operation, current):
    if operation == "add":
        return store.add({"name": "New", "url": URL})
    if operation == "current":
        return store.add_current("Current", current)
    if operation == "rename":
        return store.rename(SAVED_ID, "Renamed")
    if operation == "target":
        return store.set_url(SAVED_ID, URL, "clear")
    return store.delete(SAVED_ID)


@pytest.mark.parametrize("operation", OPERATIONS)
def test_failed_absence_check_latches_store_and_preserves_storage(unavailable_storage, operation):
    api, assert_preserved = unavailable_storage
    for call in [api.favorites.document,
                 lambda: api.favorites.playback_snapshot(SAVED_ID),
                 lambda: mutate_store(api.favorites, operation, api.state.queue.current)]:
        with pytest.raises(FavoriteError) as raised:
            call()
        assert (raised.value.status, raised.value.code, str(raised.value)) == (
            503, LEGACY_UNAVAILABLE["error"], LEGACY_UNAVAILABLE["message"])
        assert_preserved()
    assert api.state.effective_presets() == configured_presets(api.cfg)
    assert api.favorites.playback_snapshot("configured:1") == configured_presets(api.cfg)[0]


def web_payload(operation, snapshot):
    fields = {
        "add": {"name": "New", "url": URL},
        "current": {"name": "Current", "expected_generation": snapshot["generation"]},
        "rename": {"id": SAVED_ID, "name": "Renamed"},
        "target": {"id": SAVED_ID, "url": URL, "metadata": "clear"},
        "delete": {"id": SAVED_ID},
    }
    return {"action": "favorite_" + operation, **fields[operation]}


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_failed_absence_check_keeps_web_error_categories_and_storage(unavailable_storage, operation):
    api, assert_preserved = unavailable_storage
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        assert "PRIVATE_" not in json.dumps(snapshot)
        assert await favorites(session, base) == (503, UNAVAILABLE)
        assert await command(session, base, snapshot["csrf_token"], web_payload(operation, snapshot)) == (
            502, COMMAND_FAILED)
        assert await favorites(session, base) == (503, UNAVAILABLE)
        assert_preserved()
    assert_preserved()


def legacy_request(operation):
    return {
        "add": ("POST", BASE, {"json": {"name": "New", "url": URL}}),
        "current": ("POST", BASE + "/current", {"json": {"name": "Current"}}),
        "rename": ("PATCH", BASE + "/" + SAVED_ID, {"json": {"name": "Renamed"}}),
        "target": ("PUT", BASE + "/" + SAVED_ID + "/target", {"json": {"url": URL, "metadata": "clear"}}),
        "delete": ("DELETE", BASE + "/" + SAVED_ID, {}),
    }[operation]


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_failed_absence_check_keeps_legacy_json_errors_and_storage(unavailable_storage, operation):
    api, assert_preserved = unavailable_storage
    async with serving(api) as (session, base):
        for method, path, kwargs in [("GET", BASE, {}), legacy_request(operation)]:
            async with session.request(method, base + path, **kwargs) as response:
                assert (response.status, await response.json()) == (503, LEGACY_UNAVAILABLE)
            assert_preserved()
    assert_preserved()


@pytest.mark.parametrize("kind", ["missing-file", "missing-directory", "valid-file", "valid-symlink"])
def test_healthy_storage_and_existing_valid_symlink_semantics_are_unchanged(tmp_path, kind):
    root = tmp_path / "new-directory" if kind == "missing-directory" else tmp_path
    path, target = root / "favorites.json", root / "target.json"
    if kind == "valid-file":
        path.write_bytes(SAVED_BYTES)
    elif kind == "valid-symlink":
        target.write_bytes(SAVED_BYTES)
        path.symlink_to(target.name)
    store = FavoritesStore(config(root))
    managed = store.document()["favorites"][1:]
    assert [entry["id"] for entry in managed] == ([SAVED_ID] if kind.startswith("valid-") else [])
    if kind == "valid-symlink":
        assert path.is_symlink()
        assert target.read_bytes() == SAVED_BYTES
    added = store.add({"name": "New", "url": URL})
    assert added["editable"] is True
    assert FavoritesStore(store.cfg).document() == store.document()
    assert not path.is_symlink()  # Existing successful atomic replacement behavior.
    if kind == "valid-symlink":
        assert target.read_bytes() == SAVED_BYTES
    assert not list(root.glob(".favorites.json.*"))


async def test_dangling_target_repair_requires_new_store_before_healthy_http_responses(tmp_path):
    path, target = tmp_path / "favorites.json", tmp_path / "target.json"
    path.symlink_to(target.name)
    cfg = config(tmp_path)
    api = HttpApi(cfg, RendererState.from_config(cfg), WebBackend())
    target.write_bytes(SAVED_BYTES)
    with pytest.raises(FavoriteError, match="could not be loaded"):
        api.favorites.document()
    assert path.is_symlink() and target.read_bytes() == SAVED_BYTES
    restarted = HttpApi(cfg, RendererState.from_config(cfg), WebBackend())
    async with serving(restarted) as (session, base):
        snapshot = await state(session, base)
        code, document = await favorites(session, base)
        assert code == 200
        assert document["session_id"] == snapshot["session_id"]
        assert [entry["id"] for entry in document["favorites"]] == ["configured:1", SAVED_ID]
        assert "PRIVATE_TOKEN" not in json.dumps(document)
        assert path.is_symlink() and target.read_bytes() == SAVED_BYTES
        async with session.post(base + BASE, json={"name": "New", "url": URL}) as response:
            assert response.status == 201
        status, response = await command(session, base, snapshot["csrf_token"], {
            "action": "favorite_add", "name": "Web new", "url": URL})
        assert status == 200 and response["request_acknowledged"] is True
        assert len(restarted.favorites.document()["favorites"]) == 4
        assert not restarted.mpv.calls
    assert not path.is_symlink()
    assert target.read_bytes() == SAVED_BYTES
