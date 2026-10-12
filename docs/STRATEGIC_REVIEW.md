# Strategic review

Optimize useful, verified user workflows per hour, not test/commit/line counts.
All work stays on the autonomous branch and draft PR; no merge or release.
Stabilization starts 2026-10-12 19:00 UTC; active work ends at 20:00 UTC.

## Current decision (2026-10-12 14:34 UTC)

- **COMPLETE:** standard DMR now has actual production-Unix client acceptance on
  both architectures and full standard CI at 0d11eea. Four reproduced adapter
  failures are fixed; original-app and duplicate-service selection limits remain.
- **CONTINUE:** publish reviewed safe current-queue URL-playlist save. Exact local
  native/read-only behavior and final integrated defaults pass. The one-file floor
  rebalance improves local margin without dropping tests or increasing caps;
  require hosted evidence for the new checkpoint.
- **NEXT:** offline append/remove/reorder of saved playlists, then managed-favorite
  URL repair preserving stable identity. Current files otherwise need hand-edited
  JSON, and correcting a favorite target currently requires delete/recreate.
  These are useful 45–90 minute adapter/file packages, not live queue redesign.
- **DEFER:** DSP CLI unless its global/wifi context contract can be settled quickly
  after those packages. Do not implement a band editor or add work merely to fill
  time. Existing status already exposes transport/metadata; another view is low value.
- **DEFER:** browser controller without established acceptance, per-instance DAC
  YAML (owned mpv already honors its account's config), additional multihour runs,
  broad core decomposition and provider-account experiments.

Priority order: critical regressions/security and green CI; useful new local
features; targeted low-risk refactoring; special tests only with high information
value; documentation and later hardware preparation. Existing quality gates stay.

Review after the next major package, or around 16:04–16:34 UTC. Record value,
effort, risk and CONTINUE/PIVOT/DEFER; green counts alone are not completion.

## Open proposals (maximum five)

| Proposal | Category | Value / effort / risk | Next action |
|---|---|---|---|
| Safe current-queue URL-playlist save | A_AUTONOM | Reviewed/native-verified useful snapshot; no playback changes | Publish and verify complete standard CI. |
| Offline saved-playlist editing | A_AUTONOM | Removes manual JSON edits; 60–90min, bounded file/index risk | Add append/remove/move to a new output only; preserve exact URLs and define selection rules. |
| Managed-favorite target repair | A_AUTONOM | Preserves stable IDs when a target moves; 45–75min | Explicit replacement with documented metadata policy; active admitted playback stays unchanged. |
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
