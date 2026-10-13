"""UNIT/MOCK explicit output selection; no sockets, mpv, or hardware accessed."""

from __future__ import annotations

import asyncio
import struct
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from fake_linkplay.config import Config, StartupConfigError, read_startup_config
from fake_linkplay.mpv_backend import BackendError, MpvBackend
from test_backend_lifecycle import FakeIpc, FakeProcess


class OwnedOutput:
    """Synthetic path and peer identities, using real backend lifecycle code."""

    def __init__(self, monkeypatch, device="alsa/dmix:default"):
        self.backend = MpvBackend("/synthetic-mpv.sock", audio_device=device)
        self.process = FakeProcess()
        self.process.pid = 1234
        self.identity = None
        self.peer_pid = self.process.pid
        self.ipc = FakeIpc()
        self.backend._socket_identity = lambda: self.identity

        async def spawn(*_args, **_kwargs):
            self.identity = (1, 2)
            return self.process

        async def connect(path):
            reader, writer = await self.ipc.open(path)
            ipc_socket = Mock()
            if self.peer_pid is None:
                ipc_socket.getsockopt.side_effect = OSError("synthetic unavailable credentials")
            else:
                ipc_socket.getsockopt.return_value = struct.pack("3i", self.peer_pid, 1000, 1000)
            writer.get_extra_info = Mock(return_value=ipc_socket)
            return reader, writer

        self.spawn = AsyncMock(side_effect=spawn)
        self.connect = AsyncMock(side_effect=connect)
        self.unlink = Mock(side_effect=lambda _: setattr(self, "identity", None))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", self.spawn)
        monkeypatch.setattr(asyncio, "open_unix_connection", self.connect)
        monkeypatch.setattr("fake_linkplay.mpv_backend.os.unlink", self.unlink)


def test_config_preserves_device_identifiers_and_default_compatibility(tmp_path):
    for device in (None, "auto", "null", "alsa/dmix:default", "alsa/hw:CARD=USB,DEV=0",
                   "pulse/Device with spaces; literal $(text)"):
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"mpv_audio_device": device}))
        assert read_startup_config(path).mpv_audio_device == device
        assert MpvBackend("/synthetic.sock", audio_device=device).audio_device == device
    assert Config().mpv_audio_device is None
    assert Config(auto_spawn_mpv=False).mpv_audio_device is None
    assert MpvBackend("/synthetic.sock", False).audio_device is None
    assert not (tmp_path / ".state").exists()


def test_invalid_device_and_external_mode_fail_privately_before_startup(tmp_path):
    values = ("", " ", "help", "alsa/private\nvalue", "alsa/\x1b[31mprivate",
              "alsa/private\x7fvalue", "alsa/\ud800", " alsa/private", "alsa/private ",
              "--audio-device=private", "/private", "alsa/" + "x" * 1024,
              "alsa/" + "ä" * 600, False, 123, [], {})
    path = tmp_path / "config.yaml"
    for device in values:
        for make in (lambda: Config(mpv_audio_device=device),
                     lambda: MpvBackend("/synthetic.sock", audio_device=device)):
            with pytest.raises(ValueError, match="mpv_audio_device") as failure:
                make()
            assert "private" not in str(failure.value)
        path.write_text(yaml.safe_dump({"mpv_audio_device": device}))
        with pytest.raises(StartupConfigError) as failure:
            read_startup_config(path)
        assert "private" not in str(failure.value)
    for auto_spawn in (False, "true", 1):
        with pytest.raises(ValueError, match="requires auto_spawn_mpv"):
            Config(mpv_audio_device="auto", auto_spawn_mpv=auto_spawn)
        with pytest.raises(ValueError, match="requires auto_spawn_mpv"):
            MpvBackend("/synthetic.sock", auto_spawn, audio_device="auto")
    assert not (tmp_path / ".state").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("device", [None, "alsa/Device with spaces; $(literal)"])
