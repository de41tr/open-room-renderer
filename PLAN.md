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
Public-station address refresh is implemented and independently reviewed. Next:
verify repeated-lifetime production IPC and the clocked Snapcast ingress repair;
complete the separately frozen continuous-station soak. Nested playlist ancestry
and real hardware/app acceptance remain open. The
least-privilege public CI lane passed real Unix IPC on x86_64 and ARM64.
This records progress, not full hardware/app acceptance.

Lost-acknowledgement owner controls and retired-native-output races now have a
reviewed fence. Actual-client HTTP navigation and encoded-media URI preservation
are fixed. Next verify the installed CLI process boundary and genuine pywiim
production IPC in native CI, then complete the frozen continuous-station run.

Installed CLI and genuine pywiim production-IPC gates now pass both architectures.
The Python3.11 runtime floor is locally verified; a separate bounded CI floor job is added. A reproduced wrong-resource playlist parse is corrected. Next: evaluate
unused backward history for explicit non-seekable live stations and complete
continuous-stream endurance without changing the existing run or its budgets.

Explicit non-seekable stations now avoid unused backward packet history with
reviewed per-file native options. Complete the separately frozen hour and the
one-time 900-second production IPC checkpoint, then restore normal CI duration.
A reproduced mDNS unregister-failure resource leak is being fixed independently.
