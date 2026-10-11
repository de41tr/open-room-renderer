"""Optional REAL_BACKEND/null lane using local, synthetic finite WAV files.

Opt in with ORR_TEST_LIBMPV_PATH=/absolute/path/to/libmpv.so.2, then run:
    PYTHONPATH=. .venv/bin/pytest -q -s tests/test_libmpv_integration.py

HttpApi, loopback HTTP, MpvBackend's control/snapshot methods, and libmpv's
decoder/player are real. Only the command transport is a test-local ctypes
adapter. This does NOT validate production UNIX JSON IPC or REAL_AUDIO/hardware,
audible gain/mute, device compatibility, or latency. No external media is used.
Native calls run in a child Python process with a hard timeout, since libmpv's
synchronous C calls cannot be bounded by asyncio cancellation alone.
"""

from __future__ import annotations

import asyncio
import ctypes as ct
import json
from html import escape
from xml.etree import ElementTree as ET
import locale
import math
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
from typing import Any, Callable
import wave

import pytest
from aiohttp import ClientSession, ClientTimeout, web

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.mpv_backend import BackendError, MpvBackend
from fake_linkplay.state import RendererState


# The structures and signatures follow the stable public libmpv client API:
# https://github.com/mpv-player/mpv/blob/v0.40.0/include/mpv/client.h
class _Node(ct.Structure):
    pass


class _NodeList(ct.Structure):
    _fields_ = [
        ("num", ct.c_int),
        ("values", ct.POINTER(_Node)),
        ("keys", ct.POINTER(ct.c_char_p)),
    ]


class _NodeValue(ct.Union):
    _fields_ = [
        ("string", ct.c_char_p),
        ("flag", ct.c_int),
        ("int64", ct.c_int64),
        ("double", ct.c_double),
        ("list", ct.POINTER(_NodeList)),
        ("byte_array", ct.c_void_p),
    ]


_Node._fields_ = [("u", _NodeValue), ("format", ct.c_int)]
_FORMAT_NODE = 6


def _decode_node(node: _Node) -> Any:
    """Copy library-owned property data before freeing the entire node."""
    if node.format == 0:
        return None
    if node.format == 1:
        return node.u.string.decode("utf-8", errors="replace")
    if node.format == 3:
        return bool(node.u.flag)
    if node.format == 4:
        return node.u.int64
    if node.format == 5:
        return node.u.double
    if node.format in {7, 8}:
        items = node.u.list.contents
        values = [_decode_node(items.values[index]) for index in range(items.num)]
        if node.format == 7:
            return values
        return {items.keys[index].decode("utf-8"): value for index, value in enumerate(values)}
    raise AssertionError(f"Unsupported libmpv node format: {node.format}")


