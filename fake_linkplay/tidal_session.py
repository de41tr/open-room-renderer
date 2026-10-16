"""Private, offline TIDAL session persistence; this does not enable a provider.

Callers must explicitly select a dedicated directory outside release, renderer
configuration, and favorites storage. There is no default path, login, token
refresh, network access, or claim that a saved session is accepted by TIDAL.
Tokens are plaintext on disk protected by Unix ownership and permissions.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
import fcntl
import json
import math
import os
from pathlib import Path
import secrets
import stat

MAX_SESSION_BYTES = 65536
_TOKEN_LIMIT = 16384
_SESSION_FILE = "session.json"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class TidalSessionError(ValueError):
    """Fixed diagnostics that never include token contents or supplied paths."""


def _text(value: object, limit: int) -> bool:
    return type(value) is str and 0 < len(value) <= limit and bool(value.strip())


def _expiry(value: object) -> bool:
    if value is None:
        return True
    if type(value) not in (int, float):
        return False
    try:
        return value >= 0 and math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True)
class TidalSession:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: float | None = None  # Unix seconds; None means unknown, not valid forever.
    user_id: str | None = field(default=None, repr=False)
    country_code: str | None = None

    def __post_init__(self) -> None:
        if not _text(self.access_token, _TOKEN_LIMIT):
            raise TidalSessionError("A nonempty, bounded TIDAL access token is required.")
        if self.refresh_token is not None and not _text(self.refresh_token, _TOKEN_LIMIT):
            raise TidalSessionError("The TIDAL refresh token is invalid.")
        if not _expiry(self.expires_at):
            raise TidalSessionError("The TIDAL session expiry is invalid.")
        if self.user_id is not None and not _text(self.user_id, 256):
            raise TidalSessionError("The TIDAL user identifier is invalid.")
        if self.country_code is not None and (
            type(self.country_code) is not str or len(self.country_code) != 2
            or any(c < "A" or c > "Z" for c in self.country_code)
        ):
            raise TidalSessionError("The TIDAL country code is invalid.")


def _unique_object(pairs: list[tuple]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise TidalSessionError("Stored TIDAL session fields must be unique.")
        value[key] = item
    return value


def _file_info(info: os.stat_result) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise TidalSessionError("TIDAL session storage must be a private, owned regular file.")


def _fingerprint(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class TidalSessionStore:
    """Store one session in an explicitly supplied private Linux directory.

    Save or lease creates the final directory; its ancestors must already exist.
    Existing unsafe files/directories are rejected, never permission-repaired.
    load returns None only for missing storage, and raises for invalid storage.
    clear removes the stored session only; it does not revoke provider tokens.
    """

    def __init__(self, directory: str | Path):
        if not isinstance(directory, (str, Path)):
            raise TidalSessionError("Use an explicit absolute private TIDAL directory.")
        text = str(directory)
        self.directory = Path(text)
        if (not self.directory.is_absolute() or self.directory == Path("/")
                or text.startswith("//") or text != str(self.directory)
                or ".." in self.directory.parts or any(ord(c) < 32 for c in text)):
            raise TidalSessionError("Use an explicit absolute private TIDAL directory.")

    @contextmanager
    def _directory(self, *, create: bool = False):
        # Pin every component. No symlink is followed, including an ancestor.
        fd = os.open("/", _DIR_FLAGS)
        try:
            for index, part in enumerate(self.directory.parts[1:], start=1):
                final = index == len(self.directory.parts) - 1
                if final and create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
                info = os.fstat(fd)
                if final:
                    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                        raise TidalSessionError("The TIDAL directory must be private and owned by you.")
                else:
                    # Root-owned sticky /tmp is safe with a private final directory.
                    sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
                    if info.st_uid not in (0, os.geteuid()) or (info.st_mode & 0o022 and not sticky_root):
                        raise TidalSessionError("A TIDAL directory ancestor is unsafe.")
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _entry(directory: int) -> os.stat_result | None:
        try:
            info = os.stat(_SESSION_FILE, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return None
        _file_info(info)
        return info

    @staticmethod
    @contextmanager
    def _locked(directory: int):
        try:
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TidalSessionError("Another TIDAL session operation is already in progress.") from None
        except OSError:
            raise TidalSessionError("Exclusive TIDAL session ownership is unavailable.") from None
        lease = _TidalSessionLease(directory)
        try:
            yield lease
        finally:
            lease._directory_fd = None
            # The enclosing directory context closes its descriptor and releases
            # the lock. No persistent lock file, shared flag, or reentrant bypass.

    @contextmanager
    def lease(self):
        """Own storage across a login attempt; conflicts fail without waiting.

        Use the yielded object's load/save methods while ownership is held.
        Ordinary save/clear, even through this same store, cannot bypass it.
        """
        with ExitStack() as stack:
            try:
                directory = stack.enter_context(self._directory(create=True))
            except OSError:
                raise TidalSessionError("The private TIDAL directory could not be opened.") from None
            with self._locked(directory) as lease:
                yield lease

    def load(self) -> TidalSession | None:
        try:
            with self._directory() as directory:
                return self._load_at(directory)
        except FileNotFoundError:
            return None
        except OSError:
            raise TidalSessionError("The private TIDAL session could not be read.") from None

    @staticmethod
    def _load_at(directory: int) -> TidalSession | None:
        try:
            if TidalSessionStore._entry(directory) is None:
                return None
            fd = os.open(_SESSION_FILE, _FILE_FLAGS, dir_fd=directory)
            try:
                before = os.fstat(fd)
                _file_info(before)
                if before.st_size > MAX_SESSION_BYTES:
                    raise TidalSessionError("Stored TIDAL session exceeds the size limit.")
                with os.fdopen(fd, "rb", closefd=False) as source:
                    data = source.read(MAX_SESSION_BYTES + 1)
                if len(data) > MAX_SESSION_BYTES:
                    raise TidalSessionError("Stored TIDAL session exceeds the size limit.")
                if _fingerprint(before) != _fingerprint(os.fstat(fd)):
                    raise TidalSessionError("Stored TIDAL session changed while being read.")
            finally:
                os.close(fd)
        except FileNotFoundError:
            return None
        except OSError:
            raise TidalSessionError("The private TIDAL session could not be read.") from None
        try:
            value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
            if (type(value) is not dict or set(value) != {
                "schema", "access_token", "refresh_token", "expires_at", "user_id", "country_code"
            } or type(value["schema"]) is not int or value["schema"] != 1):
                raise ValueError
            return TidalSession(**{key: item for key, item in value.items() if key != "schema"})
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise TidalSessionError("Stored TIDAL session data is invalid.") from None

    def save(self, session: TidalSession) -> None:
        with self.lease() as lease:
            lease.save(session)

    @staticmethod
    def _save_at(directory: int, session: TidalSession) -> None:
        if type(session) is not TidalSession:
            raise TidalSessionError("A validated TIDAL session is required.")
        session.__post_init__()
        try:
            data = (json.dumps({
                "schema": 1, "access_token": session.access_token,
                "refresh_token": session.refresh_token, "expires_at": session.expires_at,
                "user_id": session.user_id, "country_code": session.country_code,
            }, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        except (ValueError, UnicodeError):
            raise TidalSessionError("TIDAL session data could not be encoded.") from None
        if len(data) > MAX_SESSION_BYTES:
            raise TidalSessionError("TIDAL session data exceeds the size limit.")
        try:
            TidalSessionStore._entry(directory)
            name = ".session-" + secrets.token_hex(16) + ".tmp"
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=directory)
            try:
                try:
                    os.fchmod(fd, 0o600)
                    with os.fdopen(fd, "wb", closefd=False) as output:
                        output.write(data)
                        output.flush()
                        os.fsync(fd)
                finally:
                    os.close(fd)
                TidalSessionStore._entry(directory)
                os.replace(name, _SESSION_FILE, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                try:
                    os.unlink(name, dir_fd=directory)
                except FileNotFoundError:
                    pass
        except OSError:
            raise TidalSessionError("The private TIDAL session could not be saved.") from None

    def clear(self) -> bool:
        """Delete only the private session; return whether one was present."""
        try:
            with self._directory() as directory:
                with self._locked(directory):
                    if self._entry(directory) is None:
                        return False
                    os.unlink(_SESSION_FILE, dir_fd=directory)
                    os.fsync(directory)
                    return True
        except FileNotFoundError:
            return False
        except OSError:
            raise TidalSessionError("The private TIDAL session could not be removed.") from None


class _TidalSessionLease:
    """A scoped capability, invalidated before its owned descriptor is closed."""

    def __init__(self, directory: int):
        self._directory_fd: int | None = directory

    def _active_directory(self) -> int:
        if self._directory_fd is None:
            raise TidalSessionError("The TIDAL session lease is no longer active.")
        try:
            info = os.fstat(self._directory_fd)
        except OSError:
            raise TidalSessionError("The TIDAL session lease is unavailable.") from None
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise TidalSessionError("The TIDAL directory must be private and owned by you.")
        return self._directory_fd

    def load(self) -> TidalSession | None:
        return TidalSessionStore._load_at(self._active_directory())

    def save(self, session: TidalSession) -> None:
        TidalSessionStore._save_at(self._active_directory(), session)
