# Strategic review

Optimize useful, verified user workflows per hour, not test/commit/line counts.
All work stays on the autonomous branch and draft PR; no merge or release.
Stabilization starts 2026-10-12 19:00 UTC; active work ends at 20:00 UTC.

## Current decision (2026-10-12 12:31 UTC)

- **COMPLETE:** queue load/show/index selection and explicit authored playlist
  creation/restore have native source/progress/event proof. The first hosted
  checkpoint's Python-floor timeout is retained; its reviewed same-runner
  scheduling repair now has all standard jobs green, without relaxed caps or
  omitted checks. Stop expanding infrastructure while the bounds hold.
- **CONTINUE:** publish reviewed client-only next/previous/mute/seek/current-mode
  commands. They expose existing useful behavior without core changes; the exact
  expanded native flow and final default suites pass. Finish hosted verification.
- **CONTINUE:** stable-ID favorite recall. Numeric slots move after deletion;
  resolving a detached snapshot at request admission prevents wrong-target recall.
  Estimated 45–60 minutes; keep the existing playback path and a tiny shared helper,
  with independent race/failure and one short native verification.
- **DEFER:** browser controller. Installed browser executables are not successful
  QA. Supported cloud browser interaction has not established a working fixture;
  no alternative exposure, permission change or unsupported automation route.
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
| Common transport CLI controls | A_AUTONOM | Reviewed existing-contract convenience; low core risk | Publish tested commands and verify standard CI. |
| Stable-ID favorite recall | A_AUTONOM | Prevent wrong selection after slot compaction; 45–60min, bounded ownership risk | Finish snapshot/callback review and short native proof; keep playback internals unchanged. |
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
