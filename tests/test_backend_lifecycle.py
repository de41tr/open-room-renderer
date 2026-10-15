"""UNIT/MOCK lifecycle contracts; no AF_UNIX socket or actual player is used."""

from __future__ import annotations

import asyncio
import errno
import json
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from fake_linkplay import cli
from fake_linkplay.config import Config
from fake_linkplay.mpv_backend import BackendError, MpvBackend
from fake_linkplay.state import PlaybackPhase


class FakeProcess:
    def __init__(self, *, ignore_terminate=False, returncode=None):
        self.returncode = returncode
        self.ignore_terminate = ignore_terminate
        self.terminated = 0
        self.killed = 0
        self.waited = 0
        self.exited = asyncio.Event()
        if returncode is not None:
            self.exited.set()

    def terminate(self):
        self.terminated += 1
        if not self.ignore_terminate:
            self.returncode = -15
            self.exited.set()

    def kill(self):
        self.killed += 1
        self.returncode = -9
        self.exited.set()

    async def wait(self):
        self.waited += 1
        await self.exited.wait()
        return self.returncode


class FakeWriter:
    def __init__(self):
        self.transport = Mock()
        self.payload = None
        self.closed = False
        self.drain = AsyncMock()
        self.wait_closed = AsyncMock()

    def write(self, data):
        self.payload = json.loads(data)

    def close(self):
        self.closed = True


class FakeIpc:
    def __init__(self):
        self.writers = []
        self.responses = []
        self.connect = AsyncMock(side_effect=self.open)

    async def open(self, _path):
        writer = FakeWriter()
        self.writers.append(writer)

        async def readline():
            if self.responses:
                return self.responses.pop(0)
            return json.dumps({"error": "success", "request_id": writer.payload["request_id"]}).encode() + b"\n"

        return SimpleNamespace(readline=readline), writer


@pytest.fixture
def ipc(monkeypatch):
    fake = FakeIpc()
    monkeypatch.setattr(asyncio, "open_unix_connection", fake.connect)
    return fake


def short_backend(*, auto_spawn=True):
    backend = MpvBackend("/synthetic-mpv.sock", auto_spawn=auto_spawn)
    backend._startup_timeout = 0.08
    backend._shutdown_timeout = 0.015
    return backend


@pytest.mark.asyncio
async def test_concurrent_start_and_commands_spawn_exactly_one_owned_child(monkeypatch, ipc):
    backend = short_backend()
    process = FakeProcess()
    exists = False

    async def spawn(*args, **kwargs):
        nonlocal exists
        await asyncio.sleep(0)
        exists = True
        return process

    backend._socket_exists = lambda: exists
    spawn_mock = AsyncMock(side_effect=spawn)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_mock)
    results = await asyncio.gather(*[backend.start() for _ in range(8)], *[backend.command(["stop"]) for _ in range(8)])
    assert all(result == {"error": "success", "request_id": index + 1} for index, result in enumerate(results[8:]))
    spawn_mock.assert_awaited_once()
    assert spawn_mock.call_args.args[0] == "mpv"
    assert "--input-ipc-server=/synthetic-mpv.sock" in spawn_mock.call_args.args
    assert backend.process is process
    await asyncio.gather(backend.stop_process(), backend.stop_process())
    assert process.terminated == 1
    assert process.waited == 1
    assert backend.process is None
    assert all(writer.closed for writer in ipc.writers)


@pytest.mark.asyncio
async def test_owned_startup_requires_connectable_socket_not_only_path(monkeypatch, ipc):
    backend = short_backend()
    process = FakeProcess()
    backend._socket_exists = lambda: backend.process is not None
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    attempts = 0

    async def connect(path):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionRefusedError(errno.ECONNREFUSED, "not ready")
        return await ipc.open(path)

    ipc.connect.side_effect = connect
    await backend.start()
    assert attempts == 2
    await backend.stop_process()


