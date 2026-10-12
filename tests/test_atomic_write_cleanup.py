"""Atomic replacement cleanup must not change the recorded commit outcome."""
from pathlib import Path
import os

import pytest

from fake_linkplay.persistence import AtomicWriteCommittedError, atomic_write_text


def test_successful_replace_does_not_unlink_already_removed_temporary(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    atomic_write_text(path, "original")
    def fail_unlink(*_args, **_kwargs):
        raise OSError("synthetic cleanup failure")
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    atomic_write_text(path, "replacement")
    assert path.read_text() == "replacement"
    assert list(tmp_path.iterdir()) == [path]


def test_precommit_failure_still_removes_temporary_and_preserves_previous(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    atomic_write_text(path, "original")
    def fail_replace(*_args):
        raise OSError("synthetic replace failure")
    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failure"):
        atomic_write_text(path, "replacement")
    assert path.read_text() == "original"
    assert list(tmp_path.iterdir()) == [path]


def test_postcommit_fsync_failure_remains_committed_even_if_unlink_would_fail(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    atomic_write_text(path, "original")
    original = os.fsync
    calls = 0
    def fail_directory_fsync(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic directory fsync failure")
        return original(descriptor)
    def fail_unlink(*_args, **_kwargs):
        raise OSError("synthetic cleanup failure")
    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(AtomicWriteCommittedError):
        atomic_write_text(path, "replacement")
    assert path.read_text() == "replacement"
    assert list(tmp_path.iterdir()) == [path]
