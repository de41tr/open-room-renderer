"""Focused source diagnosis: fake backend, synthetic inputs and local 404 only."""
import asyncio
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.state import RendererState
from fake_linkplay.diagnostics import Diagnostics, MAX_BYTES, MAX_EVENTS
from test_soap_faults import FakeBackend, request

COMMIT="a"*40

class Backend(FakeBackend):
    async def snapshot(self):
        return {"idle-active":True,"pause":False,"track-list":[]}

@pytest.fixture
def api(tmp_path):
    cfg=Config(uuid="00000000-0000-4000-8000-000000000000",state_dir=str(tmp_path),
               advertise_ip="127.0.0.1",log_wiim_http_details=True)
    api=HttpApi(cfg,RendererState.from_config(cfg),Backend())
    api.diagnostics=Diagnostics(tmp_path,lambda:api.state,version="0.1.0",commit=COMMIT)
    now=dt.datetime.now(dt.timezone.utc)
    assert api.diagnostics.activate({"test_id":"synthetic-app-window","commit":COMMIT,
        "started_at":now.isoformat(),"expires_at":(now+dt.timedelta(seconds=120)).isoformat()})
    return api

def http(command):
    return SimpleNamespace(query={"command":command},remote="127.0.0.1",host="localhost",
        path="/httpapi.asp",path_qs="/httpapi.asp?command="+command,headers={},method="GET")

def finish_window(recorder):
    # Synthetic clock window ending now; do not wait 120 seconds or record real traffic.
    now=dt.datetime.now(dt.timezone.utc)
    recorder.window["started_at"]=(now-dt.timedelta(seconds=120)).isoformat()
    recorder.window["expires_at"]=now.isoformat()
    recorder.deadline=0
    assert not recorder.active()
    return json.loads(recorder.snapshot())

def assert_private(data, caplog):
    text=json.dumps(data)+caplog.text
    for marker in ("SECRET_MARKER","PRIVATE_URL","TOKEN_MARKER","private-title"):
        assert marker not in text

@pytest.mark.asyncio
async def test_real_rejection_acceptance_confirmation_and_unknown(api,caplog):
    rejected=await api.soap(request("CreateQueue",service="PlayQueue"))
    assert rejected.status==500 and "<errorCode>402</errorCode>" in rejected.text
    stopped=await api.soap(request("Stop"))
    assert stopped.status==200 and api.mpv.calls==[("stop",None)]
    unknown=await api.handle_httpapi(http("SECRET_MARKER:https://PRIVATE_URL/?token=TOKEN_MARKER"))
    assert unknown.status==400  # Existing response contract is retained.
    data=finish_window(api.diagnostics)
    rows=data["events"]
    assert any(e["action"]=="CreateQueue" and e["reason"]=="queue-context-missing" and
               e["http_code"]==500 and e["soap_code"]==402 and e["outcome"]=="rejected" for e in rows)
    stop=[e for e in rows if e["action"]=="Stop"]
    assert any(e["outcome"]=="accepted" for e in stop)
    assert any(e["stage"]=="backend" and e["outcome"]=="completed" and e["reason"]=="backend_transport_confirmed" for e in stop)
    assert any(e["stage"]=="command" and e["outcome"]=="completed" and e["state_after"]=="STOPPED" for e in stop)
    assert any(e["action"]=="unknown" and e["outcome"]=="unknown" for e in rows)
    assert_private(data,caplog)
    before=next(e["peer"] for e in rows if e["action"]=="unknown")
    now=dt.datetime.now(dt.timezone.utc)
    assert api.diagnostics.activate({"test_id":"rotated-synthetic-window","commit":COMMIT,
        "started_at":now.isoformat(),"expires_at":(now+dt.timedelta(seconds=120)).isoformat()})
    trace=api.diagnostics.begin("http",http("getStatusEx"))
    assert trace.peer!=before  # Peer labels cannot link devices across test windows.

