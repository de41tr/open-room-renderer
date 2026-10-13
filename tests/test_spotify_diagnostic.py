"""Synthetic loopback contracts only; no provider client, account or audio."""
from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import json
import socket
import time

from aiohttp import web
import pytest

from fake_linkplay import cli
from fake_linkplay import spotify_diagnostic as diagnostic

SECRET = "synthetic-private-account-track-token"
STATUS = {
    "username": SECRET, "device_id": SECRET, "device_type": "speaker", "device_name": SECRET,
    "play_origin": None, "play_origin_device_id": SECRET, "context_uri": SECRET,
    "context_name": SECRET, "stopped": False, "paused": True, "buffering": False,
    "volume": 25, "volume_steps": 100, "repeat_context": False, "repeat_track": True,
    "shuffle_context": False, "track": {"name": SECRET, "artwork": SECRET}, "next_track": None,
}


@asynccontextmanager
async def peer(handler):
    requests = []

    async def dispatch(request):
        requests.append((request.method, request.path, dict(request.headers)))
        return await handler(request)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", dispatch)
    runner = web.AppRunner(app, shutdown_timeout=.05)
    await runner.setup()
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    site = web.SockSite(runner, server)
    await site.start()
    try:
        yield port, requests
    finally:
        await runner.cleanup()


async def ordinary(request):
    return web.json_response({"playback_ready": False} if request.path == "/" else STATUS)


def client(port):
    return diagnostic.SpotifyDiagnosticClient("127.0.0.1", port)


def test_only_canonical_numeric_loopback_and_explicit_port():
    assert diagnostic._endpoint("127.0.0.1", 1) == "http://127.0.0.1:1"
    assert diagnostic._endpoint("::1", 65535) == "http://[::1]:65535"
    for host in ("localhost", "127.1", "127.000.0.1", "0.0.0.0", "192.0.2.1", "::",
                 "::ffff:127.0.0.1", "::1%lo", "[::1]", "http://user:" + SECRET + "@127.0.0.1"):
        with pytest.raises(diagnostic.SpotifyDiagnosticError, match="Invalid Spotify"):
            diagnostic._endpoint(host, 3678)
    for port in (0, -1, 65536, True, "3678"):
        with pytest.raises(diagnostic.SpotifyDiagnosticError):
            diagnostic._endpoint("127.0.0.1", port)
    for seconds in ("nan", "inf", "-1", "31", True):
        with pytest.raises(diagnostic.SpotifyDiagnosticError):
            diagnostic._watch_seconds(seconds)


