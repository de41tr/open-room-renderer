# Getting Started

The default installation runs as your normal Linux user on **port 8080**. It
keeps its identity across restarts and never automatically invokes sudo.

## Browser controls

After starting the service, open `http://<renderer-ip>:8080/ui/` in a browser on
the trusted LAN. Use your configured API port if different. No Node runtime,
frontend build or extra listener is required. The [Web controller guide](docs/WEB_UI.md)
explains playback/queue/favorite/DSP controls, pending/unknown outcomes, security
limits and the outstanding real-browser acceptance checklist.

## 1. Native Prerequisites

Install these using your distribution's package manager:

- Python **3.11 or newer**, with `venv` / `ensurepip` support
- native **mpv 0.37 or newer**, with its distribution-provided codec/audio libraries
- `curl`, `jq`, and `iproute2` for the checkout's diagnostics
- optional: `alsa-utils` for ALSA checks; `tcpdump` for private packet diagnosis

On Debian-family systems, the usual package names are `python3`,
`python3-venv`, `mpv`, `curl`, `jq`, and `iproute2`. Check `mpv --version` before
installing the renderer. Debian 12/Bookworm's default
[mpv 0.35.1 package](https://packages.debian.org/bookworm/mpv) does **not** meet the
minimum, including its `arm64` and `armhf` builds. Use a maintained native OS or
trusted distribution repository providing mpv 0.37 or newer; this installer does
not add repositories or upgrade the OS.

The renderer's M3U/PLS readiness and ownership checks use `playlist-path` and
`playlist/N/playlist-path`, properties [introduced in mpv 0.37](https://github.com/mpv-player/mpv/blob/v0.37.0/DOCS/interface-changes.rst).
Older mpv can decode a child without proving its playlist ancestry and therefore
cannot meet this playback contract. Package availability is not hardware
acceptance, and a newer mpv alone does not validate ARMv7 hardware.

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
- The installer enforces the mpv 0.37 profile baseline; this is not a claim that
  every mpv build, decoder, or output device has been exercised.

## 2. Install In A Private Prefix

From the checkout, as your normal account:

```bash
python3 scripts/install-native.py --dry-run
python3 scripts/install-native.py
```

The default prefix is `~/.local/share/open-room-renderer`. `--prefix /short/path`
selects a new dedicated directory; existing directories are refused. Use a short
path because Linux Unix sockets have a 108-byte path limit. New installation and
offline-migration destination prefixes cannot contain literal single/double quotes
or DEL: systemd rejects those executable paths even when escaped. Spaces and `%`
remain supported. Manifest-aware uninstall still accepts older prefixes containing
quotes or DEL; this destination restriction does not apply to migration source or
config filenames.

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

Before starting the service, check an explicit configuration file without creating
identity/state, reading saved settings, probing the network, or touching mpv/audio:

```bash
~/.local/share/open-room-renderer/venv/bin/open-room-renderer config check --config ~/.local/share/open-room-renderer/config.yaml
```

Exit **0** means only that the startup **input** checks below passed; exit **2**
reports invalid arguments or unreadable/invalid input with a private-safe diagnostic.
The command requires `--config` and has no implicit file/default fallback. It does
not fully validate every runtime setting or verify saved state, backend, ports, or
DAC availability. In particular, `channel_balance` is validated later at startup,
after saved balance/active DSP precedence is applied. It does not configure logging
or change any saved files. Once the check passes, start the service separately:

```bash
~/.local/share/open-room-renderer/venv/bin/open-room-renderer serve --config ~/.local/share/open-room-renderer/config.yaml
```

An explicit `--config` must name a readable YAML file. A missing file, directory,
unreadable file, invalid UTF-8, malformed YAML, or unsupported document/constructor
structure exits with status **2** and a short diagnostic on stderr, before loading
identity/state or constructing the backend/listeners. The diagnostic does not echo
the path, YAML source, supplied values, URLs, or parser exception chain. Check the
selected file locally and compare its structure with `examples/config.example.yaml`.

The startup checks cover a mapping of known settings, preset/source entry
construction, existing UUID/discovery/mute/staging constructor checks, and the
logging-level/default-volume conversions used at startup. Unknown logging names
still fall back to INFO; accepted numeric volume coercions and clamping are
unchanged. Empty files, YAML null, and `{}` still mean default settings. Other
scalars and sequences (including `false`, `0`, and `[]`) are rejected. This is not
a complete field type/range schema or a backend, port, or saved-state health check.

**Duplicate-setting compatibility change:** startup and `config check` reject a
string setting explicitly written more than once at the top level, even if both
values are the same or the later value is null. Previously YAML silently used the
last value, which could clear an output choice or change the state directory or
port. Keep one explicit value per setting. Rejection is before Config construction
or identity/state/backend effects, with status **2** and fixed guidance that does
not echo the key, values, source, or path. YAML aliases and merge defaults with a
single explicit override keep their existing behavior; this is a root-only
check, not recursive duplicate validation. The public `load_config()` loader
retains its historical last-value behavior. This is an intentional input/error
policy change, separate from diagnostic-only refactoring.

An invalid `default_volume` now fails before saved gain is read, even when a valid
saved gain would previously have masked that malformed setting. Fix the YAML;
do not delete the saved gain. Valid startup settings still restore saved gain as
before, and rejection leaves the existing state untouched.
After YAML validation, expected failures while loading saved identity, gain,
device name, active DSP, or legacy balance also stop startup with status
**2** and a fixed, stage-specific stderr diagnostic that omits file contents,
private paths, and exception chains. Check the named file locally and its permissions,
or restore a known-good backup; do not delete saved sound settings to force startup.
The active DSP read is checked again during API construction; failure still cleans
up the acquired backend before any listeners start. Existing state rules and read
order are unchanged: an identity may already have been published before a later
failure, and no automatic reset or retry is added. The public configuration/state
readers and ordinary API construction retain their original exceptions. Favorites'
unavailable/config fallback remains. A separate saved-EQ protection now preserves
failed-load storage and blocks saved-library writes while independent active DSP
stays usable; see [preset recovery](docs/WEB_DSP_EDITOR.md#unavailable-saved-preset-storage).
Invalid initial channel balance, selected from configuration or saved state, now
also exits **2** with a fixed source-neutral startup diagnostic instead of an
unhandled ValueError traceback (previously exit 1). Check `channel_balance` in
configuration or saved state locally. Source precedence and numeric normalization
are unchanged: saved balance overrides configuration, and valid active DSP
overrides both. This check is **after backend acquisition and gain initialization**;
existing teardown still runs, and identity may already have been published.
Saved bytes are not reset or repaired. Public loaders, ordinary API construction,
backend availability, binding and unrelated cleanup diagnostics retain their behavior.

For compatibility, bare `serve` still selects `configs/config.example.yaml`
relative to the current directory and uses built-in defaults if that implicit
file is missing. It can then create identity/state and start normal services.
Use an explicit `--config` for fail-closed operator startup, even when selecting
that same example path. The library `load_config()` and the two `print-*` config
commands keep their existing missing-file behavior. Checkout scripts always
select a file; `ORR_CONFIG` uses the same private-safe reader before their existing
mpv/socket/privileged-port preflight. That preflight creates no renderer identity
and adds no new bind probes; the actual service reads the selected file again
after exec.

Leave the terminal open. In another terminal:

```bash
curl -fsS 'http://127.0.0.1:8080/httpapi.asp?command=getStatusEx' | jq
curl -fsS 'http://127.0.0.1:8080/description.xml'
```

The API starting does not prove that sound works. Check your audio session/device:

```bash
mpv --no-config --audio-device=help
aplay -l
mpv --no-video /path/to/your/local-test.wav
```

`aplay` is an optional ALSA diagnostic, not a requirement for PipeWire/PulseAudio.
`--require-alsa` makes installation fail if no readable/writable ALSA PCM device
exists under `/dev/snd`. Without that flag, absence is a warning, because session
audio can work without direct ALSA access. Do not fix permissions with a broad
`chmod`. Run discovery as the same user/session that runs the service: device
names and access can differ between your desktop and a system service. Copy the
quoted device name, not its description, into the renderer YAML, for example:

```yaml
auto_spawn_mpv: true
mpv_audio_device: "alsa/dmix:default"
```

Use the exact name reported on your machine; this example does not establish
that an ALSA device exists. Omit `mpv_audio_device` or set it to YAML `null` to
retain existing mpv defaults and configuration. The quoted string `"auto"`
explicitly requests mpv's normal device selection; it does not pin a DAC.
Identifiers are preserved exactly, limited to 1024 UTF-8 bytes, and may not be
blank, contain control characters, or use the special `help` value.

Explicit selection takes effect on the next renderer restart. It is passed as
one literal argument to the mpv child owned by that renderer, together with
`--no-config`, `--load-scripts=no`, and `--audio-fallback-to-null=no`. This **opts
out of ambient system/user mpv configuration**, including custom output, format,
replaygain, and profile settings, and disables automatic script loading. These
settings cannot override the selected driver or hide a failed device with silent
null output. ORR's configured DSP and gain continue to apply. Leave this option
unset if you rely on your own mpv profile. It requires `auto_spawn_mpv: true`,
an unused private IPC socket at startup, and Linux peer
credential verification. A preexisting, replaced, or unverifiable player/socket
is refused before any control command is sent; ORR does not reconfigure or kill
that foreign player. The API can remain available with backend errors so you
can diagnose the problem. Choose an unused socket and restart; do not delete
an unfamiliar socket. For an externally managed mpv, leave this setting unset
and configure its output in its own service instead.

This is startup configuration, not hot-switching or proof of audible output.
To inspect the file's intent and a running player's two output properties
without starting playback or changing settings, run on the renderer host:

```bash
open-room-renderer audio diagnose --config /path/to/renderer.yaml
```

It never starts an absent player. Run as the service user from its working
directory. A missing socket, unavailable property and malformed/transport
failure remain distinct. See [read-only audio diagnosis](docs/AUDIO_DIAGNOSTIC.md)
for exit codes and the limits of this non-atomic observation.

mpv's `audio-device` readback reports intent; `current-ao` identifies an active
output driver, not a measured physical DAC. An audio server can still remap a
logical device, and another local IPC client can alter player settings. ORR
does not claim immutable hardware routing. Test a local file and the intended
physical output after restart. See the official
[audio-device](https://mpv.io/manual/stable/#options-audio-device),
[configuration isolation](https://mpv.io/manual/stable/#options-no-config),
and [null fallback](https://mpv.io/manual/stable/#options-audio-fallback-to-null)
documentation.

Stop with `Ctrl+C`. Restart uses the same `state/uuid`; do not copy that state
between independent renderers. Each needs its own prefix, state directory and
IPC socket. For two renderers on one host, pass a different installer `--port`
(for example 8081), or set distinct `http_api_port`/`upnp_port` values in each
config. `serve` accepts `--config`, not `--port`; control-command `--port` chooses
the destination. Discovery also needs distinct identities. Two live multicast
responders on one host still need target-network acceptance.

The remaining CLI examples use the short `open-room-renderer` command. In each
control terminal, activate the installed environment first (adjust the path for
a custom prefix):

```bash
source ~/.local/share/open-room-renderer/venv/bin/activate
```

For an editable checkout, use `source .venv/bin/activate` instead. Activation
only changes this shell; alternatively use the full executable path shown above.
The installer does not add a global command or change your shell startup files.

### Find a renderer address with bounded SSDP discovery

```bash
open-room-renderer discover --interface 192.0.2.10
open-room-renderer discover --interface 192.0.2.10 --timeout 5
```

Replace the documentation address with an IPv4 address assigned to the computer
running the command, on the interface you want to search. An explicit local IPv4
is required; the command does not enumerate interfaces or load renderer config.
For same-host loopback testing, explicitly use `127.0.0.1`.

The JSON `candidates` contain `address`, `description_port`, `description_url`
and a bounded advertised `identity`. All are explicitly
`unverified_unauthenticated`: SSDP is a self-description that can be spoofed, not
authentication or permission to control a device. The advertised description
port is **not a verified primary API port**. Current renderers mount the full
API on their configured listening ports, but this command neither checks those
ports nor chooses one for later commands. It never automatically uses a result.

Only replies advertising the bounded `OpenRoomRenderer` product token and
MediaRenderer identity are considered. The HTTP description URL must use a
literal IPv4 matching the reply's source, an explicit valid port, and exactly
`/description.xml`. No description, status, mDNS or DNS lookup is performed.
Nothing is saved, and config/identity state is not created or changed.

The default absolute discovery deadline is 3 seconds; `--timeout` must be finite,
above zero and at most 5 seconds. One ephemeral UDP socket sends at most two
MediaRenderer M-SEARCH packets, on the selected interface with TTL 1 (TTL 0 for
explicit loopback). It receives at most 256 datagrams of up to 4 KiB, retaining at
most 64 unique candidates. Duplicate identities at different endpoints remain
separate hints. The socket closes after the operation, errors or interruption.

`truncated` and `limits_reached` report early receive/candidate limits;
`oversized_datagrams` reports replies discarded for exceeding the packet limit.
An empty array with `no_matching_replies_observed` means only that no matching
replies were observed during this bounded search. It does not prove no renderer
exists. This is a headless address-finding aid, not physical-LAN, app or hardware
compatibility evidence.

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
unchanged manifest-owned files and empty owned directories. The root `config.yaml`,
`state/`, and `migrated-profile/` are user data: they are excluded from new manifests
and preserved even if an older manifest lists them. Edits, unknown files, and any
manually copied unit are also preserved; review/remove that unit separately and
reload the user manager. Protected user data alone does not retain the manifest,
receipt, or uninstall script; modified installer-owned files still retain the
manifest and uninstall script for review. No recursive broad deletion is used.
Stop all renderer/mpv processes first; a remaining IPC socket blocks removal.
Older installations retain their original `uninstall.py`. To use the current
preservation rules on those installations, run this checkout's
`scripts/install-native.py --prefix /path/to/old-prefix --uninstall`, first adding
`--dry-run` to review the proposed removal.

### Optional Offline Upgrade Profile

Install the new version into a fresh prefix first. Stop every source/destination
renderer and its mpv process yourself, including manually enabled units; keep them
stopped throughout preparation. A missing IPC socket cannot prove they are stopped.
The optional preparer does not contact, start, stop, install, enable or reload any
service. It creates a separate profile for your review and leaves NEW/config.yaml
and NEW/state untouched.

Use the new installation's Python, an explicit old installation (or offline
snapshot), and the actual config used by that old instance:

```bash
NEW="$HOME/.local/share/open-room-renderer-new"
OLD="$HOME/.local/share/open-room-renderer"
"$NEW/venv/bin/python" -I -m fake_linkplay.migration_profile \
  --source "$OLD" --source-config "$OLD/config.yaml" --destination "$NEW" \
  --acknowledge-offline --dry-run
# Review the dry run, then repeat without --dry-run to prepare the profile.
```

This is a narrow same-user native workflow. Both prefixes must have complete
native receipts; staged-only receipts are accepted for offline review but do not
supply a runnable installation. NEW must be newly installed with initial-config
digest metadata, unchanged default config, empty state/run, and no prior
migrated-profile. Older destinations without that metadata need a fresh install.
The config must be inside SOURCE and explicitly name an absolute state directory
inside its original prefix, the native original-prefix/run/mpv.sock, and an owned
(auto-spawn) mpv. A configured identity or existing nonempty state UUID is required.

For a relocated offline backup, point --source and --source-config at its current
location and add --original-prefix /absolute/original/installation. This rebases
the original config's state and socket locations; it does not connect to the old
host or establish whether any original instance is stopped. External state/socket
layouts, symlink components, special files, hardlinks, other users' files, writable-
by-others inputs, and unexpected state entries are refused. Prefix/state/run
folders must be private (0700); existing ordinary 0644 input files are accepted
inside those private roots. Reads are capped at one MiB per file. Use a deliberate
manual migration for unsupported layouts; do not weaken ownership or permissions
merely to bypass a refusal.

Preparation copies existing uuid, device_name, channel_balance, gain.json,
active_dsp.json, eq_presets.json and favorites.json byte-for-byte into
NEW/migrated-profile/state. Absent optional files stay absent. It skips
plex_client_id and plex_user_info.json without reading their contents; provider sign-in/session setup may need to be repeated separately by you. There is no persisted playback queue. All other parsed
config settings, including audio output, discovery, ports, presets and URLs, are
retained; only state_dir and mpv_socket are rebased. YAML comments/formatting are
not retained, and duplicate keys, anchors/aliases and merges are refused. This is
configuration-shape validation, not a guarantee that every saved state file is
valid or that the selected audio device exists on the new machine.

The output contains private config.yaml, state/ and an open-room-renderer.service
review template. State and unit bytes are synced before config is published last
with no overwrite. Errors before publication may leave a private incomplete
migrated-profile without config.yaml. Keep it offline, inspect/remove only that
partial profile manually, and use a fresh destination; automatic resume/merge is
never attempted. Exit status 3 means config was committed but final directory
sync failed: preserve and inspect it rather than retrying or treating it as absent.
The source is never modified; observed source changes abort preparation.

Review the separate config and unit. When you choose to activate, use its explicit
config path and review/copy that unit through the normal user-service procedure;
the default NEW/config.yaml is still the fresh default. Never run old and new
instances with the same identity at once. Keep the old receipt, installation and
backup until the new profile's local playback/API/discovery checks pass. Neither
preparation nor these checks prove physical audio, login lingering or reboot
behavior. Uninstall preserves both default and migrated user config/state.

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

## Experimental Finite-Queue Staging

The renderer normally loads the next canonical item after observing EOF. For
controlled finite-media testing, this default-off option lets mpv stage one
successor and keeps canonical metadata tied to the verified native entry:

```yaml
native_queue_staging: true
```

Use a boolean, not a quoted string. Existing profiles remain false. Volume, DSP
and mpv audio settings do not change. This is an experimental reduced-gap path,
not a promise of gapless playback for every codec, rate, playlist or output.
Production Unix-IPC clocked staging passed on x86_64 and AArch64, including
measured equal-rate and mixed-rate limits. See the scoped results in
[the compatibility matrix](docs/COMPATIBILITY_MATRIX.md) and [testing guide](docs/TESTING.md);
these digital measurements do not establish physical DAC or universal gapless playback.

Active playlist containers are excluded from further staging. Exact first-level
container ancestry is supported, but nested/foreign-tail ancestry can be
unprovable. That case returns ERROR with silence explicitly unconfirmed and
does not stop an unproved foreign source; audio can remain active until an
explicit owner control succeeds. Keep the option false where that boundary is
unacceptable. No synchronized multiroom capability follows from staging.

## Editable local favorites

With the renderer running, manage its own local library through the CLI:

```sh
open-room-renderer favorites list
open-room-renderer favorites add "Morning radio" "https://media.example.test/live.m3u"
open-room-renderer favorites save-current "Selected track"
open-room-renderer favorites play FAVORITE_ID
open-room-renderer favorites play configured:1
open-room-renderer favorites rename FAVORITE_ID "New label"
open-room-renderer favorites set-url FAVORITE_ID "https://media.example.test/repaired.m3u" --metadata keep
open-room-renderer favorites set-url FAVORITE_ID "https://media.example.test/replacement.wav" --metadata clear
open-room-renderer favorites delete FAVORITE_ID
```

Replace the example URL with a stable public or user-owned HTTP(S) media URL.
Each command accepts `--host` and `--port` (defaults: `127.0.0.1`, `8080`). Use the
returned stable favorite ID for edits and recall. Existing
configuration-owned favorites stay first and read-only; new entries append.
Deleting a managed entry compacts subsequent slots, so refresh the list before
using `MCUKeyShortClick:N` or a public client's preset selector. Editing a favorite
does not alter audio that is already playing.

`favorites play ID` sends one bodyless request and selects a detached snapshot of
that favorite. Deleting, renaming or replacing a library URL after admission cannot change
the requested source. Use the listed `configured:N` ID for a configuration-owned
favorite. `configured:N` identifies a configuration slot; changing configuration
order can change that slot. Managed IDs remain stable across library edits.
The CLI reports `request_acknowledged`, not a claim that audio is still
playing. A timeout leaves the outcome unconfirmed: check status before deciding
whether to try again. The CLI never retries or follows a redirect.

The browser also offers **Change URL** for managed favorites, with a blank private
input and explicit keep/clear choice. Its redacted view reports ACKNOWLEDGED rather
than proving the exact saved target; see [browser favorite repair](docs/WEB_UI.md#repair-a-saved-favorite).

For a small managed library, the browser's **Backup JSON** and **Restore JSON**
provide a private paired transfer. Restore requires an empty, healthy managed
library; configured favorites may remain. Saved IDs, order, exact targets and
metadata survive without affecting playback. The complete escaped Web request
must fit 32,768 bytes; larger libraries and corrupt-library repair still require
an offline preservation workflow. See [favorites backup and restore](docs/WEB_UI.md#back-up-and-restore-managed-favorites)
for privacy, limits and uncertain-write handling.

`favorites set-url ID URL --metadata keep|clear` repairs a managed favorite while
preserving its stable ID, library name and position. The metadata choice is
required: `keep` retains title, artist, album and artwork; `clear` empties all four.
Choose `keep` only when the captured display fields still describe the replacement.
The operation validates and saves the reference without fetching or checking the
media. Current audio and already-admitted playback requests retain their old
snapshot; only a later explicit recall selects the replacement. It does not
change the queue, recovery or native staging. An unchanged target and unchanged
display fields succeed without rewriting the library.

The target-edit CLI makes one direct request without environment proxies/netrc,
redirects or retries. It checks the bounded returned record and prints only a
fixed acknowledgement, ID and metadata policy. A timeout, disconnect or invalid
acknowledgement leaves the write outcome unconfirmed; inspect `favorites list`
before deciding to retry. No automatic rollback is attempted. The 10-second
network timeout bounds connection/read inactivity, not total command runtime.

`add` optionally accepts `--title`, `--artist`, `--album` and `--artwork`.
`save-current` saves the canonical selected direct URL and display metadata, not
an expanded playlist child, temporary resolved URL, queue position or account
state. A proven live public-radio selection saves the existing fixed anonymous
station reference and resolves it afresh when recalled. Malformed or mismatched
resolver provenance is rejected. Captured title/artist/album remain authoritative,
so a saved live-song title may remain displayed on later recall. Fresh provider
availability is experimental and was not established by the local acceptance.

This is an ORR-owned feature, not an OEM/provider preset-write implementation.
It uses `state_dir/favorites.json`, with one running renderer as the writer.
Do not share that state directory between processes or edit the file while serving.
A corrupt library is preserved and managed operations return an error; configured
favorites and unrelated playback remain usable. Repair/restore it and restart.
After a durability warning, run `favorites list` before retrying a library change:
the replacement may already have committed.

The API and stored library are for a trusted private control LAN. URL userinfo is
rejected, but token-bearing queries cannot all be detected. Do not save credentials,
signed temporary URLs or account-scoped references. See the precise
[local-library API contract](docs/COMPATIBILITY_MATRIX.md#editable-local-favorites).

## Local transport controls

Use the CLI for ordinary controls without writing HTTP requests:

```sh
open-room-renderer next
open-room-renderer previous
open-room-renderer mute on
open-room-renderer mute off
open-room-renderer seek 45
```

`seek` takes an absolute nonnegative integer number of seconds. Fractions,
negative numbers and implicit mute toggles are rejected before networking.
The renderer still decides whether the selected media and transport state allow
seeking. `next` and `previous` use HTTP queue navigation, including shuffle
history. Stopped navigation selects silently; unavailable paused navigation
fails without resuming. These commands accept `--host` and `--port`, defaulting
to `127.0.0.1:8080` (bare or bracketed IPv6 hosts are also accepted).

A successful JSON result says `request_acknowledged`, not that audio was observed.
Only the renderer's exact `OK` response is accepted. The client never redirects
or retries a command, and ignores environment proxies and netrc credentials.
An uncertain result may already have taken effect; inspect status before retrying.

## Local DSP controls

Use the existing renderer's channel balance and EQ bypass controls directly:

```sh
open-room-renderer dsp balance get
open-room-renderer dsp balance set -0.25
open-room-renderer dsp balance set 0
open-room-renderer dsp eq off
open-room-renderer dsp eq on
open-room-renderer dsp balance get --host 192.0.2.10 --port 8080
```

Balance uses one normalized scale: **-1 is left only, 0 is centered, +1 is
right only**. Positive values attenuate the left channel; negative values
attenuate the right. Values outside -1..1, percentages and non-finite values
are rejected before networking. Values are rounded to the existing renderer's
three-decimal resolution. `get` returns JSON with the renderer-confirmed
`balance` value; it is a point-in-time read, not a physical audio measurement.

`eq off` bypasses the existing active EQ while retaining its configured bands;
`eq on` reapplies that same active EQ, including its existing source context.
Neither command selects a preset, changes bands, changes volume, or starts
playback. Enabling EQ can restore previously configured positive gain: keep the
volume low when testing an unfamiliar configuration. Preset/band editing and
source-context selection are not part of this small CLI.

All four forms accept `--host` and `--port` after their final subcommand/value,
defaulting to `127.0.0.1:8080`. Use a trusted private renderer endpoint; the
existing HTTP API is unauthenticated. The client makes one request, follows no
redirect, ignores environment proxies/netrc credentials, and never retries a
mutation automatically. Its ten-second connect/read inactivity timeout is not
a total elapsed-time deadline.

A successful mutation prints `request_acknowledged: true`, not a promise that
its state is still current or that sound was heard. A rejected request and an
**UNKNOWN** outcome are different. In particular, HTTP 502 can mean the DSP was
already applied but saving it failed, so it does not prove the old state remains.
A missing/malformed acknowledgement also leaves the outcome unknown. Inspect
`dsp balance get` and the existing status/DSP interface before explicitly
retrying; generic playback status alone does not report active EQ. Failure
returns nonzero, prints a fixed private-safe diagnostic, and prints no success
JSON. A read failure never substitutes an assumed balance value.

## Legacy direct controls and endpoint syntax

The legacy `status`, `play`, `pause`, `resume`, `stop`, `volume`, and
`favorites list/add/save-current/rename/delete` commands make direct requests,
ignoring environment proxies and netrc credentials. They never retry or follow redirects. Their ten-second
connect/read inactivity timeout is not a total elapsed-time deadline. Successful
response text is printed unchanged, with a trailing newline; non-2xx responses
fail without printing their bodies.

Renderer hosts use the shared client grammar: ASCII letters, digits, dots and
hyphens (up to 253 characters), or IPv6 literals with optional brackets. Scoped
IPv6 addresses, Unicode IDNs and underscores are not accepted; use an ASCII
punycode hostname where appropriate. Supply the port separately as an integer
from 1 through 65535. Userinfo, URL schemes, paths, queries and fragments are not
host input. This validates endpoint syntax, not membership in a private network.

## Multi-track queues and authored playlists

Use existing queue controls without writing SOAP by hand:

```sh
open-room-renderer queue load --name "Evening" --start-index 2 --mode repeat-all \
  "https://media.example.test/first.wav" "https://media.example.test/second.wav"
open-room-renderer queue show --name "Evening"
open-room-renderer queue play-index 2 --name "Evening"
open-room-renderer queue mode shuffle-once
```

Replace example URLs with your own playable HTTP(S) references. `load` replaces
**the entire queue and stops playback**. It selects the supplied one-based index
but starts audio only with `--play`. `play-index` always requests playback; it is
not a silent selection operation. Existing `pause`, `resume`, `volume` and `stop`
commands remain available. Commands contacting the renderer accept `--host` and
`--port`, defaulting to `127.0.0.1:8080`.

Modes are `normal`, `repeat-one`, `repeat-all`, `shuffle-once` and
`shuffle-repeat-all`. `queue mode MODE` changes the **current queue** without
replacing it or requesting playback. It takes no `--name`: the existing service
advertises only a LoopMode input, so there is no name/revision guard against a
concurrent controller. Unavailable mode changes are reported without retrying.
`show` returns the queue name, count, current index, mode
and canonical item positions. It omits URLs and opaque/display metadata by default.
`--include-urls` explicitly reveals renderer-reported URLs, which may be resolved
or credential-bearing. **This is not a canonical live-queue export.**

To author a reusable file from explicit URLs:

```sh
open-room-renderer playlist create evening.json --name "Evening" \
  --start-index 2 --mode shuffle-once \
  "https://media.example.test/first.wav" "https://media.example.test/second.wav"
open-room-renderer playlist restore evening.json
open-room-renderer playlist restore evening.json --play
```

`create` is local-only and never reads the renderer. It saves your ordered URLs,
name, index and mode; not the current playback position, metadata, shuffle history,
resolved addresses or account state. The destination's parent directory must
already exist. Existing files are preserved unless you explicitly pass
`--overwrite`. Files are private (0600), with atomic publication; final-component
symlinks and nonregular files are rejected. Keep these files private: URL query
strings can contain secrets even when usernames/passwords are rejected.

To edit an existing authored JSON playlist without changing playback:

```sh
open-room-renderer playlist append evening.json extended.json "https://media.example.test/third.wav"
open-room-renderer playlist remove extended.json shortened.json --index 1
open-room-renderer playlist move shortened.json reordered.json --from-index 2 --to-index 1
open-room-renderer playlist restore reordered.json
```

Each edit reads `INPUT` and writes a **new `OUTPUT`**, leaving the input bytes
unchanged. The output must not exist or alias the input, and its parent directory
must already exist. There are no overwrite, host, port, name, mode or play options.
Edits never contact a renderer, resolve URLs, fetch media or change active playback.
The separate `restore` replaces the queue **stopped by default**; playing still
requires an explicit playback request.

Name, all five modes and exact URL strings survive, including duplicates, query
strings, Unicode and percent-escape case. Indices use one-based positive ASCII
digits, without signs, spaces or leading zeros. Selection follows URL
**occurrences by original position**, not URL equality:

- `append` adds one or more URLs in the supplied order and preserves selection.
- `remove --index N` removes exactly one occurrence. Removing before selection
  shifts the selected index down by one; removing after it leaves selection alone.
  Removing the selected item selects its next surviving neighbor at that same
  final index, or the final previous item if there is no next neighbor. Removing
  the only item is rejected.
- `move --from-index N --to-index M` places that occurrence at final position M
  after removal and preserves the selected occurrence, even for equal URLs.
  Both indices must be within the original item count. Moving an item to its
  current position still creates a new equivalent file.

These commands edit valid files; they cannot repair malformed playlists by
removing a bad entry. The whole source and result must pass the existing strict
v1 schema, URL and complete escaped SOAP limits before any output temporary is
created. The 1000-item limit and independent 1 MiB input, output and request limits
still apply. Publication uses the same private 0600 atomic no-clobber writer.
A crash-durability warning means the output was already saved; inspect it before
retrying. Keep the files private: URLs can contain secrets or expire, and valid
syntax does not guarantee future playability. Editing adds no titles, artwork,
durations, provider metadata, playback position/state or shuffle history.

To save a point-in-time URL playlist from the current queue:

```sh
open-room-renderer playlist save-current current.json --host 127.0.0.1 --port 8080
open-room-renderer playlist restore current.json
```

`save-current` makes one read-only request on the renderer's existing local-network
control listener. It preserves the canonical URL order and duplicates, queue name,
selected one-based index and all five modes. Saving does not pause, stop, resolve,
fetch media or otherwise change playback. The output parent must already exist;
the private 0600 file is published atomically without overwriting anything,
including a symlink. There is no `--overwrite` or `--play` option on this command.

This is a **URL playlist, not a lossless queue backup**. Titles, artists, artwork,
durations, source/provider fields, IDs, opaque metadata, playback position/state,
shuffle history and runtime provenance are omitted. The separate restore defaults
to stopped playback and preserves the saved selection and mode. A queue change
after capture does not invalidate the file; there is no revision/CAS guarantee.

Locally proven public-radio stations are saved as fixed anonymous public station
references, never their temporary resolved streams. A later explicit playback
resolves that reference afresh. Every item must be safe, including inactive items:
empty/unresolved provider queues, invalid URLs/state/provenance, unproven direct
station classifications and size-limit failures reject the entire save. No item
is silently skipped. An ordinary explicit public station URL can be saved before
resolution. Other originally supplied token-bearing or temporary URLs cannot be
recognized reliably; **keep the file private and do not assume future playability**.
Older renderers without this endpoint fail clearly, without a BrowseQueue fallback.

To convert a local M3U URL list into that same JSON format:

```sh
open-room-renderer playlist import-m3u stations.m3u8 stations.json --name "Stations"
open-room-renderer playlist restore stations.json
```

`import-m3u` is an **offline, strict subset** converter. It never fetches media,
resolves nested playlists, contacts a renderer or starts playback. The new file
selects index 1 and mode `normal`. The output must not already exist, including
an alias of the input; this command has no overwrite, play, host, mode or
format-detection options. Use `restore` separately when you want to replace the
renderer queue. The same private, atomic file publication rules apply.

Supported input:

- UTF-8, with at most one BOM at the very beginning; LF or CRLF line endings.
  UTF-16, invalid UTF-8, embedded/repeated BOMs, NUL and bare CR are rejected.
- 1–1000 absolute HTTP(S) URL lines, with no trimming or normalization. Order,
  duplicates, percent-escape case, query strings, fragments and Unicode
  composition are preserved. Relative paths, local schemes, credentials and
  reserved renderer proxy targets are rejected by the existing queue validator.
- An optional exact `#EXTM3U` header once, before any entries. With that header,
  simple `#EXTINF:duration,title` records may precede URLs. Duration must be `-1`
  or ASCII digits optionally followed by a dot and more digits, such as `0` or
  `12.5`; attributes, signs other than `-1`, and exponent notation are unsupported.
  Each record needs one URL; consecutive or orphan records fail. Titles may be
  empty or contain commas. **Titles and durations are discarded.**
- Empty lines and human comments consisting of `#` or beginning with `# `.
  These may also appear between an EXTINF record and its URL. All other hash
  directives are rejected, including HLS `#EXT-X-*`, `#EXTVLCOPT` and `#KODIPROP`.

A final valid URL does not require a newline. A valid-looking file ending cannot
prove that the source was not truncated. An `.m3u8` filename or URL suffix alone
neither proves HLS content nor makes it invalid. Import does not verify remote
playability, expiration or nested content. Input bytes (including comments and
metadata), output JSON bytes and the full escaped SOAP request each have an
independent 1 MiB limit, checked before any output temporary is created. Metadata
is never silently interpreted as player configuration or authentication options.

`restore` validates the complete file and escaped request before contacting the
renderer, replaces the queue once, and leaves it stopped unless `--play` is given.
It is not automatic startup restore. Limits are 1–1000 HTTP(S) URLs, a 1 MiB file,
and a separate 1 MiB limit on the complete escaped SOAP request. A file within
its own size limit can still exceed the request limit.

A successful playback response acknowledges the request; use status to observe
actual playback. An uncertain replacement is never followed by Play or an automatic
retry. If replacement succeeds but Play is not acknowledged, the new queue stays
selected and the CLI reports that partial outcome. Inspect `queue show` and status
before retrying. A file durability warning similarly means the file may already
be saved. Queue names distinguish differently named queues, but do not protect
against another controller replacing a queue with the same name concurrently.
