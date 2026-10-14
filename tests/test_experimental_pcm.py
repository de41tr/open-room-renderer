from collections import deque
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from fake_linkplay.experimental_pcm.alsa import AlsaPcmSink
from fake_linkplay.experimental_pcm.diagnostic import generated_pcm
from fake_linkplay.experimental_pcm.playout import (PcmFormat, PcmPlayout, PlayoutError, PlayoutObservation,
                             SampleAnchor, StaleGeneration)


class ScriptedSink:
    """Contract test double only. Real output acceptance is in subprocess below."""
    format = PcmFormat()

    def __init__(self, writes=()):
        self.writes = deque(writes)
        self.output = bytearray()
        self.delay = 0
        self.resets = 0
        self.closed = 0
        self.reset_error = None

    def write(self, data):
        accepted = self.writes.popleft() if self.writes else len(data) // 4
        if isinstance(accepted, Exception):
            raise accepted
        self.output += data[:accepted * 4]
        return accepted

    def avail_delay(self):
        return 100, self.delay

    def drop_prepare(self):
        self.resets += 1
        if self.reset_error:
            raise self.reset_error

    def close(self):
        self.closed += 1


def test_partial_write_backpressure_cursor_and_signed_delay():
    sink = ScriptedSink([2, 0, 3])
    sink.delay = -3
    data = generated_pcm(5, phase=0)
    with PcmPlayout(sink, start_frame=100) as player:
        player.submit(data, first_frame=100, generation=0)
        assert player.step() == 2
        snapshot = player.observe()
        assert (snapshot.accepted_frame, snapshot.pending_frames) == (102, 3)
        assert (snapshot.delay_frames, snapshot.estimated_output_frame) == (-3, 105)
        assert player.step() == 0
        assert player.observe().accepted_frame == 102
        assert player.step() == 3
        assert player.observe().accepted_frame == 105
        assert sink.output == data
    assert sink.closed == 1
    player.close()
    assert sink.closed == 1
    with pytest.raises(PlayoutError, match="closed"):
        player.step()


def test_flush_discards_only_unaccepted_audio_and_stale_generation():
    sink = ScriptedSink([2])
    old = generated_pcm(5, phase=0)
    new = generated_pcm(3, phase=40)
    with PcmPlayout(sink) as player:
        player.submit(old, first_frame=0, generation=0)
        player.step()
        assert player.flush(start_frame=900) == 1
        assert (player.pending_frames, sink.resets) == (0, 1)
        with pytest.raises(StaleGeneration):
            player.submit(old, first_frame=900, generation=0)
        player.submit(new, first_frame=900, generation=1)
        player.step()
        assert player.observe().accepted_frame == 903
        assert sink.output == old[:8] + new


def test_write_failure_and_failed_flush_never_silently_resume():
    sink = ScriptedSink([1, OSError("synthetic underrun")])
    with PcmPlayout(sink) as player:
        player.submit(generated_pcm(3, phase=0), first_frame=0, generation=0)
        player.step()
        with pytest.raises(OSError):
            player.step()
        with pytest.raises(PlayoutError, match="flush/re-anchor"):
            player.observe()
        sink.reset_error = OSError("synthetic reset failure")
        with pytest.raises(OSError):
            player.flush(start_frame=50)
        assert player.generation == 1
        assert player.pending_frames == 0
        with pytest.raises(PlayoutError):
            player.submit(generated_pcm(1, phase=1), first_frame=50, generation=1)
        sink.reset_error = None
        assert player.flush(start_frame=75) == 2
        player.submit(generated_pcm(1, phase=1), first_frame=75, generation=2)
        player.step()
        assert player.observe().accepted_frame == 76


def test_bounds_and_contiguity_reject_before_sink_effects():
    sink = ScriptedSink()
    with PcmPlayout(sink, max_block_frames=2) as player:
        for data in (b"", b"a", b"a" * 12, bytearray(4)):
            with pytest.raises(ValueError):
                player.submit(data, first_frame=0, generation=0)
        with pytest.raises(PlayoutError, match="noncontiguous"):
            player.submit(b"\0" * 4, first_frame=1, generation=0)
        player.submit(b"\0" * 4, first_frame=0, generation=0)
        with pytest.raises(PlayoutError, match="pending"):
            player.submit(b"\0" * 4, first_frame=0, generation=0)
        with pytest.raises(ValueError):
            player.flush(start_frame=True)
        assert player.generation == 0
        assert sink.output == b""


