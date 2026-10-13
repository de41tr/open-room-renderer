#!/usr/bin/env python3
"""One disposable installed offline-preparation smoke for the native-install lane."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import yaml

import fake_linkplay.migration_profile as migration


def main() -> None:
    source, destination = map(Path, sys.argv[1:])
    assert Path(migration.__file__).resolve().is_relative_to(destination / "venv")
    state = {
        "uuid": b"synthetic-upgrade-smoke-identity\n", "device_name": b"Upgrade smoke\n",
        "channel_balance": b"0.0\n", "gain.json": b'{"volume": 23, "mute": false}\n',
        "active_dsp.json": b"{}\n", "eq_presets.json": b"{}\n", "favorites.json": b"[]\n",
    }
    for name, data in state.items():
        (source / "state" / name).write_bytes(data)
    for name in migration.SKIP_FILES:
        (source / "state" / name).write_bytes(b"synthetic omitted private state\n")
    raw = json.loads((source / "config.yaml").read_text())
    raw.update(friendly_name="Upgrade smoke", http_api_port=9090, upnp_port=9090,
               mpv_audio_device="alsa/plughw:CARD=Smoke,DEV=0")
    (source / "config.yaml").write_text(json.dumps(raw))
    before = {p.name: p.read_bytes() for p in (source / "state").iterdir()}
    initial = (destination / "config.yaml").read_bytes()
    command = [sys.executable, "-I", "-m", "fake_linkplay.migration_profile",
               "--source", str(source), "--source-config", str(source / "config.yaml"),
               "--destination", str(destination), "--acknowledge-offline"]
    subprocess.run([*command, "--dry-run"], check=True)
    assert not (destination / "migrated-profile").exists()
    subprocess.run(command, check=True)
    profile = destination / "migrated-profile"
    raw.update(state_dir=str(profile / "state"), mpv_socket=str(destination / "run/mpv.sock"))
    assert yaml.safe_load((profile / "config.yaml").read_text()) == raw
    assert {p.name: p.read_bytes() for p in (profile / "state").iterdir()} == state
    assert {p.name: p.read_bytes() for p in (source / "state").iterdir()} == before
    assert (destination / "config.yaml").read_bytes() == initial
    assert not list((destination / "state").iterdir()) and not list((destination / "run").iterdir())
    receipt = json.loads((destination / "installation-receipt.json").read_text())
    assert receipt["initial_config_sha256"] == hashlib.sha256(initial).hexdigest()
    assert str(profile / "config.yaml") in (profile / "open-room-renderer.service").read_text()
    again = subprocess.run(command, capture_output=True, text=True)
    assert again.returncode == 2 and "already exists" in again.stderr
    print("Installed offline migration dry run, exact seven-file capture, Plex omission, no-clobber and untouched source/default config verified.")
    print("No renderer, mpv, service, network listener or physical audio was activated.")


if __name__ == "__main__":
    main()
