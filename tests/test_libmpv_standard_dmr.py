"""Strict standard client -> real HTTP/SOAP/GENA -> libmpv/null acceptance.

Opt in with ORR_TEST_LIBMPV_PATH. Two description profiles bind unchanged with
async-upnp-client, subscribe fully, issue typed commands and receive library-
parsed LastChange BEFORE polling. Two generated HTTP WAVs must reach distinct
native entries with selected PCM and advancing playback; controls read native
properties. Only the command transport is the existing test ctypes adapter.
No production Unix IPC, physical audio, discovery or handset claim.
"""
from __future__ import annotations

import asyncio
import json
import locale
import os
from pathlib import Path
import subprocess
import sys

import pytest

from standard_dmr_support import exercise_standard_dmr
from test_libmpv_integration import _write_tone
from test_libmpv_queue_staging import _StagingBackend


@pytest.mark.skipif(not os.environ.get("ORR_TEST_LIBMPV_PATH"),
                   reason="Standard DMR native acceptance requires ORR_TEST_LIBMPV_PATH")
def test_standard_dmr_real_libmpv(tmp_path: Path) -> None:
    library = Path(os.environ["ORR_TEST_LIBMPV_PATH"]).expanduser().resolve()
    assert library.is_file(), f"Configured libmpv does not exist: {library}"
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root) + os.pathsep + str(root / "tests")}
    completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--probe", str(library), str(tmp_path)],
        cwd=root, env=env, text=True, capture_output=True, timeout=25, check=False)
    assert completed.returncode == 0, f"Standard DMR/libmpv failed\n{completed.stdout}\n{completed.stderr}"
    evidence = json.loads(completed.stdout)
    assert evidence["lane"] == "STANDARD_DMR_REAL_LIBMPV/null"
    assert evidence["native_handles_destroyed"] is True
    assert [item["wiim_home_profile"] for item in evidence["profiles"]] == [False, True]
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 4 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")

    async def main():
        profiles = []
        async with asyncio.timeout(20):
            for profile in (False, True):
                directory = Path(sys.argv[3]) / str(profile)
                directory.mkdir()
                tracks = (directory / "first-440.wav", directory / "second-660.wav")
                for path, frequency in zip(tracks, (440, 660)):
                    _write_tone(path, frequency)
                backend = _StagingBackend(sys.argv[2], tracks)
                try:
                    profiles.append(await exercise_standard_dmr(backend, directory, tracks, wiim_home_profile=profile))
                finally:
                    backend.close()
                    assert backend._handle is None and backend.process is None
        print(json.dumps({"lane": "STANDARD_DMR_REAL_LIBMPV/null", "command_transport": "test ctypes command-node; not Unix IPC",
                          "native_handles_destroyed": True, "profiles": profiles}, sort_keys=True))

    asyncio.run(main())
