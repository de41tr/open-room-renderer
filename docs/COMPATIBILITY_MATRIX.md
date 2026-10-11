# Compatibility matrix

This is scoped evidence, not a product certification. Updated 2026-10-11 UTC.

| Area | Evidence | Remaining limit |
| --- | --- | --- |
| SOAP action validation/faults | UNIT/MOCK regression coverage | Actual client/app execution pending |
| Basic volume/mute/control acknowledgement | UNIT/MOCK | Real Unix IPC and DAC readback pending |
| Successful selected-URI playback | MOCK, exactly one load | Real audio pending |
| Playback generations/EOF | Baseline behavior only | AP02 in progress next |
| Multi-item queue | Not implemented | AP05 planned |
| Loop/Next SOAP mutations | Explicit unsupported fault | Actual semantics planned with queue |
| Discovery and app handshake | Existing static/unit coverage | LAN multicast and Android app pending |
| GENA lifecycle and callback security | Existing implementation | AP04 review/tests pending |
| Synthetic WAV/MP3/FLAC decode | REAL_BACKEND, mpv null output | No audible/DAC assertion |
| Linux ARMv7 / Odroid XU4 | Architectural target | HARDWARE not run |
| Spotify/TIDAL/AirPlay/Cast/multiroom | Unsupported/deferred | No cloud login or provisioning performed |
