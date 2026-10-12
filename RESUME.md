# Resume development

1. Recheck current user instructions and UTC time. Stabilization only from
   2026-10-12 19:00 UTC; stop active work at20:00 UTC. Do not infer unattended work.
2. Check git status and remote head. Work only on
   dot/renderer-autonomous-20261012; never edit main, merge or release.
   Draft PR: https://github.com/de41tr/open-room-renderer/pull/1
3. Read STATUS.md, PLAN.md and docs/TESTING.md. Last verified native checkpoint
   e62daf23b2cd2faf462a9efd1d2bc38cd5dc8ef7 passed all lanes on x86_64/AArch64:
   https://github.com/de41tr/open-room-renderer/actions/runs/38194471807
4. Actual same-host multicast discovery now leads through fetched endpoint, two
   distinct decoded sources and production Unix-IPC controls with HTTP/SOAP/GENA
   agreement. Failed/unready source loads now confirm same-owner Stop/idle.
5. Clocked calibration is valid, but ORR has measured transition gaps:
   equal-rate6.125ms/x86 and25.125ms/ARM64 versus0ms direct mpv playlist. Native
   EOF/sink timing and a minimal preload approach are under investigation;
   no gapless claim. Preserve strict calibration and sample checks.
6. Active independent work: owner-fenced sustained-cache-stall recovery is implemented and independently
   reviewed; monitor its native CI. Continue frozen15-minute stability with reviewed
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

Native probe supervision and the optional Snapcast acceptance tool are reviewed.
Monitor the new private-package production-IPC Snapcast CI gate; standalone
control/PCM success alone is not ORR IPC or two-host synchronization evidence.

Default-off native successor staging is independently reviewed and locally
validated. Monitor the new phase-offset clocked equal-rate2ms gate before
claiming native transition improvement. Preserve ambiguous-ancestry uncertainty.
The new Snapcast IPC gate failed first Play in run38195598391 while all other
lanes passed; FIFO/control backpressure is being diagnosed without weakening
production readbacks/timeouts. Public-station refresh is separately reviewed
and being integrated with staging's typed resolver caller.
