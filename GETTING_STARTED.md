# Getting Started

The default installation runs as your normal Linux user on **port 8080**. It
keeps its identity across restarts and never automatically invokes sudo.

## 1. Native Prerequisites

Install these using your distribution's package manager:

- Python **3.11 or newer**, with `venv` / `ensurepip` support
- native **mpv 0.35 or newer**, with its distribution-provided codec/audio libraries
- `curl`, `jq`, and `iproute2` for the checkout's diagnostics
- optional: `alsa-utils` for ALSA checks; `tcpdump` for private packet diagnosis

For Debian 12/Bookworm or newer, the usual package names are `python3`,
`python3-venv`, `mpv`, `curl`, `jq`, and `iproute2`. Debian's
[Python package](https://packages.debian.org/bookworm/python3) and
[mpv package](https://packages.debian.org/bookworm/mpv) are available for `arm64`
and `armhf`. Package availability is not hardware acceptance.

Python dependencies are `aiohttp`, `PyYAML`, `requests`, and `zeroconf`; `pywiim`
and pytest are development/test extras. They install into a virtual environment,
not the distribution's system Python. On ARMv7, a compatible wheel may not exist
for every dependency/version. Source builds may require your distro's compiler
and Python headers; keep them native to that OS/ABI. Do not copy an x86/ARM64 venv,
foreign shared libraries, or a different distribution's binary closure.

Validation limits:

- Native x86_64 and 64-bit AArch64 userspace CI passed Python tests, real libmpv null/digital-PCM,
  and production mpv Unix-IPC/lifecycle lanes in
  [this recorded run](https://github.com/de41tr/open-room-renderer/actions/runs/38187041169).
- **ARMv7 is untested**, including 32-bit ARM userspace on a 64-bit kernel.
  The installer records interpreter bitness and reports that limit.
- Raspberry Pi/XU4 hardware, audible output, systemd boot/reboot behavior, and
  original Android app acceptance still require separate acceptance.
- The installer enforces the mpv 0.35 profile baseline; this is not a claim that
  every mpv build, decoder, or output device has been exercised.

## 2. Install In A Private Prefix

From the checkout, as your normal account:

```bash
python3 scripts/install-native.py --dry-run
python3 scripts/install-native.py
```

The default prefix is `~/.local/share/open-room-renderer`. `--prefix /short/path`
selects a new dedicated directory; existing directories are refused. Use a short
path because Linux Unix sockets have a 108-byte path limit.

The installer checks Python, Linux architecture, mpv version, and optional ALSA
access. It snapshots this checkout's application sources, creates `venv/`,
installs the package with runtime dependencies, and writes:

- `config.yaml`: supported YAML (JSON syntax is also valid YAML), TCP port 8080
- `open-room-renderer.service`: an uninstalled user-service unit
- `installation-receipt.json`: source revision/hash, architecture, libc, Python,
  mpv, and resolved Python package versions
- `pip-install-report.json`: pip's artifact/version report for a full install
- `installed-files.json`: hashes/links for files owned by this installation
- `uninstall.py`: the same manifest-aware installer for later removal

This is a reproducible setup procedure with a version receipt, not a locked
cross-architecture dependency bundle. Save receipts privately; they may contain
local paths. The installer does not enable services, create accounts, change
network/security settings, or install system packages.

To inspect packaging without installing dependencies or requiring mpv:

```bash
python3 scripts/install-native.py --stage-only --dry-run --prefix /tmp/orr-stage
python3 scripts/install-native.py --stage-only --prefix /tmp/orr-stage
```

A staged-only tree is explicitly labeled and **cannot run** until a real install
is made into a new prefix. Failed installs retain an incomplete receipt and
owned-file manifest for inspection/removal.

## 3. Start And Check

```bash
~/.local/share/open-room-renderer/venv/bin/open-room-renderer serve --config ~/.local/share/open-room-renderer/config.yaml
```

Leave the terminal open. In another terminal:

```bash
curl -fsS 'http://127.0.0.1:8080/httpapi.asp?command=getStatusEx' | jq
curl -fsS 'http://127.0.0.1:8080/description.xml'
```

The API starting does not prove that sound works. Check your audio session/device:

```bash
mpv --audio-device=help
aplay -l
mpv --no-video /path/to/your/local-test.wav
```

`aplay` is an optional ALSA diagnostic, not a requirement for PipeWire/PulseAudio.
`--require-alsa` makes installation fail if no readable/writable ALSA PCM device
exists under `/dev/snd`. Without that flag, absence is a warning, because session
audio can work without direct ALSA access. Do not fix permissions with a broad
`chmod`. Select the desired device in the service account's mpv configuration;
see the [mpv audio-device documentation](https://mpv.io/manual/stable/#options-audio-device).

Stop with `Ctrl+C`. Restart uses the same `state/uuid`; do not copy that state
between independent renderers. Each needs its own prefix, state directory, IPC
socket, and, on the same host, a different `--port` (for example 8081). Discovery
also needs distinct identities. Two live multicast responders on one host still
need target-network acceptance.

## 4. Optional User Service

After reviewing the generated unit and successfully testing foreground playback:

```bash
mkdir -p ~/.config/systemd/user
# Do not overwrite an existing unit without inspecting it first.
cp -i ~/.local/share/open-room-renderer/open-room-renderer.service ~/.config/systemd/user/open-room-renderer.service
systemctl --user daemon-reload
systemctl --user enable --now open-room-renderer.service
journalctl --user -u open-room-renderer.service
```

The supplied `examples/systemd/user-service.example` uses the default prefix;
custom prefixes should use the generated unit. A user service generally follows
the user manager's login lifecycle. Boot without login/lingering is a separate
administrator decision, not something the installer enables. Reboot and physical
audio behavior have not been verified here. No low-port capability is required.

## 5. Network And DHCP

The API has no authentication. Use a trusted LAN and do not forward its ports to
the internet. The neutral checkout profile enables SSDP; native installation and
app-compat additionally enable mDNS. Controllers need the same reachable network:
TCP 8080 (or the chosen port), SSDP UDP 1900, and mDNS UDP 5353 where enabled.
Check existing firewall and AP isolation policy rather than disabling it broadly.

`bind_address` controls HTTP listeners; `advertise_ip: null` chooses an address
from the current default route. On a multi-interface machine, set the reachable
LAN IPv4 address explicitly when necessary. HTTP binding does not enforce a
shared interface selection for every discovery transport. DHCP/default-route
changes may require restarting the service and checking advertised URLs; there
is no claimed seamless interface rebinding. A DHCP reservation can simplify this.

## Editable Development And App Compatibility

For editing and running tests in the checkout instead of a native prefix:

```bash
./scripts/dev-setup.sh
./scripts/run.sh
# In another terminal:
./scripts/test-curl.sh
./scripts/acceptance-open-source-client.sh
```

Setup selects `python3` and verifies Python 3.11+. Override with
`PYTHON=python3.12 ./scripts/dev-setup.sh` when needed. The helper installs the dev
extras; the native installation deliberately omits them. Curl alone is diagnostic;
the acceptance helper uses a real open-source client and local SSDP.

`run.sh` / `run-dev.sh` use the neutral YAML, high ports, and persistent `.state`.
`./scripts/run-app-compat.sh` explicitly selects app-shaped discovery and only the
implemented TuneIn-style service surface, also on port 8080 with stable
`.state/app-compat`. Some proprietary controllers insist on TCP 80 despite the
advertised discovery port; their acceptance remains untested.

### Optional App-Compatible Port-80 Service

`APP_COMPAT_PORT80=1 ./scripts/run-app-compat.sh` explicitly requests port 80.
The runner checks the bind prerequisite and gives a diagnostic instead of running
sudo. Some Linux systems allow low ports unprivileged; others deny them. The
script does not change that policy.

For an administrator-managed appliance, review these **manual templates**:

- `examples/systemd/system-port80.service.example`
- `examples/systemd/config.port80.yaml`

They expect a dedicated `open-room-renderer` account, a root-owned application
venv under `/opt/open-room-renderer/venv`, and a reviewed config at
`/etc/open-room-renderer/config.yaml`. Build/install from the same native source
and dependencies; an administrator must provision those paths/account and copy
the reviewed unit to `/etc/systemd/system/open-room-renderer.service`. The normal
user installer does not perform these steps and must not be run with sudo.

The process runs as the dedicated account with only `CAP_NET_BIND_SERVICE` in
its ambient/bounding capability sets. State is under `/var/lib/open-room-renderer`
and IPC under `/run/open-room-renderer`, created by systemd. The unit restricts
writes and hides home directories. Direct ALSA device access must be reviewed for
that account; the commented audio-group line is not universally sufficient or
necessary. A desktop user's PipeWire/PulseAudio session is not automatically
available to this system service.

Do not add capabilities to a shared Python interpreter, globally relax low-port
policy, or run the whole renderer as root. The optional system unit is reviewed
configuration, **not a verified systemd/boot/hardware deployment**. Do not enable
both profiles on the same port.

### Migrating Earlier Checkout Runners

Earlier `run.sh` used app-compat, port 80, and a newly generated identity every
start. It now uses the neutral stable 8080 profile. An explicit `APP_COMPAT_*`
variable still routes `run.sh` to the app runner, but fresh identity is opt-in:
`APP_COMPAT_FRESH=1`. Port 80 is separately opt-in: `APP_COMPAT_PORT80=1`.

To keep the last old generated identity/config, retain that file explicitly:

```bash
ORR_CONFIG=.state/app-compat-tmp/config.yaml ./scripts/run-app-compat.sh
```

This uses the file unchanged, including its ports/state/socket. Review it first;
update to high ports and a private writable IPC path if needed. Old root-owned
state may need an administrator-reviewed ownership migration. Back up the state;
do not erase it or run as root just to suppress permission errors. Custom YAML is
always explicit via `ORR_CONFIG`; TOML was never supported by the loader and its
old example has been replaced with `examples/config.example.yaml`.

## Uninstall Or Upgrade

First stop the foreground renderer or stop/disable the manually enabled user unit:

```bash
systemctl --user disable --now open-room-renderer.service
python3 ~/.local/share/open-room-renderer/uninstall.py --uninstall --dry-run
python3 ~/.local/share/open-room-renderer/uninstall.py --uninstall
```

Use the original `--prefix` for a custom installation. Uninstall removes only
unchanged manifest-owned files and empty owned directories. It preserves runtime
state, edits, unknown files, and any manually copied unit; review/remove that unit
separately and reload the user manager. No recursive broad deletion is used.
Stop all renderer/mpv processes first; a remaining IPC socket blocks removal.

For upgrades, install into a new prefix, stop the old instance, and migrate a
backup of its state and chosen settings. Update state/socket paths and the user
unit for the new prefix before starting. Do not run the old and new instances
with the same identity at the same time. Keep the old receipt and installation
until the new version's local playback/API/discovery checks pass.

## Discovery On A Chosen IPv4 Interface

By default discovery uses the operating system's routing choices. On a multihomed
host you can explicitly select a local IPv4 address in config.yaml:

```yaml
discovery_interface: "192.0.2.10" # Replace with this host's actual LAN address.
advertise_ip: "192.0.2.10"        # Address clients can reach for HTTP/control.
```

The example address is documentation-only, not a usable default. The selected
address must exist locally; startup fails if it cannot join/send on that interface.
This setting selects SSDP/mDNS multicast, not the HTTP listener bind or firewall.
Use a trusted LAN and make the advertised HTTP port reachable there. Loopback is
useful only for same-host acceptance. IPv6 interface selection is not implemented.

On Linux the explicit profile also isolates owned multicast receive sockets from
other interface memberships. Generic mDNS validates the installed zeroconf socket
layout and fails closed if it cannot apply that isolation; default routing remains
unchanged. A changed DHCP address requires updating these settings and restarting;
automatic interface migration is not implemented. Keep the same state directory
and UUID when restarting or renaming. Real original-app/LAN acceptance remains a
separate test from the native same-host multicast and playback CI lanes.
