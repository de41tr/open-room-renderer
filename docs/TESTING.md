# Test evidence and limits

## Automated suite

```sh
.venv/bin/python -m pytest -q
```

Latest completed native CI checkpoint: `21f1a017`, on Ubuntu24.04 x86_64 and
AArch64, with actual mpv/libmpv0.37.0. The default suite, explicit libmpv
null/digital-PCM tests, production Unix IPC/lifecycle, private native installation,
clocked measurement and discovered-endpoint playback all passed.
[Verified run](https://github.com/de41tr/open-room-renderer/actions/runs/38193687112).
Newer work must be checked against its own CI result. Baseline was92 tests;
checkpoint history is in STATUS.md. Counts alone do not establish app or hardware
compatibility.

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
runners (below). Explicit-interface loopback SSDP/mDNS multicast now also passes
in the sandbox, and the complete discovered-endpoint production-IPC flow passes
in native CI. Do not replace production IPC to accommodate this sandbox. Physical
LAN, ARMv7/Odroid, DAC/acoustic audio and original Android app validation remain
separate acceptance steps.

## Playback generation and local client coverage

`tests/test_playback_generations.py` checks delayed load/resolve/readiness,
source mismatch, pause/stop/resume, EOF/error/buffering, bounded seek completion,
and observer cleanup with a deterministic backend (UNIT/MOCK).

`tests/test_local_client_integration.py` uses real loopback HTTP and installed
pywiim 2.3.9 with a stateful fake backend (INTEGRATION + MOCK). It exercises
transport, volume/mute, explicit backend failures and recovery, and asserts
session/listener/task cleanup. It does not fetch media or perform discovery.
The installed client's TLS-version deprecation warning is an upstream warning.

The public client's `play_playlist(url)` command preserves an unindexed URI,
including ports, IPv6, path/query colons and escaped resource bytes. The legacy
indexed form is recognized only when the entire inner URI is percent-quoted
before the `:<index>` suffix. This renderer supports only exact index `0` there,
meaning start the container at its beginning; it is unrelated to the canonical
queue's one-based index. Unsupported indexes and empty/invalid queue text fault
before changing playback. A literal URI ending in `:3` remains that exact URI.
The optional libmpv format lane also exercises actual pywiim M3U/PLS playback,
native child source/decoder progression and rejection preserving paused audio.

## Real client with production Unix IPC

```sh
ORR_TEST_PYWIIM_UNIX=1 PYTHONPATH=. .venv/bin/pytest -q -s \
  tests/test_pywiim_unix_integration.py
```

This separate, bounded gate uses the actual installed pywiim client and parser
with unmodified production `MpvBackend` Unix JSON IPC. Linux, mpv, pywiim and
working Unix sockets are required; explicit opt-in fails rather than skipping
or substituting another backend. The shared whole-process supervisor bounds
worker and native child cleanup. CI runs it separately on both architectures.

Only generated distinct WAVs and exact loopback HTTP media targets are allowed.
One resource contains an encoded path separator and synthetic signed-query-shaped
values; the fixture rejects changed raw request targets. All controls use the
real public client methods: play_url, pause, resume, next_track, previous_track,
set_volume, set_mute and stop. SOAP creates the finite queue because pywiim 2.3.9
has no queue-creation method; it does not stand in for any client control.

Checks include independently read native source/decoded progression, stationary
paused time, paused single-track Previous restart, next/previous native resource
changes, exact parsed/canonical HTTP fields, and actually delivered ordered GENA.
Gain/mute values are actual native readbacks; null output is not audible gain or
mute evidence. The suite checks session, listener, subscriptions, observer/event
tasks and fixture-owned native child cleanup. No discovery, physical LAN, original
Android app, vendor account or hardware acceptance is claimed.

Local production Unix IPC is explicitly NOT_RUN because AF_UNIX is denied with
EPERM. A separate real pywiim 2.3.9 + libmpv 0.40/null diagnostic passed the same
control/HTTP/GENA assertions and both exact raw media targets after the navigation
and URI-preservation fixes. That diagnostic is not production Unix IPC evidence.
Actual CI results for this new gate must be recorded separately.

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

`test_identity_publication.py` uses real local files, controlled partial writes
and eight concurrent subprocess creators with distinct synthetic proposals.
First-run UUID creation writes and fsyncs a private same-directory file, then
hard-links it atomically to `uuid` without overwriting an existing destination.
Both the winner and losing creators remove only their own private link and
fsync the state directory before returning. A partial write or pre-publication
file-sync/link failure cannot publish an empty/truncated UUID. Hard-link support
is required; unsupported filesystems fail without a direct-write/replace fallback.
After publication, cleanup/directory-open/sync failures fail startup explicitly
while preserving the full published ID; a restart reuses it. A reader can see
the complete published file before the creator finishes its directory sync.

Existing nonempty normalized/legacy identities and explicit configured IDs retain
their previous semantics without rewrite or stricter format validation. Existing
empty or unreadable UUIDs still fail; an old nonempty truncated file cannot be
distinguished safely from an intentional legacy ID and is not auto-repaired.
An abruptly killed creator can leave an ignored `.uuid.*` temporary file; only
the canonical `uuid` is adopted. Tests exercise Linux local filesystem behavior
and synthetic failures, not power loss, network filesystems or hardware storage
guarantees. Directory fsync does not certify persistence of newly created ancestor
directories. Existing-file reads do not recertify durability or modify the file.

`test_device_rename_persistence.py` uses local HTTP with synthetic storage and
refresh faults. Rename atomically saves UTF-8 before updating live fields; failure
before replacement preserves the previous file/name and does not refresh discovery.
If replacement succeeds but directory durability cannot be confirmed, the live name
matches the new file and refresh is attempted, but HTTP returns 502/`Failed`.
A refresh failure likewise returns 502; cancellation propagates. Neither rolls
back a committed name, and status notification is still scheduled. A same-name
retry repeats refresh. Concurrent renames are serialized through the callback.
These tests re-read actual config/state files and check stable identity after
restart; they do not simulate power loss or verify remote discovery reception.
HTTP `OK` requires successful local persistence and completion of the configured
refresh callback, which may be a no-op when discovery is disabled or stopped.

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
Most cases are UNIT/MOCK. Nine child-interpreter tests deliver actual SIGINT,
SIGTERM or SIGHUP during startup/teardown while renderer resources are mocked.
They verify that cleanup executes instead of the interpreter exiting early on
the signal, and ordinary startup errors still exit nonzero after cleanup.
These are not production AF_UNIX or real mpv-process shutdown acceptance.

## Real CLI service and installed-entry-point lifecycle

```sh
ORR_TEST_CLI_LIFECYCLE=1 PYTHONPATH=. .venv/bin/python -m pytest -q -s \
  tests/test_cli_lifecycle_native.py
```

This separate REAL_BACKEND/null lane starts the actual
`python -m fake_linkplay.cli serve` process, without importing or replacing its
backend in the worker. Each owned/external-mpv scenario exercises three lifetimes
on the same private generated config, state, socket and high HTTP/UPnP ports.
Real CLI HTTP commands play generated WAV audio, pause/resume, set volume and
stop; independent native readback confirms the decoded source, advancing time,
null audio output, gain and balance graph. Restart must restore generated UUID,
name, volume, mute and balance; the surviving external player is deliberately
changed between lifetimes so stale native settings cannot masquerade as restore.

SIGTERM after idle, then SIGINT and SIGHUP during playback, must return zero
and release both HTTP listeners. SIGHUP means graceful stop, not config reload.
An owned native child must be reaped and its socket removed; an external native
PID/socket must survive all three renderer exits before the fixture reaps it.
Each scenario runs under the existing signal-safe group supervisor with a
90-second hard deadline. HTTP binds/advertised addresses/discovery interface
are explicit loopback; discovery is disabled because its broad protocol coverage
belongs to the separate discovery lane. No host service, account or security
setting is changed.

Set `ORR_TEST_CLI_EXECUTABLE` to an absolute installed `open-room-renderer` entry
point to run the identical lane from outside the source tree without inherited
`PYTHONPATH` or `PYTHONHOME`. Native installer CI runs it before uninstall; source
execution remains an optional local command. `ORR_TEST_MPV_BINARY` can select a trusted real mpv
executable. The fixture adds only null/config/script safety flags with a
PID-preserving exec wrapper; production defaults and IPC stay unchanged.

Without the exact opt-in these tests skip. With it, missing mpv or forbidden
AF_UNIX is a failure, never a skip or alternate transport. The current sandbox
rejects AF_UNIX at preflight. SIGTERM/SIGINT owned and external cycles have
passed production-IPC CI. The SIGHUP cycle also passed both architectures in
run 38205184583, whose ARM job separately failed the final optional Snapcast gate. Its
helper/isolation tests run in the default suite. Null output proves neither
audible audio nor hardware, physical LAN, original app or systemd/reboot behavior.

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
Saved gain rejects duplicate JSON fields (including escaped field names) without
rewriting the file. CLI startup rejects such ambiguous state before constructing
the backend; absent state still uses configured defaults. The 4096-byte document limit
and valid version-1 gain format are unchanged.
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

AP07 also has a dedicated post-decode owned-player-death case:

```sh
ORR_TEST_UNIX_IPC=1 PYTHONPATH=. .venv/bin/python -m pytest -q -s \
  tests/test_mpv_unix_integration.py -k owned_player_death
```

It verifies Linux peer credentials and actual null-output decoder progress
before SIGKILL of the fixture's own child. Without status polling, the renderer
must report STOPPED/ERROR through a real GENA callback and withdraw confirmed
gain. HTTP and SOAP must agree. The case watches past the complete station
retry window, counts executable invocations, and checks no source retry, queue
advance, automatic respawn or changed desired gain. Explicit Play on the failed
instance must fail; a fresh renderer backend/API on the cleaned socket starts
idle and plays a different generated source only after an explicit command.
Owned children, IPC pathname, subscriptions, monitors and listeners are cleaned
up. This is a focused process/control acceptance case, not a service-manager
restart, measured acoustic silence or hardware test.

The first native CI execution at ba7b3120 reached the explicit retry after real
owned-player death on both architectures, then failed because the test expected
503 instead of the established 502. The exact assertion now requires 502/plain
`Failed`, with an additional loopback regression. Its later fresh-restart tail
still awaits native execution. Unix sockets remain disallowed in the local
sandbox; no alternate transport is credited as that process-death result.
The local deterministic counterparts in `test_backend_lifecycle.py` and
`test_gena_lifecycle.py` use fake child/IPC or fake audio with real loopback
HTTP/SOAP/GENA; neither establishes actual process death. No libmpv substitution
is accepted as evidence for the Unix process-death gate.

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

## Failed source loads stop only their own active decoder

A failed/unready load or cancellation now performs bounded checked Stop and
actual idle confirmation when that load may have reached the backend. Resolver
failures before any load leave old playback untouched. Newer source owners are
fenced; physical cleanup remains eligible if the same generation's recovery
token is invalidated or shutdown begins. Failed Stop/readback preserves explicit
backend-control uncertainty instead of claiming silence or retrying the source.

An actual libmpv nested-M3U fixture demonstrates the original problem: the inner
MP3 is decoding while outer-source readiness fails. With cleanup the HTTP502
returns only after confirmed idle and cleared actual identity; a subsequent
valid source plays. Nested playlist ancestry is still unsupported. Independent
review covered observer-error, shutdown and newer-owner interleavings.

## Explicit discovery interface and real discovered-client flow

Optional discovery_interface selects a literal local IPv4 address for SSDP and
both mDNS implementations. It controls multicast membership and outbound traffic;
on Linux, owned receive sockets disable all-interface membership widening. Generic
zeroconf startup verifies its socket layout and fails closed if explicit isolation
cannot be established. Default routing is unchanged. Startup/cancellation errors
close partially acquired sockets and await tasks. No host routes/firewalls change.

Actual Linux loopback multicast discovery passed locally: SSDP M-SEARCH and alive,
custom mDNS PTR/SRV/TXT/A, two independent identities and HTTP descriptions, plus
zeroconf receive isolation. Group-address-bound client sockets reject a deliberate
unicast-only mutation. This establishes multicast wire behavior on one host, not
physical LAN or Android compatibility.

A separate explicit native CI flow discovers its endpoint before fetching the
description, then uses production mpv Unix IPC with two different generated WAV
files. It checks play/pause/resume/Next/volume/mute/unmute/Stop and consistent
HTTP/SOAP/ordered GENA. That combined native flow remains NOT_RUN locally because
Unix socket creation is restricted; its CI result must be recorded separately.

Exact combined cleanup/discovery snapshot: 1849 passed with real libmpv and
multicast discovery enabled; five explicit production-IPC/clocked/combined-flow
cases skipped locally. All code is synthetic or independently implemented; no
physical LAN/audio or original Android claim follows from these checks.

Clocked retry run38192155611 passed all preexisting lanes, but the new optional
audio-parameter diagnostic raced lazy output creation before calibration. A
bounded optional-metadata sampler now records unavailable values without
bypassing any strict PCM checks (22 focused tests passed, one native skip).
The next native run must establish calibration and renderer measurements.

## Native discovery/playback and clocked transition result

Head eb7c2ac9f3718ec80ef2efd05f1e4a4e0eeddcf6 passed all lanes on native
x86_64 and AArch64 in
[run38192653073](https://github.com/de41tr/open-room-renderer/actions/runs/38192653073).
Actual multicast discovery led to fetched control/event URLs, two distinct WAV
resources and real production Unix-IPC play/pause/resume/Next/volume/mute/unmute/
Stop, with HTTP/SOAP/GENA agreement and complete child/task/listener cleanup.
The new nested-playlist cleanup also passed with actual mpv0.37 decoding.

The continuously clocked digital probe now calibrates the known125ms silence
exactly and preserves all96000 frames per track. Equal-rate ORR boundary gaps
were6.125ms on x86_64 and25.125ms on AArch64; mixed-rate gaps6.8125/25.333ms;
pause-near-transition gaps4.9375/26.125ms. Direct native equal-rate mpv playlist
measured0ms on both. These are bounded same-host measurements with2ms stated
tolerance, not a distribution of repeated results or physical DAC evidence.
All ORR cases are GAP_OBSERVED; passing the measurement does not mean gapless.
Observer phase and native-EOF versus sink timing are being investigated
before attributing the gap or changing queue loading.

## Experimental service honesty

The unfinished Plex scaffolding no longer advertises active/configurable
playback, even when explicitly selected for diagnostics. Its inactive shape is
retained, but playable service lists and legacy support masks exclude Plex.
All protocol capability projections agree. This does not implement or exercise
account login. The README no longer suggests another application's OAuth
callback or a manual token-file recipe for the unsupported integration.

Final experimental-service snapshot: 1860 default tests passed,22 explicit
native skips. Independent review passed109 focused tests and confirmed27
ordinary neutral/TuneIn/direct-source configurations match baseline. Reserved
provider source aliases and padded legacy decimal masks are covered.

## Established live stations recover from no-EOF cache stalls

A real held-open local response reproduced an indefinite BUFFERING state: audio
reached9.984s, then paused-for-cache/core-idle stayed true without EOF. The
monitor now requires15 seconds of unchanged decoded position for an established
explicit live station before using the existing three-attempt retry budget.
Transient return or any decoded progress resets the grace; fresh snapshots
recheck that evidence before a queued retry or terminal Stop. Initial loads,
finite files, unknown sources and owner Pause/Stop remain separate.

At exhaustion a stalled socket is stopped and actual idle confirmed, so delayed
network bytes cannot silently restart audio after ERROR. Cleanup remains bounded
and same-owner-fenced through shutdown and newer source selection. Failed Stop
or readback reports silence unconfirmed rather than claiming a stopped decoder.
No gain, mute, canonical selection or generation is changed by successful retry.

Independent review reproduced and fixed a backoff race where resumed progress
previously failed to reset the queued deadline. The original negative controls
now pass unchanged. Native tests use generated held-open HTTP media and real
libmpv/null; production15-second recovery, transient return, owner commands,
finite exclusion and exhausted retries are separate scenarios. This is not
provider-URL refresh, physical WAN/router testing or hardware playback proof.

Final rebased snapshot: 1937 passed with all optional libmpv cases enabled;
eight explicit native IPC/discovery/clocked cases skipped locally. Independent
review cleared the unchanged adversarial repros and production15-second grace.
The seven actual no-EOF scenarios are now in the native CI libmpv lane; monitor
that revision separately. Prior21f1a017 CI passed all lanes on both architectures.

## Bounded native probe supervision

The native acceptance supervisors now clean only their owned process group after
SIGTERM/SIGHUP/SIGINT, timeout, spawn failure, abnormal worker exit and normal
exit with leftover descendants. Signal handlers are restored. Independent real
Snapcast and synthetic clocked-wrapper interruption checks found zero survivors;
previous supervisors could leave detached processes running. This is test/tool
lifecycle code, not a production playback transport change.

## Optional Snapcast digital/control experiment

This is a standalone acceptance tool, not an advertised multiroom feature or a
mandatory runtime dependency. Official Debian Snapcast0.31.0 and mpv0.40.0 passed
real same-host PCM delivery to two file-output clients, join/leave/rejoin, group
mute, non-atomic per-client volume fan-out and reconnect retention. Every stage
checks decoded stereo frequency/RMS and live source/group/control state, rather
than accepting silent output alone as proof of mute.

```sh
python tools/acceptance_snapcast.py \
  --snapserver /absolute/path/to/snapserver \
  --snapclient /absolute/path/to/snapclient \
  --mpv /absolute/path/to/mpv --output /tmp/orr-snapcast-new-run
```

The output directory must be new. Keep it private: it contains generated audio,
raw digital captures and upstream logs, which can contain the host name. Only
reviewed aggregate metrics may be shared. The tool uses quiet generated48kHz
stereo, a private FIFO, file outputs, synthetic per-run identities, loopback
listeners and an isolated child D-Bus address. It installs nothing and starts no
host service. Its whole owned process group has a90-second hard deadline.

On a native Unix-socket-capable Linux host with project dependencies installed,
add `--orr-ipc` to require the unmodified production HttpApi/MpvBackend path and
additional downstream PCM checks for HTTP play, gain, mute, restore and Stop.
This gate is NOT_RUN in the restricted development host. CI downloads signed-
index-verified Ubuntu Snapcast packages and extracts data only, without running
maintainer scripts or enabling their services, then runs this separate gate.
Record its actual result before claiming ORR-to-Snapcast acceptance.

Same-host file delivery does not establish two-host clock skew, DAC timing or
acoustic synchronization. Group volume uses acknowledged per-client fan-out,
not an atomic group transaction. Upstream mpv gain and per-room Snapcast gain
remain separate; no vendor group compatibility or account flow is implemented.
Primary references: [Snapcast](https://github.com/snapcast/snapcast),
[mpv/FIFO setup](https://github.com/snapcast/snapcast/blob/develop/doc/player_setup.md#mpv),
and [control API](https://github.com/snapcast/snapcast/blob/develop/doc/json_rpc_api/control.md).

## Experimental native successor staging (default off)

`native_queue_staging: true` opts into one-successor mpv playlist staging for
confirmed finite direct playback. The default is false in every profile; strings
are rejected as flag values. Gain, DSP and mpv audio defaults are unchanged.
Append acknowledgement alone never changes canonical metadata: an owned native
entry ID must actually become current/playing before queue adoption. Duplicate
URLs, delayed acknowledgement, Pause/Resume/seek and manual Next/Previous around
a native transition are fenced by ownership and confirmed state.

Active native M3U/PLS containers are not staged further. A successor with exact
first-level container ancestry can be adopted as one canonical item. Nested or
foreign-tail ancestry that cannot be proved is explicitly unsupported: ERROR
reports silence not confirmed, does not falsely adopt the source, and does not
Stop an unproved foreign source. Such audio may remain active until an explicit
owner control succeeds. This opt-in is not blanket gapless certification.

Independent review reproduced and fixed lost-ACK-after-EOF, invalid-seek pause
restoration, stale source-resolution ownership and gain-error adoption races.
The exact rebased snapshot passed2024 tests with real libmpv enabled; eight
explicit native IPC/discovery/clocked cases skipped locally. Native/null tests
confirm entry ownership, cleanup, navigation and unchanged gain/DSP defaults.
A supplementary private clocked diagnostic measured equal-rate128.75ms off
versus0ms on at a0.35s monitor-phase offset, with96000 frames per track. Mixed
rate still measured9.083ms. This is not production Unix-IPC acceptance.

The native clocked CI lane now compares off/on equal and mixed rates at the
phase offset, plus staged pause near EOF. Exact sample counts remain required;
only staged equal-rate playback has a hard2ms acceptance gate. Mixed-rate gaps
are reported honestly. The actual native results are recorded below.

Native production Unix-IPC measurements in [run38196685851](https://github.com/de41tr/open-room-renderer/actions/runs/38196685851)
passed the staged equal-rate gate on both architectures: phase-offset unstaged
168.354ms (x86_64) / 186.979ms (AArch64) became 0ms with staging. Staged pause
also measured 0ms; mixed-rate remained 8.25ms / 5.9375ms. Each track retained
exactly 96000 frames and the known 125ms silence calibration remained exact.
This is clocked digital output, not physical audio. Staging stays default off.
Every renderer lane passed; the separate direct-FIFO Snapcast experiment failed.

## Public-station address renewal

Successfully resolved anonymous station references now carry internal, URL-bound
provenance. Recovery and a later explicit Play refresh an expired stream address
through that same fixed public resolver, retaining the requested station, queue
identity, metadata and gain. Client-supplied labels, IDs and XML cannot grant
resolver authority. Initial materialization resolves once; explicit finite
tracks and ordinary URLs retain their existing behavior. Typed station results
are deliberately not native-staged as finite successors.

Fresh progress is checked again at the serialized load boundary; a deferral
refunds retry budget. Terminal recovery failure confirms same-owner Stop/idle
or reports uncertainty, including EOF/idle cases that began buffering again.
Independent review covered staging off/on, finite-to-station transitions and
foreign tails. The exact current-base snapshot passed 2081 tests with optional
libmpv enabled, with eight environment-specific native cases skipped. Controlled
local resolver/media tests prove expired address A is replaced with B; this is
not a live-provider availability or Android acceptance claim.

## Bounded repeated-playback stability

`ORR_TEST_UNIX_STABILITY=1 PYTHONPATH=. .venv/bin/pytest -q -s tests/test_mpv_unix_stability.py`
opts into unmodified production Unix IPC over three owned mpv lifetimes (180s
by default, bounded to 180..900s). The shared flow exercises HTTP/SOAP/GENA,
actual source changes, controls, callback retries, station interruptions and
independent Stop/source-replacement recovery fences. Native events independently
count loads and EOF; controller and child resources are measured separately.
Linux PPid enumeration has a real disposable-child sensitivity test. Opt-in
fails when Unix IPC is unavailable; it never silently substitutes libmpv.

The separately opt-in libmpv lane uses `ORR_TEST_LIBMPV_STABILITY=1`,
`ORR_TEST_LIBMPV_PATH` and `ORR_TEST_LIBMPV_STABILITY_SECONDS` (recommended 60..900; allowed 30..3600) with
`tests/test_libmpv_stability.py`. At production checkpoint 7c20ef8, the final
reviewed short lane passed in 88.258s: 24 cycles, 60 loads, 52 source changes,
121 cross-surface agreements and six callback retries held through actual gain
readback. All three cleanups returned to one task, seven FDs, one thread and
zero children/subscriptions. A separately frozen 907.402s run at older production
checkpoint eb7c2ac9 covered 367 cycles and 746 loads; it is not evidence for
later changes or the final strengthened counter assertions. Repeated in-process
libmpv creation retained allocator memory; these bounded budgets do not prove
leak freedom or uninterrupted-stream endurance. The new production Unix stress
lane awaits CI. Actual pending-retry Pause cancellation remains separately
covered by libmpv recovery, not this Unix stress flow.

## Snapcast production-IPC ingress correction

The first native Snapcast gate failed HTTP Play on both architectures: direct
untimed mpv PCM/FIFO backpressure blocked property reads beyond production's
existing deadline. Renderer control timeouts and readbacks are unchanged.
`tools/acceptance_snapcast.py --orr-ipc` now defaults to a private clocked Pulse
null sink and monitor feeding the private Snapcast FIFO. It needs pulseaudio,
parec and pactl, uses its own runtime/server/sink and supervised process group,
and neither discovers host audio nor installs/enables a service. `--ingress=fifo`
retains the original diagnostic path; standalone mode still defaults to FIFO.

Independent real-libmpv diagnostic comparisons passed all eleven control/PCM
stages, and the original standalone FIFO passed seven. Native production Unix
IPC with the distribution's Snapcast version still awaits CI. Spectral checks
use independent short windows with an 80% window tolerance plus unchanged RMS
and gain requirements; this is not sample-perfect continuity or clock-skew
measurement. Mute still requires exact-zero PCM. Upstream silent-source status
may be playing or idle only with independent native identity/control/progress
proof; group mute still requires an active playing source. Killed-worker,
recorder-failure and termination tests verified bounded owned-process cleanup.
No two-host synchronization or advertised service capability is claimed.

Combined publication snapshot: 2082 default tests passed / 36 explicit opt-in
skips; 46 focused native-refresh/staging/Snapcast/helper tests passed. The
refreshed production snapshot also passed the separately enabled short libmpv
stability run (four tests, 88.69s). Production Unix IPC, architecture-specific
behavior and the clocked Snapcast path require the new native CI run.


## Unconfirmed owner transport commands

`test_transport_uncertainty.py` checks that rejected, timed-out or cancelled
Stop/Pause/Resume commands retain the latest owner intent and expose ERROR,
rather than restoring stale PLAYING or treating a possibly applied Stop as EOF.
No retry or finite queue advance is permitted until a new owner command.
Delayed old loads/snapshots and newer Play commands remain generation-fenced.
Retired native entry IDs retain an output-uncertainty fence before Stop can yield;
cancelled cleanup and a superseding Pause cannot let later Play/Resume bypass
checked Stop/reload and resume an unidentified native successor.

`test_libmpv_transport_uncertainty.py` uses the existing ORR_TEST_LIBMPV_PATH
opt-in. Generated local WAVs and real libmpv/null confirm Stop or Pause applied,
then the test injects a lost acknowledgement. Actual HTTP failure, SOAP state,
delivered GENA error, no automatic second track, and later explicit Play of the
original selection are verified. This is fault-injected REAL_BACKEND/null plus
local HTTP/SOAP/GENA integration, not production Unix IPC or physical audio.

## Verified native checkpoint b011557

[Run38198310911](https://github.com/de41tr/open-room-renderer/actions/runs/38198310911)
passed every lane on Ubuntu x86_64 and AArch64. The production Unix-IPC stress
ran 185.745s / 186.315s across three owned mpv lifetimes per architecture:
132 / 138 native loads, 124 / 130 source changes, 301 / 316 HTTP/SOAP/GENA
agreements, and six independently observed EOFs each. Every lifetime reaped
its child, removed its Unix socket and closed event observers/listeners;
controller cleanup returned to one task, seven FDs, one thread and zero children.
Child resource counts were separate; this is bounded stress, not leak freedom.

The repaired private clocked Snapcast ingress passed all eleven stages using
production Unix IPC with mpv0.37, PulseAudio16.1 and Snapcast0.27 on both runners.
Two file-output clients demonstrated distribution, join/leave/rejoin, per-client
non-atomic group gain, exact-zero group/upstream mute, reconnect retention and
HTTP Stop draining to digital silence. All owned children were reaped and
checked ports closed. This replaces the initial direct-FIFO gate failure;
it does not establish two-host synchronization, physical audio, vendor grouping
or an advertised renderer service. Station-address refresh and all previous
renderer lanes also passed at this exact checkpoint.

## Owner intent and real-client compatibility corrections

An applied native Stop with a lost acknowledgement previously restored old play
intent, so its idle observation could auto-start the next finite track. Rejected,
unconfirmed or cancelled Stop/Pause/Resume now retain latest owner intent and
report explicit ERROR; unconfirmed silence is never described as successful Stop.
Retired native output remains fenced across cancellation and newer commands.
Only a new owner command can recover playback, with checked Stop/reload when
native ownership is uncertain. Independent real libmpv/null HTTP/SOAP/GENA tests
proved no automatic second track and explicit Play recovery of the selection.

Real pywiim HTTP Next/Previous now share existing SOAP navigation semantics.
Already decoded absolute media URIs retain encoded path separators and signed-
query bytes; only a fully quoted outer URI wrapper gets one additional decode.
The real client and decoder reached an exact local target containing `%2F`,
`%26`, `%3D`, `%252F` and UTF-8 escapes. This fixes an actual wrong-resource load,
not merely display text. Malformed navigation suffixes still fail without mutation.

CLI commands return nonzero on HTTP errors, redirects and network failures;
errors omit media URLs/response bodies, and redirects are not followed. The
reviewed installed-entry lifecycle gate checks actual serve/restart, persisted
identity/gain/DSP, signals and owned/external process boundaries. The new actual
pywiim/Unix-IPC gate verifies real-client controls and exact media resources.
These two production-native additions require their own CI result; local native
transport remains restricted. The combined default snapshot passed 2190 tests
with 41 explicit opt-in skips. With the real-libmpv option enabled, the same
combined regression suite passed 2218 tests with 13 separate opt-in native cases
skipped in 291.85s. Installed CLI/actual-client production IPC still await CI.

## Verified native checkpoint 9f5f529 and Python floor

[Run38200058359](https://github.com/de41tr/open-room-renderer/actions/runs/38200058359)
passed every lane on x86_64 and AArch64. The actual pywiim2.3.9 client used
production Unix IPC with mpv0.37, selected two distinct generated resources,
and delivered the exact encoded media path/query unchanged. Parsed client,
canonical HTTP, native readback and ordered GENA agreed for play, pause, resume,
Next/Previous, volume, mute/unmute and Stop. The null-output gate checks actual
gain settings, not audible gain. Its fixture child and all owned tasks/listeners
were cleaned up.

The installed CLI passed owned/external mpv scenarios on both architectures,
with SIGTERM and SIGINT across same-port/socket restarts. UUID, name, volume,
mute and balance survived restart; actual native state was restored even after
the surviving external player was deliberately changed. Owned children/sockets
were removed, while the external peer was preserved until fixture teardown.
These two scenarios took 7.97s / 10.23s, leaving the complete jobs below eleven
minutes. This is actual installed process acceptance, not systemd boot or hardware.

The exact 9f5f529 source also passed an isolated CPython3.11.17/x86_64 run:
2190 default tests /41 gated skips, plus six actual libmpv0.40/null or digital-PCM
cases and one supporting encoding unit test. Controls, seek/queue, gain/mute,
load options, native staging and uncertain owner controls were exercised.
Official-PyPI dependencies passed a 35-package compatibility check. The runtime
came from uv's official Astral CPython distribution; independent archive checksum
verification was unavailable, and no security/newest-version certification is
claimed. This does not prove ARMv7, Debian bookworm's own package combination or
all Python patch/dependency versions. Local Unix sockets remained restricted.

## Complete public-client playlist resources

A real pywiim play_playlist request was previously split at URI colons, including
ports or a legitimate final `:3`. A reproduced request for an AAC playlist could
therefore decode a different MP3 playlist and still report success. Complete
unindexed URIs now remain intact, including escaped path/signature values.

Only a fully quoted inner URI followed by a literal colon has a distinguishable
legacy index. Exact index0 is this renderer's supported start-at-first form;
other or empty indices fault before native Stop, queue replacement, generation
changes or events. This is separate from the canonical queue's one-based index.
Candidate queue validation also precedes existing native-stage interruption.
Independent native tests proved both the intended AAC resource and rejection
without disturbing paused staged playback. Local verification: 2203 default
passes /41 skips, plus98 focused/native-format cases in the publication snapshot.
Production-native results for this new revision must be checked separately.

A separate standard x86_64 Python3.11 default-suite CI job now protects the
declared runtime floor. It uses verified immutable first-party checkout/setup
actions, official-PyPI dependencies, no dependency cache, no native package
installation, and an eight-minute whole-job cap. Existing native jobs and their
fifteen-minute caps are unchanged. The first hosted floor result is pending.

The playlist publication snapshot also passed the full default suite on actual
CPython3.11.17: 2203 passed /41 gated skips in65.16s. The selected Python3.11
real-libmpv evidence above remains tied to checkpoint9f5f529.

## Per-load live-station packet history

For a canonical current queue item explicitly marked `stream_type="station"`
with zero duration, the renderer supplies `demuxer-max-back-bytes=1048576` and `demuxer-donate-buffer=no` in the
existing named `loadfile` options. It does not change global mpv configuration,
forward buffering, cache/pause settings or retry timing. Finite or untyped media
and prospective unrelated native successors keep the native defaults. Known
live stations already reject seeking; Pause/Resume continues buffering normally.

The [mpv 0.37 option contract](https://github.com/mpv-player/mpv/blob/v0.37.0/DOCS/man/options.rst)
defines backward packet retention separately from forward caching. Its
[loadfile contract](https://github.com/mpv-player/mpv/blob/v0.37.0/DOCS/man/input.rst)
restores per-file options after playback. The tested mpv 0.40 defaults retain up
to 50 MiB of backward packets, which is expected native cache growth, not a
Python leak. A finite 1 MiB backward limit keeps this unused history bounded; disabling
buffer donation prevents backward history borrowing unused forward capacity.
Zero is deliberately avoided: paced ICY on mpv 0.40 lost native timed-title
updates with zero retention, while the same typed station and source retained
all changes with a 1 MiB cap. The previous zero-history endurance results do not
validate this revised policy.

`test_station_cache_options.py` tests exact canonical ownership, stale live
mirrors, finite/unknown media, resolver/header typing, address refresh, successor
isolation and both native-fixture command shapes. The opt-in
`test_libmpv_station_cache.py` reads actual native options, checks finite and
unknown loads reset to their pre-station defaults, and verifies advancing audio,
Pause/Resume without reload, owner Stop/replay, source identity and gain/DSP.
The real EOF/stall/refresh lanes also check that successful station reloads retain
the history policy without altering forward cache settings. These tests are
included in the existing Ubuntu mpv/libmpv acceptance matrix, including 0.37.

```bash
ORR_TEST_LIBMPV_PATH=/path/to/libmpv.so.2 PYTHONPATH=. .venv/bin/python -m pytest -q -s \
  tests/test_libmpv_station_cache.py tests/test_libmpv_recovery.py \
  tests/test_libmpv_stall_recovery.py tests/test_libmpv_station_refresh.py
```

This native/null lane does not certify production Unix IPC, WAN stations, audible
hardware or indefinite memory stability. Continuous-stream resource evidence must
come from a separate bounded, frozen-source run with unchanged resource budgets;
short per-file option checks alone do not establish that result.

## Live-station history reduction and extended native checkpoint

The preceding [60d6b0a checkpoint](https://github.com/de41tr/open-room-renderer/actions/runs/38201708621)
passed both native architectures and the new hosted Python 3.11.17 default job.
Its real public-client playlist tests now decode the complete intended M3U/PLS
resource, including literal colons and escaped query bytes.

At that earlier checkpoint, only the matching canonical zero-duration station
received per-file zero backward packet history. The positive-cap policy above
supersedes zero; those frozen results remain historical. Native forward buffering, cache/pause settings,
recovery timing, gain and DSP remain unchanged. Finite and unknown media restore
their previous native defaults. This is supported by the documented
[mpv 0.35.1 backward-cache option](https://github.com/mpv-player/mpv/blob/v0.35.1/DOCS/man/options.rst)
and [per-file load options](https://github.com/mpv-player/mpv/blob/v0.35.1/DOCS/man/input.rst),
as well as the reviewed 0.37 contract; documentation is not a 0.35 runtime test.

The original frozen b011557 uninterrupted-station attempt is FAILED endurance:
its test exited after 3299.25s overall, before the requested hour completed.
RSS grew 49,160 KiB against the unchanged 49,152 KiB allowance; other progress,
protocol and resource checks had not failed. Cleanup completed. Paired 300s
measurements attribute sustained growth to native backward packet caching and
allocator retention, not an accumulating Python object set. Do not relabel this
failed run as an hour pass or as proof of a Python leak.

A separate frozen production-policy run passed 300.09s with one load/request,
300.053s decoded progression, RSS growth 84 KiB and essentially flat 22 KiB packet
cache. Original budgets, state/progress assertions and cleanup gates were retained;
there was no diagnostic global override. A new immutable hour run began at
04:03:22 UTC and remains pending, with no result expected before 05:03:23 UTC.
Local combined regression passed 2249 tests with real libmpv enabled and 13
separate native opt-ins skipped in 303.89s. The added production Unix-IPC flow
also reads the actual station option and finite-load reset; it awaits CI here.

One extended CI checkpoint now requests 900 seconds of production Unix-IPC
stress on the same two standard public runners. The stress step is capped at
18 minutes and each native job at 30 minutes; the Python floor job stays at
8 minutes. Permissions, assertions and resource budgets are unchanged. After
recording its terminal result, restore normal 180-second stress, 6-minute step
and 15-minute native-job caps. This is not a permanent CI-duration increase.


### Extended production-IPC checkpoint

Checkpoint 4ef68e4 passed the explicit 900-second request on both native CI
architectures in [run 38203316362](https://github.com/de41tr/open-room-renderer/actions/runs/38203316362).
Actual elapsed time was 905.076s (x86_64) and 906.365s (AArch64), with 684/706 loads
and 1681/1736 protocol agreements across three separately owned mpv lifetimes.
Every lifetime passed listener/subscription/task/socket cleanup and child reap;
post-cleanup counts were 7 FDs, 1 task, 1 thread, 0 children. The production digest
was `f1cb53c37437134ec76c8eac37fed47c20e6a419f4cc0cd19b081a5f0882ee61`.
This result covers that exact snapshot, not later policy or shutdown
changes. The routine CI request is again 180s, with 6-minute stress and 15-minute
whole-native-job bounds; no resource or state assertion was relaxed.

`test_mdns_teardown.py` injects failure/cancellation during withdrawal and close.
The opt-in discovery lane additionally starts real explicit-loopback Zeroconf,
injects only unregister failure/cancellation, and verifies every owned socket
and selector reader is removed. No physical-LAN/app acceptance is implied.


## Changing live ICY metadata

`test_libmpv_live_metadata.py` is an explicit `ORR_TEST_LIBMPV_PATH` lane using
runtime-generated finite MP3 paced over one loopback HTTP response. Three native
StreamTitle changes occur with one renderer load and stable native playlist
entry, queue identity and advancing decoded audio. Autonomous GENA callbacks
are checked before client status requests can synchronize the renderer.
HTTP/SOAP/GENA and generated DIDL follow matching fallback titles; explicit
station titles, artist/album/artwork and opaque DIDL remain exact. A title-less
opaque DIDL is never rewritten to insert dynamic metadata. The renderer does
not split arbitrary ICY title text into artist/track fields or invent another
now-playing schema. This covers native libmpv and null output, not production
Unix IPC, remote stations, handset presentation or physical audio.

## Native station playlist-child history

mpv 0.40 resets a container's per-load options when it expands M3U/PLS. To extend
the corrected live-station policy to proven first-level children, the existing
readiness/state observer makes one bounded attempt per unchanged observed child.
It uses the same `LIVE_STATION_HISTORY_OPTIONS` as direct loads: a 1 MiB backward
history cap plus `demuxer-donate-buffer=no`. Zero history is not used, because it
can suppress timed ICY metadata. Donation is disabled so unused forward capacity
cannot silently enlarge the backward allocation; forward settings are unchanged.

The canonical zero-duration station, current generation, queue/item identity,
loaded source and complete first-level native parentage must agree under the
backend lock. The native process ID is read before fresh source proof. Both
file-local writes independently use an identical native process/active-entry
property-expansion guard. Donation is disabled first, then the history cap is
set. A transition between writes rejects the later write; partial application
is explicitly unconfirmed. A guarded readback must report both expected values
and both file-local flags. Integer-only guards never interpolate media URLs.

This begins after the child becomes observable, normally on the existing
half-second observer cadence, rather than a pre-demux hook. No additional writer
or monitor is created, and a failed unchanged child is not retried repeatedly.
The policy transaction is limited to two seconds. A fresh normal snapshot is
then required before publishing state; its existing IPC read latency is outside
that budget. Unsupported or failed proof/readback produces one unconfirmed
warning while otherwise valid playback continues. New admitted loads reset the
attempt budget; Pause/Resume alone does not. Native file-local restoration
handles EOF, Stop and finite replacement. Nested/ambiguous/foreign ancestry is
excluded from the optimization.

`test_station_playlist_cache_options.py` covers owner/source races, both exact
guards, partial application, full readback, bounded timeout/cancellation and
stale-observation rejection. `test_libmpv_station_playlist_cache.py` uses real
mpv with generated M3U/PLS: first and natural-next child readback, finite reset,
Pause/Resume, genuine replacement between the two writes, and different native
processes reusing the same path/entry ID. Its paced ICY child cases verify three
changing native titles and canonical HTTP/SOAP/GENA metadata without reloads.
The explicit Unix-IPC lane has corresponding child guard/reset cases. Native
libmpv tests alone do not certify Unix IPC, older runtime versions, real radio
providers or physical audio. The frozen zero-policy direct hour remains separate
historical evidence and does not validate this finite child policy.

Primary contracts: [mpv file-local options and conditional property expansion](https://mpv.io/manual/stable/),
[mpv 0.37 input API](https://github.com/mpv-player/mpv/blob/v0.37.0/DOCS/man/input.rst),
and [mpv 0.40 synchronous command implementation](https://github.com/mpv-player/mpv/blob/v0.40.0/player/command.c).


## Reproducible uninterrupted station endurance

`test_libmpv_sustained_station.py` and `sustained_station_support.py` freeze and
hash the exact tested sources. The explicit opt-in generates bounded synthetic
AAC/ADTS with official FFmpeg, then keeps one loopback source and one native
handle alive without reloads, source changes or seeks. The regular suite runs
only small parser/observation/child-sensitivity checks; it does not start a soak.

```sh
ORR_TEST_LIBMPV_SUSTAINED=1 ORR_TEST_LIBMPV_SUSTAINED_SECONDS=3600 \
ORR_TEST_LIBMPV_PATH=/path/to/libmpv.so.2 PYTHONPATH=. .venv/bin/python -m pytest -q -s \
  tests/test_libmpv_sustained_station.py
```

Without the duration override the opted-in smoke baseline is 10s. Valid requested
baselines are 5..3600s after warmup, bounded by a process-group supervisor deadline
of baseline+90s. Missing explicit prerequisites fail rather than silently skip.
Use a private `--basetemp` outside the repository to retain generated diagnostics;
do not publish raw audio/captures. Only aggregate results belong in reports.

Checks include advancing actual decoder time, exact serialized observation
mapping and separate monitor freshness, HTTP/SOAP/GENA agreement, lease renewal,
no recovery/reload, unchanged volume/mute, bounded RSS/FDs/tasks/threads/children,
and final native/session/listener/subscription/task cleanup. The fixture never
changes production cache policy. Its 10s smoke passed after replay, but an hour
result must be tied to its own production/source hashes and completion record.
The historical zero-cache hour does not test the corrected finite policy or
changing ICY metadata. This is REAL_BACKEND/null, not production Unix IPC,
physical audio, WAN reliability or original-app acceptance.


The separately frozen final finite-policy constant-title baseline passed
3600.675s with one load/request, 362 protocol agreements and 1936 KiB peak RSS
growth, followed by verified cleanup. Source digest:
`ff5f255e08cc54255cff69621c7633da1c604109208d07b389201fbceb1a2735`;
production digest:
`b3ae8a5c7ad01696b12be6feb52c16fe51fed9ad6128015992b6d14fd055b5ef`.
Its original constant-title harness is distinct from the later ICY extension;
these hashes, rather than later branch heads, define the accepted snapshot.

### Uninterrupted changing-ICY endurance

The same sustained fixture also supports an explicitly opted-in changing-ICY
mode. FFmpeg generates a bounded 64 kb/s AAC seed at runtime; complete ADTS
frames are paced continuously and ICY blocks are inserted at exact 1024-byte
audio boundaries. No external station or custom player is involved.

```bash
ORR_TEST_LIBMPV_SUSTAINED=1 ORR_TEST_LIBMPV_SUSTAINED_ICY=1 \
ORR_TEST_LIBMPV_SUSTAINED_SECONDS=240 \
ORR_TEST_LIBMPV_PATH=/path/to/libmpv.so.2 PYTHONPATH=. .venv/bin/python -m pytest -q -s \
  tests/test_libmpv_sustained_station.py --basetemp=/tmp/orr-changing-icy
```

The default is 240 seconds; local requested baselines may be 20..14400 seconds.
A new unique synthetic title is requested roughly every eight seconds. Each
must reach native/renderer state and fresh acknowledged AVTransport/PlayQueue
callbacks within six seconds, before client status/SOAP reads can synchronize
state. Every title is then checked across HTTP, SOAP, generated DIDL, queue XML
and GENA. One pending title, aggregate counters/hash and bounded 32-entry event
histories prevent the evidence collector from growing or concealing a miss.

The original observation-versus-integer-position model, canonical queue and
native playlist-entry identity, one handle/load/source/HTTP connection,
recovery-free decoded progress, resource budgets, lease renewal and complete
cleanup checks apply. Native total/forward cache bytes are capped separately;
a >=240-second run also requires four one-minute epochs and a retained-history
plateau across all samples from 180 seconds onward (at least fifty late
samples). Shorter smoke runs report no verified plateau.
The source is frozen and hashed; the unchanged process-group supervisor caps
native execution at requested duration+90 seconds including bounded cleanup.
This lane is local-only and never automatically schedules an hour in routine CI.

Evidence scope remains REAL_BACKEND/null with loopback HTTP; it establishes
neither production Unix IPC nor physical audio, WAN reliability, device/app
compatibility, or indefinite leak freedom.


The Snapcast failure diagnostic retains all four channel summaries for valid
captures before any signal-gate assertion. It freezes both PCM file endpoints
before analysis and emits bounded fitted-window statistics and fixed log-marker
counts, never raw PCM or log lines. Signal thresholds are unchanged; malformed
or short captures can still fail before all channel summaries exist. Synthetic
phase jumps reproduce the observed 5/7 failure shape without proving the ARM
cause. The historical failed job remains failed, and the next native run is
needed to establish current delivery behavior.


### Emulated ARMv7 scope

A bounded direct-[QEMU user-mode](https://www.qemu.org/docs/master/user/main.html)
probe at ba7b3120 executed Debian ARM hard-float Python 3.11.2 and actual ARM C
extensions, including aiohttp request/response parsers. Imports, persistent UUID,
XML and actual loopback HTTP/SOAP error status passed with MOCK/unavailable audio.
Debian signed package metadata and exact official-PyPI wheel hashes were checked.
This is INTEGRATION/EMULATED_ARMV7, not native ARM hardware or installation.

Dependencies were extracted into private import paths. The unprivileged PRoot
route failed at ptrace, and direct QEMU's ordinary ARM child exec failed; no
security workaround or subprocess shim was introduced. Thus the unmodified
installer's venv/pip/entry-point/receipt/uninstall chain remains NOT_RUN on ARMv7.
The successful native AArch64 installation lane does not fill that separate gap.


## Rolling live HLS and recovery

`test_libmpv_live_hls.py` uses the existing `ORR_TEST_LIBMPV_PATH` opt-in and
standard FFmpeg/libmpv, with a bounded generated AAC/MPEG-TS seed. An HTTP
publisher exposes only clock-ready entries in an advancing five-segment playlist,
without ENDLIST, encryption or alternate variants. It does not implement a player.

Five cases cover a playlist 503 outage, one failed renderer reload and return;
a real pending segment HTTP response whose body is held, then delivered without
EOF while retaining the same native entry; and owner Pause, Stop or replacement during observed zero-attempt
recovery backoff. Owner commands must cancel that retry and prevent stale HLS
fetch/reload after availability returns. The 15s production stall grace and
0.5/1/2s retries are not shortened. The transient segment case returns before that
grace; it is not a separate full-duration HLS stall-exhaustion claim.

Unsolicited GENA is checked before status reads. Canonical live URI, duration 0,
queue, native progress, HTTP/SOAP/GENA, volume/mute readback and complete cleanup
must agree. Gain is read back, not acoustically measured. Local five-case runtime
was 76.65s for the original fixture; the normal hosted lane includes five cases
without raising the 15-minute
job cap. Each subprocess is independently bounded. Only aggregate synthetic
request observations are printed. Scope is REAL_BACKEND/libmpv/null, not HLS
through production Unix IPC, real WAN, original app, DRM or physical audio.


At 3e74b24, actual Ubuntu mpv0.37/FFmpeg6.1.1 revealed that repeated segment
503s can end the HLS demuxer about 1.5s after its initial cache stall. Native
END_FILE/EOF, idle and disappearance of path/time proved teardown, not just a
missing clock. The no-EOF fixture therefore now holds an actual pending HTTP
body, with open-transport, no-EOF, stationary-time and delivery checks. Playlist
503/reload/owner cases remain unchanged; no missing timestamp is tolerated.

The same hosted run exposed a native child-readiness race: path/current/playing
can appear before the demuxer opens and playback-time exists. A public on_load
hook reproduced the original failure on mpv0.37/0.40 with both M3U and PLS. The
bounded test helper now waits for a nonnegative clock while pinning exact native
child identity and renderer ownership, then still requires >0.25s actual decoded
progress. Only exact property-unavailable and finite negative preroll are retried;
invalid values, changed ownership, other errors or a stalled clock fail. Actual
invalid-child and Stop-during-readiness negative controls still fail. Production
code, history policy, timeouts, pause/default-reset and stale guards are unchanged.


The exact integrated correction passed 2363 default tests /62 opt-in skips.
Private official Ubuntu mpv0.37 plus FFmpeg6.1.1 generation/decoding passed all
five HLS cases, six child-cache cases and 27 deterministic readiness guards in
112.46s. mpv0.40 plus FFmpeg7.1.5 passed the eleven native cases in 111.15s.
These are actual x86_64 library replays with compatible host dependencies,
not a full Ubuntu installation or a substitute for the native hosted gates.


## Minimum mpv version is a playback contract

An actual private official Debian mpv0.35.1/FFmpeg5.1 runtime can decode direct
sources and perform gain/DSP controls, but lacks both playlist ancestry fields.
Real HTTP M3U/PLS playback therefore returns 502 rather than falsely confirming
the selected container. Upstream's 0.37 interface changes identify when the
required properties were added. Installer boundary tests reject 0.35/0.36 and
accept 0.37; actual 0.35 rejection also occurs before creating the install prefix.
README and Getting Started now state >=0.37 and the Bookworm package limitation.
This audit uses privately extracted official packages, not a native ARMv7 or
full Bookworm installation, and does not relax the renderer's ownership proof.

## Queue navigation preserves stopped and paused intent

AVTransport `Next` and `Previous` now select a valid neighboring item without
starting playback when stopped. The selection, metadata and position update;
only an explicit `Play`/HTTP `resume` or `PlayQueueWithIndex` starts it. Because
backend loading currently unpauses, multi-item navigation while paused returns
SOAP 701 (HTTP 400) before mutation. Loaded finite single-item `Previous` retains
its existing pause-preserving restart behavior. Navigation does not clear a
backend error or uncertain native ownership; explicit Stop/Play recovery remains
required. A pending Stop retains silent-selection intent, and a paused seek
waiting for readback retains paused intent despite its temporary transition
phase. Play-intent buffering can still navigate. The shared legacy HTTP commands
use this same documented policy.

The normative reference is the [UPnP AVTransport:1 specification](https://upnp.org/specs/av/UPnP-av-AVTransport-v1-Service.pdf),
sections 2.4.13.2–3 and 2.4.14.2–3, printed pages 31–32: successful navigation
restores the prior transport state, and paused navigation may reject 701. This
is a bounded navigation correction, not a claim of full UPnP conformance or
proprietary-device behavior.

`test_queue_navigation_intent.py` covers silent selection, paused rejection,
error/uncertainty preservation, native EOF, cancelled station retry, and newer
owners during checked Stop. `test_local_client_navigation_intent.py` exercises
real pywiim with a loopback renderer and deterministic mock audio. The optional
`test_libmpv_queue_navigation_intent.py` uses generated local WAVs, null output,
real HTTP/SOAP/GENA and command traces to confirm no load/unpause during silent
selection, plus positive playback and lost-Stop-ack controls. It passed with
libmpv 0.37.0 and 0.40.0, each with native staging off and on. Native adapters
replace only command transport; this does not establish production Unix IPC,
physical audio, hardware, proprietary apps, or network-media interoperability.


## Current transport actions and runtime compatibility

SOAP GetCurrentTransportActions and all AVTransport LastChange aliases now share
one live calculation from canonical state, owner intent and the last confirmed
backend identity. Static SCPDs still list implemented operations. Explicit station
Seek, invalid neighbors, unloaded paused Seek and inconsistent seek intent are
not offered. Unknown-duration finite sources remain seekable. Paused multi-item
navigation is absent, including while a paused seek is awaiting confirmation;
play-intent loading/buffering can still skip a track and pending Stop can select
silently. Empty Pause rejects before changing generation or issuing a command.
This is not a full UPnP or fresh external-native-ownership certification.

The Python3.13 runtime check used official-PyPI dependencies in a private pip-less
venv. Its initial full suite had twelve fixture failures because optional host
ensurepip was absent; isolating that unit-test prerequisite preserves explicit
missing-venv and missing-ensurepip negatives. The real installer still rejects
the environment before creating a prefix. No system package or production
preflight was bypassed, and native installation is not claimed by this run.

The exact combined navigation/action snapshot passed 2534 default tests /64 skips
on Python3.12 and 3.13; Python3.11 focused checks passed 315. Five targeted actual
libmpv control/navigation/uncertainty cases passed on each of 0.37 and 0.40 in
19.57s, with default-cadence navigation and complete cleanup. New hosted results
remain separate; normal native job and stress limits are unchanged.


## Post-ready source ownership and automatic admission

A coherent active current-generation path, unique current/playing entry and
matching parentage can establish that the confirmed source was replaced. Missing
or torn properties, old generations and transient EOF/idle cannot. The canonical
selection/metadata/intent and last native evidence remain; ERROR explicitly says
silence is unconfirmed and gain is no longer certified. Notifications alone are
scheduled: no automatic Stop/load/seek/gain correction touches the foreign source.
Unloaded STOPPED/PAUSED selections cannot import unrelated media fields.

Station retry wake and locked post-resolution admission use the same source
check. Queued EOF advancement rechecks native state before selecting/loading;
new coherent active audio blocks the automatic step, even at a repeated URL.
An intervening terminal ERROR also vetoes the step before and across resolver
or snapshot waits. Existing checked explicit Stop/Play recovery and genuine
idle/EOF advancement remain. Independent negative controls reproduced both
foreign takeover and ERROR revival before these fences were added.

`test_source_ownership_observations.py` covers these state/generation/cancellation
boundaries. `test_libmpv_source_ownership.py` uses actual native replacement and
the unchanged 0.5s observer, capturing scheduled error content before status reads
while the foreign decoder continues unchanged. That native case does not claim
network callback delivery, production Unix IPC or hardware. An external
replacement during an already-admitted load/readiness interval remains excluded;
separate causal entry/load evidence is needed before changing cleanup there.

The final integrated snapshot passed 2602 default tests /65 skips on Python3.12
and 3.13, 435 focused Python3.11 tests, 17 real-backend 0.37 cases including recovery
and stalls, and six 0.40 navigation/ownership/staging/uncertainty cases. Normal
native CI limits are unchanged. Frozen long-radio evidence belongs to its own
recorded production/source hashes rather than these later revisions.


### Pending load observations and ambiguous EQ input

`test_admitted_load_observer_boundary.py` separates admitted readiness/cleanup
from post-ready source ownership, including same-generation station retries,
backend errors and stale marker cleanup. The existing optional
`test_libmpv_load_cleanup.py` is a required regression check for actual decoded
nested descendants that cannot establish outer ancestry: failure must confirm
native silence before returning. The earlier 5d64bfb failure remains recorded.

`test_eq_duplicate_fields.py` rejects duplicate JSON fields at every nested
object level for EQ commands and persisted libraries. It verifies unchanged
backend/state/files and valid explicit library reload; the separate active-DSP
and gain parsers retain their existing contracts. These parser checks are
UNIT/MOCK, not native/audio measurements.

`test_admitted_load_terminal_error.py` separately proves that a previous terminal
backend error survives both readiness outcomes and an error observation during
owned Stop cleanup. Owned cleanup still runs; a failed cleanup must continue
to expose unconfirmed silence. This does not prove external-root causality.


### Acknowledged root identity and coherent Stop observations

`test_admitted_load_root_identity.py` validates the pending load ACK token,
invalid/missing ACKs, torn/old samples, gain confirmation and owned cleanup.
`test_libmpv_admitted_load_root.py` exercises eight same/different-root
interleavings using actual 0.37/0.40 decoding, unchanged 0.5s background observation,
untouched foreign progress/gain and explicit recovery. Its owned merge-files EDL
counterexample requires a warmup/Stop before M3U expansion for deterministic native
preparation. EDL roots remain ambiguous, not newly supported playback.

This proof applies after HttpApi receives the complete load wrapper ACK. It does
not provide atomic protection against raw external writers during unpause or
between final snapshot and Stop, nor attribution for missing ACKs, EDL/container
roots, range reloads, arbitrary native scripts or restarted cores with reused IDs.
Native child parentage is populated before playlist transfer in the public
[0.37 load path](https://raw.githubusercontent.com/mpv-player/mpv/v0.37.0/player/loadfile.c)
and [0.40 load path](https://raw.githubusercontent.com/mpv-player/mpv/v0.40.0/player/loadfile.c).
The native merge transformation is distinct: see
[0.37 merge implementation](https://raw.githubusercontent.com/mpv-player/mpv/v0.37.0/player/misc.c)
and [0.40 merge implementation](https://raw.githubusercontent.com/mpv-player/mpv/v0.40.0/player/misc.c).

The existing nested-cleanup test now samples a coherent stopped result before
its same absolute 4.5s command deadline. It still requires actual decoded audio
before cleanup, a fresh idle/empty-path/empty-playlist sample, retained error,
subsequent explicit playback and full teardown. Twelve deterministic helper
checks are UNIT; the actual libmpv case is REAL_BACKEND/null. The selected native
lane contains 47 opt-in native cases and 13 deterministic checks, not 60 hardware
tests. The former active-audio regression remains red with this helper.


### Pure metadata-edit extraction

`test_metadata_formatting.py` characterizes the existing three edit helpers
through both preserved HttpApi wrappers and the new pure module. Exact strings
cover Unicode, CR/LF/tab, quotes, opaque/encoded resources, first-match behavior,
malformed tags, backslash replacement errors and existing duration quirks.
The old bodies move without algorithm changes; DIDL construction/escaping and
all stateful code remain untouched. Existing metadata/status/GENA and full
native/Unix/installed-client regressions still gate the refactor. No additional
provider, original-app, hardware or performance claim follows from extraction.

## Deferred XU4 / DAC / original-app acceptance

**HARDWARE_ACCEPTANCE_PENDING.** The XU4 and original app are currently unavailable;
there is no known test date. Do not block software work or reinterpret hosted ARM64,
limited ARMv7 emulation, null decode or digital PCM as this acceptance.

When the owner makes a device available, use this bounded first session (about
45–60 minutes). Record the exact reviewed commit, OS/architecture, Python/mpv
versions, DAC model and selected ALSA device, controller/app version and profile.
Keep real identifiers, addresses, app screenshots and any captures private; publish
only sanitized aggregate results. Stop at a failed prerequisite rather than
changing firewall, network/security settings or installing privileged services
without the required approval.

1. **Install / identity (10 min):** follow the isolated native installer in
   GETTING_STARTED.md as a non-root account on the actual ARMv7 XU4. Verify its
   installed CLI and production Unix IPC, restart twice using the same state
   directory, and confirm identity/name persistence. Do not delete state to
   resolve a discovery problem. Record receipt-owned uninstall separately.
2. **ALSA / DAC (10 min):** select the intended device explicitly after checking
   mpv's device listing. Start with low volume and synthetic/user-owned files.
   Verify two clearly distinct WAVs, then MP3/FLAC, actual audible output,
   pause/resume, volume, exact commanded mute and Stop. Record observed clicks,
   underruns or channel errors; digital-null evidence cannot clear these checks.
3. **LAN / controller (10 min):** test the neutral profile first from a separate
   LAN client, discover the advertised instance, and run a two-track queue through
   Play → Pause → Resume → Next → volume/mute → Stop. Check real second-source
   identity and HTTP/SOAP/received GENA agreement, not merely a playing label.
4. **Original app (10–15 min):** use the explicit compatibility profile and its
   advertised address. Record discovery, device details, queue/metadata/control
   behavior and unsupported-service visibility. Test port80 only through the
   separately reviewed non-root service option when approved; do not run the
   renderer as root or change global interpreter capabilities. An app failure
   remains ANDROID_APP failed/pending even when public-client tests pass.
5. **Recovery / cleanup (5–10 min):** after basic audio passes, stop and restart
   the owned renderer/backend, verify silence and no orphan process, then explicit
   new playback. Treat DAC unplug, system boot and two-host synchronization as
   separate later cases, not implied successes of this short session.

Record each step as PASS / FAIL / NOT_RUN with its evidence scope. A completed
session may establish individual HARDWARE / REAL_AUDIO / ANDROID_APP results;
it is not a blanket WiiM replacement or long-term reliability certification.


### Four-hour changing-ICY qualified evidence (2026-10-12)

The existing opt-in sustained test completed a 14,402.516s baseline and cleanup
with its original thresholds: 1,795/1,795 autonomous titles plus matching raw
HTTP/SOAP/received GENA, 1,348 broader protocol agreements, one native load/source/
request and no disconnects. History remained 1,047,520–1,048,560 bytes; live RSS
peak growth was 4,428 KiB. Cleanup returned task/FD/thread counts to 1/7/1 with
no children or subscriptions; retained RSS +42,512 KiB stayed below the unchanged
49,152 KiB pre-start budget. Ten pytest checks passed in 14,408.70s; only one is
the real sustained case. Full frozen source and production digests are in
STATUS.md. This was libmpv0.40, loopback AAC/ADTS and null output; its older frozen
code cannot certify later commits, hardware, WAN or Unix IPC endurance. Do not
launch another multihour run without a concrete new information need.

## Repeat One / All acceptance

The bounded native flow is opt-in:

```sh
ORR_TEST_LIBMPV_PATH=/path/to/libmpv.so.2 PYTHONPATH=. \
  python -m pytest -q -s tests/test_libmpv_repeat_queue.py
```

It uses three distinct generated two-second WAVs and the production 0.5-second
observer cadence. Real native identity, selected audio, codec/sample parameters,
null output and increasing playback time prove A→B→C→Stop, RepeatAll C→A, and
RepeatOne B→B with a new native ID/load. HTTP Next escapes RepeatOne; explicit
boundary Next/Previous do not wrap. Mode changes preserve the active native
lifetime/generation, and confirmed Stop remains idle with no reload beyond the
media duration plus an observer tick. Raw HTTP, SOAP settings/position/queue and
received GENA agree. The ctypes command transport is test-local, not Unix IPC;
existing hosted Unix gates remain separate. Clean replay passed on libmpv 0.37 in
15.55s and 0.40 in 15.53s, each with 43 received events and full cleanup. No hardware,
audible-output, gapless-repeat or source-error-reason claim is made.

Pure/controller tests cover strict adapters, empty/single queues, mode changes
around awaited EOF preflight, owner/error priority, finite station retry budgets
and normal-only native-staging fences. Actual pywiim writes and raw server reads
were independently checked; its optimistic mode cache alone is not acceptance.
The public numeric compatibility correction and station/end/staging limits are
specified in COMPATIBILITY_MATRIX.md.

The first integrated native replay exposed an older fixture race on 0.37:
`test_libmpv_formats` accepted any selected audio after load acknowledgement and
then asserted its path, although native properties could still describe the
previous playlist child. This happened before renderer HTTP playback. The helper
was byte-identical to the earlier green checkpoints; isolated replay passed.
Both versions independently showed PLS acknowledgement still exposing the previous
MP3 path before the actual AAC child became ready. The test-only correction waits
for path-bracketed expected source, selected codec, null output and finite position,
then requires actual same-source progress. Existing readiness/progress deadlines
remain 3s each. Fifteen fast-clock regression checks retain wrong-source/no-progress
failures; all four native format scenarios passed on both 0.37/0.40. The failed
initial integrated run remains recorded; it is not relabeled as a pass.