def test_integer_timing_signed_truncation_and_nonzero_delay():
    anchor = SampleAnchor(1000, 10**18)
    # Negative sub-frame offsets truncate towards zero, not Python floor.
    assert anchor.frame_at(10**18 - 1, 48_000) == 1000
    assert anchor.frame_at(10**18 - 20_834, 48_000) == 999
    sample = PlayoutObservation(0, 1000, 0, 100, 480, 10**18 - 10, 10**18 + 10, 48_000)
    assert sample.estimated_output_frame == 520
    assert sample.output_anchor().local_ns == 10**18 + 10_000_000
    assert sample.write_error_frames(anchor) == 480
    negative = replace(sample, delay_frames=-480)
    assert negative.output_anchor().local_ns == 10**18 - 10_000_000
    assert negative.estimated_output_frame == 1480
    assert negative.write_error_frames(anchor) == -480
    assert sample.uncertainty_ns == 20
    with pytest.raises(ValueError, match="clock domain"):
        SampleAnchor(0, 0, clock_domain="CLOCK_MONOTONIC_RAW")
    with pytest.raises(ValueError, match="clock domain"):
        replace(sample, clock_domain="CLOCK_MONOTONIC_RAW").output_anchor()


def test_flush_serializes_with_inflight_write():
    entered, release, flushed = threading.Event(), threading.Event(), threading.Event()
    sink = ScriptedSink()
    original_write = sink.write

    def held_write(data):
        entered.set()
        assert release.wait(1)
        return original_write(data)

    sink.write = held_write
    with PcmPlayout(sink) as player:
        player.submit(b"\1\0" * 4, first_frame=0, generation=0)
        writer = threading.Thread(target=player.step)
        writer.start()
        assert entered.wait(1)
        reset = threading.Thread(target=lambda: (player.flush(start_frame=20), flushed.set()))
        reset.start()
        assert not flushed.wait(0.02)
        release.set()
        writer.join(1)
        reset.join(1)
        assert not writer.is_alive() and not reset.is_alive()
        assert flushed.is_set()
        assert player.observe().accepted_frame == 20
        with pytest.raises(StaleGeneration):
            player.submit(b"\0" * 4, first_frame=20, generation=0)


@pytest.mark.skipif(os.environ.get("ORR_TEST_PCM_OUTPUT") != "1", reason="explicit real libasound null/file lane")
def test_real_alsa_consumer_subprocess_capture_and_private_config(tmp_path):
    capture = tmp_path / "generated.pcm"
    invalid_global = tmp_path / "global-not-used.conf"
    invalid_global.write_text("this is intentionally not an ALSA configuration\n")
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
               ALSA_CONFIG_PATH=str(invalid_global))
    process = subprocess.run([sys.executable, "-m", "fake_linkplay.cli", "audio", "pcm-check", "--capture", str(capture)],
                             capture_output=True, text=True, timeout=5, env=env)
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result["capture_verified"] is True
    assert result["stale_generation_rejected"] is True
    assert result["after_flush"]["accepted_frame"] == 10512
    assert result["after_flush"]["delay_frames"] == 0  # null, not a DAC
    assert capture.read_bytes() == generated_pcm(256, phase=0) + generated_pcm(512, phase=67)
    assert capture.stat().st_mode & 0o777 == 0o600
    # Existing captures cannot be replaced by accidentally repeating a command.
    again = subprocess.run([sys.executable, "-m", "fake_linkplay.cli", "audio", "pcm-check", "--capture", str(capture)],
                           capture_output=True, text=True, timeout=5, env=env)
    assert again.returncode == 2
    assert str(capture) not in again.stdout + again.stderr
    assert "Traceback" not in again.stderr
    assert len(capture.read_bytes()) == 3072
    null = subprocess.run([sys.executable, "-m", "fake_linkplay.cli", "audio", "pcm-check"],
                          capture_output=True, text=True, timeout=5, env=env)
    assert null.returncode == 0, null.stderr
    assert json.loads(null.stdout)["output"] == "alsa_null"


@pytest.mark.skipif(os.environ.get("ORR_TEST_PCM_OUTPUT") != "1", reason="explicit real libasound null/file lane")
def test_real_alsa_owns_duplicate_and_rejects_device_or_pipe(tmp_path):
    with (tmp_path / "owned.pcm").open("w+b") as original:
        sink = AlsaPcmSink(PcmFormat(), capture_fd=original.fileno())
        sink.write(b"\0" * 4)
        sink.close()
        sink.close()
        # ALSA/candidate closure must leave the caller's FD usable.
        original.seek(0)
        assert original.read() == b"\0" * 4
    with (tmp_path / "owned.pcm").open("rb") as read_only:
        with pytest.raises(ValueError, match="writable"):
            AlsaPcmSink(PcmFormat(), capture_fd=read_only.fileno())
    read_fd, write_fd = os.pipe()
    try:
        with pytest.raises(ValueError, match="regular file"):
            AlsaPcmSink(PcmFormat(), capture_fd=write_fd)
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_installed_cli_shape_and_missing_native_library_are_private(monkeypatch, capsys):
    from fake_linkplay import cli
    from fake_linkplay.experimental_pcm import diagnostic

    args = cli.build_parser().parse_args(["audio", "pcm-check"])
    assert args.audio_cmd == "pcm-check" and args.capture is None
    secret = "synthetic-private-native-path"

    def unavailable(*_args, **_kwargs):
        raise OSError(secret)

    monkeypatch.setattr(diagnostic, "AlsaPcmSink", unavailable)
    assert cli.main(["audio", "pcm-check"]) == 1
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert "not verified" in output.err and "Traceback" not in output.err
