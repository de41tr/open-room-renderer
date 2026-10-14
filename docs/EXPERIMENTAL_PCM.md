# Experimental decoded-PCM boundary

ORR now includes a small, optional PCM submission/observation component and a
finite installed-package consumers:

```sh
open-room-renderer audio pcm-check
open-room-renderer audio pcm-check --capture new-output.pcm
```

The `pcm-check` command needs Linux `libasound.so.2`. It generates its own short stereo PCM
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

## Decode a finite local FLAC file

With the distribution's `ffmpeg` installed on `PATH` (for example, the official
Debian/Ubuntu `ffmpeg` package):

```sh
open-room-renderer audio pcm-decode --flac short-local.flac
open-room-renderer audio pcm-decode --flac short-local.flac --capture new-decoded.pcm
```

This is an actual compressed-file consumer of the same PCM boundary. Input must
be a regular nonsymlink **standard FLAC** file, at most **64 MiB**, with a known nonzero
STREAMINFO frame count at most **2,880,000** (60 seconds at 48 kHz). Its declared
sample rate (up to 192 kHz) and channel count (1–8) are preserved; output is raw
interleaved **S16_LE**. Higher-bit-depth input loses precision in that conversion.
The JSON report declares source bit depth and output rate, channels, byte count
and PCM hash.
Keep the input file unchanged while the command runs.

FFmpeg receives an already-open local descriptor, a pinned FLAC demuxer and a
pipe-only protocol allowlist; filenames, URLs and arbitrary options are never
passed as decoder inputs. Video and metadata are not exported. There is one
owned child, no background decoder service. Decoded blocks are at most 4096
frames, output cannot exceed the declared bounded count, and the decode loop
has a 15-second total budget plus up to one second for owned-child cleanup.
As with `pcm-check`, filesystem/native calls are not hard-real-time guarantees.

Exit 0 requires successful FFmpeg completion, exactly the declared frame count,
and, with `--capture`, matching decoded/captured bytes. Decoder errors, an
incomplete frame, count mismatch or timeout fail with exit 1; Ctrl-C returns
130 after child cleanup. Exit 2 means the capture already exists. Captures are
new-only, mode 0600. Any failed run may leave a partial capture, explicitly
reported as unverified. Errors omit filenames and raw decoder diagnostics;
inherited `FFREPORT` is removed to prevent unsolicited decoder reports.

The command still opens only ALSA null/file-on-null. It does not play audible
audio, control mpv, change a service, or establish physical output ownership.
Standard FLAC success does not establish WiiM packet framing, transport,
sample-clock behavior or native multiroom interoperability.

## Reusable module contract

`fake_linkplay.experimental_pcm` exports `PcmFormat`, `PcmSink`, `PcmPlayout`,
`PlayoutObservation`, `SampleAnchor`, and the error types. The only concrete sink
currently supplied is `AlsaPcmSink`, restricted to null/file-on-null output.

- A frame is one sample per channel. Input is explicitly decoded S16_LE PCM;
  there is no compressed codec or packet-type inference in this boundary.
  The separate local-file consumer explicitly decodes standard FLAC first.
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
input, fault/reset, flush serialization and frame bounds. Native CI opts into
real libasound null/file checks with `ORR_TEST_PCM_OUTPUT=1`, then executes the
installed command with Python isolated from the checkout. No account or hardware
is needed; there is no physical/ARMv7 acceptance claim.

The same native gate encodes original generated PCM to real standard FLAC with
FFmpeg and decodes it through the CLI and ALSA file-on-null, comparing every
captured byte. It also checks corrupt/truncated input and a declared-frame
shortfall. Focused process tests cover split-frame pipe reads, excess output,
nonzero exit, deadline and cancellation cleanup. No workflow change is needed.

The implementation uses original generic frame accounting and the public ALSA
[PCM interface](https://www.alsa-project.org/alsa-doc/alsa-lib/group___p_c_m.html),
[file/null plugins](https://www.alsa-project.org/alsa-doc/alsa-lib/pcm_plugins.html),
[configuration](https://www.alsa-project.org/alsa-doc/alsa-lib/group___config.html)
and [input](https://www.alsa-project.org/alsa-doc/alsa-lib/group___input.html) APIs.
Local FLAC admission follows public
[STREAMINFO](https://www.rfc-editor.org/rfc/rfc9639.html#section-8.2), with decoding
via the documented [FFmpeg CLI](https://ffmpeg.org/ffmpeg.html) and
[pipe/protocol allowlist](https://ffmpeg.org/ffmpeg-protocols.html).
