"""Offline session-storage checks with disposable files and synthetic tokens."""
from dataclasses import FrozenInstanceError, replace
import json
import os
import stat
import traceback

import pytest

from fake_linkplay import tidal_session
from fake_linkplay.tidal_session import (
    MAX_SESSION_BYTES, TidalSession, TidalSessionError, TidalSessionStore,
)

ACCESS = "synthetic-access-token"
REFRESH = "synthetic-refresh-token"


@pytest.fixture
def session():
    return TidalSession(ACCESS, REFRESH, 1234567890.5, "synthetic-user", "ZZ")


def perform(store, operation, session):
    return store.save(session) if operation == "save" else getattr(store, operation)()


def test_session_is_immutable_and_repr_excludes_tokens_and_user(session):
    assert ACCESS not in repr(session) and REFRESH not in repr(session)
    assert "synthetic-user" not in repr(session)
    with pytest.raises(FrozenInstanceError):
        session.access_token = "synthetic-replacement"
    assert TidalSession(ACCESS).refresh_token is None


@pytest.mark.parametrize("changes", [
    {"access_token": ""}, {"access_token": " \t"}, {"access_token": []},
    {"access_token": "x" * 16385}, {"refresh_token": ""}, {"refresh_token": []},
    {"expires_at": float("nan")}, {"expires_at": float("inf")},
    {"expires_at": -1}, {"expires_at": True}, {"expires_at": "123"},
    {"user_id": ""}, {"user_id": 42}, {"country_code": "zz"}, {"country_code": "ZZZ"},
])
def test_session_rejects_invalid_values_without_echoing_content(session, changes):
    with pytest.raises(TidalSessionError) as error:
        replace(session, **changes)
    assert ACCESS not in str(error.value) and REFRESH not in repr(error.value)


def test_roundtrip_replace_and_clear_touch_only_dedicated_storage(tmp_path, session):
    config = tmp_path / "config.yaml"
    favorites = tmp_path / "favorites.json"
    config.write_text("synthetic config")
    favorites.write_text("synthetic favorites")
    directory = tmp_path / "private-tidal"
    store = TidalSessionStore(directory)
    assert store.load() is None and not store.clear() and not directory.exists()
    store.save(session)
    path = directory / "session.json"
    assert store.load() == session
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert directory.stat().st_uid == path.stat().st_uid == os.geteuid()
    # Saving an already-expired or unknown expiry is storage, not authentication.
    replacement = TidalSession("synthetic-new-access", expires_at=0)
    store.save(replacement)
    assert TidalSessionStore(directory).load() == replacement
    marker = directory / "unrelated.txt"
    marker.write_text("leave this alone")
    assert store.clear() and store.load() is None and not store.clear()
    assert marker.read_text() == "leave this alone" and directory.is_dir()
    assert config.read_text() == "synthetic config"
    assert favorites.read_text() == "synthetic favorites"


@pytest.mark.parametrize("path", ["relative", "/", "//tmp/private", "/tmp/../private", "/tmp/./private", "/tmp/\0private"])
def test_requires_an_explicit_absolute_normalized_directory(path):
    with pytest.raises(TidalSessionError):
        TidalSessionStore(path)


@pytest.mark.parametrize("operation", ["load", "save", "clear"])
@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "fifo", "directory", "mode"])
def test_refuses_unsafe_session_entries_without_touching_target(tmp_path, session, operation, unsafe):
    store = TidalSessionStore(tmp_path / "private")
    store.save(session)
    path = store.directory / "session.json"
    original = path.read_bytes()
    outside = tmp_path / "outside"
    if unsafe in ("symlink", "hardlink"):
        path.rename(outside)
        if unsafe == "symlink":
            path.symlink_to(outside)
        else:
            os.link(outside, path)
    elif unsafe in ("fifo", "directory"):
        path.unlink()
        os.mkfifo(path, 0o600) if unsafe == "fifo" else path.mkdir(mode=0o700)
    else:
        path.chmod(0o644)
    before = path.lstat()
    with pytest.raises(TidalSessionError):
        perform(store, operation, session)
    after = path.lstat()
    assert (before.st_ino, before.st_mode) == (after.st_ino, after.st_mode)
    if outside.exists():
        assert outside.read_bytes() == original


@pytest.mark.parametrize("operation", ["load", "save", "clear"])
@pytest.mark.parametrize("unsafe", ["directory_mode", "ancestor_mode", "directory_symlink", "ancestor_symlink"])
def test_refuses_unsafe_directory_or_ancestor(tmp_path, session, operation, unsafe):
    ancestor = tmp_path / "ancestor"
    ancestor.mkdir(mode=0o700)
    directory = ancestor / "private"
    store = TidalSessionStore(directory)
    store.save(session)
    original = (directory / "session.json").read_bytes()
    if unsafe == "directory_mode":
        directory.chmod(0o750)
    elif unsafe == "ancestor_mode":
        ancestor.chmod(0o777)
    elif unsafe == "directory_symlink":
        alias = ancestor / "alias"
        alias.symlink_to(directory, target_is_directory=True)
        store = TidalSessionStore(alias)
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(ancestor, target_is_directory=True)
        store = TidalSessionStore(alias / "private")
    with pytest.raises(TidalSessionError):
        perform(store, operation, session)
    assert (directory / "session.json").read_bytes() == original


