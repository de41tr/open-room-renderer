# Resume development

1. Recheck latest user instructions and UTC time. Stabilization only from
   2026-10-12 19:00 UTC; stop active work at 20:00 UTC. Do not infer unattended work.
2. Work only on dot/renderer-autonomous-20261012. Never edit main, merge or release.
   Draft PR: https://github.com/de41tr/open-room-renderer/pull/1
3. Check local/remote head and CI, then read STATUS.md, PLAN.md and docs/TESTING.md.
   Latest completed checkpoint 60d6b0a passed native x86_64/AArch64 plus Python3.11:
   https://github.com/de41tr/open-room-renderer/actions/runs/38201708621
   Genuine pywiim controls/encoded resources, installed CLI restart/signals,
   owned/external cleanup, public playlists and private Snapcast delivery pass.
4. Default-off finite staging measured equal-rate 0ms with exact frame counts;
   mixed-rate gaps and ambiguous nested/foreign ancestry remain explicit limits.
   Station address refresh and owner-fenced recovery are implemented. Uncertain
   Stop/Pause acknowledgements cannot auto-start another track.
5. The new per-file policy avoids backward history only for exact explicit live
   stations. Local native/reset/forward-buffer tests and a frozen 300s run pass.
   Verify the new mpv0.37 and production Unix readback/extended-stress CI result.
6. IMPORTANT: this is one extended 900-second native IPC checkpoint. After its
   terminal result, restore ORR_TEST_UNIX_STABILITY_SECONDS=180, stress step6min
   and native job15min (currently900/18/30). Keep the floor job8min unchanged.
   Do not cancel the first extended checkpoint with another push before result.
7. Original frozen b011557 hour attempt FAILED the unchanged RSS budget before
   an hour; do not call it successful or a Python leak. Paired diagnostics show
   native backward-cache retention. The separately reviewed policy hour began
   04:03:22 UTC and is due no earlier than05:03:23; await its actual result,
   unchanged budgets, cleanup and source hashes. It uses real libmpv/null, not
   production Unix IPC. Preserve both runs and their separate provenance.
8. Independent mDNS teardown work fixes a reproduced unregister-failure leak;
   review/integrate it after the extended checkpoint. Continue meaningful backlog
   and actual stability work, not cosmetic changes or test-count inflation.
9. No vendor cloud auth/registration, OEM keys, DRM bypass, firmware execution or
   proprietary publication. No binaries, APKs, original IDs, credentials or raw
   captures. Inspect every publication and verify its remote SHA. Keep production
   Unix IPC despite the local socket restriction. Physical LAN, original Android,
   DAC/acoustic audio, ARMv7/Pi/XU4 and full systemd boot remain NOT_RUN.
10. Do not stop merely because a checkpoint is green. At the deadline stop owned
    active processes and provide the final honest handover.
