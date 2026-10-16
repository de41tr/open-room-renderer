"""Opt-in experiment using tidalapi 0.8.11's ordinary device-login API.

No library client credentials are copied here. Importing this module, creating a
manager and reading status perform no login or network requests. A connected
state means a checked session was saved, never that playback is available.

Upstream login_oauth() starts its own polling Future. Cancellation cannot stop
an already-running upstream thread: the worker keeps its exclusive storage lease
until that Future really ends, and discards any result after cancellation. close()
requests cancellation without blocking application shutdown on that polling loop;
Python's upstream ThreadPoolExecutor can still delay interpreter exit.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future, InvalidStateError
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import http.client
from importlib.metadata import version
import logging
import math
import re
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

from .tidal_session import TidalSession, TidalSessionError, TidalSessionStore


_MESSAGES = {
    "dependency_unavailable": "Install the experimental tidalapi==0.8.11 dependency to use device login.",
    "session_exists": "A TIDAL session is already stored. Remove it explicitly before starting another login.",
    "storage_unavailable": "Private TIDAL storage is unavailable or already in use.",
    "invalid_challenge": "TIDAL returned an unsupported device-login challenge.",
    "login_failed": "TIDAL device login failed; no new session was saved.",
    "expired": "The TIDAL login code expired; no new session was saved.",
    "unsafe_debug": "Disable HTTP wire debugging before starting TIDAL device login.",
}


class _Failure(Exception):
    """Internal errors contain fixed keys, never provider exception text."""


class _DropLibraryLog(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return False


@contextmanager
def _quiet_library_logs():
    # These upstream loggers include raw bodies, exception text and session URLs.
    # Logger filters are needed: filtering a parent logger misses child records.
    quiet = _DropLibraryLog()
    loggers = [logging.getLogger(name) for name in ("tidalapi.session", "tidalapi.request")]
    for logger in loggers:
        logger.addFilter(quiet)
    try:
        yield
    finally:
        for logger in loggers:
            logger.removeFilter(quiet)


def _library_session():
    """Lazy, pinned public package API with bounded, quiet HTTPS transport."""
    try:
        if version("tidalapi") != "0.8.11":
            raise _Failure("dependency_unavailable")
        from tidalapi import Config, Session
        from requests.adapters import HTTPAdapter
    except (ImportError, ModuleNotFoundError):
        raise _Failure("dependency_unavailable") from None

    class QuietTransport(logging.Filter):
        def __init__(self):
            super().__init__()
            self.local = threading.local()

        def filter(self, record):
            return not getattr(self.local, "active", False)

    class PrivateAdapter(HTTPAdapter):
        def __init__(self):
            super().__init__(max_retries=0)
            self.quiet = QuietTransport()
            self.loggers = [logging.getLogger(name) for name in (
                "urllib3.connectionpool", "urllib3.util.retry",
                "requests.packages.urllib3.connectionpool", "requests.packages.urllib3.util.retry",
            )]
            for logger in self.loggers:
                logger.addFilter(self.quiet)

        def send(self, request, **kwargs):
            parts = urlsplit(request.url)
            if (parts.scheme != "https" or parts.netloc not in {"auth.tidal.com", "api.tidal.com"}
                    or parts.fragment):
                raise _Failure("login_failed")
            if http.client.HTTPConnection.debuglevel or http.client.HTTPSConnection.debuglevel:
                raise _Failure("unsafe_debug")
            kwargs["timeout"] = (5, 15)
            kwargs["verify"] = True
            self.quiet.local.active = True
            try:
                return super().send(request, **kwargs)
            finally:
                self.quiet.local.active = False

        def close(self):
            try:
                super().close()
            finally:
                for logger in self.loggers:
                    logger.removeFilter(self.quiet)

    session = Session(Config())
    session.request_session.trust_env = False
    adapter = PrivateAdapter()
    session.request_session.mount("https://", adapter)
    session.request_session.mount("http://", adapter)  # Reject downgrade redirects too.
    return session


def _challenge(login) -> dict:
    code = login.user_code
    raw = login.verification_uri_complete
    expires = login.expires_in
    interval = login.interval
    if (type(code) is not str or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", code)
            or type(raw) is not str
            or raw not in {f"link.tidal.com/{code}", f"https://link.tidal.com/{code}"}
            or type(expires) not in (int, float) or not math.isfinite(expires)
            or not 1 <= expires <= 3600
            or type(interval) not in (int, float) or not math.isfinite(interval)
            or not 1 <= interval <= 60):
        raise _Failure("invalid_challenge")
    # Exact upstream documented link host/path; no arbitrary query, fragment,
    # credentials, subdomain, port or redirect destination can reach the UI.
    return {"verification_uri": f"https://link.tidal.com/{code}", "user_code": code,
            "expires_in": float(expires), "expires_at": time.time() + expires}


def _saved_session(session) -> TidalSession:
    expiry = session.expiry_time
    if not isinstance(expiry, datetime):
        raise _Failure("login_failed")
    # 0.8.11 uses datetime.utcnow(), so naive values are UTC, not host local time.
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    expires_at = expiry.timestamp()
    if not math.isfinite(expires_at) or expires_at <= time.time():
        raise _Failure("login_failed")
    identifier = session.user.id
    if type(identifier) is int:
        identifier = str(identifier)
    if (type(identifier) is not str or not re.fullmatch(r"[1-9][0-9]{0,18}", identifier)
            or int(identifier) > 2**63 - 1):
        raise _Failure("login_failed")
    return TidalSession(session.access_token, session.refresh_token,
                        expires_at, identifier, session.country_code)


@dataclass(repr=False)
class _Attempt:
    ready: Future = field(default_factory=Future)
    stop: threading.Event = field(default_factory=threading.Event)
    commit: threading.Lock = field(default_factory=threading.Lock)
    saved: bool = False
    deadline: float | None = None


class TidalDeviceLoginManager:
    """One explicit login at a time; safe dictionaries for a private web UI.

    All public methods are async. start() returns after obtaining the challenge,
    not after authorization. Repeated start() is idempotent while work is active.
    cancel()/close() acknowledge local cancellation; active stays true until the
    upstream worker drains. An existing session is never automatically replaced.
    session_factory is an offline test seam, not a configuration/client override.
    """

    def __init__(self, store: TidalSessionStore, *, session_factory: Callable | None = None):
        self._store = store
        self._factory = session_factory or _library_session
        self._api_lock = asyncio.Lock()
        self._state_lock = threading.Lock()
        self._attempt: _Attempt | None = None
        self._closed = False
        self._retry_at = 0.0
        self._public = {"state": "idle", "active": False, "session_saved": False}

    def _snapshot(self) -> dict:
        with self._state_lock:
            result = dict(self._public)
            attempt = self._attempt
            retry_after = self._retry_at - time.monotonic()
        if not result["active"] and result["state"] != "closed" and retry_after > 0:
            result["retry_after"] = math.ceil(retry_after)
        if (result["state"] == "pending" and attempt is not None
                and attempt.deadline is not None and time.monotonic() >= attempt.deadline):
            result = {"state": "expired", "active": result["active"], "session_saved": False,
                      "error": "expired", "message": _MESSAGES["expired"]}
        return result

    def _publish(self, attempt: _Attempt, state: str, **extra) -> dict:
        with self._state_lock:
            if self._attempt is not attempt:
                return dict(self._public)
            self._public = {"state": state, "active": True,
                            "session_saved": attempt.saved, **extra}
            return dict(self._public)

    def _ready(self, attempt: _Attempt) -> None:
        try:
            attempt.ready.set_result(self._snapshot())
        except InvalidStateError:
            pass  # Cancellation and the worker may both acknowledge readiness.

    async def status(self) -> dict:
        """Return a fresh safe snapshot; no account lookup or token refresh."""
        return self._snapshot()

    async def start(self) -> dict:
        async with self._api_lock:
            current = self._snapshot()
            if self._closed or current["active"] or current.get("retry_after", 0):
                return current
            attempt = _Attempt()
            self._attempt = attempt
            self._publish(attempt, "starting")
            threading.Thread(target=self._run, args=(attempt,),
                             name="tidal-device-login", daemon=True).start()
        try:
            return await asyncio.shield(asyncio.wrap_future(attempt.ready))
        except asyncio.CancelledError:
            # A disconnected HTTP request must not leave an adoptable login.
            await asyncio.shield(asyncio.to_thread(self._cancel_attempt, attempt))
            raise

    def _cancel_attempt(self, attempt: _Attempt) -> dict:
        # Serialize with the final write. If adoption already committed, report
        # that truthfully; cancellation does not delete a saved session.
        with attempt.commit:
            if self._attempt is not attempt:
                return self._snapshot()
            if not attempt.saved:
                attempt.stop.set()
                if self._snapshot()["active"]:
                    self._publish(attempt, "cancelling")
                self._ready(attempt)
        return self._snapshot()

    async def cancel(self) -> dict:
        async with self._api_lock:
            if self._attempt is not None:
                return await asyncio.to_thread(self._cancel_attempt, self._attempt)
            return self._snapshot()

    async def close(self) -> dict:
        """Prevent future login and discard pending results; do not revoke tokens."""
        async with self._api_lock:
            self._closed = True
            if self._attempt is not None:
                await asyncio.to_thread(self._cancel_attempt, self._attempt)
            with self._state_lock:
                if not self._public["active"]:
                    self._public = {"state": "closed", "active": False,
                                    "session_saved": self._public["session_saved"]}
            return self._snapshot()

    def _run(self, attempt: _Attempt) -> None:
        session = polling = None
        with ExitStack() as stack:
            try:
                lease = stack.enter_context(self._store.lease())
                if lease.load() is not None:
                    raise _Failure("session_exists")
                if attempt.stop.is_set():
                    return
                stack.enter_context(_quiet_library_logs())
                session = self._factory()
                if attempt.stop.is_set():
                    return
                login, polling = session.login_oauth()
                challenge = _challenge(login)
                attempt.deadline = time.monotonic() + challenge["expires_in"]
                with attempt.commit:
                    if not attempt.stop.is_set():
                        self._publish(attempt, "pending", **challenge)
                    self._ready(attempt)
                if attempt.stop.is_set():
                    polling.cancel()
                # This is the only polling owner: never call process_link_login
                # or run an additional OAuth polling loop in the application.
                result = polling.result()
                if attempt.stop.is_set():
                    return
                if time.monotonic() >= attempt.deadline:
                    raise _Failure("expired")
                if result is not True or session.check_login() is not True:
                    raise _Failure("login_failed")
                record = _saved_session(session)
                with attempt.commit:
                    if not attempt.stop.is_set():
                        lease.save(record)
                        attempt.saved = True
                        self._publish(attempt, "connected")
            except _Failure as error:
                key = error.args[0]
                self._publish(attempt, "expired" if key == "expired" else "error",
                              error=key, message=_MESSAGES[key])
                self._ready(attempt)
            except TimeoutError:
                self._publish(attempt, "expired", error="expired", message=_MESSAGES["expired"])
                self._ready(attempt)
            except TidalSessionError:
                self._publish(attempt, "error", error="storage_unavailable",
                              message=_MESSAGES["storage_unavailable"])
                self._ready(attempt)
            except Exception:
                self._publish(attempt, "error", error="login_failed", message=_MESSAGES["login_failed"])
                self._ready(attempt)
            finally:
                # Even invalid challenges and cancelled HTTP handlers retain the
                # lease and log guards until the upstream future actually ends.
                if polling is not None:
                    polling.cancel()
                    try:
                        polling.result()
                    except BaseException:
                        pass
                if session is not None:
                    try:
                        session.request_session.close()
                    except Exception:
                        pass
                # Run ExitStack cleanup before publishing inactive. No new login
                # can race an old lease or still-running package worker.
                stack.close()
                with self._state_lock:
                    if not attempt.saved:
                        self._retry_at = time.monotonic() + 5
                    if self._closed:
                        state = "closed"
                    elif attempt.stop.is_set() and not attempt.saved:
                        state = "cancelled"
                    else:
                        state = self._public["state"]
                    self._public = {**self._public, "state": state, "active": False}
                    if state in {"cancelled", "closed"}:
                        self._public = {"state": state, "active": False, "session_saved": attempt.saved}
                self._ready(attempt)
