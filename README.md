# Open Room Renderer

A tiny Linux audio box you can run on a laptop or Raspberry Pi: it shows up on your LAN, accepts local playback commands, and plays audio through `mpv`.

It is an independent, experimental Linux/mpv-based local network audio renderer for hobbyist audio setups. Think: garage speaker, workshop radio, kitchen Raspberry Pi, or “I have an old Linux box and want it to behave like a network audio sink”.

It is alpha. It is useful. It is also a little weird in places because network audio apps are weird in places.

## Start Here

Use Linux with Python **3.11+**, venv support, and native **mpv 0.37+**. See
[Getting Started](GETTING_STARTED.md) for distribution prerequisites, ARM status,
audio checks, service setup, and uninstall instructions.
Debian 12/Bookworm's default mpv 0.35.1 is below this minimum; playlist
ancestry checks require properties added in mpv 0.37.

For an isolated user installation from this checkout:

```bash
python3 scripts/install-native.py --dry-run
python3 scripts/install-native.py
~/.local/share/open-room-renderer/venv/bin/open-room-renderer serve --config ~/.local/share/open-room-renderer/config.yaml
```

The installer checks prerequisites, installs into a new private prefix, records
source/dependency versions and owned files, and generates a reviewable user unit.
It does **not** install distro packages, start services, change firewall settings,
or use sudo. The default profile uses port **8080** and a stable local identity.

For editable development instead:

```bash
./scripts/dev-setup.sh
./scripts/run.sh
```

In a second terminal, with the development dependencies installed:

```bash
./scripts/test-curl.sh
./scripts/acceptance-open-source-client.sh
```

Curl checks HTTP endpoints. The acceptance helper also exercises a real
open-source client and local discovery. Live playback and discovery still depend
on the selected media, network, and audio device.

## What It Does Today

Working in this alpha:

