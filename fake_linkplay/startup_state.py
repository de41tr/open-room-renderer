"""Private diagnostics for known persisted-state operations during CLI startup."""
from __future__ import annotations

from collections.abc import Callable
from typing import Literal, TypeVar


class StartupStateError(ValueError):
    """An expected startup state failure, with only fixed operator guidance."""


StateStage = Literal["directory", "identity", "gain", "device_name", "active_dsp", "channel_balance"]
_STATE_ERRORS: dict[StateStage, str] = {
    "directory": "State directory could not be opened. Check its path and permissions.",
    "identity": (
        "Saved identity could not be loaded or created. Check uuid and state directory "
        "permissions; preserve any published identity."
    ),
    "gain": (
        "Saved gain settings could not be loaded. Check gain.json in the configured "
        "state directory or restore a known-good backup."
    ),
    "device_name": (
        "Saved device name could not be loaded. Check device_name in the configured "
        "state directory, its text encoding, and permissions."
    ),
    "active_dsp": (
        "Saved active DSP settings could not be loaded. Check active_dsp.json in the "
        "configured state directory or restore a known-good backup."
    ),
    "channel_balance": (
        "Saved channel balance could not be loaded. Check channel_balance in the "
        "configured state directory, its text encoding, and permissions."
    ),
}
_T = TypeVar("_T")


def _read_state(reader: Callable[[], _T], stage: StateStage, *, startup: bool) -> _T:
    """Guard only a known reader's expected failures, with an explicit startup opt-in.

    ValueError includes decoding/JSON errors from these readers. Programming
    errors and cancellation must propagate unchanged. The ordinary library path
    retains the original exception and its diagnostic chain.
    """
    if not startup:
        return reader()
    message = _STATE_ERRORS[stage]
    try:
        return reader()
    except (OSError, ValueError):
        raise StartupStateError(message) from None
