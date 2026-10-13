# Spotify feasibility: unattended operation and a bounded sidecar

Reviewed: 2026-10-13 UTC. Supersedes the Soloist-first assessment of 2026-10-12.
Decision: [ADR 001](adr/001-spotify-sidecar.md).
Scope: official provider documentation and upstream project source/release review.
No client installation, binary download, account login, credentials, provider API
calls, music playback or physical-device test was performed for this assessment.

## Recommendation

**Prefer go-librespot for the optional technical experiment**, with librespot as
the second choice. Soloist is no longer the preferred candidate for this
project's one-time-setup, long-unattended-operation requirement. No candidate
comes with evidence of twelve maintenance-free months.

The smallest responsible implementation is an **opt-in, read-only, loopback
status/event inspector**, tested against synthetic local contracts. It neither
starts nor authenticates a sidecar and cannot dispatch playback. It keeps its
observations separate from mpv's confirmed state. Actual provider use remains
**TERMS_PENDING / ACCOUNT_PENDING / HARDWARE_PENDING**.

An explicit, mutually exclusive **whole-service Spotify mode** is a valid next
audio experiment once its gates are met. A universal provider abstraction,
seamless switching and shared DSP are **not prerequisites**. Direct ALSA Spotify
output would have separate volume and no ORR mpv EQ/balance processing.

## Evidence and scoring method

- **FACT:** directly documented or present in the reviewed source.
- **INFERENCE:** engineering judgment, including the scores and proposed limits.
- **UNKNOWN:** requires account, hardware, endurance or provider clarification.

Scores are weighted points earned, not percentages of measured reliability.
Maximum points follow the user's 100-point matrix. Unverified target behavior
loses points; no candidate receives full unattended or resource credit.
Provider authorization and exclusive audio ownership are separate **hard gates**;
a high total cannot compensate for an unresolved gate.

| Criterion | Maximum | go-librespot | librespot | Soloist |
| --- | ---: | ---: | ---: | ---: |
| Long-term unattended operation | 25 | 20 | 18 | 9 |
| Audio integration, DSP and arbitration | 25 | 22 | 20 | 11 |
| ARMv7 / Odroid XU4 compatibility | 15 | 11 | 13 | 13 |
| Controls and events | 15 | 14 | 7 | 13 |
| Code license and provider conditions | 15 | 6 | 8 | 12 |
| Resource needs and installation | 5 | 3 | 3 | 3 |
| **Total** | **100** | **76** | **69** | **61** |

### Why these scores

1. **Unattended:** go-librespot and librespot have reusable credentials, token
   renewal and reconnect machinery, with no comparable documented build-age
   expiry found. This is not a promise that their sessions or protocol support
   last forever. go-librespot's current release includes relevant recovery
   fixes; librespot has important fixes still on its development branch. Soloist
   restores sessions, but its mandatory build replacement imposes recurring
   operational work. [G1], [G4], [G5], [L1], [L4], [L5], [S1], [S2]
2. **Audio:** go-librespot supplies both direct ALSA and explicit little-endian
   PCM/FIFO contracts, plus local controls. librespot also offers ALSA/pipe but
   needs more control glue and release-specific gain validation. Soloist's
   reviewed output is PipeWire/PulseAudio, with no documented direct ALSA/raw
   PCM contract. All still need output exclusivity; none automatically uses
   ORR's mpv DSP. [G6], [G7], [G8], [L6], [L7], [S3]
3. **ARMv7:** librespot explicitly builds armv7 hard-float; Soloist lists an
   official ARMv7 archive. go-librespot's generic ARMv6 hard-float build is a
   plausible XU4 candidate, not proof of the exact target ABI. No XU4/DAC
   acceptance was run for any candidate. [G2], [L2], [S4]
4. **Control:** go-librespot has REST and WebSocket contracts; Soloist has
   documented local WebSocket commands/events. The librespot CLI has event
   hooks while bidirectional control lives in its Rust library, so a local
   API wrapper adds maintenance. [G3], [G9], [L3], [S5]
