# Offline proposal for manual exclusive Spotify mode

`spotify prepare-exclusive` writes a bounded **OFFLINE_ARTIFACT_ONLY** package.
It never installs or runs a client, logs in, reads/imports account credentials,
contacts a provider, calls systemctl, starts/stops mpv, opens a socket or probes
a device. **TERMS_PENDING / ACCOUNT_PENDING / HARDWARE_PENDING /
ARBITRATION_PENDING** remain unchanged. This is neither legal clearance nor
playback-ready evidence.

The optional independently installed client is pinned in the proposal to
**go-librespot v0.10.3**, commit
`4dbc099f46da3529adaa85feb97c4f49deb0b130`. The generator checks a Linux ELF
header and executable permissions without running the binary. That is **not**
version/provenance/ABI verification. It ships no upstream binary or credentials.

## Supported profile and use

Only an existing completed unprivileged Linux native installation, the same
user's systemd manager, and the existing canonical `open-room-renderer.service`
are supported. Supply the exact native unit proposal, not a custom unit or
wrapper. The generator compares that file to the native template; it does not
inspect installed/effective systemd units, active jobs or cgroups.

The explicit renderer config must be `PREFIX/config.yaml` or
`PREFIX/migrated-profile/config.yaml`, matched by the supplied native unit.
It must explicitly select:

- `auto_spawn_mpv: true`
- `mpv_socket: /absolute/PREFIX/run/mpv.sock`
- `state_dir: /absolute/PREFIX/state` or `PREFIX/migrated-profile/state`
- `mpv_audio_device: alsa/hw:CARD=NAME,DEV=N`
- Unprivileged explicit HTTP/UPnP ports, distinct from the Spotify API port

The prefix, run directory and selected state directory must be yours and mode
0700. Source inputs must be regular files without group/other write permission.
No symlink traversal, aliases, YAML merges, duplicate/unknown config keys,
custom layouts/services, wrapper scripts, extra client args, shared/default/
dmix/PulseAudio/PipeWire/pipe/null routes or credential inputs are accepted.
Executable paths additionally reject single/double quotes because systemd
rejects those literal executable names. All paths are explicit,
absolute and normalized; controls, backslashes, dollar expansion and parent
traversal are rejected. Paths into the renderer prefix and client executable
cannot overlap the new package.

Each input leaf is checked without following symlinks before it is opened;
existing FIFOs, sockets, devices and other non-regular entries are refused
without opening them. The opened descriptor is checked again. These checks
are not an atomic defense against a same-user process replacing an input
between the metadata check and open; keep inputs stable during preparation.

Example, using an already independently installed client and already configured
native direct-ALSA profile (these paths are placeholders to replace):

```bash
open-room-renderer spotify prepare-exclusive \
  --renderer-prefix /home/alice/.local/share/orr \
  --renderer-config /home/alice/.local/share/orr/config.yaml \
  --renderer-unit /home/alice/.local/share/orr/open-room-renderer.service \
  --go-librespot-binary /home/alice/.local/bin/go-librespot \
  --output-dir /home/alice/.local/share/orr-spotify-proposal \
  --host 127.0.0.1 --port 3678
```

The output directory must be new; its parent must already exist. No existing
file is overwritten. Directories are 0700 and files 0600, independently of the
usual umask. An I/O failure can leave partial inert files; the success receipt is
written last. Its hashes describe artifact completeness, not crash/power-loss
durability; recheck them before any manual installation. Review a partial
directory manually and choose a new path for a
retry. The generator does not recursively delete anything.

## Generated files

- `sidecar/config.yml`: JSON-compatible YAML with fixed `device_auth` mode,
  direct ALSA matching mpv, numeric IPv4 loopback/fixed unprivileged API port and
  empty CORS. Cache, metadata prefetch, MPRIS and Zeroconf are disabled. Initial
  Spotify volume is zero on every start; a remote controller can still raise it.
- `open-room-renderer-spotify.service`: a proposal with
  `Conflicts=open-room-renderer.service` and one
  `After=open-room-renderer.service` edge. No `[Install]` or boot enablement.
- `open-room-renderer.service.d/90-spotify-exclusive.conf`: drop-in for the
  existing canonical renderer; no second renderer service is created.
- `MANUAL_HANDOVER.md`: installation, user-operated pairing, bidirectional
  handover and removal instructions tailored to the absolute paths.
- `receipt.json`: hashes and explicit pending gates. It contains no account
  information and does not claim effective units, service activation or audio.

Both service proposals explicitly require `KillMode=control-group`,
`SendSIGKILL=yes`, `TimeoutStopSec=45`, and `Restart=no`. Shutdown is by systemd
signals and owned-cgroup termination, not a REST player-stop request. There is
no automatic fallback, switch or supervisor.

The package's `sidecar/` directory is the future upstream config/state location;
keep it outside renderer state and exports. On later user-operated execution,
upstream can create `state.json`, `credentials.json`, and `lockfile` there.
Never export those files or pairing logs. Do not add a higher-priority
`config.yaml` or CLI overrides. Loopback and empty CORS do not authenticate other
local users/processes; do not expose the API through a reverse proxy or to a LAN.

## Required manual gates

Read the complete generated guide before any installation or client execution.
Manually disable prior renderer boot enablement, remove independent launch
paths, and inspect both effective units/drop-ins in the same user manager.
For initial installation only, an actually absent Spotify unit can instead be
verified absent (including aliases/drop-ins), with no independent Spotify
process or foreign DAC owner and successful renderer stop/cgroup/release checks.
This is not a successful Spotify stop and cannot excuse a failed/unknown unit.
After the proposals are installed, for every first start and transition in
either direction:

1. Stop both and wait for completed successful stops.
2. Inspect effective units, stop results, pending jobs and empty owned cgroups,
   including descendants. Unknown/failed/timed-out stops remain off.
3. Verify no foreign DAC owner or independent launch path and verify physical
   hardware release. Never kill unrelated processes to force handover.
4. Only then start exactly one selected service. Any uncertainty remains off.

Conflicts, ordering and cgroup termination are defense in depth. They do not
prove universal physical ownership or exclude processes launched through other
users, managers, shared audio servers or escaped processes. There is no automatic
fail-closed interlock. Account pairing and real-network/hardware acceptance are
separate user-operated stages requiring their own authorization.

Spotify's direct ALSA output **bypasses ORR mpv EQ/balance**, has separate volume,
and temporarily makes ORR controls unavailable when the renderer is off. No
shared DSP, seamless switching, target ABI, unattended endurance or twelve-month
maintenance-free claim follows from this offline package.

## Source contract and verification scope

The pinned official [CLI source](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/cmd/daemon/cli_config.go)
confirms `--config_dir` (pflag long form), config selection and metadata/cache
settings. The [README](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/README.md)
and [schema](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/config_schema.json)
confirm `device_auth`, direct ALSA, API/CORS, MPRIS and discovery settings;
[main.go](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/cmd/daemon/main.go)
confirms the distinct state and credential files. The pinned
[API listener](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/daemon/api_server.go)
formats host/port without IPv6 bracketing; this generator deliberately accepts
only canonical IPv4 loopback addresses. The separate diagnostic still supports
IPv6 for independently configured servers.

Focused deterministic tests cover private outputs, rejection, no forbidden
operations, CLI dispatch/errors and static systemd validation with inert fixture
executables. Static verification does not execute those binaries or test service
runtime/hardware. Existing installation/package smoke checks module inclusion.
See [ADR 001](adr/001-spotify-sidecar.md) and
[the separate read-only diagnostic](SPOTIFY_DIAGNOSTIC.md).
