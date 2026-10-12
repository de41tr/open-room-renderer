# Development plan

Branch: `dot/renderer-autonomous-20261012`; base: `3672b9838e9c39ae8191613108f0d91a5cada97d`.
Work window ends 2026-10-12 20:00 UTC. Stabilization only after 19:00 UTC.
No main-branch edits, merge, or release.

## Current acceptance and handover package (17:00 UTC)

Legacy-isolation checkpoint `a7e8d8` has complete standard CI green. A final
requirements-to-evidence review of all 20 original packages identified one
worthwhile native gap: intentionally malformed media. The new short test now
passes exact reviewed source on libmpv 0.37/0.40; runtime code is unchanged.
Final default suites pass both Python minors. Publish test/CI wiring separately
from the reviewed acceptance map, operator links and next-phase handover, then
verify complete standard CI without changing any cap or existing assertion.

Continue only meaningful bounded quality work and operational consistency;
do not add speculative features or artificial tests. Preserve prior failures and
frozen-source endurance scope. Physical/original-app acceptance and provider
integrations remain separate, with no known hardware availability date.
Formal stabilization starts 19:00 UTC; active work ends 20:00 UTC today.

## Ordered work

1. AP01: explicit SOAP contracts, faults, and acknowledged backend commands.
2. AP02: authoritative playback phases and generation-scoped observations.
3. AP05: real multi-item queue, one-based selection, browse and transport navigation.
4. AP03: consistent discovery, persistent independent identity and honest capabilities.
5. AP04: renewable, bounded, safe GENA subscriptions and queue events.
6. AP06: header-only public-radio selection through a bounded anonymous public-station resolver.
7. AP07: backend/process failures and owned shutdown.
8. AP08: recovery that respects stop, pause, and newer playback generations.
9. P1 where time permits: metadata, confirmed seek, volume/mute, existing DSP,
   persistent settings, ARM installation, radio formats and measured transitions.

P2 provider integrations remain disabled. Bounded Snapcast digital/control
experiments are separate from two-host synchronization or an advertised service.
No proprietary
material, credentials, original identifiers, captures or binaries belong here.
Keep the production Unix-socket mpv design; use deterministic test doubles where
this execution environment cannot create Unix-domain sockets.

## Checkpoints reached

AP01, AP02, AP05, AP03, AP04, AP06, AP07 and AP08 have implementation checkpoints,
including real libmpv/null control, two-track/callback and interrupted-source
recovery flows. AP11 now confirms and persists actual digital gain. AP12 now validates and transactionally confirms existing DSP, with measured
PCM effects. AP09 preserves canonical and opaque metadata across client surfaces.
AP10 confirms actual seeks, including pause/resume and stale-command rejection.
AP14 provides reviewed native installation and stable unprivileged profiles.
AP13 restores complete confirmed active DSP, including deferred startup.
AP15 has first-level native playlist ownership and local radio-format evidence.
Log privacy, calibrated clocked transition measurement, version-independent load
options, explicit multicast discovery and same-owner unready-load cleanup are
implemented. Native discovery-to-playback passed on both CI architectures.
Sustained no-EOF station stalls now have reviewed owner-fenced bounded recovery.
Equal-rate native staging now measures 0ms on both CI architectures, with mixed-rate limits.
Public-station address refresh, uncertain-owner transport fences and exact
public-client resource parsing are implemented. Installed CLI persistence,
genuine pywiim controls and the Python3.11 floor pass CI. The extended native
Unix-IPC checkpoint passed over fifteen minutes on both architectures; routine
CI durations are restored.

Current checks: complete owned-player death/error/fresh-backend restart now
passes both native architectures, as do the finite-history and changing-metadata
policies. The separately frozen constant-title finite-policy hour passed with
complete cleanup. The bounded changing-ICY endurance mode passes four-minute
replay and negative sensitivity; its separate frozen four-hour run completed successfully including cleanup at
10:06:26.620761 UTC. Keep its frozen
provenance separate from later code changes. Rolling live-HLS cases pass locally
but the expanded hosted libmpv lanes failed at 3e74b24. Both fixture assumptions
(segment503 native EOF and path-before-decoder readiness) are reproduced and
corrected; integrated and hosted 0.37/0.40 checks pass at 2fc8296, including
the honest >=0.37 installation floor. Current concrete gaps: stopped/paused
queue navigation and the shared canonical SOAP/GENA action projection are now
reviewed and integrated together with foreign-source observation and automatic
admission fences. Verify the next hosted checkpoint; preserve explicit Play
and terminal errors. Separately investigate causal ownership during an already
admitted load/readiness window without weakening existing source checks. A confirmed
ambiguous persisted-gain parsing gap now has a reviewed fail-closed fix with
full local default-suite and Python3.11 targeted verification. Limited
ARMv7 emulated ABI/API evidence does not fill native installation/hardware gaps.
Nested ancestry, original-app/hardware audio and two-host sync remain explicit
limits. Do not erase the earlier unexplained Snapcast failure with later passes.