@pytest.mark.asyncio
async def test_poll_storm_keeps_mutations_errors_states_and_pending_reads(api,caplog):
    for i in range(5000):
        if i==2500: assert (await api.soap(request("CreateQueue",service="PlayQueue"))).status==500
        if i==3500: assert (await api.soap(request("Stop"))).status==200
        if i==4000: await api.handle_httpapi(http("SECRET_MARKER:TOKEN_MARKER"))
        assert (await api.soap(request("BrowseQueue",service="PlayQueue"))).status==200
    # Incomplete read is not silently erased by successful-poll compaction.
    pending=api.diagnostics.begin("soap",request("GetQueueIndex",service="PlayQueue"))
    data=finish_window(api.diagnostics)
    assert sum(e["count"] for e in data["events"] if e["action"]=="BrowseQueue" and e["outcome"]=="completed")==5000
    assert any(e["action"]=="CreateQueue" and e["soap_code"]==402 for e in data["events"])
    assert any(e["action"]=="Stop" and e["outcome"]=="accepted" for e in data["events"])
    assert any(e["stage"]=="state" and e["generation_after"]==1 for e in data["events"])
    assert any(e["request_id"]==pending.request_id and e["outcome"]=="unknown" for e in data["events"])
    assert data["statistics"]["compacted_events"]>=9000
    assert data["statistics"]["dropped_events"]==data["statistics"]["overwritten_events"]==0
    assert len(data["events"])<40 and len(json.dumps(data).encode())<MAX_BYTES
    assert_private(data,caplog)

@pytest.mark.asyncio
async def test_backend_uncertainty_and_diagnostic_failure_do_not_change_commands(api,caplog,monkeypatch):
    api.mpv.failure=True
    response=await api.soap(request("Stop"))
    assert response.status==500
    rows=api.diagnostics.events
    assert any(e["action"]=="Stop" and e["stage"]=="command" and e["outcome"]=="unknown"
               and e["soap_code"]==501 and e["reason"]=="backend_operation_failed" for e in rows)
    assert not any(e["reason"]=="backend_transport_confirmed" for e in rows)
    api.execute=AsyncMock(side_effect=RuntimeError("SECRET_MARKER https://PRIVATE_URL/?token=TOKEN_MARKER"))
    assert (await api.handle_httpapi(http("getPlayerStatus"))).status==500
    api.mpv.failure=False
    original=api._execute_soap_side_effect
    def broken(*_args,**_kwargs): raise OSError("SECRET_MARKER")
    monkeypatch.setattr(api.diagnostics,"add",broken)
    api._execute_soap_side_effect=original
    assert (await api.soap(request("Stop"))).status==200
    assert api.mpv.calls==[("stop",None),("stop",None)]
    assert_private(api.diagnostics.events,caplog)

@pytest.mark.asyncio
@pytest.mark.parametrize("stage,code", [
    ("page", "invalid_response"),
    ("page", "invalid_page_response"),
    ("page", "unsupported_page"),
    ("page", "unauthorized"),
    ("track", "unsupported_manifest"),
])
async def test_tidal_create_queue_terminal_reason_survives_backend_handler(api, tmp_path, caplog, stage, code):
    from fake_linkplay.tidal_playback import TidalPlaybackError
    from test_tidal_page import header_request
    from test_tidal_queue_playback import configured, create_explicit

    api.cfg.enable_plex_service = True  # Current root contract plays CreateQueue immediately.
    resolver = await configured(api, tmp_path)
    error = TidalPlaybackError(code)
    error.args = ("SECRET_MARKER https://PRIVATE_URL/?token=TOKEN_MARKER",)
    previous, generation, phase = api.state.queue, api.state.generation, api.state.phase
    if stage == "page":
        resolver.resolve_page = AsyncMock(side_effect=error)
        response = await api.soap(header_request(
            "https://api.tidal.com/v1/users/42/favorites/tracks?countryCode=DE&offset=0&limit=50"))
        resolver.resolve_page.assert_awaited_once()
        assert resolver.calls == []
    else:
        resolver.error = error
        response = await create_explicit(api)
        assert resolver.calls == [("43", "LOSSLESS")]

    assert response.status == 500 and "<errorCode>501</errorCode>" in response.text
    assert api.state.queue is previous and api.state.generation == generation and api.state.phase == phase
    assert api.mpv.calls == []
    data = finish_window(api.diagnostics)
    terminal = [e for e in data["events"] if e["action"] == "CreateQueue" and e["stage"] == "command"
                and e["http_code"] == 500]
    assert len(terminal) == 1
    assert terminal[0]["reason"] == "tidal_" + code
    assert terminal[0]["soap_code"] == 501 and terminal[0]["outcome"] == "unknown"
    assert_private(data, caplog)
    assert "SECRET_MARKER" not in response.text

