"""Strict, pure transfer of one saved EQ curve in the existing v1 format.

This boundary intentionally accepts less than the persisted library loader.
It never reads, mutates or activates a library, renderer or backend. In
particular, the private plugin identifier is retained verbatim so the caller
can compare it with its current plugin before publishing a saved curve.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, DecimalException
import json
import math
import re
from urllib.parse import unquote

from .eq_presets import normalize_eq_preset_name
from .log_safety import redact_media_urls
from .persistence import validate_eq_records


MAX_EQ_TRANSFER_BYTES = 32 * 1024
MAX_EQ_TRANSFER_NAME_BYTES = 256
MAX_EQ_TRANSFER_PLUGIN_BYTES = 8192
_BUILTIN_NAMES = frozenset({"Flat", "Bass Boost", "Treble Boost"})
_INVALID_TEXT = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff\ufffe\uffff]")
_PRIVATE_NAME = re.compile(
    r"(?i)(?:https?|file|ftp)://|[?][^ ]*=|"
    r"(?:access_token|auth_token|api_key|password|token|signature)="
)
_INVALID_TRANSFER = "Invalid saved EQ curve transfer"


@dataclass(frozen=True)
class EqTransferCurve:
    """Detached saved data; freezing fields does not freeze the band records."""

    name: str
    plugin: str = field(repr=False)
    bands: list[dict[str, object]]


def _valid_text(value: object, limit: int) -> bool:
    return (isinstance(value, str) and bool(value)
            and not _INVALID_TEXT.search(value)
            and len(value.encode("utf-8")) <= limit)


def _visible_name(value: str) -> bool:
    # Match the Web DSP display boundary, including escaped/mixed media URLs.
    return (not _INVALID_TEXT.search(value)
            and redact_media_urls(value) == value
            and not _PRIVATE_NAME.search(value))


def valid_transfer_name(value: object) -> bool:
    """Accept a literal, canonical, safely displayable non-built-in name.

    Percent signs stay literal. Decoding is used only to reject hidden private
    metadata, matching the Web name check's two conservative decoding passes.
    """
    try:
        if (not _valid_text(value, MAX_EQ_TRANSFER_NAME_BYTES)
                or value in _BUILTIN_NAMES
                or normalize_eq_preset_name(value) != value
                or not _visible_name(value)):
            return False
        decoded = value
        for _ in range(2):
            decoded = unquote(decoded, errors="strict")
            if not _visible_name(decoded):
                return False
        return True
    except (ValueError, TypeError, UnicodeError):
        return False


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(_INVALID_TRANSFER)
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(_INVALID_TRANSFER)


def _exact_float(raw: str) -> float:
    """Reject JSON decimals whose meaning would change in a float round trip.

    Validate the original decimal before float rounding could turn an invalid
    boundary value or filter mode into an accepted one, or erase a tiny gain.
    Python's canonical finite float encodings remain valid, as do equivalent
    decimal/exponent spellings. This is not a binary-exact decimal requirement.
    """
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(_INVALID_TRANSFER)
    try:
        if Decimal(str(value)) != Decimal(raw):
            raise ValueError(_INVALID_TRANSFER)
    except DecimalException:
        raise ValueError(_INVALID_TRANSFER) from None
    return value


def _validated_curve(name: object, plugin: object, bands: object) -> EqTransferCurve:
    if (not valid_transfer_name(name)
            or not _valid_text(plugin, MAX_EQ_TRANSFER_PLUGIN_BYTES)
            or not plugin.strip()):
        raise ValueError(_INVALID_TRANSFER)
    # The persisted/API validator permits numeric strings. Transfers do not:
    # this boundary must neither coerce values nor silently complete a curve.
    if (not isinstance(bands, list) or len(bands) != 40
            or any(not isinstance(item, dict) or set(item) != {"param_name", "value"}
                   or type(item["value"]) not in {int, float} for item in bands)):
        raise ValueError(_INVALID_TRANSFER)
    validate_eq_records(bands, require_complete=True)
    # Retain numeric types, record order and key order; validation's float
    # projection is deliberately discarded. No mutable caller data is kept.
    return EqTransferCurve(name, plugin, [dict(item) for item in bands])


def decode_eq_transfer(encoded: bytes) -> EqTransferCurve:
    """Decode one complete UTF-8 saved curve, or raise a private-safe error."""
    try:
        if not isinstance(encoded, bytes) or len(encoded) > MAX_EQ_TRANSFER_BYTES:
            raise ValueError(_INVALID_TRANSFER)
        payload = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_object,
                             parse_constant=_reject_constant, parse_float=_exact_float)
        if (not isinstance(payload, dict) or set(payload) != {"version", "presets"}
                or type(payload["version"]) is not int or payload["version"] != 1):
            raise ValueError(_INVALID_TRANSFER)
        presets = payload["presets"]
        if not isinstance(presets, dict) or len(presets) != 1:
            raise ValueError(_INVALID_TRANSFER)
        name, plugins = next(iter(presets.items()))
        if not isinstance(plugins, dict) or len(plugins) != 1:
            raise ValueError(_INVALID_TRANSFER)
        plugin, bands = next(iter(plugins.items()))
        return _validated_curve(name, plugin, bands)
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise ValueError(_INVALID_TRANSFER) from None


def encode_eq_transfer(name: str, plugin: str, bands: object) -> bytes:
    """Encode explicitly selected saved data without resolving built-in curves."""
    try:
        curve = _validated_curve(name, plugin, bands)
        payload = {"version": 1, "presets": {curve.name: {curve.plugin: curve.bands}}}
        encoded = (json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False)
                   + "\n").encode("utf-8")
        if len(encoded) > MAX_EQ_TRANSFER_BYTES:
            raise ValueError(_INVALID_TRANSFER)
        return encoded
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise ValueError(_INVALID_TRANSFER) from None
