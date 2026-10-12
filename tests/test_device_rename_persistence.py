"""INTEGRATION/MOCK rename transactions: local HTTP, synthetic disk/discovery faults.

Restart checks re-read actual state files. No multicast, Android, hardware or
power-loss durability is exercised; discovery transports are deterministic doubles.
"""
import asyncio
import errno
import io
import json
import os
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from xml.etree import ElementTree as ET

import pytest
from aiohttp.test_utils import TestClient, TestServer

from fake_linkplay.config import load_config
from fake_linkplay.http_api import HttpApi, ParsedCommand
from fake_linkplay.mdns import CustomMdnsProtocol, CustomMdnsResponder, MdnsAdvertiser, encode_name
from fake_linkplay.state import RendererState
from test_status_json import DummyMpv


OLD_NAME = "Original Café"
NEW_NAME = "Küche 日本語 🔊"
IDENTITY = "00000000-0000-4000-8000-000000000001"


def rename_command(name=NEW_NAME, *, hexadecimal=False):
    return ("setHexDeviceName:" + name.encode("utf-8").hex() if hexadecimal
            else "setDeviceName:" + name)


@pytest.fixture(params=[False, True], ids=["generic", "wiim-home"])
async def renderer(tmp_path, request):
    directory = tmp_path / "state"
    directory.mkdir()
    (directory / "uuid").write_text(IDENTITY + "\n", encoding="utf-8")
    (directory / "device_name").write_text(OLD_NAME + "\n", encoding="utf-8")
    (directory / "channel_balance").write_text("0.25\n", encoding="utf-8")
    (directory / "gain.json").write_text('{"version":1,"volume":25,"mute":true}\n', encoding="utf-8")
    config = tmp_path / "renderer.yaml"
    config.write_text(f"state_dir: {directory}\nfriendly_name: Configured Room\nadvertise_ip: 127.0.0.1\nwiim_home_profile: {str(request.param).lower()}\n", encoding="utf-8")
    cfg = load_config(config)
    callback = AsyncMock()
    api = HttpApi(cfg, RendererState.from_config(cfg), DummyMpv(), on_rename=callback)
    notify = Mock()
    api._schedule_event_notify = notify
    client = TestClient(TestServer(api.app()))
    await client.start_server()
    env = SimpleNamespace(api=api, cfg=cfg, client=client, callback=callback,
                          notify=notify, directory=directory, config=config)
    try:
        yield env
    finally:
        await client.close()
    assert api._state_monitor_task is None


def files(env):
    return {path.name: path.read_bytes() for path in env.directory.iterdir()}


def assert_restart_name(env, expected):
    restarted = load_config(env.config)
    assert restarted.friendly_name == restarted.mdns_instance_name == expected
    assert restarted.uuid == env.cfg.uuid == IDENTITY
    assert restarted.upnp_uuid == env.cfg.upnp_uuid
    assert restarted.mdns_hostname == env.cfg.mdns_hostname
    assert restarted.default_volume == 25 and restarted.default_mute is True
    assert restarted.channel_balance == "0.25"


async def request(env, command):
    async with env.client.get("/httpapi.asp", params={"command": command}) as response:
        return response.status, await response.text()


def fail_partial_write(monkeypatch):
    """Inject the same partial text-write ENOSPC into direct and temp writes."""
    original_open = io.open

    class PartialWrite:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, value):
            self.stream.write(value[:3])
            self.stream.flush()
            raise OSError(errno.ENOSPC, "synthetic disk full")

        def __getattr__(self, name):
            return getattr(self.stream, name)

    def fault_open(file, mode="r", *args, **kwargs):
        stream = original_open(file, mode, *args, **kwargs)
        return PartialWrite(stream) if mode == "w" else stream

    monkeypatch.setattr(io, "open", fault_open)


@pytest.mark.parametrize("hexadecimal", [False, True], ids=["plain", "hex"])
@pytest.mark.parametrize("fault", ["temp-create", "partial-write", "file-fsync", "replace"])
async def test_precommit_failure_preserves_existing_file_live_name_and_identity(renderer, monkeypatch, hexadecimal, fault):
    env = renderer
    before = files(env)

    def fail(*args, **kwargs):
        raise OSError(errno.ENOSPC, "synthetic disk failure")

    with monkeypatch.context() as patch:
        if fault == "partial-write":
            fail_partial_write(patch)
        else:
            target = {"temp-create": "tempfile.mkstemp", "file-fsync": "os.fsync", "replace": "os.replace"}[fault]
            patch.setattr("fake_linkplay.persistence." + target, fail)
        outcome = await request(env, rename_command(hexadecimal=hexadecimal))

    assert outcome == (502, "Failed")
    assert files(env) == before
    assert env.cfg.friendly_name == env.cfg.mdns_instance_name == OLD_NAME
    assert env.api.device_name_override == ""
    env.callback.assert_not_awaited()
    env.notify.assert_not_called()
    assert_restart_name(env, OLD_NAME)


