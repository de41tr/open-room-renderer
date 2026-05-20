# Release Notes

## 0.1.0

Initial local prototype release.

### Verified

- Distro-neutral setup script that checks dependencies and prints missing tools.
- UPnP/SSDP renderer discovery for open-source clients.
- Compatibility-style `/httpapi.asp` status and transport commands.
- mpv playback through JSON IPC.
- pywiim acceptance script for status, play, pause, resume, and stop.
- WiiM Home-style experimental profile can expose the renderer in local testing and has been used for TuneIn, Open Network Stream, PEQ, balance, rename, and Plex-login experiments. Plex is not surfaced in the alpha profile.

### Known Boundaries

- This is not a production firmware image.
- Proprietary app behavior remains experimental and version-dependent.
- Plex was started as a user-owned-library experiment, then disabled/not surfaced for this alpha because login/server/library behavior is not reliable enough yet.
- No DRM, Spotify Connect, Deezer/TIDAL credential flow, or real multiroom sync.
- SOAP/eventing is pragmatic and incomplete.

### License

GNU GPLv3 or later (`GPL-3.0-or-later`).

### Required Gate

`./scripts/acceptance-open-source-client.sh` remains the definition of done for the portable prototype. Curl-only success is not enough.
