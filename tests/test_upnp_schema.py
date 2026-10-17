"""UNIT/MOCK: implemented SCPD contracts, not Android or audio acceptance."""
from html import escape
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET

import pytest
from aiohttp.test_utils import make_mocked_request

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.soap import MUTATING_ACTIONS, READ_ACTIONS, SERVICE_TYPES, SOAP_NS, parse_request
from fake_linkplay.state import RendererState
from fake_linkplay.upnp_schema import SCPD_NS, scpd_xml


NS = {"s": SCPD_NS}
SERVICES = tuple(SERVICE_TYPES)
READS = [(service, action) for service in SERVICES for action in sorted(READ_ACTIONS[service])]


def contract(service, *, enable_plex=False, include_control_source=False):
    root = ET.fromstring(scpd_xml(service, enable_plex=enable_plex,
                                  include_control_source=include_control_source))
    actions = {
        node.findtext("s:name", namespaces=NS): node
        for node in root.findall("s:actionList/s:action", NS)
    }
    variables = {
        node.findtext("s:name", namespaces=NS): node
        for node in root.findall("s:serviceStateTable/s:stateVariable", NS)
    }
    return root, actions, variables


def arguments(action, direction=None):
    result = []
    for node in action.findall("s:argumentList/s:argument", NS):
        item = (
            node.findtext("s:name", namespaces=NS),
            node.findtext("s:direction", namespaces=NS),
            node.findtext("s:relatedStateVariable", namespaces=NS),
        )
        if direction is None or direction == item[1]:
            result.append(item)
    return result


def variable_for(service, action, argument, direction, *, enable_plex=False, include_control_source=False):
    _, actions, variables = contract(service, enable_plex=enable_plex,
                                      include_control_source=include_control_source)
    return variables[next(ref for name, _, ref in arguments(actions[action], direction) if name == argument)]


def allowed_values(variable):
    return [node.text for node in variable.findall("s:allowedValueList/s:allowedValue", NS)]


def value_range(variable):
    return tuple(variable.findtext(f"s:allowedValueRange/s:{name}", namespaces=NS)
                 for name in ("minimum", "maximum", "step"))


def assert_typed_value(variable, value):
    data_type = variable.findtext("s:dataType", namespaces=NS)
    if data_type == "boolean":
        assert value in {"0", "1", "true", "false", "yes", "no"}
    elif data_type in {"ui1", "ui2", "ui4", "i4"}:
        limits = {"ui1": (0, 255), "ui2": (0, 65535), "ui4": (0, 4294967295),
                  "i4": (-2147483648, 2147483647)}
        lower, upper = limits[data_type]
        assert lower <= int(value) <= upper
    allowed = allowed_values(variable)
    if allowed:
        assert value in allowed
    lower, upper, step = value_range(variable)
    if lower is not None:
        assert int(lower) <= int(value) <= int(upper)
        assert (int(value) - int(lower)) % int(step) == 0


@pytest.mark.parametrize("service", SERVICES)
@pytest.mark.parametrize("enable_plex", [False, True])
def test_scpds_are_parseable_and_match_shared_action_registry(service, enable_plex):
    root, actions, variables = contract(service, enable_plex=enable_plex)
    assert root.tag == f"{{{SCPD_NS}}}scpd"
    assert root.findtext("s:specVersion/s:major", namespaces=NS) == "1"
    assert root.findtext("s:specVersion/s:minor", namespaces=NS) == "0"
    expected = READ_ACTIONS[service] | MUTATING_ACTIONS[service]
    if service == "PlayQueue" and not enable_plex:
        expected -= {"UserLogin"}
    assert set(actions) == expected
    assert len(root.findall("s:actionList/s:action", NS)) == len(actions)
    assert len(root.findall("s:serviceStateTable/s:stateVariable", NS)) == len(variables)
    assert scpd_xml(service, enable_plex=enable_plex) == scpd_xml(service, enable_plex=enable_plex)


