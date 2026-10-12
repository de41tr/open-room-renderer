# ADR 001: defer integrated Spotify; prefer a separate official client experiment

Date: 2026-10-12 UTC
Status: Accepted for this development phase
Evidence: [source-linked comparison](../SPOTIFY_FEASIBILITY.md)

## Decision

Keep Spotify inactive and unadvertised in ORR. Prefer separately user-installed
**Spotify Soloist** for a future bounded official-client experiment. Do not add
a fake provider adapter, account session, unified player state, or shared-DSP
claim in this phase. Spend the remaining effort on functional DSP/playlist UI.

This is **BLOCKED / DEFERRED integration**, not a judgment that standalone
Soloist is prohibited. Spotify explicitly supports personal headless/DIY use;
its setup requires a Premium user's own key, and subsequently permits Free and
Premium users to connect. [Official announcement](https://developer.spotify.com/blog/2026-08-13-introducing-spotify-soloist)

## Why this candidate

- **Soloist:** official ARMv7 build and local WebSocket controls/events are
  documented. Output is PipeWire/PulseAudio; no reviewed direct PCM/mpv contract.
  Users obtain their own binary/key; binary redistribution is disallowed and
  builds expire after 90 days. [Downloads](https://developer.spotify.com/documentation/soloist/reference/downloads-and-updates),
  [CLI](https://developer.spotify.com/documentation/soloist/reference/command-line),
  [distribution rules](https://developer.spotify.com/documentation/soloist)
- **go-librespot v0.10.3:** strongest ready-made technical REST/WS + raw-PCM
  sidecar; ALSA/PulseAudio/pipe output, GPLv3 source. ARMv6 hard-float is a plausible
  ARMv7 target, not tested XU4 evidence. Provider permission and volume-path
  details remain unresolved. [Pinned release](https://github.com/devgianlu/go-librespot/releases/tag/v0.10.3)
- **librespot v0.8.0:** MIT source and explicit ARMv7 build target, with pipe
  output; local bidirectional control needs an additional wrapper. Its stable
  volume behavior differs from current development source. No real target test.
  [Pinned release](https://github.com/librespot-org/librespot/releases/tag/v0.8.0)

See the comparison for pinned source paths, formats, licensing and limitations.
No CPU/RSS/ARMv7 playback benchmark was performed.

## Blocking boundaries

1. **Audio authority:** a separate output engine bypasses mpv's EQ/balance/gain.
   A browser-only switch cannot prevent a Spotify phone from reactivating it.
   Safe arbitration must cover legacy HTTP/SOAP, favorites, EOF/recovery/staging,
   remote Connect transfers, crashes and unknown acknowledgements. That requires
   a reviewed whole-renderer output-ownership design, not a late wrapper.
2. **Terms scope:** Spotify's broad Developer Policy restricts combined-service
   content and audio overlap. Soloist's specific DIY invitation is also real.
   Applicability to this project's combined-provider/shared-audio design remains
   unresolved; obtain scoped clarification rather than claim either blanket
   permission or blanket prohibition. [Developer Policy](https://developer.spotify.com/policy)
3. **Account/hardware:** no user key, pairing, actual Connect music, ARM32 runtime,
   DAC or target resource acceptance exists. **ACCOUNT_PENDING / HARDWARE_PENDING**.

## Reconsideration gate

First settle the intended integration/terms scope and output-ownership design.
Then let the user obtain the official client and perform their own key/pairing
steps. Begin with mutually exclusive standalone playback, retaining separate
state and private credentials. A shared PCM/DSP pipeline needs its own measured
format, buffering, seek, lifecycle and gain proof. Do not distribute proprietary
binaries, evade expiry/provisioning, or treat mock contracts as real Connect.
