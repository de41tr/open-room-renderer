"""Bounded, versioned local settings with atomic durable replacement."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


def atomic_write_text(path: Path, value: str) -> None:
    """Replace one owned state file; failures never truncate its previous value."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _gain_values(volume: object, mute: object) -> tuple[int, bool]:
    if type(volume) is not int or not 0 <= volume <= 100 or type(mute) is not bool:
        raise ValueError("Persisted gain must contain volume 0..100 and boolean mute")
    return volume, mute


def load_gain(state_dir: str | Path) -> tuple[int, bool] | None:
    """Read confirmed gain; corrupt/unknown state fails closed instead of unmuting."""
    path = Path(state_dir) / "gain.json"
    try:
        with path.open("rb") as stream:
            encoded = stream.read(4097)
    except FileNotFoundError:
        return None
    if len(encoded) > 4096:
        raise ValueError("Persisted gain exceeds its size limit")
    try:
        payload = json.loads(encoded)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Invalid persisted gain document") from None
    if (not isinstance(payload, dict) or set(payload) != {"version", "volume", "mute"}
            or type(payload["version"]) is not int or payload["version"] != 1):
        raise ValueError("Unsupported persisted gain document")
    return _gain_values(payload["volume"], payload["mute"])


def save_gain(state_dir: str | Path, volume: int, mute: bool) -> None:
    """Persist only gain already confirmed by the backend, without account data."""
    volume, mute = _gain_values(volume, mute)
    atomic_write_text(Path(state_dir) / "gain.json", json.dumps(
        {"version": 1, "volume": volume, "mute": mute}, sort_keys=True) + "\n")
