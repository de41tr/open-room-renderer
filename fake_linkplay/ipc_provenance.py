"""Private, peer-established socket provenance across Linux boots.

This is not a stale-socket detector. Only a socket previously certified by our
own child can be removed, and only after the kernel boot identity has changed.
Missing/invalid provenance always leaves the existing path alone.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat

LOG = logging.getLogger(__name__)


def boot_id() -> str | None:
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as source:
            value = source.read(64).strip()
        return value if re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value) else None
    except (OSError, UnicodeError):
        return None


def fingerprint(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, info.st_ctime_ns, info.st_uid, info.st_mode]


def host_id() -> str | None:
    """Bind a receipt to this host, not another host sharing its filesystem."""
    try:
        with open("/etc/machine-id", encoding="ascii") as source:
            value = source.read(64).strip()
        if re.fullmatch(r"[0-9a-f]{32}", value) and value != "0" * 32:
            return hashlib.sha256(b"orr-mpv-provenance-v1\0" + value.encode("ascii")).hexdigest()
    except (OSError, UnicodeError):
        pass
    return None


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate socket receipt field")
        result[key] = value
    return result


class SocketProvenance:
    def __init__(self, socket_path: str):
        self.path = Path(socket_path)
        self.created: tuple[str, list[int], list[int]] | None = None

    @contextmanager
    def startup_guard(self, enabled: bool = True):
        """Serialize cooperating starts through readiness, without a lock file."""
        if not enabled:
            yield False
            return
        try:
            directory = self._directory()
            fd = directory.__enter__()
        except OSError:
            yield False
            return
        try:
            # Never block the event loop behind another renderer. Holding the
            # private directory inode excludes another cooperating recovery
            # until this startup has either published its own child or failed.
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.ENOSYS, errno.EOPNOTSUPP):
                    raise
                # Unsupported recovery storage must not break an otherwise
                # valid ordinary startup. A busy supported lock still fails.
                yield False
                return
            yield True
        finally:
            directory.__exit__(None, None, None)

    @contextmanager
    def _directory(self):
        """Pin a private owner directory; never traverse a symlink component."""
        if not self.path.is_absolute() or ".." in self.path.parts:
            raise OSError("socket provenance needs an absolute private path")
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in self.path.parent.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
                info = os.fstat(fd)
                # A root-owned sticky ancestor such as /tmp is safe only
                # because the final directory must be private and ours.
                sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
                if info.st_uid not in (0, os.geteuid()) or (info.st_mode & 0o022 and not sticky_root):
                    raise OSError("unsafe socket directory ancestor")
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise OSError("socket directory is not private")
            yield fd
        finally:
            os.close(fd)

    def _socket(self, directory: int) -> os.stat_result:
        info = os.stat(self.path.name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise OSError("socket is not a private single-owner entry")
        return info

    def _name(self, info: os.stat_result) -> str:
        key = os.fsencode(self.path.name) + repr(fingerprint(info)).encode("ascii")
        return ".orr-mpv-" + hashlib.sha256(key).hexdigest() + ".json"

    @staticmethod
    def _receipt(directory: int, name: str) -> tuple[dict, list[int]]:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > 1024):
                raise OSError("unsafe socket receipt")
            value = json.loads(os.read(fd, 1025), object_pairs_hook=_unique_object)
            if fingerprint(os.fstat(fd)) != fingerprint(info) or not isinstance(value, dict):
                raise OSError("socket receipt changed")
            return value, fingerprint(info)
        finally:
            os.close(fd)

    def snapshot(self) -> list[int] | None:
        try:
            with self._directory() as directory:
                return fingerprint(self._socket(directory))
        except OSError:
            return None

    def remember(self, verified_fingerprint: list[int]) -> None:
        """Called synchronously only after SO_PEERCRED matched our live child."""
        if self.created is not None:
            return
        current_boot = boot_id()
        current_host = host_id()
        if current_boot is None or current_host is None:
            return
        try:
            with self._directory() as directory:
                info = self._socket(directory)
                if fingerprint(info) != verified_fingerprint:
                    return
                name = self._name(info)
                payload = json.dumps({"schema": 1, "boot_id": current_boot, "host": current_host,
                                      "socket": fingerprint(info)}, separators=(",", ":")).encode("ascii")
                # Never overwrite an unknown receipt, even in our directory.
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
                try:
                    os.fchmod(fd, 0o600)
                    with os.fdopen(fd, "wb", closefd=False) as output:
                        output.write(payload)
                        output.flush()
                        os.fsync(fd)
                    receipt = fingerprint(os.fstat(fd))
                finally:
                    os.close(fd)
                self.created = (name, receipt, fingerprint(os.fstat(directory)))
                if fingerprint(self._socket(directory)) != fingerprint(info):
                    self.forget()
                    return
                os.fsync(directory)
        except (OSError, ValueError):
            # Unsupported/unwritable storage loses automatic reboot recovery,
            # never the existing fail-closed socket admission policy.
            LOG.warning("mpv reboot provenance was not persisted; automatic recovery is not assured")
            return

    def recover_previous_boot(self) -> bool:
        current_boot = boot_id()
        current_host = host_id()
        if current_boot is None or current_host is None:
            return False
        try:
            with self._directory() as directory:
                info = self._socket(directory)
                name = self._name(info)
                value, receipt = self._receipt(directory, name)
                previous_boot = value.get("boot_id")
                if (set(value) != {"schema", "boot_id", "host", "socket"}
                        or value["host"] != current_host or type(value["schema"]) is not int
                        or value["schema"] != 1 or not isinstance(value["socket"], list)
                        or any(type(part) is not int for part in value["socket"])
                        or value["socket"] != fingerprint(info) or not isinstance(previous_boot, str)
                        or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", previous_boot)
                        or previous_boot == current_boot):
                    return False
                # No await between the final identity checks and unlink.
                if (fingerprint(self._socket(directory)) != fingerprint(info)
                        or fingerprint(os.lstat(self.path)) != fingerprint(info)
                        or fingerprint(os.stat(name, dir_fd=directory, follow_symlinks=False)) != receipt):
                    return False
                os.unlink(self.path.name, dir_fd=directory)
                os.unlink(name, dir_fd=directory)
                os.fsync(directory)
                return True
        except (OSError, ValueError, TypeError, UnicodeError):
            return False

    def forget(self) -> None:
        """Remove only the unchanged receipt this instance actually created."""
        if self.created is None:
            return
        name, receipt, parent = self.created
        try:
            with self._directory() as directory:
                # Directory timestamps change as sockets/receipts come and go.
                if (fingerprint(os.fstat(directory))[:2] == parent[:2]
                        and fingerprint(os.stat(name, dir_fd=directory, follow_symlinks=False)) == receipt):
                    os.unlink(name, dir_fd=directory)
        except OSError:
            pass
        self.created = None