@pytest.mark.asyncio
async def test_tidal_reason_rejects_unrecognized_code_without_exporting_text(api, caplog):
    from fake_linkplay.http_api import TidalPlaybackFailure

    error = TidalPlaybackFailure("SECRET_MARKER:https://PRIVATE_URL/?token=TOKEN_MARKER")
    api.execute = AsyncMock(side_effect=error)
    response = await api.handle_httpapi(http("setPlayerCmd:play:https://PRIVATE_URL/TOKEN_MARKER"))
    assert response.status == 502
    row = api.diagnostics.events[-1]
    assert row["reason"] == "unknown" and row["outcome"] == "unknown"
    assert_private(api.diagnostics.events, caplog)

@pytest.mark.asyncio
async def test_private_snapshot_expiry_restart_and_bounded_overwrite(api,tmp_path):
    # Normal off-loop publication and automatic end, no extra renderer operations.
    await api.diagnostics.pump()
    saved=tmp_path/"diagnostic-buffer.json"
    assert saved.stat().st_mode&0o777==0o600
    restarted=Diagnostics(tmp_path,lambda:api.state,version="0.1.0",commit=COMMIT)
    lease={k:api.diagnostics.window[k] for k in ("test_id","commit","started_at","expires_at")}
    (tmp_path/"diagnostic-window.json").write_text(json.dumps(lease))
    (tmp_path/"diagnostic-window.json").chmod(0o600)
    before=saved.read_bytes()
    await restarted.pump()
    assert restarted.window is None and saved.read_bytes()==before
    # Pressure can overwrite old records; it must be explicit and bounded.
    for _ in range(MAX_EVENTS+20):
        await api.soap(request("CreateQueue",service="PlayQueue"))
    data=finish_window(api.diagnostics)
    assert len(data["events"])<=MAX_EVENTS and data["statistics"]["overwritten_events"]>0
    await api.diagnostics.pump()
    assert saved.stat().st_size<=MAX_BYTES and json.loads(saved.read_bytes())["complete"]
    assert api.mpv.calls==[]

    pending=asyncio.create_task(asyncio.Event().wait())
    api.diagnostics.task=pending
    api.diagnostics.close()
    with pytest.raises(asyncio.CancelledError):
        await pending

