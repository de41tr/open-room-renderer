"""In-memory Web admission and synthetic IPC; no native socket or audio proof."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from multidict import CIMultiDict
import pytest

from fake_linkplay import web_audio_output, web_ui
from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.mpv_backend import MpvBackend
from fake_linkplay.state import RendererState
from fake_linkplay.web_ui import PREFIX, WebControlApi
from test_audio_diagnostic import probe  # Synthetic transport fixture, never AF_UNIX.
from test_web_playlist_export import request as export_request
from test_web_ui import WebBackend

PATH = PREFIX + "/output/inspect"
SECRET = "synthetic-private-identifier"


def forbidden(*args, **kwargs):
    pytest.fail("Output inspection must not touch playback, state, files or dispatch")


def live(**changes):
    return {"connection": "connected", "socket_path_check": "unchanged_at_checks",
        "audio_device": {"status": "value", "value": "alsa/" + SECRET},
        "current_ao": {"status": "value", "value": "alsa"}, **changes}


@pytest.fixture
def control():
    inspect = AsyncMock(return_value=web_audio_output.output_summary(live(), "alsa/" + SECRET, True))
    api = WebControlApi(current_state=forbidden, device_name=forbidden, runtime=forbidden,
        dsp_snapshot=forbidden, favorites=SimpleNamespace(document=forbidden), dispatch=forbidden,
        inspect_output=inspect, allowed_hosts=("renderer.example.test",))
    return api, inspect


def request(api, body=None, **kwargs):
    if body is None:
        body = {"action": "inspect", "expected_session": api._session_id}
    return export_request(api, body, path=kwargs.pop("path", PATH), **kwargs)


def check(response, status, code=None):
    assert response.status == status
    body = json.loads(response.body)
    if code:
        assert body["error"] == code
        assert body["outcome"] != "UNKNOWN"
    assert SECRET.encode() not in response.body
    assert b"http" not in response.body and b"/tmp/" not in response.body
    for key, value in web_ui._HEADERS.items():
        assert response.headers[key] == value
    assert "Access-Control-Allow-Origin" not in response.headers
    return body


async def test_admitted_post_is_one_private_read_not_a_command_or_snapshot(control, monkeypatch):
    api, inspect = control
    monkeypatch.setattr(web_ui, "files", forbidden)
    response = await api.handle(request(api))
    body = check(response, 200)
    assert body["session_id"] == api._session_id
    assert body["comparison"] == "matches"
    assert body["configuration"]["identifier"] == body["live_intent"]["identifier"] == "hidden"
    assert body["configuration"]["source"] == "running_renderer"
    assert api._revision == 0 and not api._output_inspection_busy
    inspect.assert_awaited_once_with()


@pytest.mark.parametrize("method", ["GET", "HEAD", "PUT", "DELETE", "OPTIONS"])
async def test_inspection_requires_explicit_post(control, method):
    api, inspect = control
    check(await api.handle(request(api, method=method)), 405, "method_not_allowed")
    inspect.assert_not_awaited()


@pytest.mark.parametrize("body", [b"{}", b"[]", b"null", b"{", b'\xef\xbb\xbf{}',
    b'{"action":"inspect","action":"inspect","expected_session":"x"}',
    {"action": "inspect", "expected_session": "x", "socket_path": "/tmp/" + SECRET},
    {"action": "inspect", "expected_session": 7}, {"action": "inspect", "expected_session": "夜"},
    {"action": "diagnose", "expected_session": "x"}, {"action": "inspect"}])
async def test_exact_schema_rejected_before_probe(control, body):
    api, inspect = control
    check(await api.handle(request(api, body)), 400, "invalid_request")
    inspect.assert_not_awaited()


@pytest.mark.parametrize("session", ["", "older-session"])
async def test_session_rejected_before_probe(control, session):
    api, inspect = control
    check(await api.handle(request(api, {"action": "inspect", "expected_session": session})), 409, "stale_output_inspection")
    inspect.assert_not_awaited()


@pytest.mark.parametrize("edit,status,code", [
    (("Host", "evil.example.test:8099"), 403, "forbidden"),
    (("Host", "renderer.example.test:80"), 403, "forbidden"),
    (("Origin", "http://evil.example.test:8099"), 403, "forbidden"),
    (("Origin", None), 403, "forbidden"),
    (("X-ORR-CSRF", "wrong"), 403, "forbidden"),
    (("X-ORR-CSRF", None), 403, "forbidden"),
    (("Sec-Fetch-Site", "cross-site"), 403, "forbidden"),
    (("Content-Type", "text/plain"), 415, "unsupported_media_type"),
    (("Content-Encoding", "gzip"), 415, "unsupported_encoding"),
    (("Content-Length", None), 411, "length_required"),
    (("Content-Length", "32769"), 413, "request_too_large"),
    (("Transfer-Encoding", "chunked"), 411, "length_required"),
])
async def test_same_admission_boundary_and_limits(control, edit, status, code):
    api, inspect = control
    headers = CIMultiDict(request(api).headers)
    key, value = edit
    if key in headers: del headers[key]
    if value is not None: headers[key] = value
    check(await api.handle(request(api, headers=headers)), status, code)
    inspect.assert_not_awaited()


@pytest.mark.parametrize("header", ["Host", "Origin", "X-ORR-CSRF", "Content-Type", "Content-Length", "Sec-Fetch-Site"])
async def test_duplicate_admission_headers_never_probe(control, header):
    api, inspect = control
    headers = CIMultiDict(request(api).headers)
    if header not in headers: headers.add(header, "same-origin")
    headers.add(header, headers[header])
    response = await api.handle(request(api, headers=headers))
    assert response.status in {403, 411, 415}
    check(response, response.status)
    inspect.assert_not_awaited()


@pytest.mark.parametrize("path", [PATH + "?secret=" + SECRET, PATH + "/", PREFIX + "/output%2finspect"])
async def test_no_path_alias_or_query_probe(control, path):
    api, inspect = control
    response = await api.handle(request(api, path=path))
    assert response.status in {403, 404}
    check(response, response.status)
    inspect.assert_not_awaited()


async def test_body_timeout_or_truncation_does_not_claim_busy(control):
    api, inspect = control
    for error, status in [(asyncio.IncompleteReadError(b"", 10), 400), (TimeoutError(), 408)]:
        response = await api.handle(request(api, content=SimpleNamespace(readexactly=AsyncMock(side_effect=error))))
        check(response, status)
        assert not api._output_inspection_busy
    inspect.assert_not_awaited()


async def test_overlap_rejected_immediately_without_queue_and_claim_cleared(control):
    api, inspect = control
    started, finish = asyncio.Event(), asyncio.Event()
    summary = inspect.return_value
    async def pending():
        started.set()
        await finish.wait()
        return summary
    inspect.side_effect = pending
    first = asyncio.create_task(api.handle(request(api)))
    await started.wait()
    try:
        check(await asyncio.wait_for(api.handle(request(api)), 0.1), 409, "output_inspection_busy")
        assert inspect.await_count == 1 and api._output_inspection_busy
    finally:
        finish.set()
    check(await first, 200)
    assert not api._output_inspection_busy
    check(await api.handle(request(api)), 200)
    assert inspect.await_count == 2


async def test_failure_and_cancellation_clear_claim_without_command_outcome(control):
    api, inspect = control
    inspect.side_effect = RuntimeError(SECRET)
    check(await api.handle(request(api)), 503, "output_inspection_failed")
    assert not api._output_inspection_busy
    inspect.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await api.handle(request(api))
    assert not api._output_inspection_busy
    api._inspect_output = None
    check(await api.handle(request(api)), 503, "output_inspection_unavailable")


@pytest.mark.parametrize("identifier", ["alsa/" + SECRET, "https://example.test/?token=" + SECRET,
    "/tmp/" + SECRET, "file:///tmp/" + SECRET, "pulse/https://example.test/" + SECRET,
    "alsa/%2Ftmp%2F" + SECRET, SECRET, "auto"])
def test_identifiers_never_returned_even_printable_paths_urls_or_known_family(identifier):
    observed = live(audio_device={"status": "value", "value": identifier}, extra=SECRET)
    summary = web_audio_output.output_summary(observed, identifier, True)
    encoded = json.dumps(summary)
    assert SECRET not in encoded and "example.test" not in encoded and "/tmp/" not in encoded
    assert summary["comparison"] == "matches"
    assert summary["configuration"]["selection"] == "explicit_output"
    assert summary["live_intent"]["identifier"] == "hidden"
    observed["audio_device"]["value"] += "different"
    assert web_audio_output.output_summary(observed, identifier, True)["comparison"] == "differs"


def test_no_comparison_for_default_or_unstable_connection_and_null_is_explicit():
    summary = web_audio_output.output_summary(live(current_ao={"status": "value", "value": "null"}), None, False)
    assert summary["null_output"] == "deliberately_silent"
    assert summary["configuration"]["selection"] == "mpv_default"
    assert summary["configuration"]["spawn_policy"] == "external_only"
    assert summary["comparison"] == "not_compared"
    for changes in [{"socket_path_check": "changed"}, {"connection": "timed_out"},
                    {"audio_device": {"status": "unavailable"}}]:
        summary = web_audio_output.output_summary(live(**changes), "alsa/" + SECRET, True)
        assert summary["comparison"] == "not_compared"
    summary = web_audio_output.output_summary(live(current_ao={"status": "unavailable"}), "alsa/" + SECRET, True)
    assert summary["status"] == "complete" and summary["driver"]["status"] == "unavailable"
    assert summary["null_output"] == "not_observed"


def test_unexpected_raw_statuses_and_driver_names_are_never_echoed():
    summary = web_audio_output.output_summary(live(connection=SECRET, socket_path_check=SECRET,
        audio_device={"status": SECRET}, current_ao={"status": "value", "value": SECRET}), SECRET, True)
    assert SECRET not in json.dumps(summary)
    assert summary["status"] == "incomplete"
    assert summary["driver"]["family"] == "unrecognized"


async def test_scalar_callback_fresh_clients_only_and_config_not_reread(monkeypatch, tmp_path):
    clients = []
    def create(socket_path, *, auto_spawn):
        assert socket_path == "/tmp/" + SECRET and auto_spawn is False
        client = SimpleNamespace(diagnose_audio_output=AsyncMock(return_value=live()))
        clients.append(client)
        return client
    monkeypatch.setattr(web_audio_output, "MpvBackend", create)
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path),
        mpv_socket="/tmp/" + SECRET, mpv_audio_device="alsa/" + SECRET, auto_spawn_mpv=True)
    backend = WebBackend()
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)
    callback = api.web_api._inspect_output
    assert {type(cell.cell_contents) for cell in callback.__closure__} == {str, bool}
    cfg.mpv_socket = "/tmp/changed"; cfg.mpv_audio_device = "pulse/changed"; cfg.auto_spawn_mpv = False
    before = deepcopy(api.state)
    for _ in range(2):
        summary = await callback()
        assert summary["configuration"]["driver_family"] == "alsa"
        assert summary["configuration"]["spawn_policy"] == "auto_spawn_enabled"
    assert len(clients) == 2 and clients[0] is not clients[1]
    assert all(client.diagnose_audio_output.await_args.args == () for client in clients)
    assert api.state == before and backend.calls == []
    # Every listener routes through the identical adapter and its one busy flag.
    for app in (api.app(), api.app()):
        route = next(route for route in app.router.routes() if route.resource.canonical == PREFIX + "/{tail}")
        assert route.handler.__self__ is api.web_api


async def test_real_diagnostic_method_has_only_two_reads_and_busy_spans_cleanup(control, probe, monkeypatch):
    api, _ = control
    started, finish = asyncio.Event(), asyncio.Event()
    async def wait_closed():
        started.set()
        await finish.wait()
    probe.writer.wait_closed.side_effect = wait_closed
    for name in ("start", "stop", "command", "snapshot", "stop_process"):
        if hasattr(MpvBackend, name): monkeypatch.setattr(MpvBackend, name, forbidden)
    api._inspect_output = web_audio_output.make_output_inspector(socket_path="/tmp/" + SECRET,
        configured_output="alsa/dmix:default", auto_spawn=True)
    first = asyncio.create_task(api.handle(request(api)))
    await started.wait()
    try:
        assert probe.writer.closed and api._output_inspection_busy
        check(await api.handle(request(api)), 409, "output_inspection_busy")
        assert probe.connect.await_count == 1
        assert probe.writer.commands == [
            {"command": ["get_property", "audio-device"], "request_id": 1},
            {"command": ["get_property", "current-ao"], "request_id": 2},
        ]
    finally:
        finish.set()
    check(await first, 200)
    probe.writer.transport.abort.assert_called_once()
    assert not api._output_inspection_busy


async def test_cancellation_through_adapter_closes_transport_before_busy_release(control, probe):
    api, _ = control
    entered = asyncio.Event()
    async def read():
        entered.set()
        await asyncio.Event().wait()
    probe.reader.readline.side_effect = read
    api._inspect_output = web_audio_output.make_output_inspector(socket_path="/tmp/" + SECRET,
        configured_output=None, auto_spawn=False)
    task = asyncio.create_task(api.handle(request(api)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert probe.writer.closed and not api._output_inspection_busy
    probe.writer.transport.abort.assert_called_once()
