"""Small shared schema for explicit, startup-only mpv output selection."""

from __future__ import annotations

import re


def validate_audio_device(value: str | None, *, auto_spawn: bool) -> str | None:
    """Preserve a device identifier exactly; never interpret it as shell text.

    mpv owns device availability and backend-specific syntax. This only checks
    a bounded printable identifier and rules out its special help-and-exit mode.
    Errors deliberately contain no supplied value.
    """
    if value is None:
        return None
    if (not isinstance(value, str) or not value or value != value.strip()
            or not value.isprintable() or len(value.encode("utf-8")) > 1024
            or re.fullmatch(r"[A-Za-z0-9_-]+", value.partition("/")[0]) is None
            or value == "help"):
        raise ValueError("mpv_audio_device must be a printable mpv device identifier (not help), at most 1024 bytes")
    if auto_spawn is not True:
        raise ValueError("mpv_audio_device requires auto_spawn_mpv: true and an owned mpv process")
    return value
