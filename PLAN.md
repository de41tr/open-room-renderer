# Next-phase development plan — 13 October 2026

The authorized development window is **2026-10-12 22:12:31 UTC → 2026-10-13
18:00 UTC (20:00 Europe/Berlin)**. Work only on
`dot/renderer-next-phase-20261013` in Draft PR #2, based on main
`ad21bd4972d4af81dd29d1d952e38313dbe491d9`. No main push, merge or release is
authorized. No large/risky starts after **16:30 UTC**; stabilize from **17:00**.

## Current plan: hold scope and complete PR verification

The corrected 05:35/05:56 pipeline is delivered: genuine browser smoke, passive
Spotify diagnosis and revised ADR, owned-player output selection, config/output
checks, room name, installer retention and offline upgrade/Spotify preparation.
Private saved-curve and empty-library favorites transfer, observed saved-state
protection, headless DSP recovery and redirect privacy are also delivered.
Architecture remains the two cohesive seams with one playback/queue authority.
See [STATUS.md](STATUS.md) for usable features and [TESTING.md](docs/TESTING.md)
for exact evidence; the checkpoint history below is not a fresh work queue.

The evidence-led three-group default scheduling correction is now verified at
**`53f4831bd469735e13c1448a91abdeba838dc1cf`** in
[run 38394821007](https://github.com/de41tr/open-room-renderer/actions/runs/38394821007),
all four jobs/every step, **12:21 UTC**. Floor **7260/76**, group times
**149.62s /94.67s /89.85s** under **165s shared**; native defaults **7261/75** on
x86_64/ARM64. All old nodes/skips, native opt-in two-group commands and caps remain.
Retain failed `0ea93ec` floor and unresolved `70d9308` ARM Snapcast evidence.
Neither later success nor this documentation changes those failed outcomes.

1. **Keep this handover useful and source-pinned.** PLAN/STATUS/RESUME and
   TESTING/COMPATIBILITY/STRATEGIC separate delivered work from target acceptance
   and the next three user decisions. Preserve frozen STATUS/RESUME history
   verbatim. The runtime result above does not certify a handover-only revision;
   verify that revision through its own unchanged PR workflow. No further
   documentation commit is needed merely to refresh a timestamp or record a pass.
2. **Preserve acceptance boundaries.** Original Chrome control/focus/download
   smoke passes; new dialogs and wider browser scope lack dedicated acceptance.
   Native/null/PCM/installed proof does not substitute for physical ARMv7/DAC,
   effective user-service/boot, physical LAN or original-app acceptance.
   Spotify remains passive/offline, with no account/client activation or audio.
3. **Use new evidence, not an empty clock, to reopen work.** Whole-product and
   metadata-only feasibility reviews found no further independent GO feature.
   Richer bookmarks/metadata need cross-consumer/versioning/title-origin and
   duration/seek policy; they are not deferred because hardware is missing.
   General favorites merge/overwrite, whole-library DSP restore, universal player
   frameworks and extra timing/soak/browser campaigns remain out of scope.
4. **Hand decisions back clearly.** The user next decides PR review/merge,
   authorized real ARMv7/DAC/effective-service acceptance, and whether to proceed
   with go-librespot under manual terms/account/exclusive-output gates. Direct
   Spotify ALSA would bypass ORR DSP. Do not activate services or providers as
   part of documentation preparation.
5. **Stabilize and stop on time.** Keep the existing CI/review checks exact for
   any authorized successor, diagnose actual failures without weakened gates or
   retries-until-green, and report unfinished work at **18:00 UTC**. Remain
   available within the window; do not create timestamp-only commits.

## Earlier phase plan and checkpoint history

1. **ARCHITECTURE:** cohesive DSP preset-library and pure protocol-contract
   extractions, behavior-neutral commits, unchanged state/lock/error contracts.
   Limit this phase to approximately 3–4 hours; then pivot to user functionality.
2. **WEB UI:** functional responsive controls on the existing aiohttp listener,
   canonical state, explicit pending/unknown/error outcomes, same-origin mutation
   protection, safe metadata and credential-bearing URL handling. Browser QA if
   genuinely available; otherwise implemented with BROWSER_ACCEPTANCE_PENDING.
3. **SPOTIFY FEASIBILITY:** after functional UI, at most one hour comparing
   go-librespot, librespot and official Soloist evidence. Optional separately
   installed sidecar only if justified; no account login or proprietary copies.
4. **HIGHEST-VALUE BACKLOG:** practical DSP/playlist/diagnostic/lifecycle work.
5. **STABILIZATION:** no new large/risky work after 16:30 UTC; stabilize from
   17:00 UTC. Stop at 18:00 UTC even if CI remains PENDING.

**Early pivot decision:** two cohesive extractions are implemented and reviewed;
finish integrated verification, publish them separately, and move to Web UI.
The architecture budget is a maximum, not a reason to add lower-value refactors.

**Web milestone:** the complete required controller plus balance/EQ bypass is
implemented and independently reviewed. Actual native Web control wiring passes;
real-browser acceptance remains pending. Publish this cohesive checkpoint after
final integrated checks, keep exact-head CI separate, and start the bounded
Spotify comparison without waiting idly on CI. Do not add provider controls that
pretend two independent audio engines share one playback/DSP authority.

**DSP/backlog checkpoint:** ten-band editing and saved-preset management now have
independent review, full local floor/default coverage and discriminating actual
native PCM evidence. Publish the cohesive milestone and its bounded same-runner
CI scheduling improvement. Spotify feasibility is complete and integration is
DEFERRED (ADR 001); prioritize stopped-only playlist loading/editing next, without
introducing another playback authority. Exact new-head CI and real browser QA
remain distinct gates.

**Practical workflow checkpoint:** stopped-only JSON/M3U/URL playlist loading and
canonical append/remove/move are integrated with independent review and short
actual native explicit-Play checks. Startup errors for explicitly selected config
files now fail privately before identity/backend/listener effects, preserving the
public loader and bare-serve fallback. Final combined default/floor verification
and exact-head CI passed at `f7c6ed7` (run 38325071508). The first 90-minute assessment is recorded
in docs/STRATEGIC_REVIEW.md; no speculative new audio engine or core rewrite.

**Operator follow-through:** bounded review found navigation-private-draft and
UNKNOWN-gain recovery gaps. Correct those using existing browser lifecycle and
checked volume commands, with narrow review/native wiring evidence. No extra
status panel or core rewrite is warranted. Retain genuine browser/hardware gates
and verify complete exact-head CI before describing a new checkpoint as green. Recovery head `0668209` now passes every hosted step.
One final evidence-led same-process selector balance and modal-use clarification
follow; no more timing changes without a failed gate. Next assess an explicit
private-file export workflow, retaining ordinary URL redaction and existing limits.
That bounded queue-only JSON workflow is now implemented with independent review,
complete local suites and short native no-mutation checks. Publish separate
admission/feature/native/docs commits, verify exact-head CI, then reassess value.
Favorites/M3U export and genuine browser download acceptance remain separate.

**01:12 value review:** two reproduced operator gaps justify bounded fixes:
private-safe known saved-state startup errors and logical dialog focus return.
Both have independent review and complete local coverage; verify exact-head
hosted CI, without expanding persistence, playback or browser claims. Export
head `1dfd642` is already fully green in run 38330676949.

**Favorite workflow follow-through:** expose existing stable-ID URL repair with
explicit metadata policy and honest ACK-only target status, plus observed-conflict
protection. No new server contract or playback authority. Independent review,
focused checks and short actual native no-mutation evidence precede publication.
Developer maps are updated to remove obsolete next-extraction/UI-priority claims.

**Bounded diagnostics:** an unavailable saved-favorites read now has actionable,
private-safe service-restart guidance without new routes or storage policies.
Verify its independent checkpoint. Current status/resume instructions are
consolidated without altering historical evidence; assess any further storage
concern read-only before choosing a policy change.

**02:42 preservation review:** the failed-load preset overwrite is reproduced
and a narrowly fenced, independently reviewed correction is integrated. Normal
valid-storage responses and active DSP remain; degraded legacy write failures
change explicitly. Complete local and short actual native preservation checks
pass. Verify exact-head CI before moving to another concrete operator defect;
no automatic repair, broad storage concurrency or speculative feature expansion.
Preservation head `3e44ad0` is fully green in run 38339941041. A remaining
reproduced initial-balance traceback justifies a ten-line private startup-error
correction, with independently reviewed focused checks and unchanged precedence,
public exceptions, saved bytes and teardown. It is post-acquisition, not early
validation. Verify its separate exact-head CI; no extra native/full campaign.
The exact initial-balance floor run then hit its existing 165s watchdog. A single
measured module transfer is permitted only to correct this observed failure,
with collection parity and unchanged caps. An independently reviewed frontend
Save current draft guard also fixes a reproduced Refresh/reject loop without
rebasing captured intent. Publish separate commits and verify combined CI.
**04:12 closure:** `ba7c759` passes all three hosted jobs/every step in run
38343514991. The floor correction passes 142.33s/138.29s under the unchanged
165s cap; scheduling work is closed. Independent value review recommends
holding feature expansion for the next 90–120 minutes: the requested workflows
are implemented and genuine browser acceptance is the largest remaining gap.
Continue with actionable new evidence or changed acceptance prerequisites rather
than another speculative feature or test campaign. Next review: 05:42–06:12 UTC.

Review usefulness, risk and marginal return every 90–120 minutes. Focused tests
and independent review precede milestone publication. Full native CI follows
cohesive milestones; unchanged older endurance evidence is not rerun without
strong new information. Physical ARMv7/XU4/DAC and original-app checks remain
HARDWARE_PENDING. No test budget or assertion weakening.

---

## Historical completed work

# Bounded GENA architecture extraction

Actual start: **2026-10-12 21:05:18 UTC (23:05:18 Berlin)**.
Hard stop: **2026-10-13 01:05:18 UTC (03:05:18 Berlin)**, without extension.
Midpoint fallback deadline: 23:05:18 UTC. A safe implementation was already
independently reviewed and passing by 21:10 UTC.
Final validation/publication/handover was reserved from **00:35:18 UTC** at the
latest and began early. This is a maximum budget, not an instruction to fill it.

Baseline: clean `1478864ac3351cd253dca32c56726723b3605f60`; all three jobs
passed in https://github.com/de41tr/open-room-renderer/actions/runs/38306668149 .
The preceding DSP CLI task was complete before this separate authorization.
Branch: `dot/renderer-autonomous-20261012`, open Draft PR #1. Main remains
`3672b9838e9c39ae8191613108f0d91a5cada97d`; no merge or release.

## Verified architecture checkpoint

Production extraction: `9acb44bc83319c053d9995e6c9b7f56e10677a7a`.
Independent boundary tests: `aafa6649d5141123cfa644a7c30150f99fba9325`.
[Exact application/test CI 38309188954](https://github.com/de41tr/open-room-renderer/actions/runs/38309188954)
passed **all three jobs**, verified 2026-10-12 **21:27 UTC**.
Python 3.11 and native x86_64/AArch64 retain every default case, native lane,
assertion, skip and time cap. Hosted defaults pass **4588 /73 opt-in skips**;
Python 3.11 groups pass 4338+250 in 150.23s/88.54s. Both native architectures
pass all 66 libmpv checks and every production Unix/client/stress/install/
clocked/discovery/Snapcast stage. No workflow or existing test was modified.

`EventSubscriptionManager` now owns subscription admission/renewal, SID/leases,
limits, SEQ, dirty coalescing, retry/delivery tasks, expiry and cancellation/drain.
Its sole renderer dependency is a synchronous service-to-XML snapshot provider.
HttpApi remains the only playback/queue/DSP authority and owns payload projection,
trigger points and overall listener/shutdown order. No new lock, task, backend
command or scheduling boundary was introduced. The added close coroutine wraps
the same conditional gather; empty-close and final-listener tests verify it.

All 4583 unchanged defaults separately pass locally on Python 3.11/3.12;
five new manager/compatibility tests pass both minors. A real-loopback comparison
with the exact baseline yields **113 identical ordered observations**, including
byte-identical payloads and normalized protocol headers. Local full libmpv 0.37
passes 23+43 cases; focused 0.40 integration/DMR/DSP/invalid-media passes four.
These results preserve the distinction between mock, real-native ctypes/null/PCM,
hosted production Unix IPC and unavailable physical/app acceptance.

- [Actual boundary, dependency map, tradeoffs and next seam](docs/architecture.md#gena-lifecycle-boundary)
- [Reproduction and evidence scope](docs/TESTING.md#gena-ownership-extraction-verification)

This documentation-only handover leaves application/test/workflow bytes identical
to the green `aafa664` checkpoint. Verify the documentation head's own full CI
in [Draft PR #1](https://github.com/de41tr/open-room-renderer/pull/1) before final
completion; the earlier green run must not be substituted for its check status.
No further extraction is scheduled. Once that verification completes, stop
active work. This handover is not continuing authorization for another task.

**HARDWARE_ACCEPTANCE_PENDING:** XU4/ARMv7, physical DAC/ALSA/audio, original
WiiM Home app and two-host acceptance remain outstanding, not blockers here.

## Scope and stop boundary

The chosen GENA seam is implemented; no fallback migration or broader player-core
rewrite was necessary. Final steps are documentation publication, exact-head CI
verification and clean branch/process handover. Do not add unrelated features or
extra test campaigns to consume the remaining budget. If the hard stop arrives
first, report any unfinished check as pending and end active work without extension.

The sections below are historical completed work; their earlier deadlines
do not govern this separate task.

---

# Development plan

Branch: `dot/renderer-autonomous-20261012`; base: `3672b9838e9c39ae8191613108f0d91a5cada97d`.
The previous work window completed at 2026-10-12 20:00 UTC.
A separate explicit DSP CLI task runs 20:35:04–21:35:04 UTC, with its last
15 minutes reserved for validation/publication/handover. No automatic continuation.
Scope: client-only balance get/set and EQ on/off over existing HTTP contracts,
focused unit/loopback/native checks and operator docs. No server/backend expansion.
No main-branch edits, merge, or release.

## Bounded DSP CLI delivery

The narrow implementation and independent review are complete. Focused local
client/HTTP and actual libmpv 0.37/0.40 PCM checks pass. Full default suites
pass on Python 3.11/3.12; exact-head hosted CI must be verified before a final
success report. Publish client/tests,
native evidence and docs on the existing branch; leave the PR draft. No later
feature or architecture work is part of this hour. If CI is unfinished at the
hard stop, report it as pending and end active work without extending the task.

## Previous completed stabilization plan

Application/test checkpoint `1d05480` has full standard CI green, including the
reviewed malformed-format failure/explicit-recovery case on both native
architectures. Runtime source remains unchanged from a7e8d8. The original 20-AP
scope map, operator commands and required next-phase assessment are published.
No further justified low-risk runtime correction emerged from that bounded review.

During formal stabilization from 19:00 UTC, verify this documentation-only handover,
its complete CI, branch/PR agreement and clean checkout. Do not begin new features
or artificial test campaigns. Preserve prior failures and frozen-source endurance
scope. Main changes, merge, release, provider accounts and unavailable hardware
acceptance remain outside this task. Stop active work at 20:00 UTC and leave no
owned development test running. After 20:00 UTC, do not continue automatically;
wait for a new explicit task. This plan is a handover, not a recurring instruction.

## Ordered work

1. AP01: explicit SOAP contracts, faults, and acknowledged backend commands.
2. AP02: authoritative playback phases and generation-scoped observations.
3. AP05: real multi-item queue, one-based selection, browse and transport navigation.
4. AP03: consistent discovery, persistent independent identity and honest capabilities.
5. AP04: renewable, bounded, safe GENA subscriptions and queue events.
6. AP06: header-only public-radio selection through a bounded anonymous public-station resolver.
7. AP07: backend/process failures and owned shutdown.
8. AP08: recovery that respects stop, pause, and newer playback generations.
9. P1 where time permits: metadata, confirmed seek, volume/mute, existing DSP,
   persistent settings, ARM installation, radio formats and measured transitions.

P2 provider integrations remain disabled. Bounded Snapcast digital/control
experiments are separate from two-host synchronization or an advertised service.
No proprietary
material, credentials, original identifiers, captures or binaries belong here.
Keep the production Unix-socket mpv design; use deterministic test doubles where
this execution environment cannot create Unix-domain sockets.

## Checkpoints reached

AP01, AP02, AP05, AP03, AP04, AP06, AP07 and AP08 have implementation checkpoints,
including real libmpv/null control, two-track/callback and interrupted-source
recovery flows. AP11 now confirms and persists actual digital gain. AP12 now validates and transactionally confirms existing DSP, with measured
PCM effects. AP09 preserves canonical and opaque metadata across client surfaces.
AP10 confirms actual seeks, including pause/resume and stale-command rejection.
AP14 provides reviewed native installation and stable unprivileged profiles.
AP13 restores complete confirmed active DSP, including deferred startup.
AP15 has first-level native playlist ownership and local radio-format evidence.
Log privacy, calibrated clocked transition measurement, version-independent load
options, explicit multicast discovery and same-owner unready-load cleanup are
implemented. Native discovery-to-playback passed on both CI architectures.
Sustained no-EOF station stalls now have reviewed owner-fenced bounded recovery.
Equal-rate native staging now measures 0ms on both CI architectures, with mixed-rate limits.
Public-station address refresh, uncertain-owner transport fences and exact
public-client resource parsing are implemented. Installed CLI persistence,
genuine pywiim controls and the Python3.11 floor pass CI. The extended native
Unix-IPC checkpoint passed over fifteen minutes on both architectures; routine
CI durations are restored.

Current checks: complete owned-player death/error/fresh-backend restart now
passes both native architectures, as do the finite-history and changing-metadata
policies. The separately frozen constant-title finite-policy hour passed with
complete cleanup. The bounded changing-ICY endurance mode passes four-minute
replay and negative sensitivity; its separate frozen four-hour run completed successfully including cleanup at
10:06:26.620761 UTC. Keep its frozen
provenance separate from later code changes. Rolling live-HLS cases pass locally
but the expanded hosted libmpv lanes failed at 3e74b24. Both fixture assumptions
(segment503 native EOF and path-before-decoder readiness) are reproduced and
corrected; integrated and hosted 0.37/0.40 checks pass at 2fc8296, including
the honest >=0.37 installation floor. Current concrete gaps: stopped/paused
queue navigation and the shared canonical SOAP/GENA action projection are now
reviewed and integrated together with foreign-source observation and automatic
admission fences. Verify the next hosted checkpoint; preserve explicit Play
and terminal errors. Separately investigate causal ownership during an already
admitted load/readiness window without weakening existing source checks. A confirmed
ambiguous persisted-gain parsing gap now has a reviewed fail-closed fix with
full local default-suite and Python3.11 targeted verification. Limited
ARMv7 emulated ABI/API evidence does not fill native installation/hardware gaps.
Nested ancestry, original-app/hardware audio and two-host sync remain explicit
limits. Do not erase the earlier unexplained Snapcast failure with later passes.

The 5d64bfb native nested-load cleanup regression is repaired and the complete
b18b43e standard CI is green. The current final checkpoint adds narrowly proven
observed root replacement, atomic first identity publication, and coherent
cleanup sampling inside the existing deadline. Complete its native/hosted gates
before the requested extraction. Atomic rename and duplicate EQ JSON
have independent concrete failure proofs. The older readiness/cleanup terminal
ERROR relabeling has a separate reviewed correction. Next evaluate narrowly
provable root-ACK ownership excludes EDL/container/restart cases and raw-writer
race windows; the original unrestricted parentless-root candidate was rejected.

## Requested bounded behavior-neutral extraction

User update 2026-10-12 08:35UTC: finish the already-running functional fixes and
long tests without interruption, and require their complete standard CI to be
green before starting any extraction. Then select one small, clearest HttpApi
responsibility, preferably pure DIDL/XML/metadata formatting. Keep public HTTP,
SOAP and GENA bytes/fields, errors, state, timing and interfaces unchanged.

Target 90 minutes, hard maximum 120 minutes including native CI verification.
Record the actual UTC start, target finish and hard stop here and in RESUME.md
when the green gate opens; the timer is now COMPLETE. Use a separate refactor
commit, existing regression coverage and only necessary extraction tests.
No new dependencies/frameworks, large rewrite, or fundamental playback/recovery/
queue-staging logic changes. If risky or unable to obtain native-green within
the budget, abandon the extraction and retain a concrete next-iteration plan.
Include a short target architecture covering protocol adapters, player core,
queue, eventing, DSP and service adapters. Stabilization/deadline take priority.

Refactor green gate confirmed: 07c72c5, all three standard CI jobs passed in
https://github.com/de41tr/open-room-renderer/actions/runs/38226980559 .
Actual start: 2026-10-12 09:14:53 UTC. Target finish: 10:44:53 UTC.
Hard stop: 11:14:53 UTC, including complete native CI verification.
Status: COMPLETED at 09:51:33 UTC, elapsed 36m40s. Revision 8291763 passed all
three standard CI jobs in run 38230830981. Extracted only the three stateless
metadata-edit helpers into
metadata_formatting.py, retaining exact-signature HttpApi wrappers. DIDL creation
and dynamic attribute escaping remain in place. No protocol dispatch, state or
playback logic moves. Abort/retain a concrete plan if unsafe or not native-green
by the hard stop.

Strategic update 2026-10-12 09:37UTC: after the current refactor reaches full green
CI, prioritize useful local features (repeat/shuffle/playlist navigation/presets)
over additional edge-case or long-run expansion. Keep at most five proposals in
docs/STRATEGIC_REVIEW.md with A_AUTONOM/B_AWAITING_DECISION/C_APPROVAL_REQUIRED
and evidence/value/effort/risk decisions. Review about every 90–120 minutes and
after major packages. No new unnecessary multihour tests. XU4/original-app work
is HARDWARE_ACCEPTANCE_PENDING; do not wait for unavailable hardware. Prepare
the bounded later acceptance plan after higher-value software work. Final handover
adds three practical constraints, three debts, three missing features and user
decisions. Existing branch, publication, safety and deadline limits remain.


10:34 UTC functional progress: Repeat One / All passes independent review, actual
three-track native flows and the complete local default/native suites. Publish
it with the separately reviewed native-format fixture correction and verify full
standard CI. Continue isolated favorites/shuffle implementation meanwhile; do not
restart another radio soak or wait for hardware. Keep mode/source/owner semantics
explicit and one canonical queue authority.


10:53 UTC review: Repeat checkpoint c780c80 has full standard CI green. The
editable-favorites candidate passes real client/native workflows and full local
suites; publish next with its separate atomic-helper repair and a scheduling-only
CI change. Two explicit libmpv groups run on the same existing runner, retaining
all cases and later gates within the unchanged 15-minute job cap. Then publish
reviewed Shuffle after combined native/CI verification. Stable anonymous radio
favorite references are the next bounded investigation; do not store temporary
resolver URLs or account data. See the five-item strategic review.


11:28 UTC: favorites has full standard CI green. Publish reviewed Shuffle and the
small stable-radio save-current improvement as independent functional commits in
one fully tested checkpoint, avoiding duplicate hosted runs. Combined local native
and both Python-minor suites pass. Next bounded investigation: easier CLI queue
load/show/play and explicit saved-playlist restore through existing SOAP actions.
Defer the controller page until genuine browser verification is available; no
network exposure, aliases/tunnels, dependency installation or policy bypass.


13:38 UTC: stable-ID favorites and transport controls have full standard CI green
at d91140d. Complete reviewed offline M3U import through final integrated defaults
and its own publication/CI, keeping its small byte-reader extraction separate.
Then prioritize a bounded standard UPnP-client reproduction over another local
convenience: description/SCPD binding, typed SOAP and client-parsed LastChange are
not covered by the existing HTTP pywiim flow. No new dependency, major player-core
change, browser workaround or multihour run. Keep 19:00/20:00 UTC boundaries.
