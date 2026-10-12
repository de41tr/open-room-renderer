"""Target-edit CLI: in-process commands and synthetic threaded loopback peers."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
from threading import Thread

import pytest

from fake_linkplay import cli, favorites, favorites_target_client as client
from fake_linkplay.favorites import FavoritesStore, MAX_REQUEST_BYTES
from fake_linkplay.persistence import AtomicWriteCommittedError
from test_favorite_target import DISPLAY, EXACT
from test_favorites_cli import FAVORITE_ID
from test_music_presets import FIRST, configured, running

PATH = "/orr/v1/favorites/" + FAVORITE_ID + "/target"
NAME = "Private library label 夜"


def ack(policy="keep", url=EXACT):
    return {"favorite": {"id": FAVORITE_ID, "name": NAME, "url": url, "editable": True,
                         "slot": 3, **(DISPLAY if policy == "keep" else dict.fromkeys(DISPLAY, ""))}}


@contextmanager
def peer():
    """One fixture serves many bounded probes without spawning CLI processes."""
    reply = {"status": 200, "body": json.dumps(ack()).encode(), "headers": {}, "length": True}
    received = []
    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            received.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                             "body": self.rfile.read(int(self.headers.get("Content-Length", "0")))})
            if reply.get("disconnect"):
                self.close_connection = True
                return
            self.send_response(reply["status"])
            headers = {"Content-Type": "application/json; charset=utf-8", **reply["headers"]}
            if reply.get("chunks") is not None:
                headers["Transfer-Encoding"] = "chunked"
            elif reply["length"]:
                headers.setdefault("Content-Length", str(len(reply["body"])))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            try:
                if reply.get("chunks") is not None:
                    for chunk in reply["chunks"]:
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                else:
                    self.wfile.write(reply["body"])
            except (BrokenPipeError, ConnectionResetError):
                pass  # The bounded client deliberately closes invalid peers.
        do_PUT = do_GET = do_POST = do_PATCH = do_DELETE = handle_request
        def log_message(self, *_args):
            pass
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
        thread.start()
        try:
            yield server.server_port, reply, received
        finally:
            server.shutdown()
            thread.join(timeout=2)
            assert not thread.is_alive()


def invoke(port, policy="keep", url=EXACT):
    return cli.main(["favorites", "set-url", FAVORITE_ID, url, "--metadata", policy,
                     "--host", "127.0.0.1", "--port", str(port)])


def assert_private_failure(capsys, *, expected=None):
    captured = capsys.readouterr()
    assert captured.out == ""
    assert NAME not in captured.err and EXACT not in captured.err and "signature=" not in captured.err
    assert "no retry was sent" in captured.err
    if expected is not None:
        assert captured.err == expected + "\n"
    return captured.err


def test_target_cli_exact_one_put_keep_clear_no_proxy_netrc_or_fallback(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "favorites_request", lambda *_a, **_k: pytest.fail("legacy fallback"))
    monkeypatch.setattr(cli, "play_favorite", lambda *_a, **_k: pytest.fail("playback fallback"))
    netrc = tmp_path / "netrc"
    netrc.write_text("machine 127.0.0.1 login synthetic-user password synthetic-private\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    with peer() as (proxy_port, _, proxy_requests), peer() as (port, reply, received):
        for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(key, f"http://127.0.0.1:{proxy_port}")
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setenv("no_proxy", "")
        for index, policy in enumerate(("keep", "clear"), 1):
            reply["body"] = json.dumps(ack(policy)).encode()
            assert invoke(port, policy) == 0
            captured = capsys.readouterr()
            assert captured.err == ""
            assert json.loads(captured.out) == {
                "request_acknowledged": True, "favorite_id": FAVORITE_ID, "metadata": policy}
            assert EXACT not in captured.out and NAME not in captured.out and DISPLAY["title"] not in captured.out
            assert len(received) == index and proxy_requests == []
            request = received[-1]
            assert request["method"] == "PUT" and request["path"] == PATH
            assert json.loads(request["body"]) == {"url": EXACT, "metadata": policy}
            assert json.loads(request["body"])["url"].encode() == EXACT.encode()
            assert len(request["body"]) <= MAX_REQUEST_BYTES
            assert request["headers"]["Content-Type"] == "application/json"
            assert request["headers"]["Accept-Encoding"] == "identity"
            assert request["headers"]["Content-Length"] == str(len(request["body"]))
            assert not {"Authorization", "Proxy-Authorization", "Transfer-Encoding"} & request["headers"].keys()


def test_target_cli_accepts_maximum_unicode_record_fields_and_bounded_chunked_ack(capsys):
    prefix = "https://media.example.test/"
    url = prefix + "é" * ((8192 - len(prefix)) // 2) + "x" * ((8192 - len(prefix)) % 2)
    assert len(url.encode()) == 8192
    payload = ack(url=url)
    payload["favorite"].update(name="é" * 128, title="é" * 512, artist="é" * 512,
                               album="é" * 512, artwork=url)
    encoded = json.dumps(payload).encode()
    assert 32 * 1024 < len(encoded) < client.MAX_ACK_BYTES
    with peer() as (port, reply, received):
        reply.update(body=encoded)
        assert invoke(port, url=url) == 0
        assert capsys.readouterr().err == ""
        reply.update(chunks=[encoded[:31], encoded[31:]], length=False)
        assert invoke(port, url=url) == 0
        assert capsys.readouterr().err == "" and len(received) == 2


def test_target_cli_rejects_malformed_duplicate_unexpected_or_wrong_ack(capsys):
    malformed = [b"", b"OK", b"[]", b"null", b"true", b"\xff", b'{"favorite":NaN}',
                 json.dumps(ack()).encode() + b" trailing",
                 ('{"favorite":' + json.dumps(ack()["favorite"]) + ',"favorite":{}}').encode(),
                 json.dumps({**ack(), "private": EXACT}).encode(),
                 json.dumps({"favorite": []}).encode()]
    for field, value in (("id", "a" * 32), ("url", EXACT.replace("%2F", "%2f")),
                         ("editable", False), ("editable", 1), ("editable", "true"),
                         ("slot", 0), ("slot", -1), ("slot", True), ("slot", 1.0), ("slot", "3"),
                         ("name", ""), ("name", "x" * 257), ("name", None),
                         ("title", None), ("artist", []), ("album", "a" * 1025),
                         ("artwork", "file:///private"), ("title", "secret\ud800")):
        payload = ack()
        payload["favorite"][field] = value
        malformed.append(json.dumps(payload).encode())
    for field in ack()["favorite"]:
        payload = ack()
        del payload["favorite"][field]
        malformed.append(json.dumps(payload).encode())
    payload = ack()
    payload["favorite"]["ignored"] = EXACT
    malformed.append(json.dumps(payload).encode())
    malformed.append(json.dumps(ack()).replace('"slot": 3', '"slot": 3,"slot": 3').encode())
    with peer() as (port, reply, received):
        for count, body in enumerate(malformed, 1):
            reply["body"] = body
            assert invoke(port) == 1
            assert_private_failure(capsys, expected=client._UNCONFIRMED)
            assert len(received) == count
        # Clear acknowledges all four empty fields, never only an empty title.
        for field in DISPLAY:
            payload = ack("clear")
            payload["favorite"][field] = DISPLAY[field]
            reply["body"] = json.dumps(payload).encode()
            assert invoke(port, "clear") == 1
            assert_private_failure(capsys, expected=client._UNCONFIRMED)


def test_target_cli_limits_and_strict_json_encoding_even_for_errors(capsys):
    valid = json.dumps(ack()).encode()
    cases = [({"Content-Type": "text/html"}, valid, None),
             ({"Content-Type": "application/json; charset=latin1"}, valid, None),
             ({"Content-Type": "application/json, application/json"}, valid, None),
             ({"Content-Encoding": "gzip"}, valid, None),
             ({"Content-Encoding": "identity, identity"}, valid, None),
             ({"Content-Length": str(client.MAX_ACK_BYTES + 1)}, b"", None),
             ({"Content-Length": "invalid"}, valid, None),
             ({"Content-Length": "+20"}, valid, None),
             ({"Content-Length": str(len(valid) + 1)}, valid, None),
             ({}, b" " * (client.MAX_ACK_BYTES + 1), None),
             ({}, b"", [b" " * client.MAX_ACK_BYTES, b"x"])]
    with peer() as (port, reply, received):
        for headers, body, chunks in cases:
            reply.update(headers=headers, body=body, chunks=chunks)
            assert invoke(port) == 1
            assert_private_failure(capsys, expected=client._UNCONFIRMED)
        assert len(received) == len(cases)
        reply.update(status=503, headers={}, body=b" " * (client.MAX_ERROR_BYTES + 1), chunks=None)
        assert invoke(port) == 1
        assert_private_failure(capsys, expected=client._UNCONFIRMED)


def test_target_cli_non_200_and_redirects_never_follow_or_retry(capsys):
    with peer() as (target_port, _, target_received), peer() as (port, reply, received):
        reply.update(headers={"Location": f"http://127.0.0.1:{target_port}/unexpected"})
        for count, status in enumerate((201, 204, 300, 301, 302, 303, 307, 308, 400, 403, 404, 409, 500), 1):
            reply["status"] = status
            assert invoke(port) == 1
            assert_private_failure(capsys, expected=f"Favorite target update was not confirmed: HTTP {status}. "
                                   "Read favorites list before retrying; no retry was sent.")
            assert len(received) == count and target_received == []
        reply["disconnect"] = True
        assert invoke(port) == 1
        assert_private_failure(capsys, expected=client._UNCONFIRMED)
        assert len(received) == 14 and target_received == []
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        assert invoke(reserved.getsockname()[1]) == 1
        assert_private_failure(capsys, expected=client._UNCONFIRMED)


def test_target_cli_only_known_bounded_durability_discriminator_warns_committed(capsys):
    warning = {"error": "durability_unconfirmed", "committed": True, "message": EXACT}
    cases = [(warning, True), ({**warning, "committed": False}, False),
             ({**warning, "committed": 1}, False), ({**warning, "committed": "true"}, False),
             ({**warning, "error": "storage_unavailable"}, False), ({**warning, "ignored": EXACT}, False),
             ({**warning, "message": {}}, False), ([warning], False), ({}, False)]
    with peer() as (port, reply, received):
        reply["status"] = 503
        for payload, committed in cases:
            reply["body"] = json.dumps(payload).encode()
            assert invoke(port) == 1
            assert_private_failure(capsys, expected=client._COMMITTED if committed else client._UNCONFIRMED)
        reply["body"] = json.dumps(warning).replace('"committed": true', '"committed": true,"committed": true').encode()
        assert invoke(port) == 1
        assert_private_failure(capsys, expected=client._UNCONFIRMED)
        assert len(received) == len(cases) + 1


def test_target_cli_prevalidates_entire_request_without_network(monkeypatch, capsys):
    monkeypatch.setattr(client.requests, "Session", lambda: pytest.fail("invalid arguments reached network"))
    valid = ["127.0.0.1", 8080, FAVORITE_ID, EXACT, "keep"]
    cases = []
    for index, invalid in ((0, EXACT), (0, "user@localhost"), (1, 0), (1, 65536), (1, True),
                           (2, "configured:1"), (2, FAVORITE_ID + "?secret"), (2, "A" * 32),
                           (3, "file:///tmp/secret"), (3, "http://user:secret@media.example.test/"),
                           (3, "https://media.example.test/a b"), (3, "x" * 8193),
                           (4, None), (4, []), (4, ""), (4, "KEEP")):
        values = valid.copy()
        values[index] = invalid
        cases.append(values)
    for values in cases:
        assert client.set_favorite_url(*values) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "Invalid favorite target, metadata policy, ID or renderer host/port.\n"
    monkeypatch.setattr(client, "MAX_REQUEST_BYTES", 1)
    assert client.set_favorite_url(*valid) == 1
    assert capsys.readouterr().out == ""


def test_target_cli_malformed_arguments_anywhere_are_private_without_changing_legacy(monkeypatch, capsys):
    monkeypatch.setattr(client.requests, "Session", lambda: pytest.fail("invalid arguments reached network"))
    prefix = ["favorites", "set-url", FAVORITE_ID, EXACT]
    cases = [["--unexpected", EXACT, *prefix, "--metadata", "keep"],
             ["favorites", "--unexpected", EXACT, "set-url", FAVORITE_ID, EXACT],
             ["favorites", "set-url", EXACT, EXACT, "--metadata", "keep"],
             ["favorites", "set-url", FAVORITE_ID, "--metadata", "keep"],
             [*prefix], [*prefix, "--metadata"], [*prefix, "--metadata", EXACT],
             [*prefix, "--metadata", "keep", "--port", EXACT],
             [*prefix, "--metadata", "keep", "--unexpected", EXACT]]
    for arguments in cases:
        with pytest.raises(SystemExit) as caught:
            cli.main(arguments)
        assert caught.value.code == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "Invalid client command arguments. Run open-room-renderer --help.\n"
    for label in ("label", "set-url"):
        with pytest.raises(SystemExit):
            cli.main(["favorites", "rename", "bad-id", label])
        assert "expected a 32-character lowercase hexadecimal favorite ID" in capsys.readouterr().err
    # The previously existing play-marker behavior is deliberately unchanged.
    with pytest.raises(SystemExit):
        cli.main(["favorites", "rename", "bad-id", "play"])
    assert capsys.readouterr().err == "Invalid client command arguments. Run open-room-renderer --help.\n"


def test_target_client_closes_every_path_and_checks_headers_before_read(monkeypatch, capsys):
    attempts, closed, read = [], [], []
    headers, chunks, failure, status = {"Content-Type": "application/json"}, [json.dumps(ack()).encode()], None, 200
    class Response:
        def __init__(self):
            self.headers, self.status_code = dict(headers), status
        def __enter__(self):
            return self
        def __exit__(self, *args):
            closed.append("response")
        def iter_content(self, *, chunk_size):
            assert chunk_size == (client.MAX_ERROR_BYTES if status == 503 else client.MAX_ACK_BYTES) + 1
            read.append(True)
            yield from chunks
    class Session:
        trust_env = True
        def __enter__(self):
            return self
        def __exit__(self, *args):
            closed.append("session")
        def put(self, endpoint, **kwargs):
            attempts.append(endpoint)
            assert self.trust_env is False
            assert endpoint == "http://[::1]:8080" + PATH
            assert set(kwargs) == {"data", "timeout", "allow_redirects", "stream", "headers"}
            assert kwargs["timeout"] == 10 and kwargs["allow_redirects"] is False and kwargs["stream"] is True
            assert kwargs["headers"] == {"Accept-Encoding": "identity", "Content-Type": "application/json"}
            assert json.loads(kwargs["data"]) == {"url": EXACT, "metadata": "keep"}
            if failure:
                raise failure(EXACT)
            return Response()
    monkeypatch.setattr(client.requests, "Session", Session)
    assert client.set_favorite_url("::1", 8080, FAVORITE_ID, EXACT, "keep") == 0
    assert len(attempts) == 1 and closed == ["response", "session"] and read == [True]
    capsys.readouterr()
    for override, chunks, failure, status, should_read in [
        ({"Content-Encoding": "gzip"}, [], None, 200, False),
        ({"Content-Type": "text/html"}, [], None, 200, False),
        ({"Content-Length": str(client.MAX_ACK_BYTES + 1)}, [], None, 200, False),
        ({"Content-Length": "invalid"}, [], None, 200, False),
        ({}, [b" " * client.MAX_ACK_BYTES, b"x"], None, 200, True),
        ({}, [], client.requests.Timeout, 200, False),
        ({}, [], client.requests.ConnectionError, 200, False),
        ({}, [], None, 302, False),
        ({"Content-Length": str(client.MAX_ERROR_BYTES + 1)}, [], None, 503, False),
    ]:
        attempts.clear()
        closed.clear()
        read.clear()
        headers = {"Content-Type": "application/json", **override}
        assert client.set_favorite_url("::1", 8080, FAVORITE_ID, EXACT, "keep") == 1
        assert len(attempts) == 1 and bool(read) is should_read
        assert closed == (["session"] if failure else ["response", "session"])
        error = assert_private_failure(capsys)
        if failure is client.requests.Timeout:
            assert "timed out" in error


async def test_real_cli_to_renderer_target_transaction_and_ack(tmp_path, capsys):
    async with running(configured(tmp_path)) as (api, backend, server, _session):
        chosen = api.favorites.add({"name": NAME, "url": FIRST, **DISPLAY})
        for policy in ("keep", "clear"):
            arguments = ["favorites", "set-url", chosen["id"], EXACT, "--metadata", policy,
                         "--host", "127.0.0.1", "--port", str(server.port)]
            assert await asyncio.to_thread(cli.main, arguments) == 0
            captured = capsys.readouterr()
            assert captured.err == ""
            assert json.loads(captured.out) == {"request_acknowledged": True,
                                               "favorite_id": chosen["id"], "metadata": policy}
            assert api.favorites.document()["favorites"][-1] == {
                **chosen, "url": EXACT, **(dict.fromkeys(DISPLAY, "") if policy == "clear" else {})}
            assert FavoritesStore(api.cfg).document() == api.favorites.document()
        assert not backend.calls


async def test_committed_write_with_invalid_ack_stays_unconfirmed_without_rollback(tmp_path, monkeypatch, capsys):
    async with running(configured(tmp_path)) as (api, backend, server, _session):
        chosen = api.favorites.add({"name": NAME, "url": FIRST, **DISPLAY})
        original, edits = api.favorites.set_url, []
        def misleading_ack(identifier, url, metadata):
            edits.append(True)
            updated = original(identifier, url, metadata)
            return {**updated, "url": FIRST}
        monkeypatch.setattr(api.favorites, "set_url", misleading_ack)
        arguments = ["favorites", "set-url", chosen["id"], EXACT, "--metadata", "clear",
                     "--host", "127.0.0.1", "--port", str(server.port)]
        assert await asyncio.to_thread(cli.main, arguments) == 1
        assert_private_failure(capsys, expected=client._UNCONFIRMED)
        assert edits == [True] and not backend.calls
        assert api.favorites.document()["favorites"][-1]["url"] == EXACT
        assert FavoritesStore(api.cfg).document() == api.favorites.document()


@pytest.mark.parametrize("committed", [False, True])
async def test_real_cli_to_renderer_storage_failure_is_truthful_and_single_shot(tmp_path, monkeypatch, capsys, committed):
    async with running(configured(tmp_path)) as (api, backend, server, _session):
        chosen = api.favorites.add({"name": NAME, "url": FIRST, **DISPLAY})
        original, writes = favorites.atomic_write_text, []
        def fail(path, encoded):
            writes.append(True)
            if committed:
                original(path, encoded)
                raise AtomicWriteCommittedError(EXACT)
            raise OSError(EXACT)
        monkeypatch.setattr(favorites, "atomic_write_text", fail)
        arguments = ["favorites", "set-url", chosen["id"], EXACT, "--metadata", "clear",
                     "--host", "127.0.0.1", "--port", str(server.port)]
        assert await asyncio.to_thread(cli.main, arguments) == 1
        assert_private_failure(capsys, expected=client._COMMITTED if committed else client._UNCONFIRMED)
        assert writes == [True] and not backend.calls
        assert api.favorites.document()["favorites"][-1]["url"] == (EXACT if committed else FIRST)
        assert FavoritesStore(api.cfg).document() == api.favorites.document()