@pytest.mark.parametrize("service", SERVICES)
@pytest.mark.parametrize("enable_plex", [False, True])
def test_every_argument_has_direction_and_a_declared_typed_state_variable(service, enable_plex):
    _, actions, variables = contract(service, enable_plex=enable_plex)
    for action in actions.values():
        args = arguments(action)
        names = [name for name, _, _ in args]
        assert len(names) == len(set(names))
        directions = [direction for _, direction, _ in args]
        assert set(directions) <= {"in", "out"}
        assert directions == sorted(directions)  # All inputs precede outputs.
        for name, direction, ref in args:
            assert name and direction and ref in variables
            assert variables[ref].findtext("s:dataType", namespaces=NS) in {
                "string", "boolean", "ui1", "ui2", "ui4", "i4", "uri",
            }
    for variable in variables.values():
        assert variable.attrib["sendEvents"] in {"yes", "no"}
        assert not (variable.find("s:allowedValueList", NS) is not None
                    and variable.find("s:allowedValueRange", NS) is not None)
        default = variable.findtext("s:defaultValue", namespaces=NS)
        if default is not None:
            assert_typed_value(variable, default)


@pytest.mark.parametrize("service", SERVICES)
def test_unsupported_actions_are_never_advertised(service):
    _, actions, _ = contract(service, enable_plex=True)
    assert not set(actions) & {
        "GetStatus", "SetChannel", "SelectPreset", "SetQueuePolicy", "AppendQueue",
        "AppendTracksInQueue", "RemoveTracksInQueue",
        "SetNextAVTransportURI", "GetCurrentConnectionInfo",
    }
    assert "QPlay" not in scpd_xml(service, enable_plex=True)


def test_login_is_explicitly_plex_gated_and_has_only_implemented_arguments():
    _, default_actions, default_variables = contract("PlayQueue")
    _, plex_actions, plex_variables = contract("PlayQueue", enable_plex=True)
    assert set(plex_actions) - set(default_actions) == {"UserLogin"}
    assert arguments(plex_actions["UserLogin"]) == [
        ("AccountSource", "in", "A_ARG_TYPE_LoginSource"),
        ("Code", "in", "A_ARG_TYPE_LoginCode"),
        ("Result", "out", "A_ARG_TYPE_UserInfo"),
    ]
    assert allowed_values(plex_variables["A_ARG_TYPE_LoginSource"]) == ["Plex"]
    assert "A_ARG_TYPE_LoginCode" not in default_variables
    for service in SERVICES[:-1]:
        assert scpd_xml(service, enable_plex=True) == scpd_xml(service)


@pytest.mark.parametrize("action,argument,datatype", [
    ("SetVolume", "DesiredVolume", "ui2"), ("GetVolume", "CurrentVolume", "ui2"),
    ("SetMute", "DesiredMute", "boolean"), ("GetMute", "CurrentMute", "boolean"),
])
def test_android_rendering_inputs_use_actual_types_and_both_channels(action, argument, datatype):
    direction = "in" if action.startswith("Set") else "out"
    variable = variable_for("RenderingControl", action, argument, direction)
    assert variable.findtext("s:dataType", namespaces=NS) == datatype
    if datatype == "ui2":
        assert value_range(variable) == ("0", "100", "1")
    channel = variable_for("RenderingControl", action, "Channel", "in")
    assert allowed_values(channel) == ["Master", "Single"]
    instance = variable_for("RenderingControl", action, "InstanceID", "in")
    assert instance.findtext("s:dataType", namespaces=NS) == "ui4"
    assert value_range(instance) == ("0", "0", "1")


@pytest.mark.parametrize("service,action", [
    ("RenderingControl", "SetVolume"), ("RenderingControl", "SetMute"),
    *[("AVTransport", action) for action in sorted(MUTATING_ACTIONS["AVTransport"])],
])
def test_control_source_is_only_declared_on_legacy_scpds(service, action):
    _, actions, _ = contract(service)
    assert "ControlSource" not in [name for name, _, _ in arguments(actions[action], "in")]
    variable = variable_for(service, action, "ControlSource", "in", include_control_source=True)
    assert variable.findtext("s:dataType", namespaces=NS) == "string"
    assert not allowed_values(variable)


@pytest.mark.parametrize("channel", ["Master", "Single"])
def test_schema_driven_android_volume_request_passes_soap_framing(channel):
    _, actions, _ = contract("RenderingControl", include_control_source=True)
    values = {"InstanceID": "0", "Channel": channel, "DesiredVolume": "7", "ControlSource": "WiiMApp"}
    body = "".join(f"<{name}>{escape(values[name])}</{name}>"
                   for name, _, _ in arguments(actions["SetVolume"], "in"))
    namespace = SERVICE_TYPES["RenderingControl"]
    envelope = (f'<s:Envelope xmlns:s="{SOAP_NS}"><s:Body>'
                f'<u:SetVolume xmlns:u="{namespace}">{body}</u:SetVolume></s:Body></s:Envelope>')
    service, action, parsed = parse_request("rendercontrol1", f'"{namespace}#SetVolume"', envelope)
    assert (service, action) == ("RenderingControl", "SetVolume")
    assert ET.fromstring(parsed).findtext("ControlSource") == "WiiMApp"
    assert ET.fromstring(parsed).findtext("Channel") == channel


