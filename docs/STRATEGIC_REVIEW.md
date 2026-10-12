# Strategic review

Optimize useful, verified user workflows per hour, not test/commit/line counts.
All work stays on the autonomous branch and draft PR; no merge or release.
Stabilization starts 2026-10-12 19:00 UTC; active work ends at 20:00 UTC.

## Current decision (2026-10-12 15:57 UTC)

- **COMPLETE:** offline playlist editing has full standard CI green at 3737c93.
  The headless file workflow now covers capture, import, append/remove/reorder,
  explicit stopped restore and playback without manual JSON editing.
- **COMPLETE:** managed-favorite target repair has full standard CI green at
  d9a94e2. Required metadata policy makes target changes explicit; actual native
  playback remains untouched until later recall. All previous gates remain green.
- **CONTINUE:** bounded SSDP-only candidate discovery, without HTTP/config side
  effects or an authenticated-device claim. Independent review, both final
  default suites and actual native acceptance pass; publish and verify full CI
  within the original 60–75 minute budget.
- **CONTINUE narrowly:** older CLI transport helpers inherit environment proxy/
  netrc behavior and interpolate unvalidated hosts. The source-only assessment
  supports a small endpoint-validation and isolated-session correction. Existing
  redirect rejection already works; preserve valid wire bytes/output and avoid
  unrelated parsing, response-schema or server changes. Budget 60 minutes.
- **DEFER:** DSP CLI, browser UI without established QA, per-instance DAC YAML,
  extra multihour runs and major core/provider work. Native test redistribution
  preserves all cases/caps and now has hosted timing verification; count growth is
  not a feature or a reason to relax acceptance.

Priority order: critical regressions/security and green CI; useful new local
features; targeted low-risk refactoring; special tests only with high information
value; documentation and later hardware preparation. Existing quality gates stay.

Review after the next major package, or around 17:03–17:33 UTC. Record value,
effort, risk and CONTINUE/PIVOT/DEFER; green counts alone are not completion.

## Open proposals (maximum five)

| Proposal | Category | Value / effort / risk | Next action |
|---|---|---|---|
| Bounded candidate-discovery CLI | A_AUTONOM | Fills headless address-finding gap; 60–75min | Short explicit SSDP search, no HTTP fetch, unverified-candidate labels and native acceptance. |
| Legacy CLI transport consistency | A_AUTONOM | Concrete host/proxy/netrc discrepancy; bounded 60-minute correction | Preserve valid wire bytes and outputs, independently review and verify complete CI. |
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
