"""A bounded decoded-PCM consumer. No protocol, provider, sockets, or clock servo.

A frame is one simultaneous sample per channel. Timestamp/frame origins are
explicit inputs; never interpreted as UTC or inferred from packet bytes.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Protocol

NS_PER_SECOND = 1_000_000_000


def integer(value: int, name: str, minimum: int | None = None) -> int:
    if type(value) is not int or (minimum is not None and value < minimum):
        raise ValueError(f"invalid {name}")
    return value


def trunc_ratio(value: int, denominator: int) -> int:
    """Signed rational conversion towards zero; no float precision loss."""
    integer(value, "numerator")
    integer(denominator, "denominator", 1)
    magnitude = abs(value) // denominator
    return -magnitude if value < 0 else magnitude


@dataclass(frozen=True)
class PcmFormat:
    """Interleaved signed little-endian PCM; S16_LE remains the default."""
    rate: int = 48_000
    channels: int = 2
    sample_format: str = "S16_LE"

    def __post_init__(self) -> None:
        integer(self.rate, "sample rate", 1)
        integer(self.channels, "channel count", 1)
        if self.rate > 192_000 or self.channels > 8:
            raise ValueError("PCM format outside experimental limits")
        if self.sample_format not in ("S16_LE", "S32_LE"):
            raise ValueError("unsupported PCM sample format")

    @property
    def frame_bytes(self) -> int:
        return self.channels * (2 if self.sample_format == "S16_LE" else 4)


@dataclass(frozen=True)
class SampleAnchor:
    frame: int
    local_ns: int
    clock_domain: str = "python.monotonic_ns"

    def __post_init__(self) -> None:
        integer(self.frame, "anchor frame")
        integer(self.local_ns, "anchor time")
        if self.clock_domain != "python.monotonic_ns":
            raise ValueError("anchor must use the explicitly supported local clock domain")

    def frame_at(self, local_ns: int, rate: int) -> int:
        integer(local_ns, "local time")
        integer(rate, "sample rate", 1)
        return self.frame + trunc_ratio((local_ns - self.local_ns) * rate, NS_PER_SECOND)


@dataclass(frozen=True)
class PlayoutObservation:
    generation: int
    accepted_frame: int
    pending_frames: int
    available_frames: int
    delay_frames: int
    sampled_before_ns: int
    sampled_after_ns: int
    sample_rate: int
    clock_domain: str = "python.monotonic_ns"

    @property
    def estimated_output_frame(self) -> int:
        # Retain a signed delay exactly. An underrun/error is not a zero delay.
        return self.accepted_frame - self.delay_frames

    @property
    def estimated_local_ns(self) -> int:
        return self.sampled_before_ns + (self.sampled_after_ns - self.sampled_before_ns) // 2

    @property
    def uncertainty_ns(self) -> int:
        """Host-call bracketing only, not a hardware-latency accuracy bound."""
        return self.sampled_after_ns - self.sampled_before_ns

    def output_anchor(self) -> SampleAnchor:
        return SampleAnchor(self.accepted_frame, self.estimated_local_ns + trunc_ratio(
            self.delay_frames * NS_PER_SECOND, self.sample_rate), clock_domain=self.clock_domain)

    def write_error_frames(self, anchor: SampleAnchor) -> int:
        """Target write cursor minus accepted cursor; observation, not a servo."""
        if anchor.clock_domain != self.clock_domain:
            raise ValueError("sample anchor and output observation use different clock domains")
        return (anchor.frame_at(self.estimated_local_ns, self.sample_rate)
                + self.delay_frames - self.accepted_frame)


class PcmSink(Protocol):
    """Owned exclusively by one PcmPlayout. Calls must not block indefinitely."""
    format: PcmFormat

    def write(self, data: bytes) -> int: ...  # accepted frames, zero = would block
    def avail_delay(self) -> tuple[int, int]: ...
    def drop_prepare(self) -> None: ...
    def close(self) -> None: ...


class PlayoutError(RuntimeError):
    pass


class StaleGeneration(PlayoutError):
    pass


class PcmPlayout:
    """One bounded pending block, partial writes and a flush-generation barrier.

    All sink access is serialized. No automatic underrun recovery: it invalidates
    continuity, so the owner must explicitly flush and supply a new frame origin.
    The sink is transferred to this object; it must not be used elsewhere.
    """
    def __init__(self, sink: PcmSink, *, start_frame: int = 0, max_block_frames: int = 4096):
        integer(start_frame, "start frame")
        integer(max_block_frames, "block limit", 1)
        if max_block_frames > 65_536:
            raise ValueError("block limit exceeds experimental bound")
        self._sink = sink
        self.format = sink.format
        self.max_block_frames = max_block_frames
        self._lock = threading.RLock()
        self._generation = 0
        self._cursor = start_frame
        self._pending = b""
        self._offset = 0
        self._failed = False
        self._closed = False

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def pending_frames(self) -> int:
        with self._lock:
            return (len(self._pending) - self._offset) // self.format.frame_bytes

    def _require_ready(self) -> None:
        if self._closed:
            raise PlayoutError("PCM playout is closed")
        if self._failed:
            raise PlayoutError("PCM timeline failed; explicit flush/re-anchor required")

    def submit(self, data: bytes, *, first_frame: int, generation: int) -> None:
        integer(first_frame, "first frame")
        integer(generation, "generation", 0)
        if (type(data) is not bytes or not data or len(data) % self.format.frame_bytes
                or len(data) > self.max_block_frames * self.format.frame_bytes):
            raise ValueError("a nonempty bounded whole-frame PCM block is required")
        with self._lock:
            self._require_ready()
            if generation != self._generation:
                raise StaleGeneration("PCM block belongs to an obsolete generation")
            if self._pending:
                raise PlayoutError("pending PCM block must be consumed first")
            if first_frame != self._cursor:
                raise PlayoutError("noncontiguous PCM; explicit flush/re-anchor required")
            self._pending = data
            self._offset = 0

    def step(self) -> int:
        """Make one nonblocking write attempt. Never count unaccepted frames."""
        with self._lock:
            self._require_ready()
            if not self._pending:
                return 0
            try:
                accepted = self._sink.write(self._pending[self._offset:])
                integer(accepted, "accepted frames", 0)
                if accepted > self.pending_frames:
                    raise PlayoutError("sink accepted more frames than offered")
            except Exception:
                self._failed = True
                raise
            self._cursor += accepted
            self._offset += accepted * self.format.frame_bytes
            if self._offset == len(self._pending):
                self._pending = b""
                self._offset = 0
            return accepted

    def observe(self) -> PlayoutObservation:
        with self._lock:
            self._require_ready()
            try:
                before = time.monotonic_ns()
                available, delay = self._sink.avail_delay()
                after = time.monotonic_ns()
                integer(available, "available frames", 0)
                integer(delay, "signed delay frames")
            except Exception:
                self._failed = True
                raise
            return PlayoutObservation(self._generation, self._cursor, self.pending_frames,
                                      available, delay, before, after, self.format.rate)

    def flush(self, *, start_frame: int) -> int:
        """Drop application/driver pending frames, then establish a new epoch.

        Even a failed driver reset invalidates old work. Never pretend it resumed.
        A flush cannot retract samples the sink already consumed.
        """
        integer(start_frame, "start frame")
        with self._lock:
            if self._closed:
                raise PlayoutError("PCM playout is closed")
            self._generation += 1
            self._pending = b""
            self._offset = 0
            self._failed = True
            self._sink.drop_prepare()
            self._cursor = start_frame
            self._failed = False
            return self._generation

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._generation += 1
                self._pending = b""
                self._offset = 0
                self._sink.close()

    def __enter__(self) -> PcmPlayout:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
