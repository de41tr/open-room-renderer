"""Small process-wide utility helpers."""

from __future__ import annotations

import logging
import socket
from pathlib import Path

from .log_safety import redact_media_urls


class RedactingFormatter(logging.Formatter):
    """Sanitize fully formatted messages, including access logs and tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_media_urls(super().format(record))


def setup_logging(level: str) -> None:
    """Configure root logging with the project's standard text format."""

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)


def detect_lan_ip() -> str:
    """Best-effort LAN IP detection without sending application data.

    Returns:
        The local address selected for a UDP route to the public internet, or
        ``127.0.0.1`` when no route can be determined.
    """

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def ensure_dir(path: str | Path) -> Path:
    """Create ``path`` if needed and return it as a ``Path`` object."""

    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p
