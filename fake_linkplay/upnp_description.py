"""UPnP-style device descriptions and small XML/JSON response helpers."""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from html import escape as _html_escape
from typing import Any

from .config import Config
from .queue_policy import NORMAL, play_mode_name
from .upnp_schema import scpd_xml as _declared_scpd


from .capabilities import (PLEX_STREAMS_BIT, add_plex_stream_service, add_stream_service_sentinels,
    capability_fields, configured_presets, configured_sources, new_tunein_stream_service, plex_stream_service,
    service_capability, stream_mask_with_plex, stream_service_item, stream_services_capability)


def escape(value: str) -> str:
    """Escape XML text without losing carriage returns to XML normalization."""
    return _html_escape(value).replace("\r", "&#13;")


def description_xml(cfg: Config) -> str:
    """Render the UPnP root device description XML for this renderer."""

    manufacturer_url = cfg.manufacturer_url
    model_description = "Independent Linux network audio renderer"
    model_number = cfg.model_number
    friendly_name = cfg.linkplay_instance_name if cfg.wiim_home_profile else cfg.friendly_name
    # Retain compatibility aliases, but publish canonical services last for
    # clients whose service-type lookup keeps the final entry.
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
    <iconList><icon>
      <mimetype>image/png</mimetype><width>485</width><height>483</height><depth>32</depth>
      <url>/ui/logo.png</url>
    </icon></iconList>
    <serviceList>
      {service("urn:schemas-upnp-org:service:AVTransport:1", "urn:upnp-org:serviceId:AVTransport1", "/upnp/control/rendertransport1", "/upnp/event/rendertransport1", "/upnp/rendertransportSCPD.xml")}
      {service("urn:schemas-upnp-org:service:AVTransport:1", "urn:upnp-org:serviceId:AVTransport", "/upnp/control/AVTransport", "/upnp/event/AVTransport", "/upnp/AVTransport.xml")}
      {service("urn:schemas-upnp-org:service:RenderingControl:1", "urn:upnp-org:serviceId:RenderingControl1", "/upnp/control/rendercontrol1", "/upnp/event/rendercontrol1", "/upnp/rendercontrolSCPD.xml")}
      {service("urn:schemas-upnp-org:service:RenderingControl:1", "urn:upnp-org:serviceId:RenderingControl", "/upnp/control/RenderingControl", "/upnp/event/RenderingControl", "/upnp/RenderingControl.xml")}
      {service("urn:schemas-upnp-org:service:ConnectionManager:1", "urn:upnp-org:serviceId:ConnectionManager1", "/upnp/control/renderconnmgr1", "/upnp/event/renderconnmgr1", "/upnp/renderconnmgrSCPD.xml")}
      {service("urn:schemas-upnp-org:service:ConnectionManager:1", "urn:upnp-org:serviceId:ConnectionManager", "/upnp/control/ConnectionManager", "/upnp/event/ConnectionManager", "/upnp/ConnectionManager.xml")}
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


def scpd_xml(service_name: str, *, enable_plex: bool = False, include_control_source: bool = False) -> str:
    """Render only implemented service actions and their typed contracts."""
    return _declared_scpd(service_name, enable_plex=enable_plex, include_control_source=include_control_source)


def rendering_control_scpd_xml() -> str:
    return scpd_xml("RenderingControl")


def connection_manager_properties() -> dict[str, str]:
    """Existing static capabilities; no allocated connection is advertised."""
    return {"SourceProtocolInfo": "", "SinkProtocolInfo": "http-get:*:audio/*:*",
            "CurrentConnectionIDs": ""}


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
    preset_view = getattr(state, "effective_presets", None)
    presets = preset_view() if callable(preset_view) else configured_presets(cfg)
    payload = {
        "uuid": cfg.upnp_uuid,
        "upnp_uuid": f"uuid:{cfg.upnp_uuid}",
        "DeviceName": cfg.linkplay_instance_name if cfg.wiim_home_profile else cfg.friendly_name,
        "GroupName": cfg.linkplay_instance_name if cfg.wiim_home_profile else cfg.friendly_name,
        "ssid": cfg.linkplay_instance_name if cfg.wiim_home_profile else cfg.friendly_name,
        "project": "Open_Room_Renderer",
        "project_build_name": cfg.model_number,
        "firmware": "0.1.0",
        "Release": "0.1.0",
        "FW_Release_version": "0.1.0",
        "NewVer": "0.1.0",
        "service_version": "1.4",
        "preset_key": str(len(presets)),
        "security": cfg.security,
        "MAC": cfg.wiim_mac,
        "mac": cfg.wiim_mac,
        "capability": cfg.legacy_capability_value,
        "capabilities": cfg.legacy_capability_value,
        "wmrm_capability": 0,
        "EQ_support": "1",
        "EQVersion": "4.3",
        "mode": getattr(state, "mode", "0"),
        "status": status,
        "internet": "0",
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
    capability_view = getattr(state, "effective_capability_fields", None)
    payload.update(capability_view() if callable(capability_view) else capability_fields(cfg))
    sources = configured_sources(cfg)
    variant = cfg.source_experiment_variant
    if sources and variant in {"new_source", "music_service", "all"}:
        payload.update(source_list=sources, sourceList=sources, sources=sources,
                       service_list=sources, services=sources,
                       supported_sources=",".join(str(item["source"]) for item in sources))
    if sources and variant in {"radioparadise_replacement", "all"}:
        payload.update(RadioParadise=sources[0]["name"], radio_paradise_name=sources[0]["name"], radio_paradise_url=sources[0]["url"])
    if sources and variant in {"linkplay_radio_replacement", "all"}:
        payload.update(LinkplayRadio=sources[0]["name"], linkplay_radio_name=sources[0]["name"], linkplay_radio_url=sources[0]["url"])
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
            "<MultiType>1</MultiType>",
            "<PlayMode>0</PlayMode>",
            f"<Router>uuid:{escape(cfg.upnp_uuid)}</Router>",
            f"<Ssid>{escape(device_name)}</Ssid>",
            "<SlaveMask>0</SlaveMask>",
            f"<CurrentVolume>{escape(str(volume))}</CurrentVolume>",
            f"<CurrentMute>{'1' if mute else '0'}</CurrentMute>",
            f"<GainConfirmed>{'1' if getattr(state, 'gain_confirmed', True) else '0'}</GainConfirmed>",
            "<CurrentChannel>Master</CurrentChannel>",
            '<SlaveList>{"slaves":0,"slave_list":[]}</SlaveList>',
            f"<StreamCapability>{escape(str(stream_capability))}</StreamCapability>",
            f"<Status>{escape(status)}</Status>",
        ]
    )


