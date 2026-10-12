"""INTEGRATION + MOCK backend: real pywiim client and loopback HTTP server.

The HTTP transport, renderer handlers, and installed pywiim parser are real.
Only audio playback is fake: no mpv, media downloads, discovery multicast,
proprietary hardware/apps, or cloud services are exercised by these tests.
Validated against pywiim 2.3.9; an absent optional client skips this module.
"""

from __future__ import annotations

import asyncio
import errno
import socket
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from html import escape
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import ClientSession, ClientTimeout, web
from yarl import URL

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.state import RendererState

pywiim = pytest.importorskip("pywiim", reason="local client integration requires the optional pywiim package")

MEDIA_URL = "https://media.example.test/play-pause-stop.wav?item=1&format=wav"
MEDIA_TITLE = "Synthetic play pause stop"


@dataclass
class FakeAudioBackend:
    """Deterministic MOCK backend; storing a URL never fetches it."""

    url: str = ""
    paused: bool = False
    idle: bool = True
    position: int = 0
    volume_value: int = 50
    mute_value: bool = False
    fail_operation: str | None = None
    snapshot_error: bool = False
    calls: list[tuple[str, object]] = field(default_factory=list)

    def _record(self, operation: str, argument: object = None) -> None:
        self.calls.append((operation, argument))
        if self.fail_operation == operation:
            raise BackendError(f"Synthetic {operation} failure")

    async def loadfile(self, url: str, options: dict[str, str] | None = None) -> None:
        self._record("loadfile", url)
        self.url, self.idle, self.paused, self.position = url, False, False, 0

    async def pause(self) -> None:
        self._record("pause")
        self.paused = True

    async def resume(self) -> None:
        self._record("resume")
        self.paused = False

    async def stop(self) -> None:
        self._record("stop")
        self.url, self.idle, self.paused, self.position = "", True, False, 0

    async def volume(self, value: int) -> None:
        self._record("volume", value)
        self.volume_value = value

    async def mute(self, value: bool) -> None:
        self._record("mute", value)
        self.mute_value = value

    async def seek(self, seconds: int) -> None:
        self._record("seek", seconds)
        self.position = seconds

    async def snapshot(self) -> dict[str, object]:
        if self.snapshot_error:
            return {"backend-error": True}
        return {
            "path": self.url,
            "idle-active": self.idle,
            "core-idle": self.idle,
            "pause": self.paused,
            "paused-for-cache": False,
            "playback-time": self.position,
            "duration": 120 if self.url else None,
            "media-title": MEDIA_TITLE if self.url else "",
            "track-list": [] if self.idle else [{"type": "audio", "selected": True}],
        }


@dataclass
class LocalRenderer:
    client: Any
    backend: FakeAudioBackend
    commands: list[str]
    session: ClientSession
    base_url: str


def loopback_listener() -> socket.socket:
    """Skip only explicit OS denial/unavailability of the loopback socket."""
    listener = None
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(socket.SOMAXCONN)
        listener.setblocking(False)
        return listener
    except OSError as exc:
        if listener is not None:
            listener.close()
        if exc.errno in {errno.EACCES, errno.EPERM, errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL}:
            pytest.skip(f"loopback TCP socket unavailable: errno={exc.errno} ({exc.strerror})")
        raise


