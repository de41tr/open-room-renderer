# Optional exclusive Spotify receiver candidate

This adds an executable, independent service-switching CLI beyond the earlier
[offline proposal](SPOTIFY_EXCLUSIVE.md). It is an **implementation candidate**
for a private Linux appliance. It is not a certified Spotify product or a claim
that a Spotify account, XU4, USB DAC or unattended boot has been accepted.

The selected path is pinned **go-librespot v0.10.3**, direct **ALSA hardware**,
mutually exclusive with the whole ORR renderer/mpv user service. Spotify has its
own volume. **ORR EQ and balance do not apply to Spotify.** The renderer Web UI is
unavailable in Spotify mode; the CLI below remains independently reachable over
an ordinary local terminal/SSH session. No new playback queue or provider
framework is introduced.

## Requirements and target

- A normal Linux user, Python >=3.11, systemd's working **user manager**, cgroup v2,
  and the existing completed native ORR installation. No root/system service,
  custom unit, external mpv, shared audio server or foreground runner is adopted.
- Distribution-owned `/usr/bin/mpv` >=0.37 and native `libasound.so.2`.
- One explicitly selected, **single-subdevice direct ALSA playback endpoint**.
  `default`, `dmix`, PulseAudio/PipeWire, pipe and null outputs are refused. The
  opened PCM type/card/device is checked too; a user ALSA alias called `hw` is not
  sufficient. No user audio configuration is changed.
- Independently reviewed Spotify service permission and an operator with a
  Spotify Premium account for the upstream documented device-auth flow. The
  software license is not provider authorization. Do not pass credentials to ORR.

