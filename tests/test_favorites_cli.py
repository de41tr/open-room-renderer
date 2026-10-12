"""Managed-favorites CLI subprocesses use only synthetic loopback HTTP peers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Thread

import pytest


ROOT = Path(__file__).resolve().parents[1]
FAVORITE_ID = "00000000000040008000000000000001"
API_PATH = "/orr/v1/favorites"
NAME = "Café 夜のラジオ 🎵"
MEDIA_URL = "https://media.example.test/音声/a%2Fb?token=synthetic-secret&name=a b+c#部分"
ARTWORK_URL = "https://images.example.test/カバー.jpg?size=large&token=synthetic-art"
COMMANDS = [
    ("list",),
    ("add", NAME, MEDIA_URL),
    ("save-current", NAME),
    ("rename", FAVORITE_ID, NAME),
    ("delete", FAVORITE_ID),
]


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    content_type: str | None
    body: bytes


@contextmanager
def http_peer(
    status: int = 200, body: str = "OK", *,
    disconnect: bool = False, location: str | None = None,
    request_headers: list[dict[str, str]] | None = None,
) -> Iterator[tuple[int, list[RecordedRequest]]]:
    received: list[RecordedRequest] = []

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self) -> None:
            if request_headers is not None:
                request_headers.append(dict(self.headers))
            length = int(self.headers.get("Content-Length", "0"))
            received.append(RecordedRequest(
                self.command, self.path, self.headers.get("Content-Type"), self.rfile.read(length),
            ))
            if disconnect:
                self.close_connection = True
                return
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            if location is not None:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PATCH = do_DELETE = handle_request

        def log_message(self, *_args: object) -> None:
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


def invoke(
    port: int, *arguments: str, env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ, "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1",
        "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1",
    }
    env.update(env_overrides or {})
    return subprocess.run(
        [sys.executable, "-m", "fake_linkplay.cli", "favorites", *arguments,
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=15,
    )


@pytest.mark.parametrize("arguments,method,path,payload,status", [
    (("list",), "GET", API_PATH, None, 200),
    (("add", NAME, MEDIA_URL), "POST", API_PATH, {"name": NAME, "url": MEDIA_URL}, 201),
    (("add", NAME, MEDIA_URL, "--title", "Été / 夏", "--artist", "Synthetic & Artist",
      "--album", "Album + One", "--artwork", ARTWORK_URL), "POST", API_PATH,
     {"name": NAME, "url": MEDIA_URL, "title": "Été / 夏", "artist": "Synthetic & Artist",
      "album": "Album + One", "artwork": ARTWORK_URL}, 201),
    (("add", NAME, MEDIA_URL, "--title", ""), "POST", API_PATH,
     {"name": NAME, "url": MEDIA_URL, "title": ""}, 201),
    (("save-current", NAME), "POST", API_PATH + "/current", {"name": NAME}, 201),
    (("rename", FAVORITE_ID, NAME), "PATCH", API_PATH + "/" + FAVORITE_ID, {"name": NAME}, 200),
    (("delete", FAVORITE_ID), "DELETE", API_PATH + "/" + FAVORITE_ID, None, 204),
])
def test_favorites_cli_exact_routes_and_json_payloads(
    arguments: tuple[str, ...], method: str, path: str,
    payload: dict[str, str] | None, status: int,
) -> None:
    response = "" if status == 204 else '{"result":"保存済み café"}'
    with http_peer(status, response) as (port, received):
        result = invoke(port, *arguments)
    assert result.returncode == 0
    assert result.stdout == response + "\n"
    assert result.stderr == ""
    assert len(received) == 1
    request = received[0]
    assert (request.method, request.path) == (method, path)
    if payload is None:
        assert request.body == b"" and request.content_type is None
    else:
        assert request.content_type == "application/json"
        assert json.loads(request.body) == payload
        assert request.body == json.dumps(payload, allow_nan=False).encode("utf-8")


@pytest.mark.parametrize("arguments", COMMANDS)
@pytest.mark.parametrize("status", [400, 404, 409, 413, 503])
def test_favorites_cli_http_failure_hides_response_body_and_urls(
    arguments: tuple[str, ...], status: int,
) -> None:
    with http_peer(status, NAME + " " + MEDIA_URL) as (port, received):
        result = invoke(port, *arguments)
    assert len(received) == 1
    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr == f"Request failed: HTTP {status}\n"


@pytest.mark.parametrize("arguments", COMMANDS)
@pytest.mark.parametrize("status", [300, 301, 302, 303, 307, 308])
def test_favorites_cli_redirects_are_not_followed(
    arguments: tuple[str, ...], status: int,
) -> None:
    with http_peer(200, "misleading success") as (target_port, target_received):
        location = f"http://127.0.0.1:{target_port}/unexpected?token=synthetic-secret"
        with http_peer(status, MEDIA_URL, location=location) as (port, received):
            result = invoke(port, *arguments)
    assert len(received) == 1 and target_received == []
    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr == f"Request failed: HTTP {status}\n"


@pytest.mark.parametrize("command", ["rename", "delete"])
@pytest.mark.parametrize("invalid_id", [
    "../current", FAVORITE_ID + "/../current", "..%2Fcurrent", FAVORITE_ID + "?secret=value",
    FAVORITE_ID + "#fragment", "a" * 31, "a" * 33, "A" * 32,
    "00000000-0000-4000-8000-000000000001", "０" * 32, FAVORITE_ID + "\n", "",
])
def test_favorites_cli_rejects_unsafe_identifiers_without_network(
    command: str, invalid_id: str,
) -> None:
    arguments = (command, invalid_id, NAME) if command == "rename" else (command, invalid_id)
    with http_peer() as (port, received):
        result = invoke(port, *arguments)
    assert received == []
    assert result.returncode == 2 and result.stdout == ""
    assert "expected a 32-character lowercase hexadecimal favorite ID" in result.stderr


@pytest.mark.parametrize("arguments", COMMANDS)
def test_favorites_cli_connection_refusal_is_safe(arguments: tuple[str, ...]) -> None:
    # Hold the port without listening so another process cannot claim it.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        result = invoke(reserved.getsockname()[1], *arguments)
    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr == "Request failed: could not contact renderer\n"


def test_favorites_cli_disconnected_peer_is_safe() -> None:
    with http_peer(disconnect=True) as (port, received):
        result = invoke(port, "add", NAME, MEDIA_URL)
    assert len(received) == 1
    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr == "Request failed: could not contact renderer\n"


@pytest.mark.parametrize("arguments,method,path,payload", [
    (("list",), "GET", API_PATH, None),
    (("add", NAME, MEDIA_URL), "POST", API_PATH, {"name": NAME, "url": MEDIA_URL}),
    (("save-current", NAME), "POST", API_PATH + "/current", {"name": NAME}),
    (("rename", FAVORITE_ID, NAME), "PATCH", API_PATH + "/" + FAVORITE_ID, {"name": NAME}),
    (("delete", FAVORITE_ID), "DELETE", API_PATH + "/" + FAVORITE_ID, None),
])
def test_favorites_cli_timeout_and_request_options(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    arguments: tuple[str, ...], method: str, path: str, payload: dict[str, str] | None,
) -> None:
    from fake_linkplay import cli

    def timeout(actual_method: str, url: str, **kwargs: object) -> None:
        assert actual_method == method
        assert url == "http://127.0.0.1:8080" + path
        expected: dict[str, object] = {"timeout": 10, "allow_redirects": False}
        if payload is not None:
            expected["json"] = payload
        assert kwargs == expected
        raise cli.requests.Timeout("sensitive request: " + MEDIA_URL)

    class Session:
        trust_env = True
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True

        def request(self, method, url, **kwargs):
            assert self.trust_env is False
            return timeout(method, url, **kwargs)

    session = Session()
    monkeypatch.setattr(cli.requests, "Session", lambda: session)
    assert cli.main(["favorites", *arguments]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Request failed: timed out\n"
    assert session.closed


@pytest.mark.parametrize("body,committed_warning", [
    ({"error": "durability_unconfirmed", "committed": True, "message": MEDIA_URL}, True),
    ({"error": "durability_unconfirmed", "committed": False, "message": MEDIA_URL}, False),
    ({"error": "durability_unconfirmed", "committed": "true", "message": MEDIA_URL}, False),
    ({"error": "unknown", "committed": True, "message": MEDIA_URL}, False),
    ([{"error": "durability_unconfirmed", "committed": True}], False),
])
def test_cli_committed_failure_warns_without_echoing_arbitrary_response(body, committed_warning):
    with http_peer(503, json.dumps(body)) as (port, received):
        result = invoke(port, "add", NAME, MEDIA_URL)
    assert result.returncode == 1 and result.stdout == "" and len(received) == 1
    expected = ("Favorites were replaced, but crash durability is unconfirmed. "
                "Run favorites list before retrying.\n") if committed_warning else "Request failed: HTTP 503\n"
    assert result.stderr == expected
    assert MEDIA_URL not in result.stderr and NAME not in result.stderr


@pytest.mark.parametrize("arguments", COMMANDS)
def test_favorites_cli_ignores_environment_proxy_and_synthetic_netrc(tmp_path, arguments):
    # Explicitly clear both NO_PROXY spellings after invoke's default environment.
    netrc = tmp_path / "synthetic.netrc"
    netrc.write_text("machine 127.0.0.1 login synthetic-user password synthetic-password\n")
    netrc.chmod(0o600)
    headers = []
    with http_peer(200, "unexpected proxy") as (proxy_port, proxy_received):
        proxy = f"http://synthetic-proxy:synthetic-password@127.0.0.1:{proxy_port}"
        env = {key: proxy for key in (
            "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY",
        )}
        env.update({"NETRC": str(netrc), "NO_PROXY": "", "no_proxy": ""})
        with http_peer(200, "direct renderer", request_headers=headers) as (port, received):
            result = invoke(port, *arguments, env_overrides=env)
    assert result.returncode == 0 and result.stdout == "direct renderer\n"
    assert result.stderr == ""
    assert len(received) == 1 and proxy_received == []
    assert len(headers) == 1
    assert {name.lower() for name in headers[0]}.isdisjoint({"authorization", "proxy-authorization"})
