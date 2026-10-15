"""Optional REAL_BACKEND/null nested-playlist readiness failure and cleanup.

Opt in with ORR_TEST_LIBMPV_PATH. This reuses generated public-format fixtures,
loopback-only URLs, native libmpv decoding and null audio output. Only readiness
has a shorter injected deadline; source checks and cleanup are production code.
A disposable interpreter bounds native calls and verifies all fixture teardown.
"""

from __future__ import annotations

import asyncio
import json
import locale
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from fake_linkplay.http_api import HttpApi
from fake_linkplay.state import PlaybackPhase
from test_libmpv_formats import _command, _generate_media, _renderer


async def _wait_for_cleanup_snapshot(backend, deadline: float) -> dict:
    """Require one fresh coherent stopped sample before the command's deadline."""
    loop = asyncio.get_running_loop()
    snapshot = None
    try:
        async with asyncio.timeout_at(deadline):
            while loop.time() < deadline:
                snapshot = await backend.snapshot()
                assert not snapshot.get("backend-error"), snapshot
                # snapshot() reads native properties separately. Stop can leave
                # an idle/empty-playlist sample with the previous path briefly.
                # Do not combine fields from different samples or accept a late
                # result, even if a native call blocked cancellation delivery.
                if loop.time() >= deadline:
                    break
                if (snapshot.get("idle-active") is True
                        and "path" in snapshot and snapshot["path"] in (None, "")
                        and snapshot.get("playlist") == []):
                    return snapshot
                await asyncio.sleep(min(0.01, max(0.0, deadline - loop.time())))
    except TimeoutError:
        pass
    raise AssertionError(f"No coherent cleanup snapshot before command deadline: {snapshot!r}")


class _CleanupSnapshotBackend:
    def __init__(self, *snapshots: dict) -> None:
        self.snapshots = snapshots
        self.reads = 0

    async def snapshot(self) -> dict:
        snapshot = self.snapshots[min(self.reads, len(self.snapshots) - 1)]
        self.reads += 1
        return snapshot


def _cleanup_clock(monkeypatch):
    """Advance polling deterministically without native calls or wall time."""
    loop = asyncio.get_running_loop()
    now = [loop.time()]
    sleeps = []

    async def sleep(delay: float) -> None:
        assert delay > 0
        sleeps.append(delay)
        now[0] += delay

    monkeypatch.setattr(loop, "time", lambda: now[0])
    monkeypatch.setattr(asyncio, "sleep", sleep)
    return now, sleeps


async def test_cleanup_snapshot_requires_a_fresh_coherent_sample(monkeypatch) -> None:
    now, sleeps = _cleanup_clock(monkeypatch)
    stale = {"idle-active": True, "path": "prior-child", "playlist": [], "playback-time": 0.8}
    clean = {"idle-active": True, "path": None, "playlist": []}
    backend = _CleanupSnapshotBackend(stale, clean)
    snapshot = await _wait_for_cleanup_snapshot(backend, now[0] + 0.025)
    assert snapshot is clean and backend.reads == 2 and sleeps == [0.01]


@pytest.mark.parametrize("snapshot", [
    {"idle-active": False, "path": None, "playlist": []},
    {"idle-active": True, "path": "prior-child", "playlist": []},
    {"idle-active": True, "path": None, "playlist": [{"filename": "prior-child"}]},
    {"idle-active": True, "playlist": []},
    {"idle-active": True, "path": None},
], ids=["never-idle", "stale-path", "stale-playlist", "missing-path", "missing-playlist"])
async def test_cleanup_snapshot_rejects_incomplete_cleanup(monkeypatch, snapshot) -> None:
    now, sleeps = _cleanup_clock(monkeypatch)
    deadline = now[0] + 0.025
    backend = _CleanupSnapshotBackend(snapshot)
    with pytest.raises(AssertionError, match="No coherent cleanup snapshot"):
        await _wait_for_cleanup_snapshot(backend, deadline)
    assert now[0] == deadline and backend.reads == 3
    assert sum(sleeps) == pytest.approx(0.025)


