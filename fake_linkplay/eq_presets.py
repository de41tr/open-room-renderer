"""Synchronous EQ preset library and strict parameter/request validation.

The library owns saved curves only. Active plugin/source caches, backend
confirmation, transaction rollback and active_dsp.json remain with the caller.
Operations do not schedule work or acquire locks: HttpApi calls them under its
existing DSP command lock, before/after backend confirmation as appropriate.
No renderer, backend, configuration or transport object is retained here.
"""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
import json
from pathlib import Path
import re
from urllib.parse import unquote

from .persistence import atomic_write_text, validate_eq_records

DEFAULT_EQ_PLUGIN = "http://moddevices.com/plugins/caps/EqNp"
PRESET_LIBRARY_UNAVAILABLE_MESSAGE = (
    "Saved EQ presets could not be loaded. Stop the renderer service, repair or "
    "restore its saved EQ preset library, then restart it."
)
DEFAULT_EQ_BAND_FREQS = {
    "a": 30.0,
    "b": 62.5,
    "c": 125.0,
    "d": 250.0,
    "e": 500.0,
    "f": 1000.0,
    "g": 2000.0,
    "h": 4000.0,
    "i": 8000.0,
    "j": 16000.0,
}


def normalize_eq_preset_name(name: str) -> str:
    if any(ord(char) < 32 for char in name):
        raise ValueError("Invalid EQ preset name")
    name = name.strip()
    if name.startswith("{") and name.endswith("}") and "Name=" in name:
        match = re.search(r"(?:^|\{|,\s*)Name=([^,}]+)", name)
        if match:
            return match.group(1).strip() or "Custom"
    return name or "Custom"


def default_eq_bands() -> list[dict[str, object]]:
    bands: list[dict[str, object]] = []
    for letter, freq in DEFAULT_EQ_BAND_FREQS.items():
        bands.extend(
            [
                {"param_name": f"{letter}_freq", "value": freq},
                {"param_name": f"{letter}_gain", "value": 0.0},
                {"param_name": f"{letter}_q", "value": 0.25},
                {"param_name": f"{letter}_mode", "value": 1},
            ]
        )
    return bands


def preset_eq_bands(updates: dict[str, float]) -> list[dict[str, object]]:
    bands = default_eq_bands()
    for item in bands:
        param_name = item.get("param_name")
        if isinstance(param_name, str) and param_name in updates:
            item["value"] = updates[param_name]
    return bands


def is_flat_eq_bands(bands: list[dict[str, object]]) -> bool:
    for item in bands:
        param_name = item.get("param_name")
        if not isinstance(param_name, str):
            continue
        try:
            value = float(item.get("value", 0.0))
        except (TypeError, ValueError):
            continue
        if param_name.endswith("_gain") and abs(value) >= 0.01:
            return False
        if param_name.endswith("_mode") and value not in {-1.0, 1.0}:
            return False
    return True


def merge_eq_bands(current: list[dict[str, object]] | None, updates: list[object]) -> list[dict[str, object]]:
    """Validate partial changes and retain exactly forty canonical records."""
    base = validate_eq_records(current if current is not None else default_eq_bands(), require_complete=True)
    changed = validate_eq_records(updates)
    merged = {str(item["param_name"]): item for item in base}
    merged.update({str(item["param_name"]): item for item in changed})
    return [merged[f"{letter}_{suffix}"] for letter in DEFAULT_EQ_BAND_FREQS
            for suffix in ("freq", "gain", "q", "mode")]


def reject_eq_constant(value: str) -> object:
    raise ValueError("Non-finite EQ JSON value")


def unique_eq_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject ambiguous fields before selecting any DSP value or preset."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate EQ field")
        result[key] = value
    return result


def eq_payload(command: str, prefix: str) -> dict[str, object]:
    raw = unquote(command.removeprefix(prefix))
    if len(raw) > 128 * 1024:
        raise ValueError("EQ request is too large")
    try:
        payload = json.loads(raw, parse_constant=reject_eq_constant,
                             object_pairs_hook=unique_eq_object)
    except RecursionError as exc:
        raise ValueError("EQ request nesting is too deep") from exc
    if not isinstance(payload, dict):
        raise ValueError("EQ request must be an object")
    for field in ("source_name", "source"):
        if field in payload and payload[field] != "wifi":
            raise ValueError("Only wifi EQ is supported")
    if "channelMode" in payload and payload["channelMode"] != "Stereo":
        raise ValueError("Only Stereo EQ is supported")
    for field in ("pluginURI", "plugin", "Name", "name", "newName", "NewName"):
        if field in payload and not isinstance(payload[field], str):
            raise ValueError("EQ identifier must be text")
    for field in ("EQBandL", "EQBandR"):
        if field in payload and payload[field] != []:
            raise ValueError("Per-channel EQ is unsupported")
    return payload


