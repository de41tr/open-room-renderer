"""Real CLI/HttpApi loopback controls with deterministic mock audio only."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import os
import sys
from unittest.mock import AsyncMock

import pytest

from fake_linkplay.state import PlaybackPhase
from test_queue_client_renderer import (
    ROOT, CliResult, MODES, NAME, URLS, renderer, request_fields, rich_queue,
)
from test_shuffle_policy import ReverseRandom


async def control(renderer, *arguments):
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "fake_linkplay.cli", *arguments,
        "--host", "127.0.0.1", "--port", str(renderer.server.port),
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    return CliResult(process.returncode, stdout.decode(), stderr.decode())


async def test_mode_changes_preserve_current_queue_playback_and_metadata(renderer):
    await asyncio.to_thread(renderer.client.replace, rich_queue(), play=True)
    queue = renderer.api.state.queue
    original = asdict(queue)
    calls = list(renderer.api.mpv.calls)
    generation = renderer.api.state.generation
    renderer.api.mpv.position = 7
    for mode, wire_mode in MODES:
        renderer.requests.clear()
        result = await renderer.cli("mode", mode)
        assert result.success() == {"mode_request_acknowledged": True, "mode": mode}
        assert [action for action, _ in renderer.requests] == ["SetQueueLoopMode"]
        assert request_fields(renderer.requests[0][1]) == {"LoopMode": str(wire_mode)}
        assert renderer.api.state.queue is queue
        assert {key: value for key, value in asdict(queue).items() if key not in {"loop", "_shuffle"}} == {
            key: value for key, value in original.items() if key not in {"loop", "_shuffle"}}
        assert queue.loop == wire_mode and queue.index == 2 and queue.name == NAME
        assert renderer.api.state.phase == PlaybackPhase.PLAYING
        assert renderer.api.state.generation == generation
        assert renderer.api.mpv.url == URLS[1] and renderer.api.mpv.position == 7
        assert renderer.api.mpv.calls == calls


async def test_http_shuffle_history_keeps_stopped_and_paused_intent(renderer):
    renderer.api._shuffle_rng = ReverseRandom()
    await asyncio.to_thread(renderer.client.replace, rich_queue(index=1, loop=3))
    queue = renderer.api.state.queue
    assert queue._shuffle.path == (1, 3, 2)
    for command, index in (("next", 3), ("previous", 1)):
        assert (await control(renderer, command)).success() == {
            "request_acknowledged": True, "command": command}
        assert queue.index == index and renderer.api.state.phase == PlaybackPhase.STOPPED
        assert renderer.api.mpv.idle and not renderer.api.mpv.url
        assert renderer.api.mpv.calls == []
    await asyncio.to_thread(renderer.client.play_index, NAME, 1)
    for command, index in (("next", 3), ("previous", 1)):
        (await control(renderer, command)).success()
        assert queue.index == index and renderer.api.state.phase == PlaybackPhase.PLAYING
        assert renderer.api.mpv.url == URLS[index - 1]
    await renderer.api._transport_command("pause")
    before, calls = asdict(renderer.api.state), list(renderer.api.mpv.calls)
    for command in ("next", "previous"):
        result = await control(renderer, command)
        assert result.returncode == 1 and not result.stdout
        assert "Inspect status" in result.stderr and "Traceback" not in result.stderr
        assert asdict(renderer.api.state) == before
        assert renderer.api.mpv.calls == calls and renderer.api.mpv.paused


async def test_seek_and_explicit_mute_do_not_resume_paused_or_stopped_playback(renderer):
    renderer.api.mpv.mute = AsyncMock()
    await asyncio.to_thread(renderer.client.replace, rich_queue(index=1), play=True)
    assert (await control(renderer, "seek", "4")).success() == {
        "request_acknowledged": True, "command": "seek", "seconds": 4}
    assert renderer.api.mpv.position == renderer.api.state.curpos == 4
    await renderer.api._transport_command("pause")
    assert (await control(renderer, "seek", "7")).success()["seconds"] == 7
    assert renderer.api.mpv.position == renderer.api.state.curpos == 7
    assert renderer.api.state.phase == PlaybackPhase.PAUSED and renderer.api.mpv.paused
    calls = list(renderer.api.mpv.calls)
    for value in ("on", "off"):
        assert (await control(renderer, "mute", value)).success() == {
            "request_acknowledged": True, "command": "mute", "value": value}
        renderer.api.mpv.mute.assert_awaited_with(value == "on")
        assert renderer.api.state.mute is (value == "on")
        assert renderer.api.state.phase == PlaybackPhase.PAUSED and renderer.api.mpv.paused
        assert renderer.api.mpv.calls == calls
    await renderer.api._transport_command("stop")
    calls = list(renderer.api.mpv.calls)
    result = await control(renderer, "seek", "12")
    assert result.returncode == 1 and not result.stdout and "Inspect status" in result.stderr
    assert renderer.api.state.phase == PlaybackPhase.STOPPED and renderer.api.mpv.idle
    assert renderer.api.mpv.calls == calls
