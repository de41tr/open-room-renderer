# Release Manifest

This folder is a curated alpha-release candidate for Open Room Renderer.

It is intentionally smaller than the working prototype checkout. Local research artifacts, packet captures, private state, virtual environments, logs, and experimental reverse-engineering helpers are not included.

## Included

- `fake_linkplay/`: the Python renderer package.
- `configs/config.example.yaml`: safe default YAML configuration.
- `configs/config.app-compat.yaml`: optional phone-app discovery profile with mDNS enabled.
- `examples/`: safe TOML config, user-level systemd example, sanitized log example.
- `scripts/`: minimal setup, run, curl diagnostics, SSDP/mDNS sniffing, and open-source-client acceptance scripts.
- `tools/`: open-source-client acceptance helpers only.
- `tests/`: synthetic pytest suite.
- `docs/`: contributor, architecture, clean-room, demo, release, and safety documentation.
- `.github/`: issue and pull request templates.
- `README.md`, `LICENSE`, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `ROADMAP.md`.

## Excluded On Purpose

- `.venv/`
- `.state/`
- `logs/`
- raw packet captures
- local bus/session files
- local notes
- generated `*.egg-info`
- `__pycache__`
- app-capture analyzers and fuzzers
- standalone Plex token login/diagnostic tools
- proprietary assets, screenshots, APK/IPA/firmware fragments, copied code, keys, certificates, tokens, cookies, credentials, real MAC addresses, and real UUIDs

Note: the renderer source still contains disabled Plex compatibility scaffolding from local experiments. It is not surfaced by the release app-facing profile and is documented as experimental.

## Release Status

Alpha. Experimental. Useful for developers, hobbyists, and local audio hackers, but not a polished streamer product.

This release candidate is licensed under GNU GPLv3 or later (`GPL-3.0-or-later`).

## Validation Commands

From this folder:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
PYTHONPATH=. pytest -q
./scripts/acceptance-open-source-client.sh
```

The acceptance script starts the renderer if needed, checks local endpoints, checks SSDP, and uses an open-source client-level path for status/play/pause/resume/stop.

## Publishing Checklist

- Confirm `LICENSE` and `pyproject.toml` still declare `GPL-3.0-or-later`.
- Confirm `.state/`, `logs/`, raw captures, and private notes are absent.
- Run the tests and acceptance script from a clean checkout.
- Confirm README and disclaimer are still accurate.
- Tag as an alpha release, for example `v0.1.0-alpha.1`.
