# Optional foreground Snapcast source

`open-room-renderer-snapcast run` makes ORR's existing playback, queue,
favorites, radio URLs, volume and DSP the source for ordinary Snapclients.
It is an explicit Linux foreground mode, not a system-service installation.
No physical sound device is opened by this launcher. Each room needs an
operator-configured Snapclient and its own output device.

This is open multiroom distribution, **not native WiiM Home grouping**.
Spotify receiver mode remains separate and is not captured or forwarded.
Two-host clock skew, DAC timing, acoustic synchronization, XU4/KA17 output and
original WiiM Home grouping are **NOT_TESTED** by the software acceptance.

## Requirements and first run

- An installed ORR Python package and a regular (non-root) Linux user.
- `mpv`, PulseAudio, `pactl`, `parec`, and **Snapserver 0.27.0 or 0.31.0** on `PATH`.
  Other versions are rejected rather than guessing listener/config semantics.
  Ubuntu 24.04 / 0.27.0 is the hosted software-acceptance target; 0.31.0 has a
  narrowly reviewed compatible public config/API but its runtime on the target
  remains NOT_TESTED. No downgrade or target service activation is implied.
  Install dependencies through your normal distro/operator workflow; the launcher installs/enables nothing.
- User-owned or otherwise authorized media and independently configured
  Snapclients. This does not grant distribution rights for provider audio.
- Stop any separately running ORR instance before reusing its ports/state.
  Do not enable this launcher alongside an existing renderer service using
  the same state. The launcher never discovers or kills unrelated processes.

Make a separate copy of your renderer configuration and explicitly set:

```yaml
auto_spawn_mpv: true
mpv_audio_device: pulse/orr_snapcast
mirror_port_80: false
```

Keep your desired `state_dir`, identity, discovery and ordinary HTTP settings.
The source file is not rewritten. Normal ORR settings still govern its HTTP
and discovery exposure; this launcher does not silently narrow them. The new
Snapcast listeners have independent, stricter defaults described below.
Run from the same working directory as ordinary ORR: relative state and media
paths keep their existing working-directory semantics.

```sh
open-room-renderer-snapcast check --config /path/to/renderer-snapcast.yaml
open-room-renderer-snapcast run --config /path/to/renderer-snapcast.yaml \
  --initial-client-volume 10
```

`check` validates the input, reads existing discovery identity/provenance to
retain legacy listener defaults, and checks executable paths/free listener
addresses without creating state or starting native software. It does not establish
native version compatibility or audio readiness. `run` prints `READY` only
after checking the private null sink, the unique Snapserver stream, the owned
mpv output, and the renderer's own listening sockets. This means the source
is ready; it does not mean a remote client or speaker has been tested.

Fresh sessions request volume **0** for every newly connected Snapclient.
This is not a guarantee of physical silence: clients configured with
`--mixer none` ignore server volume. Use a volume-respecting software or
hardware mixer, and set low physical output gain before connecting.
Choose an explicit low startup level with `--initial-client-volume 10` as in the
example; the accepted range is 0–100. Snapcast's temporary state does not retain
prior room attenuation, so a session restart reuses the explicitly selected
initial level. Never assume a room retains its old volume across restarts
or that a server acknowledgement proves a speaker is muted.

Use the normal ORR web UI, CLI or supported controller to choose audio and
adjust the shared source volume/EQ. Start with low volume on each room's
Snapclient. All attached clients receive the same source. Room gain remains
separate from ORR source gain; there is no new ORR group-control UI.

## Connect rooms explicitly

By default only local Snapclients can connect to stream port 1704. To serve
other computers on a trusted LAN, select **one actual local interface IPv4**:

```sh
open-room-renderer-snapcast run --config /path/to/renderer-snapcast.yaml \
  --stream-bind 192.0.2.10 --initial-client-volume 10
```

Replace the documentation-only address with your chosen interface address.
Wildcard addresses and hostnames are refused. A local Snapclient can use
`--host 127.0.0.1 --port 1704`; a remote one must use the selected server
address and its own deliberately configured audio output. Snapclients are
not launched or reconfigured by ORR. Follow the
[official client setup](https://github.com/snapcast/snapcast/blob/v0.27.0/doc/configuration.md#client).

The stream is unencrypted and unauthenticated. Anyone able to reach the selected
stream listener may receive the audio. Use only a trusted network; do not port
forward it to the internet. No firewall, router, system audio or network settings
are changed. Snapcast HTTP/WebSocket control is disabled; JSON-RPC control stays
on 127.0.0.1:1705. Snapcast's D-Bus discovery path is isolated from the host bus.
Use `--stream-port` / `--control-port` for unused unprivileged ports if needed.
Existing services are never stopped to make room.

## Lifecycle and limits

The launcher owns a private per-run PulseAudio daemon with exactly a clocked
null sink and local protocol module. Its monitor feeds a private FIFO into
Snapserver as fixed 48 kHz / signed 16-bit / stereo PCM. This clocked route
keeps mpv's playback/control path separate from the FIFO reader's pacing.
It is not bit-perfect high-resolution passthrough. Uncompressed transport is
about 1.54 Mbit/s per client before protocol overhead. The conservative
Snapcast buffer is one second; pause, gain, mute and stop reach clients after
buffered audio drains. A silent source is expected while stopped or paused.

The ordinary renderer starts its own peer-verified mpv child with explicit
Pulse output and no physical/default/null fallback. Only child environments
receive the private Pulse server address. Your desktop audio server and
ambient Pulse settings are not changed. A private captured config substitutes
only the temporary mpv socket; original YAML and normal ORR state paths remain
intact. Snapcast groups/client gain are temporary and reset between sessions.

Ctrl+C, SIGTERM or SIGHUP stops the whole owned session. A dead source component,
unconfirmed source response, stalled PCM recorder or mismatched identity also stops the session,
with no automatic restart and no fallback to speakers. Stop your Snapclients
separately if desired. SIGKILL/power loss cannot perform graceful cleanup;
restart the foreground session only after checking its prior child processes.
Never use a broad process-name kill to clean up someone else's audio session.

Temporary config and logs are private (0700 directory / 0600 files) and removed
on normal exit. Failures print fixed guidance rather than raw logs or media
URLs. If startup fails, check dependency versions, unused ports and regular-user
permissions. Original ORR config/state are retained; restart your ordinary
renderer explicitly to return to its normal output.

## Verification and public sources

Focused tests cover route admission, private config/environment, readiness
identity, port conflicts, source failure and owned-process teardown. The new
`tools/acceptance_snapcast_runtime.py` launches the shipped foreground command,
its real owned ORR/mpv/Pulse/Snapserver, and two file-output Snapclients. It
checks downstream source audio, shared volume, pause/resume/stop, unchanged
source config, relative state, and clean shutdown. It reuses existing signal
measurements without replacing the earlier Snapcast experiment or its gates.
The native check runs in the existing x86_64/ARM64 CI job using extracted
Ubuntu packages; consult that PR's exact-head results before claiming a pass.
Neither architecture is ARMv7 hardware acceptance.

Primary references:
[Snapcast 0.27 configuration](https://github.com/snapcast/snapcast/blob/v0.27.0/server/etc/snapserver.conf),
[0.31 configuration](https://github.com/snapcast/snapcast/blob/v0.31.0/server/etc/snapserver.conf),
[JSON-RPC](https://github.com/snapcast/snapcast/blob/v0.27.0/doc/json_rpc_api/control.md),
[PulseAudio null-sink/protocol modules](https://www.freedesktop.org/wiki/Software/PulseAudio/Documentation/User/Modules/).