@pytest.mark.asyncio
async def test_missing_executable_is_visible_and_does_not_spawn_on_every_request(monkeypatch):
    backend = short_backend()
    backend._socket_exists = lambda: False
    spawn = AsyncMock(side_effect=FileNotFoundError("mpv"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(BackendError, match="executable not found"):
        await backend.start()
    for _ in range(3):
        assert "executable not found" in (await backend.command(["stop"]))["error"]
    spawn.assert_awaited_once()
    await backend.stop_process()


@pytest.mark.asyncio
async def test_unavailable_owned_ipc_is_reaped_and_never_respawned(monkeypatch):
    backend = short_backend()
    backend._startup_timeout = 0.01
    backend._socket_exists = lambda: False
    process = FakeProcess()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(BackendError, match="startup timed out"):
        await backend.start(timeout=0.3)
    assert process.waited == 1
    assert process.returncode == -15
    assert backend.process is None
    assert "startup timed out" in (await backend.command(["stop"]))["error"]
    spawn.assert_awaited_once()
    await backend.stop_process()


@pytest.mark.asyncio
async def test_early_child_exit_is_reported_and_reaped(monkeypatch):
    backend = short_backend()
    backend._socket_exists = lambda: False
    process = FakeProcess(returncode=2)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(BackendError, match="exited during startup.*2"):
        await backend.start()
    assert process.waited == 1
    assert process.terminated == 0
    await backend.stop_process()


@pytest.mark.asyncio
async def test_shutdown_kills_then_awaits_stubborn_owned_child(monkeypatch, ipc):
    backend = short_backend()
    backend._socket_exists = lambda: backend.process is not None
    process = FakeProcess(ignore_terminate=True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    await backend.start()
    await backend.stop_process()
    assert process.terminated == process.killed == 1
    assert process.waited == 2
    assert process.exited.is_set()
    assert backend.process is None
    assert "shut down" in (await backend.command(["stop"]))["error"]


@pytest.mark.asyncio
async def test_shutdown_during_startup_reaps_child(monkeypatch):
    backend = short_backend()
    backend._socket_exists = lambda: False
    process = FakeProcess()
    spawned = asyncio.Event()

    async def spawn(*args, **kwargs):
        spawned.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    starting = asyncio.create_task(backend.start())
    await spawned.wait()
    await backend.stop_process()
    await asyncio.gather(starting, return_exceptions=True)
    assert process.waited == 1
    assert backend._startup_task.done()
    assert backend._shutdown_task.done()


@pytest.mark.asyncio
async def test_cancelling_one_start_waiter_does_not_cancel_shared_startup(monkeypatch, ipc):
    backend = short_backend()
    process = FakeProcess()
    release = asyncio.Event()
    entered = asyncio.Event()
    backend._socket_exists = lambda: backend.process is not None

    async def spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        return process

    spawn_mock = AsyncMock(side_effect=spawn)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_mock)
    first = asyncio.create_task(backend.start())
    await entered.wait()
    second = asyncio.create_task(backend.start())
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    await second
    spawn_mock.assert_awaited_once()
    await backend.stop_process()
    assert process.waited == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["regular", "symlink", "dangling-symlink"])
async def test_start_never_replaces_regular_files_or_symlinks(tmp_path, monkeypatch, kind):
    target = tmp_path / "target"
    target.write_text("preserve me")
    path = tmp_path / "ipc"
    if kind == "regular":
        path.write_text("preserve this too")
    else:
        path.symlink_to(target if kind == "symlink" else tmp_path / "absent")
    backend = MpvBackend(str(path))
    spawn = AsyncMock()
    connect = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(asyncio, "open_unix_connection", connect)
    with pytest.raises(BackendError, match="not a socket"):
        await backend.start()
    assert path.is_symlink() if kind != "regular" else path.read_text() == "preserve this too"
    assert target.read_text() == "preserve me"
    spawn.assert_not_awaited()
    connect.assert_not_awaited()
    await backend.stop_process()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ConnectionRefusedError(errno.ECONNREFUSED, "refused"), FileNotFoundError(errno.ENOENT, "disappeared"), PermissionError(errno.EACCES, "denied")])
async def test_unrelated_socket_failure_is_visible_without_unlink_or_spawn(monkeypatch, error):
    backend = short_backend()
    backend._socket_exists = lambda: True
    spawn, unlink = AsyncMock(), Mock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(asyncio, "open_unix_connection", AsyncMock(side_effect=error))
    monkeypatch.setattr("fake_linkplay.mpv_backend.os.unlink", unlink)
    response = await backend.command(["stop"])
    assert response["error"] != "success"
    spawn.assert_not_awaited()
    unlink.assert_not_called()
    await backend.stop_process()


@pytest.mark.asyncio
async def test_external_backend_is_never_quit_or_killed_and_can_recover(ipc, monkeypatch):
    backend = short_backend(auto_spawn=False)
    backend._socket_exists = lambda: False
    assert "unavailable" in (await backend.command(["stop"]))["error"]
    backend._socket_exists = lambda: True
    assert (await backend.command(["get_property", "pause"]))["error"] == "success"
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await backend.stop_process()
    spawn.assert_not_awaited()
    assert [writer.payload["command"] for writer in ipc.writers if writer.payload] == [["get_property", "pause"]]


@pytest.mark.asyncio
async def test_unowned_process_handle_is_never_terminated():
    process = FakeProcess()
    backend = MpvBackend("/unused", process=process)
    await backend.stop_process()
    assert process.terminated == process.killed == process.waited == 0


async def never():
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_start_connection_timeout_is_bounded(monkeypatch):
    backend = short_backend(auto_spawn=False)
    backend._socket_exists = lambda: True
    async def connect(_):
        await never()
    monkeypatch.setattr(asyncio, "open_unix_connection", connect)
    begin = asyncio.get_running_loop().time()
    with pytest.raises(BackendError, match="timed out"):
        await backend.start(timeout=0.01)
    assert asyncio.get_running_loop().time() - begin < 0.3
    await backend.stop_process()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,no_wait", [(stage, no_wait) for stage in ["startup", "connect", "drain", "response", "close"] for no_wait in [False, True] if not (no_wait and stage == "response")])
