# Resume development

1. Check current user instructions and UTC deadline: stop at 2026-10-12 20:00;
   stabilization only from 19:00. Do not assume unattended work occurred.
2. Check `git status`, branch and remote head. Work only on
   `dot/renderer-autonomous-20261012`. Never merge or release.
3. Read STATUS.md and PLAN.md, then run `.venv/bin/python -m pytest -q`.
4. AP02 authoritative phase/generation checkpoint is tested (259 passing).
   Next is AP05 multi-item queue integration and navigation.
5. AP01 is published in draft PR #1 as `2e3068ede2b095b572f7249bc7bdaadecdd9641a`.
   The exact-state PyWiiM checkpoint is published as `a079284e14b1d5f86286b97ff0e5237ca5014bde`.
   Local queue-model and optional libmpv test-lane files may be in progress; do not
   accidentally include unfinished work in the AP02 commit.
6. Keep production mpv JSON IPC. AF_UNIX is denied in this cloud sandbox; use
   fake-backend tests and optional libmpv/null-output validation with honest scope.
7. Review all changed files and commit content for private data before publishing;
   verify remote commit and draft PR after publishing. Maintain these notes.
