# Next-phase status — 13 October 2026

## Current checkpoint

**IN PROGRESS:** the authorized window is 2026-10-12 22:12:31 UTC through
**2026-10-13 18:00 UTC (20:00 Europe/Berlin)**. Work remains on
`dot/renderer-next-phase-20261013` in [Draft PR #2](https://github.com/de41tr/open-room-renderer/pull/2),
based on main `ad21bd4972d4af81dd29d1d952e38313dbe491d9`. PR #1 was already
squash-merged. No main push, merge or release is authorized for this phase.

Latest CI-verified checkpoint **`660cab19a908090660a751e7dbbd7681e3b56637`** passes all three
jobs and every step in **run 38337365883**, verified **02:47 UTC**. Hosted floor
groups pass **4807 + 550 /74 opt-in skips** in **153.73s/131.05s** under the
unchanged 165-second watchdog: **5357 passed /74 skipped** in total. Native,
installed-lifecycle, production Unix and later acceptance gates are successful.
This evidence applies only to that exact source; later edits need their own CI.

## Implemented scope

- **Architecture:** `EqPresetLibrary` owns saved-curve validation/storage and
  `queue_protocol` owns pure authored queue/request contracts. Playback and
  active DSP retain their existing authority, lock order and error semantics.
  Differential observations match the baseline (51 DSP, 102 queue/adapter).
  Architecture work stopped at these cohesive seams; see [the boundaries](docs/architecture.md#saved-dsp-library-and-authored-queue-contracts).
- **Local Web controller:** Now Playing, position/artwork, transport/seek,
  volume/mute, queue selection, all five repeat/shuffle modes, favorites
  add/save-current/play/rename/delete/target repair, balance, EQ bypass and
  ten-band/saved-preset editing are implemented and independently reviewed.
  Typed same-origin/CSRF controls use canonical renderer truth; private-safe
  metadata, sticky unknown outcomes, Stop and explicit volume=0 recovery remain
  honest. Dialog cleanup, delayed-close races and logical focus return are
  covered. See [controls and privacy limits](docs/WEB_UI.md).
- **Playlists and export:** stopped-only JSON/M3U/direct-URL loading and authored
  append/remove/move preserve metadata and selected duplicate occurrences.
  Session/generation-fenced drafts make no media request before explicit Play;
  provider/radio queues remain read-only for editing. Private current-queue JSON
  export separates redacted preparation, private-URL consent and user-clicked
  download, with exact-byte HMAC stale-review protection. Existing 32 KiB Web
  and 1 MiB escaped request limits remain. No durable queue storage or new
  playback/backend authority was added. See [playlist editing](docs/WEB_PLAYLISTS.md)
  and [private export](docs/WEB_PLAYLIST_EXPORT.md); favorites/M3U export is deferred.
- **Startup and delivery:** explicit-config failures and known saved-state
  reader failures have private-safe startup diagnostics without adding reset/default
  fallback or changing public loader/library error contracts. Backend cleanup
  still precedes listener startup after a second active-DSP read failure.
  Native source snapshots/wheels include all four Web assets; isolated HTTP
  and hosted installed-CLI restart checks cover delivery on x86_64/ARM64.
  Same-runner default-suite partitioning retains exact coverage, skips and caps.

### Saved-state failure protection

Saved-favorites read diagnostics are fully green at `660cab1`: fixed private-safe
service-restart guidance, retained stale rows/gates, unchanged storage/mutation
policy. Focused 246 Python cases per interpreter and 156 Node checks preceded
that exact-head result. Current handover sections were consolidated; both older
historical suffixes remain byte-identical.

A separately reviewed saved-EQ correction now fences persistence after observed
load failure. Valid/missing storage behavior remains normal, while previously
destructive legacy Save/SourceSave/no-op Delete fail with 502/Failed. Web saved-
library writes reject before effects with 409/REJECTED. Original bytes, active DSP,
rollback and real committed-write uncertainty are retained; built-in load/edit/
bypass remain usable. No automatic repair/reset/migration is added. External file
changes after healthy/missing load are outside this bounded protection.

Final combined local defaults pass **5399/74** on Python 3.12 and 3.11; **164
Node** checks pass. Actual mpv 0.37/0.40 fresh-object malformed-library startup
passes in **5.16s/4.85s**. Seven refused Web/legacy writes preserve bytes and live
playback with no native/media mutations; all 231 previous native assertions and
30s/35s caps remain. A one-fence-removed negative probe really overwrites its
synthetic file and fails. This is not process/systemd restart, unreadable-file
native proof, repaired-library recovery, browser or physical audio acceptance.
Exact new-source hosted CI is **PENDING**; `660cab1` does not certify this change.

### Latest favorite-repair evidence

Change URL uses the existing stable-ID API, a blank private input, explicit
keep/clear metadata choice and one submission per opening. It retains name,
ID and order, clears private input and reports ACKNOWLEDGED or UNKNOWN; a
redacted reread cannot establish exact-URL verification. Observed Rename/Delete/
unsent Change URL conflicts require reopening, without claiming atomic concurrency.
No server/store/API contract, detached playback or backend ownership changed.

Independent review passes. Favorite-repair local evidence is **149 Node/DOM checks**,
**159 focused Web/frontend checks on each Python 3.12 and 3.11**, and **195 root
Web/frontend/installation checks**. Actual libmpv 0.37/0.40 keep/clear flows pass
in **4.32s/4.19s**, preserving prior assertions/caps and proving no native/media
side effects. No redundant full local sweep was run for this browser-only delta.
The earlier complete local **5322/74** result belongs to `5534c4d`, not this head.
Detailed scope and reproduction remain in [TESTING.md](docs/TESTING.md#browser-favorite-target-repair-checkpoint).

## Compact checkpoint history

These are exact-source checkpoints, not blanket certification of later work.
Each listed hosted run passed all three jobs; from the DSP/package checkpoint
onward, every step was also verified. Detailed local counts, timings, findings
and evidence limits are retained in [TESTING.md](docs/TESTING.md).

- Architecture `5b86bdd`: [run 38317217424](https://github.com/de41tr/open-room-renderer/actions/runs/38317217424).
- First Web controller `81a3d72`: [run 38319222442](https://github.com/de41tr/open-room-renderer/actions/runs/38319222442), including 67 native checks and real Unix IPC on both native architectures.
- DSP/editor and native packaging `7d68d79`: [run 38323337010](https://github.com/de41tr/open-room-renderer/actions/runs/38323337010); guarded Web -12 dB measured **-12.0387 dB at 1 kHz** on libmpv 0.37/0.40.
- Playlist/startup `f7c6ed7`: [run 38325071508](https://github.com/de41tr/open-room-renderer/actions/runs/38325071508); the selector-order finding was corrected without relaxing any assertion or cap.
- Navigation privacy/quiet-gain recovery `0668209`: [run 38326558265](https://github.com/de41tr/open-room-renderer/actions/runs/38326558265).
- Final scheduling/docs `59726d5`: [run 38328911125](https://github.com/de41tr/open-room-renderer/actions/runs/38328911125), verified 01:02 UTC; no further timing changes unless a gate fails.
- Private queue export `1dfd642`: [run 38330676949](https://github.com/de41tr/open-room-renderer/actions/runs/38330676949), verified 01:22 UTC.
- Saved-state diagnostics/logical focus `5534c4d`: [run 38332263993](https://github.com/de41tr/open-room-renderer/actions/runs/38332263993), verified 01:42 UTC; full local defaults **5322/74** on both Python versions.
- Browser favorite target repair/navigation docs `8c7df63`: run **38334412981**, verified 02:08 UTC; current hosted defaults **5324/74**, as scoped above.

- Saved-favorites diagnostic/handover `660cab1`: [run 38337365883](https://github.com/de41tr/open-room-renderer/actions/runs/38337365883), verified 02:47 UTC; 5357/74 hosted defaults.

## Remaining limits and next decisions

**BROWSER_ACCEPTANCE_PENDING:** genuine rendering/layout, accessibility/focus,
interruption and download-to-disk acceptance remain unperformed. The full UI is
available at the existing `/ui/` path; use [the browser checklist](docs/WEB_BROWSER_ACCEPTANCE.md)
when a supported route is available. DOM doubles and native HTTP flows are not
browser acceptance. Legacy unauthenticated controls remain a trusted-LAN limit.
**HARDWARE_PENDING:** physical ARMv7/XU4, DAC/ALSA/audio and original-app acceptance
remain outstanding; no physical-silence or hardware claim is made.

Spotify feasibility is complete. [ADR 001](docs/adr/001-spotify-sidecar.md)
supports a separately user-installed official Soloist DIY experiment, while
integrated playback is **DEFERRED** on unresolved all-entry audio arbitration
and combined-provider policy scope. Provider account acceptance is
**ACCOUNT_PENDING**. No credentials, client/key, provider code or proprietary
binary was added.

The saved-EQ preservation correction awaits its own exact-head CI. The 02:42
value review prioritizes concrete data-preservation/recovery defects over broad
feature growth. Next review: **04:12–04:42 UTC**. Avoid redundant endurance,
timing or known-blocked browser campaigns. Start no large/risky new work after
**16:30 UTC**, stabilize from **17:00 UTC**, and stop at **18:00 UTC**.
See [RESUME.md](RESUME.md) for the bounded next steps and [PLAN.md](PLAN.md) for
this phase's full constraints. The historical reports below retain their own
exact-source scope, deadlines and failures.

---

## Historical completed work

# Bounded GENA architecture extraction

Actual start: **2026-10-12 21:05:18 UTC (23:05:18 Berlin)**.
Hard stop: **2026-10-13 01:05:18 UTC (03:05:18 Berlin)**, without extension.
Midpoint fallback deadline: 23:05:18 UTC. A safe implementation was already
independently reviewed and passing by 21:10 UTC.
Final validation/publication/handover was reserved from **00:35:18 UTC** at the
latest and began early. This is a maximum budget, not an instruction to fill it.

Baseline: clean `1478864ac3351cd253dca32c56726723b3605f60`; all three jobs
passed in https://github.com/de41tr/open-room-renderer/actions/runs/38306668149 .
The preceding DSP CLI task was complete before this separate authorization.
Branch: `dot/renderer-autonomous-20261012`, open Draft PR #1. Main remains
`3672b9838e9c39ae8191613108f0d91a5cada97d`; no merge or release.

## Verified architecture checkpoint

Production extraction: `9acb44bc83319c053d9995e6c9b7f56e10677a7a`.
Independent boundary tests: `aafa6649d5141123cfa644a7c30150f99fba9325`.
[Exact application/test CI 38309188954](https://github.com/de41tr/open-room-renderer/actions/runs/38309188954)
passed **all three jobs**, verified 2026-10-12 **21:27 UTC**.
Python 3.11 and native x86_64/AArch64 retain every default case, native lane,
assertion, skip and time cap. Hosted defaults pass **4588 /73 opt-in skips**;
Python 3.11 groups pass 4338+250 in 150.23s/88.54s. Both native architectures
pass all 66 libmpv checks and every production Unix/client/stress/install/
clocked/discovery/Snapcast stage. No workflow or existing test was modified.

`EventSubscriptionManager` now owns subscription admission/renewal, SID/leases,
limits, SEQ, dirty coalescing, retry/delivery tasks, expiry and cancellation/drain.
Its sole renderer dependency is a synchronous service-to-XML snapshot provider.
HttpApi remains the only playback/queue/DSP authority and owns payload projection,
trigger points and overall listener/shutdown order. No new lock, task, backend
command or scheduling boundary was introduced. The added close coroutine wraps
the same conditional gather; empty-close and final-listener tests verify it.

All 4583 unchanged defaults separately pass locally on Python 3.11/3.12;
five new manager/compatibility tests pass both minors. A real-loopback comparison
with the exact baseline yields **113 identical ordered observations**, including
byte-identical payloads and normalized protocol headers. Local full libmpv 0.37
passes 23+43 cases; focused 0.40 integration/DMR/DSP/invalid-media passes four.
These results preserve the distinction between mock, real-native ctypes/null/PCM,
hosted production Unix IPC and unavailable physical/app acceptance.

- [Actual boundary, dependency map, tradeoffs and next seam](docs/architecture.md#gena-lifecycle-boundary)
- [Reproduction and evidence scope](docs/TESTING.md#gena-ownership-extraction-verification)

This documentation-only handover leaves application/test/workflow bytes identical
to the green `aafa664` checkpoint. Verify the documentation head's own full CI
in [Draft PR #1](https://github.com/de41tr/open-room-renderer/pull/1) before final
completion; the earlier green run must not be substituted for its check status.
No further extraction is scheduled. Once that verification completes, stop
active work. This handover is not continuing authorization for another task.

**HARDWARE_ACCEPTANCE_PENDING:** XU4/ARMv7, physical DAC/ALSA/audio, original
WiiM Home app and two-host acceptance remain outstanding, not blockers here.

## Critical assessment

The gain is an independent owner for a coherent lifecycle, not just wrappers:
admission and delivery no longer inspect renderer internals. The cost is a small
component, one provider contract and explicit compatibility forwarding. The live
mutable registry/tuning views remain deliberate coupling. Payload serialization
and the large playback/recovery/DSP controller remain; this is not a completed
whole-system decomposition. Next consider DSP preset-library validation/storage,
keeping active backend transactions separate. Playback ownership, generation or
lock-order redesign needs a separate user decision and wider plan.

The sections below are historical completed work; their earlier deadlines
do not govern this separate task.

---

# Development status

The earlier autonomous window completed at 2026-10-12 20:00 UTC.
A separate user-authorized DSP CLI hour began at 20:35:04 UTC and ends at
21:35:04 UTC (23:35:04 Berlin), with no automatic continuation. Its current
verification record will be kept separately from the older evidence below.
This is scoped software evidence, not a compatibility certification.

## Bounded DSP CLI checkpoint

The new client-only increment adds `dsp balance get`, `dsp balance set VALUE`
and `dsp eq on|off`, using only the existing HTTP API. Balance is normalized
-1..1 at the server's three-decimal resolution. EQ enable/bypass preserves the
existing active context and saved curve. The successful output acknowledges a
request; it does not claim ongoing state or audible sound.

Validation happens before networking. Every request is single-shot, bounded,
private on failure and isolated from environment proxy/netrc credentials. Exact
HTTP 400 rejection is distinct from UNKNOWN. HTTP 502 stays UNKNOWN because
real transaction tests prove it can occur after DSP applies but saving fails.
Lost acknowledgement or unconfirmed rollback never becomes success or an
automatic mutation retry. There is no new endpoint, audio backend or core refactor.

Independent review approved the exact production, tests and operator docs.
The new focused suites pass 171 cases. The full Python 3.12 default suite passes
**4583 /73 opt-in skips** in 196.65s, with the same 32 upstream warnings.
The complete Python 3.11 groups pass 4333 +250 /73 in 132.25s/64.69s,
inside the unchanged 165-second group watchdog. No case or gate was removed.

The existing real DSP PCM lane passes on libmpv 0.37/0.40 in 3.91s/3.77s. Nine
actual CLI children each send one bodyless GET. EQ off restores sample identity;
on restores the same native graph and measured -12.0387 dB cut. Balance -1/+1/0
retains exact silent-channel/identity checks. All 13 prior captures remain, plus
one restored-EQ capture. In-flight CLI cancellation reaps the child and closes
native/server resources. Final DSP + restore replay passes both versions
(2 cases, 5.58s/5.34s); the 35s/45s lane caps and all earlier gates remain unchanged.

This is loopback/test-ctypes digital PCM, not production Unix DSP CLI, physical
DAC/acoustic audio, original-app or hardware acceptance. Existing production
Unix coverage remains in full hosted CI; the local AF_UNIX restriction persists.
After publication, verify the exact new head's full CI in Draft PR #1. The earlier
02cb93d run remains a separate green baseline, not evidence for this new code.
No further feature is scheduled; stop no later than 21:35:04 UTC.

## Previous completed stabilization handover

Application/test checkpoint **1d054809a836eb844fd67fa833c24cccba3dc505** passed
all three standard jobs, verified at 17:16 UTC on 2026-10-12:
https://github.com/de41tr/open-room-renderer/actions/runs/38281381125 .
Both native architectures passed 4412 default cases /73 explicit opt-in skips,
all 66 selected libmpv checks (53 actual native cases plus 13 helpers), and every
production-Unix/client/stress/install/clocked/discovery/Snapcast stage. Python
3.11 groups passed 4162+250 in 141.41s/90.38s. Existing warnings and caps remain.
This final documentation update does not change application, tests or workflow.
After publication, verify this documentation-only handover's own CI in the draft PR.

The local workflow now includes real multitrack queues, repeat/shuffle and
confirmed transport controls; editable stable-ID favorites; private URL-playlist
capture/import/editing/stopped restore; and bounded unauthenticated discovery
hints. Earlier core checkpoints provide truthful faults/state, owned recovery,
metadata preservation, confirmed gain/DSP and persistent independent identity.
Unsupported services or hardware controls do not become advertised functionality.

The final acceptance addition proves malformed-format failure after a successful
native load acknowledgement: error -17, HTTP 502 and matching SOAP-read/received-
GENA STOPPED/ERROR_OCCURRED, followed only by explicit valid-source recovery.
It passed actual libmpv 0.37 on x86_64/AArch64 with complete cleanup; the exact
local x86_64 test also passed 0.40. This uses test ctypes/null transport. Physical
missing-device, mid-stream corruption and a malformed-media production-Unix case
remain unclaimed. Runtime source is unchanged from a7e8d8.

- [Reproducible local two-source command](docs/TESTING.md#reproduce-the-local-two-source-acceptance)
- [All 20 original packages and their remaining acceptance limits](docs/COMPATIBILITY_MATRIX.md#original-work-package-acceptance-overview)
- [Three practical constraints, technical debts, missing features and next decisions](docs/STRATEGIC_REVIEW.md#next-phase-assessment)
- [Later 45–60 minute hardware/app session](docs/TESTING.md#deferred-xu4--dac--original-app-acceptance)

**HARDWARE_ACCEPTANCE_PENDING:** physical ARMv7 XU4/ALSA/DAC, physical LAN,
original app, service boot and two-host synchronization remain outside the
verified scope. The four-hour radio result belongs to its older frozen source.
The branch remains `dot/renderer-autonomous-20261012`; main was not changed and
[PR #1](https://github.com/de41tr/open-room-renderer/pull/1) remains a draft.
Formal stabilization begins at 19:00 UTC; stop active work at 20:00 UTC. This handover
is not authorization for automatic continuation after that time, merging or release.

## Preceding legacy-client checkpoint (16:38 UTC)

Legacy-isolation head `a7e8d8` passed all three jobs, verified at 16:38 UTC:
https://github.com/de41tr/open-room-renderer/actions/runs/38277056416 .
Both native architectures passed 4412/72 defaults, all 65 then-selected libmpv
checks and every later Unix/client/stress/install/clocked/discovery/Snapcast gate.
Floor groups passed 4162+250/72 in 140.18s/87.45s. Legacy endpoint/session isolation
preserves valid wire/output behavior and existing redirect rejection while
removing ambient proxy/netrc effects. No playback-core or listener change.

## Preceding discovery checkpoint (16:21 UTC)

Discovery head `5cf83ee` passed all three jobs, verified at 16:21 UTC:
https://github.com/de41tr/open-room-renderer/actions/runs/38275014639 .
Both native architectures passed the actual console/multicast flow: two known
candidates, two kernel-reported TTL-0 searches, zero HTTP requests at either
fixture endpoint, child reaping and observer/renderer cleanup. Every earlier
native/Unix/client/stress/install/clocked/discovery/Snapcast gate also passed.
Floor groups passed 3938+245/72 in 134.72s/83.10s. All caps remain unchanged.

The command returns bounded, sanitized, unauthenticated SSDP candidates on an
explicit IPv4 interface. It never fetches HTTP, loads config or controls a result;
an advertised description port is not a verified primary API port. Native
same-host acceptance does not establish physical-LAN, hardware or original-app
compatibility. The separate native console wrapper loads candidate source using
PYTHONPATH; no additional installed-wheel claim follows from that specific test.

## Preceding favorite-target checkpoint (15:55 UTC)

Favorite-target head `d9a94e2` passed all three standard jobs, verified at 15:55 UTC:
https://github.com/de41tr/open-room-renderer/actions/runs/38271661061 .
Both architectures passed all 65 libmpv checks and every later Unix/client/stress/
install/clocked/discovery/Snapcast gate. Defaults passed 4026/72 opt-in skips;
floor groups passed 3781+245/72 in 129.95s/77.52s. All existing caps remain.

The published feature repairs a managed favorite's URL without changing its
stable ID, name or library position. The caller must explicitly choose keep or
clear for title/artist/album/artwork. The new bounded PUT/client leaves legacy
rename/current and playback clients unchanged. It does not resolve, play or retarget
an admitted snapshot. Configured favorites stay read-only; atomic pre/postcommit
outcomes remain truthful. Unknown acknowledgements never trigger retry/rollback.

The exact final native flow keeps the old source/entry and metadata playing through
one PUT, then a later same-ID recall performs exactly one load of the other source.
Recreation verifies repaired target/metadata persistence without autoplay. Existing
pywiim, compaction, radio and cleanup proofs remain. Final libmpv 0.37/0.40 probes
pass in 6.278s/6.274s under unchanged 16s/18s bounds. Native metadata policy is keep;
clear and concurrent admitted-snapshot cases have deterministic/HTTP coverage.
A minor legacy parser diagnostic collision was fixed before the final v2 freeze.

Final combined defaults pass 4026/72 on Python 3.12 (199.30s), and 3781+245/72 on
Python 3.11 (128.26s/74.23s), with 32 existing warnings. Production digest:
`7feac68d6165add699b8041ebc68bfd0f2d0085fd58841773107fed46e0be3a5`.
A separate workflow-only rebalance moves unchanged repeat/shuffle files to native
group one: all 24 files/65 node IDs and marks remain, now 22+43. Hosted groups
passed in 227.43s/242.88s on x86_64 and 226.38s/239.28s on AArch64.
No time cap, assertion, runner or process count changes. The reduced imbalance is
observed; total-job timing still includes ordinary runner variability.

Bounded SSDP-only candidate discovery is in private implementation, independent
review and native acceptance. A final narrow legacy CLI transport correction is
being implemented: validate endpoints and ignore ambient proxy/netrc settings,
while preserving valid request bytes, outputs and existing redirect rejection.
No speculative additional feature is scheduled before stabilization.

The requested behavior-neutral extraction began at 09:14:53 UTC after its prerequisite green gate.
It moves exactly three stateless metadata-edit helpers into metadata_formatting.py,
with unchanged HttpApi wrappers. DIDL creation, dynamic escaping, parsing and all
playback/recovery/staging/event lifecycle behavior stay in place. The exact patch
is independently reviewed, including byte/error/effect comparisons. Final local
default suites passed 2813 /66 on Python3.11/3.12, and both complete selected
libmpv lanes passed 60 checks. All three hosted jobs passed by 09:51:33 UTC;
the bounded extraction completed in 36m40s, inside its 90-minute target. The short target architecture is in
`docs/architecture.md`; this is a small seam, not a new framework.

At `3e74b24`, expanded live-HLS/libmpv lanes failed: both architectures lost native
playback-time during the segment-outage two-second hold; four other HLS cases
passed. x86 also lost playback-time immediately after a child-path observation
in both finite playlist readiness tests; ARM passed those. Both fixture causes
are now reproduced and corrected under unchanged playback acceptance. Integrated
replay passed all eleven affected native cases on both 0.37/0.40, plus 2368
default tests /62 skips after the prerequisite-floor correction. The corrected
hosted revision passed; the earlier failed checkpoint is not relabeled green. Every other 3e74b24 native gate and the
Python3.11 floor passed.

The independently frozen final finite-policy constant-title hour PASSED:
3600.675s baseline, 3600.589s native progress, one load/request, 362 protocol
agreements, 1936 KiB peak RSS growth and complete cleanup. All 26 source hashes
remained unchanged. Its production/source digests are recorded below; this is
loopback AAC/libmpv0.40/null evidence, separate from changing-ICY, Unix IPC or
hardware. Earlier failed and zero-policy runs keep their original scope.

The separately frozen four-hour changing-ICY run PASSED: baseline
06:06:23.924497–10:06:26.449348 UTC, 14,402.516s; cleanup finished at
10:06:26.620761 UTC. All 1,795 autonomous title changes agreed across HTTP/SOAP/GENA,
with one load/source/request, zero disconnects and 4,428 KiB live RSS growth.
Full source and production digests stayed unchanged; see the scoped evidence below.
Repeat, editable favorites, Shuffle and stable radio save-current now have full
hosted checkpoints. The frozen endurance result retains its earlier source scope.

## Baseline

- Source and reference review agree on base commit `3672b9838e9c39ae8191613108f0d91a5cada97d`.
- Python 3.12.14; initial suite: 92 passed.
- mpv 0.40.0 can decode synthetic WAV, MP3 and FLAC to null output.
- At baseline the sandbox denied Unix-domain sockets and default-route multicast.
  Production Unix IPC subsequently passed native CI; explicit-interface loopback
  multicast now passes locally. Physical LAN/audio/app acceptance remains open.

## AP01 checkpoint

- SOAP service path, namespace, header action and body action must agree.
- Unsupported actions, malformed inputs and previously successful queue/channel
  no-ops now produce UPnP faults without dispatching mutations.
- Missing/invalid volume, mute, URI, seek and queue-index inputs are rejected.
- Backend control commands wait for acknowledgement. Backend rejection does not
  commit successful volume, mute, pause, stop or seek state.
- Rejected loads and readiness failure no longer return SOAP success.
- Ordinary client extensions, Master/Single channels and XML text fields remain
  supported. ConnectionManager has minimal read-only protocol/connection queries.
- Loop setters are temporarily rejected at the SOAP boundary until actual queue
  looping is implemented; Next is likewise rejected rather than silently ignored.

Validation for this checkpoint: 133 tests passed (UNIT/MOCK), including 41 new
SOAP/backend regressions. `git diff --check` passed. No new REAL_AUDIO,
ANDROID_APP or HARDWARE validation.

## Earlier checkpoint limitations (superseded below)

AP02 now has a tested generation/state checkpoint (below). AP05 now provides concrete multi-item queues (below). SCPD declarations, app handshake, callback leases/policy,
recovery and backend lifecycle need their planned focused passes. Current tests
must not be taken as full WiiM Home app compatibility.

## Client assertion checkpoint

AP01 published as `2e3068ede2b095b572f7249bc7bdaadecdd9641a`, draft PR #1.
The PyWiiM acceptance check now compares actual transport fields exactly; a
`play_status` key containing `stop` cannot satisfy a play assertion. Missing,
conflicting, nested or metadata-only states fail. 85 new synthetic cases cover
the installed pywiim 2.3.9 flat response shapes. Full suite: 218 passed
(UNIT/MOCK); this is not a real-client/audio acceptance run.

## AP02 checkpoint

- One authoritative phase and playback generation feed HTTP, SOAP and GENA.
- Serialized backend mutations and source-aware snapshots prevent delayed loads,
  old observations and seek completions from overwriting newer owner commands.
- Pause/stop during resolution or loading preserve owner intent; resume loads the
  selected resource, not a superseded source. Stop is verified by backend readback.
- A shared observer detects EOF and backend errors without requiring client polls.
  Failed loads remain ERROR until a new command; real backend identity and source
  offsets are tracked independently of pending selection.
- Seek reload preserves actual backend pause. Readiness is bounded and cannot
  resurrect terminal state or overwrite fresher buffering/paused observations.
- HTTP getPlayerStatusEx is supported; backend failures return HTTP 502, so real
  PyWiiM does not treat a text Failed response as successful mutation.
- Production Unix JSON IPC architecture remains unchanged.

This checkpoint: 259 passed (32 new state UNIT/MOCK cases and 9 INTEGRATION
cases using real local HTTP + installed pywiim 2.3.9 with a fake audio backend).
The client tests clean up sessions, listeners and observer tasks. PyWiiM emits
9 upstream TLS-version deprecation warnings. No actual Android or DAC claim.
AP02 published as `4a3849e391111e2fb45b95dc7ee6f6aaf30423fb`.

## AP05 and real-backend checkpoint

- Concrete QueueContext tracks are parsed into one bounded queue model; counts
  and one-based indices come from that model. Empty queues report zero.
- Create/replace, named indexed play, Next, Previous, Browse and queue-index
  queries select the actual resource. Finite EOF advances to the next item.
- Invalid indices/names/XML do not mutate state. Old EOF work is cancelled by
  newer owner commands. Direct source replacement cannot reuse a stale resolved
  URL. HTTP, SOAP and event payloads report the same index/resource.
- Metadata text and provider identity survive XML round trips. Only normal
  loop mode is supported; repeat/shuffle requests still fail explicitly.
- An optional test-only libmpv transport exercises the real backend through
  local HTTP with generated WAVs and null output. It verified actual different
  resources for indexed play/Next/Previous/EOF, advancing position, pause/resume,
  volume/mute properties, paused seek, stop/reload and cleanup/cancellation.
  Event payload consistency was checked; callback delivery is an AP04 task.

Validation: 372 passed with the optional REAL_BACKEND/null lane enabled;
otherwise 371 passed and 1 explicit skip. Nine upstream PyWiiM TLS deprecation
warnings remain. This does not verify production Unix IPC, audible output,
physical DAC, target ARM hardware or actual Android app behavior.

Known follow-ons: legacy unresolved-provider CreateQueue still needs AP06
canonical replacement/generation integration. Discovery/SCPDs/capabilities,
GENA callback lifecycle/security, backend lifecycle and recovery remain open.

## AP03 protocol/discovery checkpoint

- New compatibility installations persist their own FF-prefixed random identity;
  existing/configured identities are preserved. Names, synthetic MAC-like IDs
  and DNS hostnames derive from the full identity instead of shared padded tails.
- SSDP UUID/root/service targets and UTF-8 ServiceName formatting are consistent.
  mDNS handles bounded Unicode names and refreshes after rename without changing
  identity. Neutral defaults use independent product/firmware metadata.
- Every SCPD now declares implemented actions, arguments, types and event fields.
  Unsupported mutations are absent; optional Plex UserLogin is conditional.
- All status/capability surfaces use one service list. Default is provider-empty;
  configured public TuneIn is explicit. Unsupported service sentinels remain
  inactive, cannot fake a login/premium account, and do not appear as supported.
  Unknown hardware support and output parameters are no longer invented.
- A real loopback HTTP handshake follows synthetic discovery, description/SCPDs,
  legacy reads, queue creation, two different tracks, pause/resume/Next, volume,
  mute, stop and rename. Its audio backend is a deterministic fake.

Validation: 518 passed with REAL_BACKEND/null enabled, or 517 passed plus one
explicit skip by default. The real libmpv queue/control lane still passes.
Multicast packets are tested synthetically; actual LAN discovery and original
Android application compatibility remain NOT_RUN. Existing non-FF identities
are intentionally not silently replaced.

AP05 published as `2d6e6ad29f998ab58f57ab38c4d19c93a5662b90`.
Next: AP04 bounded subscriptions, retries and same-peer callback policy.

## AP04 and network-boundary checkpoint

- GENA initial subscribe, renewal, deduplication, unsubscribe and expiry now have
  bounded peer-bound leases. A temporary failure keeps the subscription and
  retries its unchanged payload/sequence; later changes use the next sequence.
- Delivery is ordered/coalesced per SID, limited globally/per peer, and cleaned
  up on expiry, unsubscribe and shutdown. Callback redirects are never followed.
  LastChange handles both XML layers, Unicode and whitespace correctly.
- Callbacks target only the actual request peer's literal IP. Invalid rename
  text cannot persist an XML-breaking device name.
- The media proxy is disabled by default, local-backend-only and selected-source-
  bound. Each redirect is independently validated and DNS-pinned. Unsafe local,
  metadata and unapproved private targets are rejected; no client credentials or
  environment proxy settings are forwarded. Setup, concurrency and redirects
  are bounded. See SECURITY.md for explicit LAN media exceptions and limits.
- The real libmpv/null workflow now also verifies actual GENA callback delivery
  for backend volume/mute and the selected second track. The local app-shaped
  handshake exercises HTTP, SOAP and callbacks together with its fake backend.

Validation: the exact AP04 publication snapshot passed 740 tests with the
REAL_BACKEND/null lane enabled (739 plus one explicit skip by default). Nine
upstream PyWiiM TLS deprecation warnings remain. Proxy integration uses local
fixtures with explicitly mocked approved routing; forbidden destinations are
rejected without being contacted.

AP03 published as `99d771d0142934f63d534493e7e7c93b68f664ca`.
Next: AP06 canonical header-only provider resolution; AP07 lifecycle work is
independent and in progress; AP08 owner-respecting recovery follows.

## AP06 public-station queue checkpoint

- Header-only `newTuneIn` station IDs resolve into exactly one concrete queue
  item. Source, station ID, name, show name, artwork and opaque SearchUrl survive;
  Play uses the resolved media URL through the ordinary backend.
- A fixed anonymous OPML JSON endpoint receives only station ID and format
  parameters. No OEM partner fields, credentials or arbitrary SearchUrl are sent.
  Responses, deadline and candidate attempts are bounded; redirects and service
  error announcement clips are rejected. Unsafe candidates do not hide later
  valid ones. No account/premium integration is claimed.
- Resolution is generation-scoped. Pause, stop or a new source invalidate old
  results. Failed resolution faults rather than inventing playable state.
- Unsupported provider/episode headers fault explicitly. Concrete episode tracks
  retain their actual supplied duration; the invented one-hour fallback is gone.
  Optional on-demand proxying also works for concrete episode URLs.

Validation: 861 tests passed in the exact publication snapshot with the optional
REAL_BACKEND/null lane enabled (860 plus one explicit skip by default), including
MOCK station response and SOAP queue regressions. Public endpoint and bounded AAC
bytes were independently observed through research tools; the production Python
resolver's live attempt was blocked by this environment's DNS resolution. This
is not a live end-to-end radio playback, Android or hardware certification.

AP04 published as `792d79c773273255b2eb25307cc814d2a96344ff`.
Next: finish AP07 lifecycle review fixes, then AP08 bounded owner-respecting
source recovery.

## AP07 backend ownership and shutdown checkpoint

- Concurrent requests share bounded startup and one spawn attempt. Failed
  startup does not become a process-spawning loop; the control API remains
  available with ERROR. External IPC can recover without a renderer restart.
- Commands have end-to-end deadlines and validate acknowledgements. Shutdown
  blocks late writes, reaps only owned children, and never sends quit to an
  externally managed backend.
- Existing files, symlinks and foreign sockets are preserved. On Linux, an owned
  socket is removed after reap only with matching peer PID and unchanged inode.
- SIGINT/SIGTERM are handled during startup and throughout bounded teardown;
  repeated signals cannot strand a child by interrupting cleanup. Partial
  HTTP/discovery startup is cleaned up too.

Validation: 938 tests passed with the optional REAL_BACKEND/null lane enabled
(937 plus one explicit skip by default). The 77 lifecycle tests include actual
child-interpreter signal delivery with mocked renderer resources. Unix socket
ownership/protocol cases are deterministic mocks because AF_UNIX creation is
blocked here. Failed owned spawning requires a renderer restart; an unverified
stale foreign socket is deliberately not deleted automatically.

AP06 published as `bba255dc81df0257662ac60537f41fc7e38e041f`.
Next: AP08 bounded station recovery, then meaningful P1 acceptance gaps.

## AP08 established-station recovery checkpoint

- An explicitly marked live station that reaches EOF/unexpected idle gets one
  generation-bound recovery task and a lifetime budget of three delayed reloads.
  The budget is not replenished by repeated observations or successful retries.
- Pause, stop, queue/index replacement and newer sources invalidate pending work.
  A bounded in-flight backend mutation records its actual identity before a newer
  owner command runs. Volume, mute, metadata and queue index are preserved.
- Native audio returning during backoff prevents a redundant reload only when
  actual resource identity and active audio are confirmed. Unrelated paths cannot
  restore playback. Source resolution, EOF/idle and control failures are distinct.
- Finite/unknown-duration media never gain automatic station retries; normal
  finite queue EOF advance remains unchanged. Exhaustion remains ERROR.

The optional REAL_BACKEND/null fixture used an actual loopback HTTP server to
truncate generated WAV audio, reject the next renderer reload with HTTP 503, then
restore audio. Actual decoded position resumed without client status polling.
Separate real cases verified stop/pause during backoff and teardown; volume 37,
mute and queue index 2 survived recovery. This is controlled local source failure,
not a real WAN outage, production Unix IPC, audible output or handset test.

Scope limits: recovery starts after initial audio has been confirmed (or owner
resume succeeds). A never-playable initial station still fails explicitly.
Sustained cache-stall detection is deferred; ordinary buffering does not trigger
a reload. Public provider URL refresh is also outside this checkpoint.

Validation: the isolated AP08 publication snapshot passed 999 tests with all
five optional REAL_BACKEND/null cases enabled (994 plus five skips by default).
Nine upstream PyWiiM TLS deprecation warnings remain.

AP07 published as `aa3d455d5c80195a2f3e3ccce351b8b8a7702a9c`.
Next: confirmed/persistent digital gain, then existing DSP safety and practical
metadata/seek/install acceptance gaps.

## AP11 confirmed digital gain checkpoint

- Startup applies configured/persisted volume and mute before audio can load.
  Every load and resume reconciles the complete desired pair, including after an
  externally managed backend restarts at its native defaults.
- Volume/mute operations acknowledge and read back both properties in safe order.
  A failed/cancelled confirmation cannot commit a proposed pair or bypass the
  next playback check. HTTP invalid volume/mute no longer clamps into a louder
  setting or silently unmutes.
- Both protocols use one ordered confirmed-state path. Gain is saved atomically
  in a bounded versioned local document; corrupt/unknown saved gain fails closed.
  Disk failure is reported separately from successful backend application.
- Uncertain output is explicitly flagged: gain_confirmed=false, SOAP gain reads
  fault, and GENA omits unknown gain values. A gain-command failure reports ERROR
  without claiming that actual audio is silent. Transport errors remain until a
  new owner transport command; reconfirmed gain clears contradictory wording.
- Actual HTTP gain controls were measured through libmpv into float PCM files:
  volume 25=1/64 amplitude, volume 50=1/8, volume 100=unity. Mute and volume 0 produce
  exact digital zero. Mute survives volume and source changes. Fresh persisted
  config/state/backend reconstruction restores 25 + mute before the first decoded
  sample; unmute then measures 1/64.

This is REAL_BACKEND/digital-PCM with generated low-amplitude input and a file
sink. It does not measure host mixer/DAC/acoustic output or full systemd reboot.
Fast PCM source loads are explicitly direct backend loads, while gain commands
use real HTTP; the separate null lane still verifies HTTP playback readiness.

Validation: the isolated AP11 snapshot passed 1130 tests with all six optional
REAL_BACKEND cases enabled (1124 plus six skips by default). Nine upstream
PyWiiM TLS deprecation warnings remain. Independent review covered failed and
concurrent gain confirmation, full-pair reconciliation and resume boundaries.

AP08 published as `a6b2b2594403bc92709a874fcf00b8848375cab7`.
Next: existing DSP safety/transactionality and measured filters; active EQ
restart persistence, metadata/seek and native installation acceptance follow.

## AP12 existing DSP safety checkpoint

- Balance/PEQ reject nonfinite, malformed and out-of-range inputs before they
  can become gain or alter state. Only implemented band modes, WiFi source and
  stereo processing are accepted. Zero-gain high/low-pass filters are retained.
- The backend serializes DSP changes, replaces its named graph atomically and
  verifies acknowledgement/configuration readback. Rejected/uncertain changes
  restore the previous graph when possible; unconfirmed rollback is explicit.
- API mutations and getters share a transaction boundary. Unconfirmed proposals
  are not visible, cancellation settles safely, and failed commands do not claim
  active settings. Getters do not select a different plugin.
- Startup balance, including zero, uses the same confirmed path before listeners
  start. Desired config and actual state stay distinct on failure and retry.
- Preset library writes are atomic and bounded like reads. Rename collisions,
  post-replacement durability errors and oversized candidates preserve consistent
  state. Saved names are not falsely restored as active EQ on startup.
- Real HTTP DSP controls into libmpv/PCM measured a requested -12 dB cut at
  -12.039 dB, exact balance-channel silence, zero-gain pass-filter attenuation,
  combined EQ/balance, source retention and retained attenuation after a rejected
  native candidate. Bypass was exact.

Validation: isolated publication snapshot 1390 passed with all seven optional
REAL_BACKEND cases enabled (1383 plus seven skips by default). Native tests use
low-level generated input, negative cuts and a file sink; idle filter readback
is configuration evidence, while decoded PCM proves the tested filter effects.
No automatic clipping/headroom guarantee or physical audio claim is made. Active
EQ application across restart is still an AP13 follow-on; the preset library is
persistent, but it does not silently claim a restored active curve.

AP11 published as `664483c0c2ddb22641f9bd25d42f33d5a11b1f74`.
Next: metadata consistency, confirmed active-setting restore and seek/install
acceptance. Native Unix IPC/ARM64 CI is being prepared separately.

## Native IPC CI prepared

Added a read-only PR workflow on standard public Ubuntu x86_64 and ARM64 runners
with 15-minute limits. No deployment, secret use or repository security setting
change is included. The new opt-in test exercises actual mpv Unix IPC, local
controls/queues/EOF and owned/external process/socket lifetime with null output.

Local verification: 104 related tests passed; the two native cases skip by
default, and explicit opt-in without mpv was confirmed to fail rather than skip.
Positive native execution is pending CI. The public repository has no previous
workflow runs; Actions settings were not changed.

AP12 published as `2bab4bc4a802b9fe8108aca8ad8566ede49eb7b4`.
Continue metadata/active settings/seek work while monitoring actual CI results.

## Native Unix IPC and ARM64 CI verified

Both standard Ubuntu jobs in
[run 38185537983](https://github.com/de41tr/open-room-renderer/actions/runs/38185537983)
passed at `52f5c4d0dffc3dcc900c2726703d0af7524aba37`. Logs verify native x86_64
and AArch64, Python 3.12.3 and mpv/libmpv 0.37.0-1ubuntu4. Each job passed 1383
default tests, then 7 libmpv cases and 2 production Unix IPC cases.

Actual Unix JSON commands confirmed playback/control/queue EOF, independent
resource selection, peer PID ownership, owned socket removal and fresh restart,
and preservation of externally owned mpv. The same jobs also measured digital
gain/DSP and decoded local source recovery. No physical audio, ARMv7/Odroid, LAN
multicast or Android claim follows from this result. The sandbox restriction
remains local; it is no longer the only evidence for production IPC.

## AP09 metadata consistency checkpoint

Queue items now retain artist, album and supplied DIDL alongside title/artwork.
Explicit transport URI remains authoritative; metadata resource URLs are hints,
never replacement destinations. Bounded, namespace-aware parsing rejects unsafe
XML and preserves the original opaque payload, including significant whitespace.
HTTP queue/status, SOAP media/position and actual loopback GENA agree through
selection and Next. Missing fields clear rather than leaking the previous track.
Matching backend titles can refine generated fallback metadata; stale/foreign
source observations cannot. Unknown source and MIME stay unknown instead of
being inferred from URL substrings. Extended gain-bearing SOAP reads now expose
GainConfirmed consistently with their declared schemas.

Validation: the isolated publication snapshot passed 1458 tests with all seven
optional libmpv cases enabled; the two production Unix IPC cases explicitly
skipped in this restricted sandbox. Thirteen new loopback client cases use fake
audio and real HTTP/SOAP/GENA connections. This is not original Android acceptance.
Native Unix IPC and ARM64 evidence above belongs to the preceding CI checkpoint;
the new commit's CI will be checked separately. Next: actual seek confirmation,
active EQ restore and deployment acceptance.

## Configured music presets now select actual targets

`getPresetInfo` exposes the real client's `preset_list` plus the legacy alias,
with one-based numbers shared by selection and advertised slot count.
`MCUKeyShortClick:N` and `N:1` start that configured direct target through the
normal confirmed queue/load path. Empty or malformed configured entries are not
advertised; unknown slots and unsupported internal playlist indexes fail before
changing the current owner. No phantom six slots, provider account, saved
playlist position or successful audio from a failed load is claimed.

Actual pywiim 2.3.9 over loopback lists and selects two different resources.
Configured order, UTF-8 rename and identity survive configuration reload.
Delayed old selection cannot replace a newer target. Isolated snapshot: 1477
passed with seven real libmpv cases; two native IPC cases skipped locally.
Nineteen preset checks include the explicit experimental slot and failed
replacement while another target is active. Audio in these client checks is
MOCK; native decoder coverage is in the separate existing lane.

AP09 is published as `93f51e4c310827b1ec1ad9da38b3d6eb9eda851c`. Its
[CI run 38187041169](https://github.com/de41tr/open-room-renderer/actions/runs/38187041169)
passed on both x86_64 and AArch64: 1451 default tests, seven libmpv cases and
two production Unix IPC cases per architecture.

## AP10 confirmed seek checkpoint

HTTP/SOAP seek and first-track Previous now await actual backend position within
a fixed one-second target window. Requested position is never published as an
observation. Forward/backward ignored acknowledgements, including the former
12-to-10-second false positive, fail rather than succeeding after a timer.

The entire seek operation is tracked for shutdown, with bounded preflight lock,
mutation and confirmation phases. Stop/pause/new selection/new seek, backend
failure, EOF and changed native resource identities cannot complete old work.
Known live stations reject seeks. Absolute negative/end-or-later targets reject
consistently; relative buttons clamp, and fractional backend durations retain
valid last-second targets. Lost acknowledgements report uncertainty and a public
resume reloads the selected source rather than retaining a false range identity.

Optional byte-range reload remains disabled by default. Segment coordinates are
translated consistently, shifted responses must supply matching 206 ranges,
and ignored/invalid Range responses fail. Bitrate-derived global timing remains
approximate, especially for VBR/container media; these range cases are synthetic
HTTP tests, not a gapless or precise compressed-media seek certification.

Independent review: 168 focused checks plus reproduced race/failure cases passed.
Isolated default snapshot: 1590 passed, nine explicit native opt-in skips.
Extended real libmpv/null verified a generated 30-second source, paused seek to
10 seconds, forward/backward HTTP/SOAP seek, resume, Previous and cleanup.
The native Unix fixture is extended and awaits this commit's CI run.

Configured preset checkpoint is published as
`84d7caeda4f3c4fc823217305fc0a6529735b2a7`; its
[CI run 38187800513](https://github.com/de41tr/open-room-renderer/actions/runs/38187800513)
passed default, libmpv and production Unix IPC lanes on both architectures.

## AP14 native installation and safe profiles

Neutral core/client defaults and normal checkout runners now use port8080 with
a stable identity. Explicit app-compatible configuration is separate; port80
and fresh identity are individually opt-in. Launch/acceptance helpers no longer
automatically run the entire renderer with sudo. Explicit existing YAML remains
unchanged; migration guidance preserves old state rather than deleting it.

The native installer uses a new private prefix, native Python/venv/mpv checks,
source/dependency receipts and an owned-file manifest. It does not enable a
service, create accounts or change security/network settings. Uninstall removes
only unchanged owned files, preserving edited config, runtime identity and
unknown files. Valid YAML replaces the unusable TOML example. User units and an
optional restricted non-root port80 system template are documented separately.

Independent review resolved prefix quoting/whitespace, stage-only checks and
32-bit ARM userspace labeling. Rebased isolated default suite: 1616 passed, nine
explicit native skips. Actual isolated x86_64 installation used Python3.12.14,
mpv0.40 and19 resolved packages; installed-site-packages import, stable identity,
neutral ports, explicit port80, description and static systemd verification
passed. Actual uninstall preserved modified/unknown/runtime files. The new CI
step repeats installation/import/identity/uninstall on each native architecture;
its result is pending this publication. No systemd runtime/reboot, ARMv7, Pi/DAC
or original Android acceptance is claimed.

AP10 published as `349a56ccafb707e9ee2052335cdaf439e1c27c31`. Both native jobs
in [run38188276268](https://github.com/de41tr/open-room-renderer/actions/runs/38188276268)
passed1590 default tests, seven libmpv cases and two actual Unix IPC cases,
including observed seek positions before HTTP/SOAP replies.

## AP13 active DSP restore checkpoint

A bounded, versioned active-state file now stores the complete confirmed EQ
curve, plugin/source/name, bypass and balance. Startup replays the complete
graph before exposing active settings. Deferred load/resume and output-enabling
gain reconcile desired DSP before audio can resume; explicit mute/zero remain
accessible if a saved graph cannot be restored. Corrupt/unknown saved state
fails closed instead of silently selecting flat output.

Independent review found and resolved four real gaps: unmute after a failed
restore, stale uncertainty after successful backend replay, malformed legacy
balance overriding valid complete state, and invalid preset names committing
before validation. Library-only edits and pre/post-replacement storage failures
remain distinct from actual confirmed audio settings.

Exact snapshot rebased on the native-install checkpoint: 1692 passed with all
eight optional libmpv cases enabled; two native Unix IPC cases skipped locally.
Independent AP13-only review passed1658 default and401 focused tests. Digital
PCM from fresh Config/API/backend/native handles reproduced the original
-12dB cut plus half-channel balance (-18.059dB normalized attenuation), including
deferred first-load restore. Restored bypass retained balance and the cached
curve/name/source. These are digital sample tests, not acoustic or systemd
reboot acceptance. The new restore case is added to both native CI jobs.

AP14 is published as `324469c1a8afb72b277735e268e59196edcc6b10`; its
[run38188925897](https://github.com/de41tr/open-room-renderer/actions/runs/38188925897)
passed both architectures, including the new real installed-package/identity/
manifest-uninstall step. This establishes native AArch64 installation evidence,
not ARMv7, hardware or systemd boot.

## AP15 native radio formats and playlist ownership

First-level M3U/PLS now play through the normal mpv backend rather than returning
a false failure when mpv expands a container URL into a different child URL.
Exact native container provenance plus the sole current/playing entry establishes
ownership; stale, foreign or inconsistent snapshots still reject. The selected
container remains one canonical queue item while native entries advance inside
it. Only final container EOF advances the renderer's top-level queue.

Native entry loading reports BUFFERING even when old time/duration remain.
Pause/resume verify actual source identity and boolean state before claiming
completion; seek additionally freezes the actual child/entry identity.

Generated local fixtures passed real libmpv 0.40 decoding for MP3, ADTS AAC,
application/octet-stream media, unencrypted HLS segment crossing, first-level
M3U/PLS, redirects/loop failure and ICY metadata. Two-entry container transitions,
HTTP/SOAP state and final top-level queue advance passed. Exact isolated snapshot:
1714 passed with all 12 real libmpv cases enabled; two Unix IPC cases skipped
locally. Independent review and delayed-entry native probes confirmed ownership
and buffering fixes. FFmpeg is a test-fixture dependency, not a new renderer
runtime dependency; CI now includes these format cases.

Nested playlists remain unsupported: mpv reports only immediate ancestry, so
strict readiness may fail while an inner decoder is active. Do not loosen the
ownership guard or call this arbitrary-playlist compatibility. Owner-fenced
cleanup of unready loads is a follow-on in progress. No WAN/provider/hardware
audio or original Android claim follows from these local tests.

AP13 is published as `4b2f1d6f112b9ed8be2aa97261db2344068139eb`; both jobs in
[run38189242029](https://github.com/de41tr/open-room-renderer/actions/runs/38189242029)
passed1684 default tests, eight real libmpv cases, two production Unix IPC cases
and actual native installer/import/identity/uninstall, including AArch64.

## Media URL diagnostics no longer expose arbitrary query values

Playback URLs/options are unchanged, but normal source/readiness/proxy/SOAP
and backend diagnostics now sanitize credentials, complete query strings and
fragments. Ambiguous encoded or JSON-escaped URL wrappers are masked as a whole
rather than decoded into new delimiters. The final log formatter also sanitizes
access logs and tracebacks. Repeated sanitization preserves the private-address
placeholder instead of mislabeling ordinary LAN sources as invalid.

Review reproduced and fixed escaped-userinfo delimiters, apostrophes, mixed
encoding, HTML-entity ambiguity and double-formatting. Focused tests, an
independent bounded edge corpus and concurrent formatter checks passed. Exact
rebased publication snapshot: 1742 passed with12 real libmpv cases enabled,
two local Unix IPC skips. This is media-URL hygiene, not general-purpose DLP;
resource paths and arbitrary free text are not guaranteed secret-free.

AP15 is published as `9f4a1d5e7ca4f9dd8c96c25fa2b1c722847d2cca`;
[run38189809370](https://github.com/de41tr/open-room-renderer/actions/runs/38189809370)
passed both native architectures, including the new real format/container lane.

## Native mpv load-option compatibility

Per-file options now use mpv's named command map instead of positional arguments.
This avoids the insertion-index argument added in mpv0.38 without guessing a
version or retrying a load that might already have applied. Ordinary loads stay
unchanged; failed option loads do not unpause or issue duplicate loads.

Exact isolated snapshot: 1753 passed with13 real libmpv cases enabled, three
production Unix IPC cases skipped locally. Independent review confirmed the
public0.37 named-map contract and reran actual0.40 option application: start8
reached about8 seconds, demuxer/seekability/cache applied, gain persisted, and
all per-file options reset on ordinary replacement. Both an explicit libmpv
case and a new production IPC case now run in CI. Actual 0.37 execution awaits
this revision's result; this does not certify bitrate-derived range timing.

Log privacy is published as `de8b9b03d219e50734ff95295c307d34abf7f945`;
[run38190231373](https://github.com/de41tr/open-room-renderer/actions/runs/38190231373)
passed both native architectures and all existing lanes.

## AP16 clocked transition measurement prepared

Added an explicit native measurement using a private PulseAudio null sink and
its continuously clocked48kHz monitor. Unlike concatenated fast PCM files, this
sink records silent frames during actual idle gaps. The probe calibrates a known
125ms gap, compares direct mpv playlist playback, then measures real renderer
queues with equal/mixed source rates and a pause near EOF. Duration, queue
metadata, actual sources and delivered GENA are checked. Results distinguish
measured silence, non-silent boundary ambiguity and a stated2ms tolerance.

No production playback behavior or gapless capability is changed. Private
config/socket/cookie paths and generated low-level audio keep it off host audio
devices/accounts; all child processes have bounded cleanup and a hard group
timeout. Independent review checked clock/calibration, cleanup, and right-channel
sequence validation. Local default snapshot: 1757 passed,17 explicit native
skips. Native clocked execution is NOT_RUN locally because the required private
server/executables are unavailable here. CI adds official PulseAudio utilities
and runs the explicit lane; only its actual result can establish a transition
measurement. This is not a physical DAC or general gapless certification.

Named load options are published as `622d5ca9c21c8561e6ab81a544568878d683497d`;
[run38190695731](https://github.com/de41tr/open-room-renderer/actions/runs/38190695731)
passed both architectures, including the new native0.37 production options case.

## Clocked measurement calibration repair

The first native clocked run38191121333 failed calibration on both architectures
(first marker11400/11793 frames versus12000). Existing playback, native IPC and
installation lanes passed. No renderer gap measurement was established.
The fixture now isolates its HOME/cookie, disables null-sink rewinds as an
experimental control, drains independently clocked frames to a quiet tail and
reports bounded aggregate diagnostics. Rewinds are not a proven cause. Strict
marker counts,125ms calibration and2ms tolerance are unchanged. The exact
repair snapshot passed 1759 default tests with17 explicit native skips. Native
retry remains pending; do not describe this as gapless playback verification.

## Failed source loads stop only their own active decoder

A failed/unready load or cancellation now performs bounded checked Stop and
actual idle confirmation when that load may have reached the backend. Resolver
failures before any load leave old playback untouched. Newer source owners are
fenced; physical cleanup remains eligible if the same generation's recovery
token is invalidated or shutdown begins. Failed Stop/readback preserves explicit
backend-control uncertainty instead of claiming silence or retrying the source.

An actual libmpv nested-M3U fixture demonstrates the original problem: the inner
MP3 is decoding while outer-source readiness fails. With cleanup the HTTP 502
returns only after confirmed idle and cleared actual identity; a subsequent
valid source plays. Nested playlist ancestry is still unsupported. Independent
review covered observer-error, shutdown and newer-owner interleavings.

## Explicit discovery interface and real discovered-client flow

Optional discovery_interface selects a literal local IPv4 address for SSDP and
both mDNS implementations. It controls multicast membership and outbound traffic;
on Linux, owned receive sockets disable all-interface membership widening. Generic
zeroconf startup verifies its socket layout and fails closed if explicit isolation
cannot be established. Default routing is unchanged. Startup/cancellation errors
close partially acquired sockets and await tasks. No host routes/firewalls change.

Actual Linux loopback multicast discovery passed locally: SSDP M-SEARCH and alive,
custom mDNS PTR/SRV/TXT/A, two independent identities and HTTP descriptions, plus
zeroconf receive isolation. Group-address-bound client sockets reject a deliberate
unicast-only mutation. This establishes multicast wire behavior on one host, not
physical LAN or Android compatibility.

A separate explicit native CI flow discovers its endpoint before fetching the
description, then uses production mpv Unix IPC with two different generated WAV
files. It checks play/pause/resume/Next/volume/mute/unmute/Stop and consistent
HTTP/SOAP/ordered GENA. That combined native flow remains NOT_RUN locally because
Unix socket creation is restricted; its CI result must be recorded separately.

Exact combined cleanup/discovery snapshot: 1849 passed with real libmpv and
multicast discovery enabled; five explicit production-IPC/clocked/combined-flow
cases skipped locally. All code is synthetic or independently implemented; no
physical LAN/audio or original Android claim follows from these checks.

Clocked retry run38192155611 passed all preexisting lanes, but the new optional
audio-parameter diagnostic raced lazy output creation before calibration. A
bounded optional-metadata sampler now records unavailable values without
bypassing any strict PCM checks (22 focused tests passed, one native skip).
The next native run must establish calibration and renderer measurements.

## Native discovery/playback and clocked transition result

Head eb7c2ac9f3718ec80ef2efd05f1e4a4e0eeddcf6 passed all lanes on native
x86_64 and AArch64 in
[run38192653073](https://github.com/de41tr/open-room-renderer/actions/runs/38192653073).
Actual multicast discovery led to fetched control/event URLs, two distinct WAV
resources and real production Unix-IPC play/pause/resume/Next/volume/mute/unmute/
Stop, with HTTP/SOAP/GENA agreement and complete child/task/listener cleanup.
The new nested-playlist cleanup also passed with actual mpv0.37 decoding.

The continuously clocked digital probe now calibrates the known125ms silence
exactly and preserves all96000 frames per track. Equal-rate ORR boundary gaps
were6.125ms on x86_64 and25.125ms on AArch64; mixed-rate gaps6.8125/25.333ms;
pause-near-transition gaps4.9375/26.125ms. Direct native equal-rate mpv playlist
measured0ms on both. These are bounded same-host measurements with2ms stated
tolerance, not a distribution of repeated results or physical DAC evidence.
All ORR cases are GAP_OBSERVED; passing the measurement does not mean gapless.
Observer phase and native-EOF versus sink timing are being investigated
before attributing the gap or changing queue loading.

## Experimental service honesty

The unfinished Plex scaffolding no longer advertises active/configurable
playback, even when explicitly selected for diagnostics. Its inactive shape is
retained, but playable service lists and legacy support masks exclude Plex.
All protocol capability projections agree. This does not implement or exercise
account login. The README no longer suggests another application's OAuth
callback or a manual token-file recipe for the unsupported integration.

Final experimental-service snapshot: 1860 default tests passed,22 explicit
native skips. Independent review passed109 focused tests and confirmed27
ordinary neutral/TuneIn/direct-source configurations match baseline. Reserved
provider source aliases and padded legacy decimal masks are covered.

## Established live stations recover from no-EOF cache stalls

A real held-open local response reproduced an indefinite BUFFERING state: audio
reached9.984s, then paused-for-cache/core-idle stayed true without EOF. The
monitor now requires15 seconds of unchanged decoded position for an established
explicit live station before using the existing three-attempt retry budget.
Transient return or any decoded progress resets the grace; fresh snapshots
recheck that evidence before a queued retry or terminal Stop. Initial loads,
finite files, unknown sources and owner Pause/Stop remain separate.

At exhaustion a stalled socket is stopped and actual idle confirmed, so delayed
network bytes cannot silently restart audio after ERROR. Cleanup remains bounded
and same-owner-fenced through shutdown and newer source selection. Failed Stop
or readback reports silence unconfirmed rather than claiming a stopped decoder.
No gain, mute, canonical selection or generation is changed by successful retry.

Independent review reproduced and fixed a backoff race where resumed progress
previously failed to reset the queued deadline. The original negative controls
now pass unchanged. Native tests use generated held-open HTTP media and real
libmpv/null; production15-second recovery, transient return, owner commands,
finite exclusion and exhausted retries are separate scenarios. This is not
provider-URL refresh, physical WAN/router testing or hardware playback proof.

Final rebased snapshot: 1937 passed with all optional libmpv cases enabled;
eight explicit native IPC/discovery/clocked cases skipped locally. Independent
review cleared the unchanged adversarial repros and production15-second grace.
The seven actual no-EOF scenarios are now in the native CI libmpv lane; monitor
that revision separately. Prior21f1a017 CI passed all lanes on both architectures.

## Native probe lifecycle and optional distribution gate

Independent review reproduced and fixed detached child leaks on termination in
acceptance supervisors. Shared tool-only supervision now handles signals, timeouts
and abnormal worker exit, with deterministic and actual-process checks.
The Snapcast experiment remains standalone and optional. Real official binaries
passed seven PCM/control stages with verified ownership and live source/group
state; the prepared production Unix-IPC mode awaits native CI. No service
capability, hardware or two-host synchronization claim was added.

Exact rebased helper/tool snapshot:1955 default tests passed,29 explicit native
skips. Separate local standalone acceptance and independent signal-kill checks
passed. Current production head e62daf23 passed all native CI lanes on x86_64
and AArch64, including all seven no-EOF station scenarios:
[run38194471807](https://github.com/de41tr/open-room-renderer/actions/runs/38194471807).
Actual production-grace recovery took16.04/16.03 seconds on those runners;
exhaustion used exactly three retries and confirmed idle.

## Experimental native successor staging (default off)

`native_queue_staging: true` opts into one-successor mpv playlist staging for
confirmed finite direct playback. The default is false in every profile; strings
are rejected as flag values. Gain, DSP and mpv audio defaults are unchanged.
Append acknowledgement alone never changes canonical metadata: an owned native
entry ID must actually become current/playing before queue adoption. Duplicate
URLs, delayed acknowledgement, Pause/Resume/seek and manual Next/Previous around
a native transition are fenced by ownership and confirmed state.

Active native M3U/PLS containers are not staged further. A successor with exact
first-level container ancestry can be adopted as one canonical item. Nested or
foreign-tail ancestry that cannot be proved is explicitly unsupported: ERROR
reports silence not confirmed, does not falsely adopt the source, and does not
Stop an unproved foreign source. Such audio may remain active until an explicit
owner control succeeds. This opt-in is not blanket gapless certification.

Independent review reproduced and fixed lost-ACK-after-EOF, invalid-seek pause
restoration, stale source-resolution ownership and gain-error adoption races.
The exact rebased snapshot passed2024 tests with real libmpv enabled; eight
explicit native IPC/discovery/clocked cases skipped locally. Native/null tests
confirm entry ownership, cleanup, navigation and unchanged gain/DSP defaults.
A supplementary private clocked diagnostic measured equal-rate128.75ms off
versus0ms on at a0.35s monitor-phase offset, with96000 frames per track. Mixed
rate still measured9.083ms. This is not production Unix-IPC acceptance.

The native clocked CI lane now compares off/on equal and mixed rates at the
phase offset, plus staged pause near EOF. Exact sample counts remain required;
only staged equal-rate playback has a hard2ms acceptance gate. Mixed-rate gaps
are reported honestly. The actual native results are recorded below.

Native production Unix-IPC measurements in [run38196685851](https://github.com/de41tr/open-room-renderer/actions/runs/38196685851)
passed the staged equal-rate gate on both architectures: phase-offset unstaged
168.354ms (x86_64) / 186.979ms (AArch64) became 0ms with staging. Staged pause
also measured 0ms; mixed-rate remained 8.25ms / 5.9375ms. Each track retained
exactly 96000 frames and the known 125ms silence calibration remained exact.
This is clocked digital output, not physical audio. Staging stays default off.
Every renderer lane passed; the separate direct-FIFO Snapcast experiment failed.

## Public-station address renewal

Successfully resolved anonymous station references now carry internal, URL-bound
provenance. Recovery and a later explicit Play refresh an expired stream address
through that same fixed public resolver, retaining the requested station, queue
identity, metadata and gain. Client-supplied labels, IDs and XML cannot grant
resolver authority. Initial materialization resolves once; explicit finite
tracks and ordinary URLs retain their existing behavior. Typed station results
are deliberately not native-staged as finite successors.

Fresh progress is checked again at the serialized load boundary; a deferral
refunds retry budget. Terminal recovery failure confirms same-owner Stop/idle
or reports uncertainty, including EOF/idle cases that began buffering again.
Independent review covered staging off/on, finite-to-station transitions and
foreign tails. The exact current-base snapshot passed 2081 tests with optional
libmpv enabled, with eight environment-specific native cases skipped. Controlled
local resolver/media tests prove expired address A is replaced with B; this is
not a live-provider availability or Android acceptance claim.

## Bounded repeated-playback stability

`ORR_TEST_UNIX_STABILITY=1 PYTHONPATH=. .venv/bin/pytest -q -s tests/test_mpv_unix_stability.py`
opts into unmodified production Unix IPC over three owned mpv lifetimes (180s
by default, bounded to 180..900s). The shared flow exercises HTTP/SOAP/GENA,
actual source changes, controls, callback retries, station interruptions and
independent Stop/source-replacement recovery fences. Native events independently
count loads and EOF; controller and child resources are measured separately.
Linux PPid enumeration has a real disposable-child sensitivity test. Opt-in
fails when Unix IPC is unavailable; it never silently substitutes libmpv.

The separately opt-in libmpv lane uses `ORR_TEST_LIBMPV_STABILITY=1`,
`ORR_TEST_LIBMPV_PATH` and `ORR_TEST_LIBMPV_STABILITY_SECONDS` (recommended 60..900; allowed 30..3600) with
`tests/test_libmpv_stability.py`. At production checkpoint 7c20ef8, the final
reviewed short lane passed in 88.258s: 24 cycles, 60 loads, 52 source changes,
121 cross-surface agreements and six callback retries held through actual gain
readback. All three cleanups returned to one task, seven FDs, one thread and
zero children/subscriptions. A separately frozen 907.402s run at older production
checkpoint eb7c2ac9 covered 367 cycles and 746 loads; it is not evidence for
later changes or the final strengthened counter assertions. Repeated in-process
libmpv creation retained allocator memory; these bounded budgets do not prove
leak freedom or uninterrupted-stream endurance. The new production Unix stress
lane awaits CI. Actual pending-retry Pause cancellation remains separately
covered by libmpv recovery, not this Unix stress flow.

## Snapcast production-IPC ingress correction

The first native Snapcast gate failed HTTP Play on both architectures: direct
untimed mpv PCM/FIFO backpressure blocked property reads beyond production's
existing deadline. Renderer control timeouts and readbacks are unchanged.
`tools/acceptance_snapcast.py --orr-ipc` now defaults to a private clocked Pulse
null sink and monitor feeding the private Snapcast FIFO. It needs pulseaudio,
parec and pactl, uses its own runtime/server/sink and supervised process group,
and neither discovers host audio nor installs/enables a service. `--ingress=fifo`
retains the original diagnostic path; standalone mode still defaults to FIFO.

Independent real-libmpv diagnostic comparisons passed all eleven control/PCM
stages, and the original standalone FIFO passed seven. Native production Unix
IPC with the distribution's Snapcast version still awaits CI. Spectral checks
use independent short windows with an 80% window tolerance plus unchanged RMS
and gain requirements; this is not sample-perfect continuity or clock-skew
measurement. Mute still requires exact-zero PCM. Upstream silent-source status
may be playing or idle only with independent native identity/control/progress
proof; group mute still requires an active playing source. Killed-worker,
recorder-failure and termination tests verified bounded owned-process cleanup.
No two-host synchronization or advertised service capability is claimed.

Combined publication snapshot: 2082 default tests passed / 36 explicit opt-in
skips; 46 focused native-refresh/staging/Snapcast/helper tests passed. The
refreshed production snapshot also passed the separately enabled short libmpv
stability run (four tests, 88.69s). Production Unix IPC, architecture-specific
behavior and the clocked Snapcast path require the new native CI run.


## Real-client navigation and native compatibility gate prepared

Actual pywiim 2.3.9 next_track/previous_track use HTTP next/prev. Those exact
commands now reuse the existing SOAP navigation implementation, preserving
owner/gain/staging/readback semantics and single-track Previous restart. Empty
queues, boundaries, failed loads and malformed command suffixes remain failures.
Focused real-client/mock, parser, queue and native-staging regressions passed 97
cases, including paused restart and native-successor index reconciliation.

A separate opt-in real-client/production-Unix-IPC gate uses two distinct generated
WAVs, exact loopback HTTP targets and null audio. It checks actual native resource,
decoded time and gain alongside exact parsed/raw HTTP and delivered ordered GENA.
The encoded-separator/synthetic-signature resource must arrive unchanged. Client
controls are genuine public methods; SOAP only creates the queue. Explicit opt-in
fails on missing dependencies or IPC, with bounded whole-process supervision.

Production Unix IPC remains NOT_RUN locally (AF_UNIX EPERM). A separately labeled
real pywiim 2.3.9/libmpv 0.40 diagnostic passed all controls, actual readbacks, raw
media-target checks and cleanup with both navigation and URI-preservation fixes.
That is not the production Unix transport, original Android app or hardware proof.
The new native CI gate still requires its own result.

## Verified native checkpoint b011557

[Run38198310911](https://github.com/de41tr/open-room-renderer/actions/runs/38198310911)
passed every lane on Ubuntu x86_64 and AArch64. The production Unix-IPC stress
ran 185.745s / 186.315s across three owned mpv lifetimes per architecture:
132 / 138 native loads, 124 / 130 source changes, 301 / 316 HTTP/SOAP/GENA
agreements, and six independently observed EOFs each. Every lifetime reaped
its child, removed its Unix socket and closed event observers/listeners;
controller cleanup returned to one task, seven FDs, one thread and zero children.
Child resource counts were separate; this is bounded stress, not leak freedom.

The repaired private clocked Snapcast ingress passed all eleven stages using
production Unix IPC with mpv0.37, PulseAudio16.1 and Snapcast0.27 on both runners.
Two file-output clients demonstrated distribution, join/leave/rejoin, per-client
non-atomic group gain, exact-zero group/upstream mute, reconnect retention and
HTTP Stop draining to digital silence. All owned children were reaped and
checked ports closed. This replaces the initial direct-FIFO gate failure;
it does not establish two-host synchronization, physical audio, vendor grouping
or an advertised renderer service. Station-address refresh and all previous
renderer lanes also passed at this exact checkpoint.

## Owner intent and real-client compatibility corrections

An applied native Stop with a lost acknowledgement previously restored old play
intent, so its idle observation could auto-start the next finite track. Rejected,
unconfirmed or cancelled Stop/Pause/Resume now retain latest owner intent and
report explicit ERROR; unconfirmed silence is never described as successful Stop.
Retired native output remains fenced across cancellation and newer commands.
Only a new owner command can recover playback, with checked Stop/reload when
native ownership is uncertain. Independent real libmpv/null HTTP/SOAP/GENA tests
proved no automatic second track and explicit Play recovery of the selection.

Real pywiim HTTP Next/Previous now share existing SOAP navigation semantics.
Already decoded absolute media URIs retain encoded path separators and signed-
query bytes; only a fully quoted outer URI wrapper gets one additional decode.
The real client and decoder reached an exact local target containing `%2F`,
`%26`, `%3D`, `%252F` and UTF-8 escapes. This fixes an actual wrong-resource load,
not merely display text. Malformed navigation suffixes still fail without mutation.

CLI commands return nonzero on HTTP errors, redirects and network failures;
errors omit media URLs/response bodies, and redirects are not followed. The
reviewed installed-entry lifecycle gate checks actual serve/restart, persisted
identity/gain/DSP, signals and owned/external process boundaries. The new actual
pywiim/Unix-IPC gate verifies real-client controls and exact media resources.
These two production-native additions require their own CI result; local native
transport remains restricted. The combined default snapshot passed 2190 tests
with 41 explicit opt-in skips. With the real-libmpv option enabled, the same
combined regression suite passed 2218 tests with 13 separate opt-in native cases
skipped in 291.85s. Installed CLI/actual-client production IPC still await CI.

## Verified native checkpoint 9f5f529 and Python floor

[Run38200058359](https://github.com/de41tr/open-room-renderer/actions/runs/38200058359)
passed every lane on x86_64 and AArch64. The actual pywiim2.3.9 client used
production Unix IPC with mpv0.37, selected two distinct generated resources,
and delivered the exact encoded media path/query unchanged. Parsed client,
canonical HTTP, native readback and ordered GENA agreed for play, pause, resume,
Next/Previous, volume, mute/unmute and Stop. The null-output gate checks actual
gain settings, not audible gain. Its fixture child and all owned tasks/listeners
were cleaned up.

The installed CLI passed owned/external mpv scenarios on both architectures,
with SIGTERM and SIGINT across same-port/socket restarts. UUID, name, volume,
mute and balance survived restart; actual native state was restored even after
the surviving external player was deliberately changed. Owned children/sockets
were removed, while the external peer was preserved until fixture teardown.
These two scenarios took 7.97s / 10.23s, leaving the complete jobs below eleven
minutes. This is actual installed process acceptance, not systemd boot or hardware.

The exact 9f5f529 source also passed an isolated CPython3.11.17/x86_64 run:
2190 default tests /41 gated skips, plus six actual libmpv 0.40/null or digital-PCM
cases and one supporting encoding unit test. Controls, seek/queue, gain/mute,
load options, native staging and uncertain owner controls were exercised.
Official-PyPI dependencies passed a 35-package compatibility check. The runtime
came from uv's official Astral CPython distribution; independent archive checksum
verification was unavailable, and no security/newest-version certification is
claimed. This does not prove ARMv7, Debian bookworm's own package combination or
all Python patch/dependency versions. Local Unix sockets remained restricted.

## Complete public-client playlist resources

A real pywiim play_playlist request was previously split at URI colons, including
ports or a legitimate final `:3`. A reproduced request for an AAC playlist could
therefore decode a different MP3 playlist and still report success. Complete
unindexed URIs now remain intact, including escaped path/signature values.

Only a fully quoted inner URI followed by a literal colon has a distinguishable
legacy index. Exact index0 is this renderer's supported start-at-first form;
other or empty indices fault before native Stop, queue replacement, generation
changes or events. This is separate from the canonical queue's one-based index.
Candidate queue validation also precedes existing native-stage interruption.
Independent native tests proved both the intended AAC resource and rejection
without disturbing paused staged playback. Local verification: 2203 default
passes /41 skips, plus98 focused/native-format cases in the publication snapshot.
Production-native results for this new revision must be checked separately.

A separate standard x86_64 Python3.11 default-suite CI job now protects the
declared runtime floor. It uses verified immutable first-party checkout/setup
actions, official-PyPI dependencies, no dependency cache, no native package
installation, and an eight-minute whole-job cap. Existing native jobs and their
fifteen-minute caps are unchanged. The first hosted floor result is pending.

The playlist publication snapshot also passed the full default suite on actual
CPython3.11.17: 2203 passed /41 gated skips in65.16s. The selected Python3.11
real-libmpv evidence above remains tied to checkpoint9f5f529.

## Live-station history reduction and extended native checkpoint

The preceding [60d6b0a checkpoint](https://github.com/de41tr/open-room-renderer/actions/runs/38201708621)
passed both native architectures and the new hosted Python 3.11.17 default job.
Its real public-client playlist tests now decode the complete intended M3U/PLS
resource, including literal colons and escaped query bytes.

Only the matching canonical zero-duration station now receives per-file zero
backward packet history. Existing native forward buffering, cache/pause settings,
recovery timing, gain and DSP remain unchanged. Finite and unknown media restore
their previous native defaults. This is supported by the documented
[mpv 0.35.1 backward-cache option](https://github.com/mpv-player/mpv/blob/v0.35.1/DOCS/man/options.rst)
and [per-file load options](https://github.com/mpv-player/mpv/blob/v0.35.1/DOCS/man/input.rst),
as well as the reviewed 0.37 contract; documentation is not a 0.35 runtime test.

The original frozen b011557 uninterrupted-station attempt is FAILED endurance:
its test exited after 3299.25s overall, before the requested hour completed.
RSS grew 49,160 KiB against the unchanged 49,152 KiB allowance; other progress,
protocol and resource checks had not failed. Cleanup completed. Paired 300s
measurements attribute sustained growth to native backward packet caching and
allocator retention, not an accumulating Python object set. Do not relabel this
failed run as an hour pass or as proof of a Python leak.

A separate frozen production-policy run passed 300.09s with one load/request,
300.053s decoded progression, RSS growth 84 KiB and essentially flat 22 KiB packet
cache. Original budgets, state/progress assertions and cleanup gates were retained;
there was no diagnostic global override. A new immutable hour run began at
04:03:22 UTC and remains pending, with no result expected before 05:03:23 UTC.
Local combined regression passed 2249 tests with real libmpv enabled and 13
separate native opt-ins skipped in 303.89s. The added production Unix-IPC flow
also reads the actual station option and finite-load reset; it awaits CI here.

One extended CI checkpoint now requests 900 seconds of production Unix-IPC
stress on the same two standard public runners. The stress step is capped at
18 minutes and each native job at 30 minutes; the Python floor job stays at
8 minutes. Permissions, assertions and resource budgets are unchanged. After
recording its terminal result, restore normal 180-second stress, 6-minute step
and 15-minute native-job caps. This is not a permanent CI-duration increase.

## Extended production IPC and cleanup checkpoint

[4ef68e4 completed all three jobs](https://github.com/de41tr/open-room-renderer/actions/runs/38203316362).
Production Unix-IPC stress passed 905.076s on x86_64 and 906.365s on AArch64,
with 684/706 loads, 676/698 source changes and 1681/1736 protocol agreements.
Each architecture exercised three real mpv lifetimes. Every teardown returned to
zero children, seven file descriptors, one task and one thread. Separate native
child resource budgets passed. This is bounded repeated-lifetime evidence, not
an uninterrupted-station hour or indefinite leak freedom.

Actual native readback confirms zero backward history for the direct station,
unchanged forward options and restored finite defaults. Genuine pywiim,
installed CLI SIGINT/SIGTERM persistence, discovery-to-distinct-source playback,
clocked digital staging and private same-host Snapcast also passed. Equal-rate
staging again measured 0ms; mixed-rate staged gaps were 8.875ms on x86_64 and
5.9375ms on AArch64. No physical-audio or two-host synchronization claim follows.
Normal CI is restored to 180-second stress, six-minute stress step and fifteen-
minute native jobs; the Python3.11 floor remains eight minutes.

A reproduced mDNS unregister exception previously left two actual owned sockets
and selector readers open. Bounded finally-close now removes them on unregister
error or cancellation; failed close retains the handle for a later retry.
Local real Zeroconf resources with only unregister failure injected pass both
cases. Independently, direct SIGHUP could terminate the actual CLI while leaving
its synthetic startup child alive. SIGHUP now takes the existing graceful-stop
path, reaps owned startup children and closes active HTTP ports. Ordinary startup
errors still exit nonzero. These signal reproductions are MOCK playback; the
added real-mpv owned/external SIGHUP cycle awaits this revision's native CI.

A new paced ICY diagnostic found that native title updates can disappear with
zero station backward history while the same stream with native defaults exposes
all changing titles through HTTP/SOAP/GENA. At checkpoint 8d643657 this interaction was under investigation, and the prepared
playlist-child extension was held. The correction and extension below supersede
that earlier state. The separately frozen direct
hour does not test changing ICY metadata or establish playlist endurance.

Combined cleanup regression: 2233 default tests passed /44 gated skips; Python3.11
focused lifecycle/CLI checks passed 142 with 2 native-only skips. Separately, 28
focused mDNS/actual-loopback tests passed on Python3.11. Native SIGHUP remains
pending its own hosted result.

## Bounded history for native playlist station children

M3U/PLS children do not inherit their container's per-load options in tested
mpv 0.40. The current corrected policy uses a finite 1 MiB backward cap and
disables forward-buffer donation so native timed ICY updates remain available.
The previous zero-history measurements and frozen direct-hour run remain
historical evidence for their exact snapshots; they are not evidence for this
finite policy or the playlist-child path.

Proven first-level station children now receive the same shared policy through
bounded existing observer/readiness work. Each of the two file-local writes is
independently guarded in mpv by process and exact active-entry identity. Both
values and local flags require guarded readback; partial/failed application is
unconfirmed and warns once. Natural advancement, owner/source changes, a native
replacement between the writes and process restart with reused entry IDs are
covered. Finite-source defaults restore at native file boundaries. No global
configuration, native script, playlist expansion or additional monitor is used.
See [test scope and native opt-ins](docs/TESTING.md#native-station-playlist-child-history).

## Metadata-safe history and native cleanup results

The direct station policy now keeps 1 MiB of backward packets and disables unused
forward-buffer donation per file. Zero history had removed native timed ICY
updates; a positive cap alone could still borrow the large forward allocation.
The paired correction restores updates without changing forward capacity/time,
cache/pause behavior, canonical metadata, gain or recovery timing. Shared policy
constants prevent direct and proven first-level child loads from diverging.

Local mpv0.40 streamed 12 changing ICY titles through one source/load over 70s.
All changes reached native and renderer metadata. Backward retention plateaued
at 1,047,872 bytes for the final 20s; forward retention was 16,640–40,768 bytes and
RSS spread 20,480 bytes in that window. Separate fallback, explicit-title and
opaque-DIDL cases preserve the intended HTTP/SOAP/GENA contract. These bounded
measurements do not replace the new corrected-policy hour or native0.37 CI.

[8d643657 CI](https://github.com/de41tr/open-room-renderer/actions/runs/38205184583)
verified actual installed owned/external CLI SIGHUP on both architectures:
three signal/restart cycles each, external peers preserved and owned children
reaped. Real Zeroconf error/cancellation injection closed both owned sockets and
selector readers on both architectures. Python3.11 and x86_64 fully passed.
ARM passed all renderer stages but failed the optional Snapcast group-gain PCM
check: fitted amplitude 249.69 matched expected 250, but only 5/7 short windows
passed the unchanged signal criterion. This remains a failed whole job; the
separate diagnostic work must not weaken the gate or imply two-host support.

A new native owned-player-death gate checks background failure projection before
status polling, no station retry/queue advance/respawn, unconfirmed preserved gain,
and explicit different-source playback after a fresh backend restart. Its
production Unix execution remains pending. The new uninterrupted-station harness
uses frozen/hashed source, one generated paced stream, unchanged observation and
resource gates, a hard outer deadline and verified cleanup. Existing frozen runs
retain separate provenance; no later source is credited with their duration.

The separately frozen zero-policy hour completed at 05:03:23 UTC: 3600.669s
baseline, 3600.589s native progress, one load/source/request, 362 protocol checks
and 7193 monitor observations. Peak RSS grew 1128 KiB; live FDs/tasks/threads stayed
12/5/10 and teardown returned to 7 FDs, 1 task, 1 thread, 0 children. All unchanged
gates and cleanup passed. Its production digest is
`f1cb53c37437134ec76c8eac37fed47c20e6a419f4cc0cd19b081a5f0882ee61` and source digest
`212bb4d04bd571193369d0faa35557e63533a62ad7718fbafbaed4460fa82d71`.
This proves memory/progress for that older direct zero-history snapshot only;
its ICY limitation remains, and corrected finite-policy endurance is separate.

The exact combined finite-policy/child/death/harness snapshot passed 2338 tests
with real libmpv enabled and 19 explicit native-only skips in 380.49s. Production
digest: `b3ae8a5c7ad01696b12be6feb52c16fe51fed9ad6128015992b6d14fd055b5ef`.
The new production Unix gates and final-policy hour remain separate results.

The separate corrected finite-policy hour is active from 05:05:34.826684 UTC,
with completion expected around 06:05:35 UTC plus bounded cleanup. It freezes
production digest `b3ae8a5c7ad01696b12be6feb52c16fe51fed9ad6128015992b6d14fd055b5ef`
and source digest `ff5f255e08cc54255cff69621c7633da1c604109208d07b389201fbceb1a2735`.
The same reviewed harness/gates apply, with no diagnostic override. This is
pending, not credited as a completed duration.

The Snapcast investigation adds bounded per-window amplitude/RMS/residual/phase
statistics, both-client capture boundaries and allowlisted event counts before
signal assertions. No gain/mute/source threshold was relaxed. Synthetic phase
jumps can reproduce the 5/7 failure shape with correct gain, but do not establish
the original ARM cause. Failure-injection checks still fail nonzero and clean
up children, ports and FIFO. The original ARM failure remains unresolved.

Final combined default suite: 2317 passed /57 explicit skips in 66.43s. Before the
Snapcast diagnostics-only addition, the combined finite-policy snapshot passed
2300 /57 on Python3.11 in 66.73s and 2338 /19 with real libmpv in 380.49s. The final
Snapcast/helper/supervisor checks separately passed 72 on Python3.11. Production
bytes and the active frozen final-policy hour are unchanged by those diagnostics.

## Production finite-policy verification and exact error contract

[ba7b3120 run](https://github.com/de41tr/open-room-renderer/actions/runs/38206823992)
passed 39 real libmpv cases on both native architectures, including changing ICY
and first-level playlist children. Five of six production Unix cases passed.
The single failure on each runner was the new owned-player-death test expecting
HTTP 503; the established renderer contract correctly returned HTTP 502 and plain
`Failed`. No production status behavior is changed to satisfy a test. The exact
expectation is corrected and a real-loopback/MOCK-backend regression now checks
HTTP 502, SOAP 500/fault 501, persistent ERROR and unconfirmed gain. The actual
fresh-restart portion after that failed assertion still needs its next native run.

Real pywiim, installed SIGTERM/SIGINT/SIGHUP cycles, discovery and all eleven
same-host Snapcast stages passed on both architectures. Production Unix stress
ran 185.793s /184.996s with 132/136 loads, 301/311 protocol agreements, and complete
owned cleanup. The older ARM Snapcast window failure remains unexplained;
later passes do not retroactively change it or establish two-host synchronization.
The active finite-policy hour's production bytes are unchanged by this test fix.

## Limited ARMv7 ABI and protocol execution

Checkpoint ba7b3120 also passed a separate INTEGRATION/EMULATED_ARMV7 experiment
under direct QEMU 7.2.22 with real Debian ARM hard-float CPython 3.11.2, 32-bit pointers
and `cpython-311-arm-linux-gnueabihf` ABI. The CLI/API/config modules and six actual
ELF32 ARM dependency extensions imported. aiohttp 3.14.5, requests 2.34.2,
zeroconf 0.151.5 and PyYAML 6.0 meet the declared runtime requirements. Persisted
UUID, generated XML and libyaml C parsing passed. Actual loopback HTTP/SOAP used
ARM aiohttp C parsers and correctly projected an explicitly MOCK/unavailable
backend as stop/ERROR with unconfirmed gain. No player or audio was exercised.

Debian Release signatures, package-index/archive hashes and 16 official-PyPI wheel
hashes were verified. Dependencies were privately extracted, not installed into
a venv. PRoot failed at forbidden ptrace; direct QEMU cannot launch the ordinary
ARM child subprocess chain here. No security workaround or installer shim was
used. Native ARMv7 installation, pip/entry-point/uninstall semantics, mpv/ALSA,
service reboot and Pi/XU4 hardware remain NOT_RUN. This reproduces no installer
bug and does not justify a production change or costly additional CI lane.

The exact test correction plus current documentation passed 2318 default tests
with 57 explicit native-only skips locally. Production digest remains
`b3ae8a5c7ad01696b12be6feb52c16fe51fed9ad6128015992b6d14fd055b5ef`; the active
frozen hour is unaffected. Full native death-to-restart acceptance is pending.

## Complete native death-to-restart checkpoint

[62b089ad passed all three CI jobs](https://github.com/de41tr/open-room-renderer/actions/runs/38208267462).
Both x86_64 and AArch64 passed 39 actual libmpv cases and all six production Unix
cases. The corrected owned-player-death gate now completes its whole sequence:
verified owned decoder SIGKILL, background HTTP/SOAP/GENA failure and unconfirmed
gain, no automatic retry/respawn, fresh backend starting idle, then explicit
playback of a different source with actual progress. Two owned children, socket
and monitor tasks were cleaned up per architecture. The earlier test failure
was retained and corrected to the existing 502 contract, not hidden by a status
change. All remaining discovery, installed-CLI and Snapcast stages also passed.

## Bounded changing-title endurance mode

The existing opt-in sustained harness now supports paced AAC+ICY and a bounded
per-title callback/metadata audit. There is no production behavior change.
Each unique title must reach native state and fresh acknowledged callbacks
before status polls, then agree across HTTP/SOAP, generated DIDL and queue XML.
Backward retention is measured separately from forward bytes; evidence storage
stays bounded. The ordinary constant-title mode and its original budgets remain.

Independent review and exact publication replay passed 240.821s with 30/30 title
epochs, one load/source/HTTP connection, 240.768s native progress and clean teardown.
Retained history stayed 1,047,520–1,048,496 bytes over 61 late samples; live RSS rose
1152 KiB, with FDs/tasks/threads/children flat at 12/5/10/0. Production digest remains
`b3ae8a5c7ad01696b12be6feb52c16fe51fed9ad6128015992b6d14fd055b5ef`; frozen source is
`b8c7c3828554d0d0bdd0cd04258d01537956d38a2a191b41d43879603f3fc2b4`.
Changing only the history allowance to zero fails the first title deadline in
about 6s. A four-hour run is planned separately after the current hour; no longer
duration is credited here. Default replay: 2322 passes /57 explicit skips.

A separate quiet-PCM DSP audit found stable above-Nyquist bypass through
16/44.1/48kHz changes, with balance isolation and saved-curve restoration. EQStat
confirms configuration, not every band's effective attenuation at every source
rate. This matches the existing documented sample-rate limit; no production
resampling or warning policy was invented. The audit does not certify broadband
music, positive boosts, arbitrary Q or other FFmpeg versions.

## Rolling live-HLS outage coverage

A new synthetic live-HLS publisher exposes a rolling five-segment window and
advancing media sequence without ENDLIST. Actual libmpv 0.40/null passed all five
cases in 76.65s in the publication snapshot: playlist outage/failed reload/return,
transient segment-only no-EOF stall/native return, and Pause/Stop/replacement
while explicitly observed retry backoff is pending. Canonical station identity,
HTTP/SOAP/GENA, selected native gain and cleanup agree. The default 15s stall grace
and 0.5/1/2s retry delays are unchanged. Segment return before the grace does not
cause a renderer reload. Owner cases do not claim every possible outage phase.

No production fix was needed; this validates existing standard-backend behavior.
The five cases join the existing native CI lane with its unchanged 15-minute
whole-job limit. Older-runtime hosted results remain pending separately. Public
output contains aggregate synthetic request counts/sequences rather than the
request trace. There is no WAN, DRM, adaptive-variant, measured acoustic-gain,
production-Unix HLS or hardware certification.

The final combined harness/HLS snapshot passed 2322 default tests with 62 explicit
opt-in skips in 66.66s. Production code remains unchanged; the current one-hour
run and future four-hour changing-title run retain separate frozen provenance.


## Ambiguous saved gain fails closed

A duplicate JSON field in local gain.json previously selected its last value:
for example mute true followed by false restored an unmuted desired setting,
or volume 0 followed by 100 restored maximum UI volume. Duplicate version could
also conceal an unsupported version. The loader now rejects all duplicate keys,
including escaped equivalents, before CLI backend construction, without rewriting
state. Valid version-1 files, absent-state defaults and the 4096-byte bound remain
unchanged. This is local corrupt-state handling; no remote exploit, startup
autoplay or acoustic output was demonstrated.

Independent review approved the narrow parser change. Baseline regressions
reproduced twelve failures; the exact patched default suite passed 2336 tests
with 62 explicit opt-in skips in 68.33s. Python3.11 gain/lifecycle/DSP/identity
checks passed 216 tests; independent valid-state differential and focused
startup checks also passed. The ongoing endurance runner stays on its immutable
pre-change production digest; do not silently credit later whole-source hashes.


## Reproduced native fixture assumptions

Both failures from 3e74b24 are now reproduced. mpv can expose a selected child
path before decoder time exists; a public on_load hold reproduces the original
readiness failure on both 0.37/0.40 and both playlist formats. The corrected
bounded helper pins native and renderer ownership before/after every clock read
and retains >0.25s genuine progress plus all original policy/reset/stale guards.
Invalid media and an actual Stop during readiness still fail.

For HLS segment 503s, native 0.37 reaches ordinary EOF after roughly 1.5s of cache
stall. The missing timestamp follows real teardown, not a property-read quirk.
The specifically no-EOF test now holds a real segment response body before
release and verifies unchanged position, open transport, no EOF, delivered body,
same native entry and resumed decoding. Other 503/reload/owner cases stay intact.
These are tests-only corrections, not changes to production recovery behavior.
Private official Ubuntu 0.37/FFmpeg6.1.1 packages reproduced the failure and the
corrected case; 0.40 also passes it. No system installation or security setting
was changed; this private library route is distinct from full hosted Ubuntu CI.


Exact integrated replay after gain and fixture corrections: 2363 default passes
/62 opt-in skips in 68.42s; actual private Ubuntu mpv0.37/FFmpeg6.1.1 passed the
five HLS and six child-cache cases plus 27 readiness guards (38 total) in 112.46s.
Actual mpv0.40/FFmpeg7.1.5 passed the eleven native cases in 111.15s. Both native
runs verified cleanup; standard hosted Ubuntu architecture acceptance remains
separate. Production digest after the persistence-only change is
`0f005051620620aa68f62a7d8e99e320af442dc1c35c80be0b67d617bb14a99b`.
The one-hour and active four-hour runs retain their original earlier digests.

## Corrected native mpv minimum

Actual official Debian mpv0.35.1 reproduces a supported-runtime gap: M3U/PLS
children decode, but both real HTTP Play requests fail honestly with 502 because
native playlist ancestry is unavailable. Other direct-source/gain/DSP and guarded
command checks can pass; that does not satisfy the complete playback contract.
Upstream [introduced the required playlist-path properties in mpv0.37](https://github.com/mpv-player/mpv/blob/v0.37.0/DOCS/interface-changes.rst).
The installer and prerequisite docs now require mpv >=0.37; strict source
ownership is unchanged. Boundary checks reject 0.35/0.36 and accept 0.37+.
The actual official 0.35.1 executable is rejected before an installation prefix
is created. Debian Bookworm's default package, including ARM builds, therefore
does not meet this floor; no OS, repository or system package was changed.

Final publication snapshot including the installer floor passed 2368 default
tests /62 explicit skips in 69.12s. Python3.11 gain/lifecycle/DSP/identity,
readiness and installer checks passed 274 tests in 4.28s. Hosted native checks
remain required for this new revision; routine CI limits are unchanged.


## Current compatibility work

Official Debian Python3.13.5 plus official-PyPI dependencies passed the application
suite. Its unmodified full suite exposed twelve installer-unit fixture failures
because the host lacks ensurepip; a minimal test-only fixture isolation preserves
explicit missing-venv/ensurepip negatives and yields 2369 default passes /62
opt-in skips. The real installer still rejects missing ensurepip before writing
its prefix. This is runtime/application evidence, not native installation or
audio acceptance. Python3.11 also passes all 32 corrected installer unit cases.

A separate actual-state audit reproduced unintended playback: valid SOAP/HTTP
Next or Previous always uses the play path even from stopped/paused queues.
The AVTransport contract requires successful navigation to preserve its prior
state. A narrow correction is in development: healthy stopped selection without
load, paused multi-item 701 with no mutation, existing playing/explicit Play
paths retained, and unchanged uncertainty/owner fences. The dynamic action list
also incorrectly advertises known-unusable Seek/navigation; a single shared
SOAP/LastChange projection is being developed. Neither change is claimed tested
or published yet. Static supported-action SCPDs will remain distinct.


The navigation review reproduced two transient-state gaps before integration:
paused seek confirmation reports BUFFERING, and an in-flight Stop reports
COMMAND_ACCEPTED. Phase-only navigation still resumed playback in both cases.
The reviewed candidate now honors retained pause/stop intent as well as phase;
independent red/green races prove the correction. The dynamic action list is
being matched to these same preconditions, including useful play-intent skips
while buffering/loading. No new checkpoint is published from these candidates.

A separate existing observation defect was reproduced: a coherent external
mpv replacement could leave stale PLAYING/source fields, and an unloaded paused
selection could import foreign time/title/play state. A bounded post-ready fix
is under review. It retains canonical selection and owner intent, invalidates
stale work, and reports silence-unconfirmed ERROR without automatically stopping
or reloading the foreign source. Loading-window causal ownership is explicitly
outside this correction; no whole-external-control guarantee is claimed.


## Navigation and dynamic-action publication checkpoint

Reviewed navigation now preserves stopped/paused owner intent, including delayed
Stop acknowledgement and paused seek confirmation. Stopped Next/Previous selects
a neighbor without resolving/loading; paused multi-item movement rejects 701
without mutation. Explicit Play and playing navigation keep their existing
behavior and native ownership fences. The canonical SOAP/LastChange action list
uses one live calculation, respects queue boundaries and explicit stations,
keeps finite unknown-duration Seek, and omits seek when source/intent readiness
is absent. Empty Pause fails before backend mutation. Static SCPDs are unchanged.

Exact integrated default suites passed 2534 tests /64 opt-in skips on Python3.12
(74.35s) and Python3.13 (73.66s); Python3.11 focused checks passed 315 in 8.49s.
Integrated native control/navigation/uncertainty replay passed five cases on
both actual libmpv 0.37 and 0.40, 19.57s each. The new navigation pair uses default
0.5s observation cadence and takes about 8s; it joins normal hosted CI without
changing its 15-minute cap. New hosted results remain pending publication.

The action list reflects canonical state and last confirmed backend identity.
The separately reproduced external-source observation and automatic-admission
gaps remain under review; this checkpoint does not claim they are repaired.
The frozen four-hour changing-ICY run has crossed one hour with all title epochs
agreeing, but its full-duration result and teardown remain pending after 10:06 UTC.


## Observed foreign sources and automatic admission are fenced

Coherent current native path/entry/parent observations now detect replacement of
an already confirmed source, including changed adopted entry IDs at the same URL.
The renderer preserves canonical selection/intent/evidence, reports ERROR with
silence unconfirmed, invalidates stale EOF/recovery/seek work and stops certifying
old gain. It does not automatically Stop, load, seek or reconcile foreign gain.
Unloaded stopped/paused selections ignore unrelated decoder media fields.

Review exposed and fixed automatic takeover at retry wake, resolver yields and
queued EOF admission. A coherent active non-EOF source after a recorded terminal
state blocks automatic advancement even when its URL repeats. Genuine idle/EOF
still advances. Terminal backend ERROR is also retained before and across these
admission waits; it cannot be cleared by an automatic restart. Old-generation,
partial/torn observations and valid first-level container children keep their
existing fences. Explicit checked Stop/Play recovery remains functional.

The exact combined snapshot passed 2602 default tests /65 skips on Python3.12
in 74.82s and Python3.13 in 73.94s; Python3.11 focused checks passed 435 in 5.35s.
Actual libmpv 0.37 control/navigation/ownership/recovery/stall cases passed 17 in
163.80s; 0.40 navigation/ownership/staging/uncertainty passed 6 in 40.30s. The new
ownership proof uses the unchanged 0.5s observer, records scheduled event content
before status reads and proves continued untouched foreign playback. This is
not actual callback-delivery or production-Unix ownership proof. The three new
native cases join existing CI with no timeout increase.

Production digest: `a90982dfd51a45b8f13d949d2cf363f0b587595c31cec7b450865ff93da758e9`. Earlier constant-hour and ongoing four-hour
radio evidence keep their original frozen digests. External replacement during
an already-admitted load/readiness window still requires separate causal proof;
this correction does not claim that window or all raw-mpv concurrency is solved.


## Admitted readiness retains owned failed-load cleanup

The 5d64bfb observer regression is retained as a failed checkpoint. Generic
backend observations no longer interpret a still-admitted readiness/cleanup
transaction as post-ready ownership loss. A generation-scoped marker is set only
at native load admission, cleared on successful readiness or transaction exit,
and cannot be cleared by an older generation. Backend-error observations remain
visible. Same-generation station retries are covered without relying on the
previous native-ready marker. No ancestry checks, readiness deadlines, Stop
confirmation or post-ready foreign-source fences are weakened.

Independent review passed 314 targeted cases including actual libmpv0.40 nested
cleanup, cancellation, metadata suppression and observations during cleanup.
Author replay also passed the actual existing cleanup case on 0.37. These checks
confirm silence after the rejected nested source and a later successful explicit
Play; nested playlists themselves remain unsupported. Root ACK identity and
foreign replacement during admitted readiness are not claimed fixed here.

## Atomic device rename and unambiguous EQ values

Device rename previously returned OK after a synthetic partial ENOSPC write,
leaving a full new live name but a truncated restart name. It now atomically
saves UTF-8 before live mutation, leaves old state intact on pre-replace failure,
and reports 502 if persistence or discovery refresh cannot be confirmed.
Post-replace durability failure and callback cancellation retain matching new
file/live state; a same-name retry refreshes again. Concurrent rename callbacks
are serialized. Stable UUID and ordinary Unicode rename/reload remain unchanged.
This is local HTTP/file evidence with synthetic faults, not power-loss, physical
multicast reception or original Android acceptance. Exact author suites passed
2662 /65 on Python3.12 and3.11; the new 60 cases passed on both.

EQ request and preset-library JSON now reject duplicate fields before choosing
any gain, plugin, curve or saved preset. Previously a nested -12 then +12 value
silently selected +12 and returned HTTP200. The rejection is whole-object/library,
including escaped duplicate keys; existing valid and legacy partial libraries
still load without rewrite or automatic application. Gain and active-DSP parsing
are unchanged. Eleven regressions fail on the prior code; one valid control
passes. Independent full verification passed 2614 /65, with 336 focused checks
on Python3.11 and all 15 shared EQ JSON command routes checked for no mutation.
This is UNIT/MOCK configuration evidence, not an acoustic gain claim.


## Earlier backend ERROR survives later readiness and cleanup

A separate older defect allowed a later successful readiness result to revive
PLAYING after a backend observer had already diagnosed terminal ERROR. A failed
readiness result could instead overwrite that cause with source-unready, also
when the backend error arrived while Stop cleanup awaited. The narrow correction
vetoes success after ERROR and avoids replacing an earlier terminal cause, while
still performing owned cleanup. Cleanup failure retains the stronger explicit
silence-unconfirmed diagnosis. Three regressions fail before the correction;
independent combined boundary/error review passed 323 tests including actual
libmpv0.40 nested cleanup. This is separate from root-ACK causal ownership.

Final combined publication verification: Python3.12 default 2682 /65 skips in
72.65s; Python3.11 focused 392 in 2.77s. The boundary/rename/EQ snapshot passed all 47
workflow-selected libmpv cases on 0.37 (403.17s) and 0.40 (403.06s). After the
separate three-line terminal-error delta, seven affected native cases passed on
each version (46.44s/46.88s). Final production digest:
`788f30eb6f142227a083fa4a39cfb5b7ec34dd569b931df42a3289677c2ba855`.
The next hosted run must independently verify the final revision, including
production Unix IPC; earlier failures and frozen radio digests are unchanged.


## Causal root replacement is deliberately narrow

The native load acknowledgement can provide its entry ID. A dedicated pending
load token retains that causal ID without borrowing a later snapshot or changing
the adopted-current/staging fields. Once the complete load wrapper returns, an
agreeing active root entry with a different ID can veto readiness or owned
cleanup; observed replacement reports ERROR and uncertain silence without a
new Stop/load/gain mutation. Same-URL replacements are covered. The gain wrapper
returns its original result while retaining all success/error/finally behavior.

The first candidate was rejected before publication: native merge-files can
create a new parentless EDL entry from our own source. The final guard excludes
all edl:// roots from positive attribution, including genuinely external EDL
replacements. A deterministic warmup/Stop/M3U/merge-files fixture on 0.37/0.40
proves owned unready EDL still receives exactly one Stop and confirmed idle;
EDL playback is not newly supported. Entry-level parentage ordering is supported
by public mpv source, but not by a claim about arbitrary native scripts.

Actual 0.37/0.40 proofs retain eight same/different-root observer, readiness and
failed/cancelled-cleanup interleavings. They check continued foreign progress and
gain, preserved canonical selection, uncertainty and later explicit Stop/Play
recovery. These use direct HttpApi calls and a real libmpv command adapter/null
sink; they do not add network-callback or production-Unix evidence by themselves.
This is an observation-time veto, not atomic control over external writers.
Replacement during the backend's inner unpause, failed unpause preventing ACK
delivery, or replacement after cleanup's last snapshot but before Stop remain
outside the guarantee. Missing/lost ACK, EDL/container attribution, range reload
and native-core restart/reused IDs remain excluded.

## First-run identity is never partially published

Interrupted old UUID creation exposed an empty or seven-byte file, and another
reader could accept the truncated nonempty value. Creation now writes/fsyncs a
private same-directory file, atomically hard-links it without replacing a winner,
removes only its own temporary name and syncs the directory. Competing creators
return one fully published ID. Existing/explicit/legacy identities are neither
rewritten nor silently regenerated; old nonempty truncated values are not repaired
because they cannot be distinguished safely from intentional legacy IDs.

Independent review found and fixed a pre-publication fdopen failure descriptor
leak: the text stream does not own the raw descriptor, which closes in a finally
before publication. Unsupported hard links or sync failures fail startup without
a non-atomic fallback. Post-publication failures retain the complete identity.
Actual local threads/eight competing subprocesses and synthetic fault paths are
covered on Python3.11/3.12; author and independent full suites passed 2709 /65.
These checks do not establish physical power-loss or ancestor-directory durability.

## Coherent cleanup evidence uses the original deadline

An intermittent idle=true/empty-playlist snapshot could retain prior path/time
while separate mpv property reads straddled Stop. It appeared on 0.37/0.40 and
published/candidate code; it is distinct from the prior idle=false active-audio
regression. A test-only helper now requires one fresh idle+empty-path+empty-
playlist sample before the original absolute command-start+4.5s deadline,
including read time. It does not mix samples, add a time budget, ignore residue
or change production Stop/readiness. Permanent residue, missing fields, backend
errors, late/blocked reads and cancellation are tested. The old active-audio
regression still fails this unchanged deadline on both native versions.
Independent native 0.40 replay plus nine additional negative cases passed.

Final exact combined verification: Python3.12 default 2759 /66 skips in 76.23s;
Python3.11 default 2759 /66 in 77.19s. Both complete selected libmpv lanes passed
60 cases: 47 REAL_BACKEND/null-or-PCM cases plus 13 UNIT helper checks. Actual 0.37
completed in 408.84s; 0.40 in 411.34s. All existing thresholds/timeouts are unchanged.
Production digest: `d1838974ffce99e18fa57bb9c470820306b4345f1cc12e4f74c37fb0385bbfea`.
Hosted production-Unix/architecture validation for this exact revision remains
its own gate before the requested behavior-neutral extraction begins. The
separately frozen radio endurance source and evidence are not relabeled.


## Bounded pure metadata extraction

Only artwork/live-tag editing, first resource-URI replacement and duration
attribute editing move into metadata_formatting.py. The former function bodies
are AST-identical; their HttpApi instance/static wrappers retain signatures and
call-site behavior. There are no new dependencies, awaits, I/O, state reads or
changes to other HttpApi method bodies. DIDL construction stays in place because
its dynamic attribute-escaping dispatch would require a less direct extraction.

Existing regex quirks remain deliberately unchanged: backslash replacements can
expand or raise, namespace/prefix matching is textual, single-quoted duration
attributes are retained, and spaced equals signs can cause another attribute.
This refactor does not repair those cases. Necessary characterizations cover the
existing wrappers and new pure functions; existing integration/native suites
remain the main protocol and playback gate.

Independent review matched 38,347 bounded baseline/candidate records across
results, exceptions, object identity, signatures and observable evaluation traces,
and passed 200 focused tests. A separate author corpus matched 6,849 cases,
including 1,093 exceptions and 11 effect traces; 212 focused tests passed. These
are finite equivalence checks, not a proof for every possible input or runtime
monkeypatch. Exact final full-suite and native results are recorded separately.

Final extracted-code snapshot passed 2813 default tests /66 opt-in skips on
Python3.12 in 79.04s and Python3.11 in 80.47s. The complete selected libmpv lane
passed 60 checks on 0.37 in 410.55s and 0.40 in 413.67s (47 actual native cases, 13
deterministic helpers). No thresholds, timing budgets or opt-in defaults changed.
Production digest: `93e39232b679ab4d321aaf4f98bdfa4befe61138dcdb04cf3e7dae1ea6ee381e`.
The separate refactor commit and verification record, head 8291763, passed all
three hosted standard CI jobs by 09:51:33 UTC (36m40s after extraction began). Frozen radio
provenance and all earlier limitations remain unchanged.


## Qualified four-hour changing-radio result

The existing frozen run completed on 2026-10-12 at 10:06:26.620761 UTC including
cleanup. Pytest exited zero: 10 passed in 14,408.70s (one real sustained case and
its deterministic gate checks). Requested baseline 14,400s; observed 14,402.516s,
native playback progress 14,402.104s. All 1,795/1,795 synthetic titles arrived
through autonomous native metadata callbacks and matched HTTP/SOAP/received GENA;
1,348 broader protocol agreements also passed. Exactly one handle/load/source/
request and zero disconnects; no hidden reload or reconnect is credited.

Backward-history plateau: 1,047,520–1,048,560 bytes. Live peak RSS growth was
4,428 KiB above the warm baseline; live resources stayed at five tasks, twelve
FDs, ten threads and zero children. Cleanup closed the native handle, listener
and client, removed subscriptions/pending tasks and ended the stream handler.
Resources returned to one task, seven FDs, one thread and zero children. Cleanup
RSS remained 42,512 KiB above pre-start, within the unchanged 49,152 KiB budget;
the retained RSS was within budget, not a claim that all RSS returned to zero.

All 27 frozen source-manifest entries and before/after checks matched:
- Source: `b8c7c3828554d0d0bdd0cd04258d01537956d38a2a191b41d43879603f3fc2b4`
- Production: `b3ae8a5c7ad01696b12be6feb52c16fe51fed9ad6128015992b6d14fd055b5ef`

Scope: REAL_BACKEND/libmpv 0.40, synthetic AAC/ADTS over loopback, null output,
x86_64. It predates later production commits including the formatter extraction;
do not transfer four-hour evidence to their different source digests. No physical
DAC, WAN/provider reliability, original app or production-Unix endurance claim
follows. Earlier failed/default/zero-history results remain separately recorded.
No further multihour run is planned; development has pivoted to useful queue modes.


## Real Repeat One / Repeat All queue behavior

A small pure queue_policy module now owns supported mode adapters and the
observed-end successor decision. Queue.loop is the single authority; HTTP status,
SOAP settings, QueueContext and GENA agree, including an explicitly empty queue.
The numeric public-client dialect is normal 4 / one 1 / all 0, correcting the former
normal 0 misreport. Old explicit 0 queue documents now repeat all; the default
actual sequential behavior remains unchanged. Full contract and conflicting
vendor HTTP write documentation are in docs/COMPATIBILITY_MATRIX.md.

Mode changes do not restart playback, increment its generation, cancel pending
seek/recovery, resume pause or clear errors. Automatic selection rechecks mode
following its ownership preflight. Manual numerical navigation and finite station
recovery retain their existing semantics. Native staging is normal-only; an armed
native playlist/cleanup/output-uncertainty rejects an actual mode change until
confirmed Stop. Repeat does not claim gapless transitions or reason-aware EOF.

Independent real pywiim/raw HTTP/SOAP and deterministic owner/staging review passed.
The short real three-track flow passed on libmpv 0.37/0.40 in 15.55s/15.53s: exact
A→B→C end, C→A wrap, B→B with distinct native IDs and decoded progress, manual
navigation, live mode-change continuity, received GENA and final Stop/cleanup.

Final immutable integration passed 2952 default tests /67 opt-in skips on
Python3.12 (75.52s) and Python3.11 (79.57s). Complete selected native lanes passed
61 checks on 0.37 (428.04s) and 0.40 (427.18s): 48 actual native cases plus 13
unit helpers. Production digest:
`55603494cf0c63fae1aa73f972e2e1c6c30d4ee331e9df66e74346ae4359605d`.
The first integration attempt had one pre-existing raw-playlist readiness fixture
failure on 0.37 (60 other passes; 0.40 passed 61). The separate test-only correction
requires expected-source readiness/progress with unchanged 3s deadlines; its
controlled red reproduction and four-format native replay are documented in
TESTING.md. That failed attempt remains failed. Hosted verification for this new
feature checkpoint must still finish; no hardware/original-app claim is added.


## Editable local favorites and truthful persistence

The new ORR-owned favorites module provides list/add/save-current/rename/delete
through a bounded JSON API and CLI. One effective view serves those routes,
public getPresetInfo/MCUKeyShortClick recall and status preset_key. Configuration
favorites remain an unchanged read-only prefix; managed entries have stable edit
IDs and current dense slots. Renaming/deleting/saving does not mutate active
playback, its queue, position, volume, generation or repeat policy.

The versioned bounded file is atomically replaced. Invalid/unsupported/unreadable
state stays untouched, managed operations report unavailable, and configured
recall still works. Precommit failure keeps the old state; postcommit durability
failure keeps memory consistent with the new visible file and returns an explicit
committed warning. The separate atomic helper repair avoids unnecessary temporary
pathname cleanup masking an already-committed result; CLI errors preserve a fixed
read-before-retry warning without echoing arbitrary error bodies.

This is not an OEM preset-write, provider-account or secure multi-user API.
HTTP(S) references only; userinfo and local audio-proxy paths are rejected, while
query encoding is preserved. Token-bearing queries cannot all be recognized.
Save-current copies the canonical selected direct target/display fields, never
backend/resolved children or account state; all resolver-backed radio is currently
conservatively rejected. A stable public URL may be added explicitly. The public
contract and practical CLI examples are in COMPATIBILITY_MATRIX/GETTING_STARTED.

Actual pywiim selected two distinct loopback WAVs with native ID/codec/progress
and received GENA agreement. Active library edits retained the native lifetime;
service/backend recreation restored favorites without autoplay, then explicit
recall decoded again. Both libmpv 0.37/0.40 passed in 3.35–3.42s with full cleanup.
Independent CLI/HTTP, repeat interaction, corruption and atomic-failure review
passed. Final local default suites including the scheduler passed 3155 /68 skips
on Python 3.12 (113.37s) and Python 3.11 (114.10s). Production digest:
`d3f9c2a02c67081942f9b8aa80bd5ce221117915e0103783ecb1dc0f04f8b7ce`.
Complete serial native lanes passed 62 on 0.37/0.40 (49 native plus 13 unit checks).

## Preserve CI coverage within the existing job budget

The completed Repeat jobs took 881.168s/882.316s under the 900s cap. Added feature
coverage would exhaust that serial margin. A separate reviewed standard-library
runner now executes the unchanged explicit libmpv file list in two independent
groups on the same runner, emits both complete logs, and propagates failures.
Neither group is skipped when the other fails. Owned process groups are cleaned
on timeout/interruption, with bounded TERM/KILL/reap handling; unrelated processes
are preserved. Nonfinite watchdog values are rejected.

All 21 existing native files remain exactly once: group A 20 checks, group B 42.
Actual parallel 0.37 passed 20+42 in 235.386s total (group durations 193.46s/235.02s).
Actual parallel 0.40 also passed 20+42 (197.91s/234.67s), exit 0. Twelve process/CLI
fixtures passed on both Python minors, with independent review. The job stays
15min, Python-floor job 8min, stress 180s/6min, and every later Unix/client/install/
clock/discovery/Snapcast gate remains unchanged. The new native step has a 360s
watchdog and 7min ceiling; no assertion/audio threshold is relaxed and no runner
or dependency is added. Hosted architecture scheduling must still pass this new
checkpoint; local success does not predeclare that result.


## Shuffle and stable radio favorites

The reviewed shuffle module keeps canonical items/indices unchanged and stores
only two cycles of private traversal. HTTP next/prev follows that history;
multi-item SOAP navigation remains numerical/nonwrapping, with the existing
singleton Previous seek exception. Mode 3 plays once; mode 2 repeats shuffled
cycles without immediate boundary repetition. Both project to AVTransport SHUFFLE;
its setter deliberately chooses 3, and RANDOM stays unsupported. Failed Stop never
consumes traversal; failed load retains the selected item in ERROR. Station recovery
and normal-only staging fences are unchanged. The real four-track acceptance proves
actual order/history, cycle end/wrap, received events and owner Stop on both native
versions. Queue XML remains canonical; no gapless or original-app claim is added.

The separate small radio-favorite fix reuses the existing fixed anonymous public
station reference. Save-current converts only matching trusted successful-resolver
provenance on a live zero-duration station, rejecting stale/malformed/finite data
without temporary-URL fallback. It does not fetch or alter playback. Explicit recall
resolves afresh through existing station semantics and recovery. Public XML, JSON
and source labels cannot create private provenance; no account/provenance fields
are persisted. Existing authoritative title/artist/album/artwork behavior stays,
including a captured live-song title that may remain fixed on later recall.

A controlled resolver and real pywiim/libmpv flow proves stable save, persistence,
restart without autoplay/resolution, then a different decoded target with a fresh
native entry ID. Both 0.37/0.40 pass; independent 0.40 replay also passed. Fresh WAN
provider availability remains unverified; blocked cloud endpoint access was not
retried through another route. This adds no partner account or cloud authentication.

Final combined default suites passed 3235 /69 opt-in skips on Python 3.11 (114.86s)
and 3.12 (113.53s). Complete native groups passed 20+43 on 0.37 (195.37s/249.51s)
and 0.40 (195.68s/250.68s), both exit 0: 50 native cases plus 13 deterministic helpers.
All source files match the reviewed immutable snapshot. Production digest:
`43d6c4da7bfdb2dd9adea44d0dda7c32101fa4c077b329cb9b1244760a1f94d3`.
The preceding favorites CI passed every gate in 733.509s/743.046s on x86_64/AArch64,
restoring margin within the unchanged 900s job limit. New checkpoint CI is still
required; no hosted result is predeclared. The older four-hour radio source digest
and all hardware/original-app/WAN limitations remain separate.
