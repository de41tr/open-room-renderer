"""Real CLI processes against adversarial synthetic loopback SOAP peers."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Thread
from xml.etree import ElementTree as ET

import pytest

from fake_linkplay import queue_client as client
from fake_linkplay.queue import PlaybackQueue, QueueItem, parse_queue_context, serialize_queue_context
from fake_linkplay.upnp_description import soap_response

ROOT = Path(__file__).resolve().parents[1]
NAME = "Queue & café 夜"
URLS = ["https://media.example.test/a%2Fb?token=synthetic-secret&part=1+2#one",
        "https://media.example.test/音声.wav?token=synthetic-other&part=%2f"]


@dataclass
class Reply:
    body: bytes
    status: int = 200
    location: str | None = None
    disconnect: bool = False
    length: int | None = None
    encoding: str | None = None


def ack(action="ReplaceQueue", value=NAME):
    fields = f"<QueueName>{escape(value)}</QueueName>" if action == "ReplaceQueue" else ""
    return Reply(soap_response(action, "PlayQueue", fields).encode())


def browse(queue):
    return Reply(soap_response("BrowseQueue", "PlayQueue",
        "<QueueContext>" + escape(serialize_queue_context(queue)) + "</QueueContext>").encode())


@contextmanager
def peer(replies):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, dict(self.headers), self.rfile.read(int(self.headers["Content-Length"]))))
            reply = replies[min(len(seen) - 1, len(replies) - 1)]
            if reply.disconnect:
                self.close_connection = True
                return
            self.send_response(reply.status)
            self.send_header("Content-Type", "text/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(reply.body) if reply.length is None else reply.length))
            if reply.location:
                self.send_header("Location", reply.location)
            if reply.encoding:
                self.send_header("Content-Encoding", reply.encoding)
            self.end_headers()
            try:
                self.wfile.write(reply.body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

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


def invoke(port, *args):
    return subprocess.run([sys.executable, "-m", "fake_linkplay.cli", "queue", *args,
                           "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1"},
        capture_output=True, text=True, encoding="utf-8", timeout=15)


def decode_request(request, action):
    path, headers, body = request
    assert path == client.CONTROL_PATH
    assert headers["SOAPAction"] == f'"{client.SERVICE_TYPE}#{action}"'
    assert headers["Content-Type"] == "text/xml; charset=utf-8"
    assert headers["Accept-Encoding"] == "identity"
    root = ET.fromstring(body)
    assert root.tag == f"{{{client.SOAP_NS}}}Envelope"
    assert len(root) == 1 and root[0].tag == f"{{{client.SOAP_NS}}}Body"
    assert len(root[0]) == 1 and root[0][0].tag == f"{{{client.SERVICE_TYPE}}}{action}"
    return {node.tag: node.text or "" for node in root[0][0]}


@pytest.mark.parametrize("mode,wire_mode", client.MODES.items())
def test_load_sends_one_replace_stopped_by_default(mode, wire_mode):
    with peer([ack()]) as (port, seen):
        result = invoke(port, "load", "--name", NAME, "--start-index", "2", "--mode", mode, *URLS)
    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout) == {"queue_replaced": True, "name": NAME, "index": 2,
        "count": 2, "mode": mode, "playback_request_acknowledged": False}
    assert len(seen) == 1
    fields = decode_request(seen[0], "ReplaceQueue")
    assert set(fields) == {"QueueContext"}
    queue = parse_queue_context(fields["QueueContext"])
    assert queue.name == NAME and queue.index == 2 and queue.loop == wire_mode
    assert [item.url for item in queue.items] == URLS
    assert all(not item.resolved_url for item in queue.items)
    assert not any(url in result.stdout + result.stderr for url in URLS)


def test_load_play_sends_play_only_after_exact_replace_acknowledgement():
    with peer([ack(), ack("PlayQueueWithIndex")]) as (port, seen):
        result = invoke(port, "load", "--name", NAME, "--start-index", "2", "--play", *URLS)
    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout)["playback_request_acknowledged"] is True
    assert len(seen) == 2
    decode_request(seen[0], "ReplaceQueue")
    assert decode_request(seen[1], "PlayQueueWithIndex") == {"QueueName": NAME, "Index": "2"}


def test_play_index_is_one_direct_action_without_browse_or_rewrite():
    with peer([ack("PlayQueueWithIndex")]) as (port, seen):
        result = invoke(port, "play-index", "2", "--name", NAME)
    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout) == {"playback_request_acknowledged": True, "name": NAME, "index": 2}
    assert len(seen) == 1
    assert decode_request(seen[0], "PlayQueueWithIndex") == {"QueueName": NAME, "Index": "2"}


@pytest.mark.parametrize("include_urls", [False, True])
@pytest.mark.parametrize("explicit_name", [False, True])
def test_show_single_browse_preserves_order_index_mode_with_private_default(include_urls, explicit_name):
    queue = PlaybackQueue(name=NAME, index=2, loop=3, source=URLS[0], artwork=URLS[0], items=[
        QueueItem(url=URLS[0], title=URLS[0], artist=URLS[0], album=URLS[0], metadata=URLS[0], id=URLS[0]),
        QueueItem(url=URLS[1], resolved_url="https://resolved.example.test/temp?token=temporary-secret")])
    arguments = ["show", *( ["--include-urls"] if include_urls else []),
                 *(["--name", NAME] if explicit_name else [])]
    with peer([browse(queue)]) as (port, seen):
        result = invoke(port, *arguments)
    assert result.returncode == 0 and result.stderr == ""
    assert len(seen) == 1
    assert decode_request(seen[0], "BrowseQueue") == {"QueueName": NAME if explicit_name else ""}
    data = json.loads(result.stdout)
    assert (data["name"], data["index"], data["count"], data["mode"]) == (NAME, 2, 2, "shuffle-once")
    assert [item["index"] for item in data["items"]] == [1, 2]
    assert not any(key in data for key in ["source", "artwork", "metadata"])
    if include_urls:
        assert data["items"][0]["renderer_reported_url"] == URLS[0]
        assert data["items"][1]["renderer_reported_url"] == queue.items[1].resolved_url
        assert "potentially resolved" in data["url_notice"]
        assert "not a canonical" in data["url_notice"]
    else:
        assert data["items"] == [{"index": 1}, {"index": 2}]
        assert "https://" not in result.stdout and "secret" not in result.stdout


BAD_REPLIES = [
    Reply(URLS[0].encode(), status=500), Reply(b"OK"), Reply(b"<html>OK</html>"),
    Reply(b"", disconnect=True), Reply(b"\xff"), Reply(b"truncated", length=50),
    Reply(b"", length=client.MAX_SOAP_BYTES + 1), Reply(b"x" * (client.MAX_SOAP_BYTES + 1)),
    Reply(b"empty", encoding="gzip"), ack(value="Wrong name"), ack("PlayQueueWithIndex"),
    Reply(ack().body.replace(b"</QueueName>", b"</QueueName><QueueName>secret</QueueName>")),
    Reply(ack().body.replace(b"<QueueName>", b"<QueueName><nested>" ).replace(b"</QueueName>", b"</nested></QueueName>")),
    Reply(ack().body.replace(client.SERVICE_TYPE.encode(), b"urn:wrong")),
    Reply(ack().body.replace(b"<s:Body>", b"<s:Body/><s:Body>")),
]


@pytest.mark.parametrize("reply", BAD_REPLIES)
def test_failed_or_uncertain_replace_never_plays_and_never_prints_body(reply):
    with peer([reply, ack("PlayQueueWithIndex")]) as (port, seen):
        result = invoke(port, "load", "--name", NAME, "--play", *URLS)
    assert len(seen) == 1
    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr == client._REPLACE_UNCONFIRMED + "\n"


@pytest.mark.parametrize("reply", [Reply(URLS[0].encode(), status=500), Reply(b"OK"),
    Reply(b"", disconnect=True), ack(), Reply(b"", length=client.MAX_SOAP_BYTES + 1)])
def test_play_failure_after_successful_replace_reports_committed_queue_without_rollback(reply):
    with peer([ack(), reply]) as (port, seen):
        result = invoke(port, "load", "--name", NAME, "--play", *URLS)
    assert len(seen) == 2
    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr == client._REPLACED_PLAY_UNCONFIRMED + "\n"


@pytest.mark.parametrize("status", [300, 301, 302, 303, 307, 308])
def test_redirects_are_not_followed_and_mutations_are_not_retried(status):
    with peer([ack()]) as (target_port, target_seen):
        reply = Reply(URLS[0].encode(), status=status,
                      location=f"http://127.0.0.1:{target_port}/secret-target")
        with peer([reply]) as (port, seen):
            result = invoke(port, "load", "--name", NAME, "--play", *URLS)
    assert len(seen) == 1 and not target_seen
    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr == client._REPLACE_UNCONFIRMED + "\n"


@pytest.mark.parametrize("args", [
    ["load", "--name", NAME, "--start-index", "0", *URLS],
    ["load", "--name", NAME, "--start-index", "3", *URLS],
    ["load", "--name", NAME, "--mode", "secret-unsupported", *URLS],
    ["load", "--name", NAME, URLS[0] + " "],
    ["load", "--name", NAME, "https://user:secret@example.test/a"],
    ["load", "--name", NAME, "https://example.test/proxy/audio?secret=1"],
    ["play-index", "0", "--name", NAME], ["play-index", "1001", "--name", NAME],
    ["play-index", "2"], ["show", "--name", ""],
])
def test_invalid_cli_input_has_no_network_and_empty_stdout(args):
    with peer([ack()]) as (port, seen):
        result = invoke(port, *args)
    assert seen == []
    assert result.returncode in {1, 2} and result.stdout == ""
    assert "secret" not in result.stderr and "https://" not in result.stderr


def test_connection_refusal_has_no_traceback_and_empty_stdout():
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        result = invoke(reserved.getsockname()[1], "load", "--name", NAME, "--play", *URLS)
    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr == client._REPLACE_UNCONFIRMED + "\n"


@pytest.mark.parametrize("context", [
    "", "<PlayList><Count>2</Count><Tracks/></PlayList>",
    '<!DOCTYPE PlayList [<!ENTITY x "secret">]><PlayList/>',
    '<PlayList><Tracks><Track1><URL>https://secret</URL></Track1></Tracks><Index>0</Index></PlayList>',
])
def test_invalid_browse_context_is_not_success_or_echoed(context):
    reply = Reply(soap_response("BrowseQueue", "PlayQueue",
        "<QueueContext>" + escape(context) + "</QueueContext>").encode())
    with peer([reply]) as (port, seen):
        result = invoke(port, "show", "--include-urls")
    assert len(seen) == 1 and result.returncode == 1 and result.stdout == ""
    assert result.stderr == client._INVALID_RESPONSE + "\n"


@pytest.mark.parametrize("body", [b"", b" ", soap_response("BrowseQueue", "PlayQueue", "").encode()])
def test_browse_missing_body_or_required_field_is_failure(body):
    with peer([Reply(body)]) as (port, seen):
        result = invoke(port, "show")
    assert len(seen) == 1 and result.returncode == 1 and result.stdout == ""
    assert result.stderr == client._INVALID_RESPONSE + "\n"


def test_empty_queue_browse_preserves_zero_index_and_count():
    with peer([browse(PlaybackQueue(name=NAME))]) as (port, seen):
        result = invoke(port, "show")
    assert len(seen) == 1 and result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout) == {"name": NAME, "count": 0, "index": 0, "mode": "normal", "items": []}
