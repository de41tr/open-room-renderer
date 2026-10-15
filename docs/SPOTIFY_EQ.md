# Opt-in Spotify PCM through the existing ORR EQ

This is a separate **opt-in software candidate**, not an installed or accepted
XU4 Spotify feature. It keeps the existing ORR Web UI, gain, balance and EQ running:

`go-librespot v0.10.3 -> private 44.1 kHz stereo f32le FIFO -> existing mpv/DSP -> configured ALSA output`

There is one DAC owner: ORR's own mpv. The receiver cannot select an ALSA output
in this mode. No second EQ, decoder, playback queue, audio server or kernel
loopback is introduced. The existing exclusive receiver remains unchanged.

## Verified upstream contract

The exact existing pin is
[`4dbc099f46da3529adaa85feb97c4f49deb0b130`](https://github.com/devgianlu/go-librespot/tree/4dbc099f46da3529adaa85feb97c4f49deb0b130):

- [`output/driver-pipe.go`](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/output/driver-pipe.go) implements `f32le` and software volume. Its `Drop` is a no-op and `DelayMs` reports zero; this candidate does not promise gapless/seek timing or exact output latency.
- [`output/driver-pipe-unix.go`](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/output/driver-pipe-unix.go) opens an existing FIFO. ORR retains a private keeper descriptor across upstream track/stream closures; only mpv consumes audio. There is no recorded PCM file.
- [`player/player.go`](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/player/player.go#L31-L34) fixes output at 44100 Hz and two channels. This PCM format is not the Spotify source codec or a claim of lossless service support.
- [`daemon/app.go`](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/daemon/app.go#L186-L190) enables discovery for `credentials.type: zeroconf`; the [local app callback](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/daemon/app.go#L693-L717) accepts the session selected by the user's Spotify app. Browser/device-code pairing is not mandatory for this mode.

## Preparation is not activation

Linux, Python >=3.11, native mpv and the separately verified pinned client are
required. Use the existing [`spotify install-client`](SPOTIFY_RECEIVER.md#client-provenance-and-actual-arm-abi)
procedure only when installation is authorized. ARMv7 uses the existing verified
ARM hard-float pin; x86/ARM64 tests do not establish XU4 execution.

Prepare a **new**, private directory. This checks the pinned client and native
loader dependencies, but starts no daemon, listener, discovery, login or audio:

```sh
open-room-renderer spotify eq --directory /absolute/private/spotify-eq setup \
  --client-dir /absolute/private/verified-client \
  --name "Studio Spotify" --bitrate 160 --port 3678
```

Do not reuse the exclusive receiver directory. Setup will not overwrite an
existing directory. The new profile contains only bounded settings, verified
client identity and a newly generated stable receiver device ID. It contains
no credentials. Keep it private and out of support exports/Git.

## Existing user service, separately authorized activation

The existing unit stays unchanged: its `ExecStart` still runs
`open-room-renderer serve --config /absolute/path/to/existing-config.yaml`.
There is no second renderer, user unit, installation prefix, state directory,
playback backend or DAC owner. This also supports an external config and the
existing shared state/run directories behind a versioned `current` installation.

`spotify_eq_profile` defaults to `null`. **Setup never writes this setting.**
Only after separate approval to enable LAN Spotify discovery/account contacts,
an operator can set this startup-only key in that existing external YAML:

```yaml
spotify_eq_profile: /absolute/private/spotify-eq
```

The normal authorized `systemctl --user restart open-room-renderer.service`
then starts the same ORR service with one owned PCM receiver child. A later
service restart starts discovery again while this key remains enabled. To
remove the activation, remove the key (or set `null`) and restart the same
service. No daemon reload, unit replacement or enabling a second unit is needed.
The child remains in the service cgroup; ordinary ORR shutdown reaps it first.
A Spotify startup failure leaves the existing ORR UI available with an error and
Stop action, rather than repeatedly restarting the receiver.

A deployment adapter should keep these distinct fixed stages:

1. Read-only preflight: report current release/config, explicit output, existing
   service state, client receipt and whether this key is disabled; do not migrate
   the current profile or weaken the legacy exclusive-receiver installer guards.
2. Prepare the verified client and the new profile using the command above,
   only under installation approval. Report `PREPARED`, `activated: false`.
3. Separately approved activation: back up and change only this YAML key, then
   restart the existing service. Report service/process ownership and discovery
   readiness separately from account, audio and hardware acceptance.
4. The user selects it in their Spotify app. Confirm actual playback and EQ by
   the real app/listening acceptance, not merely process or API readiness.
5. Stop or normal-source selection reaps the receiver and keeps ORR usable.
   Removing the key and restarting restores the ordinary startup path.

This candidate supplies the application behavior, not a host-specific updater or
remote service-control adapter. Existing host guards must gain an explicit,
reviewed opt-in path rather than being loosened globally.

## Explicit foreground activation, separately authorized

Review provider/account eligibility before activation. A free software license
is not Spotify service authorization. Stop the normal renderer and any previous
exclusive receiver yourself through the previously approved control route;
ensure no independent activation path can restart a competing renderer. This
command does **not** stop services, alter units or enable anything for boot:

```sh
open-room-renderer spotify eq --directory /absolute/private/spotify-eq run \
  --renderer-config /absolute/path/to/existing-config.yaml
```

The config may be external to the installation, with existing shared state/run
directories. It is read, never rewritten or migrated. Requirements are:

- `auto_spawn_mpv: true`, a private existing mpv socket directory, and an explicit
  `alsa/hw:CARD=NAME,DEV=N` or `alsa/plughw:CARD=NAME,DEV=N` output
- Existing literal IPv4 renderer listeners must be free. Their sockets are
  reserved before identity/state or mpv access, so a duplicate start cannot
  mutate the running renderer. A live foreign mpv/socket is never adopted. No port/firewall/kernel/system changes
- The selected loopback control port must differ from renderer ports and be
  available. Controls verify the established connection belongs to the owned
  receiver child before sending HTTP bytes

Activation starts normal ORR plus one owned receiver child. The receiver uses
upstream's built-in `_spotify-connect._tcp` mDNS and a dynamic LAN pairing
listener, and contacts Spotify. Use a trusted LAN. The user selects the receiver
in their own Spotify app on the same network. No browser code, token import or
automated account action is required by this path. Actual discovery and account
acceptance still need a real user/app test.

`persist_credentials: false` is fixed. Each run uses a fresh 0700 runtime;
upstream stdout/stderr are discarded, audio caching/metadata caching are off,
and no account files are copied from another receiver. Receiver identity alone
is stable across runs. The user may need to select it anew after restart.

## Controls and safe return

- Spotify starts at volume zero. Raise it cautiously in the Spotify app. ORR's
  existing master volume/mute is also applied; the two controls are independent.
- Use the existing ORR EQ, presets, bypass and balance controls. They affect the
  same mpv filter graph used by ordinary playback.
- Spotify app controls own its track queue, seek and next/previous. ORR supports
  pause/resume for an active observed session. Metadata is a bounded provider
  snapshot; PLAYING also requires a coherent active native audio observation.
- ORR **Stop**, or selecting an ordinary radio/media source, terminates and
  reaps this receiver before stopping its FIFO reader. Replacement is allowed
  only after actual mpv idle. A failed/unknown stop blocks replacement rather
  than starting another owner. Stop does not restart Spotify automatically.
- After Stop, the same ORR process/UI can play normal sources. To enable Spotify
  again, explicitly restart the enabled existing service, or exit the foreground
  run and explicitly run it again. This first
  candidate does not add automatic source takeover or a new UI activation button.
- Ctrl-C/SIGTERM tears down the owned receiver and renderer. Then use the
  previously authorized normal renderer start path. Existing service boot
  settings, renderer config, favorites and saved EQ remain in place.

FIFO buffering is bounded with file-local mpv options. Pauses/seeks/track changes
may retain a short buffered tail; timing must be checked on the real device.
Nothing in software status is a physical sound or audibility measurement.

## Acceptance evidence and remaining gates

- Focused tests cover private nonactivating setup, existing-service opt-in,
  real listener collision before any state/backend access, failed Stop blocking
  a new source, and cancellation not abandoning producer cleanup.
- A real owned synthetic PCM producer, existing ORR EQ and actual libmpv digital
  PCM output measured a -12 dB cut amplitude ratio of approximately **0.25119**,
  and existing EQ bypass approximately **1.00011** in two fully completed
  digital segments, on mpv **0.37.0 and 0.40.0**. Producer reaping, FIFO cleanup
  and ordinary-source input-option restoration were confirmed.
- Those local measurements use the existing test-only libmpv command adapter.
  The same test also proves live EQ bypass with continued native position
  progress through null output. The CI-only production Unix IPC test repeats
  that live-bypass/source-return proof using the real production transport.
- **Known measurement-path limitation:** mpv 0.37's untimed `ao=pcm` file sink
  stalls after a live filter removal with this paced FIFO. The live untimed-file
  transition is not claimed as passing. Static digital amplitudes and live
  clocked/null progress are separate evidence; no product workaround, skip or
  relaxed time limit hides this limitation.
- **NOT TESTED on XU4/KA17, no Spotify account/audio/discovery acceptance, no
  provider-policy clearance, no physical output or listening claim.** These
  gates remain until explicitly authorized and independently observed.
