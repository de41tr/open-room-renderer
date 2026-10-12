"""UNIT/MOCK gain contracts; no real player, mixer, device or audio output."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from fake_linkplay.mpv_backend import BackendError, MpvBackend


class GainPeer:
    """Synthetic mpv properties, initially at mpv's native volume of 100."""

    def __init__(self):
        self.properties = {"volume": 100, "mute": False, "pause": False}
        self.calls = []
        self.loads = []
        self.responses = {}

    async def command(self, args, timeout=5.0):
        self.calls.append(args.copy())
        await asyncio.sleep(0)
        operation, *arguments = args
        override = self.responses.get(tuple(args))
        if override is not None:
            return override
        if operation == "set_property":
            name, value = arguments
            self.properties[name] = value
        elif operation == "get_property":
            return {"error": "success", "data": self.properties[arguments[0]]}
        elif operation == "loadfile":
            self.loads.append((arguments[0], self.properties.copy()))
        return {"error": "success"}


def make_backend(**kwargs):
    backend = MpvBackend("/unused-synthetic-gain", auto_spawn=False, **kwargs)
    peer = GainPeer()
    backend.command = AsyncMock(side_effect=peer.command)
    return backend, peer


@pytest.mark.asyncio
async def test_initialize_replaces_native_volume100_with_configured_default50():
    backend, peer = make_backend()
    assert peer.properties["volume"] == 100
    await backend.initialize_gain()
    assert peer.properties == {"volume": 50, "mute": False, "pause": False}
    assert peer.calls == [
        ["set_property", "volume", 50], ["get_property", "volume"],
        ["set_property", "mute", False], ["get_property", "mute"],
    ]
    await backend.initialize_gain()
    assert len(peer.calls) == 4
    assert backend._gain_initialized


@pytest.mark.asyncio
async def test_first_load_initializes_explicit_mute_before_volume_and_load():
    backend, peer = make_backend(initial_volume=23, initial_mute=True)
    result = await backend.loadfile("https://media.example.test/one.wav")
    assert result == {"error": "success"}
    assert peer.calls[:4] == [
        ["set_property", "mute", True], ["get_property", "mute"],
        ["set_property", "volume", 23], ["get_property", "volume"],
    ]
    assert peer.loads == [
        ("https://media.example.test/one.wav", {"volume": 23, "mute": True, "pause": False}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,value,previous", [("volume", 25, 50), ("mute", True, False)])
@pytest.mark.parametrize("operation", ["set_property", "get_property"])
@pytest.mark.parametrize("response", [{}, {"error": "synthetic rejected"}, {"error": None}])
async def test_gain_requires_explicit_success_for_write_and_read(name, value, previous, operation, response):
    backend, peer = make_backend()
    await backend.initialize_gain()
    peer.calls.clear()
    command = (operation, name, value) if operation == "set_property" else (operation, name)
    peer.responses[command] = response
    with pytest.raises(BackendError, match="Backend operation failed"):
        await getattr(backend, name)(value)
    assert getattr(backend, f"_desired_{name}") == previous
    assert not backend._gain_initialized
    assert len(peer.calls) == (1 if operation == "set_property" else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,value,response", [
    ("volume", 25, {"error": "success"}),
    *[("volume", 25, {"error": "success", "data": value})
      for value in (None, 26, "25", True, float("nan"), float("inf"))],
    ("mute", True, {"error": "success"}),
    *[("mute", True, {"error": "success", "data": value})
      for value in (None, False, 1, "true")],
])
async def test_gain_rejects_missing_wrong_or_malformed_readback(name, value, response):
    backend, peer = make_backend()
    await backend.initialize_gain()
    peer.responses[("get_property", name)] = response
    with pytest.raises(BackendError, match=f"{name} readback"):
        await getattr(backend, name)(value)
    assert backend._desired_volume == 50
    assert backend._desired_mute is False
    assert not backend._gain_initialized


@pytest.mark.asyncio
async def test_volume_accepts_real_mpv_float_readback():
    backend, peer = make_backend()
    peer.responses[("get_property", "volume")] = {"error": "success", "data": 25.0}
    await backend.volume(25)
    assert backend._desired_volume == 25


@pytest.mark.asyncio
@pytest.mark.parametrize("name,value", [
    ("volume", -1), ("volume", 101), ("volume", True), ("volume", 2.5),
    ("volume", float("nan")), ("mute", 1), ("mute", "false"),
])
async def test_invalid_gain_is_rejected_before_backend_write(name, value):
    backend, peer = make_backend()
    with pytest.raises(BackendError):
        await getattr(backend, name)(value)
    assert peer.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_command,response", [
    (("set_property", "volume", 50), {}),
    (("get_property", "volume"), {"error": "success", "data": 100}),
    (("set_property", "mute", False), {"error": "unavailable"}),
    (("get_property", "mute"), {"error": "success"}),
])
async def test_failed_initialization_never_loads_audio_and_can_recover(failed_command, response):
    backend, peer = make_backend()
    peer.responses[failed_command] = response
    with pytest.raises(BackendError):
        await backend.loadfile("https://media.example.test/one.wav")
    assert peer.loads == []
    assert not any(command[0] == "loadfile" for command in peer.calls)
    assert not backend._gain_initialized
    peer.responses.clear()
    await backend.loadfile("https://media.example.test/two.wav")
    assert peer.loads[0][1]["volume"] == 50
    assert peer.loads[0][1]["mute"] is False


