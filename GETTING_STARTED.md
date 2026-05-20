# Getting Started

This is the short version. You should be able to go from “fresh checkout” to “renderer is running” in a few minutes.

## 1. Install The Basics Yourself

Use your distro's package manager to install:

```text
python3
Python venv support
mpv
curl
jq
ip / iproute2
tcpdump optional, only for packet sniffing
```

You need `mpv` because Open Room Renderer does not decode audio itself. It tells `mpv` what to play.

## 2. Set Up The Project

```bash
./scripts/dev-setup.sh
```

This does not install system packages. It checks for the tools above, creates `.venv`, installs the package editable, and installs the test dependencies.

## 3. Start It

```bash
./scripts/run.sh
```

You should see something like:

```text
Starting Open Room Renderer
Laptop IP: ...
Status: curl "http://.../httpapi.asp?command=getStatusEx" | jq
Description: curl "http://.../description.xml"
Acceptance: ./scripts/acceptance-open-source-client.sh
```

Leave that terminal open. That is your renderer.

## 4. Poke It

Open another terminal:

```bash
./scripts/test-curl.sh
```

This does a quick “is the thing breathing?” check:

- status endpoint,
- player status,
- description XML,
- play test URL,
- pause,
- resume,
- stop.

## 5. Run The Real Check

```bash
./scripts/acceptance-open-source-client.sh
```

This is more important than curl. It checks that an open-source client-level path can talk to the renderer.

Passing acceptance means:

- discovery works locally,
- status can be read,
- a test MP3 can be started,
- pause/resume/stop work.

## 6. Try A Controller App

Now try a compatible controller on the same Wi-Fi.

For phone-app discovery, keep using the normal runner:

```bash
./scripts/run.sh
```

Why? `run.sh` turns on mDNS and uses the app-facing discovery shape. `run-dev.sh` is only a developer diagnostics path.

This release profile advertises only the TuneIn-style service path that currently works. It intentionally does not make non-functional music-service tiles appear in the app.

If it does not show up, do not panic. Discovery is often just network plumbing:

- same subnet?
- VPN off?
- firewall relaxed?
- router client isolation off?
- multicast allowed?
- app force-stopped/reopened after earlier failed attempts?

By default, the renderer uses HTTP port 80:

```bash
./scripts/run.sh
```

Why this exists: some controller apps expect real audio devices to answer on plain HTTP port 80, even when discovery advertises another port. The script explains before it asks for `sudo`: Linux normally restricts ports below 1024, including HTTP port 80, to privileged processes.

If you want to test without sudo/port 80:

```bash
APP_COMPAT_PORT80=0 ./scripts/run.sh
```

For local debugging:

```bash
./scripts/sniff-ssdp.sh
```

Do not publish raw captures. They can contain local network details.

## What You Can Expect

Good today:

- direct URL playback,
- TuneIn-style stream playback,
- mpv audio output,
- play/pause/stop,
- volume/mute,
- balance,
- 10-band EQ.

Rough today:

- proprietary app compatibility,
- Raspberry Pi service packaging,
- some seek behavior on long streams,
- advanced service browsing.

## Stop It

Press `Ctrl+C` in the terminal running `./scripts/run.sh`.

That is it. Tiny audio sink achieved.
