"""Non-native checks for the CLI fixture's isolation and fail-closed helpers."""
from __future__ import annotations

import errno
from email.message import Message
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from fake_linkplay.config import load_config
from tools.acceptance_cli_lifecycle import (
    assert_ports_closed, assert_web_assets, child_environment, free_ports,
    native_preflight, wait_for, write_config, write_null_wrapper,
)


@pytest.mark.parametrize("owned", [True, False])
def test_generated_config_stays_private_loopback_and_persists_identity(tmp_path, owned):
    path = write_config(tmp_path, [45001, 45002], owned)
    first, second = load_config(path), load_config(path)
    assert first.uuid == second.uuid and first.uuid
    assert first.bind_address == first.advertise_ip == first.discovery_interface == "127.0.0.1"
    assert not first.enable_mdns and not first.enable_ssdp and not first.mirror_port_80
    assert first.http_api_port == 45001 and first.upnp_port == 45002
    assert first.auto_spawn_mpv is owned
    assert Path(first.state_dir).is_relative_to(tmp_path)
    assert Path(first.mpv_socket).is_relative_to(tmp_path)
    assert first.default_volume == 25 and first.default_mute is False


@pytest.mark.parametrize("installed", [True, False])
def test_child_environment_isolates_install_and_host_settings(tmp_path, monkeypatch, installed):
    monkeypatch.setenv("HTTP_PROXY", "http://synthetic.example.test")
    monkeypatch.setenv("https_proxy", "http://synthetic.example.test")
    monkeypatch.setenv("ALL_PROXY", "http://synthetic.example.test")
    monkeypatch.setenv("PYTHONHOME", "/synthetic/foreign-python")
    monkeypatch.setenv("PYTHONPATH", "/synthetic/foreign-code")
    root = tmp_path / "source"
    env = child_environment(root, tmp_path, installed=installed)
    assert not {name.lower() for name in env} & {"http_proxy", "https_proxy", "all_proxy"}
    assert "PYTHONHOME" not in env
    assert env.get("PYTHONPATH") == (None if installed else str(root))
    assert env["HOME"] == str(tmp_path)
    assert env["PATH"].split(os.pathsep)[0] == str(tmp_path / "bin")
    assert env["NO_PROXY"] == env["no_proxy"] == "127.0.0.1,localhost"
    # The parent interpreter environment is untouched.
    assert os.environ["PYTHONPATH"] == "/synthetic/foreign-code"


def test_native_preflight_denial_fails_before_creating_a_fallback(tmp_path, monkeypatch):
    def denied(*_args, **_kwargs):
        raise PermissionError(errno.EPERM, "synthetic AF_UNIX denial")
    monkeypatch.setattr(socket, "socket", denied)
    with pytest.raises(RuntimeError, match="requires usable AF_UNIX; no fallback") as failure:
        native_preflight(tmp_path)
    assert isinstance(failure.value.__cause__, PermissionError)
    assert not list(tmp_path.iterdir())


def test_wait_retries_transient_absence_but_propagates_assertion():
    tries = []
    def eventually():
        tries.append(1)
        if len(tries) < 2:
            raise ConnectionRefusedError()
        return "ready"
    assert wait_for("fixture", eventually) == "ready"
    with pytest.raises(AssertionError, match="Timed out waiting"):
        wait_for("never ready", lambda: False, timeout=0)
    def broken():
        raise AssertionError("process exited")
    with pytest.raises(AssertionError, match="process exited"):
        wait_for("broken fixture", broken)


def test_port_cleanup_check_detects_actual_live_listener():
    ports = free_ports()
    assert len(set(ports)) == 2 and min(ports) > 1024
    assert_ports_closed(ports)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", ports[0]))
        listener.listen()
        with pytest.raises(AssertionError, match="listener alive"):
            assert_ports_closed(ports)
    assert_ports_closed(ports)


def test_safety_wrapper_exec_preserves_pid_arguments_and_quoted_paths(tmp_path):
    directory = tmp_path / "private fixture's directory"
    (directory / "bin").mkdir(parents=True)
    executable = directory / "synthetic mpv"
    executable.write_text(f"#!{sys.executable}\nimport json, os, sys\nprint(json.dumps([os.getpid(), sys.argv[1:]]))\n")
    executable.chmod(0o700)
    wrapper = write_null_wrapper(directory, executable)
    process = subprocess.Popen([str(wrapper), "--ao=synthetic-host-output", "--load-scripts=yes",
        "--input-ipc-server=path with spaces"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
    assert process.returncode == 0 and not stderr
    pid, arguments = json.loads(stdout)
    assert pid == process.pid
    # Literal expectations must not share the tuple under test. Forwarded
    # conflicting options come first; mandatory safe values must come last.
    assert arguments == ["--ao=synthetic-host-output", "--load-scripts=yes",
        "--input-ipc-server=path with spaces", "--ao=null", "--no-config",
        "--load-scripts=no", "--ytdl=no", "--terminal=no",
        "--input-default-bindings=no", "--no-video", "--idle=yes"]


@pytest.mark.parametrize("missing", ["--ao=null", "--no-config", "--load-scripts=no", "--ytdl=no"])
def test_safety_wrapper_rejects_removed_required_flag_before_spawn(tmp_path, monkeypatch, missing):
    from tools import acceptance_cli_lifecycle as fixture
    (tmp_path / "bin").mkdir()
    monkeypatch.setattr(fixture, "NULL_OPTIONS", tuple(option for option in fixture.NULL_OPTIONS if option != missing))
    with pytest.raises(AssertionError, match="Missing required null-output fixture safety options"):
        write_null_wrapper(tmp_path, tmp_path / "never executed")
    assert not (tmp_path / "bin/mpv").exists()


@pytest.mark.parametrize("fault", [None, "wrong_body", "wrong_type", "wrong_status", "oversize", "empty_source"])
def test_web_assets_probe_requires_actual_bodies_and_types(tmp_path, fault):
    routes = {"/ui/": ("index.html", "text/html"),
              "/ui/index.html": ("index.html", "text/html"),
              "/ui/style.css": ("style.css", "text/css"),
              "/ui/controller.js": ("controller.js", "text/javascript"),
              "/ui/app.js": ("app.js", "text/javascript")}
    for name, _ in routes.values():
        (tmp_path / name).write_bytes(name.encode() if fault != "empty_source" else b"")
    calls = []

    class Opener:
        def open(self, url, timeout):
            route = url.removeprefix("http://127.0.0.1:45001")
            name, content_type = routes[route]
            assert timeout == 3
            calls.append(route)
            body = name.encode()
            if fault == "wrong_body":
                body = b"fallback page"
            elif fault == "oversize":
                body = b"x" * 1048577
            response = io.BytesIO(body)
            response.status = 404 if fault == "wrong_status" else 200
            response.headers = Message()
            response.headers["Content-Type"] = "text/plain" if fault == "wrong_type" else content_type + "; charset=utf-8"
            return response

    if fault:
        with pytest.raises(AssertionError):
            assert_web_assets(Opener(), "http://127.0.0.1:45001", tmp_path)
    else:
        assert assert_web_assets(Opener(), "http://127.0.0.1:45001", tmp_path) == list(routes)
        assert calls == list(routes)
