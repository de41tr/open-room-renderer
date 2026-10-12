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


if __name__ == "__main__":
    assert len(sys.argv) == 4 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")

    async def main() -> None:
        async with asyncio.timeout(15):
            print(json.dumps(await _exercise(sys.argv[2], Path(sys.argv[3])), sort_keys=True))

    asyncio.run(main())
