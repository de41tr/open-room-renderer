"""Focused source-runtime boundaries; synthetic processes, no native audio."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest
import yaml

from fake_linkplay import snapcast_runtime as runtime
from fake_linkplay.config import Config, StartupConfigError


def arguments():
    return argparse.Namespace(stream_bind="127.0.0.1", stream_port=1704, control_port=1705, initial_client_volume=0)


def instance(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSE_SERVER", "foreign-default")
    monkeypatch.setenv("PULSE_SINK", "physical-default")
    cfg = Config(mpv_audio_device=runtime.AUDIO_DEVICE)
    raw = {"mpv_audio_device": runtime.AUDIO_DEVICE, "state_dir": "relative-state",
           "podcast_presets": [{"name": "Synthetic", "url": "https://example.test/media?token=private"}]}
    return runtime.Runtime(tmp_path, cfg, raw, dict.fromkeys(runtime.NATIVE_TOOLS, "/unused"), arguments())


def test_snapshot_is_private_preserves_relative_state_and_never_alters_original(tmp_path, monkeypatch):
    original = tmp_path / "source.yaml"
    original.write_text("mpv_audio_device: pulse/orr_snapcast\nstate_dir: relative-state\n")
    cfg, raw = runtime.read_config(str(original))
    before = original.read_bytes()
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    run = runtime.Runtime(directory, cfg, raw, {}, arguments())
    copied = yaml.safe_load(run.renderer_config.read_text())
    assert copied == {"mpv_audio_device": runtime.AUDIO_DEVICE, "state_dir": "relative-state",
                      "mpv_socket": str(directory / "mpv.sock")}
    assert original.read_bytes() == before and not Path("relative-state").exists()
    assert run.renderer_config.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    assert run.fifo.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("contents", ["", "mpv_audio_device: auto", "mpv_audio_device: alsa/default",
    "mpv_audio_device: pulse/orr_snapcast\nauto_spawn_mpv: false",
    "mpv_audio_device: pulse/orr_snapcast\nmpv_audio_device: auto",
    "mpv_audio_device: pulse/orr_snapcast\nhttp_api_port: 80",
    "mpv_audio_device: pulse/orr_snapcast\nmirror_port_80: true"])
def test_invalid_or_ambiguous_route_fails_before_state_or_processes(tmp_path, contents):
    path = tmp_path / "input.yaml"
    path.write_text(contents)
    with pytest.raises((runtime.RuntimeErrorSafe, StartupConfigError)):
        runtime.read_config(str(path))
    assert list(tmp_path.iterdir()) == [path]


def test_config_bounds_and_nonregular_input_do_not_block(tmp_path):
    path = tmp_path / "input"
    path.write_bytes(b"x" * (runtime.MAX_BYTES + 1))
    with pytest.raises(runtime.RuntimeErrorSafe, match="size limit"):
        runtime.read_config(str(path))
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(runtime.RuntimeErrorSafe, match="regular file"):
        runtime.read_config(str(path))


@pytest.mark.parametrize("value", ["0.0.0.0", "::", "localhost", "224.0.0.1", "255.255.255.255", "127.0.0.1\nprivate"])
def test_network_opt_in_requires_one_literal_interface(value):
    with pytest.raises(argparse.ArgumentTypeError):
        runtime.literal_address(value)
    assert runtime.literal_address("127.0.0.1") == "127.0.0.1"


def test_child_environment_never_changes_ambient_audio_or_renderer_home(tmp_path, monkeypatch):
    run = instance(tmp_path, monkeypatch)
    assert os.environ["PULSE_SERVER"] == "foreign-default"
    assert os.environ["PULSE_SINK"] == "physical-default"
    assert run.env["PULSE_SERVER"] == f"unix:{run.pulse_endpoint}"
    assert "PULSE_SINK" not in run.env
    assert run.env["HOME"] == os.environ["HOME"]
    assert run.native_env["HOME"] == str(tmp_path / "home")
    assert run.native_env["DBUS_SYSTEM_BUS_ADDRESS"] == f"unix:path={tmp_path}/no-dbus"
    config = run.server_config.read_text()
    assert "[http]\nenabled = false" in config
    assert "[tcp]\nenabled = true\nbind_to_address = 127.0.0.1" in config
    assert "initial_volume = 0" in config
    assert "codec = pcm" in config and "sampleformat = 48000:16:2" in config


def test_bind_conflicts_are_read_only_and_fail_before_process_launch():
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        cfg = Config(bind_address="127.0.0.1", http_api_port=held.getsockname()[1], upnp_port=held.getsockname()[1])
        with pytest.raises(runtime.RuntimeErrorSafe, match="unavailable"):
            runtime.preflight_ports(cfg, "127.0.0.1", 1704, 1705)
        assert held.fileno() >= 0
    with pytest.raises(runtime.RuntimeErrorSafe, match="distinct"):
        runtime.preflight_ports(Config(), "127.0.0.1", 1704, 1704)


def test_private_pulse_rejects_any_unexpected_output_or_module(tmp_path, monkeypatch):
    run = instance(tmp_path, monkeypatch)
    monkeypatch.setattr(Path, "is_socket", lambda path: True)
    values = iter(["0\tmodule-native-protocol-unix\t\n1\tmodule-alsa-sink\t\n"])
    run.inspect_pulse = lambda *_: next(values)
    with pytest.raises(runtime.RuntimeErrorSafe, match="module set"):
        run.pulse_ready()
    values = iter(["0\tmodule-native-protocol-unix\t\n1\tmodule-null-sink\t\n", "0\tphysical\tmodule-alsa-sink\ts16le 2ch 48000Hz\t\n"])
    with pytest.raises(runtime.RuntimeErrorSafe, match="output"):
        run.pulse_ready()
    values = iter(["0\tmodule-native-protocol-unix\t\n"])
    assert run.pulse_ready() is False


def test_no_player_launch_before_checked_bridge_and_finite_missing_readiness(tmp_path, monkeypatch):
    run = instance(tmp_path, monkeypatch)
    run.start = Mock(side_effect=runtime.RuntimeErrorSafe("synthetic startup failure"))
    run.close = Mock()
    monkeypatch.setattr(runtime, "Runtime", lambda *_: run)
    monkeypatch.setattr(runtime, "read_config", lambda _: (run.cfg, {}))
    monkeypatch.setattr(runtime, "require_tools", lambda: {})
    monkeypatch.setattr(runtime, "preflight_ports", lambda *_: None)
    assert runtime.main(["run", "--config", "unused"]) == 2
    run.close.assert_called_once()
    assert run.children == {}
    with pytest.raises(runtime.RuntimeErrorSafe, match="did not become ready"):
        run.wait_ready(lambda: False, "synthetic", timeout=.001)


def test_source_death_is_not_restarted_or_ignored(tmp_path, monkeypatch):
    run = instance(tmp_path, monkeypatch)
    failed = Mock()
    monkeypatch.setattr(run, "child_exited", lambda child: True)
    run.children["recorder"] = failed
    with pytest.raises(runtime.RuntimeErrorSafe, match="Owned recorder exited"):
        run.launch("renderer", ["/never-start"])
    assert set(run.children) == {"recorder"}


def test_real_owned_process_stops_without_touching_foreign_process(tmp_path, monkeypatch):
    run = instance(tmp_path, monkeypatch)
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    child = None
    try:
        child = run.launch("synthetic", [sys.executable, "-c", "import time; time.sleep(30)"])
        assert os.getpgid(child.pid) == child.pid != os.getpgid(unrelated.pid)
        run.close()
        assert child.poll() is not None and unrelated.poll() is None
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=2)
        unrelated.terminate()
        unrelated.wait(timeout=2)


def test_readiness_exchange_skips_notifications_and_rejects_oversize():
    peer = Mock()
    peer.recv.side_effect = [b'{"method":"notice"}\n', b'{"id":1,"result":{}}\n']
    assert runtime.receive_reply(peer, {"id": 1}) == {"id": 1, "result": {}}
    peer.recv.side_effect = [b"x" * (runtime.MAX_BYTES + 1)]
    with pytest.raises(runtime.RuntimeErrorSafe, match="oversized"):
        runtime.receive_reply(peer, {"id": 1})


def test_server_identity_cannot_be_satisfied_by_foreign_status(tmp_path, monkeypatch):
    run = instance(tmp_path, monkeypatch)
    peer = Mock()
    peer.__enter__ = Mock(return_value=peer)
    peer.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(socket, "create_connection", lambda *_args, **_kwargs: peer)
    monkeypatch.setattr(runtime, "receive_reply", lambda *_: {"result": {"server": {"streams": [{"id": "foreign", "status": "playing"}]}}})
    with pytest.raises(runtime.RuntimeErrorSafe, match="identity"):
        run.server_ready()


def test_cli_errors_do_not_echo_private_config_paths(capsys):
    with pytest.raises(SystemExit):
        runtime.main(["run", "--config", "/secret/token.yaml", "--stream-bind", "private-token"])
    output = capsys.readouterr()
    assert "secret" not in output.err and "private-token" not in output.err


def test_exited_leader_is_retained_until_last_group_signal(tmp_path, monkeypatch):
    run = instance(tmp_path, monkeypatch)
    child = run.launch("short", [sys.executable, "-c", "pass"])
    end = time.monotonic() + 3
    while not run.child_exited(child):
        assert time.monotonic() < end
        time.sleep(.01)
    assert child.returncode is None
    signals = []
    real_killpg = os.killpg
    def checked_signal(pid, signum):
        assert pid == child.pid and child.returncode is None
        assert os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        signals.append(signum)
        return real_killpg(pid, signum)
    monkeypatch.setattr(os, "killpg", checked_signal)
    run.close()
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert child.returncode == 0
    run.close()
    assert len(signals) == 2


def test_silent_snapcast_status_needs_independent_pcm_progress(tmp_path, monkeypatch):
    run = instance(tmp_path, monkeypatch)
    recorder = Mock(pid=1234)
    run.children["recorder"] = recorder
    now = [10.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    run.recorder_progress_at = now[0]
    count = [8192]
    real_read = Path.read_text
    def io_read(path, *args, **kwargs):
        if str(path) == "/proc/1234/io":
            return "wchar: " + str(count[0]) + "\n"
        return real_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", io_read)
    assert run.recorder_progress()
    now[0] += 3.1
    assert not run.recorder_progress()
    count[0] += 192000
    assert run.recorder_progress()


@pytest.mark.parametrize("value", ["-1", "101", "1.5", "private-token"])
def test_initial_room_volume_is_bounded_and_silent_by_default(value):
    with pytest.raises(argparse.ArgumentTypeError):
        runtime.client_volume(value)
    assert runtime.client_volume("0") == 0
    assert runtime.client_volume("10") == 10
    assert runtime.client_volume("100") == 100
