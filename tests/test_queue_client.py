"""Pure and transport-contract checks for the bounded local queue client."""
from __future__ import annotations

from html import escape
from unittest.mock import Mock
from xml.etree import ElementTree as ET

import pytest
import requests

from fake_linkplay import cli
from fake_linkplay import queue_client as client
from fake_linkplay.queue import (MAX_QUEUE_BYTES, MAX_QUEUE_TRACKS, PlaybackQueue,
                                QueueItem, parse_queue_context, serialize_queue_context)
from fake_linkplay.upnp_description import soap_response

URL = "https://media.example.test/音声/a%2Fb?token=synthetic-secret&z=2+1&z=%2f#部分"
NAME = "Café & 夜 <queue>"


def response(action="ReplaceQueue", fields=None):
    if fields is None:
        fields = {"QueueName": NAME} if action == "ReplaceQueue" else {}
    inner = "".join(f"<{key}>{escape(value)}</{key}>" for key, value in fields.items())
    return soap_response(action, "PlayQueue", inner).encode()


def test_request_frame_exact_namespace_fields_and_preserved_url_bytes():
    queue = client.explicit_queue(NAME, [URL, URL], start_index=2, mode="shuffle-repeat-all")
    encoded = client.prepare_replace(queue)
    root = ET.fromstring(encoded)
    assert root.tag == f"{{{client.SOAP_NS}}}Envelope"
    assert len(root) == 1 and root[0].tag == f"{{{client.SOAP_NS}}}Body"
    action = root[0][0]
    assert action.tag == f"{{{client.SERVICE_TYPE}}}ReplaceQueue"
    assert [field.tag for field in action] == ["QueueContext"]
    restored = parse_queue_context(action.findtext("QueueContext"))
    assert restored.name == NAME and restored.index == 2 and restored.loop == 2
    assert [item.url.encode() for item in restored.items] == [URL.encode(), URL.encode()]
    assert all(not item.metadata and not item.source and not item.id for item in restored.items)


@pytest.mark.parametrize("name,mode", client.MODES.items())
def test_modes_are_exact_existing_policy_values(name, mode):
    assert client.explicit_queue(NAME, [URL], mode=name).loop == mode


@pytest.mark.parametrize("url", [
    " " + URL, URL + " ", URL + "\n", "https://example.test/a b", "https://example.test/a\tb",
    "https://example.test/a\u2003b", "https://example.test/a\x7fb", "https://user:pass@example.test/a",
    "https://user@example.test/a", "https://example.test:0/a", "https://example.test:65536/a",
    "https://example.test:x/a", "https://[broken/a", "http:///path", "http://", "ftp://example.test/a",
    "file:///tmp/a", "//example.test/a", "https://example.test/\\a", "https://example.test/<a>",
    "https://example.test/\ud800", "https://example.test/\ufffe", "", None, 3, True,
    "http://127.0.0.1/proxy/audio?url=secret", "https://remote.example.test/proxy/audio/",
    "https://example.test/a/../proxy/audio", "https://example.test/%70roxy%2faudio",
    "https://example.test//proxy/audio", "https://example.test/proxy/x/../audio",
])
def test_explicit_urls_rejected_without_rewriting_or_leaking(url):
    with pytest.raises(client.QueueClientError) as error:
        client.explicit_queue(NAME, [url])
    assert str(error.value) == client._INVALID_INPUT


@pytest.mark.parametrize("name", ["", " ", " name", "name ", "\nname", None, 2, "bad\x00", "bad\udfff", "One\rTwo"])
def test_names_validate_without_trimming_or_leaking(name):
    with pytest.raises(client.QueueClientError, match="Invalid queue input"):
        client.explicit_queue(name, [URL])


@pytest.mark.parametrize("index", [0, -1, 3, True, "1", None])
def test_invalid_indices(index):
    with pytest.raises(client.QueueClientError, match="Invalid queue input"):
        client.explicit_queue(NAME, [URL, URL], start_index=index)


