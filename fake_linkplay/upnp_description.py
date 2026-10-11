"""UPnP-style device descriptions and small XML/JSON response helpers."""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from html import escape
from typing import Any

from .config import Config


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


def plex_stream_service(cfg: Config | None) -> dict[str, object]:
    """Build the StreamServicesCapability item used for Plex experiments."""

    model = getattr(cfg, "model_name", "") or "WiiM Pro"
    payload: dict[str, object] = {
        "id": "Plex",
        "version": "1.4",
        "brand": "Linkplay",
        "hw": "WiiM",
        "model": model,
        "quality": [],
        "audioSurround": [],
        "active": True,
        "configurable": True,
        "removable": True,
        "replayGain": True,
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
    model = getattr(cfg, "model_name", "") or "WiiM Pro"
    return {
        "id": "newTuneIn",
        "version": "1.4",
        "brand": "Linkplay",
        "hw": "WiiM",
        "model": model,
        "quality": [],
        "audioSurround": [],
        "active": True,
        "configurable": True,
        "removable": True,
        "replayGain": True,
    }


def stream_service_item(cfg: Config | None, service_id: str, version: str = "1.4") -> dict[str, object]:
    """Build a generic active stream-service capability item."""

    model = getattr(cfg, "model_name", "") or "WiiM Pro"
    return {
        "id": service_id,
        "version": version,
        "brand": "Linkplay",
        "hw": "WiiM",
        "model": model,
        "quality": [],
        "audioSurround": [],
        "active": True,
        "configurable": True,
        "removable": True,
        "replayGain": True,
    }


def add_stream_service_sentinels(cfg: Config | None, stream_services: list[dict[str, object]]) -> None:
    """Append experimental service IDs when sentinel mode is enabled."""

    if not getattr(cfg, "enable_stream_service_sentinels", False):
        return
    existing = {str(item.get("id", "")).lower() for item in stream_services}
    for service_id in ("Deezer2", "SoundCloud", "OPS", "LPRP", "HM", "PLEX", "LPDZ", "SC"):
        if service_id.lower() not in existing:
            stream_services.append(stream_service_item(cfg, service_id))


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


def description_xml(cfg: Config) -> str:
    """Render the UPnP root device description XML for this renderer."""

    manufacturer_url = cfg.manufacturer_url or ("http://www.muzohifi.com" if cfg.wiim_home_profile else "")
    model_description = "MUZO Cobblestone" if cfg.wiim_home_profile else "Wireless Audio Renderer"
    model_number = cfg.model_number or ("V01-Oct 17 2016" if cfg.wiim_home_profile else "")
    friendly_name = cfg.linkplay_instance_name if cfg.wiim_home_profile else cfg.friendly_name
    return f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <URLBase>http://{cfg.resolved_advertise_ip}:{cfg.http_api_port if cfg.wiim_home_profile else cfg.upnp_port}/</URLBase>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>{escape(friendly_name)}</friendlyName>
    <manufacturer>{escape(cfg.manufacturer)}</manufacturer>
    <manufacturerURL>{escape(manufacturer_url)}</manufacturerURL>
    <modelDescription>{escape(model_description)}</modelDescription>
    <modelName>{escape(cfg.model_name)}</modelName>
    <modelNumber>{escape(model_number)}</modelNumber>
    <modelURL>{escape(cfg.model_url)}</modelURL>
    <serialNumber>{escape(cfg.upnp_uuid.replace('-', '')[:16])}</serialNumber>
    <UDN>uuid:{escape(cfg.upnp_uuid)}</UDN>
    <presentationURL>http://{cfg.resolved_advertise_ip}:{cfg.http_api_port}/httpapi.asp?command=getStatusEx</presentationURL>
    <serviceList>
      {service("urn:schemas-upnp-org:service:AVTransport:1", "urn:upnp-org:serviceId:AVTransport", "/upnp/control/AVTransport", "/upnp/event/AVTransport", "/upnp/AVTransport.xml")}
      {service("urn:schemas-upnp-org:service:AVTransport:1", "urn:upnp-org:serviceId:AVTransport1", "/upnp/control/rendertransport1", "/upnp/event/rendertransport1", "/upnp/rendertransportSCPD.xml")}
      {service("urn:schemas-upnp-org:service:RenderingControl:1", "urn:upnp-org:serviceId:RenderingControl", "/upnp/control/RenderingControl", "/upnp/event/RenderingControl", "/upnp/RenderingControl.xml")}
      {service("urn:schemas-upnp-org:service:RenderingControl:1", "urn:upnp-org:serviceId:RenderingControl1", "/upnp/control/rendercontrol1", "/upnp/event/rendercontrol1", "/upnp/rendercontrolSCPD.xml")}
      {service("urn:schemas-upnp-org:service:ConnectionManager:1", "urn:upnp-org:serviceId:ConnectionManager1", "/upnp/control/renderconnmgr1", "/upnp/event/renderconnmgr1", "/upnp/renderconnmgrSCPD.xml")}
      {service("urn:schemas-wiimu-com:service:PlayQueue:1", "urn:wiimu-com:serviceId:PlayQueue", "/upnp/control/PlayQueue", "/upnp/event/PlayQueue", "/upnp/PlayQueue.xml")}
      {service("urn:schemas-wiimu-com:service:PlayQueue:1", "urn:wiimu-com:serviceId:PlayQueue1", "/upnp/control/PlayQueue1", "/upnp/event/PlayQueue1", "/upnp/PlayQueueSCPD.xml")}
    </serviceList>
  </device>
</root>
"""


def service(service_type: str, service_id: str, control: str, event: str, scpd: str) -> str:
    """Render one UPnP service entry for the root device description."""

    return f"""<service>
        <serviceType>{service_type}</serviceType>
        <serviceId>{service_id}</serviceId>
        <controlURL>{control}</controlURL>
        <eventSubURL>{event}</eventSubURL>
        <SCPDURL>{scpd}</SCPDURL>
      </service>"""


def scpd_xml(service_name: str) -> str:
    """Render a minimal SCPD document for a named UPnP service."""

    if service_name == "RenderingControl":
        return rendering_control_scpd_xml()
    return f"""<?xml version="1.0"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>GetStatus</name><argumentList /></action>
    <action><name>Play</name><argumentList /></action>
    <action><name>Pause</name><argumentList /></action>
    <action><name>Stop</name><argumentList /></action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>{service_name}Status</name><dataType>string</dataType></stateVariable>
  </serviceStateTable>
</scpd>
"""


def rendering_control_scpd_xml() -> str:
    """Render the RenderingControl SCPD with app-used extension actions."""

    return """<?xml version="1.0"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action>
      <name>GetControlDeviceInfo</name>
      <argumentList>
        <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
        <argument><name>MultiType</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_String</relatedStateVariable></argument>
        <argument><name>PlayMode</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_String</relatedStateVariable></argument>
        <argument><name>Router</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_String</relatedStateVariable></argument>
        <argument><name>Ssid</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_String</relatedStateVariable></argument>
        <argument><name>SlaveMask</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_String</relatedStateVariable></argument>
        <argument><name>CurrentVolume</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_String</relatedStateVariable></argument>
        <argument><name>CurrentMute</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_String</relatedStateVariable></argument>
        <argument><name>CurrentChannel</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_String</relatedStateVariable></argument>
        <argument><name>SlaveList</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_String</relatedStateVariable></argument>
        <argument><name>Status</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_String</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action>
      <name>StreamServicesCapability</name>
      <argumentList>
        <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
        <argument><name>AppVersion</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_String</relatedStateVariable></argument>
        <argument><name>StreamCapability</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_String</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action><name>GetStatus</name><argumentList /></action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_InstanceID</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_String</name><dataType>string</dataType></stateVariable>
  </serviceStateTable>
</scpd>
"""


def soap_response(action: str = "OK", service_name: str = "", inner_xml: str | None = None) -> str:
    """Wrap response fields in a SOAP envelope for the requested service."""

    service_type = "urn:schemas-upnp-org:service:AVTransport:1"
    service_lower = service_name.lower()
    if "rendercontrol" in service_lower or "renderingcontrol" in service_lower:
        service_type = "urn:schemas-upnp-org:service:RenderingControl:1"
    elif "playqueue" in service_lower:
        service_type = "urn:schemas-wiimu-com:service:PlayQueue:1"
    elif "conn" in service_lower:
        service_type = "urn:schemas-upnp-org:service:ConnectionManager:1"
    response_body = inner_xml if inner_xml is not None else "<Result>OK</Result>"
    return f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body><u:{action}Response xmlns:u="{service_type}">{response_body}</u:{action}Response></s:Body>
</s:Envelope>
"""


def control_device_info_json(cfg: Config, ip: str, state: Any | None = None) -> str:
    """Return the JSON string embedded in GetControlDeviceInfo responses."""

    volume = getattr(state, "volume", 30)
    mute = getattr(state, "mute", False)
    status = getattr(state, "status", "stop")
    stream_url = getattr(state, "current_url", "")
    payload = {
        "uuid": cfg.upnp_uuid,
        "upnp_uuid": f"uuid:{cfg.upnp_uuid}",
        "DeviceName": cfg.linkplay_instance_name if cfg.wiim_home_profile else cfg.friendly_name,
        "GroupName": cfg.linkplay_instance_name if cfg.wiim_home_profile else cfg.friendly_name,
        "ssid": cfg.linkplay_instance_name if cfg.wiim_home_profile else cfg.friendly_name,
        "project": "WiiM_Pro" if cfg.wiim_home_profile else "Fake_Linkplay",
        "project_build_name": "WiiM_Pro" if cfg.wiim_home_profile else cfg.model_number,
        "firmware": "4.8.999999" if cfg.wiim_home_profile else "1.0.0",
        "Release": "20260519",
        "FW_Release_version": "4.8.999999" if cfg.wiim_home_profile else "1.0.0",
        "NewVer": "4.8.999999" if cfg.wiim_home_profile else "1.0.0",
        "service_version": "1.4",
        "security": cfg.security,
        "MAC": cfg.wiim_mac if cfg.wiim_home_profile else "02:00:00:00:00:01",
        "mac": cfg.wiim_mac if cfg.wiim_home_profile else "02:00:00:00:00:01",
        "capability": cfg.legacy_capability_value,
        "capabilities": cfg.legacy_capability_value,
        "wmrm_capability": 0,
        "EQ_support": "1",
        "EQVersion": "4.3",
        "mode": "10",
        "status": status,
        "volume": str(volume),
        "mute": "1" if mute else "0",
        "uri": stream_url,
        "stream_url": stream_url,
        "max_volume": 100,
        "battery": "0",
        "BatteryFlag": "0",
        "BatteryMains": "0",
        "BatteryPercent": "0",
        "battery_flag": "0",
        "battery_mains": "0",
        "battery_percent": "0",
        "is_battery_powered": "0",
        "portable": "0",
        "ip": ip,
        "http_port": str(cfg.http_api_port),
        "model": cfg.model_name,
    }
    sources = []
    for source in getattr(cfg, "experimental_sources", []):
        if not getattr(source, "enabled", False):
            continue
        source_name = getattr(source, "source", "") or getattr(source, "name", "")
        icon = getattr(source, "icon", "")
        if icon:
            icon = icon.replace("{host}", cfg.resolved_advertise_ip).replace("{port}", str(cfg.http_api_port))
        else:
            icon = f"http://{cfg.resolved_advertise_ip}:{cfg.http_api_port}/icons/{getattr(source, 'id', 'source')}.svg"
        sources.append(
            {
                "id": getattr(source, "id", ""),
                "source_id": getattr(source, "id", ""),
                "name": getattr(source, "name", ""),
                "title": getattr(source, "name", ""),
                "source": source_name,
                "SourceName": source_name,
                "service": source_name,
                "url": getattr(source, "url", ""),
                "uri": getattr(source, "url", ""),
                "icon": icon,
                "icon_url": icon,
                "type": getattr(source, "type", "radio"),
                "enabled": True,
            }
        )
    variant = getattr(cfg, "source_experiment_variant", "none")
    if variant in {"tunein", "new_source", "music_service", "all"}:
        stream_services: list[dict[str, object]] = [
            {
                "id": "RadioParadise2",
                "version": "1.0",
                "active": True,
                "configurable": False,
                "removable": False,
            },
            {
                "id": "newTuneIn",
                "version": "1.1",
                "active": True,
                "configurable": False,
                "removable": False,
            },
            {
                "id": "tunein",
                "version": "1.0",
                "active": True,
                "configurable": False,
                "removable": False,
            },
            {
                "id": "Samba",
                "version": "1.0",
                "active": True,
                "configurable": False,
                "removable": False,
            },
            {
                "id": "Roon",
                "version": "1.0",
                "active": False,
                "configurable": True,
                "removable": False,
            },
            {
                "id": "Squeezelite",
                "version": "1.2",
                "active": False,
                "configurable": True,
                "removable": False,
            },
        ]
        if getattr(cfg, "tunein_minimal_capability", False):
            stream_services = [new_tunein_stream_service(cfg, modern=getattr(cfg, "enable_plex_service", False))]
        if getattr(cfg, "enable_plex_service", False):
            add_plex_stream_service(cfg, stream_services)
        add_stream_service_sentinels(cfg, stream_services)
        if sources:
            stream_services.extend(
                {
                    "id": item["id"],
                    "version": "1.0",
                    "active": True,
                    "configurable": True,
                    "removable": False,
                    "name": item["name"],
                    "brand": item["name"],
                    "url": item["url"],
                    "icon": item["icon"],
                }
                for item in sources
            )
    if variant == "tunein":
        plex_enabled = getattr(cfg, "enable_plex_service", False)
        plex_legacy_gate = plex_enabled and getattr(cfg, "enable_plex_legacy_gate", False)
        stream_capability = stream_services_capability(cfg, stream_services, "1.4" if plex_enabled else "1.0")
        supported_services = [str(item["id"]) for item in stream_services if item.get("active")]
        if plex_legacy_gate and "PLEX" not in supported_services:
            supported_services.append("PLEX")
        music_service_ids = {"newTuneIn", "Plex"}
        if getattr(cfg, "enable_stream_service_sentinels", False):
            music_service_ids.update(str(item["id"]) for item in stream_services if item.get("active"))
        music_services = [
            {"id": item["id"], "name": item.get("name") or ("TuneIn" if item["id"] == "newTuneIn" else item["id"]), "source": item["id"], "enabled": True}
            for item in stream_services
            if item.get("active") and item["id"] in music_service_ids
        ]
        if plex_legacy_gate:
            music_services.append({"id": "PLEX", "name": "Plex", "source": "Plex", "enabled": True})
        streams_value = str(getattr(cfg, "tunein_streams_value", "200"))
        if plex_legacy_gate:
            streams_value = stream_mask_with_plex(streams_value)
        payload.update(
            {
                "streams": streams_value,
                "StreamCapability": json.dumps(stream_capability, separators=(",", ":")),
                "streamServicesCapability": stream_capability,
                "servicesCapability": stream_capability,
                "supported_services": ",".join(supported_services),
                "music_services": music_services,
                "musicServices": music_services,
            }
        )
        stream_all = getattr(cfg, "tunein_stream_all_value", None)
        if stream_all is not None:
            payload["streams_all"] = stream_mask_with_plex(stream_all) if plex_legacy_gate else str(stream_all)
        elif plex_legacy_gate:
            payload["streams_all"] = streams_value
    if sources and variant in {"new_source", "music_service", "all"}:
        payload.update(
            {
                "source_list": sources,
                "sourceList": sources,
                "sources": sources,
                "service_list": sources,
                "services": sources,
                "music_services": sources,
                "musicServices": sources,
                "streamServicesCapability": {
                    "status": "0",
                    "version": "1.0",
                    "encrypt": "0",
                    "streamServices": stream_services,
                    "stream_services": stream_services,
                },
                "supported_sources": ",".join(str(item["source"]) for item in sources),
                "supported_services": ",".join(str(item["service"]) for item in sources),
            }
        )
    if sources and variant in {"radioparadise_replacement", "all"}:
        payload["RadioParadise"] = sources[0]["name"]
        payload["radio_paradise_name"] = sources[0]["name"]
        payload["radio_paradise_url"] = sources[0]["url"]
    if sources and variant in {"linkplay_radio_replacement", "all"}:
        payload["LinkplayRadio"] = sources[0]["name"]
        payload["linkplay_radio_name"] = sources[0]["name"]
        payload["linkplay_radio_url"] = sources[0]["url"]
    return json.dumps(payload, separators=(",", ":"))


def get_control_device_info_response_fields(cfg: Config, ip: str, state: Any | None = None) -> str:
    """Return SOAP fields for RenderingControl GetControlDeviceInfo."""

    device_name = cfg.linkplay_instance_name if cfg.wiim_home_profile else cfg.friendly_name
    volume = getattr(state, "volume", 30)
    mute = getattr(state, "mute", False)
    status = control_device_info_json(cfg, ip, state)
    try:
        stream_capability = json.loads(status).get("StreamCapability", "")
    except json.JSONDecodeError:
        stream_capability = ""
    return "\n".join(
        [
            "<MultiType>0</MultiType>",
            "<PlayMode>0</PlayMode>",
            f"<Router>uuid:{escape(cfg.upnp_uuid)}</Router>",
            f"<Ssid>{escape(device_name)}</Ssid>",
            "<SlaveMask>0</SlaveMask>",
            f"<CurrentVolume>{escape(str(volume))}</CurrentVolume>",
            f"<CurrentMute>{'1' if mute else '0'}</CurrentMute>",
            "<CurrentChannel>0</CurrentChannel>",
            '<SlaveList>{"slaves":0,"slave_list":[]}</SlaveList>',
            f"<StreamCapability>{escape(str(stream_capability))}</StreamCapability>" if stream_capability else "",
            f"<Status>{escape(status)}</Status>",
        ]
    )


def avtransport_response_fields(action: str, state: Any | None = None) -> str | None:
    """Return SOAP fields for supported AVTransport read actions."""

    volume = getattr(state, "volume", 30)
    mute = getattr(state, "mute", False)
    status = getattr(state, "status", "stop")
    cfg = getattr(state, "cfg", None)
    current_uri = getattr(state, "current_url", "")
    track_uri = getattr(state, "resolved_url", "") or current_uri
    title = getattr(state, "title", "")
    track_metadata = getattr(state, "track_metadata", "")
    track_source = getattr(state, "track_source", "") or ("RadioParadise" if current_uri else "")
    transport_state = {"play": "PLAYING", "pause": "PAUSED_PLAYBACK", "stop": "STOPPED", "transition": "TRANSITIONING"}.get(status, "STOPPED")
    transport_status = getattr(state, "transport_status", "OK")
    play_medium = getattr(state, "play_medium", "") or ("STATION-NETWORK" if current_uri else "NONE")
    loop_mode = getattr(state, "loop", "0")
    stream_capability = ""
    if getattr(cfg, "source_experiment_variant", "") == "tunein":
        stream_services = [new_tunein_stream_service(cfg, modern=getattr(cfg, "enable_plex_service", False))]
        if not getattr(cfg, "tunein_minimal_capability", False):
            stream_services.append({"id": "tunein", "version": "1.0", "active": True, "configurable": False, "removable": False})
        if getattr(cfg, "enable_plex_service", False):
            add_plex_stream_service(cfg, stream_services)
        add_stream_service_sentinels(cfg, stream_services)
        capability_version = "1.4" if getattr(cfg, "enable_plex_service", False) else "1.0"
        stream_capability = json.dumps(stream_services_capability(cfg, stream_services, capability_version), separators=(",", ":"))
    if action == "GetMediaInfo":
        duration = seconds_to_hms(getattr(state, "totlen", 0))
        return "\n".join(
            [
                "<NrTracks>1</NrTracks>" if current_uri else "<NrTracks>0</NrTracks>",
                f"<MediaDuration>{duration}</MediaDuration>",
                f"<CurrentURI>{escape(track_uri)}</CurrentURI>",
                f"<CurrentURIMetaData>{escape(track_metadata)}</CurrentURIMetaData>",
                "<NextURI>NOT_IMPLEMENTED</NextURI>",
                "<NextURIMetaData>NOT_IMPLEMENTED</NextURIMetaData>",
                f"<TrackSource>{escape(track_source)}</TrackSource>",
                f"<StreamCapability>{escape(stream_capability)}</StreamCapability>" if stream_capability else "",
                f"<PlayMedium>{play_medium}</PlayMedium>",
                "<RecordMedium>NOT_IMPLEMENTED</RecordMedium>",
                "<WriteStatus>NOT_IMPLEMENTED</WriteStatus>",
            ]
        )
    if action in {"GetInfoEx", "GetPositionInfo"}:
        duration = seconds_to_hms(getattr(state, "totlen", 0))
        info_play_medium = play_medium if current_uri else "NETWORK"
        info_track_source = track_source or "URL"
        return "\n".join(
            [
                "<Track>1</Track>" if current_uri else "<Track>0</Track>",
                f"<TrackDuration>{duration}</TrackDuration>",
                f"<TrackMetaData>{escape(track_metadata)}</TrackMetaData>",
                f"<TrackURI>{escape(track_uri)}</TrackURI>",
                f"<RelTime>{seconds_to_hms(getattr(state, 'curpos', 0))}</RelTime>",
                f"<AbsTime>{seconds_to_hms(getattr(state, 'curpos', 0))}</AbsTime>",
                "<RelCount>0</RelCount>",
                "<AbsCount>0</AbsCount>",
                f"<CurrentTransportState>{transport_state}</CurrentTransportState>",
                f"<CurrentTransportStatus>{transport_status}</CurrentTransportStatus>",
                "<CurrentSpeed>1</CurrentSpeed>",
                f"<LoopMode>{escape(str(loop_mode))}</LoopMode>",
                "<PlayType>0</PlayType>",
                f"<CurrentVolume>{escape(str(volume))}</CurrentVolume>",
                f"<CurrentMute>{'1' if mute else '0'}</CurrentMute>",
                "<CurrentChannel>Master</CurrentChannel>",
                "<SlaveFlag>0</SlaveFlag>",
                "<SlaveList>[]</SlaveList>",
                f"<PlayMedium>{info_play_medium}</PlayMedium>",
                f"<TrackSource>{escape(info_track_source)}</TrackSource>",
                f"<StreamCapability>{escape(stream_capability)}</StreamCapability>" if stream_capability else "",
                "<InternetAccess>1</InternetAccess>",
                "<VerUpdateFlag>0</VerUpdateFlag>",
                "<VerUpdateStatus>0</VerUpdateStatus>",
                "<BatteryFlag>0</BatteryFlag>",
                "<BatteryMains>0</BatteryMains>",
                "<BatteryPercent>0</BatteryPercent>",
                "<AlarmFlag>0</AlarmFlag>",
                "<TimeStamp>0</TimeStamp>",
                "<SubNum>2</SubNum>",
                "<SpotifyActive>0</SpotifyActive>",
            ]
        )
    if action == "GetTransportInfo":
        return "\n".join(
            [
                f"<CurrentTransportState>{transport_state}</CurrentTransportState>",
                f"<CurrentTransportStatus>{transport_status}</CurrentTransportStatus>",
                "<CurrentSpeed>1</CurrentSpeed>",
            ]
        )
    if action == "GetCurrentTransportActions":
        return "<Actions>Play,Pause,Stop,Seek,Previous</Actions>"
    return None


def seconds_to_hms(value: Any) -> str:
    """Format a loose seconds value as HH:MM:SS."""

    try:
        total = max(0, int(value))
    except (TypeError, ValueError):
        total = 0
    hours = total // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def playqueue_response_fields(action: str) -> str | None:
    """Return fallback SOAP fields for PlayQueue actions."""

    if action == "GetKeyMapping":
        return "<QueueContext>{}</QueueContext>"
    if action == "GetUserInfo":
        result = json.dumps({"id": "", "userId": "", "showName": "", "anonUserId": "", "version": "1.0"}, separators=(",", ":"))
        return "\n".join(
            [
                f"<Result>{escape(result)}</Result>",
            ]
        )
    if action in {"BrowseQueue", "BrowseQueueEx"}:
        return "<QueueContext></QueueContext>"
    if action in {"CreateQueue", "ReplaceQueue"}:
        return ""
    if action == "PlayQueueWithIndex":
        return ""
    if action in {"SetQueueLoopMode", "SetQueuePolicy"}:
        return ""
    if action == "GetQueueLoopMode":
        return "<LoopMode>0</LoopMode>"
    if action == "GetQueueIndex":
        return "<CurrentIndex>1</CurrentIndex><PreloadingIndex>1</PreloadingIndex><CurrentPage>1</CurrentPage><TrackNums>1</TrackNums>"
    return None
