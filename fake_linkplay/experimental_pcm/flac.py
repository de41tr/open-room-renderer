"""Finite standard-FLAC file consumer, unrelated to any native packet format.

FFmpeg owns codec decoding; we inspect only the mandatory first STREAMINFO block
to bound output and detect complete-frame truncation. No URLs or device routing.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
import selectors
import stat
import subprocess
import sys
import time

from .alsa import AlsaPcmSink
from .diagnostic import consume
from .playout import PcmFormat, PcmPlayout, PlayoutError

MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_FRAMES = 2_880_000
DECODE_SECONDS = 15.0
BLOCK_FRAMES = 4096


def _streaminfo(fd: int) -> tuple[PcmFormat, int, int]:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or not 42 <= info.st_size <= MAX_INPUT_BYTES:
        raise ValueError("bounded regular FLAC input required")
    # fLaC marker, first metadata block header, and its 34-byte STREAMINFO.
    # pread leaves the descriptor positioned at the beginning for FFmpeg.
    header = os.pread(fd, 42, 0)
    if (len(header) != 42 or header[:4] != b"fLaC" or header[4] & 0x7f != 0
            or int.from_bytes(header[5:8], "big") != 34):
        raise ValueError("standard FLAC STREAMINFO required")
    packed = int.from_bytes(header[18:26], "big")
    format = PcmFormat(rate=packed >> 44, channels=((packed >> 41) & 7) + 1)
    source_bits = ((packed >> 36) & 31) + 1
    frames = packed & ((1 << 36) - 1)
    if source_bits < 4:
        raise ValueError("standard FLAC bit depth required")
    if not 1 <= frames <= MAX_OUTPUT_FRAMES:
        raise ValueError("known bounded FLAC sample count required")
    return format, frames, source_bits


def _decode(fd: int, playout: PcmPlayout, expected_frames: int, deadline: float) -> str:
    """Consume one owned child's output with only a bounded frame-aligned block."""
    # Use the distribution executable on PATH, as the mpv backend does. There is
    # no command/argument override. The original filename is never given to it.
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats",
        "-xerror", "-err_detect", "crccheck+explode", "-threads", "1",
        "-f", "flac", "-protocol_whitelist", "pipe", "-i", "pipe:0",
        "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1",
        "-threads", "1", "-acodec", "pcm_s16le", "-ar", str(playout.format.rate),
        "-ac", str(playout.format.channels), "-f", "s16le", "pipe:1",
    ]
    env = dict(os.environ)
    env.pop("FFREPORT", None)  # Do not create ambient FFmpeg diagnostic files.
    digest = hashlib.sha256()
    cursor = 0
    carry = b""
    frame_bytes = playout.format.frame_bytes
    process = subprocess.Popen(command, stdin=fd, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=env)
    try:
        assert process.stdout is not None
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError("FLAC decode deadline exceeded")
                chunk = os.read(process.stdout.fileno(), BLOCK_FRAMES * frame_bytes - len(carry))
                if not chunk:
                    break
                data = carry + chunk
                aligned = len(data) - len(data) % frame_bytes
                carry = data[aligned:]
                if aligned:
                    frames = aligned // frame_bytes
                    if cursor + frames > expected_frames:
                        raise PlayoutError("FLAC output exceeds declared frame count")
                    pcm = data[:aligned]
                    consume(playout, pcm, first_frame=cursor, deadline=deadline)
                    cursor += frames
                    digest.update(pcm)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("FLAC decode deadline exceeded")
        if process.wait(timeout=remaining) != 0 or carry or cursor != expected_frames:
            raise PlayoutError("FLAC decode incomplete or invalid")
        return digest.hexdigest()
    finally:
        # Kill/reap only the exact child we created, including Ctrl-C, timeout,
        # sink failure and excess output. No process-name or group signalling.
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=1.0)
        finally:
            process.stdout.close()


def run(flac_path: str, capture_fd: int | None = None) -> dict:
    if not stat.S_ISREG(os.lstat(flac_path).st_mode):
        raise ValueError("regular nonsymlink FLAC input required")
    # Recheck the opened object below; O_NONBLOCK avoids a FIFO replacement hang.
    fd = os.open(flac_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        format, frames, source_bits = _streaminfo(fd)
        deadline = time.monotonic() + DECODE_SECONDS
        with PcmPlayout(AlsaPcmSink(format, capture_fd=capture_fd),
                        max_block_frames=BLOCK_FRAMES) as playout:
            digest = _decode(fd, playout, frames, deadline)
            observation = playout.observe()
        return {
            "schema": 1, "input": "local_standard_flac",
            "output": "alsa_file_on_null" if capture_fd is not None else "alsa_null",
            "sample_format": "S16_LE", "sample_rate": format.rate, "channels": format.channels,
            "source_bits_per_sample": source_bits,
            "written_frames": frames, "decoded_pcm_bytes": frames * format.frame_bytes,
            "decoded_pcm_sha256": digest, "final_observation": asdict(observation),
            "limits": [
                "standard FLAC file only; no native packet framing or transport proof",
                "null consumes immediately; no physical output or sample-clock proof",
                "rate/channels preserved; higher bit depths converted to S16_LE",
            ],
        }
    finally:
        os.close(fd)


def _verify_capture(fd: int, report: dict) -> bool:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    remaining = report["decoded_pcm_bytes"]
    while remaining:
        data = os.read(fd, min(remaining, 65536))
        if not data:
            return False
        digest.update(data)
        remaining -= len(data)
    return not os.read(fd, 1) and digest.hexdigest() == report["decoded_pcm_sha256"]


def run_pcm_decode(args) -> int:
    """Fixed private errors; new-only captures are explicitly partial on failure."""
    capture_fd = None
    try:
        if args.capture is not None:
            capture_fd = os.open(args.capture, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_CLOEXEC, 0o600)
        report = run(args.flac, capture_fd)
        if capture_fd is not None:
            report["capture_verified"] = _verify_capture(capture_fd, report)
            if not report["capture_verified"]:
                raise PlayoutError("capture did not match decoded PCM")
    except FileExistsError:
        print("PCM decode failed: capture already exists; choose a new file.", file=sys.stderr)
        return 2
    except (OSError, PlayoutError, ValueError, TimeoutError, subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        print("PCM decode failed: finite local FLAC output was not verified.", file=sys.stderr)
        if capture_fd is not None:
            print("The newly created capture may be partial; do not use it as verified output.", file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1
    finally:
        if capture_fd is not None:
            os.close(capture_fd)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0