- advertises itself on the local network,
- exposes a local renderer/control API,
- can be discovered by open-source WiiM/Linkplay-style clients,
- has real open-source client and app-shaped HTTP/SOAP/GENA test coverage; original Android app acceptance remains untested,
- can play TuneIn-style streams,
- can play direct DRM-free audio URLs,
- uses `mpv` for actual playback,
- supports play, pause, resume, stop, seek, volume, and mute,
- supports real multi-track queues and [Repeat One / Repeat All](docs/COMPATIBILITY_MATRIX.md#local-queue-repeat-contract),
- supports persistent device rename,
- supports channel balance,
- supports a 10-band parametric EQ through mpv filters.

Plex/cloud-library support was started as a local experiment, then disabled and hidden from the app-facing alpha profile. It is not a supported feature in this release.

In plain English: you run the Python service, it starts `mpv`, your controller sends it audio commands, and sound comes out of the Linux machine.

```text
controller app / client
        |
        | discovery + commands over LAN
        v
Open Room Renderer
        |
        | JSON IPC
        v
mpv
        |
        v
your speakers
```

## What It Is Not

This is not a polished commercial streamer. It is not an official product. It is not a cloud-service unlocker.

Not goals:

- no DRM circumvention,
- no protected streaming-service support,
- no auth/pairing/signature/certificate bypass,
- no copied app or firmware code,
- no proprietary assets,
- no surfaced Plex/cloud-library feature in this alpha; that work is experimental and disabled,
- no guaranteed compatibility with proprietary apps,
- no real multiroom sync yet.

## Quick Checks

After starting either default profile:

```bash
curl "http://127.0.0.1:8080/httpapi.asp?command=getStatusEx" | jq
curl "http://127.0.0.1:8080/description.xml"
```

With a development checkout, run `./scripts/test-curl.sh` for playback diagnostics
or `./scripts/acceptance-open-source-client.sh` for the open-source client path.
Both default to port 8080. An explicit `API_PORT` / `UPNP_PORT` overrides it.

## Running It Day To Day

`./scripts/run.sh` and `./scripts/run-dev.sh` select the neutral, unprivileged
profile in `configs/config.example.yaml`. Neither invokes sudo or replaces the
persisted UUID on restart. The native installer also generates a user-service
unit; review [Getting Started](GETTING_STARTED.md) before enabling it.

Use `ORR_CONFIG=/absolute/path/config.yaml ./scripts/run.sh` to retain an explicit
custom YAML profile. The old `fake-linkplay` command remains a compatibility alias.

The `status`, `play`, `pause`, `resume`, `stop` and `volume` CLI commands print
successful HTTP response bodies and exit zero. HTTP errors, redirects, timeouts
and connection failures exit nonzero with a short error on stderr. Redirects are
not followed; use the renderer's direct host and port. Error messages omit the
request URL and response body because media URLs may contain credentials.

## Trying The Phone App

App compatibility is an explicit experimental profile:

```bash
./scripts/run-app-compat.sh
```

This enables app-shaped discovery, SSDP, mDNS, and only the implemented
TuneIn-style service surface, on port **8080** by default. It keeps a stable,
separate identity in `.state/app-compat`. A compatible response shape does not
prove original Android app acceptance.

Some controller apps probe plain `http://device-ip/` (port 80) even when discovery
advertises another port. To explicitly request that endpoint:

```bash
APP_COMPAT_PORT80=1 ./scripts/run-app-compat.sh
```

The runner checks whether it can bind the requested low port and explains a
failure; it never elevates the whole renderer. For a dedicated appliance, the
optional non-root system service limits its capability to `CAP_NET_BIND_SERVICE`.
It requires administrator review and manual installation; see
[Getting Started](GETTING_STARTED.md#optional-app-compatible-port-80-service).
Do not give a shared Python interpreter global capabilities or run the renderer
as root to work around the check.

`APP_COMPAT_FRESH=1` deliberately creates a new testing identity. Normal starts
no longer do so. Existing invocations of `run.sh` with explicit `APP_COMPAT_*`
variables route to the separate app runner. To keep an old generated identity,
use its existing YAML via `ORR_CONFIG`; see the migration notes in Getting Started.

Default neutral ports:

- HTTP API and UPnP description: TCP `8080`
- SSDP: UDP `1900`
- mDNS, when enabled: UDP `5353`
- mpv IPC: private per-install socket (checkout: `.state/mpv.sock`)

Use this unauthenticated control service only on a trusted LAN. Do not publish
its ports to the internet.

## If Discovery Does Not Work

Most discovery problems are boring network problems:

- phone and renderer are not on the same subnet,
- VPN is eating multicast,
- Wi-Fi client isolation is enabled,
- firewall blocks UDP multicast or local HTTP,
- the advertised IP is wrong.
- mDNS is disabled in the neutral checkout config; the native install and app-compat profiles enable it.
- the app cached an older failed attempt.

Useful local helper:

```bash
./scripts/sniff-ssdp.sh
```

That script explains before it asks for `sudo`: packet capture normally needs elevated privileges to read local multicast traffic. Do not paste raw captures into public issues.

## Configuration

Start with:

```bash
configs/config.example.yaml
```

Public examples use neutral names and synthetic values. Runtime state such as generated UUID, device rename, EQ presets, and account state belongs in `.state/`, which is intentionally ignored.

Verbose HTTP logging is off by default. Keep it that way for normal use. If you enable it for private debugging, sanitize before sharing anything.

## Project Status

Alpha. Expect rough edges.

Stable enough to explore:

- discovery,
- basic playback,
- TuneIn-style flow,
- direct URLs,
- mpv backend,
- balance,
- 10-band EQ.

Still experimental:

- proprietary app behavior,
- SOAP/eventing completeness,
- long-form seek edge cases,
- Raspberry Pi packaging,
- Plex/cloud-library adapter, which is experimental and disabled/not surfaced,
- optional service adapters.

## Disabled Cloud-Library Experiments

Plex/cloud-library scaffolding is not a supported playback integration. Even an
explicit experimental configuration keeps its capability item inactive and
excludes it from playable music services and legacy support bits. A configured
label or stored account shape is not evidence of server selection, browsing or
working playback. No provider login, registration or token-based acceptance was
performed in this work; existing disabled account scaffolding remains. Do not reuse another application's OAuth
callback or OEM credentials for future integrations.

## Docs Worth Reading

- [GETTING_STARTED.md](GETTING_STARTED.md): friendliest first run.
- [docs/demo.md](docs/demo.md): simple demo flow.
- [docs/architecture.md](docs/architecture.md): how the pieces fit.
- [docs/developer-map.md](docs/developer-map.md): where to start in the code.
- [docs/clean-room-linkplay-renderer-spec.md](docs/clean-room-linkplay-renderer-spec.md): behavior-level compatibility notes.
- [docs/app-service-overview.md](docs/app-service-overview.md): service/source labels and adapter priorities.
- [CONTRIBUTING.md](CONTRIBUTING.md): how to contribute safely.

## Compatibility And Trademark Note

Open Room Renderer is independent and unofficial. It is not affiliated with, endorsed by, sponsored by, or supported by WiiM, Linkplay, or related companies. Product and service names are used only where needed to describe interoperability.

Do not use this project to bypass authentication, pairing, signatures, DRM, encryption, certificates, access controls, or other technical protection measures.

## License

Open Room Renderer is licensed under **GNU GPLv3 or later** (`GPL-3.0-or-later`).

See [LICENSE](LICENSE). In short: you may use, study, share, and modify the project, but distributed modified versions must keep the same copyleft freedoms.

## Contributing

Small PRs are very welcome: docs, tests, Raspberry Pi notes, mpv robustness, config validation, better errors, and sanitized examples.

Good first tasks:

- improve Raspberry Pi OS setup notes,
- extend native installation and systemd hardware verification,
- improve missing-mpv error messages,
- add firewall/discovery troubleshooting,
- add more config validation tests,
- add more sanitized log examples.

Please read [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md) first.
