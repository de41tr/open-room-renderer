# Strategic review

Optimize useful, verified user workflows per hour, not test/commit/line counts.
All work stays on the autonomous branch and draft PR; no merge or release.
The previous window completed at 2026-10-12 20:00 UTC. A separately authorized
DSP CLI task runs 20:35:04–21:35:04 UTC; it does not reopen the broader plan.

## Final stabilization decision (2026-10-12)

- **COMPLETE:** the published local workflows, legacy request isolation and final
  malformed-format/recovery acceptance have full standard CI green at 1d05480,
  verified at 17:16 UTC. The last addition changed tests, CI wiring and docs; runtime source
  remains unchanged from a7e8d8. No existing gate or cap was weakened.
- **COMPLETE:** all 20 original acceptance packages have a scoped review; actual
  missing observations remain explicit. The required practical constraints, debts,
  missing features, decisions and later hardware plan are recorded below.
- **STABILIZE:** from 19:00 UTC, verify the documentation-only final handover, its
  complete CI and branch/PR/checkout agreement. No further justified low-risk
  runtime defect emerged. Do not invent a feature or test merely to fill time.
- **STOP:** active work ends at 20:00 UTC. No automatic continuation, merge, release or
  provider-account action follows from this record. A later task must establish
  its own scope; hardware/app availability remains unknown.

Priority order: critical regressions/security and green CI; useful new local
features; targeted low-risk refactoring; special tests only with high information
value; documentation and later hardware preparation. Existing quality gates stay.

The final review values verified user workflows and explicit limits over counts.
The remaining proposals below are for later explicit decisions, not unfinished
permission to extend this autonomous window.

## Open proposals (maximum five)

| Proposal | Category | Value / effort / risk | Next action |
|---|---|---|---|
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

## Next-phase assessment

This assessment distinguishes missing software from acceptance that needs a real
device. Current hosted results do not remove **HARDWARE_ACCEPTANCE_PENDING**.
The task's authorization ends at 20:00 UTC on 2026-10-12; the proposals below do
not authorize unattended work, merge, release or provider-account access later.

### Three practical constraints

1. **Target-device acceptance:** actual ARMv7 XU4 installation, ALSA/DAC sound,
   physical-LAN discovery and the original app remain untested. Hardware/app
   availability has no known date. Native x86_64/AArch64 Unix IPC, null decoding
   and digital PCM are software evidence, not substitutes for those checks.
2. **Deployment prerequisites:** a suitable Linux audio environment, Python
   >=3.11 and native mpv >=0.37 are required. The API is unauthenticated and
   belongs on a trusted LAN, not the public internet. DAC selection already
   works through the service account's mpv configuration; per-instance YAML
   selection is a deferred convenience, not missing basic output selection.
3. **Ecosystem limits:** this remains a local renderer. Provider-account playback,
   AirPlay/Cast and advertised synchronized multiroom are unsupported/deferred.
   Anonymous public-radio resolution, experimental staging and same-host
   Snapcast evidence do not establish a complete commercial-service ecosystem,
   universal gapless playback or two-host synchronization.

### Three technical debts

1. **Concentrated stateful orchestration:** HttpApi still combines protocol
   dispatch, generation/source ownership, recovery, staging, DSP transactions
   and event delivery. A later separation must preserve one playback authority
   and the existing command/observation ordering; it is not a late-round rewrite.
2. **Shared contracts inside adapters:** server queue export and several clients
   reuse validation/framing and the private endpoint helper from queue_client.
   A small behavior-neutral pure-contract module is the clearest next extraction
   before moving recovery, staging or player ownership.
3. **Legacy/canonical service presentation:** duplicate UPnP service types retain
   legacy aliases and suit the tested last-entry-wins client. A first-entry-wins
   controller may select the legacy schema. Changing this compromise requires
   explicit controller/profile acceptance, not a claim of universal UPnP support.

See [the target architecture](architecture.md#bounded-extraction-and-target-responsibilities)
for protocol, player, queue, eventing, DSP and service-adapter boundaries.

### Three high-value missing features

1. A small local browser controller for playback, queue, favorites and truthful
   pending/error states, with an established real-browser acceptance path.
2. Headless preset/band editing and explicit DSP source-context selection. The
   separate bounded DSP CLI increment covers balance and active EQ bypass only;
   it preserves the existing active context rather than inventing a new one.
3. Richer explicit queue bookmarks: selected metadata and playback position/
   history beyond the current ordered URL-list format, with versioned validation
   and stopped-by-default restore semantics. Current URL playlists already
   support capture, import, editing and explicit restore.

### Decisions for the next phase

- Choose the first real OS/mpv/DAC target and foreground/service profile when
  hardware becomes available. Start with neutral foreground playback, then the
  explicit original-app compatibility profile.
- The selected one-hour increment is the smaller balance/active-EQ CLI. Any
  later preset editor or browser controller needs its own scope and acceptance.
- Approve one architecture boundary and acceptance budget at a time. Prefer
  shared pure contracts first; core/eventing movement needs its own scope.
- Prioritize richer bookmarks, broader controller compatibility or per-instance
  audio configuration after the first hardware session. Provider-account work
  and merge/release remain outside the current authorization.

Reuse the [45–60 minute hardware session](TESTING.md#deferred-xu4--dac--original-app-acceptance):
installation/identity, selected DAC at low volume, separate-LAN-client two-source
playback with HTTP/SOAP/GENA agreement, original app, then owned restart/cleanup.
Record exact commit/versions and PASS/FAIL/NOT_RUN. Stop at a failed prerequisite;
DAC unplug, full service boot and two-host synchronization are later cases.
The four-hour radio result remains attached to its older frozen source and is
not final-head endurance or hardware acceptance.
