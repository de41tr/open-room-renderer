# Optional go-librespot diagnostic

`open-room-renderer spotify diagnose` performs a bounded, read-only observation
of an **already running** go-librespot sidecar, including an explicitly enabled
[Spotify EQ source](SPOTIFY_EQ.md). It does
not require the ORR service or mpv to run. Nothing calls it automatically.

This is the first small integration experiment, not a Spotify playback mode.
It does not install/download/start/stop/supervise a client, log in, pair an
account, write configuration or credentials, send playback commands, persist
provider state, or change the Web UI. Tests use synthetic local peers only.
No actual account, provider client, Connect session, XU4 audio or DAC was tested.

## Use

Supply the port you explicitly configured for the sidecar's local API. For
example, **only if you chose port 3678**:

```bash
open-room-renderer spotify diagnose --port 3678
open-room-renderer spotify diagnose --port 3678 --watch-seconds 5
```

The host defaults to `127.0.0.1`; `--host ::1` supports numeric IPv6 loopback.
The port is mandatory. Upstream v0.10.3 has its server **disabled by default**,
a `localhost` bind default and port `0` (OS-selected); 3678 is an upstream README
example, not a fixed default. An enabled API must be bound to numeric loopback
and have a known port. This diagnostic does not enable or discover it, and
rejects hostnames, URL arguments, non-loopback addresses and IPv6 scope IDs.

There is no authentication option. An API requiring authentication is reported
as unavailable; do not pass a token in the host or port arguments. Loopback
limits the destination but does not authenticate the peer. Another local process
could supply matching JSON. Do not expose the sidecar API to the LAN or internet.

## What the JSON means

One successful observation produces one JSON object on stdout:

- `observation: "sidecar_reported"`: every state value is an unverified sidecar
  report. It is not an independently observed audio result.
- `api_reachable: true`: the allowed HTTP reads completed. The
  `expected_contract` names ORR's pinned **expected projection**, not a detected
  sidecar version. Neither upstream endpoint returns a version identifier.
- `bootstrap_ready`: upstream `GET /` field `playback_ready`. This is bootstrap
  readiness, not account readiness or audible playback.
- `session_observation: "NO_SESSION"`: `/status` returned HTTP 204, meaning
  no active session. `status` is null. A true bootstrap flag does not change this.
- `session_observation: "SESSION_PRESENT"`: `/status` returned a compatible
  retained-field projection. This does not establish whose account it is, whether
  it is authorized, or whether anything is audible.
- `status`: only boolean stopped/paused/buffering/repeat/shuffle fields and
  unsigned volume/volume_steps values. `volume_fraction` is their reported ratio,
  or null if volume_steps is zero; it is not measured gain. Account/device IDs,
  device name, context, track/next-track data, artwork and raw errors are omitted.
- `snapshot_freshness: "snapshot_only"`: a point-in-time HTTP observation, not a
  claim of continuous freshness. It becomes `unknown` if the event channel
  disconnects; retained values are then the last observation only.
- `audio_output` and `shared_dsp` remain `unverified`.

Exit 0 means the bounded diagnostic returned an observation, including a 204 or
an observed event disconnect. Inspect the fields rather than using exit 0 as a
playback-readiness signal. Request/contract/deadline errors return 1, print a fixed
private-safe error to stderr, and produce no partial JSON. Parser errors return
2; an interactive interruption returns 130. No endpoint, private response body
or underlying exception is printed.

## Optional event observation

`--watch-seconds` accepts 0..30 seconds; zero skips WebSocket access. The observer
opens `/events` once, refreshes `/status` after connection, and treats each valid
known **or unknown** event as a hint to refresh `/status`. Events never change
the state projection. Null event data is accepted.
A final `/status` refresh also covers a quiet or lossy event stream. There is no
application reconnect loop. After a disconnect, freshness stays unknown and the
last snapshot is retained, without automatically reconnecting.

`events.channel` is `not_requested`, `observed`, `unconfirmed` or `disconnected`.
`observed` means a channel was established during this command; the command
closes it before exiting. `end_reason` distinguishes duration, message budget
and disconnect. Counts are bounded diagnostic observations, **not a complete
event log**. Upstream has no sequence/replay/initial-snapshot guarantee and its
64-event buffer can drop oldest entries. A successful refresh is still only a
point-in-time sidecar report. An oversized/error frame is treated as a disconnect;
a malformed text event or binary application message fails the observation.

