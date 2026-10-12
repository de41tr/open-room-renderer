"""Bounded single-shot DSP controls, synthetic peers and private diagnostics."""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
import os
import subprocess
import sys
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from fake_linkplay import cli
from fake_linkplay import dsp_client as client
from test_cli_commands import ROOT, SECRET_URL, http_peer, invoke


@pytest.mark.parametrize("arguments,body,wire,result", [
    (("balance", "get"), "-0.25", "getChannelBalance", {"balance": -0.25}),
    (("balance", "set", "-1"), "OK", "setChannelBalance:-1",
     {"request_acknowledged": True, "command": "balance set", "value": -1.0}),
    (("balance", "set", "1"), "OK", "setChannelBalance:1",
     {"request_acknowledged": True, "command": "balance set", "value": 1.0}),
    (("balance", "set", "-0.0004"), "OK", "setChannelBalance:0.0",
     {"request_acknowledged": True, "command": "balance set", "value": 0.0}),
    (("balance", "set", "0.1236"), "OK", "setChannelBalance:0.124",
     {"request_acknowledged": True, "command": "balance set", "value": 0.124}),
    (("eq", "on"), '{"status": "0"}', "EQOn",
     {"request_acknowledged": True, "command": "eq", "value": "on"}),
    (("eq", "off"), '{"status":"0"}', "EQOff",
     {"request_acknowledged": True, "command": "eq", "value": "off"}),
])
def test_cli_exact_wire_and_small_success_schema(arguments, body, wire, result):
    with http_peer(200, body) as (port, seen):
        response = invoke(port, "dsp", *arguments)
    assert response.returncode == 0 and response.stderr == ""
    assert json.loads(response.stdout) == result
    assert len(seen) == 1
    assert urlsplit(seen[0]).path == "/httpapi.asp"
    assert parse_qs(urlsplit(seen[0]).query) == {"command": [wire]}


@pytest.mark.parametrize("value,expected", [
    ("-1", -1.0), ("1", 1.0), ("0", 0.0), ("-0", 0.0),
    ("-.5", -0.5), ("+.5", 0.5), ("1.", 1.0), ("00.25", 0.25),
    ("0.00049", 0.00049),
])
def test_balance_decimal_parser_preserves_valid_input_until_request(value, expected):
    assert client.balance_value(value) == expected


@pytest.mark.parametrize("value", [
    "", "nan", "NaN", "inf", "-inf", "Infinity", "true", "50", "-50", "100", "-100",
    "1.001", "-1.001", "1.00000000000000001", "-1.00000000000000001",
    " 0.5", "0.5 ", "0.5\n", "0,5", "0_1", "0.5%", "1e-3", "-1e-3",
    "\u0660.\u0665", "0." + "0" * 128, SECRET_URL, None, True, 0.5, [],
])
def test_balance_decimal_parser_rejects_ambiguous_nonfinite_and_out_of_range(value):
    with pytest.raises(argparse.ArgumentTypeError) as error:
        client.balance_value(value)
    assert str(error.value) == client._INVALID_BALANCE


