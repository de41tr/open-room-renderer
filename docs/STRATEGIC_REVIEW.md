# Strategic review

Optimize useful, verified user workflows per hour, not test/commit/line counts.
All work stays on the autonomous branch and draft PR; no merge or release.
Stabilization starts 2026-10-12 19:00 UTC; active work ends at 20:00 UTC.

## Current decision (2026-10-12 10:02 UTC)

- **COMPLETE:** bounded formatter extraction at 8291763 passed all three standard
  CI jobs by 09:51:33 UTC, elapsed 36m40s. No further extraction is needed now.
- **PIVOT / CONTINUE:** implement Repeat One / All through the existing guarded
  EOF path, with one mode authority and a small pure queue-policy module. Public
  pywiim and independent SOAP-client evidence use normal=4, one=1, all=0; current
  normal=0 is incorrectly read as repeat-all. Correct this explicitly, document
  the conflicting official HTTP write table, and retain original-app acceptance
  pending. Manual Next/Previous remain numerical and nonwrapping. Native staging
  stays normal-only; changing an armed mode requires a confirmed Stop first.
- **DEFER:** more raw-backend corner-case expansion, additional multihour probes,
  broad architecture changes and provider experiments without a viable lawful
  path. Earlier effort is not a reason to keep extending low-value work.

Priority order: critical regressions/security and green CI; useful new local
features; targeted low-risk refactoring; special tests only with high information
value; documentation and later hardware preparation. Existing quality gates stay.

Review next around 11:32–12:02 UTC, or after the repeat package finishes earlier,
then about every 90–120 minutes
and after major packages. Each review records CONTINUE/PIVOT/DEFER with concrete
value, effort and risk. Failed evidence stays visible; a green test count alone
is not functional completion.

## Open proposals (maximum five)

| Proposal | Category | Value / effort / risk | Next action |
|---|---|---|---|
| Repeat All / Repeat One for real local queues | A_AUTONOM | High everyday value; estimate 1–2h after contract check; medium ownership/EOF risk | Verify public mode semantics, implement a small queue-policy boundary, prove actual source/position and HTTP/SOAP/event agreement through EOF and owner controls. |
| Shuffle with an explicit play-order contract | A_AUTONOM | High queue value; estimate 1–2h; medium index/history/staging risk | Build on a verified queue policy. Keep canonical item/index/source consistent; test a complete multi-item cycle and manual navigation. |
| Editable persistent local favorites | A_AUTONOM | Useful local control; estimate 1–2h; medium persistence/interface risk | Check the public control contract, then choose a bounded create/rename/delete/recall workflow using existing atomic state. No provider accounts or invented compatibility claims. |
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
