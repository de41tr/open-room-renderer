"""One bounded REAL_BACKEND/null acceptance flow for the owned Web JSON API.

Opt in with ORR_TEST_LIBMPV_PATH. Real aiohttp requests use production HttpApi,
MpvBackend and libmpv; only the command transport is the existing test-only
ctypes adapter. Generated finite WAVs are served on literal loopback, with null
output and no accounts/external media. This verifies Web wiring and native
readback, not a browser, production Unix IPC, audible output or DSP samples.
The existing test_libmpv_dsp.py digital-PCM gate remains the sample-level proof.
"""
from __future__ import annotations

import asyncio
from html import escape
import json
import locale
import os
from pathlib import Path
import sys
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.test_utils import TestServer
import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.queue import PlaybackQueue, QueueItem, serialize_queue_context
from fake_linkplay.queue_policy import REPEAT_ALL
from fake_linkplay.state import RendererState
from test_libmpv_integration import _eventually, _write_tone
from test_libmpv_queue_staging import _StagingBackend
from tools.native_probe_supervisor import run_probe


BASE = "/orr/v1/web"


def _native_source(backend: _StagingBackend, url: str) -> dict[str, Any] | None:
    """Read actual selected native media; never substitute the API's state."""
    if backend.read_property("path") != url:
        return None
    entries = [entry for entry in backend.read_property("playlist")
               if entry.get("current") is True and entry.get("playing") is True]
    audio = [track for track in backend.read_property("track-list")
             if track.get("type") == "audio" and track.get("selected") is True]
    if (len(entries) != 1 or not audio or audio[0].get("codec") != "pcm_s16le"
            or backend.read_property("current-ao") != "null"):
        return None
    assert backend.read_property("audio-params")["samplerate"] == 16000
    return {"native_id": entries[0]["id"], "position": backend.read_property("playback-time"),
            "pause": backend.read_property("pause"), "codec": audio[0]["codec"], "ao": "null"}


