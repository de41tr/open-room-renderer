# Architecture

Open Room Renderer is a small asyncio Python service. It advertises a local audio renderer, accepts HTTP/UPnP-ish control calls, stores a simple renderer state, and forwards playback/DSP commands to `mpv` through JSON IPC.

## Component Diagram

```text
client app / open-source controller
        |
        | SSDP M-SEARCH, optional mDNS
        v
fake_linkplay.ssdp / fake_linkplay.mdns
        |
        | LOCATION / service records
        v
aiohttp app in fake_linkplay.http_api
        |
        +--> fake_linkplay.upnp_description
        |
        +--> fake_linkplay.eventing.EventSubscriptionManager
        |       |
        |       +--> validated GENA callbacks (NOTIFY)
        |       +<-- synchronous immutable XML snapshot provider
        |
        +--> fake_linkplay.state.RendererState
        |
        +--> fake_linkplay.mpv_backend.MpvBackend
                         |
                         | JSON IPC over Unix socket
                         v
                        mpv
                         |
                         v
              Linux audio output stack
```

## Main Modules

- `fake_linkplay/cli.py`: command-line entry point, config loading, startup/shutdown orchestration.
- `fake_linkplay/config.py`: YAML config model, generated UUID, persisted device name, persisted channel balance.
- `fake_linkplay/http_api.py`: HTTP/SOAP endpoints and current playback, queue, event payload projection, settings and DSP orchestration.
- `fake_linkplay/eventing.py`: GENA admission/leases, registry, sequence numbers, coalescing, bounded delivery/retry tasks and cancellation/drain; consumes only a synchronous XML snapshot provider.
- `fake_linkplay/metadata_formatting.py`: pure metadata artwork/live-tag, resource-URI and duration edits; no state, network or backend access.
- `fake_linkplay/state.py`: in-memory renderer state and status payload generation.
- `fake_linkplay/queue.py` and `queue_policy.py`: canonical items/indices and pure repeat/shuffle traversal; backend admission stays in HttpApi.
- `fake_linkplay/favorites.py`: bounded renderer-owned library, atomic storage and its JSON routes; edits do not control playback.
- `fake_linkplay/mpv_backend.py`: mpv process startup, JSON IPC commands, snapshots, volume/mute/seek, balance and PEQ filter graph.
- `fake_linkplay/ssdp.py`: UDP multicast responder and periodic NOTIFY packets.
- `fake_linkplay/discovery_client.py`: bounded explicit-interface SSDP candidate reader; no HTTP fetch, configuration or playback side effects.
- `fake_linkplay/mdns.py`: optional mDNS/DNS-SD responder.
- `fake_linkplay/upnp_description.py`: device description XML, SCPD XML, capability payload helpers.
- `fake_linkplay/podcast.py`: simple playlist/podcast helpers.
- `fake_linkplay/utils.py`: logging setup, LAN IP detection, filesystem helpers.


## GENA lifecycle boundary

`EventSubscriptionManager` now owns the complete subscription/delivery lifecycle.
HttpApi constructs one manager and shares it across its existing listeners. It
provides one callable, `service -> str`, which returns the existing complete XML
body. The manager receives no `HttpApi`, `RendererState`, queue, backend, config
or DSP object. The provider remains dynamically bound to `_event_body`, preserving
its existing replacement/override behavior. It performs no I/O or await.

- **Manager-owned:** subscriber records and registry; SID generation; canonical
  service admission; lease parsing/renewal/expiration; same-peer callback
  validation; total/per-peer limits; dirty coalescing; semaphore; one delivery
  task per SID; sequence advancement/wrap; immutable retry body; cancellation.
- **Renderer-owned:** authoritative playback/queue/DSP state; generations and
  intent; backend observations; event payload projection including transport
  action availability; notification trigger points; shared-listener accounting
  and placement of final cleanup within the overall shutdown sequence.
- **External support:** `network_policy.validate_callback_url` still enforces the
  exact destination policy; `soap.SERVICE_ALIASES` still supplies the shared
  service names; aiohttp still performs the same bounded HTTP operations.

Subscription handlers execute synchronously inside the preserved asynchronous
`HttpApi.event_subscribe` / `event_unsubscribe` handlers. Those wrappers contain
no await. A new event captures its XML only when delivery starts, before waiting
for the existing eight-slot semaphore. Failed attempts keep the identical body
and SEQ; acknowledgement advances SEQ and the next dirty event captures current
renderer state. There is no second playback cache or observer.