class _LibmpvBackend(MpvBackend):
    """Test-only command adapter; production control/snapshot code is inherited."""

    def __init__(self, library_path: str, media_paths: tuple[Path, Path]) -> None:
        super().__init__("unused-test-only-no-ipc", auto_spawn=False)
        self._allowed_media = {str(path.resolve()) for path in media_paths}
        self._lib = ct.CDLL(library_path)
        self._handle: int | None = None
        # Declare every function's argument/result types, especially pointers:
        # ctypes' default c_int result truncates mpv handles on 64-bit hosts.
        signatures = {
            "mpv_create": (ct.c_void_p, []),
            "mpv_initialize": (ct.c_int, [ct.c_void_p]),
            "mpv_set_option_string": (ct.c_int, [ct.c_void_p, ct.c_char_p, ct.c_char_p]),
            "mpv_set_property_string": (ct.c_int, [ct.c_void_p, ct.c_char_p, ct.c_char_p]),
            "mpv_get_property": (ct.c_int, [ct.c_void_p, ct.c_char_p, ct.c_int, ct.c_void_p]),
            "mpv_free_node_contents": (None, [ct.POINTER(_Node)]),
            "mpv_command": (ct.c_int, [ct.c_void_p, ct.POINTER(ct.c_char_p)]),
            "mpv_error_string": (ct.c_char_p, [ct.c_int]),
            "mpv_terminate_destroy": (None, [ct.c_void_p]),
        }
        for name, (restype, argtypes) in signatures.items():
            function = getattr(self._lib, name)
            function.restype, function.argtypes = restype, argtypes
        self._handle = self._lib.mpv_create()
        if not self._handle:
            raise RuntimeError("mpv_create returned NULL")
        try:
            for name, value in {
                "config": "no", "terminal": "no", "video": "no", "ao": "null",
                "idle": "yes", "ytdl": "no", "load-scripts": "no",
                "input-default-bindings": "no",
            }.items():
                self._check(self._lib.mpv_set_option_string(self._handle, name.encode(), value.encode()))
            self._check(self._lib.mpv_initialize(self._handle))
        except BaseException:
            self.close()
            raise

    def _check(self, result: int) -> None:
        if result < 0:
            message = self._lib.mpv_error_string(result).decode("utf-8")
            raise BackendError(f"libmpv {result}: {message}")

    def read_property(self, name: str) -> Any:
        """Independently read actual libmpv properties, never renderer state."""
        assert self._handle is not None, "libmpv handle already closed"
        node = _Node()
        self._check(self._lib.mpv_get_property(self._handle, name.encode(), _FORMAT_NODE, ct.byref(node)))
        try:
            return _decode_node(node)
        finally:
            self._lib.mpv_free_node_contents(ct.byref(node))

    async def start(self) -> None:
        assert self._handle is not None

    async def stop_process(self) -> None:
        self.close()

    async def command(self, args: list[Any], timeout: float = 5.0) -> dict[str, Any]:
        # Per-command asyncio timeouts cannot interrupt C. The subprocess below
        # bounds the whole run, including initialization and native destruction.
        assert self._handle is not None, "libmpv handle already closed"
        operation, *arguments = args
        try:
            if operation == "get_property":
                return {"error": "success", "data": self.read_property(arguments[0])}
            if operation == "set_property":
                name, value = arguments
                encoded = ("yes" if value else "no") if isinstance(value, bool) else str(value)
                self._check(self._lib.mpv_set_property_string(self._handle, name.encode(), encoded.encode()))
            else:
                assert operation in {"loadfile", "seek", "stop"}, args
                if operation == "loadfile":
                    assert arguments[0] in self._allowed_media, "Only generated local WAVs may be loaded"
                assert all(isinstance(arg, (str, int, float)) for arg in args), args
                command = (ct.c_char_p * (len(args) + 1))(*(str(arg).encode() for arg in args), None)
                self._check(self._lib.mpv_command(self._handle, command))
        except BackendError as exc:
            return {"error": str(exc)}
        return {"error": "success"}

    async def command_no_wait(self, args: list[Any]) -> dict[str, Any]:
        raise AssertionError("This REAL_BACKEND lane requires acknowledged commands")

    def close(self) -> None:
        if self._handle is not None:
            handle, self._handle = self._handle, None
            self._lib.mpv_terminate_destroy(handle)


def _write_tone(path: Path, frequency: int) -> None:
    """Generate 30 seconds of 16 kHz stereo PCM at about -30 dBFS peak."""
    rate = 16000
    second = b"".join(
        struct.pack("<hh", sample, sample)
        for index in range(rate)
        for sample in [round(1000 * math.sin(2 * math.pi * frequency * index / rate))]
    )
    with wave.open(str(path), "wb") as output:
        output.setparams((2, 2, rate, 0, "NONE", "not compressed"))
        for _ in range(30):
            output.writeframesraw(second)


async def _eventually(description: str, check: Callable[[], Any], timeout: float = 3.0) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    last: Any = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            last = check()
            if last:
                return last
        except BackendError as exc:
            last = str(exc)
        await asyncio.sleep(0.025)
    raise AssertionError(f"Timed out waiting for {description}; last result={last!r}")


