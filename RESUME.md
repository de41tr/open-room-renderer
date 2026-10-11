# Resume development

1. Check current user instructions and UTC deadline: stop at 2026-10-12 20:00;
   stabilization only from 19:00. Do not assume unattended work occurred.
2. Check `git status`, branch and remote head. Work only on
   `dot/renderer-autonomous-20261012`. Never merge or release.
3. Read STATUS.md and PLAN.md, then run `.venv/bin/python -m pytest -q`.
4. AP12 DSP transactionality and measured filters are implemented and reviewed.
   AP09 pure queue metadata changes are ready but need HttpApi projection; keep
   queue.py/test_queue_metadata.py edits. Native IPC/ARM64 CI files are in progress.
   Active EQ restore and seek/native install acceptance remain open.
5. AP01 is published in draft PR #1 as `2e3068ede2b095b572f7249bc7bdaadecdd9641a`.
   The exact-state PyWiiM checkpoint is published as `a079284e14b1d5f86286b97ff0e5237ca5014bde`.
   AP02 is published as `4a3849e391111e2fb45b95dc7ee6f6aaf30423fb`.
   AP05 is published as `2d6e6ad29f998ab58f57ab38c4d19c93a5662b90`.
   AP03 is published as `99d771d0142934f63d534493e7e7c93b68f664ca`.
   AP04/security is published as `792d79c773273255b2eb25307cc814d2a96344ff`.
   AP06 is published as `bba255dc81df0257662ac60537f41fc7e38e041f`.
   AP07 is published as `aa3d455d5c80195a2f3e3ccce351b8b8a7702a9c`.
   AP08 is published as `a6b2b2594403bc92709a874fcf00b8848375cab7`.
   AP11 is published as `664483c0c2ddb22641f9bd25d42f33d5a11b1f74`.
   AP12 is the next publication checkpoint; metadata/CI edits remain separate.
6. Keep production mpv JSON IPC. AF_UNIX is denied in this cloud sandbox; use
   fake-backend tests and optional libmpv/null-output validation with honest scope.
7. Review all changed files and commit content for private data before publishing;
   verify remote commit and draft PR after publishing. Maintain these notes.