@pytest_asyncio.fixture
async def local_renderer(tmp_path, monkeypatch) -> AsyncIterator[LocalRenderer]:
    """Own and close every listener/session/task in the same event loop."""
    baseline_tasks = asyncio.all_tasks()
    resources = AsyncExitStack()
    listener = resources.enter_context(loopback_listener())
    port = listener.getsockname()[1]
    attempted_external_urls: list[str] = []
    original_request = ClientSession._request

    async def loopback_only_request(session, method, url, *args, **kwargs):
        target = URL(url)
        if target.scheme != "http" or target.host != "127.0.0.1" or target.port != port:
            attempted_external_urls.append(str(target))
            raise AssertionError(f"integration test attempted non-fixture HTTP request: {target}")
        return await original_request(session, method, url, *args, **kwargs)

    # Keep the actual aiohttp request path intact, but fail closed if any code
    # tries fetching the synthetic media or another external service.
    monkeypatch.setattr(ClientSession, "_request", loopback_only_request)
    runner = None
    session = None
    try:
        async with resources:
            cfg = Config(
                uuid="00000000-0000-4000-8000-000000000000",
                friendly_name="Synthetic Local Renderer",
                advertise_ip="127.0.0.1",
                bind_address="127.0.0.1",
                http_api_port=port,
                upnp_port=port,
                state_dir=str(tmp_path),
                auto_spawn_mpv=False,
                enable_ssdp=False,
                enable_mdns=False,
            )
            backend = FakeAudioBackend(volume_value=cfg.default_volume)
            api = HttpApi(cfg, RendererState.from_config(cfg), backend)
            commands: list[str] = []

            @web.middleware
            async def record_commands(request, handler):
                commands.append(request.query.get("command", ""))
                return await handler(request)

            app = api.app()
            app.middlewares.append(record_commands)
            runner = web.AppRunner(app, shutdown_timeout=1)
            resources.push_async_callback(runner.cleanup)
            session = await resources.enter_async_context(
                ClientSession(timeout=ClientTimeout(total=5), trust_env=False)
            )
            client = pywiim.WiiMClient(
                host="127.0.0.1",
                port=port,
                protocol="http",
                timeout=5,
                session=session,
                # Supply only transport policy, not a status-endpoint override:
                # the real client's standard getPlayerStatusEx must interoperate.
                capabilities={"retry_count": 1, "supports_metadata": False},
            )
            resources.push_async_callback(client.close)
            await runner.setup()
            await web.SockSite(runner, listener).start()
            # The first pywiim request probes/caches the endpoint. Make it
            # read-only so probing cannot duplicate the first mutation.
            await client.get_status()
            yield LocalRenderer(client, backend, commands, session, f"http://127.0.0.1:{port}")
    finally:
        # aiohttp schedules connection-close callbacks for the next loop tick.
        await asyncio.sleep(0)
        remaining = asyncio.all_tasks() - baseline_tasks - {asyncio.current_task()}
        pending = [task for task in remaining if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        assert session is None or session.closed
        assert listener.fileno() == -1
        assert runner is None or not runner.sites
        assert not pending, f"integration fixture leaked background tasks: {pending!r}"
        assert not attempted_external_urls, f"unexpected external requests: {attempted_external_urls!r}"


async def assert_transport(client, expected: str) -> dict[str, Any]:
    """Check real parsed fields, never a substring in metadata or JSON text."""
    player = await client.get_player_status()
    assert player.get("play_status") == expected, player
    status = await client.get_status()
    assert status.get("play_status") == expected, status
    raw = await client.send_command("getPlayerStatus")
    assert raw.get("status") == expected, raw
    return player


@pytest.mark.asyncio
async def test_local_client_exact_transport_round_trip(local_renderer) -> None:
    client, backend = local_renderer.client, local_renderer.backend
    initial = await assert_transport(client, "stop")
    assert initial["volume"] == 50
    assert initial["volume_level"] == 0.5
    assert initial["mute"] is False

    await client.play_url(MEDIA_URL)
    playing = await assert_transport(client, "play")
    assert playing["title"] == MEDIA_TITLE
    assert playing["uri"] == MEDIA_URL
    assert backend.url == MEDIA_URL

    await client.pause()
    paused = await assert_transport(client, "pause")
    assert paused["title"] == MEDIA_TITLE  # All transport words are metadata.
    assert backend.paused

    await client.resume()
    await assert_transport(client, "play")
    assert not backend.paused
    assert [call for call in backend.calls if call[0] == "loadfile"] == [("loadfile", MEDIA_URL)]

    await client.stop()
    stopped = await assert_transport(client, "stop")
    assert stopped["position"] == 0
    assert backend.idle
    assert backend.calls == [("loadfile", MEDIA_URL), ("pause", None), ("resume", None), ("stop", None)]
    assert "getPlayerStatusEx" in local_renderer.commands


@pytest.mark.asyncio
async def test_local_client_volume_and_mute_round_trip(local_renderer) -> None:
    client, backend = local_renderer.client, local_renderer.backend
    for volume, percent in [(0.0, 0), (0.37, 37), (1.0, 100)]:
        await client.set_volume(volume)
        status = await client.get_player_status()
        assert status.get("volume") == percent, status
        assert status.get("volume_level") == volume, status
        assert backend.volume_value == percent

    for muted in (True, False):
        await client.set_mute(muted)
        status = await client.get_player_status()
        assert status.get("mute") is muted, status
        assert backend.mute_value is muted
        assert status["volume"] == 100

    assert backend.calls == [
        ("volume", 0), ("volume", 37), ("volume", 100), ("mute", True), ("mute", False)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation, method, argument, expected_state",
    [
        ("loadfile", "play_url", MEDIA_URL, "stop"),
        ("pause", "pause", None, "stop"),
        ("resume", "resume", None, "stop"),
        ("stop", "stop", None, "stop"),
        ("volume", "set_volume", 0.25, "stop"),
        ("mute", "set_mute", True, "stop"),
    ],
)
async def test_local_client_backend_failure_is_not_success(
    local_renderer, operation, method, argument, expected_state
) -> None:
    client, backend = local_renderer.client, local_renderer.backend
    if operation in {"pause", "resume", "stop"}:
        await client.play_url(MEDIA_URL)
    if operation == "resume":
        await client.pause()
    backend.fail_operation = operation

    with pytest.raises(pywiim.WiiMError):
        action = getattr(client, method)
        if argument is None:
            await action()
        else:
            await action(argument)

    if operation == "loadfile":
        # An unsuccessful acknowledgement may follow an applied load. Cleanup
        # checks Stop, and must not retry or silently start a different source.
        assert backend.calls == [("loadfile", MEDIA_URL), ("stop", None)]
    else:
        assert backend.calls[-1][0] == operation
    status = await assert_transport(client, expected_state)
    assert status["volume"] == 50
    assert status["mute"] is False
    assert backend.volume_value == 50
    assert backend.mute_value is False
    if operation in {"pause", "resume", "stop"}:
        assert status["playback_phase"] == "ERROR"
        assert "confirmation failed" in status["transport_error"]
        assert status["stream_url"] == MEDIA_URL
        if operation == "stop":
            assert "silence not confirmed" in status["transport_error"]
        backend.fail_operation = ""
        await client.resume()
        recovered = await assert_transport(client, "play")
        assert recovered["transport_error"] == ""
        assert backend.url == MEDIA_URL
    if operation == "loadfile":
        assert status["playback_phase"] == "ERROR"
        assert status["transport_error"] == "Backend control failed"
        assert status["stream_url"] == MEDIA_URL
        assert backend.idle and backend.url == ""


@pytest.mark.asyncio
async def test_local_client_observes_backend_error_and_recovers(local_renderer) -> None:
    client, backend = local_renderer.client, local_renderer.backend
    await client.play_url(MEDIA_URL)
    await assert_transport(client, "play")

    backend.snapshot_error = True
    failed = await assert_transport(client, "stop")
    assert failed["playback_phase"] == "ERROR"
    assert failed["transport_error"]

    backend.snapshot_error = False
    await client.play_url(MEDIA_URL)
    recovered = await assert_transport(client, "play")
    assert recovered["playback_phase"] == "PLAYING"
    assert recovered["transport_error"] == ""


async def create_client_pair(renderer: LocalRenderer) -> tuple[str, str]:
    """The real client has no queue creation API; SOAP only sets up the fixture."""
    urls = ("https://media.example.test/first.wav", "https://media.example.test/second.wav")
    queue = ('<PlayList><QueueName>Client pair</QueueName>'
             '<ListInfo><SourceName>UPnPServer</SourceName><TrackNumber>2</TrackNumber></ListInfo><Tracks>'
             + ''.join(f'<Track{i}><Title>Track {i}</Title><URL>{url}</URL><Duration>120</Duration></Track{i}>'
                       for i, url in enumerate(urls, 1)) + '</Tracks></PlayList>')
    namespace = "urn:schemas-wiimu-com:service:PlayQueue:1"
    body = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            f'<u:CreateQueue xmlns:u="{namespace}"><QueueContext>{escape(queue)}</QueueContext>'
            '</u:CreateQueue></s:Body></s:Envelope>')
    async with renderer.session.post(renderer.base_url + "/upnp/control/PlayQueue", data=body.encode(),
            headers={"SOAPACTION": f'"{namespace}#CreateQueue"', "Content-Type": "text/xml"}) as response:
        assert response.status == 200, await response.text()
    return urls