5. **License/provider:** MIT makes librespot code integration less restrictive
   than go-librespot's GPLv3. Both lack established provider approval for this
   intended use. Soloist has Spotify's explicit personal DIY invitation, but
   proprietary redistribution and broader integration conditions remain
   relevant. This score does not certify legal compliance. [G10], [L8], [S6],
   [P1], [P2], [P3]
6. **Resources/install:** no CPU/RSS/underrun measurements justify ranking by
   footprint. All get equal partial credit. Native decode/ALSA dependencies,
   Rust build features or an audio server respectively add setup work. Prefer
   separately installed, pinned builds, not a new mandatory ORR dependency.
   [G12], [L2], [S3]

## Versioned findings

### go-librespot: preferred technical candidate

FACT: Latest release rechecked is **v0.10.3**, 2026-10-03, commit
`4dbc099f46da3529adaa85feb97c4f49deb0b130`. Current master was
`3508ee5c2b1723ab8c9c0e0a96c34c8d3967e3b5`; contracts below are pinned to the
release, not silently taken from master. Premium is required. Source builds
require Go 1.25+, CGO and native decoding/ALSA libraries. [G1], [G12]

FACT: State includes reusable credentials. Zeroconf credential persistence is
**off by default** and must be deliberately enabled for restart restoration in
that mode. Interactive/device-auth modes reuse stored credentials on subsequent
starts. Login5 renews expiring access tokens using stored credentials and retries
transient failures; AP reconnection also exists. A lost session with Zeroconf
disabled can terminate the daemon rather than remain falsely healthy. None of
this establishes an indefinite session lifetime. [G4], [G5], [G15]

FACT: The CLI keeps state separately in its config directory and uses a lockfile
to prevent duplicate instances sharing that directory. Linux state updates use
a temporary file followed by replacement; this is not an fsync/power-loss
durability proof. The default audio cache is disabled; when enabled its default
limit is 1 GB. Metadata prefetch is opt-in. [G11], [G12], [G13]

FACT: The API includes `GET /`, `GET /status`, transport/queue/volume operations
and WebSocket `/events`. `GET /status` reports HTTP 204 when no session exists.
The per-client event buffer is 64 entries; slow consumers can lose old events.
The server supports optional TLS/CORS settings, but the reviewed server has no
client-credential authentication layer. It must not become a browser-facing
proxy to arbitrary sidecar operations. Actual defaults are disabled, host
`localhost`, port `0` (OS-assigned); README port `3678` is an example. Root and
status have no version field, so an inspector must name its expected contract,
not pretend to discover the binary version. [G3], [G9], [G13], [G17]

FACT: Pipe output is 44.1 kHz, stereo, `s16le`, `s32le` or `f32le`. Opening fails
without a reader by default; optional wait-for-reader changes startup blocking.
ALSA and pipe honor external-volume handling, while PulseAudio controls its own
stream volume. Neither setting establishes a universal unity-gain contract.
These are useful future PCM capabilities, not proof of buffering/seek/DSP
correctness in ORR. [G6], [G7], [G8], [G14], [G16]

UNKNOWN: XU4 runtime ABI, real authentication eligibility, DAC operation,
long-outage recovery, safe playback resumption, CPU/RSS and physical exclusivity.

### librespot: viable second choice, more wrapper maintenance

FACT: Latest release rechecked is **v0.8.0**, 2025-11-10, commit
`d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d`. Current dev was
`4a005903e078d512456fb008aaa9ba6246881bd9`. ARMv7 hard-float is an explicit build
target. Code is MIT; upstream requires Premium and warns that Spotify use may
be forbidden. That warning is not a definitive jurisdiction-specific ruling.
[L1], [L2], [L8], [L10]

