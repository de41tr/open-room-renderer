# Contributing

Thanks for helping improve Open Room Renderer. This is an early alpha interoperability project, so small, careful changes are much easier to review than broad rewrites.

## Local Setup

Install the system dependencies with your distro's package manager:

```text
python3, Python venv support, mpv, curl, jq, ip/iproute2
optional: tcpdump
```

Then run:

```bash
./scripts/dev-setup.sh
```

Run the service:

```bash
./scripts/run.sh
```

Run tests:

```bash
PYTHONPATH=. .venv/bin/pytest -q
```

Run the open-source-client acceptance gate:

```bash
./scripts/acceptance-open-source-client.sh
```

## Formatting And Linting

No dedicated formatter or linter is configured yet. For now:

- Follow the existing Python style.
- Use type hints for new code.
- Keep functions small enough to test.
- Prefer structured logging over ad hoc prints.
- Run `PYTHONPATH=. .venv/bin/pytest -q` before submitting.

Adding a lightweight formatter/linter configuration is a welcome future contribution.

## Pull Requests

- Keep PRs small and reviewable.
- Explain what behavior changed and how you tested it.
- Add or update tests for behavior changes.
- Update README/docs when commands, config, or user-visible behavior change.
- Prefer explicit config flags for uncertain compatibility experiments.

Good contribution areas:

- Documentation and troubleshooting.
- Synthetic tests and fixtures.
- mpv backend robustness.
- Config validation.
- Raspberry Pi setup and packaging.
- User-level systemd service examples.
- Better error messages.
- Sanitized log examples.

## No Proprietary Material

Do not submit proprietary source code, decompiled code, firmware fragments, app assets, screenshots, logos, icons, keys, certificates, tokens, cookies, real device identifiers, real MAC addresses, serials, credentials, or raw protocol dumps.

## No Bypass Or Circumvention

Do not submit code, documentation, tests, or instructions that bypass authentication, pairing, signatures, DRM, encryption, certificates, access controls, or other technical protection measures.

## Tests And Fixtures

- Tests must be synthetic.
- Tests must not require real proprietary hardware, proprietary apps, user credentials, private networks, or cloud-service accounts.
- Use documentation-only hosts such as `example.test` or `192.0.2.0/24`.
- Use placeholder UUIDs such as `00000000-0000-4000-8000-000000000000`.
- Use synthetic MAC addresses such as `02:00:00:00:00:00`.

## Safe Bug Reports

Do not attach raw captures or verbose app traces to public issues.

Useful safe bug reports include:

- OS/distro and Python version.
- Hardware type, for example Raspberry Pi model or laptop.
- Audio stack: ALSA, PulseAudio, or PipeWire.
- mpv version.
- Install method.
- Client/app version if you choose to provide it.
- What worked before.
- Expected behavior and actual behavior.
- Sanitized logs with tokens, MAC addresses, serial numbers, UUIDs, private IPs, cookies, keys, and certificates removed.

## Maintainer Expectations

Use a respectful tone. Compatibility work can be fragile and sometimes breaks for reasons outside this project. Maintainers may close, edit, or redact issues and pull requests that include sensitive material, proprietary material, or bypass logic.
