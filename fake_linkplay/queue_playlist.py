"""Read-only projection of one canonical queue into a restorable URL playlist.

A playlist is deliberately not a backup of runtime playback state or metadata.
No resolver, backend, favorite store or server filesystem is consulted here.
"""
from __future__ import annotations

from collections.abc import Callable

from aiohttp import web

from .canonical_target import canonical_target_url
from .playlist_files import encode_playlist, validate_playlist
from .queue import PlaybackQueue
from .queue_client import MODES, explicit_queue, prepare_replace

PATH = "/orr/v1/queue/playlist"
_MODE_NAMES = {value: name for name, value in MODES.items()}
_UNAVAILABLE = {"error": "playlist_unavailable",
                "message": "Current queue cannot be saved as a URL playlist"}
_HEADERS = {"Cache-Control": "no-store"}


def encode_current_playlist(queue: PlaybackQueue) -> bytes:
    """Synchronously detach and validate the entire captured queue, or fail.

    Queue ownership/mutation is on the renderer's event loop. This function has
    no await and returns immutable bytes before the handler yields control.
    Later queue changes do not invalidate that point-in-time snapshot.
    """
    if not isinstance(queue, PlaybackQueue):
        raise ValueError("Invalid queue structure")
    queue.validate()
    urls = []
    for item in queue.items:
        # Without locally proven radio identity a restored plain URL loses its
        # station/live-recovery semantics. Saving cannot silently promise that.
        if item._radio_station is None and item.stream_type == "station":
            raise ValueError("Station has no canonical public reference")
        urls.append(canonical_target_url(item))
    payload = validate_playlist({"version": 1, "name": queue.name, "index": queue.index,
                                 "mode": _MODE_NAMES[queue.loop], "urls": urls})
    encoded = encode_playlist(payload)
    # Validate the same explicit URLs and full escaped SOAP frame as restore.
    # Neither validation step performs I/O or touches the captured queue.
    prepare_replace(explicit_queue(payload["name"], payload["urls"],
                                   start_index=payload["index"], mode=payload["mode"]))
    return encoded


class QueuePlaylistApi:
    """A bodyless GET on the renderer's existing trusted-LAN listeners."""

    def __init__(self, current_queue: Callable[[], PlaybackQueue]) -> None:
        self.current_queue = current_queue

    def routes(self) -> list[web.RouteDef]:
        return [web.get(PATH, self.handle, allow_head=False)]

    async def handle(self, request: web.Request) -> web.Response:
        if (request.query_string or request.content_length not in {None, 0}
                or request.headers.get("Transfer-Encoding")):
            return web.json_response({"error": "unexpected_input",
                "message": "Queue playlist requires a bodyless GET without query arguments"},
                status=400, headers=_HEADERS)
        try:
            # Capture the queue exactly once. All projection/encoding happens
            # synchronously, without sync_mpv_state or any other await.
            encoded = encode_current_playlist(self.current_queue())
        except (ValueError, TypeError, UnicodeError):
            return web.json_response(_UNAVAILABLE, status=409, headers=_HEADERS)
        return web.Response(body=encoded, content_type="application/json", headers=_HEADERS)
