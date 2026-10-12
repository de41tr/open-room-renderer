"""REAL_BACKEND/null: causal replace ACK versus a later foreign root.

Generated local WAVs; existing public libmpv command-node adapter with production
controls/readiness. This does not certify Unix IPC, physical audio, container
replacement, missing ACKs, range reloads, or native-core restart identity.
"""
import asyncio
import json
import locale
import os
from pathlib import Path
import subprocess
import sys

import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi, SourcePlaybackError
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.queue import PlaybackQueue, QueueItem
from fake_linkplay.state import PlaybackPhase, RendererState
from test_libmpv_integration import _eventually, _write_tone
from test_libmpv_queue_staging import _StagingBackend


async def _exercise(library, directory):
    baseline = asyncio.all_tasks()
    paths = (directory / "owned.wav", directory / "foreign.wav")
    for path, hz in zip(paths, (440, 660)):
        _write_tone(path, hz)
    evidence = {"lane": "REAL_BACKEND/null/admitted-load-root", "cases": {}}
    for route in ("observer", "readiness", "failure", "cancel"):
        for same_url in (False, True):
            backend = _StagingBackend(library, paths)
            cfg = Config(uuid="00000000-0000-4000-8000-000000000000",
                         state_dir=str(directory / f"{route}-{same_url}"), enable_ssdp=False, enable_mdns=False)
            api = HttpApi(cfg, RendererState.from_config(cfg), backend)
            api._schedule_event_notify = lambda *args: None
            api.state.queue = PlaybackQueue(index=1, items=[QueueItem(str(paths[0]), title="Owned selection", duration=30)])
            api._activate_queue_item()
            real_readiness = api._wait_for_mpv_audio_ready
            entered, release = asyncio.Event(), asyncio.Event()
            async def readiness(generation, url):
                entered.set()
                await release.wait()
                if route in {"failure", "cancel"}:
                    return False
                return await real_readiness(generation, url)
            api._wait_for_mpv_audio_ready = readiness
            monitor = None
            task = None
            try:
                evidence["mpv_version"] = backend.read_property("mpv-version")
                task = asyncio.create_task(api._play_url(str(paths[0])))
                await entered.wait()
                admitted = api._admitted_load_entry_id
                assert type(admitted) is int and admitted >= 0
                foreign = paths[0] if same_url else paths[1]
                result = await backend.checked_command(["loadfile", str(foreign), "replace"])
                replaced = result["data"]["playlist_entry_id"]
                assert replaced != admitted
                await backend.checked_command(["set_property", "volume", 73])
                await _eventually("replacement decoded", lambda: (
                    backend.read_property("path") == str(foreign)
                    and backend.read_property("playback-time") > .05
                    and any(entry.get("id") == replaced and entry.get("playing")
                            for entry in backend.read_property("playlist"))))
                position = backend.read_property("playback-time")
                commands = list(backend.commands)
                if route == "observer":
                    assert api._monitor_interval == .5
                    monitor = asyncio.create_task(api._monitor_backend())
                    await _eventually("background root loss", lambda: api.state.phase == PlaybackPhase.ERROR)
                if route == "cancel":
                    task.cancel()
                else:
                    release.set()
                with pytest.raises(asyncio.CancelledError if route == "cancel" else BackendError):
                    await task
                assert api.state.phase == PlaybackPhase.ERROR and api._native_output_uncertain
                assert api.state.current_url == api.state.backend_url == str(paths[0])
                assert api.state.title == "Owned selection" and api.state.intent == "play"
                assert "silence not confirmed" in api.state.last_error and not api.state.gain_confirmed
                assert api._admitted_load_generation is None and api._admitted_load_entry_id is None
                await _eventually("foreign progress survives", lambda: backend.read_property("playback-time") > position + .05)
                assert backend.read_property("path") == str(foreign)
                assert backend.read_property("volume") == 73 and backend.read_property("idle-active") is False
                assert backend.commands == commands
                api._wait_for_mpv_audio_ready = real_readiness
                await api._execute_soap_side_effect("Play", "<Play/>")
                assert backend.commands[len(commands)] == ["stop"]
                assert api.state.phase == PlaybackPhase.PLAYING and not api._native_output_uncertain
                assert backend.read_property("path") == str(paths[0]) and backend.read_property("volume") == 50
                await api._transport_command("stop")
                assert backend.read_property("idle-active") is True
                evidence["cases"][f"{route}-{'same' if same_url else 'different'}"] = {
                    "different_ack_ids": True, "foreign_native_progress_untouched": True,
                    "canonical_selection_retained": True, "checked_explicit_play_stop_recovered": True}
            finally:
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if monitor is not None:
                    monitor.cancel()
                    await asyncio.gather(monitor, return_exceptions=True)
                backend.close()
    # Non-default merge-files is an actual native counterexample to treating
    # every unequal parentless ID as external replacement. Only this case's
    # readiness result is rejected deliberately; native decoding/IDs are real.
    merge_playlist = directory / "owned-merge.m3u"
    merge_playlist.write_text("#EXTM3U\nowned.wav\n")
    backend = _StagingBackend(library, (*paths, merge_playlist))
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(directory / "owned-edl"),
                 enable_ssdp=False, enable_mdns=False)
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)
    api._schedule_event_notify = lambda *args: None
    async def unready_edl(generation, url):
        await _eventually("owned native EDL decoded", lambda: (
            str(backend.read_property("path")).startswith("edl://")
            and backend.read_property("playback-time") > .05))
        snapshot = await backend.snapshot()
        active = api._native_active(snapshot)
        assert active and active["id"] != api._admitted_load_entry_id
        assert not snapshot.get("playlist-path") and not active.get("playlist-path")
        await api.sync_mpv_state()
        assert api.state.generation == generation and api.state.phase == PlaybackPhase.BUFFERING
        return False
    api._wait_for_mpv_audio_ready = unready_edl
    try:
        # Finish the initial empty-playlist preparation before changing this
        # option. The subsequent M3U expansion then deterministically reaches
        # populate-parentage -> prepare_playlist -> merge_playlist_files.
        await backend.loadfile(str(paths[0]))
        await _eventually("startup preparation finished", lambda: (
            backend.read_property("path") == str(paths[0])
            and backend.read_property("playback-time") > .05))
        await backend.stop()
        await _eventually("warmup stopped", lambda: backend.read_property("idle-active") is True)
        await backend.checked_command(["set_property", "merge-files", True])
        backend.commands.clear()
        with pytest.raises(SourcePlaybackError):
            await api._play_url(str(merge_playlist))
        assert backend.read_property("idle-active") is True and backend.read_property("playlist") == []
        assert api.state.phase == PlaybackPhase.ERROR and not api._native_output_uncertain
        assert api.state.last_error == "Source did not confirm playback"
        assert [command for command in backend.commands if command == ["stop"]] == [["stop"]]
        evidence["owned_edl_cleanup"] = {"merge_files": True, "native_changed_root_id": True,
                                           "owned_unready_audio_stopped": True}
    finally:
        backend.close()
    assert not [task for task in asyncio.all_tasks() - baseline if not task.done()]
    evidence["cleanup"] = {"handles_closed": True, "no_pending_tasks": True}
    return evidence


@pytest.mark.skipif(not os.environ.get("ORR_TEST_LIBMPV_PATH"), reason="Native root ownership requires ORR_TEST_LIBMPV_PATH")
def test_native_admitted_root_replacement_preserves_foreign_playback(tmp_path):
    library = Path(os.environ["ORR_TEST_LIBMPV_PATH"]).expanduser().resolve()
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--probe", str(library), str(tmp_path)],
                            cwd=root, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    evidence = json.loads(result.stdout)
    assert len(evidence["cases"]) == 8 and evidence["cleanup"]["no_pending_tasks"]
    assert evidence["owned_edl_cleanup"]["owned_unready_audio_stopped"]
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 4 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")
    async def main():
        async with asyncio.timeout(25):
            print(json.dumps(await _exercise(sys.argv[2], Path(sys.argv[3])), sort_keys=True))
    asyncio.run(main())