FACT: Release code caches reusable credentials, renews expired HTTP tokens and
recreates invalidated sessions. The CLI can exit after initialization failure
or repeated reconnects, so a supervisor is needed. Cached credentials are
written directly; malformed data can be treated as absent. The inspected dev
changelog includes credential-permission, dealer/CDN recovery and fixed-volume
fixes not in v0.8.0. Release `fixed` volume must not be assumed equivalent to
dev or proven unity gain. [L4], [L5], [L7], [L11]

FACT: An upstream August 2026 issue reports failure of externally supplied
third-party tokens at private playback endpoints. The maintainer distinguishes
this from Web API access. This is evidence of protocol-maintenance risk, **not**
evidence that every v0.8.0 login method is broken. Do not solve it by borrowing
identities, keys or tokens. [L9]

FACT: ALSA/pipe/subprocess output and Spirc library controls exist. PCM is
44.1 kHz stereo; format/endian handling must be pinned to the chosen target.
The CLI's event hooks are useful but are not a ready-made REST/WS control API.
Serial event handlers must not hang; optional sink hooks intentionally block
playback. [L3], [L6], [L12]

UNKNOWN: target endurance and the safe, maintainable wrapper version to ship.
Using an unreleased dev build trades useful fixes for additional validation.

### Soloist: official DIY option, recurring build maintenance

FACT: **The 90-day expiry applies to the binary's build date, not a documented
90-day API-key renewal schedule.** The key is required at each start; docs
describe replacing/revoking it and restoring the separate stored Connect
session. No fixed recurring key-renewal interval is documented in the reviewed
authentication page. Retaining the same private data directory allows normal
restart restoration. This is not a guarantee that keys/sessions cannot ever be
revoked. [S1], [S2]

FACT: Stable architecture-specific download names, ARMv7 availability,
`--version` build metadata, and replace-then-restart updates are documented.
Expired builds exit with code 10. INFERENCE: updates can plausibly be automated,
but no official guarantee of a maintenance-free updater, uninterrupted archive
availability or safe automatic rollback was established. A 365-day run starting
with a freshly built binary needs at least four replacements even with ideal
release timing; older downloads and safety margins add earlier update windows.
This burden is binary deployment, not repeated key generation. [S1], [S4]

FACT: Spotify expressly supports personal headless/DIY playback and local
controls. Premium is required for initial setup; the announcement allows Free
and Premium users to connect afterward. Binary redistribution is disallowed.
The reviewed CLI uses PipeWire/PulseAudio, defaults to unbounded cache unless
configured, and may continue without its WS API if binding fails. API command
acknowledgement is not confirmation of completed playback. [S3], [S5], [S6]

UNKNOWN: exact ARM32 runtime compatibility, unattended update/restart endurance,
resource use and applicability of broader terms to the proposed ORR integration.

## Expected twelve-month operating burden

These are **risk scenarios and required mitigations**, not measured failure
rates or promises of zero manual work.

| Area | go-librespot | librespot | Soloist |
| --- | --- | --- | --- |
| Scheduled updates | No comparable build-expiry timer documented. Monitor pinned upstream/security/protocol changes; test before upgrading. | Same absence of documented expiry; release/dev divergence may require choosing and validating a newer build. | Mandatory replacement before 90 days from build; stable URL alone is insufficient. Validate architecture/build age and preserve a usable rollback. |
| Authentication | Persist reusable credentials deliberately. Automatic token renewal does not prevent revocation or re-pair needs. | Cache reusable credentials; same token-vs-session distinction. Audit permissions and corruption recovery. | Keep private key and session directory. Normal restarts restore pairing; key revocation or invalid session can require user action. |
| Session/network failure | AP/Login5 recovery exists; session loss or startup failures can need bounded process restart. Never loop permanently on rejected credentials. | Reconnect can exhaust and exit. Supervisor and first reconnect after provisioning need validation. | Session restoration is documented; outage/recovery guarantees are not. Missing WS readiness must not look healthy. |
| Restart/power loss | Preserve state; verify API readiness, exact process ownership and output exclusivity before admitting audio. | Same, with direct credential-write corruption risk. | Preserve data; reject expired builds, do not restart-loop exit 10. |
| Recovery/update safety | Pin version, compare contract, retain compatible state backup, bounded retry; stop for user re-auth when needed. | Pin release/dev commit and dependencies; wrapper compatibility is extra work. | Download/verify/replace/restart needs an update mechanism and failure alert. A previous build may already be expired. |

