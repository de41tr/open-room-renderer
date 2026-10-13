"""Small, bounded PlayQueue client for explicit local queue workflows.

BrowseQueue reports canonical order/index/mode, but may expose resolved media
URLs. It is not a lossless canonical export. Mutations are never retried, and a
queue name is not a revision token: same-name concurrent replacement has no
compare-and-swap protection.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
from xml.etree import ElementTree as ET

import requests

from .queue import (MAX_QUEUE_BYTES, MAX_QUEUE_TRACKS, MAX_XML_DEPTH, PlaybackQueue,
                    QueueError, QueueItem, parse_queue_context, serialize_queue_context)
from .queue_policy import NORMAL, REPEAT_ONE, REPEAT_ALL, SHUFFLE_ONCE, SHUFFLE_REPEAT_ALL

# Compatibility exports: pure queue users should import queue_protocol directly.
from .queue_protocol import (CONTROL_PATH, MAX_SOAP_BYTES, MODES, SERVICE_TYPE, SOAP_NS,
                             QueueClientError, _INVALID_INPUT, explicit_queue,
                             prepare_replace, request_body as _protocol_request_body,
                             validate_name, validate_url)

_INVALID_RESPONSE = "Queue request failed: invalid renderer response."
_REQUEST_FAILED = "Queue request failed: could not confirm the renderer response."
_REPLACE_UNCONFIRMED = ("Queue replacement was not confirmed. "
                        "Inspect queue show and status before retrying.")
_MODE_UNCONFIRMED = ("Queue mode change was not confirmed. "
                     "Inspect queue show and status before retrying.")
_PLAY_UNCONFIRMED = ("Playback was not confirmed. "
                     "Inspect queue show and status before retrying.")
_REPLACED_PLAY_UNCONFIRMED = ("Queue replaced; playback was not confirmed. "
                              "Inspect queue show and status before retrying.")


def _request_body(action: str, fields: dict[str, str]) -> bytes:
    # Keep the existing client-local size boundary while sharing the encoder.
    return _protocol_request_body(action, fields, max_bytes=MAX_SOAP_BYTES)


def _prepare_play(name: str, index: int) -> bytes:
    validate_name(name)
    if type(index) is not int or not 1 <= index <= MAX_QUEUE_TRACKS:
        raise QueueClientError(_INVALID_INPUT)
    return _request_body("PlayQueueWithIndex", {"QueueName": name, "Index": str(index)})


def _container(node: ET.Element) -> None:
    if (node.text or "").strip() or any((child.tail or "").strip() for child in node):
        raise ValueError


def parse_response(encoded: bytes, action: str) -> dict[str, str]:
    """Require exact SOAP 1.1 framing and unique scalar response fields."""
    try:
        required = {"BrowseQueue": {"QueueContext"}, "ReplaceQueue": {"QueueName"},
                    "PlayQueueWithIndex": set(), "SetQueueLoopMode": set()}[action]
        if not isinstance(encoded, bytes) or len(encoded) > MAX_SOAP_BYTES:
            raise ValueError
        text = encoded.decode("utf-8-sig")
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
            raise ValueError
        declaration = re.match(r"<\?xml\b[^?]*\?>", text)
        if declaration:
            encoding = re.search(r"\bencoding\s*=\s*(['\"])(.*?)\1", declaration[0])
            if encoding and encoding[2].lower() not in {"utf-8", "utf8"}:
                raise ValueError
        root = ET.fromstring(text)
        pending = [(root, 1)]
        while pending:
            node, depth = pending.pop()
            if depth > MAX_XML_DEPTH:
                raise ValueError
            pending.extend((child, depth + 1) for child in node)
        if root.tag != f"{{{SOAP_NS}}}Envelope" or len(root) != 1:
            raise ValueError
        _container(root)
        body = root[0]
        if body.tag != f"{{{SOAP_NS}}}Body" or len(body) != 1:
            raise ValueError
        _container(body)
        node = body[0]
        if node.tag != f"{{{SERVICE_TYPE}}}{action}Response":
            raise ValueError
        _container(node)
        fields = {}
        for field in node:
            if field.tag not in required or field.tag in fields or len(field) or field.attrib:
                raise ValueError
            fields[field.tag] = field.text or ""
        if set(fields) != required:
            raise ValueError
        return fields
    except (ValueError, TypeError, KeyError, UnicodeError, ET.ParseError):
        raise QueueClientError(_INVALID_RESPONSE) from None


def _endpoint(host: str, port: int, *, path: str = CONTROL_PATH) -> str:
    try:
        if not isinstance(host, str) or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if ":" in host:
            ipaddress.IPv6Address(host)
            if "%" in host:
                raise ValueError
            host = f"[{host}]"
        elif not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
            raise ValueError
        return f"http://{host}:{port}{path}"
    except (ValueError, TypeError):
        raise QueueClientError("Invalid renderer host or port.") from None


class QueueClient:
    """Single-shot requests, bounded responses, and no implicit playback retry."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        self.endpoint = _endpoint(host, port)

    def _send(self, action: str, body: bytes) -> dict[str, str]:
        try:
            # Avoid environment proxy/netrc credentials and implicit redirects.
            # requests' default adapters perform zero retries.
            with requests.Session() as session:
                # allow_redirects=False still prepares Response.next and reads its body.
                # Skip that work so status/size checks remain before any body read.
                session.resolve_redirects = lambda *_args, **_kwargs: iter(())
                session.trust_env = False
                # Ten seconds per connect/read inactivity, not a total deadline.
                with session.post(self.endpoint, data=body, headers={
                    "SOAPAction": f'"{SERVICE_TYPE}#{action}"',
                    "Content-Type": "text/xml; charset=utf-8", "Accept-Encoding": "identity",
                }, timeout=10, allow_redirects=False, stream=True) as response:
                    if response.status_code != 200:
                        raise QueueClientError(_REQUEST_FAILED)
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise QueueClientError(_INVALID_RESPONSE)
                    length = response.headers.get("Content-Length")
                    if length is not None and (not re.fullmatch(r"[0-9]{1,10}", length)
                                               or int(length) > MAX_SOAP_BYTES):
                        raise QueueClientError(_INVALID_RESPONSE)
                    encoded = bytearray()
                    for chunk in response.iter_content(chunk_size=16384):
                        if len(encoded) + len(chunk) > MAX_SOAP_BYTES:
                            raise QueueClientError(_INVALID_RESPONSE)
                        encoded.extend(chunk)
                    return parse_response(bytes(encoded), action)
        except requests.RequestException:
            raise QueueClientError(_REQUEST_FAILED) from None

    def browse(self, name: str | None = None) -> PlaybackQueue:
        if name is not None:
            validate_name(name)
        fields = self._send("BrowseQueue", _request_body("BrowseQueue", {"QueueName": name or ""}))
        try:
            queue = parse_queue_context(fields["QueueContext"])
            if name is not None and queue.name != name:
                raise ValueError
            return queue
        except (QueueError, ValueError, TypeError, UnicodeError):
            raise QueueClientError(_INVALID_RESPONSE) from None

    def replace(self, queue: PlaybackQueue, *, play: bool = False) -> None:
        # Validate both requests before ReplaceQueue. Any uncertain replacement
        # must stop here; no compensating mutation can safely restore old state.
        body = prepare_replace(queue)
        name, index = queue.name, queue.index
        play_body = _prepare_play(name, index) if play else None
        try:
            result = self._send("ReplaceQueue", body)
            if result["QueueName"] != name:
                raise QueueClientError(_INVALID_RESPONSE)
        except QueueClientError:
            raise QueueClientError(_REPLACE_UNCONFIRMED) from None
        if play_body is not None:
            try:
                self._send("PlayQueueWithIndex", play_body)
            except QueueClientError:
                raise QueueClientError(_REPLACED_PLAY_UNCONFIRMED) from None

    def play_index(self, name: str, index: int) -> None:
        body = _prepare_play(name, index)
        try:
            self._send("PlayQueueWithIndex", body)
        except QueueClientError:
            raise QueueClientError(_PLAY_UNCONFIRMED) from None

    def set_mode(self, mode: str) -> None:
        """Change only the current queue's mode; SCPD has no QueueName input."""
        if not isinstance(mode, str) or mode not in MODES:
            raise QueueClientError(_INVALID_INPUT)
        body = _request_body("SetQueueLoopMode", {"LoopMode": str(MODES[mode])})
        try:
            self._send("SetQueueLoopMode", body)
        except QueueClientError:
            raise QueueClientError(_MODE_UNCONFIRMED) from None


