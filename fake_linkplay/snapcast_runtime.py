"""Explicit foreground ORR -> clocked private Pulse -> Snapcast workflow.

No system services, physical sinks, existing players, or native WiiM protocols
are managed here. The normal renderer remains the sole playback/DSP owner.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time

import yaml

from .config import StartupConfigError, parse_startup_yaml, read_startup_config
from .queue_client import PrivateClientArgumentParser

AUDIO_DEVICE = "pulse/orr_snapcast"
MAX_BYTES = 256 * 1024
NATIVE_TOOLS = ("mpv", "pulseaudio", "pactl", "parec", "snapserver")


class RuntimeErrorSafe(RuntimeError):
    """Fixed diagnostics only; never include upstream logs or configuration."""


def literal_address(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value)
        if address.is_unspecified or address.is_multicast or int(address) == 0xFFFFFFFF:
            raise ValueError
        return str(address)
    except ValueError:
        raise argparse.ArgumentTypeError("choose one literal local unicast IPv4 address") from None


def port_number(value: str) -> int:
    try:
        number = int(value)
        if not 1024 <= number <= 65535:
            raise ValueError
        return number
    except ValueError:
        raise argparse.ArgumentTypeError("choose an unprivileged port from 1024 to 65535") from None


def client_volume(value: str) -> int:
    try:
        number = int(value)
        if not 0 <= number <= 100:
            raise ValueError
        return number
    except ValueError:
        raise argparse.ArgumentTypeError("choose an initial client volume from 0 to 100") from None


def read_config(path: str) -> tuple[object, dict]:
    """Capture and validate once, without creating identity or saved state."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise RuntimeErrorSafe("Renderer configuration must be a regular file.")
            encoded = source.read(MAX_BYTES + 1)
        if len(encoded) > MAX_BYTES:
            raise RuntimeErrorSafe("Renderer configuration exceeds the size limit.")
        text = encoded.decode("utf-8")
        cfg = read_startup_config(path, captured_text=text)
        raw = parse_startup_yaml(text) or {}
    except (OSError, UnicodeError):
        raise RuntimeErrorSafe("Renderer configuration could not be read.") from None
    if cfg.auto_spawn_mpv is not True or cfg.mpv_audio_device != AUDIO_DEVICE:
        raise RuntimeErrorSafe("Require auto_spawn_mpv: true and mpv_audio_device: pulse/orr_snapcast.")
    # The launcher creates neither a physical output nor another service. A
    # separate renderer using the same state must be stopped by its operator.
    for value in (cfg.http_api_port, cfg.upnp_port):
        if type(value) is not int or not 1024 <= value <= 65535:
            raise RuntimeErrorSafe("Renderer ports must be unprivileged integers.")
    if cfg.mirror_port_80:
        raise RuntimeErrorSafe("Port-80 mirroring is unsupported in this foreground workflow.")
    return cfg, raw


def require_tools() -> dict[str, str]:
    if sys.platform != "linux" or os.geteuid() == 0:
        raise RuntimeErrorSafe("Run as a regular user on Linux, never as root.")
    result = {}
    for name in NATIVE_TOOLS:
        executable = shutil.which(name)
        if executable is None:
            raise RuntimeErrorSafe(f"Required executable is missing: {name}.")
        result[name] = executable
    return result


def preflight_ports(cfg, stream_bind: str, stream_port: int, control_port: int) -> None:
    ports = {cfg.http_api_port, cfg.upnp_port}
    if stream_port == control_port or stream_port in ports or control_port in ports:
        raise RuntimeErrorSafe("Snapcast and renderer ports must be distinct.")
    # This is not a reservation or an ownership proof. Startup/readiness below
    # still fails closed if a process races for a released port.
    endpoints = [(cfg.bind_address, port) for port in ports]
    endpoints += [(stream_bind, stream_port), ("127.0.0.1", control_port)]
    try:
        for address, port in endpoints:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind((address, port))
    except (OSError, TypeError):
        raise RuntimeErrorSafe("A requested listener is unavailable. Stop the separate service or choose unused ports.") from None


