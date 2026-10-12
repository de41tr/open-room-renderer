# Resume development

1. Recheck latest user instructions and UTC time. Stabilization only from
   2026-10-12 19:00 UTC; stop active work at 20:00 UTC. Do not infer unattended work.
2. Work only on dot/renderer-autonomous-20261012. Never edit main, merge or release.
   Draft PR: https://github.com/de41tr/open-room-renderer/pull/1
3. Check local/remote head and CI, then read STATUS.md, PLAN.md and docs/TESTING.md.
   Latest completed checkpoint b69a60a passed all three jobs, including the
   complete owned-player SIGKILL/error/fresh-backend explicit-restart tail:
   https://github.com/de41tr/open-room-renderer/actions/runs/38243274967
   Both architectures passed 63 libmpv-lane checks (50 native/13 unit) and six Unix cases. Normal
   limits stay 180s stress /6min step /15min native job /8min Python3.11 floor.
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
   installer floor passed 2368 default tests /62 skips and 274 Python3.11
   targeted tests.
   The new 2fc8296 hosted result passed all gates.
   The reviewed duplicate saved-gain rejection is locally verified: 2336 default
   passes /62 skips and 216 Python3.11 focused passes. It changes persistence
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
   392 Python3.11 focused checks passed. Verify the next hosted result. Keep this failure visible. Atomic rename and
   duplicate EQ-field rejection are separate reviewed changes. Ordinary-load
   terminal-error preservation has its own reviewed correction; root-ACK causal
   ownership now has a reviewed narrow correction. The original unrestricted
   parentless-root guard was rejected: merge-files creates owned EDL. The revised
   guard excludes EDL and retains owned cleanup. Final combined local suites
   passed 2759 /66 on Python3.11/3.12 and 60 selected libmpv cases on 0.37/0.40
   (47 actual native, 13 unit). Full standard CI must pass before extraction. Keep raw-writer/unpause/Stop
   race, missing-ACK/container/range/restart exclusions explicit. Atomic identity
   creation and coherent cleanup sampling are separately reviewed changes.
   Navigation/action/foreign-observation fixes are integrated and reviewed. Final
   Python3.12/3.13 default suites passed 2602 /65 skips; Python3.11 focused 435.
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
Final local default suites passed 2952/67 on Python3.11/3.12; full selected native
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
browser or a separate shell; this is not proof of a browser policy block. Do not
retry the policy-denied failed-tab cleanup. Assess useful CLI multi-track queue/
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