@pytest.mark.parametrize("hexadecimal", [False, True], ids=["plain", "hex"])
async def test_rename_commits_before_callback_and_survives_restart(renderer, hexadecimal):
    env = renderer
    before = files(env)
    order = []

    async def callback():
        order.append("refresh")
        assert env.cfg.friendly_name == env.cfg.mdns_instance_name == NEW_NAME
        assert env.api.device_name_override == NEW_NAME
        assert_restart_name(env, NEW_NAME)
        env.notify.assert_not_called()

    env.api.on_rename = callback
    assert await request(env, rename_command(hexadecimal=hexadecimal)) == (200, "OK")
    assert order == ["refresh"]
    env.notify.assert_called_once_with()
    assert files(env) == {**before, "device_name": (NEW_NAME + "\n").encode("utf-8")}
    assert_restart_name(env, NEW_NAME)
    status, body = await request(env, "getStatusEx")
    assert status == 200
    assert json.loads(body)["DeviceName"] == NEW_NAME
    async with env.client.get("/description.xml") as response:
        description = ET.fromstring(await response.text())
    ns = {"d": "urn:schemas-upnp-org:device-1-0"}
    assert description.findtext(".//d:friendlyName", namespaces=ns) == NEW_NAME
    assert description.findtext(".//d:UDN", namespaces=ns) == "uuid:" + env.cfg.upnp_uuid


@pytest.mark.parametrize("fault", ["directory-open", "directory-fsync"])
async def test_postcommit_durability_failure_keeps_disk_and_live_name_aligned(renderer, monkeypatch, fault, caplog):
    env = renderer
    caplog.set_level("INFO", logger="fake_linkplay.http_api")
    before = files(env)
    real_open, real_fsync = os.open, os.fsync

    def open_directory(path, flags, *args, **kwargs):
        if flags & os.O_DIRECTORY:
            raise OSError(errno.EIO, "synthetic directory open failure")
        return real_open(path, flags, *args, **kwargs)

    def sync_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "synthetic directory fsync failure")
        return real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr("fake_linkplay.persistence.os." + ("open" if fault == "directory-open" else "fsync"),
                      open_directory if fault == "directory-open" else sync_directory)
        assert await request(env, rename_command()) == (502, "Failed")

    assert files(env) == {**before, "device_name": (NEW_NAME + "\n").encode("utf-8")}
    assert env.cfg.friendly_name == env.cfg.mdns_instance_name == env.api.device_name_override == NEW_NAME
    env.callback.assert_awaited_once_with()
    env.notify.assert_called_once_with()
    assert_restart_name(env, NEW_NAME)
    assert "Persisted WiiM Home device rename request" not in caplog.text
    assert await request(env, rename_command()) == (200, "OK")
    assert_restart_name(env, NEW_NAME)


@pytest.mark.parametrize("failure", [OSError("synthetic discovery failure"), ValueError("synthetic callback failure")])
async def test_callback_failure_reports_failure_without_rolling_back_committed_name(renderer, failure):
    env = renderer
    env.callback.side_effect = failure
    assert await request(env, rename_command()) == (502, "Failed")
    assert env.cfg.friendly_name == env.api.device_name_override == NEW_NAME
    env.notify.assert_called_once_with()
    assert_restart_name(env, NEW_NAME)
    env.callback.side_effect = None
    assert await request(env, rename_command()) == (200, "OK")
    assert env.callback.await_count == 2
    assert_restart_name(env, NEW_NAME)


