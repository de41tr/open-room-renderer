# Resume development

1. Recheck latest user instructions and UTC time. Stabilization only from
   2026-10-12 19:00 UTC; stop active work at 20:00 UTC. Do not infer unattended work.
2. Work only on dot/renderer-autonomous-20261012. Never edit main, merge or release.
   Draft PR: https://github.com/de41tr/open-room-renderer/pull/1
3. Check local/remote head and CI, then read STATUS.md, PLAN.md and docs/TESTING.md.
   Completed checkpoint 4ef68e4 passed native x86_64/AArch64 plus Python3.11:
   https://github.com/de41tr/open-room-renderer/actions/runs/38203316362
   Its one-time extended production Unix-IPC stress passed 905/906 seconds across
   three owned mpv lifetimes per architecture. All children/listeners/tasks were
   cleaned up. Direct station cache readback and finite default reset passed.
4. Normal CI duration is restored: 180-second stress, six-minute stress step,
   fifteen-minute native jobs; the separate Python3.11 job remains eight minutes.
5. New fixes close mDNS sockets even after unregister failure/cancellation and
   treat Linux SIGHUP as graceful stop, not reload. The real-resource loopback
   discovery checks pass locally; the added actual-mpv SIGHUP cycle awaits CI.
6. Hold the prepared playlist-child cache extension for now. A paced ICY probe
   suggests the existing zero-history direct-station policy can suppress native
   title changes. Narrow this metadata/cache interaction and preserve both
   metadata and bounded history before extending the policy. M3U/PLS children
   currently retain native defaults; direct endurance is not playlist evidence.
7. The original frozen b011557 hour attempt FAILED the unchanged RSS budget
   before an hour. Paired diagnostics attribute growth to native backward-cache
   retention, not accumulating Python objects. The separately frozen direct-policy
   hour began 04:03:22 UTC and is due no earlier than05:03:23; await its actual
   result, unchanged gates, cleanup and hashes. It uses real libmpv/null, not
   production Unix IPC, and predates the new playlist-child policy.
8. Continue concrete stability work: verify owned-player death propagation,
   evaluate changing ICY metadata without overwriting canonical station metadata,
   and publish the reproducible hour harness/evidence after review and completion.
   Do not treat green tests or this checkpoint as the end of the assignment.
9. No vendor cloud auth/registration, OEM keys, DRM bypass, firmware execution or
   proprietary publication. No binaries, APKs, original IDs, credentials or raw
   captures. Inspect every publication and verify its remote SHA. Keep production
   Unix IPC despite the local socket restriction. Physical LAN, original Android,
   DAC/acoustic audio, ARMv7/Pi/XU4 and full systemd boot remain NOT_RUN.
10. At the deadline stop owned active processes and provide the honest handover.