@pytest.mark.asyncio
async def test_confirmed_controls_before_first_load_replace_initial_desired_gain():
    backend, peer = make_backend(initial_volume=50, initial_mute=False)
    await backend.mute(True)
    await backend.volume(17)
    assert backend._gain_initialized
    peer.calls.clear()
    await backend.loadfile("https://media.example.test/one.wav")
    await backend.volume(33)
    await backend.loadfile("https://media.example.test/two.wav")
    assert [(props["volume"], props["mute"]) for _, props in peer.loads] == [(17, True), (33, True)]
    assert backend._desired_volume == 33
    assert backend._desired_mute is True
    assert ["set_property", "volume", 50] not in peer.calls
    assert ["set_property", "mute", False] not in peer.calls


@pytest.mark.asyncio
async def test_concurrent_gain_mutations_serialize_complete_gain_pairs():
    backend, peer = make_backend()
    await asyncio.gather(backend.volume(11), backend.mute(True), backend.volume(22), backend.volume(33))
    assert peer.calls == [
        ["set_property", "volume", 11], ["get_property", "volume"],
        ["set_property", "mute", False], ["get_property", "mute"],
        ["set_property", "mute", True], ["get_property", "mute"],
        ["set_property", "volume", 11], ["get_property", "volume"],
        ["set_property", "mute", True], ["get_property", "mute"],
        ["set_property", "volume", 22], ["get_property", "volume"],
        ["set_property", "mute", True], ["get_property", "mute"],
        ["set_property", "volume", 33], ["get_property", "volume"],
    ]
    await backend.loadfile("https://media.example.test/one.wav")
    assert peer.loads[0][1]["volume"] == 33
    assert peer.loads[0][1]["mute"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["initialize", "volume"])
async def test_concurrent_initialization_and_volume_never_restore_stale_default(first):
    backend, peer = make_backend()
    entered, release = asyncio.Event(), asyncio.Event()

    async def command(args, timeout=5.0):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await peer.command(args, timeout)

    backend.command = command
    operations = [backend.initialize_gain(), backend.volume(27)]
    if first == "volume":
        operations.reverse()
    first_task = asyncio.create_task(operations[0])
    await entered.wait()
    second_task = asyncio.create_task(operations[1])
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first_task, second_task)
    assert peer.properties["volume"] == backend._desired_volume == 27
    assert backend._gain_initialized


@pytest.mark.asyncio
async def test_unconfirmed_gain_is_restored_before_next_load():
    backend, peer = make_backend()
    await backend.initialize_gain()
    peer.responses[("get_property", "volume")] = {"error": "success", "data": 99}
    with pytest.raises(BackendError):
        await backend.volume(100)
    assert peer.properties["volume"] == 100
    assert backend._desired_volume == 50
    peer.responses.clear()
    await backend.loadfile("https://media.example.test/one.wav")
    assert peer.loads[0][1]["volume"] == 50