class EqPresetLibraryUnavailableError(OSError):
    """Known pre-write refusal after an observed saved-library load failure."""

    def __init__(self) -> None:
        super().__init__(PRESET_LIBRARY_UNAVAILABLE_MESSAGE)


class EqPresetLibrary:
    """Own the saved preset mapping without claiming any curve is active."""

    def __init__(self) -> None:
        self.presets: dict[str, dict[str, list[dict[str, object]]]] = {}
        self._unavailable = False

    @property
    def unavailable(self) -> bool:
        """A failed load fences writes for this lifetime, independent of DSP."""
        return self._unavailable

    def load(self, state_dir: str | Path) -> None:
        """Replace the library only after the complete bounded file validates."""
        path = Path(state_dir) / "eq_presets.json"
        try:
            try:
                with path.open("rb") as stream:
                    encoded = stream.read(1024 * 1024 + 1)
            except FileNotFoundError:
                # A dangling file symlink is existing storage that could still
                # be atomically replaced. Only a genuinely absent entry is new.
                try:
                    path.lstat()
                except FileNotFoundError:
                    return
                raise
            if len(encoded) > 1024 * 1024:
                raise ValueError("EQ preset document is too large")
            payload = json.loads(encoded, parse_constant=reject_eq_constant,
                                 object_pairs_hook=unique_eq_object)
            if (not isinstance(payload, dict) or type(payload.get("version", 1)) is not int
                    or payload.get("version", 1) != 1):
                raise ValueError("Unsupported EQ preset document")
            presets = payload.get("presets")
            if not isinstance(presets, dict):
                raise ValueError("Invalid EQ preset library")
            loaded: dict[str, dict[str, list[dict[str, object]]]] = {}
            for name, plugins in presets.items():
                if not isinstance(name, str) or not isinstance(plugins, dict):
                    raise ValueError("Invalid EQ preset")
                clean_name = normalize_eq_preset_name(name)
                if clean_name in loaded:
                    raise ValueError("Duplicate EQ preset name")
                loaded[clean_name] = {}
                for plugin_uri, bands in plugins.items():
                    if not isinstance(plugin_uri, str) or not plugin_uri:
                        raise ValueError("Invalid EQ plugin")
                    loaded[clean_name][plugin_uri] = merge_eq_bands(None, bands)
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            self._unavailable = True
            raise
        self.presets = loaded

    def persist(self, state_dir: str | Path, *,
                write_text: Callable[[Path, str], None] = atomic_write_text) -> None:
        """Write library bytes only; preserve pre/post-replacement exceptions."""
        if self.unavailable:
            raise EqPresetLibraryUnavailableError()
        payload = {"version": 1, "presets": self.presets}
        encoded = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
        if len(encoded.encode("utf-8")) > 1024 * 1024:
            raise ValueError("EQ preset library exceeds its persisted size limit")
        write_text(Path(state_dir) / "eq_presets.json", encoded)

    def names(self, plugin_uri: str) -> list[str]:
        return [normalize_eq_preset_name(name) for name in sorted(self.presets)
                if plugin_uri in self.presets[name]]

    def save(self, name: str, plugin_uri: str, bands: list[dict[str, object]]) -> None:
        self.presets.setdefault(name, {})[plugin_uri] = deepcopy(bands)

    def bands(self, name: str, plugin_uri: str) -> list[dict[str, object]]:
        """Resolve a saved or built-in curve without activating or aliasing it."""
        if name in self.presets and plugin_uri in self.presets[name]:
            bands = [dict(band) for band in self.presets[name][plugin_uri]]
        elif name == "Flat":
            bands = default_eq_bands()
        elif name == "Bass Boost":
            bands = preset_eq_bands({"a_gain": 5.0, "b_gain": 3.0})
        elif name == "Treble Boost":
            bands = preset_eq_bands({"i_gain": 3.0, "j_gain": 5.0})
        else:
            raise ValueError("Unknown EQ preset for plugin")
        return merge_eq_bands(None, bands)

    def delete(self, name: str) -> None:
        self.presets.pop(name, None)

    def rename(self, old_name: str, new_name: str) -> None:
        if old_name not in self.presets:
            raise ValueError("Unknown EQ preset")
        if new_name != old_name and new_name in self.presets:
            raise ValueError("EQ preset name already exists")
        self.presets[new_name] = self.presets.pop(old_name)

    def display_name(self, plugin: str, bands: list[dict[str, object]], name: str) -> str:
        if name and self.presets.get(name, {}).get(plugin) == bands:
            return name
        if name in {"Bass Boost", "Treble Boost"}:
            expected = preset_eq_bands({"a_gain": 5.0, "b_gain": 3.0} if name == "Bass Boost" else {"i_gain": 3.0, "j_gain": 5.0})
            if bands == expected:
                return name
        return "Flat" if is_flat_eq_bands(bands) else "Custom"
