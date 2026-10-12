"""SCPDs for the renderer's implemented SOAP actions.

The action allowlists in :mod:`soap` are authoritative.  This module supplies
their wire arguments and state-variable types, rather than advertising the
larger action sets of commercial renderers.  These are local protocol
contracts, not a claim of UPnP certification or handset interoperability.
"""
from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree as ET

from .soap import MUTATING_ACTIONS, READ_ACTIONS, SERVICE_TYPES


SCPD_NS = "urn:schemas-upnp-org:service-1-0"


@dataclass(frozen=True)
class _Variable:
    data_type: str = "string"
    allowed_values: tuple[str, ...] = ()
    value_range: tuple[int, int, int] | None = None
    default: str | None = None


@dataclass(frozen=True)
class _Argument:
    name: str
    direction: str
    variable: str


def _in(name: str, variable: str | None = None) -> _Argument:
    return _Argument(name, "in", variable or name)


def _out(name: str, variable: str | None = None) -> _Argument:
    return _Argument(name, "out", variable or name)


_INSTANCE = _in("InstanceID", "A_ARG_TYPE_InstanceID")
_CHANNEL = _in("Channel", "A_ARG_TYPE_Channel")
_CONTROL_SOURCE = _in("ControlSource", "A_ARG_TYPE_ControlSource")

# The extended position response is shared by both implemented read handlers.
# Keep its order aligned with avtransport_response_fields(), including the
# empty StreamCapability field when no service integration is enabled.
_POSITION_OUTPUTS = (
    _out("Track", "CurrentTrack"),
    _out("TrackDuration", "CurrentTrackDuration"),
    _out("TrackMetaData", "CurrentTrackMetaData"),
    _out("TrackURI", "CurrentTrackURI"),
    _out("RelTime", "RelativeTimePosition"),
    _out("AbsTime", "AbsoluteTimePosition"),
    _out("RelCount", "RelativeCounterPosition"),
    _out("AbsCount", "AbsoluteCounterPosition"),
    _out("CurrentTransportState", "TransportState"),
    _out("CurrentTransportStatus", "TransportStatus"),
    _out("CurrentSpeed", "TransportPlaySpeed"),
    _out("LoopMode"),
    _out("PlayType"),
    _out("CurrentVolume", "Volume"),
    _out("CurrentMute", "Mute"),
    _out("GainConfirmed"),
    _out("CurrentChannel"),
    _out("SlaveFlag"),
    _out("SlaveList"),
    _out("PlayMedium", "PlaybackStorageMedium"),
    _out("TrackSource"),
    _out("StreamCapability"),
    _out("InternetAccess"),
    _out("VerUpdateFlag"),
    _out("VerUpdateStatus"),
    _out("BatteryFlag"),
    _out("BatteryMains"),
    _out("BatteryPercent"),
    _out("AlarmFlag"),
    _out("TimeStamp"),
    _out("SubNum"),
    _out("SpotifyActive"),
)

