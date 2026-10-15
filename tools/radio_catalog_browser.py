"""Focused catalog UI check for the existing hosted Chrome/null-output lane.

Synthetic directory records and generated MP3 only. Production DOM, browser
requests, typed commands, favorites, public proxy and Unix-IPC playback run.
"""
from copy import deepcopy
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_linkplay.network_policy import ProxyTarget, resolve_proxy_target
from fake_linkplay.radio_catalog import CatalogError, CatalogStation
from test_mpv_unix_integration import _property


async def exercise_catalog(browser, api, backend, base, artifacts, label):
    import asyncio
    original_queue = deepcopy(api.state.queue)
    station = CatalogStation("00000000-0000-4000-8000-000000000000",
        "Synthetic <radio>", "DE", "MP3", 128, base + "/browser-smoke/station.mp3")
    entered, release = asyncio.Event(), asyncio.Event()
    searches = []
    async def search(name, country):
        searches.append((name, country))
        if name == "slow":
            entered.set(); await release.wait()
        if name == "unavailable":
            raise CatalogError("Synthetic outage")
        return [station]
    async def resolve_station(_, **_kwargs):
        return station
    async def resolve(url, allowed_lan_hosts=()):
        if url == station.url:
            parsed = urlsplit(url)
            assert not allowed_lan_hosts
            return ProxyTarget(url, parsed.hostname, parsed.port, ("127.0.0.1",))
        return await resolve_proxy_target(url, allowed_lan_hosts)
    async def type_field(selector, value):
        item = await browser.call("POST", "/element", {"using": "css selector", "value": selector})
        key = next(value for key, value in item.items() if key.startswith("element-"))
        await browser.call("POST", f"/element/{key}/clear", {})
        await browser.call("POST", f"/element/{key}/value", {"text": value})
    try:
        with patch("fake_linkplay.web_ui.search_stations", search), \
             patch("fake_linkplay.http_api.resolve_catalog_station", resolve_station), \
             patch("fake_linkplay.http_api.resolve_proxy_target", resolve):
            repair = api.favorites.add({"name": "My morning radio", "url": "https://media.example.test/retired.mp3",
                "title": "Old station title", "artist": "Old artist"})
            admitted = api.favorites.playback_snapshot(repair["id"])
            before = len(api.favorites.document()["favorites"])
            await type_field("#radio-name", "Synthetic")
            await browser.click("#radio-search")
            await browser.wait("document.querySelectorAll('#radio-results li').length === 1")
            assert await browser.inspect("document.querySelector('#radio-results .item-title').textContent") == station.name
            assert not await browser.inspect("document.querySelector('#radio-results script') !== null")
            await browser.click("#radio-clear")
            await type_field("#radio-country", "de")
            await browser.click("#radio-search")
            await browser.wait("document.querySelectorAll('#radio-results li').length === 1")
            assert searches == [("Synthetic", ""), ("", "DE")]
            assert await browser.inspect("document.querySelector('#radio-name').value") == ""
            await browser.click("#radio-results [data-radio-action='favorite_radio']")
            await browser.wait("document.querySelector('#radio-results [data-radio-action=\"favorite_radio\"]').textContent === 'Saved'")
            assert len(api.favorites.document()["favorites"]) == before + 1
            assert await browser.inspect("document.querySelector('#radio-results [data-radio-action=\"favorite_radio\"]').disabled")
            await browser.click("#radio-results [data-radio-action='radio_play']")
            await browser.wait("document.querySelector('#playback-state').textContent === 'PLAYING'")
            assert await _property(backend, "audio-codec-name") == "mp3"
            assert await _property(backend, "current-ao") == "null"
            await browser.screenshot(artifacts / (label + "-radio.png"))
            # Existing native playback must survive a library-only repair.
            queue, queue_before, generation = api.state.queue, deepcopy(api.state.queue), api.state.generation
            path_before = await _property(backend, "path")
            opener = "#radio-results [data-radio-action='favorite_radio_target']"
            await browser.click(opener)
            assert await browser.inspect("document.activeElement.id") == "favorite-radio-select"
            assert await browser.inspect("document.querySelector('#favorite-submit').disabled")
            await browser.key("\ue00c")  # Escape cancels without writing.
            await browser.wait("!document.querySelector('#favorite-dialog').open")
            assert api.favorites.playback_snapshot(repair["id"])["url"] == repair["url"]
            assert await browser.inspect("document.activeElement.dataset.radioAction") == "favorite_radio_target"
            await browser.click(opener)
            await browser.click("#favorite-radio-select option[value='" + repair["id"] + "']")
            await browser.click("#favorite-metadata option[value='clear']")
            assert await browser.inspect("document.querySelector('#favorite-radio-station').textContent") == "Selected station: Synthetic <radio> · DE · MP3 · 128 kbps"
            assert "My morning radio" in await browser.inspect("document.querySelector('#favorite-delete-name').textContent")
            assert await browser.inspect("document.querySelectorAll('#favorite-radio-select option[value^=\"configured:\"]').length") == 0
            await browser.screenshot(artifacts / (label + "-radio-repair.png"))
            assert await browser.inspect("document.documentElement.scrollWidth <= innerWidth + 1")
            await browser.click("#favorite-submit")
            await browser.wait("document.querySelector('#favorite-dialog-status').textContent.startsWith('ACKNOWLEDGED')")
            updated = next(item for item in api.favorites.document()["favorites"] if item["id"] == repair["id"])
            assert updated == {**repair, "url": station.reference, "title": "", "artist": ""}
            assert admitted["url"] == repair["url"] and len(api.favorites.document()["favorites"]) == before + 1
            assert api.state.queue is queue and queue == queue_before and api.state.generation == generation
            assert await _property(backend, "path") == path_before
            assert await browser.inspect("document.querySelector('#favorite-submit').disabled")
            await browser.click("#favorite-cancel")
            assert await browser.inspect("document.documentElement.scrollWidth <= innerWidth + 1")
            await browser.click("#stop")
            await browser.wait("document.querySelector('#playback-state').textContent === 'STOPPED'")
            await type_field("#radio-name", "slow")
            await browser.click("#radio-search")
            await asyncio.wait_for(entered.wait(), 2)
            await browser.click("#radio-clear")
            release.set(); await asyncio.sleep(.2)
            assert await browser.inspect("document.querySelectorAll('#radio-results li').length") == 0
            await type_field("#radio-name", "unavailable")
            await browser.click("#radio-search")
            await browser.wait("document.querySelector('#radio-status').textContent.startsWith('Search unavailable')")
            assert await _property(backend, "idle-active") is True
            await browser.click("#radio-clear")
    finally:
        release.set()
        await api._replace_queue(original_queue)
    return {"search_save_play": True, "country_only_browse_save_play": True, "repair_preserves_identity_and_playback": True,
            "repair_escape_cancels": True,
            "native_codec": "mp3", "output": "null",
            "clear_discards_late_result": True, "outage_preserves_stopped_playback": True,
            "directory": "synthetic; no live-provider claim"}