def test_only_implemented_play_modes_are_advertised():
    play_mode = variable_for("AVTransport", "SetPlayMode", "NewPlayMode", "in")
    assert allowed_values(play_mode) == ["NORMAL", "REPEAT_ONE", "REPEAT_ALL", "SHUFFLE"]
    seek_mode = variable_for("AVTransport", "Seek", "Unit", "in")
    assert allowed_values(seek_mode) == ["REL_TIME"]
    speed = variable_for("AVTransport", "Play", "Speed", "in")
    assert allowed_values(speed) == ["1"]
    for action, direction in [("SetQueueLoopMode", "in"), ("GetQueueLoopMode", "out")]:
        assert allowed_values(variable_for("PlayQueue", action, "LoopMode", direction)) == ["4", "1", "0", "3", "2"]
    _, actions, _ = contract("PlayQueue")
    assert arguments(actions["SetQueueLoopMode"]) == [("LoopMode", "in", "LoopMode")]
    assert arguments(actions["GetQueueLoopMode"]) == [("LoopMode", "out", "LoopMode")]
    _, actions, _ = contract("AVTransport")
    assert arguments(actions["GetTransportSettings"]) == [
        ("InstanceID", "in", "A_ARG_TYPE_InstanceID"),
        ("PlayMode", "out", "CurrentPlayMode"),
        ("RecQualityMode", "out", "CurrentRecordQualityMode"),
    ]
    assert allowed_values(variable_for("AVTransport", "GetTransportSettings", "RecQualityMode", "out")) == ["NOT_IMPLEMENTED"]
    all_xml = "".join(scpd_xml(service) for service in SERVICES).upper()
    assert "SHUFFLE" in all_xml and "RANDOM" not in all_xml


def test_extended_info_declares_all_implemented_outputs_and_client_fields():
    _, actions, _ = contract("AVTransport")
    names = [name for name, _, _ in arguments(actions["GetInfoEx"], "out")]
    assert names == [
        "Track", "TrackDuration", "TrackMetaData", "TrackURI", "RelTime", "AbsTime",
        "RelCount", "AbsCount", "CurrentTransportState", "CurrentTransportStatus",
        "CurrentSpeed", "LoopMode", "PlayType", "CurrentVolume", "CurrentMute", "GainConfirmed",
        "CurrentChannel", "SlaveFlag", "SlaveList", "PlayMedium", "TrackSource",
        "StreamCapability", "InternetAccess", "VerUpdateFlag", "VerUpdateStatus",
        "BatteryFlag", "BatteryMains", "BatteryPercent", "AlarmFlag", "TimeStamp",
        "SubNum", "SpotifyActive",
    ]
    assert arguments(actions["GetInfoEx"]) == arguments(actions["GetPositionInfo"])
    _, actions, _ = contract("RenderingControl")
    assert [name for name, _, _ in arguments(actions["GetControlDeviceInfo"], "out")] == [
        "MultiType", "PlayMode", "Router", "Ssid", "SlaveMask", "CurrentVolume",
        "CurrentMute", "GainConfirmed", "CurrentChannel", "SlaveList", "StreamCapability", "Status",
    ]


def test_queue_contract_has_identity_context_indexes_and_service_read_fields():
    _, actions, variables = contract("PlayQueue")
    for action in ("CreateQueue", "ReplaceQueue"):
        assert [(name, direction) for name, direction, _ in arguments(actions[action])] == [
            ("QueueContext", "in"), ("QueueName", "out"),
        ]
    for action in ("BrowseQueue", "BrowseQueueEx"):
        assert [(name, direction) for name, direction, _ in arguments(actions[action])] == [
            ("QueueName", "in"), ("QueueContext", "out"),
        ]
    assert [name for name, _, _ in arguments(actions["PlayQueueWithIndex"], "in")] == ["QueueName", "Index"]
    assert [name for name, _, _ in arguments(actions["GetQueueIndex"], "out")] == [
        "Count", "CurrentIndex", "PreloadingIndex", "CurrentPage", "TrackNums",
    ]
    assert value_range(variable_for("PlayQueue", "PlayQueueWithIndex", "Index", "in"))[0] == "1"
    assert [name for name, _, _ in arguments(actions["GetUserInfo"], "in")] == [
        "AccountSource", "Version", "RefreshToken",
    ]
    for _, _, ref in arguments(actions["GetUserInfo"]):
        assert variables[ref].findtext("s:dataType", namespaces=NS) == "string"
        assert not allowed_values(variables[ref])  # Empty Version/RefreshToken are valid.


