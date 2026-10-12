# Strategic review

Optimize useful, verified user workflows per hour, not test/commit/line counts.
All work stays on the autonomous branch and draft PR; no merge or release.
Stabilization starts 2026-10-12 19:00 UTC; active work ends at 20:00 UTC.

## Current decision (2026-10-12 11:28 UTC)

- **COMPLETE:** Repeat and editable favorites have full standard CI green. Real
  source/progress, edit continuity, restart and received-event evidence support
  these workflows. The scheduling-only change retains every gate on the same
  runners and brings hosted jobs to about 734–743s under the unchanged 900s cap.
- **CONTINUE:** publish Shuffle and stable-radio save-current as separate reviewed
  functional commits in one fully tested head. Their combined local Python and
  full native suites pass. Finish hosted verification before closing the package.
- **PIVOT next:** assess convenient CLI multi-track queue and saved-playlist flows
  through existing SOAP contracts, without altering backend ownership logic.
- **DEFER:** the optional local controller page until actual browser verification
  is available. The cloud browser and a separate shell could not reach the isolated
  fixture; this does not establish a browser-specific policy block. No alternative
  exposure, tunnel or access bypass is justified. Failed-tab cleanup was separately
  policy-denied and is not retried.
- **DEFER:** additional multihour probes, broad core extraction and provider-account
  experiments. Hardware remains unavailable; useful software work continues.

Priority order: critical regressions/security and green CI; useful new local
features; targeted low-risk refactoring; special tests only with high information
value; documentation and later hardware preparation. Existing quality gates stay.

Review next around 12:58–13:28 UTC, or after the next package finishes earlier,
then about every 90–120 minutes
and after major packages. Each review records CONTINUE/PIVOT/DEFER with concrete
value, effort and risk. Failed evidence stays visible; a green test count alone
is not functional completion.

## Open proposals (maximum five)

| Proposal | Category | Value / effort / risk | Next action |
|---|---|---|---|
| Shuffle and stable-radio favorite checkpoint | A_AUTONOM | Implemented/reviewed with real native workflows; low remaining publication risk | Publish separate functional commits in one tested head; finish hosted CI. |
| CLI queue and saved-playlist workflows | A_AUTONOM | Useful without the original app; estimate 1–2h after contract check; medium file/partial-failure risk | Verify existing SOAP read/replace/index contracts; prefer client-side responsibility module and explicit no-autoplay restore. |
| Optional local controller page | A_AUTONOM / DEFER | Useful but real browser verification was not established | Keep the small same-origin/no-framework boundary for a later verified environment. No exposure or permission expansion. |
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
