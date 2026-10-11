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
6. AP06: header-only public-radio selection through the existing resolver.
7. AP07: backend/process failures and owned shutdown.
8. AP08: recovery that respects stop, pause, and newer playback generations.
9. P1 where time permits: metadata, confirmed seek, volume/mute, existing DSP,
   persistent settings, ARM installation, radio formats and measured transitions.

P2 service integrations and synchronized multiroom are deferred. No proprietary
material, credentials, original identifiers, captures or binaries belong here.
Keep the production Unix-socket mpv design; use deterministic test doubles where
this execution environment cannot create Unix-domain sockets.

## Checkpoints reached

AP01, AP02, AP05, AP03 and AP04 have tested implementation checkpoints, including
real libmpv/null control and two-track/callback flows. AP06, AP07 and AP08 are next;
P1 follows. This records progress, not full hardware/app acceptance.
