"""Real local files/concurrent creators; faults are synthetic, not power loss."""
from concurrent.futures import ThreadPoolExecutor
import errno
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time
from uuid import UUID

import pytest

from fake_linkplay import config


FIRST = "00000000-0000-4000-8000-000000000001"
SECOND = "00000000-0000-4000-8000-000000000002"


class InterruptedWrite:
    """Expose a prefix on the real opened inode before finishing/failing."""

    def __init__(self, stream, count, pause=None):
        self.stream = stream
        self.count = count
        self.pause = pause

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *exc):
        return self.stream.__exit__(*exc)

    def write(self, value):
        self.stream.write(value[:self.count])
        self.stream.flush()
        os.fsync(self.stream.fileno())
        if self.pause is None:
            raise OSError(errno.ENOSPC, "synthetic interrupted UUID write")
        started, release = self.pause
        started.set()
        assert release.wait(10), "test did not release its writer"
        self.stream.write(value[self.count:])
        return len(value)


def intercept_first_write(monkeypatch, count, pause=None):
    # Both the original direct-publication and fixed staged-write paths receive
    # the same partial write on an actual local file descriptor.
    original_open, original_fdopen = Path.open, os.fdopen
    selected = False
    lock = threading.Lock()

    def wrap(stream):
        nonlocal selected
        with lock:
            if selected:
                return stream
            selected = True
        return InterruptedWrite(stream, count, pause)

    def path_open(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return wrap(stream) if mode == "x" else stream

    def fdopen(fd, mode="r", *args, **kwargs):
        stream = original_fdopen(fd, mode, *args, **kwargs)
        return wrap(stream) if mode == "w" else stream

    monkeypatch.setattr(Path, "open", path_open)
    monkeypatch.setattr(os, "fdopen", fdopen)


@pytest.mark.parametrize("prefix", [0, 7])
def test_interrupted_write_never_publishes_empty_or_truncated_identity(tmp_path, monkeypatch, prefix):
    monkeypatch.setattr(config.uuidlib, "uuid4", lambda: UUID(FIRST))
    intercept_first_write(monkeypatch, prefix)
    with pytest.raises(OSError, match="synthetic interrupted UUID write"):
        config.load_or_create_uuid(tmp_path)
    assert not (tmp_path / "uuid").exists()
    assert list(tmp_path.iterdir()) == []
    assert config.load_or_create_uuid(tmp_path) == FIRST
    assert (tmp_path / "uuid").read_text() == FIRST + "\n"


@pytest.mark.parametrize("prefix", [0, 7])
def test_reader_during_incomplete_write_publishes_and_keeps_complete_winner(tmp_path, monkeypatch, prefix):
    identities = iter([UUID(FIRST), UUID(SECOND)])
    monkeypatch.setattr(config.uuidlib, "uuid4", lambda: next(identities))
    started, release = threading.Event(), threading.Event()
    original_sync = os.fsync
    directory_syncs = []

    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_syncs.append(threading.get_ident())
        return original_sync(fd)

    monkeypatch.setattr(os, "fsync", sync)
    intercept_first_write(monkeypatch, prefix, (started, release))
    with ThreadPoolExecutor(max_workers=1) as executor:
        creator = executor.submit(config.load_or_create_uuid, tmp_path)
        try:
            assert started.wait(10), "creator did not start writing"
            assert not (tmp_path / "uuid").exists()
            winner = config.load_or_create_uuid(tmp_path)
            assert winner == SECOND
            assert (tmp_path / "uuid").read_text() == SECOND + "\n"
        finally:
            release.set()
        assert creator.result(timeout=10) == SECOND
    assert [path.name for path in tmp_path.iterdir()] == ["uuid"]
    assert len(set(directory_syncs)) == 2  # Winner and losing creator both sync.
    assert config.load_or_create_uuid(tmp_path) == SECOND


@pytest.mark.parametrize("profile", [False, True])
def test_real_competing_processes_converge_without_overwriting_winner(tmp_path, profile):
    state, ready, gate = tmp_path / "state", tmp_path / "ready", tmp_path / "gate"
    state.mkdir()
    ready.mkdir()
    code = r'''
import pathlib, sys, time, uuid
from fake_linkplay import config
state, ready, gate, number, profile = sys.argv[1:]
identity = uuid.UUID(f"00000000-0000-4000-8000-{int(number):012x}")
def propose():
    pathlib.Path(ready, number).write_text(str(identity))
    deadline = time.monotonic() + 15
    while not pathlib.Path(gate).exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("parent did not release creators")
        time.sleep(0.005)
    return identity
config.uuidlib.uuid4 = propose
print(config.load_or_create_uuid(state, wiim_home_profile=profile == "True"), flush=True)
'''
    children = []
    try:
        for number in range(1, 9):
            children.append(subprocess.Popen(
                [sys.executable, "-c", code, str(state), str(ready), str(gate), str(number), str(profile)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))
        deadline = time.monotonic() + 15
        while len(list(ready.iterdir())) < len(children):
            assert time.monotonic() < deadline, "creators did not reach the start barrier"
            assert all(child.poll() is None for child in children), "creator failed before the barrier"
            time.sleep(0.01)
        assert len({p.read_text() for p in ready.iterdir()}) == len(children)
        gate.touch()
        results = [child.communicate(timeout=15) for child in children]
        assert all(child.returncode == 0 for child in children), results
        values = [stdout.strip() for stdout, _ in results]
        assert len(set(values)) == 1
        value = values[0]
        assert value in {
            "FF00" + UUID(p.read_text()).hex.upper() if profile else p.read_text()
            for p in ready.iterdir()
        }
        assert (state / "uuid").read_text() == value + "\n"
        assert config.load_or_create_uuid(state, wiim_home_profile=not profile) == value
        assert [path.name for path in state.iterdir()] == ["uuid"]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)


@pytest.mark.parametrize("stage", ["file_sync", "link"])
def test_prepublication_failure_has_no_direct_write_fallback(tmp_path, monkeypatch, stage):
    original_sync = os.fsync

    def fail_file_sync(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "synthetic file-sync failure")
        return original_sync(fd)

    def fail_link(source, target):
        assert Path(source).parent == tmp_path
        assert Path(source).read_text().endswith("\n")
        assert not Path(target).exists()
        raise OSError(errno.EOPNOTSUPP, "synthetic unsupported hard links")

    if stage == "file_sync":
        monkeypatch.setattr(os, "fsync", fail_file_sync)
    else:
        monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(OSError):
        config.load_or_create_uuid(tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("error_type", [OSError, ValueError])
def test_fdopen_failure_closes_raw_descriptor_and_removes_private_file(tmp_path, monkeypatch, error_type):
    opened = []

    def fail_fdopen(descriptor, *args, **kwargs):
        opened.append(descriptor)
        raise error_type("synthetic fdopen failure")

    monkeypatch.setattr(os, "fdopen", fail_fdopen)
    try:
        with pytest.raises(error_type, match="synthetic fdopen failure"):
            config.load_or_create_uuid(tmp_path)
        assert list(tmp_path.iterdir()) == []
        assert len(opened) == 1
        with pytest.raises(OSError) as closed:
            os.fstat(opened[0])
        assert closed.value.errno == errno.EBADF
    finally:
        # A failing regression must not leave its deliberately exposed FD open.
        for descriptor in opened:
            try:
                os.close(descriptor)
            except OSError as exc:
                assert exc.errno == errno.EBADF


def test_raw_descriptor_close_failure_prevents_publication(tmp_path, monkeypatch):
    original_close = os.close
    closed = []

    def fail_close(descriptor):
        regular = stat.S_ISREG(os.fstat(descriptor).st_mode)
        original_close(descriptor)
        if regular:
            closed.append(descriptor)
            raise OSError(errno.EIO, "synthetic raw-close failure")

    monkeypatch.setattr(os, "close", fail_close)
    with pytest.raises(OSError, match="synthetic raw-close failure"):
        config.load_or_create_uuid(tmp_path)
    assert list(tmp_path.iterdir()) == []
    assert len(closed) == 1
    with pytest.raises(OSError) as unavailable:
        os.fstat(closed[0])
    assert unavailable.value.errno == errno.EBADF


def test_staging_cleanup_precedes_directory_sync(tmp_path, monkeypatch):
    original_sync = os.fsync
    calls = []

    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            calls.append("directory")
            assert [path.name for path in tmp_path.iterdir()] == ["uuid"]
        else:
            calls.append("file")
            assert not (tmp_path / "uuid").exists()
        return original_sync(fd)

    monkeypatch.setattr(os, "fsync", sync)
    config.load_or_create_uuid(tmp_path)
    assert calls == ["file", "directory"]


@pytest.mark.parametrize("stage", ["directory_open", "directory_sync", "temporary_unlink"])
def test_postpublication_failure_preserves_complete_identity_on_restart(tmp_path, monkeypatch, stage):
    monkeypatch.setattr(config.uuidlib, "uuid4", lambda: UUID(FIRST))
    original_open, original_sync, original_unlink = os.open, os.fsync, Path.unlink

    def fail_open(path, flags, *args, **kwargs):
        if Path(path) == tmp_path and flags & os.O_DIRECTORY:
            raise OSError(errno.EIO, "synthetic directory-open failure")
        return original_open(path, flags, *args, **kwargs)

    def fail_sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "synthetic directory-sync failure")
        return original_sync(fd)

    def fail_unlink(path, *args, **kwargs):
        if path.name.startswith(".uuid."):
            raise OSError(errno.EIO, "synthetic temporary-unlink failure")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        if stage == "directory_open":
            fault.setattr(os, "open", fail_open)
        elif stage == "directory_sync":
            fault.setattr(os, "fsync", fail_sync)
        else:
            fault.setattr(Path, "unlink", fail_unlink)
        with pytest.raises(OSError, match="UUID published; directory durability unconfirmed"):
            config.load_or_create_uuid(tmp_path)
    assert (tmp_path / "uuid").read_text() == FIRST + "\n"
    monkeypatch.setattr(config.uuidlib, "uuid4", lambda: pytest.fail("published UUID must not be regenerated"))
    assert config.load_or_create_uuid(tmp_path) == FIRST
    assert (tmp_path / "uuid").read_text() == FIRST + "\n"


@pytest.mark.parametrize("saved", [
    " UuId: 00000000-0000-4000-8000-000000000001 \n",
    "uuid: ff0000000000000040008000000000000001\n",
    "legacy-compact-id\n", "abc", "FF123456",
])
def test_existing_nonempty_identity_never_rewritten_or_revalidated(tmp_path, monkeypatch, saved):
    path = tmp_path / "uuid"
    path.write_text(saved)
    original = path.stat()
    monkeypatch.setattr(config.uuidlib, "uuid4", lambda: pytest.fail("existing identity must not be regenerated"))
    monkeypatch.setattr(os, "link", lambda *args: pytest.fail("existing identity must not be republished"))
    for profile in (False, True):
        assert config.load_or_create_uuid(tmp_path, wiim_home_profile=profile) == config.normalize_uuid(saved)
        assert path.read_text() == saved
        assert path.stat().st_ino == original.st_ino
        assert path.stat().st_mtime_ns == original.st_mtime_ns


@pytest.mark.parametrize("saved", ["", " \nuUiD: \t"])
def test_existing_empty_identity_fails_without_generating_replacement(tmp_path, monkeypatch, saved):
    path = tmp_path / "uuid"
    path.write_text(saved)
    monkeypatch.setattr(config.uuidlib, "uuid4", lambda: pytest.fail("empty identity must not be regenerated"))
    with pytest.raises(ValueError, match="Persisted UUID is empty"):
        config.load_or_create_uuid(tmp_path)
    assert path.read_text() == saved


@pytest.mark.parametrize("target", ["empty", "directory", "dangling_symlink"])
def test_conflicting_destination_is_preserved_and_fails_without_recursive_retry(tmp_path, monkeypatch, target):
    original_link = os.link
    destination = tmp_path / "uuid"

    def competing_link(source, path):
        if target == "empty":
            destination.touch()
        elif target == "directory":
            destination.mkdir()
        else:
            destination.symlink_to(tmp_path / "missing")
        return original_link(source, path)

    monkeypatch.setattr(os, "link", competing_link)
    with pytest.raises((OSError, ValueError)):
        config.load_or_create_uuid(tmp_path)
    assert [path.name for path in tmp_path.iterdir()] == ["uuid"]
    if target == "empty":
        assert destination.read_text() == ""
    elif target == "directory":
        assert destination.is_dir()
    else:
        assert destination.is_symlink() and not destination.exists()


def test_abandoned_private_candidate_is_not_adopted_or_removed(tmp_path, monkeypatch):
    candidate = tmp_path / ".uuid.abandoned"
    candidate.write_text("0000000")
    monkeypatch.setattr(config.uuidlib, "uuid4", lambda: UUID(SECOND))
    assert config.load_or_create_uuid(tmp_path) == SECOND
    assert (tmp_path / "uuid").read_text() == SECOND + "\n"
    assert candidate.read_text() == "0000000"


def test_explicit_configured_identity_bypasses_even_an_empty_persisted_id(tmp_path, monkeypatch):
    path = tmp_path / "uuid"
    path.write_text("")
    settings = tmp_path / "config.yaml"
    settings.write_text(f"state_dir: {tmp_path}\nuuid: 'UuId: {FIRST}'\n")
    monkeypatch.setattr(config.uuidlib, "uuid4", lambda: pytest.fail("explicit identity must not be regenerated"))
    assert config.load_config(settings).uuid == FIRST
    assert path.read_text() == ""
