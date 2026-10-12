"""REAL_BACKEND/null: station history is per-file; forward cache stays native.

Generated WAVs stand in for explicitly typed queue items. Separate sustained
ADTS acceptance measures retention during a continuous live stream. This
command-node lane tests native options, not production Unix IPC or hardware.
"""
from __future__ import annotations

import asyncio
import json
import locale
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from fake_linkplay.config import Config
from fake_linkplay.mpv_backend import LIVE_STATION_BACK_BYTES
from fake_linkplay.http_api import HttpApi, ParsedCommand
from fake_linkplay.queue import PlaybackQueue, QueueItem
from fake_linkplay.state import PlaybackPhase, RendererState
from test_libmpv_integration import _eventually, _write_tone
from test_libmpv_recovery import _StationBackend


CACHE_KEYS = ("demuxer-max-back-bytes", "demuxer-donate-buffer", "demuxer-max-bytes", "demuxer-readahead-secs", "cache", "cache-secs")


async def _exercise(library: str, directory: Path) -> dict[str, Any]:
    baseline = asyncio.all_tasks()
    tracks = (directory / "station.wav", directory / "finite.wav")
    for path, frequency in zip(tracks, (440, 660)):
        _write_tone(path, frequency)
    backend = _StationBackend(library, tracks, str(tracks[0]))
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(directory / "state"),
                 auto_spawn_mpv=False, enable_ssdp=False, enable_mdns=False)
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)
    def options():
        return {key: backend.read_property("options/" + key) for key in CACHE_KEYS}
    evidence: dict[str, Any] = {"lane": "REAL_BACKEND/null/station-history"}
    try:
        evidence["mpv_version"] = backend.read_property("mpv-version")
        evidence["option_info"] = backend.read_property("option-info/demuxer-max-back-bytes")
        defaults = options()
        assert defaults["demuxer-max-back-bytes"] > 0
        await api._set_confirmed_gain(volume=37, mute=True)
        await api.execute(ParsedCommand("setChannelBalance:0.5"), "127.0.0.1")
        graph = backend.read_property("af")
        assert graph
        queue = PlaybackQueue(items=[QueueItem(str(tracks[0]), stream_type="station"),
                                     QueueItem(str(tracks[1]), stream_type="track", duration=30)], index=1)
        await api._replace_queue(queue)
        await api._play_queue_index(1, queue.name)
        selected = queue.current
        station_options = {**defaults, "demuxer-max-back-bytes": LIVE_STATION_BACK_BYTES, "demuxer-donate-buffer": False}
        assert options() == station_options
        assert backend.read_property("option-info/demuxer-max-back-bytes/set-locally") is True
        assert api.state.current_url == api.state.backend_original_url == api.state.backend_url == selected.url
        before = backend.read_property("playback-time")
        await _eventually("station decode", lambda: backend.read_property("playback-time") > before + .25)
        await api.execute(ParsedCommand("pause"), "127.0.0.1")
        await asyncio.sleep(.1)
        paused_at = backend.read_property("playback-time")
        await asyncio.sleep(.3)
        assert backend.read_property("pause") is True
        assert backend.read_property("playback-time") == pytest.approx(paused_at, abs=.001)
        assert options() == station_options and api.state.phase == PlaybackPhase.PAUSED
        await api.execute(ParsedCommand("resume"), "127.0.0.1")
        await _eventually("station resumes without reload", lambda: backend.read_property("playback-time") > paused_at + .25)
        assert backend.loads == [selected.url]
        await api.execute(ParsedCommand("stop"), "127.0.0.1")
        await _eventually("owner Stop", lambda: backend.read_property("idle-active") is True)
        assert api.state.phase == PlaybackPhase.STOPPED
        await api._play_queue_index(1, queue.name)
        assert options() == station_options and len(backend.loads) == 2
        assert queue.current is selected
        # Native defaults, including forward history, return on an ordinary
        # finite load and remain intact on unknown-duration untyped media.
        await api._play_queue_index(2, queue.name)
        assert options() == defaults
        assert backend.read_property("option-info/demuxer-max-back-bytes/set-locally") is False
        await api._play_url(str(tracks[0]))
        assert not api.state.queue.current.stream_type
        assert options() == defaults
        assert backend.read_property("af") == graph
        assert backend.read_property("volume") == pytest.approx(37)
        assert backend.read_property("mute") is True
        assert backend.read_property("current-ao") == "null"
        evidence.update(native_defaults=defaults, station_options=station_options,
                        finite_and_unknown_reset=True, pause_resume_without_reload=True,
                        stop_replay=True, identity_preserved=True, gain_and_dsp_preserved=True)
        await api.execute(ParsedCommand("stop"), "127.0.0.1")
    finally:
        api._invalidate_station_recovery()
        backend.close()
        await asyncio.sleep(0)
        pending = [task for task in asyncio.all_tasks() - baseline if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        assert not pending, f"Station history probe leaked tasks: {pending!r}"
        assert backend._handle is None
    evidence["cleanup"] = {"handle_closed": True, "no_pending_tasks": True}
    return evidence


@pytest.mark.skipif(not os.environ.get("ORR_TEST_LIBMPV_PATH"), reason="REAL_BACKEND/null requires ORR_TEST_LIBMPV_PATH")
def test_real_libmpv_station_history_is_per_file(tmp_path: Path) -> None:
    library = Path(os.environ["ORR_TEST_LIBMPV_PATH"]).expanduser().resolve()
    assert library.is_file()
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--probe", str(library), str(tmp_path)],
                            cwd=root, env=env, text=True, capture_output=True, timeout=20, check=False)
    assert result.returncode == 0, f"Station history options failed\n{result.stdout}\n{result.stderr}"
    evidence = json.loads(result.stdout)
    assert evidence["finite_and_unknown_reset"] and evidence["gain_and_dsp_preserved"]
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 4 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")
    async def main() -> None:
        async with asyncio.timeout(15):
            evidence = await _exercise(sys.argv[2], Path(sys.argv[3]))
        print(json.dumps(evidence, sort_keys=True))
    asyncio.run(main())
