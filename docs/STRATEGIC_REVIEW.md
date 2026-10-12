# Strategic review

Optimize useful, verified user workflows per hour, not test/commit/line counts.
All work stays on the autonomous branch and draft PR; no merge or release.
Stabilization starts 2026-10-12 19:00 UTC; active work ends at 20:00 UTC.

## Current decision (2026-10-12 14:55 UTC)

- **COMPLETE:** safe queue save has full standard CI green at 3fd5df8. The floor
  rebalance is hosted-verified with preserved cases/caps and improved margin.
- **CONTINUE:** publish reviewed offline playlist editing. Real native CLI and
  final integrated defaults pass; append/remove/reorder no longer require manual
  JSON changes. Keep the new checkpoint's hosted result pending until observed.
- **NEXT:** managed-favorite URL repair preserving stable identity/position and
  admitted playback. Require an explicit metadata policy, not accidental stale
  title/artwork reuse or unexplained deletion.
- **PIVOT after that:** bounded SSDP-only candidate discovery for headless setup,
  using the existing product token and actual native multicast fixture. No HTTP
  fetch, config-writing side effect or authenticated-device claim. A 60–75 minute
  slice is more deterministic than ambiguous DSP global/wifi controls.
- **DEFER:** DSP CLI unless time and a precise context contract remain; browser UI,
  per-instance DAC YAML, extra multihour runs and major core/provider work. A small
  redistribution of existing native tests may recover job margin; never relax
  acceptance or increase runner/process counts merely to stay green.

Priority order: critical regressions/security and green CI; useful new local
features; targeted low-risk refactoring; special tests only with high information
value; documentation and later hardware preparation. Existing quality gates stay.

Review after the next major package, or around 16:25–16:55 UTC. Record value,
effort, risk and CONTINUE/PIVOT/DEFER; green counts alone are not completion.

## Open proposals (maximum five)

| Proposal | Category | Value / effort / risk | Next action |
|---|---|---|---|
| Offline saved-playlist editing | A_AUTONOM | Reviewed useful file operations; no live queue mutation | Publish and verify complete standard CI. |
| Managed-favorite target repair | A_AUTONOM | Preserves stable IDs when targets move; 45–75min | Explicit replacement/metadata policy; active admitted playback stays unchanged. |
| Bounded candidate-discovery CLI | A_AUTONOM | Fills headless address-finding gap; 60–75min | Short explicit SSDP search, no HTTP fetch, unverified-candidate labels and native fixture acceptance. |
| Further player-core / eventing decomposition | B_AWAITING_DECISION | Potential maintenance gain; major scope and ownership/timing risk | Defer this round. Present a concrete boundary/migration plan for explicit later decision; do other authorized work meanwhile. |
| Merge or release | C_APPROVAL_REQUIRED | Distribution decision, not an implementation milestone | Do not act. Requires separate explicit authorization; current instruction prohibits it. Silence is not consent. |

A_AUTONOM means small, reversible work within the current authorization, not
permission to expand scope indefinitely. B items need a decision before the major
change. C items require action-specific authorization; none is requested merely
to keep this software-development task moving.

## Hardware and final handover

**HARDWARE_ACCEPTANCE_PENDING:** XU4 and the original WiiM app are unavailable;
there is no known availability date. Do not wait for them or repeatedly ask for
access. Existing null/PCM, loopback and hosted-architecture evidence does not
replace physical ALSA/DAC, original-app, physical-LAN or two-host acceptance.

The frozen four-hour radio test passed including cleanup at 10:06:26.620761 UTC. Do not
start another multihour run without a concrete high-information reason. A bounded
45–60 minute XU4/ALSA/DAC/app plan is now in docs/TESTING.md; it has not been executed.

At the deadline, report the three biggest remaining practical constraints,
technical debts and high-value missing features, plus next-phase user decisions
and the later hardware plan. Base these on the final state, not this initial list.
