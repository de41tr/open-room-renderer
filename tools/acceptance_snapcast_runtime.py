"""Bounded real foreground-source lifecycle acceptance, file outputs only.

Uses the shipped launcher, its owned production renderer/mpv, two operator-side
Snapclients, and quiet generated stereo. No user account, physical output,
service installation, LAN listener, or two-host synchronization claim.
"""
from __future__ import annotations

from array import array
import ctypes
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.acceptance_snapcast import channel_measurement, assert_channel_measurement, freeport, generate
from fake_linkplay.snapcast_runtime import Runtime


def main():
    # Process-local test harness only: adopt orphaned owned leaders if the
    # launcher crashes, so their IDs can be retained safely through cleanup.
    assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
    children = []
    owned_groups = set()
    launcher = None
    evidence = {"lane": "REAL_FOREGROUND_SNAPCAST_SOURCE", "two_host_sync": "NOT_TESTED", "stages": []}
    deadline = time.monotonic() + 55
    with tempfile.TemporaryDirectory(prefix="orr-sc-runtime-test-", dir="/tmp") as directory:
        directory = Path(directory)
        ports = [freeport() for _ in range(3)]
        assert len(set(ports)) == 3
        stream_port, control_port, http_port = ports
        config = directory / "source.yaml"
        raw = {"mpv_audio_device": "pulse/orr_snapcast", "auto_spawn_mpv": True,
            "state_dir": "relative-state", "uuid": "00000000-0000-4000-8000-000000000000",
            "enable_ssdp": False, "enable_mdns": False, "wiim_home_profile": False,
            "http_api_port": http_port, "upnp_port": http_port,
            "bind_address": "127.0.0.1", "advertise_ip": "127.0.0.1", "default_volume": 100}
        config.write_text(yaml.safe_dump(raw))
        original_config = config.read_bytes()
        tone = directory / "tone.wav"
        generate(tone)
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        # A poisoned ambient default must never become the source audio server.
        env["PULSE_SERVER"] = "unix:/nonexistent/foreign-audio-server"
        env["DBUS_SYSTEM_BUS_ADDRESS"] = f"unix:path={directory}/unused-bus"
        logs = []

        def discover_owned_groups():
            # These are direct children of the exact launcher Popen PID, not
            # name-based process discovery. Retain only its session leaders for
            # emergency test cleanup after a bounded launcher failure.
            if launcher is not None and not Runtime.child_exited(launcher):
                try:
                    pids = Path(f"/proc/{launcher.pid}/task/{launcher.pid}/children").read_text().split()
                    for value in pids:
                        pid = int(value)
                        try:
                            if os.getpgid(pid) == pid:
                                owned_groups.add(pid)
                        except ProcessLookupError:
                            pass
                except FileNotFoundError:
                    pass

        def checked_sleep(duration):
            end = time.monotonic() + duration
            while time.monotonic() < end:
                assert time.monotonic() < deadline, "Runtime acceptance exceeded its work deadline"
                discover_owned_groups()
                assert not Runtime.child_exited(launcher), "Foreground launcher exited before acceptance finished"
                time.sleep(max(0, min(.05, end - time.monotonic())))

        def launch(command):
            log = (directory / f"child-{len(children)}.log").open("w")
            logs.append(log)
            child = subprocess.Popen(command, cwd=directory, env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            children.append(child)
            return child

        def command(value):
            request = f"http://127.0.0.1:{http_port}/httpapi.asp?" + urlencode({"command": value})
            with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
                body = response.read(65537)
                assert response.status == 200 and len(body) <= 65536
                return json.loads(body) if response.headers.get_content_type() == "application/json" else body.decode()

        def measure(label, gain):
            # The public workflow uses a conservative 1-second Snapcast buffer.
            checked_sleep(1.5)
            paths = [directory / f"client-{index}.pcm" for index in range(2)]
            offsets = [path.stat().st_size for path in paths]
            checked_sleep(.8)
            ends = [path.stat().st_size for path in paths]
            measurements = []
            for index, (path, start, end) in enumerate(zip(paths, offsets, ends)):
                with path.open("rb") as source:
                    source.seek(start)
                    data = source.read(end - start)
                assert len(data) % 4 == 0 and len(data) >= 48000 * 2
                samples = array("h")
                samples.frombytes(data)
                values = [channel_measurement(samples[channel::2], hz, gain)
                          for channel, hz in enumerate((440, 733))]
                for channel, value in enumerate(values):
                    assert_channel_measurement(value, gain, label, index, channel)
                measurements.append({"frames": len(data) // 4, "rms": [v["rms"] for v in values]})
            evidence["stages"].append({"name": label, "gain": gain, "clients": measurements})

        try:
            launcher = launch([sys.executable, "-m", "fake_linkplay.snapcast_runtime", "run", "--config", str(config),
                "--stream-port", str(stream_port), "--control-port", str(control_port),
                "--initial-client-volume", "100"])
            end = time.monotonic() + 15
            while "READY:" not in (directory / "child-0.log").read_text():
                assert time.monotonic() < end, "Foreground source did not reach readiness"
                checked_sleep(.05)
            assert len(owned_groups) == 4, "Expected owned Pulse, Snapserver, recorder and renderer groups"
            for index in range(2):
                launch(["snapclient", "--host", "127.0.0.1", "--port", str(stream_port),
                    "--hostID", f"orr-runtime-{secrets.token_hex(12)}-{index}",
                    "--player", f"file:filename={directory}/client-{index}.pcm,mode=a",
                    "--mixer", "software:poly:1", "--logsink", "stdout"])
            checked_sleep(1.5)
            assert command(f"setPlayerCmd:play:{tone}") == "OK"
            assert command("getPlayerStatus")["status"] == "play"
            measure("owned_source_to_two_clients", 1)
            assert command("setPlayerCmd:vol:50") == "OK"
            measure("shared_renderer_gain", .125)
            assert command("setPlayerCmd:pause") == "OK"
            assert command("getPlayerStatus")["status"] == "pause"
            measure("pause_drains_to_silence", 0)
            assert command("setPlayerCmd:resume") == "OK"
            measure("resume_restores_source", .125)
            assert command("setPlayerCmd:stop") == "OK"
            assert command("getPlayerStatus")["status"] == "stop"
            measure("stop_drains_to_silence", 0)
            assert config.read_bytes() == original_config
            assert (directory / "relative-state" / "gain.json").exists()
            assert command("getPlayerStatus")["vol"] in ("50", 50)
            launcher.send_signal(signal.SIGTERM)
            launcher.wait(timeout=9)
            assert launcher.returncode == 0
            for port in ports:
                with socket.socket() as probe:
                    probe.settimeout(.2)
                    assert probe.connect_ex(("127.0.0.1", port)) != 0, "Owned port leaked"
            for pgid in owned_groups:
                # A dead orphan can briefly remain a zombie under slow init;
                # no owned group may contain a live/running process.
                alive = []
                for entry in Path("/proc").iterdir():
                    if not entry.name.isdigit():
                        continue
                    try:
                        status = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                        if int(status[2]) == pgid and status[0] != "Z":
                            alive.append(entry.name)
                    except (OSError, ValueError, IndexError):
                        pass
                assert not alive, "Owned process group leaked"
            evidence.update(cleanup="all owned groups stopped, listeners closed", config_unchanged=True,
                            relative_state_preserved=True, production_renderer="owned mpv via private Pulse")
            print(json.dumps(evidence, sort_keys=True))
        finally:
            if launcher is not None and launcher.returncode is None:
                discover_owned_groups()
                launcher.terminate()
                end = time.monotonic() + 9
                while not Runtime.child_exited(launcher) and time.monotonic() < end:
                    time.sleep(.05)
            for child in children:
                if child.returncode is not None:
                    continue
                # Every group leader remains unreaped until the last signal.
                Runtime.child_exited(child)
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=2)
            for pgid in owned_groups:
                try:
                    # Only an actually adopted, unreaped child still anchors
                    # its own group. Reaped IDs are never used for signals.
                    os.waitid(os.P_PID, pgid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                except ChildProcessError:
                    continue
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(pgid, 0)
            for log in logs:
                log.close()



if __name__ == "__main__":
    main()