@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["web", "httpasp"])
async def test_local_404_source_keeps_context_without_urls_or_real_player(api, caplog, entry):
    """Real loopback 404 + command handlers; a fake backend replaces mpv/audio."""
    from aiohttp import ClientSession, web
    from test_web_ui import serving, state, command

    fetched = []
    async def missing(request):
        fetched.append(request.method)
        return web.Response(status=404, text="SECRET_MARKER PRIVATE_URL TOKEN_MARKER")

    source_app = web.Application()
    source_app.router.add_get("/SECRET_MARKER", missing)
    source = web.AppRunner(source_app, access_log=None)
    await source.setup()
    site = web.TCPSite(source, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/SECRET_MARKER?token=TOKEN_MARKER"

    class Http404Backend(Backend):
        async def loadfile(self, url, options=None, *, start_paused=False):
            # Simulate mpv's acknowledged load followed by failed source opening.
            # This request is the only source I/O; no real backend/device exists.
            async with ClientSession(trust_env=False) as session:
                async with session.get(url, allow_redirects=False) as response:
                    assert response.status == 404
            return await super().loadfile(url, options, start_paused=start_paused)

        async def snapshot(self):
            return {**await super().snapshot(), "path": self.url}

    api.mpv = Http404Backend()
    api.cfg.proxy_on_demand_audio = False
    readiness = api._wait_for_mpv_audio_ready
    async def short_readiness(generation, url):
        # Same production readiness path; shorten only its existing test deadline.
        return await readiness(generation, url, timeout=0.04)
    api._wait_for_mpv_audio_ready = short_readiness
    try:
        with caplog.at_level("INFO", logger="fake_linkplay.http_api"):
            async with serving(api) as (session, base):
                if entry == "web":
                    api.favorites.add({"name": "private-title", "url": url})
                    snapshot = await state(session, base)
                    favorite = api.web_api.favorites_snapshot()["favorites"][0]["id"]
                    code, body = await command(session, base, snapshot["csrf_token"],
                                              {"action": "favorite_play", "id": favorite})
                    assert code == 502 and body["outcome"] == "UNKNOWN"
                else:
                    async with session.get(base + "/httpapi.asp",
                            params={"command": "setPlayerCmd:play:" + url}) as response:
                        assert response.status == 502 and await response.text() == "Failed"
    finally:
        await source.cleanup()

    assert fetched == ["GET"]
    assert [name for name, _ in api.mpv.calls] == ["load", "stop"]
    data = finish_window(api.diagnostics)
    endpoint, action = ("web_control", "favorite_play") if entry == "web" else ("linkplay_http", "play")
    final = [e for e in data["events"] if e["endpoint"] == endpoint
             and e["action"] == action and e["stage"] == "command" and e["http_code"] == 502]
    assert len(final) == 1
    result = final[0]
    assert result["reason"] == "source_unready" and result["outcome"] == "unknown"
    assert result["state_after"] == "ERROR"
    assert result["generation_after"] == api.state.generation > result["generation_before"]
    related = [e for e in data["events"] if e["request_id"] == result["request_id"]]
    assert any(e["outcome"] == "accepted" for e in related)
    assert any(e["stage"] == "playback" and e["reason"] == "source_unready" for e in related)
    assert not any(e["reason"] in {"backend_audio_ready_observed", "backend_transport_confirmed"} for e in related)
    if entry == "httpasp":
        assert result["subcommand"] == "play"
    assert_private(data, caplog)
    assert url not in json.dumps(data) + caplog.text
    assert "http://" not in json.dumps(data) + caplog.text
    print("LOCAL_404_DIAG " + json.dumps({k: result[k] for k in
          ("protocol", "endpoint", "action", "subcommand", "stage", "outcome", "reason",
           "http_code", "soap_code", "state_before", "state_after", "generation_after")}))

@pytest.mark.asyncio
async def test_fixed_source_categories_and_unknown_web_command(api, caplog):
    from fake_linkplay.http_api import SourcePlaybackError
    from fake_linkplay import diagnostics as diagnostic
    from test_web_ui import serving, state, command

    for category in ("source_resolution", "source_unready", "source_eof", "SECRET_MARKER:https://PRIVATE_URL"):
        api.execute = AsyncMock(side_effect=SourcePlaybackError("SECRET_MARKER TOKEN_MARKER", category))
        response = await api.handle_httpapi(http("setPlayerCmd:play:https://PRIVATE_URL/TOKEN_MARKER"))
        assert response.status == 502
        row = api.diagnostics.events[-1]
        expected = category if category.startswith("source_") else "unknown"
        assert row["reason"] == expected and row["outcome"] == "unknown"
    # Web input must be decoded before a fixed action label is recorded.
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        code, _ = await command(session, base, snapshot["csrf_token"],
                                {"action": "SECRET_MARKER:https://PRIVATE_URL/TOKEN_MARKER"})
        assert code == 400
        row = api.diagnostics.events[-1]
        assert row["endpoint"] == "web_control" and row["action"] == "unknown"
        assert row["outcome"] == "unknown"
        # Same generic web catch preserves each authored source category, too.
        for category in ("source_resolution", "source_unready", "source_eof", "SECRET_MARKER"):
            api.web_api._dispatch = AsyncMock(side_effect=SourcePlaybackError("TOKEN_MARKER", category))
            code, body = await command(session, base, snapshot["csrf_token"], {"action": "stop"})
            assert code == 502 and body["outcome"] == "UNKNOWN"
            row = api.diagnostics.events[-1]
            assert row["action"] == "stop" and row["outcome"] == "unknown"
            assert row["reason"] == (category if category.startswith("source_") else "unknown")
    assert diagnostic.source_reason(["SECRET_MARKER"]) is diagnostic.Reason.UNKNOWN
    assert_private(api.diagnostics.events, caplog)
