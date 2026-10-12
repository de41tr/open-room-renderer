"""Authored playlist CLI subprocesses against synthetic loopback SOAP peers."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from threading import Thread
from xml.etree import ElementTree as ET

import pytest

from fake_linkplay import playlist_cli, playlist_files
from fake_linkplay.queue import parse_queue_context
from fake_linkplay.queue_client import MODES, SERVICE_TYPE, SOAP_NS

ROOT = Path(__file__).resolve().parents[1]
NAME = "Café 夜のラジオ 🎵"
URLS = ["https://media.example.test/音声/a%2fb?token=synthetic-secret&a=1+2#片",
        "http://[::1]:8080/track%2FB.wav?x=a%2520b&x=&case=%2f%2F"]


def payload(**changes):
    return {"version": 1, "name": NAME, "index": 2, "mode": "repeat-all", "urls": URLS.copy(), **changes}


def document(tmp_path, **changes):
    path = tmp_path / "authored.json"
    playlist_files.save_playlist(path, payload(**changes))
    return path


def invoke(*arguments):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
    return subprocess.run([sys.executable, "-m", "fake_linkplay.cli", "playlist", *map(str, arguments)],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)


def soap_response(action, name=NAME):
    envelope = ET.Element(f"{{{SOAP_NS}}}Envelope")
    body = ET.SubElement(envelope, f"{{{SOAP_NS}}}Body")
    response = ET.SubElement(body, f"{{{SERVICE_TYPE}}}{action}Response")
    if action == "ReplaceQueue":
        ET.SubElement(response, "QueueName").text = name
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


@contextmanager
def soap_peer(*, status=200, response=None, disconnect=False, play_response=None, location=None):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            action = self.headers["SOAPAction"].strip('"').split("#")[1]
            received.append((self.path, action, body))
            if disconnect:
                self.close_connection = True
                return
            encoded = response if response is not None else soap_response(action)
            if action == "PlayQueueWithIndex" and play_response is not None:
                encoded = play_response
            self.send_response(status)
            self.send_header("Content-Type", "text/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            if location:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(encoded)
        def log_message(self, *args):
            pass
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .02})
        thread.start()
        try:
            yield server.server_port, received
        finally:
            server.shutdown()
            thread.join(timeout=2)
            assert not thread.is_alive()


def restore(path, port, *arguments):
    return invoke("restore", path, *arguments, "--host", "127.0.0.1", "--port", port)


def queue_request(body):
    action = ET.fromstring(body).find(f"{{{SOAP_NS}}}Body")[0]
    return action, {child.tag: child.text for child in action}


@pytest.mark.parametrize("mode", MODES)
def test_create_exact_schema_urls_and_modes(tmp_path, mode):
    path = tmp_path / "authored.json"
    result = invoke("create", path, "--name", NAME, "--start-index", "2", "--mode", mode, *URLS)
    assert result.returncode == 0 and result.stderr == ""
    assert result.stdout == "Authored playlist saved.\n"
    assert json.loads(path.read_bytes()) == payload(mode=mode)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "synthetic-secret" not in result.stdout


def test_create_defaults_and_no_network(tmp_path, monkeypatch, capsys):
    from fake_linkplay import cli
    def forbidden(*args, **kwargs):
        pytest.fail("authoring must not contact a renderer")
    monkeypatch.setattr(playlist_cli, "QueueClient", forbidden)
    path = tmp_path / "authored.json"
    assert cli.main(["playlist", "create", str(path), "--name", NAME, *URLS]) == 0
    assert playlist_files.load_playlist(path) == payload(index=1, mode="normal")
    assert capsys.readouterr().out == "Authored playlist saved.\n"


def test_create_no_clobber_then_explicit_overwrite(tmp_path):
    path = document(tmp_path)
    original = path.read_bytes()
    result = invoke("create", path, "--name", "Replacement", URLS[0])
    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr == ("Playlist not saved: destination already exists. "
                             "Use --overwrite to replace it explicitly.\n")
    assert path.read_bytes() == original
    result = invoke("create", path, "--name", "Replacement", "--overwrite", URLS[0])
    assert result.returncode == 0 and result.stderr == ""
    assert playlist_files.load_playlist(path) == payload(name="Replacement", urls=URLS[:1], index=1, mode="normal")


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("play", [False, True])
def test_restore_replaces_exact_queue_and_only_explicitly_plays(tmp_path, mode, play):
    path = document(tmp_path, mode=mode)
    original = path.read_bytes()
    with soap_peer() as (port, received):
        result = restore(path, port, *(["--play"] if play else []))
    assert result.returncode == 0 and result.stderr == ""
    assert result.stdout == ("Playlist restored; playback request acknowledged.\n" if play
                             else "Playlist restored; playback was not requested.\n")
    assert [action for _, action, _ in received] == (["ReplaceQueue", "PlayQueueWithIndex"] if play else ["ReplaceQueue"])
    assert all(route == "/upnp/control/PlayQueue" for route, _, _ in received)
    _, fields = queue_request(received[0][2])
    assert set(fields) == {"QueueContext"}
    queue = parse_queue_context(fields["QueueContext"])
    assert (queue.name, queue.index, queue.loop) == (NAME, 2, MODES[mode])
    assert [item.url.encode() for item in queue.items] == [url.encode() for url in URLS]
    assert all(not item.resolved_url and item._radio_station is None for item in queue.items)
    if play:
        _, fields = queue_request(received[1][2])
        assert fields == {"QueueName": NAME, "Index": "2"}
    assert path.read_bytes() == original


@pytest.mark.parametrize("changes", [
    {"version": True}, {"version": 2}, {"index": 1.0}, {"index": 0}, {"index": 3},
    {"mode": "SHUFFLE"}, {"urls": []}, {"urls": [URLS[0]] * 1001},
    {"resolved_url": "synthetic-secret"}, {"name": "bad\x00synthetic-secret"},
    {"name": "bad\rsynthetic-secret"},
    {"urls": [URLS[0], "https://media.example.test/\ud800"]},
    {"urls": [URLS[0], "https://media.example.test/a b"]},
    {"urls": [URLS[0], "https://user:synthetic-secret@media.example.test/audio"]},
    {"urls": [URLS[0], "file:///synthetic-secret"]},
    {"urls": [URLS[0], "http://127.0.0.1:8080/proxy/audio?token=synthetic-secret"]},
])
def test_invalid_file_never_contacts_renderer_or_prints_data(tmp_path, changes):
    path = tmp_path / "synthetic-secret.json"
    path.write_text(json.dumps(payload(**changes)), encoding="utf-8")
    original = path.read_bytes()
    with soap_peer() as (port, received):
        result = restore(path, port, "--play")
    assert result.returncode == 1 and result.stdout == "" and received == []
    assert "synthetic-secret" not in result.stderr and "Traceback" not in result.stderr
    assert path.read_bytes() == original


@pytest.mark.parametrize("encoded", [
    b'{"version":1,"version":1}', b"\xffsynthetic-secret", b'{"version":NaN}',
    b'{"version":Infinity}', b" " * (playlist_files.MAX_PLAYLIST_BYTES + 1),
], ids=["duplicate", "invalid-utf8", "nan", "infinity", "oversized"])
def test_corrupt_file_never_contacts_renderer(tmp_path, encoded):
    path = tmp_path / "synthetic-secret.json"
    path.write_bytes(encoded)
    with soap_peer() as (port, received):
        result = restore(path, port, "--play")
    assert result.returncode == 1 and received == [] and result.stdout == ""
    assert "synthetic-secret" not in result.stderr and "Traceback" not in result.stderr


def test_complete_soap_expansion_is_checked_before_restore(tmp_path):
    path = document(tmp_path, urls=[URLS[0], "https://media.example.test/?q=" + "&" * 120000])
    assert path.stat().st_size < playlist_files.MAX_PLAYLIST_BYTES
    with soap_peer() as (port, received):
        result = restore(path, port, "--play")
    assert result.returncode == 1 and received == [] and result.stdout == ""
    assert "Invalid queue input" in result.stderr


def test_complete_soap_expansion_is_checked_before_create(tmp_path):
    path = tmp_path / "authored.json"
    result = invoke("create", path, "--name", NAME, "https://media.example.test/?q=" + "&" * 120000)
    assert result.returncode == 1 and result.stdout == ""
    assert "Invalid queue input" in result.stderr and not path.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("arguments", [
    ("create", "synthetic-secret.json", "--name", NAME, "--mode", "synthetic-secret", URLS[0]),
    ("create", "synthetic-secret.json", "--name", NAME, "--start-index", "synthetic-secret", URLS[0]),
    ("create", "synthetic-secret.json", "--name", NAME, "--synthetic-secret", URLS[0]),
    ("create", "synthetic-secret.json", "--name", NAME),
    ("restore", "synthetic-secret.json", "--port", "synthetic-secret"),
    ("export", "synthetic-secret.json"),
    ("restore", "synthetic-secret.json", "--synthetic-secret"),
])
def test_parser_errors_are_private(arguments):
    result = invoke(*arguments)
    assert result.returncode == 2 and result.stdout == ""
    assert "Invalid client command arguments" in result.stderr
    assert "synthetic-secret" not in result.stderr and "Traceback" not in result.stderr


@pytest.mark.parametrize("stage", ["create", "restore"])
def test_file_io_errors_do_not_print_paths_or_exception(tmp_path, stage):
    path = tmp_path / "missing-synthetic-secret" / "authored.json"
    args = [stage, path]
    if stage == "create":
        args.extend(["--name", NAME, *URLS])
    result = invoke(*args)
    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr == "Playlist file operation failed. Check the path and permissions.\n"
    assert not path.parent.exists()


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 404, 500, 503])
def test_unconfirmed_replace_never_plays_or_retries(tmp_path, status):
    path = document(tmp_path)
    with soap_peer(status=status, response=b"synthetic-secret") as (port, received):
        result = restore(path, port, "--play")
    assert result.returncode == 1 and result.stdout == ""
    assert [action for _, action, _ in received] == ["ReplaceQueue"]
    assert "Queue replacement was not confirmed" in result.stderr
    assert "synthetic-secret" not in result.stderr and "Traceback" not in result.stderr


@pytest.mark.parametrize("response", [b"synthetic-secret", soap_response("ReplaceQueue", "other-synthetic-secret"),
                                       soap_response("PlayQueueWithIndex")])
def test_invalid_replace_ack_never_plays(tmp_path, response):
    path = document(tmp_path)
    with soap_peer(response=response) as (port, received):
        result = restore(path, port, "--play")
    assert result.returncode == 1 and result.stdout == ""
    assert [action for _, action, _ in received] == ["ReplaceQueue"]
    assert "Queue replacement was not confirmed" in result.stderr and "synthetic-secret" not in result.stderr


def test_disconnect_has_no_retry_or_play(tmp_path):
    path = document(tmp_path)
    with soap_peer(disconnect=True) as (port, received):
        result = restore(path, port, "--play")
    assert result.returncode == 1 and result.stdout == ""
    assert [action for _, action, _ in received] == ["ReplaceQueue"]
    assert "Queue replacement was not confirmed" in result.stderr


def test_redirect_does_not_contact_target(tmp_path):
    path = document(tmp_path)
    with soap_peer() as (target_port, redirected):
        with soap_peer(status=307, location=f"http://127.0.0.1:{target_port}/synthetic-secret") as (port, received):
            result = restore(path, port, "--play")
    assert result.returncode == 1 and len(received) == 1 and redirected == []
    assert "synthetic-secret" not in result.stderr


def test_play_failure_reports_committed_replacement_without_retry(tmp_path):
    path = document(tmp_path)
    with soap_peer(play_response=b"synthetic-secret") as (port, received):
        result = restore(path, port, "--play")
    assert result.returncode == 1 and result.stdout == ""
    assert [action for _, action, _ in received] == ["ReplaceQueue", "PlayQueueWithIndex"]
    assert "Queue replaced; playback was not confirmed" in result.stderr
    assert "synthetic-secret" not in result.stderr


def test_postcommit_file_failure_tells_truth_without_secrets(tmp_path, monkeypatch, capsys):
    from fake_linkplay import cli
    path = tmp_path / "synthetic-secret.json"
    real_fsync = os.fsync
    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("synthetic-secret")
        real_fsync(fd)
    monkeypatch.setattr(os, "fsync", fail_directory)
    assert cli.main(["playlist", "create", str(path), "--name", NAME, *URLS]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ("Playlist saved, but crash durability is unconfirmed. "
                            "Inspect the destination before retrying.\n")
    assert playlist_files.load_playlist(path) == payload(index=1, mode="normal")


def test_create_invalid_last_url_preserves_destination(tmp_path):
    path = document(tmp_path)
    original = path.read_bytes()
    result = invoke("create", path, "--name", NAME, "--overwrite", URLS[0], "not-a-url-synthetic-secret")
    assert result.returncode == 1 and result.stdout == ""
    assert "synthetic-secret" not in result.stderr
    assert path.read_bytes() == original


@pytest.mark.parametrize("kind", ["fifo", "symlink", "dangling-symlink", "directory"])
def test_nonregular_restore_is_bounded_and_never_contacts_renderer(tmp_path, kind):
    path = tmp_path / "input-synthetic-secret"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        target = tmp_path / "target"
        if kind == "symlink":
            playlist_files.save_playlist(target, payload())
        path.symlink_to(target)
    original = path.lstat()
    with soap_peer() as (port, received):
        result = restore(path, port, "--play")
    assert result.returncode == 1 and result.stdout == "" and received == []
    assert result.stderr == "Playlist path must be a regular file; symbolic links are not accepted.\n"
    after = path.lstat()
    assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (original.st_ino, original.st_mode, original.st_mtime_ns)


@pytest.mark.parametrize("kind", ["fifo", "symlink", "dangling-symlink", "directory"])
def test_nonregular_overwrite_is_bounded_and_preserves_target(tmp_path, kind):
    path = tmp_path / "output-synthetic-secret"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        target = tmp_path / "target"
        if kind == "symlink":
            playlist_files.save_playlist(target, payload())
        path.symlink_to(target)
    original = path.lstat()
    result = invoke("create", path, "--name", NAME, "--overwrite", *URLS)
    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr == "Playlist path must be a regular file; symbolic links are not accepted.\n"
    assert path.lstat() == original


def test_create_carriage_return_name_is_rejected_before_file_creation(tmp_path):
    path = tmp_path / "authored.json"
    result = invoke("create", path, "--name", "bad\rsynthetic-secret", *URLS)
    assert result.returncode == 1 and result.stdout == ""
    assert not path.exists() and list(tmp_path.iterdir()) == []
    assert "synthetic-secret" not in result.stderr
