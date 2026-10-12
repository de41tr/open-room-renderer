"""UNIT: opting into client/native acceptance cannot silently drop a dependency."""
import builtins
import subprocess
import sys
from types import SimpleNamespace

import pytest

import test_pywiim_unix_integration as native


def test_explicit_native_client_gate_fails_without_pywiim(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    original = builtins.__import__
    def without_client(name, *args, **kwargs):
        if name == "pywiim":
            raise ModuleNotFoundError("synthetic missing pywiim")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", without_client)
    with pytest.raises(ModuleNotFoundError, match="missing pywiim"):
        native.test_real_pywiim_controls_production_unix_ipc()


def test_explicit_native_client_gate_fails_without_mpv(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "pywiim", SimpleNamespace(WiiMClient=object))
    monkeypatch.setattr(native.shutil, "which", lambda _name: None)
    with pytest.raises(AssertionError, match="requires a real mpv"):
        native.test_real_pywiim_controls_production_unix_ipc()


def test_explicit_native_client_gate_fails_on_worker_error(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "pywiim", SimpleNamespace(WiiMClient=object))
    monkeypatch.setattr(native.shutil, "which", lambda _name: "/synthetic/mpv")
    calls = []
    def failed(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 1, "", "AF_UNIX denied")
    monkeypatch.setattr(native, "run_probe", failed)
    with pytest.raises(AssertionError, match="AF_UNIX denied"):
        native.test_real_pywiim_controls_production_unix_ipc()
    assert len(calls) == 1 and calls[0][1]["timeout"] == 60
    assert calls[0][0][-1] == "/synthetic/mpv"


def test_explicit_native_client_gate_reports_denied_unix_ipc(monkeypatch):
    def denied(*_args, **_kwargs):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(native.socket, "socket", denied)
    with pytest.raises(AssertionError, match="Production Unix IPC NOT_RUN: PermissionError, errno=1"):
        native.require_unix_ipc()
