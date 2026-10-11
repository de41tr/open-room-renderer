# Test evidence and limits

## Automated suite

```sh
.venv/bin/python -m pytest -q
```

Current default suite: 371 passed and 1 explicit REAL_BACKEND skip on Python 3.12.14.
With the optional libmpv lane enabled: 372 passed. Baseline: 92 passed.
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
Unix JSON IPC, GENA callback delivery, acoustic mute/gain, DAC, Android or ARM.
