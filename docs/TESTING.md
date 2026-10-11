# Test evidence and limits

## Automated suite

```sh
.venv/bin/python -m pytest -q
```

Current suite: 218 passed on Python 3.12.14. Baseline: 92 passed.
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
