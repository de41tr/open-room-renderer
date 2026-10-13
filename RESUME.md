# Resume the next-phase task

## Active authorization and checkpoint

The current window is **2026-10-12 22:12:31 UTC → 2026-10-13 18:00 UTC**
(13 October, 20:00 Europe/Berlin). Stop active work at the hard deadline and
report unfinished CI as PENDING. Start no large/risky new work after **16:30 UTC**;
stabilize from **17:00 UTC**. Older task deadlines below are historical only.

Work exclusively on `dot/renderer-next-phase-20261013` in
[Draft PR #2](https://github.com/de41tr/open-room-renderer/pull/2), based on main
`ad21bd4972d4af81dd29d1d952e38313dbe491d9`. Never push main, merge or release.
Latest fully CI-verified checkpoint **`322139c6ac74a8446e3e25a2d73204b6ecc185ac`**
passes **all four jobs/every step** in **run 38382292288**, verified **10:31 UTC**.
Floor groups **5948+708 /76 skips** pass **156.60s/143.82s** under 165s:
**6656 passed /76 skipped**; native defaults **6657/75** on both architectures.
Standard Chrome and all native/Unix, installed, discovery and Snapcast gates pass.
This includes private favorites transfer, saved-audio-state preservation and
rejected queue-edit recovery. Prior `70d9308` remains FAILED with unresolved
zero-run cause. The subsequent implicit-config and native-guide corrections
require their own exact-head CI. New workflow interactions still have no
dedicated browser or physical-audio acceptance claim.

## What is already delivered

The bounded architecture extractions, functional local Web controller,
ten-band DSP/preset editing, managed favorites including target repair,
stopped-only playlist loading/editing, private queue JSON export, startup
privacy diagnostics, logical dialog focus/cleanup, quiet-gain recovery and
native asset packaging are implemented and independently reviewed. Do not
restart the original architecture/Web/Spotify sequence. [STATUS.md](STATUS.md)
contains the compact checkpoint history; [TESTING.md](docs/TESTING.md) preserves
detailed evidence and limits.

For the latest favorite-repair delta, local checks pass **149 Node/DOM cases**,
**159 focused Web/frontend cases on each Python 3.12 and 3.11**, and **195 root
Web/frontend/installation cases**. Actual libmpv 0.37/0.40 keep/clear flows pass
in **4.32s/4.19s** with no native/media side effects and unchanged prior assertions
and caps. No full local replay was added; the earlier **5322/74** complete local
result belongs to `5534c4d`. Redacted favorite readback remains ACKNOWLEDGED,
not proof of exact URL; UNKNOWN and observed conflict/reopen rules stay explicit.

Saved-favorites read diagnostics are now fully green at the checkpoint above.
The following separately reviewed saved-EQ data-preservation correction passes
5399/74 complete local defaults on both interpreters, 164 Node checks and actual
mpv 0.37/0.40 startup preservation in 5.16s/4.85s. Its exact-head CI is now green;
hosted native logs confirm the same seven refusals and byte/active-state retention.
Observed failed loads fence library persistence; normal storage responses and
active DSP stay unchanged. Legacy degraded writes now fail, intentionally; this
is not an extraction-equivalence claim. No automatic repair/reset/migration or
external-file concurrency policy is added.

## Immediate priorities

The explicit **05:35/05:56 corrected mandate** supersedes the 04:12 wait decision.
Maintain three useful potential packages and work independently during CI.

1. **Keep the real-browser checkpoint exact.** The bounded smoke completed
   successfully at 06:29 on `66cbe158`, run 38356723243/job 115161233008. Verified
   artifact 11792544975 proves desktop/mobile, native dialog keyboard/focus, actual
   mpv/null control and downloaded JSON bytes. First two failed focus assumptions
   and the repaired favicon CSP defect stay recorded. No further harness growth
   is needed; later source gets the existing gate, with wider manual scope pending.
2. **Keep operator proof exact.** Passive Spotify diagnosis, explicit owned
   output selection, pure config checking and no-start output diagnosis are
   published and fully green at `4fd1607`. Native output/null proof passes on both
   architectures. No provider-account, shared-DSP or physical-output claim.
3. **Next ranked packages:** the favorites transfer and two recovery corrections
   are fully green at `322139c`. Verify the subsequent implicit-config leaf fence
   and prefix-qualified Spotify handover command through exact-head CI. Keep
   one-shot uncertainty and all existing gates; assess further work only from
   demonstrated startup/configuration or operator-handover gaps.
   Whole-library DSP transfer and general favorites merge/overwrite stay deferred.
   No format/cap expansion merely to create work.
4. **Honor the corrected Spotify choice.** [ADR001](docs/adr/001-spotify-sidecar.md)
   ranks go-librespot 76 / librespot 69 / Soloist 61. Build expiry is not key renewal.
   No actual account/client/provider operation occurred. A separately accepted
   exclusive whole-service mode is possible without shared DSP, but foreign mpv,
   uncertain shutdown, provider terms and physical output remain real boundaries.
5. **10:10 value decision: CONTINUE** the bounded preservation/recovery work;
   review again around 11:40–12:10 UTC, without waiting
   just for reporting, CI or unavailable hardware/accounts. No large/risky work
   after 16:30; stabilize 17:00; stop 18:00 UTC even if CI is pending. Remain on the
   existing branch/Draft PR #2, no main push, merge or release.

The historical handovers below are retained verbatim for evidence only. They do
not reopen completed tasks or authorize work beyond this phase's hard stop.

---

## Historical completed handovers

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

## Resume limits

Do not automatically continue from this file after completion or the hard stop.
A later task requires fresh user authorization and rechecking branch, actual
latest instructions and CI. Keep the single renderer state authority and existing
security/protocol/cancellation contracts. Preserve the EventSubscriber re-export,
live registry/tuning views and public async handlers. Private delivery internals
and their logger now live in eventing.py. No merge, release, vendor authentication
or proprietary material was authorized.

The sections below are historical completed work; their earlier deadlines
do not govern this separate task.

---

# Current bounded DSP CLI task

A new explicit user task began 2026-10-12 20:35:04 UTC, after the earlier
completed window. Its hard stop is **2026-10-12 21:35:04 UTC (23:35:04 Berlin)**.
The last 15 minutes, from 21:20:04 UTC, are reserved for tests, publication and
handover. Do not continue automatically after this new deadline.

Scope: a small local client-only DSP CLI using the existing HTTP contracts:
`dsp balance get`, `dsp balance set VALUE`, and `dsp eq on|off`.
No new server endpoints, audio backends, broad refactors, browser/provider work,
device authentication, firmware, merge or release. Keep the same branch and
Draft PR #1. Code, focused tests, docs, commits and branch publication are allowed.
Confirmed rejection and unknown mutation outcomes must remain distinct; no
automatic mutation retry or unknown-state success. Actual native checks now pass on libmpv 0.37/0.40, including nine real DSP CLI
requests and digital sample effects. Full Python 3.12 defaults pass 4583/73;
full Python 3.11 groups pass 4333+250/73. Exact published-head CI must still be
verified after publication. See STATUS.md
for exact scope. Do not extend the deadline to await CI.

Starting checkpoint: clean local/remote `02cb93d19e7e5a5b2c6c1f8773c884a1ba0c4952`,
all three jobs green in run 38294774900; main stays at
`3672b9838e9c39ae8191613108f0d91a5cada97d`. Reverified at 20:36 UTC.

The previous handover below is historical. Its earlier work authority ended;
this narrow new task does not reopen that broader plan.

# Previous completed handover and resume boundaries

1. This autonomous work window ends 2026-10-12 20:00 UTC. Formal stabilization
   starts at 19:00 UTC. After 20:00 UTC, do not resume or continue automatically.
   Wait for a new explicit task; this file is a handover, not standing authority.
   Recheck the latest user instructions and UTC time before any later work.
2. Work only on dot/renderer-autonomous-20261012. Never edit main, merge or release.
   Draft PR: https://github.com/de41tr/open-room-renderer/pull/1
3. Check local/remote head and CI, then read STATUS.md, PLAN.md and docs/TESTING.md.
   Latest completed application/test checkpoint 1d05480 passed all three jobs,
   verified at 17:16 UTC: https://github.com/de41tr/open-room-renderer/actions/runs/38281381125
   The final handover commit changes documentation only; verify its own CI in PR #1.
   Python floor uses complete bounded groups on the same runner, preserving the
   3-minute step/8-minute job; both native jobs retain all existing gates.
   The prior 763f7f7 remains failed solely for its floor time budget.
4. Native evidence also verifies real pywiim, repeated-lifetime stress, installed
   CLI signals, discovery and all eleven same-host Snapcast stages. The earlier
   8d ARM Snapcast failure remains unexplained; later passes do not erase it or
   establish two-host synchronization. Signal thresholds remain unchanged.
5. Zero backward history suppressed native changing ICY titles. The corrected
   shared per-file policy is 1MiB plus demuxer-donate-buffer=no. Cap alone can
   borrow forward capacity and is insufficient. Local native title/DIDL/reset
   tests pass; 70s sampling proved all 12 title changes and a bounded plateau.
6. Proven first-level M3U/PLS station children now receive the same finite policy
   through native process/entry-guarded writes and complete guarded readback.
   Both formats preserve changing titles. Finite defaults restore; partial writes,
   newer owners, replacement entries/processes are fenced. Nested/ambiguous
   ancestry stays excluded. Check the new mpv0.37/Unix gates separately.
7. The original b011557 hour FAILED its unchanged RSS gate. The separately
   frozen zero-policy run then passed a full 3600.669s baseline and cleanup, with
   one load/request and 1128 KiB peak RSS growth. It predates the ICY correction;
   do not credit the finite policy with that duration. Its production hash is
   f1cb53c37437134ec76c8eac37fed47c20e6a419f4cc0cd19b081a5f0882ee61.
   The separate finite-policy hour PASSED 3600.675s plus full cleanup at
   06:05:35 UTC: one load/request, 362 protocol agreements, 1936 KiB peak RSS
   growth. All 26 frozen file hashes stayed unchanged. Production digest is
   b3ae8a5c7ad01696b12be6feb52c16fe51fed9ad6128015992b6d14fd055b5ef; source digest
   ff5f255e08cc54255cff69621c7633da1c604109208d07b389201fbceb1a2735.
8. The extended changing-ICY endurance mode is now reproducible and opt-in only.
   Its exact 240.821s replay passed 30/30 autonomous title/callback and metadata
   agreements, one source/load/request, retained-history plateau and cleanup.
   Frozen source b8c7c3828554d0d0bdd0cd04258d01537956d38a2a191b41d43879603f3fc2b4.
   The separately frozen four-hour changing-title run PASSED at 10:06:26.620761 UTC
   including cleanup: 14,402.516s baseline, 1,795/1,795 autonomous title/surface
   agreements, one load/source/request, zero disconnects and 4,428 KiB live RSS
   growth. All 27 source entries and both frozen digests matched. Resources returned
   to one task/seven FDs/one thread/zero children. No new multihour run is planned.
   Keep original failed/zero/final-constant and this result separately scoped.
   Checkpoint 3e74b24 adds five real rolling-HLS cases. CI38210542104 has
   failures in both expanded libmpv lanes: segment-return loses playback-time
   during its two-second hold. x86 also loses time immediately after child path
   observation in M3U/PLS tests; ARM passes those. Both fixture assumptions are
   reproduced: native segment503 EOF and path-before-decoder readiness. Reviewed
   tests-only corrections preserve acceptance. Integrated native replay passes:
   eleven affected cases on both 0.37/0.40. Final publication including the
   installer floor passed 2368 default tests /62 skips and 274 Python 3.11
   targeted tests.
   The new 2fc8296 hosted result passed all gates.
   The reviewed duplicate saved-gain rejection is locally verified: 2336 default
   passes /62 skips and 216 Python 3.11 focused passes. It changes persistence
   only; immutable long-run source/production hashes remain their original ones.
   The actual mpv 0.35.1 audit found missing playlist ancestry: M3U/PLS HTTP
   Play fails 502 under strict ownership. Upstream added it only in 0.37; the
   reviewed installer/docs floor is now >=0.37 and passed hosted checks; do
   not weaken source ownership to accept older players. Continue
   meaningful protocol/source stability work, not cosmetic test growth.
9. Published 5d64bfb failed the native nested-cleanup case on both architectures,
   despite 46 other native cases and all later runtime gates passing. The reviewed
   admitted-load boundary repair restores owned cleanup; all 47 local native
   cases passed on 0.37/0.40 before the separate terminal-error delta, followed by
   seven affected native passes per version afterward. Final 2682 /65 default and
   392 Python 3.11 focused checks passed. Verify the next hosted result. Keep this failure visible. Atomic rename and
   duplicate EQ-field rejection are separate reviewed changes. Ordinary-load
   terminal-error preservation has its own reviewed correction; root-ACK causal
   ownership now has a reviewed narrow correction. The original unrestricted
   parentless-root guard was rejected: merge-files creates owned EDL. The revised
   guard excludes EDL and retains owned cleanup. Final combined local suites
   passed 2759 /66 on Python 3.11/3.12 and 60 selected libmpv cases on 0.37/0.40
   (47 actual native, 13 unit). Full standard CI must pass before extraction. Keep raw-writer/unpause/Stop
   race, missing-ACK/container/range/restart exclusions explicit. Atomic identity
   creation and coherent cleanup sampling are separately reviewed changes.
   Navigation/action/foreign-observation fixes are integrated and reviewed. Final
   Python3.12/3.13 default suites passed 2602 /65 skips; Python 3.11 focused 435.
   Native0.37 control/recovery/stall/ownership passed 17; 0.40 targeted cases passed 6.
   Check the new hosted result independently. Stop/Pause intent survives transient
   phases; empty Pause rejects without mutation; action lists remain shared.
   Foreign observations and automatic retry/EOF preflights now invalidate stale
   work without taking over observed unrelated audio. Terminal ERROR cannot be
   revived automatically. Partial/old observations remain fenced. The native
   ownership proof records scheduled events, not actual callback delivery.
   Already-admitted load/readiness replacement remains outside scope and needs
   separate causal evidence. The Python3.13 installer-fixture correction is
   included; real native installation still needs its missing ensurepip package.
10. No vendor cloud auth/registration, OEM keys, DRM bypass, firmware execution or
   proprietary publication. No binaries, APKs, original IDs, credentials or raw
   captures. Inspect every publication and verify its remote SHA. Keep production
   Unix IPC despite the local socket restriction. Physical LAN, original Android,
   DAC/acoustic audio, ARMv7/Pi/XU4 and full systemd boot remain NOT_RUN.
11. User-requested small behavior-neutral HttpApi extraction is gated on full
    green standard CI for the current functional fixes. Prefer pure DIDL/XML/
    metadata formatting; no playback/recovery/staging logic refactor or new deps.
    Timer COMPLETE: started 2026-10-12 09:14:53 UTC: target 10:44:53 UTC, hard 11:14:53 UTC
    including native verification.
    Reviewed extraction, full local suites and all three hosted jobs passed by
    09:51:33 UTC, elapsed 36m40s. Scope: three stateless helpers and exact-signature wrappers;
    DIDL creation/dynamic attribute escaping stay in HttpApi. Keep
    all protocol payload/error/state/timing interfaces unchanged, separate commit.
    The target architecture is recorded; no further extraction is active.
    The frozen radio test has now completed and cleaned up.
12. At the deadline stop owned active processes and provide the honest handover.

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


Current functional handoff (10:34 UTC): Repeat One / All is reviewed and integrated.
Final local default suites passed 2952/67 on Python 3.11/3.12; full selected native
lanes passed 61 on 0.37/0.40. Hosted CI passed all three jobs, verified at 10:51:21 UTC. Default
mode reports 4; explicit 0 now means Repeat All, 1 Repeat One. Keep the documented public
client dialect and original-app limit. Native staging remains normal-only with
confirmed Stop required before changing an armed mode. The pure policy is queue-owned;
no second mutable state.loop field remains. Next isolated packages are editable
ORR-owned favorites and bounded shuffle, each requiring independent verification.


Editable favorites is the next reviewed checkpoint: stable IDs/configured prefix,
ORR-owned API/CLI, honest atomic failure/corruption behavior, real client/native
recall and restart without autoplay. Final local 3155/68 default checks passed on
Python 3.11/3.12; 62 native checks passed serially and in bounded two-group runs on
libmpv 0.37/0.40. The same-runner CI scheduler preserves every existing case and
later gate: 360s native watchdog/7min step, unchanged 15min job and 180s stress.
Verify the new hosted result before claiming completion. Shuffle is reviewed
and privately integrated with favorites; publish separately after the prior CI
is green. Stable anonymous radio favorites are under read-only investigation.


11:28 UTC: favorites checkpoint 4ae0628 passed all three hosted jobs. The next
combined checkpoint contains separate Shuffle and stable-radio save-current commits.
Both are independently reviewed; final local 3235/69 default checks pass on both
Python minors, and full native groups 20+43 pass on 0.37/0.40. Verify their hosted
result after publication. No long runs are active or planned. A browser controller
is deferred because the isolated fixture could not be reached from the cloud
browser or a separate shell; this is not proof of a browser policy block. Assess useful CLI multi-track queue/
saved-playlist commands using existing protocol contracts instead of waiting.

11:46 UTC: b69a60a is published and all three hosted jobs passed, verified at
11:45:54 UTC. Shuffle and stable-radio save-current are complete software
checkpoints. Current private work: client-only queue show/load/play-index and
explicit authored playlist files (create/restore, default stopped). Keep complete
validation before networking, no retries after uncertain mutation, safe no-clobber
file creation and truthful post-commit warnings. BrowseQueue is not canonical URL
export. Integrate independent review and a short actual CLI/native flow before
publication; no playback-core changes or new multihour probes.

11:58 UTC: queue/client and authored-file patches are integrated and independently
reviewed. Final default suites pass 3652/70 on Python 3.11/3.12, and the exact short
native CLI flow passes on 0.37/0.40. Publish separate valid queue and authored-file
commits plus their integrated native test/CI entry and documentation, then verify
all standard CI. No local full native rerun is needed for unchanged renderer core.
Read-only preinstalled browser inventory is included in the existing CI environment
step; no browser install/start or extra runner. The cloud desktop terminal's text
input failed before any fixture, so actual browser QA remains unestablished.

12:10 UTC: 763f7f7 failed Python 3.11's unchanged 180s step cap despite all 3652
assertions passing in 181.55s. Keep that checkpoint failed. Native jobs are still
running; let them finish to retain complete evidence/browser inventory before
publishing the minimal scheduling correction. It reuses the unchanged two-group
runner with a 165s watchdog; exact node-ID union is 3723 = 3593 + 130, no duplicates
or missing/skipped-policy changes. Local 3.11 groups pass3523+130/70, longest 130.51s.
Independent review approves; no new dependency/runner or relaxed cap. A separate
private client-only next/previous/mute/seek/current-mode convenience patch is
under review and short native verification, not part of this scheduling repair.

12:15 UTC: both native jobs of 763f7f7 completed successfully, including 64 libmpv
checks and every later gate. The checkpoint stays FAILED solely for the Python 3.11
180s time budget. Publish the separately reviewed scheduling correction now.
Read-only CI inventory found preinstalled browsers, but no supported actual
browser QA route has been established; keep the controller UI deferred.

12:31 UTC: publish the reviewed local transport CLI package after the now-green
scheduling repair. Final default suites pass 3674/70; exact 16-command native
flow passes both 0.37/0.40 with the original 20s bound. No renderer core changes.
Stable-ID favorite recall is in a separate private candidate; finish independent
review and short native proof before integrating, and merge its CLI guard with
the new transport guard. Browser UI remains deferred because browser acceptance has not been established.
No extra multihour tests.

12:53 UTC: transport head 0b6defa is published and hash-verified, but PR head/CI
still lag at 3e2455f. Parent independently confirmed the mismatch. No blind repush,
close/reopen or invented CI pass. Continue naturally completed tested checkpoints.
Stable-ID favorites now pass final 3695/70 defaults, exact collection 3765 = 3622 + 143,
and short native compaction proof on both versions. Publish the small existing
preset-helper extraction separately from the new route/CLI, then native test and
unchanged-budget third CLI-file partition, then docs. Helper-only baseline passes
137 focused/native checks. Keep configured IDs explicitly positional within config.
A strict offline M3U import is being assessed only; browser UI remains deferred.

13:38 UTC: d91140d and all three standard jobs are green; the earlier PR/branch
lag is resolved without blind retry. Strict offline M3U import passed independent
review and is running final integrated defaults on both supported Python minors.
Keep the byte-reader extraction separate; no playback/core change or new native
sweep is needed for an offline converter. Next high-value investigation is actual
standard DmrDevice binding, SOAP controls and parsed GENA, which current HTTP
pywiim acceptance does not exercise. Reproduce before fixing; preserve vendor
compatibility where intended. Browser UI remains deferred. Stabilization begins
19:00 UTC; stop active work at 20:00 UTC today.

13:42 UTC: final M3U integration passes 3729/70 defaults on Python 3.12 (180.38s)
and 3586 +143/70 on Python 3.11 (140.84s/42.23s), all existing caps unchanged.
Isolated byte-reader extraction passes 202 existing tests. Publish extraction,
feature/tests and docs separately in one checkpoint, then verify hosted CI.
The standard-client diagnostic confirmed all three gaps on the unchanged baseline
in both profiles. A separate private protocol-adapter correction is underway;
do not fold unreviewed compatibility changes into this offline-file checkpoint.

13:58 UTC: final standard-DMR candidate is independently approved and passes
3777/72 defaults on 3.12 (187.30s), grouped 3.11 (145.54s/41.68s), and short actual
libmpv 0.37/0.40 workflows. Publish adapter/tests separately from native/shared-Unix
acceptance and docs, after the current M3U workflow finishes. The new Unix lane
remains locally NOT_RUN, requiring hosted evidence. Preserve four reproduced red
mismatches, alias schemas, duplicate-type limits and all existing lifecycle guards.
No core redesign; stabilization 19:00 UTC and active-work end 20:00 UTC remain.

13:58:28 UTC: M3U checkpoint 369feeb is fully green. Both native architectures
passed every stage in 838.805s/830.491s, floor groups 151.26s/51.19s. Publish the
already-reviewed DMR package now; keep its new hosted-IPC result pending until seen.

14:18 UTC: standard-DMR checkpoint 0d11eea has all three standard jobs green.
Both native architectures passed the new shared production-Unix client flow,
all 65 libmpv checks and every previous stage. Jobs took 796.797s/856.717s;
floor groups passed in 163.34s/56.96s, close to the unchanged 165s watchdog.
One-file scheduling rebalance is measured: existing favorites CLI cases cost
25.84s, and moving them preserves the exact collection/skip semantics. Verify
final combined grouping without changing caps or removing cases.

The assessed safe current-URL-playlist save is now in private implementation.
It uses an existing-listener read, no new bind/auth/CORS policy, coherent canonical
snapshot and private no-clobber file publication. Trusted public-radio provenance
must be canonicalized on every item; invalid/stale or unproven direct-station
semantics reject the entire export. No playback/resolver/backend-sync effect.
It is not a lossless backup; restore stays stopped and metadata/history are omitted.
Require separate helper-only evidence if reusing extracted favorite canonicalization,
independent review, focused actual-native CLI proof and complete final CI.
Stabilization 19:00 UTC and active-work end 20:00 UTC remain unchanged today.

14:34 UTC: queue-save candidate is reviewed and final integrated defaults pass
3910/72 on Python 3.12 (193.07s); floor groups 3665+245/72 (121.63s/72.84s).
Exact collection 3982=3737+245 preserves all IDs and skip metadata. Helper-only
132 existing/native checks and both final native queue-save flows pass. Publish
helper, feature, native acceptance, floor rebalance and docs as separate commits
in one checkpoint. Then require full standard CI. Next useful work is offline
saved-playlist editing, followed by stable-ID favorite target repair, each bounded
and independent of live queue ownership. Keep all private/hardware/deadline limits.

14:55 UTC: queue-save CI is fully green, including hosted floor groups
135.98s/97.64s. Offline append/remove/move is reviewed and final defaults pass
3973/72 on Python 3.12 (187.79s), and 3728+245/72 on 3.11 (120.26s/68.94s).
Both native versions pass the extended 24-command flow with unchanged bounds.
Publish feature, native and docs separately, then monitor full CI. Next private
package is stable-ID favorite target repair, followed by bounded SSDP-only
candidate discovery if its safety/acceptance contract remains small. A native
same-runner rebalance assessment is underway; do not raise caps or lose cases.
Keep no new integration/publication before the predecessor is green. Existing
19:00/20:00 UTC stabilization/stop times and all hardware/privacy limits remain.

15:33 UTC: favorite target-repair v2 is reviewed and final combined defaults pass
4026/72 on Python 3.12 (199.30s), grouped 3.11 (128.26s/74.23s). Exact native v2
probes pass 0.37/0.40 in 6.278s/6.274s; do not reuse earlier v1 source hashes.
Publish feature, native proof, workflow-only native rebalance and docs separately.
The rebalance preserves all 24 files/65 cases as 22+43, same two processes/caps;
its hosted timing remains pending. Next implement bounded SSDP-only discovery,
with no HTTP/config side effects or verified-identity claim. A narrow pre-existing
legacy CLI proxy/netrc behavior is being considered, not a restarted broad audit.
Keep the 19:00 UTC feature cutoff and 20:00 UTC active-work stop unchanged.

15:58 UTC: d9a94e2 is fully green, verified at 15:55 UTC, including the native
rebalance and target-repair flow. Native groups remain all 65 cases, now 22+43;
x86 timings 227.43s/242.88s and AArch64 226.38s/239.28s. Floor groups passed
3781+245/72 in 129.95s/77.52s. Discovery CLI is privately implemented with
independent review; the real subprocess already found both owned loopback
renderers without HTTP requests in the existing native fixture. Wait for exact
final patch/review and run integrated defaults before publication. A separate
legacy client host/proxy/netrc correction is in private implementation; keep its
valid request bytes, outputs and redirect policy. No speculative next feature;
reserve time for final assessment, clean full CI and 19:00/20:00 UTC deadlines.

16:05 UTC: discovery candidate is independently approved and final integrated
defaults pass 4183/72 on Python 3.12 (201.09s), grouped 3.11 (129.30s/77.21s).
Production digest e8e2442a692c9fb9d727b41d86c222aa0257e70044e217394e2e599d508f52a3.
Exact native console acceptance passed all five cases twice (10.19s/10.22s),
with real TTL-0 multicast and no HTTP requests at either owned endpoint. Publish
feature/native/docs separately and require full CI. Legacy isolation remains
private until this checkpoint is green. Keep all deadlines and scope limits.

16:22 UTC: discovery is fully green on both native architectures and Python 3.11.
Actual console candidates/TTL-0/no-HTTP/cleanup evidence is present in both hosted
logs. The approved final legacy CLI isolation patch is integrated byte-for-byte
with its tested private candidate: 4412/72 on 3.12 (200.46s), 4162+250/72 on 3.11
(128.65s/72.88s). Production digest:
6abaedb03144f6bd23545300a232c58fb7fc4a9be003e2d8061fbd90cd71d734.
Five concrete operator-doc fixes are reviewed. Publish functional and doc commits
separately, verify complete CI, then finalize handover/stabilization without
speculative extra features. No main/merge/release or changed deadline authority.

17:00 UTC: all 20 original acceptance packages have a reviewed scope map in the
compatibility matrix; final constraints/debts/missing features/decisions and
hardware plan are linked from README and strategic review. The one specific
malformed-media native gap now passes on exact final test source in 0.37/0.40
(15.52s each), without a production fix. It proves format error -17, HTTP502,
received GENA and SOAP read state, native idle/empty source, bounded no-retry,
explicit valid recovery and cleanup; missing physical device/Unix case remains
unclaimed. Final defaults pass 4412/73 on 3.12 (200.58s), 4162+250/73 on 3.11
(130.87s/74.49s). Runtime digest stays
6abaedb03144f6bd23545300a232c58fb7fc4a9be003e2d8061fbd90cd71d734.
Publish test/workflow and docs separately, verify full CI, then continue bounded
quality work until formal stabilization at 19:00 UTC and stop at 20:00 UTC.
No main-branch edits, merge or release.

## Final handover record

The final runtime/test source is 1d054809a836eb844fd67fa833c24cccba3dc505, with
full native x86_64/AArch64 and Python 3.11 CI green in run 38281381125. Runtime
files remain identical to a7e8d8; digest:
6abaedb03144f6bd23545300a232c58fb7fc4a9be003e2d8061fbd90cd71d734.
The malformed-format case passed on both hosted native architectures with
error -17, cross-surface non-playing state, explicit recovery and all cleanup;
all 66 selected libmpv checks and later gates passed. Keep its ctypes/null scope
distinct from production-Unix malformed-media or physical missing-device proof.

Use the current STATUS, compatibility overview and strategic next-phase assessment
for final results, commands, three-category limitations and decisions. Hardware/app
availability is unknown; do not wait indefinitely or repeatedly request access.
The older frozen four-hour source remains its own endurance result. No new
large feature or long-running test is planned in stabilization. At 20:00 UTC stop
owned active work and report the verified head, draft PR, exact CI and clean state.
After 20:00 UTC no automatic continuation is authorized. Main/merge/release remain
prohibited unless a later explicit instruction supplies that separate authority.
