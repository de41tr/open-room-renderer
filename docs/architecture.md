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
- `fake_linkplay/mpv_backend.py`: mpv process startup, JSON IPC commands, snapshots, volume/mute/seek, balance and PEQ filter graph.
- `fake_linkplay/ssdp.py`: UDP multicast responder and periodic NOTIFY packets.
- `fake_linkplay/mdns.py`: optional mDNS/DNS-SD responder.
- `fake_linkplay/upnp_description.py`: device description XML, SCPD XML, capability payload helpers.
- `fake_linkplay/podcast.py`: simple playlist/podcast helpers.
- `fake_linkplay/utils.py`: logging setup, LAN IP detection, filesystem helpers.


## Bounded extraction and target responsibilities

This iteration extracts only three pure metadata-edit functions. HttpApi keeps
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
