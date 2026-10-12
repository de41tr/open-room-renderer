"""Optional real mpv acceptance through the production Unix JSON IPC transport.

Run ORR_TEST_UNIX_IPC=1 PYTHONPATH=. .venv/bin/pytest -q -s \
    tests/test_mpv_unix_integration.py on Linux with mpv installed.
Without the exact opt-in, this module skips. Once opted in, missing mpv,
unavailable Unix IPC, or failed native playback is a failure, never a skip or a
fallback transport. No production backend method is replaced or overridden.

Only generated low-amplitude local WAV files and literal loopback HTTP are
used. Every mpv process has ao=null, config/scripts/ytdl disabled, and bounded
startup/teardown. This validates control, readback, queue/EOF, and owned process
lifecycle, not physical audio, audible gain, ARMv7, or Raspberry Pi hardware.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from html import escape
import json
import math
import os
from pathlib import Path
import platform
import shlex
import shutil
import socket
import struct
import sys
import tempfile
from typing import Any, AsyncIterator, Awaitable, Callable, Iterator
import wave

import pytest
from aiohttp import ClientSession, ClientTimeout, web

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.mpv_backend import BackendError, MpvBackend
from fake_linkplay.state import RendererState


pytestmark = pytest.mark.skipif(
    os.environ.get("ORR_TEST_UNIX_IPC") != "1",
    reason="Production Unix IPC requires explicit ORR_TEST_UNIX_IPC=1",
)

_NULL_OPTIONS = (
    "--ao=null", "--no-config", "--load-scripts=no", "--ytdl=no",
    "--terminal=no", "--input-default-bindings=no",
)
_DURATION = 30


@pytest.fixture
def native_workspace() -> Iterator[tuple[Path, str]]:
    assert sys.platform == "linux", "Explicit Unix IPC lane requires Linux/SO_PEERCRED"
    executable = shutil.which("mpv")
    assert executable is not None, "ORR_TEST_UNIX_IPC=1 requires a real mpv executable"
    # Keep AF_UNIX paths well below Linux's 108-byte sockaddr_un limit, even
    # when pytest's default per-test temporary directory has a very long name.
    with tempfile.TemporaryDirectory(prefix="orr-ipc-") as directory:
        yield Path(directory), str(Path(executable).resolve())


def _write_tone(path: Path, frequency: int) -> None:
    """Thirty seconds of synthetic stereo PCM, peak below -36 dBFS."""
    rate = 16000
    second = b"".join(
        struct.pack("<hh", sample, sample)
        for index in range(rate)
        for sample in [round(500 * math.sin(2 * math.pi * frequency * index / rate))]
    )
    with wave.open(str(path), "wb") as output:
        output.setparams((2, 2, rate, 0, "NONE", "not compressed"))
        for _ in range(_DURATION):
            output.writeframesraw(second)


async def _property(backend: MpvBackend, name: str) -> Any:
    # Read native responses through the actual production JSON request/response
    # path. An unavailable property is an error, not a fabricated state value.
    return (await backend.checked_command(["get_property", name]))["data"]


async def _eventually(
    description: str, check: Callable[[], Awaitable[Any]], timeout: float = 6.0,
) -> Any:
    last: Any = None
    try:
        async with asyncio.timeout(timeout):
            while True:
                try:
                    last = await check()
                    if last:
                        return last
                except BackendError as exc:
                    # Properties can disappear briefly at load/EOF boundaries.
                    # A broken or forbidden IPC transport still fails at deadline.
                    last = str(exc)
                await asyncio.sleep(0.04)
    except TimeoutError as exc:
        raise AssertionError(f"Timed out waiting for {description}; last={last!r}") from exc


async def _matches(backend: MpvBackend, name: str, expected: Any) -> bool:
    return await _property(backend, name) == expected


async def _reap(process: asyncio.subprocess.Process) -> None:
    """Terminate and wait for fixture-owned children even after an assertion."""
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), 3)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await asyncio.wait_for(process.wait(), 3)
    assert process.returncode is not None, "Fixture child was not reaped"


async def _stop_owned(backend: MpvBackend) -> None:
    process = backend.process
    try:
        await asyncio.wait_for(backend.stop_process(), 8)
    finally:
        # Preserve production teardown failures as test failures, but never
        # leave the real test child behind if the lifecycle under test regresses.
        if process is not None:
            await _reap(process)


@asynccontextmanager
async def _external_player(
    directory: Path, executable: str,
) -> AsyncIterator[tuple[MpvBackend, asyncio.subprocess.Process]]:
    endpoint = directory / "external.sock"
    backend = MpvBackend(str(endpoint), auto_spawn=False, initial_volume=25)
    with (directory / "mpv-external.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            executable, *_NULL_OPTIONS, "--no-video", "--idle=yes",
            f"--input-ipc-server={endpoint}",
            stdout=asyncio.subprocess.DEVNULL, stderr=log,
        )
        try:
            async def ready() -> bool:
                assert process.returncode is None, (
                    "Native mpv exited during startup: "
                    + (directory / "mpv-external.log").read_text(errors="replace")
                )
                if not endpoint.is_socket():
                    return False
                await backend.start(timeout=1)
                return await _matches(backend, "idle-active", True)

            await _eventually("external mpv Unix IPC readiness", ready)
            assert backend.process is None and backend._owned_process is None
            yield backend, process
        finally:
            try:
                await asyncio.wait_for(backend.stop_process(), 8)
            finally:
                await _reap(process)
                # This entire temporary directory belongs to this fixture.
                # Unlike production cleanup, this removes its external socket.
                endpoint.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_real_unix_ipc_controls_queue_eof_and_external_ownership(native_workspace) -> None:
    directory, executable = native_workspace
    tracks = (directory / "tone-440.wav", directory / "tone-660.wav")
    for path, frequency in zip(tracks, (440, 660)):
        _write_tone(path, frequency)
    evidence: dict[str, Any] = {
        "lane": "PRODUCTION_UNIX_IPC/null", "architecture": platform.machine(),
    }
    async with _external_player(directory, executable) as (backend, process):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        runner: web.AppRunner | None = None
        monitor: asyncio.Task | None = None
        session: ClientSession | None = None
        try:
            async with asyncio.timeout(45):
                listener.bind(("127.0.0.1", 0))
                listener.listen(socket.SOMAXCONN)
                listener.setblocking(False)
                port = listener.getsockname()[1]
                base = f"http://127.0.0.1:{port}"
                cfg = Config(
                    uuid="00000000-0000-4000-8000-000000000000",
                    advertise_ip="127.0.0.1", bind_address="127.0.0.1",
                    http_api_port=port, upnp_port=port,
                    state_dir=str(directory / "state"), default_volume=25,
                    auto_spawn_mpv=False, enable_ssdp=False, enable_mdns=False,
                )
                api = HttpApi(cfg, RendererState.from_config(cfg), backend)
                runner = web.AppRunner(api.app(), shutdown_timeout=1)
                await runner.setup()
                await web.SockSite(runner, listener).start()
                monitor = api._state_monitor_task
                assert monitor is not None and not monitor.done()

                async with ClientSession(timeout=ClientTimeout(total=12), trust_env=False) as session:
                    async def command(value: str) -> Any:
                        async with session.get(base + "/httpapi.asp", params={"command": value}) as response:
                            body = await response.text()
                            assert response.status == 200, (value, response.status, body)
                            return json.loads(body) if response.content_type == "application/json" else body

                    async def status(expected: str) -> dict[str, Any]:
                        body = await command("getPlayerStatus")
                        assert body["status"] == expected and body["transport_error"] == "", body
                        return body

                    async def soap(action: str, fields: str = "", service: str = "AVTransport") -> str:
                        namespace = ("urn:schemas-wiimu-com:service:PlayQueue:1" if service == "PlayQueue"
                                     else "urn:schemas-upnp-org:service:AVTransport:1")
                        envelope = (f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
                                    f'<s:Body><u:{action} xmlns:u="{namespace}">{fields}'
                                    f'</u:{action}></s:Body></s:Envelope>')
                        async with session.post(
                            base + f"/upnp/control/{service}", data=envelope.encode(),
                            headers={"SOAPACTION": f'"{namespace}#{action}"', "Content-Type": "text/xml"},
                        ) as response:
                            body = await response.text()
                            assert response.status == 200, (action, response.status, body)
                            return body

                    # This direct native error must be acknowledged, then a
                    # later real command must work on its own Unix connection.
                    with pytest.raises(BackendError):
                        await backend.checked_command(["orr-deliberately-invalid-command"])
                    assert await _property(backend, "idle-active") is True
                    evidence["mpv_version"] = await _property(backend, "mpv-version")
                    await backend.initialize_gain()
                    await status("stop")
                    assert await command(f"setPlayerCmd:play:{tracks[0]}") == "OK"
                    await status("play")
                    assert await _property(backend, "current-ao") == "null"
                    assert await _property(backend, "path") == str(tracks[0])
                    assert await _property(backend, "duration") == pytest.approx(_DURATION, abs=0.05)
                    assert any(track.get("type") == "audio" and track.get("selected") is True
                               for track in await _property(backend, "track-list"))
                    before = await _property(backend, "playback-time")

                    async def progressing() -> bool:
                        return await _property(backend, "playback-time") > before + 0.25

                    await _eventually("decoded playback progression", progressing)
                    assert await command("setPlayerCmd:pause") == "OK"
                    await status("pause")
                    assert await _property(backend, "pause") is True
                    await asyncio.sleep(0.15)
                    before = await _property(backend, "playback-time")
                    await asyncio.sleep(0.3)
                    assert abs(await _property(backend, "playback-time") - before) < 0.04
                    assert await command("setPlayerCmd:vol:25") == "OK"
                    assert await command("setPlayerCmd:mute:1") == "OK"
                    volume, muted = await asyncio.gather(
                        _property(backend, "volume"), _property(backend, "mute"),
                    )
                    assert volume == pytest.approx(25) and muted is True
                    body = await status("pause")
                    assert body["vol"] == "25" and body["mute"] == "1", body
                    assert await command("setPlayerCmd:mute:0") == "OK"
                    assert await _property(backend, "mute") is False
                    assert await command("setPlayerCmd:seek:8") == "OK"

                    # Completion must already be confirmed when HTTP/SOAP
                    # responds, including backward seeks and paused playback.
                    assert abs(await _property(backend, "playback-time") - 8) < 0.2
                    assert await _property(backend, "pause") is True
                    for surface in ("http", "soap"):
                        for target in (18, 3):
                            if surface == "http":
                                assert await command(f"setPlayerCmd:seek:{target}") == "OK"
                            else:
                                await soap("Seek", f"<Unit>REL_TIME</Unit><Target>00:00:{target:02}</Target>")
                            assert abs(await _property(backend, "playback-time") - target) < 0.2
                            assert await _property(backend, "pause") is True
                            assert abs(api.state.curpos - target) <= 1 and api.state.status == "pause"
                    await soap("Previous")
                    assert await _property(backend, "playback-time") < 0.2
                    assert await _property(backend, "pause") is True
                    assert await command("setPlayerCmd:resume") == "OK"
                    await status("play")
                    before = await _property(backend, "playback-time")
                    await _eventually("resumed native playback progression", progressing)
                    for surface in ("http", "soap"):
                        for target in (18, 3):
                            if surface == "http":
                                assert await command(f"setPlayerCmd:seek:{target}") == "OK"
                            else:
                                await soap("Seek", f"<Unit>REL_TIME</Unit><Target>00:00:{target:02}</Target>")
                            assert abs(await _property(backend, "playback-time") - target) < 0.5
                            assert await _property(backend, "pause") is False
                            assert abs(api.state.curpos - target) <= 1 and api.state.status == "play"
                    evidence["confirmed_seek"] = {"http_soap_forward_backward": True,
                                                  "playing_paused": True, "previous_paused": True,
                                                  "actual_position_before_response": True}
                    assert await command("setPlayerCmd:stop") == "OK"
                    await _eventually("explicit stop", lambda: _matches(backend, "idle-active", True))
                    assert (await status("stop"))["curpos"] == "0"
                    evidence["controls"] = ["play", "pause", "volume", "mute", "seek", "resume", "stop"]

                    queue = ('<PlayList><QueueName>Synthetic pair</QueueName>'
                             '<ListInfo><SourceName>UPnPServer</SourceName><TrackNumber>2</TrackNumber></ListInfo>'
                             '<Tracks>' + ''.join(
                                 f'<Track{index}><Title>Tone {index}</Title><URL>{escape(str(path))}</URL>'
                                 f'<Duration>{_DURATION}</Duration></Track{index}>'
                                 for index, path in enumerate(tracks, 1)
                             ) + '</Tracks></PlayList>')
                    await soap("CreateQueue", f"<QueueContext>{escape(queue)}</QueueContext>", "PlayQueue")
                    await soap("PlayQueueWithIndex", "<QueueName>Synthetic pair</QueueName><Index>1</Index>", "PlayQueue")
                    assert await _property(backend, "path") == str(tracks[0])
                    await soap("Next")
                    assert await _property(backend, "path") == str(tracks[1])
                    body = await status("play")
                    assert body["plicurr"] == "2" and body["plicount"] == "2"
                    assert "<Track>2</Track>" in await soap("GetPositionInfo")
                    assert "<CurrentIndex>2</CurrentIndex>" in await soap("GetQueueIndex", service="PlayQueue")
                    await soap("Previous")
                    assert await _property(backend, "path") == str(tracks[0])
                    assert await command(f"setPlayerCmd:seek:{_DURATION - 1}") == "OK"

                    async def advanced() -> bool:
                        # No status HTTP request drives EOF reconciliation.
                        return (api.state.plicurr == 2 and api.state.status == "play"
                                and await _property(backend, "path") == str(tracks[1]))

                    await _eventually("background queue EOF advance", advanced, timeout=8)
                    assert await _property(backend, "current-ao") == "null"
                    assert await command(f"setPlayerCmd:seek:{_DURATION - 1}") == "OK"

                    async def ended() -> bool:
                        return (api.state.plicurr == 2 and api.state.status == "stop"
                                and await _property(backend, "idle-active") is True)

                    await _eventually("final item natural EOF", ended, timeout=8)
                    assert (await status("stop"))["curpos"] == "0"
                    evidence["queue"] = {"distinct_resources": True, "next_previous": True,
                                         "background_eof_advance": True, "final_eof_stop": True}
        finally:
            try:
                if runner is not None:
                    await asyncio.wait_for(runner.cleanup(), 8)
            finally:
                listener.close()
        assert session is None or session.closed
        assert monitor is not None and monitor.done()
        assert runner is not None and not runner.sites
        identity = os.lstat(backend.socket_path)
        await backend.stop_process()
        assert process.returncode is None, "Renderer shutdown terminated an externally owned mpv"
        remaining = os.lstat(backend.socket_path)
        assert (identity.st_dev, identity.st_ino) == (remaining.st_dev, remaining.st_ino)
        inspector = MpvBackend(backend.socket_path, auto_spawn=False)
        try:
            assert await _property(inspector, "idle-active") is True
        finally:
            await inspector.stop_process()
        evidence["external_ownership"] = {"process_preserved": True, "socket_preserved": True}
    assert process.returncode is not None
    evidence["cleanup"] = {"fixture_child_reaped": True, "monitor_done": True, "listener_closed": True}
    print(json.dumps(evidence, sort_keys=True))


@pytest.mark.asyncio
async def test_real_unix_ipc_owned_start_shutdown_cleanup_and_restart(native_workspace, monkeypatch) -> None:
    directory, executable = native_workspace
    bin_directory = directory / "bin"
    bin_directory.mkdir()
    wrapper = bin_directory / "mpv"
    # exec preserves the production child's PID for Linux SO_PEERCRED. The
    # wrapper changes only test safety options, never startup or IPC methods.
    wrapper.write_text(
        "#!/bin/sh\nexec " + shlex.quote(executable) + " "
        + " ".join(shlex.quote(option) for option in _NULL_OPTIONS) + ' "$@"\n'
    )
    wrapper.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_directory) + os.pathsep + os.environ.get("PATH", ""))
    endpoint = directory / "owned.sock"
    track = directory / "owned-440.wav"
    _write_tone(track, 440)
    children: list[asyncio.subprocess.Process] = []
    for attempt in range(2):
        # A shut-down backend is terminal. Restart means a fresh renderer
        # backend using the same now-clean socket path, not reviving the old one.
        backend = MpvBackend(str(endpoint), initial_volume=25, initial_mute=True)
        try:
            async with asyncio.timeout(15):
                await asyncio.gather(*(backend.start() for _ in range(4)))
                process = backend.process
                assert process is not None and process.returncode is None
                children.append(process)
                assert backend._owned_process is process
                identity = os.lstat(endpoint)
                assert backend._owned_socket_identity == (identity.st_dev, identity.st_ino)
                assert (await backend.loadfile(str(track)))["error"] == "success"
                await _eventually("owned null output", lambda: _matches(backend, "current-ao", "null"))
                assert await _property(backend, "path") == str(track)
                assert await _property(backend, "volume") == pytest.approx(25)
                assert await _property(backend, "mute") is True
                await backend.pause()
                snapshot = await backend.snapshot()
                assert snapshot["path"] == str(track) and snapshot["pause"] is True
                assert snapshot["idle-active"] is False and not snapshot.get("backend-error")
                await backend.stop()
                await _eventually("owned stop", lambda: _matches(backend, "idle-active", True))
        finally:
            await _stop_owned(backend)
        assert process.returncode is not None
        assert backend.process is None and backend._owned_process is None
        assert not endpoint.exists(), "Production shutdown left its peer-verified socket behind"
        await backend.stop_process()  # Repeated shutdown remains harmless.
        with pytest.raises(BackendError, match="shut down"):
            await backend.start()
        assert not endpoint.exists(), "A closed backend spawned another child"
        assert all(task is None or task.done() for task in (
            backend._startup_task, backend._shutdown_task, backend._reap_task,
        )), f"Owned backend left a lifecycle task pending after attempt {attempt + 1}"
    assert len(children) == 2 and all(child.returncode is not None for child in children)
    print(json.dumps({
        "lane": "PRODUCTION_UNIX_IPC/null", "architecture": platform.machine(),
        "owned_spawn_cycles": 2, "peer_pid_verified": True, "same_socket_restart": True,
        "cleanup": {"all_children_reaped": True, "owned_socket_removed": True,
                    "lifecycle_tasks_done": True, "closed_backend_did_not_respawn": True},
    }, sort_keys=True))


@pytest.mark.asyncio
async def test_real_unix_ipc_named_load_options_and_per_file_reset(native_workspace) -> None:
    """Exercise the same named options on CI's older mpv through real JSON IPC."""
    directory, executable = native_workspace
    tracks = (directory / "options-440.wav", directory / "plain-660.wav")
    for path, frequency in zip(tracks, (440, 660)):
        _write_tone(path, frequency)
    keys = ("start", "demuxer", "force-seekable", "cache")
    options = {"start": "8", "demuxer": "lavf", "force-seekable": "yes", "cache": "no"}
    async with _external_player(directory, executable) as (backend, process):
        async with asyncio.timeout(15):
            version = await _property(backend, "mpv-version")
            defaults = {key: await _property(backend, "options/" + key) for key in keys}
            assert (await backend.loadfile(str(tracks[0]), options))["error"] == "success"

            async def started_at_offset() -> bool:
                return (await _property(backend, "path") == str(tracks[0])
                        and 8 <= await _property(backend, "playback-time") < 10
                        and await _property(backend, "current-ao") == "null")

            await _eventually("named-load start position", started_at_offset)
            await backend.pause()
            assert await _property(backend, "pause") is True
            position = await _property(backend, "playback-time")
            actual = {key: await _property(backend, "options/" + key) for key in keys}
            assert actual == {"start": "8", "demuxer": "lavf", "force-seekable": True, "cache": False}, actual
            assert await _property(backend, "volume") == pytest.approx(25)
            assert await _property(backend, "mute") is False
            assert (await backend.loadfile(str(tracks[1])))["error"] == "success"

            async def restarted_without_offset() -> bool:
                return (await _property(backend, "path") == str(tracks[1])
                        and 0 <= await _property(backend, "playback-time") < 2
                        and await _property(backend, "pause") is False)

            await _eventually("plain replacement resets start", restarted_without_offset)
            reset = {key: await _property(backend, "options/" + key) for key in keys}
            assert reset == defaults, (reset, defaults)
    assert process.returncode is not None
    print(json.dumps({
        "lane": "PRODUCTION_UNIX_IPC/null/named-load-options", "mpv_version": version,
        "architecture": platform.machine(), "applied_options": actual,
        "start_position": position, "per_file_options_reset": True,
        "gain_preserved": True, "fixture_child_reaped": True,
    }, sort_keys=True))
