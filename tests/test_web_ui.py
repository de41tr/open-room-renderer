"""Browser boundary: UNIT and actual loopback HTTP + MOCK backend.

These tests do not execute browser JavaScript or decode/audio; the separate
opt-in native lane covers libmpv. No provider accounts or external requests.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from aiohttp import ClientSession, web
import pytest

from fake_linkplay.config import Config, PodcastPreset
from fake_linkplay.http_api import HttpApi, ParsedCommand
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.persistence import AtomicWriteCommittedError
from fake_linkplay.queue import PlaybackQueue, QueueItem
from fake_linkplay.state import PlaybackPhase, RendererState
from fake_linkplay.web_ui import (MAX_REQUEST_BYTES, PREFIX, WebAdmissionError,
                                   decode_command, safe_artwork)
from test_playback_generations import StatefulBackend


class WebBackend(StatefulBackend):
    def __init__(self):
        super().__init__()
        self.gain_confirmed = True
        self.balance = 0.0
        self.bands = []
        self.actual_volume = 50
        self.actual_mute = False
        self.dsp_error = None
        self._dsp_configuration_confirmed = True

    @property
    def confirmed_dsp(self):
        return (self.balance, deepcopy(self.bands)) if self._dsp_configuration_confirmed else None

    async def volume(self, value):
        self.calls.append(("volume", value))
        self.actual_volume = value

    async def mute(self, value):
        self.calls.append(("mute", value))
        self.actual_mute = value

    async def channel_balance(self, value):
        self.calls.append(("balance", value))
        if self.dsp_error:
            raise self.dsp_error
        self.balance = value

    async def peq(self, bands):
        self.calls.append(("peq", deepcopy(bands)))
        if self.dsp_error:
            raise self.dsp_error
        self.bands = deepcopy(bands)


@pytest.fixture
def api(tmp_path):
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path),
                 advertise_ip="127.0.0.1", default_volume=50)
    return HttpApi(cfg, RendererState.from_config(cfg), WebBackend())


@asynccontextmanager
async def serving(api):
    runner = web.AppRunner(api.app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    try:
        async with ClientSession(trust_env=False) as session:
            yield session, base
    finally:
        await runner.cleanup()
    assert api._state_monitor_task is None
    assert api._app_users == 0
    assert not api._seek_tasks


async def state(session, base):
    async with session.get(base + PREFIX + "/state") as response:
        assert response.status == 200
        return await response.json()


async def command(session, base, csrf, payload, **kwargs):
    headers = {"Origin": base, "X-ORR-CSRF": csrf, **kwargs.pop("headers", {})}
    async with session.post(base + PREFIX + "/command", json=payload, headers=headers, **kwargs) as response:
        return response.status, await response.json()


def selected(api, *, count=3, index=1, mode=0):
    api.state.queue = PlaybackQueue(items=[QueueItem(url=f"https://media.example.test/{i}.wav",
                    title=f"Song {i}", duration=30) for i in range(1, count + 1)], index=index, loop=mode)
    api._activate_queue_item()


async def test_real_http_bootstrap_is_detached_private_and_no_new_backend_work(api):
    selected(api)
    api.state.current_url = "https://private.example.test/media?token=STREAM_SECRET"
    api.state.backend_url = "https://private.example.test/backend?token=BACKEND_SECRET"
    api.state.last_error = "https://private.example.test/error?token=ERROR_SECRET"
    api.state.title = "song.wav?token=TITLE_SECRET"
    api.state.queue.current.title = api.state.title
    api.state.queue.current.title_is_fallback = True
    api.state.album_art_uri = "https://art.example.test/a.jpg?token=ART_SECRET"
    api.plex_user_info = {"accessToken": "ACCOUNT_SECRET"}
    async with serving(api) as (session, base):
        one = await state(session, base)
        two = await state(session, base)
        assert one["version"] == 1
        assert one["session_id"] == two["session_id"] != one["csrf_token"]
        assert two["revision"] > one["revision"]
        assert one["generation"] == api.state.generation
        assert one["media"]["title"] == one["queue"]["items"][0]["title"] == "Track 1"
        assert one["media"]["artwork"] is None
        assert one["dsp"] == {"status": "UNKNOWN", "balance": None, "eq_enabled": None, "name": "", "editor": None}
        assert "SECRET" not in json.dumps(one)
        assert not api.mpv.calls
        api.state.queue.current.title = "Changed"
        assert one["queue"]["items"][0]["title"] == "Track 1"


async def test_real_http_transport_seek_gain_modes_queue_and_stop(api):
    selected(api)
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        csrf = snapshot["csrf_token"]
        async def send(action, **fields):
            fresh = await state(session, base)
            if action in {"play", "pause", "next", "previous", "seek", "queue_select", "queue_mode"}:
                fields["expected_generation"] = fresh["generation"]
            code, body = await command(session, base, csrf, {"action": action, **fields})
            assert (code, body) == (200, {"request_acknowledged": True})
            return await state(session, base)
        assert (await send("play"))["transport"]["phase"] == "PLAYING"
        assert (await send("pause"))["transport"]["phase"] == "PAUSED"
        assert (await send("seek", seconds=7))["media"]["position_seconds"] == 7
        assert (await send("play"))["transport"]["phase"] == "PLAYING"
        assert (await send("volume", value=31))["gain"]["volume"] == 31
        assert (await send("mute", value=True))["gain"]["mute"] is True
        for mode in ("repeat-one", "repeat-all", "shuffle-once", "shuffle-repeat-all", "normal"):
            result = await send("queue_mode", queue_name="CurrentQueue", mode=mode)
            assert result["queue"]["mode"] == mode
        assert (await send("next"))["queue"]["index"] == 2
        assert (await send("previous"))["queue"]["index"] == 1
        assert (await send("queue_select", queue_name="CurrentQueue", index=3))["queue"]["index"] == 3
        assert (await send("stop"))["transport"]["phase"] == "STOPPED"
        assert api.mpv.actual_volume == 31 and api.mpv.actual_mute is True


async def test_real_http_pending_play_remains_responsive_and_stop_wins(api):
    selected(api)
    api.mpv.release_load = asyncio.Event()
    async with serving(api) as (session, base):
        before = await state(session, base)
        play = asyncio.create_task(command(session, base, before["csrf_token"],
                    {"action": "play", "expected_generation": before["generation"]}))
        await asyncio.wait_for(api.mpv.load_started.wait(), 1)
        pending = await asyncio.wait_for(state(session, base), 1)
        assert pending["transport"]["status"] == "PENDING"
        assert pending["media"]["position_seconds"] is None
        assert pending["actions"]["stop"] is True
        assert not play.done()
        stop = asyncio.create_task(command(session, base, before["csrf_token"], {"action": "stop"}))
        for _ in range(100):
            if api.state.intent == "stop":
                break
            await asyncio.sleep(.001)
        assert api.state.intent == "stop"
        api.mpv.release_load.set()
        assert (await play)[0] == 502
        assert (await stop) == (200, {"request_acknowledged": True})
        assert (await state(session, base))["transport"]["phase"] == "STOPPED"


@pytest.mark.parametrize("action,fields", [
    ("play", {}), ("pause", {}), ("next", {}), ("previous", {}), ("seek", {"seconds": 2}),
    ("queue_select", {"queue_name": "CurrentQueue", "index": 2}),
    ("queue_mode", {"queue_name": "CurrentQueue", "mode": "repeat-all"}),
    ("favorite_current", {"name": "Saved"}),
])
async def test_stale_generation_is_pre_dispatch_rejection(api, action, fields):
    selected(api)
    api.state.generation = 4
    async with serving(api) as (session, base):
        csrf = (await state(session, base))["csrf_token"]
        before = deepcopy(api.state.queue)
        code, body = await command(session, base, csrf, {"action": action, "expected_generation": 3, **fields})
        assert code == 409 and body["outcome"] == "REJECTED"
        assert body["error"] == "stale_generation"
        assert not api.mpv.calls
        assert api.state.queue == before
        assert api.favorites.document()["favorites"] == []


async def test_queue_identity_guard_survives_same_name_replacement_while_stop_waits(api):
    selected(api)
    await api.execute(ParsedCommand("resume"), "")
    started, release = asyncio.Event(), asyncio.Event()
    original_stop = api.mpv.stop
    async def slow_stop():
        started.set()
        await release.wait()
        await original_stop()
    api.mpv.stop = slow_stop
    async with serving(api) as (session, base):
        before = await state(session, base)
        task = asyncio.create_task(command(session, base, before["csrf_token"], {
            "action": "queue_select", "queue_name": "CurrentQueue", "index": 2,
            "expected_generation": before["generation"]}))
        await asyncio.wait_for(started.wait(), 1)
        replacement = PlaybackQueue(items=[QueueItem(url="https://media.example.test/replacement.wav")], index=1)
        api.state.queue = replacement
        release.set()
        code, body = await task
        assert code == 502 and body["outcome"] == "UNKNOWN"
        assert api.state.queue is replacement and replacement.index == 1
        assert not any(value == replacement.current.url for kind, value in api.mpv.calls if kind == "load")


async def test_real_http_favorites_crud_uses_stable_ids_and_no_raw_targets(api):
    selected(api)
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        csrf = snapshot["csrf_token"]
        code, result = await command(session, base, csrf, {"action": "favorite_current", "name": "Current",
                                                          "expected_generation": snapshot["generation"]})
        assert code == 200
        first_id = result["favorite_id"]
        code, result = await command(session, base, csrf, {"action": "favorite_add", "name": "Added",
                                  "url": "https://media.example.test/new.wav?token=FAVORITE_SECRET"})
        assert code == 200
        identifier = result["favorite_id"]
        for payload in [
            {"action": "favorite_rename", "id": identifier, "name": "Renamed"},
            {"action": "favorite_target", "id": identifier, "url": "https://media.example.test/other.wav?token=NEW_SECRET", "metadata": "clear"},
            {"action": "favorite_play", "id": identifier},
            {"action": "favorite_delete", "id": first_id},
        ]:
            assert (await command(session, base, csrf, payload))[0] == 200
        async with session.get(base + PREFIX + "/favorites") as response:
            document = await response.json()
            assert response.status == 200
            assert "SECRET" not in json.dumps(document)
            assert document["favorites"][0]["id"] == identifier
            assert document["favorites"][0]["target_label"] == "media.example.test"
            assert document["favorites"][0]["name"] == "Renamed"
            assert "url" not in document["favorites"][0]
        assert api.mpv.url == "https://media.example.test/other.wav?token=NEW_SECRET"


async def test_backend_gain_uncertain_and_pending_never_claim_success(api):
    api.mpv.gain_confirmed = False
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        assert snapshot["gain"]["status"] == "UNKNOWN"
        assert snapshot["gain"]["volume"] is snapshot["gain"]["mute"] is None
        assert snapshot["gain"]["last_confirmed"] is None
        api.mpv.gain_confirmed = True
        entered, release = asyncio.Event(), asyncio.Event()
        async def pending_volume(value):
            entered.set()
            await release.wait()
            raise BackendError("https://private.example.test/?token=GAIN_SECRET")
        api.mpv.volume = pending_volume
        task = asyncio.create_task(command(session, base, snapshot["csrf_token"], {"action": "volume", "value": 20}))
        await entered.wait()
        pending = await state(session, base)
        assert pending["gain"]["status"] == "PENDING"
        assert pending["gain"]["volume"] is None
        release.set()
        status, error = await task
        assert status == 502 and error["outcome"] == "UNKNOWN" and "SECRET" not in json.dumps(error)
        failed = await state(session, base)
        assert failed["gain"]["status"] == "UNKNOWN"
        assert failed["transport"]["phase"] == "ERROR"
        assert failed["transport"]["status"] == "ERROR"
        assert failed["gain"]["last_confirmed"] is None


async def test_dsp_pending_proposal_hidden_then_confirmed_and_failure_is_unknown(api):
    await api.initialize_channel_balance()
    entered, release = asyncio.Event(), asyncio.Event()
    original = api.mpv.channel_balance
    async def slow_balance(value):
        entered.set()
        await release.wait()
        await original(value)
    api.mpv.channel_balance = slow_balance
    async with serving(api) as (session, base):
        before = await state(session, base)
        assert before["dsp"]["status"] == "CONFIRMED"
        task = asyncio.create_task(command(session, base, before["csrf_token"], {"action": "balance", "value": -.375}))
        await entered.wait()
        assert api.channel_balance == "-0.375"  # Proposed cache must stay hidden.
        pending = await state(session, base)
        assert pending["dsp"] == {"status": "PENDING", "balance": None, "eq_enabled": None, "name": "", "editor": None}
        release.set()
        assert (await task)[0] == 200
        assert (await state(session, base))["dsp"]["balance"] == -.375
        assert (await command(session, base, before["csrf_token"], {"action": "eq", "enabled": False}))[0] == 200
        assert (await state(session, base))["dsp"]["eq_enabled"] is False
        api.mpv.dsp_error = BackendError("https://example.test/?token=DSP_SECRET")
        api.mpv._dsp_configuration_confirmed = False
        code, error = await command(session, base, before["csrf_token"], {"action": "eq", "enabled": True})
        assert code == 502 and error["outcome"] == "UNKNOWN"
        unknown = (await state(session, base))["dsp"]
        assert unknown["status"] == "UNKNOWN" and unknown["balance"] is unknown["eq_enabled"] is None
        assert "SECRET" not in json.dumps(error)


async def test_native_uncertainty_overrides_playing_and_never_disables_stop(api):
    selected(api)
    api.state.phase = PlaybackPhase.PLAYING
    api._native_output_uncertain = True
    api._native_waiting_for_audio = True
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        assert snapshot["transport"]["status"] == "ERROR"
        assert snapshot["media"]["position_seconds"] is None
        assert snapshot["actions"]["stop"]
        assert not snapshot["actions"]["queue_mode"]
        assert not snapshot["actions"]["seek"]
        assert not snapshot["actions"]["next"]
        assert (await command(session, base, snapshot["csrf_token"], {"action": "stop"}))[0] == 200


@pytest.mark.parametrize("value", [
    "https://user:pass@art.example.test/a.jpg", "https://art.example.test/a.jpg?token=x",
    "https://art.example.test/a.jpg#x", "https://art.example.test/a.jpg?", "https://art.example.test/a.jpg#",
    "javascript:alert(1)", "data:image/png;base64,a", "//art.example.test/a", "https://art.example.test/\nx",
    "https://art.example.test:0/a", "https://art.example.test/\\x", "https://art.example.test/\ud800",
])
def test_unsafe_artwork_is_absent(value):
    assert safe_artwork(value) is None


def test_plain_artwork_is_preserved():
    assert safe_artwork("https://art.example.test/cover.png") == "https://art.example.test/cover.png"


@pytest.mark.parametrize("payload", [
    None, [], {}, {"action": "raw", "command": "getStatusEx"}, {"action": "stop", "extra": 1},
    {"action": "play"}, {"action": "play", "expected_generation": True},
    {"action": "volume", "value": True}, {"action": "volume", "value": -1}, {"action": "volume", "value": 101},
    {"action": "volume", "value": 20.0}, {"action": "mute", "value": 1}, {"action": "eq", "enabled": "true"},
    {"action": "balance", "value": True}, {"action": "balance", "value": 1.01},
    {"action": "seek", "seconds": 1.5, "expected_generation": 0},
    {"action": "queue_mode", "queue_name": "Queue", "mode": "shuffle", "expected_generation": 0},
    {"action": "favorite_delete", "id": "not-a-favorite"},
    {"action": "favorite_add", "name": "X", "url": "file:///tmp/private"},
    {"action": "favorite_add", "name": "X", "url": "https://user:pass@media.example.test/a"},
    {"action": "favorite_target", "id": "a" * 32, "url": "https://example.test/x", "metadata": "auto"},
])
def test_strict_typed_command_schema(payload):
    with pytest.raises(WebAdmissionError):
        decode_command(json.dumps(payload).encode())


@pytest.mark.parametrize("encoded", [
    b'{"action":"stop","action":"stop"}', b'{"action":"balance","value":NaN}',
    b'{"action":"balance","value":Infinity}', b'{"action":"balance","value":1e9999}',
    b'{"action":"stop"}\xff', b'\xef\xbb\xbf{"action":"stop"}', b'{"action":"stop"} trailing',
    b'[' * 1200 + b']' * 1200,
])
def test_strict_json_rejects_duplicate_nonfinite_invalid_utf8_and_depth(encoded):
    with pytest.raises(WebAdmissionError):
        decode_command(encoded)


async def test_saved_current_fallback_title_is_private_in_favorites_and_after_recall(api):
    url = "https://media.example.test/song.mp3?arbitrary_signature=SIGNED_SECRET"
    api.state.queue = PlaybackQueue(items=[QueueItem(url=url)], index=1)
    api._activate_queue_item()
    assert api.state.queue.current.title_is_fallback
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        status, saved = await command(session, base, snapshot["csrf_token"], {
            "action": "favorite_current", "name": "Safe label", "expected_generation": snapshot["generation"]})
        assert status == 200
        assert api.favorites.document()["favorites"][0]["title"].endswith("SIGNED_SECRET")
        async with session.get(base + PREFIX + "/favorites") as response:
            safe = await response.json()
            assert safe["favorites"][0]["title"] == ""
            assert "SIGNED_SECRET" not in json.dumps(safe)
        status, _ = await command(session, base, snapshot["csrf_token"], {"action": "favorite_play", "id": saved["favorite_id"]})
        assert status == 200
        recalled = await state(session, base)
        assert recalled["media"]["title"] == "Track 1"
        assert "SIGNED_SECRET" not in json.dumps(recalled)


@pytest.mark.parametrize("queue_name", ["Long" * 100, "https://example.test/q?token=SECRET", "ü" * 129])
async def test_private_or_long_queue_name_uses_generation_fenced_safe_label(api, queue_name):
    selected(api)
    api.state.queue.name = queue_name
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        assert snapshot["actions"]["queue_select"]
        assert snapshot["actions"]["queue_mode"]
        assert snapshot["actions"]["stop"]
        assert "SECRET" not in json.dumps(snapshot)
        code, body = await command(session, base, snapshot["csrf_token"], {
            "action": "queue_mode", "queue_name": snapshot["queue"]["name"], "mode": "repeat-all",
            "expected_generation": snapshot["generation"]})
        assert code == 200
        code, body = await command(session, base, snapshot["csrf_token"], {
            "action": "queue_select", "queue_name": snapshot["queue"]["name"], "index": 2,
            "expected_generation": snapshot["generation"]})
        assert code == 200 and api.state.queue.index == 2
        assert api.state.queue.name == queue_name


@pytest.mark.parametrize("artwork", ["https://art.example.test/a%", "https://art.example.test/a%zz",
    "https://art.example.test/a%0a", "https://art.example.test/a%7f", "https://art.example.test/a%ff",
    "https://art.example.test/a%ed%a0%80", "https://art.example.test/a%5csecret"])
def test_badly_encoded_artwork_is_absent(artwork):
    assert safe_artwork(artwork) is None


async def test_browser_boundary_headers_and_legacy_routes_remain_separate(api):
    async with serving(api) as (session, base):
        async with session.get(base + PREFIX + "/state") as response:
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["Referrer-Policy"] == "no-referrer"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["X-Frame-Options"] == "DENY"
            csp = response.headers["Content-Security-Policy"]
            for rule in ("default-src 'none'", "script-src 'self'", "style-src 'self'", "connect-src 'self'",
                         "frame-ancestors 'none'", "base-uri 'none'", "form-action 'none'"):
                assert rule in csp
            assert "Access-Control-Allow-Origin" not in response.headers
        # Browser safety is deliberately not an authentication claim for the
        # old compatibility API: no Origin or CSRF header is required there.
        async with session.get(base + "/httpapi.asp?command=setPlayerCmd:vol:17") as response:
            assert response.status == 200 and await response.text() == "OK"
        assert api.mpv.actual_volume == 17
        async with session.get(base + "/orr/v1/favorites") as response:
            assert response.status == 200


@pytest.mark.parametrize("kind", ["foreign_host", "userinfo_host", "wrong_port", "forwarded_host",
    "null_origin", "foreign_origin", "origin_path", "foreign_scheme", "missing_origin", "missing_csrf",
    "wrong_csrf", "nonascii_csrf", "cross_site", "same_site"])
async def test_real_http_browser_admission_rejects_without_effects(api, kind):
    async with serving(api) as (session, base):
        token = (await state(session, base))["csrf_token"]
        headers = {"Origin": base, "X-ORR-CSRF": token}
        port = base.rsplit(":", 1)[-1]
        if kind == "foreign_host": headers["Host"] = f"attacker.example.test:{port}"
        elif kind == "userinfo_host": headers["Host"] = f"user@127.0.0.1:{port}"
        elif kind == "wrong_port": headers["Host"] = "127.0.0.1:1"
        elif kind == "forwarded_host":
            headers.update(Host=f"attacker.example.test:{port}", Forwarded=f'host="127.0.0.1:{port}"', **{"X-Forwarded-Host": f"127.0.0.1:{port}"})
        elif kind == "null_origin": headers["Origin"] = "null"
        elif kind == "foreign_origin": headers["Origin"] = "https://attacker.example.test"
        elif kind == "origin_path": headers["Origin"] = base + "/"
        elif kind == "foreign_scheme": headers["Origin"] = base.replace("http:", "https:")
        elif kind == "missing_origin": del headers["Origin"]
        elif kind == "missing_csrf": del headers["X-ORR-CSRF"]
        elif kind == "wrong_csrf": headers["X-ORR-CSRF"] = "wrong"
        elif kind == "nonascii_csrf": headers["X-ORR-CSRF"] = "é"
        elif kind == "cross_site": headers["Sec-Fetch-Site"] = "cross-site"
        elif kind == "same_site": headers["Sec-Fetch-Site"] = "same-site"
        async with session.post(base + PREFIX + "/command", json={"action": "volume", "value": 5}, headers=headers) as response:
            body = await response.json()
            assert response.status == 403 and body["outcome"] == "REJECTED"
            assert "Access-Control-Allow-Origin" not in response.headers
        assert not api.mpv.calls and api.state.volume == 50


@pytest.mark.parametrize("path", ["/", "/ui/", "/ui/index.html", "/ui/style.css", "/ui/app.js", "/ui/controller.js",
                                  PREFIX + "/state", PREFIX + "/favorites", PREFIX + "/command"])
async def test_host_guard_covers_every_new_route_before_asset_read_or_state(api, path):
    async with serving(api) as (session, base):
        port = base.rsplit(":", 1)[-1]
        async with session.get(base + path, headers={"Host": f"evil.example.test:{port}"}) as response:
            assert response.status == 403
            assert (await response.json())["outcome"] == "REJECTED"
        assert not api.mpv.calls


@pytest.mark.parametrize("method,path,status", [
    ("GET", PREFIX + "/command", 405), ("POST", PREFIX + "/state", 405),
    ("PUT", PREFIX + "/command", 405), ("DELETE", PREFIX + "/favorites", 405),
    ("OPTIONS", PREFIX + "/command", 405), ("HEAD", PREFIX + "/state", 405),
    ("GET", PREFIX + "/missing", 404), ("GET", "/ui/private.txt", 404),
    ("GET", PREFIX + "/state?raw=SECRET", 400),
])
async def test_no_cors_method_or_arbitrary_path_escape(api, method, path, status):
    async with serving(api) as (session, base):
        async with session.request(method, base + path, headers={"Origin": base,
                "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "X-ORR-CSRF"}) as response:
            assert response.status == status
            assert "Access-Control-Allow-Origin" not in response.headers
            assert "Access-Control-Allow-Methods" not in response.headers
            if method != "HEAD":
                assert "SECRET" not in await response.text()
        assert not api.mpv.calls


async def raw_http(base, request):
    port = int(base.rsplit(":", 1)[-1])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), 8)
        head, body = data.split(b"\r\n\r\n", 1)
        return int(head.split(b" ", 2)[1]), head, body
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.parametrize("extra,body,status", [
    (["Content-Type: text/plain"], b'{"action":"stop"}', 415),
    (["Content-Type: application/json;charset=latin-1"], b'{"action":"stop"}', 415),
    (["Content-Type: application/json", "Content-Type: application/json"], b'{"action":"stop"}', 400),
    (["Content-Type: application/json", "Content-Encoding: unsupported"], b'{"action":"stop"}', 415),
    (["Content-Type: application/json", "Content-Encoding: identity", "Content-Encoding: identity"], b'{"action":"stop"}', 415),
    (["Content-Type: application/json"], b'{"action":"volume","value":5,"value":6}', 400),
    (["Content-Type: application/json"], b'{"action":"balance","value":NaN}', 400),
    (["Content-Type: application/json"], b'{"action":"stop"}\xff', 400),
    (["Content-Type: application/json"], b'{"action":"stop"}' + b' ' * MAX_REQUEST_BYTES, 413),
])
async def test_actual_http_strict_body_framing(api, extra, body, status):
    async with serving(api) as (session, base):
        csrf = (await state(session, base))["csrf_token"]
        authority = base.removeprefix("http://")
        lines = [f"POST {PREFIX}/command HTTP/1.1", f"Host: {authority}", f"Origin: {base}",
                 f"X-ORR-CSRF: {csrf}", "Connection: close", f"Content-Length: {len(body)}", *extra]
        result, head, encoded = await raw_http(base, ("\r\n".join(lines) + "\r\n\r\n").encode() + body)
        assert result == status
        if extra.count("Content-Type: application/json") == 2:
            # aiohttp rejects this invalid framing before a route exists.
            assert b"Duplicate" in encoded and b"Content-Type" in encoded
        else:
            assert json.loads(encoded)["outcome"] == "REJECTED"
            assert b"no-store" in head
        assert b"Access-Control-Allow-Origin" not in head
        assert not api.mpv.calls


@pytest.mark.parametrize("duplicate", ["Host", "Origin", "X-ORR-CSRF", "Sec-Fetch-Site"])
async def test_duplicate_browser_security_headers_reject(api, duplicate):
    async with serving(api) as (session, base):
        csrf = (await state(session, base))["csrf_token"]
        values = {"Host": base.removeprefix("http://"), "Origin": base, "X-ORR-CSRF": csrf,
                  "Sec-Fetch-Site": "same-origin"}
        body = b'{"action":"stop"}'
        lines = [f"POST {PREFIX}/command HTTP/1.1", *[f"{key}: {value}" for key, value in values.items()],
                 f"{duplicate}: {values[duplicate]}", "Connection: close", "Content-Type: application/json",
                 f"Content-Length: {len(body)}"]
        code, _, encoded = await raw_http(base, ("\r\n".join(lines) + "\r\n\r\n").encode() + body)
        if duplicate == "Host":
            # The HTTP parser owns duplicate Host rejection, before routing.
            assert code == 400 and b"Duplicate" in encoded and b"Host" in encoded
        else:
            assert code == 403 and json.loads(encoded)["outcome"] == "REJECTED"
        assert not api.mpv.calls


@pytest.mark.parametrize("chunked", [True, False])
async def test_chunked_or_unbounded_post_cannot_mutate(api, chunked):
    async with serving(api) as (session, base):
        csrf = (await state(session, base))["csrf_token"]
        lines = [f"POST {PREFIX}/command HTTP/1.1", f"Host: {base.removeprefix('http://')}", f"Origin: {base}",
                 f"X-ORR-CSRF: {csrf}", "Connection: close", "Content-Type: application/json"]
        body = b""
        if chunked:
            lines.append("Transfer-Encoding: chunked")
            payload = b'{"action":"stop"}'
            body = f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n"
        code, _, encoded = await raw_http(base, ("\r\n".join(lines) + "\r\n\r\n").encode() + body)
        assert code == 411 and json.loads(encoded)["outcome"] == "REJECTED"
        assert not api.mpv.calls


async def test_failed_favorite_durability_reports_committed_without_echoing_target(api, monkeypatch):
    original = api.favorites._commit
    def committed(records):
        original(records)
        raise AtomicWriteCommittedError("https://private.example.test/?token=SECRET")
    monkeypatch.setattr(api.favorites, "_commit", committed)
    async with serving(api) as (session, base):
        csrf = (await state(session, base))["csrf_token"]
        code, body = await command(session, base, csrf, {"action": "favorite_add", "name": "Saved",
                                                       "url": "https://media.example.test/a?token=SECRET"})
        assert code == 502 and body["outcome"] == "UNKNOWN" and body["committed"] is True
        assert "SECRET" not in json.dumps(body)
        assert len(api.favorites.document()["favorites"]) == 1
        assert not api.mpv.calls


async def test_post_dispatch_soap_failure_never_claims_unchanged(api, monkeypatch):
    from fake_linkplay.soap import SoapFault
    selected(api)
    async def side_effect_then_reject(command):
        api.state.generation += 1
        raise SoapFault(701, "https://private.example.test/?token=SECRET")
    monkeypatch.setattr(api.web_api, "_dispatch", side_effect_then_reject)
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        code, body = await command(session, base, snapshot["csrf_token"], {
            "action": "play", "expected_generation": snapshot["generation"]})
        assert code == 502 and body["outcome"] == "UNKNOWN"
        assert "SECRET" not in json.dumps(body)
        assert api.state.generation == snapshot["generation"] + 1


async def test_static_assets_are_exact_packaged_resources_without_inline_bootstrap(api):
    from importlib.resources import files
    expected = {"/": ("index.html", "text/html"), "/ui/": ("index.html", "text/html"),
                "/ui/index.html": ("index.html", "text/html"), "/ui/style.css": ("style.css", "text/css"),
                "/ui/controller.js": ("controller.js", "text/javascript"), "/ui/app.js": ("app.js", "text/javascript")}
    async with serving(api) as (session, base):
        token = (await state(session, base))["csrf_token"]
        for route, (filename, content_type) in expected.items():
            async with session.get(base + route, allow_redirects=False) as response:
                assert response.status == 200
                assert response.content_type == content_type
                body = await response.read()
                assert body == files("fake_linkplay").joinpath("web_assets", filename).read_bytes()
                assert token.encode() not in body
                assert response.headers["Cache-Control"] == "no-store"
                assert "Content-Security-Policy" in response.headers
        for route in ("/ui/index.html?x=1", "/ui/app.js?x=1", "/ui/missing.js", "/ui/secret.json"):
            async with session.get(base + route) as response:
                assert response.status in {400, 404}
        assert not api.mpv.calls


async def test_unsupported_encoded_asset_spelling_has_no_file_access(api):
    from yarl import URL
    async with serving(api) as (session, base):
        for path in ("/ui/%61pp.js", "/ui/%2e%2e/pyproject.toml", "/ui/%2fetc/passwd"):
            async with session.get(URL(base + path, encoded=True)) as response:
                assert response.status == 404
                assert (await response.json())["outcome"] == "REJECTED"
        assert not api.mpv.calls


@pytest.mark.parametrize("enabled", [False, True])
async def test_only_enabled_fixed_mdns_hostname_is_admitted(api, enabled):
    api.cfg.enable_mdns = enabled
    api = HttpApi(api.cfg, api.state, api.mpv)
    async with serving(api) as (session, base):
        port = base.rsplit(":", 1)[-1]
        hostname = api.cfg.mdns_hostname.rstrip(".")
        async with session.get(base + PREFIX + "/state", headers={"Host": f"{hostname}:{port}"}) as response:
            assert response.status == (200 if enabled else 403)
        for hostname in ("0.0.0.0", "other.local"):
            async with session.get(base + PREFIX + "/state", headers={"Host": f"{hostname}:{port}"}) as response:
                assert response.status == 403


async def test_single_item_shuffle_previous_uses_http_history_not_soap_seek(api):
    from fake_linkplay.queue_policy import SHUFFLE_ONCE, shuffle_cycle
    import random
    selected(api, count=1, mode=SHUFFLE_ONCE)
    api.state.queue._shuffle = shuffle_cycle(1, 1, random.Random(1))
    await api.execute(ParsedCommand("resume"), "")
    assert "Previous" in api._current_transport_actions().split(",")
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        assert snapshot["actions"]["previous"] is False
        before = list(api.mpv.calls)
        code, body = await command(session, base, snapshot["csrf_token"], {
            "action": "previous", "expected_generation": snapshot["generation"]})
        assert code == 409 and body["outcome"] == "REJECTED"
        assert api.mpv.calls == before


@pytest.mark.parametrize("wrapped", [
    "https://media.example.test/private?a=SECRET",
    "https%3A%2F%2Fmedia.example.test%2Fprivate%3Fa%3DSECRET",
    "https%253A%252F%252Fmedia.example.test%252Fprivate%253Fa%253DSECRET",
    r"https:\/\/media.example.test\/private?a=SECRET",
    r"https\u003a\u002f\u002fmedia.example.test\u002fprivate?a=SECRET",
])
async def test_wrapped_urls_never_become_browser_metadata(api, wrapped):
    selected(api)
    item = api.state.queue.current
    item.title = item.artist = item.album = wrapped
    api._activate_queue_item()
    api.cfg.friendly_name = wrapped
    api.state.queue.name = wrapped
    api.favorites.add({"name": wrapped, "url": "https://media.example.test/public.wav",
                       "title": wrapped, "artist": wrapped, "album": wrapped})
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        assert "SECRET" not in json.dumps(snapshot)
        assert snapshot["media"]["title"] == "Track 1"
        async with session.get(base + PREFIX + "/favorites") as response:
            assert "SECRET" not in await response.text()


async def test_state_and_favorites_share_one_session_but_restart_rotates_it(api):
    async with serving(api) as (session, base):
        old = await state(session, base)
        async with session.get(base + PREFIX + "/favorites") as response:
            assert (await response.json())["session_id"] == old["session_id"]
    new_api = HttpApi(api.cfg, RendererState.from_config(api.cfg), WebBackend())
    async with serving(new_api) as (session, base):
        new = await state(session, base)
        assert new["session_id"] != old["session_id"]
        assert new["csrf_token"] != old["csrf_token"]
        code, body = await command(session, base, old["csrf_token"], {"action": "stop"})
        assert code == 403 and body["outcome"] == "REJECTED"
        assert not new_api.mpv.calls


async def test_flagged_safe_backend_title_remains_visible_but_url_basename_does_not(api):
    selected(api, count=1)
    item = api.state.queue.current
    item.title_is_fallback = True
    api.state.title = item.title = "Synthetic artist — Live song"
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        assert snapshot["media"]["title"] == "Synthetic artist — Live song"
        assert snapshot["queue"]["items"][0]["title"] == "Synthetic artist — Live song"
        api.state.title = item.title = "1.wav"
        snapshot = await state(session, base)
        assert snapshot["media"]["title"] == snapshot["queue"]["items"][0]["title"] == "Track 1"
        item.url = "https://media.example.test/private%20filename.wav?access_key=SECRET"
        api.state.title = item.title = "private filename.wav"
        snapshot = await state(session, base)
        assert snapshot["media"]["title"] == snapshot["queue"]["items"][0]["title"] == "Track 1"


@pytest.mark.parametrize("path", ["/", "/ui/", "/ui/index.html"])
@pytest.mark.parametrize("site", ["cross-site", "same-site"])
async def test_external_top_level_html_navigation_is_allowed(api, path, site):
    async with serving(api) as (session, base):
        async with session.get(base + path, headers={"Sec-Fetch-Site": site,
                "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}) as response:
            assert response.status == 200 and response.content_type == "text/html"
            assert response.headers["X-Frame-Options"] == "DENY"
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
            assert "Access-Control-Allow-Origin" not in response.headers
        assert not api.mpv.calls


@pytest.mark.parametrize("method,path,mode,dest", [
    ("GET", "/ui/", "navigate", "iframe"),
    ("GET", "/ui/index.html", "no-cors", "script"),
    ("GET", "/ui/app.js", "no-cors", "script"),
    ("GET", "/ui/style.css", "no-cors", "style"),
    ("GET", PREFIX + "/state", "navigate", "document"),
    ("GET", PREFIX + "/favorites", "navigate", "document"),
    ("POST", PREFIX + "/command", "navigate", "document"),
    ("POST", "/ui/", "navigate", "document"),
    ("GET", "/ui/", "cors", "document"),
    ("GET", "/ui/", "navigate", "empty"),
])
async def test_external_navigation_exception_never_reaches_frames_scripts_data_or_writes(api, method, path, mode, dest):
    async with serving(api) as (session, base):
        csrf = (await state(session, base))["csrf_token"]
        headers = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": mode,
                   "Sec-Fetch-Dest": dest, "Origin": base, "X-ORR-CSRF": csrf}
        async with session.request(method, base + path, headers=headers,
                json={"action": "stop"} if method == "POST" else None) as response:
            assert response.status == 403
            assert (await response.json())["outcome"] == "REJECTED"
            assert "Access-Control-Allow-Origin" not in response.headers
        assert not api.mpv.calls


@pytest.mark.parametrize("bad", ["host", "origin", "duplicate_mode", "duplicate_dest", "duplicate_site", "missing_mode", "missing_dest"])
async def test_external_html_navigation_retains_host_origin_and_exact_metadata_guards(api, bad):
    async with serving(api) as (session, base):
        headers = [("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Mode", "navigate"),
                   ("Sec-Fetch-Dest", "document")]
        if bad == "host": headers.append(("Host", "attacker.example.test:" + base.rsplit(":", 1)[-1]))
        elif bad == "origin": headers.append(("Origin", "https://attacker.example.test"))
        elif bad == "duplicate_mode": headers.append(("Sec-Fetch-Mode", "navigate"))
        elif bad == "duplicate_dest": headers.append(("Sec-Fetch-Dest", "document"))
        elif bad == "duplicate_site": headers.append(("Sec-Fetch-Site", "cross-site"))
        elif bad == "missing_mode": headers = [(key, value) for key, value in headers if key != "Sec-Fetch-Mode"]
        elif bad == "missing_dest": headers = [(key, value) for key, value in headers if key != "Sec-Fetch-Dest"]
        async with session.get(base + "/ui/", headers=headers) as response:
            assert response.status == 403 and (await response.json())["outcome"] == "REJECTED"
        assert not api.mpv.calls
