"""REAL_BACKEND/null foreign-source observations using generated local WAVs.

Production snapshots and controls run through the existing test-only public
libmpv adapter. This proves native path/entry identity and non-mutation, not
production Unix IPC, physical output, or Android/controller compatibility.
"""
from __future__ import annotations

import asyncio
import json
import locale
import os
from pathlib import Path
import subprocess
import sys
from xml.etree import ElementTree as ET

import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.queue import PlaybackQueue, QueueItem
from fake_linkplay.soap import SoapFault
from fake_linkplay.state import PlaybackPhase, RendererState
from fake_linkplay.upnp_description import avtransport_response_fields
from queue_transition_probe import RATE, write_source
from test_libmpv_integration import _eventually, _write_tone
from test_libmpv_queue_staging import _StagingBackend


async def _exercise(library: str, directory: Path) -> dict:
    baseline = asyncio.all_tasks()
    paths = tuple(directory / name for name in ("short.wav", "long.wav", "foreign.wav"))
    write_source(paths[0], RATE, 1)
    _write_tone(paths[1], 440)
    _write_tone(paths[2], 660)
    evidence = {"lane": "REAL_BACKEND/null/source-ownership", "cases": {}}
    for kind in ("direct", "terminal_same_url"):
        backend = _StagingBackend(library, paths)
        cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(directory / kind),
                     native_queue_staging=kind != "direct", enable_ssdp=False, enable_mdns=False)
        api = HttpApi(cfg, RendererState.from_config(cfg), backend)
        events = []
        api._schedule_event_notify = lambda *args: events.append(api._event_body("AVTransport"))
        try:
            evidence["mpv_version"] = backend.read_property("mpv-version")
            chosen = paths[:2] if kind != "direct" else (paths[1],)
            api.state.queue = PlaybackQueue(name="Owned synthetic queue", index=1, items=[
                QueueItem(url=str(path), title=f"Owned {index}", duration=2 if path == paths[0] else 30)
                for index, path in enumerate(chosen, 1)])
            api._activate_queue_item()
            await api._play_url(str(chosen[0]))
            if kind != "direct":
                await _eventually("native successor ready", lambda: (
                    backend.read_property("path") == str(paths[1])
                    and any(entry.get("filename") == str(paths[1]) and entry.get("playing") is True
                            for entry in backend.read_property("playlist"))), timeout=5)
                await api.sync_mpv_state()
                assert api.state.queue.index == 2 and api._native_stage is None
                assert api._native_current_id is not None
            selection = (api.state.current_url, api.state.backend_url, api.state.title, api.state.queue.index,
                         api.state.intent, api.state.volume, api.state.mute)
            old_id, generation = api._native_current_id, api.state.generation
            assert api._monitor_interval == .5
            api._state_monitor_task = asyncio.create_task(api._monitor_backend())
            foreign = paths[1] if kind != "direct" else paths[2]
            await backend.checked_command(["loadfile", str(foreign), "replace"])
            await backend.checked_command(["set_property", "volume", 73])
            await _eventually("foreign native audio ready", lambda: (
                backend.read_property("path") == str(foreign)
                and backend.read_property("playback-time") > .05
                and any(entry.get("filename") == str(foreign) and entry.get("playing") is True
                        and entry.get("id") != old_id for entry in backend.read_property("playlist"))), timeout=5)
            snapshot = await backend.snapshot()
            assert api._native_source_observation_coherent(snapshot)
            assert not api._native_control_source_matches(snapshot)
            commands = list(backend.commands)
            foreign_position = backend.read_property("playback-time")
            await _eventually("default observer detects foreign source", lambda: api.state.phase == PlaybackPhase.ERROR)
            assert api.state.phase == PlaybackPhase.ERROR and api._native_output_uncertain
            assert api.state.generation == generation + 1 and not api.state.gain_confirmed
            assert "playback silence not confirmed" in api.state.last_error
            assert (api.state.current_url, api.state.backend_url, api.state.title, api.state.queue.index,
                    api.state.intent, api.state.volume, api.state.mute) == selection
            assert any('TransportStatus val="ERROR_OCCURRED"' in ET.fromstring(event).findtext(".//LastChange")
                       for event in events)
            await _eventually("foreign audio still progressing", lambda:
                              backend.read_property("playback-time") > foreign_position + .05)
            assert "ERROR_OCCURRED" in avtransport_response_fields("GetTransportInfo", api.state)
            assert 'TransportStatus val="ERROR_OCCURRED"' in ET.fromstring(api._event_body("AVTransport")).findtext(".//LastChange")
            await api.sync_mpv_state()
            with pytest.raises(SoapFault):
                await api._seek_command(1)
            assert backend.commands == commands
            assert backend.read_property("path") == str(foreign)
            assert backend.read_property("volume") == 73 and backend.read_property("idle-active") is False
            await api._execute_soap_side_effect("Play", "<Play/>")
            assert backend.commands[len(commands)] == ["stop"]
            assert api.state.phase == PlaybackPhase.PLAYING and not api._native_output_uncertain
            assert api.state.gain_confirmed and backend.read_property("volume") == selection[5]
            assert backend.read_property("path") == selection[0]
            assert api.state.queue.index == selection[3]
            await api._transport_command("stop")
            assert backend.read_property("idle-active") is True and api.state.phase == PlaybackPhase.STOPPED
            evidence["cases"][kind] = {"coherent_native_replacement": True,
                "same_url_different_id": kind != "direct", "foreign_audio_untouched": True,
                "default_monitor_interval_seconds": api._monitor_interval,
                "error_event_scheduled_before_status_read": True, "foreign_progress_continues": True,
                "no_gain_donation": True, "explicit_play_checked_stop_and_recovered": True,
                "explicit_stop_confirmed": True}
        finally:
            monitor, api._state_monitor_task = api._state_monitor_task, None
            if monitor is not None:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
            backend.close()
            assert backend._handle is None
    assert not [task for task in asyncio.all_tasks() - baseline if not task.done()]
    evidence["cleanup"] = {"native_handles_destroyed": True, "no_pending_tasks": True}
    return evidence


@pytest.mark.skipif(not os.environ.get("ORR_TEST_LIBMPV_PATH"), reason="Native ownership proof requires ORR_TEST_LIBMPV_PATH")
def test_real_native_foreign_source_observations(tmp_path):
    library = Path(os.environ["ORR_TEST_LIBMPV_PATH"]).expanduser().resolve()
    assert library.is_file()
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--probe", str(library), str(tmp_path)],
                            cwd=root, env=env, capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    evidence = json.loads(result.stdout)
    assert evidence["cleanup"] == {"native_handles_destroyed": True, "no_pending_tasks": True}
    assert len(evidence["cases"]) == 2
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 4 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")
    async def main():
        async with asyncio.timeout(20):
            print(json.dumps(await _exercise(sys.argv[2], Path(sys.argv[3])), sort_keys=True))
    asyncio.run(main())
