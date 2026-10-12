"""Small, single-shot DSP controls for the renderer's active context.

Balance input is normalized -1..1, never a percentage, and writes use the
renderer's 0.001 resolution. A mutation ACK acknowledges a request only; it
does not independently observe the current DSP graph or audible output.
There is intentionally no EQ read: the legacy API does not identify the active
source reliably enough to select an unambiguous read command.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
import re
import sys

import requests

from .mpv_backend import BackendError, MpvBackend
from .queue_client import QueueClientError, _endpoint

MAX_RESPONSE_BYTES = 256
MAX_BALANCE_INPUT_CHARS = 128
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")
_INVALID_BALANCE = "Invalid DSP balance. Use a finite normalized decimal from -1 to 1, not a percentage."
_INVALID_EQ = "Invalid DSP EQ input. Use on or off."
_READ_FAILED = "DSP balance read was not confirmed."
_REJECTED = "DSP request was rejected by the renderer."
_UNKNOWN = ("DSP request outcome is unknown. "
            "Inspect dsp balance get and status before an explicit retry; "
            "the command may already have taken effect.")


class DspClientError(ValueError):
    """A fixed, private-safe failure without response bodies or endpoint data."""


def _validate_balance(value: object) -> float:
    try:
        return MpvBackend.validate_channel_balance(value)
    except BackendError:
        raise DspClientError(_INVALID_BALANCE) from None


def balance_value(value: str) -> float:
    """Parse an ASCII decimal without percentages or implicit range rounding."""
    try:
        if (not isinstance(value, str) or len(value) > MAX_BALANCE_INPUT_CHARS
                or _DECIMAL.fullmatch(value) is None):
            raise ValueError
        # Check the exact decimal first: float('1.00000000000000001') would
        # otherwise silently accept an out-of-range value by rounding to 1.
        if not Decimal(-1) <= Decimal(value) <= Decimal(1):
            raise ValueError
        return _validate_balance(float(value))
    except (ValueError, InvalidOperation):
        raise argparse.ArgumentTypeError(_INVALID_BALANCE) from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ValueError


class DspClient:
    """No environment credentials/proxies, redirects, or automatic retries."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        try:
            self.endpoint = _endpoint(host, port, path="/httpapi.asp")
        except QueueClientError:
            raise DspClientError("Invalid renderer host or port.") from None

    def _request(self, command: str, *, mutation: bool) -> bytes:
        failure = _UNKNOWN if mutation else _READ_FAILED
        try:
            with requests.Session() as session:
                session.trust_env = False
                # Requests' default adapters perform zero retries. Ten seconds
                # bounds connect/read inactivity, not the overall elapsed time.
                with session.get(self.endpoint, params={"command": command},
                                 headers={"Accept-Encoding": "identity"}, timeout=10,
                                 allow_redirects=False, stream=True) as response:
                    if response.status_code not in ((200, 400) if mutation else (200,)):
                        raise DspClientError(failure)
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise DspClientError(failure)
                    length = response.headers.get("Content-Length")
                    if length is not None and (not re.fullmatch(r"[0-9]{1,10}", length)
                                               or int(length) > MAX_RESPONSE_BYTES):
                        raise DspClientError(failure)
                    encoded = bytearray()
                    for chunk in response.iter_content(chunk_size=MAX_RESPONSE_BYTES + 1):
                        if len(encoded) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise DspClientError(failure)
                        encoded.extend(chunk)
                    if length is not None and len(encoded) != int(length):
                        raise DspClientError(failure)
                    if response.status_code == 400:
                        # This is the existing API's pre-application rejection
                        # contract. A 502 can instead follow an applied graph
                        # and failed persistence, so must remain unknown.
                        raise DspClientError(_REJECTED if encoded == b"Failed" else failure)
                    return bytes(encoded)
        except requests.RequestException:
            raise DspClientError(failure) from None

    def get_balance(self) -> float:
        """Read the renderer's normalized balance; never infer a fallback value."""
        encoded = self._request("getChannelBalance", mutation=False)
        try:
            return balance_value(encoded.decode("ascii"))
        except (argparse.ArgumentTypeError, UnicodeError):
            raise DspClientError(_READ_FAILED) from None

    def set_balance(self, value: int | float) -> float:
        """Acknowledge a balance request; return its canonical requested value."""
        value = _validate_balance(value)
        # Match the existing API's three-decimal resolution before sending, so
        # the acknowledged request value cannot overstate submitted precision.
        value = float(f"{value:.3f}") or 0.0
        wire_value = f"{value:.3f}".rstrip("0").rstrip(".") if value else "0.0"
        if self._request("setChannelBalance:" + wire_value, mutation=True) != b"OK":
            raise DspClientError(_UNKNOWN)
        return value

    def set_eq(self, value: str) -> None:
        """Enable/bypass the active EQ context without replacing its curve."""
        if not isinstance(value, str) or value not in {"on", "off"}:
            raise DspClientError(_INVALID_EQ)
        encoded = self._request("EQOn" if value == "on" else "EQOff", mutation=True)
        try:
            result = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_object,
                                parse_constant=_invalid_constant)
            if result != {"status": "0"}:
                raise ValueError
        except (ValueError, UnicodeError, RecursionError):
            raise DspClientError(_UNKNOWN) from None


def add_dsp_parser(subparsers) -> None:
    dsp = subparsers.add_parser("dsp", help="read balance or request balance/EQ changes",
        description="Active DSP controls. Mutation acknowledgements do not observe audible output.")
    commands = dsp.add_subparsers(dest="dsp_cmd", required=True)
    balance = commands.add_parser("balance", help="read or set normalized channel balance")
    balance_commands = balance.add_subparsers(dest="balance_cmd", required=True)
    get = balance_commands.add_parser("get", help="read renderer-reported normalized balance")
    set_parser = balance_commands.add_parser("set", help="set normalized balance, never a percentage",
        description="Request normalized balance: -1 left, 0 center, 1 right. Rounded to 0.001.")
    set_parser.add_argument("value", type=balance_value, metavar="VALUE",
        help="finite decimal from -1 to 1 (no percentages); rounded to 0.001")
    eq = commands.add_parser("eq", help="enable or bypass the active EQ context")
    eq.add_argument("value", choices=("on", "off"))
    for parser in (get, set_parser, eq):
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=8080)


def run_dsp_command(args: argparse.Namespace) -> int:
    try:
        client = DspClient(args.host, args.port)
        if args.dsp_cmd == "balance" and args.balance_cmd == "get":
            result = {"balance": client.get_balance()}
        elif args.dsp_cmd == "balance" and args.balance_cmd == "set":
            value = client.set_balance(args.value)
            result = {"request_acknowledged": True, "command": "balance set", "value": value}
        else:
            client.set_eq(args.value)
            result = {"request_acknowledged": True, "command": "eq", "value": args.value}
    except DspClientError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0