The 5d64bfb native nested-load cleanup regression is repaired and the complete
b18b43e standard CI is green. The current final checkpoint adds narrowly proven
observed root replacement, atomic first identity publication, and coherent
cleanup sampling inside the existing deadline. Complete its native/hosted gates
before the requested extraction. Atomic rename and duplicate EQ JSON
have independent concrete failure proofs. The older readiness/cleanup terminal
ERROR relabeling has a separate reviewed correction. Next evaluate narrowly
provable root-ACK ownership excludes EDL/container/restart cases and raw-writer
race windows; the original unrestricted parentless-root candidate was rejected.

## Requested bounded behavior-neutral extraction

User update 2026-10-12 08:35UTC: finish the already-running functional fixes and
long tests without interruption, and require their complete standard CI to be
green before starting any extraction. Then select one small, clearest HttpApi
responsibility, preferably pure DIDL/XML/metadata formatting. Keep public HTTP,
SOAP and GENA bytes/fields, errors, state, timing and interfaces unchanged.

Target 90 minutes, hard maximum 120 minutes including native CI verification.
Record the actual UTC start, target finish and hard stop here and in RESUME.md
when the green gate opens; the timer is now COMPLETE. Use a separate refactor
commit, existing regression coverage and only necessary extraction tests.
No new dependencies/frameworks, large rewrite, or fundamental playback/recovery/
queue-staging logic changes. If risky or unable to obtain native-green within
the budget, abandon the extraction and retain a concrete next-iteration plan.
Include a short target architecture covering protocol adapters, player core,
queue, eventing, DSP and service adapters. Stabilization/deadline take priority.

Refactor green gate confirmed: 07c72c5, all three standard CI jobs passed in
https://github.com/de41tr/open-room-renderer/actions/runs/38226980559 .
Actual start: 2026-10-12 09:14:53 UTC. Target finish: 10:44:53 UTC.
Hard stop: 11:14:53 UTC, including complete native CI verification.
Status: COMPLETED at 09:51:33 UTC, elapsed 36m40s. Revision 8291763 passed all
three standard CI jobs in run 38230830981. Extracted only the three stateless
metadata-edit helpers into
metadata_formatting.py, retaining exact-signature HttpApi wrappers. DIDL creation
and dynamic attribute escaping remain in place. No protocol dispatch, state or
playback logic moves. Abort/retain a concrete plan if unsafe or not native-green
by the hard stop.

Strategic update 2026-10-12 09:37UTC: after the current refactor reaches full green
CI, prioritize useful local features (repeat/shuffle/playlist navigation/presets)
over additional edge-case or long-run expansion. Keep at most five proposals in
docs/STRATEGIC_REVIEW.md with A_AUTONOM/B_AWAITING_DECISION/C_APPROVAL_REQUIRED
and evidence/value/effort/risk decisions. Review about every 90–120 minutes and
after major packages. No new unnecessary multihour tests. XU4/original-app work
is HARDWARE_ACCEPTANCE_PENDING; do not wait for unavailable hardware. Prepare
the bounded later acceptance plan after higher-value software work. Final handover
adds three practical constraints, three debts, three missing features and user
decisions. Existing branch, publication, safety and deadline limits remain.


10:34 UTC functional progress: Repeat One / All passes independent review, actual
three-track native flows and the complete local default/native suites. Publish
it with the separately reviewed native-format fixture correction and verify full
standard CI. Continue isolated favorites/shuffle implementation meanwhile; do not
restart another radio soak or wait for hardware. Keep mode/source/owner semantics
explicit and one canonical queue authority.


10:53 UTC review: Repeat checkpoint c780c80 has full standard CI green. The
editable-favorites candidate passes real client/native workflows and full local
suites; publish next with its separate atomic-helper repair and a scheduling-only
CI change. Two explicit libmpv groups run on the same existing runner, retaining
all cases and later gates within the unchanged 15-minute job cap. Then publish
reviewed Shuffle after combined native/CI verification. Stable anonymous radio
favorite references are the next bounded investigation; do not store temporary
resolver URLs or account data. See the five-item strategic review.


11:28 UTC: favorites has full standard CI green. Publish reviewed Shuffle and the
small stable-radio save-current improvement as independent functional commits in
one fully tested checkpoint, avoiding duplicate hosted runs. Combined local native
and both Python-minor suites pass. Next bounded investigation: easier CLI queue
load/show/play and explicit saved-playlist restore through existing SOAP actions.
Defer the controller page until genuine browser verification is available; no
network exposure, aliases/tunnels, dependency installation or policy bypass.


13:38 UTC: stable-ID favorites and transport controls have full standard CI green
at d91140d. Complete reviewed offline M3U import through final integrated defaults
and its own publication/CI, keeping its small byte-reader extraction separate.
Then prioritize a bounded standard UPnP-client reproduction over another local
convenience: description/SCPD binding, typed SOAP and client-parsed LastChange are
not covered by the existing HTTP pywiim flow. No new dependency, major player-core
change, browser workaround or multihour run. Keep 19:00/20:00 UTC boundaries.
