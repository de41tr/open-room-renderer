"""Bounded SOAP 1.1 framing and explicit UPnP action contracts.

This module validates framing, not an exhaustive list of client extensions.
Action arguments remain the responsibility of their implementation.
"""
from __future__ import annotations

from html import escape
import re
from xml.etree import ElementTree as ET

from .queue_diagnostics import QueueRejectionReason

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
SERVICE_TYPES = {
    "AVTransport": "urn:schemas-upnp-org:service:AVTransport:1",
    "RenderingControl": "urn:schemas-upnp-org:service:RenderingControl:1",
    "ConnectionManager": "urn:schemas-upnp-org:service:ConnectionManager:1",
    "PlayQueue": "urn:schemas-wiimu-com:service:PlayQueue:1",
}
SERVICE_ALIASES = {
    "avtransport": "AVTransport", "rendertransport1": "AVTransport",
    "avtransport1": "AVTransport", "renderingcontrol": "RenderingControl",
    "rendercontrol1": "RenderingControl", "renderingcontrol1": "RenderingControl",
    "connectionmanager": "ConnectionManager", "connectionmanager1": "ConnectionManager",
    "renderconnmgr1": "ConnectionManager", "playqueue": "PlayQueue",
    "playqueue1": "PlayQueue",
}
READ_ACTIONS = {
    "AVTransport": {"GetInfoEx", "GetPositionInfo", "GetMediaInfo", "GetTransportInfo", "GetTransportSettings", "GetCurrentTransportActions"},
    "RenderingControl": {"GetControlDeviceInfo", "StreamServicesCapability", "GetVolume", "GetMute", "GetChannel", "ListPresets"},
    "ConnectionManager": {"GetProtocolInfo", "GetCurrentConnectionIDs"},
    "PlayQueue": {"BrowseQueue", "BrowseQueueEx", "GetQueueIndex", "GetQueueLoopMode", "GetKeyMapping", "GetUserInfo"},
}
MUTATING_ACTIONS = {
    "AVTransport": {"SetAVTransportURI", "Play", "Pause", "Stop", "Next", "Previous", "Seek", "SeekForward", "SeekBackward", "SetPlayMode"},
    "RenderingControl": {"SetVolume", "SetMute"},
    "ConnectionManager": set(),
    "PlayQueue": {"CreateQueue", "ReplaceQueue", "PlayQueueWithIndex", "SetQueueLoopMode", "UserLogin", "AppendTracksInQueueEx"},
}


class SoapFault(Exception):
    """A public, non-sensitive UPnP failure description."""
    def __init__(self, code: int, description: str, *, queue_rejection: QueueRejectionReason | None = None) -> None:
        super().__init__(description)
        self.code = code
        self.description = description
        self.queue_rejection = queue_rejection


def fault_xml(fault: SoapFault) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<s:Envelope xmlns:s="{SOAP_NS}"><s:Body><s:Fault>'
        '<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring>'
        '<detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
        f'<errorCode>{fault.code}</errorCode>'
        f'<errorDescription>{escape(fault.description)}</errorDescription>'
        '</UPnPError></detail></s:Fault></s:Body></s:Envelope>'
    )


def parse_request(service_path: str, soap_action: str, body: str) -> tuple[str, str, str]:
    """Require one action whose path, header and XML namespaces agree."""
    service = SERVICE_ALIASES.get(service_path.lower())
    if service is None:
        raise SoapFault(401, "Invalid Action")
    if len(body.encode("utf-8")) > 1024 * 1024 or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", body, re.I):
        raise SoapFault(402, "Invalid Args")
    header = soap_action.strip().strip('"')
    namespace, sep, action = header.rpartition("#")
    if not sep or namespace != SERVICE_TYPES[service] or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", action):
        raise SoapFault(401, "Invalid Action")
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, ValueError):
        raise SoapFault(402, "Invalid Args") from None
    bodies = root.findall(f"{{{SOAP_NS}}}Body")
    if root.tag != f"{{{SOAP_NS}}}Envelope" or len(bodies) != 1 or len(bodies[0]) != 1:
        raise SoapFault(402, "Invalid Args")
    action_node = bodies[0][0]
    if action_node.tag != f"{{{namespace}}}{action}":
        raise SoapFault(401, "Invalid Action")
    if action not in READ_ACTIONS[service] | MUTATING_ACTIONS[service]:
        raise SoapFault(401, "Invalid Action")
    # Ordinary extension arguments (e.g. ControlSource) are intentionally allowed.
    names = [child.tag.rsplit("}", 1)[-1] for child in action_node]
    if len(names) != len(set(names)):
        raise SoapFault(402, "Invalid Args")
    for child in action_node:
        name = child.tag.rsplit("}", 1)[-1]
        if name == "InstanceID" and (child.text or "").strip() != "0":
            raise SoapFault(718, "Invalid InstanceID")
        if name == "Channel" and (child.text or "").strip() not in {"Master", "Single"}:
            raise SoapFault(402, "Invalid Args")
    arguments = []
    for child in action_node:
        if len(child):
            raise SoapFault(402, "Invalid Args")
        name = child.tag.rsplit("}", 1)[-1]
        arguments.append(f"<{name}>{escape(child.text or '')}</{name}>")
    return service, action, "<Action>" + "".join(arguments) + "</Action>"