async def _exercise_real_backend(
    library_path: str, directory: Path, cancel_ready: asyncio.Event | None = None,
) -> dict[str, Any]:
    baseline_tasks = asyncio.all_tasks()
    tracks = (directory / "tone-440.wav", directory / "tone-660.wav")
    for path, frequency in zip(tracks, (440, 660)):
        _write_tone(path, frequency)
    backend = _LibmpvBackend(library_path, tracks)
    runner: web.AppRunner | None = None
    session: ClientSession | None = None
    listener: socket.socket | None = None
    monitor: asyncio.Task | None = None
    evidence: dict[str, Any] = {"lane": "REAL_BACKEND/null", "library": library_path}
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(socket.SOMAXCONN)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        cfg = Config(
            uuid="00000000-0000-4000-8000-000000000000",
            advertise_ip="127.0.0.1", bind_address="127.0.0.1",
            http_api_port=port, upnp_port=port, state_dir=str(directory / "state"),
            auto_spawn_mpv=False, enable_ssdp=False, enable_mdns=False,
        )
        api = HttpApi(cfg, RendererState.from_config(cfg), backend)
        # These are known finite test media, rather than the default live radio.
        api.queue_is_live = False
        notifications = []
        async def receive_event(request):
            props = {node.tag: node.text or "" for prop in ET.fromstring(await request.text()) for node in prop}
            notifications.append((request.headers.get("SID"), props))
            return web.Response()
        app = api.app()
        app.router.add_route("NOTIFY", "/_fixture_events", receive_event)
        runner = web.AppRunner(app, shutdown_timeout=1)
        await runner.setup()
        await web.SockSite(runner, listener).start()
        monitor = api._state_monitor_task
        assert monitor is not None and not monitor.done()
        async with ClientSession(timeout=ClientTimeout(total=12), trust_env=False) as session:
            subscriptions = {}
            for service in ("AVTransport", "RenderingControl", "PlayQueue"):
                async with session.request("SUBSCRIBE", f"http://127.0.0.1:{port}/upnp/event/{service}", headers={"NT": "upnp:event", "CALLBACK": f"<http://127.0.0.1:{port}/_fixture_events>", "TIMEOUT": "Second-60"}) as response:
                    assert response.status == 200
                    subscriptions[service] = response.headers["SID"]
            def event_matches(service, predicate):
                return any(sid == subscriptions[service] and predicate(props) for sid, props in notifications)
            async def command(value: str) -> Any:
                async with session.get(f"http://127.0.0.1:{port}/httpapi.asp", params={"command": value}) as response:
                    body = await response.text()
                    assert response.status == 200, (value, response.status, body)
                    return json.loads(body) if response.content_type == "application/json" else body

            async def status(expected: str) -> dict[str, Any]:
                payload = await command("getPlayerStatus")
                assert isinstance(payload, dict), payload
                assert payload["status"] == expected, payload
                assert payload["transport_error"] == "", payload
                return payload

            await status("stop")
            assert await command(f"setPlayerCmd:play:{tracks[0]}") == "OK"
            await status("play")
            assert backend.read_property("current-ao") == "null"
            assert backend.read_property("path") == str(tracks[0])
            assert backend.read_property("duration") == pytest.approx(30, abs=0.05)
            assert any(item.get("type") == "audio" and item.get("selected") is True
                       for item in backend.read_property("track-list"))
            before = backend.read_property("playback-time")
            await _eventually("advancing decoded playback", lambda: backend.read_property("playback-time") > before + 0.25)
            evidence["play"] = {"before": before, "after": backend.read_property("playback-time")}

            assert await command("setPlayerCmd:pause") == "OK"
            await status("pause")
            assert backend.read_property("pause") is True
            await asyncio.sleep(0.15)  # Let the already-buffered null AO settle.
            before = backend.read_property("playback-time")
            await asyncio.sleep(0.35)
            after = backend.read_property("playback-time")
            assert abs(after - before) < 0.03, (before, after)
            evidence["pause"] = {"before": before, "after": after}

            if cancel_ready is not None:
                # A separate exercise cancels here while native playback,
                # the HTTP listener, and the state monitor are all alive.
                cancel_ready.set()
                await asyncio.Future()

            assert await command("setPlayerCmd:vol:37") == "OK"
            assert await command("setPlayerCmd:mute:1") == "OK"
            assert backend.read_property("volume") == pytest.approx(37, abs=0.01)
            assert backend.read_property("mute") is True
            payload = await status("pause")
            assert payload["vol"] == "37" and payload["mute"] == "1", payload
            await _eventually("GENA volume/mute delivery", lambda: event_matches("RenderingControl", lambda props: props.get("Volume") == "37" and props.get("Mute") == "1"))
            assert await command("setPlayerCmd:mute:0") == "OK"
            assert backend.read_property("mute") is False
            assert (await status("pause"))["mute"] == "0"
            evidence["volume_mute"] = {"volume": 37, "mute_on_off": True, "gena_delivery": True}

            assert await command("setPlayerCmd:seek:8") == "OK"
            await _eventually("paused seek", lambda: abs(backend.read_property("playback-time") - 8) < 0.2)
            assert backend.read_property("pause") is True
            payload = await status("pause")
            assert abs(int(payload["curpos"]) - 8) <= 1, payload
            evidence["paused_seek"] = backend.read_property("playback-time")

            assert await command("setPlayerCmd:resume") == "OK"
            await status("play")
            assert backend.read_property("pause") is False
            before = backend.read_property("playback-time")
            await _eventually("resumed playback", lambda: backend.read_property("playback-time") > before + 0.25)
            evidence["resume"] = {"before": before, "after": backend.read_property("playback-time")}

            assert await command(f"setPlayerCmd:play:{tracks[1]}") == "OK"
            payload = await status("play")
            assert payload["stream_url"] == str(tracks[1]), payload
            assert backend.read_property("path") == str(tracks[1])
            assert backend.read_property("duration") == pytest.approx(30, abs=0.05)
            assert backend.read_property("playback-time") < 5
            evidence["replace"] = {"file": tracks[1].name, "position": backend.read_property("playback-time")}

            assert await command("setPlayerCmd:stop") == "OK"
            assert backend.read_property("idle-active") is True
            payload = await status("stop")
            assert payload["curpos"] == "0", payload
            evidence["stop"] = {"idle": True, "status": payload["status"]}

            # Resume after explicit stop must reload the selected second item.
            assert await command("setPlayerCmd:resume") == "OK"
            await status("play")
            assert backend.read_property("path") == str(tracks[1])
            assert await command("setPlayerCmd:seek:29") == "OK"
            await _eventually("natural EOF", lambda: backend.read_property("idle-active") is True)
            # Observe background reconciliation before making another HTTP poll.
            await _eventually("EOF state monitor", lambda: api.state.status == "stop")
            assert (await status("stop"))["curpos"] == "0"
            evidence["eof"] = {"idle": True, "observed_without_http_poll": True}

            async def soap(action: str, fields: str = "", service: str = "AVTransport") -> str:
                namespace = "urn:schemas-wiimu-com:service:PlayQueue:1" if service == "PlayQueue" else "urn:schemas-upnp-org:service:AVTransport:1"
                envelope = f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><u:{action} xmlns:u="{namespace}">{fields}</u:{action}></s:Body></s:Envelope>'
                async with session.post(f"http://127.0.0.1:{port}/upnp/control/{service}", data=envelope.encode(), headers={"SOAPACTION": f'"{namespace}#{action}"', "Content-Type": "text/xml"}) as response:
                    text = await response.text()
                    assert response.status == 200, (action, response.status, text)
                    ET.fromstring(text)
                    return text

            queue = '<PlayList><QueueName>Synthetic pair</QueueName><ListInfo><SourceName>UPnPServer</SourceName><TrackNumber>2</TrackNumber></ListInfo><Tracks>' + ''.join(
                f'<Track{index}><Title>Tone {index}</Title><URL>{escape(str(path))}</URL><Duration>30</Duration></Track{index}>'
                for index, path in enumerate(tracks, 1)
            ) + '</Tracks></PlayList>'
            await soap("CreateQueue", f"<QueueContext>{escape(queue)}</QueueContext>", "PlayQueue")
            await soap("PlayQueueWithIndex", "<QueueName>Synthetic pair</QueueName><Index>1</Index>", "PlayQueue")
            assert backend.read_property("path") == str(tracks[0])
            assert (await status("play"))["plicurr"] == "1"
            await soap("Next")
            assert backend.read_property("path") == str(tracks[1])
            payload = await status("play")
            assert payload["plicurr"] == "2" and payload["plicount"] == "2"
            assert "<Track>2</Track>" in await soap("GetPositionInfo")
            assert "<CurrentIndex>2</CurrentIndex>" in await soap("GetQueueIndex", service="PlayQueue")
            assert "<CurrentIndex>2</CurrentIndex>" in api._event_body("PlayQueue")
            await _eventually("GENA actual second resource", lambda: event_matches("PlayQueue", lambda props: props.get("CurrentIndex") == "2" and props.get("TrackURI") == str(tracks[1])))
            await _eventually("GENA transport second resource", lambda: event_matches("AVTransport", lambda props: '<CurrentTrack val="2"/>' in props.get("LastChange", "") and str(tracks[1]) in props.get("LastChange", "")))
            await soap("Previous")
            assert backend.read_property("path") == str(tracks[0])
            assert await command("setPlayerCmd:seek:29") == "OK"
            await _eventually("queue EOF selects second actual file", lambda: backend.read_property("path") == str(tracks[1]), timeout=5)
            await _eventually("queue EOF confirms playback", lambda: api.state.status == "play", timeout=5)
            assert (await status("play"))["plicurr"] == "2"
            evidence["queue"] = {"distinct_resources": True, "next_previous": True, "eof_advance": True, "http_soap_event_index": 2, "gena_callback_delivery": True}
            await command("setPlayerCmd:stop")
    finally:
        try:
            if runner is not None:
                await runner.cleanup()
        finally:
            if listener is not None:
                listener.close()
            backend.close()
        await asyncio.sleep(0)
        pending = [task for task in asyncio.all_tasks() - baseline_tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        assert not pending, f"REAL_BACKEND fixture leaked tasks: {pending!r}"
        assert session is None or session.closed
        assert listener is None or listener.fileno() == -1
        assert runner is None or not runner.sites
        assert backend._handle is None
        assert backend.process is None
        assert monitor is None or monitor.done(), "HttpApi monitor survived cleanup"
    evidence["cleanup"] = {"handle_closed": True, "monitor_done": True, "no_pending_tasks": True}
    return evidence


@pytest.mark.skipif(not os.environ.get("ORR_TEST_LIBMPV_PATH"), reason="REAL_BACKEND/null requires explicit ORR_TEST_LIBMPV_PATH")
def test_http_api_real_libmpv_null_output(tmp_path: Path) -> None:
    library_path = Path(os.environ["ORR_TEST_LIBMPV_PATH"]).expanduser().resolve()
    assert library_path.is_file(), f"Configured libmpv does not exist: {library_path}"
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    # subprocess.run kills AND waits for the isolated interpreter on timeout.
    # No mpv executable, script subprocess, or IPC server is started by the lane.
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--probe", str(library_path), str(tmp_path)],
        cwd=root, env=env, text=True, capture_output=True, timeout=40, check=False,
    )
    assert completed.returncode == 0, f"REAL_BACKEND/null failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    evidence = json.loads(completed.stdout)
    assert evidence["lane"] == "REAL_BACKEND/null"
    assert evidence["cleanup"] == {"handle_closed": True, "monitor_done": True, "no_pending_tasks": True}
    assert evidence["cleanup_after_cancellation"] is True
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 4 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")

    async def main() -> None:
        async with asyncio.timeout(30):
            evidence = await _exercise_real_backend(sys.argv[2], Path(sys.argv[3]))
            cancel_ready = asyncio.Event()
            probe = asyncio.create_task(_exercise_real_backend(sys.argv[2], Path(sys.argv[3]), cancel_ready))
            try:
                await asyncio.wait_for(cancel_ready.wait(), timeout=8)
                probe.cancel()
                try:
                    await probe
                except asyncio.CancelledError:
                    # Cancellation propagates only after the same teardown
                    # assertions used on the successful path have all passed.
                    evidence["cleanup_after_cancellation"] = True
                else:
                    raise AssertionError("Probe did not propagate cancellation")
            finally:
                if not probe.done():
                    probe.cancel()
                    await asyncio.gather(probe, return_exceptions=True)
        print(json.dumps(evidence, sort_keys=True))

    asyncio.run(main())
