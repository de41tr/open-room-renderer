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
not automatically respawn; restart the renderer to retry owned spawning. The
same one-spawn-per-backend contract applies after an owned player dies following
successful startup. A new AP07 post-decode SIGKILL/HTTP/SOAP/GENA case is gated for
standard native CI; its real process-death result remains pending. Deterministic
post-ready death and loopback failure-projection counterparts pass locally.

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

Per-load native options now use named arguments shared by mpv0.37 and 0.40,
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
loopback and separate real-libmpv decoder evidence. The production Unix-client
and installed CLI restart/signal lanes passed on both architectures in run38200058359. Lost
Stop/Pause acknowledgements expose ERROR and preserve owner intent instead of
automatically advancing or restarting the queue.

Python3.11.17 has actual x86_64 default-suite and selected real-libmpv evidence at
9f5f529; this does not imply ARMv7/Bookworm acceptance. Public-client playlist
URIs now preserve ports, encoded bytes and literal colons, with real local
M3U/PLS decoding and honest pre-mutation faults for unsupported legacy indexes.

Explicit zero-duration station loads now avoid native backward packet history;
finite/unknown files and forward buffering retain their native defaults. Local
mpv 0.40 property/reset and 300-second continuous-stream checks passed. The
original default-cache hour attempt failed its RSS gate before completion; the
new policy hour and extended production Unix checkpoint remain pending. No
indefinite-memory, WAN, physical-audio or hardware claim is implied.


Checkpoint 4ef68e4: production Unix-IPC repeated-lifetime stress passed over 900s
on native x86_64 and AArch64, with all owned processes/resources cleaned up.
The zero-history policy was superseded after a changing-ICY regression. Current
per-file 1 MiB history plus donation disabled preserves native timed titles and
bounds retention; local mpv0.40 reset/metadata and first-level M3U/PLS child tests
pass. Native0.37/Unix gates and a separately frozen finite-policy hour remain
pending their own results. The actual installed SIGHUP cycle and injected
Zeroconf withdrawal-failure cleanup passed on both architectures at 8d643657.
That run's ARM whole job failed its final optional Snapcast signal check; the
failure is retained and under diagnosis. Physical-LAN/app acceptance remains open.


At ba7b3120, native mpv0.37 finite-history/changing-metadata and first-level
M3U/PLS child gates passed on both x86_64 and AArch64. The only failed test expected
HTTP 503 rather than the existing 502 after real owned-player death; correcting
that test does not change production behavior. Its full restart tail still needs
native verification. The active finite-policy hour remains pending.

A separate INTEGRATION/EMULATED_ARMV7 probe passed real 32-bit ARM Python/C-extension
imports, persisted identity and loopback HTTP/SOAP with MOCK/unavailable audio.
Native ARMv7 installation, subprocess chain, mpv/ALSA and hardware stay NOT_RUN;
no successful emulator import is presented as installation or audio proof.


Checkpoint 62b089ad completed all native x86_64/AArch64 CI gates, including the
actual owned-player-death/fresh-restart tail. The new optional changing-ICY
endurance mode passed 240.821s and 30 independently observed title epochs locally,
with one connection/load, bounded history/resources and verified cleanup. This
is REAL_BACKEND/null, not a completed four-hour or production-Unix endurance run.
EQStat remains configured-graph status: tested FFmpeg 7.1 above-Nyquist filters
bypass at low source rates, as documented; no universal acoustic effect is claimed.


Rolling live HLS now has local libmpv 0.40/null coverage for playlist/segment
outage and return plus owner Pause/Stop/replacement during recovery backoff.
Production timings are unchanged; no custom player is introduced. Five new
hosted cases remain pending their own result. This is separate from production
Unix HLS, real WAN, encryption/DRM, adaptive variants and hardware acceptance.


The final finite-policy constant-title hour passed 3600.675s with one load/request,
362 protocol agreements, peak RSS growth 1936 KiB and complete cleanup. This is
separately frozen loopback AAC/libmpv0.40/null evidence, not changing-ICY endurance.
The four-hour changing-ICY run started 06:06:23.924 UTC and remains in progress.

