"""Pinned, offline installation of the optional upstream Spotify executable.

Only an operator-supplied official release archive is accepted. No download,
client execution, credential operation, or Spotify request occurs here.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import struct
import tarfile

from .spotify_exclusive import (ExclusiveProfileError, UPSTREAM_COMMIT,
                                UPSTREAM_VERSION, _directory, _path, _read)

RELEASE = "https://github.com/devgianlu/go-librespot/releases/tag/" + UPSTREAM_VERSION
# GitHub's official release-asset SHA-256 values, checked 2026-10-13.
ARCHIVES = {
    "x86_64": ("go-librespot_linux_x86_64.tar.gz", "8006f7f967704955c7e4f5f19bdd3cbf68c14d1ce100d9248a7db28884d61bd4", 2, 62),
    "aarch64": ("go-librespot_linux_arm64.tar.gz", "fdab3c06f3a776a3f696afb26aa59fbdea63557e24d3c39f2df2830ab1851a42", 2, 183),
    "armv7l": ("go-librespot_linux_armv6.tar.gz", "7d053d35209e147779f3e5dcbe0097f2e3cd6e183e369bddcd48fa822310bcc0", 1, 40),
}
# Binary hashes are independent of the mutable local receipt. Additional
# reviewed architectures are populated from their exact pinned archives.
BINARY_SHA256 = {
    "x86_64": "3e883f4779717d40fcb5a48adfda5f56815b808f2a9a9c7e2a1ff26d9229d5ee",
    "armv7l": "58f9c34e807345c6d4924326f946d20c2f253ced10805a34aaffd92f1e7bafff",
    "aarch64": "ccade5c0b229a4d0260f490c4bf338c13508e5164b0e7bd33cf7699982a167c8",
}
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_BINARY = 64 * 1024 * 1024


def fail(message: str) -> None:
    raise ExclusiveProfileError(message)


def _binary(path: Path) -> bytes:
    with _directory(path.parent) as parent:
        entry = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(entry.st_mode):
            fail("Client inputs must be regular files, not links or special devices.")
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o6022 or info.st_size > MAX_BINARY):
                fail("Client files must be regular, owned by you and not writable by other users.")
            data = handle.read(MAX_BINARY + 1)
            if len(data) > MAX_BINARY:
                fail("Client file exceeds its size limit.")
            return data


def elf_abi(data: bytes, machine: str) -> dict:
    """Inspect ELF class/machine/EABI flags, not an archive filename claim."""
    spec = ARCHIVES.get(machine)
    if spec is None or len(data) < 64 or data[:4] != b"\x7fELF" or data[5:7] != b"\x01\x01":
        fail("Require a supported little-endian native Linux ELF client.")
    elf_class, elf_machine = data[4], struct.unpack_from("<H", data, 18)[0]
    if (elf_class, elf_machine) != spec[2:] or struct.calcsize("P") != (4 if elf_class == 1 else 8):
        fail("Client ELF architecture does not match this Python userspace; cross-installation is unsupported.")
    flags = struct.unpack_from("<I", data, 36 if elf_class == 1 else 48)[0]
    if elf_machine == 40 and (flags & 0xFF000000 != 0x05000000 or not flags & 0x400 or flags & 0x200):
        fail("ARM client must declare EABI5 hard-float in its ELF flags.")
    # PT_INTERP inspection does not load or execute untrusted code (unlike ldd).
    phoff = struct.unpack_from("<I" if elf_class == 1 else "<Q", data, 28 if elf_class == 1 else 32)[0]
    phsize, phnum = struct.unpack_from("<HH", data, 42 if elf_class == 1 else 54)
    if phnum > 256 or phsize < (32 if elf_class == 1 else 56) or phoff + phnum * phsize > len(data):
        fail("Client ELF program headers are invalid.")
    interpreter = None
    for index in range(phnum):
        start = phoff + index * phsize
        if struct.unpack_from("<I", data, start)[0] != 3:
            continue
        offset = struct.unpack_from("<I" if elf_class == 1 else "<Q", data, start + (4 if elf_class == 1 else 8))[0]
        size = struct.unpack_from("<I" if elf_class == 1 else "<Q", data, start + (16 if elf_class == 1 else 32))[0]
        if interpreter is not None or not 2 <= size <= 512 or offset + size > len(data):
            fail("Client ELF interpreter is invalid.")
        raw = data[offset:offset + size]
        if raw[-1:] != b"\0" or b"\0" in raw[:-1]:
            fail("Client ELF interpreter is invalid.")
        interpreter = raw[:-1].decode("ascii")
        if not interpreter.startswith("/") or not Path(interpreter).is_file():
            fail("The client ELF loader is absent on this target; use a matching native userspace.")
    return {"machine": machine, "elf_class": elf_class, "elf_machine": elf_machine,
            "elf_flags": flags, "interpreter": interpreter,
            "arm_hard_float": elf_machine == 40, "hardware_verified": False}


def install_client(archive: str, output_dir: str) -> dict:
    """Extract exactly one pinned binary into a fresh private client directory."""
    if os.geteuid() == 0:
        fail("Install the optional client as your normal user, never root.")
    source, output = _path(archive), _path(output_dir)
    machine = platform.machine().lower()
    if machine not in ARCHIVES:
        fail("Client installation supports native x86_64, aarch64 and ARMv7 hard-float userspace only.")
    expected = ARCHIVES[machine]
    payload = _binary(source)
    if len(payload) > MAX_ARCHIVE or hashlib.sha256(payload).hexdigest() != expected[1]:
        fail("Archive does not match the pinned official release SHA-256 for this userspace.")
    import io
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as bundle:
        members = bundle.getmembers()
        candidates = [m for m in members if m.name == "go-librespot" and m.isfile()]
        if len(members) > 8 or len(candidates) != 1 or not 64 <= candidates[0].size <= MAX_BINARY:
            fail("The pinned archive has an unexpected binary layout.")
        handle = bundle.extractfile(candidates[0])
        if handle is None:
            fail("The pinned archive does not contain the expected executable.")
        data = handle.read(MAX_BINARY + 1)
    abi = elf_abi(data, machine)
    if hashlib.sha256(data).hexdigest() != BINARY_SHA256.get(machine):
        fail("The pinned release contains an unrecognized executable.")
    receipt = {"format": 1, "version": UPSTREAM_VERSION, "commit": UPSTREAM_COMMIT,
               "release": RELEASE, "archive": expected[0], "archive_sha256": expected[1],
               "binary_sha256": hashlib.sha256(data).hexdigest(), "abi": abi,
               "client_executed": False, "provider_permission": "PENDING"}
    with _directory(output.parent) as parent:
        os.mkdir(output.name, 0o700, dir_fd=parent)
    with _directory(output) as directory:
        for name, body, mode in (("go-librespot", data, 0o700),
                                  ("client-receipt.json", (json.dumps(receipt, indent=2) + "\n").encode(), 0o600)):
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=directory)
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
    return receipt


def verify_client(directory: str | Path) -> tuple[Path, dict]:
    root = _path(directory)
    receipt = json.loads(_read(root / "client-receipt.json"))
    machine = platform.machine().lower()
    spec = ARCHIVES.get(machine)
    if (spec is None or receipt.get("format") != 1 or receipt.get("version") != UPSTREAM_VERSION
            or receipt.get("commit") != UPSTREAM_COMMIT or receipt.get("archive_sha256") != spec[1]):
        fail("Use an intact client installed from the pinned official archive.")
    binary = root / "go-librespot"
    data = _binary(binary)
    digest = hashlib.sha256(data).hexdigest()
    if (digest != BINARY_SHA256.get(machine) or digest != receipt.get("binary_sha256")
            or not os.access(binary, os.X_OK)):
        fail("The installed client changed or is not executable; reinstall into a new directory.")
    elf_abi(data, machine)
    return binary, receipt
