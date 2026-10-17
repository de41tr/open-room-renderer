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
    """Explicit sharing of one provenance-bound device session.

    The real device client is projected; no registered redirect is invented.
    The app's already-authenticated branch uses Bearer /v1/sessions. Native
    app OAuth re-login remains unsupported. Remembering consent is a separate
    opt-in; ordinary sharing still ends at restart. No startup network activity.
    """

    def __init__(self, store):
        self._store = store
        self._lock = asyncio.Lock()
        self._shared_session = None
        self._remembered = False
        self._refresh = None
        try:
            session = self._store.load()
            if self._shareable(session) and self._store.sharing_matches(session):
                self._shared_session = session
                self._remembered = True
        except TidalSessionError:
            pass  # Missing/unsafe consent fails closed without blocking startup.

    @staticmethod
    def _shareable(session):
        # Expired remembered consent may be restored only as suspended consent.
        return (session is not None and session.auth_flow == "tidalapi-device-v0.8.11"
                and bool(session.client_id) and bool(session.expires_at) and bool(session.country_code)
                and bool(session.user_id) and re.fullmatch(r"[1-9][0-9]{0,18}", session.user_id)
                and int(session.user_id) <= 2**63 - 1)

    def _bind_refresh_owner(self, owner):
        """Bind only the runtime's exact store owner and consent callback."""
        from .tidal_refresh import TidalSessionRefreshManager
        if (type(owner) is not TidalSessionRefreshManager or owner._store is not self._store
                or owner._on_committed != self._session_refreshed):
            raise TidalAccountUnavailable("Device account refresh ownership is unavailable")
        self._refresh = owner

    async def clear_local_session(self) -> None:
        await super().clear_local_session()
        self._shared_session = None
        self._remembered = False

    async def get_user_info(self, short_uuid: str, *, version: str, now: float | None = None) -> dict:
        if version != "2":
            raise TidalAccountUnavailable("Unsupported TIDAL account contract version")
        checked_at = time.time() if now is None else now
        try:
            if (type(checked_at) not in (int, float) or not math.isfinite(checked_at)
                    or checked_at < 0):
                raise TidalAccountUnavailable("TIDAL account time is unavailable")
            if not self.sharing_requested:
                raise TidalAccountUnavailable("Device account sharing needs explicit confirmation")
            previous = self._shared_session
            # Validate UUID and codec availability before a refresh can consume a
            # grant. This result is discarded; it is never logged or published.
            encode_account_field(short_uuid, previous.client_id)
            if (self._refresh is not None and previous.expires_at is not None
                    and previous.expires_at <= checked_at):
                with self._store.lease() as lease:
                    if lease.load() != previous or self._shared_session != previous:
                        raise TidalAccountUnavailable("Device account sharing needs explicit confirmation")
                    # Do not hold the account lock across network I/O: Unshare
                    # must revoke immediately, including while refresh is pending.
                    result = await self._refresh.refresh_owned(lease, timeout=10, only_if_expired=True)
                    if result["state"] not in {"current", "refreshed"} or not self.sharing_requested:
                        raise TidalAccountUnavailable("Device account refresh or sharing is unavailable")
                now = None  # Projection must use fresh time after network I/O.
            # Existing projection reloads storage and rechecks exact consent,
            # expiry and identity. A stale result can never authorize export.
            return await super().get_user_info(short_uuid, version=version, now=now)
        except (TidalSessionError, TidalCodecUnavailable, ValueError, TypeError, OverflowError):
            raise TidalAccountUnavailable("TIDAL account projection is unavailable") from None

    @property
    def sharing_requested(self):
        """Existing exact-record consent, including a suspended expired record.

        This authorizes only an on-demand resolution attempt, never export of
        expired credentials. Explicit Unshare clears it even during refresh.
        """
        session = self._shared_session
        if session is None:
            return False
        try:
            valid = (self._store.load() == session
                     and (not self._remembered or self._store.sharing_matches(session)))
        except TidalSessionError:
            valid = False
        if not valid:
            self._shared_session = None
            self._remembered = False
        return valid

    @property
    def sharing_remembered(self):
        """Durable status, including an unsuccessful attempt to revoke it."""
        try:
            session = self._store.load()
            return bool(self._shareable(session) and self._store.sharing_matches(session))
        except TidalSessionError:
            return False

    @property
    def shared(self):
        # Expiry suspends projection, but a verified refresh of this exact
        # same owner may renew the existing explicit opt-in.
        return bool(self.sharing_requested and self._shared_session.expires_at
                    and self._shared_session.expires_at > time.time())

    async def set_shared(self, enabled: bool, *, remember: bool = False):
        if type(enabled) is not bool or type(remember) is not bool or remember and not enabled:
            raise TidalAccountUnavailable("TIDAL sharing confirmation is invalid")
        async with self._lock:
            self._shared_session = None
            self._remembered = False
            try:
                if not enabled:
                    self._store.remember_sharing(None)
                    return
                # Refuse while login/refresh owns the store; pin the exact record.
                with self._store.lease() as lease:
                    session = lease.load()
                    if not self._shareable(session) or session.expires_at <= time.time():
                        raise TidalAccountUnavailable("A verified device session is required")
                    self._store.remember_sharing(session if remember else None)
                    self._shared_session = session
                    self._remembered = remember
            except TidalSessionError:
                raise TidalAccountUnavailable("Device account sharing is unavailable") from None

    def _session_refreshed(self, previous, replacement):
        """Carry explicit sharing across one verified same-owner token rotation.

        Called synchronously after the refresh owner's atomic save, without an
        event-loop yield. Arbitrary store replacement still invalidates sharing.
        """
        if self._shared_session != previous:
            return
        identity = lambda record: (record.user_id, record.country_code,
                                   record.client_id, record.auth_flow)
        if (identity(previous) == identity(replacement)
                and replacement.auth_flow == "tidalapi-device-v0.8.11"
                and replacement.expires_at and replacement.expires_at > time.time()):
            if self._remembered:
                # Revoke this projection before committing renewed consent.
                # An error must not keep it active; durable Unshare stays final.
                self._shared_session = None
                self._remembered = False
                if not self._store.renew_sharing(previous, replacement):
                    return
                self._remembered = True
            self._shared_session = replacement
        else:
            self._shared_session = None
            self._remembered = False

    def _client_fields(self, session, short_uuid):
        if not self.shared or session != self._shared_session:
            raise TidalAccountUnavailable("Device account sharing needs explicit confirmation")
        return {"clientId": encode_account_field(short_uuid, session.client_id)}
