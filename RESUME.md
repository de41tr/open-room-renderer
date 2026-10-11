# Resume development

1. Check current user instructions and UTC deadline: stop at 2026-10-12 20:00;
   stabilization only from 19:00. Do not assume unattended work occurred.
2. Check `git status`, branch and remote head. Work only on
   `dot/renderer-autonomous-20261012`. Never merge or release.
3. Read STATUS.md and PLAN.md, then run `.venv/bin/python -m pytest -q`.
4. AP01 code/fault tests are the first checkpoint. Next is AP02 authoritative
   playback phase/generation handling, then AP05 multi-item queue.
5. A separate exact-state PyWiiM acceptance assertion fix is being reviewed;
   include it in its own commit, not mixed into AP01.
6. Keep production mpv JSON IPC. AF_UNIX is denied in this cloud sandbox; use
   fake-backend tests and optional libmpv/null-output validation with honest scope.
7. Review all changed files and commit content for private data before publishing;
   verify remote commit and draft PR after publishing. Maintain these notes.