@pytest.mark.parametrize("operation", ["load", "save", "clear"])
def test_rejects_other_owner_without_changing_permissions(tmp_path, monkeypatch, session, operation):
    store = TidalSessionStore(tmp_path / "private")
    store.save(session)
    real_stat = os.stat

    def other_owner(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if path == "session.json":
            values = list(info)
            values[4] = os.geteuid() + 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(tidal_session.os, "stat", other_owner)
    with pytest.raises(TidalSessionError):
        perform(store, operation, session)
    assert stat.S_IMODE((store.directory / "session.json").stat().st_mode) == 0o600


@pytest.mark.parametrize("contents", [
    b"not JSON synthetic-access-token", b"\xff", b"[]",
    b'{"schema":1,"schema":1}', b"[" * 1200 + b"]" * 1200,
])
def test_malformed_files_raise_only_redacted_errors(tmp_path, session, contents):
    store = TidalSessionStore(tmp_path / "private")
    store.save(session)
    (store.directory / "session.json").write_bytes(contents)
    with pytest.raises(TidalSessionError) as error:
        store.load()
    rendered = "".join(traceback.format_exception(error.value))
    assert ACCESS not in str(error.value) and ACCESS not in rendered
    assert REFRESH not in rendered


@pytest.mark.parametrize("changes", [
    {"schema": True}, {"schema": 2}, {"unexpected": ACCESS}, {"access_token": ""},
    {"refresh_token": []}, {"expires_at": float("nan")}, {"country_code": 42},
])
def test_rejects_untrusted_saved_schema_and_values(tmp_path, session, changes):
    store = TidalSessionStore(tmp_path / "private")
    store.save(session)
    path = store.directory / "session.json"
    value = json.loads(path.read_bytes())
    value.update(changes)
    path.write_text(json.dumps(value))
    with pytest.raises(TidalSessionError):
        store.load()


def test_large_file_is_rejected_before_read_and_large_encoding_before_write(tmp_path, monkeypatch, session):
    store = TidalSessionStore(tmp_path / "private")
    store.save(session)
    path = store.directory / "session.json"
    path.write_bytes(b"x" * (MAX_SESSION_BYTES + 1))

    def no_read(*args, **kwargs):
        pytest.fail("oversized input must not be opened as a stream")

    monkeypatch.setattr(tidal_session.os, "fdopen", no_read)
    with pytest.raises(TidalSessionError, match="size limit"):
        store.load()
    oversized = TidalSession("\U0001f340" * 16384, "\U0001f340" * 16384)
    with pytest.raises(TidalSessionError, match="size limit"):
        store.save(oversized)
    assert path.stat().st_size == MAX_SESSION_BYTES + 1


@pytest.mark.parametrize("failure", ["replace", "fsync"])
def test_failed_atomic_save_preserves_previous_session_and_removes_temp(tmp_path, monkeypatch, session, failure):
    store = TidalSessionStore(tmp_path / "private")
    store.save(session)
    original = (store.directory / "session.json").read_bytes()

    def fail(*args, **kwargs):
        raise OSError(ACCESS)

    monkeypatch.setattr(tidal_session.os, failure, fail)
    with pytest.raises(TidalSessionError) as error:
        store.save(TidalSession("synthetic-new-access"))
    assert ACCESS not in "".join(traceback.format_exception(error.value))
    assert (store.directory / "session.json").read_bytes() == original
    assert list(store.directory.iterdir()) == [store.directory / "session.json"]


def test_save_replaces_complete_private_file_atomically(tmp_path, monkeypatch, session):
    store = TidalSessionStore(tmp_path / "private")
    store.save(session)
    original = (store.directory / "session.json").read_bytes()
    real_replace = os.replace
    calls = []

    def inspect_replace(source, target, **kwargs):
        assert (store.directory / target).read_bytes() == original
        temporary = store.directory / source
        assert stat.S_IMODE(temporary.stat().st_mode) == 0o600
        assert json.loads(temporary.read_bytes())["access_token"] == "synthetic-new-access"
        assert kwargs["src_dir_fd"] == kwargs["dst_dir_fd"]
        calls.append(target)
        return real_replace(source, target, **kwargs)

    monkeypatch.setattr(tidal_session.os, "replace", inspect_replace)
    store.save(TidalSession("synthetic-new-access"))
    assert calls == ["session.json"]
    assert store.load() == TidalSession("synthetic-new-access")
