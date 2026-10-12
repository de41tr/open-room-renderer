# Resume development

1. Recheck current user instructions and UTC time. Stabilization only from
   2026-10-12 19:00 UTC; stop active work at20:00 UTC. Do not infer unattended work.
2. Check git status and remote head. Work only on
   dot/renderer-autonomous-20261012; never edit main, merge or release.
   Draft PR: https://github.com/de41tr/open-room-renderer/pull/1
3. Read STATUS.md, PLAN.md and docs/TESTING.md. Last verified native checkpoint
   eb7c2ac9f3718ec80ef2efd05f1e4a4e0eeddcf6 passed all lanes on x86_64/AArch64:
   https://github.com/de41tr/open-room-renderer/actions/runs/38192653073
4. Actual same-host multicast discovery now leads through fetched endpoint, two
   distinct decoded sources and production Unix-IPC controls with HTTP/SOAP/GENA
   agreement. Failed/unready source loads now confirm same-owner Stop/idle.
5. Clocked calibration is valid, but ORR has measured transition gaps:
   equal-rate6.125ms/x86 and25.125ms/ARM64 versus0ms direct mpv playlist. Native
   EOF/sink timing and a minimal preload approach are under investigation;
   no gapless claim. Preserve strict calibration and sample checks.
6. Active independent work: owner-fenced sustained-cache-stall recovery (actual
   held-open source reproduced), frozen15-minute native stability with reviewed
   event/resource scope, and private Snapcast local digital/control experiments.
   The latter is not two-host synchronization or a supported ORR service.
7. Keep optional experiments inactive unless their actual advertised function
   works. No vendor cloud auth, registration, OEM-key use or DRM bypass.
8. Before publication inspect all files for secrets/proprietary content. No
   firmware, decompiled fragments, APKs, original identifiers, raw captures or
   binaries. Run final scoped and aggregate tests, publish small commits, fetch
   and verify remote SHA, then monitor that exact revision's CI.
9. Local sandbox Unix-socket creation is restricted. Keep production Unix IPC;
   use its native CI lane and clearly separate local libmpv/null evidence. Physical
   LAN, original Android, DAC/acoustic audio, ARMv7/Pi/XU4 and service reboot remain
   NOT_RUN. Do not stop simply because a checkpoint is green.