@pytest.mark.parametrize("mode", ["", "NORMAL", "repeat", "5", 4, True, None, []])
def test_invalid_modes(mode):
    with pytest.raises(client.QueueClientError, match="Invalid queue input"):
        client.explicit_queue(NAME, [URL], mode=mode)


def test_count_bounds_and_duplicate_urls_preserve_positions():
    for urls in [[], [URL] * (MAX_QUEUE_TRACKS + 1), (URL,), None]:
        with pytest.raises(client.QueueClientError):
            client.explicit_queue(NAME, urls)
    queue = client.explicit_queue(NAME, ["https://example.test/a"] * MAX_QUEUE_TRACKS,
                                  start_index=MAX_QUEUE_TRACKS)
    assert queue.count == queue.index == MAX_QUEUE_TRACKS


def test_complete_escaped_frame_limit_is_checked_before_any_network(monkeypatch, capsys):
    url = "https://example.test/?query=" + "&" * 120000
    queue = PlaybackQueue(name=NAME, items=[QueueItem(url=url)], index=1)
    assert len(serialize_queue_context(queue).encode()) < MAX_QUEUE_BYTES
    forbidden = Mock(side_effect=AssertionError("network was used"))
    monkeypatch.setattr(client.requests, "Session", forbidden)
    with pytest.raises(client.QueueClientError):
        client.prepare_replace(queue)
    assert cli.main(["queue", "load", "--name", NAME, "--play", url]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == client._INVALID_INPUT + "\n"
    forbidden.assert_not_called()


def test_request_limit_includes_exact_boundary(monkeypatch):
    encoded = client._request_body("BrowseQueue", {"QueueName": NAME})
    monkeypatch.setattr(client, "MAX_SOAP_BYTES", len(encoded))
    assert client._request_body("BrowseQueue", {"QueueName": NAME}) == encoded
    monkeypatch.setattr(client, "MAX_SOAP_BYTES", len(encoded) - 1)
    with pytest.raises(client.QueueClientError):
        client._request_body("BrowseQueue", {"QueueName": NAME})


def test_canonical_input_rejects_resolved_url_substitution():
    queue = PlaybackQueue(name=NAME, items=[QueueItem(url=URL, resolved_url="https://example.test/temp")], index=1)
    with pytest.raises(client.QueueClientError):
        client.prepare_replace(queue)


@pytest.mark.parametrize("action,fields", [
    ("ReplaceQueue", {"QueueName": NAME}), ("PlayQueueWithIndex", {}), ("SetQueueLoopMode", {}),
    ("BrowseQueue", {"QueueContext": serialize_queue_context(client.explicit_queue(NAME, [URL]))}),
])
def test_strict_success_response(action, fields):
    assert client.parse_response(response(action, fields), action) == fields


VALID = response()
BAD_RESPONSES = [
    b"", b"OK", b"<html>success</html>", b"\xff", VALID.decode().encode("utf-16"),
    VALID.replace(b"utf-8", b"iso-8859-1"),
    VALID.replace(client.SOAP_NS.encode(), b"http://www.w3.org/2003/05/soap-envelope"),
    VALID.replace(client.SERVICE_TYPE.encode(), b"urn:schemas-upnp-org:service:PlayQueue:1"),
    VALID.replace(b"ReplaceQueueResponse", b"CreateQueueResponse"),
    VALID.replace(b"<s:Body>", b"<s:Body/><s:Body>"),
    VALID.replace(b"</s:Body>", b"</s:Body><evil/>"),
    VALID.replace(b"<s:Body>", b"<s:Body>unexpected"),
    VALID.replace(b"<QueueName>", b"unexpected<QueueName>"),
    VALID.replace(b"</QueueName>", b"</QueueName>unexpected"),
    VALID.replace(b"</QueueName>", b"</QueueName><QueueName>other</QueueName>"),
    VALID.replace(b"<QueueName>", b"<QueueName><nested>" ).replace(b"</QueueName>", b"</nested></QueueName>"),
    VALID.replace(b"<QueueName>", b'<QueueName secret="no">'),
    VALID.replace(b"QueueName", b"Missing"),
    VALID.replace(b"<QueueName>", b'<QueueName xmlns="urn:evil">'),
    VALID.replace(b"</QueueName>", b"</QueueName><Result>OK</Result>"),
    b'<!DOCTYPE s:Envelope [<!ENTITY leak "secret">]>' + VALID.split(b"?>", 1)[1],
    b'<!ENTITY leak "secret">' + VALID,
    response("PlayQueueWithIndex", {"Result": "OK"}),
    soap_response("Fault", "PlayQueue", "<faultstring>secret</faultstring>").encode(),
]


@pytest.mark.parametrize("encoded", BAD_RESPONSES)
def test_response_rejects_malformed_unsafe_or_ambiguous_success(encoded):
    with pytest.raises(client.QueueClientError) as error:
        client.parse_response(encoded, "ReplaceQueue")
    assert str(error.value) == client._INVALID_RESPONSE


def test_response_depth_and_byte_limits():
    for encoded in [b" " * (client.MAX_SOAP_BYTES + 1), b"<x>" * 40 + b"</x>" * 40]:
        with pytest.raises(client.QueueClientError):
            client.parse_response(encoded, "ReplaceQueue")


class FakeResponse:
    def __init__(self, body=VALID, *, status=200, headers=None, error=None):
        self.body, self.status_code, self.error = body, status, error
        self.headers = {} if headers is None else headers
        self.closed = False
        self.reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def iter_content(self, chunk_size):
        assert chunk_size == 16384
        self.reads += 1
        if self.error:
            raise self.error
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]


