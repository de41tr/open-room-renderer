"""LOCAL HTTP + CLI subprocess coverage with a deterministic, mock audio backend.

The real aiohttp HttpApi and SOAP parser run on an ephemeral loopback port.
No native mpv process, external media fetch, or acoustic result is involved.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import random
import sys
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET

from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.queue import PlaybackQueue, QueueItem, parse_queue_context
from fake_linkplay.queue_client import QueueClient, explicit_queue
from fake_linkplay.state import PlaybackPhase, RendererState
from test_playback_generations import StatefulBackend


ROOT = Path(__file__).resolve().parents[1]
URLS = tuple(f"https://media.example.test/{name}.wav" for name in ("one", "two", "three"))
# Explicit wire expectations, independent of the client's mapping under test.
MODES = (("normal", 4), ("repeat-one", 1), ("repeat-all", 0),
         ("shuffle-once", 3), ("shuffle-repeat-all", 2))
NAME = "Local & \u03b1 queue"


@dataclass
class CliResult:
    returncode: int
    stdout: str
    stderr: str

    def success(self):
        assert self.returncode == 0, self.stderr
        assert self.stderr == ""
        return json.loads(self.stdout)


@dataclass
class LoopbackRenderer:
    api: HttpApi
    server: TestServer
    requests: list[tuple[str, str]]

    @property
    def client(self):
        return QueueClient("127.0.0.1", self.server.port)

    async def cli(self, command, *arguments):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "fake_linkplay.cli", "queue", command,
            "--host", "127.0.0.1", "--port", str(self.server.port), *arguments,
            cwd=ROOT, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise
        return CliResult(process.returncode, stdout.decode(), stderr.decode())


@pytest.fixture
async def renderer(tmp_path):
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path),
                 advertise_ip="127.0.0.1", enable_ssdp=False)
    api = HttpApi(cfg, RendererState.from_config(cfg), StatefulBackend())
    # Resolution and audio readiness are deterministic; HTTP/SOAP dispatch and
    # actual client network I/O remain unmocked. No URL leaves this process.
    api._resolve_playback_url = AsyncMock(side_effect=lambda url: url)
    api._wait_for_mpv_audio_ready = AsyncMock(return_value=True)
    api._shuffle_rng = random.Random(0)
    requests = []

    @web.middleware
    async def record_request(request, handler):
        requests.append((request.headers.get("SOAPAction", "").strip('"').rsplit("#", 1)[-1],
                         await request.text()))
        return await handler(request)

    app = api.app()
    app.middlewares.append(record_request)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        yield LoopbackRenderer(api, server, requests)
    finally:
        await server.close()
    assert api._state_monitor_task is None


def request_fields(body):
    action = ET.fromstring(body)[0][0]
    return {field.tag: field.text or "" for field in action}


def rich_queue(*, index=2, loop=4):
    return PlaybackQueue(
        name=NAME, list_name="private-list-label", source="private-provider-source",
        search_url="https://search.example.test/?token=private-search-token",
        artwork="https://art.example.test/private-cover.jpg", quality="private-quality",
        stream_id="private-stream-id", show_name="private-show-name",
        stream_type="private-queue-type", content_type="private-queue-content",
        index=index, loop=loop,
        items=[QueueItem(
            url=url, title=f"private-title-{position}", artist=f"private-artist-{position}",
            album=f"private-album-{position}", id=f"private-provider-id-{position}",
            source=f"private-track-source-{position}",
            artwork=f"https://art.example.test/private-track-{position}.jpg",
            metadata=f"<private-metadata>private-didl-{position}</private-metadata>",
            stream_type="track", content_type="audio", duration=30,
        ) for position, url in enumerate(URLS, 1)],
    )


@pytest.mark.asyncio
async def test_default_cli_replacement_stops_active_queue_without_loading_new_media(renderer):
    old_url = "https://media.example.test/old.wav"
    await asyncio.to_thread(renderer.client.replace, explicit_queue("Old queue", [old_url]), play=True)
    assert renderer.api.mpv.calls == [("load", old_url), ("resume", None)]
    old_queue = renderer.api.state.queue
    renderer.requests.clear()
    renderer.api._resolve_playback_url.reset_mock()

    result = await renderer.cli("load", "--name", NAME, "--start-index", "2", *URLS)
    assert result.success() == {
        "queue_replaced": True, "name": NAME, "index": 2, "count": 3,
        "mode": "normal", "playback_request_acknowledged": False,
    }
    assert [action for action, _ in renderer.requests] == ["ReplaceQueue"]
    assert renderer.api.state.queue is not old_queue
    assert renderer.api.state.queue.name == NAME
    assert renderer.api.state.queue.index == renderer.api.state.plicurr == 2
    assert renderer.api.state.current_url == URLS[1]
    assert [item.url for item in renderer.api.state.queue.items] == list(URLS)
    assert renderer.api.state.phase == PlaybackPhase.STOPPED
    assert renderer.api.mpv.idle and renderer.api.mpv.url == ""
    assert renderer.api.mpv.calls == [("load", old_url), ("resume", None), ("stop", None)]
    renderer.api._resolve_playback_url.assert_not_awaited()

    browsed = await asyncio.to_thread(renderer.client.browse, NAME)
    assert browsed.index == 2 and browsed.count == 3
    assert [item.url for item in browsed.items] == list(URLS)
    assert renderer.api.mpv.calls == [("load", old_url), ("resume", None), ("stop", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "wire_mode"), MODES)
async def test_cli_load_play_preserves_mode_and_plays_requested_canonical_index(renderer, mode, wire_mode):
    result = await renderer.cli("load", "--name", NAME, "--start-index", "2",
                                "--mode", mode, "--play", *URLS)
    assert result.success() == {
        "queue_replaced": True, "name": NAME, "index": 2, "count": 3,
        "mode": mode, "playback_request_acknowledged": True,
    }
    assert [action for action, _ in renderer.requests] == ["ReplaceQueue", "PlayQueueWithIndex"]
    fields = request_fields(renderer.requests[0][1])
    sent_queue = parse_queue_context(fields["QueueContext"])
    assert set(fields) == {"QueueContext"}
    assert sent_queue.name == NAME
    assert sent_queue.loop == wire_mode and sent_queue.index == 2
    assert [item.url for item in sent_queue.items] == list(URLS)
    assert request_fields(renderer.requests[1][1]) == {"QueueName": NAME, "Index": "2"}
    assert renderer.api.state.queue.loop == wire_mode
    assert renderer.api.state.queue.index == renderer.api.state.plicurr == 2
    assert renderer.api.state.phase == PlaybackPhase.PLAYING
    assert renderer.api.mpv.calls == [("load", URLS[1]), ("resume", None)]
    assert renderer.api.state.current_url == renderer.api.mpv.url == URLS[1]

    summary = (await renderer.cli("show", "--name", NAME)).success()
    assert summary == {"name": NAME, "count": 3, "index": 2, "mode": mode,
                       "items": [{"index": 1}, {"index": 2}, {"index": 3}]}


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "wire_mode"), MODES)
async def test_cli_play_index_never_rewrites_queue_or_uses_shuffle_position(renderer, mode, wire_mode):
    await asyncio.to_thread(renderer.client.replace, rich_queue(index=3, loop=wire_mode), play=True)
    queue = renderer.api.state.queue
    items = list(queue.items)
    item_metadata = [(item.url, item.title, item.artist, item.album, item.metadata, item.id)
                     for item in items]
    renderer.requests.clear()
    renderer.api.mpv.calls.clear()

    result = await renderer.cli("play-index", "--name", NAME, "1")
    assert result.success() == {"playback_request_acknowledged": True, "name": NAME, "index": 1}
    assert [action for action, _ in renderer.requests] == ["PlayQueueWithIndex"]
    assert request_fields(renderer.requests[0][1]) == {"QueueName": NAME, "Index": "1"}
    assert renderer.api.state.queue is queue
    assert all(actual is original for actual, original in zip(queue.items, items))
    assert [(item.url, item.title, item.artist, item.album, item.metadata, item.id)
            for item in queue.items] == item_metadata
    assert queue.loop == wire_mode and queue.index == renderer.api.state.plicurr == 1
    assert [item.url for item in queue.items] == list(URLS)
    assert renderer.api.state.phase == PlaybackPhase.PLAYING
    assert renderer.api.state.current_url == renderer.api.mpv.url == URLS[0]
    assert renderer.api.mpv.calls == [("stop", None), ("load", URLS[0]), ("resume", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize(("command", "arguments", "expected_actions"), [
    ("play-index", ("--name", "Wrong queue", "1"), ["PlayQueueWithIndex"]),
    ("play-index", ("--name", NAME, "4"), ["PlayQueueWithIndex"]),
    ("play-index", ("--name", NAME, "0"), []),
    ("play-index", ("--name", NAME, "-1"), []),
    ("play-index", ("--name", NAME, "1001"), []),
    ("play-index", ("--name", NAME, "1.5"), []),
    ("show", ("--name", "Wrong queue"), ["BrowseQueue"]),
    ("load", ("--name", "Replacement", "--start-index", "0", "--play", *URLS), []),
    ("load", ("--name", "Replacement", "--start-index", "4", "--play", *URLS), []),
])
async def test_cli_rejected_requests_leave_active_queue_and_backend_unchanged(
        renderer, command, arguments, expected_actions):
    await asyncio.to_thread(renderer.client.replace, rich_queue(), play=True)
    queue = renderer.api.state.queue
    state_before = asdict(renderer.api.state)
    calls_before = list(renderer.api.mpv.calls)
    renderer.requests.clear()

    result = await renderer.cli(command, *arguments)
    assert result.returncode != 0
    assert result.stdout == "" and result.stderr
    assert "Traceback" not in result.stderr
    assert [action for action, _ in renderer.requests] == expected_actions
    assert renderer.api.state.queue is queue
    assert asdict(renderer.api.state) == state_before
    assert renderer.api.mpv.calls == calls_before
    assert renderer.api.mpv.url == URLS[1] and not renderer.api.mpv.idle


@pytest.mark.asyncio
async def test_browse_can_report_resolved_urls_but_cli_hides_urls_and_all_metadata(renderer):
    resolved = "https://resolved.example.test/two.wav?token=private-resolved-token"
    renderer.api._resolve_playback_url.side_effect = lambda url: resolved if url == URLS[1] else url
    await asyncio.to_thread(renderer.client.replace, rich_queue(), play=True)
    canonical = renderer.api.state.queue
    assert canonical.current.url == URLS[1]
    assert canonical.current.resolved_url == renderer.api.mpv.url == resolved
    calls_before = list(renderer.api.mpv.calls)
    renderer.requests.clear()

    browsed = await asyncio.to_thread(renderer.client.browse, NAME)
    assert browsed.index == 2 and browsed.loop == 4
    assert [item.url for item in browsed.items] == [URLS[0], resolved, URLS[2]]
    assert browsed.current.title == "private-title-2"
    assert browsed.current.metadata == "<private-metadata>private-didl-2</private-metadata>"
    assert browsed.search_url == "https://search.example.test/?token=private-search-token"

    hidden = await renderer.cli("show", "--name", NAME)
    assert hidden.success() == {
        "name": NAME, "count": 3, "index": 2, "mode": "normal",
        "items": [{"index": 1}, {"index": 2}, {"index": 3}],
    }
    assert "private-" not in hidden.stdout
    assert "https://" not in hidden.stdout

    disclosed = await renderer.cli("show", "--name", NAME, "--include-urls")
    summary = disclosed.success()
    assert [item["renderer_reported_url"] for item in summary["items"]] == [URLS[0], resolved, URLS[2]]
    assert "not a canonical" in summary["url_notice"]
    assert "credential-bearing" in summary["url_notice"]
    assert all(set(item) == {"index", "renderer_reported_url"} for item in summary["items"])
    assert "private-title" not in disclosed.stdout and "private-didl" not in disclosed.stdout
    assert "private-provider" not in disclosed.stdout and "private-search-token" not in disclosed.stdout
    assert [action for action, _ in renderer.requests] == ["BrowseQueue", "BrowseQueue", "BrowseQueue"]
    assert renderer.api.state.queue is canonical
    assert canonical.current.url == URLS[1]
    assert renderer.api.mpv.calls == calls_before