async def test_command_deadline_covers_every_transport_stage(monkeypatch, stage, no_wait):
    backend = short_backend(auto_spawn=False)
    backend.start = AsyncMock()
    if stage == "startup":
        async def slow_start(**kwargs):
            await never()
        backend.start = slow_start
    writer = FakeWriter()
    reader = SimpleNamespace(readline=AsyncMock(return_value=b'{"request_id":1,"error":"success"}\n'))
    if stage == "drain":
        writer.drain = never
    elif stage == "response":
        reader.readline = never
    elif stage == "close":
        writer.wait_closed = never

    async def connect(_):
        if stage == "connect":
            await never()
        return reader, writer

    monkeypatch.setattr(asyncio, "open_unix_connection", connect)
    begin = asyncio.get_running_loop().time()
    method = backend.command_no_wait if no_wait else backend.command
    response = await method(["stop"], timeout=0.015)
    assert asyncio.get_running_loop().time() - begin < 0.3
    assert response["error"] == "success" if stage == "close" else "timed out" in response["error"]
    if stage not in {"startup", "connect"}:
        assert writer.closed
        writer.transport.abort.assert_called_once()
    await backend.stop_process()


@pytest.mark.asyncio
@pytest.mark.parametrize("wire,error", [
    (b"not json\n", "invalid JSON"), (b"\xff\n", "invalid JSON"),
    (b"[]\n", "non-object"), (b"null\n", "non-object"),
    (b'{"request_id":1}\n', "missing an error status"),
    (b'{"request_id":1,"error":true}\n', "missing an error status"),
    (b"", "closed before acknowledgement"),
])
async def test_malformed_responses_and_eof_are_errors(ipc, wire, error):
    backend = short_backend(auto_spawn=False)
    backend.start = AsyncMock()
    ipc.responses = [wire]
    assert error in (await backend.command(["stop"]))["error"]
    assert all(writer.closed for writer in ipc.writers)
    await backend.stop_process()


@pytest.mark.asyncio
async def test_unrelated_events_and_acknowledgements_are_ignored(ipc):
    backend = short_backend(auto_spawn=False)
    backend.start = AsyncMock()
    ipc.responses = [b'{"event":"idle"}\n', b'{"request_id":99,"error":"success"}\n']
    assert await backend.command(["stop"]) == {"request_id": 1, "error": "success"}
    await backend.stop_process()


@pytest.mark.asyncio
async def test_cancelled_command_closes_transport_and_propagates(monkeypatch):
    backend = short_backend(auto_spawn=False)
    backend.start = AsyncMock()
    writer = FakeWriter()
    reading = asyncio.Event()

    async def read():
        reading.set()
        await never()

    monkeypatch.setattr(asyncio, "open_unix_connection", AsyncMock(return_value=(SimpleNamespace(readline=read), writer)))
    task = asyncio.create_task(backend.command(["stop"]))
    await reading.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.closed
    writer.transport.abort.assert_called_once()
    await backend.stop_process()


@pytest.mark.asyncio
@pytest.mark.parametrize("responses", [[{"error": "loading failed"}], [{"error": "success"}, {"error": "unpause failed"}]])
async def test_loadfile_and_unpause_failures_remain_visible(responses):
    backend = short_backend(auto_spawn=False)
    backend.command = AsyncMock(side_effect=mock_gain_responses() + responses)
    assert await backend.loadfile("https://media.example.test/synthetic.wav") == responses[-1]
    assert backend.command.await_count == 4 + len(responses)
    assert backend.command.await_args_list[4].args[0][0] == "loadfile"
    await backend.stop_process()


def mock_gain_responses():
    """Confirm gain before injecting failures specific to source loading."""
    return [
        {"error": "success"}, {"error": "success", "data": 50},
        {"error": "success"}, {"error": "success", "data": False},
    ]


@pytest.mark.asyncio
async def test_checked_command_reports_sanitized_failure():
    backend = short_backend(auto_spawn=False)
    backend.command = AsyncMock(return_value={"error": "failed token=secret at 192.168.1.2"})
    with pytest.raises(BackendError, match="token=\\*\\*\\* at \\[private-ip\\]"):
        await backend.stop()
    await backend.stop_process()


