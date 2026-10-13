"""Single-shot managed favorite target edits with bounded, private-safe ACKs."""
from __future__ import annotations

import json
import re
import sys

import requests

from .favorites import MAX_REQUEST_BYTES, _decode, _record, validate_target_edit
from .queue_client import QueueClientError, _endpoint

MAX_ACK_BYTES = 128 * 1024
MAX_ERROR_BYTES = 4096
_UNCONFIRMED = ("Favorite target update was not confirmed. "
                "Read favorites list before retrying; no retry was sent.")
_COMMITTED = ("Favorites were replaced, but crash durability is unconfirmed. "
              "Read favorites list before retrying; no retry was sent.")
_RECORD_FIELDS = {"id", "name", "url", "title", "artist", "album", "artwork"}


def _response_json(response: requests.Response, limit: int) -> object:
    """Read identity-encoded UTF-8 JSON only, bounded even without a length."""
    if (response.headers.get("Content-Encoding", "identity").lower() != "identity"
            or re.fullmatch(r'application/json(?:\s*;\s*charset\s*=\s*(?:utf-8|"utf-8"))?',
                            response.headers.get("Content-Type", ""), re.I) is None):
        raise ValueError
    length = response.headers.get("Content-Length")
    if length is not None and (not re.fullmatch(r"[0-9]{1,10}", length) or int(length) > limit):
        raise ValueError
    encoded = bytearray()
    for chunk in response.iter_content(chunk_size=limit + 1):
        if len(encoded) + len(chunk) > limit:
            raise ValueError
        encoded.extend(chunk)
    if length is not None and len(encoded) != int(length):
        raise ValueError
    return _decode(bytes(encoded))


def _check_ack(payload: object, identifier: str, url: str, metadata: str) -> None:
    if not isinstance(payload, dict) or set(payload) != {"favorite"}:
        raise ValueError
    favorite = payload["favorite"]
    if (not isinstance(favorite, dict) or set(favorite) != _RECORD_FIELDS | {"slot", "editable"}
            or favorite["id"] != identifier or favorite["url"] != url
            or favorite["editable"] is not True
            or type(favorite["slot"]) is not int or favorite["slot"] < 1):
        raise ValueError
    # Validate every display field using the server's persisted-record limits;
    # compare the URL above verbatim, without normalizing or resolving it.
    _record({key: favorite[key] for key in _RECORD_FIELDS}, persisted=True)
    if metadata == "clear" and any(favorite[key] != "" for key in ("title", "artist", "album", "artwork")):
        raise ValueError


def set_favorite_url(host: str, port: int, identifier: str, url: str, metadata: str) -> int:
    """Validate before a single PUT; uncertain writes never retry or roll back."""
    try:
        if not isinstance(identifier, str) or re.fullmatch(r"[0-9a-f]{32}", identifier) is None:
            raise ValueError
        url, metadata = validate_target_edit(url, metadata)
        endpoint = _endpoint(host, port, path=f"/orr/v1/favorites/{identifier}/target")
        body = json.dumps({"url": url, "metadata": metadata}, ensure_ascii=False,
                          allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError
    except (ValueError, QueueClientError, TypeError, UnicodeError):
        print("Invalid favorite target, metadata policy, ID or renderer host/port.", file=sys.stderr)
        return 1
    try:
        # Requests' default adapters do not retry. No environment proxy/netrc
        # credentials, redirects, preliminary GET, or legacy-client fallback.
        with requests.Session() as session:
            # allow_redirects=False still prepares Response.next and reads its body.
            # Skip that work so status/size checks remain before any body read.
            session.resolve_redirects = lambda *_args, **_kwargs: iter(())
            session.trust_env = False
            with session.put(endpoint, data=body, timeout=10, allow_redirects=False, stream=True,
                             headers={"Accept-Encoding": "identity", "Content-Type": "application/json"}) as response:
                if response.status_code == 503:
                    error = _response_json(response, MAX_ERROR_BYTES)
                    if (isinstance(error, dict) and set(error) == {"error", "committed", "message"}
                            and error["error"] == "durability_unconfirmed" and error["committed"] is True
                            and isinstance(error["message"], str)):
                        print(_COMMITTED, file=sys.stderr)
                        return 1
                    raise ValueError
                if response.status_code != 200:
                    print(f"Favorite target update was not confirmed: HTTP {response.status_code}. "
                          "Read favorites list before retrying; no retry was sent.", file=sys.stderr)
                    return 1
                _check_ack(_response_json(response, MAX_ACK_BYTES), identifier, url, metadata)
    except requests.Timeout:
        print("Favorite target update was not confirmed: request timed out. "
              "Read favorites list before retrying; no retry was sent.", file=sys.stderr)
        return 1
    except (requests.RequestException, ValueError, UnicodeError, RecursionError):
        print(_UNCONFIRMED, file=sys.stderr)
        return 1
    print(json.dumps({"request_acknowledged": True, "favorite_id": identifier, "metadata": metadata}))
    return 0
