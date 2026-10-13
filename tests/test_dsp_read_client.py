"""DSP show parsing/transport boundaries: synthetic data and normal TCP only."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread
from unittest.mock import Mock

import pytest
import requests

from fake_linkplay import cli, dsp_client
from fake_linkplay import dsp_read_client as client
from fake_linkplay.eq_presets import default_eq_bands

ROOT = Path(__file__).resolve().parents[1]
SECRET = "https://private.example.test/media?token=SYNTHETIC_PRIVATE"
PUBLIC_FIELDS = {"version", "status", "balance", "eq_enabled", "source", "channel_mode", "bands",
                 "observation_only", "physical_output_verified"}


def snapshot(status="CONFIRMED"):
    dsp = {"status": status, "balance": None, "eq_enabled": None, "name": "", "editor": None}
    if status == "CONFIRMED":
        dsp.update(balance=-0.375, eq_enabled=True, name=SECRET, editor={
            "source": "wifi", "channel_mode": "Stereo", "bands": default_eq_bands(),
            "revision": SECRET, "presets": [{"name": SECRET, "id": SECRET}], "plugin": SECRET})
    return {"version": 1, "dsp": dsp, "csrf_token": SECRET, "session_id": SECRET, "revision": 91,
            "device": {"name": SECRET}, "queue": {"items": [SECRET]}, "media": {"title": SECRET}}


def encoded(value=None):
    return json.dumps(snapshot() if value is None else value).encode()


def assert_private_error(error):
    assert str(error.value) == client._READ_FAILED
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__


@pytest.mark.parametrize("status", ["CONFIRMED", "PENDING", "UNKNOWN"])
def test_exact_output_allowlist_and_unknown_nulls(status):
    result = client.parse_dsp_snapshot(encoded(snapshot(status)))
    assert set(result) == PUBLIC_FIELDS
    assert result["version"] == 1 and result["status"] == status
    assert result["observation_only"] is True and result["physical_output_verified"] is False
    assert SECRET not in json.dumps(result)
    if status == "CONFIRMED":
        assert result["balance"] == -.375 and result["eq_enabled"] is True
        assert result["source"] == "wifi" and result["channel_mode"] == "Stereo"
        assert result["bands"] == default_eq_bands()
    else:
        assert all(result[key] is None for key in ("balance", "eq_enabled", "source", "channel_mode", "bands"))


@pytest.mark.parametrize("source", ["global", "wifi"])
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("balance", [-1, 0, 1, -.999, .123])
def test_supported_context_enablement_and_balance(source, enabled, balance):
    data = snapshot()
    data["dsp"].update(balance=balance, eq_enabled=enabled)
    data["dsp"]["editor"]["source"] = source
    result = client.parse_dsp_snapshot(encoded(data))
    assert (result["source"], result["eq_enabled"], result["balance"]) == (source, enabled, balance)
    assert result["bands"] == default_eq_bands()  # Bypass retains configured bands.


@pytest.mark.parametrize("mode", [-1, 0, 1, 2, 3, 5])
def test_all_supported_modes_and_numeric_boundaries(mode):
    data = snapshot()
    changes = {"a_freq": 10, "b_freq": 24000, "a_gain": -24, "b_gain": 24,
               "a_q": .1, "b_q": 30, "a_mode": mode}
    for band in data["dsp"]["editor"]["bands"]:
        band["value"] = changes.get(band["param_name"], band["value"])
    result = client.parse_dsp_snapshot(encoded(data))
    assert result["bands"] == data["dsp"]["editor"]["bands"]


@pytest.mark.parametrize("status", ["PENDING", "UNKNOWN"])
@pytest.mark.parametrize("field,value", [("balance", 0), ("balance", False), ("eq_enabled", False),
    ("eq_enabled", 0), ("editor", {}), ("editor", {"source": "wifi"}), ("name", SECRET), ("name", None)])
def test_unconfirmed_cannot_smuggle_values(status, field, value):
    data = snapshot(status)
    data["dsp"][field] = value
    with pytest.raises(client.DspReadError) as error:
        client.parse_dsp_snapshot(encoded(data))
    assert_private_error(error)


@pytest.mark.parametrize("path,value", [
    (("version",), True), (("version",), 1.0), (("version",), "1"), (("version",), 2),
    (("dsp",), None), (("dsp",), []), (("dsp", "status"), "confirmed"),
    (("dsp", "status"), "ERROR"), (("dsp", "status"), SECRET), (("dsp", "status"), []),
    (("dsp", "balance"), True), (("dsp", "balance"), "0"), (("dsp", "balance"), None),
    (("dsp", "balance"), 1.001), (("dsp", "balance"), -1.001), (("dsp", "balance"), .0001),
    (("dsp", "eq_enabled"), 1), (("dsp", "eq_enabled"), "false"), (("dsp", "eq_enabled"), None),
    (("dsp", "name"), None), (("dsp", "editor"), None), (("dsp", "editor"), []),
    (("dsp", "editor", "source"), "spotify"), (("dsp", "editor", "source"), "WiFi"),
    (("dsp", "editor", "source"), []), (("dsp", "editor", "source"), None),
    (("dsp", "editor", "channel_mode"), "Mono"), (("dsp", "editor", "channel_mode"), True),
    (("dsp", "editor", "bands"), None), (("dsp", "editor", "bands"), []),
])
def test_malformed_consumed_fields_fail_closed(path, value):
    data = snapshot()
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(client.DspReadError) as error:
        client.parse_dsp_snapshot(encoded(data))
    assert_private_error(error)


@pytest.mark.parametrize("path", [("version",), ("dsp",), ("dsp", "status"), ("dsp", "balance"),
    ("dsp", "eq_enabled"), ("dsp", "name"), ("dsp", "editor"), ("dsp", "editor", "source"),
    ("dsp", "editor", "channel_mode"), ("dsp", "editor", "bands")])
def test_missing_consumed_fields_fail_closed(path):
    data = snapshot()
    node = data
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    with pytest.raises(client.DspReadError) as error:
        client.parse_dsp_snapshot(encoded(data))
    assert_private_error(error)


@pytest.mark.parametrize("record", [None, [], {}, {"param_name": "a_gain"},
    {"param_name": "a_gain", "value": 1, "private": SECRET},
    {"param_name": SECRET, "value": 0}, {"param_name": None, "value": 0},
    {"param_name": "k_gain", "value": 0}, {"param_name": "a_freq", "value": 9.999},
    {"param_name": "a_freq", "value": 24000.001}, {"param_name": "a_q", "value": .0999},
    {"param_name": "a_q", "value": 30.001}, {"param_name": "a_gain", "value": 24.001},
    {"param_name": "a_gain", "value": -24.001}, {"param_name": "a_gain", "value": True},
    {"param_name": "a_gain", "value": "0"}, {"param_name": "a_gain", "value": None},
    {"param_name": "a_mode", "value": 4}, {"param_name": "a_mode", "value": 1.01},
    {"param_name": "a_mode", "value": "1"}, {"param_name": "a_mode", "value": True},
])
def test_malformed_numeric_records_fail_closed(record):
    data = snapshot()
    data["dsp"]["editor"]["bands"][0] = record
    with pytest.raises(client.DspReadError) as error:
        client.parse_dsp_snapshot(encoded(data))
    assert_private_error(error)


@pytest.mark.parametrize("change", ["missing", "extra", "duplicate", "outer_extra"])
def test_complete_unique_parameter_set_required(change):
    data = snapshot()
    bands = data["dsp"]["editor"]["bands"]
    if change == "missing":
        bands.pop()
    elif change == "extra":
        bands.append(deepcopy(bands[0]))
    elif change == "duplicate":
        bands[-1] = deepcopy(bands[0])
    else:
        data["dsp"]["source"] = "global"
    with pytest.raises(client.DspReadError) as error:
        client.parse_dsp_snapshot(encoded(data))
    assert_private_error(error)


@pytest.mark.parametrize("raw", [b"", b"null", b"[]", b"{}", b"1", b"true", b"\xff", b"\xef\xbb\xbf{}",
    b'{"version":1,"version":1}', b'{"private":NaN}', b'{"private":Infinity}',
    b'{"private":-Infinity}', b'{"private":1e99999}', b'{"private":1e-99999}', b'{"private":01}',
    b'{"version":1}{}', b'{"version":1', b'{"x":"\xc0\xaf"}'])
def test_invalid_json_private_failure(raw):
    with pytest.raises(client.DspReadError) as error:
        client.parse_dsp_snapshot(raw)
    assert_private_error(error)


@pytest.mark.parametrize("key", ["version", "status", "param_name", "value", "csrf_token"])
def test_duplicate_keys_anywhere_in_document_rejected(key):
    body = encoded().replace(('"' + key + '":').encode(), ('"' + key + '":null,"' + key + '":').encode(), 1)
    with pytest.raises(client.DspReadError) as error:
        client.parse_dsp_snapshot(body)
    assert_private_error(error)


@pytest.mark.parametrize("field,value", [("balance", "1.00000000000000000000000001"),
    ("balance", "-1.00000000000000000000000001"), ("balance", "0.000000000000000000000001"),
    ("a_freq", "24000.0000000000000000000001"), ("a_gain", "-24.0000000000000000000001"),
    ("a_q", "0.09999999999999999999999999"), ("a_q", "30.0000000000000000000001"),
    ("a_mode", "1.0000000000000000000000001")])
def test_exact_decimal_boundary_cannot_round_malformed_input_into_truth(field, value):
    data = snapshot()
    if field == "balance":
        data["dsp"][field] = "REPLACE_NUMBER"
    else:
        for record in data["dsp"]["editor"]["bands"]:
            if record["param_name"] == field:
                record["value"] = "REPLACE_NUMBER"
    body = encoded(data).replace(b'"REPLACE_NUMBER"', value.encode())
    with pytest.raises(client.DspReadError) as error:
        client.parse_dsp_snapshot(body)
    assert_private_error(error)


def test_nesting_and_whole_response_cap_include_unrelated_fields():
    body = encoded()
    assert client.parse_dsp_snapshot(body + b" " * (client.MAX_RESPONSE_BYTES - len(body)))["status"] == "CONFIRMED"
    data = snapshot()
    data["private"] = "[" * 2000 + '"\\' * 100 + "]" * 2000
    assert client.parse_dsp_snapshot(encoded(data))["status"] == "CONFIRMED"
    for body in (body + b" " * (client.MAX_RESPONSE_BYTES - len(body) + 1),
                 b'[{"private":' + b"[" * client.MAX_JSON_DEPTH + b"0" + b"]" * client.MAX_JSON_DEPTH + b"}]",
                 b"[" * 2000 + b"0" + b"]" * 2000):
        with pytest.raises(client.DspReadError) as error:
            client.parse_dsp_snapshot(body)
        assert_private_error(error)


class Reply:
    def __init__(self, body=None, *, status=200, headers=None, error=None, step=137):
        self.body = encoded() if body is None else body
        self.status_code, self.error, self.step = status, error, step
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.closed = False
        self.reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def iter_content(self, chunk_size):
        assert chunk_size == 65536
        for offset in range(0, len(self.body), self.step):
            chunk = self.body[offset:offset + self.step]
            self.reads += len(chunk)
            yield chunk
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
    monkeypatch.setattr(client, "_ReadSession", lambda: session)
    return session


def assert_single_get_closed(session):
    assert session.closed and not session.trust_env
    assert session.calls == [(("http://127.0.0.1:8080/orr/v1/web/state",), {
        "headers": {"Accept": "application/json", "Accept-Encoding": "identity"},
        "timeout": 10, "allow_redirects": False, "stream": True})]
    if isinstance(session.reply, Reply):
        assert session.reply.closed


@pytest.mark.parametrize("status,code", [("CONFIRMED", 0), ("UNKNOWN", 1), ("PENDING", 1)])
def test_cli_exit_status_is_semantic_not_http_success(monkeypatch, capsys, status, code):
    session = install_reply(monkeypatch, Reply(encoded(snapshot(status))))
    forbidden = Mock(side_effect=AssertionError("legacy command used"))
    monkeypatch.setattr(dsp_client, "DspClient", forbidden)
    assert cli.main(["dsp", "show"]) == code
    out = capsys.readouterr()
    assert out.err == "" and json.loads(out.out)["status"] == status
    assert SECRET not in out.out
    assert_single_get_closed(session)
    forbidden.assert_not_called()


@pytest.mark.parametrize("headers", [
    {}, {"Content-Type": "application/json; charset=utf-8"},
    {"Content-Type": 'Application/JSON; Charset="UTF-8"'},
    {"Content-Length": str(len(encoded()))}, {"Transfer-Encoding": "chunked"},
    {"Content-Encoding": "identity"},
])
def test_supported_response_headers(monkeypatch, headers):
    session = install_reply(monkeypatch, Reply(headers=headers))
    assert client.DspReadClient().show()["status"] == "CONFIRMED"
    assert_single_get_closed(session)


@pytest.mark.parametrize("headers", [
    {"Content-Type": ""}, {"Content-Type": "text/plain"}, {"Content-Type": "application/jsonp"},
    {"Content-Type": "application/json; charset=latin1"}, {"Content-Type": "application/json; x=y"},
    {"Content-Type": "application/json, application/json"}, {"Content-Encoding": "gzip"},
    {"Content-Encoding": "br"}, {"Content-Length": "-1"}, {"Content-Length": "+1"},
    {"Content-Length": "1, 1"}, {"Content-Length": "1.0"}, {"Content-Length": "1"},
    {"Content-Length": "99999999999"}, {"Content-Length": str(len(encoded()) + 1)},
    {"Content-Length": str(client.MAX_RESPONSE_BYTES + 1)},
    {"Transfer-Encoding": "chunked", "Content-Length": str(len(encoded()))},
    {"Transfer-Encoding": "gzip, chunked"}, {"Transfer-Encoding": "identity"},
])
def test_malformed_response_framing_is_private(monkeypatch, capsys, headers):
    session = install_reply(monkeypatch, Reply(headers=headers))
    assert cli.main(["dsp", "show"]) == 1
    out = capsys.readouterr()
    assert out.out == "" and out.err == client._READ_FAILED + "\n"
    assert_single_get_closed(session)


@pytest.mark.parametrize("status", [201, 204, 300, 301, 302, 303, 307, 308, 400, 403, 404, 500, 502, 503])
def test_only_http_200_can_supply_dsp_values(monkeypatch, status):
    reply = Reply(status=status, headers={"Location": SECRET})
    session = install_reply(monkeypatch, reply)
    with pytest.raises(client.DspReadError) as error:
        client.DspReadClient().show()
    assert_private_error(error)
    assert reply.reads == 0
    assert_single_get_closed(session)


@pytest.mark.parametrize("error_type", [requests.Timeout, requests.ConnectionError,
    requests.exceptions.ChunkedEncodingError, requests.exceptions.ContentDecodingError])
@pytest.mark.parametrize("during_stream", [True, False])
def test_transport_failure_and_truncated_stream_never_print_raw_errors(monkeypatch, capsys, error_type, during_stream):
    failure = error_type(SECRET)
    session = install_reply(monkeypatch, Reply(error=failure) if during_stream else failure)
    assert cli.main(["dsp", "show"]) == 1
    out = capsys.readouterr()
    assert out.out == "" and out.err == client._READ_FAILED + "\n"
    assert_single_get_closed(session)


@pytest.mark.parametrize("step", [1, 65536, client.MAX_RESPONSE_BYTES + 1])
def test_stream_cap_is_cumulative_before_parse(monkeypatch, step):
    reply = Reply(b" " * (client.MAX_RESPONSE_BYTES + 1), step=step)
    session = install_reply(monkeypatch, reply)
    with pytest.raises(client.DspReadError) as error:
        client.DspReadClient().show()
    assert_private_error(error)
    assert reply.reads <= client.MAX_RESPONSE_BYTES + 65536
    assert_single_get_closed(session)


def test_invalid_host_and_port_fail_before_network(monkeypatch, capsys):
    forbidden = Mock(side_effect=AssertionError("network was used"))
    monkeypatch.setattr(client, "_ReadSession", forbidden)
    for host, port in ((SECRET, 8080), ("user@example.test", 8080), ("example.test/path", 8080),
            ("::1%scope", 8080), ("[::broken]", 8080), ("127.0.0.1", 0),
            ("127.0.0.1", 65536), ("127.0.0.1", True), ("127.0.0.1", "8080")):
        with pytest.raises(client.DspReadError, match="^Invalid renderer host or port.$"):
            client.DspReadClient(host, port)
    assert cli.main(["dsp", "show", "--host", SECRET]) == 1
    out = capsys.readouterr()
    assert out.out == "" and out.err == "Invalid renderer host or port.\n"
    forbidden.assert_not_called()
    for host in ("::1", "[::1]"):
        assert client.DspReadClient(host, 1234).endpoint == "http://[::1]:1234/orr/v1/web/state"


def test_show_help_and_argument_errors_are_private(capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["dsp", "show", "--help"])
    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert "CONFIRMED" in help_text and "physical output" in help_text
    for arguments in (["dsp", "show", SECRET], ["dsp", "show", "--port", SECRET]):
        with pytest.raises(SystemExit) as error:
            cli.main(arguments)
        assert error.value.code == 2
        out = capsys.readouterr()
        assert out.out == "" and out.err == "Invalid client command arguments. Run open-room-renderer --help.\n"


@contextmanager
def http_peer(body, *, status=200, chunked=False, terminate_chunked=True, declared=None, extra_headers=()):
    """Normal TCP only, with no asyncio loop or Unix/socketpair activity."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.command, self.path, dict(self.headers)))
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", str(len(body) if declared is None else declared))
            for key, value in extra_headers:
                self.send_header(key, value)
            self.end_headers()
            try:
                if chunked:
                    for start in range(0, len(body), 137):
                        part = body[start:start + 137]
                        self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                    if terminate_chunked:
                        self.wfile.write(b"0\r\n\r\n")
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Rejection before body completion is expected.

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .02})
        thread.start()
        try:
            yield server.server_port, seen
        finally:
            server.shutdown()
            thread.join(timeout=2)
            assert not thread.is_alive()


