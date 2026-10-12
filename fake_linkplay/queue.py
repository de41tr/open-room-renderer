"""Pure, bounded PlayList queue parsing and serialization.

Queue positions are one-based; an empty queue has position zero. Provider
header-only selections describe unresolved media, not a playable item. They
retain their provider fields but have an empty local queue until a resolver
supplies real tracks. This module never resolves or fetches a resource.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
from xml.etree import ElementTree as ET


MAX_QUEUE_BYTES = 1024 * 1024
MAX_QUEUE_TRACKS = 1000
MAX_XML_DEPTH = 32
DIDL_NS = "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
DC_NS = "http://purl.org/dc/elements/1.1/"
UPNP_NS = "urn:schemas-upnp-org:metadata-1-0/upnp/"


class QueueError(ValueError):
    """Invalid queue data, queue name, or one-based queue position."""


@dataclass
class QueueItem:
    """A playable URL plus unmodified provider identity and metadata text."""

    url: str
    title: str = ""
    source: str = ""
    id: str = ""
    metadata: str = ""
    artwork: str = ""
    stream_type: str = ""
    content_type: str = ""
    duration: float = 0.0
    resolved_url: str = ""
    artist: str = ""
    album: str = ""
    title_is_fallback: bool = False
    # Local resolver provenance. Never parsed from or emitted into queue XML;
    # provider names, IDs and search URLs cannot authorize a refresh.
    _radio_station: tuple[str, str] | None = field(default=None, init=False, repr=False)
    _radio_resolution_attempted: bool = field(default=False, init=False, repr=False)


@dataclass(frozen=True)
class DidlMetadata:
    """Optional display hints, never a replacement for the original DIDL text."""

    title: str = ""
    artist: str = ""
    album: str = ""
    artwork: str = ""
    duration: float = 0.0


@dataclass
class PlaybackQueue:
    """A locally materialized queue; ``name`` is its control-plane identity."""

    name: str = "CurrentQueue"
    list_name: str = ""
    source: str = ""
    search_url: str = ""
    artwork: str = ""
    quality: str = ""
    items: list[QueueItem] = field(default_factory=list)
    index: int = 0
    loop: int = 0
    stream_id: str = ""
    show_name: str = ""
    stream_type: str = ""
    content_type: str = ""

    def __post_init__(self) -> None:
        self.validate()

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def current(self) -> QueueItem | None:
        self.validate_index(self.index)
        return self.items[self.index - 1] if self.items else None

    def validate_index(self, index: int) -> int:
        """Return a valid position, or raise without changing the queue."""
        if type(index) is not int:
            raise QueueError("Queue index must be an integer")
        if self.items:
            if not 1 <= index <= self.count:
                raise QueueError("Queue index must be between 1 and track count")
        elif index != 0:
            raise QueueError("Empty queue index must be 0")
        return index

    def require_name(self, name: str) -> None:
        """Reject a request addressed to a different or missing queue."""
        if not isinstance(name, str) or not name or name != self.name:
            raise QueueError("Unknown queue name")

    def validate(self, *, max_tracks: int = MAX_QUEUE_TRACKS) -> None:
        """Validate mutable state before exposing it as a wire document."""
        if not isinstance(self.name, str) or not self.name.strip():
            raise QueueError("Queue name must not be empty")
        for value in (self.name, self.list_name, self.source, self.search_url,
                      self.artwork, self.quality, self.stream_id, self.show_name,
                      self.stream_type, self.content_type):
            _validate_text(value)
        if not isinstance(self.items, list):
            raise QueueError("Queue items must be a list")
        if len(self.items) > max_tracks:
            raise QueueError("Queue exceeds track limit")
        self.validate_index(self.index)
        if type(self.loop) is not int or self.loop != 0:
            raise QueueError("Only queue loop mode 0 is supported")
        for item in self.items:
            if not isinstance(item, QueueItem):
                raise QueueError("Queue contains an invalid item")
            if type(item.title_is_fallback) is not bool:
                raise QueueError("Queue title origin flag must be boolean")
            if not isinstance(item.url, str) or not item.url.strip():
                raise QueueError("Queue track requires a direct URL")
            for value in (item.url, item.title, item.source, item.id, item.metadata,
                          item.artwork, item.stream_type, item.content_type, item.resolved_url,
                          item.artist, item.album):
                _validate_text(value)
            if (isinstance(item.duration, bool)
                    or not isinstance(item.duration, (int, float))
                    or item.duration < 0):
                raise QueueError("Queue duration must be finite and nonnegative")
            try:
                finite = math.isfinite(item.duration)
            except OverflowError:
                finite = False
            if not finite:
                raise QueueError("Queue duration must be finite and nonnegative")


def _validate_text(value: str) -> None:
    if not isinstance(value, str):
        raise QueueError("Queue text fields must be strings")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", value):
        raise QueueError("Queue text contains invalid XML characters")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(parent: ET.Element | None, name: str) -> ET.Element | None:
    if parent is None:
        return None
    matches = [node for node in parent if _local_name(node.tag) == name]
    if len(matches) > 1:
        raise QueueError(f"Duplicate queue field {name}")
    return matches[0] if matches else None


def _structure(parent: ET.Element | None) -> None:
    """Container elements allow whitespace, not mixed text content."""
    if parent is not None and (
        (parent.text or "").strip() or any((node.tail or "").strip() for node in parent)
    ):
        raise QueueError("Queue container contains unexpected text")


def _text(parent: ET.Element | None, name: str) -> str | None:
    node = _child(parent, name)
    if node is None:
        return None
    if len(node):
        raise QueueError(f"Queue field {name} must contain text only")
    return node.text or ""


def _integer(value: str, name: str) -> int:
    value = value.strip()
    if not re.fullmatch(r"[0-9]+", value) or len(value) > 10:
        raise QueueError(f"Invalid queue {name}")
    return int(value)


def _matching_integer(values: list[str | None], name: str) -> int | None:
    parsed = [_integer(value, name) for value in values if value is not None]
    if parsed and any(value != parsed[0] for value in parsed):
        raise QueueError(f"Conflicting queue {name} values")
    return parsed[0] if parsed else None


def _parse_xml(value: str | bytes, max_bytes: int) -> ET.Element:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise QueueError("Queue byte limit must be a positive integer")
    if not isinstance(value, (str, bytes)):
        raise QueueError("QueueContext must contain XML text")
    try:
        encoded = value.encode("utf-8") if isinstance(value, str) else value
        if len(encoded) > max_bytes:
            raise QueueError("QueueContext exceeds byte limit")
        text = encoded.decode("utf-8-sig")
    except UnicodeError:
        raise QueueError("QueueContext must be valid UTF-8") from None
    _validate_text(text)
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
        raise QueueError("QueueContext DTDs and entities are not allowed")
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, ValueError):
        raise QueueError("Malformed QueueContext XML") from None
    pending = [(root, 1)]
    while pending:
        node, depth = pending.pop()
        if depth > MAX_XML_DEPTH:
            raise QueueError("QueueContext exceeds XML depth limit")
        pending.extend((child, depth + 1) for child in node)
    return root


def _duration(value: str) -> float:
    """Accept numeric seconds or an explicit hours:minutes:seconds duration."""
    value = value.strip()
    if not value:
        return 0.0
    if ":" in value:
        match = re.fullmatch(r"([0-9]+):([0-5][0-9]):([0-5][0-9](?:\.[0-9]+)?)", value)
        if not match:
            raise QueueError("Invalid queue duration")
        hours, minutes, seconds = match.groups()
        result = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    else:
        try:
            result = float(value)
        except ValueError:
            raise QueueError("Invalid queue duration") from None
    if not math.isfinite(result) or result < 0:
        raise QueueError("Queue duration must be finite and nonnegative")
    return float(result)


def parse_didl_metadata(
    metadata: str | bytes,
    *,
    max_bytes: int = MAX_QUEUE_BYTES,
) -> DidlMetadata:
    """Read bounded, explicitly namespaced DIDL hints without changing text.

    Read only the first direct DIDL item and the first matching direct, scalar
    field. Unknown namespaces, nested fields and arbitrary URL-looking text
    have no special meaning. The resource URL is deliberately never selected:
    callers must continue to use their explicit transport/queue URL. Malformed,
    unsafe or oversized metadata supplies no hints; preserve its original text
    separately rather than serializing this projection back over the payload.
    """
    if type(max_bytes) is not int or max_bytes <= 0:
        raise QueueError("Queue byte limit must be a positive integer")
    try:
        root = _parse_xml(metadata, max_bytes)
    except QueueError:
        return DidlMetadata()
    if root.tag != f"{{{DIDL_NS}}}DIDL-Lite":
        return DidlMetadata()
    item = root.find(f"{{{DIDL_NS}}}item")
    if item is None:
        return DidlMetadata()

    def text(namespace: str, name: str) -> str:
        node = item.find(f"{{{namespace}}}{name}")
        if node is None or len(node):
            return ""
        return node.text or ""

    resource = item.find(f"{{{DIDL_NS}}}res")
    duration = 0.0
    if resource is not None and not len(resource) and resource.get("duration"):
        try:
            duration = _duration(resource.get("duration", ""))
        except (QueueError, ValueError, OverflowError):
            pass
    return DidlMetadata(
        title=text(DC_NS, "title"),
        artist=text(UPNP_NS, "artist"),
        album=text(UPNP_NS, "album"),
        artwork=text(UPNP_NS, "albumArtURI"),
        duration=duration,
    )


def parse_queue_context(
    value: str | bytes,
    *,
    name: str | None = None,
    max_bytes: int = MAX_QUEUE_BYTES,
    max_tracks: int = MAX_QUEUE_TRACKS,
) -> PlaybackQueue:
    """Parse a PlayList without fetching resources or searching arbitrary XML.

    Track labels must form Track1..TrackN; label order determines queue order.
    A supplied count must match actual tracks, rather than silently hiding a
    malformed/truncated queue. Header-only provider selections are the sole
    exception: their count/start hints describe unresolved remote content and
    normalize to actual local count/index zero. Their hints must still agree
    and be in range. ``name`` may assign an explicit server-side queue name.
    """
    if type(max_tracks) is not int or not 1 <= max_tracks <= MAX_QUEUE_TRACKS:
        raise QueueError(f"Queue track limit must be between 1 and {MAX_QUEUE_TRACKS}")
    root = _parse_xml(value, max_bytes)
    if _local_name(root.tag) != "PlayList":
        raise QueueError("QueueContext root must be PlayList")
    info = _child(root, "ListInfo")
    tracks = _child(root, "Tracks")
    for container in (root, info, tracks):
        _structure(container)
    list_name = _text(root, "ListName") or ""
    wire_name = _text(root, "QueueName")
    queue_name = name if name is not None else wire_name
    if queue_name is None:
        queue_name = list_name or "CurrentQueue"
    source = _text(info, "SourceName") or ""
    search_url = _text(info, "SearchUrl") or ""
    stream_id = _text(info, "StreamId") or ""
    declared_count = _matching_integer(
        [_text(root, "Count"), _text(info, "Count"), _text(info, "TrackNumber")], "count"
    )
    requested_index = _matching_integer(
        [_text(root, "Index"), _text(info, "Index"), _text(info, "StartPlayIndex")], "index"
    )
    loop = _matching_integer([_text(root, "LoopMode"), _text(info, "LoopMode")], "loop mode")
    if declared_count is not None and declared_count > max_tracks:
        raise QueueError("Queue exceeds track limit")

    numbered: dict[int, ET.Element] = {}
    if tracks is not None:
        if len(tracks) > max_tracks:
            raise QueueError("Queue exceeds track limit")
        for track in tracks:
            match = re.fullmatch(r"Track([1-9][0-9]*)", _local_name(track.tag))
            if match is None or len(match[1]) > 10:
                raise QueueError("Tracks must contain numbered TrackN elements")
            number = int(match[1])
            if number in numbered:
                raise QueueError("Duplicate queue track number")
            numbered[number] = track
    if sorted(numbered) != list(range(1, len(numbered) + 1)):
        raise QueueError("Queue tracks must be numbered consecutively from 1")

    items = []
    for number in sorted(numbered):
        track = numbered[number]
        _structure(track)
        track_number = _text(track, "TrackNumber")
        if track_number is not None and _integer(track_number, "track number") != number:
            raise QueueError("Queue track number disagrees with TrackN label")
        url = (_text(track, "URL") or "").strip()
        if not url:
            raise QueueError("Queue track requires a direct URL")
        metadata = _text(track, "Metadata") or ""
        hints = parse_didl_metadata(metadata, max_bytes=max_bytes)
        direct_title = _text(track, "Title")
        direct_artist = _text(track, "Artist")
        direct_album = _text(track, "Album")
        direct_artwork = _text(track, "PicUrl")
        direct_source = _text(track, "Source")
        direct_duration = _text(track, "Duration")
        duration = hints.duration
        if direct_duration is not None:
            try:
                duration = _duration(direct_duration)
            except (ValueError, OverflowError):
                raise QueueError("Invalid queue duration") from None
        items.append(QueueItem(
            url=url,
            title=direct_title if direct_title is not None else hints.title,
            artist=direct_artist if direct_artist is not None else hints.artist,
            album=direct_album if direct_album is not None else hints.album,
            source=direct_source if direct_source is not None else source,
            id=_text(track, "Id") or "",
            metadata=metadata,
            artwork=direct_artwork if direct_artwork is not None else hints.artwork,
            stream_type=_text(track, "StreamType") or _text(info, "StreamType") or "",
            content_type=_text(track, "ContentType") or _text(info, "ContentType") or "",
            duration=duration,
        ))

    count = len(items)
    unresolved = not items and bool(source and (stream_id or search_url))
    expected_count = declared_count if unresolved and declared_count is not None else count
    if declared_count is not None and declared_count != expected_count:
        raise QueueError("Declared queue count does not match tracks")
    if requested_index is None:
        requested_index = 1 if expected_count else 0
    if expected_count:
        if not 1 <= requested_index <= expected_count:
            raise QueueError("Queue index must be between 1 and track count")
    elif requested_index != 0:
        raise QueueError("Empty queue index must be 0")
    queue = PlaybackQueue(
        name=queue_name,
        list_name=list_name,
        source=source,
        search_url=search_url,
        artwork=_text(info, "PicUrl") or "",
        quality=_text(info, "Quality") or "",
        items=items,
        index=requested_index if items else 0,
        loop=0 if loop is None else loop,
        stream_id=stream_id,
        show_name=_text(info, "ShowName") or "",
        stream_type=_text(info, "StreamType") or "",
        content_type=_text(info, "ContentType") or "",
    )
    queue.validate(max_tracks=max_tracks)
    return queue


def serialize_queue_context(queue: PlaybackQueue) -> str:
    """Build fresh XML from actual state; escape nested Metadata exactly once."""
    queue.validate()
    root = ET.Element("PlayList")

    def add(parent: ET.Element, name: str, value: str | int | float) -> None:
        ET.SubElement(parent, name).text = str(value)

    add(root, "QueueName", queue.name)
    add(root, "ListName", queue.list_name)
    add(root, "Count", queue.count)
    add(root, "Index", queue.index)
    add(root, "LoopMode", queue.loop)
    info = ET.SubElement(root, "ListInfo")
    for name, value in (
        ("SourceName", queue.source), ("TrackSource", queue.source), ("SearchUrl", queue.search_url),
        ("TrackNumber", queue.count), ("StreamId", queue.stream_id),
        ("StartPlayIndex", queue.index), ("ShowName", queue.show_name),
        ("PicUrl", queue.artwork), ("Quality", queue.quality),
        ("StreamType", queue.stream_type), ("ContentType", queue.content_type),
    ):
        add(info, name, value)
    tracks = ET.SubElement(root, "Tracks")
    for number, item in enumerate(queue.items, 1):
        track = ET.SubElement(tracks, f"Track{number}")
        for name, value in (
            ("TrackNumber", number), ("Title", item.title), ("Source", item.source),
            ("Artist", item.artist), ("Album", item.album),
            ("Id", item.id), ("URL", item.resolved_url or item.url), ("Metadata", item.metadata),
            ("PicUrl", item.artwork), ("StreamType", item.stream_type),
            ("ContentType", item.content_type), ("Duration", item.duration),
        ):
            add(track, name, value)
    # XML normalizes literal CR on input; character references preserve it in
    # opaque metadata (and other text) across a serialize/parse round trip.
    return ET.tostring(root, encoding="unicode", xml_declaration=True).replace("\r", "&#13;")
