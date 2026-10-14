"""Read-only output diagnosis and a separate experimental digital PCM check."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .config import StartupConfigError, read_startup_config
from .mpv_backend import MpvBackend


def add_audio_parser(subparsers) -> None:
    parser = subparsers.add_parser("audio", help="inspect output intent or check experimental PCM")
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
    pcm = commands.add_parser(
        "pcm-check", help="exercise experimental PCM frames through ALSA null/file output",
        description=(
            "Finite generated stereo PCM check using Linux libasound. Uses only null or "
            "file-on-null output and does not start/control the renderer. This verifies "
            "digital bytes and flush behavior, not physical output or synchronization."
        ),
    )
    pcm.add_argument("--capture", help="create a new raw stereo S16_LE/48 kHz file; never overwrite")
    decode = commands.add_parser(
        "pcm-decode", help="decode one finite local standard FLAC through ALSA null/file output",
        description=(
            "Experimental Linux/FFmpeg consumer for a regular local FLAC file with a known "
            "STREAMINFO frame count. Preserves rate/channels, converts to S16_LE, and opens "
            "only ALSA null or file-on-null. Maximum 64 MiB input, 2880000 frames and 15 seconds "
            "decode time. No URLs, physical output, renderer control or native packet framing."
        ),
    )
    decode.add_argument("--flac", required=True, help="explicit finite standard FLAC file")
    decode.add_argument("--capture", help="create a new raw S16_LE file; never overwrite")


def run_audio_diagnostic(args: argparse.Namespace) -> int:
    """Keep config/socket paths and raw backend diagnostics out of all output."""
    if args.audio_cmd in {"pcm-check", "pcm-decode"}:
        if not sys.platform.startswith("linux"):
            print("PCM check requires Linux libasound.", file=sys.stderr)
            return 1
        # Keep the optional native dependency out of normal renderer startup.
        if args.audio_cmd == "pcm-check":
            from .experimental_pcm.diagnostic import run_pcm_check
            return run_pcm_check(args)
        from .experimental_pcm.flac import run_pcm_decode
        return run_pcm_decode(args)
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
