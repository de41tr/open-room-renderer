# Compatibility matrix

This is scoped evidence, not a product certification. Updated 2026-10-11 UTC.

| Area | Evidence | Remaining limit |
| --- | --- | --- |
| SOAP action validation/faults | UNIT/MOCK regression coverage | Actual client/app execution pending |
| Volume/mute/control | Real Unix IPC on x86_64/AArch64; measured digital PCM; local PyWiiM integration | Physical mixer/DAC/acoustic effects pending |
| Selected-URI playback/control | Real production Unix IPC + libmpv/null HTTP flows on x86_64/AArch64 | Physical audio and Android pending |
| Playback generations/EOF | UNIT/MOCK races + actual Unix IPC observer/queue EOF | Target hardware and WAN conditions pending |
| Concrete multi-item queue | UNIT/MOCK + REAL_BACKEND/null indexed/Next/Previous/EOF selection | Unsupported provider headers fail explicitly |
| Next/Previous; normal loop mode | Actual queue selection; normal mode only | Repeat/shuffle remain unsupported |
| Discovery/identity and app-shaped handshake | UNIT/MOCK discovery + actual local HTTP handshake with fake backend; truthful typed SCPDs | LAN multicast and actual Android app pending |
| GENA lifecycle/callback safety | Real local subscribe/renew/notify/expiry tests; actual callbacks in REAL_BACKEND/null workflow | Actual Android parser/client pending |
| Synthetic WAV/MP3/FLAC decode | REAL_BACKEND, mpv null output | No audible/DAC assertion |
| Linux AArch64 | Native Ubuntu CI: build/dependencies, Unix IPC, null/PCM tests | Not a physical audio board or full native service install |
| Linux ARMv7 / Odroid XU4 | Architectural target | HARDWARE not run |
| Spotify/TIDAL/AirPlay/Cast/multiroom | Unsupported/deferred | No cloud login or provisioning performed |

Neutral and compatibility profiles share independent product/version metadata.
The compatibility profile changes protocol presentation and discovery identity;
it does not claim to be original hardware. Existing IDs survive upgrades and
renames. New compatibility IDs are random and persistent. Music-provider entries
are configuration-driven; unsupported diagnostic sentinels remain inactive.

The optional audio proxy is now local-backend-only and limited to selected media,
with per-hop validated/pinned destinations. This does not make the unauthenticated
renderer safe to expose publicly. LAN multicast and real target devices remain
separate acceptance steps.

Header-only `newTuneIn` public station queues now materialize one real item with
preserved identity/artwork and bounded anonymous resolution (UNIT/MOCK evidence).
Concrete episode tracks retain real durations; episode-ID resolution is unsupported.
Live public endpoint research is separate from the still-unverified end-to-end
provider playback and Android acceptance.

Backend lifecycle has bounded startup/command/teardown and explicit child/socket
ownership (UNIT/MOCK plus actual subprocess signal delivery). Production Unix
IPC remains a separate host acceptance requirement. A failed spawn attempt does
not automatically respawn; restart the renderer to retry owned spawning.

Established explicit live stations now have bounded owner-respecting EOF/idle
recovery (UNIT/MOCK and actual local HTTP outage/return with REAL_BACKEND/null).
Initial load failure, sustained cache stalls and provider URL refresh remain
separate limitations; finite/unknown media are never silently restarted.

Volume/mute now use confirmed full-pair backend readback, pre-load/resume
reconciliation and atomic persistence. Actual HTTP controls have digital PCM
evidence for cubic gain, exact mute/zero, source retention and restored settings.
Unknown gain is marked explicitly rather than freshly advertised as confirmed.
Host mixer, acoustic silence, DAC controls and full service reboot remain untested.

Existing PEQ/balance has transactional configuration readback plus measured
digital PCM cuts, pass filters, stereo balance silence, reload and failed-native-
candidate retention. Invalid/unimplemented controls fail instead of succeeding.
Preset library persistence is tested; active EQ replay on restart remains pending.
Configuration readback, decoded PCM and physical/acoustic effects remain separate.

The standard Ubuntu x86_64/ARM64 jobs both passed actual production Unix IPC
and all libmpv/null/PCM cases at `52f5c4d`, using Python 3.12.3 and mpv 0.37.0.
[Verified CI run](https://github.com/de41tr/open-room-renderer/actions/runs/38185537983).
This replaces the earlier IPC validation gap, not the physical audio/ARMv7/app
limits. The development sandbox itself still cannot create Unix sockets.

AP09 preserves supplied title/artist/album/artwork and opaque DIDL through
HTTP/SOAP/GENA and real queue navigation (UNIT and loopback INTEGRATION with
fake audio). Explicit URI wins over metadata resource hints; absent fields clear
and unknown source/MIME are not invented. Matching backend title updates only
refine generated fallback metadata. Original Android presentation is untested.

Configured local music favorites now have matching one-based listing, slot
counts and actual selection (`MCUKeyShortClick`, optional index1 only). Real
pywiim loopback verifies two distinct selected resources with MOCK audio.
Unknown/invalid entries do not become phantom playable slots. Favorites remain
configuration-owned; provider preset editing and saved playlist resume are not
implemented.

AP10 normal seek is confirmed from actual backend position (fixed ±1-second
window) and preserves pause. HTTP/SOAP/Previous share the same bounded path.
Live stations and invalid absolute bounds fault; stale or failed work cannot
claim completion. Generated local30-second media has REAL_BACKEND/null evidence.
Optional range reload stays off by default and its bitrate-derived global
position is approximate; shifted-range HTTP behavior has synthetic coverage.