@pytest.mark.asyncio
async def test_local_client_next_previous_select_distinct_queue_resources(local_renderer) -> None:
    client, backend = local_renderer.client, local_renderer.backend
    first, second = await create_client_pair(local_renderer)
    await client.resume()
    assert (await assert_transport(client, "play"))["uri"] == backend.url == first
    await client.next_track()
    assert (await assert_transport(client, "play"))["uri"] == backend.url == second
    raw = await client.send_command("getPlayerStatus")
    assert raw["plicurr"] == raw["plicount"] == "2"
    await client.previous_track()
    assert (await assert_transport(client, "play"))["uri"] == backend.url == first
    raw = await client.send_command("getPlayerStatus")
    assert raw["plicurr"] == "1" and raw["plicount"] == "2"
    assert [call[1] for call in backend.calls if call[0] == "loadfile"] == [first, second, first]
    assert [command for command in local_renderer.commands if command.startswith("setPlayerCmd:")] == [
        "setPlayerCmd:resume", "setPlayerCmd:next", "setPlayerCmd:prev"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["next_track", "previous_track"])
async def test_local_client_empty_navigation_fails_without_mutation(local_renderer, method) -> None:
    client, backend = local_renderer.client, local_renderer.backend
    before = await client.send_command("getPlayerStatus")
    with pytest.raises(pywiim.WiiMError):
        await getattr(client, method)()
    assert await client.send_command("getPlayerStatus") == before
    assert backend.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method,index", [("previous_track", 1), ("next_track", 2)])
