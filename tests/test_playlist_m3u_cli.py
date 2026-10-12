"""Offline import validation and safe publication, with synthetic private data."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys

import aiohttp
import pytest
import requests

from fake_linkplay import cli, playlist_cli, playlist_files
from fake_linkplay.playlist_files import MAX_PLAYLIST_BYTES
from fake_linkplay.queue_client import MAX_SOAP_BYTES, explicit_queue, prepare_replace
from test_playlist_m3u import URLS

ROOT = Path(__file__).resolve().parents[1]
NAME = "Imported café 夜"
SUCCESS = "M3U URL list imported. Titles and durations were not saved.\n"


def arguments(source, target, *, name=NAME):
    return ["playlist", "import-m3u", str(source), str(target), "--name", name]


def source_file(tmp_path, encoded=None):
    source = tmp_path / "input-synthetic-secret.m3u8"
    source.write_bytes(encoded if encoded is not None else "\n".join(URLS).encode())
    return source


def expected(urls=URLS, name=NAME):
    return {"version": 1, "name": name, "index": 1, "mode": "normal", "urls": urls}


def private_failure(capsys):
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err
    assert not any(secret in captured.err for secret in ("synthetic-secret", "https://", "Traceback"))
    return captured.err


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline import must not construct a client/session or use a socket")
    monkeypatch.setattr(playlist_cli, "QueueClient", forbidden)
    monkeypatch.setattr(requests, "Session", forbidden)
    monkeypatch.setattr(aiohttp, "ClientSession", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def test_import_exact_private_json_without_network_or_metadata(tmp_path, capsys):
    urls = [*URLS, URLS[0]]
    source = source_file(tmp_path, ("\ufeff#EXTM3U\r\n#EXTINF:-1,discarded secret title\r\n"
                                   + "\r\n".join(urls)).encode())
    original = source.read_bytes()
    target = tmp_path / "output-synthetic-secret.json"
    assert cli.main(arguments(source, target)) == 0
    assert json.loads(target.read_bytes()) == expected(urls)
    assert playlist_files.load_playlist(target) == expected(urls)
    assert [url.encode() for url in playlist_files.load_playlist(target)["urls"]] == [url.encode() for url in urls]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert b"discarded" not in target.read_bytes()
    assert source.read_bytes() == original and set(tmp_path.iterdir()) == {source, target}
    captured = capsys.readouterr()
    assert captured.out == SUCCESS and captured.err == ""


def test_invalid_final_urls_cannot_publish_or_create_temporary_files(tmp_path, monkeypatch, capsys):
    source = source_file(tmp_path)
    target = tmp_path / "new.json"
    def forbidden(*args, **kwargs):
        pytest.fail("validation must finish before creating the output temporary")
    monkeypatch.setattr(playlist_files.tempfile, "mkstemp", forbidden)
    for last in (
        "../synthetic-secret.wav", "//media.example.test/audio", "file:///synthetic-secret",
        "ftp://media.example.test/audio", "https://user:synthetic-secret@media.example.test/audio",
        "http://127.0.0.1:8080/a/../proxy/%61udio?token=synthetic-secret",
        URLS[0] + " ", " " + URLS[0], "\t", URLS[0] + "\u2028tail",
        "https://media.example.test/\x01synthetic-secret", "https://media.example.test:0/audio",
    ):
        source.write_bytes((URLS[0] + "\n" + last).encode())
        assert cli.main(arguments(source, target)) == 1
        private_failure(capsys)
        assert not target.exists() and set(tmp_path.iterdir()) == {source}


def test_all_format_name_count_input_and_output_limits_precede_temporary_creation(tmp_path, monkeypatch, capsys):
    source = source_file(tmp_path)
    target = tmp_path / "new.json"
    def forbidden(*args, **kwargs):
        pytest.fail("invalid input must not create a temporary file")
    monkeypatch.setattr(playlist_files.tempfile, "mkstemp", forbidden)
    for encoded, name in (
        (b"#EXTM3U\n#EXTINF:1,orphan", NAME),
        (("#EXTM3U\n#EXT-X-VERSION:3\n" + URLS[0]).encode(), NAME),
        (b"\xffsynthetic-secret", NAME),
        ((URLS[0] + "\n").encode() * 1001, NAME),
        (b"# " + b"x" * MAX_PLAYLIST_BYTES + b"\n" + URLS[0].encode(), NAME),
        (URLS[0].encode(), "bad\rsynthetic-secret"),
        (URLS[0].encode(), ""),
    ):
        source.write_bytes(encoded)
        assert cli.main(arguments(source, target, name=name)) == 1
        private_failure(capsys)
        assert not target.exists() and set(tmp_path.iterdir()) == {source}

    # Input and full SOAP fit, but JSON quotes double on encoding. No lowering
    # or monkeypatching of production limits is needed to reach this boundary.
    url = "https://media.example.test/" + '"' * 600000
    urls = [url]
    encoded = "\n".join(urls).encode()
    assert len(encoded) < MAX_PLAYLIST_BYTES
    # Quotes also expand in XML attributes only; URL text quotes remain literal.
    assert len(prepare_replace(explicit_queue(NAME, urls))) < MAX_SOAP_BYTES
    source.write_bytes(encoded)
    assert cli.main(arguments(source, target)) == 1
    assert "Invalid authored playlist" in private_failure(capsys)
    assert not target.exists() and set(tmp_path.iterdir()) == {source}


def test_full_soap_expansion_limit_is_independent_of_input_and_json(tmp_path, monkeypatch, capsys):
    urls = [URLS[0], "https://media.example.test/?q=" + "&" * 120000]
    encoded = "\n".join(urls).encode()
    assert len(encoded) < MAX_PLAYLIST_BYTES
    assert len(playlist_files.encode_playlist(expected(urls))) < MAX_PLAYLIST_BYTES
    source = source_file(tmp_path, encoded)
    target = tmp_path / "new.json"
    def forbidden(*args, **kwargs):
        pytest.fail("complete SOAP validation must precede temporary creation")
    monkeypatch.setattr(playlist_files.tempfile, "mkstemp", forbidden)
    assert cli.main(arguments(source, target)) == 1
    assert "Invalid queue input" in private_failure(capsys)
    assert set(tmp_path.iterdir()) == {source}


def test_input_limit_reads_only_one_oversize_sentinel(tmp_path, monkeypatch, capsys):
    source = source_file(tmp_path, b"# " + b"x" * (MAX_PLAYLIST_BYTES * 2))
    real_fdopen = os.fdopen
    reads = []
    class Reader:
        def __init__(self, descriptor, mode):
            self.stream = real_fdopen(descriptor, mode)
        def __enter__(self):
            return self
        def read(self, limit):
            reads.append(limit)
            return self.stream.read(limit)
        def __exit__(self, *args):
            self.stream.close()
    monkeypatch.setattr(os, "fdopen", Reader)
    assert cli.main(arguments(source, tmp_path / "new.json")) == 1
    assert reads == [MAX_PLAYLIST_BYTES + 1]
    private_failure(capsys)
    assert set(tmp_path.iterdir()) == {source}


@pytest.mark.parametrize("kind", ["existing", "same-input", "hardlink", "symlink", "dangling-symlink", "directory", "fifo"])
def test_existing_destination_and_input_aliases_are_preserved(tmp_path, capsys, kind):
    source = source_file(tmp_path)
    target = tmp_path / "output-synthetic-secret.json"
    if kind == "same-input":
        target = source
    elif kind == "hardlink":
        os.link(source, target)
    elif kind == "symlink":
        target.symlink_to(source)
    elif kind == "dangling-symlink":
        target.symlink_to(tmp_path / "missing-synthetic-secret")
    elif kind == "directory":
        target.mkdir()
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.write_bytes(b"old synthetic-secret content")
    original = source.read_bytes()
    old_content = target.read_bytes() if kind in {"existing", "same-input", "hardlink"} else None
    before = target.lstat()
    before_paths = set(tmp_path.iterdir())
    assert cli.main(arguments(source, target)) == 1
    assert private_failure(capsys) == "Playlist not saved: destination already exists. Choose a new output file.\n"
    after = target.lstat()
    assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (before.st_ino, before.st_mode, before.st_mtime_ns)
    assert source.read_bytes() == original and set(tmp_path.iterdir()) == before_paths
    if old_content is not None:
        assert target.read_bytes() == old_content


@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink", "dangling-symlink", "character-device"])
def test_nonregular_input_rejects_in_bounded_subprocess(tmp_path, kind):
    source = tmp_path / "input-synthetic-secret"
    if kind == "fifo":
        os.mkfifo(source)
    elif kind == "directory":
        source.mkdir()
    elif kind == "character-device":
        source = Path("/dev/null")
    else:
        referent = tmp_path / "referent-synthetic-secret"
        if kind == "symlink":
            referent.write_bytes(URLS[0].encode())
        source.symlink_to(referent)
    original = source.lstat()
    target = tmp_path / "new.json"
    result = subprocess.run([sys.executable, "-m", "fake_linkplay.cli", *arguments(source, target)],
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1 and result.stdout == "" and not target.exists()
    assert result.stderr == "Playlist path must be a regular file; symbolic links are not accepted.\n"
    after = source.lstat()
    assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (original.st_ino, original.st_mode, original.st_mtime_ns)
    assert not list(tmp_path.glob(".orr-playlist-*"))


def test_parser_rejects_unoffered_options_and_missing_name_privately(tmp_path, capsys):
    source = source_file(tmp_path)
    target = tmp_path / "new.json"
    for option in ("--overwrite", "--play", "--host=synthetic-secret", "--format=m3u", "--start-index=2", "--mode=repeat-all"):
        with pytest.raises(SystemExit) as caught:
            cli.main([*arguments(source, target), option])
        assert caught.value.code == 2
        assert private_failure(capsys) == "Invalid client command arguments. Run open-room-renderer --help.\n"
    with pytest.raises(SystemExit) as caught:
        cli.main(arguments(source, target)[:-2])
    assert caught.value.code == 2
    private_failure(capsys)
    assert set(tmp_path.iterdir()) == {source}


@pytest.mark.parametrize("stage", ["file-fsync", "link", "directory-open", "directory-fsync", "unlink"])
def test_pre_and_postcommit_failures_report_the_actual_file_outcome(tmp_path, monkeypatch, capsys, stage):
    source = source_file(tmp_path)
    original = source.read_bytes()
    target = tmp_path / "output-synthetic-secret.json"
    real_open, real_fsync, real_unlink = os.open, os.fsync, os.unlink
    committed = stage in {"directory-open", "directory-fsync", "unlink"}
    def fail(*args, **kwargs):
        raise OSError(errno.EIO, "synthetic-secret")
    def open_directory(path, flags, *args, **kwargs):
        if flags & os.O_DIRECTORY:
            fail()
        return real_open(path, flags, *args, **kwargs)
    def fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode) == (stage == "directory-fsync"):
            fail()
        return real_fsync(descriptor)
    def unlink_once(path):
        monkeypatch.setattr(os, "unlink", real_unlink)
        fail()
    if stage == "link":
        monkeypatch.setattr(os, "link", fail)
    elif stage == "directory-open":
        monkeypatch.setattr(os, "open", open_directory)
    elif stage == "unlink":
        monkeypatch.setattr(os, "unlink", unlink_once)
    else:
        monkeypatch.setattr(os, "fsync", fsync)
    assert cli.main(arguments(source, target)) == 1
    error = private_failure(capsys)
    assert source.read_bytes() == original
    if committed:
        assert error == ("Playlist saved, but crash durability is unconfirmed. "
                         "Inspect the destination before retrying.\n")
        assert playlist_files.load_playlist(target) == expected()
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    else:
        assert error == "Playlist file operation failed. Check the path and permissions.\n"
        assert not target.exists()
    assert set(tmp_path.iterdir()) == ({source, target} if committed else {source})


def test_io_errors_and_json_restore_format_remain_private_and_separate(tmp_path, capsys):
    source = source_file(tmp_path)
    for input_path, output_path in (
        (tmp_path / "missing-synthetic-secret", tmp_path / "new.json"),
        (source, tmp_path / "missing-synthetic-secret" / "new.json"),
    ):
        assert cli.main(arguments(input_path, output_path)) == 1
        assert private_failure(capsys) == "Playlist file operation failed. Check the path and permissions.\n"
    assert cli.main(["playlist", "restore", str(source)]) == 1
    assert "Invalid authored playlist" in private_failure(capsys)
    source.write_bytes(b"\xef\xbb\xbf" + playlist_files.encode_playlist(expected()))
    assert cli.main(["playlist", "restore", str(source)]) == 1
    assert "Invalid authored playlist" in private_failure(capsys)
    assert set(tmp_path.iterdir()) == {source}