The monitor still prunes immediately after its existing sleep. Final-listener
cleanup still drains native cleanup and seeks, then clears/cancels/drains GENA,
then cancels the backend monitor and drains recovery/queue work. `close()` adds
one coroutine call, not a new scheduling boundary: it runs without suspension
when no event tasks exist, and otherwise awaits the same captured tasks at the
same point. No new locks, semaphores, observer tasks or delivery tasks were added.
There is no new terminal closed/reopen state or altered admission policy.

Compatibility keeps `EventSubscriber` importable from `http_api`, the live
`event_subscribers` dictionary and tuning properties (including assignment), and
renderer notification/pruning/legacy duplicate helpers. The dictionary is not a
copy. The legacy duplicate filter deliberately keeps its old raw service-name
comparison and pop-only semantics; changing that policy is outside this refactor.
Private delivery internals now live in the manager; delivery logs use the
`fake_linkplay.eventing` logger. The mutable compatibility registry and tuning
properties remain an intentional coupling for existing Python callers/tests,
rather than being silently replaced with a read-only or copied view. Event XML builders remain in
HttpApi because their projection depends on renderer-owned readiness/intent,
metadata, queue and device identity.

### Dependency and side-effect overview

| Component | Authority / state | Effects and synchronization | Dependencies |
| --- | --- | --- | --- |
| HttpApi + RendererState | Playback phase, generation, intent, canonical queue, confirmed state and renderer orchestration | Existing backend/gain/DSP/rename locks; monitor, seek, stage cleanup, queue advance and recovery tasks | mpv, queue/policy, persistence, protocol serializers, event manager |
| EventSubscriptionManager | SID records, lease/SEQ, dirty flag and delivery task per SID | Existing 8-slot semaphore; bounded NOTIFY/retry; explicit prune and close, no lock or observer | synchronous XML provider, network_policy, SOAP aliases, aiohttp |
| MpvBackend | Backend/process ownership and confirmed backend operations | Unix IPC, owned process lifecycle; existing gain/DSP locks | stdlib asyncio/process/IPC, log_safety; caller-supplied settings |
| queue / queue_policy | Canonical item/index data / pure traversal | No backend or notification authority | Pure parsing and policy |
| soap / upnp_schema / upnp_description | Framing/action contracts / declarations / representations | Pure validation and XML, no playback effects | Shared aliases/actions, queue mode constants, capabilities/config |
| persistence | Durable settings format | Bounded validation, atomic file replacement | Filesystem; no GENA or playback authority |

The new import direction is `http_api -> eventing -> network_policy / soap`.
There is no reverse import into HttpApi. Existing pure metadata helpers remain
separate and unchanged.

### Why this seam, and what it does not solve

| Candidate | Cohesion and coupling benefit | Risk / interface / effort | Decision |
| --- | --- | --- | --- |
| GENA lifecycle/delivery | High: one protocol lifecycle and its own state; removes delivery/admission policy from renderer | Low-to-moderate: one synchronous string provider plus notify/prune/close; rich existing real-loopback and native coverage | Implemented |
| Pure protocol contracts | Clear but mostly already separated into soap/schema/description; smaller ownership improvement | Low; chiefly representation work, little controller state removed | Useful later if duplication grows |
| DSP preset library | Cohesive storage, but active vs desired state and committed-write failures cross restore/transaction logic | Moderate: carefully distinguish library edits from active DSP commits | Best next bounded candidate after explicit contract analysis |
| Playback/recovery/native staging | Large potential benefit but tightly coupled generations, backend identity and cancellation | High: would require wider ownership/lock-order design | Not changed in this task |

The structural gain is real but deliberately limited: subscription policy can
now be read, instantiated and tested without constructing a renderer, and event
transport cannot call playback or inspect mutable queue/DSP state. HttpApi no
longer implements SID admission, delivery retries or lease/task bookkeeping.
The costs are one small component, a provider contract and explicit compatibility
forwarders. Total code grows slightly; line-count reduction is not the measure
of success. Keeping aiohttp request/response types in the manager is intentional
protocol cohesion, rather than adding a second request/result abstraction.

HttpApi remains a large orchestrator for HTTP/SOAP, playback, native staging,
stream recovery, DSP transactions, metadata and service adapters. This extraction
does not make it a finished player-core architecture. Next investigate a DSP
preset-library boundary with pure validation/storage and a detached result;
keep active backend transactions and restore ordering in the renderer initially.
Moving playback ownership, generations, queue/backend control, asynchronous
lock order, or introducing a broader service framework requires a separate user
decision and a wider design/test plan. New providers/authentication or changes to
supported protocols likewise remain outside this refactor.

