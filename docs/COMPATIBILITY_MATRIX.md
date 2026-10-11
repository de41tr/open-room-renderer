# Compatibility matrix

This is scoped evidence, not a product certification. Updated 2026-10-11 UTC.

| Area | Evidence | Remaining limit |
| --- | --- | --- |
| SOAP action validation/faults | UNIT/MOCK regression coverage | Actual client/app execution pending |
| Basic volume/mute/control acknowledgement | UNIT/MOCK + real local PyWiiM HTTP integration with fake backend | Real Unix IPC and DAC readback pending |
| Selected-URI playback/control | MOCK + real libmpv/null through HTTP | Unix IPC and real audio pending |
| Playback generations/EOF | UNIT/MOCK, generation-race and observer tests | Actual production IPC and hardware pending |
| Concrete multi-item queue | UNIT/MOCK + REAL_BACKEND/null indexed/Next/Previous/EOF selection | Legacy unresolved providers need AP06 |
| Next/Previous; normal loop mode | Actual queue selection; normal mode only | Repeat/shuffle remain unsupported |
| Discovery/identity and app-shaped handshake | UNIT/MOCK discovery + actual local HTTP handshake with fake backend; truthful typed SCPDs | LAN multicast and actual Android app pending |
| GENA lifecycle and callback security | Existing implementation | AP04 review/tests pending |
| Synthetic WAV/MP3/FLAC decode | REAL_BACKEND, mpv null output | No audible/DAC assertion |
| Linux ARMv7 / Odroid XU4 | Architectural target | HARDWARE not run |
| Spotify/TIDAL/AirPlay/Cast/multiroom | Unsupported/deferred | No cloud login or provisioning performed |

Neutral and compatibility profiles share independent product/version metadata.
The compatibility profile changes protocol presentation and discovery identity;
it does not claim to be original hardware. Existing IDs survive upgrades and
renames. New compatibility IDs are random and persistent. Music-provider entries
are configuration-driven; unsupported diagnostic sentinels remain inactive.
