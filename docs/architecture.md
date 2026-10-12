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
- `fake_linkplay/http_api.py`: HTTP/SOAP endpoints and current playback, queue, eventing, settings and DSP orchestration.
- `fake_linkplay/metadata_formatting.py`: pure metadata artwork/live-tag, resource-URI and duration edits; no state, network or backend access.
- `fake_linkplay/state.py`: in-memory renderer state and status payload generation.
- `fake_linkplay/queue.py` and `queue_policy.py`: canonical items/indices and pure repeat/shuffle traversal; backend admission stays in HttpApi.
- `fake_linkplay/favorites.py`: bounded renderer-owned library, atomic storage and its JSON routes; edits do not control playback.
- `fake_linkplay/mpv_backend.py`: mpv process startup, JSON IPC commands, snapshots, volume/mute/seek, balance and PEQ filter graph.
- `fake_linkplay/ssdp.py`: UDP multicast responder and periodic NOTIFY packets.
- `fake_linkplay/mdns.py`: optional mDNS/DNS-SD responder.
- `fake_linkplay/upnp_description.py`: device description XML, SCPD XML, capability payload helpers.
- `fake_linkplay/podcast.py`: simple playlist/podcast helpers.
- `fake_linkplay/utils.py`: logging setup, LAN IP detection, filesystem helpers.


## Bounded extraction and target responsibilities

The completed behavior-neutral refactor extracted only three pure metadata-edit functions. HttpApi keeps
its existing helper signatures as delegation wrappers; DIDL construction and its
dynamic attribute-escaping call remain in place. Existing string/regex behavior,
including awkward legacy cases, is preserved rather than repaired in the move.
No await, I/O, state transition, callback scheduling or timing policy is added.

The target below is a map for later incremental work, not a claim that a new
player-core framework or all these module boundaries already exist:

- **Protocol adapters:** HTTP/SOAP dispatch currently remains in HttpApi, with
  `soap.py`, `upnp_description.py` and discovery modules alongside it. Adapters
  validate requests and format results; they must not invent successful state.
- **Player core:** guarded commands, owner intent/generations, readiness, failure
  cleanup and recovery remain in HttpApi with `RendererState` and `MpvBackend`.
  A later extraction would preserve that single ownership contract and its
  locking/order tests. None of this logic moves in the current refactor.
- **Queue:** `queue.py` owns item/index validation and queue parsing/serialization.
  The later functional package adds pure repeat/shuffle policy and bounded history
  in `queue_policy.py`; HttpApi commits selection with existing ownership checks.
  Native sequencing/staging orchestration stays where it is; queue data must not
  independently claim that a decoder changed source.
- **Eventing:** leases, coalescing, delivery order and retries remain in HttpApi.
  A future event publisher should consume authoritative snapshots, including
  pending/error phases, rather than control playback. Pure XML formatting is separate from subscription lifecycle.
- **DSP:** existing validation/persistence helpers and MpvBackend retain the
  confirmed gain/filter transactions and startup restore ordering. A future
  service boundary must keep configuration distinct from effective output.
- **Service adapters:** `radio.py` resolves bounded anonymous public-station
  selections into concrete resources/provenance. Additional providers would
  require their own lawful lifecycle; a source label is not an implementation.

Dependency direction should stay simple: protocol adapters issue commands to
the player/queue contract; the player owns backend effects; eventing consumes
observed state. Pure representation helpers accept values and return strings.
Further extraction should be one tested seam at a time, on a separate commit,
without changing supported payloads, faults or owner timing.

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