def test_connection_manager_advertises_only_existing_read_responses():
    _, actions, _ = contract("ConnectionManager")
    assert arguments(actions["GetProtocolInfo"]) == [
        ("Source", "out", "SourceProtocolInfo"), ("Sink", "out", "SinkProtocolInfo"),
    ]
    assert arguments(actions["GetCurrentConnectionIDs"]) == [
        ("ConnectionIDs", "out", "CurrentConnectionIDs"),
    ]


@pytest.mark.parametrize("service", ["QPlay", "Unknown", "AVTransport1", "<invalid>"])
def test_unknown_services_do_not_get_fabricated_scpds(service):
    with pytest.raises(ValueError, match="Unknown UPnP service"):
        scpd_xml(service)


def test_new_registry_action_requires_an_explicit_contract(monkeypatch):
    monkeypatch.setitem(READ_ACTIONS, "ConnectionManager", READ_ACTIONS["ConnectionManager"] | {"UnimplementedRead"})
    with pytest.raises(ValueError, match="Missing SCPD contract.*UnimplementedRead"):
        scpd_xml("ConnectionManager")


def test_removed_registry_action_is_not_advertised(monkeypatch):
    monkeypatch.setitem(READ_ACTIONS, "ConnectionManager", {"GetProtocolInfo"})
    _, actions, variables = contract("ConnectionManager")
    assert set(actions) == {"GetProtocolInfo"}
    # Its direct event still declares the state variable without the action.
    assert variables["CurrentConnectionIDs"].attrib["sendEvents"] == "yes"


def make_api(tmp_path, *, enable_plex=False):
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", advertise_ip="127.0.0.1",
                 state_dir=str(tmp_path), enable_plex_service=enable_plex)
    api = HttpApi(cfg, RendererState.from_config(cfg), SimpleNamespace())
    api.sync_mpv_state = AsyncMock()
    return api


def soap_request(service, action, values):
    namespace = SERVICE_TYPES[service]
    fields = "".join(f"<{name}>{escape(value)}</{name}>" for name, value in values.items())
    body = (f'<s:Envelope xmlns:s="{SOAP_NS}"><s:Body><u:{action} xmlns:u="{namespace}">'
            f'{fields}</u:{action}></s:Body></s:Envelope>')
    return SimpleNamespace(match_info={"service": service},
                           headers={"SOAPACTION": f'"{namespace}#{action}"'},
                           text=AsyncMock(return_value=body))


@pytest.mark.parametrize("service,action", READS)
@pytest.mark.parametrize("enable_plex", [False, True])
async def test_scpd_read_outputs_match_actual_soap_response_order_and_types(tmp_path, service, action, enable_plex):
    api = make_api(tmp_path, enable_plex=enable_plex)
    _, actions, variables = contract(service, enable_plex=enable_plex)
    input_values = {"InstanceID": "0", "Channel": "Single", "AccountSource": "newTuneIn"}
    values = {name: input_values.get(name, "") for name, _, _ in arguments(actions[action], "in")}
    response = await api.soap(soap_request(service, action, values))
    assert response.status == 200
    node = ET.fromstring(response.text).find(f".//{{{SERVICE_TYPES[service]}}}{action}Response")
    assert node is not None
    outputs = arguments(actions[action], "out")
    assert [field.tag for field in node] == [name for name, _, _ in outputs]
    for field, (_, _, ref) in zip(node, outputs):
        assert_typed_value(variables[ref], field.text or "")


@pytest.mark.parametrize("action", ["CreateQueue", "ReplaceQueue"])
async def test_scpd_queue_creation_output_matches_actual_selected_name(tmp_path, action):
    api = make_api(tmp_path)
    context = "<PlayList><QueueName>Synthetic &amp; queue</QueueName><Tracks/></PlayList>"
    response = await api.soap(soap_request("PlayQueue", action, {"QueueContext": context}))
    assert response.status == 200
    node = ET.fromstring(response.text).find(f".//{{{SERVICE_TYPES['PlayQueue']}}}{action}Response")
    _, actions, _ = contract("PlayQueue")
    assert [field.tag for field in node] == [name for name, _, _ in arguments(actions[action], "out")]
    assert node.findtext("QueueName") == api.state.queue.name == "Synthetic & queue"