def invoke(port, *, env=None):
    return subprocess.run([sys.executable, "-m", "fake_linkplay.cli", "dsp", "show",
        "--host", "127.0.0.1", "--port", str(port)], cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT), **(env or {})},
        capture_output=True, text=True, timeout=15)


@pytest.mark.parametrize("status,code", [("CONFIRMED", 0), ("UNKNOWN", 1), ("PENDING", 1)])
@pytest.mark.parametrize("chunked", [False, True])
def test_real_tcp_cli_semantics_and_one_bodyless_get(status, code, chunked):
    with http_peer(encoded(snapshot(status)), chunked=chunked) as (port, seen):
        result = invoke(port)
    assert result.returncode == code and result.stderr == ""
    assert json.loads(result.stdout)["status"] == status and SECRET not in result.stdout
    assert len(seen) == 1 and seen[0][:2] == ("GET", "/orr/v1/web/state")
    assert "Content-Length" not in seen[0][2] and "Transfer-Encoding" not in seen[0][2]
    assert seen[0][2]["Accept-Encoding"] == "identity"


def test_real_tcp_cli_ignores_proxy_netrc_and_never_sends_cookies(tmp_path):
    netrc = tmp_path / "netrc"
    netrc.write_text("machine 127.0.0.1 login synthetic-user password synthetic-secret\n")
    env = {"NETRC": str(netrc), "HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1",
           "ALL_PROXY": "http://127.0.0.1:1", "all_proxy": "http://127.0.0.1:1", "NO_PROXY": "", "no_proxy": ""}
    with http_peer(encoded(), extra_headers=(("Set-Cookie", "synthetic=secret"),)) as (port, seen):
        result = invoke(port, env=env)
    assert result.returncode == 0 and result.stderr == "" and len(seen) == 1
    assert not {"Authorization", "Proxy-Authorization", "Cookie"}.intersection(seen[0][2])