class ServiceHarness:
    """Fake server resources with real tasks and ordered failure injection."""
    def __init__(self, monkeypatch, *, fail=None, block=None, backend_error=None, cleanup_error=None):
        self.events = []
        self.entered = asyncio.Event()
        self.state = None
        self.tasks = []
        self.signal_callbacks = []
        self.removed_signals = []
        self.runners = []
        self.fail = fail
        self.block = block
        self.cleanup_error = cleanup_error
        cfg = Config(uuid="00000000-0000-4000-8000-000000000000", advertise_ip="192.0.2.1", http_api_port=18080, upnp_port=18081, mirror_port_80=True, enable_ssdp=True)
        self.cfg = cfg
        monkeypatch.setattr(cli, "load_startup_config", lambda _, **kwargs: cfg)
        monkeypatch.setattr(cli, "setup_logging", lambda _: None)
        backend = SimpleNamespace()
        async def backend_start():
            await self.step("backend.start")
            if backend_error == "start":
                raise BackendError("synthetic missing executable")
        async def backend_stop():
            await self.step("backend.stop")
            if backend_error == "stop":
                raise BackendError("synthetic refused socket")
        async def backend_close():
            await self.step("backend.close")
        async def backend_gain():
            await self.step("backend.initialize_gain")
            if backend_error == "gain":
                raise BackendError("synthetic unconfirmed gain")
        backend.start = AsyncMock(side_effect=backend_start)
        backend.stop = AsyncMock(side_effect=backend_stop)
        backend.initialize_gain = AsyncMock(side_effect=backend_gain)
        backend.stop_process = AsyncMock(side_effect=backend_close)
        self.backend = backend
        self.backend_factory = Mock(return_value=backend)
        monkeypatch.setattr(cli, "MpvBackend", self.backend_factory)

        async def initialize_balance():
            await self.step("api.initialize_balance")
            if backend_error == "balance":
                raise BackendError("synthetic rejected balance")
        def api_factory(_cfg, state, _backend, **kwargs):
            assert kwargs == {"_startup": True}
            self.state = state
            self.api = SimpleNamespace(cfg=cfg, app=lambda: object(), initialize_channel_balance=AsyncMock(side_effect=initialize_balance))
            return self.api
        monkeypatch.setattr(cli, "HttpApi", api_factory)
        harness = self

        class Runner:
            def __init__(self, app):
                self.index = len(harness.runners)
                harness.runners.append(self)
            async def setup(self):
                await harness.step(f"runner{self.index}.setup")
            async def cleanup(self):
                await harness.step(f"runner{self.index}.close")
        class Site:
            def __init__(self, runner, *_args):
                self.index = runner.index
            async def start(self):
                await harness.step(f"site{self.index}.start")
        class Ssdp:
            tasks = self.tasks
            async def start(self):
                self.tasks.append(asyncio.create_task(never()))
                await harness.step("ssdp.start")
            async def stop(self):
                for task in self.tasks:
                    task.cancel()
                await harness.step("ssdp.close")
        class Mdns:
            async def start(self):
                await harness.step("mdns.start")
                # Signal only after startup, rather than when handlers register.
                asyncio.get_running_loop().call_soon(harness.signal_callbacks[-1][1])
            async def stop(self):
                await harness.step("mdns.close")
            async def refresh(self):
                pass
        monkeypatch.setattr(cli.web, "AppRunner", Runner)
        monkeypatch.setattr(cli.web, "TCPSite", Site)
        monkeypatch.setattr(cli, "SsdpServer", lambda _: Ssdp())
        monkeypatch.setattr(cli, "MdnsAdvertiser", lambda _: Mdns())
        loop = asyncio.get_running_loop()
        def register(sig, callback):
            self.signal_callbacks.append((sig, callback))
        monkeypatch.setattr(loop, "add_signal_handler", register)
        monkeypatch.setattr(loop, "remove_signal_handler", self.removed_signals.append)

    async def step(self, name):
        self.events.append(name)
        if name == self.block:
            self.entered.set()
            await never()
        if name in {self.fail, self.cleanup_error}:
            raise RuntimeError(f"synthetic {name} failure")


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_error", ["start", "stop", "gain", "balance"])
async def test_cli_backend_start_failure_keeps_control_api_available(monkeypatch, backend_error):
    harness = ServiceHarness(monkeypatch, backend_error=backend_error)
    await cli.serve("synthetic.yaml")
    assert harness.state.phase == PlaybackPhase.ERROR
    assert "site0.start" in harness.events
    assert "mdns.start" in harness.events
    assert harness.events[-1] == "backend.close"
    assert set(harness.removed_signals) == {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
    assert all(task.done() for task in harness.tasks)
    if backend_error == "start":
        harness.backend.stop.assert_not_awaited()
    if backend_error in {"start", "stop"}:
        harness.backend.initialize_gain.assert_not_awaited()


@pytest.mark.asyncio
async def test_spotify_failure_keeps_service_available_without_restart(monkeypatch):
    harness = ServiceHarness(monkeypatch)
    source = SimpleNamespace(failed=False, close=AsyncMock())
    source.start = AsyncMock(side_effect=BackendError('synthetic Spotify startup failure'))
    await cli._serve(harness.cfg, spotify_source=source)
    source.start.assert_awaited_once_with(harness.api)
    source.close.assert_awaited_once()
    assert source.failed and harness.state.phase == PlaybackPhase.ERROR
    assert 'site0.start' in harness.events and harness.events[-1] == 'backend.close'


@pytest.mark.asyncio
@pytest.mark.parametrize("muted", [False, True])
async def test_cli_initializes_configured_gain_after_stopping_backend(monkeypatch, muted):
    harness = ServiceHarness(monkeypatch)
    harness.cfg.default_volume = 37
    harness.cfg.default_mute = muted
    harness.cfg.mpv_audio_device = "alsa/dmix:default" if muted else None
    await cli.serve("synthetic.yaml")
    harness.backend_factory.assert_called_once_with(
        harness.cfg.mpv_socket, harness.cfg.auto_spawn_mpv,
        initial_volume=37, initial_mute=muted,
        audio_device=harness.cfg.mpv_audio_device,
    )
    assert harness.events[:3] == ["backend.start", "backend.stop", "backend.initialize_gain"]
    harness.backend.initialize_gain.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('encoded', [
    '{"version":1,"volume":25,"mute":true,"mute":false}',
    '{"version":1,"volume":0,"volume":100,"mute":false}',
    '{"version":2,"version":1,"volume":25,"mute":false}',
])
async def test_cli_rejects_duplicate_saved_gain_before_creating_backend(tmp_path, monkeypatch, encoded):
    config = tmp_path / 'renderer.yaml'
    config.write_text(f'state_dir: {tmp_path}\ndefault_volume: 12\ndefault_mute: true\n')
    path = tmp_path / 'gain.json'
    path.write_text(encoded)
    backend_factory = Mock(side_effect=AssertionError('Backend must not be constructed'))
    monkeypatch.setattr(cli, 'MpvBackend', backend_factory)

    with pytest.raises(ValueError, match='Saved gain settings could not be loaded'):
        await cli.serve(str(config))

    backend_factory.assert_not_called()
    assert path.read_text() == encoded


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["backend.start", "backend.initialize_gain", "runner0.setup", "site0.start", "runner1.setup", "site1.start", "runner2.setup", "site2.start", "ssdp.start", "mdns.start"])
async def test_cli_cleans_every_partially_started_resource_after_failure(monkeypatch, stage):
    harness = ServiceHarness(monkeypatch, fail=stage)
    with pytest.raises(RuntimeError, match="synthetic"):
        await cli.serve("synthetic.yaml")
    assert set(harness.removed_signals) == {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
    assert harness.events[-1] == "backend.close"
    for index in range(len(harness.runners)):
        assert f"runner{index}.close" in harness.events
    if "ssdp.start" in harness.events:
        assert "ssdp.close" in harness.events
        assert all(task.done() for task in harness.tasks)
    if "mdns.start" in harness.events:
        assert "mdns.close" in harness.events


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,failure", [
    ("site0.start", OSError(errno.EACCES, "synthetic permission failure")),
    ("site0.start", OSError(errno.EADDRNOTAVAIL, "synthetic address failure")),
    ("runner0.setup", OSError(errno.EADDRINUSE, "synthetic setup failure")),
])
async def test_cli_only_translates_occupied_port_errors_from_site_start(monkeypatch, stage, failure):
    harness = ServiceHarness(monkeypatch)
    step = harness.step

    async def fail_at_stage(name):
        await step(name)
        if name == stage:
            raise failure

    harness.step = fail_at_stage
    with pytest.raises(OSError) as caught:
        await cli.serve("synthetic.yaml")
    assert caught.value is failure
    assert harness.events[-1] == "backend.close"
    assert "runner0.close" in harness.events


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["backend.start", "backend.initialize_gain", "runner0.setup", "site1.start", "ssdp.start", "mdns.start"])
async def test_cli_cancellation_awaits_partial_start_cleanup(monkeypatch, stage):
    harness = ServiceHarness(monkeypatch, block=stage)
    task = asyncio.create_task(cli.serve("synthetic.yaml"))
    await harness.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert harness.events[-1] == "backend.close"
    for index in range(len(harness.runners)):
        assert f"runner{index}.close" in harness.events
    assert all(task.done() for task in harness.tasks)


