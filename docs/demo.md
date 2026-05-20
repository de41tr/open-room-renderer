# Demo: Make It Make Noise

This is a safe local demo. No app internals, no raw dumps, no secret sauce.

## Start The Renderer

```bash
./scripts/run.sh
```

Keep it running. It will print the URLs it is advertising.

## Check The Boring Stuff

In another terminal:

```bash
./scripts/test-curl.sh
```

If that works, the renderer can answer HTTP commands and ask `mpv` to play a test file.

## Check The Better Stuff

```bash
./scripts/acceptance-open-source-client.sh
```

This verifies the project through a client-level path, not just curl.

## Try Discovery From Another Device

Use a controller app on the same network and look for the renderer name.

For app discovery, prefer:

```bash
./scripts/run.sh
```

`run-dev.sh` is a developer diagnostics path and is not the recommended demo start command.

The app-compat profile only advertises the TuneIn-style service path that currently works. It should not show extra music-service tiles that the renderer cannot actually handle.

If it does not appear:

- same Wi-Fi/subnet,
- VPN off,
- router client isolation off,
- firewall not blocking local traffic,
- multicast working.
- mDNS enabled by using `run.sh`,
- app restarted after old failed discovery attempts.

The renderer uses port 80 by default:

```bash
./scripts/run.sh
```

Port 80 is the default because some controller apps still probe the classic `http://device-ip/` endpoint during discovery or setup. Using it by default avoids a common discovery failure.

To run app-compat mode without sudo/port 80:

```bash
APP_COMPAT_PORT80=0 ./scripts/run.sh
```

Discovery helper:

```bash
./scripts/sniff-ssdp.sh
```

The script explains before it asks for `sudo`: packet capture normally needs elevated privileges to read local multicast traffic. Keep captures private.

## What To Demo

Good demo path:

1. Start the renderer.
2. Run acceptance.
3. Start a TuneIn-style stream or direct test URL.
4. Pause/resume.
5. Change volume.
6. Change balance.
7. Change the 10-band EQ.
8. Stop playback.

That shows the whole current loop:

```text
controller -> renderer -> mpv -> speakers
```

## What Not To Demo

Do not demo:

- protected streaming services,
- credential extraction,
- DRM bypass,
- raw packet dumps,
- copied app/firmware material.

This project is about local audio interoperability and DRM-free/user-owned playback paths.
