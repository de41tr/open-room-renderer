# Resume development

1. Recheck latest user instructions and UTC time. Stabilization only from
   2026-10-12 19:00 UTC; stop active work at 20:00 UTC. Do not infer unattended work.
2. Work only on dot/renderer-autonomous-20261012. Never edit main, merge or release.
   Draft PR: https://github.com/de41tr/open-room-renderer/pull/1
3. Check local/remote head and current CI, then read STATUS.md, PLAN.md and
   docs/TESTING.md. Checkpoint 7c20ef8 passed every renderer lane on x86_64/AArch64:
   https://github.com/de41tr/open-room-renderer/actions/runs/38196685851
   Its separate direct-FIFO Snapcast experiment failed because PCM backpressure
   blocked production property reads. A reviewed private clocked-ingress repair
   is included next; verify actual native CI before claiming its success.
4. Default-off finite native staging measured equal-rate 0ms on both native
   architectures with exact frame counts/calibration. Mixed-rate remains 6–8ms;
   ambiguous nested/foreign ancestry reports uncertainty. No blanket gapless claim.
5. Public-station address renewal is reviewed with staging integration. Verify
   its explicit native lane. Monitor the new 180s production Unix-IPC stress lane,
   keeping controller/child resources and native events distinct. The qualified
   older 15-minute libmpv stress is not the newer code's acceptance evidence.
6. Current native CI run38198310911 for b011557 passed both architectures,
   including production Unix stress and the clocked Snapcast gate.
   The corrected frozen one-hour station run began 03:10 UTC against b011557;
   expect no result before about 04:10 UTC. The earlier 245s failure was precisely
   explained by integer truncation plus monitor cadence, not stale playback.
   Exact snapshot mapping and separate monitor freshness now replace that flawed
   comparison. No success is claimed until the full run and cleanup pass.
   The next reviewed package fences lost Stop/Pause acknowledgements and native
   output uncertainty; real pywiim Next/Previous and percent-encoded media URIs
   now work through the normal queue/backend path. CLI failures return nonzero
   without URL disclosure or following redirects. Monitor the new installed-CLI
   lifecycle and actual-pywiim Unix-IPC CI gates before claiming native success.
7. No vendor cloud auth/registration, OEM keys, DRM bypass, firmware execution or
   proprietary publication. No binaries, APKs, original IDs, credentials or raw
   captures. Inspect all files before each small tested publication and verify
   the pushed SHA. Experiments stay inactive unless their function is proven.
8. Local Unix-socket creation is restricted. Keep production Unix IPC and use
   native CI; label local libmpv/null evidence separately. Physical LAN, original
   Android, DAC/acoustic audio, ARMv7/Pi/XU4 and service reboot remain NOT_RUN.
9. Do not stop merely because a checkpoint is green. Continue through the user’s
   deadline, then stop owned active processes and deliver the final handover.