@pytest.mark.asyncio
async def test_cli_cleanup_failure_does_not_skip_other_resources(monkeypatch):
    harness = ServiceHarness(monkeypatch, cleanup_error="mdns.close")
    await cli.serve("synthetic.yaml")
    assert "ssdp.close" in harness.events
    assert "runner0.close" in harness.events
    assert harness.events[-1] == "backend.close"
    assert all(task.done() for task in harness.tasks)


@pytest.mark.asyncio
async def test_command_uses_one_budget_for_startup_and_connection(monkeypatch):
    backend = short_backend(auto_spawn=False)
    async def start(**kwargs):
        await asyncio.sleep(0.02)
    async def connect(_):
        await asyncio.sleep(0.02)
        return SimpleNamespace(readline=AsyncMock()), FakeWriter()
    backend.start = start
    monkeypatch.setattr(asyncio, "open_unix_connection", connect)
    begin = asyncio.get_running_loop().time()
    assert "timed out" in (await backend.command(["stop"], timeout=0.03))["error"]
    assert asyncio.get_running_loop().time() - begin < 0.3
    await backend.stop_process()


@pytest.mark.asyncio
async def test_short_command_deadline_does_not_abandon_owned_startup(monkeypatch):
    backend = short_backend()
    backend._startup_timeout = 0.04
    backend._socket_exists = lambda: False
    process = FakeProcess()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert "timed out" in (await backend.command(["stop"], timeout=0.01))["error"]
    assert backend.process is process
    # The shared attempt finishes and reaps on its independent startup bound,
    # even after the last requesting client has stopped waiting.
    await asyncio.gather(backend._startup_task, return_exceptions=True)
    assert process.waited == 1
    assert process.returncode == -15
    assert "timed out" in (await backend.command(["stop"]))["error"]
    spawn.assert_awaited_once()
    await backend.stop_process()


