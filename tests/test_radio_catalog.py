"""Public-directory and real loopback HTTP contracts; synthetic provider/backend."""
import asyncio
from dataclasses import replace
import json
import time
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest

from fake_linkplay import radio_catalog as catalog
from fake_linkplay.canonical_target import canonical_target_url
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.network_policy import NetworkPolicyError
from fake_linkplay.state import PlaybackPhase
from fake_linkplay.web_ui import PREFIX
from test_web_ui import api, command, serving, state

UUID = "00000000-0000-4000-8000-000000000000"
STATION = catalog.CatalogStation(UUID, "Synthetic radio", "DE", "MP3", 128,
                                 "https://media.example.test/live.mp3")


def provider_row(**changes):
    return {"stationuuid": UUID, "name": STATION.name, "countrycode": "DE", "codec": "MP3",
            "bitrate": 128, "url_resolved": STATION.url, "hls": 0, "lastcheckok": 1, **changes}


async def search(session, base, snapshot, **payload):
    return await session.post(base + PREFIX + "/radio/search", json={"action": "search", "name": "Synthetic", "country": "DE", **payload},
        headers={"Origin": base, "X-ORR-CSRF": snapshot["csrf_token"]})


async def test_directory_search_bounds_filters_and_mirror_fallback(monkeypatch):
    monkeypatch.setattr(catalog, "_mirrors", AsyncMock(return_value=["one.api.radio-browser.info", "two.api.radio-browser.info"]))
    fetch = AsyncMock(side_effect=[catalog.CatalogError("Unavailable"), json.dumps([
        provider_row(), provider_row(stationuuid="11111111-1111-4111-8111-111111111111", hls=1),
        provider_row(stationuuid="22222222-2222-4222-8222-222222222222", codec="FLAC"),
        provider_row(name="duplicate"), provider_row(stationuuid="invalid")]).encode()])
    monkeypatch.setattr(catalog, "_fetch", fetch)
    assert await catalog.search_stations(" Synthetic ", "DE") == [STATION]
    query = parse_qs(urlsplit(fetch.call_args.args[0]).query)
    assert query["limit"] == ["20"] and query["hidebroken"] == ["true"]
    assert query["name"] == ["Synthetic"] and query["countrycode"] == ["DE"]
    with pytest.raises(catalog.CatalogError):
        await catalog.search_stations("x", "DE")
    assert fetch.await_count == 2
    for codec in ("MP3", "AAC", "AAC+", "OGG"):
        assert catalog._stations(json.dumps([provider_row(codec=codec)]).encode()) == [replace(STATION, codec=codec)]
    for codec in ("VORBIS", "OPUS", "FLAC", "ogg", "Ogg", "audio/ogg", "unknown", ""):
        assert catalog._stations(json.dumps([provider_row(codec=codec)]).encode()) == []
    for changes in ({"hls": 1}, {"lastcheckok": 0}, {"url_resolved": "file:///station.ogg"}):
        assert catalog._stations(json.dumps([provider_row(codec="OGG", **changes)]).encode()) == []
    for raw in (b"{}", b"[]" * (catalog.MAX_BYTES // 2 + 1), json.dumps([provider_row()] * 21).encode()):
        with pytest.raises(catalog.CatalogError):
            catalog._stations(raw)


async def test_resolution_rejects_private_destinations_and_untrusted_references(monkeypatch):
    monkeypatch.setattr(catalog, "_query", AsyncMock(return_value=([replace(STATION, url="http://127.0.0.1/private")], "one.api.radio-browser.info")))
    with pytest.raises(catalog.CatalogError, match="unavailable"):
        await catalog.resolve_station(UUID)
    for url in (STATION.reference + "?x=1", STATION.reference + "/", STATION.reference.replace("https:", "http:"),
                STATION.reference.replace("www.", "user@www."), STATION.reference.replace(UUID, "../private")):
        assert catalog.station_from_reference(url) == ""
    assert catalog.station_from_reference(STATION.reference) == UUID


async def test_real_http_search_save_play_recall_uses_existing_owners(api, monkeypatch):
    monkeypatch.setattr("fake_linkplay.web_ui.search_stations", AsyncMock(return_value=[STATION]))
    resolve = AsyncMock(return_value=STATION)
    monkeypatch.setattr("fake_linkplay.http_api.resolve_catalog_station", resolve)
    api._wait_for_mpv_audio_ready = AsyncMock(return_value=True)
    original_load = api.mpv.loadfile
    api.mpv.loadfile = AsyncMock(side_effect=original_load)
    async with serving(api) as (session, base):
        before = await state(session, base)
        response = await search(session, base, before)
        assert response.status == 200
        result = await response.json()
        station = result["stations"][0]
        assert "url" not in station and STATION.url not in json.dumps(result)
        assert not api.mpv.calls and api.state.generation == before["generation"]
        status, saved = await command(session, base, before["csrf_token"], {"action": "favorite_radio", "id": station["id"]})
        assert status == 200 and api.favorites.playback_snapshot(saved["favorite_id"])["url"] == STATION.reference
        assert not api.mpv.calls and not resolve.await_count
        status, _ = await command(session, base, before["csrf_token"], {"action": "radio_play", "id": station["id"], "expected_generation": before["generation"]})
        assert status == 200 and api.state.phase == PlaybackPhase.PLAYING
        assert canonical_target_url(api.state.queue.current) == STATION.reference
        url, options = api.mpv.loadfile.call_args.args
        assert url.startswith("http://127.0.0.1:") and "/proxy/audio?" in url
        assert options["demuxer-lavf-format"] == "mp3" and options["demuxer"] == "lavf"
        assert api.state.queue.current.stream_type == "station"
        await command(session, base, before["csrf_token"], {"action": "stop"})
        resolve.return_value = replace(STATION, codec="OGG", url="https://media.example.test/live.ogg")
        status, _ = await command(session, base, before["csrf_token"], {"action": "favorite_play", "id": saved["favorite_id"]})
        assert status == 200 and resolve.await_count == 2
        assert api.state.queue.current.content_type == "audio/ogg"
        assert api.mpv.loadfile.call_args.args[1]["demuxer-lavf-format"] == "ogg"
        assert api.mpv.loadfile.call_args.args[1]["ytdl"] == "no"
        api.state.queue.current.content_type = "application/octet-stream"
        with pytest.raises(BackendError, match="Unsupported radio catalog format"):
            api._mpv_playback_options(STATION.reference, api.state.queue.current.resolved_url)


async def test_search_admission_busy_failure_and_expired_selection(api, monkeypatch):
    lookup = AsyncMock(side_effect=AssertionError("Expired selection reached resolver"))
    monkeypatch.setattr("fake_linkplay.http_api.resolve_catalog_station", lookup)
    entered, release = asyncio.Event(), asyncio.Event()
    async def held(*_):
        entered.set(); await release.wait(); raise catalog.CatalogError("private error should stay hidden")
    monkeypatch.setattr("fake_linkplay.web_ui.search_stations", held)
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        response = await session.post(base + PREFIX + "/radio/search", json={"name": "Synthetic", "country": "DE"})
        assert response.status == 403 and not entered.is_set()
        response = await search(session, base, snapshot, name="x")
        assert response.status == 400
        pending = asyncio.create_task(search(session, base, snapshot))
        await asyncio.wait_for(entered.wait(), 2)
        response = await search(session, base, snapshot)
        assert response.status == 409
        release.set()
        response = await pending
        assert response.status == 503 and "private error" not in await response.text()
        api.web_api._radio_results["a" * 32] = (time.monotonic() - 901, STATION)
        status, body = await command(session, base, snapshot["csrf_token"], {"action": "radio_play", "id": "a" * 32, "expected_generation": snapshot["generation"]})
        assert status == 409 and body["error"] == "radio_expired" and not api.mpv.calls
        lookup.assert_not_awaited()


async def test_catalog_proxy_never_uses_lan_allowlist_or_redirects_to_private(api, monkeypatch):
    api.cfg.proxy_allowed_lan_hosts = ("127.0.0.1",)
    api.state.current_url = STATION.reference
    api.state.intent = "play"
    api.state.resolved_url = "http://127.0.0.1/private"
    with pytest.raises(NetworkPolicyError):
        await api._open_proxy_source(api.state.resolved_url, {})
    assert not api.mpv.calls


async def test_stop_during_station_resolution_cannot_start_late_audio(api, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    async def held(_, **_kwargs):
        entered.set(); await release.wait(); return STATION
    monkeypatch.setattr("fake_linkplay.web_ui.search_stations", AsyncMock(return_value=[STATION]))
    monkeypatch.setattr("fake_linkplay.http_api.resolve_catalog_station", held)
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        response = await search(session, base, snapshot)
        token = (await response.json())["stations"][0]["id"]
        pending = asyncio.create_task(command(session, base, snapshot["csrf_token"],
            {"action": "radio_play", "id": token, "expected_generation": snapshot["generation"]}))
        await asyncio.wait_for(entered.wait(), 2)
        status, _ = await command(session, base, snapshot["csrf_token"], {"action": "stop"})
        assert status == 200
        release.set()
        assert (await pending)[0] == 502
        assert api.state.phase == PlaybackPhase.STOPPED
        assert not [call for call in api.mpv.calls if call[0] == "load"]


async def test_click_report_is_explicit_best_effort_and_never_supplies_audio(monkeypatch):
    monkeypatch.setattr(catalog, "_query", AsyncMock(return_value=([STATION], "one.api.radio-browser.info")))
    monkeypatch.setattr(catalog, "resolve_proxy_target", AsyncMock())
    report = AsyncMock(side_effect=TimeoutError)
    monkeypatch.setattr(catalog, "_fetch", report)
    assert await catalog.resolve_station(UUID) == STATION
    report.assert_not_awaited()
    assert await catalog.resolve_station(UUID, count_click=True) == STATION
    report.assert_awaited_once_with("https://one.api.radio-browser.info/json/url/" + UUID)


async def test_upcoming_catalog_station_is_never_prefetched_or_counted(tmp_path, monkeypatch):
    from test_native_queue_staging import A, make_api, start
    api = make_api(tmp_path, urls=(A, STATION.reference))
    lookup = AsyncMock(return_value=STATION)
    monkeypatch.setattr("fake_linkplay.http_api.resolve_catalog_station", lookup)
    await start(api)
    for _ in range(3):
        await api.sync_mpv_state()
    lookup.assert_not_awaited()
    assert api.state.queue.index == 1 and api.state.phase == PlaybackPhase.PLAYING
    assert not [call for call in api.mpv.calls if call[0] == "append"]


async def test_specific_bind_uses_proxy_only_ephemeral_loopback_and_cleans_up(api):
    from aiohttp import ClientSession, ClientConnectorError
    from fake_linkplay.cli import _start_catalog_proxy
    runners = []
    api.cfg.bind_address = "127.0.0.2"  # Specific-address bind; no external interface.
    try:
        await _start_catalog_proxy(api, runners)
        port = api.catalog_proxy_port
        assert port and port != api.cfg.http_api_port and len(runners) == 1
        assert api._app_users == 0 and api._state_monitor_task is None
        assert f"127.0.0.1:{port}/proxy/audio" in api._backend_playback_url(STATION.reference, STATION.url)
        async with ClientSession(trust_env=False) as session:
            for path in ("/", PREFIX + "/state", PREFIX + "/command"):
                async with session.get(f"http://127.0.0.1:{port}" + path) as response:
                    assert response.status == 404
            async with session.get(f"http://127.0.0.1:{port}/proxy/audio") as response:
                assert response.status in {403, 404}
        await runners[0].cleanup()
        async with ClientSession(trust_env=False) as session:
            with pytest.raises(ClientConnectorError):
                await session.get(f"http://127.0.0.1:{port}/proxy/audio")
    finally:
        for runner in runners:
            await runner.cleanup()


async def test_optional_catalog_listener_failure_preserves_startup_and_rejects_play(api, monkeypatch):
    from aiohttp import web
    from fake_linkplay.cli import _start_catalog_proxy
    from fake_linkplay.web_ui import WebAdmissionError, WebCommand
    runners = []
    await _start_catalog_proxy(api, runners)
    assert not runners and api.catalog_proxy_port is None
    api.cfg.bind_address = "192.0.2.1"
    monkeypatch.setattr(web.TCPSite, "start", AsyncMock(side_effect=OSError("synthetic bind failure")))
    try:
        await _start_catalog_proxy(api, runners)
        assert api.catalog_proxy_port == 0 and len(runners) == 1
        api.web_api._radio_results["a" * 32] = (time.monotonic(), STATION)
        with pytest.raises(WebAdmissionError) as failure:
            api.web_api._admit(WebCommand(action="radio_play", id="a" * 32, expected_generation=api.state.generation))
        assert failure.value.code == "radio_proxy_unavailable"
        assert not api.mpv.calls
    finally:
        for runner in runners:
            await runner.cleanup()


async def test_unavailable_proxy_favorite_and_queue_admission_preserve_existing_audio(api, monkeypatch):
    from fake_linkplay.http_api import ParsedCommand
    from fake_linkplay.queue import PlaybackQueue, QueueItem
    original = "https://media.example.test/ordinary.mp3"
    lookup = AsyncMock(return_value=STATION)
    monkeypatch.setattr("fake_linkplay.http_api.resolve_catalog_station", lookup)
    api._wait_for_mpv_audio_ready = AsyncMock(return_value=True)
    api.state.queue = PlaybackQueue(items=[QueueItem(original), QueueItem(STATION.reference)], index=1)
    await api._play_url(original)
    api.catalog_proxy_port = 0
    saved = api.favorites.add({"name": STATION.name, "url": STATION.reference})
    queue = api.state.queue
    before = (api.state.generation, api.state.backend_url, api.state.gain_confirmed, list(api.mpv.calls))
    async with serving(api) as (session, base):
        snapshot = await state(session, base)
        status, body = await command(session, base, snapshot["csrf_token"], {"action": "favorite_play", "id": saved["id"]})
        assert status == 503 and body["error"] == "radio_proxy_unavailable" and body["outcome"] == "REJECTED"
        response = await session.post(base + "/orr/v1/favorites/" + saved["id"] + "/play")
        assert response.status == 503 and (await response.json())["error"] == "radio_proxy_unavailable"
        for operation in (api._play_url(STATION.reference), api.execute(ParsedCommand("play", (STATION.reference,)), ""),
                          api._play_queue_index(2), api._play_queue_index(selection="next")):
            with pytest.raises(catalog.CatalogPlaybackUnavailable):
                await operation
        assert api.state.queue is queue and queue.index == 1 and api.state.phase == PlaybackPhase.PLAYING
        assert (api.state.generation, api.state.backend_url, api.state.gain_confirmed, api.mpv.calls) == before
        lookup.assert_not_awaited()



async def test_unavailable_catalog_navigation_rejects_before_native_pause(tmp_path):
    from test_native_queue_staging import A, B, make_api, start
    api = make_api(tmp_path, urls=(STATION.reference, A, B))
    api.state.queue.index = 2
    api._activate_queue_item()
    api.catalog_proxy_port = 0
    await start(api)
    assert api._native_stage is not None
    before = (api.state.generation, list(api.mpv.calls))
    with pytest.raises(catalog.CatalogPlaybackUnavailable):
        await api._navigate_queue(-1)
    assert api.state.phase == PlaybackPhase.PLAYING and api.state.queue.index == 2
    assert (api.state.generation, api.mpv.calls) == before


async def test_unavailable_catalog_eof_keeps_observed_stop_and_completes_task(tmp_path):
    from test_native_queue_staging import A, make_api, start
    api = make_api(tmp_path, enabled=False, urls=(A, STATION.reference))
    api.catalog_proxy_port = 0
    await start(api)
    confirmed_gain = api.state.gain_confirmed
    api.mpv.advance()
    await api.sync_mpv_state()
    await api._queue_advance_task
    assert api.state.phase == PlaybackPhase.STOPPED and api.state.queue.index == 1
    assert api.state.gain_confirmed == confirmed_gain and not api.state.backend_url
    assert "local radio proxy" in api.state.last_error
    assert api._queue_advance_task.exception() is None
