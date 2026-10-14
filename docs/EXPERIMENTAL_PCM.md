# Experimental decoded-PCM boundary

ORR now includes a small, optional PCM submission/observation component and a
finite installed-package consumer:

```sh
open-room-renderer audio pcm-check
open-room-renderer audio pcm-check --capture new-output.pcm
```

This Linux command needs `libasound.so.2`. It generates its own short stereo PCM
signal and opens only ALSA **null** or **file-on-null**, using a per-handle local
configuration. It never opens the default/physical device, loads renderer
configuration, starts mpv, changes a service or contacts a renderer. Normal ORR
playback continues to use mpv; the experimental module is not a new runtime
source or a multiroom implementation.

## What the command verifies

The report shows accepted sample-frame cursors, pending frame count, signed ALSA
delay/availability, a flush generation and local timestamp observations. It
submits two generated segments, abandons an unwritten segment at flush and
rejects stale-generation input. With `--capture`, it also verifies the output
bytes against the generated segments. Exit 0 means this digital contract passed,
not that sound played or physical synchronization was established.

A capture contains raw interleaved **S16_LE, 48 kHz, two-channel PCM**. New files
are created with mode 0600; existing paths are never overwritten. A failed run
can leave a partial newly created capture. Exit 1 means output was not verified;
exit 2 means the requested capture already exists. Error messages omit file paths
and raw backend exceptions. No sample audio from another source is included.

## Reusable module contract

`fake_linkplay.experimental_pcm` exports `PcmFormat`, `PcmSink`, `PcmPlayout`,
`PlayoutObservation`, `SampleAnchor`, and the error types. The only concrete sink
currently supplied is `AlsaPcmSink`, restricted to null/file-on-null output.

- A frame is one sample per channel. Input is explicitly decoded S16_LE PCM;
  there is no compressed codec or packet-type inference.
- The caller submits one bounded contiguous block tagged with its generation.
  `step()` makes one nonblocking PCM write attempt and advances the cursor only
  for accepted frames. A zero return means backpressure. The caller owns pacing
  and retry deadlines; the component adds no hidden worker or unbounded queue.
- `observe()` pairs availability and signed delay using one ALSA call, bracketed
  by two `time.monotonic_ns()` reads. Estimated output frame is accepted cursor
  minus delay. Negative delay is preserved. The bracketing span describes host
  call timing, not physical-latency accuracy.
- Sample/time conversion uses integer arithmetic with signed truncation towards
  zero. Anchors must use the explicit `python.monotonic_ns` clock domain. Other
  clocks, including `CLOCK_MONOTONIC_RAW`, need a separately verified mapping.
- `flush(start_frame=...)` drops pending application/driver data, increments
  generation and establishes a new origin. All sink access is serialized so an
  in-flight write completes before the flush barrier. Already consumed samples
  cannot be retracted. Failed writes/resets require explicit flush/re-anchor;
  they never silently invent continuity. `close()` releases only owned resources.

Null consumes immediately, so delay 0 here is not a hardware clock measurement.
There is no jitter scheduler, drift servo, gain/DSP path or physical output route.
Nonblocking PCM also does not promise a hard deadline on filesystem I/O.

Any future renderer use must enter through one source/output owner, confirm that
mpv/Spotify released the output, apply gain/DSP safely and close this owner before
returning output. Merely constructing a second player beside mpv is unsupported.

## Verification and sources

Focused unit tests cover partial progress/backpressure, signed delay, stale
input, fault/reset, flush serialization and frame bounds. Native CI opts into two
real libasound null/file tests with `ORR_TEST_PCM_OUTPUT=1`, then executes the
installed command with Python isolated from the checkout. No account or hardware
is needed; there is no physical/ARMv7 acceptance claim.

The implementation uses original generic frame accounting and the public ALSA
[PCM interface](https://www.alsa-project.org/alsa-doc/alsa-lib/group___p_c_m.html),
[file/null plugins](https://www.alsa-project.org/alsa-doc/alsa-lib/pcm_plugins.html),
[configuration](https://www.alsa-project.org/alsa-doc/alsa-lib/group___config.html)
and [input](https://www.alsa-project.org/alsa-doc/alsa-lib/group___input.html) APIs.
