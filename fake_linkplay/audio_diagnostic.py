"""Explicit, read-only local output diagnosis; never a playback health check."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .config import StartupConfigError, read_startup_config
from .mpv_backend import MpvBackend


def add_audio_parser(subparsers) -> None:
    parser = subparsers.add_parser("audio", help="inspect configured and live output intent")
    commands = parser.add_subparsers(dest="audio_cmd", required=True)
    diagnose = commands.add_parser(
        "diagnose", help="read two mpv output properties without starting playback",
        description=(
            "Read explicit on-disk configuration and, with a two-second shared IPC budget, "
            "audio-device and current-ao from its local mpv socket. No player startup, "
            "saved-state loading, playback commands or device changes. This cannot verify "
            "the running renderer's configuration, ownership, physical DAC or audible output."
        ),
    )
    diagnose.add_argument("--config", required=True, help="explicit readable YAML file; no default fallback")


def run_audio_diagnostic(args: argparse.Namespace) -> int:
    """Keep config/socket paths and raw backend diagnostics out of all output."""
    try:
        cfg = read_startup_config(args.config)
        if (not isinstance(cfg.mpv_socket, str) or not cfg.mpv_socket
                or "\0" in cfg.mpv_socket or type(cfg.auto_spawn_mpv) is not bool):
            raise StartupConfigError("Audio diagnosis requires a local socket path and boolean auto_spawn_mpv.")
    except StartupConfigError as exc:
        print(f"Audio diagnosis failed: {exc}", file=sys.stderr)
        return 2

    # Deliberately do not pass cfg.mpv_audio_device: it configures an owned
    # child at startup, whereas this separate read-only client owns no player.
    backend = MpvBackend(cfg.mpv_socket, auto_spawn=False)
    live = asyncio.run(backend.diagnose_audio_output())
    complete = (live["connection"] == "connected"
                and live["socket_path_check"] == "unchanged_at_checks"
                and all(live[key]["status"] in {"value", "unavailable", "unsupported"}
                        for key in ("audio_device", "current_ao")))
    intent = live["audio_device"]
    comparison = "not_compared"
    if cfg.mpv_audio_device is not None and intent["status"] == "value":
        comparison = "matches" if cfg.mpv_audio_device == intent["value"] else "differs"
    driver = live["current_ao"]
    report = {
        "schema": 1, "status": "complete" if complete else "incomplete",
        "configuration": {
            "source": "on_disk_may_differ_from_running_service",
            "mpv_audio_device": cfg.mpv_audio_device,
            "auto_spawn_mpv": cfg.auto_spawn_mpv,
        },
        "live_mpv": live,
        "interpretation": {
            "configuration_vs_live_intent": comparison,
            "null_output": "deliberately_silent" if driver.get("value") == "null" else "not_observed",
            "observations": "sequential_not_atomic",
            "renderer_ownership": "unverified",
            "unavailable_driver": "can_be_idle_not_proof_of_playback_failure",
            "audible_output": "not_verified", "physical_device": "not_verified",
        },
    }
    print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if complete else 1
