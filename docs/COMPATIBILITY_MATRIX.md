# Compatibility matrix

This is scoped evidence, not a product certification. Updated 2026-10-12 UTC.

| Area | Evidence | Remaining limit |
| --- | --- | --- |
| SOAP action validation/faults | UNIT/MOCK regression coverage | Actual client/app execution pending |
| Volume/mute/control | Real Unix IPC on x86_64/AArch64; measured digital PCM; local PyWiiM integration | Physical mixer/DAC/acoustic effects pending |
| Selected-URI playback/control | Real production Unix IPC + libmpv/null HTTP flows on x86_64/AArch64 | Physical audio and Android pending |
| Playback generations/EOF | UNIT/MOCK races + actual Unix IPC observer/queue EOF | Target hardware and WAN conditions pending |
| Concrete multi-item queue | UNIT/MOCK + REAL_BACKEND/null indexed/Next/Previous/EOF selection | Unsupported provider headers fail explicitly |
| Next/Previous; normal loop mode | Actual queue selection; normal mode only | Repeat/shuffle remain unsupported |
| Discovery/identity and app-shaped handshake | Actual same-host multicast discovery/independent identities + local HTTP handshake; truthful typed SCPDs | Physical LAN and actual Android app pending |
| GENA lifecycle/callback safety | Real local subscribe/renew/notify/expiry tests; actual callbacks in REAL_BACKEND/null workflow | Actual Android parser/client pending |
| Synthetic WAV/MP3/FLAC decode | REAL_BACKEND, mpv null output | No audible/DAC assertion |
| Linux AArch64 | Native Ubuntu CI: build/dependencies, Unix IPC, null/PCM tests | Native isolated installation verified; physical board/service boot untested |
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
IPC is verified on native x86_64 and AArch64 CI; target-host acceptance remains. A failed spawn attempt does
not automatically respawn; restart the renderer to retry owned spawning.

Established explicit live stations now have bounded owner-respecting EOF/idle
recovery (UNIT/MOCK and actual local HTTP outage/return with REAL_BACKEND/null).
Sustained no-progress cache stalls now use a15-second observation grace and
the same finite retry budget; progress/owner commands reset or cancel recovery.
Exhaustion confirms actual silence before terminal source failure. Automatic initial-load retry remains unsupported. Provenance-bound public
station addresses are refreshed on established recovery or a later explicit Play;
finite/unknown media are never silently restarted.

Volume/mute now use confirmed full-pair backend readback, pre-load/resume
reconciliation and atomic persistence. Actual HTTP controls have digital PCM
evidence for cubic gain, exact mute/zero, source retention and restored settings.
Unknown gain is marked explicitly rather than freshly advertised as confirmed.
Host mixer, acoustic silence, DAC controls and full service reboot remain untested.

Existing PEQ/balance has transactional configuration readback plus measured
digital PCM cuts, pass filters, stereo balance silence, reload and failed-native-
candidate retention. Invalid/unimplemented controls fail instead of succeeding.
Preset library persistence and active EQ replay on fresh backend startup are tested.
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

AP14 neutral startup/client ports default to8080 and identity persists. Explicit
app-compat/port80 is separate; no launcher automatically elevates to root.
Temporary native installation/import/uninstall and static systemd-unit checks
passed on x86_64. Native AArch64 installer execution passed in
[run38188925897](https://github.com/de41tr/open-room-renderer/actions/runs/38188925897). Neither that nor existing AArch64 playback CI proves ARMv7 or a
Pi/XU4/systemd-reboot/DAC deployment. See GETTING_STARTED.md for exact profiles.

AP13 now restores the complete confirmed active EQ/balance/bypass context,
including before deferred load/resume/unmute. Corrupt saved state fails closed.
Fresh native-handle digital PCM confirms the tested cut/balance and bypass
restore; this is not a full-service reboot or acoustic/DAC claim. Explicit mute
and zero volume remain usable if DSP restoration fails.

AP15 REAL_BACKEND local fixtures verify MP3, ADTS AAC, octet-stream, unencrypted
HLS, redirects, ICY and first-level M3U/PLS. The container is the canonical queue
resource; native children retain proven container ownership and final EOF alone
advances the top-level queue. Entry loading reports BUFFERING and foreign-source
controls cannot falsely succeed. Nested playlists remain unsupported because
outer provenance is lost. Owner-fenced failed-load cleanup now confirms Stop
and actual idle before reporting a controlled readiness failure; failed cleanup
remains explicitly uncertain. Actual nested-M3U decoding and cleanup are tested.

Per-load native options now use named arguments shared by mpv0.37 and0.40,
avoiding the positional insertion-index change in0.38. Actual0.40 and native CI0.37 start/options/
reset and gain checks passed. This fixes
option transport compatibility, not approximate byte-range timestamp accuracy.

AP16 now has a reviewed clocked digital transition probe with a2ms reported
tolerance, known-silence calibration and equal/mixed-rate/pause cases. Native
calibration now passes on x86_64/AArch64. Renderer equal-rate gaps measured
6.125/25.125ms; direct native mpv playlist measured0ms in the same runs.
Renderer gapless capability remains unadvertised. A passing
measurement test must not be confused with a WITHIN_TOLERANCE audio result.

Explicit IPv4 discovery now has real same-host multicast and socket-isolation
evidence. The discovered-endpoint production Unix-IPC flow passed on both native
architectures in [run38192653073](https://github.com/de41tr/open-room-renderer/actions/runs/38192653073). No
physical-network reachability or original Android compatibility is inferred.

Experimental Plex capability entries remain inactive and absent from playable
service lists/legacy bits, including explicit diagnostic profiles. Account
scaffolding is not complete playback integration and no account flow was run.

Optional Snapcast acceptance now has actual same-host digital delivery, group
controls, gain/mute and reconnect evidence using official binaries. It is a
separate tool with no advertised renderer capability or mandatory dependency.
The repaired private clocked-ingress production gate passed on both native architectures in run38198310911; two-host synchronization, physical
output and WiiM grouping remain NOT_RUN. See TESTING.md for exact invocation,
private evidence handling and non-atomic group-volume limits.

Native successor staging is a default-off experiment with real libmpv ownership
and supplementary equal-rate clocked improvement evidence. Production Unix-IPC
clocked equal-rate staging measured 0ms on x86_64 and AArch64 in run38196685851. It changes no audio defaults or advertised
capability. Mixed-rate gaps and unprovable nested/foreign-tail ancestry remain
explicit limits; the latter reports ERROR and silence not confirmed without an
unsafe Stop. See GETTING_STARTED.md before enabling.

Public-station renewal now has controlled real-backend address-change evidence,
including staging-enabled operation; live-provider availability remains unverified.
The repeated-lifetime production Unix stress lane passed on both native architectures in run38198310911 (three lifetimes each).

Actual pywiim navigation and percent-encoded media resources now have real-client
loopback and separate real-libmpv decoder evidence. The new production Unix-client
and installed CLI restart/signal lanes await their exact native CI result. Lost
Stop/Pause acknowledgements expose ERROR and preserve owner intent instead of
automatically advancing or restarting the queue.