async def _exercise(library: str, directory: Path) -> dict[str, Any]:
    baseline = asyncio.all_tasks()
    tracks = tuple(directory / name for name in ("first.wav", "second.wav"))
    for track, frequency in zip(tracks, (440, 660)):
        _write_tone(track, frequency)
    entered, release = asyncio.Event(), asyncio.Event()
    media_requests: list[str] = []

    async def media(request):
        media_requests.append(request.path)
        if request.path == "/first.wav" and not release.is_set():
            entered.set()
            await release.wait()
        return web.FileResponse(tracks[int(request.path == "/second.wav")])

    media_app = web.Application()
    media_app.router.add_get("/{name}.wav", media)
    media_server = TestServer(media_app, shutdown_timeout=1)
    server = backend = api = session = pending_play = monitor = None
    listeners = []
    evidence: dict[str, Any] = {
        "lane": "REAL_BACKEND/null/Web-JSON", "decoded": [],
        "control_transport": "aiohttp Web JSON -> HttpApi -> MpvBackend -> test ctypes -> libmpv",
        "output": "null; no physical device or sample measurement",
    }
    try:
        await media_server.start_server()
        listeners.extend(sock for site in media_server.runner.sites for sock in site._server.sockets)
        urls = [str(media_server.make_url("/" + track.name)) for track in tracks]
        backend = _StagingBackend(library, tracks)
        backend._allowed_media = set(urls)  # Exact fixture URLs only; never arbitrary media.
        evidence["mpv_version"] = backend.read_property("mpv-version")
        cfg = Config(uuid="00000000-0000-4000-8000-000000000000",
                     state_dir=str(directory / "state"), advertise_ip="127.0.0.1",
                     bind_address="127.0.0.1", default_volume=25,
                     auto_spawn_mpv=False, enable_ssdp=False, enable_mdns=False,
                     native_queue_staging=False)
        api = HttpApi(cfg, RendererState.from_config(cfg), backend)
        await api.initialize_channel_balance()
        server = TestServer(api.app(), shutdown_timeout=1)
        await server.start_server()
        cfg.http_api_port = cfg.upnp_port = server.port
        listeners.extend(sock for site in server.runner.sites for sock in site._server.sockets)
        monitor = api._state_monitor_task
        assert monitor is not None and not monitor.done()
        origin = str(server.make_url("/")).rstrip("/")
        async with ClientSession(timeout=ClientTimeout(total=10), trust_env=False) as session:
            async def snapshot() -> dict[str, Any]:
                async with session.get(server.make_url(BASE + "/state")) as response:
                    payload = await response.json()
                    assert response.status == 200, payload
                    assert payload["generation"] == api.state.generation
                    assert payload["transport"]["phase"] == api.state.phase.value
                    assert payload["queue"]["index"] == api.state.queue.index
                    assert payload["queue"]["count"] == api.state.queue.count
                    assert not any(url in json.dumps(payload) for url in urls), "Web state leaked a raw target"
                    return payload

            token = (await snapshot())["csrf_token"]

            async def command(action: str, *, expected=200, guard=False, headers=None, **fields):
                payload = {"action": action, **fields}
                if guard:
                    payload["expected_generation"] = (await snapshot())["generation"]
                request_headers = {"Origin": origin, "X-ORR-CSRF": token, **(headers or {})}
                async with session.post(server.make_url(BASE + "/command"), json=payload,
                                        headers=request_headers) as response:
                    result = await response.json()
                    assert response.status == expected, (payload, response.status, result)
                    if expected == 200:
                        assert result["request_acknowledged"] is True, result
                    return result

            async def legacy(value):
                async with session.get(server.make_url("/httpapi.asp"), params={"command": value}) as response:
                    text = await response.text()
                    assert response.status == 200, (value, text)
                    return json.loads(text) if response.content_type == "application/json" else text

            async def canonical(index, phase):
                state = await snapshot()
                assert state["queue"]["index"] == index
                assert state["transport"]["phase"] == phase
                assert state["transport"]["status"] == "CONFIRMED", state
                old = await legacy("getPlayerStatus")
                assert old["plicurr"] == str(index) and old["playback_phase"] == phase
                assert old["playback_generation"] == str(state["generation"])
                assert old["transport_error"] == ""
                return state

            async def decoded(index):
                first = await _eventually("Web-selected actual PCM source", lambda: _native_source(backend, urls[index - 1]))
                assert first["pause"] is False
                later = await _eventually("same native lifetime advances", lambda: (
                    sample if (sample := _native_source(backend, urls[index - 1]))
                    and sample["native_id"] == first["native_id"]
                    and sample["position"] > first["position"] + .08 else None))
                evidence["decoded"].append({"source": index, **later, "first_position": first["position"]})
                return later

            # Seed the owned queue through its existing public SOAP surface.
            # Every subsequent user control goes through the new Web endpoints.
            queue = PlaybackQueue(name="Web native pair", index=1, loop=REPEAT_ALL,
                items=[QueueItem(url=url, title=f"Synthetic tone {index}", artist="Owned fixture",
                                 album="Web acceptance", duration=30)
                       for index, url in enumerate(urls, 1)])
            namespace = "urn:schemas-wiimu-com:service:PlayQueue:1"
            xml = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                   f'<u:CreateQueue xmlns:u="{namespace}"><QueueContext>'
                   f'{escape(serialize_queue_context(queue))}</QueueContext></u:CreateQueue></s:Body></s:Envelope>')
            async with session.post(server.make_url("/upnp/control/PlayQueue"), data=xml,
                headers={"SOAPACTION": f'"{namespace}#CreateQueue"', "Content-Type": "text/xml"}) as response:
                body = await response.text()
                assert response.status == 200, body
            staged = await canonical(1, "STOPPED")
            assert staged["queue"]["mode"] == "repeat-all"
            assert [item["index"] for item in staged["queue"]["items"]] == [1, 2]
            assert backend.read_property("idle-active") is True and not media_requests
            await command("volume", value=25)
            await command("mute", value=True)
            assert backend.read_property("volume") == pytest.approx(25)
            assert backend.read_property("mute") is True
            gain = (await snapshot())["gain"]
            assert (gain["status"], gain["volume"], gain["mute"]) == ("CONFIRMED", 25, True)
            await command("mute", value=False)
            assert backend.read_property("mute") is False
            evidence["gain_mute"] = {"volume": 25, "native_mute_on_off": True, "state_confirmed": True}

            before = len(backend.commands)
            rejected = await command("volume", value=26, expected=403,
                                     headers={"Origin": "http://untrusted.example.test"})
            assert rejected["outcome"] == "REJECTED"
            assert backend.commands[before:] == [] and backend.read_property("volume") == 25
            evidence["security_rejection"] = {"cross_origin": 403, "native_mutations": 0}

            # Hold real media bytes while native demuxing waits. Acknowledgement
            # and PLAYING must not precede actual audio admission/readback.
            pending_play = asyncio.create_task(command("play", guard=True))
            await asyncio.wait_for(entered.wait(), timeout=4)
            pending = await snapshot()
            assert pending["transport"]["status"] == "PENDING", pending
            assert pending["transport"]["phase"] in {"COMMAND_ACCEPTED", "LOADING", "BUFFERING"}
            assert not pending_play.done()
            assert not any(track.get("selected") and track.get("type") == "audio"
                           for track in backend.read_property("track-list"))
            evidence["readiness"] = {"held_phase": pending["transport"]["phase"],
                                     "no_early_acknowledgement": True, "no_early_playing": True}
            release.set()
            await pending_play
            await decoded(1)
            await canonical(1, "PLAYING")

            await command("pause", guard=True)
            assert backend.read_property("pause") is True
            await canonical(1, "PAUSED")
            await command("seek", seconds=4, guard=True)
            assert abs(backend.read_property("playback-time") - 4) < .3
            assert backend.read_property("pause") is True
            paused = await canonical(1, "PAUSED")
            assert abs(paused["media"]["position_seconds"] - 4) <= 1
            evidence["paused_seek"] = {"actual_position": backend.read_property("playback-time"),
                                        "pause_retained": True, "reported_position": paused["media"]["position_seconds"]}
            native_before = backend.read_property("playlist")
            await command("play", guard=True)
            assert backend.read_property("playlist") == native_before
            await decoded(1)

            before = len(backend.commands)
            generation = (await snapshot())["generation"]
            await command("queue_mode", queue_name=queue.name, mode="normal", guard=True)
            assert backend.commands[before:] == []
            assert backend.read_property("playlist") == native_before
            changed = await canonical(1, "PLAYING")
            assert changed["generation"] == generation and changed["queue"]["mode"] == "normal"
            evidence["queue_policy"] = {"normal_without_reload": True, "generation_retained": True}
            await command("next", guard=True)
            await decoded(2)
            current = await canonical(2, "PLAYING")
            assert current["generation"] > generation
            native_before = backend.read_property("playlist")
            before = len(backend.commands)
            rejected = await command("previous", expected_generation=generation, expected=409)
            assert rejected["outcome"] == "REJECTED"
            assert backend.commands[before:] == [] and backend.read_property("playlist") == native_before
            await canonical(2, "PLAYING")
            evidence["stale_generation"] = {"status": 409, "native_mutations": 0, "source_retained": 2}

            # Configure one attenuation-only curve through the existing surface,
            # then prove Web EQ bypass/on and balance change the real mpv graph.
            assert await legacy("EQSetLV2Band:" + json.dumps({"pluginURI": "synthetic.web", "EQBand": [
                {"param_name": "a_freq", "value": 1000}, {"param_name": "a_gain", "value": -12},
                {"param_name": "a_q", "value": 1}, {"param_name": "a_mode", "value": 1},
            ]})) == {"status": "OK"}
            await command("balance", value=.5)
            graph = backend.read_property("af")
            assert len(graph) == 1 and graph[0]["label"] == "fake_linkplay_dsp"
            assert graph[0]["enabled"] is True and graph[0]["name"] == "lavfi"
            assert "pan=" in graph[0]["params"]["graph"] and "equalizer=" in graph[0]["params"]["graph"]
            await command("eq", enabled=False)
            bypass = backend.read_property("af")
            assert len(bypass) == 1 and bypass[0]["label"] == "fake_linkplay_dsp"
            assert bypass[0]["enabled"] is True and bypass[0]["name"] == "lavfi"
            assert "pan=" in bypass[0]["params"]["graph"] and "equalizer=" not in bypass[0]["params"]["graph"]
            dsp = (await snapshot())["dsp"]
            assert (dsp["status"], dsp["balance"], dsp["eq_enabled"]) == ("CONFIRMED", .5, False)
            await command("eq", enabled=True)
            assert backend.read_property("af") == graph
            assert (await snapshot())["dsp"]["eq_enabled"] is True
            evidence["dsp"] = {"native_combined_graph": graph, "bypass_preserves_balance": True,
                               "scope": "native af readback only; samples are measured by the separate existing DSP gate"}

            saved = await command("favorite_current", name="Second owned tone", guard=True)
            identifier = saved["favorite_id"]
            async with session.get(server.make_url(BASE + "/favorites")) as response:
                favorites = await response.json()
                assert response.status == 200, favorites
            match = next(item for item in favorites["favorites"] if item["id"] == identifier)
            assert match["name"] == "Second owned tone" and "url" not in match
            assert not any(url in json.dumps(favorites) for url in urls)
            await command("queue_select", queue_name=queue.name, index=1, guard=True)
            await decoded(1)
            await canonical(1, "PLAYING")
            await command("favorite_play", id=identifier)
            await decoded(2)
            recalled = await canonical(1, "PLAYING")
            assert recalled["queue"]["count"] == 1 and recalled["media"]["title"] == "Synthetic tone 2"
            assert backend.read_property("volume") == 25 and backend.read_property("mute") is False
            assert backend.read_property("af") == graph
            evidence["favorite"] = {"stable_id_recalled": True, "actual_source": 2,
                                    "gain_and_dsp_retained": True, "raw_url_omitted": True}
            await command("stop")
            assert backend.read_property("idle-active") is True
            await canonical(1, "STOPPED")
    finally:
        release.set()
        if pending_play is not None and not pending_play.done():
            pending_play.cancel()
            await asyncio.gather(pending_play, return_exceptions=True)
        try:
            if server is not None:
                await server.close()
        finally:
            try:
                await media_server.close()
            finally:
                if backend is not None:
                    backend.close()
        await asyncio.sleep(0)
        pending_tasks = [task for task in asyncio.all_tasks() - baseline if not task.done()]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        assert not pending_tasks, f"Web native fixture leaked tasks: {pending_tasks!r}"
        assert session is None or session.closed
        assert all(listener.fileno() == -1 for listener in listeners)
        assert backend is None or (backend._handle is None and backend.process is None)
        assert api is None or api._state_monitor_task is None
        assert monitor is None or monitor.done()
        assert server is None or (server.closed and not server.runner.sites)
        assert media_server.closed and not media_server.runner.sites
    evidence["cleanup"] = {"native_handle_destroyed": True, "monitor_stopped": True,
                           "listeners_closed": True, "session_closed": True, "no_pending_tasks": True}
    return evidence


@pytest.mark.skipif(not os.environ.get("ORR_TEST_LIBMPV_PATH"),
                    reason="Web native acceptance requires explicit ORR_TEST_LIBMPV_PATH")
def test_real_libmpv_web_controls(tmp_path: Path) -> None:
    library = Path(os.environ["ORR_TEST_LIBMPV_PATH"]).expanduser().resolve()
    assert library.is_file(), f"Configured libmpv does not exist: {library}"
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root) + os.pathsep + str(root / "tests")}
    completed = run_probe([sys.executable, str(Path(__file__).resolve()), "--probe", str(library), str(tmp_path)],
                          cwd=root, env=env, timeout=35)
    assert completed.returncode == 0, f"Web native acceptance failed\n{completed.stdout}\n{completed.stderr}"
    evidence = json.loads(completed.stdout)
    assert evidence["lane"] == "REAL_BACKEND/null/Web-JSON"
    assert all(evidence["cleanup"].values())
    assert {sample["source"] for sample in evidence["decoded"]} == {1, 2}
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 4 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")

    async def main():
        async with asyncio.timeout(30):
            return await _exercise(sys.argv[2], Path(sys.argv[3]))

    print(json.dumps(asyncio.run(main()), sort_keys=True))
