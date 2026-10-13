"""Bounded, versioned local settings with atomic durable replacement."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import tempfile


class AtomicWriteCommittedError(OSError):
    """Replacement succeeded, but its directory durability could not be confirmed."""


def atomic_write_text(path: Path, value: str) -> None:
    """Replace one owned state file; failures never truncate its previous value."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    committed = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        committed = True
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        if committed:
            raise AtomicWriteCommittedError("State replaced; directory durability unconfirmed") from exc
        raise
    finally:
        # A successful replace has already removed the source pathname. Do
        # not risk a cleanup error masking a committed write as uncommitted.
        if not committed:
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
        # A dangling link is saved state that cannot be read, not first-run
        # absence. Only a missing directory entry permits default gain.
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        raise
    if len(encoded) > 4096:
        raise ValueError("Persisted gain exceeds its size limit")
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate gain field")
            result[key] = value
        return result

    try:
        payload = json.loads(encoded, object_pairs_hook=unique_object)
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


ACTIVE_DSP_MAX_BYTES = 1024 * 1024


def validate_eq_records(bands: object, *, require_complete: bool = False) -> list[dict[str, object]]:
    """Validate API or persisted parameters without clamping or inventing gain."""
    if not isinstance(bands, list) or len(bands) > 40:
        raise ValueError("EQBand must contain at most forty parameters")
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    limits = {"freq": (10.0, 24000.0), "gain": (-24.0, 24.0), "q": (0.1, 30.0)}
    for item in bands:
        if not isinstance(item, dict) or set(item) != {"param_name", "value"}:
            raise ValueError("Invalid EQ parameter record")
        key = item["param_name"]
        if not isinstance(key, str) or not re.fullmatch(r"[a-j]_(freq|gain|q|mode)", key) or key in seen:
            raise ValueError("Invalid or duplicate EQ parameter name")
        raw = item["value"]
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise ValueError("EQ parameter must be numeric")
        try:
            value = float(raw)
        except (ValueError, OverflowError) as exc:
            raise ValueError("EQ parameter must be a finite number") from exc
        if not math.isfinite(value):
            raise ValueError("EQ parameter must be finite")
        suffix = key.split("_", 1)[1]
        if suffix == "mode":
            if value not in {-1.0, 0.0, 1.0, 2.0, 3.0, 5.0}:
                raise ValueError("Unsupported EQ filter mode")
        elif not limits[suffix][0] <= value <= limits[suffix][1]:
            raise ValueError("EQ parameter outside supported range")
        seen.add(key)
        result.append({"param_name": key, "value": value})
    if require_complete and len(result) != 40:
        raise ValueError("Incomplete EQ parameter set")
    return result


def validate_active_dsp(payload: object) -> dict[str, object]:
    """Return a private, complete active snapshot, distinct from the library."""
    fields = {"version", "plugin", "source", "enabled", "name", "balance", "bands"}
    if (not isinstance(payload, dict) or set(payload) != fields
            or type(payload["version"]) is not int or payload["version"] != 1):
        raise ValueError("Unsupported persisted active DSP document")
    for field in ("plugin", "source", "name"):
        if not isinstance(payload[field], str) or any(ord(char) < 32 for char in payload[field]):
            raise ValueError("Invalid active DSP identifier")
    if not payload["plugin"] or payload["source"] not in {"", "wifi"}:
        raise ValueError("Unsupported active DSP context")
    if type(payload["enabled"]) is not bool:
        raise ValueError("Active DSP enabled must be boolean")
    balance = payload["balance"]
    if (type(balance) not in (int, float) or not -1.0 <= balance <= 1.0
            or round(balance, 3) != balance):
        raise ValueError("Invalid active DSP balance")
    records = validate_eq_records(payload["bands"], require_complete=True)
    by_name = {record["param_name"]: record for record in records}
    return {**payload, "balance": float(balance), "bands": [
        by_name[f"{letter}_{suffix}"] for letter in "abcdefghij"
        for suffix in ("freq", "gain", "q", "mode")
    ]}


def encode_active_dsp(payload: object) -> str:
    """Validate and bound exactly the same snapshot accepted at restart."""
    encoded = json.dumps(validate_active_dsp(payload), sort_keys=True, allow_nan=False) + "\n"
    if len(encoded.encode("utf-8")) > ACTIVE_DSP_MAX_BYTES:
        raise ValueError("Persisted active DSP exceeds its size limit")
    return encoded


def load_active_dsp(state_dir: str | Path) -> dict[str, object] | None:
    """Fail closed on unknown/corrupt state; absence alone permits defaults."""
    path = Path(state_dir) / "active_dsp.json"
    try:
        with path.open("rb") as stream:
            encoded = stream.read(ACTIVE_DSP_MAX_BYTES + 1)
    except FileNotFoundError:
        # Preserve an existing unreadable link instead of selecting a default
        # graph that a later confirmed write would publish over that link.
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        raise
    if len(encoded) > ACTIVE_DSP_MAX_BYTES:
        raise ValueError("Persisted active DSP exceeds its size limit")
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate active DSP field")
            result[key] = value
        return result

    try:
        payload = json.loads(encoded, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Invalid persisted active DSP document") from None
    return validate_active_dsp(payload)
