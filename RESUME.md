# Resume development

1. Check current user instructions and UTC deadline: stop at 2026-10-12 20:00;
   stabilization only from 19:00. Do not assume unattended work occurred.
2. Check `git status`, branch and remote head. Work only on
   `dot/renderer-autonomous-20261012`. Never merge or release.
3. Read STATUS.md and PLAN.md, then run `.venv/bin/python -m pytest -q`.
4. AP09 metadata projection and exact opaque DIDL round trips are implemented
   and independently reviewed (1458 passed with seven real libmpv cases).
   AP10 confirmed-seek work is implemented and independently reviewed, with
   isolated default/native-null validation. Active EQ restore, native install
   and playlist-source fixes are implemented; AP13 is independently reviewed
   and its rebased snapshot passed1692 with eight real libmpv cases.
   AP14 installation is independently reviewed and has an isolated1616-pass
   default snapshot; publication adds native installer CI acceptance. Native IPC/ARM64 CI
   passed at the preceding published checkpoint; monitor each new commit's CI.
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
   AP12 is published as `2bab4bc4a802b9fe8108aca8ad8566ede49eb7b4`.
   Native IPC CI is published as `52f5c4d0dffc3dcc900c2726703d0af7524aba37`.
   Run 38185537983 passed on x86_64 and AArch64, including real Unix IPC.
   AP09 is published as `93f51e4c310827b1ec1ad9da38b3d6eb9eda851c`; its
   native CI run 38187041169 also passed both architectures.
   Configured music-preset selection is published as
   `84d7caeda4f3c4fc823217305fc0a6529735b2a7`; both CI architectures passed.
   AP10 is published as `349a56ccafb707e9ee2052335cdaf439e1c27c31`, with
   native CI run38188276268 passed on both architectures.
   AP14 is published as `324469c1a8afb72b277735e268e59196edcc6b10`;
   native installation CI run38188925897 passed both architectures.
   AP13 is published as `4b2f1d6f112b9ed8be2aa97261db2344068139eb`;
   run38189242029 passed all lanes on both architectures. AP15 exact snapshot
   passed1714 with12 real libmpv cases and is published as
   `9f4a1d5e7ca4f9dd8c96c25fa2b1c722847d2cca`; native CI run38189809370
   passed both architectures. Log privacy is published as
   `de8b9b03d219e50734ff95295c307d34abf7f945`, with native CI run38190231373
   passed. Named load options are published as
   `622d5ca9c21c8561e6ab81a544568878d683497d`; native CI run38190695731
   passed both architectures. The reviewed clocked-transition lane is prepared
   for first native CI execution; do not claim a measured gap before reading it.
   Preserve pending log privacy, clocked-transition, named load-option, explicit
   discovery-interface and unready-load cleanup changes. Monitor each new CI.
6. Keep production mpv JSON IPC. AF_UNIX is denied in this cloud sandbox; use
   fake-backend tests and optional libmpv/null-output validation with honest scope.
7. Review all changed files and commit content for private data before publishing;
   verify remote commit and draft PR after publishing. Maintain these notes.

Next: monitor the clocked calibration repair CI to a terminal result; then
publish the independently reviewed failed-load cleanup and explicit-interface
discovery/playback acceptance. The previous clocked run failed calibration,
so no valid queue-gap measurement exists yet. Keep19:00 UTC stabilization and
20:00 UTC stop on2026-10-12.
