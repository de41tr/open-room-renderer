"""Shared pure contracts for authored queues and PlayQueue requests.

Validate explicit URLs and the complete escaped SOAP frame without importing
client transport, CLI commands or renderer state. Runtime queues remain owned
by the renderer; these helpers neither resolve URLs nor perform any I/O.
"""
from __future__ import annotations

import posixpath
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from .queue import (MAX_QUEUE_BYTES, MAX_QUEUE_TRACKS, PlaybackQueue, QueueError,
                    QueueItem, serialize_queue_context)
from .queue_policy import NORMAL, REPEAT_ONE, REPEAT_ALL, SHUFFLE_ONCE, SHUFFLE_REPEAT_ALL

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
SERVICE_TYPE = "urn:schemas-wiimu-com:service:PlayQueue:1"
CONTROL_PATH = "/upnp/control/PlayQueue"
MAX_SOAP_BYTES = MAX_QUEUE_BYTES
MODES = {"normal": NORMAL, "repeat-one": REPEAT_ONE, "repeat-all": REPEAT_ALL,
         "shuffle-once": SHUFFLE_ONCE, "shuffle-repeat-all": SHUFFLE_REPEAT_ALL}

_INVALID_INPUT = "Invalid queue input. Check the name, URLs, index, mode and size limits."


class QueueClientError(ValueError):
    """A fixed, private-safe queue validation or client failure.

    The historical name is also re-exported by queue_client so existing callers
    and the pure authored-playlist helpers share one exception identity.
    """


def validate_name(name: str) -> str:
    """Require an XML-safe identity; raw CR cannot round-trip in server ACKs."""
    try:
        if not isinstance(name, str) or name != name.strip() or "\r" in name:
            raise ValueError
        PlaybackQueue(name=name)
    except (QueueError, ValueError, TypeError, UnicodeError):
        raise QueueClientError(_INVALID_INPUT) from None
    return name


def validate_url(value: str) -> str:
    """Validate an explicit HTTP(S) reference without rewriting any bytes."""
    try:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.netloc or not parts.hostname
                or parts.username is not None or parts.password is not None
                or "@" in parts.netloc or parts.port == 0
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
                or "\\" in value or any(char in value for char in "<>")):
            raise ValueError
        path = posixpath.normpath("/" + unquote(parts.path).lstrip("/")).rstrip("/")
        if path == "/proxy/audio":
            raise ValueError
        # Reuse the queue model's XML character validation, including surrogates.
        PlaybackQueue(items=[QueueItem(url=value)], index=1)
    except (QueueError, ValueError, TypeError, UnicodeError):
        raise QueueClientError(_INVALID_INPUT) from None
    return value


def explicit_queue(name: str, urls: list[str], *, start_index: int = 1,
                   mode: str = "normal") -> PlaybackQueue:
    """Build an explicit queue; validate the complete escaped request too."""
    try:
        if not isinstance(urls, list) or not 1 <= len(urls) <= MAX_QUEUE_TRACKS:
            raise ValueError
        if not isinstance(mode, str) or mode not in MODES:
            raise ValueError
        queue = PlaybackQueue(name=validate_name(name), index=start_index, loop=MODES[mode],
                              items=[QueueItem(url=validate_url(url)) for url in urls])
        prepare_replace(queue)
        return queue
    except (QueueError, ValueError, TypeError, UnicodeError):
        raise QueueClientError(_INVALID_INPUT) from None


def request_body(action: str, fields: dict[str, str], *,
                 max_bytes: int = MAX_SOAP_BYTES) -> bytes:
    """Encode one bounded SOAP request, preserving field order and text."""
    envelope = ET.Element(f"{{{SOAP_NS}}}Envelope")
    body = ET.SubElement(envelope, f"{{{SOAP_NS}}}Body")
    node = ET.SubElement(body, f"{{{SERVICE_TYPE}}}{action}")
    for key, value in fields.items():
        ET.SubElement(node, key).text = value
    try:
        encoded = ET.tostring(envelope, encoding="utf-8", xml_declaration=True)
    except (ValueError, TypeError, UnicodeError):
        raise QueueClientError(_INVALID_INPUT) from None
    if len(encoded) > max_bytes:
        raise QueueClientError(_INVALID_INPUT)
    return encoded


def prepare_replace(queue: PlaybackQueue) -> bytes:
    """Validate a queue and its full SOAP frame before any network side effect.

    Playlist authors may supply display fields using PlaybackQueue/QueueItem.
    Resolved URLs are not canonical input and are rejected rather than preferred
    by serialize_queue_context. This helper never contacts the renderer.
    """
    try:
        if not isinstance(queue, PlaybackQueue) or not 1 <= queue.count <= MAX_QUEUE_TRACKS:
            raise ValueError
        queue.validate()
        validate_name(queue.name)
        for item in queue.items:
            validate_url(item.url)
            if item.resolved_url:
                raise ValueError
        return request_body("ReplaceQueue", {"QueueContext": serialize_queue_context(queue)})
    except (QueueError, ValueError, TypeError, UnicodeError):
        raise QueueClientError(_INVALID_INPUT) from None
