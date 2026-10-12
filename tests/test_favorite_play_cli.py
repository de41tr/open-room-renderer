"""Stable-ID favorite CLI: exact one-shot bodyless requests and private ACKs."""
from __future__ import annotations

import json

import pytest

from fake_linkplay import cli, favorites_client
from test_favorites_cli import API_PATH, FAVORITE_ID, MEDIA_URL, http_peer, invoke


def acknowledgement(identifier=FAVORITE_ID):
    return {"request_acknowledged": True, "favorite_id": identifier}


@pytest.mark.parametrize("identifier", [FAVORITE_ID, "configured:2"])
def test_favorite_play_cli_sends_one_bodyless_post_and_prints_only_ack(identifier):
    with http_peer(200, json.dumps(acknowledgement(identifier))) as (port, received):
        result = invoke(port, "play", identifier)
    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout) == acknowledgement(identifier)
    assert len(received) == 1
    assert (received[0].method, received[0].path, received[0].body, received[0].content_type) == (
        "POST", API_PATH + "/" + identifier + "/play", b"", None)


def test_favorite_play_cli_rejects_misleading_or_unbounded_200_responses():
    invalid = [
        "OK", "", "[]", json.dumps({"played": FAVORITE_ID}),
        json.dumps(acknowledgement("configured:1")),
        json.dumps({"request_acknowledged": 1, "favorite_id": FAVORITE_ID}),
        json.dumps({"request_acknowledged": "true", "favorite_id": FAVORITE_ID}),
        json.dumps({**acknowledgement(), "private": MEDIA_URL}),
        '{"request_acknowledged":true,"favorite_id":"' + FAVORITE_ID + '","favorite_id":"' + FAVORITE_ID + '"}',
        json.dumps(acknowledgement()) + " " + MEDIA_URL,
        " " * (favorites_client.MAX_ACK_BYTES + 1) + json.dumps(acknowledgement()),
    ]
    for body in invalid:
        with http_peer(200, body) as (port, received):
            result = invoke(port, "play", FAVORITE_ID)
        assert len(received) == 1
        assert result.returncode == 1 and result.stdout == ""
        assert result.stderr == favorites_client._UNCONFIRMED + "\n"
        assert MEDIA_URL not in result.stderr


def test_favorite_play_cli_rejects_non_200_status_and_never_follows_redirect():
    with http_peer(200, json.dumps(acknowledgement())) as (target_port, target_received):
        for status in (201, 204, 302, 307, 400, 404, 502, 503):
            with http_peer(status, MEDIA_URL,
                           location=f"http://127.0.0.1:{target_port}/unexpected") as (port, received):
                result = invoke(port, "play", FAVORITE_ID)
            assert len(received) == 1 and target_received == []
            assert result.returncode == 1 and result.stdout == ""
            assert result.stderr == (f"Favorite playback was not confirmed: HTTP {status}. "
                                     "Check status before retrying; no retry was sent.\n")


def test_favorite_play_cli_validates_ids_without_loosening_edit_validation(capsys):
    parser = cli.build_parser(private_client_errors=True)
    assert parser.parse_args(["favorites", "play", "configured:999999"]).favorite_id == "configured:999999"
    for identifier in ("configured:0", "configured:01", "configured:+1", "configured:１",
                       "configured:1000000", "configured:1\n", "configured:1/extra",
                       FAVORITE_ID + "?token=synthetic-secret", "A" * 32, "../current", ""):
        with pytest.raises(SystemExit) as error:
            parser.parse_args(["favorites", "play", identifier])
        assert error.value.code == 2
    for command, tail in (("rename", ["New name"]), ("delete", [])):
        with pytest.raises(SystemExit) as error:
            parser.parse_args(["favorites", command, "configured:1", *tail])
        assert error.value.code == 2
    assert "synthetic-secret" not in capsys.readouterr().err


def test_malformed_favorite_play_arguments_are_private_before_dispatch(monkeypatch, capsys):
    monkeypatch.setattr(favorites_client.requests, "Session", lambda: pytest.fail("invalid arguments reached network"))
    for args in (["--unexpected", MEDIA_URL, "favorites", "play", FAVORITE_ID],
                 ["favorites", "--unexpected", MEDIA_URL, "play", FAVORITE_ID],
                 ["favorites", "play", FAVORITE_ID, MEDIA_URL],
                 ["favorites", "play", FAVORITE_ID, "--port", MEDIA_URL],
                 ["favorites", "play", MEDIA_URL]):
        with pytest.raises(SystemExit) as error:
            cli.main(args)
        assert error.value.code == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "Invalid client command arguments. Run open-room-renderer --help.\n"
    for args in (["--host", MEDIA_URL], ["--port", "0"]):
        assert cli.main(["favorites", "play", FAVORITE_ID, *args]) == 1
        captured = capsys.readouterr()
        assert captured.out == "" and captured.err == "Invalid favorite ID or renderer host/port.\n"


def test_favorite_play_cli_uses_bounded_private_session_and_closes_every_path(monkeypatch, capsys):
    attempts, closed, read = [], [], []
    headers = {"Content-Type": "application/json"}
    chunks = [json.dumps(acknowledgement()).encode()]
    failure = None

    class Response:
        status_code = 200

        def __init__(self):
            self.headers = dict(headers)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append("response")

        def iter_content(self, *, chunk_size):
            assert chunk_size == favorites_client.MAX_ACK_BYTES + 1
            read.append(True)
            yield from chunks

    class Session:
        trust_env = True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append("session")

        def post(self, endpoint, **kwargs):
            attempts.append(endpoint)
            assert self.trust_env is False
            assert endpoint == "http://[::1]:8080" + API_PATH + "/" + FAVORITE_ID + "/play"
            assert kwargs == {"timeout": 10, "allow_redirects": False, "stream": True,
                              "headers": {"Accept-Encoding": "identity"}}
            if failure:
                raise failure(MEDIA_URL)
            return Response()

    monkeypatch.setattr(favorites_client.requests, "Session", Session)
    assert favorites_client.play_favorite("::1", 8080, FAVORITE_ID) == 0
    assert len(attempts) == 1 and closed == ["response", "session"] and read == [True]
    assert json.loads(capsys.readouterr().out) == acknowledgement()
    scenarios = [
        ({"Content-Encoding": "gzip"}, [], None, False),
        ({"Content-Type": "text/html"}, [], None, False),
        ({"Content-Length": str(favorites_client.MAX_ACK_BYTES + 1)}, [], None, False),
        ({"Content-Length": "invalid"}, [], None, False),
        ({}, [b" " * favorites_client.MAX_ACK_BYTES, b"x"], None, True),
        ({}, [], favorites_client.requests.Timeout, False),
        ({}, [], favorites_client.requests.ConnectionError, False),
    ]
    for override, chunks, failure, should_read in scenarios:
        attempts.clear()
        closed.clear()
        read.clear()
        headers = {"Content-Type": "application/json", **override}
        assert favorites_client.play_favorite("::1", 8080, FAVORITE_ID) == 1
        assert len(attempts) == 1
        assert closed == (["session"] if failure else ["response", "session"])
        assert bool(read) is should_read
        captured = capsys.readouterr()
        assert captured.out == "" and MEDIA_URL not in captured.err
        assert "not confirmed" in captured.err and "no retry was sent" in captured.err
        if failure is favorites_client.requests.Timeout:
            assert "timed out" in captured.err
