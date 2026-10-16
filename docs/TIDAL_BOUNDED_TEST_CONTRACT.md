# Experimental bounded TIDAL hardware test, contract version 1

This optional private test surface uses the existing renderer listener and its
Host/Origin/CSRF admission. It adds no login, LAN identity or listener. No general
TIDAL capability is advertised. The helper must not fall back to `/tidal/play`.

## Requests

- `GET /orr/v1/tidal/test/status`: no body. Check `capability_version == 1`.
- `POST /orr/v1/tidal/test/start`: exact JSON object
  `{"track_id":"42","duration_seconds":60,"volume_percent":1,"confirm_experimental_playback":true}`.
  Track ID is a positive decimal string of at most 19 digits. Duration is an
  integer from 1 through 120 seconds; the host helper uses exactly 60. Volume
  must be integer 1, confirmation literal true. No extra or duplicate keys.
- `POST /orr/v1/tidal/test/stop`: exact JSON object
  `{"lease_id":"<opaque value returned by start>","stop":true}`.
  A mismatched lease never stops any source. Repeating stop for the current,
  already terminal lease is harmless and returns its actual terminal result.

POSTs require the existing same-origin `Origin` and `X-ORR-CSRF` token obtained
from the renderer's existing web bootstrap. Existing method/body/Host/Origin/CSRF
failures keep the existing fixed 400/403/404/405 responses.

## Test responses

All three successful routes return ONLY these fields:

- `capability_version`: integer 1
- `available`: boolean; a new test can be attempted only when true. It is false
  while a lease is active, after unconfirmed stop, or when backend/idle safety
  checks fail. This is a fresh preflight hint, never a reservation.
- `state`: `idle`, `starting`, `playing`, `stopping`, `stopped`, `expired`,
  `superseded`, or `failed`
- `lease_id`: opaque string, or null before the first admitted test
- `remaining_seconds`: nonnegative integer rounded up; zero when terminal
- `volume_percent`: 1 only after fresh raw mpv gain confirmation, otherwise null
- `volume_confirmed`: boolean; unknown or unsafe gain immediately clears it
- `playback_position_seconds`: finite nonnegative actual mpv playback position,
  or null when not observed for this owned lease
- `observed_progress_seconds`: nonnegative increase between actual owned position
  observations, initially 0; not inferred from wall time
- `decoded_audio_observed`: boolean, true only after actual selected audio and
  backend audio parameter observation for this owned lease
- `error`: null or one fixed code: `test_unavailable`, `test_busy`,
  `source_not_idle`, `volume_unconfirmed`, `playback_failed`,
  `volume_limit_exceeded`, `source_changed`, `stop_unconfirmed`,
  `lease_mismatch`, `test_closed`

Start returns HTTP 202 after an admitted lease has confirmed raw mpv volume 1;
playback/provider resolution continues in a renderer-owned task. Poll status
until `playing` or a terminal state. An HTTP/client disconnect does not cancel
the renderer's watchdog. A rejected preflight returns HTTP 409 and makes no
gain/playback changes. A gain mutation whose confirmation fails returns HTTP
503 with `failed` and `volume_unconfirmed`; it does not restore old gain.
Stop returns HTTP 200 for confirmed terminal results, 409 for a mismatched
lease, or 503 for `failed` with `stop_unconfirmed`. A deadline/stop is never
reported as successful silence unless the actual owned backend is confirmed
idle. `superseded` means the lease lost ownership; it does NOT claim silence.
GET returns HTTP 200, including when a previous test failed.

Responses never contain provider/session tokens, stream or catalogue URLs,
account IDs, login challenges, track metadata, exceptions, or device identity.

## Safety and ownership

The renderer verifies its own stopped state, no foreign pending work or Spotify
owner, required gain/readback/ownership hooks, and actual mpv idle state before
changing gain. This admission is fenced against competing renderer commands.
A monotonic renderer-owned deadline starts before provider resolution. It owns
only its exact queue, selected item and command generation. Expiry fences late
resolution, lock acquisition, load and resume. New renderer or native source
ownership ends the lease without stopping that newer source.

The existing confirmed-gain path writes 1, followed by fresh raw mpv volume
readback before any test load/unpause. Managed increases above 1 are rejected
before writing while this test owns output. A separate bounded-interval
watchdog reads actual native volume, ending/stopping only the owned test on
unknown or greater-than-1 gain. External native changes can be observed only
at the watchdog interval, not prevented in advance. The maximum polling
interval is 250 ms; bounded backend operations and stop confirmation can add
latency. This is not a hardware safety interlock.

Timer cancellation is not a substitute for backend stop. Renderer shutdown
invalidates pending work and attempts a bounded owned stop; failed/unknown stop
remains `stop_unconfirmed`. A renderer process/OS failure cannot guarantee a
stop; a disconnected or crashed *host helper* does not affect the lease.

The test does not select/change output device, EQ, mute or old gain restoration.
The 1% setting remains after termination. Existing unrelated active sources
are refused rather than stopped or replaced. Only synthetic tests are included;
this contract and its tests are not evidence of audible hardware playback.
`playing` and HTTP success alone are not audio proof. Even nonzero progress
and decoded audio observations do not prove audibility, full-track completion,
or intended physical output. No UI/metadata defaults count as observations.
