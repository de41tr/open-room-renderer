"""Small one-shot requests over an established, owned IPv4 connection.

Only the receiver calls this after validating its pinned configuration and
service invocation. No authentication, redirects or reconnects. Explicit play
can activate an already paired, playback-ready session; replacing playing or
paused content requires a separate explicit opt-in.
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

# Pinned v0.10.3 metadata.proto AudioFile.Format names, not playback support.
_FILE_FORMATS = frozenset({
    "OGG_VORBIS_96", "OGG_VORBIS_160", "OGG_VORBIS_320", "MP3_96", "MP3_160",
    "MP3_256", "MP3_320", "MP3_160_ENC", "AAC_24", "AAC_48", "XHE_AAC_12",
    "XHE_AAC_16", "XHE_AAC_24", "FLAC_FLAC", "FLAC_FLAC_24BIT",
})


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


def _status(connection: http.client.HTTPConnection, *, timing: bool = False,
            require_track: bool = False) -> dict:
    code, encoded = _request(connection, "GET", "/status")
    if code == 204:
        return {"session_observation": "NO_SESSION", "status": None}
    value = _json_object(encoded)
    status = _project_status(value)
    if require_track and not isinstance(value.get("track"), dict):
        _fail("Spotify must report a current track; no enqueue request was sent.")
    if timing:
        # Seek needs only numeric timing, never names, URIs or artwork.
        track = value.get("track")
        if track is not None and not isinstance(track, dict):
            _fail("Spotify track timing could not be interpreted.")
        for field in ("duration", "position"):
            number = track.get(field) if track is not None else None
            if number is not None and (type(number) is not int or not 0 <= number <= 2**63 - 1):
                _fail("Spotify track timing could not be interpreted.")
            status[field + "_ms"] = number
    return {"session_observation": "SESSION_PRESENT", "status": status}


def _project_track(value: dict) -> dict:
    """Explicit now-playing only: project v0.10.3 track fields, never URIs/artwork."""
    track = value.get("track")
    if track is None:
        return {"track_observation": "NO_TRACK" if "track" in value else "UNKNOWN", "track": None}
    if not isinstance(track, dict):
        _fail("Spotify now-playing metadata could not be interpreted.")
    result = {}
    for source, target in (("name", "title"), ("album_name", "album")):
        text = track.get(source)
        if text is not None and not isinstance(text, str):
            _fail("Spotify now-playing metadata could not be interpreted.")
        result[target] = text or None
    artists = track.get("artist_names")
    if artists is not None and (not isinstance(artists, list) or
                                any(not isinstance(artist, str) for artist in artists)):
        _fail("Spotify now-playing metadata could not be interpreted.")
    result["artists"] = artists
    for source, target in (("duration", "duration_ms"), ("position", "position_ms")):
        number = track.get(source)
        if number is not None and (type(number) is not int or not 0 <= number <= 2**63 - 1):
            _fail("Spotify now-playing metadata could not be interpreted.")
        result[target] = number
    # api-spec.yml and daemon/api_server.go:249-278 at 4dbc099f46da3529adaa85feb97c4f49deb0b130.
    # The decoded file and decoder's report, never negotiated ALSA/DAC output.
    decoded_file = {}
    for field, allowed in (("format", _FILE_FORMATS),
                           ("codec", {"vorbis", "flac", "mp3", "aac", "unknown"})):
        text = track.get(field)
        if text is not None and (type(text) is not str or (text and text not in allowed)):
            _fail("Spotify now-playing audio format could not be interpreted.")
        decoded_file[field] = text or None
    for source, target in (("bitrate", "nominal_bitrate_kbps"),
                           ("sample_rate", "decoder_sample_rate_hz"),
                           ("bit_depth", "source_bit_depth_bits")):
        number = track.get(source)
        # Upstream emits only positive values; Stream's rate/depth are int32.
        # Keep the same portable bound for its Go int nominal bitrate too.
        if number is not None and (type(number) is not int or not 1 <= number <= 2**31 - 1):
            _fail("Spotify now-playing audio format could not be interpreted.")
        decoded_file[target] = number
    result["decoded_file"] = decoded_file
    return {"track_observation": "PRESENT", "track": result}


def now_playing(host: str, port: int, pid: str, validate_invocation: Callable[[], None]) -> dict:
    """One owned GET; return metadata only after the service fence still holds."""
    try:
        with _owned_connection(host, port, pid) as connection:
            validate_invocation()
            code, encoded = _request(connection, "GET", "/status")
            if code == 204:
                observed = {"session_observation": "NO_SESSION", "status": None,
                            "track_observation": "NO_TRACK", "track": None}
            else:
                value = _json_object(encoded)
                observed = {"session_observation": "SESSION_PRESENT", "status": _project_status(value),
                            **_project_track(value)}
            validate_invocation()
        return {"command": "now-playing", "observation": "sidecar_reported",
                "listener_ownership": "owned_connection", "snapshot_freshness": "snapshot_only",
                "audio_output": "UNVERIFIED", **observed}
    except (OSError, http.client.HTTPException, SpotifyDiagnosticError):
        _fail("Spotify now-playing could not be read; no playback command was sent.")


def control(host: str, port: int, pid: str, action: str, value: int | bool | str | None,
            validate_invocation: Callable[[], None], *, replace_current: bool = False) -> dict:
    if type(replace_current) is not bool or (replace_current and action != "play"):
        _fail("Spotify replace-current is a boolean opt-in for play only.")
    option = {"repeat-context": "repeat_context", "repeat-track": "repeat_track",
              "shuffle": "shuffle_context"}.get(action)
    if action not in {"play", "enqueue", "pause", "resume", "stop-playback", "next", "previous", "volume", "seek"} and option is None:
        _fail("Use Spotify play, enqueue, pause, resume, stop-playback, next, previous, volume, seek, repeat-context, repeat-track or shuffle.")
    if action == "play":
        # A deliberately narrow content selector, never URLs, user collections,
        # local files, encoded delimiters, credentials or a new queue model.
        if (type(value) is not str or len(value) > 39
                or not re.fullmatch(r"spotify:(?:track|album|playlist):[A-Za-z0-9]{22}", value)):
            _fail("Use one canonical Spotify track, album or playlist URI with a 22-character base62 ID.")
    elif action == "enqueue":
        if (type(value) is not str or len(value) != 36
                or not re.fullmatch(r"spotify:track:[A-Za-z0-9]{22}", value)):
            _fail("Use one canonical Spotify track URI with a 22-character base62 ID.")
    elif option is not None:
        if type(value) is not bool:
            _fail("Spotify repeat-context, repeat-track and shuffle require explicit on or off.")
    elif action in {"volume", "seek"}:
        maximum = 100 if action == "volume" else 2**63 - 1
        if type(value) is not int or not 0 <= value <= maximum:
            _fail("Use an absolute Spotify volume from 0 to 100 or a nonnegative int64 seek position in milliseconds.")
    elif value is not None:
        _fail("Spotify pause, resume, stop-playback, next and previous take no value.")
    result = {"command": action, "request_outcome": "NOT_SENT", "provider_before": None,
              "provider_after": None, "observation": "sidecar_reported", "audio_output": "UNVERIFIED",
              "playback_effect": "UNVERIFIED"}
    if action == "play":
        result["requested_content_type"] = value.split(":")[1]
        result["replace_current"] = replace_current
    elif action == "enqueue":
        result["requested_content_type"] = "track"
        result["queue_effect"] = "UNVERIFIED"
    elif action == "volume":
        result["requested_volume"] = value
    elif action == "seek":
        result["requested_position_ms"] = value
    elif option is not None:
        result["requested_" + option] = value
    try:
        with _owned_connection(host, port, pid) as connection:
            validate_invocation()
            if action in {"play", "enqueue"}:
                code, encoded = _request(connection, "GET", "/")
                if code != 200 or _json_object(encoded).get("playback_ready") is not True:
                    _fail(f"Spotify playback readiness was not confirmed; no {action} request was sent.")
                result["provider_playback_ready"] = True
            result["provider_before"] = _status(connection, timing=action == "seek", require_track=action == "enqueue")
            before = result["provider_before"]["status"]
            if before is None:
                _fail("Spotify has no session; no control was sent. Pair and select it yourself first.")
            if before["volume_steps"] != 100 or before["volume"] > 100:
                _fail("Spotify volume does not match the pinned receiver configuration; no control was sent.")
            if action == "play":
                if before["buffering"]:
                    _fail("Spotify must report a non-buffering session; no play request was sent.")
                if not before["stopped"] and not replace_current:
                    _fail("Spotify must report a stopped session; use play --replace-current to replace playing or paused content.")
            elif action not in {"volume", "stop-playback"} and before["stopped"]:
                _fail("Spotify reports no active playback; no control was sent.")
            # Stop also cancels an owned load and resets an inactive session.
            # It must not depend on playback readiness or non-buffering state.
            if action == "enqueue" and before["buffering"]:
                # Upstream can acknowledge a missing/replaced context. Avoid
                # observed loads, but a racing controller can still replace it.
                _fail("Spotify must report a non-buffering session; no enqueue request was sent.")
            if action == "seek":
                duration = before["duration_ms"]
                if duration is None or duration <= 0:
                    _fail("Spotify track duration is unknown; no seek was sent.")
                if value > duration:
                    _fail("Spotify seek exceeds the reported track duration; no seek was sent.")
            validate_invocation()
            result["request_outcome"] = "UNCONFIRMED"
            # v0.10.3 accepts a bodyless next/prev; never pass a content URI.
            payload = ({"volume" if action == "volume" else "position": value, "relative": False}
                       if action in {"volume", "seek"} else None)
            if option is not None:
                payload = {option: value}
            elif action == "play":
                # Pinned v0.10.3 resolves before taking over the device; its
                # acknowledgement does not wait for the subsequent audio load.
                payload = {"uri": value}
            elif action == "enqueue":
                # This appends to upstream's existing queue asynchronously;
                # a 200 does not prove that the track was actually added.
                payload = {"uri": value}
            endpoint = option or {"previous": "prev", "enqueue": "add_to_queue",
                                  "stop-playback": "stop"}.get(action, action)
            code, encoded = _request(connection, "POST", "/player/" + endpoint, payload)
            # Pinned stop replies (nil, nil), encoded as JSON null. A 204
            # means no session, not successful cancellation or a logout ACK.
            if code != 200 or (action == "stop-playback" and encoded.strip(b" \t\r\n") != b"null"):
                _fail("Spotify did not acknowledge the control.")
            result["request_outcome"] = "ACKNOWLEDGED"
            result["provider_after"] = _status(connection, timing=action == "seek")
            validate_invocation()
    except (OSError, http.client.HTTPException, SpotifyDiagnosticError, ExclusiveProfileError) as exc:
        if result["request_outcome"] == "NOT_SENT":
            if isinstance(exc, ExclusiveProfileError):
                raise
            _fail("Spotify API ownership or status could not be read; no control was sent.")
        if action == "enqueue":
            result["error"] = ("Spotify enqueue or subsequent state was not confirmed. "
                               "It may have added the track; inspect the queue in Spotify before retrying to avoid duplicates.")
        else:
            result["error"] = ("Spotify control or subsequent state was not confirmed. "
                               "It may have taken effect; inspect status --playback or now-playing before retrying.")
    return result
