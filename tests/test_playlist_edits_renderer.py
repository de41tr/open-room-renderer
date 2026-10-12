"""Offline edits consumed by the existing stopped restore, LOCAL HTTP + MOCK."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
import socket

import aiohttp
import pytest
import requests

from fake_linkplay import cli, playlist_cli, playlist_files
from fake_linkplay.queue import parse_queue_context
from fake_linkplay.queue_client import explicit_queue
from fake_linkplay.state import PlaybackPhase
from test_playlist_edits import URLS, payload
from test_playlist_edits_cli import SUCCESS
from test_queue_client_renderer import renderer, request_fields


@pytest.mark.asyncio
async def test_edit_chain_leaves_active_renderer_untouched_then_restores_stopped(renderer, tmp_path, monkeypatch, capsys):
    old_url = "https://media.example.test/old.wav"
    await asyncio.to_thread(renderer.client.replace, explicit_queue("Old queue", [old_url]), play=True)
    old_queue = renderer.api.state.queue
    state = deepcopy(asdict(renderer.api.state))
    calls = list(renderer.api.mpv.calls)
    renderer.requests.clear()
    renderer.api._resolve_playback_url.reset_mock()
    source = tmp_path / "source.json"
    playlist_files.save_playlist(source, payload(mode="shuffle-once"))
    originals = {source: source.read_bytes()}
    appended, removed, moved = (tmp_path / name for name in ("appended.json", "removed.json", "moved.json"))

    def forbidden(*args, **kwargs):
        pytest.fail("editing must not construct a client/session/renderer/backend or use the network")
    with monkeypatch.context() as offline:
        for owner, attribute in ((playlist_cli, "QueueClient"), (playlist_cli, "fetch_current_playlist"),
                                 (requests, "Session"), (aiohttp, "ClientSession"),
                                 (socket, "socket"), (socket, "getaddrinfo"),
                                 (cli, "HttpApi"), (cli, "MpvBackend")):
            offline.setattr(owner, attribute, forbidden)
        for command, input_file, target, options in (
            ("append", source, appended, [URLS[1]]),
            ("remove", appended, removed, ["--index", "1"]),
            ("move", removed, moved, ["--from-index", "2", "--to-index", "3"]),
        ):
            assert cli.main(["playlist", command, str(input_file), str(target), *options]) == 0
            originals[target] = target.read_bytes()
            captured = capsys.readouterr()
            assert captured.out == SUCCESS and captured.err == ""
            assert renderer.requests == [] and renderer.api.state.queue is old_queue
            assert asdict(renderer.api.state) == state
            assert renderer.api.mpv.calls == calls and renderer.api.state.phase == PlaybackPhase.PLAYING
            renderer.api._resolve_playback_url.assert_not_awaited()
    urls = [URLS[1], URLS[1], URLS[0]]
    assert playlist_files.load_playlist(moved) == payload(urls=urls, index=3, mode="shuffle-once")

    assert await asyncio.to_thread(cli.main, [
        "playlist", "restore", str(moved), "--host", "127.0.0.1", "--port", str(renderer.server.port)]) == 0
    captured = capsys.readouterr()
    assert captured.out == "Playlist restored; playback was not requested.\n" and captured.err == ""
    assert [action for action, _ in renderer.requests] == ["ReplaceQueue"]
    sent = parse_queue_context(request_fields(renderer.requests[0][1])["QueueContext"])
    for queue in (sent, renderer.api.state.queue):
        assert (queue.name, queue.index, queue.loop) == (payload()["name"], 3, 3)
        assert [item.url.encode() for item in queue.items] == [url.encode() for url in urls]
        assert all(not item.resolved_url and item._radio_station is None and item.duration == 0
                   for item in queue.items)
    assert all(not item.title for item in sent.items)
    assert renderer.api.state.queue is not old_queue
    assert renderer.api.state.phase == PlaybackPhase.STOPPED and renderer.api.mpv.idle
    assert renderer.api.mpv.calls == [*calls, ("stop", None)]
    renderer.api._resolve_playback_url.assert_not_awaited()
    assert all(path.read_bytes() == encoded for path, encoded in originals.items())
