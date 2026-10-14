# Read-only audio output diagnosis

Run this on the renderer host, as the service user and from its working
directory, using the same configuration file:

```bash
open-room-renderer audio diagnose --config /path/to/renderer.yaml
```

This answers a small but useful question: **what output does the file request,
what does the attached player's output option currently say, and which output
driver is active?** It does not play a test sound or start an absent player.

## Reading the JSON report

- `configuration.mpv_audio_device` is the **on-disk setting**. The file may have
  changed since service startup, or belong to a different service. JSON `null`
  means the setting is unset; the string `"null"` deliberately requests silent
  output. The string `"auto"` requests mpv's automatic selection, not a pinned DAC.
- `live_mpv.audio_device` is the attached player's **live option intent**. A
  successful value such as `alsa/dmix:default` is an identifier, not confirmation
  that this physical device opened. Unset ORR configuration can coexist with any
  live option value because mpv's own configuration is still allowed in that mode.
- `live_mpv.current_ao` is the **observed active audio output driver**. For example,
  `alsa` identifies the driver, not a specific DAC. `null` is deliberately silent.
  `unavailable` can occur while idle or without an active audio output; it does
  not establish a playback failure. This probe does not read playback state to
  distinguish those conditions.
- Each property has `status: value` plus a validated `value`, or a fixed status:
  `unavailable`, `unsupported`, `request_failed`, `malformed_response`,
  `response_limit`, `transport_error`, `timed_out`, or `not_read`. A missing or
  invalid value is never silently converted into `unavailable` or a string.
- `live_mpv.connection` distinguishes `connected`, `socket_missing`,
  `socket_invalid`, `transport_error`, `timed_out`, and `not_attempted`.
  A stale refused socket or a permissions error is `transport_error`; the probe
  neither removes the socket nor retries. `socket_invalid` includes a regular
  file or symlink in place of the socket.
- `interpretation.configuration_vs_live_intent` compares only the explicit file
  setting and an observed option value. `differs` can help explain a setup problem;
  it does not prove the running renderer ignored its configuration. `matches`
  also does not prove ownership or output. Unset/missing observations are
  `not_compared`.

`status: complete` and exit **0** mean both fixed property reads completed with
a value, unavailable, or unsupported result. They do **not** mean audio works.
Exit **1** means the diagnostic was incomplete, including socket replacement,
connection/protocol failure, or timeout. Earlier successful observations remain
visible when a later read fails. Exit **2** means invalid CLI/configuration input;
the error omits the supplied path and YAML values.

## Observation and safety boundaries

The command uses the shared pure startup configuration reader. It does not apply
saved identity, gain, DSP, device-name or queue state, validate every runtime
setting, or establish that the running service used this file. Relative socket
paths retain their usual working-directory meaning.

After configuration parsing, one absolute **two-second IPC deadline** covers
socket inspection, connect, both responses and client transport cleanup. There
is one connection, no startup probe and no retry. Synchronous OS filesystem
inspection cannot itself be preempted if the filesystem blocks; immediately
after it returns, an expired budget stops the probe without a later request or
a successful report. The deadline is also checked around buffered IPC work.
It sends only these two JSON
IPC commands, in order:

```text
get_property audio-device
get_property current-ao
```

There are no arbitrary property names or device-list queries. Up to 16 response
or event lines are examined, each at most 8 KiB, with at most 32 KiB accepted
across them. Larger/malformed responses stop the probe. Device identifiers are
bounded to 1024 UTF-8 bytes and driver names to 64 ASCII characters; only these
validated values and fixed report fields are printed. Config/socket paths, raw
responses and exception text are omitted. Device identifiers themselves are
shown locally and may identify a device; review before sharing the report.

The two reads are **sequential, not atomic**, even on one connection. No
property-change observer is installed. The connected peer cannot be replaced
within that stream, but its state can change between reads. Socket-path identity
is checked around the reads: `changed` aborts remaining reads and makes the
report incomplete. `unchanged_at_checks` is only an advisory observation; it
cannot exclude replacement-and-restoration between checks or prove the pathname
still points to that peer later. The tool never reconnects to combine values
from a replacement peer. A separately attached CLI cannot certify that this is
the renderer's owned mpv process, so ownership always remains `unverified`.

No renderer HTTP request is made. In particular this does not call `getStatusEx`,
whose synchronization can have playback side effects. No player is launched,
authenticated, controlled, reconfigured or signalled; no configuration or renderer
state is changed or deleted. Cleanup closes only this IPC client connection and never invokes player
shutdown. No server route, port, background watcher or device discovery is added.

Neither property proves audible output or routing to a physical DAC. Audio
servers, mixers and hardware can still mute or reroute output. Inspect this
read-only report first; perform an intentional local-file listening test
separately when it is appropriate to produce sound.

## Primary references

The mpv manual defines [`audio-device`](https://mpv.io/manual/master/#command-interface-audio-device)
as the output option and explicitly distinguishes it from the actual device;
[`current-ao`](https://mpv.io/manual/master/#command-interface-current-ao)
reports the active driver. The [`null` output](https://mpv.io/manual/master/#audio-output-drivers)
produces no audio. See also [JSON IPC](https://mpv.io/manual/master/#json-ipc).
These are property observations, not a hardware measurement or a Spotify
integration check.

## Separate experimental PCM check

For a finite generated signal through ALSA null/file output, see
[experimental PCM](EXPERIMENTAL_PCM.md) and `open-room-renderer audio pcm-check`.
It has its own digital-byte/flush contract and does not inspect or change the
running renderer. The `audio diagnose` command above remains read-only.
