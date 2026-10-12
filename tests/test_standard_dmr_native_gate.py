"""UNIT: standard-client native opt-ins fail rather than silently lose coverage."""
import builtins
import subprocess
import sys

import pytest

import test_standard_dmr_unix_integration as native


def test_standard_native_gate_requires_client(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    original = builtins.__import__
    def without_client(name, *args, **kwargs):
        if name.startswith("async_upnp_client"):
            raise ModuleNotFoundError("synthetic missing standard client")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", without_client)
    with pytest.raises(ModuleNotFoundError, match="missing standard client"):
        native.test_standard_dmr_production_unix_ipc()


def test_standard_native_gate_requires_mpv(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(native.shutil, "which", lambda _name: None)
    with pytest.raises(AssertionError, match="requires a real mpv"):
        native.test_standard_dmr_production_unix_ipc()


def test_standard_native_gate_preserves_worker_failure_and_deadline(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(native.shutil, "which", lambda _name: "/synthetic/mpv")
    calls = []
    def failed(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 1, "", "Production Unix IPC NOT_RUN: AF_UNIX denied")
    monkeypatch.setattr(native, "run_probe", failed)
    with pytest.raises(AssertionError, match="AF_UNIX denied"):
        native.test_standard_dmr_production_unix_ipc()
    assert len(calls) == 1 and calls[0][1]["timeout"] == 25
    assert calls[0][0][-1] == "/synthetic/mpv"
