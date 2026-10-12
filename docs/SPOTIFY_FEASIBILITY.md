# Spotify feasibility: source evidence and engineering limits

Date: 2026-10-12 UTC
Status: Evidence supporting [ADR 001](adr/001-spotify-sidecar.md)
Scope: Public documentation and source review only. No provider installation, login, API-key creation, binary download, playback or changes to the renderer checkout.

## Decision

Keep Spotify inactive and unadvertised in this phase. Prefer separately user-installed Spotify Soloist for a future official-client experiment. Its standalone personal DIY use is expressly intended; integrating it into this renderer remains conditional on the gates below. Do not add a source dropdown, second player state, provider launcher, PCM bridge or shared-DSP claim now.

This is a documented blocker, not a working Spotify integration. The functional Web UI and existing mpv renderer remain the useful deliverable. Local contract fixtures could eventually prove an adapter's parsing and failure behavior, but would remain ACCOUNT_PENDING and would not establish Connect playback.

## Evidence labels and context

FACT means directly supported by an inspected primary source or this checkout. INFERENCE means an engineering conclusion from those facts. UNKNOWN means evidence is still needed.

FACT: Open Room Renderer declares GPL-3.0-or-later. HttpApi and RendererState own generation, intent, queue and confirmed playback state; MpvBackend owns mpv operations and the existing gain/DSP transactions. The Web UI forwards to these owners. The inspected Web UI authority boundaries are published at commit 81a3d72b81809209d619c9e527cda0cf6a761096. Relevant local evidence:

- `pyproject.toml`: project license
- `fake_linkplay/http_api.py`: `_web_dispatch`, `_state_monitor_context`, `sync_mpv_state`, `_apply_backend_snapshot`, `execute`, `_load_playback_url`, `_execute_soap_side_effect`
- `fake_linkplay/mpv_backend.py`: owned-process lifecycle, confirmed gain, DSP graph/readback
- `docs/architecture.md`: renderer and browser-controller authority boundaries

## Candidate assessment

### Spotify Soloist