_ACTIONS: dict[str, dict[str, tuple[_Argument, ...]]] = {
    "AVTransport": {
        "SetAVTransportURI": (
            _INSTANCE, _in("CurrentURI", "AVTransportURI"),
            _in("CurrentURIMetaData", "AVTransportURIMetaData"), _CONTROL_SOURCE,
        ),
        "Play": (_INSTANCE, _in("Speed", "TransportPlaySpeed"), _CONTROL_SOURCE),
        "Pause": (_INSTANCE, _CONTROL_SOURCE),
        "Stop": (_INSTANCE, _CONTROL_SOURCE),
        "Next": (_INSTANCE, _CONTROL_SOURCE),
        "Previous": (_INSTANCE, _CONTROL_SOURCE),
        "Seek": (
            _INSTANCE, _in("Unit", "A_ARG_TYPE_SeekMode"),
            _in("Target", "A_ARG_TYPE_SeekTarget"), _CONTROL_SOURCE,
        ),
        "SeekForward": (_INSTANCE, _CONTROL_SOURCE),
        "SeekBackward": (_INSTANCE, _CONTROL_SOURCE),
        "SetPlayMode": (_INSTANCE, _in("NewPlayMode", "CurrentPlayMode"), _CONTROL_SOURCE),
        "GetInfoEx": (_INSTANCE,) + _POSITION_OUTPUTS,
        "GetPositionInfo": (_INSTANCE,) + _POSITION_OUTPUTS,
        "GetMediaInfo": (
            _INSTANCE,
            _out("NrTracks", "NumberOfTracks"),
            _out("MediaDuration", "CurrentMediaDuration"),
            _out("CurrentURI", "AVTransportURI"),
            _out("CurrentURIMetaData", "AVTransportURIMetaData"),
            _out("NextURI", "NextAVTransportURI"),
            _out("NextURIMetaData", "NextAVTransportURIMetaData"),
            _out("TrackSource"),
            _out("StreamCapability"),
            _out("PlayMedium", "PlaybackStorageMedium"),
            _out("RecordMedium", "RecordStorageMedium"),
            _out("WriteStatus", "RecordMediumWriteStatus"),
        ),
        "GetTransportInfo": (
            _INSTANCE, _out("CurrentTransportState", "TransportState"),
            _out("CurrentTransportStatus", "TransportStatus"),
            _out("CurrentSpeed", "TransportPlaySpeed"),
        ),
        "GetCurrentTransportActions": (_INSTANCE, _out("Actions", "CurrentTransportActions")),
    },
    "RenderingControl": {
        "GetVolume": (_INSTANCE, _CHANNEL, _out("CurrentVolume", "Volume")),
        "SetVolume": (_INSTANCE, _CHANNEL, _in("DesiredVolume", "Volume"), _CONTROL_SOURCE),
        "GetMute": (_INSTANCE, _CHANNEL, _out("CurrentMute", "Mute")),
        "SetMute": (_INSTANCE, _CHANNEL, _in("DesiredMute", "Mute"), _CONTROL_SOURCE),
        "GetChannel": (_INSTANCE, _out("CurrentChannel")),
        "ListPresets": (_INSTANCE, _out("CurrentPresetNameList", "PresetNameList")),
        "StreamServicesCapability": (
            _INSTANCE, _in("AppVersion", "A_ARG_TYPE_AppVersion"), _out("StreamCapability"),
        ),
        "GetControlDeviceInfo": (
            _INSTANCE, _out("MultiType"), _out("PlayMode", "DevicePlayMode"),
            _out("Router"), _out("Ssid"), _out("SlaveMask"),
            _out("CurrentVolume", "Volume"), _out("CurrentMute", "Mute"), _out("GainConfirmed"),
            _out("CurrentChannel"), _out("SlaveList"), _out("StreamCapability"),
            _out("Status", "DeviceStatus"),
        ),
    },
    "ConnectionManager": {
        "GetProtocolInfo": (_out("Source", "SourceProtocolInfo"), _out("Sink", "SinkProtocolInfo")),
        "GetCurrentConnectionIDs": (_out("ConnectionIDs", "CurrentConnectionIDs"),),
    },
    "PlayQueue": {
        # The optional requested name belongs inside QueueContext.  QueueName
        # is the actual selected name in the response, not an in/out duplicate.
        "CreateQueue": (_in("QueueContext"), _out("QueueName", "CurrentPlayListName")),
        "ReplaceQueue": (_in("QueueContext"), _out("QueueName", "CurrentPlayListName")),
        "PlayQueueWithIndex": (_in("QueueName", "CurrentPlayListName"), _in("Index", "A_ARG_TYPE_QueueIndex")),
        # Paging is not implemented: both browse actions return the full queue.
        "BrowseQueue": (_in("QueueName", "CurrentPlayListName"), _out("QueueContext")),
        "BrowseQueueEx": (_in("QueueName", "CurrentPlayListName"), _out("QueueContext")),
        "GetQueueIndex": (
            _in("QueueName", "CurrentPlayListName"), _out("Count"), _out("CurrentIndex"),
            _out("PreloadingIndex"), _out("CurrentPage"), _out("TrackNums", "Count"),
        ),
        "GetQueueLoopMode": (_in("QueueName", "CurrentPlayListName"), _out("LoopMode")),
        "SetQueueLoopMode": (_in("QueueName", "CurrentPlayListName"), _in("LoopMode")),
        "GetKeyMapping": (_in("QueueName", "CurrentPlayListName"), _out("QueueContext")),
        "GetUserInfo": (
            _in("AccountSource", "A_ARG_TYPE_AccountSource"),
            _in("Version", "A_ARG_TYPE_ServiceVersion"),
            _in("RefreshToken", "A_ARG_TYPE_RefreshToken"),
            _out("Result", "A_ARG_TYPE_UserInfo"),
        ),
        "UserLogin": (
            _in("AccountSource", "A_ARG_TYPE_LoginSource"),
            _in("Code", "A_ARG_TYPE_LoginCode"), _out("Result", "A_ARG_TYPE_UserInfo"),
        ),
    },
}

