"""Pure edits of valid authored URL playlists, with occurrence-based selection.

No file, network, resolver or playback I/O happens here. Both the entire source
and result must satisfy the v1 schema and explicit-URL/full escaped SOAP limits.
The file reader/writer separately enforce their independent byte limits.
"""
from __future__ import annotations

from .playlist_files import PlaylistError, validate_playlist
from .queue_protocol import explicit_queue


def _validated(payload: object) -> dict[str, object]:
    document = validate_playlist(payload)
    # explicit_queue validates every URL and the complete escaped SOAP request.
    explicit_queue(document["name"], document["urls"],
                   start_index=document["index"], mode=document["mode"])
    return document


def _check_index(index: int, count: int) -> None:
    if type(index) is not int or not 1 <= index <= count:
        raise PlaylistError("Edit index must select one authored URL")


def append_playlist(payload: object, urls: list[str]) -> dict[str, object]:
    """Append exact URL occurrences without changing the selected occurrence."""
    document = _validated(payload)
    if type(urls) is not list or not urls:
        raise PlaylistError("Append requires at least one URL")
    document["urls"].extend(urls)
    return _validated(document)


def remove_playlist(payload: object, index: int) -> dict[str, object]:
    """Remove one occurrence; its selection goes to next, or final previous."""
    document = _validated(payload)
    urls = document["urls"]
    _check_index(index, len(urls))
    if len(urls) == 1:
        raise PlaylistError("Cannot remove the only authored URL")
    del urls[index - 1]
    selected = document["index"]
    document["index"] = min(selected - (index < selected), len(urls))
    return _validated(document)


def move_playlist(payload: object, from_index: int, to_index: int) -> dict[str, object]:
    """Move to a final one-based position, preserving the selected occurrence.

    Original positions are identities: equal URL values remain distinct items.
    A same-position move returns an independent, equivalent document.
    """
    document = _validated(payload)
    urls = document["urls"]
    _check_index(from_index, len(urls))
    _check_index(to_index, len(urls))
    order = list(range(len(urls)))
    order.insert(to_index - 1, order.pop(from_index - 1))
    document["urls"] = [urls[position] for position in order]
    document["index"] = order.index(document["index"] - 1) + 1
    return _validated(document)
