"""Operator-run public-client PKCE login, never implicit renderer startup.

Only the operator's independently registered client is accepted. This obtains
OAuth credentials, not playback entitlement. Run on the local browser's Linux
host; session storage and callback traffic remain on that host.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from dataclasses import dataclass
import hashlib
import json
import math
import re
import secrets
import sys
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit
import webbrowser

import aiohttp

from .tidal_account import TidalAccountUnavailable, TidalClientIdentity
from .tidal_session import TidalSession, TidalSessionError, TidalSessionStore

AUTHORIZE_URL = "https://login.tidal.com/authorize"
TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
SESSIONS_URL = "https://api.tidal.com/v1/sessions"
MAX_RESPONSE_BYTES = 65536


class TidalAuthError(ValueError):
    """Fixed, credential-free diagnostics for an unsuccessful login."""


class OAuthIncomplete(TidalAuthError):
    """OAuth succeeded, but identity/storage was not ready for adoption."""


@dataclass(frozen=True, repr=False)
class _Attempt:
    verifier: str
    state: str
    challenge: str

    @classmethod
    def create(cls) -> _Attempt:
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        return cls(verifier, secrets.token_urlsafe(32), challenge.rstrip(b"=").decode("ascii"))


@dataclass(frozen=True)
class LoginResult:
    v1_session_verified: bool = False
    # A saved OAuth session is deliberately never described as playback proof.


def _configuration(client: TidalClientIdentity, scope: str, timeout: float) -> tuple[int, str]:
    try:
        parts = urlsplit(client.redirect_uri)
        port = parts.port
        if (parts.scheme != "http" or parts.hostname != "127.0.0.1"
                or parts.netloc != f"127.0.0.1:{port}" or port is None
                or not 1024 <= port <= 65535 or parts.query or parts.fragment
                or not re.fullmatch(r"/[A-Za-z0-9/_~.-]*", parts.path)
                or any(part in {".", ".."} for part in parts.path.split("/"))):
            raise ValueError
    except ValueError:
        raise TidalAuthError("Register an exact http://127.0.0.1:PORT/path redirect with a port from 1024 to 65535.") from None
    if (type(scope) is not str or len(scope) > 1024
            or not re.fullmatch(r'[\x21\x23-\x5b\x5d-\x7e]+(?: [\x21\x23-\x5b\x5d-\x7e]+)*', scope)):
        raise TidalAuthError("Supply the explicit space-separated scopes permitted for your own client.")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 600:
        raise TidalAuthError("The login timeout must be between 1 and 600 seconds.")
    return port, parts.path


def _authorize_url(client: TidalClientIdentity, scope: str, attempt: _Attempt) -> str:
    return AUTHORIZE_URL + "?" + urlencode({
        "response_type": "code", "client_id": client.client_id,
        "redirect_uri": client.redirect_uri, "scope": scope,
        "code_challenge_method": "S256", "code_challenge": attempt.challenge,
        "state": attempt.state,
    })


class _Callback:
    """Small, short-lived IPv4-loopback receiver with no request logging."""

    def __init__(self, port: int, path: str, state: str):
        self.authority = f"127.0.0.1:{port}"
        self.path = path
        self.state = state
        self.result = asyncio.get_running_loop().create_future()
        self.connections: set[asyncio.Task] = set()

    def _parse(self, raw: bytes) -> str | None:
        if len(raw) > 8192:
            raise ValueError
        lines = raw.decode("ascii").split("\r\n")
        method, target, version = lines[0].split(" ")
        if method != "GET" or version not in {"HTTP/1.0", "HTTP/1.1"} or not target.startswith("/"):
            raise ValueError
        headers = {}
        for line in lines[1:]:
            if not line:
                continue
            name, value = line.split(":", 1)
            name = name.lower()
            if name in headers:
                raise ValueError
            headers[name] = value.strip()
        if (headers.get("host") != self.authority or "transfer-encoding" in headers
                or headers.get("content-length", "0") != "0"):
            raise ValueError
        parts = urlsplit(target)
        if parts.scheme or parts.netloc or parts.path != self.path or parts.fragment:
            raise ValueError
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
        params = dict(pairs)
        state = params.get("state", "")
        if len(params) != len(pairs) or not state.isascii() or not secrets.compare_digest(state, self.state):
            raise ValueError
        if "error" in params and "code" not in params:
            return None
        code = params.get("code", "")
        if "error" in params or not re.fullmatch(r"[\x21-\x7e]{1,4096}", code):
            raise ValueError
        return code

    async def receive(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if len(self.connections) >= 8:
            writer.close()
            return
        self.connections.add(task)
        accepted = False
        code = None
        try:
            async with asyncio.timeout(5):
                try:
                    code = self._parse(await reader.readuntil(b"\r\n\r\n"))
                    accepted = not self.result.done()
                except (ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                    pass
                # Remove OAuth callback parameters from the visible browser URL.
                # No code, state, error description, or requested path is echoed.
                nonce = secrets.token_urlsafe(16)
                body = ("<!doctype html><meta charset=utf-8><title>TIDAL login</title>"
                        f'<script nonce="{nonce}">history.replaceState(null,"",location.pathname)</script>'
                        + ("Login response received. Close this tab and return to the terminal."
                           if accepted else "Invalid login response. Return to the terminal.")).encode("utf-8")
                status = "200 OK" if accepted else "400 Bad Request"
                writer.write((f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\n"
                              f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
                              "Referrer-Policy: no-referrer\r\nX-Content-Type-Options: nosniff\r\n"
                              "X-Frame-Options: DENY\r\n"
                              f"Content-Security-Policy: default-src 'none'; script-src 'nonce-{nonce}'; "
                              "base-uri 'none'; frame-ancestors 'none'\r\nConnection: close\r\n\r\n").encode("ascii") + body)
                await writer.drain()
                if accepted and not self.result.done():
                    if code is None:
                        self.result.set_exception(TidalAuthError("TIDAL authorization was declined or failed; no session was saved."))
                    else:
                        self.result.set_result(code)
        except (OSError, TimeoutError):
            pass
        finally:
            writer.close()
            self.connections.discard(task)

    async def close(self) -> None:
        for task in list(self.connections):
            task.cancel()
        if self.connections:
            await asyncio.gather(*self.connections, return_exceptions=True)
        if not self.result.done():
            self.result.cancel()


async def _open_browser(url: str) -> bool:
    # webbrowser backends can block. A daemon avoids extending the login deadline
    # or asyncio.run's executor shutdown if the local browser launcher hangs.
    loop = asyncio.get_running_loop()
    result = loop.create_future()

    def launch() -> None:
        try:
            opened = webbrowser.open(url, new=1, autoraise=True)
        except Exception:
            opened = False
        def finish() -> None:
            if not result.done():
                result.set_result(bool(opened))
        try:
            loop.call_soon_threadsafe(finish)
        except RuntimeError:
            pass  # The bounded login already ended.

    threading.Thread(target=launch, daemon=True).start()
    return await result


def _unique_json(pairs: list[tuple]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


async def _request_json(url: str, *, data: dict | None = None, token: str | None = None) -> dict:
    # No endpoint overrides, redirects, environment proxies or netrc credentials.
    if (url, data is None, token is None) not in {
        (TOKEN_URL, False, True), (SESSIONS_URL, True, False),
    }:
        raise TidalAuthError("Unsupported TIDAL request.")
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    try:
        async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=15)) as http:
            async with http.request("POST" if data is not None else "GET", url, data=data,
                                    headers=headers, allow_redirects=False) as response:
                if response.status != 200:
                    raise TidalAuthError("TIDAL rejected the request; no session was saved.")
                body = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ValueError
                value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_json)
                if type(value) is not dict:
                    raise ValueError
                return value
    except TidalAuthError:
        raise
    except (aiohttp.ClientError, OSError, TimeoutError):
        raise TidalAuthError("The TIDAL request failed or timed out; no session was saved.") from None
    except (ValueError, UnicodeError, RecursionError):
        raise TidalAuthError("TIDAL returned an invalid response; no session was saved.") from None


def _identity(value: object) -> tuple[str, str] | None:
    if type(value) is not dict:
        return None
    user_id = value.get("userId")
    if type(user_id) is int:
        user_id = str(user_id)
    country = value.get("countryCode")
    if (type(user_id) is str and re.fullmatch(r"[0-9]{1,19}", user_id)
            and 0 < int(user_id) <= 2**63 - 1 and type(country) is str
            and re.fullmatch("[A-Z]{2}", country)):
        return str(int(user_id)), country
    return None


def _token_session(value: dict) -> TidalSession:
    access = value.get("access_token")
    refresh = value.get("refresh_token")
    lifetime = value.get("expires_in")
    try:
        if (type(access) is not str or not re.fullmatch(r"[\x21-\x7e]{1,16384}", access)
                or (refresh is not None and (type(refresh) is not str
                    or not re.fullmatch(r"[\x21-\x7e]{1,16384}", refresh)))
                or type(value.get("token_type")) is not str or value["token_type"].lower() != "bearer"
                or type(lifetime) not in (int, float) or not math.isfinite(lifetime) or lifetime <= 0):
            raise ValueError
        return TidalSession(access, refresh, time.time() + lifetime)
    except (ValueError, OverflowError):
        raise TidalAuthError("TIDAL returned incomplete or invalid OAuth credentials; no session was saved.") from None


async def login(client: TidalClientIdentity, scope: str, store: TidalSessionStore, *,
                timeout: float = 180, probe_v1_session: bool = False,
                no_browser: bool = False, announce=print) -> LoginResult:
    """Save only after a complete grant and identity; failures preserve storage.

    probe_v1_session is operator confirmation that their client is separately
    permitted to use this endpoint. OAuth scopes do not establish permission.
    """
    port, path = _configuration(client, scope, timeout)
    store.load()  # Refuse unsafe existing storage before sending the operator to login.
    attempt = _Attempt.create()
    callback = _Callback(port, path, attempt.state)
    oauth_succeeded = False
    try:
        async with asyncio.timeout(timeout):
            try:
                server = await asyncio.start_server(callback.receive, "127.0.0.1", port,
                                                    limit=8192, backlog=8)
            except OSError:
                raise TidalAuthError("The registered loopback port is unavailable. Run this CLI on the browser's host with that port free.") from None
            try:
                async with server:
                    url = _authorize_url(client, scope, attempt)
                    if no_browser:
                        announce("Open this authorization URL in a browser on this same host:")
                        announce(url)
                    elif not await _open_browser(url):
                        raise TidalAuthError("The local browser could not open. Run on the browser's host, or use --no-browser there; cloud callbacks are not supported.")
                    else:
                        announce("Finish TIDAL login in this host's browser, then return here.")
                    code = await callback.result
            finally:
                await callback.close()
            value = await _request_json(TOKEN_URL, data={
                "grant_type": "authorization_code", "client_id": client.client_id,
                "code": code, "redirect_uri": client.redirect_uri, "code_verifier": attempt.verifier,
            })
            session = _token_session(value)
            oauth_succeeded = True
            identity = _identity(value.get("user"))
            v1_verified = False
            if probe_v1_session:
                try:
                    probed_identity = _identity(await _request_json(SESSIONS_URL, token=session.access_token))
                except TidalAuthError:
                    raise OAuthIncomplete("OAuth succeeded, but the permitted v1 session probe failed. No session was saved; v1 access and full playback remain unproven.") from None
                if probed_identity is None:
                    raise OAuthIncomplete("OAuth succeeded, but the permitted v1 session probe returned incomplete identity. No session was saved; full playback remains unproven.")
                if identity is not None and identity != probed_identity:
                    raise OAuthIncomplete("OAuth succeeded, but token and v1 session identities disagree. No session was saved; full playback remains unproven.")
                identity = probed_identity
                v1_verified = True
            if identity is None:
                raise OAuthIncomplete("OAuth succeeded, but account identity is incomplete. No session was saved. A v1 session probe requires separate client permission and --probe-v1-session; full playback remains unproven.")
            if session.expires_at <= time.time():
                raise OAuthIncomplete("OAuth succeeded, but its token expired before adoption. No session was saved.")
            user_id, country = identity
            try:
                store.save(TidalSession(session.access_token, session.refresh_token, session.expires_at,
                                        user_id, country))
            except TidalSessionError:
                raise OAuthIncomplete("OAuth succeeded, but local session storage failed. Check private storage before retrying; full playback remains unproven.") from None
            return LoginResult(v1_verified)
    except TimeoutError:
        if oauth_succeeded:
            raise OAuthIncomplete("OAuth succeeded, but login timed out before adoption. No session was saved; full playback remains unproven.") from None
        raise TidalAuthError("Login timed out; no session was saved. The browser and CLI must run on the same host.") from None
    finally:
        await callback.close()


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse normally echoes unknown arguments, which could be secrets.
        self.exit(2, "Invalid login arguments. Run with --help; do not pass tokens or client secrets.\n")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="User-run TIDAL PKCE login for your independently registered public client. Run on your browser's Linux host.")
    parser.add_argument("--client-id", required=True, help="your own registered public client ID")
    parser.add_argument("--redirect-uri", required=True, help="exact registered http://127.0.0.1:PORT/path redirect")
    parser.add_argument("--scope", required=True, help="explicit quoted space-separated scopes permitted for your client")
    parser.add_argument("--session-dir", required=True, help="absolute dedicated private directory; credentials are plaintext at rest")
    parser.add_argument("--timeout", type=float, default=180, help="total login deadline in seconds, 1 to 600 (default 180)")
    parser.add_argument("--no-browser", action="store_true", help="print the authorization URL for manual use in a browser on the SAME host")
    parser.add_argument("--probe-v1-session", action="store_true", help="confirm your client is separately permitted to use v1 /sessions and test its identity response")
    args = parser.parse_args(argv)
    try:
        client = TidalClientIdentity(args.client_id, args.redirect_uri)
        store = TidalSessionStore(args.session_dir)
        result = asyncio.run(login(client, args.scope, store, timeout=args.timeout,
                                   no_browser=args.no_browser, probe_v1_session=args.probe_v1_session))
    except OAuthIncomplete as error:
        print(str(error), file=sys.stderr)
        return 2
    except (TidalAuthError, TidalSessionError, TidalAccountUnavailable) as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Login cancelled. No completed adoption was confirmed.", file=sys.stderr)
        return 130
    except Exception:
        print("Login failed. Check local setup; no completed adoption was confirmed.", file=sys.stderr)
        return 1
    print("OAuth session saved privately on this host.")
    print("v1 session identity verified." if result.v1_session_verified else "v1 session access was not tested.")
    print("Full playback, entitlement, XU4 transfer and renderer adoption remain unverified.")
    print("Automatic token renewal is not implemented.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
