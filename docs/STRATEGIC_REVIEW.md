# Strategic review

Optimize useful, verified user workflows per hour, not test/commit/line counts.
All work stays on the autonomous branch and draft PR; no merge or release.
Stabilization starts 2026-10-12 19:00 UTC; active work ends at 20:00 UTC.

## Current decision (2026-10-12 16:22 UTC)

- **COMPLETE:** offline playlist capture/import/edit/restore, stable-ID favorites,
  explicit target repair and bounded SSDP candidate discovery have full hosted
  checkpoints. Discovery's actual console/multicast case passes both architectures.
- **CONTINUE narrowly:** publish the independently reviewed legacy endpoint/session
  correction. Exact merged defaults pass both minors; require complete hosted CI.
  This preserves valid wire/output behavior and existing redirect rejection.
- **PIVOT:** final operator-document consistency, acceptance boundaries and clean
  stabilization. Five concrete documentation errors are corrected without runtime
  changes. Do not invent cosmetic features or special tests merely to fill time.
- **DEFER:** DSP CLI, browser UI without established QA, rich bookmarks, per-instance
  DAC YAML, extra multihour runs and major core/provider work. The remaining choices
  and hardware plan belong in the final handover, not an unbounded late redesign.

Priority order: critical regressions/security and green CI; useful new local
features; targeted low-risk refactoring; special tests only with high information
value; documentation and later hardware preparation. Existing quality gates stay.

Review after the final legacy gate, then during the 19:00 UTC stabilization window. Record value,
effort, risk and CONTINUE/PIVOT/DEFER; green counts alone are not completion.

## Open proposals (maximum five)

| Proposal | Category | Value / effort / risk | Next action |
|---|---|---|---|
| Legacy CLI transport consistency | A_AUTONOM | Reviewed concrete host/proxy/netrc correction; local suites green | Publish and verify complete CI, then close. |
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
