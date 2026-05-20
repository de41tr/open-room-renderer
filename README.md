# Open Room Renderer

A tiny Linux audio box you can run on a laptop or Raspberry Pi: it shows up on your LAN, accepts local playback commands, and plays audio through `mpv`.

It is an independent, experimental Linux/mpv-based local network audio renderer for hobbyist audio setups. Think: garage speaker, workshop radio, kitchen Raspberry Pi, or “I have an old Linux box and want it to behave like a network audio sink”.

It is alpha. It is useful. It is also a little weird in places because network audio apps are weird in places.

## Start Here

Install the system programs with your distro's package manager:

```text
python3, Python venv support, mpv, curl, jq, ip/iproute2
optional: tcpdump
```

Then set up and start the project:

```bash
./scripts/dev-setup.sh
./scripts/run.sh
```

The setup script does not install distro packages. It only checks what it needs, prints missing tools, creates `.venv`, and installs the Python package.

In a second terminal:

```bash
./scripts/test-curl.sh
./scripts/acceptance-open-source-client.sh
```

If the acceptance script passes, the basics are alive: discovery, status, play, pause, resume, and stop through an open-source client-level path.

## What It Does Today

Working in this alpha:

- advertises itself on the local network,
- exposes a local renderer/control API,
- can be discovered by open-source WiiM/Linkplay-style clients,
- has been tested with WiiM Home-style local network control,
- can play TuneIn-style streams,
- can play direct DRM-free audio URLs,
- uses `mpv` for actual playback,
- supports play, pause, resume, stop, seek, volume, and mute,
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

After `./scripts/run.sh`, try:

```bash
curl "http://127.0.0.1/httpapi.asp?command=getStatusEx" | jq
curl "http://127.0.0.1/description.xml"
```

Or just use:

```bash
./scripts/test-curl.sh
```

The real smoke test is still:

```bash
./scripts/acceptance-open-source-client.sh
```

Curl proves the web endpoints respond. Acceptance proves a real open-source client path can control it.

## Running It Day To Day

Normal start:

```bash
./scripts/run.sh
```

Developer diagnostics mode:

```bash
./scripts/run-dev.sh
```

`run-dev.sh` uses the plain example config and is mostly useful when you are debugging scripts, curl output, or open-source client acceptance. For phone-app discovery, use `run.sh`.

The old `fake-linkplay` command still exists as a temporary compatibility alias for older local scripts.

## Trying The Phone App

The default config now uses port 80 because some phone apps probe plain `http://device-ip/` during discovery or setup. Some phone apps also expect a more app-shaped discovery profile, especially mDNS.

Use the normal runner when you want to test discovery in the app:

```bash
./scripts/run.sh
```

This enables:

- SSDP,
- mDNS,
- app-compat identity/profile fields,
- HTTP and description XML on port `80`,
- a fresh device identity by default,
- only the TuneIn-style service surface that currently works.

The release profile intentionally does not advertise non-functional music services. Earlier local experiments could make extra services appear in the app, but showing buttons that do not work is annoying. For this alpha, the app-facing profile stays honest: TuneIn-style playback only.

Port 80 is the default. Linux requires elevated privileges for port 80, so the script explains why and then asks for `sudo`:

```bash
./scripts/run.sh
```

`run-app-compat.sh` is kept as a compatibility alias for older local workflows. New users should prefer `run.sh`.

Why port 80 can matter: many real network audio devices expose their local HTTP control surface on plain `http://device-ip/`, which means TCP port 80. Some controller apps correctly read the advertised SSDP/mDNS port, but still probe or assume port 80 during early discovery or handshake. If nothing answers there, the app may decide the device is missing or broken.

If you want the app-compat profile without sudo/port 80, run:

```bash
APP_COMPAT_PORT80=0 ./scripts/run.sh
```

Also force-stop and reopen the phone app after failed attempts. These apps can cache discovery results pretty aggressively.

Default ports:

- HTTP API: `80`
- UPnP description: `80`
- SSDP discovery: UDP `1900`
- mpv IPC socket: `/tmp/open-room-renderer-mpv.sock`

## If Discovery Does Not Work

Most discovery problems are boring network problems:

- phone and renderer are not on the same subnet,
- VPN is eating multicast,
- Wi-Fi client isolation is enabled,
- firewall blocks UDP multicast or local HTTP,
- the advertised IP is wrong.
- mDNS is off because you used a custom/dev config instead of `run.sh`.
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

## Continuing The Plex Experiment

Plex is deliberately **off** in the alpha runner. If someone wants to continue that experiment locally, use a private config copy and do not share the resulting state files:

```bash
mkdir -p .state
cp configs/config.app-compat.yaml .state/plex-experiment.yaml
```

Edit `.state/plex-experiment.yaml`:

```yaml
enable_plex_service: true
enable_plex_legacy_gate: false
plex_capability_mode: "exact"
plex_user_info_mode: "not_login"
```

Then start that config directly:

```bash
sudo .venv/bin/open-room-renderer serve --config .state/plex-experiment.yaml
```

`sudo` is only for binding port `80`. If you move the experiment to an unprivileged port, you can run without it, but some controller-app discovery flows may stop seeing the renderer.

For a manually authorized Plex token, the experimental code looks for `.state/plex_user_info.json`. Use only a token from your own Plex account and never commit or paste this file:

```json
{
  "source": "Plex",
  "accountSource": "Plex",
  "msg": "success",
  "userId": "synthetic-user-id",
  "userName": "plex-user",
  "showName": "Plex User",
  "email": "",
  "accessToken": "REDACTED_USER_TOKEN",
  "refreshToken": "",
  "expires": 4102444800,
  "subscription": "0",
  "subscriptionDescription": "",
  "country": "US",
  "uuid": "00000000-0000-4000-8000-000000000000",
  "clientIdentifier": "open-room-renderer-dev-client",
  "sourceVersion": "1.4",
  "capabilityVersion": "1.4",
  "replayGain": true,
  "redirectUrl": "com.linkplay.wiimhome%3A%2F%2Fcallback%3Fcategory%3Doauth%26service%3Dplex"
}
```

This is not a supported feature yet. The missing work is reliable account state, server selection, library browsing, artwork proxying without leaking tokens, and playback URLs that the renderer can hand to `mpv`.

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
- add mpv version detection,
- improve missing-mpv error messages,
- add firewall/discovery troubleshooting,
- add more config validation tests,
- add more sanitized log examples.

Please read [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md) first.
