"""One M3U import/restore round trip: real HTTP with a mock audio backend."""
from __future__ import annotations

import asyncio

import pytest
import requests

from fake_linkplay import cli, playlist_cli, playlist_files
from fake_linkplay.queue import parse_queue_context
from fake_linkplay.queue_client import explicit_queue
from fake_linkplay.state import PlaybackPhase
from test_playlist_m3u import URLS
from test_queue_client_renderer import renderer, request_fields


@pytest.mark.asyncio
async def test_import_is_offline_and_separate_restore_replaces_queue_stopped(renderer, tmp_path, monkeypatch, capsys):
    old_url = "https://media.example.test/old.wav"
    await asyncio.to_thread(renderer.client.replace, explicit_queue("Old queue", [old_url]), play=True)
    old_queue = renderer.api.state.queue
    calls = list(renderer.api.mpv.calls)
    renderer.requests.clear()
    renderer.api._resolve_playback_url.reset_mock()
    urls = [*URLS, URLS[0]]
    source = tmp_path / "synthetic.m3u8"
    source.write_bytes(("\ufeff#EXTM3U\r\n#EXTINF:12.5,Discarded title\r\n" + "\r\n".join(urls)).encode())
    original = source.read_bytes()
    target = tmp_path / "authored.json"
    name = "Imported café 夜"

    def forbidden(*args, **kwargs):
        pytest.fail("import must not instantiate a client or HTTP session")
    with monkeypatch.context() as offline:
        offline.setattr(playlist_cli, "QueueClient", forbidden)
        offline.setattr(requests, "Session", forbidden)
        assert cli.main(["playlist", "import-m3u", str(source), str(target), "--name", name]) == 0
    captured = capsys.readouterr()
    assert captured.out == "M3U URL list imported. Titles and durations were not saved.\n" and captured.err == ""
    assert renderer.requests == [] and renderer.api.state.queue is old_queue
    assert renderer.api.mpv.calls == calls and renderer.api.state.phase == PlaybackPhase.PLAYING
    assert source.read_bytes() == original
    saved = target.read_bytes()
    assert playlist_files.load_playlist(target) == {
        "version": 1, "name": name, "index": 1, "mode": "normal", "urls": urls}

    result = await asyncio.to_thread(cli.main, [
        "playlist", "restore", str(target), "--host", "127.0.0.1", "--port", str(renderer.server.port)])
    assert result == 0
    captured = capsys.readouterr()
    assert captured.out == "Playlist restored; playback was not requested.\n" and captured.err == ""
    assert [action for action, _ in renderer.requests] == ["ReplaceQueue"]
    sent = parse_queue_context(request_fields(renderer.requests[0][1])["QueueContext"])
    for queue in (sent, renderer.api.state.queue):
        assert (queue.name, queue.index, queue.loop) == (name, 1, 4)
        assert [item.url.encode() for item in queue.items] == [url.encode() for url in urls]
        assert all(not item.resolved_url and item._radio_station is None and item.duration == 0
                   for item in queue.items)
    assert all(not item.title for item in sent.items)
    # Existing stopped selection supplies its normal URL-derived display fallback.
    assert renderer.api.state.queue.current.title == urls[0].rsplit("/", 1)[-1]
    assert renderer.api.state.queue.current.title_is_fallback
    assert renderer.api.state.queue is not old_queue
    assert renderer.api.state.phase == PlaybackPhase.STOPPED and renderer.api.mpv.idle
    assert renderer.api.mpv.calls == [*calls, ("stop", None)]
    renderer.api._resolve_playback_url.assert_not_awaited()
    assert target.read_bytes() == saved and source.read_bytes() == original
