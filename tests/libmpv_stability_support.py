"""Bounded native stability fixture. Only synthetic loopback media is allowed.

The fixture keeps aggregate counters and bounded callback history, so evidence
collection itself cannot masquerade as a renderer RSS leak. The explicit lane
selects the existing libmpv test adapter or production Unix IPC; neither can
fall back to the other. Production code is never replaced by the Unix fixture.
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
import gc
import hashlib
import inspect
from html import escape
import json
import os
from pathlib import Path
import socket
import sys
from typing import Any
from xml.etree import ElementTree as ET

from aiohttp import ClientSession, ClientTimeout, web
import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.queue import PlaybackQueue, QueueItem, serialize_queue_context
from fake_linkplay.state import PlaybackPhase, RendererState
from test_libmpv_integration import _LibmpvBackend, _write_tone


SERVICES = ("AVTransport", "RenderingControl", "PlayQueue")
# These are detection budgets, not an assertion of a perfectly flat allocator.
LIVE_BUDGET = {"tasks": 8, "fds": 8, "rss_kib": 48 * 1024, "threads": 6}
CLEAN_BUDGET = {"tasks": 0, "fds": 2, "rss_kib": 48 * 1024, "threads": 2}


def child_pids(pid: int | None = None) -> set[int]:
    """Read PPid from process stat; some sandboxes omit per-thread children."""
    parent = os.getpid() if pid is None else pid
    children = set()
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        try:
            # A process name can itself contain spaces or right parentheses.
            tail = (process / "stat").read_text().rpartition(")")[2].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        assert len(tail) >= 2, "Process stat omitted parent identity"
        if int(tail[1]) == parent:
            children.add(int(process.name))
    return children


def resources(pid: int | None = None) -> dict[str, int]:
    """Current resident memory, descriptors and threads, rather than high-water RSS."""
    root = Path("/proc/self") if pid is None else Path(f"/proc/{pid}")
    status = dict(line.split(":", 1) for line in (root / "status").read_text().splitlines())
    result = {"fds": len(list((root / "fd").iterdir())), "rss_kib": int(status["VmRSS"].split()[0]),
              "threads": int(status["Threads"]), "children": len(child_pids(pid))}
    if pid is None:
        result["tasks"] = len(asyncio.all_tasks())
    return result


def within_budget(sample: dict[str, int], baseline: dict[str, int], budget: dict[str, int], expected_children: int = 0) -> None:
    assert sample["children"] == expected_children, "Unexpected native fixture child-process count"
    for key, allowance in budget.items():
        assert sample[key] <= baseline[key] + allowance, (key, baseline, sample, budget)


def fields(body: str) -> dict[str, str]:
    return {node.tag.rsplit("}", 1)[-1]: node.text or "" for node in ET.fromstring(body).iter()}


def event_fields(body: str) -> dict[str, str]:
    result = {node.tag.rsplit("}", 1)[-1]: node.text or "" for prop in ET.fromstring(body) for node in prop}
    if "LastChange" in result:
        result.update({"last:" + node.tag.rsplit("}", 1)[-1]: node.attrib["val"]
                       for node in ET.fromstring(result["LastChange"]).iter() if "val" in node.attrib})
    return result


class CountingBackend(_LibmpvBackend):
    def __init__(self, library: str, tracks: tuple[Path, Path], station_url: str) -> None:
        super().__init__(library, tracks)
        self._allowed_media.add(station_url)
        self.loads = 0
        self.source_changes = 0
        self.last_source = ""

    async def command(self, args: list[Any], timeout: float = 5.0) -> dict[str, Any]:
        if args[0] == "loadfile":
            assert args[1] in self._allowed_media
            self.loads += 1
            self.source_changes += int(args[1] != self.last_source)
            self.last_source = args[1]
        return await super().command(args, timeout)


class LibraryPlayer:
    expected_children = 0
    child_pid = None
    eof_events = None

    def __init__(self, library: str, tracks: tuple[Path, Path], station_url: str) -> None:
        self.backend = CountingBackend(library, tracks, station_url)
        self.closed = False

    @property
    def loads(self) -> int:
        return self.backend.loads

    @property
    def source_changes(self) -> int:
        return self.backend.source_changes

    async def property(self, name: str) -> Any:
        return (await self.backend.checked_command(["get_property", name]))["data"]

    def assert_alive(self) -> None:
        assert self.backend._handle is not None and self.backend.process is None

    async def close(self) -> None:
        self.backend.close()
        assert self.backend._handle is None and self.backend.process is None
        self.closed = True

    def cleanup_evidence(self) -> dict[str, bool]:
        assert self.closed
        return {"native_handle_closed": True}


async def _eventually(description: str, check, timeout: float = 3.0) -> Any:
    """Shared state predicates may be synchronous; native IPC reads are async."""
    last = None
    try:
        async with asyncio.timeout(timeout):
            while True:
                try:
                    last = check()
                    if inspect.isawaitable(last):
                        last = await last
                    if last:
                        return last
                except BackendError as exc:
                    last = str(exc)
                await asyncio.sleep(0.025)
    except TimeoutError as exc:
        raise AssertionError(f"Timed out waiting for {description}; last={last!r}") from exc


class Events:
    def __init__(self) -> None:
        self.receipts = {name: deque(maxlen=32) for name in SERVICES}
        self.expected_seq: dict[str, int] = {}
        self.pending_digest: dict[str, str] = {}
        self.fail_next: set[str] = set()
        self.retry_hold: set[str] = set()
        self.accepted = Counter()
        self.retried = 0
        self.errors: list[str] = []

    async def callback(self, request: web.Request) -> web.Response:
        try:
            assert request.remote == "127.0.0.1"
            service, sid = request.match_info["service"], request.headers["SID"]
            seq = int(request.headers["SEQ"])
            assert request.headers["NT"] == "upnp:event" and request.headers["NTS"] == "upnp:propchange"
            assert seq == self.expected_seq.get(sid, 0), "GENA sequence skipped or advanced before acknowledgement"
            body = await request.text()
            digest = hashlib.sha256(body.encode()).hexdigest()
            assert self.pending_digest.get(sid, digest) == digest, "GENA retry changed its unacknowledged payload"
            if service in self.fail_next:
                self.fail_next.remove(service)
                self.pending_digest[sid] = digest
                return web.Response(status=503)
            if service in self.retry_hold and sid in self.pending_digest:
                return web.Response(status=503)
            if self.pending_digest.pop(sid, None) is not None:
                self.retried += 1
            self.receipts[service].append((sid, event_fields(body)))
            self.accepted[service] += 1
            self.expected_seq[sid] = 1 if seq == 0xFFFFFFFF else seq + 1
            return web.Response()
        except (AssertionError, KeyError, ValueError, ET.ParseError) as exc:
            if not self.errors:
                self.errors.append(str(exc))
            return web.Response(status=500)

    def forget(self, sid: str) -> None:
        self.expected_seq.pop(sid, None)
        self.pending_digest.pop(sid, None)


class BrokenStation:
    def __init__(self, wav: bytes) -> None:
        self.wav = wav
        self.mode = "disconnect"
        self.requests = Counter()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        mode = self.mode
        self.requests[mode] += 1
        if mode == "unavailable":
            return web.Response(status=503)
        if mode == "available":
            return web.Response(body=self.wav, content_type="audio/wav")
        self.mode = "unavailable"
        response = web.StreamResponse(headers={"Content-Type": "audio/wav", "Content-Length": str(len(self.wav)),
                                               "Connection": "close"})
        await response.prepare(request)
        await response.write(self.wav[:44 + 2 * 16000 * 2 * 2])
        assert request.transport is not None
        request.transport.close()
        return response


async def exercise_epoch(library: str, directory: Path, seconds: float, epoch: int, mode: str = "libmpv") -> dict[str, Any]:
    baseline_tasks = asyncio.all_tasks()
    tracks = (directory / "tone-440.wav", directory / "tone-660.wav")
    for track, frequency in zip(tracks, (440, 660)):
        _write_tone(track, frequency)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    backend = player = runner = session = api = monitor = None
    subscriptions: dict[str, str] = {}
    events = Events()
    result: dict[str, Any] = {"epoch": epoch, "cycles": 0, "agreements": 0, "renewals": 0,
                              "subscription_rotations": 0, "natural_eofs": 0, "confirmed_eof_events": 0, "inflight_gain_retries": 0}
    sample_count = 0
    sample_max: dict[str, int] = {}
    child_baseline = child_final = None
    child_max: dict[str, int] = {}
    started = asyncio.get_running_loop().time()
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(socket.SOMAXCONN)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        station_url = base + "/_fixture_station.wav"
        if mode == "libmpv":
            player = LibraryPlayer(library, tracks, station_url)
        else:
            assert mode == "unix"
            from native_stability_transport import UnixPlayer
            player = UnixPlayer(library)
            await player.start()
        backend = player.backend
        fixture = BrokenStation(tracks[0].read_bytes())
        cfg = Config(uuid="00000000-0000-4000-8000-000000000000", advertise_ip="127.0.0.1",
                     bind_address="127.0.0.1", http_api_port=port, upnp_port=port,
                     state_dir=str(directory / "state"), auto_spawn_mpv=False, enable_ssdp=False, enable_mdns=False)
        api = HttpApi(cfg, RendererState.from_config(cfg), backend)
        app = api.app()
        app.router.add_route("NOTIFY", "/_fixture_events/{service}", events.callback)
        app.router.add_get("/_fixture_station.wav", fixture.handle)
        runner = web.AppRunner(app, shutdown_timeout=1)
        await runner.setup()
        await web.SockSite(runner, listener).start()
        monitor = api._state_monitor_task
        assert monitor is not None and not monitor.done()
        result["mpv_version"] = await player.property("mpv-version")
        async with ClientSession(timeout=ClientTimeout(total=15), trust_env=False) as session:
            async def command(value: str) -> Any:
                async with session.get(base + "/httpapi.asp", params={"command": value}) as response:
                    body = await response.text()
                    assert response.status == 200, (value.split(":", 2)[:2], response.status)
                    return json.loads(body) if response.content_type == "application/json" else body

            async def soap(action: str, values: str = "", service: str = "AVTransport") -> dict[str, str]:
                namespace = ("urn:schemas-wiimu-com:service:PlayQueue:1" if service == "PlayQueue"
                             else f"urn:schemas-upnp-org:service:{service}:1")
                envelope = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                            f'<u:{action} xmlns:u="{namespace}">{values}</u:{action}></s:Body></s:Envelope>')
                async with session.post(base + "/upnp/control/" + service, data=envelope.encode(),
                                        headers={"SOAPACTION": f'"{namespace}#{action}"', "Content-Type": "text/xml"}) as response:
                    body = await response.text()
                    assert response.status == 200, (service, action, response.status)
                    return fields(body)

            async def delivered(service: str, expected: dict[str, str]) -> None:
                sid = subscriptions[service]
                def matches() -> bool:
                    if not events.receipts[service]:
                        return False
                    item_sid, props = events.receipts[service][-1]
                    return item_sid == sid and all(props.get(key) == value for key, value in expected.items())
                await _eventually("matching latest acknowledged GENA state", matches)
                assert not events.errors, events.errors

            async def subscribe(service: str) -> None:
                async with session.request("SUBSCRIBE", base + "/upnp/event/" + service, headers={
                    "NT": "upnp:event", "CALLBACK": f"<{base}/_fixture_events/{service}>", "TIMEOUT": "Second-60"
                }) as response:
                    assert response.status == 200
                    subscriptions[service] = response.headers["SID"]
                await delivered(service, {})

            async def renew_or_rotate(rotate: bool) -> None:
                for service, sid in list(subscriptions.items()):
                    if rotate:
                        subscriber = api.event_subscribers[sid]
                        await _eventually("GENA delivery settled", lambda: subscriber.task is None)
                        async with session.request("UNSUBSCRIBE", base + "/upnp/event/" + service,
                                                   headers={"SID": sid}) as response:
                            assert response.status == 200
                        assert sid not in api.event_subscribers
                        events.forget(sid)
                        # Failure occurs over actual HTTP and retains SEQ=0/body
                        # until the new callback acknowledges the retry.
                        events.fail_next.add(service)
                        await subscribe(service)
                        assert subscriptions[service] != sid
                        result["subscription_rotations"] += 1
                    else:
                        async with session.request("SUBSCRIBE", base + "/upnp/event/" + service,
                                                   headers={"SID": sid, "TIMEOUT": "Second-60"}) as response:
                            assert response.status == 200 and response.headers["SID"] == sid
                        result["renewals"] += 1
                assert len(api.event_subscribers) == len(events.expected_seq) == 3

            async def agreement(status: str, index: int, url: str, count: int, volume: int, mute: bool) -> None:
                body = await command("getPlayerStatus")
                expected = {"play": "PLAYING", "pause": "PAUSED_PLAYBACK", "stop": "STOPPED"}[status]
                assert body["status"] == status and body["transport_error"] == ""
                assert body["plicurr"] == str(index) and body["plicount"] == str(count)
                assert body["vol"] == str(volume) and body["mute"] == str(int(mute))
                assert body["stream_url"] == url
                transport = await soap("GetTransportInfo")
                assert transport["CurrentTransportState"] == expected and transport["CurrentTransportStatus"] == "OK"
                position = await soap("GetPositionInfo")
                assert position["Track"] == str(index) and position["TrackURI"] == url
                queue = await soap("GetQueueIndex", service="PlayQueue")
                assert queue["CurrentIndex"] == str(index) and queue["Count"] == str(count)
                gain = "<InstanceID>0</InstanceID><Channel>Master</Channel>"
                assert (await soap("GetVolume", gain, "RenderingControl"))["CurrentVolume"] == str(volume)
                assert (await soap("GetMute", gain, "RenderingControl"))["CurrentMute"] == str(int(mute))
                assert await player.property("volume") == pytest.approx(volume, abs=0.01)
                assert await player.property("mute") is mute
                assert await player.property("idle-active") is (status == "stop")
                if status != "stop":
                    assert await player.property("path") == url
                    assert await player.property("pause") is (status == "pause")
                    assert await player.property("current-ao") == "null"
                await delivered("AVTransport", {"TransportState": expected, "TrackURI": url,
                                                "last:TransportState": expected, "last:TransportStatus": "OK",
                                                "last:CurrentTrack": str(index), "last:CurrentTrackURI": url})
                await delivered("PlayQueue", {"CurrentIndex": str(index), "Count": str(count), "TrackURI": url,
                                              "last:CurrentIndex": str(index)})
                await delivered("RenderingControl", {"Volume": str(volume), "Mute": str(int(mute)),
                                                     "last:Volume": str(volume), "last:Mute": str(int(mute))})
                result["agreements"] += 1

            async def progress() -> None:
                before = await player.property("playback-time")
                async def advancing():
                    return await player.property("playback-time") > before + 0.12
                await _eventually("native decoded playback progress", advancing)

            async def queue(items: list[QueueItem], name: str) -> None:
                value = serialize_queue_context(PlaybackQueue(name=name, items=items, index=1))
                await soap("CreateQueue", f"<QueueContext>{escape(value)}</QueueContext>", "PlayQueue")
                await soap("PlayQueueWithIndex", f"<QueueName>{name}</QueueName><Index>1</Index>", "PlayQueue")

            # Read actual gain before asking the initial GENA snapshot to agree.
            assert (await command("getPlayerStatus"))["status"] == "stop"
            for service in SERVICES:
                await subscribe(service)
            await queue([QueueItem(station_url, title="Synthetic station", stream_type="station", duration=0)], "Synthetic station")
            await progress()
            await _eventually("unpolled monitor-owned backoff", lambda: api.state.phase == PlaybackPhase.BUFFERING
                             and api._station_recovery is not None and api._station_recovery.attempts == 0
                             and api._station_recovery_task is not None, timeout=5)
            recovery_task = api._station_recovery_task
            if epoch == 0:
                # Preserve real readiness timing: a renderer reload must first
                # hit the genuine 503 before the fixture becomes healthy again.
                await _eventually("failed station reload", lambda: api._station_recovery is not None
                                 and api._station_recovery.attempts == 1
                                 and api.state.last_error == "Station source unavailable; retry pending", timeout=12)
                assert fixture.requests["unavailable"] > 0
                fixture.mode = "available"
                await _eventually("recovered native station", lambda: api.state.phase == PlaybackPhase.PLAYING
                                 and api.state.last_error == "", timeout=5)
                await progress()
                await agreement("play", 1, station_url, 1, 50, False)
                assert player.loads == 3
                result["station_recovery"] = "disconnect_then_503_then_recovery"
            elif epoch == 1:
                await command("setPlayerCmd:stop")
                fixture.mode = "available"
                loads = player.loads
                await asyncio.sleep(sum(api._station_recovery_delays) + api._monitor_interval)
                assert player.loads == loads and await player.property("idle-active") is True
                assert api.state.status == "stop" and api.state.intent == "stop"
                assert recovery_task.done() and api._station_recovery is None
                result["station_recovery"] = "stop_cancelled_stale_retry"
            else:
                # No Stop precedes this owner selection: replacement must own
                # its generation fence independently of stop cancellation.
                await command(f"setPlayerCmd:play:{tracks[1]}")
                fixture.mode = "available"
                loads = player.loads
                await asyncio.sleep(sum(api._station_recovery_delays) + api._monitor_interval)
                assert player.loads == loads and await player.property("path") == str(tracks[1])
                assert recovery_task.done() and api._station_recovery is None
                result["station_recovery"] = "direct_source_change_cancelled_stale_retry"
            await command("setPlayerCmd:stop")
            finite = [QueueItem(str(path), title=f"Tone {i}", duration=30) for i, path in enumerate(tracks, 1)]
            await queue(finite, "Synthetic pair")
            await command("setPlayerCmd:stop")
            volume, mute = 50, False
            live_baseline = None
            while asyncio.get_running_loop().time() - started < seconds or result["cycles"] < 8:
                cycle = result["cycles"]
                await renew_or_rotate(rotate=(cycle % 4 == 3))
                await soap("PlayQueueWithIndex", "<QueueName>Synthetic pair</QueueName><Index>1</Index>", "PlayQueue")
                await agreement("play", 1, str(tracks[0]), 2, volume, mute)
                await progress()
                await command("setPlayerCmd:pause")
                await agreement("pause", 1, str(tracks[0]), 2, volume, mute)
                await asyncio.sleep(0.1)
                before = await player.property("playback-time")
                await asyncio.sleep(0.15)
                assert abs(await player.property("playback-time") - before) < 0.04
                target = 2 + cycle % 8
                await soap("Seek", f"<Unit>REL_TIME</Unit><Target>00:00:{target:02}</Target>")
                assert abs(await player.property("playback-time") - target) < 0.2
                volume, mute = 20 + (cycle * 7) % 60, bool(cycle % 2)
                retry_sid = subscriptions["RenderingControl"]
                retry_inflight = cycle % 4 == 1
                if retry_inflight:
                    assert events.expected_seq[retry_sid] > 0
                    events.fail_next.add("RenderingControl")
                    events.retry_hold.add("RenderingControl")
                await command(f"setPlayerCmd:vol:{volume}")
                if retry_inflight:
                    await _eventually("later-sequence callback refused", lambda: retry_sid in events.pending_digest)
                await soap("SetMute", f"<Channel>Master</Channel><DesiredMute>{int(mute)}</DesiredMute>", "RenderingControl")
                if retry_inflight:
                    # Owner state changes while the earlier volume-only body
                    # remains unacknowledged. Callback validates exact retry
                    # bytes, then agreement must see the newer muted state.
                    assert retry_sid in events.pending_digest
                    assert await player.property("mute") is mute
                    events.retry_hold.remove("RenderingControl")
                    await _eventually("in-flight gain event recovered", lambda: retry_sid not in events.pending_digest)
                    result["inflight_gain_retries"] += 1
                await soap("Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")
                await progress()
                await agreement("play", 1, str(tracks[0]), 2, volume, mute)
                await soap("Next")
                await agreement("play", 2, str(tracks[1]), 2, volume, mute)
                await progress()
                if cycle % 8 == 7:
                    eof_mark = player.eof_events
                    await command("setPlayerCmd:seek:29")
                    async def idle():
                        return await player.property("idle-active") is True
                    await _eventually("native natural EOF", idle)
                    await _eventually("unpolled monitor EOF reconciliation", lambda: api.state.status == "stop")
                    if eof_mark is not None:
                        await _eventually("fresh native end-file reason=eof", lambda: player.eof_events > eof_mark)
                        result["confirmed_eof_events"] += 1
                    result["natural_eofs"] += 1
                else:
                    await command("setPlayerCmd:stop")
                await agreement("stop", 2, str(tracks[1]), 2, volume, mute)
                await _eventually("no outstanding event delivery", lambda: all(item.task is None for item in api.event_subscribers.values()))
                assert not api._seek_tasks and api._station_recovery_task is None
                assert not monitor.done()
                player.assert_alive()
                assert not events.errors and not events.pending_digest and not events.retry_hold
                sample = resources()
                # First four complete cycles warm decoder/filter/HTTP paths,
                # then every cycle is checked against that fixed baseline.
                if cycle == 3:
                    live_baseline = sample
                if live_baseline is not None:
                    within_budget(sample, live_baseline, LIVE_BUDGET, player.expected_children)
                    sample_count += 1
                    sample_max = {key: max(sample_max.get(key, 0), value) for key, value in sample.items()}
                    if player.child_pid is not None:
                        child_final = resources(player.child_pid)
                        if child_baseline is None:
                            child_baseline = child_final
                        within_budget(child_final, child_baseline, {key: LIVE_BUDGET[key] for key in ("fds", "rss_kib", "threads")})
                        child_max = {key: max(child_max.get(key, 0), value) for key, value in child_final.items()}
                result["cycles"] += 1
            if child_baseline is not None:
                result["child_resources"] = {"baseline": child_baseline, "final": child_final, "max": child_max}
            result.update(events=dict(events.accepted), callback_retries=events.retried,
                          station_requests=dict(fixture.requests), live_baseline=live_baseline,
                          live_final=sample, live_max=sample_max, samples=sample_count)
            # Leave acknowledged subscriptions and active native playback alive
            # for cleanup, rather than making teardown a vacuous stopped case.
            await command("setPlayerCmd:resume")
            await progress()
            player.assert_alive()
            result.update(loads=player.loads, source_changes=player.source_changes)
    finally:
        try:
            if runner is not None:
                await asyncio.wait_for(runner.cleanup(), 8)
        finally:
            listener.close()
            if player is not None:
                await player.close()
        await asyncio.sleep(0.05)
        pending = [task for task in asyncio.all_tasks() - baseline_tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        assert not pending, f"Native stability leaked {len(pending)} asyncio tasks"
        assert session is None or session.closed
        assert listener.fileno() == -1
        assert runner is None or not runner.sites
        assert player is None or player.closed
        assert monitor is None or monitor.done()
        assert api is None or (not api.event_subscribers and not api._seek_tasks
                               and api._state_monitor_task is None and api._station_recovery_task is None)
    result["elapsed_seconds"] = round(asyncio.get_running_loop().time() - started, 3)
    result["cleanup"] = {**player.cleanup_evidence(), "listener_closed": True, "client_closed": True,
                         "no_pending_tasks": True, "no_subscriptions": True, "no_child_processes": True}
    gc.collect()
    result["after_cleanup"] = resources()
    assert result["after_cleanup"]["children"] == 0
    return result


async def exercise(library: str, directory: Path, seconds: int, mode: str = "libmpv") -> dict[str, Any]:
    started = asyncio.get_running_loop().time()
    result: dict[str, Any] = {"lane": ("REAL_BACKEND/null bounded stability" if mode == "libmpv" else "PRODUCTION_UNIX_IPC/null bounded stability"), "requested_seconds": seconds,
                              "budgets": {"live_above_warm_baseline": LIVE_BUDGET, "clean_above_first_cleanup": CLEAN_BUDGET},
                              "epochs": []}
    before = resources()
    for epoch in range(3):
        target = directory / f"epoch-{epoch}"
        target.mkdir()
        evidence = await exercise_epoch(library, target, seconds / 3, epoch, mode)
        if result["epochs"]:
            within_budget(evidence["after_cleanup"], result["epochs"][0]["after_cleanup"], CLEAN_BUDGET)
        assert evidence["after_cleanup"]["tasks"] == before["tasks"]
        assert evidence["after_cleanup"]["fds"] <= before["fds"] + 2
        result["epochs"].append(evidence)
        print(json.dumps({"checkpoint": "epoch_complete", "epoch": epoch, "cycles": evidence["cycles"],
                          "elapsed_seconds": evidence["elapsed_seconds"], "after_cleanup": evidence["after_cleanup"]}),
              file=sys.stderr, flush=True)
    result["elapsed_seconds"] = round(asyncio.get_running_loop().time() - started, 3)
    assert result["elapsed_seconds"] >= seconds
    result["totals"] = {key: sum(item[key] for item in result["epochs"])
                        for key in ("cycles", "agreements", "loads", "source_changes", "callback_retries",
                                    "subscription_rotations", "renewals", "natural_eofs", "confirmed_eof_events", "inflight_gain_retries", "samples")}
    return result

def validate_evidence(evidence: dict[str, Any]) -> None:
    """Lane-independent lower bounds prevent absent observations from passing."""
    assert evidence["lane"] in {"REAL_BACKEND/null bounded stability", "PRODUCTION_UNIX_IPC/null bounded stability"}
    epochs, totals = evidence["epochs"], evidence["totals"]
    assert len(epochs) == 3
    for key in ("cycles", "loads", "source_changes", "callback_retries", "subscription_rotations",
                "renewals", "agreements", "inflight_gain_retries", "samples", "natural_eofs", "confirmed_eof_events"):
        assert totals[key] == sum(epoch[key] for epoch in epochs), f"Invalid aggregate {key}"
    for epoch in epochs:
        cycles = epoch["cycles"]
        assert cycles >= 8 and epoch["samples"] >= 5 and epoch["natural_eofs"] >= 1
        assert epoch["source_changes"] >= 2 * cycles, "Native path/source observations were lost"
        assert epoch["loads"] >= 2 * cycles + 3, "Native load observations were lost"
        assert epoch["agreements"] >= 5 * cycles
        assert epoch["inflight_gain_retries"] >= 2 and epoch["subscription_rotations"] >= 6
        assert epoch["callback_retries"] >= epoch["subscription_rotations"] + epoch["inflight_gain_retries"]
        if evidence["lane"].startswith("PRODUCTION_UNIX_IPC"):
            assert epoch["confirmed_eof_events"] == epoch["natural_eofs"], "Native EOF reason was not observed"
        else:
            assert epoch["confirmed_eof_events"] == 0