def queue_summary(queue: PlaybackQueue, *, include_urls: bool = False) -> dict[str, object]:
    """Project structural queue state; omit all provider/display metadata."""
    items = [{"index": index} for index in range(1, queue.count + 1)]
    result = {"name": queue.name, "count": queue.count, "index": queue.index,
              "mode": next(name for name, value in MODES.items() if value == queue.loop),
              "items": items}
    if include_urls:
        result["url_notice"] = ("Renderer-reported URLs, potentially resolved or credential-bearing; "
                                "not a canonical live-queue export.")
        for item, source in zip(items, queue.items):
            item["renderer_reported_url"] = source.url
    return result


class PrivateClientArgumentParser(argparse.ArgumentParser):
    """Do not echo URL-like invalid arguments through argparse diagnostics."""

    def error(self, message: str) -> None:
        self.exit(2, "Invalid client command arguments. Run open-room-renderer --help.\n")


def add_queue_commands(subparsers) -> None:
    queue = subparsers.add_parser("queue", help="show, replace, play or change the current queue mode",
        description="Local queue controls. A queue name does not guard against same-name concurrent replacement.")
    commands = queue.add_subparsers(dest="queue_cmd", required=True)
    for command in ("show", "load", "play-index", "mode"):
        help_text = {"show": "show canonical ordering/index/mode; not a canonical URL export",
                     "load": "replace the entire queue and stop playback; --play explicitly starts it",
                     "play-index": "request playback at a one-based index, without rewriting the queue",
                     "mode": "change the current queue mode without replacing or restarting playback"}[command]
        parser = commands.add_parser(command, help=help_text, description=help_text)
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=8080)
        if command != "mode":
            parser.add_argument("--name", required=command != "show")
        if command == "show":
            parser.add_argument("--include-urls", action="store_true",
                help="include renderer-reported URLs, potentially resolved or credential-bearing")
        elif command == "load":
            parser.add_argument("--start-index", type=int, default=1)
            parser.add_argument("--mode", choices=tuple(MODES), default="normal")
            parser.add_argument("--play", action="store_true")
            parser.add_argument("urls", nargs="+", metavar="URL",
                help="1–1000 explicit HTTP(S) URLs; no usernames/passwords or renderer proxies")
        elif command == "mode":
            parser.add_argument("mode", choices=tuple(MODES))
        else:
            parser.add_argument("index", type=int, metavar="N")


def run_queue_command(args: argparse.Namespace) -> int:
    try:
        client = QueueClient(args.host, args.port)
        if args.queue_cmd == "show":
            result = queue_summary(client.browse(args.name), include_urls=args.include_urls)
        elif args.queue_cmd == "load":
            queue = explicit_queue(args.name, args.urls, start_index=args.start_index, mode=args.mode)
            client.replace(queue, play=args.play)
            result = {"queue_replaced": True, "name": queue.name, "index": queue.index,
                      "count": queue.count, "mode": args.mode,
                      "playback_request_acknowledged": args.play}
        elif args.queue_cmd == "mode":
            client.set_mode(args.mode)
            result = {"mode_request_acknowledged": True, "mode": args.mode}
        else:
            client.play_index(args.name, args.index)
            result = {"playback_request_acknowledged": True, "name": args.name, "index": args.index}
    except QueueClientError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0
