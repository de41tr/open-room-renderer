# Resume development

1. Check current user instructions and UTC deadline: stop at 2026-10-12 20:00;
   stabilization only from 19:00. Do not assume unattended work occurred.
2. Check `git status`, branch and remote head. Work only on
   `dot/renderer-autonomous-20261012`. Never merge or release.
3. Read STATUS.md and PLAN.md, then run `.venv/bin/python -m pytest -q`.
4. AP05 concrete multi-item queue/navigation and optional real libmpv/null lane
   are tested (372 passing when opted in). Next: AP03 identity/SCPDs/capabilities,
   AP04 GENA, then canonical header-only provider resolution AP06.
5. AP01 is published in draft PR #1 as `2e3068ede2b095b572f7249bc7bdaadecdd9641a`.
   The exact-state PyWiiM checkpoint is published as `a079284e14b1d5f86286b97ff0e5237ca5014bde`.
   AP02 is published as `4a3849e391111e2fb45b95dc7ee6f6aaf30423fb`.
   AP05 is the next publication checkpoint.
6. Keep production mpv JSON IPC. AF_UNIX is denied in this cloud sandbox; use
   fake-backend tests and optional libmpv/null-output validation with honest scope.
7. Review all changed files and commit content for private data before publishing;
   verify remote commit and draft PR after publishing. Maintain these notes.
