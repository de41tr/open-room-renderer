"""Shared, inert native user-unit template and systemd path quoting."""
from pathlib import Path


def systemd_quote(value: Path) -> str:
    # systemd specifier expansion applies even inside quotes.
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def user_unit(prefix: Path, *, config: Path | None = None) -> str:
    return f"""# Copy to ~/.config/systemd/user/open-room-renderer.service after review.
# Generated unit does not install, enable, start, or configure login lingering.
[Unit]
Description=Open Room Renderer (unprivileged native profile)
After=network.target

[Service]
Type=simple
WorkingDirectory={str(prefix).replace("%", "%%")}
ExecStart={systemd_quote(prefix / 'venv/bin/open-room-renderer')} serve --config {systemd_quote(config if config is not None else prefix / 'config.yaml')}
Restart=on-failure
RestartSec=5
TimeoutStopSec=45
UMask=0077
NoNewPrivileges=yes
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""