async def test_cleanup_snapshot_rejects_backend_error_without_retry(monkeypatch) -> None:
    now, sleeps = _cleanup_clock(monkeypatch)
    clean = {"idle-active": True, "path": None, "playlist": []}
    backend = _CleanupSnapshotBackend(dict(clean, **{"backend-error": True}), clean)
    with pytest.raises(AssertionError, match="backend-error"):
        await _wait_for_cleanup_snapshot(backend, now[0] + 0.025)
    assert backend.reads == 1 and not sleeps


async def test_cleanup_snapshot_never_combines_samples(monkeypatch) -> None:
    now, _ = _cleanup_clock(monkeypatch)
    backend = _CleanupSnapshotBackend(
        {"idle-active": True, "path": "prior-child", "playlist": []},
        {"idle-active": False, "path": None, "playlist": []},
    )
    with pytest.raises(AssertionError, match="No coherent cleanup snapshot"):
        await _wait_for_cleanup_snapshot(backend, now[0] + 0.025)


async def test_cleanup_snapshot_keeps_original_command_deadline(monkeypatch) -> None:
    now, sleeps = _cleanup_clock(monkeypatch)
    start = now[0]
    now[0] += 4.495  # The command has already consumed almost all its budget.
    backend = _CleanupSnapshotBackend(
        {"idle-active": True, "path": "prior-child", "playlist": []},
        {"idle-active": True, "path": None, "playlist": []},
    )
    with pytest.raises(AssertionError, match="No coherent cleanup snapshot"):
        await _wait_for_cleanup_snapshot(backend, start + 4.5)
    assert now[0] == start + 4.5 and backend.reads == 1
    assert sum(sleeps) == pytest.approx(0.005)


async def test_cleanup_snapshot_rejects_sample_finishing_after_deadline(monkeypatch) -> None:
    now, sleeps = _cleanup_clock(monkeypatch)
    deadline = now[0] + 0.025

    class LateBackend:
        async def snapshot(self) -> dict:
            now[0] = deadline
            return {"idle-active": True, "path": None, "playlist": []}

    with pytest.raises(AssertionError, match="No coherent cleanup snapshot"):
        await _wait_for_cleanup_snapshot(LateBackend(), deadline)
    assert not sleeps


async def test_cleanup_snapshot_does_not_start_after_deadline(monkeypatch) -> None:
    now, sleeps = _cleanup_clock(monkeypatch)
    backend = _CleanupSnapshotBackend({"idle-active": True, "path": None, "playlist": []})
    with pytest.raises(AssertionError, match="No coherent cleanup snapshot"):
        await _wait_for_cleanup_snapshot(backend, now[0])
    assert backend.reads == 0 and not sleeps


async def test_cleanup_snapshot_cancels_a_blocked_read_at_deadline() -> None:
    cancelled = False

    class BlockedBackend:
        async def snapshot(self) -> dict:
            nonlocal cancelled
            try:
                await asyncio.Event().wait()
            finally:
                cancelled = True

    with pytest.raises(AssertionError, match="No coherent cleanup snapshot"):
        await _wait_for_cleanup_snapshot(BlockedBackend(), asyncio.get_running_loop().time() + 0.025)
    assert cancelled


