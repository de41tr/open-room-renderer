# Inspect output from the Web controller

Choose **Inspect output** in the inline card below the playback controls. This
is an explicit, one-time local read. It does not open a modal dialog, block Stop,
change an output device, play a test sound or start an absent player. Existing
state polling does not run inspections, refresh their results or retry failures.

The result compares output intent from the **running renderer's startup
configuration** with the attached mpv player's live `audio-device` option and
reads its `current-ao` driver. It does not reread YAML or saved renderer state.
This differs from the [CLI audio diagnostic](AUDIO_DIAGNOSTIC.md), which reads an
explicit on-disk configuration file that may have changed since startup.

## Reading the result

- **Running configuration:** `mpv default` means `mpv_audio_device` was unset;
  mpv's own configuration may select an output. `explicit output` means a value
  was configured. Only a recognized driver family is displayed. The exact
  identifier is always hidden, including ordinary-looking values and paths.
- **Startup policy:** reports the captured `auto_spawn_mpv` setting. The
  diagnostic always uses a fresh client with auto-spawn disabled, regardless of
  that setting. It does not attach through the renderer's active backend.
- **Live output intent:** is the player's option value, not proof that a named
  device opened. `auto` is displayed as `mpv default`. Its identifier is hidden.
- **Live output driver:** displays a fixed recognized family, such as `alsa`,
  `pulse`, `pipewire` or `null`. Other values are reported as unrecognized.
  An unavailable property can mean the player is idle; it does not by itself
  prove a playback failure. Unsupported, unread, malformed and failed reads are
  reported distinctly rather than silently becoming unavailable.
- **Matches / differs:** compares the complete configured and observed intent
  strings internally, without sending them to the browser. Comparison requires
  an explicit setting, a valid observed intent, a connected probe and an
  unchanged socket identity at the checks. Default selection, socket replacement
  and connection failures remain **not compared**. A match is not proof of the
  physical output; a difference is a reason to review configuration and player
  ownership before changing anything.
- **Null:** an observed `current-ao` of `null` on an otherwise stable connection
  is deliberately silent. It sends no audio to a physical device. Failing to
  establish null does not prove sound is audible.
- **Missing or invalid socket:** the player may be absent, or the configured
  path may not be a socket. The inspection never starts a player, removes the
  path, repairs it or retries. Connection failure also covers permissions and
  stale refused sockets. A changed path is reported as incomplete.
- **Timeout:** one shared two-second IPC budget covers socket checks, connection,
  both sequential responses and bounded client-transport cleanup. The browser
  independently stops waiting after four seconds. Filesystem calls cannot be
  preempted if the filesystem itself stalls; this is not a hard real-time wall
  clock guarantee.

These are sequential observations, **not an atomic snapshot**. Unchanged socket
identity at the check instants is advisory, not proof of renderer ownership, and
cannot rule out every replacement race. Player ownership, the physical device
and audible sound remain unverified. The page does not query playback state to
infer any of them. Results are a point-in-time observation and can become old.

## Dismissal, failures and parallel controls

Diagnostic pending/result/error text stays inside this card. It never replaces
playback-command acknowledgements, unknown outcomes, gain or DSP state. Stop
keeps its normal availability throughout inspection. Other browser dialogs still
have their existing modal behavior.

**Dismiss inspection**, navigation and an observed renderer-session change clear
the card and discard late responses. Dismissal does not retract the server-side
read; its bounded cleanup still runs. Reopening is an explicit new request. If
another client or listener still has a probe running, the server immediately
rejects the new probe as busy. It does not queue, retry or hold a playback lock.
Refresh is appropriate after a session/admission failure; it does not repeat the
inspection automatically.

## Boundary and privacy

`POST /orr/v1/web/output/inspect` accepts exactly:

```json
{"action":"inspect","expected_session":"<current Web session>"}
```

It reuses the existing Web adapter's exact local Host/port, same-origin Origin,
CSRF token, request Content-Type/encoding/length/size and fetch-metadata checks.
There is no new listener, CORS exception or command action. Responses, including
errors, use the same `Cache-Control: no-store` and security headers. This remains
a trusted-LAN boundary, not authentication.

The adapter receives one narrow asynchronous callback, constructed from the
running configuration's socket path, configured output and spawn-policy scalar
values. Each admitted, non-busy request creates a separate `MpvBackend` with
`auto_spawn=False`, omits `audio_device`, and calls only
`diagnose_audio_output()`. The existing diagnostic sends exactly the two fixed
`get_property` requests, performs its bounded response validation and closes only
that client transport. The adapter's one synchronous busy claim is shared by all
listeners using the adapter and is released in `finally` after cleanup completes.

Responses contain only a session identity, schema version and fixed allowlisted
classifications. Raw socket paths, output identifiers, URLs, exception text,
backend error messages, config paths and unknown fields are never returned.
Printable text validation alone would not make device identifiers safe to expose,
so this first version deliberately hides them all.

## Verification limits

Focused in-memory server tests cover strict admission, privacy sentinels,
session fencing, overlapping requests, exception/cancellation cleanup, shared
adapter wiring and fresh scalar-configured clients. Synthetic transport tests
exercise the existing real diagnostic method and verify only two fixed reads
with no playback lifecycle calls. Dependency-free Node/DOM-double tests cover
explicit clicks, duplicate suppression, independent notices, Stop availability,
private failures, session/dismissal/navigation fencing and no automatic retry.

These checks do not establish real-browser layout, screen-reader behavior,
physical DAC routing or audible output. The earlier Chrome acceptance campaign
predates this card and is not acceptance of this new feature. No additional
browser campaign, native AF_UNIX run, hardware test or real account access is
part of this change.
