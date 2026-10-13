"""Legacy CLI redirects stay private; existing non-streaming reads are unchanged."""
from __future__ import annotations

from io import BytesIO
import json

import pytest
import requests
from requests.adapters import BaseAdapter

from test_cli_commands import SECRET_URL, invoke as invoke_api
from test_favorites_cli import COMMANDS, http_peer, invoke as invoke_favorites
from test_legacy_cli_transport import CALLS, invoke


PRIVATE = "SYNTHETIC_PRIVATE_REDIRECT"
LOCATIONS = (
    f"http://redirect.example.test/{PRIVATE}?token=synthetic-secret",
    f"http://[{PRIVATE}]/",
    f"http://redirect.example.test/\xff{PRIVATE}",
)


class RedirectAdapter(BaseAdapter):
    """Replace only the wire adapter, preserving real Session.send behavior."""

    def __init__(self, call, status, location):
        self.call = call
        self.status = status
        self.location = location
        self.calls = []
        self.response = None
        self.closed = False

    def send(self, request, **kwargs):
        self.calls.append((request, kwargs))
        assert len(self.calls) == 1, "a redirect, retry or second mutation was sent"
        assert request.method == self.call[1]
        assert request.url == "http://127.0.0.1:8080" + self.call[2]
        assert not {name.lower() for name in request.headers} & {"authorization", "proxy-authorization"}
        if self.call[3] is None:
            assert request.body is None
        else:
            assert json.loads(request.body) == self.call[3]
        response = requests.Response()
        response.status_code = self.status
        response.headers["Location"] = self.location
        response.raw = BytesIO(PRIVATE.encode())
        response.request = request
        response.url = request.url
        self.response = response
        return response

    def close(self):
        self.closed = True


@pytest.mark.parametrize("call", CALLS)
@pytest.mark.parametrize("status", (301, 302, 303, 307, 308))
@pytest.mark.parametrize("location", LOCATIONS, ids=("normal", "malformed-bracket", "invalid-utf8"))
def test_actual_requests_preserves_private_redirect_status(monkeypatch, capsys, call, status, location):
    session = requests.Session()
    adapter = RedirectAdapter(call, status, location)
    session.mount("http://", adapter)
    monkeypatch.setattr(requests, "Session", lambda: session)

    assert invoke(call) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"Request failed: HTTP {status}\n"
    assert len(adapter.calls) == 1
    _request, options = adapter.calls[0]
    assert options["stream"] is False and options["timeout"] == 10
    assert options["proxies"] == {} and session.trust_env is False
    assert adapter.closed
    assert adapter.response.next is None
    # These legacy helpers still eagerly consume bodies without a byte cap.
    assert adapter.response._content_consumed
    assert adapter.response.content == PRIVATE.encode()


@pytest.mark.parametrize("arguments", [("play", SECRET_URL), *[("favorites", *args) for args in COMMANDS]])
@pytest.mark.parametrize("location_kind", ("normal", "malformed-bracket", "invalid-utf8"))
def test_cli_redirect_errors_are_private_without_target_contact(arguments, location_kind):
    with http_peer(200, "unexpected target") as (target_port, target_received):
        locations = {
            "normal": f"http://127.0.0.1:{target_port}/{PRIVATE}?token=synthetic-secret",
            "malformed-bracket": f"http://[{PRIVATE}]/",
            "invalid-utf8": f"http://127.0.0.1:{target_port}/\xff{PRIVATE}",
        }
        with http_peer(307, PRIVATE, location=locations[location_kind]) as (port, received):
            if arguments[0] == "favorites":
                result = invoke_favorites(port, *arguments[1:])
            else:
                result = invoke_api(port, *arguments)
    assert len(received) == 1 and target_received == []
    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr == "Request failed: HTTP 307\n"
