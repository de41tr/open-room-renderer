"""Single-owner refresh of an ordinary tidalapi 0.8.11 device session.

Refresh is explicit or on demand before an expired/near-expiry provider GET.
No refresh happens at import, construction, status reads, or provider failures.
The pinned library supplies its own ordinary client configuration; its refresh
method is deliberately not used because it discards rotated refresh tokens.
No restore/check_login call or library request wrapper can refresh recursively.
"""
from __future__ import annotations

import asyncio
import http.client
from importlib.metadata import version
import json
import math
import re
import time

import aiohttp

from .tidal_session import TidalSession, TidalSessionError, TidalSessionStore


TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
SESSIONS_URL = "https://api.tidal.com/v1/sessions"
_FLOW = "tidalapi-device-v0.8.11"
_MAX_RESPONSE_BYTES = 65536
# Enough for the bounded two-request resolver and ordinary client clock skew.
# This is evaluated only on demand; no timer or background provider request.
REFRESH_SKEW_SECONDS = 30


def refresh_due(session, *, now=None, minimum_validity=REFRESH_SKEW_SECONDS):
    """Whether a known expiry needs renewal before another protected access."""
    return (session.expires_at is not None
            and session.expires_at <= (time.time() if now is None else now) + minimum_validity)


class _Failure(Exception):
    """Only fixed local categories may cross the public result boundary."""


def _library_config():
    try:
        if version("tidalapi") != "0.8.11":
            raise _Failure("dependency_unavailable")
        from tidalapi import Config
        return Config()
    except (ImportError, ModuleNotFoundError):
        raise _Failure("dependency_unavailable") from None


def _no_wire_debug():
    if http.client.HTTPConnection.debuglevel or http.client.HTTPSConnection.debuglevel:
        raise _Failure("unsafe_debug")


def _unique_json(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise _Failure("invalid_response")
        value[key] = item
    return value


async def _request_json(url, *, data=None, token=None):
    # Only a refresh form and a new-token identity check are possible. Never
    # send credentials through a query string, redirect, proxy, or netrc.
    if (url, data is None, token is None) not in {
        (TOKEN_URL, False, True), (SESSIONS_URL, True, False),
    }:
        raise _Failure("refresh_failed")
    _no_wire_debug()
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    async with aiohttp.ClientSession(
        trust_env=False, auto_decompress=False, cookie_jar=aiohttp.DummyCookieJar(),
        timeout=aiohttp.ClientTimeout(total=15),
    ) as http:
        async with http.request(
            "POST" if data is not None else "GET", url, data=data,
            headers=headers, allow_redirects=False, ssl=True,
        ) as response:
            if response.status != 200 and not (data is not None and response.status in {400, 401}):
                raise _Failure("refresh_failed")
            body = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                body.extend(chunk)
                if len(body) > _MAX_RESPONSE_BYTES:
                    raise _Failure("invalid_response")
            try:
                value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_json)
            except (ValueError, UnicodeError, RecursionError):
                raise _Failure("invalid_response") from None
            if type(value) is not dict:
                raise _Failure("invalid_response")
            if response.status != 200:
                raise _Failure("refresh_rejected" if value.get("error") == "invalid_grant"
                               else "refresh_failed")
            return value


def _token(value):
    return type(value) is str and re.fullmatch(r"[\x21-\x7e]{1,16384}", value)


def _identity(value):
    if type(value) is not dict:
        return None
    user = value.get("userId")
    if type(user) is int:
        user = str(user)
    country = value.get("countryCode")
    if (type(user) is str and re.fullmatch(r"[1-9][0-9]{0,18}", user)
            and int(user) <= 2**63 - 1 and type(country) is str
            and re.fullmatch(r"[A-Z]{2}", country)):
        return user, country
    return None


def _replacement(value, old, started_at):
    if type(value) is not dict:
        raise _Failure("invalid_response")
    access = value.get("access_token")
    # Omission retains the existing refresh token. A supplied malformed/null
    # token must not silently fall back and lose a possible provider rotation.
    refresh = value.get("refresh_token", old.refresh_token)
    lifetime = value.get("expires_in")
    if (not _token(access) or not _token(refresh)
            or type(value.get("token_type")) is not str
            or value["token_type"].lower() != "bearer"
            or type(lifetime) not in (int, float)):
        raise _Failure("invalid_response")
    try:
        expires_at = started_at + lifetime
        if not math.isfinite(lifetime) or lifetime <= 0 or not math.isfinite(expires_at):
            raise ValueError
    except (ValueError, OverflowError):
        raise _Failure("invalid_response") from None
    # Optional token-response identity fields must agree too, even if partial.
    if "user" in value:
        user = value["user"]
        expected = {"userId": old.user_id, "countryCode": old.country_code}
        if type(user) is not dict or _identity({**expected, **user}) != _identity(expected):
            raise _Failure("identity_mismatch")
    if "client_id" in value and value["client_id"] != old.client_id:
        raise _Failure("identity_mismatch")
    return TidalSession(access, refresh, expires_at, old.user_id, old.country_code,
                        client_id=old.client_id, auth_flow=old.auth_flow)