At 3e74b24, hosted HLS playlist recovery and three owner-command cases passed;
segment-return failed on both architectures because native playback-time became
unavailable during the two-second stall hold. x86 also exposed unavailable time
immediately after a native child path appeared in two playlist tests. All other
native gates and Python3.11 passed. The transition/readiness failures remain
reproduced: native segment503 EOF and path-before-decoder readiness. Tests-only
corrections preserve actual playback/identity checks; integrated 0.37/0.40
replay passed all eleven affected native cases. The new hosted result remains
pending. The failed checkpoint is not relabeled green.


The supported native mpv floor is >=0.37. Actual 0.35.1 decoding does not provide
playlist ancestry: M3U/PLS HTTP Play fails honestly with 502. The installer now
rejects that incomplete runtime before creating its prefix. Debian Bookworm's
default 0.35.1 package, including ARM variants, is insufficient; this is a concrete
prerequisite limitation, separate from hardware/ARMv7 installation acceptance.


Queue navigation now preserves stopped/paused owner intent, including pending
Stop and paused-seek transitions. Stopped neighbor selection is silent; paused
multi-item navigation rejects without resuming. Explicit Play and playing
navigation remain functional. Canonical SOAP/GENA action availability matches
these states, finite seek readiness and queue boundaries. Native0.37/0.40 and
real pywiim/loopback evidence are distinct from original Android acceptance.

Python3.13 application/default tests also pass on the exact navigation/action
snapshot after isolating a unit fixture's optional-host prerequisite. The real
installer correctly remains blocked by missing ensurepip; a private test venv
is not successful native installation. Foreign-source observation/admission
correction remains a separate open checkpoint, so canonical actions do not yet
certify every externally replaced native source.


Coherent post-ready native replacement and observed conflicts at automatic
retry/EOF admission now report silence-unconfirmed ERROR without overwriting
unrelated audio. Terminal errors stay terminal across those waits. Unloaded
paused/stopped selections cannot import foreign media fields. Unit/race checks
and actual libmpv0.37/0.40 observer/recovery proof pass; scheduled native event
serialization is separate from delivered network callbacks and Unix IPC.
Already-admitted load/readiness replacement remains outside this guarantee.


At 5d64bfb, both hosted architectures failed only the native nested-playlist
cleanup case (46 other native cases passed); other runtime gates passed. The
reviewed pending-load observation boundary restores owned cleanup without
claiming nested playlist support or causal proof of raw external takeover during
readiness. Atomic rename has real local HTTP/reload evidence with synthetic
storage/discovery faults. Duplicate EQ JSON now rejects before mutation while
valid preset loading remains supported. Neither adds hardware/provider claims.

The repair snapshot passed all 47 local native cases on libmpv0.37/0.40; the
separate terminal-error delta then passed seven affected native cases on each.
Final combined default verification passed 2682 /65, plus 392 Python3.11 focused
checks. Hosted final-revision Unix IPC and architecture checks remain separate.


Published b18b43e passed all standard CI gates, including both hosted architectures.
The next causal-root guard is intentionally observation-time and excludes internal
EDL transformation, missing ACKs, containers, range reloads and reused core IDs;
it cannot atomically control unrelated raw mpv writers. New first-run identity
publication prevents partial files without replacing existing IDs. Its hard-link/
fsync requirements are Linux local-filesystem assumptions, not power-loss proof.
The revised stopped-snapshot test keeps its original total deadline; its twelve
helper cases are UNIT and must not be counted as actual native/hardware tests.

The exact next combined source passed 2759 default cases /66 opt-in skips on
Python3.11 and 3.12. Its selected libmpv lane passed 60 cases on 0.37/0.40, comprising
47 actual native cases and 13 deterministic helper checks. Hosted Unix IPC and
architecture verification remain a distinct pending gate for this revision.