- FACT: Spotify publishes Linux ARMv7/ARM32, ARM64 and x86_64 archives. ARMv7 availability is verified, not assumed. The tutorial identifies an ARM64 Raspberry Pi/glibc test environment; it does not establish the ARM32 binary's minimum runtime or compatibility with the target Odroid image. [Downloads](https://developer.spotify.com/documentation/soloist/reference/downloads-and-updates), [getting started](https://developer.spotify.com/documentation/soloist/tutorials/getting-started)
- FACT: Spotify explicitly presents Soloist for personal headless/DIY Linux use. Premium is needed for initial setup; both Free and Premium users can subsequently connect. This is documented intended use, subject to applicable terms, not an inferred blanket permission for all integrations. [Official announcement](https://developer.spotify.com/blog/2026-08-13-introducing-spotify-soloist)
- FACT: A Premium account is required to generate a personal Soloist API key. The key is required at each startup; a separate Connect session is paired through Spotify on the same LAN and restored from private persistent storage. Keys must not be shared or embedded in distributed/client-side code. [Authentication](https://developer.spotify.com/documentation/soloist/concepts/authentication)
- FACT: Documented audio output is PipeWire, falling back to PulseAudio; a PipeWire node can be selected. No direct ALSA or raw-PCM/stdout output is documented in the reviewed CLI. Cache defaults to unlimited, with a configurable finite limit of at least 100 MB. A failed WS bind can leave the daemon running without its control API. [CLI](https://developer.spotify.com/documentation/soloist/reference/command-line)
- FACT: The opt-in WS API provides auth/playback/queue snapshots, change events and play/pause/seek/skip/volume/queue/repeat/shuffle/activate/deactivate commands. A command result acknowledges dispatch, not completed playback. No built-in client authentication, TLS, Origin or CSRF protection is provided. [WS reference](https://developer.spotify.com/documentation/soloist/reference/websocket-api)
- FACT: Builds expire 90 days after their build date and exit with code 10; replacement requires an official newer build and restart. The data directory holds private session/device state and local crash reports. [Runtime overview](https://developer.spotify.com/documentation/soloist/concepts/overview)
- FACT: Direct redistribution of Soloist binaries/archives is forbidden by its documentation; users should obtain their own copy from Spotify. Bundled third-party license notices do not grant a general Soloist redistribution right. [Official overview](https://developer.spotify.com/documentation/soloist)
- INFERENCE: Best future provider-supported standalone client, but a poor drop-in for the current mpv DSP pipeline. Loopback alone provides no browser-origin defense. Do not connect a browser directly; any future network-facing adapter needs its own protected boundary and local-endpoint threat model. UNKNOWN: real account eligibility, installed-binary behavior, ARM32 ABI, resource usage and physical playback.

### go-librespot

- FACT: Latest release reviewed: [v0.10.3](https://github.com/devgianlu/go-librespot/releases/tag/v0.10.3), 2026-10-03, commit `4dbc099f46da3529adaa85feb97c4f49deb0b130`. Relevant contracts match current master `3508ee5c2b1723ab8c9c0e0a96c34c8d3967e3b5`.
- FACT: Its build helper targets ARMv6 hard-float, ARM64 and x86_64, without a dedicated ARMv7 variant. INFERENCE: generic ARMv6 is a plausible XU4 candidate, subject to ABI/runtime testing. [Build targets](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/crosscompile.sh)
- FACT: Requires Spotify Premium; supports Connect discovery and OAuth/token/device-code flows. Source builds need Go 1.25+, CGO/native decoding libraries and Linux ALSA dependencies. [README](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/README.md)
- FACT: Built-in REST controls/status and `/events` WebSocket cover transport, seek, volume and queue. Slow WS clients can lose oldest events from a 64-event buffer. INFERENCE: reconcile snapshots after reconnect; events alone cannot be authoritative. [API](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/api-spec.yml), [server](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/daemon/api_server.go)
- FACT: ALSA, PulseAudio and raw named-pipe output exist. Pipe PCM is 44.1 kHz stereo, selectable s16le/s32le/f32le. Reader absence/open blocking needs lifecycle handling. [Player constants](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/player/player.go), [pipe](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/output/driver-pipe.go), [FIFO](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/output/driver-pipe-unix.go)
- FACT: External-volume mode suppresses gain in ALSA/pipe paths, but the reviewed PulseAudio path manages stream volume without consulting that setting. Do not promise backend-independent unity gain. [PulseAudio source](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/output/driver-pulseaudio.go)
- FACT: Code license is GPLv3; an explicit project-specific “or later” grant was not established. [License](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/LICENSE). INFERENCE: strongest ready-made technical PCM/API sidecar, but service authorization and real-device operation remain UNKNOWN.

### librespot

- FACT: Latest release reviewed: [v0.8.0](https://github.com/librespot-org/librespot/releases/tag/v0.8.0), 2025-11-10, commit `d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d`. Current dev `4a005903e078d512456fb008aaa9ba6246881bd9` has additional behavior; it must not be confused with the release.
- FACT: CI explicitly configures `armv7-unknown-linux-gnueabihf`; that is build-target evidence, not XU4 playback acceptance. ALSA/PulseAudio are selectable features; pipe/subprocess output is also available. [CI](https://github.com/librespot-org/librespot/blob/d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d/.github/workflows/cross-compile.yml), [manifest](https://github.com/librespot-org/librespot/blob/d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d/Cargo.toml)
- FACT: Premium is required. Upstream warns that use against Spotify may be forbidden; this is an upstream warning, not a definitive legal ruling. Code is MIT-licensed. [README](https://github.com/librespot-org/librespot/blob/d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d/README.md), [license](https://github.com/librespot-org/librespot/blob/d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d/LICENSE)
- FACT: CLI hooks report playback/metadata/volume and optional sink lifecycle; periodic position events are excluded. No upstream REST/WS control server was found in the reviewed CLI. The Spirc library offers controls, requiring a wrapper for a local bidirectional API. [Hooks](https://github.com/librespot-org/librespot/blob/d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d/src/player_event_handler.rs), [Spirc](https://github.com/librespot-org/librespot/blob/d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d/connect/src/spirc.rs)
- FACT: PCM is 44.1 kHz stereo; pipe writes to stdout/file/FIFO with selectable integer/float formats. Sample bytes are native-endian. [Constants](https://github.com/librespot-org/librespot/blob/d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d/playback/src/lib.rs), [pipe](https://github.com/librespot-org/librespot/blob/d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d/playback/src/audio_backend/pipe.rs), [conversion](https://github.com/librespot-org/librespot/blob/d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d/playback/src/audio_backend/mod.rs)
- FACT: Stable CLI `fixed` volume does not set the Connect disable-volume flag, unlike current dev. Its mixer mapping can still attenuate. INFERENCE: fixed plus initial volume 100 is not a proven unity-gain contract. [Stable config](https://github.com/librespot-org/librespot/blob/d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d/src/main.rs#L1524-L1536), [mapping](https://github.com/librespot-org/librespot/blob/d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d/playback/src/mixer/mappings.rs#L14-L38), [dev change](https://github.com/librespot-org/librespot/blob/4a005903e078d512456fb008aaa9ba6246881bd9/src/main.rs#L1549-L1561)
- INFERENCE: Explicit ARMv7 build support and permissive code licensing are attractive, but the extra control wrapper and unresolved provider permission make this a larger integration task.

## Audio and ownership blocker

FACT: The renderer's EQ/balance filters and gain confirmation apply to mpv. A second client sending audio directly to ALSA/PulseAudio/PipeWire does not traverse that graph. Its volume percentage cannot be represented as confirmed mpv volume.

FACT: mpv supports raw audio with an explicit sample format, channel layout and sample rate. This proves a demuxer exists, not that a Spotify pipe behaves like a normal queued track. [mpv manual](https://mpv.io/manual/stable/)

INFERENCE: A future PCM bridge would need exact format negotiation, bounded backpressure, FIFO start/close behavior, pause/seek flushing, stale-buffer suppression, reconnect rules, track identity and timing, and one authoritative gain mapping. Spotify-side seek/queue state and mpv's continuous pipe position would differ. Do not invent track URLs, infer successful playback from incoming events, double-apply volume, or promise gapless playback. Soloist has no reviewed documented PCM contract for this route; capturing a system sink is a separate design and terms question.

INFERENCE: One owner must control admission to audible output before either engine can play. Merely pausing or deactivating Spotify and observing `is_active=false` is not documented proof of released output; a phone can later reactivate it. Detecting an event and stopping the other engine afterward leaves an overlap race.

The arbitration design must cover all paths, not just Web UI buttons:

1. Legacy HTTP play/resume/stop, URL loads, preset recall and queue controls.
2. SOAP AVTransport/PlayQueue, favorites, and any other existing provider resolver.
3. EOF-driven advance, native staged successors, stream recovery, seeks and their cancellation/drain paths.
4. Spotify-originated Connect transfers, spontaneous remote play, queue transitions and volume changes.
5. Renderer/client restarts, partial command failures, WS loss, output-device loss and stale process events.

A handover requires a new ownership epoch, invalidation and drain of retired work, confirmed silence or a trustworthy output gate, and evidence that only the admitted engine can reach the sink. Timeout or unknown ownership must fail closed. New locks cannot be casually inserted around the existing backend/gain/DSP order. No such whole-renderer arbitration is implemented or proven here.

## License and provider terms are separate gates

FACT: Spotify's [Developer Policy](https://developer.spotify.com/policy) III.5–7 restricts products integrated with other services' streams/content and mixing/overlapping audio. Its [Developer Terms](https://developer.spotify.com/terms) II.14 includes controlling a background Spotify application within its streaming definition.

INFERENCE: A sidecar boundary therefore does not itself resolve service-policy obligations.

FACT: Spotify's [User Guidelines](https://www.spotify.com/us/legal/user-guidelines/) restrict reverse engineering subject to applicable-law exceptions, circumvention and unauthorized copying/redistribution. Its [US terms](https://www.spotify.com/us/legal/end-user-agreement/) provide personal, non-commercial use subject to the applicable agreements; the user's own jurisdiction and account terms must be checked.

INFERENCE: Soloist's newer documentation and announcement explicitly support standalone headless playback and local DIY controls; the announcement even mentions multi-room setups. The relationship between those specific invitations and the broader Developer Policy is not settled by this research. Do not present standalone Soloist as prohibited, or assume generic policy text automatically forbids every DIY use. This project's combined multi-provider, shared-PCM/DSP and redistribution scope still merits provider clarification or qualified legal review before shipping an integrated feature. An open-source client's code license does not authorize Spotify service access. No OEM identity, borrowed provisioning, DRM bypass, cached-content export or proprietary redistribution is an acceptable solution.

## Lifecycle requirements

INFERENCE: A future optional client should have its own private state/cache, bounded restart policy and explicit ownership; a renderer status poll must never spawn or pair it. Treat missing control readiness, expired build, authentication failure and unknown output ownership as unavailable. Do not restart-loop exit 10 or evade build expiry. Preserve the user-owned session on ordinary restarts, bound cache growth, and redact keys/account data. Measure CPU, memory and audio underruns on the actual XU4; no resource benchmark was run.

## Smallest defensible next stage

For this phase: retain this ADR and honest capability status only. Do not spend remaining time on a fake integration or broad player-core rewrite.

For a separately authorized experiment: have the user install the official client and complete their own key creation/pairing. Start with standalone Soloist controlled by the Spotify app, isolated from renderer audio. The conservative shared-device approach is mutually exclusive whole-service operation with confirmed shutdown and output release; it temporarily makes renderer controls unavailable and is not a unified Web UI. External/unowned mpv processes cannot be killed or assumed silent. A durable supervisor/output-ownership design would need separate review before automatic switching.

If the desired outcome is one UI, shared EQ/volume and seamless source changes, first approve the broader ownership design and settle the provider-policy scope. A passive loopback adapter and synthetic contract tests may follow; it must not launch, authenticate, activate, modify or masquerade as real music playback.

## Acceptance gates before any integration claim

- ACCOUNT_PENDING: user-owned key/session, actual discovery and Connect transfer, playback controls, account disconnect/re-pair and content availability.
- HARDWARE_PENDING: exact XU4 OS/ABI, official ARM32 execution, working audio session and DAC, CPU/RSS/cache growth, underruns and restart endurance. Architecture support alone is insufficient. [XU4 manufacturer](https://www.hardkernel.com/shop/odroid-xu4/)
- CONTRACT_PENDING: fresh snapshot after reconnect, unknown/missing fields, slow-event loss, late events from an old process, dispatch ACK vs confirmed state, command uncertainty without blind retry, and control-API startup failure.
- ARBITRATION_PENDING: concurrent commands across every surface above; remote Spotify takeover while local playback runs; lost stop/deactivate acknowledgement; crash/restart during handover; no stale audio or automatic replay.
- DSP_PENDING: verified routing through the intended graph, one gain authority, mute restoration and measured output effects. Existing mpv readback does not prove provider audio received those effects.
- TERMS_PENDING: exact integration/distribution scope, applicable provider terms and any required approval. No Spotify branding/endorsement claim is made.

## Where remaining time has higher value

Finish the Web UI's final acceptance, then improve real DSP band/preset controls using the existing renderer transactions. Keep packaging/native checks and physical/original-app limitations precise. Those steps improve a working renderer without adding another uncertain playback authority.