class TidalSessionRefreshManager:
    """Two bounded requests, one exclusive lease, and one atomic commit.

    The optional synchronous on_committed(old, new) hook updates existing local
    consent only; it must not publish credentials or make network requests. Save
    and hook have no intervening await. Factories are offline test seams only.

    session_saved means a confirmed commit, not proof that an uncertain failure
    left storage unchanged. After refresh_outcome=uncertain or cancellation,
    callers must not automatically retry: the provider may have rotated a token.
    """

    def __init__(self, store: TidalSessionStore, *, on_committed=None,
                 config_factory=None, request_json=None):
        self._store = store
        self._on_committed = on_committed
        self._config_factory = config_factory or _library_config
        self._request = request_json or _request_json
        self._pending = None
        self._committed = None

    def needs_refresh(self, session, *, now=None, minimum_validity=REFRESH_SKEW_SECONDS):
        """Bound skew by a newly issued token's lifetime to avoid rotation loops.

        A provider can issue a shorter token than the caller's freshness window.
        Remember only this exact committed record; use at least 90% of its known
        lifetime before another automatic refresh. A restart can require one
        renewal, but subsequent reads cannot repeatedly rotate the new grant.
        """
        if self._committed is not None and self._committed[0] == session:
            minimum_validity = min(minimum_validity,
                                   (session.expires_at - self._committed[1]) / 10)
        return refresh_due(session, now=now, minimum_validity=minimum_validity)

    def refresh_in_progress(self, session):
        """Only this owner's exact in-flight record, never an old durable attempt."""
        return self._pending is not None and self._pending[0] == session

    async def wait_for_refresh(self, session):
        """Join an existing attempt without retrying or cancelling its owner."""
        pending = self._pending
        if pending is None or pending[0] != session:
            return False
        await pending[1].wait()
        return True

    def refresh_available(self, session):
        """Read-only availability, including this owner's still-running attempt."""
        return bool(session is not None and session.expires_at is not None
                    and session.auth_flow == _FLOW and session.client_id
                    and _identity({"userId": session.user_id, "countryCode": session.country_code})
                    and _token(session.access_token) and _token(session.refresh_token)
                    and (self.refresh_in_progress(session)
                         or self._store.refresh_outcome(session) is None))

    async def refresh(self, *, timeout=30) -> dict:
        try:
            with self._store.lease() as lease:
                return await self.refresh_owned(lease, timeout=timeout)
        except TidalSessionError:
            return {"state": "error", "session_saved": False, "refresh_outcome": "not_started",
                    "sharing_warning": False, "error": "storage_unavailable"}

    async def refresh_owned(self, lease, *, timeout=30, only_if_expired=False,
                            minimum_validity=REFRESH_SKEW_SECONDS) -> dict:
        """Use the caller's existing lease; re-read before deciding to refresh.

        This internal resolver integration must use this manager's store lease.
        There is no second lock or automatic retry of an attempted grant.
        """
        attempted = committing = saved = warning = False
        outcome = "not_started"
        pending = None
        try:
            if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                    or not 1 <= timeout <= 60):
                raise _Failure("invalid_timeout")
            old = lease.load()
            if (old is None or old.auth_flow != _FLOW or not old.client_id
                    or _identity({"userId": old.user_id, "countryCode": old.country_code}) is None):
                raise _Failure("session_unavailable")
            blocked = lease.refresh_outcome(old)
            if blocked is not None:
                outcome = blocked
                raise _Failure("refresh_rejected" if blocked == "rejected" else "refresh_result_uncertain")
            if only_if_expired:
                if old.expires_at is None:
                    raise _Failure("session_unavailable")
                if not self.needs_refresh(old, minimum_validity=minimum_validity):
                    return {"state": "current", "session_saved": False,
                            "refresh_outcome": "not_started", "sharing_warning": False}
            if not _token(old.refresh_token):
                raise _Failure("session_unavailable")
            config = self._config_factory()
            if (config.client_id != old.client_id or config.api_oauth2_token != TOKEN_URL
                    or config.api_v1_location != "https://api.tidal.com/v1/"
                    or not _token(config.client_secret)):
                raise _Failure("client_mismatch")
            _no_wire_debug()
            deadline = time.monotonic() + timeout
            async with asyncio.timeout(timeout):
                started_at = time.time()
                lease.mark_refresh_attempt(old)
                attempted = True
                pending = self._pending = (old, asyncio.Event())
                outcome = "uncertain"
                value = await self._request(TOKEN_URL, data={
                    "grant_type": "refresh_token", "refresh_token": old.refresh_token,
                    "client_id": config.client_id, "client_secret": config.client_secret,
                })
                new = _replacement(value, old, started_at)
                identity = await self._request(SESSIONS_URL, token=new.access_token)
                if (_identity(identity) != (old.user_id, old.country_code)
                        or ("clientId" in identity and identity["clientId"] != old.client_id)):
                    raise _Failure("identity_mismatch")
                if time.monotonic() >= deadline:
                    raise _Failure("refresh_failed")
                if new.expires_at <= time.time():
                    raise _Failure("invalid_response")
                if lease.load() != old:
                    raise _Failure("storage_unavailable")
                committing = True
                lease.save(new)
                saved, outcome = True, "saved"
                self._committed = (new, started_at)
                if self._on_committed is not None:
                    try:
                        self._on_committed(old, new)
                    except Exception:
                        warning = True
                lease.clear_refresh_attempt()
                return {"state": "refreshed", "session_saved": True,
                        "refresh_outcome": "saved", "sharing_warning": warning}
        except _Failure as error:
            category = error.args[0]
            if category == "refresh_rejected" and attempted:
                outcome = "rejected"
                try:
                    lease.mark_refresh_attempt(old, rejected=True)
                except TidalSessionError:
                    pass  # The already-durable uncertain marker still blocks retries.
        except TidalSessionError:
            category = "commit_failed" if committing else "storage_unavailable"
        except Exception:
            category = "commit_failed" if committing else "refresh_failed"
        finally:
            if pending is not None:
                self._pending = None
                pending[1].set()
        return {"state": "error", "session_saved": saved, "refresh_outcome": outcome,
                "sharing_warning": warning, "error": category}
