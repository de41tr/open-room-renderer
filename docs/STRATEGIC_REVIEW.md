# Strategic review

Optimize useful, verified user workflows per hour, not test/commit/line counts.
All work stays on the autonomous branch and draft PR; no merge or release.
Stabilization starts 2026-10-12 19:00 UTC; active work ends at 20:00 UTC.

## Current decision (2026-10-12 10:53 UTC)

- **COMPLETE:** Repeat One / Repeat All at c780c80 passed all three standard CI
  jobs. Actual source IDs/progress and received protocol events prove repeated
  playback and queue wrapping. The earlier bounded formatter extraction is done.
- **CONTINUE:** publish editable favorites and then shuffle as separate reviewed
  checkpoints. Favorites has real public-client recall, edit continuity and
  restart-without-autoplay evidence; shuffle has actual non-numeric progression,
  history navigation and cycle rollover. Finish their integrated native/CI gates.
- **CONTINUE, bounded:** preserve the existing 15-minute CI job limit by running
  two independent native groups on the same runner. The previous ARM job took
  874.6s before the added features; reviewed local scheduling retains every case
  and cuts the 62-case libmpv lane from about 431s to 235s. No extra runner, reduced
  assertion, shorter stability run or relaxed audio threshold is introduced.
- **PIVOT next:** investigate stable public radio references for the new favorites
  workflow. Its current conservative save-current rejection is a practical gap.
  Proceed only if existing trusted resolver provenance establishes a persistent
  anonymous reference; otherwise defer rather than save a temporary/account URL.
- **DEFER:** further raw-backend corner-case expansion, multihour probes, broad
  architecture changes and provider experiments without a viable lawful path.

Priority order: critical regressions/security and green CI; useful new local
features; targeted low-risk refactoring; special tests only with high information
value; documentation and later hardware preparation. Existing quality gates stay.

Review next around 12:23–12:53 UTC, or after the next feature package finishes earlier,
then about every 90–120 minutes
and after major packages. Each review records CONTINUE/PIVOT/DEFER with concrete
value, effort and risk. Failed evidence stays visible; a green test count alone
is not functional completion.

## Open proposals (maximum five)

| Proposal | Category | Value / effort / risk | Next action |
|---|---|---|---|
| Editable persistent local favorites | A_AUTONOM | Implemented and reviewed; real client/native workflow verified; remaining publication risk is low | Finish bounded parallel CI verification, publish separately, and verify standard CI. |
| Shuffle with explicit traversal/history | A_AUTONOM | Implemented and reviewed; actual four-track flow verifies source order/history/cycle rollover; integration risk is medium | Complete combined favorites/repeat integration and full native gates; publish after the preceding checkpoint is green. |
| Stable radio favorite references | A_AUTONOM | High practical value; estimate 30–90 min after code/public-contract check; medium provenance risk | Investigate a persistent anonymous canonical reference. Do not infer one from a temporary resolved URL or add account data; defer if evidence is insufficient. |
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
