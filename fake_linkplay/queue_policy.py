"""Pure local queue policy and explicit public-client repeat adapters.

Numeric values follow the pywiim HTTP/PlayQueue dialect, not every device's
HTTP documentation. Shuffle is deliberately unsupported.
"""
from __future__ import annotations

NORMAL = 4
REPEAT_ONE = 1
REPEAT_ALL = 0
SUPPORTED_MODES = (NORMAL, REPEAT_ONE, REPEAT_ALL)
PLAY_MODES = {"NORMAL": NORMAL, "REPEAT_ONE": REPEAT_ONE, "REPEAT_ALL": REPEAT_ALL}


def parse_loop_mode(value: str) -> int:
    """Accept only the supported numeric wire values, without coercion."""
    if type(value) is not str or value not in {"4", "1", "0"}:
        raise ValueError("Unsupported queue loop mode")
    return int(value)


def parse_play_mode(value: str) -> int:
    """Accept standard AVTransport names, never numeric or shuffle aliases."""
    if type(value) is not str or value not in PLAY_MODES:
        raise ValueError("Unsupported play mode")
    return PLAY_MODES[value]


def play_mode_name(mode: int) -> str:
    """Return the standard name for a validated canonical queue mode."""
    if type(mode) is not int or mode not in SUPPORTED_MODES:
        raise ValueError("Unsupported queue loop mode")
    return next(name for name, value in PLAY_MODES.items() if value == mode)


def natural_successor(index: int, count: int, mode: int) -> int | None:
    """Choose a one-based index at natural EOF; explicit navigation is separate."""
    play_mode_name(mode)
    if count == 0:
        return None
    if mode == REPEAT_ONE:
        return index
    if index < count:
        return index + 1
    return 1 if mode == REPEAT_ALL else None
