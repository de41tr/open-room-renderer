# Roadmap

Open Room Renderer is alpha software. This roadmap favors small, reviewable tasks that make the project easier to run and improve.

## 0.1 Alpha Hardening

- Keep README, architecture docs, and developer map current.
- Ensure tests pass on a clean checkout.
- Keep logs sanitized by default.
- Document known private working directories and ignored files.
- Add clearer config validation errors.
- Add more synthetic tests around queue state transitions.

## 0.2 Raspberry Pi Usability

- Document Raspberry Pi OS dependencies.
- Add audio-device selection troubleshooting.
- Add a conservative user-level systemd guide.
- Document firewall and multicast discovery checks.
- Add setup notes for ALSA, PulseAudio, and PipeWire.
- Add a Pi-focused quickstart once tested.

## 0.3 Playback Robustness

- Improve mpv missing/failed startup diagnostics.
- Add mpv version detection.
- Improve seek behavior documentation and tests.
- Add more tests for stream metadata and duration handling.
- Make transient loading/transition states easier to reason about.

## 0.4 Config And Observability

- Add explicit config validation.
- Add a command to print effective config with sensitive values redacted.
- Add sanitized example logs for common failure modes.
- Make debug logging modes clearer.
- Add troubleshooting docs for discovery and audio output.

## 0.5 Optional Integrations

- Explore Home Assistant documentation.
- Add packaging examples.
- Add optional Dockerfile or document why Docker is not a good fit for audio.
- Keep any third-party service work behind legitimate user-provided URLs or documented APIs.

## Good First Issues

1. Improve Debian/Raspberry Pi OS install docs.
2. Add mpv version detection.
3. Add config validation tests.
4. Add more EQ mapping tests.
5. Add more balance mapping tests.
6. Add sanitized log examples for common failures.
7. Add a user-level systemd unit walkthrough.
8. Add Dockerfile notes or document why Docker is awkward for local audio.
9. Improve the error message when mpv is missing.
10. Add troubleshooting entries for firewall and discovery problems.