@pytest.mark.parametrize("service", SERVICES)
def test_direct_event_properties_are_declared_and_typed(tmp_path, service):
    api = make_api(tmp_path)
    root = ET.fromstring(api._event_body(service))
    fields = [field for property_node in root for field in property_node]
    _, _, variables = contract(service)
    advertised = {name for name, variable in variables.items() if variable.attrib["sendEvents"] == "yes"}
    assert {field.tag for field in fields} == advertised
    for field in fields:
        assert_typed_value(variables[field.tag], field.text or "")


@pytest.mark.parametrize("service,path", [
    ("AVTransport", "/upnp/AVTransport.xml"),
    ("AVTransport", "/upnp/rendertransportSCPD.xml"),
    ("RenderingControl", "/upnp/RenderingControl.xml"),
    ("RenderingControl", "/upnp/rendercontrolSCPD.xml"),
    ("ConnectionManager", "/upnp/ConnectionManager.xml"),
    ("ConnectionManager", "/upnp/renderconnmgrSCPD.xml"),
    ("PlayQueue", "/upnp/PlayQueue.xml"),
    ("PlayQueue", "/upnp/PlayQueueSCPD.xml"),
])
@pytest.mark.parametrize("enable_plex", [False, True])
async def test_scpd_routes_preserve_canonical_and_legacy_contracts(tmp_path, service, path, enable_plex):
    api = make_api(tmp_path, enable_plex=enable_plex)
    request = make_mocked_request("GET", path)
    match = await api.app().router.resolve(request)
    response = await match.handler(request)
    assert response.status == 200
    assert response.content_type == "application/xml"
    legacy = path in {"/upnp/rendertransportSCPD.xml", "/upnp/rendercontrolSCPD.xml"}
    assert response.text == scpd_xml(service, enable_plex=enable_plex, include_control_source=legacy)


@pytest.mark.parametrize("service,legacy_digest", [
    ("AVTransport", "121f7f2ef525706a84f3cccab110d5e381c4069656d305c5d3b45651439dd69f"),
    ("RenderingControl", "67b627bb31533f9eb8e7bfe655a905b2d38d37108a72bb84448543c2614aef4c"),
])
def test_legacy_scpd_bytes_are_preserved_and_canonical_only_removes_control_source(service, legacy_digest):
    # Exact compatibility alias contract published at d91140d. An intentional
    # future contract revision must review these hashes, rather than silently
    # changing the old vendor input requirements with the standard schema.
    legacy_xml = scpd_xml(service, include_control_source=True)
    assert sha256(legacy_xml.encode()).hexdigest() == legacy_digest
    legacy = ET.fromstring(legacy_xml)
    canonical = ET.fromstring(scpd_xml(service))
    for argument_list in legacy.findall("s:actionList/s:action/s:argumentList", NS):
        for argument in list(argument_list):
            if argument.findtext("s:name", namespaces=NS) == "ControlSource":
                argument_list.remove(argument)
    table = legacy.find("s:serviceStateTable", NS)
    for variable in list(table):
        if variable.findtext("s:name", namespaces=NS) == "A_ARG_TYPE_ControlSource":
            table.remove(variable)
    ET.indent(legacy)
    ET.indent(canonical)
    assert ET.tostring(legacy) == ET.tostring(canonical)


def test_tidal_search_append_declares_only_the_implemented_wire_form():
    _, actions, variables = contract("PlayQueue")
    assert arguments(actions["AppendTracksInQueueEx"]) == [
        ("QueueContext", "in", "QueueContext"),
        ("Action", "in", "A_ARG_TYPE_AppendAction"),
        ("Direction", "in", "A_ARG_TYPE_AppendDirection"),
        ("StartIndex", "in", "A_ARG_TYPE_QueueIndex"),
        ("Play", "in", "A_ARG_TYPE_AppendPlay"),
    ]
    assert allowed_values(variables["A_ARG_TYPE_AppendAction"]) == ["playnext"]
    for field in ("A_ARG_TYPE_AppendDirection", "A_ARG_TYPE_AppendPlay"):
        assert variables[field].findtext("s:dataType", namespaces=NS) == "ui4"
        assert allowed_values(variables[field]) == ["1"]
