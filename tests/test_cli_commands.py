"""Actual CLI processes against loopback HTTP; no native playback required."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Thread
from urllib.parse import parse_qs, unquote, urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]
SECRET_URL = "https://media.example.test/track?token=synthetic-secret&name=a b"


@contextmanager
def http_peer(status, body="OK", *, disconnect=False, location=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if disconnect:
                self.close_connection = True
                return
            payload = body.encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            if location is not None:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .02})
        thread.start()
        try:
            yield server.server_port, requests
        finally:
            server.shutdown()
            thread.join(timeout=2)
            assert not thread.is_alive()


def invoke(port, *args):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
    return subprocess.run([sys.executable, "-m", "fake_linkplay.cli", *args,
        "--host", "127.0.0.1", "--port", str(port)], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=15)


@pytest.mark.parametrize("arguments,command", [
    (("status",), "getStatusEx"), (("pause",), "setPlayerCmd:pause"),
    (("resume",), "setPlayerCmd:resume"), (("stop",), "setPlayerCmd:stop"),
    (("volume", "37"), "setPlayerCmd:vol:37"),
    (("play", SECRET_URL), "setPlayerCmd:play:" + SECRET_URL),
])
def test_cli_success_and_command_encoding(arguments, command):
    with http_peer(200, "synthetic response") as (port, requests):
        result = invoke(port, *arguments)
    assert result.returncode == 0 and result.stdout == "synthetic response\n"
    assert result.stderr == ""
    assert len(requests) == 1
    query = parse_qs(urlsplit(requests[0]).query)
    assert list(query) == ["command"]
    assert unquote(query["command"][0]) == command


@pytest.mark.parametrize("status", [400, 404, 503])
def test_cli_http_error_is_nonzero_without_leaking_url_or_body(status):
    with http_peer(status, SECRET_URL) as (port, requests):
        result = invoke(port, "play", SECRET_URL)
    assert len(requests) == 1
    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr == f"Request failed: HTTP {status}\n"


def test_cli_connection_refusal_is_nonzero_without_url_traceback():
    # Reserve the port without listening so no unrelated process can claim it.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        result = invoke(reserved.getsockname()[1], "play", SECRET_URL)
    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr == "Request failed: could not contact renderer\n"


def test_cli_disconnected_http_peer_is_nonzero_without_url_traceback():
    with http_peer(200, disconnect=True) as (port, requests):
        result = invoke(port, "play", SECRET_URL)
    assert requests
    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr == "Request failed: could not contact renderer\n"


@pytest.mark.parametrize("status", [300, 301, 302, 303, 307, 308])
def test_cli_redirect_is_a_safe_failure_and_never_contacts_target(status):
    with http_peer(200, "misleading success") as (target_port, target_requests):
        location = f"http://127.0.0.1:{target_port}/httpapi.asp?command=tokenized-redirect"
        with http_peer(status, SECRET_URL, location=location) as (port, requests):
            result = invoke(port, "play", SECRET_URL)
    assert len(requests) == 1 and target_requests == []
    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr == f"Request failed: HTTP {status}\n"


def test_cli_timeout_is_nonzero_without_url_traceback(monkeypatch, capsys):
    from fake_linkplay import cli
    def timeout(url, **kwargs):
        assert kwargs == {"timeout": 10, "allow_redirects": False}
        raise cli.requests.Timeout("sensitive request: " + url)
    monkeypatch.setattr(cli.requests, "get", timeout)
    assert cli.main(["play", SECRET_URL]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Request failed: timed out\n"
