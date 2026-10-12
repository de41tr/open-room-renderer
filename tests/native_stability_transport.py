"""Real Unix-IPC observation/lifecycle for the shared synthetic stability flow.

Production MpvBackend methods are never replaced. The extra read-only client
counts public start-file/path events, as documented at
https://mpv.io/manual/stable/#json-ipc . It never sends playback commands.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path
import socket
import tempfile
from typing import Any

from fake_linkplay.mpv_backend import MpvBackend
from test_mpv_unix_integration import _external_player


class UnixEvents:
    def __init__(self) -> None:
        self.loads = 0
        self.eof_events = 0
        self.source_changes = 0
        self.last_source = ""
        self.reader = None
        self.writer = None
        self.task = None
        self.pending: dict[int, asyncio.Future] = {}
        self.error = ""
        self.closing = False

    def observe(self, message: dict[str, Any]) -> None:
        if "request_id" in message:
            future = self.pending.pop(message["request_id"], None)
            if future is not None and not future.done():
                future.set_result(message)
        event = message.get("event")
        if event == "start-file":
            self.loads += 1
        elif event == "property-change" and message.get("name") == "path":
            path = message.get("data")
            if path:
                assert isinstance(path, str)
                self.source_changes += int(path != self.last_source)
                self.last_source = path
        elif event == "end-file" and message.get("reason") == "eof":
            self.eof_events += 1
        elif event == "queue-overflow":
            raise AssertionError("Native event observer overflowed")

    async def consume(self) -> None:
        try:
            while line := await self.reader.readline():
                message = json.loads(line)
                assert isinstance(message, dict)
                self.observe(message)
            if not self.closing:
                self.error = "Native event observer closed unexpectedly"
        except asyncio.CancelledError:
            raise
        except (AssertionError, OSError, ValueError) as exc:
            self.error = str(exc)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError(self.error or "Native event observer closed"))
            self.pending.clear()

    async def start(self, endpoint: str) -> None:
        self.reader, self.writer = await asyncio.wait_for(asyncio.open_unix_connection(endpoint), 3)
        self.task = asyncio.create_task(self.consume())
        for request_id, command in enumerate((["enable_event", "start-file"], ["enable_event", "end-file"], ["observe_property", 1, "path"]), 1):
            future = asyncio.get_running_loop().create_future()
            self.pending[request_id] = future
            self.writer.write((json.dumps({"command": command, "request_id": request_id}) + "\n").encode())
            await asyncio.wait_for(self.writer.drain(), 3)
            result = await asyncio.wait_for(future, 3)
            assert result.get("error") == "success", "Native event observer setup failed"
        assert not self.error and self.loads == 0

    async def close(self) -> None:
        self.closing = True
        if self.writer is not None:
            self.writer.close()
            await asyncio.wait_for(self.writer.wait_closed(), 3)
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        assert not self.pending


class UnixPlayer:
    expected_children = 1

    def __init__(self, executable: str) -> None:
        self.executable = executable
        self.stack = AsyncExitStack()
        self.observer = UnixEvents()
        self.backend: MpvBackend | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.endpoint: Path | None = None
        self.closed = False

    @property
    def loads(self) -> int:
        return self.observer.loads

    @property
    def source_changes(self) -> int:
        return self.observer.source_changes

    @property
    def eof_events(self) -> int:
        return self.observer.eof_events

    @property
    def child_pid(self) -> int:
        assert self.process is not None
        return self.process.pid

    async def start(self) -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM):
                pass
        except OSError as exc:
            raise AssertionError(f"Native Unix IPC unavailable ({type(exc).__name__}, errno {exc.errno})") from exc
        try:
            # The HTTP/media snapshot path can exceed sockaddr_un's 108 bytes.
            directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="orr-stability-")))
            self.backend, self.process = await self.stack.enter_async_context(_external_player(directory, self.executable))
            self.endpoint = Path(self.backend.socket_path)
            await self.observer.start(self.backend.socket_path)
            # The reused external-player fixture defaults to 25; choose the
            # shared flow's desired 50 through production acknowledged controls.
            await self.backend.volume(50)
            await self.backend.mute(False)
        except BaseException:
            await self.close()
            raise

    async def property(self, name: str) -> Any:
        assert self.backend is not None
        return (await self.backend.checked_command(["get_property", name]))["data"]

    def assert_alive(self) -> None:
        assert self.process is not None and self.process.returncode is None
        assert self.backend is not None and self.backend.process is None
        assert self.observer.task is not None and not self.observer.task.done()
        assert not self.observer.error, self.observer.error
        from libmpv_stability_support import child_pids
        assert child_pids() == {self.process.pid}, "Controller spawned an unexpected child"


    async def close(self) -> None:
        observer_failure = self.observer.error
        if self.observer.task is not None and self.observer.task.done() and not self.observer.closing:
            observer_failure = observer_failure or "Native event observer stopped before cleanup"
        try:
            await self.observer.close()
        finally:
            await self.stack.aclose()
        if self.process is not None:
            assert self.process.returncode is not None
            assert not Path(f"/proc/{self.process.pid}").exists(), "Fixture child was not reaped"
        assert self.endpoint is None or not self.endpoint.exists()
        self.closed = True
        assert not observer_failure, observer_failure

    def cleanup_evidence(self) -> dict[str, bool]:
        assert self.closed
        return {"fixture_child_reaped": True, "unix_socket_removed": True, "event_observer_closed": True}