@pytest.mark.parametrize("cancel_first", [False, True], ids=["finish-refresh", "cancel-refresh"])
async def test_delayed_refresh_serializes_two_names_and_cancellation_preserves_commit(renderer, cancel_first):
    env = renderer
    started, release, second_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed = []
    second_name = "Second Room"

    async def callback():
        observed.append(env.cfg.friendly_name)
        if len(observed) == 1:
            started.set()
            await release.wait()
            assert env.cfg.friendly_name == NEW_NAME

    async def second_command():
        second_entered.set()
        return await env.api.execute(ParsedCommand(rename_command(second_name)), "127.0.0.1")

    env.api.on_rename = callback
    first = asyncio.create_task(env.api.execute(ParsedCommand(rename_command()), "127.0.0.1"))
    second = None
    try:
        async with asyncio.timeout(2):
            await started.wait()
            assert_restart_name(env, NEW_NAME)
            second = asyncio.create_task(second_command())
            await second_entered.wait()
            assert env.cfg.friendly_name == NEW_NAME
            assert observed == [NEW_NAME]
            if cancel_first:
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
            else:
                release.set()
                assert await first == "OK"
            assert await second == "OK"
    finally:
        release.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)
    assert observed == [NEW_NAME, second_name]
    assert env.notify.call_count == 2
    assert_restart_name(env, second_name)


async def test_cancelling_waiting_rename_cannot_persist_second_name(renderer):
    env = renderer
    started, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def callback():
        started.set()
        await release.wait()

    async def second_command():
        queued.set()
        return await env.api.execute(ParsedCommand(rename_command("Cancelled Room")), "127.0.0.1")

    env.api.on_rename = callback
    first = asyncio.create_task(env.api.execute(ParsedCommand(rename_command()), "127.0.0.1"))
    second = None
    try:
        async with asyncio.timeout(2):
            await started.wait()
            second = asyncio.create_task(second_command())
            await queued.wait()
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            assert_restart_name(env, NEW_NAME)
            release.set()
            assert await first == "OK"
    finally:
        release.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)
    env.notify.assert_called_once_with()
    assert_restart_name(env, NEW_NAME)


async def test_custom_discovery_refresh_failure_and_retry_use_committed_name_and_stable_identity(renderer):
    env = renderer
    protocol = CustomMdnsProtocol(env.cfg)
    transport = Mock()
    protocol.connection_made(transport)
    old_instance = protocol.instance
    responder = CustomMdnsResponder(env.cfg)
    responder._protocol = protocol
    advertiser = MdnsAdvertiser(env.cfg)
    advertiser._zeroconf = responder
    env.api.on_rename = advertiser.refresh
    transport.sendto.side_effect = OSError("synthetic send failure")
    assert await request(env, rename_command()) == (502, "Failed")
    assert protocol._published_instance == old_instance
    assert_restart_name(env, NEW_NAME)
    env.notify.assert_called_once_with()

    transport.sendto.reset_mock(side_effect=True)
    assert await request(env, rename_command()) == (200, "OK")
    assert protocol._published_instance == protocol.instance
    assert protocol.instance != old_instance
    assert transport.sendto.call_count == 2
    goodbye, announcement = [call.args[0] for call in transport.sendto.call_args_list]
    assert encode_name(old_instance) in goodbye
    assert encode_name(protocol.instance) in announcement
    assert encode_name(env.cfg.mdns_hostname) in announcement
    assert env.cfg.wiim_txt_uuid.encode() in announcement
    assert_restart_name(env, NEW_NAME)


@pytest.mark.parametrize("command", [
    "setDeviceName:", "setDeviceName:" + "x" * 129,
    "setDeviceName:bad%00name", "setDeviceName:bad%0Aname", "setDeviceName:bad%7Fname",
    "setDeviceName:bad%C2%80name", "setDeviceName:bad%EF%BF%BEname",
    "setHexDeviceName:xyz", "setHexDeviceName:ff", "setHexDeviceName:eda080",
])
async def test_invalid_rename_still_rejects_before_any_write_or_refresh(renderer, command):
    env = renderer
    before = files(env)
    assert await request(env, command) == (400, "Failed")
    assert files(env) == before
    assert env.cfg.friendly_name == OLD_NAME
    env.callback.assert_not_awaited()
    env.notify.assert_not_called()
    assert_restart_name(env, OLD_NAME)


async def test_rename_without_discovery_callback_and_first_persistence(renderer):
    env = renderer
    (env.directory / "device_name").unlink()
    env.api.on_rename = None
    assert await request(env, rename_command()) == (200, "OK")
    env.notify.assert_called_once_with()
    assert_restart_name(env, NEW_NAME)


async def test_valid_maximum_unicode_name_remains_supported(renderer):
    env = renderer
    name = "界" * 128
    assert await request(env, rename_command("  " + name + "  ", hexadecimal=True)) == (200, "OK")
    assert_restart_name(env, name)