_VARIABLES: dict[str, _Variable] = {
    "A_ARG_TYPE_InstanceID": _Variable("ui4", value_range=(0, 0, 1), default="0"),
    "A_ARG_TYPE_Channel": _Variable(allowed_values=("Master", "Single"), default="Master"),
    "A_ARG_TYPE_ControlSource": _Variable(),
    "A_ARG_TYPE_AppVersion": _Variable(),
    "A_ARG_TYPE_SeekMode": _Variable(allowed_values=("REL_TIME",)),
    "A_ARG_TYPE_SeekTarget": _Variable(),
    "A_ARG_TYPE_QueueIndex": _Variable("ui4", value_range=(1, 4294967295, 1)),
    "A_ARG_TYPE_AccountSource": _Variable(),
    "A_ARG_TYPE_ServiceVersion": _Variable(),
    "A_ARG_TYPE_RefreshToken": _Variable(),
    "A_ARG_TYPE_UserInfo": _Variable(),
    "A_ARG_TYPE_LoginSource": _Variable(allowed_values=("Plex",)),
    "A_ARG_TYPE_LoginCode": _Variable(),
    "Volume": _Variable("ui2", value_range=(0, 100, 1)),
    "Mute": _Variable("boolean"),
    "GainConfirmed": _Variable("boolean"),
    # GetChannel/GetControlDeviceInfo use a numeric extension value, whereas
    # GetInfoEx uses Master.  Do not constrain this to the input Channel list.
    "CurrentChannel": _Variable(),
    "TransportState": _Variable(allowed_values=("STOPPED", "PLAYING", "PAUSED_PLAYBACK", "TRANSITIONING")),
    "TransportStatus": _Variable(allowed_values=("OK", "ERROR_OCCURRED")),
    "TransportPlaySpeed": _Variable(allowed_values=("1",), default="1"),
    "CurrentPlayMode": _Variable(allowed_values=("NORMAL",), default="NORMAL"),
    "LoopMode": _Variable("ui4", value_range=(0, 0, 1), default="0"),
    "AVTransportURI": _Variable("uri"),
    "AVTransportURIMetaData": _Variable(),
    "NextAVTransportURI": _Variable("uri"),
    "NextAVTransportURIMetaData": _Variable(),
    "CurrentTrack": _Variable("ui4"),
    "NumberOfTracks": _Variable("ui4"),
    "CurrentTrackURI": _Variable("uri"),
    "CurrentTrackMetaData": _Variable(),
    "CurrentTrackDuration": _Variable(),
    "CurrentMediaDuration": _Variable(),
    "RelativeTimePosition": _Variable(),
    "AbsoluteTimePosition": _Variable(),
    "RelativeCounterPosition": _Variable("i4"),
    "AbsoluteCounterPosition": _Variable("i4"),
    "PlaybackStorageMedium": _Variable(),
    "RecordStorageMedium": _Variable(allowed_values=("NOT_IMPLEMENTED",)),
    "RecordMediumWriteStatus": _Variable(allowed_values=("NOT_IMPLEMENTED",)),
    "CurrentTransportActions": _Variable(),
    "PlayType": _Variable("ui4"),
    "SlaveFlag": _Variable("boolean"),
    "SlaveList": _Variable(),
    "TrackSource": _Variable(),
    "StreamCapability": _Variable(),
    "InternetAccess": _Variable("boolean"),
    "VerUpdateFlag": _Variable("boolean"),
    "VerUpdateStatus": _Variable("ui4"),
    "BatteryFlag": _Variable("boolean"),
    "BatteryMains": _Variable("boolean"),
    "BatteryPercent": _Variable("ui1", value_range=(0, 100, 1)),
    "AlarmFlag": _Variable("boolean"),
    "TimeStamp": _Variable("ui4"),
    "SubNum": _Variable("ui4"),
    "SpotifyActive": _Variable("boolean"),
    "MultiType": _Variable("ui4"),
    "DevicePlayMode": _Variable("ui4", value_range=(0, 0, 1)),
    "Router": _Variable(),
    "Ssid": _Variable(),
    "SlaveMask": _Variable("ui4"),
    "DeviceStatus": _Variable(),
    "PresetNameList": _Variable(),
    "SourceProtocolInfo": _Variable(),
    "SinkProtocolInfo": _Variable(),
    "CurrentConnectionIDs": _Variable(),
    "QueueContext": _Variable(),
    "CurrentPlayListName": _Variable(),
    "Count": _Variable("ui4"),
    "CurrentIndex": _Variable("ui4"),
    "PreloadingIndex": _Variable("ui4"),
    "CurrentPage": _Variable("ui4"),
    "LastChange": _Variable(),
    "DeviceName": _Variable(),
    "GroupName": _Variable(),
    "CurrentTransportState": _Variable(),
    "TrackURI": _Variable("uri"),
    "TrackMetaData": _Variable(),
    "TrackTitle": _Variable(),
    "PlayMedium": _Variable(),
}

