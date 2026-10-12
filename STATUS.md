# Development status

Updated: 2026-10-12 UTC. Work is in progress, not a compatibility certification.

## Baseline

- Source and reference review agree on base commit `3672b9838e9c39ae8191613108f0d91a5cada97d`.
- Python 3.12.14; initial suite: 92 passed.
- mpv 0.40.0 can decode synthetic WAV, MP3 and FLAC to null output.
- Current sandbox denies Unix-domain socket creation; multicast discovery is also
  unavailable. This is a test limitation, not evidence of a production defect.

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

Generated local fixtures passed real libmpv0.40 decoding for MP3, ADTS AAC,
application/octet-stream media, unencrypted HLS segment crossing, first-level
M3U/PLS, redirects/loop failure and ICY metadata. Two-entry container transitions,
HTTP/SOAP state and final top-level queue advance passed. Exact isolated snapshot:
1714 passed with all12 real libmpv cases enabled; two Unix IPC cases skipped
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
