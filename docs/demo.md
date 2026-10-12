# Demo: Make It Make Noise

This is a safe local demo. No app internals, no raw dumps, no secret sauce.

## Start The Renderer

```bash
./scripts/run.sh
```

Keep it running. The neutral profile uses TCP port 8080 and a persistent identity.
It does not invoke sudo. Install prerequisites first as described in
[Getting Started](../GETTING_STARTED.md).

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

To deliberately test app-shaped discovery, stop the neutral runner first and use:

```bash
./scripts/run-app-compat.sh
```

This is an experimental compatibility profile with SSDP and mDNS. The original
Android app acceptance remains untested; protocol tests do not substitute for it.

The app-compat profile advertises the implemented TuneIn-style queue interface.
This does not establish current public-provider or original-app acceptance.
Unsupported provider services stay inactive rather than appearing as playable tiles.

If it does not appear:

- same Wi-Fi/subnet,
- VPN off,
- router client isolation off,
- firewall not blocking local traffic,
- multicast working.
- mDNS enabled when using the app-compat profile,
- app restarted after old failed discovery attempts.

Both runners default to port **8080**. Some controller apps still probe plain
`http://device-ip/` (port 80) despite the advertised discovery port. An explicit
port-80 request is:

```bash
APP_COMPAT_PORT80=1 ./scripts/run-app-compat.sh
```

The runner checks the bind prerequisite and reports failure rather than invoking
sudo. Review the optional dedicated non-root system-service template in
[Getting Started](../GETTING_STARTED.md#optional-app-compatible-port-80-service)
if your appliance needs port 80. No global interpreter capabilities or firewall
changes are needed for the normal high-port demo.

For bounded unprivileged address hints, use the [explicit-interface discovery CLI](../GETTING_STARTED.md#find-a-renderer-address-with-bounded-ssdp-discovery).
Its results are unauthenticated candidates; it does not fetch or control them.

Optional private packet-diagnosis helper:

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
