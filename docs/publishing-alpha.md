# Publishing The Alpha

This guide is for maintainers preparing the first public GitHub alpha.

Open Room Renderer is a friendly, experimental local audio project. Keep the release honest: it works, it is exciting, and it is still early.

## Recommended GitHub Description

> Independent experimental Linux/mpv local network audio renderer for hobbyist audio setups.

## Repository Topics

Suggested topics:

- `linux-audio`
- `mpv`
- `raspberry-pi`
- `local-first`
- `upnp`
- `ssdp`
- `audio-renderer`
- `python`
- `home-audio`

## Before You Push

1. Confirm `LICENSE` is present and `pyproject.toml` declares `GPL-3.0-or-later`.
2. Confirm the release folder does not include:
   - logs,
   - local state,
   - packet captures,
   - private notes,
   - tokens,
   - cookies,
   - credentials,
   - real identifiers,
   - proprietary app or firmware material.
3. Run:

   ```bash
   PYTHONPATH=. pytest -q
   ./scripts/acceptance-open-source-client.sh
   ```

4. Create a first GitHub release as an alpha, not a stable release.

## First Release Notes Draft

```markdown
# Open Room Renderer 0.1.0-alpha.1

This is the first public alpha of Open Room Renderer, an independent experimental Linux/mpv-based local network audio renderer for hobbyist audio setups.

What works today:

- local discovery,
- local renderer control endpoints,
- open-source-client acceptance test,
- TuneIn-style playback flow,
- direct DRM-free URL playback,
- mpv backend,
- volume/mute/play/pause/stop,
- channel balance,
- 10-band PEQ.

Known limitations:

- alpha quality,
- proprietary app compatibility may break,
- no DRM or protected-service support,
- no real multiroom sync,
- SOAP/eventing is pragmatic and incomplete,
- Raspberry Pi packaging still needs polish.

Please read the README, CONTRIBUTING, SECURITY, and clean-room interoperability docs before contributing.
```

## Maintainer Tone

Be welcoming but clear:

- This is a hobbyist/developer alpha.
- Bug reports need sanitized logs.
- Compatibility may break.
- Contributions should be small and testable.
- No proprietary material or bypass logic is accepted.