INFERENCE: reasonable maintenance is event-driven recovery plus periodic
release/security review, rather than forced manual quarterly provisioning.
The absence of an expiry timer improves go-librespot/librespot's fit but cannot
eliminate provider-driven breakage. No exact annual incident count is supported.

## Smallest useful integration boundary

### Stage 1: passive local contract inspector

This is responsible to implement and exercise **with synthetic data now**:

- Disabled by default; explicit operator invocation/configuration. Only fixed
  `GET /`, `GET /status`, and receive-only WebSocket `/events` on numeric loopback.
  Reject redirects, remote hosts, proxy settings, URL credentials and arbitrary
  paths. Require an explicit endpoint/port. Do not access `/token`, `/auth/code`, library/context enumeration or
  artwork URLs; those are unnecessary and some trigger provider requests.
- Separate output object for sidecar transport/session/reported status. Never
  write RendererState, mpv queue, generation, volume, gain/DSP or playback truth.
  No install, spawn, key read, login, pairing, activation or player commands.
- HTTP 200 proves a valid observed response only. Root `playback_ready` means
  the daemon is ready for play commands, not that a speaker is producing audio.
  HTTP 204 from status means `NO_SESSION`. This response does not establish
  whether real-account acceptance has ever been performed; that separate gate
  remains `ACCOUNT_PENDING` for this experiment. Timeouts, invalid schema and
  unavailable endpoints remain explicit failures, not an empty successful player.
- Accept `{type, data}` events, including null data and unknown future types.
  Treat events as hints to refresh the status snapshot; reconnect invalidates
  old observations. Events lack a proven sequence/replay contract. A fresh
  response still does not prove audible playback or output ownership. [G3], [G9]
- Proposed inspector bounds: one connection, 2-second connect/read deadlines,
  64 KiB HTTP/frame limit, bounded event count/watch duration, and throttled
  snapshot refresh. Redact account/device IDs, track metadata and raw errors
  from ordinary diagnostics; never persist credentials or raw payload logs.
- Verify against synthetic local HTTP/WS fixtures: 200, 204, malformed/oversized
  payload, timeout, unexpected fields, null event data, disconnect and stale
  observation invalidation. Report **SYNTHETIC_CONTRACT_ONLY**. Actual accounts,
  target playback and legal clearance remain pending even when every test passes.

A REST-only first version is acceptable if safe receive-only WebSocket support
would expand the task. WebSocket clients must reject redirects before following
them; ordinary HTTP redirect settings do not automatically cover WS upgrades.
Document the actually implemented subset and do not report unrun WS tests.

The inspector is a concrete diagnostic tool, not a Spotify receiver. Opting into
it does not authorize installing or operating an unofficial provider client.

### Stage 2: explicit exclusive whole-service mode

INFERENCE / proposed design, not an implemented or accepted hardware feature:

1. A single local supervisor selects `renderer`, `spotify` or `off`, and owns both
   service lifecycles. The sidecar is separately user-installed and version-pinned.
   A status read never starts it. No independent autostart path may defeat the
   selected mode, including crash recovery or stale queued switch requests.
2. Entering Spotify mode stops the **whole ORR service and its owned mpv process
   group**, waits for termination and output release, then starts the sidecar.
   Returning stops/reaps the sidecar first, then starts ORR. This closes every
   HTTP/SOAP/EOF/recovery admission path by stopping its owner rather than adding
   checks to every path. Renderer controls may be temporarily unavailable.