The intended target is **Odroid XU4 / Armbian / FiiO KA17**. Its actual Armbian
image, userspace, mpv version and ALSA identifier have not been observed. Run
`uname -m`, `getconf LONG_BIT`, `dpkg --print-architecture`, `mpv --version` and
`aplay -l` on the target before selecting the artifact/output. Do not infer the
card ID from the product name. The KA17's USB data port is distinct from its
auxiliary power input; use the [manufacturer connection information](https://www.fiio.com/ka17_parameters).
[Armbian's XU4 page](https://armbian.com/boards/odroidxu4) describes available
images, not the user's installed OS. [Debian 13's native armhf mpv](https://packages.debian.org/trixie/mpv)
meets the version floor; this is not an instruction to upgrade an existing OS.

### Client provenance and actual ARM ABI

`spotify install-client` downloads the native pinned release with `--download`,
or uses a supplied `--archive` for offline installation. It verifies the official
release SHA-256 and extracts **only** its expected executable into a fresh 0700
directory. The executable has an independent code-pinned SHA-256, also checked
before every Spotify start. A mutable receipt cannot authorize an arbitrary
replacement binary. Downloads use only official GitHub HTTPS release hosts,
with a 60-second total limit, a 32 MiB size cap and no automatic retries. No
account, cookies or environment credentials are used. The installer never
executes the client and leaves an existing output directory unchanged.
Credential-free `https_proxy` / `HTTPS_PROXY` is supported; authenticated proxies
can use the existing offline archive path instead.
ELF class, machine, endianness, ARM EABI/hard-float flags and target loader are
checked against the current native Python userspace. Cross-installation is not
supported. A static ABI match does not prove target execution or dependencies.

The reviewed `go-librespot_linux_armv6.tar.gz` archive actually contains an
**ELF32 ARM EABI5 hard-float** executable. Its `.ARM.attributes` declare
**ARMv7, Thumb-2, VFPv3 and NEON**, and its loader is
`/lib/ld-linux-armhf.so.3`. This evidence comes from the pinned binary, not its
filename. XU4 execution remains pending. x86_64 and AArch64 have separate pins;
ARM64 CI cannot establish ARMv7 acceptance. The release is dynamically linked
to native ALSA and glibc libraries. Do not copy foreign shared libraries into
Armbian to work around a mismatch.

## Explicit setup, without automatic activation

The following paths are examples to replace with your actual private native
prefix. Keep the renderer stopped during installation; remove independent
foreground/custom-service launch paths first. Retain your existing config/state.

1. Install the current ORR candidate using the existing [native procedure](../GETTING_STARTED.md).
   Set the native config's `mpv_audio_device` to the actual
   `alsa/hw:CARD=NAME,DEV=N` from target inspection. Keep its native private
   state/run directories and explicit unprivileged ports.
2. Download and privately install the correct native artifact from the
   [pinned official release](https://github.com/devgianlu/go-librespot/releases/tag/v0.10.3)
   in one command, without executing it:

   ```sh
   ORR="$HOME/.local/share/open-room-renderer/venv/bin/open-room-renderer"
   "$ORR" spotify install-client \
     --download \
     --output-dir "$HOME/.local/share/orr-spotify-client-v0.10.3"
   ```

   For an offline target, replace `--download` with `--archive /absolute/path/to/archive.tar.gz`.
   On verified native ARMv7 hard-float userspace, the selected archive is the
   generic `go-librespot_linux_armv6.tar.gz`, not the Raspberry-Pi-specific variant.

3. Prepare a separate private receiver directory. No account, process or device
   is touched by this step; it validates native files and client provenance:

   ```sh
   "$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" setup \
     --renderer-prefix "$HOME/.local/share/open-room-renderer" \
     --renderer-config "$HOME/.local/share/open-room-renderer/config.yaml" \
     --renderer-unit "$HOME/.local/share/open-room-renderer/open-room-renderer.service" \
     --client-dir "$HOME/.local/share/orr-spotify-client-v0.10.3" \
     --port 3678
   ```

4. Review the generated unit/drop-in/config and stop the existing native user
   renderer. Then explicitly install the two exact service links:

   ```sh
   systemctl --user stop open-room-renderer.service
   "$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" install
   ```

   An exact native unit copy or symlink is accepted. Custom units, unrelated
   Spotify units and additional effective drop-ins are refused. The installer
   never starts/enables either service, enables lingering, invokes sudo, changes
   system services or overwrites a foreign file. A known partial installation
   can be resumed by running the same install command. Existing native renderer
   boot enablement is unchanged; Spotify is not automatically enabled for boot.

Do not reuse the previous offline proposal's drop-in alongside the managed
receiver. Remove it manually only after reviewing the stopped native service.

## Start, switch, stop and recover

Only after the provider/account/device prerequisites have been reviewed by the
operator, request one mode:

```sh
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" switch spotify
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" status
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" status --playback
```

Plain `status` only inspects the services. `status --playback` also reads one
sanitized [provider snapshot](SPOTIFY_DIAGNOSTIC.md#what-the-json-means) from the
setup's configured loopback API, only while Spotify is the sole selected
service. `provider_playback.status` contains stopped/paused/buffering,
repeat/shuffle booleans and volume/volume_steps/volume_fraction. A 204 response
reports `NO_SESSION`; an unavailable or invalid API preserves service results,
reports `observation: unavailable` with unknown session state, and exits 1.
When Spotify is not exclusively selected, no API is contacted and the snapshot
is `not_observed`. Exit 0 never establishes playback. These are separate,
non-atomic service and provider observations: the loopback listener's ownership
and actual audio remain unverified. No metadata, authentication or playback
commands are requested; no snapshot is saved.

The first command can begin upstream's device-auth flow. **No login or account
operation is performed by the installer or acceptance tests.** The operator
personally retrieves the temporary code from the loopback API, opens the official
[Spotify pairing page](https://www.spotify.com/pair), and approves their own
account. Upstream documents `GET /auth/code` for that purpose. Do not publish its
response or private `sidecar/` files. Normal ORR diagnostics never fetch that
endpoint, tokens, usernames, track metadata or raw provider errors. Connect to
the same account in the Spotify app and select the receiver after pairing.

Spotify starts at **volume zero** on every service start. Raise its app volume
carefully to a low listening level. No sound is claimed by an active process,
API readiness or a retained session report.

Return to ORR independently of Spotify or the stopped Web server:

```sh
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" switch renderer
```

This return still works if Spotify's binary, config or credentials are damaged,
provided the owned services can be stopped and the native renderer/output are
healthy. Existing ORR saved state is retained; playback is not auto-resumed.

```sh
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" switch off
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" recover
```

`recover` is an explicit **off-only** reconciliation after a failed service: it
stops both, proves empty cgroups, then resets their failure state. It never
starts a fallback or retries a provider operation. A separate new switch is
required. Network/session recovery inside upstream is not reimplemented; there
is **no ORR/systemd automatic restart loop** (`Restart=no`). For a normal explicit
restart, switch to the same mode again. Account renewal, outages and long-term
unattended operation still need real acceptance.

## Ownership and failure contract

- One private operation lock serializes CLI transitions. Each engine holds a
  second output lock through its exec. The guard accepts invocation only from
  its exact systemd service's MainPID/cgroup.
- The CLI validates actual loaded fragments, all effective drop-ins, reload
  state and stop policy. It synchronously stops **both** units, checks jobs/PIDs,
  recursively inspects their saved/current cgroups, then probes output before
  starting one. An observed exec is fenced by service invocation ID and PID.
- The guard independently checks the peer's stopped state and sibling cgroup.
  `Conflicts`/ordering and full-cgroup termination are defense in depth, not the
  sole proof. Child processes remain owned even if they create another session.
- Unknown stop/start, busy output, changed units or unexpected cgroups fail
  closed. Cleanup never depends on writing the error receipt. No raw PID,
  foreign player, audio server or unrelated service is killed.
- Existing mpv sockets are never adopted or blindly deleted. If a forced kill
  leaves one behind, inspect the stopped installation before manual cleanup.
- An ALSA probe is a point-in-time open/release check. A later foreign opener can
  win the gap; the client's real exclusive open must then fail. A second engine
  is never started to mask failure. This is a narrowly managed appliance profile,
  not authority over arbitrary other users/services or deliberate manual bypass.

The opened direct-hardware type and single subdevice reduce ownership ambiguity.
They do not prove playable formats. Upstream's
[pinned ALSA driver](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/output/driver-alsa.go)
tries FLOAT_LE, S32_LE and S16_LE and negotiates channels/rate/buffering. Real KA17
`hw_params`, sound, stop/release and reconnect remain separate hardware gates.

## Privacy, removal and upgrade

The receiver and sidecar directories are 0700; config/receipt files are 0600;
managed services use umask 0077. Upstream state and credentials stay under the
separate `sidecar/`, outside ORR exports. The API binds numeric loopback and has
empty CORS, which does not authenticate local processes. Do not proxy it to the
LAN. Unbounded upstream logs/pairing output are discarded instead of entering a
shared journal. A fixed-message `last-guard-error.json` and systemd state expose
local startup failures without raw provider content.

```sh
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" uninstall
```

This stops and verifies both complete services before removing only the exact
owned Spotify link/drop-in. The original native renderer unit remains, stopped;
review it and start it normally with `systemctl --user start open-room-renderer.service`.
Config, client and credentials are **retained**, never recursively deleted.
Interrupted removal can resume only while both services remain stopped and
remaining links still match the recorded installation. Unexpected replacements
are preserved for manual review. Stop/uninstall receiver mode before removing
its native ORR installation. For an update, use a fresh verified client/setup
and re-review the provider/target gates; no automatic binary updates are installed.

## Evidence and gates

- `CODE_IMPLEMENTED`: client verification/install, setup, explicit service
  lifecycle, independent return path, private state and retained-state removal.
- Focused synthetic contract tests cover stop-before-start, unknown stop/start,
  failed metadata writes, broken-Spotify return, private locks and interrupted
  installation. These are not physical or account tests.
- `tools/acceptance_spotify_receiver.py` requires **real systemd user services and
  cgroup v2**. It runs real parent/child processes writing synthetic PCM under an
  exclusive file lock, exercising switching, competing callers, foreign ownership,
  delayed/stubborn descendant cleanup and off-only recovery. Its admission,
  client exec target, readiness and output probe are explicitly synthetic. A
  successful run establishes that defined lifecycle integration scope only.
  Its first phase also runs the production setup/install/effective-unit/uninstall
  code against the real user manager, using an inert native/client fixture and
  never activating either product service. Client/DAC admission remains distinct.
- `ACCOUNT_PENDING`, `HARDWARE_PENDING`, `PROVIDER_POLICY_PENDING` remain open.
  No live Spotify music, XU4/KA17, full production profile or reboot claim follows.
  Exact-commit CI results must be checked separately; this document does not
  declare an unrun job passed.

The upstream [GPLv3 license](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/LICENSE)
covers its code. Spotify's [user guidelines](https://www.spotify.com/de/legal/user-guidelines/),
[developer policy](https://developer.spotify.com/policy) and applicable agreements
remain independent. Exclusive playback and private use do not themselves close
that policy gate. No certification, blanket legal permission or indefinite
maintenance-free operation is asserted.
