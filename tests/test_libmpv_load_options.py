"""Optional real libmpv named-loadfile options, with local synthetic WAVs.

ORR_TEST_LIBMPV_PATH enables this null-output lane. Production load/gain code
is inherited; a test-only public command-node adapter replaces Unix IPC.
This validates native option application, not production IPC or hardware.
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

from fake_linkplay.mpv_backend import BackendError
from test_libmpv_integration import (
    _LibmpvBackend, _Node, _NodeList, _decode_node, _eventually, _write_tone,
)


def _encode_string_map(value: str | dict[str, Any], keepalive: list[Any]) -> _Node:
    """Keep Python-owned node storage alive until mpv_command_node returns."""
    node = _Node()
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        keepalive.append(encoded)
        node.format, node.u.string = 1, encoded
    else:
        assert isinstance(value, dict) and all(isinstance(key, str) for key in value)
        keys = (ct.c_char_p * len(value))(*(key.encode("utf-8") for key in value))
        values = (_Node * len(value))(*(_encode_string_map(item, keepalive) for item in value.values()))
        items = _NodeList(len(value), values, keys)
        keepalive.extend((keys, values, items))
        node.format, node.u.list = 8, ct.pointer(items)
    return node


class _NamedLoadBackend(_LibmpvBackend):
    """Only loadfile maps are added to the existing native test transport."""

    def __init__(self, library: str, tracks: tuple[Path, Path]) -> None:
        super().__init__(library, tracks)
        self._lib.mpv_command_node.restype = ct.c_int
        self._lib.mpv_command_node.argtypes = [ct.c_void_p, ct.POINTER(_Node), ct.POINTER(_Node)]

    async def command(self, args: list[Any] | dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
        if not isinstance(args, dict):
            return await super().command(args, timeout)
        assert set(args) == {"name", "url", "flags", "options"}, args
        assert args["name"] == "loadfile" and args["flags"] == "replace", args
        assert args["url"] in self._allowed_media, "Only generated local WAVs may be loaded"
        assert isinstance(args["options"], dict) and all(isinstance(value, str) for value in args["options"].values())
        keepalive: list[Any] = []
        command = _encode_string_map(args, keepalive)
        result = _Node()
        try:
            self._check(self._lib.mpv_command_node(self._handle, ct.byref(command), ct.byref(result)))
            return {"error": "success"}
        except BackendError as exc:
            return {"error": str(exc)}
        finally:
            self._lib.mpv_free_node_contents(ct.byref(result))


def test_named_load_node_encoding_preserves_nested_strings():
    value = {"name": "loadfile", "url": "synthetic,comma=with space.wav", "flags": "replace",
             "options": {"start": "8", "force-media-title": "Synthetic, = unicode café"}}
    keepalive: list[Any] = []
    assert _decode_node(_encode_string_map(value, keepalive)) == value


async def _exercise(library: str, directory: Path) -> dict[str, Any]:
    tracks = (directory / "options-440.wav", directory / "plain-660.wav")
    for path, frequency in zip(tracks, (440, 660)):
        _write_tone(path, frequency)
    backend = _NamedLoadBackend(library, tracks)
    keys = ("start", "demuxer", "force-seekable", "cache")
    options = {"start": "8", "demuxer": "lavf", "force-seekable": "yes", "cache": "no"}
    try:
        version = backend.read_property("mpv-version")
        defaults = {key: backend.read_property("options/" + key) for key in keys}
        assert (await backend.loadfile(str(tracks[0]), options))["error"] == "success"
        await _eventually("named-load start position", lambda: (
            backend.read_property("path") == str(tracks[0])
            and 8 <= backend.read_property("playback-time") < 10
            and backend.read_property("current-ao") == "null"
        ))
        await backend.pause()
        assert backend.read_property("pause") is True
        position = backend.read_property("playback-time")
        actual = {key: backend.read_property("options/" + key) for key in keys}
        assert actual == {"start": "8", "demuxer": "lavf", "force-seekable": True, "cache": False}, actual
        assert backend.read_property("volume") == pytest.approx(50)
        assert backend.read_property("mute") is False
        assert (await backend.loadfile(str(tracks[1])))["error"] == "success"
        await _eventually("plain replacement resets start", lambda: (
            backend.read_property("path") == str(tracks[1])
            and 0 <= backend.read_property("playback-time") < 2
            and backend.read_property("pause") is False
        ))
        reset = {key: backend.read_property("options/" + key) for key in keys}
        assert reset == defaults, (reset, defaults)
        return {"lane": "REAL_BACKEND/null/named-load-options", "mpv_version": version,
                "applied_options": actual, "start_position": position,
                "per_file_options_reset": True, "gain_preserved": True}
    finally:
        backend.close()
        assert backend._handle is None


@pytest.mark.skipif(not os.environ.get("ORR_TEST_LIBMPV_PATH"), reason="Named native load options require ORR_TEST_LIBMPV_PATH")
def test_real_libmpv_named_load_options_and_per_file_reset(tmp_path: Path) -> None:
    library = Path(os.environ["ORR_TEST_LIBMPV_PATH"]).expanduser().resolve()
    assert library.is_file(), f"Configured libmpv does not exist: {library}"
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--probe", str(library), str(tmp_path)],
        cwd=root, env=env, text=True, capture_output=True, timeout=20, check=False,
    )
    assert completed.returncode == 0, f"Named load options failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    evidence = json.loads(completed.stdout)
    assert evidence["per_file_options_reset"] and evidence["gain_preserved"]
    print(json.dumps(evidence, sort_keys=True))


async def _exercise_short_admission(library: str, directory: Path) -> dict[str, Any]:
    """Real HTTP/SOAP with 400 ms audio, including ordinary queue EOF advance."""
    import wave
    from aiohttp import ClientSession, ClientTimeout, web
    from aiohttp.test_utils import TestServer
    from fake_linkplay.config import Config
    from fake_linkplay.http_api import HttpApi
    from fake_linkplay.queue_protocol import explicit_queue, prepare_replace, request_body, SERVICE_TYPE
    from fake_linkplay.state import PlaybackPhase, RendererState
    from test_libmpv_queue_staging import _StagingBackend

    short, long = directory / "short.wav", directory / "long.wav"
    for path, frames in ((short, 6400), (long, 32000)):
        # Generated 16 kHz stereo PCM: no external media or output device.
        with wave.open(str(path), "wb") as stream:
            stream.setparams((2, 2, 16000, 0, "NONE", "not compressed"))
            stream.writeframes(b"\x00\x04\x00\x04" * frames)
    media = web.Application()
    async def serve(request):
        return web.FileResponse({"short": short, "long": long}[request.match_info["name"]])
    media.router.add_get("/{name}.wav", serve)
    backend = _StagingBackend(library, (short, long))
    try:
        async with TestServer(media) as media_server:
            urls = [str(media_server.make_url("/short.wav")), str(media_server.make_url("/long.wav"))]
            backend._allowed_media.update(urls)
            cfg = Config(state_dir=str(directory / "state"), advertise_ip="127.0.0.1",
                         bind_address="127.0.0.1", native_queue_staging=False,
                         enable_ssdp=False, enable_mdns=False, auto_spawn_mpv=False)
            api = HttpApi(cfg, RendererState.from_config(cfg), backend)
            async with TestServer(api.app()) as server, ClientSession(timeout=ClientTimeout(total=15), trust_env=False) as client:
                async def command(value):
                    async with client.get(server.make_url("/httpapi.asp"), params={"command": value}) as response:
                        return response.status, await response.text()
                async def soap(action, body):
                    async with client.post(server.make_url("/upnp/control/PlayQueue"), data=body,
                                           headers={"SOAPACTION": '"' + SERVICE_TYPE + '#' + action + '"',
                                                    "Content-Type": "text/xml"}) as response:
                        return response.status, await response.text()

                assert await command("setPlayerCmd:play:" + urls[0]) == (200, "OK")
                assert not api.state.last_error
                await _eventually("short native EOF", lambda: backend.read_property("idle-active"))
                await api.sync_mpv_state()
                assert api.state.phase == PlaybackPhase.STOPPED and not api.state.last_error
                assert api.state.backend_url == api.state.backend_original_url == ""
                queue = explicit_queue("Short then long", urls)
                assert (await soap("ReplaceQueue", prepare_replace(queue)))[0] == 200
                backend.commands.clear()
                assert (await soap("PlayQueueWithIndex", request_body("PlayQueueWithIndex", {
                    "QueueName": queue.name, "Index": "1"})))[0] == 200
                await _eventually("ordinary queue advances once", lambda: (
                    api.state.queue.index == 2 and api.state.phase == PlaybackPhase.PLAYING), timeout=4)
                loads = [args["url"] if isinstance(args, dict) else args[1] for args in backend.commands
                         if (args.get("name") if isinstance(args, dict) else args[0]) == "loadfile"]
                assert loads == urls and not api.state.last_error
                assert backend.read_property("path") == urls[1]
                assert backend.read_property("current-ao") == "null"
                assert await command("setPlayerCmd:stop") == (200, "OK")
                assert api.state.phase == PlaybackPhase.STOPPED and backend.read_property("idle-active")
                pending = asyncio.create_task(command("setPlayerCmd:play:" + urls[0]))
                await _eventually("short source held before admission", lambda: (
                    backend.read_property("pause") is True and backend.read_property("path") == urls[0]
                    and backend.read_property("idle-active") is False))
                assert await command("setPlayerCmd:stop") == (200, "OK")
                assert await pending == (502, "Failed")
                assert api.state.phase == PlaybackPhase.STOPPED and api.state.intent == "stop"
                assert backend.read_property("idle-active") and not api.state.last_error
                return {"lane": "REAL_BACKEND/null/400ms-paused-admission",
                        "mpv_version": backend.read_property("mpv-version"),
                        "direct_http_success": True, "ordinary_queue_advanced_once": True,
                        "stop_confirmed": True, "stop_during_admission_wins": True,
                        "staged_successor_tested": False}
    finally:
        backend.close()
        assert backend._handle is None


@pytest.mark.skipif(not os.environ.get("ORR_TEST_LIBMPV_PATH"), reason="Short native admission requires ORR_TEST_LIBMPV_PATH")
def test_real_short_http_admission_and_ordinary_queue_advance(tmp_path: Path) -> None:
    library = Path(os.environ["ORR_TEST_LIBMPV_PATH"]).expanduser().resolve()
    assert library.is_file(), f"Configured libmpv does not exist: {library}"
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root) + os.pathsep + str(root / "tests")}
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--short-probe", str(library), str(tmp_path)],
        cwd=root, env=env, text=True, capture_output=True, timeout=15, check=False)
    assert completed.returncode == 0, f"Short admission failed\n{completed.stdout}\n{completed.stderr}"
    evidence = json.loads(completed.stdout)
    assert evidence["direct_http_success"] and evidence["ordinary_queue_advanced_once"] and evidence["stop_during_admission_wins"]
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 4 and sys.argv[1] in {"--probe", "--short-probe"}
    locale.setlocale(locale.LC_NUMERIC, "C")

    async def main() -> None:
        async with asyncio.timeout(15):
            probe = _exercise_short_admission if sys.argv[1] == "--short-probe" else _exercise
            print(json.dumps(await probe(sys.argv[2], Path(sys.argv[3])), sort_keys=True))

    asyncio.run(main())