async def test_local_client_queue_boundary_fails_without_mutation(local_renderer, method, index) -> None:
    client, backend = local_renderer.client, local_renderer.backend
    first, second = await create_client_pair(local_renderer)
    await client.resume()
    if index == 2:
        await client.next_track()
    before, calls = await client.send_command("getPlayerStatus"), list(backend.calls)
    with pytest.raises(pywiim.WiiMError):
        await getattr(client, method)()
    assert await client.send_command("getPlayerStatus") == before
    assert backend.calls == calls and backend.url == (first, second)[index - 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["setPlayerCmd:next:1", "setPlayerCmd:prev:1",
    "setPlayerCmd:next:", "setPlayerCmd:prev:", "setPlayerCmd:previous", "setPlayerCmd:Next"])
async def test_local_client_unsupported_navigation_suffix_does_not_mutate(local_renderer, command) -> None:
    client, backend = local_renderer.client, local_renderer.backend
    await create_client_pair(local_renderer)
    await client.resume()
    before, calls = await client.send_command("getPlayerStatus"), list(backend.calls)
    with pytest.raises(pywiim.WiiMError):
        await client.send_command(command)
    assert await client.send_command("getPlayerStatus") == before
    assert backend.calls == calls


@pytest.mark.asyncio
async def test_local_client_failed_next_load_is_not_success(local_renderer) -> None:
    client, backend = local_renderer.client, local_renderer.backend
    _, second = await create_client_pair(local_renderer)
    await client.resume()
    backend.fail_operation = "loadfile"
    with pytest.raises(pywiim.WiiMError):
        await client.next_track()
    status = await assert_transport(client, "stop")
    assert status["playback_phase"] == "ERROR" and status["transport_error"]
    assert status["stream_url"] == second
    assert backend.idle and backend.url == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("paused", [False, True])
async def test_local_client_previous_restarts_single_track_preserving_pause(local_renderer, paused) -> None:
    client, backend = local_renderer.client, local_renderer.backend
    await client.play_url(MEDIA_URL)
    if paused:
        await client.pause()
    backend.position = 12
    await client.previous_track()
    status = await assert_transport(client, "pause" if paused else "play")
    assert status["uri"] == backend.url == MEDIA_URL
    assert status["position"] == backend.position == 0
    assert backend.paused is paused
    assert [call for call in backend.calls if call[0] == "loadfile"] == [("loadfile", MEDIA_URL)]
    assert ("seek", 0) in backend.calls
