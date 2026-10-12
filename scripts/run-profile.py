#!/usr/bin/env python3
"""Prepare a stable local profile and run it without implicit privilege changes."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import sys
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[1]


def prepare(profile: str) -> Path:
    explicit = os.environ.get("ORR_CONFIG")
    if explicit:
        path = Path(explicit).expanduser().absolute()
        if not path.is_file():
            raise ValueError(f"Configured YAML file does not exist: {path}")
        return path
    compatibility = profile == "app-compat"
    base = ROOT / "configs" / ("config.app-compat.yaml" if compatibility else "config.example.yaml")
    data = yaml.safe_load(base.read_text())
    if compatibility:
        port80 = os.environ.get("APP_COMPAT_PORT80", "0")
        fresh = os.environ.get("APP_COMPAT_FRESH", "0")
        if port80 not in {"0", "1"} or fresh not in {"0", "1"}:
            raise ValueError("APP_COMPAT_PORT80 and APP_COMPAT_FRESH must be 0 or 1.")
        data["http_api_port"] = data["upnp_port"] = 80 if port80 == "1" else 8080
        data["mirror_port_80"] = False
        if "APP_COMPAT_NAME" in os.environ:
            data["friendly_name"] = data["mdns_instance_name"] = os.environ["APP_COMPAT_NAME"]
        if fresh == "1":
            value = "FF31" + uuid.uuid4().hex.upper()
            data["discovery_uuid"] = data["uuid"] = value
            data["state_dir"] = f".state/app-compat-{value[-8:]}"
        os.environ.setdefault("WIIM_HOME_ACOUSTIC_CAPABILITY_MODE", "peq")
        os.environ.setdefault("WIIM_HOME_AUDIO_OUTPUT_CAPABILITY_MODE", "fail")
    state = (ROOT / data["state_dir"]).absolute()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    data["state_dir"] = str(state)
    data["mpv_socket"] = str(state / "mpv.sock")
    output = state / "runner-config.yaml"
    output.write_text(yaml.safe_dump(data, sort_keys=False))
    return output


def preflight(path: Path) -> dict:
    data = yaml.safe_load(path.read_text()) or {}
    if data.get("auto_spawn_mpv", True) and not shutil.which("mpv"):
        raise ValueError("Missing mpv. Install your distribution's native mpv package.")
    ipc = Path(data.get("mpv_socket", "/tmp/open-room-renderer-mpv.sock"))
    if len(os.fsencode(ipc.absolute())) >= 108:
        raise ValueError("mpv socket path is too long for a Linux Unix socket.")
    if ipc.is_symlink() or (ipc.exists() and not ipc.is_socket()):
        raise ValueError("mpv IPC path is not a socket; choose a private, unused socket path.")
    if not ipc.parent.is_dir() or not os.access(ipc.parent, os.W_OK):
        raise ValueError("mpv IPC parent directory is missing or not writable.")
    ports = {int(data.get("http_api_port", 8080)), int(data.get("upnp_port", 8080))}
    if data.get("mirror_port_80"):
        ports.add(80)
    for port in ports:
        if not 1 <= port <= 65535:
            raise ValueError("HTTP/UPnP ports must be between 1 and 65535.")
        if port < 1024:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                    probe.bind((data.get("bind_address", "0.0.0.0"), port))
            except OSError as exc:
                raise ValueError(
                    f"Cannot bind requested port {port}: {exc}. No sudo is invoked. "
                    "Use port 8080, or review the dedicated non-root system service "
                    "with CAP_NET_BIND_SERVICE in GETTING_STARTED.md."
                ) from exc
    return data


def main() -> int:
    os.chdir(ROOT)
    try:
        path = prepare(sys.argv[1])
        data = preflight(path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Startup error: {exc}", file=sys.stderr)
        return 2
    print(f"Starting Open Room Renderer ({sys.argv[1]})", flush=True)
    print(f"Config: {path}", flush=True)
    print(f"HTTP API port: {data.get('http_api_port', 8080)}; UPnP port: {data.get('upnp_port', 8080)}", flush=True)
    print("No automatic sudo or system setting changes. Use a trusted LAN only.", flush=True)
    executable = ROOT / ".venv/bin/open-room-renderer"
    os.execv(str(executable), [str(executable), "serve", "--config", str(path)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
