# ADR 001: prefer go-librespot; start with a passive, optional sidecar contract

Date: 2026-10-13 UTC
Status: Accepted strategy; productive Spotify playback remains gated
Supersedes: 2026-10-12 Soloist-first/defer-all-integration decision
Evidence: [weighted assessment, source pins and twelve-month burden](../SPOTIFY_FEASIBILITY.md)

## Subsequent implementation decision: exclusive direct ALSA

The post-PR-2 sprint implements the narrower direct-ALSA option in
[the opt-in receiver candidate](../SPOTIFY_RECEIVER.md). Its independent CLI
controls exact systemd user units, confirms complete cgroup stops and hardware
open/release before admitting another engine, and remains available when the
renderer Web service is stopped. A shared PCM/DSP engine is deferred: it would
add buffering, clock and lifecycle work without closing account/hardware gates.
Provider status remains separate and Spotify bypasses ORR EQ/balance. This
supersedes the earlier diagnostic-only implementation scope below, not the
provider/account/physical acceptance requirements. The earlier offline generator
retains its deliberately inert contract.

## Decision

Prefer **go-librespot** for the next optional technical experiment. Rank
**go-librespot 76/100**, **librespot 69/100**, **Soloist 61/100**, using the requested
weights: unattended operation 25, audio/DSP/arbitration 25, ARMv7 15,
controls/events 15, licensing/provider conditions 15, resources/install 5.
These are engineering judgments, not measured reliability or legal approval.

Deliver the smallest useful step: an explicit **read-only local diagnostic
adapter**, initially verified with synthetic contracts. It may inspect local
readiness/status and, if safely implemented, receive events. It must not install,
spawn, authenticate, activate, send player commands, retrieve tokens, merge
sidecar observations into mpv state, or claim successful Spotify playback.
A REST-only version is sufficient if safe WebSocket support expands the task.

Do not require a universal multi-provider architecture before this experiment.
An explicit, mutually exclusive **whole-service Spotify mode** is a valid next
audio step, subject to the gates below. Direct Spotify ALSA audio would be
separate from ORR's mpv EQ/balance/volume pipeline. Shared DSP and seamless
source switching require later, independent acceptance.

## Why this ranking

