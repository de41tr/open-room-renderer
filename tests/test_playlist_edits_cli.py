"""In-process offline edits reuse the strict reader and atomic private writer."""
from __future__ import annotations

import errno
import json
import socket
import stat

import aiohttp
import pytest
import requests

from fake_linkplay import cli, playlist_cli, playlist_files
from fake_linkplay.playlist_files import MAX_PLAYLIST_BYTES, PLAYLIST_MODES
from test_playlist_edits import URLS, payload

SUCCESS = "Edited URL playlist saved. Playback was not changed.\n"


def arguments(command, source, target, *options):
    defaults = {"append": [URLS[1]], "remove": ["--index", "1"],
                "move": ["--from-index", "3", "--to-index", "1"]}
    return ["playlist", command, str(source), str(target), *(options or defaults[command])]


def source_file(tmp_path, **changes):
    source = tmp_path / "input-synthetic-secret.json"
    # A valid source need not have the writer's formatting. Preserve its bytes.
    source.write_text(json.dumps(payload(**changes), ensure_ascii=False), encoding="utf-8")
    return source


def private_failure(capsys):
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err
    assert not any(secret in captured.err for secret in ("synthetic-secret", "https://", "Traceback"))
    return captured.err


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline editing must not construct a client/session, renderer/backend or socket")
    monkeypatch.setattr(playlist_cli, "QueueClient", forbidden)
    monkeypatch.setattr(playlist_cli, "fetch_current_playlist", forbidden)
    monkeypatch.setattr(cli, "HttpApi", forbidden)
    monkeypatch.setattr(cli, "MpvBackend", forbidden)
    monkeypatch.setattr(requests, "Session", forbidden)
    monkeypatch.setattr(aiohttp, "ClientSession", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@pytest.mark.parametrize("mode", PLAYLIST_MODES)
@pytest.mark.parametrize("command", ["append", "remove", "move"])
def test_offline_edits_exact_private_output_and_reused_io(tmp_path, monkeypatch, capsys, command, mode):
    source = source_file(tmp_path, mode=mode)
    original = source.read_bytes()
    target = tmp_path / "output-synthetic-secret.json"
    calls = []
    load, save = playlist_cli.load_playlist, playlist_cli.save_playlist
    def read(path):
        calls.append(("read", path))
        return load(path)
    def write(path, document, **kwargs):
        assert kwargs == {}  # The shared writer's no-overwrite default is used.
        calls.append(("write", path))
        return save(path, document, **kwargs)
    monkeypatch.setattr(playlist_cli, "load_playlist", read)
    monkeypatch.setattr(playlist_cli, "save_playlist", write)
    assert cli.main(arguments(command, source, target)) == 0
    urls, index = {"append": ([*payload()["urls"], URLS[1]], 3),
                   "remove": ([URLS[1], URLS[0]], 2),
                   "move": ([URLS[0], URLS[0], URLS[1]], 1)}[command]
    assert playlist_files.load_playlist(target) == payload(mode=mode, urls=urls, index=index)
    assert calls == [("read", str(source)), ("write", str(target))]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert source.read_bytes() == original and set(tmp_path.iterdir()) == {source, target}
    captured = capsys.readouterr()
    assert captured.out == SUCCESS and captured.err == ""


def test_same_position_move_still_writes_new_equivalent_file(tmp_path, capsys):
    source = source_file(tmp_path)
    original = source.read_bytes()
    target = tmp_path / "new.json"
    assert cli.main(arguments("move", source, target, "--from-index", "3", "--to-index", "3")) == 0
    assert playlist_files.load_playlist(target) == payload()
    assert source.read_bytes() == original
    assert capsys.readouterr().out == SUCCESS


@pytest.mark.parametrize("command", ["append", "remove", "move"])
def test_invalid_source_cannot_be_repaired_or_create_temporary(tmp_path, monkeypatch, capsys, command):
    source = source_file(tmp_path)
    target = tmp_path / "new.json"
    def forbidden(*args, **kwargs):
        pytest.fail("source and result validation must precede temporary creation")
    monkeypatch.setattr(playlist_files.tempfile, "mkstemp", forbidden)
    options = {"append": [URLS[0]], "remove": ["--index", "3"],
               "move": ["--from-index", "3", "--to-index", "1"]}[command]
    for encoded in (
        json.dumps(payload(urls=[*URLS, "file:///synthetic-secret"])).encode(),
        json.dumps(payload(urls=[*URLS, "https://media.example.test/?q=" + "&" * 120000])).encode(),
        json.dumps(payload(index=True)).encode(),
        json.dumps(payload(metadata="synthetic-secret")).encode(),
        b'{"version":1,"version":1}', b"\xffsynthetic-secret", b'{"version":NaN}',
        b"\xef\xbb\xbf" + playlist_files.encode_playlist(payload()),
        b" " * (MAX_PLAYLIST_BYTES + 1),
    ):
        source.write_bytes(encoded)
        assert cli.main(arguments(command, source, target, *options)) == 1
        private_failure(capsys)
        assert source.read_bytes() == encoded and set(tmp_path.iterdir()) == {source}


def test_append_url_count_json_and_full_soap_limits_precede_temporary(tmp_path, monkeypatch, capsys):
    source = source_file(tmp_path)
    target = tmp_path / "new.json"
    def forbidden(*args, **kwargs):
        pytest.fail("result validation must precede temporary creation")
    monkeypatch.setattr(playlist_files.tempfile, "mkstemp", forbidden)
    for urls in (
        [URLS[0], "https://user:synthetic-secret@media.example.test/audio"],
        ["../synthetic-secret.wav"], ["file:///synthetic-secret"], [URLS[0] + " "],
        ["http://127.0.0.1:8080/a/../proxy/%61udio?token=synthetic-secret"],
        [URLS[0]] * 998,
        ["https://media.example.test/" + '"' * 600000],
        ["https://media.example.test/?q=" + "&" * 120000],
    ):
        assert cli.main(arguments("append", source, target, *urls)) == 1
        private_failure(capsys)
        assert set(tmp_path.iterdir()) == {source}


@pytest.mark.parametrize("command,flag", [("remove", "--index"), ("move", "--from-index"),
                                         ("move", "--to-index")])
def test_index_syntax_is_positive_ascii_without_signs_or_padding(tmp_path, capsys, command, flag):
    source = source_file(tmp_path)
    target = tmp_path / "new.json"
    for value in ("true", "0", "-1", "+1", "01", " 1", "1 ", "١", "１", "1.0", "1_0", "1e0", "9" * 5000):
        options = [flag + "=" + value]
        if command == "move":
            options += ["--to-index=1" if flag == "--from-index" else "--from-index=1"]
        with pytest.raises(SystemExit) as caught:
            cli.main(arguments(command, source, target, *options))
        assert caught.value.code == 2
        assert private_failure(capsys) == "Invalid client command arguments. Run open-room-renderer --help.\n"
        assert set(tmp_path.iterdir()) == {source}


def test_valid_index_syntax_still_requires_range_and_nonempty_result(tmp_path, monkeypatch, capsys):
    source = source_file(tmp_path, urls=URLS[:1], index=1)
    target = tmp_path / "new.json"
    def forbidden(*args, **kwargs):
        pytest.fail("invalid edit must not create a temporary")
    monkeypatch.setattr(playlist_files.tempfile, "mkstemp", forbidden)
    for command, options in (("remove", ["--index", "1"]), ("remove", ["--index", "2"]),
                             ("move", ["--from-index", "2", "--to-index", "1"]),
                             ("move", ["--from-index", "1", "--to-index", "2"])):
        assert cli.main(arguments(command, source, target, *options)) == 1
        assert "Invalid authored playlist" in private_failure(capsys)
    assert set(tmp_path.iterdir()) == {source}


@pytest.mark.parametrize("command", ["append", "remove", "move"])
def test_edit_cli_offers_no_overwrite_play_host_or_metadata_controls(tmp_path, capsys, command):
    source = source_file(tmp_path)
    target = tmp_path / "new.json"
    for option in ("--overwrite", "--play", "--host=synthetic-secret", "--port=8080",
                   "--name=synthetic-secret", "--mode=normal", "--start-index=1"):
        with pytest.raises(SystemExit) as caught:
            cli.main([*arguments(command, source, target), option])
        assert caught.value.code == 2
        private_failure(capsys)
    with pytest.raises(SystemExit) as caught:
        cli.main(["playlist", command, str(source), str(target)])
    assert caught.value.code == 2
    private_failure(capsys)
    assert set(tmp_path.iterdir()) == {source}


@pytest.mark.parametrize("command", ["append", "remove", "move"])
def test_edit_input_alias_never_overwrites_and_gives_new_file_advice(tmp_path, capsys, command):
    source = source_file(tmp_path)
    original = source.read_bytes()
    assert cli.main(arguments(command, source, source)) == 1
    assert private_failure(capsys) == "Playlist not saved: destination already exists. Choose a new output file.\n"
    assert source.read_bytes() == original and set(tmp_path.iterdir()) == {source}


def test_shared_io_failures_keep_private_errors_and_commit_status(tmp_path, monkeypatch, capsys):
    source = source_file(tmp_path)
    original = source.read_bytes()
    target = tmp_path / "new.json"
    for exception, message in (
        (playlist_files.PlaylistFileExistsError("synthetic-secret"),
         "Playlist not saved: destination already exists. Choose a new output file.\n"),
        (playlist_files.PlaylistFileTypeError("synthetic-secret"),
         "Playlist path must be a regular file; symbolic links are not accepted.\n"),
        (PermissionError(errno.EACCES, "synthetic-secret"),
         "Playlist file operation failed. Check the path and permissions.\n"),
        (playlist_files.PlaylistCommittedError("synthetic-secret"),
         "Playlist saved, but crash durability is unconfirmed. Inspect the destination before retrying.\n"),
    ):
        def fail(*args, **kwargs):
            raise exception
        with monkeypatch.context() as scoped:
            scoped.setattr(playlist_cli, "save_playlist", fail)
            assert cli.main(arguments("append", source, target)) == 1
        assert private_failure(capsys) == message
        assert source.read_bytes() == original
    assert set(tmp_path.iterdir()) == {source}
    # Detailed real syscall pre/postcommit, FIFO, symlink, hardlink and existing
    # target outcomes remain covered by unchanged test_playlist_files.py and
    # test_playlist_m3u_cli.py; the new commands delegate to those same functions.