## Saved DSP library and authored queue contracts

`eq_presets.EqPresetLibrary` owns saved curve lookup, save/delete/rename,
name matching, and bounded JSON load/write. Pure EQ request/name/band helpers
reuse the existing canonical persistence validator. The component receives only
ordinary values, a state-directory path, and a narrow text-writer callback. It
never receives HttpApi, Config, RendererState or the backend; it adds no lock,
await, task or reverse import.

HttpApi retains the active plugin/source caches, balance, restore/readback gates,
transaction snapshot/rollback, backend effects and active-state persistence.
Its existing DSP command lock still encloses the shielded transaction; backend
DSP/gain lock order is unchanged. Library loading never activates a curve.
Successful library persistence returns before the renderer sets its committed
flag and persists active metadata. Pre-replacement errors, committed-directory
fsync failures, and a failed second write preserve the existing distinction.

The existing `eq_presets` attribute is a mutable read/write property onto the
single library mapping, including rollback replacement. The old constants and
helper names remain callable, and the HttpApi text-writer patch seam remains.
The property no longer appears as a stored entry in `vars(api)`. Private
subordinate helper overrides are not extension hooks for the independent
library's internal calls. This is an intentional Python-private compatibility
limit, not a changed HTTP/DSP contract.

`queue_protocol` owns pure authored URL/name validation, explicit queue creation,
mode identifiers, complete escaped SOAP request framing and its bounded size
validation. Server queue export and offline playlist edits now import that
contract without pulling in `queue_client`, requests or CLI transport. The queue
client retains HTTP sessions, response parsing, uncertain-outcome policy and
CLI behavior. Legacy imports remain re-exports of the same helper/error objects;
the exception's defining module is now `queue_protocol`. The client-private
request builder still forwards its existing patched size limit.

Dependency direction is renderer/client/playlist adapter → pure queue contract
→ canonical queue/policy. Runtime queues are never duplicated or owned by the
contract module. Both extractions reduce actual responsibility/dependency
coupling while leaving playback generations and scheduling untouched.

The architecture phase deliberately stops here before a player-core rewrite.
Further work prioritizes a useful browser controller. Stateful source ownership,
recovery and native staging still warrant a wider design before extraction.

## Startup Flow

```text
open-room-renderer serve --config ...
        |
        v
load_config()
        |
        +--> create/load UUID under state_dir
        +--> load persisted device name and balance
        v
RendererState.from_config()
        |
        v
MpvBackend.start()
        |
        v
aiohttp AppRunner starts HTTP API and description ports
        |
        +--> SSDP responder starts if enabled
        +--> mDNS responder starts if enabled
        v
wait for SIGINT/SIGTERM/SIGHUP
```

At startup the backend starts or connects to `mpv`, then immediately stops playback so stale audio does not continue across restarts.

On Linux, SIGINT, SIGTERM and SIGHUP request bounded graceful shutdown during
startup or active service. SIGHUP stops the service; it does not reload
configuration. Only renderer-owned mpv processes are terminated.

## Discovery And Control Flow

SSDP is the primary discovery path:

```text
client sends M-SEARCH to 239.255.255.250:1900
        |
        v
SsdpServer receives search target
        |
        v
response_packet() returns LOCATION: http://<advertise_ip>:<port>/description.xml
        |
        v
client fetches /description.xml and service endpoints
```

Optional mDNS is available for compatibility experiments. It is off by default in the safe example config.

Control requests arrive mainly at:

- `GET /httpapi.asp?command=...`
- `GET /description.xml`
- `/upnp/control/...`
- `/upnp/event/...`

`http_api.parse_command()` converts Linkplay-style command strings into `ParsedCommand` objects. `HttpApi.execute()` dispatches commands to state changes, mpv calls, or XML/JSON response helpers.

## Playback Flow

```text
setPlayerCmd:play:<url>
        |
        v
parse_command() -> ParsedCommand("play", url)
        |
        v
HttpApi.execute()
        |
        +--> update RendererState metadata
        +--> resolve/proxy URL when configured
        v
MpvBackend.loadfile(url)
        |
        v
mpv JSON IPC: ["loadfile", url, "replace"]
```

TuneIn and queue commands follow a similar path but may first parse queue metadata and resolve stream URLs.

## EQ Flow

The 10-band PEQ path is handled mostly in `HttpApi` and `MpvBackend`:

