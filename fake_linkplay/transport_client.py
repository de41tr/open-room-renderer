"""Explicit local HTTP controls with bounded, single-shot acknowledgements.

These commands use HTTP navigation, including the renderer's shuffle history.
A successful response acknowledges a request; it does not observe playback.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

import requests

from .queue_client import QueueClientError, _endpoint

COMMANDS = ("next", "previous", "mute", "seek")
MAX_RESPONSE_BYTES = 2  # The complete successful response is exactly b"OK".
_INVALID_INPUT = "Invalid transport input. Use next, previous, mute on|off, or seek SECONDS."
_UNCONFIRMED = ("Transport request was not confirmed. "
                "Inspect status before retrying; the command may already have taken effect.")


class TransportClientError(ValueError):
    """A fixed, private-safe failure without renderer text or request details."""


def seek_seconds(value: str) -> int:
    """Accept an absolute nonnegative ASCII integer, without rounding."""
    try:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
            raise ValueError
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected nonnegative integer seconds") from None


def prepare_command(command: str, value: str | int | None = None) -> str:
    """Validate and map one CLI control before creating a network session."""
    if not isinstance(command, str):
        raise TransportClientError(_INVALID_INPUT)
    if command in {"next", "previous"} and value is None:
        return "setPlayerCmd:" + ("prev" if command == "previous" else "next")
    if command == "mute" and isinstance(value, str) and value in {"on", "off"}:
        return "setPlayerCmd:mute:" + ("1" if value == "on" else "0")
    if command == "seek" and type(value) is int and value >= 0:
        try:
            return "setPlayerCmd:seek:" + str(value)
        except ValueError:
            pass
    raise TransportClientError(_INVALID_INPUT)


class TransportClient:
    """No redirects, environment credentials/proxies, or mutation retries."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        try:
            self.endpoint = _endpoint(host, port, path="/httpapi.asp")
        except QueueClientError:
            raise TransportClientError("Invalid renderer host or port.") from None

    def command(self, command: str, value: str | int | None = None) -> None:
        wire_command = prepare_command(command, value)
        try:
            with requests.Session() as session:
                session.trust_env = False
                # Default adapters have zero retries. Ten seconds is a
                # connect/read inactivity timeout, not a total deadline.
                with session.get(self.endpoint, params={"command": wire_command},
                                 headers={"Accept-Encoding": "identity"}, timeout=10,
                                 allow_redirects=False, stream=True) as response:
                    if (response.status_code != 200 or
                            response.headers.get("Content-Encoding", "identity").lower() != "identity"):
                        raise TransportClientError(_UNCONFIRMED)
                    length = response.headers.get("Content-Length")
                    if length is not None and (not re.fullmatch(r"[0-9]{1,10}", length)
                                               or int(length) > MAX_RESPONSE_BYTES):
                        raise TransportClientError(_UNCONFIRMED)
                    encoded = bytearray()
                    for chunk in response.iter_content(chunk_size=MAX_RESPONSE_BYTES + 1):
                        if len(encoded) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise TransportClientError(_UNCONFIRMED)
                        encoded.extend(chunk)
                    if encoded != b"OK":
                        raise TransportClientError(_UNCONFIRMED)
        except requests.RequestException:
            raise TransportClientError(_UNCONFIRMED) from None


def add_transport_commands(subparsers) -> None:
    descriptions = {
        "next": "navigate forward using the renderer's HTTP queue/shuffle policy",
        "previous": "navigate backward using HTTP queue/shuffle history",
        "mute": "explicitly enable or disable mute",
        "seek": "seek to an absolute nonnegative integer position in seconds",
    }
    for command in COMMANDS:
        parser = subparsers.add_parser(command, help=descriptions[command],
                                       description=descriptions[command])
        if command == "mute":
            parser.add_argument("value", choices=("on", "off"))
        elif command == "seek":
            parser.add_argument("value", type=seek_seconds, metavar="SECONDS")
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=8080)


def run_transport_command(args: argparse.Namespace) -> int:
    value = getattr(args, "value", None)
    try:
        TransportClient(args.host, args.port).command(args.cmd, value)
    except TransportClientError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    result = {"request_acknowledged": True, "command": args.cmd}
    if value is not None:
        result["seconds" if args.cmd == "seek" else "value"] = value
    print(json.dumps(result))
    return 0