def test_cli_private_errors_preserve_legacy_favorite_argument_behavior(capsys):
    for args in (["spotify", "diagnose"], ["spotify", "diagnose", "--port", SECRET],
                 ["--bad-" + SECRET, "spotify", "diagnose"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(args)
        captured = capsys.readouterr()
        assert exc.value.code == 2 and SECRET not in captured.err
        assert captured.out == "" and captured.err.startswith("Invalid client command")
    with pytest.raises(SystemExit):
        cli.main(["favorites", "rename", "0" * 32, "spotify", "--port", "legacy-visible"])
    assert "legacy-visible" in capsys.readouterr().err
    assert cli.main(["spotify", "diagnose", "--port", "3678", "--host", "http://" + SECRET]) == 1
    assert SECRET not in capsys.readouterr().err


def test_status_projection_rejects_malformed_retained_fields_and_handles_zero_steps():
    for field, value in (("paused", 0), ("volume", True), ("volume", -1),
                         ("volume_steps", 2**32), ("buffering", None)):
        status = {**STATUS, field: value}
        with pytest.raises(diagnostic.SpotifyDiagnosticError):
            diagnostic._project_status(status)
    assert diagnostic._project_status({**STATUS, "volume_steps": 0})["volume_fraction"] is None
    assert SECRET not in json.dumps(diagnostic._project_status(STATUS))


def test_json_rejects_duplicates_depth_and_nonfinite_constants():
    for encoded in (b'{"playback_ready":true,"playback_ready":false}', b'{"ignored":NaN}',
                    b'{"ignored":1e999}', b'{"ignored":' + b'[' * 33 + b'0' + b']' * 33 + b'}'):
        with pytest.raises(diagnostic.SpotifyDiagnosticError):
            diagnostic._json_object(encoded)
    assert diagnostic._json_object(b'{"ignored":"[[[{\\\"}"}') == {"ignored": '[[[{"}'}


@pytest.mark.asyncio
async def test_real_loopback_snapshot_is_allowlisted_and_does_not_resolve_names(monkeypatch):
    async with peer(ordinary) as (port, requests):
        def no_dns(*_args, **_kwargs):
            raise AssertionError("Numeric-loopback diagnostics must not resolve DNS")
        monkeypatch.setattr(socket, "getaddrinfo", no_dns)
        result = await client(port).diagnose()
    assert [(method, path) for method, path, _ in requests] == [("GET", "/"), ("GET", "/status")]
    assert result["bootstrap_ready"] is False and result["session_observation"] == "SESSION_PRESENT"
    assert result["status"]["volume_fraction"] == .25
    assert result["snapshot_freshness"] == "snapshot_only"
    assert result["audio_output"] == result["shared_dsp"] == "unverified"
    assert result["events"]["channel"] == "not_requested"
    assert SECRET not in json.dumps(result)
    assert set(result["status"]) == {*diagnostic._BOOLEAN_FIELDS, "volume", "volume_steps", "volume_fraction"}


@pytest.mark.asyncio
async def test_204_is_no_session_even_when_bootstrap_ready():
    async def handler(request):
        return web.json_response({"playback_ready": True}) if request.path == "/" else web.Response(status=204)
    async with peer(handler) as (port, requests):
        result = await client(port).diagnose()
    assert result["bootstrap_ready"] is True
    assert result["session_observation"] == "NO_SESSION" and result["status"] is None
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect_path", ["/", "/status", "/events"])
async def test_http_and_websocket_redirects_never_reach_second_server(redirect_path):
    async with peer(ordinary) as (other_port, other_requests):
        async def handler(request):
            if request.path == redirect_path:
                return web.Response(status=302, headers={"Location": f"http://127.0.0.1:{other_port}/{SECRET}"})
            return await ordinary(request)
        async with peer(handler) as (port, requests):
            with pytest.raises(diagnostic.SpotifyDiagnosticError) as exc:
                await client(port).diagnose(.5 if redirect_path == "/events" else 0)
    assert other_requests == []
    assert "redirects are not permitted" in str(exc.value)
    assert requests[-1][1] == redirect_path
    assert all(method == "GET" for method, _, _ in requests)


@pytest.mark.asyncio
async def test_no_proxy_cookie_or_netrc_credentials(monkeypatch, tmp_path):
    async def handler(request):
        response = await ordinary(request)
        response.set_cookie("session", SECRET)
        return response
    async with peer(ordinary) as (proxy_port, proxy_requests):
        netrc = tmp_path / "netrc"
        netrc.write_text(f"machine 127.0.0.1 login {SECRET} password {SECRET}\n")
        monkeypatch.setenv("NETRC", str(netrc))
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy_port}")
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy_port}")
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setenv("no_proxy", "")
        async with peer(handler) as (port, requests):
            await client(port).diagnose()
    assert proxy_requests == []
    for _, _, headers in requests:
        assert not ({"Authorization", "Proxy-Authorization", "Cookie"} & headers.keys())
        assert headers["Accept-Encoding"] == "identity"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["oversize_chunked", "encoding", "duplicate", "wrong_root", "http_auth"])
async def test_untrusted_http_contracts_fail_without_returning_raw_data(failure):
    async def handler(request):
        if failure == "oversize_chunked":
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(b"x" * (diagnostic.MAX_BODY_BYTES + 1))
            await response.write_eof()
            return response
        if failure == "encoding":
            return web.Response(body=b"fake-gzip-" + SECRET.encode(), headers={"Content-Encoding": "gzip"})
        if failure == "duplicate":
            return web.Response(body=b'{"playback_ready":true,"playback_ready":false}')
        if failure == "wrong_root":
            return web.json_response({"playback_ready": SECRET})
        return web.Response(status=401, text=SECRET)
    async with peer(handler) as (port, _):
        with pytest.raises(diagnostic.SpotifyDiagnosticError) as exc:
            await client(port).diagnose()
    assert SECRET not in str(exc.value)


@pytest.mark.asyncio
async def test_http_read_timeout_is_bounded_and_private(monkeypatch):
    monkeypatch.setattr(diagnostic, "REQUEST_TIMEOUT", .1)
    monkeypatch.setattr(diagnostic, "READ_TIMEOUT", .03)
    async def handler(request):
        await asyncio.sleep(.2)
        return web.Response(text=SECRET)
    async with peer(handler) as (port, _):
        start = time.monotonic()
        with pytest.raises(diagnostic.SpotifyDiagnosticError, match="deadline exceeded"):
            await client(port).diagnose()
        assert time.monotonic() - start < .5


@pytest.mark.asyncio
async def test_events_are_only_hints_known_unknown_and_null_data_refresh_status():
    statuses = 0
    async def handler(request):
        nonlocal statuses
        if request.path == "/events":
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({"type": "playing", "data": {"paused": False, "raw_error": SECRET}})
            await ws.send_json({"type": SECRET, "data": None})
            await ws.receive()
            return ws
        if request.path == "/status":
            statuses += 1
        return await ordinary(request)
    async with peer(handler) as (port, requests):
        result = await client(port).diagnose(.08)
    assert result["status"]["paused"] is True  # Event did not mutate the snapshot.
    assert result["events"] == {"channel": "observed", "end_reason": "duration_limit",
        "messages_received": 2, "unknown_types": 1, "snapshot_refreshes": 4, "complete_history": False}
    assert statuses == 5
    assert SECRET not in json.dumps(result)
    assert {path for _, path, _ in requests} == {"/", "/status", "/events"}