def receive_reply(peer: socket.socket, request: dict) -> dict:
    """One bounded NDJSON exchange; ignore notifications, never retry writes."""
    peer.sendall(json.dumps(request).encode() + b"\n")
    pending = b""
    total = 0
    deadline = time.monotonic() + 2
    while total <= MAX_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        peer.settimeout(remaining)
        block = peer.recv(min(4096, MAX_BYTES + 1 - total))
        if not block:
            break
        total += len(block)
        pending += block
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            reply = json.loads(line)
            if isinstance(reply, dict) and reply.get("id", reply.get("request_id")) == request.get("id", request.get("request_id")):
                return reply
    raise RuntimeErrorSafe("A native readiness response was missing or oversized.")


class Runtime:
    """Own only Popen-created sessions and one private temporary directory."""

    def __init__(self, directory: Path, cfg, raw: dict, tools: dict, args):
        self.directory, self.cfg, self.tools, self.args = directory, cfg, tools, args
        self.children: dict[str, subprocess.Popen] = {}
        self.logs = []
        self.stopping = False
        self.closed = False
        self.recorder_bytes = 0
        self.recorder_progress_at = time.monotonic()
        self.stream_id = "orr-" + secrets.token_hex(16)
        self.socket_path = directory / "mpv.sock"
        self.pulse_endpoint = directory / "pulse" / "native"
        self.fifo = directory / "source.fifo"
        self.renderer_config = directory / "renderer.yaml"
        self.server_config = directory / "snapserver.conf"
        self.env = os.environ.copy()
        for key in tuple(self.env):
            if key.startswith("PULSE_"):
                del self.env[key]
        self.env.update(PULSE_SERVER=f"unix:{self.pulse_endpoint}",
                        PULSE_CLIENTCONFIG=str(directory / "client.conf"),
                        PULSE_COOKIE=str(directory / "cookie"))
        self.native_env = dict(self.env, HOME=str(directory / "home"),
            XDG_CONFIG_HOME=str(directory / "config"),
            PULSE_RUNTIME_PATH=str(directory / "pulse"),
            PULSE_STATE_PATH=str(directory / "pulse-state"),
            PULSE_CONFIG=str(directory / "daemon.conf"),
            DBUS_SYSTEM_BUS_ADDRESS=f"unix:path={directory}/no-dbus",
            DBUS_SESSION_BUS_ADDRESS=f"unix:path={directory}/no-dbus", LC_ALL="C")
        for name in ("home", "config", "pulse", "pulse-state", "server-state"):
            (directory / name).mkdir(mode=0o700)
        self.write("cookie", bytes(256))
        self.write("daemon.conf", "default-sample-rate = 48000\nalternate-sample-rate = 48000\n"
            "default-sample-format = s16le\nresample-method = trivial\navoid-resampling = no\n")
        self.write("client.conf", f"autospawn = no\ndefault-server = unix:{self.pulse_endpoint}\n"
            f"enable-shm = no\ncookie-file = {directory}/cookie\n")
        # CWD is preserved when launching the renderer, so all relative state
        # and media paths retain the ordinary `serve --config` semantics.
        copied = dict(raw, mpv_socket=str(self.socket_path))
        self.write("renderer.yaml", yaml.safe_dump(copied, sort_keys=False))
        self.write("snapserver.conf", f"""[server]
threads = 2
datadir = {directory}/server-state
[http]
enabled = false
[tcp]
enabled = true
bind_to_address = 127.0.0.1
port = {args.control_port}
[stream]
bind_to_address = {args.stream_bind}
port = {args.stream_port}
source = pipe://{self.fifo}?name={self.stream_id}&mode=read
sampleformat = 48000:16:2
codec = pcm
buffer = 1000
chunk_ms = 20
send_to_muted = true
[streaming_client]
initial_volume = {args.initial_client_volume}
[logging]
sink = stderr
filter = *:warning
""")
        os.mkfifo(self.fifo, 0o600)

    def write(self, name: str, data: str | bytes) -> None:
        with open(self.directory / name, "xb") as target:
            os.chmod(target.fileno(), 0o600)
            target.write(data.encode() if isinstance(data, str) else data)

    def launch(self, name: str, command: list[str], *, stdout=None, renderer=False) -> subprocess.Popen:
        self.check_alive()
        log = open(self.directory / f"{name}.log", "xb")
        os.chmod(log.fileno(), 0o600)
        self.logs.append(log)
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL,
            stdout=log if stdout is None else stdout, stderr=log,
            env=self.env if renderer else self.native_env, start_new_session=True)
        self.children[name] = child
        return child

    @staticmethod
    def child_exited(child: subprocess.Popen) -> bool:
        """Observe without reaping: the retained leader prevents PID/PGID reuse."""
        if child.returncode is not None:
            raise RuntimeErrorSafe("Child ownership was lost before group cleanup.")
        try:
            return os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except ChildProcessError:
            raise RuntimeErrorSafe("Child ownership could not be confirmed for cleanup.") from None

    def check_alive(self) -> None:
        if self.stopping:
            raise InterruptedError
        for name, child in self.children.items():
            if self.child_exited(child):
                raise RuntimeErrorSafe(f"Owned {name} exited; the complete source session has been stopped.")

    def inspect_pulse(self, *parameters: str) -> str | None:
        self.check_alive()
        try:
            result = subprocess.run([self.tools["pactl"], "--server", self.env["PULSE_SERVER"], *parameters],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=self.native_env, timeout=2, check=True)
            if len(result.stdout) > MAX_BYTES:
                raise RuntimeErrorSafe("Private PulseAudio inspection was oversized.")
            return result.stdout.decode()
        except subprocess.CalledProcessError:
            # The native socket can appear before daemon module loading ends.
            return None
        except (subprocess.SubprocessError, UnicodeError):
            raise RuntimeErrorSafe("Private PulseAudio did not acknowledge inspection.") from None

    def pulse_ready(self) -> bool:
        if not self.pulse_endpoint.is_socket():
            return False
        modules = self.inspect_pulse("list", "short", "modules")
        if modules is None:
            return False
        names = [line.split("\t")[1] for line in modules.strip().splitlines()]
        expected = {"module-null-sink", "module-native-protocol-unix"}
        if len(names) != len(set(names)) or set(names) - expected:
            raise RuntimeErrorSafe("Private PulseAudio has an unexpected module set.")
        if set(names) != expected:
            return False
        sinks = self.inspect_pulse("list", "short", "sinks")
        if not sinks:
            return False
        sinks = sinks.strip().splitlines()
        if len(sinks) != 1 or sinks[0].split("\t")[1] != "orr_snapcast" or "s16le 2ch 48000Hz" not in sinks[0]:
            raise RuntimeErrorSafe("Private PulseAudio has an unexpected output.")
        return True

    def server_ready(self) -> bool:
        with socket.create_connection(("127.0.0.1", self.args.control_port), timeout=2) as peer:
            reply = receive_reply(peer, {"id": 1, "jsonrpc": "2.0", "method": "Server.GetStatus"})
        streams = reply["result"]["server"]["streams"]
        if len(streams) != 1 or streams[0]["id"] != self.stream_id:
            raise RuntimeErrorSafe("Snapserver identity did not match the owned source.")
        # 0.31 labels continuous all-zero PCM as idle. Silence is valid while
        # ORR is stopped/paused; byte progression is checked independently.
        return streams[0]["status"] in {"playing", "idle"}

    def recorder_progress(self) -> bool:
        data = Path(f"/proc/{self.children['recorder'].pid}/io").read_text()
        match = re.search(r"^wchar:\s+(\d+)$", data, re.M)
        if not match:
            raise RuntimeErrorSafe("Owned PCM recorder progress could not be inspected.")
        count = int(match[1])
        if count >= self.recorder_bytes + 4096:
            self.recorder_bytes = count
            self.recorder_progress_at = time.monotonic()
        return self.recorder_bytes >= 4096 and time.monotonic() - self.recorder_progress_at < 3

    def renderer_ready(self) -> bool:
        if not self.socket_path.is_socket():
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(2)
            peer.connect(str(self.socket_path))
            pid, uid, _gid = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            # The renderer itself already checks this child at every explicit
            # output command. Independently reject a foreign readiness peer.
            status = Path(f"/proc/{pid}/status").read_text()
            parent = re.search(r"^PPid:\s+(\d+)$", status, re.M)
            if uid != os.getuid() or not parent or int(parent[1]) != self.children["renderer"].pid:
                raise RuntimeErrorSafe("Renderer output peer is not its owned mpv child.")
            reply = receive_reply(peer, {"request_id": 1, "command": ["get_property", "audio-device"]})
            if reply.get("error") != "success" or reply.get("data") != AUDIO_DEVICE:
                raise RuntimeErrorSafe("Renderer output did not acknowledge the private Pulse route.")
        return True

    def wait_ready(self, check, label: str, timeout=10) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check_alive()
            try:
                if check():
                    self.check_alive()
                    return
            except (ConnectionRefusedError, FileNotFoundError):
                pass
            time.sleep(.05)
        raise RuntimeErrorSafe(f"Owned {label} did not become ready.")

    def start(self) -> None:
        # Only the narrowly reviewed 0.27/0.31 configuration/API layouts. A
        # future layout must not silently expose default listeners. 0.31 uses
        # the same keys and keeps HTTPS off by default in its own public config.
        try:
            result = subprocess.run([self.tools["snapserver"], "--config", str(self.server_config), "--version"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=self.native_env, timeout=2, check=True)
        except subprocess.SubprocessError:
            raise RuntimeErrorSafe("Snapserver version could not be checked.") from None
        if not re.match(rb"snapserver v0\.(27|31)\.0(?:\s|$)", result.stdout):
            raise RuntimeErrorSafe("This workflow requires Snapserver 0.27.0 or 0.31.0 with the documented config/API.")
        self.launch("pulse", [self.tools["pulseaudio"], "-n", "--daemonize=no", "--use-pid-file=no",
            "--exit-idle-time=-1", "--disable-shm=yes", "--realtime=no", "--high-priority=no",
            "--log-target=stderr", "--log-level=warning",
            f"--load=module-native-protocol-unix socket={self.pulse_endpoint} auth-anonymous=yes auth-cookie={self.directory}/cookie",
            "--load=module-null-sink sink_name=orr_snapcast rate=48000 channels=2 format=s16le norewinds=true"])
        self.wait_ready(self.pulse_ready, "PulseAudio")
        self.launch("snapserver", [self.tools["snapserver"], "--config", str(self.server_config)])
        descriptor = os.open(self.fifo, os.O_RDWR | os.O_NONBLOCK)
        try:
            os.set_blocking(descriptor, True)
            self.launch("recorder", [self.tools["parec"], "--server", self.env["PULSE_SERVER"],
                "--device=orr_snapcast.monitor", "--raw", "--format=s16le", "--rate=48000",
                "--channels=2", "--latency-msec=20"], stdout=descriptor)
        finally:
            os.close(descriptor)
        self.wait_ready(self.server_ready, "Snapserver")
        self.wait_ready(self.recorder_progress, "clocked PCM recorder")
        self.launch("renderer", [sys.executable, "-m", "fake_linkplay.cli", "serve",
            "--config", str(self.renderer_config)], renderer=True)
        self.wait_ready(self.renderer_ready, "renderer")
        # Let startup bind its HTTP listeners after mpv initialization. A port
        # conflict exits the renderer and is caught before announcing readiness.
        self.wait_ready(self.http_ready, "renderer HTTP")

    def http_ready(self) -> bool:
        # Confirm the *owned renderer* has the listening socket. A foreign
        # server racing for the same port must never satisfy readiness.
        pid = self.children["renderer"].pid
        inodes = set()
        for item in Path(f"/proc/{pid}/fd").iterdir():
            try:
                target = os.readlink(item)
            except FileNotFoundError:
                continue
            if target.startswith("socket:["):
                inodes.add(target[8:-1])
        ports = set()
        for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == "0A" and fields[9] in inodes:
                ports.add(int(fields[1].split(":")[1], 16))
        return {self.cfg.http_api_port, self.cfg.upnp_port} <= ports

    def close(self) -> None:
        """Retain leaders through all group signals, then reap their exact PIDs."""
        if self.closed:
            return
        self.closed = True
        # No name discovery, persisted PID, poll(), or wait() before signaling.
        # A zombie leader still reserves its PID/PGID until we explicitly reap it.
        owned = []
        error = None
        for child in reversed(self.children.values()):
            try:
                self.child_exited(child)
                owned.append(child)
                os.killpg(child.pid, signal.SIGTERM)
            except (ProcessLookupError, RuntimeErrorSafe) as exc:
                error = exc
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if all(self.child_exited(child) for child in owned):
                break
            time.sleep(.05)
        # Even if the leader exited, descendants may remain. Its unreaped PID
        # still anchors the group. Never signal this numeric group after wait().
        for child in owned:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for child in owned:
            child.wait(timeout=2)
        for log in self.logs:
            log.close()
        if error is not None:
            raise RuntimeErrorSafe("An owned process group could not be confirmed during cleanup.") from None


def main(argv=None) -> int:
    parser = PrivateClientArgumentParser(prog="open-room-renderer-snapcast",
        description="Opt-in Linux foreground source; operator supplies Snapclients. No physical output or service changes.")
    parser.add_argument("command", choices=("check", "run"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--stream-bind", type=literal_address, default="127.0.0.1",
                        help="explicit trusted local interface IPv4; default is loopback only")
    parser.add_argument("--stream-port", type=port_number, default=1704)
    parser.add_argument("--control-port", type=port_number, default=1705,
                        help="loopback-only Snapcast JSON-RPC port")
    parser.add_argument("--initial-client-volume", type=client_volume, default=0,
                        help="new per-session client volume, 0..100 (default: 0, silent)")
    args = parser.parse_args(argv)
    runtime = None
    previous = {}
    pending_stop = False

    def stop(_signum, _frame):
        nonlocal pending_stop
        pending_stop = True
        if runtime is not None:
            runtime.stopping = True

    try:
        cfg, raw = read_config(args.config)
        executables = require_tools()
        preflight_ports(cfg, args.stream_bind, args.stream_port, args.control_port)
        if args.command == "check":
            print("Input, executable paths and available listener addresses checked. No processes, state or audio started; native versions/readiness remain unchecked.")
            return 0
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            previous[sig] = signal.signal(sig, stop)
        with tempfile.TemporaryDirectory(prefix="orr-snapcast-", dir="/tmp") as directory:
            try:
                runtime = Runtime(Path(directory), cfg, raw, executables, args)
                runtime.stopping = pending_stop
                runtime.start()
                print("READY: ORR source on private clocked Pulse -> Snapcast. Connect your Snapclients to the selected stream interface/port. No physical output or two-host sync verified.", flush=True)
                while not runtime.stopping:
                    runtime.check_alive()
                    if not runtime.server_ready() or not runtime.renderer_ready() or not runtime.recorder_progress():
                        raise RuntimeErrorSafe("Source readiness was lost; the complete session has been stopped.")
                    time.sleep(1)
            finally:
                if runtime is not None:
                    runtime.close()
        return 0
    except InterruptedError:
        return 0
    except (StartupConfigError, RuntimeErrorSafe) as exc:
        print(f"Snapcast source failed: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.SubprocessError):
        print("Snapcast source failed: native startup/response could not be confirmed. Check installed versions and configured listeners.", file=sys.stderr)
        return 2
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