- **go-librespot v0.10.3:** ready-made REST/WS, direct ALSA and documented raw PCM
  fit the bounded sidecar best. Reusable credentials and token renewal support
  unattended restarts; no comparable build-age expiry is documented. Generic
  ARMv6 hard-float is plausible on XU4 but requires ABI/DAC testing. GPLv3 covers
  code, not provider service authorization. [Release](https://github.com/devgianlu/go-librespot/releases/tag/v0.10.3),
  [pinned API](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/api-spec.yml)
- **librespot v0.8.0:** MIT, explicit ARMv7 target and ALSA/PCM are attractive.
  The CLI lacks the same ready-made local bidirectional API, and important
  recovery/security fixes remain on dev. A wrapper and release/dev choice add
  maintenance. [Release](https://github.com/librespot-org/librespot/releases/tag/v0.8.0),
  [reviewed dev changelog](https://github.com/librespot-org/librespot/blob/4a005903e078d512456fb008aaa9ba6246881bd9/CHANGELOG.md)
- **Soloist:** Spotify explicitly invites headless/DIY use, and supplies ARMv7
  builds/local controls. But **the binary expires 90 days after build**, requiring
  recurring replacement/restart. This is **not a documented 90-day API-key
  renewal requirement**. Stable archive URLs make an updater plausible; an
  officially guaranteed maintenance-free update process is unproven. Output is
  PipeWire/PulseAudio and binary redistribution is restricted.
  [Build lifetime](https://developer.spotify.com/documentation/soloist/concepts/overview),
  [authentication](https://developer.spotify.com/documentation/soloist/concepts/authentication),
  [updates](https://developer.spotify.com/documentation/soloist/reference/downloads-and-updates)

No candidate is established as maintenance-free for twelve months. Normal
restarts should preserve private session state. Network outages, revoked
credentials, upstream protocol changes, cache corruption and incompatible
updates need bounded recovery plus actionable diagnostics. Soloist additionally
has an unavoidable build-replacement cycle; the other candidates have no such
documented schedule but no indefinite compatibility guarantee either.

## Small first implementation

The diagnostic adapter is opt-in and uses an explicit numeric-loopback endpoint.
Allow only `GET /`, `GET /status` and optional receive-only `/events`. Reject
redirects/proxies/arbitrary destinations; bound time, size and watch duration.
Never expose a general API proxy, pairing code or token endpoint. The upstream
server defaults to disabled, `localhost`, port `0`; `3678` is a README example.

Keep separate sidecar-reported status. Root `playback_ready` is API readiness,
not confirmed sound; status 204 means `NO_SESSION`. Account acceptance is a
separate gate and remains `ACCOUNT_PENDING` regardless of these local responses.
Events can be lost, so
reconcile snapshots after connection changes. Omit private identifiers/raw
metadata/errors from normal diagnostics. Tests must identify themselves as
**SYNTHETIC_CONTRACT_ONLY** and leave **ACCOUNT_PENDING** intact.

The first concrete implementation is now `spotify diagnose --port PORT`, with
optional bounded event observation; see [the operator contract](../SPOTIFY_DIAGNOSTIC.md).
Independent synthetic tests cover REST/WS, refusal/privacy and bounded cleanup.
This assessment does not certify a real receiver, account or hardware test;
exact-head CI and the concrete evidence are reported separately.

## Next audio boundary: exclusive whole-service modes

A single supervisor owns `renderer`, `spotify` and `off`. Switching stops and
reaps the old service's whole owned process group, verifies output release,
then starts the other. No independent autostart or stale transition may revive
the retired engine. Stopping ORR itself closes its legacy HTTP/SOAP, queue,
EOF and recovery admission paths without a new universal player abstraction.
Spotify mode can temporarily make renderer controls unavailable.

Require an owned mpv child and explicit audio route. Foreign mpv processes or a
shared audio server cannot be assumed stopped; never kill unrelated processes.
Use a dedicated direct ALSA endpoint for the first physical test. A pause,
inactive event or stop acknowledgement alone does not exclude remote Connect
reactivation. With systemd, conflicts need ordering and whole-cgroup termination;
conflicts alone do not prove stop-before-start. [Official ordering semantics](https://github.com/systemd/systemd/blob/main/man/systemd.unit.xml)

Keep sidecar private state/cache separate from ORR exports. A failed stop,
unknown output owner, interrupted handover or stale process identity fails
closed to off/unavailable. WS loss invalidates observations; it must not trigger
automatic mpv takeover. Unknown command outcomes are reconciled, not replayed.
Use bounded restart/cache/log/resource policies and measure them on XU4; the
assessment includes initial guardrails, not fabricated benchmarks.

### Offline preparation only

`spotify prepare-exclusive` now emits the bounded [manual native proposal](../SPOTIFY_EXCLUSIVE.md):
a credential-free pinned-client config, Spotify user unit, drop-in for the
existing canonical renderer, receipt and stop/release runbook. This is
**OFFLINE_ARTIFACT_ONLY**, not the single supervisor or physical interlock
described above. No client execution, service activation, pairing or output test
is performed. Effective-unit review and bidirectional physical release checks
remain manual; all four terms/account/hardware/arbitration gates remain pending.

## Hard gates and stopping rule

1. **TERMS_PENDING:** open-source licensing does not authorize Spotify access.
   Spotify's integration/overlap restrictions remain relevant. Exclusive mode
   reduces overlap but does not settle the exact product's terms. Soloist's
   express DIY invitation is real; neither blanket permission for unofficial
   clients nor blanket prohibition of all personal DIY use follows.
   [Developer Policy](https://developer.spotify.com/policy),
   [Developer Terms](https://developer.spotify.com/terms),
   [Soloist invitation](https://developer.spotify.com/blog/2026-08-13-introducing-spotify-soloist)
2. **ACCOUNT_PENDING / HARDWARE_PENDING:** real user-participating setup,
   discovery/transfer, session recovery, exact ARM32 ABI, DAC and resource evidence.
3. **ARBITRATION_PENDING:** physical and failure-path proof of exclusive output
   before audible deployment. Existing mpv shutdown ownership is necessary but
   not an implemented cross-service mode switch.
4. **DSP_PENDING:** only blocks claims of a shared PCM/DSP pipeline, not the
   passive inspector or a separately accepted isolated mode.

No account login without the user's participation; no borrowed credentials,
proprietary redistribution, protection bypass or expiry evasion. Where provider
scope cannot responsibly be resolved, keep productive integration blocked,
finish the synthetic diagnostic step and return to useful UI/install work.
Do not turn an unresolved gate into hours of speculative architecture or mocks.