@pytest.mark.asyncio
async def test_shutdown_cancellation_still_awaits_owned_child(monkeypatch, ipc):
    backend = short_backend()
    backend._socket_exists = lambda: backend.process is not None
    process = FakeProcess(ignore_terminate=True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    await backend.start()
    task = asyncio.create_task(backend.stop_process())
    while process.terminated == 0:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.waited == 2
    assert process.killed == 1
    assert backend.process is None
    assert backend._shutdown_task.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("responses", [[{}], [{"error": "success"}, {}]])
async def test_loadfile_requires_explicit_acknowledgement(responses):
    backend = short_backend(auto_spawn=False)
    backend.command = AsyncMock(side_effect=mock_gain_responses() + responses)
    assert "acknowledgement missing" in (await backend.loadfile("https://media.example.test/synthetic.wav"))["error"]
    assert backend.command.await_count == 4 + len(responses)
    assert backend.command.await_args_list[4].args[0][0] == "loadfile"
    await backend.stop_process()


@pytest.mark.asyncio
async def test_cli_signal_cleanup_failure_still_cleans_resources(monkeypatch):
    harness = ServiceHarness(monkeypatch)
    monkeypatch.setattr(asyncio.get_running_loop(), "remove_signal_handler", Mock(side_effect=RuntimeError("synthetic signal failure")))
    await cli.serve("synthetic.yaml")
    assert "runner0.close" in harness.events
    assert harness.events[-1] == "backend.close"
    assert all(task.done() for task in harness.tasks)


@pytest.mark.asyncio
async def test_cli_second_cancellation_during_cleanup_waits_until_finished(monkeypatch):
    harness = ServiceHarness(monkeypatch, block="mdns.start")
    close_entered = asyncio.Event()
    close_release = asyncio.Event()
    async def close():
        close_entered.set()
        await close_release.wait()
        harness.events.append("backend.close")
    harness.backend.stop_process = close
    task = asyncio.create_task(cli.serve("synthetic.yaml"))
    await harness.entered.wait()
    task.cancel()
    await close_entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert harness.events[-1] == "backend.close"


@pytest.mark.asyncio
async def test_shutdown_during_connect_does_not_write_to_external_backend(monkeypatch):
    backend = short_backend(auto_spawn=False)
    backend.start = AsyncMock()
    entered, release = asyncio.Event(), asyncio.Event()
    writer = FakeWriter()
    async def connect(_):
        entered.set()
        await release.wait()
        return SimpleNamespace(readline=AsyncMock()), writer
    monkeypatch.setattr(asyncio, "open_unix_connection", connect)
    command = asyncio.create_task(backend.command(["stop"]))
    await entered.wait()
    await backend.stop_process()
    release.set()
    assert "shut down" in (await command)["error"]
    assert writer.payload is None
    assert writer.closed


@pytest.mark.asyncio
async def test_reaped_owned_socket_is_removed_and_next_instance_can_start(monkeypatch, ipc):
    identity = None
    processes = []
    async def spawn(*args, **kwargs):
        nonlocal identity
        process = FakeProcess()
        process.pid = 42
        processes.append(process)
        identity = (1, 100 + len(processes))
        return process
    def unlink(path):
        nonlocal identity
        assert all(process.returncode is not None for process in processes)
        identity = None
    spawn_mock, unlink_mock = AsyncMock(side_effect=spawn), Mock(side_effect=unlink)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_mock)
    monkeypatch.setattr("fake_linkplay.mpv_backend.os.unlink", unlink_mock)
    for _ in range(2):
        backend = short_backend()
        backend._socket_identity = lambda: identity
        backend._probe_socket = AsyncMock(return_value=42)
        await backend.start()
        assert backend._owned_socket_identity == identity
        await backend.stop_process()
        assert identity is None
    assert spawn_mock.await_count == unlink_mock.call_count == 2
    assert all(process.waited == 1 for process in processes)


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["socket", "regular", "symlink"])
async def test_owned_socket_cleanup_preserves_replaced_foreign_paths(monkeypatch, tmp_path, replacement):
    path = tmp_path / "ipc"
    backend = short_backend()
    backend.socket_path = str(path)
    process = FakeProcess()
    process.pid = 42
    backend._socket_identity = lambda: (1, 100) if backend.process else None
    backend._probe_socket = AsyncMock(return_value=42)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    await backend.start()
    if replacement == "socket":
        backend._socket_identity = lambda: (1, 101)
    else:
        del backend._socket_identity
        if replacement == "regular":
            path.write_text("foreign file")
        else:
            path.symlink_to(tmp_path / "unrelated")
    unlink = Mock()
    monkeypatch.setattr("fake_linkplay.mpv_backend.os.unlink", unlink)
    await backend.stop_process()
    unlink.assert_not_called()
    assert process.waited == 1
    if replacement == "regular":
        assert path.read_text() == "foreign file"
    elif replacement == "symlink":
        assert path.is_symlink()


