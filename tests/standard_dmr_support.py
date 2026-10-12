"""Unmodified standard UPnP client against owned HTTP and native audio fixtures.

No SSDP, external media, provider, physical audio or handset claim. The caller
supplies the separately labeled native transport and owns native destruction.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import timedelta
import hashlib
from importlib.metadata import version
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.test_utils import TestServer
import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.mpv_backend import MpvBackend
from fake_linkplay.state import RendererState
from test_mpv_unix_integration import _eventually, _property


async def exercise_standard_dmr(
    backend: MpvBackend, directory: Path, tracks: tuple[Path, Path],
    *, wiim_home_profile: bool = False,
) -> dict[str, Any]:
    # Missing the real client after native opt-in must fail rather than skip.
    from async_upnp_client.aiohttp import AiohttpNotifyServer, AiohttpSessionRequester
    from async_upnp_client.client_factory import UpnpFactory
    from async_upnp_client.profiles.dlna import DmrDevice, TransportState
    from async_upnp_client.utils import str_to_time

    baseline = asyncio.all_tasks()
    started = asyncio.get_running_loop().time()
    payloads = [track.read_bytes() for track in tracks]
    hashes = [hashlib.sha256(payload).hexdigest() for payload in payloads]
    assert len(set(hashes)) == 2, "Native selection must reach different source bytes"
    cfg = Config(uuid="00000000-0000-4000-8000-000000000005",
        advertise_ip="127.0.0.1", bind_address="127.0.0.1",
        state_dir=str(directory / "state"), wiim_home_profile=wiim_home_profile,
        default_volume=backend.initial_volume, default_mute=backend.initial_mute,
        auto_spawn_mpv=False, enable_ssdp=False, enable_mdns=False)
    await backend.initialize_gain()
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)
    requests, receipts, media_requests, unexpected_media = [], [], [], []
    event_tasks, listener_sockets, callback_sockets = [], [], []
    urls: list[str] = []
    expected_targets = {}
    evidence = {"client_version": version("async-upnp-client"),
        "mpv_version": await _property(backend, "mpv-version"),
        "wiim_home_profile": wiim_home_profile, "track_sha256": hashes,
        "decoded": [], "events_before_poll": []}

    @web.middleware
    async def record(request, handler):
        action = request.headers.get("SOAPAction", "").strip('"').rsplit("#", 1)[-1]
        entry = {"method": request.method, "path": request.path, "action": action,
                 "sid": request.headers.get("SID")}
        if action:
            assert "ControlSource" not in await request.text(), "Client injected vendor argument"
        requests.append(entry)
        response = await handler(request)
        entry["status"] = response.status
        return response

    async def media(request):
        media_requests.append(request.raw_path)
        index = expected_targets.get(request.raw_path)
        if index is None:
            unexpected_media.append(request.raw_path)
            return web.Response(status=400, text="Changed or unowned media target")
        return web.Response(body=payloads[index], content_type="audio/wav")

    app = api.app()
    app.middlewares.append(record)
    app.router.add_get("/fixture-media/{name}", media)
    server = TestServer(app, shutdown_timeout=1)
    notify = dmr = session = monitor = None
    try:
        async with AsyncExitStack() as resources:
            resources.push_async_callback(server.close)
            await server.start_server()
            cfg.http_api_port = cfg.upnp_port = server.port
            listener_sockets = [sock for site in server.runner.sites for sock in site._server.sockets]
            monitor = api._state_monitor_task
            assert monitor is not None and not monitor.done()
            urls = [str(server.make_url(f"/fixture-media/{track.name}"))
                    + "?literal=%252F&value=a%26b%3Dc" for track in tracks]
            base = str(server.make_url("/"))[:-1]
            expected_targets = {url.removeprefix(base): index for index, url in enumerate(urls)}
            # Test transport safety boundary only; no production client or
            # schema/action/event method is changed. Unix backend has no such set.
            if hasattr(backend, "_allowed_media"):
                backend._allowed_media = set(urls)
            session = await resources.enter_async_context(
                ClientSession(timeout=ClientTimeout(total=5), trust_env=False))
            requester = AiohttpSessionRequester(session, timeout=5)
            device = await UpnpFactory(requester).async_create_device(str(server.make_url("/description.xml")))
            assert DmrDevice.is_profile_device(device), [s.service_id for s in device.all_services]
            canonical = {f"urn:upnp-org:serviceId:{name}" for name in
                         ("AVTransport", "RenderingControl", "ConnectionManager")}
            assert canonical <= {s.service_id for s in device.all_services}
            notify = AiohttpNotifyServer(requester, source=("127.0.0.1", 0))
            resources.push_async_callback(notify.async_stop_server)
            await notify.async_start_server()
            callback_sockets = list(notify._server.sockets)
            dmr = DmrDevice(device, notify.event_handler)
            resources.push_async_callback(dmr.async_unsubscribe_services)

            def received(service, variables):
                receipts.append({"service": service.service_id.rsplit(":", 1)[-1],
                    "values": {var.name: var.value for var in variables}})
            dmr.on_event = received  # Public callback, library retains LastChange parsing.
            await dmr.async_subscribe_services()
            assert dmr.is_subscribed and len(api.event_subscribers) == 3
            assert {item["path"] for item in requests if item["method"] == "SUBSCRIBE"} == {
                "/upnp/event/" + name for name in ("AVTransport", "RenderingControl", "ConnectionManager")}
            subscribed_ids = set(api.event_subscribers)

            async def delivered(service, expected, after=0, *, parsed_change=False, position_near=None):
                def position_matches(values):
                    if position_near is None:
                        return True
                    parsed = str_to_time(values.get("RelativeTimePosition", ""))
                    # Event positions are whole seconds; native null output can
                    # settle just below the requested integer after seek.
                    return parsed is not None and abs(parsed.total_seconds() - position_near) <= 1

                async def match():
                    return next((item for item in reversed(receipts[after:])
                        if item["service"] == service
                        and (not parsed_change or "LastChange" not in item["values"])
                        and all(item["values"].get(key) == value for key, value in expected.items())
                        and position_matches(item["values"])), None)
                result = await _eventually(f"client parsed {service} {expected}", match, timeout=3)
                # No SOAP Get action or HTTP state poll can make these event
                # assertions green; binding GETs fetched descriptions only.
                assert not any(item["action"].startswith("Get") or item["path"] == "/httpapi.asp"
                               for item in requests), requests
                evidence["events_before_poll"].append({"service": service,
                    "expected_typed_values": expected, "last_change_expanded": parsed_change,
                    "changed_variables": sorted(result["values"]),
                    "event_position": result["values"].get("RelativeTimePosition")})
                return result

            await delivered("ConnectionManager", {"SourceProtocolInfo": "",
                "SinkProtocolInfo": "http-get:*:audio/*:*", "CurrentConnectionIDs": ""})
            await delivered("AVTransport", {"TransportState": "STOPPED"}, parsed_change=True)
            await delivered("RenderingControl", {"Volume": cfg.default_volume, "Mute": cfg.default_mute}, parsed_change=True)
            assert dmr.transport_state is TransportState.STOPPED
            assert dmr.volume_level == cfg.default_volume / 100 and dmr.is_volume_muted is cfg.default_mute

            async def native_source(index):
                path = urls[index]
                async def sample():
                    if await _property(backend, "path") != path:
                        return None
                    entries = [entry for entry in await _property(backend, "playlist")
                               if entry.get("current") and entry.get("playing")]
                    selected = [track for track in await _property(backend, "track-list")
                                if track.get("type") == "audio" and track.get("selected")]
                    position = await _property(backend, "playback-time")
                    if (len(entries) != 1 or entries[0]["filename"] != path or not selected
                            or selected[0].get("codec") != "pcm_s16le" or position <= .05
                            or await _property(backend, "current-ao") != "null"
                            or await _property(backend, "pause") is not False):
                        return None
                    assert (await _property(backend, "audio-params"))["samplerate"] == 16000
                    return {"path": path, "native_id": entries[0]["id"], "position": position,
                            "codec": "pcm_s16le", "ao": "null"}
                first = await _eventually("native source selected and decoded", sample, timeout=3)
                async def advanced():
                    last = await sample()
                    return last if last and last["native_id"] == first["native_id"] and last["position"] > first["position"] + .12 else None
                last = await _eventually("same native entry advances PCM", advanced, timeout=3)
                result = {**last, "first_position": first["position"]}
                evidence["decoded"].append(result)
                return result

            async def select(index):
                mark = len(receipts)
                await dmr.async_set_transport_uri(urls[index], f"Synthetic {index}", meta_data="")
                await delivered("AVTransport", {"CurrentTrackURI": urls[index], "TransportState": "STOPPED"}, mark, parsed_change=True)
                assert dmr.current_track_uri == urls[index] and dmr.can_play
                mark = len(receipts)
                await dmr.async_play()
                await delivered("AVTransport", {"CurrentTrackURI": urls[index], "TransportState": "PLAYING"}, mark, parsed_change=True)
                assert dmr.transport_state is TransportState.PLAYING and dmr.current_track_uri == urls[index]
                return await native_source(index)

            first = await select(0)
            mark = len(receipts)
            assert dmr.can_pause
            await dmr.async_pause()
            await delivered("AVTransport", {"TransportState": "PAUSED_PLAYBACK", "CurrentTrackURI": urls[0]}, mark, parsed_change=True)
            assert dmr.transport_state is TransportState.PAUSED_PLAYBACK
            assert await _property(backend, "pause") is True
            await asyncio.sleep(.15)
            before = await _property(backend, "playback-time")
            await asyncio.sleep(.25)
            after = await _property(backend, "playback-time")
            assert abs(after - before) < .04, (before, after)
            evidence["paused_position"] = {"before": before, "after": after}
            # Standard typed Seek, using the actual allowed actions received via GENA.
            assert dmr.can_seek_rel_time
            mark = len(receipts)
            await dmr.async_seek_rel_time(timedelta(seconds=5))
            native_seek = await _property(backend, "playback-time")
            assert abs(native_seek - 5) < .2
            assert await _property(backend, "pause") is True
            seek_event = await delivered("AVTransport", {"TransportState": "PAUSED_PLAYBACK", "CurrentTrackURI": urls[0]},
                                         mark, parsed_change=True, position_near=native_seek)
            assert dmr.media_position is not None and abs(dmr.media_position - native_seek) <= 1
            evidence["paused_seek"] = {"native_position": native_seek,
                "event_position": seek_event["values"]["RelativeTimePosition"], "client_position": dmr.media_position}
            mark = len(receipts)
            assert dmr.can_play
            await dmr.async_play()
            await delivered("AVTransport", {"TransportState": "PLAYING", "CurrentTrackURI": urls[0]}, mark, parsed_change=True)
            resumed = await native_source(0)
            assert resumed["native_id"] == first["native_id"], "Resume must preserve the owned entry"

            mark = len(receipts)
            await dmr.async_set_volume_level(.37)
            await delivered("RenderingControl", {"Volume": 37}, mark, parsed_change=True)
            assert await _property(backend, "volume") == pytest.approx(37)
            assert dmr.volume_level == .37
            for muted in (True, False):
                mark = len(receipts)
                await dmr.async_mute_volume(muted)
                await delivered("RenderingControl", {"Mute": muted}, mark, parsed_change=True)
                assert await _property(backend, "mute") is muted
                assert dmr.is_volume_muted is muted
            second = await select(1)
            assert second["native_id"] != first["native_id"]
            mark = len(receipts)
            assert dmr.can_stop
            await dmr.async_stop()
            await delivered("AVTransport", {"TransportState": "STOPPED", "CurrentTrackURI": urls[1]}, mark, parsed_change=True)
            assert await _property(backend, "idle-active") is True
            assert dmr.transport_state is TransportState.STOPPED
            expected_commands = ["SetAVTransportURI", "Play", "Pause", "Seek", "Play", "SetVolume",
                                 "SetMute", "SetMute", "SetAVTransportURI", "Play", "Stop"]
            assert [item["action"] for item in requests if item["action"]] == expected_commands
            evidence["typed_commands_before_poll"] = expected_commands
            assert not unexpected_media and set(media_requests) == set(expected_targets), media_requests

            # Only after every event/native assertion: ordinary library polling
            # must agree and must not be needed to manufacture event evidence.
            await dmr.async_update()
            assert dmr.transport_state is TransportState.STOPPED
            assert dmr.current_track_uri == urls[1] and dmr.volume_level == .37 and dmr.is_volume_muted is False
            evidence["post_event_poll_agrees"] = True
            await dmr.async_subscribe_services()  # Normal library renewal, including CM.
            assert set(api.event_subscribers) == subscribed_ids
            renewals = [item for item in requests if item["method"] == "SUBSCRIBE" and item["sid"]]
            assert len(renewals) == 3 and all(item["status"] == 200 for item in renewals)
            event_tasks = [item.task for item in api.event_subscribers.values() if item.task is not None]
            await dmr.async_unsubscribe_services()
            assert not dmr.is_subscribed and not api.event_subscribers
            assert len([item for item in requests if item["method"] == "UNSUBSCRIBE" and item["status"] == 200]) == 3
            evidence["renewed_and_unsubscribed_services"] = 3
    finally:
        # Native lifetime belongs to the wrapper; the shared flow stops active
        # playback even when a client assertion fails, then verifies HTTP cleanup.
        await backend.stop()
        await asyncio.sleep(0)
        pending = [task for task in asyncio.all_tasks() - baseline if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        assert not pending, f"Standard DMR fixture leaked tasks: {pending!r}"
        assert not api.event_subscribers
        assert session is None or session.closed
        assert monitor is None or monitor.done()
        assert all(task.done() for task in event_tasks)
        assert all(sock.fileno() == -1 for sock in listener_sockets + callback_sockets)
    evidence["cleanup"] = {"subscriptions_removed": True, "monitor_and_event_tasks_done": True,
                           "session_and_listeners_closed": True, "no_pending_tasks": True}
    evidence["elapsed_seconds"] = round(asyncio.get_running_loop().time() - started, 3)
    return evidence
