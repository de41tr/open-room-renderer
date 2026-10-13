"""Pure preparation for bounded browser playlist imports and stopped edits.

The renderer remains the only queue owner. These helpers never fetch URLs,
read files, persist a queue, or project a live queue through a lossy export.
All failures are fixed classifications, with no supplied text in their message.
"""
from __future__ import annotations

from copy import copy, deepcopy
import re

from .playlist_files import decode_playlist
from .playlist_m3u import decode_m3u_urls
from .queue import MAX_QUEUE_TRACKS, PlaybackQueue
from .queue_protocol import explicit_queue, prepare_replace

PLAYLIST_ACTIONS = frozenset({"playlist_replace", "playlist_append", "playlist_remove", "playlist_move"})
_SOURCE_FIELDS = {
    "json": {"format", "text"},
    "m3u": {"format", "text", "name", "index", "mode"},
    "urls": {"format", "urls", "name", "index", "mode"},
}
_INVALID_NAME = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff\ufffe\uffff]")


class WebPlaylistError(ValueError):
    def __init__(self) -> None:
        super().__init__("Invalid browser playlist input")


def _name(value: object) -> str:
    if (type(value) is not str or _INVALID_NAME.search(value)
            or len(value.encode("utf-8")) > 256):
        raise WebPlaylistError()
    return value


def prepare_source(source: object) -> PlaybackQueue:
    """Decode the exact tagged source and bound the complete escaped request."""
    try:
        if type(source) is not dict or type(source.get("format")) is not str:
            raise WebPlaylistError()
        kind = source["format"]
        if kind not in _SOURCE_FIELDS or source.keys() != _SOURCE_FIELDS[kind]:
            raise WebPlaylistError()
        if kind in {"json", "m3u"}:
            if type(source["text"]) is not str:
                raise WebPlaylistError()
            encoded = source["text"].encode("utf-8")
        if kind == "json":
            document = decode_playlist(encoded)
        else:
            document = {**source, "urls": decode_m3u_urls(encoded) if kind == "m3u" else source["urls"]}
        return explicit_queue(_name(document["name"]), document["urls"],
                              start_index=document["index"], mode=document["mode"])
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise WebPlaylistError() from None


def prepare_urls(urls: object) -> list[str]:
    """Detach a validated append input without acquiring queue ownership."""
    try:
        queue = explicit_queue("BrowserAppend", urls)
        return [item.url for item in queue.items]
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise WebPlaylistError() from None


def _validate_canonical(queue: PlaybackQueue) -> None:
    # Resolved == explicit URL is an ordinary played queue, not resolver
    # authority. Copy only the fields this validation view changes; all other
    # fields are read-only through prepare_replace. The live queue (including
    # resolver provenance and shuffle history) is never changed or traversed
    # by deepcopy merely to report capability.
    wire_queue = copy(queue)
    wire_queue.items = []
    for item in queue.items:
        if item.resolved_url:
            item = copy(item)
            item.resolved_url = ""
        wire_queue.items.append(item)
    prepare_replace(wire_queue)


def _editable(queue: PlaybackQueue) -> None:
    queue.validate()
    if not queue.items or any((queue.source, queue.search_url, queue.stream_id, queue.stream_type)):
        raise WebPlaylistError()
    for item in queue.items:
        if (item.source or item.id or item.stream_type or item._radio_station is not None
                or item._radio_resolution_attempted or item.resolved_url not in {"", item.url}):
            raise WebPlaylistError()
    _validate_canonical(queue)


def edit_reason(queue: PlaybackQueue) -> str:
    """Return only structural capability; no transport state or private text."""
    try:
        if not queue.items:
            return "empty_queue"
        _editable(queue)
        return "supported"
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        return "unsupported_queue"


def prepare_edit(queue: PlaybackQueue, action: str, *, urls: list[str] | None = None,
                 index: int | None = None, from_index: int | None = None,
                 to_index: int | None = None) -> PlaybackQueue:
    """Edit detached canonical occurrences, preserving metadata and selection."""
    try:
        _editable(queue)
        candidate = deepcopy(queue)
        if action == "playlist_append":
            additions = explicit_queue(candidate.name, urls)
            candidate.items.extend(additions.items)
        elif action == "playlist_remove":
            candidate.validate_index(index)
            if candidate.count == 1:
                raise WebPlaylistError()
            del candidate.items[index - 1]
            candidate.index = min(candidate.index - (index < candidate.index), candidate.count)
        elif action == "playlist_move":
            candidate.validate_index(from_index)
            candidate.validate_index(to_index)
            # Positions are identities even when URLs and all display fields
            # are equal. The destination is the final one-based position.
            order = list(range(candidate.count))
            order.insert(to_index - 1, order.pop(from_index - 1))
            candidate.items = [candidate.items[position] for position in order]
            candidate.index = order.index(candidate.index - 1) + 1
        else:
            raise WebPlaylistError()
        _validate_canonical(candidate)
        return candidate
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise WebPlaylistError() from None


def availability(queue: PlaybackQueue, *, stopped: bool) -> tuple[dict[str, bool], str]:
    reason = edit_reason(queue)
    editable = stopped and reason == "supported"
    return {"playlist_replace": stopped,
            "playlist_append": editable and queue.count < MAX_QUEUE_TRACKS,
            "playlist_remove": editable and queue.count > 1,
            "playlist_move": editable and queue.count > 1}, reason
