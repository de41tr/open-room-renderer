"""Project an independently authorized local session into the app contract.

No login, token exchange or entitlement is invented here. An operator must
explicitly supply their own client configuration and session store. This
module never advertises TIDAL or treats a saved token as playback acceptance.
The returned wire payload contains credentials and must not enter diagnostics.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import re
import time
from urllib.parse import urlsplit

from .tidal_codec import TidalCodecUnavailable, encode_account_field
from .tidal_session import TidalSessionError, TidalSessionStore


class TidalAccountUnavailable(RuntimeError):
    """A fixed account boundary error, with no token, identity or path values."""


@dataclass(frozen=True, repr=False)
class TidalClientIdentity:
    """The operator's authorized client identity, never an OEM fallback."""

    client_id: str
    redirect_uri: str

    def __post_init__(self) -> None:
        if not isinstance(self.client_id, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{1,256}", self.client_id):
            raise TidalAccountUnavailable("An independently authorized TIDAL client is required")
        try:
            if (not isinstance(self.redirect_uri, str) or len(self.redirect_uri) > 2048
                    or any(ord(c) <= 32 or ord(c) == 127 for c in self.redirect_uri)
                    or "\\" in self.redirect_uri):
                raise ValueError
            parts = urlsplit(self.redirect_uri)
            if (parts.scheme not in {"https", "http"} or not parts.hostname
                    or parts.username is not None or parts.password is not None or parts.fragment
                    or (parts.scheme == "http" and parts.hostname not in {"127.0.0.1", "localhost", "::1"})):
                raise ValueError
        except ValueError:
            raise TidalAccountUnavailable("A valid registered TIDAL redirect is required") from None


class TidalAccountBridge:
    """Explicit opt-in account projection with reload-on-read and logout lock.

It deliberately supplies no native-app UserLogin implementation: the observed
app flow does not establish PKCE support for an independent public client.
Refresh remains the responsibility of the authorized session owner. Expired
or incomplete sessions fail closed, rather than becoming phantom accounts.
"""

    def __init__(self, store: TidalSessionStore, client: TidalClientIdentity):
        self._store = store
        self._client = client
        self._lock = asyncio.Lock()

    async def get_user_info(self, short_uuid: str, *, version: str, now: float | None = None) -> dict:
        if version != "2":
            raise TidalAccountUnavailable("Unsupported TIDAL account contract version")
        now = time.time() if now is None else now
        if type(now) not in {int, float} or not math.isfinite(now) or now < 0:
            raise TidalAccountUnavailable("TIDAL account time is unavailable")
        async with self._lock:
            try:
                session = self._store.load()
                if session is None:
                    raise TidalAccountUnavailable("No authorized TIDAL session is available")
                if session.expires_at is None or session.expires_at <= now:
                    raise TidalAccountUnavailable("TIDAL session needs authorized refresh")
                if (not session.user_id or not re.fullmatch(r"[0-9]{1,19}", session.user_id)
                        or not 0 < int(session.user_id) <= 2**63 - 1 or not session.country_code):
                    raise TidalAccountUnavailable("TIDAL account identity is incomplete")
                return {
                    "access_token": encode_account_field(short_uuid, session.access_token),
                    "clientId": encode_account_field(short_uuid, self._client.client_id),
                    "redirectURI": self._client.redirect_uri,
                    "expires_in": max(0, int(session.expires_at - now)),
                    "token_type": "Bearer",
                    "user": {"userId": int(session.user_id), "username": "",
                             "countryCode": session.country_code},
                }
            except (TidalSessionError, TidalCodecUnavailable, ValueError, OverflowError):
                raise TidalAccountUnavailable("TIDAL account projection is unavailable") from None

    async def clear_local_session(self) -> None:
        """Explicit local logout only; this does not revoke provider tokens."""
        async with self._lock:
            try:
                self._store.clear()
            except TidalSessionError:
                raise TidalAccountUnavailable("TIDAL local logout failed") from None
