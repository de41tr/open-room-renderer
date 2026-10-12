"""Opt-in repeated native playback and lifecycle acceptance, frozen per run.

ORR_TEST_LIBMPV_PATH=/path/to/libmpv.so.2 ORR_TEST_LIBMPV_STABILITY=1 \
    PYTHONPATH=. .venv/bin/pytest -q -s tests/test_libmpv_stability.py

Defaults to a 60-second minimum bounded stress check, extended when needed to
finish three lifetimes of at least eight cycles (including seek-assisted EOF).
For a longer local run, set
ORR_TEST_LIBMPV_STABILITY_SECONDS=900 (allowed range 30..3600). The whole
interpreter is killed and reaped on timeout, including a blocked native C call.
Each run copies all executed production/helper Python into a private snapshot,
checks its SHA-256 before/after, and reports the aggregate digest. Only generated
WAVs and loopback HTTP are used; evidence contains aggregate metrics, no media.

This is real decoding/null-output control acceptance through a test adapter.
It does not test production Unix JSON IPC, audible hardware, Pi/Android, WAN
stations, physical networks, indefinite leak freedom or a hardware soak.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import locale
import os
from pathlib import Path
import sys

import pytest

from tools.native_probe_supervisor import run_probe


def source_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def freeze_sources(source: Path, target: Path, extras: tuple[str, ...] = ()) -> str:
    helpers = ("test_libmpv_stability.py", "libmpv_stability_support.py", "test_libmpv_integration.py")
    paths = [*sorted((source / "fake_linkplay").rglob("*.py")), *(source / "tests" / name for name in helpers), *(source / name for name in extras)]
    for path in paths:
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(path.read_bytes())
    manifest = {path.relative_to(target).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(target.rglob("*.py"))}
    (target / "snapshot-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return source_digest(target)


def test_stability_snapshot_digest_detects_changed_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "fixture.py").write_text("SYNTHETIC = 1\n")
    first = source_digest(source)
    (source / "fixture.py").write_text("SYNTHETIC = 2\n")
    assert first != source_digest(source)


def test_stability_snapshot_is_independent_of_live_checkout(tmp_path: Path) -> None:
    source, frozen = tmp_path / "live", tmp_path / "frozen"
    paths = ("fake_linkplay/__init__.py", "fake_linkplay/fixture.py", "tests/test_libmpv_stability.py",
             "tests/libmpv_stability_support.py", "tests/test_libmpv_integration.py")
    for name in paths:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("SYNTHETIC = 1\n")
    digest = freeze_sources(source, frozen)
    (source / paths[1]).write_text("SYNTHETIC = 2\n")
    assert source_digest(frozen) == digest
    assert json.loads((frozen / "snapshot-manifest.json").read_text())[paths[1]] == hashlib.sha256(b"SYNTHETIC = 1\n").hexdigest()


def test_stability_budgets_reject_resource_growth_and_children() -> None:
    from libmpv_stability_support import LIVE_BUDGET, within_budget
    baseline = {"tasks": 3, "fds": 10, "rss_kib": 100000, "threads": 8, "children": 0}
    within_budget(dict(baseline), baseline, LIVE_BUDGET)
    for key, allowance in {**LIVE_BUDGET, "children": 0}.items():
        with pytest.raises(AssertionError):
            within_budget({**baseline, key: baseline[key] + allowance + 1}, baseline, LIVE_BUDGET)


@pytest.mark.skipif(os.environ.get("ORR_TEST_LIBMPV_STABILITY") != "1",
                    reason="Bounded native stability requires ORR_TEST_LIBMPV_STABILITY=1")
def test_real_libmpv_repeated_playback_stability(tmp_path: Path) -> None:
    assert sys.platform == "linux" and Path("/proc/self/status").is_file(), "Explicit native stability lane requires Linux /proc metrics"
    value = os.environ.get("ORR_TEST_LIBMPV_PATH", "")
    assert value, "ORR_TEST_LIBMPV_STABILITY=1 also requires ORR_TEST_LIBMPV_PATH"
    library = Path(value).expanduser().resolve()
    assert library.is_file(), "Configured libmpv does not exist"
    seconds = int(os.environ.get("ORR_TEST_LIBMPV_STABILITY_SECONDS", "60"))
    assert 30 <= seconds <= 3600, "Native stability duration must be 30..3600 seconds"
    root = Path(__file__).resolve().parents[1]
    snapshot, media = tmp_path / "snapshot", tmp_path / "media"
    media.mkdir()
    digest = freeze_sources(root, snapshot, ("tools/native_probe_supervisor.py",))
    env = os.environ.copy()
    # A fresh interpreter imports this exact snapshot even if another worker
    # edits the live checkout or an editable install points to the live root.
    env["PYTHONPATH"] = str(snapshot) + os.pathsep + str(snapshot / "tests")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for name in tuple(env):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            env.pop(name)
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    completed = run_probe([sys.executable, str(snapshot / "tests/test_libmpv_stability.py"), "--probe",
                                str(library), str(media), str(seconds), digest], cwd=snapshot, env=env,
                               capture_output=True, timeout=seconds + 120)
    assert completed.returncode == 0, f"Native stability failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    evidence = json.loads(completed.stdout)
    assert evidence["source_sha256"] == digest == source_digest(snapshot), "Frozen sources changed during acceptance"
    assert evidence["elapsed_seconds"] >= seconds
    from libmpv_stability_support import validate_evidence
    validate_evidence(evidence)
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 6 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")
    from libmpv_stability_support import exercise

    async def main() -> None:
        root = Path(__file__).resolve().parents[1]
        import fake_linkplay.http_api as production
        assert Path(production.__file__).resolve().is_relative_to(root), "Probe imported production code outside its snapshot"
        assert source_digest(root) == sys.argv[5]
        async with asyncio.timeout(int(sys.argv[4]) + 90):
            result = await exercise(sys.argv[2], Path(sys.argv[3]), int(sys.argv[4]))
        result["source_sha256"] = source_digest(root)
        result["production_sha256"] = source_digest(root / "fake_linkplay")
        assert result["source_sha256"] == sys.argv[5]
        print(json.dumps(result, sort_keys=True), flush=True)

    asyncio.run(main())
