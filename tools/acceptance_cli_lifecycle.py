"""Worker for bounded, opt-in real CLI serve/restart acceptance on native Linux.

Run through tests/test_cli_lifecycle_native.py and native_probe_supervisor; all
children inherit that supervisor's owned process group. This worker imports no
production objects and never substitutes a playback backend or IPC transport.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import shlex
import signal
import socket
import struct
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener
from xml.etree import ElementTree
import wave


NULL_OPTIONS = ("--ao=null", "--no-config", "--load-scripts=no", "--ytdl=no",
                "--terminal=no", "--input-default-bindings=no", "--no-video", "--idle=yes")


def child_environment(root: Path, directory: Path, *, installed: bool) -> dict[str, str]:
    env = os.environ.copy()
    for name in tuple(env):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            env.pop(name)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    if not installed:
        env["PYTHONPATH"] = str(root)
    env.update(HOME=str(directory), XDG_CONFIG_HOME=str(directory / "config"),
               XDG_STATE_HOME=str(directory / "user-state"),
               NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost",
               PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1")
    env["PATH"] = str(directory / "bin") + os.pathsep + env.get("PATH", "")
    return env


def write_config(directory: Path, ports: list[int], owned: bool) -> Path:
    config = {"friendly_name": "Synthetic CLI renderer", "state_dir": str(directory / "state"),
              "mpv_socket": str(directory / "mpv.sock"), "auto_spawn_mpv": owned,
              "http_api_port": ports[0], "upnp_port": ports[1], "mirror_port_80": False,
              "bind_address": "127.0.0.1", "advertise_ip": "127.0.0.1",
              "discovery_interface": "127.0.0.1", "enable_ssdp": False, "enable_mdns": False,
              "default_volume": 25, "default_mute": False, "log_level": "WARNING"}
    path = directory / "renderer.yaml"
    path.write_text(json.dumps(config))  # JSON is a YAML subset.
    return path


def native_preflight(directory: Path) -> None:
    path = directory / "preflight.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            listener.listen(1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1)
                client.connect(str(path))
                accepted, _ = listener.accept()
                accepted.close()
    except OSError as exc:
        raise RuntimeError("Native CLI acceptance requires usable AF_UNIX; no fallback transport") from exc
    finally:
        path.unlink(missing_ok=True)


def wait_for(description, check, *, timeout=8):
    deadline = time.monotonic() + timeout
    while True:
        try:
            value = check()
            if value:
                return value
        except (OSError, URLError):
            pass  # Listener/IPC can legitimately be absent while starting.
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out waiting for {description}")
        time.sleep(.04)


def ipc(endpoint: Path, command: list) -> tuple[object, int]:
    """Independent native readback, never a replacement for production IPC."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(2)
        peer.connect(str(endpoint))
        pid = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
        peer.sendall(json.dumps({"command": command, "request_id": 1}).encode() + b"\n")
        with peer.makefile("rb") as reader:
            for _ in range(32):
                line = reader.readline(65537)
                assert line and len(line) <= 65536, "Missing/oversize native IPC response"
                result = json.loads(line)
                if result.get("request_id") == 1:
                    assert result["error"] == "success", result["error"]
                    return result.get("data"), pid
    raise AssertionError("No matching native IPC response")


def prop(endpoint: Path, name: str):
    return ipc(endpoint, ["get_property", name])[0]


def reap(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def free_ports() -> list[int]:
    # Keep both reservations live until distinct high ports have been selected.
    # The CLI owns its binds; an intervening bind race must fail, never pass.
    with ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket()) for _ in range(2)]
        for peer in sockets:
            peer.bind(("127.0.0.1", 0))
        return [peer.getsockname()[1] for peer in sockets]


def assert_ports_closed(ports: list[int]) -> None:
    for port in ports:
        with socket.socket() as peer:
            peer.settimeout(1)
            assert peer.connect_ex(("127.0.0.1", port)) != 0, "CLI left a listener alive"
        with socket.socket() as peer:
            peer.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            peer.bind(("127.0.0.1", port))


def write_tone(path: Path) -> None:
    rate = 16000
    second = b"".join(struct.pack("<hh", sample, sample) for index in range(rate)
        for sample in [round(400 * math.sin(2 * math.pi * 440 * index / rate))])
    with wave.open(str(path), "wb") as output:
        output.setparams((2, 2, rate, 0, "NONE", "not compressed"))
        for _ in range(60):
            output.writeframesraw(second)


