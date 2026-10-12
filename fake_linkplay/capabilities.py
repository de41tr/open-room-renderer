"""One explicit service capability model shared by all protocol surfaces."""
from __future__ import annotations
import json
from typing import Any
from .config import Config
from .queue import PlaybackQueue, QueueError, QueueItem

PLEX_STREAMS_BIT = 1 << 43


def stream_mask_with_plex(value: Any) -> str:
    """Return a legacy stream bitmask with the Plex bit set."""

    # Some compatible apps still carry legacy bitmask gates even for services
    # that are now also described in StreamServicesCapability. Keep the Plex bit
    # explicit so experiment configs can OR it into old masks.
    try:
        base = int(str(value or "0"), 0)
    except ValueError:
        base = 0
    return str(base | PLEX_STREAMS_BIT)


def _without_unimplemented_plex(value: Any) -> str:
    """Legacy diagnostic masks must not re-enable unimplemented playback."""
    try:
        mask = int(str(value or "0"), 0)
    except ValueError:
        try:
            mask = int(str(value or "0"), 10)
        except ValueError:
            return "0"
    return str(max(0, mask) & ~PLEX_STREAMS_BIT)


def plex_stream_service(cfg: Config | None) -> dict[str, object]:
    """Build the StreamServicesCapability item used for Plex experiments."""

    model = getattr(cfg, "model_name", "") or "Linux Room Renderer"
    payload: dict[str, object] = {
        "id": "Plex",
        "version": "1.4",
        "brand": "Open Room Renderer",
        "hw": "Linux",
        "model": model,
        "quality": [],
        "audioSurround": [],
        "active": False,
        "configurable": False,
        "experimental": True,
        "removable": True,
        "replayGain": False,
    }
    if getattr(cfg, "plex_capability_mode", "normal") != "exact":
        payload["name"] = "Plex"
    return payload


def add_plex_stream_service(cfg: Config | None, stream_services: list[dict[str, object]]) -> None:
    """Insert or append the Plex service item according to config mode."""

    plex = plex_stream_service(cfg)
    # The Android app parser was sensitive to both the Plex item shape and
    # position during experiments. "exact" keeps the app-facing JSON minimal
    # and places Plex first for reproducible capture comparisons.
    if getattr(cfg, "plex_capability_mode", "normal") == "exact":
        stream_services.insert(0, plex)
    else:
        stream_services.append(plex)


def new_tunein_stream_service(cfg: Config | None = None, modern: bool = False) -> dict[str, object]:
    """Build the TuneIn service capability item.

    Args:
        cfg: Optional config supplying app-facing model metadata.
        modern: When true, return the richer service-manager shape.
    """

    if not modern:
        # Baseline Linkplay-like TuneIn advertisement used by the pywiim/dev
        # path. WiiM Home's newer service manager accepted the richer 1.4
        # shape below in later experiments.
        return {"id": "newTuneIn", "version": "1.1", "active": True, "configurable": False, "removable": False}
    model = getattr(cfg, "model_name", "") or "Linux Room Renderer"
    return {
        "id": "newTuneIn",
        "version": "1.4",
        "brand": "Open Room Renderer",
        "hw": "Linux",
        "model": model,
        "quality": [],
        "audioSurround": [],
        "active": True,
        "configurable": True,
        "removable": True,
        "replayGain": False,
    }


def stream_service_item(cfg: Config | None, service_id: str, version: str = "1.4") -> dict[str, object]:
    """Build a generic active stream-service capability item."""

    model = getattr(cfg, "model_name", "") or "Linux Room Renderer"
    return {
        "id": service_id,
        "version": version,
        "brand": "Open Room Renderer",
        "hw": "Linux",
        "model": model,
        "quality": [],
        "audioSurround": [],
        "active": True,
        "configurable": True,
        "removable": True,
        "replayGain": False,
    }


def add_stream_service_sentinels(cfg: Config | None, stream_services: list[dict[str, object]]) -> None:
    """Append experimental service IDs when sentinel mode is enabled."""

    if not getattr(cfg, "enable_stream_service_sentinels", False):
        return
    existing = {str(item.get("id", "")).lower() for item in stream_services}
    for service_id in ("Deezer2", "SoundCloud", "OPS", "LPRP", "HM", "PLEX", "LPDZ", "SC"):
        if service_id.lower() not in existing:
            item = stream_service_item(cfg, service_id)
            item.update(active=False, configurable=False, experimental=True)
            stream_services.append(item)


def stream_services_capability(cfg: Config | None, stream_services: list[dict[str, object]], version: str) -> dict[str, object]:
    """Wrap stream service items in the capability document shape."""

    payload: dict[str, object] = {
        "version": version,
        "encrypt": "0",
        "streamServices": stream_services,
    }
    if getattr(cfg, "plex_capability_mode", "normal") != "exact":
        payload["status"] = "0"
        payload["stream_services"] = stream_services
    return payload



