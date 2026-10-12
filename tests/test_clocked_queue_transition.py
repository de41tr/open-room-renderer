"""Bounded AP16 measurement, not a blanket gapless claim.

Native opt-in: ORR_TEST_CLOCKED_QUEUE=1 PYTHONPATH=. .venv/bin/pytest -q -s
    tests/test_clocked_queue_transition.py
Requires Linux, mpv, pulseaudio and pulseaudio-utils distro packages. Explicit
opt-in makes unavailable Unix IPC or missing programs FAIL, not skip/fallback.
The probe uses an ephemeral, isolated, digital-only PulseAudio null sink.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys

import pytest

from queue_transition_probe import AMPLITUDE, GAP_TOLERANCE_FRAMES, RATE, measure_capture, source_frames


def _float_source(sign: int, count: int) -> bytes:
    return b"".join(struct.pack("<ff", sign * AMPLITUDE, AMPLITUDE / 2 if (index // 60) % 2 == 0 else -AMPLITUDE / 2)
                    for index in range(count))


@pytest.mark.parametrize("gap", [0, 1, GAP_TOLERANCE_FRAMES, GAP_TOLERANCE_FRAMES + 1, 6000])
def test_transition_analyser_counts_clocked_silence(gap):
    count = RATE // 4
    raw = bytes(100 * 8) + _float_source(1, count) + bytes(gap * 8) + _float_source(-1, count) + bytes(100 * 8)
    result = measure_capture(raw, expected_frames=count)
    assert result["gap_frames"] == result["silence_frames"] == result["longest_silence_frames"] == gap
    assert result["outcome"] == ("GAP_OBSERVED" if gap > GAP_TOLERANCE_FRAMES else "WITHIN_TOLERANCE")
    assert all(track["frame_count_delta"] == 0 for track in result["tracks"])


def test_transition_analyser_does_not_call_non_silent_boundary_a_gapless_pass():
    count = RATE // 4
    ambiguous = struct.pack("<ff", AMPLITUDE / 3, AMPLITUDE / 3) * 1000
    result = measure_capture(_float_source(1, count) + ambiguous + _float_source(-1, count), expected_frames=count)
    assert result["gap_frames"] == 1000
    assert result["silence_frames"] == 0
    assert result["outcome"] == "NON_SILENT_BOUNDARY"


def test_transition_analyser_separates_pause_from_transition():
    count = RATE // 4
    first = _float_source(1, count)
    raw = first[:count * 4] + bytes(RATE * 8) + first[count * 4:] + bytes(240 * 8) + _float_source(-1, count)
    result = measure_capture(raw, expected_frames=count)
    assert result["gap_frames"] == 240
    assert result["tracks"][0]["internal_nonsignal_frames"] == RATE


@pytest.mark.parametrize("raw", [b"", b"x", _float_source(1, 12000), _float_source(-1, 12000) + _float_source(1, 12000), struct.pack("<ff", float("nan"), 0)])
def test_transition_analyser_rejects_unusable_capture(raw):
    with pytest.raises(AssertionError):
        measure_capture(raw, expected_frames=12000)


@pytest.mark.parametrize("corruption", ["constant", "inverted"])
def test_transition_analyser_rejects_corrupt_right_sequence(corruption):
    count = RATE // 4
    captures = []
    for sign in (1, -1):
        frames = list(struct.iter_unpack("<ff", _float_source(sign, count)))
        captures.append(b"".join(struct.pack("<ff", left, AMPLITUDE / 2 if corruption == "constant" else -right)
                                 for left, right in frames))
    with pytest.raises(AssertionError, match="Right-channel sequence corrupted"):
        measure_capture(b"".join(captures), expected_frames=count)


def test_transition_analyser_allows_documented_mixed_rate_edge_tolerance():
    # The private PulseAudio fixture uses the trivial resampler. Simulate its
    # bounded nearest-neighbor edge discrepancy without needing native audio.
    source = list(struct.iter_unpack("<hh", source_frames(44100, -1)))
    mixed = b"".join(struct.pack("<ff", *(sample / 32768 for sample in source[index * 44100 // RATE]))
                     for index in range(RATE * 2))
    result = measure_capture(_float_source(1, RATE * 2) + mixed)
    assert result["tracks"][0]["right_sequence_sign_match_fraction"] == 1
    assert result["tracks"][1]["right_sequence_sign_match_fraction"] == pytest.approx(.9854166667)
    assert result["outcome"] == "WITHIN_TOLERANCE"


@pytest.mark.parametrize("rate", [44100, 48000])
def test_generated_sequence_is_nonzero_quiet_and_two_seconds(rate):
    frames = list(struct.iter_unpack("<hh", source_frames(rate, 1)))
    assert len(frames) == 2 * rate
    assert {left for left, _ in frames} == {256}
    assert {right for _, right in frames} == {-128, 128}


@pytest.mark.skipif(os.environ.get("ORR_TEST_CLOCKED_QUEUE") != "1", reason="Clocked queue lane requires explicit ORR_TEST_CLOCKED_QUEUE=1")
def test_real_clocked_queue_transition():
    assert sys.platform == "linux", "Explicit clocked native lane requires Linux"
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    # All fixture children inherit this new process group. Kill the entire
    # group on timeout so even a native deadlock cannot orphan audio services.
    process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("queue_transition_probe.py"))],
        cwd=root, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=95)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=5)
        pytest.fail(f"Clocked queue probe exceeded hard deadline\n{stdout}\n{stderr}")
    assert process.returncode == 0, f"Clocked queue measurement failed\nstdout:\n{stdout}\nstderr:\n{stderr}"
    evidence = json.loads(stdout)
    assert set(evidence["cases"]) == {"equal_rate", "mixed_rate", "pause_near_transition"}
    assert evidence["cleanup"] == {"children_reaped": True, "http_monitor_done": True, "no_pending_tasks": True}
    assert evidence["known_gap_control"]["outcome"] == "GAP_OBSERVED"
    # Current ORR may have a real gap. Do not encode that bug as the desired
    # behavior, nor relabel a passing measurement test as a gapless pass.
    print(json.dumps(evidence, sort_keys=True))