def test_invalid_input_and_endpoints_are_private_before_session_creation(monkeypatch, capsys):
    forbidden = Mock(side_effect=AssertionError("network was used"))
    monkeypatch.setattr(client.requests, "Session", forbidden)
    for value in (True, False, None, [], {}, "0.5", Decimal("0.5"), complex(0.5),
                  float("nan"), float("inf"), float("-inf"), 1.001, -1.001, 50, 10 ** 10000):
        with pytest.raises(client.DspClientError, match="Invalid DSP balance"):
            client.DspClient().set_balance(value)
    for value in (True, 1, None, [], "get", "On", "toggle", SECRET_URL):
        with pytest.raises(client.DspClientError, match="Invalid DSP EQ input"):
            client.DspClient().set_eq(value)
    for host, port in ((SECRET_URL, 8080), ("user@example.test", 8080), ("example.test/path", 8080),
                       ("::1%scope", 8080), ("[::broken]", 8080), ("127.0.0.1", 0),
                       ("127.0.0.1", 65536), ("127.0.0.1", True), ("127.0.0.1", "8080")):
        with pytest.raises(client.DspClientError, match="Invalid renderer host or port"):
            client.DspClient(host, port)
    for arguments in (("dsp",), ("dsp", "balance"), ("dsp", "eq", "get"),
                      ("dsp", "balance", "set", "50"), ("dsp", "balance", "set", "-1.001"),
                      ("dsp", "balance", "set", SECRET_URL), ("dsp", "eq", SECRET_URL),
                      ("dsp", "balance", "get", SECRET_URL),
                      ("dsp", "balance", "get", "--port", SECRET_URL),
                      ("--host", SECRET_URL, "dsp", "eq", "on"),
                      ("--port", SECRET_URL, "dsp", "balance", "set", "0"),
                      ("dsp", "--host", SECRET_URL, "balance", "get"),
                      ("dsp", "balance", "--host", SECRET_URL, "get")):
        with pytest.raises(SystemExit) as error:
            cli.main(list(arguments))
        assert error.value.code == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "Invalid client command arguments. Run open-room-renderer --help.\n"
    for arguments in (("dsp", "balance", "get", "--host", SECRET_URL),
                      ("dsp", "balance", "set", "0", "--port", "0"),
                      ("dsp", "eq", "on", "--port", "65536")):
        assert cli.main(list(arguments)) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "Invalid renderer host or port.\n"
    forbidden.assert_not_called()
    for host in ("::1", "[::1]"):
        assert client.DspClient(host, 1234).endpoint == "http://[::1]:1234/httpapi.asp"


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
        # Bytewise yields check cumulative bounds, not only one large chunk.
        for byte in self.body:
            self.reads += 1
            yield bytes([byte])
        if self.error:
            raise self.error


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


def install_reply(monkeypatch, reply):
    session = Session(reply)
    monkeypatch.setattr(client.requests, "Session", lambda: session)
    return session


def assert_single_request_closed(session, wire):
    assert session.closed and not session.trust_env
    assert session.calls == [(("http://127.0.0.1:8080/httpapi.asp",), {
        "params": {"command": wire}, "headers": {"Accept-Encoding": "identity"},
        "timeout": 10, "allow_redirects": False, "stream": True})]
    if isinstance(session.reply, Reply):
        assert session.reply.closed
        assert session.reply.reads <= client.MAX_RESPONSE_BYTES + 1


@pytest.mark.parametrize("value,wire,canonical", [
    (-1, "-1", -1.0), (1.0, "1", 1.0), (0, "0.0", 0.0), (-0.0, "0.0", 0.0),
    (-0.00049, "0.0", 0.0), (0.00049, "0.0", 0.0), (0.0005, "0.001", 0.001),
    (-0.0005, "-0.001", -0.001), (0.1236, "0.124", 0.124),
    (-0.1236, "-0.124", -0.124), (0.9999, "1", 1.0), (-0.9999, "-1", -1.0),
])
def test_set_balance_canonicalizes_server_resolution_before_wire(monkeypatch, value, wire, canonical):
    session = install_reply(monkeypatch, Reply(headers={"Content-Length": "2"}))
    assert client.DspClient().set_balance(value) == canonical
    assert_single_request_closed(session, "setChannelBalance:" + wire)