# Properties emitted directly by _event_body().  Values carried only inside
# LastChange retain sendEvents="no" unless they are also direct properties.
_EVENT_VARIABLES = {
    "AVTransport": {
        "LastChange", "TransportState", "CurrentTransportState", "CurrentTrackDuration",
        "RelativeTimePosition", "A_ARG_TYPE_SeekTarget", "TrackURI", "TrackMetaData",
        "TrackSource", "PlayMedium", "DeviceName", "GroupName",
    },
    "RenderingControl": {"LastChange", "Volume", "Mute", "DeviceName", "GroupName"},
    "ConnectionManager": set(),
    "PlayQueue": {
        "LastChange", "QueueContext", "CurrentPlayListName", "CurrentIndex", "LoopMode",
        "Count", "TrackSource", "TrackURI", "TrackTitle", "DeviceName", "GroupName",
    },
}


def scpd_xml(service_name: str, *, enable_plex: bool = False) -> str:
    """Render a service's implemented argument contract as a UPnP 1.0 SCPD.

    ``service_name`` is a canonical key from ``soap.SERVICE_TYPES``; aliases
    are resolved by HTTP routes.  Unknown services raise ``ValueError``.
    ``enable_plex`` only enables the implemented PlayQueue UserLogin action.
    Registry additions without a matching schema fail closed rather than
    silently publishing an action with an invented or empty signature.
    """
    if service_name not in SERVICE_TYPES:
        raise ValueError(f"Unknown UPnP service: {service_name}")
    advertised = READ_ACTIONS[service_name] | MUTATING_ACTIONS[service_name]
    if service_name == "PlayQueue" and not enable_plex:
        advertised = advertised - {"UserLogin"}
    missing = advertised - _ACTIONS[service_name].keys()
    if missing:
        raise ValueError(f"Missing SCPD contract for {service_name}: {', '.join(sorted(missing))}")

    root = ET.Element("scpd", {"xmlns": SCPD_NS})
    version = ET.SubElement(root, "specVersion")
    ET.SubElement(version, "major").text = "1"
    ET.SubElement(version, "minor").text = "0"
    action_list = ET.SubElement(root, "actionList")
    variables = set(_EVENT_VARIABLES[service_name])
    for name in sorted(advertised):
        action = ET.SubElement(action_list, "action")
        ET.SubElement(action, "name").text = name
        arguments = _ACTIONS[service_name][name]
        if arguments:
            argument_list = ET.SubElement(action, "argumentList")
            for spec in arguments:
                argument = ET.SubElement(argument_list, "argument")
                ET.SubElement(argument, "name").text = spec.name
                ET.SubElement(argument, "direction").text = spec.direction
                ET.SubElement(argument, "relatedStateVariable").text = spec.variable
                variables.add(spec.variable)

    state_table = ET.SubElement(root, "serviceStateTable")
    for name in sorted(variables):
        spec = _VARIABLES[name]
        variable = ET.SubElement(state_table, "stateVariable", {
            "sendEvents": "yes" if name in _EVENT_VARIABLES[service_name] else "no",
        })
        ET.SubElement(variable, "name").text = name
        ET.SubElement(variable, "dataType").text = spec.data_type
        if spec.default is not None:
            ET.SubElement(variable, "defaultValue").text = spec.default
        if spec.allowed_values:
            allowed = ET.SubElement(variable, "allowedValueList")
            for value in spec.allowed_values:
                ET.SubElement(allowed, "allowedValue").text = value
        if spec.value_range is not None:
            allowed = ET.SubElement(variable, "allowedValueRange")
            for tag, value in zip(("minimum", "maximum", "step"), spec.value_range):
                ET.SubElement(allowed, tag).text = str(value)
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
