"""Single-shot HTTP convenience controls; synthetic peers and private failures."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from fake_linkplay import cli
from fake_linkplay import transport_client as client
from test_cli_commands import ROOT, SECRET_URL, http_peer, invoke


@pytest.mark.parametrize("arguments,wire,result", [
    (("next",), "setPlayerCmd:next", {"command": "next"}),
    (("previous",), "setPlayerCmd:prev", {"command": "previous"}),
    (("mute", "on"), "setPlayerCmd:mute:1", {"command": "mute", "value": "on"}),
    (("mute", "off"), "setPlayerCmd:mute:0", {"command": "mute", "value": "off"}),
    (("seek", "0"), "setPlayerCmd:seek:0", {"command": "seek", "seconds": 0}),
    (("seek", "007"), "setPlayerCmd:seek:7", {"command": "seek", "seconds": 7}),
])
def test_cli_exact_command_mapping_and_acknowledgement(arguments, wire, result):
    with http_peer(200) as (port, seen):
        response = invoke(port, *arguments)
    assert response.returncode == 0 and response.stderr == ""
    assert json.loads(response.stdout) == {"request_acknowledged": True, **result}
    assert len(seen) == 1
    assert urlsplit(seen[0]).path == "/httpapi.asp"
    assert parse_qs(urlsplit(seen[0]).query) == {"command": [wire]}


def test_invalid_values_and_endpoints_fail_before_network_without_echo(monkeypatch, capsys):
    forbidden = Mock(side_effect=AssertionError("network was used"))
    monkeypatch.setattr(client.requests, "Session", forbidden)
    for arguments in (("seek", "-1"), ("seek", "1.5"), ("seek", "+1"),
                      ("seek", "1_0"), ("seek", "\u0661"), ("seek", " 1"),
                      ("seek", SECRET_URL), ("mute", "1"), ("mute", "true"),
                      ("mute", "toggle"), ("mute", SECRET_URL), ("next", SECRET_URL),
                      ("--host", SECRET_URL, "next"), ("--port", SECRET_URL, "seek", "1")):
        with pytest.raises(SystemExit) as error:
            cli.main(list(arguments))
        assert error.value.code == 2
        assert capsys.readouterr().err == "Invalid client command arguments. Run open-room-renderer --help.\n"
    for host, port in ((SECRET_URL, 8080), ("user@example.test", 8080),
                       ("::1%scope", 8080), ("[::broken]", 8080), ("127.0.0.1", 0),
                       ("127.0.0.1", 65536), ("127.0.0.1", True)):
        with pytest.raises(client.TransportClientError, match="Invalid renderer host or port"):
            client.TransportClient(host, port)
    for command, value in (("seek", True), ("seek", 1.0), ("seek", -1),
                           ("seek", "1"), ("mute", True), ("next", 1), ("prev", None), ([], None)):
        with pytest.raises(client.TransportClientError, match="Invalid transport input"):
            client.TransportClient().command(command, value)
    forbidden.assert_not_called()
    for host in ("::1", "[::1]"):
        assert client.TransportClient(host, 1234).endpoint == "http://[::1]:1234/httpapi.asp"


class Reply:
    def __init__(self, body=b"OK", *, status=200, headers=None, error=None):
        self.body, self.status_code, self.error = body, status, error
        self.headers = headers or {}
        self.closed = False
        self.reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def iter_content(self, chunk_size):
        assert chunk_size == client.MAX_RESPONSE_BYTES + 1
        if self.error:
            raise self.error
        # Single bytes exercise cumulative bounds, not only one large chunk.
        for byte in self.body:
            self.reads += 1
            yield bytes([byte])


class Session:
    def __init__(self, reply):
        self.reply, self.closed, self.calls = reply, False, []
        self.trust_env = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_response_limits_timeout_no_retries_and_resource_closure(monkeypatch):
    cases = [Reply(), Reply(status=204), Reply(status=302), Reply(status=500),
             Reply(b""), Reply(b"ok"), Reply(b"OK\n"), Reply(SECRET_URL.encode()),
             Reply(headers={"Content-Length": "3"}), Reply(headers={"Content-Length": "2, 2"}),
             Reply(headers={"Content-Length": "-1"}), Reply(headers={"Content-Encoding": "gzip"}),
             Reply(error=requests.Timeout(SECRET_URL)), requests.ConnectionError(SECRET_URL)]
    for index, reply in enumerate(cases):
        session = Session(reply)
        monkeypatch.setattr(client.requests, "Session", lambda: session)
        if index == 0:
            assert client.TransportClient().command("next") is None
        else:
            with pytest.raises(client.TransportClientError) as error:
                client.TransportClient().command("next")
            assert str(error.value) == client._UNCONFIRMED
        assert session.closed and not session.trust_env
        assert session.calls == [(("http://127.0.0.1:8080/httpapi.asp",), {
            "params": {"command": "setPlayerCmd:next"}, "headers": {"Accept-Encoding": "identity"},
            "timeout": 10, "allow_redirects": False, "stream": True})]
        if isinstance(reply, Reply):
            assert reply.closed and reply.reads <= client.MAX_RESPONSE_BYTES + 1


def test_cli_uncertain_results_never_follow_redirects_retry_or_echo():
    with http_peer(200) as (target_port, target_seen):
        for status, body, disconnect, location in (
            (200, "OK\n", False, None), (503, SECRET_URL, False, None),
            (200, "", True, None), (307, SECRET_URL, False, f"http://127.0.0.1:{target_port}/"),
        ):
            with http_peer(status, body, disconnect=disconnect, location=location) as (port, seen):
                result = invoke(port, "next")
            assert len(seen) == 1
            assert result.returncode == 1 and result.stdout == ""
            assert result.stderr == client._UNCONFIRMED + "\n"
        assert not target_seen


def test_cli_ignores_proxy_and_netrc_credentials(tmp_path):
    credentials = tmp_path / "netrc"
    credentials.write_text("machine 127.0.0.1 login synthetic-user password synthetic-secret\n")
    headers = []
    with http_peer(200, request_headers=headers) as (port, seen):
        env = {**os.environ, "PYTHONPATH": str(ROOT), "NETRC": str(credentials),
               "HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1",
               "ALL_PROXY": "http://127.0.0.1:1", "all_proxy": "http://127.0.0.1:1",
               "NO_PROXY": "", "no_proxy": ""}
        result = subprocess.run([sys.executable, "-m", "fake_linkplay.cli", "mute", "off",
                                 "--host", "127.0.0.1", "--port", str(port)],
                                cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0 and result.stderr == ""
    assert len(seen) == len(headers) == 1
    assert "Authorization" not in headers[0] and "Proxy-Authorization" not in headers[0]
