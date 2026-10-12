"""Strict authored-playlist files, independent of protocol and runtime state.

Version 1 stores only an authored name, selection, mode and ordered explicit
URLs. It cannot represent resolver provenance, display metadata, history or a
live queue export. URL strings are never normalized. Files may contain query
secrets; callers must not include their contents or filesystem errors in logs.
"""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import re
import stat
import tempfile

MAX_PLAYLIST_BYTES = 1024 * 1024
MAX_PLAYLIST_URLS = 1000
PLAYLIST_MODES = ("normal", "repeat-one", "repeat-all", "shuffle-once", "shuffle-repeat-all")
_FIELDS = {"version", "name", "index", "mode", "urls"}
_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


class PlaylistError(ValueError):
    """An invalid authored document; messages never contain supplied data."""


class PlaylistFileExistsError(FileExistsError):
    """The no-clobber commit found an existing destination."""


class PlaylistFileTypeError(OSError):
    """Only regular files are supported, without final-component symlinks."""


class PlaylistCommittedError(OSError):
    """The file was committed, but complete directory durability is uncertain."""


def _text(value: object) -> str:
    if type(value) is not str or not value.strip() or _INVALID_XML.search(value):
        raise PlaylistError("Playlist text must be nonempty XML-safe strings")
    return value


def validate_playlist(payload: object) -> dict[str, object]:
    """Check the strict v1 file schema and return an independent plain value.

    Protocol-specific URL and escaped SOAP limits are checked by the client
    before either authoring or restoring. This module performs no network I/O.
    """
    if (type(payload) is not dict or set(payload) != _FIELDS
            or type(payload["version"]) is not int or payload["version"] != 1):
        raise PlaylistError("Unsupported authored playlist document")
    name = _text(payload["name"])
    mode = payload["mode"]
    urls = payload["urls"]
    index = payload["index"]
    if type(mode) is not str or mode not in PLAYLIST_MODES:
        raise PlaylistError("Unsupported authored playlist mode")
    if type(urls) is not list or not 1 <= len(urls) <= MAX_PLAYLIST_URLS:
        raise PlaylistError("Playlist must contain between 1 and 1000 URLs")
    if type(index) is not int or not 1 <= index <= len(urls):
        raise PlaylistError("Playlist index must select one authored URL")
    return {"version": 1, "name": name, "index": index, "mode": mode,
            "urls": [_text(url) for url in urls]}


def encode_playlist(payload: object) -> bytes:
    """Return a bounded UTF-8 document without altering URL strings."""
    document = validate_playlist(payload)
    encoded = (json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    if len(encoded) > MAX_PLAYLIST_BYTES:
        raise PlaylistError("Authored playlist exceeds its byte limit")
    return encoded


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PlaylistError("Duplicate authored playlist field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise PlaylistError("Nonfinite authored playlist value")


def decode_playlist(encoded: bytes) -> dict[str, object]:
    """Reject unknown, duplicate, nonfinite, non-UTF-8 or oversized data."""
    if type(encoded) is not bytes or len(encoded) > MAX_PLAYLIST_BYTES:
        raise PlaylistError("Invalid authored playlist bytes")
    try:
        payload = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_object,
                             parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise PlaylistError("Invalid authored playlist document") from None
    return validate_playlist(payload)


def load_playlist(path: str | Path) -> dict[str, object]:
    """Bound reads and reject special files without blocking or following links.

    Final-component symlinks are rejected. The opened descriptor, rather than a
    racy pathname precheck, determines whether the input is a regular file.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PlaylistFileTypeError("Playlist path must be a regular file") from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PlaylistFileTypeError("Playlist path must be a regular file")
        stream = os.fdopen(descriptor, "rb")
        descriptor = None
        with stream:
            encoded = stream.read(MAX_PLAYLIST_BYTES + 1)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return decode_playlist(encoded)


def _check_overwrite_target(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode):
        raise PlaylistFileTypeError("Playlist path must be a regular file")


def save_playlist(path: str | Path, payload: object, *, overwrite: bool = False) -> None:
    """Commit a private file atomically, preserving existing files by default.

    The parent directory must exist. Overwrite accepts only an absent path or
    a regular file, rejecting known symlinks and special files. Path replacement
    between the final check and rename cannot be prevented; use a private parent
    directory when other writers are untrusted. A same-directory 0600 temporary is flushed
    and fsynced before a race-safe link (no overwrite) or explicit replacement.
    Precommit failures preserve the old destination. Once committed, any later
    failure is identified separately so callers must not imply nothing changed.
    """
    encoded = encode_playlist(payload)
    if type(overwrite) is not bool:
        raise PlaylistError("Overwrite must be boolean")
    path = Path(path)
    if overwrite:
        _check_overwrite_target(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".orr-playlist-", dir=path.parent)
    committed = False
    temporary_exists = True
    try:
        os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "wb")
        descriptor = None
        with stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            _check_overwrite_target(path)
            os.replace(temporary, path)
            temporary_exists = False
            committed = True
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise PlaylistFileExistsError("Authored playlist destination exists") from None
            committed = True
            os.unlink(temporary)
            temporary_exists = False
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        if committed:
            raise PlaylistCommittedError("Playlist saved; directory durability unconfirmed") from None
        raise
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_exists:
            # Cleanup must never mask either the primary failure or a commit.
            try:
                os.unlink(temporary)
            except OSError:
                pass
