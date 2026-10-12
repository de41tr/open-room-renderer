# Resume development

1. Recheck latest user instructions and UTC time. Stabilization only from
   2026-10-12 19:00 UTC; stop active work at 20:00 UTC. Do not infer unattended work.
2. Work only on dot/renderer-autonomous-20261012. Never edit main, merge or release.
   Draft PR: https://github.com/de41tr/open-room-renderer/pull/1
3. Check local/remote head and CI, then read STATUS.md, PLAN.md and docs/TESTING.md.
   Latest completed checkpoint 62b089ad passed all three jobs, including the
   complete owned-player SIGKILL/error/fresh-backend explicit-restart tail:
   https://github.com/de41tr/open-room-renderer/actions/runs/38208267462
   Both native architectures passed 39 libmpv and all six Unix cases. Normal
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
   A separate finite-policy hour began 05:05:34.826684 UTC and is due around
   06:05:35 UTC; await its actual result and cleanup. Production digest is
   b3ae8a5c7ad01696b12be6feb52c16fe51fed9ad6128015992b6d14fd055b5ef; source digest
   ff5f255e08cc54255cff69621c7633da1c604109208d07b389201fbceb1a2735.
8. The extended changing-ICY endurance mode is now reproducible and opt-in only.
   Its exact 240.821s replay passed 30/30 autonomous title/callback and metadata
   agreements, one source/load/request, retained-history plateau and cleanup.
   Frozen source b8c7c3828554d0d0bdd0cd04258d01537956d38a2a191b41d43879603f3fc2b4.
   After current one-hour completion/cleanup, plan a separately frozen four-hour
   changing-title run; confirm exact hashes and actual start before claiming it
   active. Keep original failed/zero/current-constant results separately scoped.
   Continue meaningful protocol/source stability work, not cosmetic test growth.
9. No vendor cloud auth/registration, OEM keys, DRM bypass, firmware execution or
   proprietary publication. No binaries, APKs, original IDs, credentials or raw
   captures. Inspect every publication and verify its remote SHA. Keep production
   Unix IPC despite the local socket restriction. Physical LAN, original Android,
   DAC/acoustic audio, ARMv7/Pi/XU4 and full systemd boot remain NOT_RUN.
10. At the deadline stop owned active processes and provide the honest handover.
