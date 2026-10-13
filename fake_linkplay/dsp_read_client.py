"""Single-shot, observation-only DSP projection from the existing Web state.

This is separate from the small legacy mutation/acknowledgement transport. No
state, credentials, preset names, identifiers or raw response is cached or saved.
"""
from __future__ import annotations

from decimal import Decimal
import json
import math
import re

import requests

from .persistence import validate_eq_records
from .queue_client import QueueClientError, _endpoint

# Match the browser's whole-state response budget: the existing route also
# contains a potentially large queue. Only the small DSP allowlist escapes.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_JSON_DEPTH = 16
READ_TIMEOUT = 10
_READ_FAILED = "DSP state could not be read safely from the renderer."
_CONTENT_TYPE = re.compile(r'application/json(?:\s*;\s*charset\s*=\s*(?:utf-8|"utf-8"))?', re.I)
_BAND_LIMITS = {"freq": (10, 24000), "gain": (-24, 24), "q": (Decimal("0.1"), 30)}


class DspReadError(ValueError):
    """Fixed private failure: never include a URL, body or underlying error."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _decimal(value: str) -> Decimal:
    number = Decimal(value)
    # Reject exponent overflow and nonzero values that would silently
    # underflow to zero. Neither can be a canonical renderer float.
    floating = float(number)
    if not math.isfinite(floating) or (floating == 0 and number != 0):
        raise ValueError
    return number


def _invalid_constant(_value: str) -> None:
    raise ValueError


def _bounded_depth(text: str) -> None:
    """Bound nesting before json.loads, ignoring brackets inside strings."""
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError
        elif char in "]}":
            depth -= 1


def _number(value: object, low: int | Decimal, high: int | Decimal) -> float:
    # bool and numeric strings must not become valid readback values. Decimal
    # comparisons reject just-outside-range JSON before float can round it in.
    if type(value) not in (int, Decimal) or not low <= value <= high:
        raise ValueError
    return float(value)


def _bands(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) != 40:
        raise ValueError
    records = []
    for record in value:
        if not isinstance(record, dict) or set(record) != {"param_name", "value"}:
            raise ValueError
        name = record["param_name"]
        if not isinstance(name, str) or re.fullmatch(r"[a-j]_(freq|gain|q|mode)", name) is None:
            raise ValueError
        suffix = name.split("_", 1)[1]
        number = record["value"]
        if suffix == "mode":
            if type(number) not in (int, Decimal) or number not in {-1, 0, 1, 2, 3, 5}:
                raise ValueError
            number = float(number)
        else:
            number = _number(number, *_BAND_LIMITS[suffix])
        records.append({"param_name": name, "value": number})
    # Reuse the renderer's small pure validator for complete/unique records.
    # The checks above additionally forbid coercion and exact-bound rounding.
    return validate_eq_records(records, require_complete=True)


def parse_dsp_snapshot(encoded: bytes) -> dict[str, object]:
    """Validate current Web DSP truth and return only the versioned allowlist."""
    try:
        if type(encoded) is not bytes or len(encoded) > MAX_RESPONSE_BYTES:
            raise ValueError
        text = encoded.decode("utf-8")
        _bounded_depth(text)
        state = json.loads(text, object_pairs_hook=_unique_object,
                           parse_float=_decimal, parse_constant=_invalid_constant)
        if not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 1:
            raise ValueError
        dsp = state["dsp"]
        if not isinstance(dsp, dict) or set(dsp) != {"status", "balance", "eq_enabled", "name", "editor"}:
            raise ValueError
        status = dsp["status"]
        if not isinstance(status, str) or status not in {"CONFIRMED", "UNKNOWN", "PENDING"}:
            raise ValueError
        result = {"version": 1, "status": status, "balance": None, "eq_enabled": None,
                  "source": None, "channel_mode": None, "bands": None,
                  "observation_only": True, "physical_output_verified": False}
        if status != "CONFIRMED":
            if (dsp["balance"] is not None or dsp["eq_enabled"] is not None
                    or dsp["editor"] is not None or dsp["name"] != ""):
                raise ValueError
            return result
        balance = _number(dsp["balance"], -1, 1)
        if round(dsp["balance"], 3) != dsp["balance"] or type(dsp["eq_enabled"]) is not bool:
            raise ValueError
        editor = dsp["editor"]
        if (not isinstance(dsp["name"], str) or not isinstance(editor, dict)
                or editor.get("source") not in ("global", "wifi")
                or editor.get("channel_mode") != "Stereo"):
            raise ValueError
        return {**result, "balance": balance, "eq_enabled": dsp["eq_enabled"],
                "source": editor["source"], "channel_mode": "Stereo", "bands": _bands(editor["bands"])}
    except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError):
        raise DspReadError(_READ_FAILED) from None


class _ReadSession(requests.Session):
    def resolve_redirects(self, *_args, **_kwargs):
        # Even allow_redirects=False normally prepares Response.next, consuming
        # the redirect body and parsing Location before our stream bounds run.
        # Never prepare or follow that request, or read that rejected body.
        return iter(())


class DspReadClient:
    """One bodyless GET with no ambient credentials/proxies or retries."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        try:
            self.endpoint = _endpoint(host, port, path="/orr/v1/web/state")
        except QueueClientError:
            raise DspReadError("Invalid renderer host or port.") from None

    def show(self) -> dict[str, object]:
        try:
            with _ReadSession() as session:
                session.trust_env = False
                # Default adapters have zero retries. This bounds connect/read
                # inactivity, NOT total wall time (a slow drip can take longer).
                with session.get(self.endpoint, headers={"Accept": "application/json",
                        "Accept-Encoding": "identity"}, timeout=READ_TIMEOUT,
                        allow_redirects=False, stream=True) as response:
                    if (response.status_code != 200
                            or _CONTENT_TYPE.fullmatch(response.headers.get("Content-Type", "")) is None
                            or response.headers.get("Content-Encoding", "identity").lower() != "identity"):
                        raise DspReadError(_READ_FAILED) from None
                    length = response.headers.get("Content-Length")
                    transfer = response.headers.get("Transfer-Encoding")
                    if (transfer is not None and (transfer.lower() != "chunked" or length is not None)
                            or length is not None and (re.fullmatch(r"[0-9]{1,10}", length) is None
                                                      or int(length) > MAX_RESPONSE_BYTES)):
                        raise DspReadError(_READ_FAILED) from None
                    encoded = bytearray()
                    for chunk in response.iter_content(chunk_size=65536):
                        if len(encoded) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise DspReadError(_READ_FAILED) from None
                        encoded.extend(chunk)
                    if length is not None and len(encoded) != int(length):
                        raise DspReadError(_READ_FAILED) from None
                    return parse_dsp_snapshot(bytes(encoded))
        except requests.RequestException:
            raise DspReadError(_READ_FAILED) from None
