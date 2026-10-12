# Strategic review

Optimize useful, verified user workflows per hour, not test/commit/line counts.
All work stays on the autonomous branch and draft PR; no merge or release.
Stabilization starts 2026-10-12 19:00 UTC; active work ends at 20:00 UTC.

## Current decision (2026-10-12 13:59 UTC)

- **COMPLETE:** offline M3U import has full standard CI green at 369feeb, following
  stable-ID favorites and local transport controls. Actual user workflows improve
  without backend changes; earlier failed/missing checkpoints keep their history.
- **CONTINUE:** publish the reviewed standard-DMR adapter correction and require
  its new production-Unix client lane to pass hosted CI. Four actual-client
  mismatches are reproduced and fixed, with short real libmpv and parsed-event
  proof on both profiles. Existing core logic and all quality bounds remain.
- **ASSESS next, not yet implement:** safe current-queue export to the existing
  private authored format. BrowseQueue exposes resolved media and cannot serve
  as canonical export. Trusted live-radio provenance needs special handling;
  assess a bounded read-only snapshot first and defer if semantics are ambiguous.
- **DEFER:** per-instance audio-device override. Owned mpv already honors its
  service-account mpv.conf. The incremental multi-instance convenience currently
  ranks below actual client compatibility and reusable playlists.
- **DEFER:** browser controller without established acceptance, additional
  multihour runs, broad core decomposition and provider-account experiments.

Priority order: critical regressions/security and green CI; useful new local
features; targeted low-risk refactoring; special tests only with high information
value; documentation and later hardware preparation. Existing quality gates stay.

Review after the next major package, or around 15:29–15:59 UTC. Record value,
effort, risk and CONTINUE/PIVOT/DEFER; green counts alone are not completion.

## Open proposals (maximum five)

| Proposal | Category | Value / effort / risk | Next action |
|---|---|---|---|
| Standard UPnP client interoperability | A_AUTONOM | Four reproduced blockers fixed; independent and native review passed | Publish and establish the new shared production-Unix flow on both CI architectures. |
| Safe current-queue export assessment | A_AUTONOM | Potential playlist reuse; canonical/radio provenance requires care | Read-only bounded assessment; implement only if small, explicit and loss-safe. |
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