@pytest.mark.parametrize("body,valid", [
    (b'{"status":"0"}', True), (b' \n{"status": "0"}\t', True),
    (b'{"status":"0","status":"0"}', False), (b'{"status":"1","status":"0"}', False),
    (b'{"status":"0","extra":0}', False), (b'{"status":0}', False),
    (b'{"status":false}', False), (b'{"status":"OK"}', False),
    (b'{"status":NaN}', False), (b'{"status":Infinity}', False),
    (b'{"status":{"status":"0"}}', False), (b'[{"status":"0"}]', False),
    (b'"0"', False), (b'null', False), (b'{}', False), (b'OK', False),
    (b'{"status":"0"', False), (b'{"status":"0"}{}', False),
    (b'\xef\xbb\xbf{"status":"0"}', False), (b'\xff', False), (b'', False),
])
@pytest.mark.parametrize("value,wire", [("on", "EQOn"), ("off", "EQOff")])
def test_eq_requires_exact_unique_status_schema(monkeypatch, body, valid, value, wire):
    session = install_reply(monkeypatch, Reply(body))
    if valid:
        assert client.DspClient().set_eq(value) is None
    else:
        with pytest.raises(client.DspClientError) as error:
            client.DspClient().set_eq(value)
        assert str(error.value) == client._UNKNOWN
    assert_single_request_closed(session, wire)


@pytest.mark.parametrize("body", [b"", b"ok", b"OK\n", b" OK", b'{"status":"0"}', b"Failed", SECRET_URL.encode()])
def test_balance_set_requires_exact_plain_ok(monkeypatch, body):
    session = install_reply(monkeypatch, Reply(body))
    with pytest.raises(client.DspClientError) as error:
        client.DspClient().set_balance(0.5)
    assert str(error.value) == client._UNKNOWN
    assert_single_request_closed(session, "setChannelBalance:0.5")


@pytest.mark.parametrize("body,expected", [
    (b"-1", -1.0), (b"1", 1.0), (b"0", 0.0), (b"0.0", 0.0), (b"-0", 0.0), (b"-0.375", -0.375),
])
def test_balance_get_returns_only_valid_renderer_scalar(monkeypatch, body, expected):
    session = install_reply(monkeypatch, Reply(body, headers={"Content-Length": str(len(body))}))
    assert client.DspClient().get_balance() == expected
    assert_single_request_closed(session, "getChannelBalance")


