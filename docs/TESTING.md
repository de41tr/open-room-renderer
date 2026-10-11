# Test evidence and limits

## Automated suite

```sh
.venv/bin/python -m pytest -q
```

AP04 checkpoint default suite: 739 passed and 1 explicit REAL_BACKEND skip on Python 3.12.14.
With the optional libmpv lane enabled: 740 passed. Baseline: 92 passed.
AP01 alone: 133 passed; the acceptance-state check adds 85 UNIT/MOCK cases.
`tests/test_soap_faults.py` adds 41 UNIT/MOCK checks for SOAP framing,
unsupported actions, argument validation, backend rejection, sanitized faults,
namespace correctness, client extensions and exactly-once loading.

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
is NOT_RUN here. Multicast SSDP validation is also unavailable. Do not replace
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
