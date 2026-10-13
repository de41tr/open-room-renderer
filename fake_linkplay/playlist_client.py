"""Single-shot, bounded client for the owned current-queue playlist endpoint."""
from __future__ import annotations

import re

import requests

from .playlist_files import MAX_PLAYLIST_BYTES, decode_playlist
from .queue_client import QueueClientError, _endpoint
from .queue_protocol import explicit_queue, prepare_replace

PATH = "/orr/v1/queue/playlist"
_FAILED = "Playlist not saved: could not confirm a valid renderer response. No retry was sent."
_UNAVAILABLE = "Playlist not saved: current queue cannot be represented as a URL playlist."
_UNSUPPORTED = "Playlist not saved: renderer does not support current-queue playlists."


def fetch_current_playlist(host: str, port: int) -> dict[str, object]:
    """Fetch one exact v1 document, without redirects, credentials or fallback.

    The caller may write only after the whole JSON and escaped restore request
    have passed validation. HTTP failures and remote text are never displayed.
    """
    endpoint = _endpoint(host, port, path=PATH)
    try:
        with requests.Session() as session:
            # allow_redirects=False still prepares Response.next and reads its body.
            # Skip that work so status/size checks remain before any body read.
            session.resolve_redirects = lambda *_args, **_kwargs: iter(())
            # Default adapters have zero retries. Never inherit proxy/netrc
            # credentials or redirect the request to another destination.
            session.trust_env = False
            with session.get(endpoint, timeout=10, allow_redirects=False, stream=True,
                    headers={"Accept": "application/json", "Accept-Encoding": "identity",
                             "Cache-Control": "no-store"}) as response:
                if response.status_code == 404:
                    raise QueueClientError(_UNSUPPORTED)
                if response.status_code == 409:
                    raise QueueClientError(_UNAVAILABLE)
                if response.status_code != 200:
                    raise ValueError
                if (response.headers.get("Content-Encoding", "identity").lower() != "identity"
                        or response.headers.get("Content-Type", "").split(";", 1)[0].lower() != "application/json"):
                    raise ValueError
                length = response.headers.get("Content-Length")
                if length is not None and (not re.fullmatch(r"[0-9]{1,10}", length)
                                           or int(length) > MAX_PLAYLIST_BYTES):
                    raise ValueError
                encoded = bytearray()
                for chunk in response.iter_content(chunk_size=16384):
                    if len(encoded) + len(chunk) > MAX_PLAYLIST_BYTES:
                        raise ValueError
                    encoded.extend(chunk)
                payload = decode_playlist(bytes(encoded))
                prepare_replace(explicit_queue(payload["name"], payload["urls"],
                    start_index=payload["index"], mode=payload["mode"]))
                return payload
    except QueueClientError:
        raise
    except (requests.RequestException, ValueError, TypeError, UnicodeError, RecursionError):
        raise QueueClientError(_FAILED) from None
