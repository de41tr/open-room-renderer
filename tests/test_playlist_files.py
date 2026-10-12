"""Authored-file schema and atomic commit tests use only synthetic content."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import stat

import pytest

from fake_linkplay import playlist_files as files

NAME = "Café 夜のラジオ 🎵"
URLS = ["https://media.example.test/音声/a%2fb?token=synthetic-secret&a=1+2#片",
        "http://[::1]:8080/track%2FB.wav?x=a%2520b&x=&case=%2f%2F"]


def playlist(**changes):
    return {"version": 1, "name": NAME, "index": 2, "mode": "repeat-all", "urls": URLS.copy(), **changes}


def test_round_trip_preserves_exact_text_and_owns_urls():
    original = playlist()
    encoded = files.encode_playlist(original)
    assert NAME.encode() in encoded and b"\\u" not in encoded
    assert encoded.endswith(b"\n")
    decoded = files.decode_playlist(encoded)
    assert decoded == original
    assert decoded is not original and decoded["urls"] is not original["urls"]
    assert [url.encode() for url in decoded["urls"]] == [url.encode() for url in URLS]
    validated = files.validate_playlist(original)
    original["urls"].append("https://changed.example.test/")
    assert validated["urls"] == URLS


@pytest.mark.parametrize("mode", files.PLAYLIST_MODES)
def test_every_supported_mode_round_trips(mode):
    assert files.decode_playlist(files.encode_playlist(playlist(mode=mode)))["mode"] == mode


@pytest.mark.parametrize("field,bad", [
    ("version", True), ("version", 1.0), ("version", "1"), ("version", 2), ("version", None),
    ("name", None), ("name", 3), ("name", True), ("name", ""), ("name", " \n\t"),
    ("index", False), ("index", True), ("index", "1"), ("index", 1.0),
    ("index", 0), ("index", -1), ("index", 3), ("index", None),
    ("mode", 4), ("mode", "NORMAL"), ("mode", "shuffle"), ("mode", None),
    ("urls", []), ("urls", URLS[0]), ("urls", tuple(URLS)), ("urls", [None, URLS[0]]),
    ("urls", [1, URLS[0]]), ("urls", [True, URLS[0]]), ("urls", ["", URLS[0]]),
    ("urls", ["  ", URLS[0]]), ("urls", [{"url": URLS[0]}, URLS[0]]),
    ("urls", [URLS[0]] * 1001),
])
def test_exact_types_and_ranges(field, bad):
    with pytest.raises(files.PlaylistError):
        files.validate_playlist(playlist(**{field: bad}))


@pytest.mark.parametrize("bad", [None, [], "playlist", 1, True])
def test_root_must_be_an_object(bad):
    with pytest.raises(files.PlaylistError):
        files.validate_playlist(bad)


@pytest.mark.parametrize("field", ["version", "name", "index", "mode", "urls"])
def test_missing_fields_rejected(field):
    payload = playlist()
    del payload[field]
    with pytest.raises(files.PlaylistError):
        files.validate_playlist(payload)


@pytest.mark.parametrize("field", ["resolved_url", "history", "provenance", "title", "state", "extra"])
def test_unknown_fields_rejected(field):
    with pytest.raises(files.PlaylistError):
        files.validate_playlist(playlist(**{field: "synthetic-secret"}))


@pytest.mark.parametrize("field", ["name", "urls"])
@pytest.mark.parametrize("character", ["\x00", "\x01", "\x08", "\x0b", "\x0c", "\x1f", "\ud800", "\udfff", "\ufffe", "\uffff"])
def test_invalid_xml_characters_rejected(field, character):
    value = "synthetic-secret" + character
    with pytest.raises(files.PlaylistError):
        files.validate_playlist(playlist(**{field: value if field == "name" else [value, URLS[0]]}))


@pytest.mark.parametrize("field", ["version", "name", "index", "mode", "urls"])
def test_duplicate_fields_rejected(field):
    encoded = files.encode_playlist(playlist()).decode().rstrip()
    duplicate = json.dumps(playlist()[field])
    malformed = encoded[:-1] + f', "{field}": {duplicate}' + "}"
    with pytest.raises(files.PlaylistError):
        files.decode_playlist(malformed.encode())


@pytest.mark.parametrize("encoded", [
    b"\xff", b"\xff\xfe{}", b"\xef\xbb\xbf{}", b"{} trailing", b"", b"not json",
    b'{"version":NaN}', b'{"version":Infinity}', b'{"version":-Infinity}',
    b'[' * 1100 + b']' * 1100,
    b'{"version":' + b'9' * 5000 + b'}',
], ids=["invalid-utf8", "utf16", "bom", "trailing", "empty", "not-json", "nan", "infinity", "negative-infinity", "deep", "huge-int"])
def test_malformed_and_nonfinite_documents(encoded):
    with pytest.raises(files.PlaylistError) as caught:
        files.decode_playlist(encoded)
    assert str(caught.value) in {"Invalid authored playlist document", "Unsupported authored playlist document"}


def test_count_boundary():
    payload = playlist(urls=[URLS[0]] * 1000, index=1000)
    assert len(files.decode_playlist(files.encode_playlist(payload))["urls"]) == 1000


def test_bytes_boundary_is_utf8_bytes_not_character_count():
    payload = playlist(name="x")
    overhead = len(files.encode_playlist(payload)) - 1
    exact = playlist(name="a" * (files.MAX_PLAYLIST_BYTES - overhead))
    encoded = files.encode_playlist(exact)
    assert len(encoded) == files.MAX_PLAYLIST_BYTES
    assert files.decode_playlist(encoded) == exact
    with pytest.raises(files.PlaylistError):
        files.encode_playlist(playlist(name=exact["name"] + "é"))
    with pytest.raises(files.PlaylistError):
        files.decode_playlist(encoded + b" ")


def test_load_is_bounded(tmp_path, monkeypatch):
    path = tmp_path / "playlist.json"
    path.write_bytes(b" " * (files.MAX_PLAYLIST_BYTES + 100))
    real_fdopen = os.fdopen
    reads = []
    class Reader:
        def __init__(self, fd, mode):
            self.stream = real_fdopen(fd, mode)
        def __enter__(self):
            return self
        def read(self, limit):
            reads.append(limit)
            return self.stream.read(limit)
        def __exit__(self, *args):
            self.stream.close()
    monkeypatch.setattr(os, "fdopen", Reader)
    with pytest.raises(files.PlaylistError):
        files.load_playlist(path)
    assert reads == [files.MAX_PLAYLIST_BYTES + 1]


def test_write_no_clobber_and_explicit_overwrite_are_private(tmp_path):
    path = tmp_path / "authored.json"
    payload = playlist()
    files.save_playlist(path, payload)
    assert files.load_playlist(path) == payload
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    original = path.read_bytes()
    with pytest.raises(files.PlaylistFileExistsError):
        files.save_playlist(path, playlist(name="Replacement"))
    assert path.read_bytes() == original
    files.save_playlist(path, playlist(name="Replacement"), overwrite=True)
    assert files.load_playlist(path)["name"] == "Replacement"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert set(tmp_path.iterdir()) == {path}


def test_no_clobber_is_race_safe(tmp_path, monkeypatch):
    path = tmp_path / "authored.json"
    original_link = os.link
    def race(source, destination):
        Path(destination).write_bytes(b"other writer won")
        return original_link(source, destination)
    monkeypatch.setattr(os, "link", race)
    with pytest.raises(files.PlaylistFileExistsError):
        files.save_playlist(path, playlist())
    assert path.read_bytes() == b"other writer won"
    assert set(tmp_path.iterdir()) == {path}


@pytest.mark.parametrize("dangling", [False, True])
def test_no_clobber_does_not_follow_symlinks(tmp_path, dangling):
    target = tmp_path / "target"
    if not dangling:
        target.write_bytes(b"untouched")
    path = tmp_path / "authored.json"
    path.symlink_to(target)
    with pytest.raises(files.PlaylistFileExistsError):
        files.save_playlist(path, playlist())
    assert path.is_symlink()
    assert not target.exists() if dangling else target.read_bytes() == b"untouched"


def test_explicit_overwrite_rejects_symlink_and_preserves_target(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"untouched")
    path = tmp_path / "authored.json"
    path.symlink_to(target)
    with pytest.raises(files.PlaylistFileTypeError):
        files.save_playlist(path, playlist(), overwrite=True)
    assert path.is_symlink()
    assert target.read_bytes() == b"untouched"


def test_parent_is_not_created(tmp_path):
    path = tmp_path / "missing" / "authored.json"
    with pytest.raises(FileNotFoundError):
        files.save_playlist(path, playlist())
    assert list(tmp_path.iterdir()) == []


def test_validation_precedes_temporary_creation(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid input must not create a temporary file")
    monkeypatch.setattr(files.tempfile, "mkstemp", forbidden)
    with pytest.raises(files.PlaylistError):
        files.save_playlist(tmp_path / "new.json", playlist(version=True))
    with pytest.raises(files.PlaylistError):
        files.save_playlist(tmp_path / "new.json", playlist(), overwrite=1)


@pytest.mark.parametrize("overwrite", [False, True])
@pytest.mark.parametrize("stage", ["fchmod", "fdopen", "file_fsync", "commit"])
def test_precommit_failures_preserve_destination(tmp_path, monkeypatch, overwrite, stage):
    path = tmp_path / "authored.json"
    if overwrite:
        path.write_bytes(b"original")
    def fail(*args, **kwargs):
        raise OSError(errno.EIO, "synthetic-secret")
    if stage == "commit":
        monkeypatch.setattr(os, "replace" if overwrite else "link", fail)
    elif stage == "file_fsync":
        monkeypatch.setattr(os, "fsync", fail)
    else:
        monkeypatch.setattr(os, stage, fail)
    with pytest.raises(OSError) as caught:
        files.save_playlist(path, playlist(), overwrite=overwrite)
    assert not isinstance(caught.value, files.PlaylistCommittedError)
    assert path.read_bytes() == b"original" if overwrite else not path.exists()
    assert set(tmp_path.iterdir()) == ({path} if overwrite else set())


@pytest.mark.parametrize("overwrite", [False, True])
@pytest.mark.parametrize("stage", ["directory_open", "directory_fsync"])
def test_postcommit_failures_are_distinct_and_truthful(tmp_path, monkeypatch, overwrite, stage):
    path = tmp_path / "authored.json"
    if overwrite:
        path.write_bytes(b"original")
    original_open, original_fsync = os.open, os.fsync
    def fail_open(target, flags, *args, **kwargs):
        if flags & os.O_DIRECTORY:
            raise OSError(errno.EIO, "synthetic-secret")
        return original_open(target, flags, *args, **kwargs)
    def fail_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "synthetic-secret")
        return original_fsync(fd)
    monkeypatch.setattr(os, "open" if stage == "directory_open" else "fsync",
                        fail_open if stage == "directory_open" else fail_fsync)
    with pytest.raises(files.PlaylistCommittedError) as caught:
        files.save_playlist(path, playlist(), overwrite=overwrite)
    assert "synthetic-secret" not in str(caught.value)
    assert files.load_playlist(path) == playlist()
    assert set(tmp_path.iterdir()) == {path}


def test_no_clobber_postcommit_cleanup_failure_keeps_truthful_status(tmp_path, monkeypatch):
    path = tmp_path / "authored.json"
    real_unlink = os.unlink
    calls = 0
    def fail_once(target):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.EIO, "synthetic-secret")
        return real_unlink(target)
    monkeypatch.setattr(os, "unlink", fail_once)
    with pytest.raises(files.PlaylistCommittedError):
        files.save_playlist(path, playlist())
    assert files.load_playlist(path) == playlist()
    assert set(tmp_path.iterdir()) == {path}


def test_precommit_cleanup_failure_does_not_mask_primary(tmp_path, monkeypatch):
    path = tmp_path / "authored.json"
    path.write_bytes(b"original")
    def fail(*args, **kwargs):
        raise OSError(errno.EIO, "synthetic-secret")
    monkeypatch.setattr(os, "unlink", fail)
    with pytest.raises(files.PlaylistFileExistsError):
        files.save_playlist(path, playlist())
    assert path.read_bytes() == b"original"
    leftovers = list(tmp_path.glob(".orr-playlist-*"))
    assert len(leftovers) == 1 and stat.S_IMODE(leftovers[0].stat().st_mode) == 0o600


def test_commit_order_and_same_directory_private_temp(tmp_path, monkeypatch):
    path = tmp_path / "authored.json"
    events = []
    real_fsync, real_link, real_unlink = os.fsync, os.link, os.unlink
    def fsync(fd):
        events.append("directory_fsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file_fsync")
        return real_fsync(fd)
    def link(source, destination):
        source = Path(source)
        assert source.parent == path.parent and destination == path
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
        assert files.load_playlist(source) == playlist()
        events.append("link")
        return real_link(source, destination)
    def unlink(source):
        events.append("unlink")
        return real_unlink(source)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(os, "unlink", unlink)
    files.save_playlist(path, playlist())
    assert events == ["file_fsync", "link", "unlink", "directory_fsync"]


@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink", "dangling-symlink"])
def test_load_rejects_special_files(tmp_path, kind):
    path = tmp_path / "input"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        target = tmp_path / "target"
        if kind == "symlink":
            files.save_playlist(target, playlist())
        path.symlink_to(target)
    with pytest.raises(files.PlaylistFileTypeError):
        files.load_playlist(path)


def test_load_rejects_character_device():
    with pytest.raises(files.PlaylistFileTypeError):
        files.load_playlist("/dev/null")


@pytest.mark.parametrize("kind", ["fifo", "directory", "dangling-symlink"])
def test_overwrite_rejects_special_files_before_temp_creation(tmp_path, monkeypatch, kind):
    path = tmp_path / "output"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        path.symlink_to(tmp_path / "absent")
    before = path.lstat()
    def forbidden(*args, **kwargs):
        pytest.fail("special target must reject before temporary creation")
    monkeypatch.setattr(files.tempfile, "mkstemp", forbidden)
    with pytest.raises(files.PlaylistFileTypeError):
        files.save_playlist(path, playlist(), overwrite=True)
    assert path.lstat() == before


def test_overwrite_rechecks_target_type_before_commit(tmp_path, monkeypatch):
    path = tmp_path / "output"
    path.write_bytes(b"original")
    original_fsync = os.fsync
    def race(fd):
        original_fsync(fd)
        if stat.S_ISREG(os.fstat(fd).st_mode):
            path.unlink()
            os.mkfifo(path)
    monkeypatch.setattr(os, "fsync", race)
    with pytest.raises(files.PlaylistFileTypeError):
        files.save_playlist(path, playlist(), overwrite=True)
    assert stat.S_ISFIFO(path.lstat().st_mode)
    assert set(tmp_path.iterdir()) == {path}


@pytest.mark.parametrize("stage", ["write", "flush", "close"])
def test_stream_failure_preserves_previous_file(tmp_path, monkeypatch, stage):
    path = tmp_path / "authored.json"
    path.write_bytes(b"original")
    real_fdopen = os.fdopen
    class BrokenStream:
        def __init__(self, descriptor, mode):
            self.stream = real_fdopen(descriptor, mode)
        def __enter__(self):
            return self
        def write(self, value):
            self.stream.write(value)
            if stage == "write":
                raise OSError(errno.EIO, "synthetic-secret")
        def flush(self):
            self.stream.flush()
            if stage == "flush":
                raise OSError(errno.EIO, "synthetic-secret")
        def fileno(self):
            return self.stream.fileno()
        def __exit__(self, *args):
            self.stream.close()
            if stage == "close":
                raise OSError(errno.EIO, "synthetic-secret")
    monkeypatch.setattr(os, "fdopen", BrokenStream)
    with pytest.raises(OSError) as caught:
        files.save_playlist(path, playlist(), overwrite=True)
    assert not isinstance(caught.value, files.PlaylistCommittedError)
    assert path.read_bytes() == b"original"
    assert set(tmp_path.iterdir()) == {path}


def test_temporary_open_failure_preserves_existing_file(tmp_path, monkeypatch):
    path = tmp_path / "authored.json"
    path.write_bytes(b"original")
    def fail(*args, **kwargs):
        raise OSError(errno.ENOSPC, "synthetic-secret")
    monkeypatch.setattr(files.tempfile, "mkstemp", fail)
    with pytest.raises(OSError) as caught:
        files.save_playlist(path, playlist(), overwrite=True)
    assert not isinstance(caught.value, files.PlaylistCommittedError)
    assert path.read_bytes() == b"original"
    assert set(tmp_path.iterdir()) == {path}