def configured_sources(cfg: Config) -> list[dict[str, object]]:
    sources = []
    for source in cfg.experimental_sources:
        label = source.source or source.name
        if (not source.enabled or str(source.id).casefold() == "plex"
                or str(label).casefold() == "plex"):
            # A local URL or UI experiment cannot turn the unfinished provider
            # into an enabled service/source alias on another status surface.
            continue
        icon = source.icon.replace("{host}", cfg.resolved_advertise_ip).replace("{port}", str(cfg.http_api_port)) if source.icon else f"http://{cfg.resolved_advertise_ip}:{cfg.http_api_port}/icons/{source.id}.svg"
        sources.append({"id": source.id, "source_id": source.id, "name": source.name, "title": source.name,
            "source": label, "SourceName": label, "service": label, "url": source.url, "uri": source.url,
            "icon": icon, "icon_url": icon, "type": source.type, "enabled": True})
    return sources


def configured_presets(cfg: Config) -> list[dict[str, object]]:
    """One-based configured local favorites, shared by listing and selection.

    These are direct targets, not provider accounts or saved playlist positions.
    Experimental favorites remain explicit opt-in and follow configured order.
    """
    records: list[dict[str, object]] = [
        {"name": item.name, "url": item.url, "source": "Preset", "picurl": ""}
        for item in cfg.podcast_presets
    ]
    if cfg.source_experiment_variant in {"preset", "open_network_favorite", "all"}:
        for source in configured_sources(cfg):
            records.append({
                "id": source["id"], "name": source["name"], "title": source["name"],
                "url": source["url"], "source": source["source"], "type": source["type"],
                "icon": source["icon"], "picurl": source["icon"], "experimental": True,
            })
    valid: list[dict[str, object]] = []
    for record in records:
        # An empty experimental source is a UI experiment, not a selectable
        # favorite. Use the same shape validation as actual queue admission.
        try:
            PlaybackQueue(items=[QueueItem(url=record["url"], title=record["name"],
                source=record["source"], artwork=record.get("picurl", ""))], index=1).validate()
        except QueueError:
            continue
        number = len(valid) + 1
        record.update(number=number, index=str(number))
        valid.append(record)
    return valid


def service_capability(cfg: Config | None) -> dict[str, object]:
    services: list[dict[str, object]] = []
    if cfg is None:
        return stream_services_capability(cfg, services, "1.0")
    if cfg.source_experiment_variant == "tunein":
        services.append(new_tunein_stream_service(cfg, modern=cfg.enable_plex_service))
    if cfg.enable_plex_service:
        add_plex_stream_service(cfg, services)
    add_stream_service_sentinels(cfg, services)
    if cfg.source_experiment_variant in {"new_source", "music_service", "all"}:
        for source in configured_sources(cfg):
            services.append({"id": source["id"], "version": "1.0",
                "active": str(source["id"]).casefold() != "plex",
                "configurable": False, "removable": False, "name": source["name"],
                "url": source["url"], "icon": source["icon"], "experimental": True})
    return stream_services_capability(cfg, services, "1.4" if cfg.enable_plex_service else "1.0")


def capability_fields(cfg: Config) -> dict[str, object]:
    capability = service_capability(cfg)
    services = capability["streamServices"]
    active = [item for item in services if item.get("active")]
    supported = [str(item["id"]) for item in active]
    music = [{"id": item["id"], "name": item.get("name") or ("TuneIn" if item["id"] == "newTuneIn" else item["id"]),
              "source": item["id"], "enabled": True} for item in active]
    for record, item in zip(music, active):
        if item.get("url"):
            record["url"] = item["url"]
    legacy_plex = cfg.enable_plex_legacy_gate and any(
        item.get("id") == "Plex" and item.get("active") for item in services)
    streams = _without_unimplemented_plex(cfg.tunein_streams_value) if cfg.source_experiment_variant == "tunein" else "0"
    if legacy_plex:
        streams = stream_mask_with_plex(streams)
        supported.append("PLEX")
        music.append({"id": "PLEX", "name": "Plex", "source": "Plex", "enabled": True})
    result = {"streams": streams, "StreamCapability": json.dumps(capability, separators=(",", ":")),
              "streamServicesCapability": capability, "servicesCapability": capability,
              "supported_services": ",".join(supported), "music_services": music, "musicServices": music}
    if cfg.tunein_stream_all_value is not None:
        result["streams_all"] = stream_mask_with_plex(cfg.tunein_stream_all_value) if legacy_plex else _without_unimplemented_plex(cfg.tunein_stream_all_value)
    elif legacy_plex:
        result["streams_all"] = streams
    return result
