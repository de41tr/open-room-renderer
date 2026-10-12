"""Single-shot stable-ID favorite recall with a bounded, private-safe ACK."""
from __future__ import annotations

import argparse
import json
import re
import sys

import requests

from .queue_client import QueueClientError, _endpoint

MAX_ACK_BYTES = 4096
_UNCONFIRMED = "Favorite playback was not confirmed. Check status before retrying; no retry was sent."


def favorite_play_id(value: str) -> str:
    """Playback also accepts configured IDs; edit validators remain separate."""
    if not isinstance(value, str) or re.fullmatch(r"(?:[0-9a-f]{32}|configured:[1-9][0-9]{0,5})", value) is None:
        raise argparse.ArgumentTypeError("expected a managed favorite ID or configured:N (positive slot, at most six digits)")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def play_favorite(host: str, port: int, identifier: str) -> int:
    """Send one bodyless POST. An acknowledgement is not ongoing audio proof."""
    try:
        identifier = favorite_play_id(identifier)
        endpoint = _endpoint(host, port, path=f"/orr/v1/favorites/{identifier}/play")
    except (argparse.ArgumentTypeError, QueueClientError):
        print("Invalid favorite ID or renderer host/port.", file=sys.stderr)
        return 1
    try:
        # No environment proxy/netrc credentials, redirects, or request retry.
        with requests.Session() as session:
            session.trust_env = False
            with session.post(endpoint, timeout=10, allow_redirects=False, stream=True,
                              headers={"Accept-Encoding": "identity"}) as response:
                if response.status_code != 200:
                    print(f"Favorite playback was not confirmed: HTTP {response.status_code}. "
                          "Check status before retrying; no retry was sent.", file=sys.stderr)
                    return 1
                if (response.headers.get("Content-Encoding", "identity").lower() != "identity"
                        or response.headers.get("Content-Type", "").split(";", 1)[0].lower() != "application/json"):
                    raise ValueError
                length = response.headers.get("Content-Length")
                if length is not None and (not re.fullmatch(r"[0-9]{1,10}", length)
                                           or int(length) > MAX_ACK_BYTES):
                    raise ValueError
                encoded = bytearray()
                for chunk in response.iter_content(chunk_size=MAX_ACK_BYTES + 1):
                    if len(encoded) + len(chunk) > MAX_ACK_BYTES:
                        raise ValueError
                    encoded.extend(chunk)
                result = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_object)
                if (not isinstance(result, dict) or set(result) != {"request_acknowledged", "favorite_id"}
                        or result["request_acknowledged"] is not True or result["favorite_id"] != identifier):
                    raise ValueError
    except requests.Timeout:
        print("Favorite playback was not confirmed: request timed out. "
              "Check status before retrying; no retry was sent.", file=sys.stderr)
        return 1
    except (requests.RequestException, ValueError, UnicodeError, RecursionError):
        print(_UNCONFIRMED, file=sys.stderr)
        return 1
    print(json.dumps({"request_acknowledged": True, "favorite_id": identifier}))
    return 0
