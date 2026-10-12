"""Current URL playlist client: real loopback HTTP, fixed errors and private files."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import stat
from threading import Thread

import pytest
import requests

from fake_linkplay import cli, playlist_files
from fake_linkplay.playlist_files import MAX_PLAYLIST_BYTES
from fake_linkplay.queue_client import MODES, explicit_queue
from fake_linkplay.state import PlaybackPhase
from test_queue_client_renderer import renderer

PATH = "/orr/v1/queue/playlist"
NAME = "Café 夜のラジオ 🎵"
URLS = ["https://media.example.test/音声/a%2fb?token=synthetic-secret&a=1+2#片",
        "http://[::1]:8080/track%2FB.wav?x=a%2520b&x=&case=%2f%2F"]
SUCCESS = ("Current URL playlist saved. Metadata and playback state were not saved. "
           "Keep the file private; URLs may contain secrets or expire.\n")


def document(**changes):
    return {"version": 1, "name": NAME, "index": 2, "mode": "repeat-all", "urls": URLS.copy(), **changes}


def encoded(**changes):
    return json.dumps(document(**changes)).encode("utf-8")


@contextmanager
def peer(*, body=None, status=200, headers=None, disconnect=False):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.command, self.path, dict(self.headers)))
            if disconnect:
                self.close_connection = True
                return
            data = encoded() if body is None else body
            self.send_response(status)
            result_headers = {"Content-Type": "application/json; charset=utf-8",
                              "Content-Length": str(len(data)), **(headers or {})}
            for key, value in result_headers.items():
                if value is not None:
                    self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def do_POST(self):
            received.append((self.command, self.path, dict(self.headers)))
            self.send_response(500)
            self.end_headers()
        def log_message(self, *args):
            pass
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
        thread.start()
        try:
            yield server.server_port, received
        finally:
            server.shutdown()
            thread.join(timeout=2)
            assert not thread.is_alive()


def save(path, port, *extra):
    return cli.main(["playlist", "save-current", str(path), "--host", "127.0.0.1",
                     "--port", str(port), *extra])


def assert_private_failure(capsys):
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err
    assert "synthetic-secret" not in captured.err and "Traceback" not in captured.err
    return captured.err


@pytest.mark.parametrize("mode", MODES)
async def test_real_renderer_save_exact_roundtrip_then_default_restore_is_stopped(renderer, tmp_path, capsys, mode):
    urls = [*URLS, URLS[0]]
    await asyncio.to_thread(renderer.client.replace, explicit_queue(NAME, urls, start_index=2, mode=mode), play=True)
    await renderer.api._transport_command("pause")
    queue, items = renderer.api.state.queue, list(renderer.api.state.queue.items)
    # Display/resolver fields are deliberately not part of the v1 document.
    for item in items:
        item.title, item.artist, item.metadata = "synthetic-secret title", "private artist", "private metadata"
    state, calls, rng = asdict(renderer.api.state), list(renderer.api.mpv.calls), renderer.api._shuffle_rng.getstate()
    traversal = queue._shuffle
    renderer.requests.clear()
    renderer.api._resolve_playback_url.reset_mock()
    path = tmp_path / "synthetic-secret.json"
    assert await asyncio.to_thread(save, path, renderer.server.port) == 0
    captured = capsys.readouterr()
    assert captured.out == SUCCESS and captured.err == ""
    expected = document(urls=urls, mode=mode)
    assert playlist_files.load_playlist(path) == expected
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [action for action, _ in renderer.requests] == [""]
    assert asdict(renderer.api.state) == state and renderer.api.mpv.calls == calls
    assert renderer.api.state.queue is queue and all(a is b for a, b in zip(queue.items, items))
    assert queue._shuffle is traversal and renderer.api._shuffle_rng.getstate() == rng
    renderer.api._resolve_playback_url.assert_not_awaited()
    saved = path.read_bytes()
    renderer.requests.clear()
    assert await asyncio.to_thread(cli.main, ["playlist", "restore", str(path),
        "--host", "127.0.0.1", "--port", str(renderer.server.port)]) == 0
    assert capsys.readouterr().out == "Playlist restored; playback was not requested.\n"
    restored = renderer.api.state.queue
    assert [action for action, _ in renderer.requests] == ["ReplaceQueue"]
    assert [item.url for item in restored.items] == urls
    assert (restored.name, restored.index, restored.loop) == (NAME, 2, MODES[mode])
    assert all(not item.artist and not item.metadata and item.duration == 0
               and not item.resolved_url and item._radio_station is None for item in restored.items)
    assert renderer.api.state.phase == PlaybackPhase.STOPPED and renderer.api.mpv.idle
    renderer.api._resolve_playback_url.assert_not_awaited()
    assert renderer.api.mpv.calls == [*calls, ("stop", None)] and path.read_bytes() == saved


def test_real_single_get_has_no_body_proxy_credentials_or_url_output(tmp_path, monkeypatch, capsys, caplog):
    target = tmp_path / "synthetic-secret.json"
    netrc = tmp_path / "private.netrc"
    netrc.write_text("machine 127.0.0.1 login synthetic-user password synthetic-secret\n")
    monkeypatch.setenv("NETRC", str(netrc))
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    with caplog.at_level("INFO"), peer() as (port, received):
        assert save(target, port) == 0
    assert playlist_files.load_playlist(target) == document()
    assert capsys.readouterr().out == SUCCESS
    assert len(received) == 1
    method, path, headers = received[0]
    assert (method, path) == ("GET", PATH)
    assert headers.get("Content-Length", "0") == "0" and "Transfer-Encoding" not in headers
    assert headers["Accept-Encoding"] == "identity" and headers["Accept"] == "application/json"
    assert headers["Cache-Control"] == "no-store" and "Authorization" not in headers
    assert "synthetic-secret" not in caplog.text and NAME not in caplog.text


@pytest.mark.parametrize("status", [201, 204, 301, 302, 303, 307, 308, 400, 404, 409, 500, 503])
def test_nonexact_status_is_one_get_no_fallback_or_file(tmp_path, capsys, status):
    target = tmp_path / "synthetic-secret.json"
    with peer(status=status, body=b"synthetic-secret") as (port, received):
        assert save(target, port) == 1
    error = assert_private_failure(capsys)
    assert len(received) == 1 and received[0][:2] == ("GET", PATH)
    assert list(tmp_path.iterdir()) == []
    if status == 404:
        assert "does not support" in error
    if status == 409:
        assert "cannot be represented" in error


def test_redirect_does_not_contact_other_target_and_disconnect_never_retries(tmp_path, capsys):
    target = tmp_path / "synthetic-secret.json"
    with peer() as (other_port, redirected):
        with peer(status=307, headers={"Location": f"http://127.0.0.1:{other_port}/synthetic-secret"}) as (port, received):
            assert save(target, port) == 1
    assert len(received) == 1 and redirected == []
    assert_private_failure(capsys)
    with peer(disconnect=True) as (port, received):
        assert save(target, port) == 1
    assert len(received) == 1 and list(tmp_path.iterdir()) == []
    assert_private_failure(capsys)


@pytest.mark.parametrize("body,headers", [
    (b"synthetic-secret", {}), (b"\xff", {}), (b'{"version":1,"version":1}', {}),
    (b'{"version":NaN}', {}), (b'{"version":Infinity}', {}),
    (b"\xef\xbb\xbf" + encoded(), {}), (encoded() + b" trailing", {}),
    (encoded(version=True), {}), (encoded(version=2), {}), (encoded(index=True), {}),
    (encoded(index=0), {}), (encoded(index=3), {}), (encoded(mode="unknown"), {}),
    (encoded(urls=[]), {}), (encoded(urls=URLS * 501), {}),
    (encoded(metadata="synthetic-secret"), {}), (encoded(name="bad\rsynthetic-secret"), {}),
    (encoded(urls=[URLS[0], "file:///synthetic-secret"]), {}),
    (encoded(urls=[URLS[0], "https://media.example.test/?q=" + "&" * 120000]), {}),
    (b" " * (MAX_PLAYLIST_BYTES + 1), {}),
    (b" " * (MAX_PLAYLIST_BYTES + 1), {"Content-Length": None}),
    (encoded(), {"Content-Type": "text/html"}), (encoded(), {"Content-Type": None}),
    (encoded(), {"Content-Encoding": "gzip"}), (encoded(), {"Content-Encoding": "identity, identity"}),
    (encoded(), {"Content-Length": str(MAX_PLAYLIST_BYTES + 1)}),
    (encoded(), {"Content-Length": "invalid"}),
])
def test_bad_response_rejects_before_output_temporary(tmp_path, monkeypatch, capsys, body, headers):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid response must not create any output temporary")
    monkeypatch.setattr(playlist_files.tempfile, "mkstemp", forbidden)
    target = tmp_path / "synthetic-secret.json"
    with peer(body=body, headers=headers) as (port, received):
        assert save(target, port) == 1
    assert_private_failure(capsys)
    assert len(received) == 1 and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["file", "symlink", "dangling-symlink", "fifo", "directory"])
def test_existing_destination_is_never_replaced_or_followed(tmp_path, capsys, kind):
    path = tmp_path / "synthetic-secret.json"
    other = tmp_path / "private-target"
    if kind == "file":
        path.write_bytes(b"original")
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        if kind == "symlink":
            other.write_bytes(b"untouched")
        path.symlink_to(other)
    before = path.lstat()
    with peer() as (port, received):
        assert save(path, port) == 1
    assert assert_private_failure(capsys) == "Playlist not saved: destination already exists. Choose a new output file.\n"
    assert path.lstat() == before and len(received) == 1
    assert not list(tmp_path.glob(".orr-playlist-*"))
    if kind == "file":
        assert path.read_bytes() == b"original"
    if kind == "symlink":
        assert other.read_bytes() == b"untouched"
    if kind == "dangling-symlink":
        assert not other.exists()


@pytest.mark.parametrize("after_commit", [False, True])
def test_pre_and_post_commit_file_failures_have_truthful_private_diagnostics(tmp_path, monkeypatch, capsys, after_commit):
    target = tmp_path / "synthetic-secret.json"
    real_fsync = os.fsync
    def fail(fd):
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if is_directory == after_commit:
            raise OSError("synthetic-secret path metadata URL")
        real_fsync(fd)
    monkeypatch.setattr(os, "fsync", fail)
    with peer() as (port, received):
        assert save(target, port) == 1
    error = assert_private_failure(capsys)
    assert len(received) == 1 and not list(tmp_path.glob(".orr-playlist-*"))
    if after_commit:
        assert "Playlist saved, but crash durability is unconfirmed" in error
        assert playlist_files.load_playlist(target) == document()
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    else:
        assert "Playlist file operation failed" in error and not target.exists()


def test_missing_parent_is_not_created(tmp_path, capsys):
    target = tmp_path / "synthetic-secret" / "file.json"
    with peer() as (port, _):
        assert save(target, port) == 1
    assert_private_failure(capsys)
    assert not target.parent.exists()


@pytest.mark.parametrize("arguments", [
    ["--overwrite"], ["--play"], ["--name", "synthetic-secret"],
    ["--port", "synthetic-secret"], ["--mode", "normal"],
])
def test_save_current_preserves_private_argparse_and_has_no_overwrite_or_play(tmp_path, capsys, arguments):
    with pytest.raises(SystemExit) as error:
        cli.main(["playlist", "save-current", str(tmp_path / "synthetic-secret"), *arguments])
    assert error.value.code == 2
    assert "Invalid client command arguments" in assert_private_failure(capsys)
    assert list(tmp_path.iterdir()) == []


def test_invalid_host_rejected_without_network_or_file(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid host must not open a session")
    monkeypatch.setattr(requests, "Session", forbidden)
    assert cli.main(["playlist", "save-current", str(tmp_path / "synthetic-secret"),
                     "--host", "synthetic-secret/path"]) == 1
    assert_private_failure(capsys)
    assert list(tmp_path.iterdir()) == []
