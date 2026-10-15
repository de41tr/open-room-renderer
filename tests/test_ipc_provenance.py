"""Filesystem/UNIT checks; synthetic socket nodes are not listening IPC peers."""
import asyncio
import errno
import os
import stat

import pytest

from fake_linkplay import ipc_provenance
from fake_linkplay.ipc_provenance import SocketProvenance
from fake_linkplay.mpv_backend import MpvBackend
from test_backend_lifecycle import FakeIpc, FakeProcess

OLD_BOOT = "11111111-1111-4111-8111-111111111111"
NEW_BOOT = "22222222-2222-4222-8222-222222222222"


def recorded(tmp_path, monkeypatch):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "mpv.sock"
    os.mknod(path, stat.S_IFSOCK | 0o600)
    proof = SocketProvenance(str(path))
    monkeypatch.setattr(ipc_provenance, "boot_id", lambda: OLD_BOOT)
    monkeypatch.setattr(ipc_provenance, "host_id", lambda: "a" * 64)
    proof.remember(proof.snapshot())
    receipt, = directory.glob(".orr-mpv-*.json")
    return path, proof, receipt


def test_recover_exact_owned_socket_only_after_boot_changes(tmp_path, monkeypatch):
    path, proof, receipt = recorded(tmp_path, monkeypatch)
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert not SocketProvenance(str(path)).recover_previous_boot()
    assert path.exists() and receipt.exists()
    monkeypatch.setattr(ipc_provenance, "boot_id", lambda: NEW_BOOT)
    recovered = SocketProvenance(str(path))
    with recovered.startup_guard() as locked:
        assert locked and recovered.recover_previous_boot()
    assert not path.exists() and not receipt.exists()
    # A pre-fix or otherwise unmarked stale entry is never adopted.
    os.mknod(path, stat.S_IFSOCK | 0o600)
    assert not recovered.recover_previous_boot()
    assert path.exists()


@pytest.mark.parametrize("change", ["same_boot", "no_boot", "other_host", "socket", "socket_mode", "parent_mode",
                                      "receipt_mode", "receipt_symlink", "receipt_fifo", "malformed", "duplicate"])
def test_missing_or_changed_proof_preserves_existing_paths(tmp_path, monkeypatch, change):
    path, proof, receipt = recorded(tmp_path, monkeypatch)
    monkeypatch.setattr(ipc_provenance, "boot_id", lambda: NEW_BOOT)
    if change == "same_boot":
        monkeypatch.setattr(ipc_provenance, "boot_id", lambda: OLD_BOOT)
    elif change == "no_boot":
        monkeypatch.setattr(ipc_provenance, "boot_id", lambda: None)
    elif change == "other_host":
        monkeypatch.setattr(ipc_provenance, "host_id", lambda: "b" * 64)
    elif change == "socket":
        path.unlink()
        os.mknod(path, stat.S_IFSOCK | 0o600)
    elif change == "socket_mode":
        path.chmod(0o640)
    elif change == "parent_mode":
        path.parent.chmod(0o755)
    elif change == "receipt_mode":
        receipt.chmod(0o644)
    elif change == "receipt_symlink":
        saved = receipt.with_name("unrelated")
        receipt.rename(saved)
        receipt.symlink_to(saved)
    elif change == "receipt_fifo":
        receipt.unlink()
        os.mkfifo(receipt, 0o600)
    elif change == "malformed":
        receipt.write_text('{"schema":true,"boot_id":"invalid","socket":[]}')
    elif change == "duplicate":
        receipt.write_text(receipt.read_text().replace('"schema":1', '"schema":1,"schema":1'))
    assert not SocketProvenance(str(path)).recover_previous_boot()
    assert path.exists() and receipt.exists()


def test_ancestor_symlink_and_competing_start_are_not_admitted(tmp_path, monkeypatch):
    path, proof, receipt = recorded(tmp_path, monkeypatch)
    alias = tmp_path / "alias"
    alias.symlink_to(path.parent, target_is_directory=True)
    assert SocketProvenance(str(alias / path.name)).snapshot() is None
    with proof.startup_guard() as locked:
        assert locked
        with pytest.raises(BlockingIOError):
            with SocketProvenance(str(path)).startup_guard():
                pytest.fail("concurrent owner admitted")
    proof.forget()
    assert path.exists() and not receipt.exists()


def test_unsupported_lock_disables_only_automatic_recovery(tmp_path, monkeypatch):
    path, proof, receipt = recorded(tmp_path, monkeypatch)
    def unsupported(*_):
        raise OSError(errno.EOPNOTSUPP, "unsupported")
    monkeypatch.setattr(ipc_provenance.fcntl, "flock", unsupported)
    with proof.startup_guard() as recovery_allowed:
        assert not recovery_allowed
    assert path.exists() and receipt.exists()


@pytest.mark.asyncio
async def test_receipt_io_failure_preserves_verified_live_peer(tmp_path, monkeypatch):
    path = tmp_path / "mpv.sock"
    os.mknod(path, stat.S_IFSOCK | 0o600)
    backend = MpvBackend(str(path), audio_device="null")
    process = FakeProcess()
    process.pid = 42
    backend._owned_process = backend.process = process
    fake = FakeIpc()
    monkeypatch.setattr(asyncio, "open_unix_connection", fake.connect)
    monkeypatch.setattr(backend, "_peer_pid", lambda _: 42)
    monkeypatch.setattr(ipc_provenance.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("full")))
    identity = backend._socket_identity()
    assert await backend._probe_socket(owned_identity=identity) == 42
    assert backend._owned_socket_identity == identity
    await backend.stop_process()
    assert process.waited == 1 and not path.exists()
