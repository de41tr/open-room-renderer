"""Finite generated-PCM consumer with real ALSA null/file output.

This is a digital interface diagnostic, not a real-time or audible playback
claim. No input packets, credentials, device selection, or networking exists.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
import struct
import sys
import time

from .alsa import AlsaPcmSink
from .playout import PcmFormat, PcmPlayout, PlayoutError, StaleGeneration


def generated_pcm(frames: int, *, phase: int) -> bytes:
    """Original deterministic stereo sawtooth, safely below full scale."""
    return b"".join(struct.pack("<hh", value, -value)
                    for i in range(frames)
                    for value in [(((i + phase) % 97) - 48) * 100])


def consume(playout: PcmPlayout, pcm: bytes, *, first_frame: int,
            deadline: float | None = None) -> None:
    """Consumer owns backpressure/deadline; the adapter holds one bounded block."""
    playout.submit(pcm, first_frame=first_frame, generation=playout.generation)
    block_deadline = time.monotonic() + 1.0
    deadline = min(deadline, block_deadline) if deadline is not None else block_deadline
    while playout.pending_frames:
        if time.monotonic() >= deadline:
            raise TimeoutError("PCM consumer made insufficient progress")
        if playout.step() == 0:
            time.sleep(0.001)


def run(capture_fd: int | None = None) -> dict:
    first = generated_pcm(256, phase=0)
    discarded = generated_pcm(640, phase=31)
    second = generated_pcm(512, phase=67)
    with PcmPlayout(AlsaPcmSink(PcmFormat(), capture_fd=capture_fd)) as playout:
        consume(playout, first, first_frame=0)
        before = playout.observe()
        old_generation = playout.generation
        # This pending block has NOT been written to ALSA and must never reach
        # the capture. Already consumed samples cannot be retracted by flush.
        playout.submit(discarded, first_frame=256, generation=old_generation)
        generation = playout.flush(start_frame=10_000)
        stale_rejected = False
        try:
            playout.submit(discarded, first_frame=10_000, generation=old_generation)
        except StaleGeneration:
            stale_rejected = True
        consume(playout, second, first_frame=10_000)
        after = playout.observe()
        anchor = after.output_anchor()
        if (not stale_rejected or before.accepted_frame != 256 or after.accepted_frame != 10512
                or after.pending_frames != 0 or generation != 1):
            raise PlayoutError("generated PCM cursor/flush contract did not hold")
    expected = first + second
    return {
        "schema": 1,
        "input": "original_generated_stereo_s16le",
        "output": "alsa_file_on_null" if capture_fd is not None else "alsa_null",
        "sample_rate": 48_000,
        "written_frames": 768,
        "discarded_application_frames": 640,
        "flush_generation": generation,
        "stale_generation_rejected": stale_rejected,
        "before_flush": asdict(before),
        "after_flush": asdict(after),
        "final_output_anchor": asdict(anchor),
        "final_anchor_write_error_frames": after.write_error_frames(anchor),
        "expected_capture_bytes": len(expected),
        "expected_capture_sha256": hashlib.sha256(expected).hexdigest(),
        "limits": [
            "null consumes immediately; no physical sample-clock proof",
            "delay comes from actual libasound, with host-call timestamp bracketing",
            "flush tests app pending discard and real driver drop/prepare, not physical buffered-audio removal",
            "experimental PCM boundary; no runtime source switching, compressed codec or clock servo",
        ],
    }


def run_pcm_check(args) -> int:
    """Installed CLI consumer; fixed safe errors omit paths and native details."""
    try:
        if args.capture is None:
            report = run()
        else:
            fd = os.open(args.capture, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_CLOEXEC, 0o600)
            try:
                report = run(fd)
                os.lseek(fd, 0, os.SEEK_SET)
                actual = os.read(fd, report["expected_capture_bytes"] + 1)
                report["capture_verified"] = (len(actual) == report["expected_capture_bytes"]
                    and hashlib.sha256(actual).hexdigest() == report["expected_capture_sha256"])
                if not report["capture_verified"]:
                    raise PlayoutError("capture did not match generated PCM")
            finally:
                os.close(fd)
    except FileExistsError:
        print("PCM check failed: capture already exists; choose a new file.", file=sys.stderr)
        return 2
    except (OSError, PlayoutError, ValueError, TimeoutError):
        print("PCM check failed: generated digital output was not verified.", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0