@pytest.mark.asyncio
async def test_event_flood_has_message_and_request_budget(monkeypatch):
    monkeypatch.setattr(diagnostic, "MAX_EVENT_MESSAGES", 3)
    async def handler(request):
        if request.path != "/events":
            return await ordinary(request)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        for _ in range(20):
            await ws.send_json({"type": "volume", "data": {"secret": SECRET}})
        await ws.receive()
        return ws
    async with peer(handler) as (port, requests):
        result = await client(port).diagnose(.5)
    assert result["events"]["end_reason"] == "message_limit"
    assert result["events"]["messages_received"] == 3
    assert len(requests) == 8  # root + initial + WS + resync + three hints + final status


@pytest.mark.asyncio
async def test_websocket_disconnect_marks_last_snapshot_unknown_without_reconnect():
    async def handler(request):
        if request.path != "/events":
            return await ordinary(request)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.close(message=SECRET.encode())
        return ws
    async with peer(handler) as (port, requests):
        result = await client(port).diagnose(.5)
    assert result["snapshot_freshness"] == "unknown"
    assert result["events"]["channel"] == result["events"]["end_reason"] == "disconnected"
    assert len([path for _, path, _ in requests if path == "/events"]) == 1
    assert SECRET not in json.dumps(result)


def test_cli_emits_one_json_observation_and_fixed_runtime_failure(monkeypatch, capsys):
    async def success(_self, watch_seconds):
        assert watch_seconds == .5
        return {"observation": "sidecar_reported", "session_observation": "NO_SESSION"}
    monkeypatch.setattr(diagnostic.SpotifyDiagnosticClient, "diagnose", success)
    assert cli.main(["spotify", "diagnose", "--port", "3678", "--watch-seconds", ".5"]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and json.loads(captured.out)["session_observation"] == "NO_SESSION"
    async def failure(_self, _seconds):
        raise diagnostic.SpotifyDiagnosticError(diagnostic._REQUEST_FAILED)
    monkeypatch.setattr(diagnostic.SpotifyDiagnosticClient, "diagnose", failure)
    assert cli.main(["spotify", "diagnose", "--port", "3678"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == diagnostic._REQUEST_FAILED + "\n"


@pytest.mark.asyncio
async def test_http_total_deadline_bounds_a_slow_drip(monkeypatch):
    monkeypatch.setattr(diagnostic, "REQUEST_TIMEOUT", .08)
    monkeypatch.setattr(diagnostic, "READ_TIMEOUT", .05)
    async def handler(request):
        response = web.StreamResponse()
        await response.prepare(request)
        try:
            for _ in range(30):
                await response.write(b" ")
                await asyncio.sleep(.015)  # Progress remains below the read-inactivity limit.
        except (ConnectionResetError, RuntimeError):
            pass
        return response
    async with peer(handler) as (port, _):
        start = time.monotonic()
        with pytest.raises(diagnostic.SpotifyDiagnosticError, match="deadline exceeded"):
            await client(port).diagnose()
        assert time.monotonic() - start < .25


@pytest.mark.asyncio
async def test_event_size_limit_or_binary_data_cannot_become_snapshot_data(monkeypatch):
    monkeypatch.setattr(diagnostic, "MAX_BODY_BYTES", 1024)
    for binary in (False, True):
        async def handler(request):
            if request.path != "/events":
                return await ordinary(request)
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            if binary:
                await ws.send_bytes(SECRET.encode())
            else:
                await ws.send_json({"type": "volume", "data": "x" * 2048})
            await ws.receive()
            return ws
        async with peer(handler) as (port, requests):
            if binary:
                with pytest.raises(diagnostic.SpotifyDiagnosticError, match="malformed"):
                    await client(port).diagnose(.5)
            else:
                result = await client(port).diagnose(.5)
                assert result["events"]["channel"] == "disconnected"
                assert result["snapshot_freshness"] == "unknown"
        assert len([path for _, path, _ in requests if path == "/events"]) == 1


@pytest.mark.asyncio
async def test_get_disconnect_transport_recovery_remains_bounded():
    async def handler(request):
        request.transport.close()
        return web.Response(text=SECRET)
    async with peer(handler) as (port, requests):
        start = time.monotonic()
        with pytest.raises(diagnostic.SpotifyDiagnosticError, match="not confirmed"):
            await client(port).diagnose()
        assert time.monotonic() - start < 1
    # aiohttp versions may recover one idempotent GET, even with force_close.
    # No application retry/reconnect loop and no other path may be attempted.
    assert 1 <= len(requests) <= 2
    assert all((method, path) == ("GET", "/") for method, path, _ in requests)
