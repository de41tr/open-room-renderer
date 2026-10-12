"""REAL_BACKEND/null staging ownership, using generated local WAV/M3U files.

Opt in with ORR_TEST_LIBMPV_PATH. The native decoder, playlist transitions,
entry IDs, gain/DSP and renderer state are real; only command transport uses
ctypes. This does not measure clocked gaps or certify production Unix IPC.
"""
from __future__ import annotations

import asyncio
import ctypes as ct
import json
import locale
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.queue import PlaybackQueue, QueueItem
from fake_linkplay.soap import SoapFault
from fake_linkplay.state import PlaybackPhase, RendererState
from queue_transition_probe import write_source, RATE
from test_libmpv_integration import _LibmpvBackend, _Node, _decode_node, _eventually
from test_libmpv_load_options import _encode_string_map


class _StagingBackend(_LibmpvBackend):
    """Preserve loadfile's actual command-node result including native ID."""

    def __init__(self, library: str, tracks: tuple[Path, ...]):
        super().__init__(library, tracks)
        self._lib.mpv_command_node.restype = ct.c_int
        self._lib.mpv_command_node.argtypes = [ct.c_void_p, ct.POINTER(_Node), ct.POINTER(_Node)]
        self.commands = []
        self.append_started = asyncio.Event()
        self.release_append = None
        self.discard_append_ack = False

    async def command(self, args: list[Any] | dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
        operation = args.get("name") if isinstance(args, dict) else args[0]
        if operation != "get_property":
            self.commands.append(args)
        if operation != "loadfile":
            return await super().command(args, timeout)
        if isinstance(args, list):
            assert len(args) == 3
            args = {"name": "loadfile", "url": args[1], "flags": args[2], "options": {}}
        assert args["url"] in self._allowed_media and args["flags"] in {"replace", "append"}
        command = _encode_string_map(args, keepalive := [])
        result = _Node()
        try:
            self._check(self._lib.mpv_command_node(self._handle, ct.byref(command), ct.byref(result)))
            response = {"error": "success", "data": _decode_node(result)}
        except BackendError as exc:
            return {"error": str(exc)}
        finally:
            self._lib.mpv_free_node_contents(ct.byref(result))
        if args["flags"] == "append":
            self.append_started.set()
            if self.release_append is not None:
                await self.release_append.wait()
            if self.discard_append_ack:
                return {"error": "synthetic lost reply after real append"}
        return response


def _loads(backend):
    return [command for command in backend.commands
            if isinstance(command, list) and command[0] == "loadfile"]


async def _exercise(library: str, directory: Path) -> dict[str, Any]:
    baseline = asyncio.all_tasks()
    tracks = tuple(directory / name for name in ("a.wav", "b.wav", "c.wav"))
    for index, path in enumerate(tracks):
        write_source(path, RATE, 1 if index == 0 else -1)
    playlist = directory / "first-level.m3u"
    playlist.write_text("#EXTM3U\nb.wav\nc.wav\n")
    inner = directory / "inner.m3u"
    inner.write_text("#EXTM3U\nb.wav\n")
    nested = directory / "nested.m3u"
    nested.write_text("#EXTM3U\ninner.m3u\n")
    backend = _StagingBackend(library, (*tracks, playlist, inner, nested))
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(directory / "state"),
                 native_queue_staging=True, enable_ssdp=False, enable_mdns=False)
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)
    evidence: dict[str, Any] = {"lane": "REAL_BACKEND/null/native-queue-staging", "cases": {}}
    try:
        evidence["mpv_version"] = backend.read_property("mpv-version")
        defaults = {key: backend.read_property("options/" + key) for key in ("gapless-audio", "audio-buffer")}
        async def queue(paths):
            await api._replace_queue(PlaybackQueue(name="Synthetic native queue", index=1,
                items=[QueueItem(url=str(path), title=f"Track {index}", duration=2) for index, path in enumerate(paths, 1)]))
            backend.commands.clear()
            backend.append_started = asyncio.Event()
        async def at(path):
            return await _eventually("native current item", lambda: backend.read_property("path") == str(path), timeout=5)
        async def idle():
            await _eventually("native EOF", lambda: backend.read_property("idle-active") is True, timeout=6)

        # State has not been polled at the transition. A Pause must discover
        # actual B, retain its native ID and update every canonical serializer.
        await queue(tracks[:2])
        await api._play_url(str(tracks[0]))
        assert api.state.queue.index == 1 and api._native_stage.confirmed
        with pytest.raises(SoapFault):
            await api._seek_command(2)
        assert backend.read_property("pause") is False and api.state.phase == PlaybackPhase.PLAYING
        evidence["cases"]["invalid_seek"] = {"temporary_pause_restored": True, "no_seek_applied": True}
        native_ids = [entry["id"] for entry in backend.read_property("playlist")]
        assert len(native_ids) == 2 and len(set(native_ids)) == 2
        await at(tracks[1])
        await api._transport_command("pause")
        assert api.state.queue.index == 2 and api.state.phase == PlaybackPhase.PAUSED
        assert backend.read_property("pause") is True
        await api._seek_command(1)
        native_seek_position = backend.read_property("playback-time")
        # The existing confirmed-seek contract is a two-sided one-second
        # window; null AO buffering can report e.g. 0.83 for target 1. Keep
        # reporting the actual position rather than fabricating integer 1.
        assert abs(native_seek_position - 1) <= 1
        assert abs(api.state.curpos - native_seek_position) <= 1
        assert backend.read_property("pause") is True and api.state.phase == PlaybackPhase.PAUSED
        await api._transport_command("resume")
        assert len(_loads(backend)) == 2 and _loads(backend)[1][2] == "append"
        evidence["cases"]["pause_seek_resume_transition"] = {"distinct_ack_ids": True, "canonical_index": 2,
            "no_successor_reload": True, "pause_preserved_during_seek": True}
        await api._transport_command("stop")

        await queue((tracks[0], tracks[0]))
        await api._play_url(str(tracks[0]))
        first_id = api._native_current_id
        next_id = api._native_stage.entry_id
        await _eventually("duplicate URL successor ID", lambda: any(entry.get("id") == next_id and entry.get("current") is True and entry.get("playing") is True for entry in backend.read_property("playlist")), timeout=5)
        await api.sync_mpv_state()
        assert first_id != next_id and api.state.queue.index == 2
        assert len(_loads(backend)) == 2
        evidence["cases"]["duplicate_url"] = {"exact_entry_id_adopted": True, "no_successor_reload": True}
        await api._transport_command("stop")

        for action, wanted in (("Next", 3), ("Previous", 1)):
            await queue(tracks)
            await api._play_url(str(tracks[0]))
            await at(tracks[1])
            await api._execute_soap_side_effect(action, f"<{action}/>")
            assert api.state.queue.index == wanted and backend.read_property("path") == str(tracks[wanted - 1])
            assert len([entry for entry in _loads(backend) if entry[2] == "replace"]) == 2
            evidence["cases"]["unobserved_transition_" + action.lower()] = {"actual_index_reconciled": True}
            await api._transport_command("stop")

        await queue((tracks[0], playlist, tracks[0]))
        await api._play_url(str(tracks[0]))
        acknowledged_container = api._native_stage.entry_id
        await at(tracks[1])
        snapshot = await backend.snapshot()
        await api.sync_mpv_state()
        assert api.state.queue.index == 2 and api.state.backend_url == str(playlist), snapshot
        assert acknowledged_container not in [entry["id"] for entry in snapshot["playlist"]]
        assert len(_loads(backend)) == 2 and api._native_stage is None
        await at(tracks[2])
        await api.sync_mpv_state()
        assert api.state.queue.index == 2
        await idle()
        await api.sync_mpv_state()
        await api._queue_advance_task
        assert api.state.queue.index == 3 and backend.read_property("path") == str(tracks[0])
        evidence["cases"]["first_level_container"] = {"ack_replaced_by_exact_parent_children": True,
            "one_canonical_item_through_children": True, "canonical_successor_after_container_eof": True}
        await api._transport_command("stop")

        # Unprovable nested ancestry stays unsupported; it is never silently
        # mapped to an apparently similar canonical URL.
        await queue((tracks[0], nested))
        await api._play_url(str(tracks[0]))
        await at(tracks[1])
        await api.sync_mpv_state()
        assert api.state.queue.index == 1 and api._native_stage is not None
        assert backend.read_property("playlist-path") == str(inner)
        assert api.state.phase == PlaybackPhase.ERROR and "silence not confirmed" in api.state.last_error
        await api._transport_command("stop")
        evidence["cases"]["nested_container"] = {"not_adopted": True, "unproven_silence_reported": True}

        for owner in ("pause", "stop", "source"):
            await queue(tracks[:2])
            backend.release_append = asyncio.Event()
            playing = asyncio.create_task(api._play_url(str(tracks[0])))
            await backend.append_started.wait()
            await at(tracks[1])
            changing = asyncio.create_task(api._play_url(str(tracks[2])) if owner == "source" else api._transport_command(owner))
            await asyncio.sleep(0)
            backend.release_append.set()
            with pytest.raises(BackendError, match="superseded"):
                await playing
            await changing
            backend.release_append = None
            if owner == "pause":
                assert api.state.queue.index == 2 and backend.read_property("pause") is True
            elif owner == "stop":
                assert api.state.phase == PlaybackPhase.STOPPED and backend.read_property("idle-active") is True
                assert backend.read_property("playlist") == []
            else:
                assert backend.read_property("path") == str(tracks[2]) and api.state.phase == PlaybackPhase.PLAYING
                assert len(backend.read_property("playlist")) == 1
            evidence["cases"]["append_eof_" + owner] = {"new_owner_preserved": True}
            await api._transport_command("stop")

        await queue(tracks[:2])
        await api._play_url(str(tracks[0]))
        resolver = api._resolve_playback_url
        async def failed_resolver(url):
            raise BackendError("Synthetic new-source resolution failure")
        api._resolve_playback_url = failed_resolver
        with pytest.raises(BackendError):
            await api._play_url(str(tracks[2]))
        api._resolve_playback_url = resolver
        assert api.state.phase == PlaybackPhase.ERROR and backend.read_property("idle-active") is True
        assert backend.read_property("playlist") == []
        evidence["cases"]["new_source_resolution_failed"] = {"retired_queue_silenced": True, "no_orphan_entry": True}

        await queue(tracks[:2])
        backend.discard_append_ack = True
        with pytest.raises(BackendError):
            await api._play_url(str(tracks[0]))
        assert api.state.phase == PlaybackPhase.ERROR and backend.read_property("idle-active") is True
        assert backend.read_property("playlist") == []
        evidence["cases"]["lost_append_reply"] = {"same_owner_silence_confirmed": True, "no_orphan_entry": True}
        backend.discard_append_ack = False
        await queue(tracks[:2])
        backend.discard_append_ack = True
        backend.release_append = asyncio.Event()
        playing = asyncio.create_task(api._play_url(str(tracks[0])))
        await backend.append_started.wait()
        await at(tracks[1])
        backend.release_append.set()
        with pytest.raises(BackendError):
            await playing
        backend.release_append = None
        assert api.state.phase == PlaybackPhase.ERROR and backend.read_property("idle-active") is True
        assert api.state.queue.index == 1 and backend.read_property("playlist") == []
        evidence["cases"]["lost_append_reply_after_eof"] = {"same_owner_silence_confirmed": True,
            "unacknowledged_entry_not_adopted": True, "no_orphan_entry": True}
        assert backend.read_property("volume") == 50 and backend.read_property("mute") is False
        assert backend.confirmed_dsp == (0.0, [])
        assert {key: backend.read_property("options/" + key) for key in defaults} == defaults
        evidence["unchanged_gain_dsp_audio_defaults"] = True
    finally:
        backend.close()
        assert backend._handle is None
    assert not [task for task in asyncio.all_tasks() - baseline if not task.done()]
    evidence["cleanup"] = {"native_handle_destroyed": True, "no_pending_tasks": True}
    return evidence


@pytest.mark.skipif(not os.environ.get("ORR_TEST_LIBMPV_PATH"), reason="Native staging requires ORR_TEST_LIBMPV_PATH")
def test_real_libmpv_native_queue_staging(tmp_path):
    library = Path(os.environ["ORR_TEST_LIBMPV_PATH"]).expanduser().resolve()
    assert library.is_file()
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--probe", str(library), str(tmp_path)],
                            cwd=root, env=env, capture_output=True, text=True, timeout=50)
    assert result.returncode == 0, f"Native staging failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    evidence = json.loads(result.stdout)
    assert evidence["cleanup"] == {"native_handle_destroyed": True, "no_pending_tasks": True}
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 4 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")
    async def main():
        async with asyncio.timeout(45):
            print(json.dumps(await _exercise(sys.argv[2], Path(sys.argv[3])), sort_keys=True))
    asyncio.run(main())
