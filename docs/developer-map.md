# Developer Map

This map is for a contributor who has 10 minutes and wants to know where to start.

## Start Here

Read these files first:

- `README.md`: what the project is and how to run it.
- `pyproject.toml`: dependencies, console scripts, pytest config.
- `configs/config.example.yaml`: main development config.
- `fake_linkplay/cli.py`: startup flow.
- `fake_linkplay/http_api.py`: playback/DSP orchestration and HTTP/SOAP/event payload projection.
- `fake_linkplay/eventing.py`: independent GENA subscriber/lease/delivery owner; one synchronous XML provider.
- `tests/test_status_json.py`: many behavior examples in synthetic form.

## Playback Work

Read:

- `fake_linkplay/http_api.py`
- `fake_linkplay/mpv_backend.py`
- `fake_linkplay/state.py`
- `tests/test_command_parser.py`
- `tests/test_status_json.py`

Look for:

- `parse_command`
- `HttpApi.execute`
- `MpvBackend.loadfile`
- queue fields such as `queue_url`, `queue_resolved_url`, and `queue_duration_sec`

## EQ And Balance Work

Read:

- `fake_linkplay/http_api.py`
- `fake_linkplay/mpv_backend.py`
- `fake_linkplay/config.py`
- `tests/test_status_json.py`

Look for:

- `DEFAULT_EQ_BAND_FREQS`
- `eq_bands_by_plugin`
- `MpvBackend.peq`
- `MpvBackend.channel_balance`
- `MpvBackend._audio_filter_graph`
- persisted `channel_balance`

## Discovery Work

Read:

- `fake_linkplay/ssdp.py`
- `fake_linkplay/mdns.py`
- `fake_linkplay/upnp_description.py`
- `tests/test_ssdp.py`
- `tests/test_mdns.py`
- `tests/test_description_xml.py`

Useful scripts:

- `scripts/sniff-ssdp.sh`
- `scripts/sniff-mdns.sh`

Do not publish raw discovery captures. Summarize behavior in sanitized form.

## Packaging, systemd, Raspberry Pi

Read:

- `README.md`
- `examples/systemd/user-service.example`
- `scripts/dev-setup.sh`
- `scripts/run-dev.sh`
- `configs/config.example.yaml`

Good first tasks:

- Debian/Raspberry Pi OS dependency docs.
- User service installation notes.
- Audio device troubleshooting.
- Firewall/discovery troubleshooting.

## Tests

Read:

- `tests/test_command_parser.py`
- `tests/test_status_json.py`
- `tests/test_description_xml.py`
- `tests/test_ssdp.py`
- `tests/test_mdns.py`
- `tests/test_acceptance_adapter_imports.py`

Run:

```bash
PYTHONPATH=. .venv/bin/pytest -q
```

## Known Confusing Parts

- The package directory is still named `fake_linkplay` for compatibility with the current code. Public docs use `Open Room Renderer`.
- Some config values are compatibility knobs and may look vendor-specific. Do not rename them casually unless tests and app behavior are checked.
- `http_api.py` is large because it grew from compatibility experiments. Prefer small extractions with tests over broad rewrites.
- Port 80 is the default because some controller apps probe plain `http://device-ip/` during discovery or setup.
- `logs/`, `.state/`, and local notes are private working artifacts and should not be published.

## Glossary

- Renderer: the local network audio sink exposed by this service.
- Controller: an app or open-source client that discovers and controls the renderer.
- SSDP: multicast discovery protocol used by UPnP-style devices.
- mDNS/DNS-SD: multicast DNS service discovery.
- Description XML: UPnP-style device description at `/description.xml`.
- HTTP API: Linkplay-style command endpoint at `/httpapi.asp`.
- SOAP-ish endpoints: minimal UPnP control/event endpoints.
- RendererState: current status, metadata, volume, position, and queue-facing fields.
- mpv JSON IPC: Unix-socket API used to control `mpv`.
- PEQ: parametric equalizer.
- Compatibility knob: config or code path that exists because clients expect a specific shape.
