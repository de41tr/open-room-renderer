# Development plan

Branch: `dot/renderer-autonomous-20261012`; base: `3672b9838e9c39ae8191613108f0d91a5cada97d`.
Work window ends 2026-10-12 20:00 UTC. Stabilization only after 19:00 UTC.
No main-branch edits, merge, or release.

## Ordered work

1. AP01: explicit SOAP contracts, faults, and acknowledged backend commands.
2. AP02: authoritative playback phases and generation-scoped observations.
3. AP05: real multi-item queue, one-based selection, browse and transport navigation.
4. AP03: consistent discovery, persistent independent identity and honest capabilities.
5. AP04: renewable, bounded, safe GENA subscriptions and queue events.
6. AP06: header-only public-radio selection through a bounded anonymous public-station resolver.
7. AP07: backend/process failures and owned shutdown.
8. AP08: recovery that respects stop, pause, and newer playback generations.
9. P1 where time permits: metadata, confirmed seek, volume/mute, existing DSP,
   persistent settings, ARM installation, radio formats and measured transitions.

P2 provider integrations remain disabled. Bounded Snapcast digital/control
experiments are separate from two-host synchronization or an advertised service.
No proprietary
material, credentials, original identifiers, captures or binaries belong here.
Keep the production Unix-socket mpv design; use deterministic test doubles where
this execution environment cannot create Unix-domain sockets.

## Checkpoints reached

AP01, AP02, AP05, AP03, AP04, AP06, AP07 and AP08 have implementation checkpoints,
including real libmpv/null control, two-track/callback and interrupted-source
recovery flows. AP11 now confirms and persists actual digital gain. AP12 now validates and transactionally confirms existing DSP, with measured
PCM effects. AP09 preserves canonical and opaque metadata across client surfaces.
AP10 confirms actual seeks, including pause/resume and stale-command rejection.
AP14 provides reviewed native installation and stable unprivileged profiles.
AP13 restores complete confirmed active DSP, including deferred startup.
AP15 has first-level native playlist ownership and local radio-format evidence.
Log privacy, calibrated clocked transition measurement, version-independent load
options, explicit multicast discovery and same-owner unready-load cleanup are
implemented. Native discovery-to-playback passed on both CI architectures.
Sustained no-EOF station stalls now have reviewed owner-fenced bounded recovery.
Equal-rate native staging now measures 0ms on both CI architectures, with mixed-rate limits.
Public-station address refresh, uncertain-owner transport fences and exact
public-client resource parsing are implemented. Installed CLI persistence,
genuine pywiim controls and the Python3.11 floor pass CI. The extended native
Unix-IPC checkpoint passed over fifteen minutes on both architectures; routine
CI durations are restored.

Current checks: complete owned-player death/error/fresh-backend restart now
passes both native architectures, as do the finite-history and changing-metadata
policies. The separately frozen constant-title finite-policy hour passed with
complete cleanup. The bounded changing-ICY endurance mode passes four-minute
replay and negative sensitivity; its separate four-hour run is active from
06:06:23.924 UTC, earliest finish 10:06:23.924 UTC plus cleanup. Keep its frozen
provenance separate from later code changes. Rolling live-HLS cases pass locally
but the expanded hosted libmpv lanes failed at 3e74b24. Both fixture assumptions
(segment503 native EOF and path-before-decoder readiness) are reproduced and
corrected; integrated and hosted 0.37/0.40 checks pass at 2fc8296, including
the honest >=0.37 installation floor. Current concrete gaps: stopped/paused
queue navigation and the shared canonical SOAP/GENA action projection are now
reviewed and integrated together with foreign-source observation and automatic
admission fences. Verify the next hosted checkpoint; preserve explicit Play
and terminal errors. Separately investigate causal ownership during an already
admitted load/readiness window without weakening existing source checks. A confirmed
ambiguous persisted-gain parsing gap now has a reviewed fail-closed fix with
full local default-suite and Python3.11 targeted verification. Limited
ARMv7 emulated ABI/API evidence does not fill native installation/hardware gaps.
Nested ancestry, original-app/hardware audio and two-host sync remain explicit
limits. Do not erase the earlier unexplained Snapcast failure with later passes.