def write_null_wrapper(directory: Path, mpv: Path) -> Path:
    # Fail before spawning if a fixture edit removes a required safety flag.
    # Keep this literal set independent of the option tuple and its tests.
    assert {"--ao=null", "--no-config", "--load-scripts=no", "--ytdl=no",
            "--terminal=no", "--input-default-bindings=no", "--no-video", "--idle=yes"} <= set(NULL_OPTIONS), (
        "Missing required null-output fixture safety options")
    wrapper = directory / "bin/mpv"
    # Exec preserves peer PID ownership. Safety flags come last so forwarded
    # production arguments cannot override null output/config/script isolation.
    wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(str(mpv)) + ' "$@" '
        + " ".join(shlex.quote(option) for option in NULL_OPTIONS) + "\n")
    wrapper.chmod(0o700)
    return wrapper


def assert_web_assets(opener, base: str, assets: Path) -> list[str]:
    # Source bytes are the test oracle only. The installed CLI runs from its own
    # private cwd with PYTHONPATH/PYTHONHOME removed by child_environment().
    routes = {"/ui/": ("index.html", "text/html"),
              "/ui/index.html": ("index.html", "text/html"),
              "/ui/style.css": ("style.css", "text/css"),
              "/ui/controller.js": ("controller.js", "text/javascript"),
              "/ui/app.js": ("app.js", "text/javascript")}
    for route, (name, content_type) in routes.items():
        expected = (assets / name).read_bytes()
        assert expected, f"Empty Web asset: {name}"
        with opener.open(base + route, timeout=3) as response:
            body = response.read(1048577)
            assert response.status == 200 and len(body) <= 1048576, route
            assert response.headers.get_content_type() == content_type, route
            assert body == expected, f"Web asset content mismatch: {route}"
    return list(routes)


