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

## Known remaining work

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