def avtransport_response_fields(
    action: str, state: Any | None = None, *, current_transport_actions: str = "",
) -> str | None:
    """Return SOAP fields for supported AVTransport read actions."""

    volume = getattr(state, "volume", 30)
    mute = getattr(state, "mute", False)
    status = getattr(state, "status", "stop")
    cfg = getattr(state, "cfg", None)
    current_uri = getattr(state, "current_url", "")
    track_uri = getattr(state, "resolved_url", "") or current_uri
    title = getattr(state, "title", "")
    track_metadata = getattr(state, "track_metadata", "")
    track_source = getattr(state, "track_source", "")
    transport_state = {"play": "PLAYING", "pause": "PAUSED_PLAYBACK", "stop": "STOPPED", "transition": "TRANSITIONING"}.get(status, "STOPPED")
    transport_status = getattr(state, "transport_status", "OK")
    play_medium = getattr(state, "play_medium", "") or ("STATION-NETWORK" if current_uri else "NONE")
    loop_mode = getattr(state, "loop", str(NORMAL))
    capability_view = getattr(state, "effective_capability_fields", None)
    stream_capability = (capability_view()["StreamCapability"] if callable(capability_view)
                         else json.dumps(service_capability(cfg), separators=(",", ":")))
    if action == "GetMediaInfo":
        duration = seconds_to_hms(getattr(state, "totlen", 0))
        return "\n".join(
            [
                f"<NrTracks>{getattr(state, 'plicount', 0)}</NrTracks>",
                f"<MediaDuration>{duration}</MediaDuration>",
                f"<CurrentURI>{escape(track_uri)}</CurrentURI>",
                f"<CurrentURIMetaData>{escape(track_metadata)}</CurrentURIMetaData>",
                "<NextURI>NOT_IMPLEMENTED</NextURI>",
                "<NextURIMetaData>NOT_IMPLEMENTED</NextURIMetaData>",
                f"<TrackSource>{escape(track_source)}</TrackSource>",
                f"<StreamCapability>{escape(stream_capability)}</StreamCapability>",
                f"<PlayMedium>{play_medium}</PlayMedium>",
                "<RecordMedium>NOT_IMPLEMENTED</RecordMedium>",
                "<WriteStatus>NOT_IMPLEMENTED</WriteStatus>",
            ]
        )
    if action in {"GetInfoEx", "GetPositionInfo"}:
        duration = seconds_to_hms(getattr(state, "totlen", 0))
        info_play_medium = play_medium if current_uri else "NETWORK"
        info_track_source = track_source
        return "\n".join(
            [
                f"<Track>{getattr(state, 'plicurr', 0)}</Track>",
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
                f"<GainConfirmed>{'1' if getattr(state, 'gain_confirmed', True) else '0'}</GainConfirmed>",
                "<CurrentChannel>Master</CurrentChannel>",
                "<SlaveFlag>0</SlaveFlag>",
                "<SlaveList>[]</SlaveList>",
                f"<PlayMedium>{info_play_medium}</PlayMedium>",
                f"<TrackSource>{escape(info_track_source)}</TrackSource>",
                f"<StreamCapability>{escape(stream_capability)}</StreamCapability>",
                "<InternetAccess>0</InternetAccess>",
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
    if action == "GetTransportSettings":
        return (f"<PlayMode>{play_mode_name(int(loop_mode))}</PlayMode>"
                "<RecQualityMode>NOT_IMPLEMENTED</RecQualityMode>")
    if action == "GetTransportInfo":
        return "\n".join(
            [
                f"<CurrentTransportState>{transport_state}</CurrentTransportState>",
                f"<CurrentTransportStatus>{transport_status}</CurrentTransportStatus>",
                "<CurrentSpeed>1</CurrentSpeed>",
            ]
        )
    if action == "GetCurrentTransportActions":
        return f"<Actions>{escape(current_transport_actions)}</Actions>"
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
        return f"<LoopMode>{NORMAL}</LoopMode>"
    if action == "GetQueueIndex":
        return "<CurrentIndex>1</CurrentIndex><PreloadingIndex>1</PreloadingIndex><CurrentPage>1</CurrentPage><TrackNums>1</TrackNums>"
    return None