class FakeSession:
    def __init__(self, response):
        self.response, self.closed, self.calls = response, False, []
        self.trust_env = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.mark.parametrize("peer", [
    FakeResponse(), FakeResponse(status=302), FakeResponse(status=500), FakeResponse(body=b"OK"),
    FakeResponse(headers={"Content-Length": str(client.MAX_SOAP_BYTES + 1)}),
    FakeResponse(headers={"Content-Length": "-1"}), FakeResponse(headers={"Content-Length": "2, 2"}),
    FakeResponse(headers={"Content-Encoding": "gzip"}),
    FakeResponse(body=b"x" * (client.MAX_SOAP_BYTES + 1)),
    FakeResponse(error=requests.Timeout("secret-URL-body")),
    FakeResponse(error=requests.ConnectionError("secret-URL-body")),
    requests.Timeout("secret-URL-body"), requests.ConnectionError("secret-URL-body"),
])
def test_transport_is_single_shot_bounded_and_always_closes(monkeypatch, peer):
    session = FakeSession(peer)
    monkeypatch.setattr(client.requests, "Session", lambda: session)
    try:
        result = client.QueueClient()._send("ReplaceQueue", b"request")
        assert result == {"QueueName": NAME}
    except client.QueueClientError as error:
        assert "secret" not in str(error)
    assert session.closed and not session.trust_env
    assert len(session.calls) == 1
    args, kwargs = session.calls[0]
    assert args == ("http://127.0.0.1:8080/upnp/control/PlayQueue",)
    assert kwargs == {"data": b"request", "timeout": 10, "allow_redirects": False, "stream": True,
                      "headers": {"SOAPAction": f'"{client.SERVICE_TYPE}#ReplaceQueue"',
                                  "Content-Type": "text/xml; charset=utf-8", "Accept-Encoding": "identity"}}
    if isinstance(peer, FakeResponse):
        assert peer.closed


@pytest.mark.parametrize("args", [
    ["load", "--name", NAME, URL, "--mode", URL],
    ["load", "--name", NAME, URL, "--start-index", URL],
    ["load", "--name", NAME, URL, "--unexpected", URL],
    ["play-index", URL, "--name", NAME], ["play-index", "1"],
    [URL], ["show", "--port", URL],
])
def test_argparse_errors_do_not_echo_user_input(args, capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["queue", *args])
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Invalid client command arguments. Run open-room-renderer --help.\n"


@pytest.mark.parametrize("host,port", [("user@example.test", 8080), ("https://example.test", 8080),
    ("example.test/path", 8080), ("example.test?token=secret", 8080), ("127.0.0.1", 0),
    ("127.0.0.1", 65536), ("127.0.0.1", True), ("::1%secret", 8080), ("::broken", 8080)])
