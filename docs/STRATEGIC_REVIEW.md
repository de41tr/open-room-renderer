# Strategic review

Optimize useful, verified user workflows per hour, not test/commit/line counts.
All work stays on the autonomous branch and draft PR; no merge or release.
Stabilization starts 2026-10-12 19:00 UTC; active work ends at 20:00 UTC.

## Current decision (2026-10-12 13:38 UTC)

- **COMPLETE:** stable-ID favorite recall and common transport controls now have
  full standard CI green at d91140d. Branch/PR synchronization is restored. Earlier
  failed/missing checkpoints retain their original evidence.
- **CONTINUE:** reviewed strict offline M3U URL-list import, with final integrated
  defaults passing on both supported Python minors. It avoids manual URL transcription with a small pure parser and
  unchanged safe file publication/restore. No playback changes or native sweep.
- **PIVOT next:** reproduce standard DmrDevice interoperability using the installed
  client. The bounded diagnostic reproduced description, required-argument and
  event-endpoint failures on the unchanged baseline in both profiles. These
  block actual standard-client controls/subscriptions. Correct only those
  confirmed gaps through bounded protocol adapters and unchanged core logic.
- **DEFER:** per-instance audio-device override. Owned mpv already honors its normal
  service-account mpv.conf, documented in getting started. A new YAML override
  mainly helps multiple instances, while attached external mpv needs explicit
  semantics. This is not a missing basic DAC-selection capability.
- **DEFER:** browser controller without established acceptance, additional multihour
  runs, major core decomposition and provider-account experiments. Hardware remains
  unavailable; useful software work continues.

Priority order: critical regressions/security and green CI; useful new local
features; targeted low-risk refactoring; special tests only with high information
value; documentation and later hardware preparation. Existing quality gates stay.

Review next after the next major package, or around 15:08–15:38 UTC. Record
value, effort, risk and CONTINUE/PIVOT/DEFER; green counts alone are not completion.

## Open proposals (maximum five)

| Proposal | Category | Value / effort / risk | Next action |
|---|---|---|---|
| Standard UPnP client interoperability | A_AUTONOM | Three reproduced real-client blockers; 45–60min bounded adapter correction | Preserve red evidence and aliases; review exact typed-control/event acceptance before publication. |
| Offline M3U URL-list import | A_AUTONOM | Avoid manual transcription; reviewed small parser, bounded file risk | Finish integrated validation, publish and require full standard CI. |
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
