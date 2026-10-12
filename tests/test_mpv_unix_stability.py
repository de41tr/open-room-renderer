"""Distinct opt-in production Unix-IPC/null stress with separate child metrics.

ORR_TEST_UNIX_STABILITY=1 PYTHONPATH=. .venv/bin/pytest -q -s \
    tests/test_mpv_unix_stability.py

Defaults to 180 seconds across three fixture-owned mpv child lifetimes. Set
ORR_TEST_UNIX_STABILITY_SECONDS=180..900 to change the bounded window. Native
commands/readback use unmodified production MpvBackend Unix JSON IPC. Public
mpv start-file/path events independently count loads/source changes. The same
HTTP/SOAP/GENA/fault/control flow is shared with libmpv stability acceptance.
Controller and child FD/RSS/thread counts are separate. Whole-group supervision
bounds blocked native children, signals and abnormal worker exits.

Explicit opt-in fails if Linux, mpv or Unix IPC is unavailable; no libmpv
fallback. Generated local WAV/loopback fixtures only. This is not hardware,
physical LAN, audible output, third-party client or indefinite soak evidence.
Pending-retry Pause cancellation is covered by the separate libmpv recovery
lane; this Unix stress flow covers Stop and direct-source cancellation.
"""
from __future__ import annotations

import asyncio
import json
import locale
import os
from pathlib import Path
import shutil
import sys

import pytest

from test_libmpv_stability import freeze_sources, source_digest
from tools.native_probe_supervisor import run_probe


@pytest.mark.skipif(os.environ.get("ORR_TEST_UNIX_STABILITY") != "1",
                    reason="Native Unix stress requires ORR_TEST_UNIX_STABILITY=1")
def test_production_unix_ipc_repeated_playback_stability(tmp_path: Path) -> None:
    assert sys.platform == "linux" and Path("/proc/self/status").is_file(), "Explicit Unix stability lane requires Linux /proc"
    executable = shutil.which(os.environ.get("ORR_TEST_MPV_BINARY", "mpv"))
    assert executable is not None, "Explicit Unix stability lane requires real mpv"
    seconds = int(os.environ.get("ORR_TEST_UNIX_STABILITY_SECONDS", "180"))
    assert 180 <= seconds <= 900, "Unix stability duration must be 180..900 seconds"
    root = Path(__file__).resolve().parents[1]
    snapshot, media = tmp_path / "snapshot", tmp_path / "media"
    media.mkdir()
    digest = freeze_sources(root, snapshot, ("tests/native_stability_transport.py", "tests/test_mpv_unix_integration.py",
                                            "tests/test_mpv_unix_stability.py", "tools/native_probe_supervisor.py"))
    env = os.environ.copy()
    env["PYTHONPATH"] = str(snapshot) + os.pathsep + str(snapshot / "tests")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for name in tuple(env):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            env.pop(name)
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    completed = run_probe([sys.executable, str(snapshot / "tests/test_mpv_unix_stability.py"), "--probe",
                           str(Path(executable).resolve()), str(media), str(seconds), digest],
                          cwd=snapshot, env=env, capture_output=True, timeout=seconds + 120)
    assert completed.returncode == 0, f"Production Unix stability failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    evidence = json.loads(completed.stdout)
    assert evidence["lane"] == "PRODUCTION_UNIX_IPC/null bounded stability"
    assert evidence["source_sha256"] == digest == source_digest(snapshot)
    assert evidence["elapsed_seconds"] >= seconds
    from libmpv_stability_support import validate_evidence
    validate_evidence(evidence)
    for epoch in evidence["epochs"]:
        assert epoch["samples"] >= 5 and epoch["natural_eofs"] >= 1
        assert epoch["child_resources"]["max"]["children"] == 0
        assert epoch["live_max"]["children"] == 1
        assert epoch["after_cleanup"]["children"] == 0
        assert epoch["cleanup"]["fixture_child_reaped"] and epoch["cleanup"]["unix_socket_removed"]
        assert epoch["cleanup"]["event_observer_closed"]
    assert evidence["totals"]["inflight_gain_retries"] >= 6
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 6 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")
    from libmpv_stability_support import exercise

    async def main() -> None:
        root = Path(__file__).resolve().parents[1]
        import fake_linkplay.http_api as production
        assert Path(production.__file__).resolve().is_relative_to(root)
        assert source_digest(root) == sys.argv[5]
        async with asyncio.timeout(int(sys.argv[4]) + 90):
            result = await exercise(sys.argv[2], Path(sys.argv[3]), int(sys.argv[4]), mode="unix")
        result["source_sha256"] = source_digest(root)
        result["production_sha256"] = source_digest(root / "fake_linkplay")
        assert result["source_sha256"] == sys.argv[5]
        print(json.dumps(result, sort_keys=True), flush=True)

    asyncio.run(main())
