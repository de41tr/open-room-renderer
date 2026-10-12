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

Current checks: finite history and changing metadata, guarded first-level
playlist children, cleanup and normal native integration pass both architectures.
Correct the new death-test HTTP status expectation to the existing 502 contract,
then verify its full actual restart tail. A separately frozen finite-policy
hour is active; preserve its exact source, unchanged gates and cleanup evidence.
Develop a bounded longer changing-ICY endurance mode to exercise repeated
metadata events without fixture-side growth. Limited emulated ARMv7 ABI and
loopback protocol probes pass, while native install/hardware remain open.
The earlier optional Snapcast window failure remains unexplained despite later
strict passes. Nested ancestry, two-host sync and physical app/audio acceptance
remain explicit gaps, not inferred successes.
