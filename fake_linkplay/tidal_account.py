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
                    **self._client_fields(session, short_uuid),
                    "expires_in": max(0, int(session.expires_at - now)),
                    "token_type": "Bearer",
                    "user": {"userId": int(session.user_id), "username": "",
                             "countryCode": session.country_code},
                }
            except (TidalSessionError, TidalCodecUnavailable, ValueError, OverflowError):
                raise TidalAccountUnavailable("TIDAL account projection is unavailable") from None

    def _client_fields(self, session, short_uuid):
        return {"clientId": encode_account_field(short_uuid, self._client.client_id),
                "redirectURI": self._client.redirect_uri}

    async def clear_local_session(self) -> None:
        """Explicit local logout only; this does not revoke provider tokens."""
        async with self._lock:
            try:
                self._store.clear()
            except TidalSessionError:
                raise TidalAccountUnavailable("TIDAL local logout failed") from None


class TidalDeviceAccountBridge(TidalAccountBridge):
    """Explicit, process-local sharing of a provenance-bound device session.

    The real device client is projected; no registered redirect is invented.
    The app's already-authenticated branch uses Bearer /v1/sessions. Native
    app OAuth re-login remains unsupported. Restart revokes this local opt-in.
    """

    def __init__(self, store):
        self._store = store
        self._lock = asyncio.Lock()
        self._shared_session = None

    @property
    def shared(self):
        session = self._shared_session
        if session is None:
            return False
        try:
            valid = bool(session.expires_at and session.expires_at > time.time()
                         and self._store.load() == session)
        except TidalSessionError:
            valid = False
        if not valid:
            self._shared_session = None
        return valid

    async def set_shared(self, enabled: bool):
        async with self._lock:
            self._shared_session = None
            if not enabled:
                return
            try:
                # Refuse while login/refresh owns the store; pin the exact record.
                with self._store.lease() as lease:
                    session = lease.load()
                    if (session is None or session.auth_flow != "tidalapi-device-v0.8.11"
                            or not session.client_id or not session.expires_at
                            or session.expires_at <= time.time() or not session.country_code
                            or not session.user_id or not re.fullmatch(r"[1-9][0-9]{0,18}", session.user_id)
                            or int(session.user_id) > 2**63 - 1):
                        raise TidalAccountUnavailable("A verified device session is required")
                    self._shared_session = session
            except TidalSessionError:
                raise TidalAccountUnavailable("Device account sharing is unavailable") from None

    def _client_fields(self, session, short_uuid):
        if not self.shared or session != self._shared_session:
            raise TidalAccountUnavailable("Device account sharing needs explicit confirmation")
        return {"clientId": encode_account_field(short_uuid, session.client_id)}