@pytest.mark.asyncio
@pytest.mark.parametrize("peer_pid", [None, 101])
async def test_socket_is_not_owned_without_matching_child_peer(monkeypatch, peer_pid):
    backend = short_backend()
    process = FakeProcess()
    process.pid = 100
    backend._socket_identity = lambda: (1, 123) if backend.process else None
    backend._probe_socket = AsyncMock(return_value=peer_pid)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    unlink = Mock()
    monkeypatch.setattr("fake_linkplay.mpv_backend.os.unlink", unlink)
    await backend.start()
    assert backend._owned_socket_identity is None
    await backend.stop_process()
    unlink.assert_not_called()


@pytest.mark.asyncio
async def test_socket_probe_reads_linux_peer_credentials_and_closes(monkeypatch):
    import socket
    import struct
    backend = short_backend()
    writer = FakeWriter()
    credentials = Mock(return_value=struct.pack("3i", 42, 1000, 1000))
    writer.get_extra_info = Mock(return_value=SimpleNamespace(getsockopt=credentials))
    monkeypatch.setattr(asyncio, "open_unix_connection", AsyncMock(return_value=(object(), writer)))
    assert await backend._probe_socket() == 42
    credentials.assert_called_once_with(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    assert writer.closed
    await backend.stop_process()


@pytest.mark.asyncio
async def test_cli_signal_handlers_exist_before_startup_and_through_cleanup(monkeypatch):
    harness = ServiceHarness(monkeypatch)
    async def start():
        assert {sig for sig, _ in harness.signal_callbacks} == {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
        await harness.step("backend.start")
    async def close():
        assert harness.removed_signals == []
        await harness.step("backend.close")
    harness.backend.start = start
    harness.backend.stop_process = close
    await cli.serve("synthetic.yaml")
    assert set(harness.removed_signals) == {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}


@pytest.mark.asyncio
async def test_cli_stuck_discovery_cleanup_is_bounded_and_backend_still_reaped(monkeypatch):
    harness = ServiceHarness(monkeypatch, block="mdns.close")
    monkeypatch.setattr(cli, "RESOURCE_CLEANUP_TIMEOUT", 0.01)
    await asyncio.wait_for(cli.serve("synthetic.yaml"), 0.5)
    assert "ssdp.close" in harness.events
    assert harness.events[-1] == "backend.close"
    assert all(task.done() for task in harness.tasks)


@pytest.mark.parametrize("phase", ["startup", "shutdown", "error"])
@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGINT", "SIGHUP"])
def test_real_process_signals_cannot_abandon_startup_or_teardown(phase, signal_name):
    """Actual OS signals in a child interpreter; player/network are mocked."""
    import subprocess
    import sys
    import textwrap
    from pathlib import Path
    source = textwrap.dedent('''
        import asyncio
        import os
        import signal
        import sys
        from types import SimpleNamespace
        from fake_linkplay import cli
        from fake_linkplay.config import Config

        phase, signal_name = sys.argv[1:]
        sig = getattr(signal, signal_name)
        def send_signal():
            os.kill(os.getpid(), sig)
        async def noop():
            pass
        async def start_backend():
            print("child_acquired", flush=True)
            if phase == "startup":
                asyncio.get_running_loop().call_soon(send_signal)
                await asyncio.sleep(30)
        async def stop_backend():
            print("child_reaped", flush=True)
        async def start_mdns():
            if phase == "error":
                raise RuntimeError("synthetic startup failure")
            if phase == "shutdown":
                asyncio.get_running_loop().call_soon(send_signal)
        async def stop_mdns():
            print("cleanup_entered", flush=True)
            # This second real signal used to restore default SIGTERM behavior
            # and terminate the interpreter before stop_process could run.
            asyncio.get_running_loop().call_soon(send_signal)
            await asyncio.sleep(0.03)
            print("cleanup_finished", flush=True)
        cli.load_startup_config = lambda _, **kwargs: Config(uuid="00000000-0000-4000-8000-000000000000", advertise_ip="192.0.2.1", enable_ssdp=False)
        cli.setup_logging = lambda _: None
        cli.MpvBackend = lambda *args, **kwargs: SimpleNamespace(start=start_backend, stop=noop, initialize_gain=noop, stop_process=stop_backend)
        cli.HttpApi = lambda *args, **kwargs: SimpleNamespace(cfg=args[0], app=lambda: object(), initialize_channel_balance=noop)
        cli.web.AppRunner = lambda *args: SimpleNamespace(setup=noop, cleanup=noop)
        cli.web.TCPSite = lambda *args: SimpleNamespace(start=noop)
        cli.MdnsAdvertiser = lambda *args: SimpleNamespace(start=start_mdns, stop=stop_mdns, refresh=noop)
        asyncio.run(cli.serve("synthetic.yaml"))
        print("clean_exit", flush=True)
    ''')
    result = subprocess.run([sys.executable, "-c", source, phase, signal_name], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5)
    if phase == "error":
        assert result.returncode == 1, result.stdout + result.stderr
        assert "RuntimeError: synthetic startup failure" in result.stderr
        assert "clean_exit" not in result.stdout
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "clean_exit" in result.stdout
    assert "child_acquired" in result.stdout
    assert "child_reaped" in result.stdout
    if phase in {"shutdown", "error"}:
        assert "cleanup_entered" in result.stdout
        assert "cleanup_finished" in result.stdout


@pytest.mark.asyncio
async def test_shutdown_while_closing_readiness_probe_cleans_verified_owned_socket(monkeypatch):
    import struct
    backend = short_backend()
    process = FakeProcess()
    process.pid = 42
    backend._socket_identity = lambda: (1, 100) if backend.process else None
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    writer = FakeWriter()
    writer.get_extra_info = Mock(return_value=SimpleNamespace(getsockopt=lambda *args: struct.pack("3i", 42, 1000, 1000)))
    closing = asyncio.Event()
    async def close():
        closing.set()
        await never()
    writer.wait_closed = close
    monkeypatch.setattr(asyncio, "open_unix_connection", AsyncMock(return_value=(object(), writer)))
    unlink = Mock()
    monkeypatch.setattr("fake_linkplay.mpv_backend.os.unlink", unlink)
    starting = asyncio.create_task(backend.start())
    await closing.wait()
    assert backend._owned_socket_identity == (1, 100)
    await backend.stop_process()
    await asyncio.gather(starting, return_exceptions=True)
    unlink.assert_called_once_with(backend.socket_path)
    assert process.waited == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('balance', ['0.0', '0.5'])
async def test_cli_always_initializes_balance_through_confirmed_api(monkeypatch, balance):
    harness = ServiceHarness(monkeypatch)
    harness.cfg.channel_balance = balance
    await cli.serve('synthetic.yaml')
    harness.api.initialize_channel_balance.assert_awaited_once()
    assert harness.events.index('api.initialize_balance') < harness.events.index('site0.start')


@pytest.mark.asyncio
async def test_death_after_ready_reaps_once_and_requires_a_fresh_backend(monkeypatch, ipc):
    """UNIT/MOCK counterpart of the native post-decode player-death case."""
    identity = None
    children = []

    async def spawn(*args, **kwargs):
        nonlocal identity
        process = FakeProcess()
        process.pid = 100 + len(children)
        children.append(process)
        identity = (1, process.pid)
        return process

    async def probe(**kwargs):
        if children[-1].returncode is not None:
            raise ConnectionRefusedError(errno.ECONNREFUSED, "synthetic dead player")
        return children[-1].pid

    def unlink(path):
        nonlocal identity
        assert children[-1].returncode is not None
        identity = None

    spawn_mock, unlink_mock = AsyncMock(side_effect=spawn), Mock(side_effect=unlink)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_mock)
    monkeypatch.setattr("fake_linkplay.mpv_backend.os.unlink", unlink_mock)
    backend = MpvBackend("/synthetic-death.sock", initial_volume=31, initial_mute=True)
    backend._socket_identity = lambda: identity
    backend._probe_socket = probe
    try:
        await backend.start()
        process = children[0]
        assert backend._owned_process is process and backend._owned_socket_identity == identity
        assert (await backend.command(["get_property", "pause"]))["error"] == "success"
        process.returncode = -9
        process.exited.set()  # No real player, signal or Unix socket is used.
        assert await backend.snapshot() == {"backend-error": True}
        assert process.waited == 1 and process.terminated == process.killed == 0
        assert backend.process is backend._owned_process is None and identity is None
        unlink_mock.assert_called_once_with(backend.socket_path)
        for _ in range(4):
            assert await backend.snapshot() == {"backend-error": True}
            with pytest.raises(BackendError):
                await backend.loadfile("https://media.example.test/must-not-load.wav")
        assert (backend._desired_volume, backend._desired_mute) == (31, True)
        assert backend.gain_confirmed is False
        assert [writer.payload["command"] for writer in ipc.writers if writer.payload] == [
            ["get_property", "pause"],
        ]
        spawn_mock.assert_awaited_once()
        assert process.waited == 1
    finally:
        await backend.stop_process()
    with pytest.raises(BackendError, match="shut down"):
        await backend.start()
    replacement = MpvBackend(backend.socket_path, initial_volume=31, initial_mute=True)
    replacement._socket_identity = lambda: identity
    replacement._probe_socket = probe
    try:
        await replacement.start()
        assert replacement.process is children[1]
        assert replacement._owned_socket_identity == identity
        assert spawn_mock.await_count == 2
        # Startup alone issued no load, seek, unpause or gain-changing command.
        assert len([writer for writer in ipc.writers if writer.payload]) == 1
    finally:
        await replacement.stop_process()
    assert identity is None and all(child.returncode is not None for child in children)
    assert all(child.waited == 1 for child in children)