`events.observations` retains at most 32 known event types in receive order.
Unknown type strings are counted but not copied. For `playback_error`, only
these additional fields survive:

- `stage`: `load`, `playback` or `session`.
- `kind`: `restricted`, `unsupported`, `rate_limited`, `rejected`, `server`,
  `timeout`, `network` or `unknown`.
- `unplayable`: the receiver's boolean classification, not ORR's conclusion.
- `explanation`: a fixed, short explanation of that reported category.

Absent, malformed or unrecognized fields become null. In particular, null
`kind` means no recognized category was received; literal `unknown` means the
receiver explicitly reported an unclassified error. Raw `message`, media/context
URIs, account/device identifiers and play-origin data are never retained.

For a skipping-track investigation, start this bounded watch before selecting a
track yourself in the Spotify app. A `load` error identifies the receiver's load
stage; `not_playing` is a separate observed event, not a diagnosis of why playback
ended. The command does not infer an account/licensing cause, blame EQ, change
playback, or retry the track. No observed errors does **not** mean successful
playback: the watch is partial and there is no event replay. Exit 0 still means
only that the observation completed, even if playback errors were observed.

## Safety and resource bounds

The only network operations are HTTP `GET /`, `GET /status` and, when requested,
one WebSocket `GET /events` handshake. No `/token`, `/auth/code`, `/context/*`,
`/library/*`, POST or playback mutation exists in this module.

- No DNS, ambient proxies/netrc credentials, HTTP authentication, cookies,
  decompression, redirects, credential storage, or endpoint persistence.
- HTTP reads use `allow_redirects=False`. WebSocket handshake redirects are
  rejected before following `Location` through aiohttp's public `TraceConfig`.
  Synthetic redirect tests use a separate target server and assert zero requests
  reach it, including the WebSocket path.
- Per HTTP request: 2-second connect and read-inactivity limits, 4-second total
  limit, 64 KiB body cap, identity encoding only. JSON rejects duplicate keys,
  non-finite values and container nesting beyond 32 levels, including in omitted
  metadata.
- At most 32 received event/control messages, each at most 64 KiB. At most 36
  application HTTP reads plus one WebSocket handshake, including initial and
  final snapshots. Duration exhaustion can stop earlier.
- The overall deadline is 12.75 seconds plus requested watch duration; shutdown
  includes a 0.25-second WebSocket-close budget. The watch timer includes its
  handshake and snapshot refreshes, so it is not a promised full listening window.
- There is no application retry/reconnect loop. Fresh HTTP connections avoid
  pool reuse, but recent aiohttp versions can recover an idempotent GET once
  after a disconnect. Such transport recovery remains inside request and overall
  deadlines. This must not be mistaken for exactly-once network requests.

A local process can still lie or become unavailable. These bounds prevent the
observer from becoming an unbounded monitor or leaking raw diagnostic data;
they do not establish a trusted provider control plane.

## Remaining gates

The project's real-account acceptance remains **ACCOUNT_PENDING** because that
acceptance was not performed; this is not inferred from a `/status` response.
Provider terms, account participation, output ownership/arbitration, lifecycle
supervision, secure sidecar persistence, real ARMv7/XU4 operation, and any shared
PCM/DSP route remain separate gates. A code license does not grant Spotify service
permission. Do not infer a productive playback integration, seamless source
switching or safe concurrent mpv/Spotify output from this diagnostic.
See [the Spotify decision](adr/001-spotify-sidecar.md) for the wider assessment.

## Contract and verification

Expected upstream: go-librespot v0.10.3, commit
`4dbc099f46da3529adaa85feb97c4f49deb0b130`:

- [API contract](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/api-spec.yml)
- [Server defaults](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/config_schema.json)
- [Playback-error fields](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/daemon/api_server.go#L383-L391)
- [Error categories](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/daemon/playback_error.go)

The projection checks the primitive fields it retains; it deliberately does not
validate or expose identity and metadata fields. It is not a full schema validator
or an implementation/version fingerprint. A compatible projection may be served
by another version or by a synthetic peer.

Focused synthetic tests (no provider installation, credentials or native audio):

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_spotify_diagnostic.py tests/test_cli_commands.py
```