def exercise(directory: Path, mpv: Path, *, owned: bool, cli: Path | None = None) -> dict:
    assert sys.platform == "linux" and Path("/proc/self/stat").is_file(), "Native CLI lane requires Linux /proc"
    native_preflight(directory)
    root = Path(__file__).resolve().parents[1]
    directory.chmod(0o700)
    (directory / "bin").mkdir()
    wrapper = write_null_wrapper(directory, mpv)
    env = child_environment(root, directory, installed=cli is not None)
    invocation = [str(cli)] if cli else [sys.executable, "-m", "fake_linkplay.cli"]
    ports = free_ports()
    config = write_config(directory, ports, owned)
    endpoint = directory / "mpv.sock"
    track = directory / "tone.wav"
    write_tone(track)
    opener = build_opener(ProxyHandler({}))

    def request(path, port=ports[0]):
        with opener.open(f"http://127.0.0.1:{port}" + path, timeout=3) as response:
            body = response.read(1048577)
            assert response.status == 200 and len(body) <= 1048576
            return body.decode()

    def command(value):
        body = request("/httpapi.asp?" + urlencode({"command": value}))
        return json.loads(body) if body.startswith("{") else body

    def control(*args):
        result = subprocess.run([*invocation, *args, "--host", "127.0.0.1", "--port", str(ports[0])],
            env=env, cwd=directory, capture_output=True, text=True, timeout=12)
        assert result.returncode == 0 and not result.stderr, (args[0], result.returncode, result.stderr)
        return result.stdout.strip()

    def status(expected):
        result = command("getPlayerStatus")
        assert result["status"] == expected and not result["transport_error"], result
        return result

    external = None
    service = None
    cycles = []
    saved_uuid = None
    saved_graph = None
    state_snapshot = None
    with (directory / "mpv.log").open("wb") as player_log:
        try:
            if not owned:
                external = subprocess.Popen([str(wrapper), f"--input-ipc-server={endpoint}"],
                    env=env, cwd=directory, stdout=subprocess.DEVNULL, stderr=player_log)
                def external_ready():
                    assert external.poll() is None, "Fixture mpv exited before IPC readiness"
                    return endpoint.is_socket() and prop(endpoint, "idle-active")
                wait_for("fixture-owned mpv", external_ready)
            for cycle, shutdown_signal in enumerate((signal.SIGTERM, signal.SIGINT, signal.SIGHUP)):
                with (directory / f"serve-{cycle}.log").open("wb") as log:
                    service = subprocess.Popen([*invocation, "serve", "--config", str(config)],
                        env=env, cwd=directory, stdout=log, stderr=log)
                    def ready():
                        assert service.poll() is None, ("CLI serve exited before HTTP readiness: "
                            + (directory / f"serve-{cycle}.log").read_text(errors="replace")[-8000:])
                        return command("getStatusEx")
                    info = wait_for("real CLI HTTP readiness", ready)
                    assert info["gain_confirmed"] and not info["transport_error"], info
                    uuid = (directory / "state/uuid").read_text().strip()
                    assert info["uuid"] == uuid
                    info = json.loads(control("status"))
                    assert info["uuid"] == uuid
                    for port in ports:
                        description = ElementTree.fromstring(request("/description.xml", port))
                        udn = description.find(".//{urn:schemas-upnp-org:device-1-0}UDN")
                        assert udn is not None and udn.text == "uuid:" + uuid
                    web_assets = assert_web_assets(opener, f"http://127.0.0.1:{ports[0]}",
                                                   root / "fake_linkplay/web_assets")
                    version, native_pid = ipc(endpoint, ["get_property", "mpv-version"])
                    socket_identity = (endpoint.stat().st_dev, endpoint.stat().st_ino)
                    if owned:
                        fields = Path(f"/proc/{native_pid}/stat").read_text().rsplit(")", 1)[1].split()
                        assert int(fields[1]) == service.pid, "Native peer is not the CLI's owned child"
                    else:
                        assert native_pid == external.pid
                    status("stop")
                    if cycle == 0:
                        assert prop(endpoint, "volume") == 25 and prop(endpoint, "mute") is False
                        saved_uuid = uuid
                    else:
                        assert uuid == saved_uuid and info["DeviceName"] == "Synthetic persisted room"
                        assert (info["vol"], info["mute"]) == ("37", "1")
                        assert prop(endpoint, "volume") == 37 and prop(endpoint, "mute") is True
                        assert command("getChannelBalance") == "0.5"
                        assert prop(endpoint, "af") == saved_graph
                        assert all((directory / "state" / name).read_bytes() == body
                                   for name, body in state_snapshot.items())
                    assert control("play", str(track)) == "OK"
                    status("play")
                    assert prop(endpoint, "current-ao") == "null"
                    assert prop(endpoint, "path") == str(track)
                    assert any(item.get("type") == "audio" and item.get("selected")
                               for item in prop(endpoint, "track-list"))
                    before = prop(endpoint, "playback-time")
                    wait_for("decoded time progression", lambda: prop(endpoint, "playback-time") > before + .15)
                    assert control("pause") == "OK"
                    status("pause")
                    assert prop(endpoint, "pause") is True
                    assert control("resume") == "OK"
                    status("play")
                    assert prop(endpoint, "pause") is False
                    if cycle == 0:
                        assert control("volume", "37") == "OK"
                        assert command("setPlayerCmd:mute:1") == "OK"
                        assert command("setDeviceName:Synthetic persisted room") == "OK"
                        assert command("setChannelBalance:0.5") == "OK"
                        assert prop(endpoint, "volume") == 37 and prop(endpoint, "mute") is True
                        saved_graph = prop(endpoint, "af")
                        assert saved_graph, "Expected persisted non-default balance graph"
                        state_snapshot = {name: (directory / "state" / name).read_bytes()
                            for name in ("uuid", "device_name", "gain.json", "active_dsp.json")}
                        assert control("stop") == "OK"
                        status("stop")
                        assert prop(endpoint, "idle-active") is True
                    # First lifetime stops idle; later signals arrive while decoding.
                    service.send_signal(shutdown_signal)
                    assert service.wait(timeout=15) == 0, "CLI signal shutdown did not exit cleanly"
                    assert_ports_closed(ports)
                    if owned:
                        assert not Path(f"/proc/{native_pid}").exists(), "Owned mpv was not reaped"
                        assert not endpoint.exists(), "Owned IPC socket was not removed"
                    else:
                        assert external.poll() is None, "CLI killed external mpv"
                        assert socket_identity == (endpoint.stat().st_dev, endpoint.stat().st_ino)
                        assert ipc(endpoint, ["get_property", "mpv-version"])[1] == external.pid
                        if cycle == 0:
                            # Deliberately make the surviving player disagree with
                            # disk, proving restart reapplies persistence to native state.
                            ipc(endpoint, ["set_property", "volume", 89])
                            ipc(endpoint, ["set_property", "mute", False])
                            ipc(endpoint, ["set_property", "af", []])
                    cycles.append({"signal": shutdown_signal.name, "exit_status": 0,
                        "decoded_null_output": True, "http_ports_closed": True, "web_assets": web_assets,
                        "owned_child_reaped": owned, "external_peer_preserved": not owned})
                    service = None
        finally:
            if service is not None:
                reap(service)
            if external is not None:
                reap(external)
                endpoint.unlink(missing_ok=True)
    return {"lane": "REAL_CLI_SERVE_UNIX_IPC/null", "launch": "installed" if cli else "module",
            "ownership": "owned" if owned else "external", "mpv_version": version,
            "cycles": cycles, "restart_persistence": ["uuid", "name", "volume", "mute", "balance"],
            "same_ports_and_socket_restart": True, "fixture_child_reaped": external is None or external.returncode is not None}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--mpv", type=Path, required=True)
    parser.add_argument("--ownership", choices=("owned", "external"), required=True)
    parser.add_argument("--cli", type=Path)
    args = parser.parse_args()
    assert os.environ.get("ORR_TEST_CLI_LIFECYCLE") == "1", "Requires ORR_TEST_CLI_LIFECYCLE=1"
    result = exercise(args.workspace.resolve(), args.mpv.resolve(), owned=args.ownership == "owned", cli=args.cli)
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
