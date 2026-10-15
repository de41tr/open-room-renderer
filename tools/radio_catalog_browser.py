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
    return {"search_save_play": True, "country_only_browse_save_play": True,
            "native_codec": "mp3", "output": "null",
            "clear_discards_late_result": True, "outage_preserves_stopped_playback": True,
            "directory": "synthetic; no live-provider claim"}