```text
app/controller sends EQ command
        |
        v
HttpApi parses/stores band data and preset state
        |
        v
MpvBackend.peq(bands)
        |
        v
_audio_filter_graph()
        |
        v
mpv "af remove" then "af add @fake_linkplay_dsp:lavfi=[...]"
```

Only synthetic tests should be added around EQ behavior. Do not copy proprietary preset tables or app internals.

## Balance Flow

Balance is persisted in the confirmed active DSP context, with legacy
`channel_balance` as a read fallback, and restored on startup. Runtime changes call:

```text
MpvBackend.channel_balance(value)
        |
        v
_audio_filter_graph()
        |
        v
lavfi pan filter
```

Values are validated in `-1.0..1.0`; invalid values fail rather than silently
clamping. This refactor does not change DSP behavior.

## mpv Integration

`MpvBackend` talks to `mpv` through a Unix socket. The default mode auto-spawns:

```text
mpv --no-video --idle=yes --input-ipc-server=<socket>
```

The backend also supports connecting to an already-running `mpv` instance if `auto_spawn_mpv` is false.

## Logging Strategy

Normal logs should be useful but sanitized. Raw request/response logging is controlled by config and should stay off for public use.

Redaction covers common token fields, login fields, private IPv4 addresses, MAC addresses, and UUIDs. This is best effort. Contributors should still avoid publishing raw logs.

## Stable Vs Experimental

Relatively stable:

- CLI startup.
- YAML config loading.
- SSDP discovery.
- Basic HTTP commands.
- mpv JSON IPC wrapper.
- Balance and 10-band EQ filter generation.
- Synthetic pytest suite.

Experimental:

- Proprietary-app compatibility flags.
- mDNS compatibility profile.
- SOAP/eventing behavior.
- Queue and stream-service details.
- Plex-style cloud/library helpers. These are experimental and disabled/not surfaced in the alpha app-facing profile.
- Port-80 compatibility experiments.

## Where To Start Reading

1. `README.md`
2. `fake_linkplay/cli.py`
3. `fake_linkplay/config.py`
4. `fake_linkplay/state.py`
5. `fake_linkplay/http_api.py`
6. `fake_linkplay/mpv_backend.py`
7. Relevant tests under `tests/`

The renderer-owned queue and authored-playlist CLI keeps new responsibilities
outside HttpApi: `queue_client.py` frames and validates existing SOAP operations;
`playlist_files.py` owns the strict authored format and safe local publication;
`playlist_m3u.py` decodes the strict offline URL-list subset without I/O;
`playlist_cli.py` composes validation/file/client steps. `transport_client.py`
frames existing HTTP controls and checks their bounded acknowledgements. No backend, recovery or
queue-staging logic moves into these adapters. Live queue export is deliberately
not inferred from potentially resolved BrowseQueue URLs.

Stable favorite recall keeps selection in `FavoritesStore.playback_snapshot`,
then crosses a dedicated callback into the existing preset playback helper.
The detached record is consumed before the first playback await; it is never
converted back into a mutable numeric slot. `favorites_client.py` owns the strict
single-request acknowledgement. Existing recovery/staging/ownership paths stay
in place, and numeric public-client recall remains an independent legacy surface.


The standard-DMR compatibility correction stays at protocol boundaries:
`upnp_description` owns canonical/legacy service presentation and pure static
ConnectionManager properties; `upnp_schema` selects canonical versus preserved
legacy input contracts. HttpApi routes those schemas, serializes the same CM
properties for reads/events, and accepts bounded unpadded seek syntax. Playback,
recovery, queue staging and event-delivery lifecycle implementations are unchanged.


Current-queue file export adds a read-only adapter, not a second queue authority:
`canonical_target` reuses the existing pure trusted-radio target predicate;
`queue_playlist` captures/validates one synchronous URL-only snapshot;
`playlist_client` performs one bounded explicit read. Existing `playlist_files`
remains the private atomic file boundary. HttpApi only registers the new route;
no playback, recovery, staging, event delivery or server filesystem logic is added.


Offline playlist editing uses a pure `playlist_edits` transformation module with
occurrence-position selection semantics. It reuses existing source/result schema,
URL and escaped-request validation plus the unchanged private file boundary.
Only the CLI chooses input/output paths; no renderer or live queue code changes.


Favorite target repair stays in the existing locked library transaction and a
separate bounded management client. An explicit keep/clear policy changes only
stored content metadata; playback uses its already-detached snapshot. Existing
rename/current/recall clients and player/recovery/staging paths stay unchanged.
