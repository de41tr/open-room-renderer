"""UNIT/MOCK: per-load history policy belongs only to the canonical live item."""
from unittest.mock import AsyncMock

import pytest

from fake_linkplay.mpv_backend import LIVE_STATION_BACK_BYTES
from fake_linkplay.queue import PlaybackQueue, QueueItem
from test_station_recovery import api, eof, finish
from test_station_url_refresh import FIRST, FRESH, select


SOURCE = "https://radio.example.test/live"
DIRECT = "https://media.example.test/resolved"
NEXT = "https://media.example.test/next"
HISTORY = {"demuxer-max-back-bytes": str(LIVE_STATION_BACK_BYTES), "demuxer-donate-buffer": "no"}


def test_live_history_limit_is_positive_and_bounded():
    assert LIVE_STATION_BACK_BYTES == 1024 * 1024


@pytest.mark.parametrize("stream_type,duration,expected", [
    ("station", 0, HISTORY), ("station", 30, {}),
    ("track", 0, {}), ("track", 30, {}), ("", 0, {}),
])
def test_only_explicit_zero_duration_station_changes_native_options(api, stream_type, duration, expected):
    api.state.queue = PlaybackQueue(items=[QueueItem(SOURCE, stream_type=stream_type, duration=duration)], index=1)
    api.state.current_url = SOURCE
    # Deliberately stale mirrors cannot broaden or suppress canonical policy.
    api.queue_is_live = stream_type != "station"
    api.queue_stream_type = "station" if stream_type != "station" else "track"
    assert api._mpv_playback_options(SOURCE, DIRECT) == expected


@pytest.mark.parametrize("original,current", [(NEXT, SOURCE), (SOURCE, NEXT), (DIRECT, SOURCE)])
def test_current_station_cannot_lend_policy_to_staged_or_stale_load(api, original, current):
    api.state.queue = PlaybackQueue(items=[
        QueueItem(SOURCE, stream_type="station", resolved_url=DIRECT),
        QueueItem(NEXT, duration=30),
    ], index=1)
    api.state.current_url = current
    api.queue_is_live = True
    assert api._mpv_playback_options(original, DIRECT) == {}


def test_empty_selection_cannot_borrow_live_mirrors(api):
    api.state.current_url = SOURCE
    api.queue_is_live = True
    api.queue_stream_type = "station"
    assert api._mpv_playback_options(SOURCE, DIRECT) == {}


@pytest.mark.parametrize("mode", ["header", "queue", "http", "soap"])
async def test_resolver_types_station_before_initial_and_refreshed_load_options(api, mode):
    api.mpv.loadfile = AsyncMock(wraps=api.mpv.loadfile)
    await select(api, mode)
    canonical = api.state.current_url
    await finish(await eof(api))
    assert [(call.args[0], call.args[1]) for call in api.mpv.loadfile.await_args_list] == [
        (FIRST, HISTORY), (FRESH, HISTORY),
    ]
    assert api.state.current_url == api.state.queue.current.url == canonical
    assert api.state.backend_url == FRESH
    await api._play_url(NEXT)
    assert api.mpv.loadfile.await_args.args == (NEXT, {})


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("counting", [False, True])
async def test_native_station_helpers_count_both_command_shapes_once(monkeypatch, named, counting):
    from libmpv_stability_support import CountingBackend
    from test_libmpv_load_options import _NamedLoadBackend
    from test_libmpv_recovery import _StationBackend
    transport = AsyncMock(return_value={"error": "success"})
    monkeypatch.setattr(_NamedLoadBackend, "command", transport)
    backend = (CountingBackend if counting else _StationBackend).__new__(CountingBackend if counting else _StationBackend)
    backend._allowed_media = {SOURCE}
    backend.loads = 0 if counting else []
    backend.source_changes, backend.last_source = 0, ""
    command = ({"name": "loadfile", "url": SOURCE, "flags": "replace", "options": HISTORY}
               if named else ["loadfile", SOURCE, "replace"])
    assert await backend.command(command) == {"error": "success"}
    assert backend.loads == (1 if counting else [SOURCE])
    if counting:
        assert backend.source_changes == 1 and backend.last_source == SOURCE
    transport.assert_awaited_once_with(command, 5.0)
    foreign = {**command, "url": NEXT} if named else ["loadfile", NEXT, "replace"]
    with pytest.raises(AssertionError):
        await backend.command(foreign)
    assert transport.await_count == 1
