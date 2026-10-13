"""One bounded REAL_BACKEND/null acceptance flow for the owned Web JSON API.

Opt in with ORR_TEST_LIBMPV_PATH. Real aiohttp requests use production HttpApi,
MpvBackend and libmpv; only the command transport is the existing test-only
ctypes adapter. Generated finite WAVs are served on literal loopback, with null
output and no accounts/external media. This verifies Web wiring and native
readback, not a browser, production Unix IPC, audible output or DSP samples.
The gain recovery case discards one real readback in that test transport to
induce uncertainty, then measures real Stop and volume-zero recovery.
The existing test_libmpv_dsp.py digital-PCM gate remains the sample-level proof.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from html import escape
import json
import locale
import os
from pathlib import Path
import re
import sys
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.test_utils import TestServer
import pytest

from fake_linkplay.config import Config
from fake_linkplay.favorites import FavoritesStore
from fake_linkplay.http_api import HttpApi
from fake_linkplay.playlist_files import decode_playlist
from fake_linkplay.queue import PlaybackQueue, QueueItem, serialize_queue_context
from fake_linkplay.queue_policy import REPEAT_ALL
from fake_linkplay.queue_playlist import encode_current_playlist
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
    dsp_entered, dsp_release = asyncio.Event(), asyncio.Event()
    dsp_tasks: list[asyncio.Task] = []
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
        "playlist_export": {"requests": [], "scope": "HTTP bytes in memory; no browser or disk-save claim"},
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

            async def export(action, context, *, expected=200, **fields):
                # The live handle and real HTTP/media servers remain present.
                # Compare the owner and native readbacks around every export,
                # including rejected consent and stale-revision requests.
                state_before = await snapshot()
                owned_queue = api.state.queue
                queue_value = deepcopy(owned_queue)
                handle = backend._handle
                assert handle is not None
                commands_before = len(backend.commands)
                requests_before = list(media_requests)
                properties = ("playlist", "idle-active", "pause", "af", "volume", "mute", "track-list")
                native_before = {key: backend.read_property(key) for key in properties}
                payload = {"action": action, "expected_session": context["session_id"],
                           "expected_generation": context["generation"], **fields}
                async with session.post(server.make_url(BASE + "/playlist/export"), json=payload,
                    headers={"Origin": origin, "X-ORR-CSRF": token}) as response:
                    body = await response.read()
                    assert response.status == expected, (response.status, body)
                    assert response.content_type == "application/json"
                    assert response.headers["Cache-Control"] == "no-store"
                    assert response.headers["Content-Length"] == str(len(body))
                    if expected == 200 and action == "download":
                        assert response.headers["Content-Disposition"] == (
                            'attachment; filename="open-room-renderer-playlist.json"')
                        assert body == encode_current_playlist(queue_value)
                        result = decode_playlist(body)
                        assert result == {"version": 1, "name": owned_queue.name,
                            "index": owned_queue.index, "mode": state_before["queue"]["mode"],
                            "urls": [item.url for item in owned_queue.items]}
                    else:
                        assert "Content-Disposition" not in response.headers
                        assert not any(url.encode() in body for url in urls), "Export preview/error leaked a target"
                        result = json.loads(body)
                        if expected == 200:
                            assert set(result) == {"version", "session_id", "generation", "revision",
                                "name", "count", "index", "mode", "size_bytes", "filename"}
                            assert re.fullmatch(r"[0-9a-f]{64}", result["revision"])
                            assert result == {"version": 1, "session_id": context["session_id"],
                                "generation": context["generation"], "revision": result["revision"],
                                "name": owned_queue.name, "count": owned_queue.count,
                                "index": owned_queue.index, "mode": state_before["queue"]["mode"],
                                "size_bytes": len(encode_current_playlist(queue_value)),
                                "filename": "open-room-renderer-playlist.json"}
                        else:
                            assert result["outcome"] == "REJECTED"
                after = await snapshot()
                assert backend._handle == handle
                assert api.state.queue is owned_queue and api.state.queue == queue_value
                assert backend.commands[commands_before:] == [] and media_requests == requests_before
                assert {key: backend.read_property(key) for key in properties} == native_before
                for key in ("session_id", "generation", "queue", "transport", "gain", "dsp"):
                    assert after[key] == state_before[key], (key, state_before[key], after[key])
                if state_before["transport"]["phase"] == "STOPPED":
                    assert native_before["idle-active"] is True and native_before["playlist"] == []
                    assert not any(track.get("selected") and track.get("type") == "audio"
                                   for track in native_before["track-list"])
                evidence["playlist_export"]["requests"].append({"action": action, "status": expected,
                    "phase": state_before["transport"]["phase"], "native_mutations": 0,
                    "media_requests": 0, "queue_generation_dsp_gain_retained": True})
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
            # Edit the metadata-bearing canonical queue, not its lossy URL
            # export. A duplicated URL is a distinct occurrence: moving the
            # selected original must retain its metadata and follow its index.
            original_queue = deepcopy(api.state.queue)
            edit_generation = staged["generation"]
            before = len(backend.commands)
            for action, fields, titles, index in (
                ("playlist_append", {"urls": [urls[0]]}, ["Synthetic tone 1", "Synthetic tone 2", ""], 1),
                ("playlist_move", {"from_index": 1, "to_index": 3}, ["Synthetic tone 2", "", "Synthetic tone 1"], 3),
                ("playlist_remove", {"index": 2}, ["Synthetic tone 2", "Synthetic tone 1"], 2),
                ("playlist_move", {"from_index": 2, "to_index": 1}, ["Synthetic tone 1", "Synthetic tone 2"], 1),
            ):
                old_queue = api.state.queue
                old_value = deepcopy(old_queue)
                generation = (await snapshot())["generation"]
                await command(action, guard=True, **fields)
                edited_queue = await canonical(index, "STOPPED")
                assert edited_queue["generation"] == generation + 1
                assert edited_queue["queue"]["playlist_edit_reason"] == "supported"
                assert [item.title for item in api.state.queue.items] == titles
                assert api.state.queue.current == original_queue.current
                assert api.state.queue.current is not old_queue.current
                assert api.state.queue is not old_queue and old_queue == old_value
                assert api.state.curpos == 0
                assert backend.commands[before:] == [] and not media_requests
                assert backend.read_property("idle-active") is True
                assert backend.read_property("playlist") == []
                assert not any(track.get("selected") and track.get("type") == "audio"
                               for track in backend.read_property("track-list"))
            assert api.state.queue == original_queue
            edited_queue = api.state.queue
            rejected = await command("playlist_append", urls=[urls[0]],
                                     expected_generation=edit_generation, expected=409)
            assert rejected["outcome"] == "REJECTED" and rejected["error"] == "stale_generation"
            assert api.state.queue is edited_queue
            assert backend.commands[before:] == [] and not media_requests
            evidence["playlist_edits"] = {
                "append_move_remove": True, "duplicate_selected_occurrence_retained": True,
                "canonical_metadata_retained": True, "detached_originals_unchanged": True,
                "position_reset": True, "stale_append": 409,
                "native_mutations_before_explicit_play": 0, "media_requests_before_explicit_play": 0,
            }

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

            before = len(backend.commands)
            active_queue = api.state.queue
            active_playlist = backend.read_property("playlist")
            for action, fields in (
                ("playlist_append", {"urls": [urls[0]]}),
                ("playlist_replace", {"source": {"format": "urls", "urls": urls,
                    "name": "Blocked while playing", "index": 1, "mode": "normal"}}),
            ):
                rejected = await command(action, expected=409, guard=True, **fields)
                assert rejected["outcome"] == "REJECTED"
                assert api.state.queue is active_queue
                assert backend.commands[before:] == []
                assert backend.read_property("playlist") == active_playlist
                await canonical(1, "PLAYING")
            evidence["playlist_edits"]["playing_rejected_without_native_mutation"] = True

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

            # Export is read-only even while actual PCM is playing. The
            # existing same-generation mode edit below must invalidate its
            # prepared bytes without reloading the selected native source.
            export_context = await snapshot()
            prepared_export = await export("prepare", export_context)
            active_export = await export("download", export_context,
                expected_export=prepared_export["revision"], private_export_confirmed=True)
            assert active_export == {"version": 1, "name": queue.name, "index": 1,
                                    "mode": "repeat-all", "urls": urls}
            evidence["playlist_export"]["playing_round_trip"] = {
                "name": active_export["name"], "index": 1, "mode": "repeat-all", "source_order": [1, 2],
                "exact_codec_bytes": True, "metadata_omitted": True}

            before = len(backend.commands)
            generation = (await snapshot())["generation"]
            await command("queue_mode", queue_name=queue.name, mode="normal", guard=True)
            assert backend.commands[before:] == []
            assert backend.read_property("playlist") == native_before
            changed = await canonical(1, "PLAYING")
            assert changed["generation"] == generation and changed["queue"]["mode"] == "normal"
            evidence["queue_policy"] = {"normal_without_reload": True, "generation_retained": True}
            rejected = await export("download", export_context, expected=409,
                expected_export=prepared_export["revision"], private_export_confirmed=True)
            assert rejected["error"] == "stale_export"
            evidence["playlist_export"]["same_generation_mode_staleness"] = 409
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

            # Extend this same native lifetime with the complete Web editor.
            # The only held operation delays the inherited, real native PEQ
            # call; it never substitutes a graph, acknowledgement or snapshot.
            async def editor():
                dsp = (await snapshot())["dsp"]
                assert dsp["status"] == "CONFIRMED", dsp
                view = dsp["editor"]
                assert re.fullmatch(r"[0-9a-f]{64}", view["revision"])
                assert view["source"] == "global" and view["channel_mode"] == "Stereo"
                assert [entry["param_name"] for entry in view["bands"]] == [
                    f"{letter}_{field}" for letter in "abcdefghij" for field in ("freq", "gain", "q", "mode")]
                assert all(re.fullmatch(r"[0-9a-f]{64}", entry["id"]) for entry in view["presets"])
                assert "synthetic.web" not in json.dumps(view), "Editor leaked the plugin identifier"
                return view

            def curve(view, **changes):
                return [{**entry, "value": changes.get(entry["param_name"], entry["value"])}
                        for entry in view["bands"]]

            async def edit(action, view, **fields):
                return await command(action, expected_dsp=view["revision"], **fields)

            original_editor = await editor()
            cut_bands = curve(original_editor, a_freq=660, a_gain=-6, a_q=1, a_mode=1)
            original_peq = backend.peq

            async def held_peq(bands):
                dsp_entered.set()
                await dsp_release.wait()
                await original_peq(bands)

            backend.peq = held_peq
            before = len(backend.commands)
            applying = asyncio.create_task(edit("dsp_bands", original_editor, bands=cut_bands))
            dsp_tasks.append(applying)
            await asyncio.wait_for(dsp_entered.wait(), timeout=2)
            pending = (await snapshot())["dsp"]
            assert pending == {"status": "PENDING", "balance": None, "eq_enabled": None,
                               "name": "", "editor": None}, pending
            assert not applying.done() and backend.read_property("af") == graph

            # Observe the second HTTP command entering the normal dispatcher
            # before releasing PEQ, proving it actually waits behind the lock.
            dispatch_entered = asyncio.Event()
            original_dispatch = api.web_api._dispatch

            async def observed_dispatch(value):
                dispatch_entered.set()
                return await original_dispatch(value)

            api.web_api._dispatch = observed_dispatch
            stale = asyncio.create_task(edit("dsp_bands", original_editor, expected=409,
                                             bands=curve(original_editor, a_gain=-18)))
            dsp_tasks.append(stale)
            try:
                await asyncio.wait_for(dispatch_entered.wait(), timeout=2)
                assert not stale.done() and api._dsp_command_lock.locked()
            finally:
                api.web_api._dispatch = original_dispatch
                backend.peq = original_peq
                dsp_release.set()
            await applying
            rejected = await stale
            assert rejected["error"] == "stale_dsp" and rejected["outcome"] == "REJECTED"
            native_edits = backend.commands[before:]
            assert len(native_edits) == 1 and native_edits[0][:2] == ["af", "add"], native_edits
            cut_graph = backend.read_property("af")
            assert cut_graph != graph
            assert "equalizer=f=660.000" in cut_graph[0]["params"]["graph"]
            assert ":g=-6.000" in cut_graph[0]["params"]["graph"]
            edited = await editor()
            assert edited["bands"] == cut_bands and edited["revision"] != original_editor["revision"]
            assert backend.read_property("volume") == 25 and backend.read_property("mute") is False
            assert backend.read_property("playlist") == native_before

            # Library-only mutations must not write to the audio backend at all.
            before = len(backend.commands)
            await edit("dsp_preset_save", edited, name="Web native cut")
            saved_editor = await editor()
            saved_preset = next(entry for entry in saved_editor["presets"] if entry["name"] == "Web native cut")
            assert saved_preset["kind"] == "saved" and saved_preset["shared"] is False
            assert saved_editor["bands"] == cut_bands and saved_editor["revision"] != edited["revision"]
            assert backend.commands[before:] == [] and backend.read_property("af") == cut_graph

            await command("eq", enabled=False)
            assert backend.read_property("af") == bypass
            bypass_editor = await editor()
            bypass_bands = curve(bypass_editor, a_gain=-9)
            before = len(backend.commands)
            await edit("dsp_bands", bypass_editor, bands=bypass_bands)
            assert backend.commands[before:] == [] and backend.read_property("af") == bypass
            assert (await snapshot())["dsp"]["eq_enabled"] is False
            bypass_edited = await editor()
            assert bypass_edited["bands"] == bypass_bands
            await edit("dsp_preset_load", bypass_edited, preset_id=saved_preset["id"])
            restored_editor = await editor()
            assert restored_editor["bands"] == cut_bands
            assert (await snapshot())["dsp"]["eq_enabled"] is True
            assert backend.read_property("af") == cut_graph

            before = len(backend.commands)
            await edit("dsp_preset_rename", restored_editor, preset_id=saved_preset["id"], name="Renamed native cut")
            renamed_editor = await editor()
            renamed = next(entry for entry in renamed_editor["presets"] if entry["name"] == "Renamed native cut")
            assert renamed["id"] != saved_preset["id"] and renamed["kind"] == "saved"
            assert not any(entry["id"] == saved_preset["id"] for entry in renamed_editor["presets"])
            assert renamed_editor["bands"] == cut_bands
            assert backend.commands[before:] == [] and backend.read_property("af") == cut_graph
            await edit("dsp_preset_delete", renamed_editor, preset_id=renamed["id"])
            deleted_editor = await editor()
            assert not any(entry["id"] == renamed["id"] for entry in deleted_editor["presets"])
            assert deleted_editor["bands"] == cut_bands
            assert backend.commands[before:] == [] and backend.read_property("af") == cut_graph
            assert backend.read_property("playlist") == native_before
            assert backend.read_property("volume") == 25 and backend.read_property("mute") is False
            evidence["dsp_editor"] = {
                "scope": "REAL_BACKEND/null/readback; no added PCM matrix or physical-output claim",
                "native_web_cut_graph": cut_graph, "complete_bands": len(cut_bands),
                "pending_hides_editor": True, "stale_after_lock": 409, "stale_extra_native_mutations": 0,
                "bypass_edit_retained": True, "saved_load_restores_curve_and_enables": True,
                "save_rename_delete_native_mutations": 0, "opaque_preset_ids": True,
                "source_and_gain_retained": True,
            }
            # Preserve the original favorite/source-change retention proof.
            await edit("dsp_bands", deleted_editor, bands=original_editor["bands"])
            assert backend.read_property("af") == graph
            assert (await editor())["bands"] == original_editor["bands"]

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

            # Repair only the stored target while its detached favorite is
            # actually playing. The existing command wrapper supplies the
            # same-origin/CSRF guard; library edits do not take a generation.
            # A second record makes order/sibling retention non-vacuous.
            sibling = await command("favorite_add", name="Other owned tone", url=urls[0])
            library_before = api.favorites.document()
            assert [item["id"] for item in library_before["favorites"]] == [identifier, sibling["favorite_id"]]
            original_favorite = library_before["favorites"][0]
            assert original_favorite["editable"] is True and original_favorite["url"] == urls[1]
            assert {key: original_favorite[key] for key in ("title", "artist", "album", "artwork")} == {
                "title": "Synthetic tone 2", "artist": "Owned fixture", "album": "Web acceptance", "artwork": ""}
            repair_queue = api.state.queue
            repair_item = repair_queue.current
            repair_queue_value = deepcopy(repair_queue)
            repair_state = await canonical(1, "PLAYING")
            repair_handle = backend._handle
            assert repair_handle is not None and repair_item.url == urls[1]
            properties = ("path", "playlist", "idle-active", "pause", "af", "volume", "mute",
                          "track-list", "audio-params", "current-ao")
            repair_native = {key: backend.read_property(key) for key in properties}
            assert repair_native["path"] == urls[1] and repair_native["idle-active"] is False
            assert repair_native["pause"] is False and repair_native["current-ao"] == "null"
            before = len(backend.commands)
            requests_before = list(media_requests)
            repair_evidence = []
            for policy, target in (("keep", urls[0]), ("clear", urls[1])):
                assert api.favorites.document()["favorites"][0]["url"] != target
                result = await command("favorite_target", id=identifier, url=target, metadata=policy)
                assert not any(url in json.dumps(result) for url in urls), "Target edit response leaked a raw target"
                expected_record = {**original_favorite, "url": target}
                if policy == "clear":
                    expected_record.update(dict.fromkeys(("title", "artist", "album", "artwork"), ""))
                expected_library = {"version": 1, "favorites": [expected_record, library_before["favorites"][1]]}
                assert api.favorites.document() == expected_library
                persisted_fields = ("id", "name", "url", "title", "artist", "album", "artwork")
                assert json.loads(api.favorites.path.read_bytes()) == {"version": 1, "favorites": [
                    {key: item[key] for key in persisted_fields} for item in expected_library["favorites"]]}
                assert FavoritesStore(cfg).document() == expected_library
                async with session.get(server.make_url(BASE + "/favorites")) as response:
                    repaired = await response.json()
                    assert response.status == 200, repaired
                assert [item["id"] for item in repaired["favorites"]] == [identifier, sibling["favorite_id"]]
                assert repaired["favorites"][0]["name"] == original_favorite["name"]
                assert not any(url in json.dumps(repaired) for url in urls), "Repaired favorites leaked a raw target"
                after = await canonical(1, "PLAYING")
                assert api.state.queue is repair_queue and api.state.queue.current is repair_item
                assert api.state.queue == repair_queue_value and repair_item.url == urls[1]
                assert backend._handle == repair_handle
                assert backend.commands[before:] == [] and media_requests == requests_before
                assert {key: backend.read_property(key) for key in properties} == repair_native
                for key in ("session_id", "generation", "queue", "transport", "gain", "dsp"):
                    assert after[key] == repair_state[key], (key, repair_state[key], after[key])
                repair_evidence.append({"metadata": policy, "status": 200,
                    "exact_target_persisted": True, "metadata_policy_persisted": True,
                    "id_name_order_and_sibling_retained": True, "reload_matches": True,
                    "detached_queue_identity_and_value_retained": True,
                    "generation_transport_dsp_gain_retained": True, "native_properties_retained": True,
                    "native_mutations": 0, "media_requests": 0, "raw_url_omitted": True})
            evidence["favorite_target"] = {"requests": repair_evidence,
                "scope": "same-origin CSRF-protected Web JSON action; no browser or repaired-target playback"}
            await command("stop")
            assert backend.read_property("idle-active") is True
            await canonical(1, "STOPPED")

            # Import each supported authored format through real Web HTTP.
            # The duplicate at position three stays selected through the edit,
            # and only the following explicit Play may load its actual source.
            authored_urls = [urls[0], urls[1], urls[0]]
            document = {"version": 1, "name": "Web imported duplicates", "index": 3,
                        "mode": "repeat-all", "urls": authored_urls}
            source_options = {"name": document["name"], "index": 3, "mode": "repeat-all"}
            sources = (
                {"format": "json", "text": json.dumps(document)},
                {"format": "m3u", "text": "#EXTM3U\n#EXTINF:30,Ignored display hint\n"
                    + "\n".join(authored_urls) + "\n", **source_options},
                {"format": "urls", "urls": authored_urls, **source_options},
            )
            before = len(backend.commands)
            requests_before = list(media_requests)
            native_before = backend.read_property("playlist")
            imported_formats = []
            for source in sources:
                generation = (await snapshot())["generation"]
                await command("playlist_replace", source=source, guard=True)
                imported = await canonical(3, "STOPPED")
                assert imported["generation"] == generation + 1
                assert imported["queue"]["name"] == document["name"]
                assert imported["queue"]["mode"] == "repeat-all"
                assert imported["queue"]["count"] == 3
                assert [item.url for item in api.state.queue.items] == authored_urls
                assert [item.title for item in api.state.queue.items] == ["", "", "first.wav"]
                assert api.state.queue.current.title_is_fallback is True
                assert all(not item.metadata for item in api.state.queue.items)
                assert backend.commands[before:] == [] and media_requests == requests_before
                assert backend.read_property("playlist") == native_before
                assert backend.read_property("idle-active") is True
                assert not any(track.get("selected") and track.get("type") == "audio"
                               for track in backend.read_property("track-list"))
                imported_formats.append(source["format"])
            generation = (await snapshot())["generation"]
            await command("playlist_move", from_index=3, to_index=2, guard=True)
            await canonical(2, "STOPPED")
            assert [item.url for item in api.state.queue.items] == [urls[0], urls[0], urls[1]]
            imported_queue = api.state.queue
            rejected = await command("playlist_replace", source=sources[-1],
                                     expected_generation=generation, expected=409)
            assert rejected["outcome"] == "REJECTED" and rejected["error"] == "stale_generation"
            assert api.state.queue is imported_queue
            assert backend.commands[before:] == [] and media_requests == requests_before
            assert backend.read_property("playlist") == native_before
            assert backend.read_property("idle-active") is True
            export_context = await snapshot()
            prepared_export = await export("prepare", export_context)
            rejected = await export("download", export_context, expected=400,
                expected_export=prepared_export["revision"], private_export_confirmed=False)
            assert rejected["error"] == "invalid_request"
            stopped_export = await export("download", export_context,
                expected_export=prepared_export["revision"], private_export_confirmed=True)
            assert stopped_export == {"version": 1, "name": document["name"], "index": 2,
                "mode": "repeat-all", "urls": [urls[0], urls[0], urls[1]]}
            evidence["playlist_export"]["stopped_duplicate_round_trip"] = {
                "name": stopped_export["name"], "index": 2, "mode": "repeat-all", "source_order": [1, 1, 2],
                "exact_codec_bytes": True, "consent_required": 400, "no_hidden_playback": True}
            await command("play", guard=True)
            selected_source = await decoded(1)
            await canonical(2, "PLAYING")
            assert len(media_requests) > len(requests_before)
            evidence["playlist_import"] = {
                "formats": imported_formats, "duplicate_count": 2,
                "selected_index_before_move": 3, "selected_index_after_move": 2,
                "stale_replace": 409, "native_mutations_before_explicit_play": 0,
                "media_requests_before_explicit_play": 0, "actual_source_after_explicit_play": 1,
                "native_id_after_explicit_play": selected_source["native_id"],
            }
            # One synthetic reply loss in the existing ctypes transport makes
            # the production gain path uncertain after a real native write.
            # Never manufacture UNKNOWN by changing backend or renderer state.
            original_command = backend.command
            lost_readbacks = []

            async def lose_one_gain_readback(args, timeout=5.0):
                result = await original_command(args, timeout)
                if args == ["get_property", "volume"] and not lost_readbacks:
                    assert result == {"error": "success", "data": 26}
                    lost_readbacks.append(result)
                    return {"error": "synthetic lost reply after real native volume readback"}
                return result

            backend.command = lose_one_gain_readback
            try:
                failed = await command("volume", value=26, expected=502)
            finally:
                backend.command = original_command
            assert failed["outcome"] == "UNKNOWN" and failed["error"] == "command_failed"
            assert len(lost_readbacks) == 1
            assert backend.read_property("volume") == 26 and backend.read_property("mute") is False
            unknown_gain = {"status": "UNKNOWN", "volume": None, "mute": None, "last_confirmed": None}
            assert (await snapshot())["gain"] == unknown_gain
            assert api.state.gain_confirmed is False and backend.gain_confirmed is False

            await command("stop")
            assert backend.read_property("idle-active") is True
            await canonical(2, "STOPPED")
            stopped = await snapshot()
            assert stopped["gain"] == unknown_gain, "Stop must not claim it reconfirmed gain"
            assert backend.read_property("volume") == 26 and backend.read_property("mute") is False
            assert backend.read_property("playlist") == []

            # The recovery itself has no injected response: both writes and
            # both confirmations pass through to the same live native handle.
            gain_calls = []

            async def observe_gain_recovery(args, timeout=5.0):
                result = await original_command(args, timeout)
                if (isinstance(args, list) and args[0] in {"set_property", "get_property"}
                        and args[1] in {"volume", "mute"}):
                    gain_calls.append({"command": args, "response": result})
                return result

            before = len(backend.commands)
            requests_before = list(media_requests)
            backend.command = observe_gain_recovery
            try:
                await command("volume", value=0)
            finally:
                backend.command = original_command
            assert gain_calls == [
                {"command": ["set_property", "volume", 0], "response": {"error": "success"}},
                {"command": ["get_property", "volume"], "response": {"error": "success", "data": 0}},
                {"command": ["set_property", "mute", False], "response": {"error": "success"}},
                {"command": ["get_property", "mute"], "response": {"error": "success", "data": False}},
            ]
            assert backend.commands[before:] == [["set_property", "volume", 0], ["set_property", "mute", False]]
            assert backend.read_property("volume") == 0 and backend.read_property("mute") is False
            assert api.state.gain_confirmed is True and backend.gain_confirmed is True
            recovered = await canonical(2, "STOPPED")
            assert recovered["gain"] == {"status": "CONFIRMED", "volume": 0, "mute": False,
                                          "last_confirmed": {"volume": 0, "mute": False}}
            assert recovered["generation"] == stopped["generation"]
            assert recovered["queue"] == stopped["queue"] and recovered["dsp"] == stopped["dsp"]
            assert backend.read_property("af") == graph and backend.read_property("playlist") == []
            assert backend.read_property("idle-active") is True and media_requests == requests_before
            evidence["gain_zero_recovery"] = {
                "scope": "REAL_BACKEND/null/readback with one synthetic lost gain reply; no physical-silence claim",
                "injection": {"kind": "discard one successful native volume readback", "count": len(lost_readbacks),
                              "actual_native_volume": lost_readbacks[0]["data"], "web_http_status": 502},
                "gain_after_successful_stop": stopped["gain"], "native_recovery_calls": gain_calls,
                "gain_after_zero": recovered["gain"], "transport_after_zero": recovered["transport"],
                "source_queue_dsp_retained": True, "media_requests_during_recovery": 0,
            }
    finally:
        release.set()
        dsp_release.set()
        for task in dsp_tasks:
            if not task.done():
                task.cancel()
        if dsp_tasks:
            await asyncio.gather(*dsp_tasks, return_exceptions=True)
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
    assert evidence["dsp_editor"]["complete_bands"] == 40
    assert evidence["dsp_editor"]["stale_extra_native_mutations"] == 0
    assert [request["metadata"] for request in evidence["favorite_target"]["requests"]] == ["keep", "clear"]
    assert all(request["status"] == 200 and request["native_mutations"] == request["media_requests"] == 0
               and all(request[key] for key in ("exact_target_persisted", "metadata_policy_persisted",
                   "id_name_order_and_sibling_retained", "reload_matches",
                   "detached_queue_identity_and_value_retained", "generation_transport_dsp_gain_retained",
                   "native_properties_retained", "raw_url_omitted"))
               for request in evidence["favorite_target"]["requests"])
    assert evidence["playlist_edits"]["canonical_metadata_retained"] is True
    assert evidence["playlist_edits"]["native_mutations_before_explicit_play"] == 0
    assert evidence["playlist_import"]["formats"] == ["json", "m3u", "urls"]
    assert evidence["playlist_import"]["actual_source_after_explicit_play"] == 1
    assert evidence["playlist_export"]["playing_round_trip"]["exact_codec_bytes"] is True
    assert evidence["playlist_export"]["stopped_duplicate_round_trip"]["no_hidden_playback"] is True
    assert evidence["playlist_export"]["same_generation_mode_staleness"] == 409
    assert len(evidence["playlist_export"]["requests"]) == 6
    assert all(request["native_mutations"] == request["media_requests"] == 0
               and request["queue_generation_dsp_gain_retained"]
               for request in evidence["playlist_export"]["requests"])
    assert evidence["gain_zero_recovery"]["injection"]["count"] == 1
    assert evidence["gain_zero_recovery"]["gain_after_successful_stop"]["status"] == "UNKNOWN"
    assert evidence["gain_zero_recovery"]["gain_after_zero"]["volume"] == 0
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 4 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")

    async def main():
        async with asyncio.timeout(30):
            return await _exercise(sys.argv[2], Path(sys.argv[3]))

    print(json.dumps(asyncio.run(main()), sort_keys=True))
