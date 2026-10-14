"""Small one-shot controls over an established, owned IPv4 connection.

Only the receiver calls this after validating its pinned configuration and
service invocation. No authentication, activation, redirects or reconnects.
"""
from __future__ import annotations

from contextlib import contextmanager
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
from typing import Callable

from .spotify_diagnostic import (MAX_BODY_BYTES, SpotifyDiagnosticError,
                                 _endpoint, _json_object, _project_status)
from .spotify_exclusive import ExclusiveProfileError

HTTP_DEADLINE_SECONDS = 12


class _DeadlineSocket(socket.socket):
    """Keep http.client's buffered header/body reads inside one deadline."""

    def __init__(self):
        super().__init__(socket.AF_INET, socket.SOCK_STREAM)
        self.deadline = time.monotonic() + HTTP_DEADLINE_SECONDS
        self._remaining()

    def _remaining(self) -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        self.settimeout(min(4, remaining))

    def recv_into(self, buffer, nbytes=0, flags=0):
        self._remaining()
        return super().recv_into(buffer, nbytes, flags)

    def sendall(self, data, flags=0):
        self._remaining()
        return super().sendall(data, flags)


def _fail(message: str) -> None:
    raise ExclusiveProfileError(message)


@contextmanager
def _owned_connection(host: str, port: int, pid: str):
    _endpoint(host, port)
    if ipaddress.ip_address(host).version != 4 or not re.fullmatch(r"[1-9][0-9]{0,19}", pid):
        _fail("Spotify controls require the configured IPv4 loopback and an owned process.")
    connection = http.client.HTTPConnection(host, port, timeout=4)
    connection.auto_open = 0  # Never reconnect to a possibly replaced listener.
    try:
        connection.sock = _DeadlineSocket()
        connection.sock.connect((host, port))  # Numeric IPv4; no HTTP bytes yet.

        def address(endpoint: tuple[str, int]) -> str:
            host, port = endpoint
            return f"{int.from_bytes(socket.inet_aton(host), sys.byteorder):08X}:{port:04X}"

        local = address(connection.sock.getpeername())
        remote = address(connection.sock.getsockname())
        process = Path("/proc") / pid
        if process.stat().st_uid != os.geteuid():
            _fail("Spotify API connection ownership was not confirmed; no command was sent.")
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            # /proc/net/tcp includes the accepted server socket even before its
            # application has read a request. Match its exact reverse four-tuple,
            # then require that inode in this owned client's descriptor set.
            rows = (process / "net/tcp").read_text().splitlines()[1:]
            inodes = {row[9] for line in rows if len(row := line.split()) >= 10
                      and row[1:4] == [local, remote, "01"] and row[7] == str(os.geteuid())}
            if len(inodes) == 1:
                wanted = f"socket:[{next(iter(inodes))}]"
                for descriptor in (process / "fd").iterdir():
                    try:
                        target = os.readlink(descriptor)
                    except FileNotFoundError:
                        continue  # An unrelated descriptor can close during this read.
                    if target == wanted:
                        yield connection
                        return
            time.sleep(.02)
        _fail("Spotify API connection ownership was not confirmed; no command was sent.")
    finally:
        connection.close()


def _request(connection: http.client.HTTPConnection, method: str, path: str,
             payload: dict | None = None) -> tuple[int, bytes]:
    body = None if payload is None else json.dumps(payload).encode("ascii")
    connection.request(method, path, body=body,
                       headers={"Accept-Encoding": "identity", "Content-Type": "application/json"})
    response = connection.getresponse()
    try:
        if response.status not in {200, 204} or response.getheader("Content-Encoding", "identity") != "identity":
            _fail("Spotify API response was not confirmed.")
        encoded = response.read(MAX_BODY_BYTES + 1)
        if len(encoded) > MAX_BODY_BYTES:
            _fail("Spotify API response was not confirmed.")
        return response.status, encoded
    finally:
        response.close()


def _status(connection: http.client.HTTPConnection) -> dict:
    code, encoded = _request(connection, "GET", "/status")
    return {"session_observation": "NO_SESSION" if code == 204 else "SESSION_PRESENT",
            "status": None if code == 204 else _project_status(_json_object(encoded))}


def control(host: str, port: int, pid: str, action: str, value: int | None,
            validate_invocation: Callable[[], None]) -> dict:
    if action not in {"pause", "resume", "volume"} or (action == "volume" and
            (type(value) is not int or not 0 <= value <= 100)) or (action != "volume" and value is not None):
        _fail("Use Spotify pause, resume or an absolute volume from 0 to 100.")
    result = {"command": action, "request_outcome": "NOT_SENT", "provider_before": None,
              "provider_after": None, "observation": "sidecar_reported", "audio_output": "UNVERIFIED"}
    if action == "volume":
        result["requested_volume"] = value
    try:
        with _owned_connection(host, port, pid) as connection:
            validate_invocation()
            result["provider_before"] = _status(connection)
            before = result["provider_before"]["status"]
            if before is None:
                _fail("Spotify has no session; no control was sent. Pair and select it yourself first.")
            if before["volume_steps"] != 100 or before["volume"] > 100:
                _fail("Spotify volume does not match the pinned receiver configuration; no control was sent.")
            if action != "volume" and before["stopped"]:
                _fail("Spotify reports no active playback; no pause or resume was sent.")
            validate_invocation()
            result["request_outcome"] = "UNCONFIRMED"
            code, _ = _request(connection, "POST", "/player/" + action,
                               {"volume": value, "relative": False} if action == "volume" else None)
            if code != 200:
                _fail("Spotify did not acknowledge the control.")
            result["request_outcome"] = "ACKNOWLEDGED"
            result["provider_after"] = _status(connection)
            validate_invocation()
    except (OSError, http.client.HTTPException, SpotifyDiagnosticError, ExclusiveProfileError) as exc:
        if result["request_outcome"] == "NOT_SENT":
            if isinstance(exc, ExclusiveProfileError):
                raise
            _fail("Spotify API ownership or status could not be read; no control was sent.")
        result["error"] = ("Spotify control or subsequent state was not confirmed. "
                           "It may have taken effect; inspect status --playback before retrying.")
    return result
