# Strategic review

Optimize useful, verified user workflows per hour, not test/commit/line counts.
All work stays on the autonomous branch and draft PR; no merge or release.
Stabilization starts 2026-10-12 19:00 UTC; active work ends at 20:00 UTC.

## Current decision (2026-10-12 12:56 UTC)

- **COMPLETE locally / CONTINUE hosted verification:** common transport controls
  are published and stable-ID favorite recall is fully reviewed/tested. Actual
  native source, mute, paused-seek and compaction evidence establishes useful
  workflows. PR synchronization remains behind the verified branch; do not label
  that missing hosted result green, retry writes blindly or change PR state.
- **CONTINUE:** publish naturally completed stable-ID recall, keeping its tiny
  existing-helper extraction separate and preserving all old playback logic.
  The old numeric public-client surface remains distinct from stable managed IDs.
- **PIVOT:** strict offline M3U URL-list import into existing authored JSON files.
  It removes manual transcription without fetching media or changing the backend.
  Estimate 45–60 minutes; reuse existing safe reads/no-clobber writes and complete
  SOAP-size validation. Reject HLS/player directives instead of ignoring their
  semantics; disclose discarded EXTINF metadata and the strict subset.
- **DEFER:** browser controller. Installed executables are not successful supported
  browser QA. No alternative exposure, permission change or unsupported automation.
- **DEFER:** extra multihour tests, broad core extraction and provider-account
  experiments. Hardware remains unavailable; useful software work continues.

Priority order: critical regressions/security and green CI; useful new local
features; targeted low-risk refactoring; special tests only with high information
value; documentation and later hardware preparation. Existing quality gates stay.

Review next around 14:01–14:31 UTC, or after the next package finishes earlier,
then about every 90–120 minutes and after major packages. Record value, effort,
risk and CONTINUE/PIVOT/DEFER; green counts alone are not functional completion.

## Open proposals (maximum five)

| Proposal | Category | Value / effort / risk | Next action |
|---|---|---|---|
| Hosted verification of completed client checkpoints | A_AUTONOM | Functional work has exact local/native evidence; synchronization is delayed | Monitor PR/CI; publish only naturally completed tested checkpoints with honest pending status. |
| Offline M3U URL-list import | A_AUTONOM | Avoid manual URL transcription; 45–60min, bounded format/file risk | Implement strict no-network subset using existing authored-file and request validation. |
| Optional local controller page | A_AUTONOM / DEFER | Useful, but installed browsers alone do not establish supported QA | Keep deferred until a supported interaction route is genuinely verified. |
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
