"""The legacy renderer-name validation contract, without transport decoding."""
from __future__ import annotations

import re


def validate_device_name(name: str) -> str:
    """Validate an already-decoded, stripped name without changing its bytes."""
    if not name or len(name) > 128 or re.search(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff\ufffe\uffff]", name):
        raise ValueError("Invalid device name")
    return name


class DeviceNameCommittedError(OSError):
    """The stored/live name changed, but discovery refresh was not confirmed."""