3. On systemd, use service conflicts **and ordering**, whole-control-group
   termination, a serialized mode transition and completion checks. `Conflicts=`
   alone does not order stop before start. Do not kill unrelated/external mpv;
   an installation using an unowned player is not eligible until output ownership
   is resolved. See [systemd unit ordering][O1] and [kill semantics][O2].
4. Prefer one explicit direct ALSA hardware endpoint for the first audio trial.
   Do not use a shared default/dmix/PulseAudio sink as evidence of exclusivity.
   Verify the previous process cannot reach the sink, there is no surviving
   audio server/FIFO buffer, and the actual DAC transition is silent. Pause,
   `inactive` events or a successful REST stop alone cannot prevent later remote
   Connect reactivation. The stopped sidecar must stay stopped in renderer mode.
5. Use independent sidecar private state (directory 0700, files 0600, dedicated
   service identity where practical) outside ORR exports/backups. Never copy
   credentials into ORR config or expose local auth/token endpoints through its
   API. Preserve state on ordinary restarts; explicit user-controlled unlinking
   is separate from stopping a service.
6. On uncertain stop, failed output release, supervisor restart during handover,
   or unexpected process identity: fail closed to `off/unavailable`. A lost
   control connection makes status stale; it must not cause automatic switching
   to mpv. Unknown command outcomes are reconciled, never blindly replayed.
7. Suggested initial guardrails, **not measured requirements**: cache disabled,
   metadata prefetch disabled, bounded journal retention; 256 MiB memory ceiling,
   64 tasks, and at most three crash restarts per five minutes with increasing
   delay. Measure headroom on the XU4 and adjust before deployment. Do not impose
   an unmeasured CPU throttle that causes underruns. Account/terms/expiry errors
   need operator attention rather than endless restarts. [O3]

No global provider framework is needed for this stage. However, the renderer's
existing ability to attach to an external mpv socket means simply stopping the
Python service cannot universally establish silence; owned-process-only mode
must be an explicit eligibility condition.

### PCM/shared DSP is a later, separate acceptance

Direct ALSA Spotify mode bypasses ORR's mpv DSP. A future pipe-to-mpv design needs
format agreement, reader/writer startup, bounded buffering, pause/seek flushing,
stale-audio rejection and one gain authority. A continuous PCM stream's position
does not identify a Spotify track. Do not expose shared EQ, seamless switching,
gapless behavior or unified queue state before measuring those properties.

## Provider and distribution gates

FACT: Spotify's Developer Policy restricts integration with other services'
streams/content and overlapping/mixed audio; Developer Terms include controlling
a background Spotify application in their streaming definition. User Guidelines
restrict circumvention and reverse engineering subject to applicable-law
exceptions. An open-source code license does not grant Spotify service access.
[P1], [P2], [P3]

INFERENCE: exclusive mode reduces the audio-overlap risk but does not by itself
settle the broader product/integration terms. Soloist's explicit DIY and local
control invitation is real and must not be replaced by a blanket prohibition.
Conversely, it is not proof that Spotify approves go-librespot or this exact
multi-service renderer. Before productive provider operation, establish the
applicable account/jurisdiction and intended personal/distribution scope, with
provider clarification or qualified review where unresolved. This is an
engineering release gate, not a legal determination.

No borrowed account/OEM identity, external token/key, proprietary component,
protection bypass, expiry circumvention or content export is acceptable. No
actual account setup is authorized without the user's participation. Keeping
the experiment synthetic avoids those steps without blocking useful diagnostics.

## Decisive blockers and next action

| Gate | What is still missing | Does it block the passive synthetic inspector? |
| --- | --- | --- |
| TERMS_PENDING | Responsible basis for this exact client/use/distribution scope | No; blocks productive provider integration |
| ACCOUNT_PENDING | User-participating setup, real discovery/transfer/session recovery | No |
| HARDWARE_PENDING | Exact XU4 OS/ABI, DAC, resource/underrun and restart evidence | No |
| ARBITRATION_PENDING | Supervised exclusive service transitions including failures | No; blocks audible two-engine deployment |
| DSP_PENDING | Measured PCM/gain/buffer/seek integration | No; only blocks shared-DSP claims |