def test_endpoint_rejects_url_and_invalid_inputs(host, port):
    with pytest.raises(client.QueueClientError, match="Invalid renderer host or port"):
        client.QueueClient(host, port)


def test_endpoint_accepts_bracketed_or_bare_ipv6():
    for host in ("::1", "[::1]"):
        assert client.QueueClient(host, 1234).endpoint == "http://[::1]:1234/upnp/control/PlayQueue"


def test_projection_hides_all_opaque_and_display_metadata():
    queue = PlaybackQueue(name=NAME, index=1, items=[QueueItem(url=URL, title=URL, artist=URL,
        album=URL, metadata=URL, artwork=URL, id=URL, source=URL, resolved_url=URL)])
    assert client.queue_summary(queue) == {"name": NAME, "count": 1, "index": 1, "mode": "normal",
                                           "items": [{"index": 1}]}
    shown = client.queue_summary(queue, include_urls=True)
    assert shown["items"] == [{"index": 1, "renderer_reported_url": URL}]
    assert "potentially resolved" in shown["url_notice"]


@pytest.mark.parametrize("index", [True, False, 0, -1, 1001, "1", 1.0, None])
def test_play_index_requires_exact_integer_before_network(monkeypatch, index):
    forbidden = Mock(side_effect=AssertionError("network was used"))
    monkeypatch.setattr(client.requests, "Session", forbidden)
    with pytest.raises(client.QueueClientError, match="Invalid queue input"):
        client.QueueClient().play_index(NAME, index)
    forbidden.assert_not_called()


def test_empty_browse_success_is_distinct_from_missing_queue_context(monkeypatch):
    queue = PlaybackQueue(name=NAME)
    valid = response("BrowseQueue", {"QueueContext": serialize_queue_context(queue)})
    session = FakeSession(FakeResponse(valid))
    monkeypatch.setattr(client.requests, "Session", lambda: session)
    shown = client.queue_summary(client.QueueClient().browse())
    assert shown == {"name": NAME, "count": 0, "index": 0, "mode": "normal", "items": []}
    assert session.closed and session.response.closed


@pytest.mark.parametrize("args", [
    ["--host", URL, "queue", "show"],
])
def test_misplaced_global_options_cannot_echo_urls(args, capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Invalid client command arguments. Run open-room-renderer --help.\n"


@pytest.mark.parametrize("action", ["replace", "browse", "play_index"])
def test_cr_name_is_rejected_before_network_because_server_ack_normalizes_cr(monkeypatch, action):
    forbidden = Mock(side_effect=AssertionError("network was used"))
    monkeypatch.setattr(client.requests, "Session", forbidden)
    instance = client.QueueClient()
    with pytest.raises(client.QueueClientError):
        if action == "replace":
            instance.replace(PlaybackQueue(name="One\rTwo", items=[QueueItem(url=URL)], index=1), play=True)
        elif action == "browse":
            instance.browse("One\rTwo")
        else:
            instance.play_index("One\rTwo", 1)
    forbidden.assert_not_called()


def test_mode_validates_before_network_and_reuses_strict_single_shot_transport(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("network was used"))
    monkeypatch.setattr(client.requests, "Session", forbidden)
    for mode in ("repeat", "NORMAL", 4, True, None, []):
        with pytest.raises(client.QueueClientError, match="Invalid queue input"):
            client.QueueClient().set_mode(mode)
    forbidden.assert_not_called()
    for reply in (FakeResponse(response("PlayQueueWithIndex")),
                  FakeResponse(response("SetQueueLoopMode", {"LoopMode": "4"})),
                  FakeResponse(b"OK"), requests.Timeout(URL)):
        session = FakeSession(reply)
        monkeypatch.setattr(client.requests, "Session", lambda: session)
        with pytest.raises(client.QueueClientError) as error:
            client.QueueClient().set_mode("normal")
        assert str(error.value) == client._MODE_UNCONFIRMED
        assert len(session.calls) == 1 and session.closed
        if isinstance(reply, FakeResponse):
            assert reply.closed