@pytest.mark.parametrize("body", [
    b"", b"nan", b"inf", b"true", b"null", b"50", b"1.001", b"-50", b"1.00000000000000001",
    b'"0.5"', b'{"balance":0.5}', b"0.5\n", b" 0.5", b"0\x00", b"\xff", SECRET_URL.encode(),
])
def test_balance_get_never_returns_fallback_on_malformed_read(monkeypatch, capsys, body):
    session = install_reply(monkeypatch, Reply(body))
    assert cli.main(["dsp", "balance", "get"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == client._READ_FAILED + "\n"
    assert_single_request_closed(session, "getChannelBalance")


@pytest.mark.parametrize("method,value,wire,success_body", [
    ("set_balance", 0.5, "setChannelBalance:0.5", b"OK"),
    ("set_eq", "on", "EQOn", b'{"status":"0"}'),
    ("get_balance", None, "getChannelBalance", b"0.5"),
])
def test_bounded_responses_failures_and_closure(monkeypatch, method, value, wire, success_body):
    failure = client._READ_FAILED if method == "get_balance" else client._UNKNOWN
    cases = [Reply(success_body), Reply(success_body, status=204), Reply(success_body, status=302),
             Reply(b"Failed", status=502), Reply(SECRET_URL.encode(), status=500),
             Reply(success_body, headers={"Content-Encoding": "gzip"}),
             Reply(success_body, headers={"Content-Length": str(client.MAX_RESPONSE_BYTES + 1)}),
             Reply(success_body, headers={"Content-Length": "3, 3"}),
             Reply(success_body, headers={"Content-Length": "-1"}),
             Reply(success_body, headers={"Content-Length": "+3"}),
             Reply(success_body, headers={"Content-Length": "1"}),
             Reply(success_body, headers={"Content-Length": str(len(success_body) + 1)}),
             Reply(b" " * (client.MAX_RESPONSE_BYTES + 1)),
             Reply(success_body, error=requests.Timeout(SECRET_URL)),
             Reply(success_body, error=requests.exceptions.ChunkedEncodingError(SECRET_URL)),
             requests.ConnectionError(SECRET_URL)]
    for index, reply in enumerate(cases):
        session = install_reply(monkeypatch, reply)
        call = getattr(client.DspClient(), method)
        if index == 0:
            call() if value is None else call(value)
        else:
            with pytest.raises(client.DspClientError) as error:
                call() if value is None else call(value)
            assert str(error.value) == failure
        assert_single_request_closed(session, wire)


@pytest.mark.parametrize("body,status,headers,rejected", [
    (b"Failed", 400, {}, True), (b"Failed", 400, {"Content-Length": "6"}, True),
    (b"Failed\n", 400, {}, False), (b"Failed", 200, {}, False),
    (b"Failed", 502, {}, False), (b"Failed", 500, {}, False), (b"OK", 400, {}, False),
    (b"Failed", 400, {"Content-Encoding": "gzip"}, False),
    (b"Failed", 400, {"Content-Length": "7"}, False),
    (b'{"error":"Failed"}', 400, {}, False),
])
@pytest.mark.parametrize("arguments,wire", [
    (["balance", "set", "0"], "setChannelBalance:0.0"), (["eq", "off"], "EQOff"),
])
def test_only_complete_400_failed_is_request_rejection(monkeypatch, capsys, body, status, headers,
                                                      rejected, arguments, wire):
    session = install_reply(monkeypatch, Reply(body, status=status, headers=headers))
    assert cli.main(["dsp", *arguments]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (client._REJECTED if rejected else client._UNKNOWN) + "\n"
    assert_single_request_closed(session, wire)


def test_eq_response_budget_includes_leading_whitespace(monkeypatch):
    ack = b'{"status":"0"}'
    for size in (client.MAX_RESPONSE_BYTES, client.MAX_RESPONSE_BYTES + 1):
        session = install_reply(monkeypatch, Reply(b" " * (size - len(ack)) + ack))
        if size == client.MAX_RESPONSE_BYTES:
            client.DspClient().set_eq("on")
        else:
            with pytest.raises(client.DspClientError, match="outcome is unknown"):
                client.DspClient().set_eq("on")
        assert_single_request_closed(session, "EQOn")


def test_cli_unknown_ack_never_redirects_retries_or_echoes():
    with http_peer(200, '{"status":"0"}') as (target_port, target_seen):
        for status, body, disconnect, location in (
            (200, "OK", False, None), (502, SECRET_URL, False, None),
            (200, "", True, None), (307, SECRET_URL, False, f"http://127.0.0.1:{target_port}/"),
        ):
            with http_peer(status, body, disconnect=disconnect, location=location) as (port, seen):
                result = invoke(port, "dsp", "eq", "on")
            assert len(seen) == 1
            assert result.returncode == 1 and result.stdout == ""
            assert result.stderr == client._UNKNOWN + "\n"
        assert target_seen == []


def test_read_rejection_is_not_a_value_or_success(monkeypatch, capsys):
    session = install_reply(monkeypatch, Reply(b"Failed", status=400))
    assert cli.main(["dsp", "balance", "get"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == client._READ_FAILED + "\n"
    assert_single_request_closed(session, "getChannelBalance")


def test_cli_ignores_environment_proxy_and_netrc(tmp_path):
    credentials = tmp_path / "netrc"
    credentials.write_text("machine 127.0.0.1 login synthetic-user password synthetic-secret\n")
    headers = []
    with http_peer(200, '{"status":"0"}', request_headers=headers) as (port, seen):
        env = {**os.environ, "PYTHONPATH": str(ROOT), "NETRC": str(credentials),
               "HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1",
               "ALL_PROXY": "http://127.0.0.1:1", "all_proxy": "http://127.0.0.1:1",
               "NO_PROXY": "", "no_proxy": ""}
        result = subprocess.run([sys.executable, "-m", "fake_linkplay.cli", "dsp", "eq", "off",
                                 "--host", "127.0.0.1", "--port", str(port)],
                                cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0 and result.stderr == ""
    assert len(seen) == len(headers) == 1
    assert "Authorization" not in headers[0] and "Proxy-Authorization" not in headers[0]
