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

The intended target is **Odroid XU4 / Armbian / FiiO KA17**. The authorized fixed
XU4 check on 2026-10-14 observed **Armbian 26.8.3 trixie, armv7l/32-bit and
Python 3.13.5**, testing main `983dddae` CLI/help and two fixed cases. It did not
test this receiver candidate or startup selection. The fixed check observed
**mpv 0.40.0**; the actual ALSA identifier/KA17 and receiver/provider acceptance
remain unverified. Run
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
Receiver setup and every Spotify start also ask the native ELF loader to list
the verified client's dependencies. This bounded check catches missing libraries
and incompatible symbol versions without starting Spotify or touching its account
configuration. The installer itself remains static; playback and hardware
compatibility still need separate acceptance.

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

3. Prepare a separate private receiver directory. No account or audio device is
   touched. It validates native files and provenance, and briefly runs the system
   loader's dependency-listing mode without starting Spotify:

   ```sh
   "$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" setup \
     --renderer-prefix "$HOME/.local/share/open-room-renderer" \
     --renderer-config "$HOME/.local/share/open-room-renderer/config.yaml" \
     --renderer-unit "$HOME/.local/share/open-room-renderer/open-room-renderer.service" \
     --client-dir "$HOME/.local/share/orr-spotify-client-v0.10.3" \
     --name "Studio Spotify" \
     --port 3678
   ```

   `--name` chooses this receiver's Spotify Connect device label before first
   setup. Omit it to retain `Open Room Renderer Spotify`. The chosen text is
   saved in private `receiver.json` and checked against the sidecar config before
   Spotify starts. Older profiles without this field keep the same default;
   loading them does not rewrite any files. Native room renames do not change
   this label. Use the stopped-only [`configure` command](#change-receiver-settings)
   below to change an existing installation's label. Setup itself never
   overwrites an existing directory.

   ORR accepts 1–128 printable Unicode characters without surrounding whitespace,
   preserving the text exactly. This is ORR's local bound, not a claimed Spotify
   limit. The pinned upstream [schema](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/config_schema.json)
   defines `device_name` as a string; its [loader](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/cmd/daemon/cli_config.go)
   passes the name into the daemon configuration. Neither declares a name-length
   bound. This setup option does not alter device IDs, accounts or the offline
   `prepare-exclusive` configuration. Actual provider/app display is unverified.

   Optionally add `--bitrate 320` to this fresh setup command to request a
   preferred bitrate of 320 kbps; `96` and `160` are also accepted. This is a
   preference, not a guarantee of delivered bitrate, codec or sound quality,
   and does not enable lossless playback. Omission leaves the sidecar config
   byte-for-byte unchanged and retains the pinned upstream's 160 kbps preference,
   including for older profiles. No existing installation is changed.

   The chosen integer is saved as `preferred_bitrate` in private `receiver.json`
   and mapped to the sidecar's top-level `bitrate`. The pinned upstream
   [schema](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/config_schema.json)
   specifies integer choices 96/160/320 and default 160; its
   [loader](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/cmd/daemon/cli_config.go)
   loads a Go `int`, defaults to 160 and passes it to `daemon.Config.Bitrate`.
   Starts and current playback controls require the sidecar to match the saved
   preference exactly. Use the stopped-only [`configure` command](#change-receiver-settings)
   below to change it in place; there is no runtime quality command.
   Account/provider and XU4 + KA17 playback acceptance remain unverified.

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

## Optional next-session startup selection

Installation leaves existing native renderer enablement unchanged. To explicitly
select a source for the **next systemd user-manager session**:

```sh
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" startup status
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" startup spotify
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" startup renderer
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" startup off
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" startup restore
```

Choose one of `spotify`, `renderer` or `off`; these commands **do not start or
stop the current services**. The selection only changes the two recognized
per-user `default.target.wants` links. The first selection saves the original
native renderer link's exact target, or its absence. `restore` and receiver
`uninstall` restore that original choice and retain private account/config data.
Restoration and `off` do not require a healthy Spotify executable or config.
Global, runtime, aliased, custom or externally changed enablement is refused for
manual review rather than rewritten. Keep the private startup receipt intact.
Inspection covers unit lookup and dependency-link layouts; it does not parse
arbitrary units' inline `Wants=` or `Requires=`. Those custom dependencies and
independent activation paths remain unsupported and must be reviewed manually.

An interrupted selection can leave both sources disabled. `startup status`
reports `APPLYING`; explicitly repeat the pending selection or use `startup
restore`. No automatic resume, service retry or restart loop is added. The
unchanged start guard still requires the whole peer cgroup and direct output to
be released before admitting a source. Spotify still starts at volume zero.

This is **not a power-on boot guarantee**: the user manager must already start
through the operator's existing login/session policy. These commands never
enable lingering or change system services. A next-session Spotify selection
still requires the separately reviewed provider/account/device prerequisites.

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
operation is performed by the installer or acceptance tests.** Once the operator
has separately started Spotify, explicitly display its already-issued user code:

```sh
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" pairing-code
```

This makes exactly one passive `GET /auth/code` through the same established
connection ownership and service-invocation fences as the playback commands.
The [pinned handler](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/daemon/api_server.go#L537-L546)
only reads the pending challenge; it does not start or retry authentication.
The [source mapping](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/daemon/app.go#L384-L398)
exposes `UserCode`, never the OAuth device code or tokens. The command accepts
only the pinned three-field contract, a short alphanumeric/hyphen user code
(4..16 characters), and a timezone-qualified expiry no more than one hour away.
Unexpected formats fail closed. `ABSENT`, `EXPIRED`, or a fixed unavailable
error never initiates or retries a flow, opens a browser, polls, or starts a service.

Only a current validated `user_code` and the fixed official
[Spotify pairing destination](https://www.spotify.com/pair) are displayed.
Spotify's [own instructions](https://support.spotify.com/cg-en/article/spotify-on-tv/)
confirm that destination. The upstream URL is discarded, even if it contains
the code. The operator personally opens the official page and approves their
own account. The code may expire before use; no successful pairing is claimed.
No code is saved by ORR; keep the terminal output private and do not publish it
or private `sidecar/` files. Normal status, diagnostic and now-playing commands
never fetch the pairing endpoint. Connect to the same account in the Spotify
app and select the receiver after pairing. Account/provider/hardware gates stay
open until their separate acceptance checks are completed.

Spotify starts at **volume zero** on every service start. Raise its app volume
carefully to a low listening level. No sound is claimed by an active process,
API readiness or a retained session report.

Once Spotify is selected and an account/session is already present, the same
independent CLI can request basic controls:

```sh
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" pause
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" resume
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" volume 5
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" next
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" previous
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" seek 45000
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" repeat-context on
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" repeat-track off
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" shuffle on
```

Volume is absolute, 0..100, for the pinned setup's 100-step Spotify volume.
Seek takes integer milliseconds (45000 = 45 seconds), from zero through the
reported track duration. Invalid values, an unknown duration or a target beyond
that duration are rejected before the POST. Seek snapshots include only numeric
`duration_ms` and `position_ms`; absent values remain null. The track can change
between the snapshot and request, so this is not a track-identity guarantee.
`next` follows Spotify's existing context and **may resume paused playback**.
`previous` normally rewinds the current track when more than three seconds in;
otherwise it requests the previous track. Navigation may finish asynchronously
or stop at a context boundary. There is no URI argument or ORR queue involved.

`repeat-context`, `repeat-track` and `shuffle` require explicit `on` or `off`.
They set the corresponding boolean; repeating the same command does not invert
it. The repeat flags are independent, so setting one does not clear the other.
The [pinned option handlers](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/daemon/controls.go#L1077-L1148)
apply shuffle to the existing context without changing the current track.
Shuffle needs a context and its reported flag can change before asynchronous
reordering finishes, then revert on failure. Even a matching readback is only a
snapshot, not proof that reshuffling completed. Only the existing projected
playback booleans and volume fields are returned; no context/queue contents or
track metadata are exposed by these controls.

Controls validate the existing exclusive service invocation and configuration,
then verify the established connection's accepted socket belongs to that client
before sending HTTP. Inaccessible `/proc` ownership data blocks controls. No
foreign listener receives HTTP, and a closed connection is never reconnected.
The HTTP exchange has one 12-second deadline, including status and response-body
reads; service checks retain their existing bounded timeouts. There is no retry.
`request_outcome: ACKNOWLEDGED` means upstream returned HTTP 200, which does not
guarantee the requested change: inspect the separately reported `provider_before`
and `provider_after` states. Errors exit 1; an uncertain POST may already have
taken effect. Inspect `status --playback` or explicitly `now-playing` before
retrying. Audio and `playback_effect` remain `UNVERIFIED`. The pinned upstream
[request handlers](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/daemon/player.go#L740-L762)
discard seek/skip errors before returning HTTP 200, so an acknowledgement does
not prove a seek or track change succeeded. These commands require existing
playback (except volume); they do not pair, activate a device or launch a URI.

To start an explicit item after pairing, while the selected Spotify receiver
reports a stopped session:

```sh
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" play "$SPOTIFY_URI"
```

Supply exactly `spotify:track:ID`, `spotify:album:ID` or `spotify:playlist:ID`,
with a 22-character ASCII base62 ID. URLs, user/collection/local-content URIs,
episodes, extra parameters and whitespace are refused. Syntax validation does
not establish that content exists or is playable by the paired account.

To choose new content while this receiver is playing or paused, explicitly opt
in for that one request:

```sh
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" play --replace-current "$SPOTIFY_URI"
```

Without this flag, playing and paused sessions are refused before the POST.
With it, stopped sessions remain eligible too, but buffering is always refused.
This requests **playing the new content even if the previous content was
paused**; it does not preserve pause or promise a seamless transition. Check
Spotify's current volume before replacing content.

`play` uses the same owned connection and invocation/cgroup fences. It first
requires `GET /` to report boolean `playback_ready: true`, then `/status` to
report a present, non-buffering session and the pinned volume scale, plus stopped
state unless `--replace-current` was supplied.
It sends exactly one `POST /player/play` with only `uri`, followed by one
sanitized status snapshot. The [pinned request/readiness schema](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/api-spec.yml)
and [play handler](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/daemon/player.go#L763-L838)
support this contract: context resolution precedes device takeover; subsequent
loading is asynchronous and can still fail after HTTP 200. The omitted `paused`
field defaults to false. `--replace-current` changes only ORR's local admission
check, not the upstream payload.

This can take Spotify playback over from another device and start sound at its
current Spotify volume. Check that volume first. Playing a URI chooses a new
Spotify context. The [pinned context loader](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/daemon/controls.go#L327-L411)
retains upcoming queued tracks; this is not an instruction to clear Spotify's
queue. The current stream can continue while the new context resolves. This
request sends no volume, shuffle or repeat changes; existing options carry over
unless upstream context restrictions change them. Another Spotify controller
can race the state snapshot; the API has no atomic stopped-only, non-buffering
or current-track condition.
`ACKNOWLEDGED` is not playback, requested-item identity or audio confirmation.
The command never retries, authenticates, starts a service or contacts ORR's
queue. Its deadline does not cancel upstream work; inspect status before retrying
an uncertain result. Account, hardware and provider-policy gates remain pending,
and Spotify still bypasses ORR EQ/balance.

Only the requested content type and `replace_current` opt-in boolean describe
the request in the result, never its ID or URI. ORR does not save the selector;
shell history/process arguments and the sidecar/provider may retain it. No
content metadata is printed by `play`.

To append one track to the selected receiver's existing Spotify queue:

```sh
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" enqueue "$SPOTIFY_TRACK_URI"
```

Supply exactly `spotify:track:ID` with a 22-character ASCII base62 ID. Albums,
playlists, URLs and other selectors are refused. This requests one append to
Spotify's existing queue, after already queued entries. It does not replace the
current context, start/resume playback or clear the queue. Paused playback is
eligible and is not deliberately resumed. Syntax validation does not establish
that the requested track exists or can eventually play.

`enqueue` uses the same pinned configuration, exclusive service invocation,
cgroup and established-connection ownership checks. It requires boolean
`playback_ready: true`, a present, non-stopped, non-buffering session with the
pinned volume scale, and a current-track object. Buffering is refused because a
context load can replace the list being mutated. These snapshots do not prove
an upstream queue/context exists or prevent another controller from changing
it before the request. No queue contents or current-track fields are returned.

On that one connection it sends `GET /`, `GET /status`, exactly one
`POST /player/add_to_queue` with only `uri`, then one sanitized `GET /status`.
The [pinned HTTP handler](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/daemon/api_server.go#L743-L757)
accepts a URI; the [player handler](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/daemon/player.go#L870-L872)
acknowledges before the [asynchronous queue mutation](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/daemon/controls.go#L1150-L1181)
finishes and even acknowledges when there is no context. The
[track-list operation](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/tracks/tracks.go#L399-L406)
appends rather than replacing existing entries. `ACKNOWLEDGED`, including an
unchanged status readback, proves neither append, queue position nor eventual
playback. `queue_effect`, `playback_effect` and audio remain `UNVERIFIED`.

The existing one-deadline, no-reconnect and no-retry policy applies. A timeout
does not cancel upstream work. Inspect the queue in the Spotify app before
retrying an uncertain enqueue: another request can add a duplicate, and ORR's
status/now-playing commands cannot confirm queue contents. ORR adds no queue
model, listing, persistence, authentication, service activation or play request.
Only `requested_content_type: track` describes the selection in its output,
never the ID/URI; shell history/process arguments and Spotify may retain it.
Account, provider-policy and hardware acceptance gates remain open.

To explicitly inspect the current track while the renderer UI is stopped:

```sh
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" now-playing
```

This one-shot command uses the same owned connection and exclusive-service
checks, sends only `GET /status`, and displays provider-reported playback state
plus `track.title`, `artists`, `album`, `duration_ms` and `position_ms`. The
[pinned upstream schema](https://github.com/devgianlu/go-librespot/blob/4dbc099f46da3529adaa85feb97c4f49deb0b130/api-spec.yml)
supplies these from `name`, `artist_names`, `album_name`, `duration` and `position`.
Position is a snapshot in milliseconds, not an extrapolated clock or evidence of
DAC/audio output. `track_observation` is `NO_TRACK` for an explicit null track
or no session, and `UNKNOWN` when the track field is absent; unavailable fields
inside a reported track remain null. A failed ownership/service check or invalid
response exits 1 without printing metadata or raw provider errors.

Track names, artists and albums are shown only by this explicit command and are
never saved or included in normal status/diagnostic output. Treat its terminal
output as private listening history. No account/device identifiers, URIs, URLs, artwork or upcoming-track
metadata are displayed or fetched separately. It does not watch, authenticate,
activate playback, or contact the renderer UI.

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

### Inspect a startup failure

```sh
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" last-start-error
```

This read-only command needs only the private receiver directory, not healthy
client/config files or a working service manager. It reads the existing private
`last-guard-error.json` and displays an allowlisted category plus a fixed hint:
`EXEC_FAILED` means the OS could not execute the native target; inspect its
installation, execute permissions and native loader/dependencies.
`GUARD_REJECTED` means the managed startup checks refused admission; inspect
private setup, exact owned service units and exclusive output. `STARTUP_FAILED`
covers other handled local startup errors. Use `status` for current services and
the off-only `recover` above before a separate, explicit retry.

The response is always marked `HISTORICAL_NOT_CURRENT`. A successful later start,
return, stop or recovery does not clear this last **recorded** failure. A later
failure might also leave it unchanged if writing fails. A missing record is null,
not evidence of successful startup. Errors after the native exec, or before the
Python guard starts, are outside this record; inspect service state too. Invalid,
oversized, legacy free-text or unsafe-file records fail closed without displaying
their contents. Reads never repair records, start services or contact a provider.

### Change receiver settings

An existing installed receiver can keep its private directory and pairing state
while changing its Connect display name and/or preferred bitrate:

```sh
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" switch off
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" configure \
  --name "Studio Spotify" --bitrate 320
# When ready for a separate explicit start, under the account/hardware gates:
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" switch spotify
```

Supply at least one of `--name` or `--bitrate`; omitted settings are preserved,
including legacy implicit defaults. The setup name rules and bitrate choices
96/160/320 apply. There is no reset-to-default action. Repeating already-saved
settings is a validated no-op.

`configure` requires the exact installed links and loaded owned units, both
services already inactive/dead with successful results, no PIDs/jobs, and empty
current and saved cgroups including descendants. Use `recover` first for a failed
service. It holds the existing control lock, verifies the pinned client and
private sidecar configuration, and changes only the selected fields in
`receiver.json` and `sidecar/config.yml`. Credentials, saved device identity and
other sidecar state are never read or written. Native paths/output, client/version,
service units, startup selection and all other settings are retained. It does
not stop/start services, contact Spotify, pair again or probe an audio device;
client verification may run the native loader's dependency-listing mode.

Each file replacement is atomic and synced, with the sidecar written first.
The pair is not one atomic filesystem transaction. If interrupted between the
two replacements, normal Spotify starts remain blocked by the mismatched files;
off and the independently validated renderer return remain available. Explicitly
repeat the same selected settings to finish. The command accepts only the exact
saved sidecar or the exact sidecar requested by those settings. A retry that no
longer describes the pending config, or an unrelated manual change, is refused.
There is no rollback, automatic retry, journal or general config repair. Do not
edit these files or start services through another path concurrently.

Success reports saved local configuration with provider settings `UNVERIFIED`.
An explicit later start still performs every existing native-profile, sidecar,
ownership and output-release check. It is not evidence that Spotify displays the
new label or delivers the chosen bitrate; those need separate real acceptance.
The change does not refresh an edited native YAML fingerprint.

### After editing the native renderer YAML

Receiver setup fingerprints the native YAML, so even a comment or port change
requires explicit admission before the next start. Current Spotify controls,
including `volume 0`, pause and now-playing, remain available for an already
owned active Spotify process even if the native YAML changes or mpv becomes
unavailable.
They still require the pinned client, unchanged private sidecar configuration
and exact loaded owned units, plus exclusive service and API connection ownership.
They do not refresh the renderer or admit another source start. For edits that
keep the same native prefix, config/unit paths and direct ALSA output:

```sh
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" switch off
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" refresh-renderer
"$ORR" spotify receiver --directory "$HOME/.local/share/orr-spotify" switch renderer
```

`refresh-renderer` requires both exact owned services already inactive/dead with
successful results, no PIDs/jobs, and empty current and saved cgroups including
descendants. Use `recover` first if a failed service prevents that proof. It
rechecks the strict native profile and updates only its YAML fingerprint in
`receiver.json`. Sidecar configuration/credentials, client files, units, startup
selection and other receipts are preserved. Refresh does not stop/start services,
probe the device or require a working Spotify client/account. The next explicit
switch still performs the normal stop and physical output-release checks.

Changing the direct ALSA output or native paths/unit requires a fresh receiver
setup. Restore the old owned paths/unit before `receiver uninstall`, then run
`receiver setup` in a new private directory for the new profile. Refresh never
migrates the sidecar or rewrites a service to match those changes.

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
shared journal. The fixed-category `last-guard-error.json` read channel above and
separate systemd state expose local startup failures without raw exception text,
paths or provider content. Writing the historical record is best effort and never
authorizes a start or prevents return/off recovery.

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
  installation. Stopped-only configuration tests cover retained private identity
  and credentials, exact installation/stop proof, interrupted pair writes and
  explicit same-settings retry, and rejection of unrelated edits. These are not
  physical or account tests.
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
