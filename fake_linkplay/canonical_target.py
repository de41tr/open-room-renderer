"""Pure target selection shared by local favorites and URL-playlist snapshots."""
from __future__ import annotations

from .queue import QueueItem
from .radio import RadioResolutionError, public_station_reference
from .radio_catalog import CatalogError, station_reference


def canonical_target_url(item: QueueItem) -> str:
    """Keep ordinary URL bytes, or require exact locally resolved radio proof.

    Header materialization can put a temporary stream in item.url itself. Any
    present but stale/malformed provenance must fail, never fall back to that
    URL. This selects a target only; callers own ordinary URL validation and
    their separate rules for unsupported live-stream classifications.
    """
    provenance = item._radio_station
    if provenance is None:
        return item.url
    if (type(provenance) is not tuple or len(provenance) != 2
            or provenance[0] != item.url or item.stream_type != "station"
            or type(item.duration) not in (int, float) or item.duration != 0):
        raise RadioResolutionError("Invalid station provenance")
    if isinstance(provenance[1], str) and provenance[1].startswith("rb:"):
        try:
            return station_reference(provenance[1][3:])
        except CatalogError:
            raise RadioResolutionError("Invalid station provenance") from None
    return public_station_reference(provenance[1])
