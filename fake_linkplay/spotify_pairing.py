"""Explicit display of an already-issued user code; never starts authentication."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import http.client
import re
from typing import Callable

from .spotify_diagnostic import SpotifyDiagnosticError, _json_object
from .spotify_exclusive import ExclusiveProfileError
from .spotify_playback import _owned_connection, _request

# Spotify's own support page documents this destination. Never emit or visit
# the upstream URL, which may contain a code or other unrequested data.
PAIRING_URL = "https://www.spotify.com/pair"
_UNAVAILABLE = "Spotify pairing code unavailable; no authentication was started or retried."


def _project_code(value: dict) -> tuple[str, datetime]:
    # v0.10.3 maps these exact fields from UserCode, VerificationUrl and Expiry.
    # Fail closed on contract changes, including added token/account fields.
    if set(value) != {"url", "code", "expires_at"}:
        raise ValueError
    code, expiry, url = value["code"], value["expires_at"], value["url"]
    if (type(code) is not str or not 4 <= len(code) <= 16
            or not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", code)
            or type(url) is not str or not 1 <= len(url) <= 2048
            or type(expiry) is not str
            or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                                r"(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})", expiry)):
        raise ValueError
    return code, datetime.fromisoformat(expiry.replace("Z", "+00:00"))


def pairing_code(host: str, port: int, pid: str, validate_invocation: Callable[[], None]) -> dict:
    """One passive owned GET; project only the permitted user code after the fence."""
    try:
        with _owned_connection(host, port, pid) as connection:
            validate_invocation()
            status, encoded = _request(connection, "GET", "/auth/code")
            pending = _project_code(_json_object(encoded)) if status == 200 else None
            validate_invocation()
        result = {"command": "pairing-code", "pairing_observation": "ABSENT",
                  "listener_ownership": "owned_connection", "authentication_started": False}
        if pending is not None:
            code, expiry = pending
            now = datetime.now(timezone.utc)
            if expiry <= now:
                result["pairing_observation"] = "EXPIRED"
            elif expiry > now + timedelta(hours=1):
                raise ValueError  # Only short-lived challenges are displayable.
            else:
                result.update(pairing_observation="AVAILABLE", user_code=code, verification_url=PAIRING_URL)
        return result
    except (OSError, http.client.HTTPException, SpotifyDiagnosticError, ValueError, ExclusiveProfileError):
        raise ExclusiveProfileError(_UNAVAILABLE) from None