@pytest.mark.asyncio
async def test_external_native_gain_reset_is_reconciled_before_each_load():
    backend, peer = make_backend(initial_volume=35, initial_mute=True)
    await backend.initialize_gain()
    await backend.volume(21)
    await backend.loadfile("https://media.example.test/one.wav")
    # Simulate the externally owned player being replaced without notifying
    # this object. The backend cache remains initialized, but mpv starts loud.
    peer.properties.update(volume=100, mute=False)
    assert backend._gain_initialized
    peer.calls.clear()
    await backend.loadfile("https://media.example.test/two.wav")
    assert peer.calls[:4] == [
        ["set_property", "mute", True], ["get_property", "mute"],
        ["set_property", "volume", 21], ["get_property", "volume"],
    ]
    assert [(props["volume"], props["mute"]) for _, props in peer.loads] == [(21, True), (21, True)]


@pytest.mark.asyncio
async def test_external_reset_with_unconfirmed_gain_blocks_new_load():
    backend, peer = make_backend(initial_mute=True)
    await backend.loadfile("https://media.example.test/one.wav")
    peer.properties.update(volume=100, mute=False)
    peer.responses[("get_property", "mute")] = {"error": "success", "data": False}
    with pytest.raises(BackendError, match="mute readback"):
        await backend.loadfile("https://media.example.test/two.wav")
    assert len(peer.loads) == 1
    assert not backend._gain_initialized


@pytest.mark.asyncio
@pytest.mark.parametrize("method,value,initial_mute,expected_volume,expected_mute", [
    ("volume", 20, True, 20, True),
    ("volume", 20, False, 20, False),
    ("mute", False, True, 25, False),
    ("mute", True, False, 25, True),
])
async def test_each_gain_control_restores_companion_property_after_external_reset(
    method, value, initial_mute, expected_volume, expected_mute,
):
    backend, peer = make_backend(initial_volume=25, initial_mute=initial_mute)
    await backend.initialize_gain()
    peer.properties.update(volume=100, mute=False)
    peer.calls.clear()
    await getattr(backend, method)(value)
    assert (peer.properties["volume"], peer.properties["mute"]) == (expected_volume, expected_mute)
    assert (backend._desired_volume, backend._desired_mute) == (expected_volume, expected_mute)
    assert backend._gain_initialized
    pairs = [("volume", expected_volume), ("mute", expected_mute)]
    if expected_mute:
        pairs.reverse()
    assert peer.calls == [command for name, setting in pairs for command in (
        ["set_property", name, setting], ["get_property", name],
    )]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,value,initial_mute,companion,setting", [
    ("volume", 20, True, "mute", True),
    ("volume", 20, False, "mute", False),
    ("mute", True, False, "volume", 25),
    ("mute", False, True, "volume", 25),
])
@pytest.mark.parametrize("operation,response", [
    ("set_property", {}), ("set_property", {"error": "synthetic rejection"}),
    ("get_property", {}), ("get_property", {"error": "synthetic rejection"}),
    ("get_property", {"error": "success", "data": None}),
])
async def test_unconfirmed_companion_does_not_commit_either_desired_value(
    method, value, initial_mute, companion, setting, operation, response,
):
    backend, peer = make_backend(initial_volume=25, initial_mute=initial_mute)
    await backend.initialize_gain()
    peer.properties.update(volume=100, mute=False)
    command = (operation, companion, setting) if operation == "set_property" else (operation, companion)
    peer.responses[command] = response
    with pytest.raises(BackendError):
        await getattr(backend, method)(value)
    assert (backend._desired_volume, backend._desired_mute) == (25, initial_mute)
    assert not backend._gain_initialized
    peer.responses.clear()
    await backend.initialize_gain()
    assert (peer.properties["volume"], peer.properties["mute"]) == (25, initial_mute)


