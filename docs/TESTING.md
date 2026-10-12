# Test evidence and limits

## Automated suite

```sh
.venv/bin/python -m pytest -q
```

Latest published CI checkpoint: `52f5c4d`. On each Ubuntu 24.04 x86_64 and
AArch64 runner, the default suite passed 1383 tests with nine explicit opt-in
skips, then all seven libmpv cases and both production Unix IPC cases passed.
[Verified run](https://github.com/de41tr/open-room-renderer/actions/runs/38185537983).
Baseline was 92 tests; checkpoint history is in STATUS.md. Counts alone do not
establish app or hardware compatibility.

## Scope labels

- STATIC: source/schema review, no execution claim.
- UNIT: isolated application logic.
- MOCK: deterministic backend/client responses, no actual decoder/audio.
- INTEGRATION: real local HTTP/client processes with the backend identified.
- REAL_BACKEND: actual mpv/libmpv execution, possibly null output.
- REAL_AUDIO: measured or observed real audio output.
- ANDROID_APP: actual identified Android app execution.
- HARDWARE: target-device validation.

Synthetic test inputs only. A SOAP 200, playback status field, mock backend call,
or null output does not establish audible playback or Android app compatibility.

## Current environment limits

The prepared mpv 0.40.0 decoder handled synthetic WAV/MP3/FLAC with null output.
The execution sandbox denies Unix-domain sockets; production JSON IPC integration
is NOT_RUN in this sandbox, but has passed on native x86_64 and AArch64 CI
runners (below). Multicast SSDP validation is still unavailable here. Do not replace
production IPC merely to accommodate this sandbox. ARMv7/Odroid, DAC, real audio,
and actual Android app validation require a suitable separate environment.

## Playback generation and local client coverage

`tests/test_playback_generations.py` checks delayed load/resolve/readiness,
source mismatch, pause/stop/resume, EOF/error/buffering, bounded seek completion,
and observer cleanup with a deterministic backend (UNIT/MOCK).

`tests/test_local_client_integration.py` uses real loopback HTTP and installed
pywiim 2.3.9 with a stateful fake backend (INTEGRATION + MOCK). It exercises
transport, volume/mute, explicit backend failures and recovery, and asserts
session/listener/task cleanup. It does not fetch media or perform discovery.
The installed client's TLS-version deprecation warning is an upstream warning.

## Real decoder/control lane without Unix sockets

```sh
ORR_TEST_LIBMPV_PATH=/absolute/path/to/libmpv.so.2 \
  PYTHONPATH=. .venv/bin/pytest -q -s tests/test_libmpv_integration.py
```

The configured shared library must already be installed/trusted. If its runtime
libraries need a custom search path, set LD_LIBRARY_PATH for this command only.
The test does not install anything. It runs a bounded child interpreter and
restricts libmpv to two locally generated, low-amplitude WAV files, no scripts,
no external media, and `ao=null`.

Verified with libmpv 2.5.0 / mpv 0.40.0: HTTP transport, advancing position,
actual pause/resume, volume/mute property readback, paused seek, resource
replacement, stop/reload, EOF observation, SOAP two-item queue, Next/Previous,
automatic second-item selection, HTTP/SOAP/event-payload index agreement, and
cleanup after success/cancellation. Production control/snapshot methods are
inherited; only command transport is replaced inside the test.

Scope: REAL_BACKEND/null plus local HTTP INTEGRATION. No claim about production
Unix JSON IPC, acoustic mute/gain, DAC, Android or ARM.

## Discovery and app-shaped handshake

Identity/SSDP/mDNS tests are UNIT/MOCK: generated packet bytes, persisted identity,
Unicode names, ports/UDNs, and mocked rename refresh. They do not prove multicast
reachability or handset behavior. `test_app_handshake.py` follows those synthetic
discovery contracts using actual loopback HTTP, loads every advertised SCPD,
then exercises local controls and two-track selection with a fake backend.
`test_upnp_schema.py` compares declared action outputs/types with actual handlers.
`test_capability_consistency.py` compares complete capability documents across
HTTP, SOAP control and transport reads. All are independent of vendor clouds.

## Callback and proxy verification

`test_gena_lifecycle.py` exercises real local subscription/NOTIFY HTTP: transient
failure and immutable retries, ordered coalescing, renewal, expiry, peer binding,
redirect refusal, limits, sequence wrap, XML/rename safety and cleanup.
`test_app_handshake.py` and the optional libmpv lane now verify actual callback
delivery, including queue index/resource agreement and volume/mute updates.

`test_network_policy.py` uses deterministic DNS answers. `test_proxy_security.py`
uses real local HTTP fixtures with explicitly mocked approved routing, checks
each redirect, blocked destinations, selected-source/loopback gating, range and
compressed-byte preservation, sanitization and resource limits. The suite never
contacts real metadata endpoints or internal administrative services.

## Public-station resolution and header queues

`test_radio_resolver.py` uses injected JSON and DNS/transport doubles: fixed
endpoint, bounded response/deadline/candidate attempts, unsafe candidates, failure
announcements and later valid choices. `test_radio_queue.py` is MOCK SOAP/state
integration: a header becomes one concrete item, opaque metadata is never fetched,
old resolver results cannot overwrite a new source, and failures never claim play.
Concrete episodes use supplied durations, not a synthetic fallback.

Anonymous public OPML resolution was observed on 2026-10-11 for station s32537
using [the fixed JSON endpoint](https://opml.radiotime.com/Tune.ashx?id=s32537&formats=mp3,aac&render=json);
a bounded returned AAC sample was readable without authentication. The official
[station-ID guide](https://broadcaster-help.tunein.com/en/support/solutions/articles/151000172143-what-is-my-station-id-)
explains the s-prefixed identifier. These observations are experimental, not an
API stability or production partnership guarantee. The documented
[Platform API](https://developer.tunein.com/cfp_login) is a separate partner flow
and is not used. A live call from the Python execution environment failed at DNS,
so no live end-to-end provider playback is asserted.

Only lowercase s plus 1–32 digits and direct MP3/AAC station candidates are
supported. Episode IDs, account media and error clips fail explicitly. The
resolver validates candidate destinations; later direct mpv playback is still
mpv-owned network access. Connection-time pinning applies to the optional media
proxy, not automatically to every backend request.

## Backend lifecycle and service teardown

`test_backend_lifecycle.py` covers one shared startup, failed/missing/refused IPC,
command deadlines, malformed replies, child reap/kill, external ownership,
peer-PID/inode guarded socket cleanup, partial startup and late-command races.
Most cases are UNIT/MOCK. Four child-interpreter tests deliver actual SIGINT or
SIGTERM during startup/teardown while renderer resources are mocked. They verify
that cleanup executes instead of the interpreter exiting early on the signal.
These are not production AF_UNIX or real mpv-process shutdown acceptance.

## Established-station source recovery

`test_station_recovery.py` covers generation/queue/item identity, finite retry
budget, owner commands during every wait/load phase, native matching-source
return, failure categories and cleanup (UNIT/MOCK). Finite EOF behavior stays
separate from explicitly declared station recovery.

`test_libmpv_recovery.py` is an additional optional REAL_BACKEND/null lane under
the same ORR_TEST_LIBMPV_PATH environment variable. Each case runs in a bounded
child interpreter. An actual loopback HTTP server disconnects generated PCM,
returns HTTP 503 to a renderer reload, then restores media. Backend load counts
distinguish renderer retries from native reconnects, and independent libmpv
position confirms advancing decoded audio. No status-poll loop drives recovery.
Stop, pause, retained gain/index and resource cleanup are separately checked.

Initial never-playable stations fail without automatic recovery; cache-stall
detection and provider/CDN refresh remain deferred. No WAN, Unix IPC, acoustic,
DAC, ARM or Android certification follows from these local tests.

## Confirmed gain and measured digital output

`test_backend_gain.py`, `test_gain_api.py` and `test_gain_persistence.py` cover
paired acknowledgement/readback, startup/load/resume, external backend resets,
cancellation, atomic saved settings, malformed input and uncertain gain reporting.
Backend confirmation failure is different from a subsequent storage failure.
SOAP gain reads fault and GENA omits gain values while confirmation is unknown.

`test_libmpv_gain.py` adds an optional REAL_BACKEND/digital-PCM lane using the
same ORR_TEST_LIBMPV_PATH opt-in. Real loopback HTTP controls production gain
methods; inherited backend loads feed a float PCM file sink. Fast file output
is not presented as HTTP play/readiness acceptance. The null lane covers that.
Each native run has a child-process timeout and checks normal/cancel cleanup.

Measured with mpv 0.40 and generated stereo tones peaking at 500/32768:

| UI volume | Measured amplitude ratio | Gain |
| --- | --- | --- |
| 0 | exactly 0 | digital silence |
| 25 | 0.015625 (1/64) | -36.1236 dB |
| 50 | 0.125 (1/8) | -18.0618 dB |
| 100 | 1 | 0 dB |

Mute is exact zero at either tested source, including after changing volume.
Fresh Config/RendererState/backend reconstruction reads saved 25 + mute and emits
zero before any new user command; unmute retains 1/64. This is not a full
CLI/systemd reboot test. Native pre-initialization defaults are recorded
separately from the production initialization hook. Host mixer gain is separate;
these ratios do not describe a physical device's volume scale or acoustic level.

## Existing PEQ and balance

`test_backend_dsp.py` and `test_dsp_api.py` cover strict finite schemas, checked
replacement/readback, rollback/cancellation, serialized reads, source/channel
limits, startup balance, library migration, rename collisions, size bounds and
both pre- and post-replacement storage failures. Idle af readback verifies a
configured graph, not successful decoder initialization.

`test_libmpv_dsp.py` is optional REAL_BACKEND/digital-PCM. Real HTTP controls use
generated quiet stereo input and a float file sink, with direct backend source
loads labeled separately. Measurements include bypass, -12 dB EQ (observed
-12.039 dB at 1 kHz), exact silent balance endpoints, zero-gain low/high-pass
attenuation, EQ+balance composition and source retention. A direct native
nonexistent-filter rejection preserves the measured old graph; this is not a
claim that every malformed lavfi expression is rejected before playback.

Tests also verify normal/cancellation cleanup. No audible output is opened.
Positive-gain headroom/clipping prevention and physical channel routing are not
certified by the negative-cut fixtures. Active EQ restore is not yet implemented;
only the validated preset library and confirmed balance are persisted.

## Native Unix IPC and public CI

`test_mpv_unix_integration.py` is opt-in with ORR_TEST_UNIX_IPC=1. Default runs
skip it explicitly; opt-in with missing mpv, blocked sockets or failed playback
fails instead of substituting a transport. It uses actual production Unix JSON
IPC, generated WAVs, loopback HTTP/SOAP and null output. Separate cases exercise
external ownership and owned process/socket cleanup plus fresh-instance restart.
A test-local exec wrapper adds only null/config-isolation options and preserves
the mpv PID for peer-credential ownership checks. All children are reaped.

The PR workflow uses read-only contents permission, pinned official checkout,
no saved credentials, secrets, deployments or artifacts, and 15-minute jobs on
standard Ubuntu x86_64/ARM64 runners. GitHub documents these standard runners as
[free for public repositories](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
It records installed versions and runs default, libmpv and native IPC lanes
separately. ARM64 runner execution is not ARMv7, Pi/DAC or handset acceptance.

Verified status: both jobs in
[run 38185537983](https://github.com/de41tr/open-room-renderer/actions/runs/38185537983)
completed successfully at `52f5c4d`. Logs confirm native `x86_64`/`aarch64`,
Python 3.12.3, Ubuntu mpv/libmpv 0.37.0-1ubuntu4, pywiim 2.3.9 and aiohttp 3.14.5.
Per architecture: 1383 default passes (nine opt-in skips), seven real libmpv
passes, two real Unix IPC passes. Logs independently confirm peer-PID ownership,
owned socket removal/fresh restart, preservation of external mpv, distinct queue
resources and decoded controls/EOF. No transport mock is used by that lane.

## Metadata projection

`test_queue_metadata.py` checks bounded namespace-aware hints without changing
explicit resource selection. `test_metadata_surfaces.py` uses actual loopback
HTTP/SOAP/GENA with fake audio: opaque DIDL/whitespace, title/artist/album/artwork,
queue navigation, no prior-track leakage, backend title refinement, unknown
source/MIME and gain-confirmation schema parity. AP09 isolated snapshot: 1458
passed with seven optional REAL_BACKEND cases; two native IPC cases skipped
locally. Original handset rendering remains ANDROID_APP untested.

Configured music presets: `test_music_presets.py` uses real loopback HTTP and
pywiim listing/selection with fake audio. It checks exact distinct target URLs,
slot/count parity, malformed/empty config omission, unsupported indexes, failed
replacement, stale resolution, UTF-8 rename and identity/config reload. Only
the client-specific case skips without pywiim; unit/HTTP cases remain runnable.
Latest isolated preset snapshot: 1477 passed with seven libmpv cases enabled,
two local Unix IPC skips. Direct presets restart their target, not a saved
provider playlist position.

## Confirmed seek

`test_confirmed_seek.py` covers ignored forward/backward seek acknowledgements,
fixed two-sided position tolerance, all-stage cancellation/deadlines, EOF/error,
new owner commands, live/unknown files and fractional bounds.
`test_seek_proxy_ranges.py` checks optional shifted HTTP range validation.
Absolute targets must satisfy 0 <= target < known duration; relative buttons
clamp into that interval. Known live stations reject seek. Unknown duration
requires an actual position confirmation rather than synthetic duration.
The real libmpv and Unix IPC fixtures now include paused seek to10 seconds and
forward/backward HTTP/SOAP checks at response time. AP10 isolated default suite:
1590 passed, nine opt-in skips; extended libmpv/null seek passed separately.
Byte-range timing uses bitrate estimates and has no precise VBR acceptance claim.

## Native installation profiles

`test_native_installation.py` stages only temporary prefixes and checks source
receipts, version/architecture preflight, neutral/explicit ports, stable identity,
symlink/traversal/modified-file uninstall safety and static user-unit syntax.
Actual isolated native pip installation and manifest removal also passed on
x86_64/Python3.12/mpv0.40; no service was enabled. The CI workflow now repeats
real isolated package installation/import/config identity/description/uninstall
on its standard x86_64/AArch64 runners, separately from actual Unix IPC playback.
ARMv7, systemd boot, physical ALSA/DAC and original Android remain untested.
AP14 rebased isolated default suite: 1616 passed, nine explicit native skips.

## Active DSP restore

`test_active_dsp_persistence.py` covers strict versioned state, startup/deferred
restore, output-enabling gain gates, emergency silence, exact-state recovery,
legacy precedence, name validation and storage/cancellation boundaries.
`test_libmpv_dsp_restore.py` reconstructs native handles and measures preserved
cut/balance/bypass from generated low-level PCM; all handles close. It is added
to CI's explicit libmpv lane. AP13 rebased snapshot: 1692 passed with eight real
libmpv cases; two production Unix IPC cases skipped locally. Physical gain,
full process/systemd reboot and hardware remain separate acceptance work.

Native installation CI run38188925897 passed on both x86_64 and AArch64,
including isolated installed-package import, stable identity and owned uninstall.

## Local radio formats and container identity

`test_libmpv_formats.py` requires explicit libmpv opt-in plus FFmpeg to generate
all media at runtime. Missing native prerequisites fail after opt-in. Four
grouped cases cover MP3/ADTS/octet-stream/HLS, M3U/PLS containers, redirects and
ICY. `test_playlist_source_identity.py` checks exact provenance, generations,
entry loading, source-fenced controls and seek identity. A container remains one
canonical queue resource; inner title/time change, but canonical URI/index stay
with the container until final EOF. Nested containers are not accepted because
the outer ancestry is unavailable in current snapshots.
AP15 exact snapshot: 1714 passed with12 real libmpv cases, two local Unix IPC
skips. Local native format evidence is mpv0.40; the added CI lane will separately
verify Ubuntu0.37 on x86_64/AArch64. Generated fixtures have no DRM, provider
accounts or proprietary captures. This is not a WAN/live-radio certification.

Media diagnostic privacy: `test_log_safety.py` uses synthetic markers for raw,
quoted, mixed and JSON-escaped URLs, userinfo, query strings, fragments, malformed
URLs, double sanitization, access logs and exception formatting. It also checks
that actual backend arguments and responses are untouched. Ambiguous wrappers
are masked completely; raw resource paths remain visible. The isolated updated
snapshot passed1742 with12 real libmpv cases, two local Unix IPC skips.

Named native load options: `test_mpv_load_options.py` checks exact map framing,
acknowledgement matching and no duplicate retry. `test_libmpv_load_options.py`
uses the public command-node API to confirm start8 and per-file option reset;
the appended production Unix case exercises the same real option behavior.
This addresses the [documented0.38 positional change](https://github.com/mpv-player/mpv/blob/v0.40.0/DOCS/man/input.rst)
using the [older named command contract](https://github.com/mpv-player/mpv/blob/v0.37.0/player/command.c).
Snapshot: 1753 passed with13 libmpv cases, three local Unix IPC skips.

## Clocked queue transitions

Opt in with `ORR_TEST_CLOCKED_QUEUE=1` and run
`tests/test_clocked_queue_transition.py`. Linux mpv, pulseaudio and
pulseaudio-utils are required; missing dependencies fail after explicit opt-in.
The helper starts only a private null sink/protocol socket, captures continuously
clocked float PCM, checks idle clock rate and125ms silence calibration, then
compares native mpv and renderer queue transitions. Generated quiet stereo
markers verify both channels. Equal/mixed source rates share a fixed48kHz sink;
this does not test DAC switching, codec padding, WAN buffering or hardware.
A successful test means a valid measurement, not necessarily gapless playback:
read GAP_OBSERVED, NON_SILENT_BOUNDARY or WITHIN_TOLERANCE plus frame counts.
All native children are reaped; a95s parent deadline kills the owned process
group if native code hangs. Local17 analyzer tests passed, clocked execution
remains NOT_RUN until native CI. Full default snapshot:1757 passed,17 skips.

## Clocked measurement calibration repair

The first native clocked run38191121333 failed calibration on both architectures
(first marker11400/11793 frames versus12000). Existing playback, native IPC and
installation lanes passed. No renderer gap measurement was established.
The fixture now isolates its HOME/cookie, disables null-sink rewinds as an
experimental control, drains independently clocked frames to a quiet tail and
reports bounded aggregate diagnostics. Rewinds are not a proven cause. Strict
marker counts,125ms calibration and2ms tolerance are unchanged. The exact
repair snapshot passed1759 default tests with17 explicit native skips. Native
retry remains pending; do not describe this as gapless playback verification.
