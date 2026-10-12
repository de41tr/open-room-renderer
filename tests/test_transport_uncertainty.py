"""UNIT/MOCK owner-intent fences after unconfirmed transport commands."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi, ParsedCommand
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.state import PlaybackPhase, RendererState
from test_playback_generations import StatefulBackend, assert_consistent
from test_queue_playback import FIRST, SECOND, create, play


@pytest.fixture
async def api(tmp_path):
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path))
    result = HttpApi(cfg, RendererState.from_config(cfg), StatefulBackend())
    result._wait_for_mpv_audio_ready = AsyncMock(return_value=True)
    await create(result)
    await play(result, 1)
    return result


def loads(api):
    return [url for name, url in api.mpv.calls if name == "load"]


def assert_uncertain(api, command):
    assert_consistent(api, PlaybackPhase.ERROR, "stop", "STOPPED")
    assert api.state.intent == {"stop": "stop", "pause": "pause", "resume": "play"}[command]
    assert "confirmation failed" in api.state.last_error
    if command == "stop":
        assert "silence not confirmed" in api.state.last_error
    else:
        assert "state unknown" in api.state.last_error
    assert api.state.backend_url == FIRST
    assert api.state.queue.index == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["stop", "pause", "resume"])
@pytest.mark.parametrize("applied", [False, True])
@pytest.mark.parametrize("failure", [BackendError, OSError, TimeoutError])
async def test_failed_transport_preserves_owner_fence_until_explicit_play(api, command, applied, failure):
    if command == "resume":
        await api._transport_command("pause")
    original = getattr(api.mpv, command)

    async def unconfirmed():
        if applied:
            await original()
        raise failure("Synthetic lost acknowledgement or pre-mutation refusal")

    setattr(api.mpv, command, unconfirmed)
    with pytest.raises(failure):
        await api._transport_command(command)
    assert_uncertain(api, command)
    # Stop can already have applied; a rejected Pause can also be followed by
    # ordinary EOF. Neither observation is permission to restart a queue.
    api.mpv.idle, api.mpv.url = True, ""
    for _ in range(3):
        await api.sync_mpv_state()
    assert_uncertain(api, command)
    assert api._queue_advance_task is None and loads(api) == [FIRST]
    setattr(api.mpv, command, original)
    await api.execute(ParsedCommand("resume"), "127.0.0.1")
    assert_consistent(api, PlaybackPhase.PLAYING, "play", "PLAYING")
    assert api.mpv.url == FIRST and loads(api) == [FIRST, FIRST]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["stop", "pause", "resume"])
async def test_acknowledged_control_with_failed_readback_is_explicitly_uncertain(api, command):
    if command == "resume":
        await api._transport_command("pause")
    original = api.mpv.snapshot
    api.mpv.snapshot = AsyncMock(return_value={"backend-error": True})
    with pytest.raises(BackendError, match="unavailable after command"):
        await api._transport_command(command)
    assert_uncertain(api, command)
    api.mpv.snapshot = original
    await api.sync_mpv_state()
    assert_uncertain(api, command)
    assert loads(api) == [FIRST]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["stop", "pause", "resume"])
@pytest.mark.parametrize("applied", [False, True])
async def test_cancelled_control_does_not_stick_accepted_or_restore_play(api, command, applied):
    if command == "resume":
        await api._transport_command("pause")
    entered = asyncio.Event()
    original = getattr(api.mpv, command)

    async def waiting():
        if applied:
            await original()
        entered.set()
        await asyncio.Future()

    setattr(api.mpv, command, waiting)
    task = asyncio.create_task(api._transport_command(command))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_uncertain(api, command)
    api.mpv.idle, api.mpv.url = True, ""
    await api.sync_mpv_state()
    assert api._queue_advance_task is None and loads(api) == [FIRST]
    setattr(api.mpv, command, original)
    await api.execute(ParsedCommand("resume"), "127.0.0.1")
    assert api.mpv.url == FIRST and api.state.phase == PlaybackPhase.PLAYING


@pytest.mark.asyncio
async def test_cancelled_stop_while_old_load_owns_lock_keeps_output_uncertain(api):
    api.mpv.release_load = asyncio.Event()
    api.mpv.load_started.clear()
    loading = asyncio.create_task(api._play_url(SECOND))
    await api.mpv.load_started.wait()
    stopping = asyncio.create_task(api._transport_command("stop"))
    await asyncio.sleep(0)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert api.state.phase == PlaybackPhase.ERROR and api.state.intent == "stop"
    api.mpv.release_load.set()
    with pytest.raises(BackendError, match="superseded"):
        await loading
    assert api.mpv.url == SECOND
    assert api.state.phase == PlaybackPhase.ERROR and api.state.intent == "stop"
    assert "silence not confirmed" in api.state.last_error
    await api.sync_mpv_state()
    assert api.state.phase == PlaybackPhase.ERROR
    await api.execute(ParsedCommand("resume"), "127.0.0.1")
    assert api.state.phase == PlaybackPhase.PLAYING and api.mpv.url == SECOND


@pytest.mark.asyncio
async def test_old_snapshot_cannot_replace_cancelled_stop_uncertainty(api):
    entered, release = asyncio.Event(), asyncio.Event()
    original = api.mpv.snapshot

    async def snapshot():
        result = await original()
        entered.set()
        await release.wait()
        return result

    api.mpv.snapshot = snapshot
    observing = asyncio.create_task(api.sync_mpv_state())
    await entered.wait()
    stopping = asyncio.create_task(api._transport_command("stop"))
    await asyncio.sleep(0)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    release.set()
    await observing
    assert_uncertain(api, "stop")
    assert not any(name == "stop" for name, _ in api.mpv.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_old_failed_stop_cannot_poison_newer_play(api, cancel):
    entered, release = asyncio.Event(), asyncio.Event()
    original = api.mpv.stop

    async def unconfirmed():
        await original()
        entered.set()
        await release.wait()
        raise BackendError("Synthetic lost Stop acknowledgement")

    api.mpv.stop = unconfirmed
    stopping = asyncio.create_task(api._transport_command("stop"))
    await entered.wait()
    newer = asyncio.create_task(api._play_url(SECOND))
    await asyncio.sleep(0)
    if cancel:
        stopping.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError if cancel else BackendError):
        await stopping
    await newer
    assert api.state.phase == PlaybackPhase.PLAYING and api.state.intent == "play"
    assert api.state.current_url == api.mpv.url == SECOND
    assert api.state.last_error == ""