@pytest.mark.asyncio
async def test_resume_restores_confirmed_mute_after_failed_unmute():
    backend, peer = make_backend(initial_volume=25, initial_mute=True)
    await backend.initialize_gain()
    await backend.pause()
    peer.responses[("get_property", "mute")] = {"error": "success", "data": True}
    with pytest.raises(BackendError, match="mute readback"):
        await backend.mute(False)
    assert peer.properties == {"volume": 25, "mute": False, "pause": True}
    assert backend._desired_mute is True
    assert not backend._gain_initialized
    peer.responses.clear()
    peer.calls.clear()
    await backend.resume()
    assert peer.calls == [
        ["set_property", "mute", True], ["get_property", "mute"],
        ["set_property", "volume", 25], ["get_property", "volume"],
        ["set_property", "pause", False],
    ]
    assert peer.properties == {"volume": 25, "mute": True, "pause": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("muted", [False, True])
async def test_resume_restores_complete_pair_after_external_reset(muted):
    backend, peer = make_backend(initial_volume=25, initial_mute=muted)
    await backend.initialize_gain()
    peer.properties.update(volume=100, mute=False, pause=True)
    await backend.resume()
    assert peer.properties == {"volume": 25, "mute": muted, "pause": False}
    assert backend._gain_initialized


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{}, {"error": "success", "data": False}])
async def test_resume_cannot_unpause_without_confirming_gain(response):
    backend, peer = make_backend(initial_volume=25, initial_mute=True)
    await backend.initialize_gain()
    peer.properties.update(volume=100, mute=False, pause=True)
    peer.calls.clear()
    peer.responses[("get_property", "mute")] = response
    with pytest.raises(BackendError):
        await backend.resume()
    assert ["set_property", "pause", False] not in peer.calls
    assert peer.properties["pause"] is True
    assert not backend._gain_initialized


@pytest.mark.asyncio
async def test_cancelled_resume_gain_confirmation_cannot_unpause():
    backend, peer = make_backend(initial_volume=25, initial_mute=True)
    await backend.initialize_gain()
    peer.properties.update(volume=100, mute=False, pause=True)
    peer.calls.clear()
    reading = asyncio.Event()

    async def command(args, timeout=5.0):
        if args == ["get_property", "volume"]:
            reading.set()
            await asyncio.Event().wait()
        return await peer.command(args, timeout)

    backend.command = command
    resuming = asyncio.create_task(backend.resume())
    await reading.wait()
    resuming.cancel()
    with pytest.raises(asyncio.CancelledError):
        await resuming
    assert ["set_property", "pause", False] not in peer.calls
    assert peer.properties["pause"] is True
    assert (backend._desired_volume, backend._desired_mute) == (25, True)
    assert not backend._gain_initialized
    assert not backend._gain_lock.locked()


@pytest.mark.asyncio
async def test_cancelled_gain_write_invalidates_initialization_and_releases_lock():
    backend, peer = make_backend()
    await backend.initialize_gain()
    reading = asyncio.Event()

    async def command(args, timeout=5.0):
        if args == ["get_property", "volume"]:
            reading.set()
            await asyncio.Event().wait()
        return await peer.command(args, timeout)

    backend.command = command
    updating = asyncio.create_task(backend.volume(100))
    await reading.wait()
    updating.cancel()
    with pytest.raises(asyncio.CancelledError):
        await updating
    assert not backend._gain_initialized
    assert backend._desired_volume == 50
    backend.command = peer.command
    await asyncio.wait_for(backend.loadfile("https://media.example.test/one.wav"), 1.0)
    assert peer.loads[0][1]["volume"] == 50


@pytest.mark.asyncio
async def test_gain_write_and_readback_share_one_deadline(monkeypatch):
    backend, peer = make_backend()
    await backend.initialize_gain()
    original_timeout = asyncio.timeout
    budgets = []

    def short_timeout(delay):
        budgets.append(delay)
        return original_timeout(0.025)

    async def command(args, timeout=5.0):
        if args == ["get_property", "volume"]:
            await asyncio.Event().wait()
        return await peer.command(args, timeout)

    backend.command = command
    monkeypatch.setattr("fake_linkplay.mpv_backend.asyncio.timeout", short_timeout)
    begin = asyncio.get_running_loop().time()
    with pytest.raises(BackendError, match="volume confirmation timed out"):
        await backend.volume(100)
    assert asyncio.get_running_loop().time() - begin < 0.3
    assert budgets == [2.0]
    assert backend._desired_volume == 50
    assert not backend._gain_initialized
    assert not backend._gain_lock.locked()