def test_real_tcp_cli_redirect_not_followed_and_truncation_fails():
    with http_peer(encoded()) as (target_port, target_seen):
        location = f"http://127.0.0.1:{target_port}/private?token=synthetic"
        with http_peer(SECRET.encode(), status=307, extra_headers=(("Location", location),)) as (port, seen):
            result = invoke(port)
        assert len(seen) == 1 and not target_seen
    assert result.returncode == 1 and result.stdout == "" and result.stderr == client._READ_FAILED + "\n"
    with http_peer(encoded(), declared=len(encoded()) + 1) as (port, seen):
        result = invoke(port)
    assert len(seen) == 1 and result.returncode == 1 and result.stdout == ""
    assert result.stderr == client._READ_FAILED + "\n"


@pytest.mark.parametrize("location", ["/elsewhere", "http://[SYNTHETIC_PRIVATE]/", "http://[bad/private", "\xff"])
def test_real_requests_never_prepare_redirect_or_read_its_body(monkeypatch, capsys, location):
    # Keep real requests preparation/send logic: mocking Session.get would hide
    # requests' eager Response.next preparation even with allow_redirects=False.
    calls = []
    response = requests.Response()
    response.status_code = 307
    response.headers.update({"Location": location, "Content-Type": "application/json",
                             "Content-Length": str(client.MAX_RESPONSE_BYTES + 1)})

    class Raw:
        closed = False
        released = False

        def stream(self, *_args, **_kwargs):
            pytest.fail("Rejected redirect body was consumed before the response budget")

        def close(self):
            self.closed = True

        def release_conn(self):
            self.released = True

    class Adapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            calls.append(request)
            response.request = request
            response.url = request.url
            return response

        def close(self):
            pass

    response.raw = Raw()
    session = client._ReadSession()
    session.mount("http://", Adapter())
    monkeypatch.setattr(client, "_ReadSession", lambda: session)
    assert cli.main(["dsp", "show"]) == 1
    out = capsys.readouterr()
    assert out.out == "" and out.err == client._READ_FAILED + "\n"
    assert len(calls) == 1 and calls[0].method == "GET" and calls[0].body is None
    assert response.next is None
    assert response.raw.closed and response.raw.released


@pytest.mark.parametrize("location", ["http://[SYNTHETIC_PRIVATE]/", "\xff"])
def test_real_tcp_cli_malformed_redirect_location_is_never_parsed(location):
    with http_peer(SECRET.encode(), status=307, extra_headers=(("Location", location),)) as (port, seen):
        result = invoke(port)
    assert len(seen) == 1 and result.returncode == 1 and result.stdout == ""
    assert result.stderr == client._READ_FAILED + "\n"



def test_real_tcp_cli_requires_complete_chunked_framing():
    with http_peer(encoded(), chunked=True, terminate_chunked=False) as (port, seen):
        result = invoke(port)
    assert len(seen) == 1 and result.returncode == 1 and result.stdout == ""
    assert result.stderr == client._READ_FAILED + "\n"
