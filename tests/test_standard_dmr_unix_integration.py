"""Strict standard UPnP client over real HTTP/GENA and production Unix IPC.

ORR_TEST_DMR_UNIX=1 enables the unchanged shared standard-client flow. Missing
client/mpv/Unix IPC fails after opt-in; never substitutes the libmpv transport.
Generated loopback WAVs, null output, both advertised profiles and owned cleanup.
No physical audio, multicast discovery or handset interoperability claim.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

import pytest

from fake_linkplay.mpv_backend import MpvBackend
from standard_dmr_support import exercise_standard_dmr
from test_mpv_unix_integration import _external_player, _write_tone
from test_pywiim_unix_integration import require_unix_ipc
from tools.native_probe_supervisor import run_probe


@pytest.mark.skipif(os.environ.get("ORR_TEST_DMR_UNIX") != "1",
                   reason="Standard DMR/Unix IPC requires ORR_TEST_DMR_UNIX=1")
def test_standard_dmr_production_unix_ipc() -> None:
    assert sys.platform == "linux", "Standard DMR/Unix IPC requires Linux"
    from async_upnp_client.profiles.dlna import DmrDevice  # noqa: F401: opt-in dependency must fail
    executable = shutil.which(os.environ.get("ORR_TEST_MPV_BINARY", "mpv"))
    assert executable is not None, "ORR_TEST_DMR_UNIX=1 requires a real mpv executable"
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root) + os.pathsep + str(root / "tests")}
    result = run_probe([sys.executable, str(Path(__file__).resolve()), "--probe", str(Path(executable).resolve())],
                       cwd=root, env=env, timeout=25)
    assert result.returncode == 0, f"Standard DMR/production Unix IPC failed\n{result.stdout}\n{result.stderr}"
    evidence = json.loads(result.stdout)
    assert evidence["lane"] == "STANDARD_DMR_PRODUCTION_UNIX_IPC/null"
    assert evidence["fixture_children_reaped"] == 2
    assert [item["wiim_home_profile"] for item in evidence["profiles"]] == [False, True]
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 3 and sys.argv[1] == "--probe"

    async def main():
        require_unix_ipc()
        profiles = []
        async with asyncio.timeout(20):
            with tempfile.TemporaryDirectory(prefix="orr-standard-dmr-") as temporary:
                for profile in (False, True):
                    directory = Path(temporary) / str(profile)
                    directory.mkdir()
                    tracks = (directory / "first-440.wav", directory / "second-660.wav")
                    for path, frequency in zip(tracks, (440, 660)):
                        _write_tone(path, frequency)
                    async with _external_player(directory, sys.argv[2]) as (backend, process):
                        assert type(backend) is MpvBackend, "Production transport must not be substituted"
                        profiles.append(await exercise_standard_dmr(backend, directory, tracks, wiim_home_profile=profile))
                        assert process.returncode is None, "Renderer must preserve externally owned mpv"
                    assert process.returncode is not None
        print(json.dumps({"lane": "STANDARD_DMR_PRODUCTION_UNIX_IPC/null",
                          "fixture_children_reaped": 2, "profiles": profiles}, sort_keys=True))

    asyncio.run(main())
