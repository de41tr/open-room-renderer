"""Owned legacy helper transport lifecycle; all requests are fakes or loopback."""
from __future__ import annotations

import pytest

from fake_linkplay import cli


FAVORITE_ID = "00000000000040008000000000000001"
CALLS = [
    ("api", "GET", "/httpapi.asp?command=getStatusEx", None),
    ("favorites", "GET", "/orr/v1/favorites", None),
    ("favorites", "POST", "/orr/v1/favorites", {"name": "Café", "url": "https://media.example.test/a"}),
    ("favorites", "POST", "/orr/v1/favorites/current", {"name": "Café"}),
    ("favorites", "PATCH", "/orr/v1/favorites/" + FAVORITE_ID, {"name": "Café"}),
    ("favorites", "DELETE", "/orr/v1/favorites/" + FAVORITE_ID, None),
]


def invoke(call, host="127.0.0.1", port=8080):
    helper, method, path, payload = call
    if helper == "api":
        return cli.api_get(host, port, "getStatusEx")
    return cli.favorites_request(host, port, method, path.removeprefix("/orr/v1/favorites"), payload)


@pytest.mark.parametrize("call", CALLS)
@pytest.mark.parametrize("host,port", [
    ("synthetic-user:synthetic-password@127.0.0.1", 8080),
    ("127.0.0.1/path", 8080), ("127.0.0.1?query=value", 8080),
    ("127.0.0.1#fragment", 8080), ("127.0.0.1\\path", 8080),
    ("http://127.0.0.1", 8080), ("127.0.0.1:8081", 8080),
    ("127.0.0.1\n", 8080), (" 127.0.0.1", 8080), ("", 8080),
    ("[::1", 8080), ("[::1]:8081", 8080),
    ("fe80::1%lo", 8080), ("[fe80::1%25lo]", 8080),
    ("café.local", 8080), ("renderer_name.local", 8080),
    ("a" * 254, 8080), (None, 8080),
    ("127.0.0.1", 0), ("127.0.0.1", 65536), ("127.0.0.1", -1),
    ("127.0.0.1", "8080"), ("127.0.0.1", True), ("127.0.0.1", 8080.0),
])
def test_legacy_endpoint_rejected_before_session(monkeypatch, capsys, call, host, port):
    def unexpected_session():
        pytest.fail("invalid endpoint must be rejected before creating a Session")

    monkeypatch.setattr(cli.requests, "Session", unexpected_session)
    assert invoke(call, host, port) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Request failed: invalid renderer host or port\n"


@pytest.mark.parametrize("call", CALLS)
@pytest.mark.parametrize("host,port,authority", [
    ("127.0.0.1", 1, "127.0.0.1:1"),
    ("renderer.local", 65535, "renderer.local:65535"),
    ("xn--caf-dma.local", 8080, "xn--caf-dma.local:8080"),
    ("::1", 8080, "[::1]:8080"),
    ("[::1]", 8080, "[::1]:8080"),
])
def test_legacy_valid_endpoint_and_fresh_session(monkeypatch, capsys, call, host, port, authority):
    sessions = []

    class Response:
        status_code = 200
        text = '  {"raw": "Café 夜"}\nuntouched\r\n'
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True

    class Session:
        trust_env = True
        closed = False

        def __init__(self):
            self.response = Response()
            self.calls = []
            sessions.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            assert self.response.closed
            self.closed = True

        def request(self, method, url, **kwargs):
            assert self.trust_env is False
            assert method == call[1]
            assert url == "http://" + authority + call[2]
            expected = {"timeout": 10, "allow_redirects": False}
            if call[3] is not None:
                expected["json"] = call[3]
            assert kwargs == expected
            self.calls.append((method, url))
            return self.response

        def get(self, url, **kwargs):
            return self.request("GET", url, **kwargs)

    monkeypatch.setattr(cli.requests, "Session", Session)
    for _ in range(2):
        assert invoke(call, host, port) == 0
        captured = capsys.readouterr()
        assert captured.out == Response.text + "\n" and captured.err == ""
    assert len(sessions) == 2 and sessions[0] is not sessions[1]
    assert all(session.closed and session.response.closed and len(session.calls) == 1 for session in sessions)


@pytest.mark.parametrize("call", CALLS)
@pytest.mark.parametrize("outcome", [
    "no_content", "redirect", "http_error", "committed", "timeout", "request_error", "text_error",
])
def test_legacy_closes_session_and_response_without_retry(monkeypatch, capsys, call, outcome):
    counts = {"requests": 0, "response_enter": 0, "response_exit": 0, "session_exit": 0}

    class Response:
        status_code = {"no_content": 204, "redirect": 302, "http_error": 503, "committed": 503}.get(outcome, 200)

        @property
        def text(self):
            if outcome == "text_error":
                raise cli.requests.RequestException("synthetic-private-response")
            return ""

        def json(self):
            if outcome == "committed":
                return {"error": "durability_unconfirmed", "committed": True, "message": "synthetic-private-body"}
            raise ValueError("synthetic-private-response")

        def __enter__(self):
            counts["response_enter"] += 1
            return self

        def __exit__(self, *_args):
            counts["response_exit"] += 1

    class Session:
        trust_env = True

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            counts["session_exit"] += 1

        def request(self, method, url, **kwargs):
            assert self.trust_env is False
            counts["requests"] += 1
            if outcome == "timeout":
                raise cli.requests.Timeout("synthetic-private-timeout")
            if outcome == "request_error":
                raise cli.requests.RequestException("synthetic-private-request")
            return Response()

        def get(self, url, **kwargs):
            return self.request("GET", url, **kwargs)

    monkeypatch.setattr(cli.requests, "Session", Session)
    assert invoke(call) == (0 if outcome == "no_content" else 1)
    captured = capsys.readouterr()
    expected = {
        "no_content": "", "redirect": "Request failed: HTTP 302\n",
        "http_error": "Request failed: HTTP 503\n", "committed": "Request failed: HTTP 503\n",
        "timeout": "Request failed: timed out\n",
        "request_error": "Request failed: could not contact renderer\n",
        "text_error": "Request failed: could not contact renderer\n",
    }[outcome]
    if outcome == "committed" and call[0] == "favorites":
        expected = ("Favorites were replaced, but crash durability is unconfirmed. "
                    "Run favorites list before retrying.\n")
    assert captured.out == ("\n" if outcome == "no_content" else "")
    assert captured.err == expected
    response_count = 0 if outcome in {"timeout", "request_error"} else 1
    assert counts == {"requests": 1, "response_enter": response_count,
                      "response_exit": response_count, "session_exit": 1}
