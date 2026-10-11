# Development status

Updated: 2026-10-11 UTC. Work is in progress, not a compatibility certification.

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
truncate generated WAV audio, reject the next renderer reload with HTTP503, then
restore audio. Actual decoded position resumed without client status polling.
Separate real cases verified stop/pause during backoff and teardown; volume37,
mute and queue index2 survived recovery. This is controlled local source failure,
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