async def _exercise(library: str, ffmpeg: str, directory: Path) -> dict:
    media = _generate_media(ffmpeg, directory)
    evidence = {"lane": "REAL_BACKEND/null", "scenario": "nested_playlist_cleanup"}
    async with _renderer(library, media, directory) as (api, backend, fixture, session):
        outer = fixture.base + "/_fixture/nested-outer.m3u"
        inner = fixture.base + "/_fixture/station.m3u"
        child = fixture.base + "/_fixture/tone.mp3"
        fixture.responses["/_fixture/nested-outer.m3u"] = (
            b"#EXTM3U\n#EXTINF:-1,Synthetic nested station\nstation.m3u\n", "audio/x-mpegurl")
        fixture.allowed_urls.add(outer)
        backend._allowed_media.add(outer)
        readiness = api._wait_for_mpv_audio_ready

        async def short_readiness(generation: int, url: str) -> bool:
            # Inject the original active/unowned-child scenario despite the
            # new global admission hold; all progress/cleanup checks remain.
            await backend.checked_command(["set_property", "pause", False])
            ready = await HttpApi._wait_for_mpv_audio_ready(api, generation, url, timeout=1.0)
            snapshot = await backend.snapshot()
            # Confirm the gap is genuinely active native playback with lost
            # ancestry, not simply an unreadable/unsupported test source.
            assert snapshot["path"] == child and snapshot["playlist-path"] == inner, snapshot
            assert snapshot["idle-active"] is False and snapshot["pause"] is False
            assert backend.read_property("current-ao") == "null"
            assert snapshot["playback-time"] > 0.25, snapshot
            assert HttpApi._mpv_snapshot_has_audio(snapshot)
            assert not HttpApi._mpv_snapshot_matches_source(snapshot, outer)
            assert ready is False
            evidence["before_cleanup"] = {
                "path": snapshot["path"], "playlist_path": snapshot["playlist-path"],
                "playback_time": snapshot["playback-time"], "idle": False,
                "requested_source_confirmed": False, "ao": "null",
            }
            return ready

        api._wait_for_mpv_audio_ready = short_readiness
        start = asyncio.get_running_loop().time()
        assert await _command(session, fixture.base, "setPlayerCmd:play:" + outer) == (502, "Failed")
        snapshot = await _wait_for_cleanup_snapshot(backend, start + 4.5)
        elapsed = asyncio.get_running_loop().time() - start
        assert elapsed < 4.5
        assert snapshot["idle-active"] is True and not snapshot["path"] and snapshot["playlist"] == [], snapshot
        assert api.state.backend_url == api.state.backend_original_url == ""
        assert api.state.current_url == api.state.queue.current.url == outer
        assert api.state.phase == PlaybackPhase.ERROR
        assert api.state.last_error == "Source did not confirm playback"
        evidence["after_cleanup"] = {"http_status": 502, "idle": True, "backend_identity": "",
                                      "error": api.state.last_error, "elapsed": elapsed}
        # Confirm native output stays idle without a second owner Stop, and
        # an error response cannot race a background late-success publication.
        await asyncio.sleep(0.6)
        assert backend.read_property("idle-active") is True
        assert api.state.phase == PlaybackPhase.ERROR
        api._wait_for_mpv_audio_ready = readiness
        assert await _command(session, fixture.base, "setPlayerCmd:play:" + child) == (200, "OK")
        assert api.state.phase == PlaybackPhase.PLAYING and not api.state.last_error
        assert backend.read_property("path") == child
        assert backend.loads == [outer, child]
        assert await _command(session, fixture.base, "setPlayerCmd:stop") == (200, "OK")
        assert backend.read_property("idle-active") is True
        evidence["subsequent_playback"] = True
    evidence["cleanup"] = {"handle_closed": True, "listener_closed": True, "no_pending_tasks": True}
    return evidence


@pytest.mark.skipif(not os.environ.get("ORR_TEST_LIBMPV_PATH"),
                    reason="REAL_BACKEND/null cleanup requires explicit ORR_TEST_LIBMPV_PATH")
def test_nested_playlist_unconfirmed_native_audio_is_silenced(tmp_path: Path) -> None:
    library = Path(os.environ["ORR_TEST_LIBMPV_PATH"]).expanduser().resolve()
    assert library.is_file(), f"Configured libmpv does not exist: {library}"
    ffmpeg = shutil.which(os.environ.get("ORR_TEST_FFMPEG_BINARY", "ffmpeg"))
    assert ffmpeg, "Opt-in fixture generation requires official FFmpeg"
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    for name in tuple(env):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            env.pop(name)
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--probe", str(library), ffmpeg, str(tmp_path)],
        cwd=root, env=env, text=True, capture_output=True, timeout=25, check=False,
    )
    assert completed.returncode == 0, f"Native cleanup failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    evidence = json.loads(completed.stdout)
    assert evidence["cleanup"] == {"handle_closed": True, "listener_closed": True, "no_pending_tasks": True}
    assert evidence["subsequent_playback"] is True
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 5 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")

    async def main() -> None:
        async with asyncio.timeout(20):
            evidence = await _exercise(sys.argv[2], sys.argv[3], Path(sys.argv[4]))
        print(json.dumps(evidence, sort_keys=True))

    asyncio.run(main())