async def test_spawn_preserves_legacy_arguments_or_isolates_one_literal_device_argument(monkeypatch, device):
    fixture = OwnedOutput(monkeypatch, device)
    backend = fixture.backend
    try:
        await asyncio.gather(*(backend.start() for _ in range(3)))
        assert (await backend.command(["get_property", "audio-device"]))["error"] == "success"
        arguments = ["mpv", "--no-video", "--idle=yes", "--input-ipc-server=/synthetic-mpv.sock"]
        if device is not None:
            arguments.extend(["--no-config", "--load-scripts=no", "--audio-fallback-to-null=no",
                              f"--audio-device={device}"])
        fixture.spawn.assert_awaited_once_with(
            *arguments, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        assert backend._owned_socket_identity == (1, 2)
    finally:
        await backend.stop_process()
    assert fixture.process.terminated == 1
    fixture.unlink.assert_called_once_with("/synthetic-mpv.sock")


@pytest.mark.asyncio
async def test_explicit_device_refuses_preexisting_socket_without_contact_or_spawn(monkeypatch):
    fixture = OwnedOutput(monkeypatch)
    fixture.identity = (9, 9)
    try:
        with pytest.raises(BackendError, match="owned, peer-verified"):
            await fixture.backend.start()
        response = await fixture.backend.command(["loadfile", "synthetic.wav"])
        assert "owned, peer-verified" in response["error"]
        assert fixture.backend.audio_device not in response["error"]
    finally:
        await fixture.backend.stop_process()
    fixture.connect.assert_not_awaited()
    fixture.spawn.assert_not_awaited()
    fixture.unlink.assert_not_called()
    assert fixture.process.terminated == 0 and fixture.identity == (9, 9)


@pytest.mark.asyncio
@pytest.mark.parametrize("peer_pid", [9999, None])
async def test_explicit_spawn_refuses_racing_or_unverifiable_player_and_reaps_only_child(monkeypatch, peer_pid):
    fixture = OwnedOutput(monkeypatch)
    fixture.peer_pid = peer_pid
    with pytest.raises(BackendError, match="owned, peer-verified"):
        await fixture.backend.start()
    await fixture.backend.stop_process()
    assert fixture.process.terminated == 1 and fixture.process.waited == 1
    assert all(writer.payload is None and writer.closed for writer in fixture.ipc.writers)
    assert fixture.identity == (1, 2)
    fixture.unlink.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["foreign-peer", "unknown-peer", "changed-path"])
async def test_replacement_between_startup_and_command_is_refused_before_any_write(monkeypatch, replacement):
    fixture = OwnedOutput(monkeypatch)
    backend = fixture.backend
    await backend.start()
    real_connect = fixture.connect.side_effect
    connections = 0

    async def replace_on_command(path):
        nonlocal connections
        connections += 1
        if connections == 2:  # First is readiness; second carries the command.
            if replacement == "changed-path":
                fixture.identity = (9, 9)
            else:
                fixture.peer_pid = 9999 if replacement == "foreign-peer" else None
        return await real_connect(path)

    fixture.connect.side_effect = replace_on_command
    try:
        response = await backend.command(["loadfile", "synthetic.wav"])
        assert "owned, peer-verified" in response["error"]
        assert connections == 2
        assert all(writer.payload is None and writer.closed for writer in fixture.ipc.writers)
        assert fixture.process.terminated == 0
    finally:
        # A replacement path must remain untouched; cleanup only owns (1, 2).
        fixture.identity = (9, 9)
        await backend.stop_process()
    fixture.unlink.assert_not_called()
    assert fixture.process.terminated == 1


@pytest.mark.asyncio
async def test_replaced_path_on_later_start_is_refused_without_contact(monkeypatch):
    fixture = OwnedOutput(monkeypatch)
    await fixture.backend.start()
    fixture.identity = (9, 9)
    fixture.connect.reset_mock()
    with pytest.raises(BackendError, match="owned, peer-verified"):
        await fixture.backend.start()
    await fixture.backend.stop_process()
    fixture.connect.assert_not_awaited()
    fixture.unlink.assert_not_called()
    assert fixture.process.terminated == 1
