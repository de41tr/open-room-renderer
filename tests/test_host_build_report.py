"""Bounded native phase receipts survive staging failure/timeout; no network."""
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import types
import pytest
from fake_linkplay import host_release as releases

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize("timeout,exit_code,reason", [(False, 2, "installer_failed"), (True, -15, "installer_timeout")])
def test_failed_prepare_keeps_last_phase_before_cleanup(tmp_path, monkeypatch, timeout, exit_code, reason):
    base = tmp_path / "native"
    for name in ("builds", "management", "releases", "current-release"):
        (base / name).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(releases.g, "BASE", base)
    monkeypatch.setattr(releases, "private", lambda p: None)
    # pytest tmp_path may have a shared /tmp ancestor: fixture I/O only.
    monkeypatch.setattr(releases.g, "document", lambda p: json.loads(Path(p).read_text()))
    monkeypatch.setattr(releases, "run", lambda *a, **k: None)
    monkeypatch.setattr(releases, "descriptor", lambda raw: {"layout": releases.g.LAYOUT})
    # Only the fixture's known descriptor read is mocked; progress file is real.
    original_read = Path.read_bytes
    def read(path):
        if path.name == "host_components.json":
            return b"{}"
        return original_read(path)
    monkeypatch.setattr(Path, "read_bytes", read)
    calls, targets = [], []
    class Process:
        pid = 876543
        returncode = None
        def __init__(self, argv, **kw):
            assert argv[0] == "/usr/bin/python3" and argv[-2] == "--prefix"
            assert kw["start_new_session"] and kw["stderr"] == subprocess.DEVNULL
            target = Path(argv[-1]); target.mkdir(mode=0o700); targets.append(target)
            stamp = dt.datetime.now(dt.timezone.utc).isoformat()
            (target / "build-progress.json").write_text(json.dumps({
                "schema_version": 1, "phase": "pure_dependencies", "phase_started_at": stamp,
                "untrusted": "SECRET_URL_MARKER"}))
            (target / "build-progress.json").chmod(0o600)
        def wait(self, timeout):
            calls.append(timeout)
            if len(calls) == 1 and globals_timeout:
                raise subprocess.TimeoutExpired("fixed", 660)
            self.returncode = exit_code
            return exit_code
    globals_timeout = timeout
    monkeypatch.setattr(releases.subprocess, "Popen", Process)
    monkeypatch.setattr(releases.os, "killpg", lambda pid, sig: calls.append((pid, int(sig))))
    report = {}
    with pytest.raises(releases.Refused, match=reason):
        releases.prepare({"commit": "a" * 40}, {"identity": (123,), "release": base / "current-release"}, report)
    assert report["build"]["phase"] == "pure_dependencies"
    assert report["build"]["result"] == reason
    assert report["build"]["finished_at"] and report["build"]["duration_seconds"] >= 0
    assert report["build"]["signal"] == (15 if timeout else None)
    assert report["build"]["exit_code"] == (None if timeout else 2)
    assert calls[0] == 660 and "SECRET_URL_MARKER" not in json.dumps(report)
    assert targets and not targets[0].exists() and not list((base / "builds").iterdir())
    if timeout:
        assert (876543, int(signal.SIGTERM)) in calls

def test_installer_progress_is_private_fixed_data(tmp_path):
    spec = importlib.util.spec_from_file_location("native_installer", ROOT / "scripts/install-native.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    progress = module.BuildProgress(tmp_path)
    progress.mark("venv"); progress.mark("yaml"); progress.mark("yaml", "installer_failed")
    data = json.loads((tmp_path / "build-progress.json").read_text())
    assert data["phase"] == "yaml" and data["result"] == "installer_failed"
    assert data["started_at"] <= data["phase_started_at"] <= data["updated_at"]
    assert (tmp_path / "build-progress.json").stat().st_mode & 0o777 == 0o600
    assert set(data) == {"schema_version", "phase", "result", "started_at", "updated_at", "duration_seconds", "phase_started_at"}
