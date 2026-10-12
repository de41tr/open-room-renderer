# Resume development

1. Recheck latest user instructions and UTC time. Stabilization only from
   2026-10-12 19:00 UTC; stop active work at 20:00 UTC. Do not infer unattended work.
2. Work only on dot/renderer-autonomous-20261012. Never edit main, merge or release.
   Draft PR: https://github.com/de41tr/open-room-renderer/pull/1
3. Check local/remote head and CI, then read STATUS.md, PLAN.md and docs/TESTING.md.
   Latest completed checkpoint 2fc8296 passed all three jobs, including the
   complete owned-player SIGKILL/error/fresh-backend explicit-restart tail:
   https://github.com/de41tr/open-room-renderer/actions/runs/38213572864
   Both native architectures passed 44 libmpv and all six Unix cases. Normal
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
   A separately frozen four-hour changing-title run is ACTIVE from
   06:06:23.924497 UTC; earliest qualifying finish is 10:06:23.924497 UTC plus
   teardown. Its source digest is the one above and production digest the one
   in step 7. No extended-duration pass is claimed before completion. Keep
   original failed/zero/final-constant results separately scoped.
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
   ownership remains the next bounded follow-up.
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
11. At the deadline stop owned active processes and provide the honest handover.