The bounded read-only [diagnostic CLI](SPOTIFY_DIAGNOSTIC.md) is implemented
and independently reviewed against synthetic peers. Continue useful UI/install/
output-configuration work while productive provider gates remain open. Do not spend the research budget trying to
resolve legal/account/physical questions through more mock tests. A future
exclusive-mode trial is smaller than, and independent of, seamless multi-provider
architecture. With `mpv_audio_device` unset, the existing default-output/external-
socket behavior remains. Optional explicit owned-output selection is now
implemented, but shared-DAC arbitration and whole-service mode switching remain
unproven.

## Primary sources

Primary-source review was conducted on 2026-10-13, with supporting links retained
from the earlier assessment where applicable. Source pins distinguish releases
from dev; some final commit-pinned links correspond to tag/raw pages inspected.
The document check verified all 41 Markdown reference labels and score arithmetic,
not an exhaustive fetch of every exact external URL.

[G1]: https://github.com/devgianlu/go-librespot/releases/tag/v0.10.3
[G2]: https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/crosscompile.sh
[G3]: https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/api-spec.yml
[G4]: https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/daemon/app.go
[G5]: https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/login5/login5.go
[G6]: https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/output/driver-pipe.go
[G7]: https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/player/player.go
[G8]: https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/output/driver-pulseaudio.go
[G9]: https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/daemon/api_server.go
[G10]: https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/LICENSE
[G11]: https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/cmd/daemon/file_state_store.go
[G12]: https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/README.md
[G13]: https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/config_schema.json
[G14]: https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/output/driver-pipe-unix.go
[G15]: https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/ap/ap.go
[G16]: https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/output/driver-alsa.go
[G17]: https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/cmd/daemon/cli_config.go
[L1]: https://github.com/librespot-org/librespot/releases/tag/v0.8.0
[L2]: https://github.com/librespot-org/librespot/blob/d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d/.github/workflows/cross-compile.yml
[L3]: https://github.com/librespot-org/librespot/blob/d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d/connect/src/spirc.rs
[L4]: https://github.com/librespot-org/librespot/blob/d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d/core/src/login5.rs
[L5]: https://github.com/librespot-org/librespot/blob/4a005903e078d512456fb008aaa9ba6246881bd9/CHANGELOG.md
[L6]: https://github.com/librespot-org/librespot/wiki/Audio-Backends
[L7]: https://github.com/librespot-org/librespot/blob/d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d/src/main.rs
[L8]: https://github.com/librespot-org/librespot/blob/d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d/README.md
[L9]: https://github.com/librespot-org/librespot/issues/1737
[L10]: https://github.com/librespot-org/librespot/blob/d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d/LICENSE
[L11]: https://github.com/librespot-org/librespot/blob/d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d/core/src/cache.rs
[L12]: https://github.com/librespot-org/librespot/wiki/Events
[S1]: https://developer.spotify.com/documentation/soloist/concepts/overview
[S2]: https://developer.spotify.com/documentation/soloist/concepts/authentication
[S3]: https://developer.spotify.com/documentation/soloist/reference/command-line
[S4]: https://developer.spotify.com/documentation/soloist/reference/downloads-and-updates
[S5]: https://developer.spotify.com/documentation/soloist/reference/websocket-api
[S6]: https://developer.spotify.com/blog/2026-08-13-introducing-spotify-soloist
[P1]: https://developer.spotify.com/policy
[P2]: https://developer.spotify.com/terms
[P3]: https://www.spotify.com/de/legal/user-guidelines/
[O1]: https://github.com/systemd/systemd/blob/main/man/systemd.unit.xml
[O2]: https://github.com/systemd/systemd/blob/main/man/systemd.kill.xml
[O3]: https://github.com/systemd/systemd/blob/main/man/systemd.resource-control.xml
