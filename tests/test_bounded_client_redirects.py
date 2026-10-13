"""Reject redirects before Requests can read bodies or prepare another request."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import pytest
import requests
from requests.adapters import BaseAdapter

from fake_linkplay import (dsp_client, favorites_client, favorites_target_client,
                           playlist_client, queue_client, transport_client)
from test_cli_commands import http_peer


PRIVATE = "SYNTHETIC_PRIVATE_REDIRECT"
IDENTIFIER = "a" * 32
TARGET = "https://media.example.test/track?token=synthetic-private"
REDIRECTS = (301, 302, 303, 307, 308)


@dataclass(frozen=True)
class ClientCase:
    name: str
    method: str
    path: str
    limit: int
    error_type: type[Exception] | None

    def call(self, port=8080):
        if self.name == "dsp_read":
            return dsp_client.DspClient(port=port).get_balance()
        if self.name == "dsp_balance":
            return dsp_client.DspClient(port=port).set_balance(0)
        if self.name == "dsp_eq":
            return dsp_client.DspClient(port=port).set_eq("on")
        if self.name == "queue_read":
            return queue_client.QueueClient(port=port).browse()
        if self.name == "queue_replace_play":
            queue = queue_client.explicit_queue("Synthetic queue", [TARGET])
            return queue_client.QueueClient(port=port).replace(queue, play=True)
        if self.name == "playlist":
            return playlist_client.fetch_current_playlist("127.0.0.1", port)
        if self.name == "favorite_play":
            return favorites_client.play_favorite("127.0.0.1", port, IDENTIFIER)
        if self.name == "favorite_target":
            return favorites_target_client.set_favorite_url(
                "127.0.0.1", port, IDENTIFIER, TARGET, "keep")
        return transport_client.TransportClient(port=port).command("next")

    def diagnostic(self, status):
        if self.name == "dsp_read":
            return dsp_client._READ_FAILED
        if self.name in {"dsp_balance", "dsp_eq"}:
            return dsp_client._UNKNOWN
        if self.name == "queue_read":
            return queue_client._REQUEST_FAILED
        if self.name == "queue_replace_play":
            return queue_client._REPLACE_UNCONFIRMED
        if self.name == "playlist":
            return playlist_client._FAILED
        if self.name == "favorite_play":
            return (f"Favorite playback was not confirmed: HTTP {status}. "
                    "Check status before retrying; no retry was sent.")
        if self.name == "favorite_target":
            return (f"Favorite target update was not confirmed: HTTP {status}. "
                    "Read favorites list before retrying; no retry was sent.")
        return transport_client._UNCONFIRMED


CASES = (
    ClientCase("dsp_read", "GET", "/httpapi.asp", dsp_client.MAX_RESPONSE_BYTES,
               dsp_client.DspClientError),
    ClientCase("dsp_balance", "GET", "/httpapi.asp", dsp_client.MAX_RESPONSE_BYTES,
               dsp_client.DspClientError),
    ClientCase("dsp_eq", "GET", "/httpapi.asp", dsp_client.MAX_RESPONSE_BYTES,
               dsp_client.DspClientError),
    ClientCase("queue_read", "POST", queue_client.CONTROL_PATH, queue_client.MAX_SOAP_BYTES,
               queue_client.QueueClientError),
    ClientCase("queue_replace_play", "POST", queue_client.CONTROL_PATH, queue_client.MAX_SOAP_BYTES,
               queue_client.QueueClientError),
    ClientCase("playlist", "GET", playlist_client.PATH, playlist_client.MAX_PLAYLIST_BYTES,
               queue_client.QueueClientError),
    ClientCase("favorite_play", "POST", f"/orr/v1/favorites/{IDENTIFIER}/play",
               favorites_client.MAX_ACK_BYTES, None),
    ClientCase("favorite_target", "PUT", f"/orr/v1/favorites/{IDENTIFIER}/target",
               favorites_target_client.MAX_ACK_BYTES, None),
    ClientCase("transport", "GET", "/httpapi.asp", transport_client.MAX_RESPONSE_BYTES,
               transport_client.TransportClientError),
)


class GuardedRaw:
    """A virtual oversized body; fail even if eager reading would be caught."""

    def __init__(self):
        self.reads = 0
        self.closed = False
        self.released = False

    def read(self, *_args, **_kwargs):
        self.reads += 1
        raise AssertionError("redirect body was read before status rejection")

    stream = read

    def close(self):
        self.closed = True

    def release_conn(self):
        self.released = True


class RedirectAdapter(BaseAdapter):
    """Only the wire adapter is replaced: actual Session.send still runs."""

    def __init__(self, case, status, location):
        self.case = case
        self.status = status
        self.location = location
        self.raw = GuardedRaw()
        self.calls = []
        self.response = None
        self.closed = False

    def send(self, request, **kwargs):
        self.calls.append((request, kwargs))
        assert len(self.calls) == 1, "a redirect, retry or second mutation was sent"
        assert request.method == self.case.method
        assert urlsplit(request.url).netloc == "127.0.0.1:8080"
        assert urlsplit(request.url).path == self.case.path
        response = requests.Response()
        response.status_code = self.status
        response.headers.update({"Location": self.location,
                                 "Content-Length": str(self.case.limit + 4096)})
        response.raw = self.raw
        response.url = request.url
        response.request = request
        self.response = response
        return response

    def close(self):
        self.closed = True


def assert_private_failure(case, status, capsys, *, port=8080):
    if case.error_type is None:
        assert case.call(port) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        diagnostic = captured.err.removesuffix("\n")
    else:
        with pytest.raises(case.error_type) as error:
            case.call(port)
        diagnostic = str(error.value)
        assert capsys.readouterr() == ("", "")
    assert diagnostic == case.diagnostic(status)
    assert PRIVATE not in diagnostic and TARGET not in diagnostic


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
@pytest.mark.parametrize("status", REDIRECTS)
@pytest.mark.parametrize("location", (
    f"http://redirect.example.test/{PRIVATE}",
    f"http://[{PRIVATE}]/",
    f"http://redirect.example.test/\xff{PRIVATE}",
), ids=("oversized", "malformed-bracket", "invalid-utf8"))
def test_actual_requests_rejects_redirect_without_body_or_next_request(
        monkeypatch, capsys, case, status, location):
    # Patching Session itself with a response-returning double would miss
    # Requests' eager Response.next preparation under allow_redirects=False.
    session = requests.Session()
    adapter = RedirectAdapter(case, status, location)
    session.mount("http://", adapter)
    monkeypatch.setattr(requests, "Session", lambda: session)

    assert_private_failure(case, status, capsys)

    assert len(adapter.calls) == 1
    _request, options = adapter.calls[0]
    assert options["stream"] is True and options["timeout"] == 10
    assert options["proxies"] == {}
    assert session.trust_env is False
    assert adapter.raw.reads == 0
    assert adapter.raw.closed and adapter.raw.released and adapter.closed
    assert adapter.response.next is None
    assert adapter.response._content is False


@pytest.mark.parametrize("case", (CASES[0], CASES[1], CASES[-1]), ids=lambda case: case.name)
@pytest.mark.parametrize("location_kind", ("oversized", "malformed-bracket", "invalid-utf8"))
def test_normal_tcp_redirect_closes_without_reading_or_contacting_target(
        monkeypatch, capsys, case, location_kind):
    reads = []
    responses = []
    original_iter = requests.Response.iter_content
    original_close = requests.Response.close

    def observe_read(response, *args, **kwargs):
        reads.append(response)
        return original_iter(response, *args, **kwargs)

    def observe_close(response):
        original_close(response)
        responses.append(response)

    monkeypatch.setattr(requests.Response, "iter_content", observe_read)
    monkeypatch.setattr(requests.Response, "close", observe_close)
    with http_peer(200) as (target_port, target_requests):
        locations = {
            "oversized": f"http://127.0.0.1:{target_port}/{PRIVATE}",
            "malformed-bracket": f"http://[{PRIVATE}]/",
            "invalid-utf8": f"http://127.0.0.1:{target_port}/\xff{PRIVATE}",
        }
        # At most 257 body bytes: enough to exceed these clients' bounds.
        with http_peer(307, "x" * (case.limit + 1),
                       location=locations[location_kind]) as (port, requests_seen):
            assert_private_failure(case, 307, capsys, port=port)
    assert len(requests_seen) == 1 and target_requests == []
    assert reads == []
    assert len(responses) == 1
    assert responses[0].next is None and responses[0].raw.closed
