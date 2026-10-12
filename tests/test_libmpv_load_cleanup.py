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
        elapsed = asyncio.get_running_loop().time() - start
        assert elapsed < 4.5
        snapshot = await backend.snapshot()
        assert snapshot["idle-active"] is True and not snapshot["path"], snapshot
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
