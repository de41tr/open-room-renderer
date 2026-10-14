"""Pinned installation and native dependency checks for the optional client.

Use a supplied archive or explicitly download the native official release.
Installation is static. Verification uses only the native loader's --list mode;
no client daemon, credential operation, or Spotify request occurs here.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import struct
import subprocess
import tarfile
from typing import Iterator
from urllib.parse import urljoin, urlsplit

import aiohttp

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
DOWNLOAD_SECONDS = 60
DOWNLOAD_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}


def fail(message: str) -> None:
    raise ExclusiveProfileError(message)


async def _download_archive(machine: str) -> bytes:
    """Fetch one fixed release, without cookies, environment credentials or retries."""
    url = f"https://github.com/devgianlu/go-librespot/releases/download/{UPSTREAM_VERSION}/{ARCHIVES[machine][0]}"
    proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    if proxy:
        parsed = urlsplit(proxy)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            fail("Client download supports only credential-free HTTP(S) proxies; use --archive instead.")
    try:
        async with asyncio.timeout(DOWNLOAD_SECONDS), aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=DOWNLOAD_SECONDS, connect=10, sock_read=10),
                trust_env=False, cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False,
                headers={"Accept-Encoding": "identity", "User-Agent": "open-room-renderer-client-installer"}) as session:
            for _ in range(4):
                target = urlsplit(url)
                if (target.scheme != "https" or target.hostname not in DOWNLOAD_HOSTS
                        or target.port not in (None, 443) or target.username is not None or target.password is not None):
                    fail("Client download left the official HTTPS release hosts.")
                async with session.get(url, allow_redirects=False, proxy=proxy) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            fail("Official client download returned an invalid redirect.")
                        url = urljoin(url, location)
                        continue
                    if response.status != 200:
                        fail("Official client download failed; retry later or use --archive.")
                    if (response.headers.get("Content-Encoding", "identity").lower() != "identity"
                            or (response.content_length is not None and response.content_length > MAX_ARCHIVE)):
                        fail("Official client download exceeds its archive size or encoding limit.")
                    payload = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        if len(payload) + len(chunk) > MAX_ARCHIVE:
                            fail("Official client download exceeds its archive size limit.")
                        payload.extend(chunk)
                    return bytes(payload)
            fail("Official client download exceeded its redirect limit.")
    except (aiohttp.ClientError, TimeoutError, OSError):
        fail("Official client download was not completed within its limits; retry later or use --archive.")


@contextmanager
def _open_binary(path: Path) -> Iterator[tuple[int, bytes]]:
    """Keep the checked file open so loader inspection cannot follow a path swap."""
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
            yield handle.fileno(), data


def _binary(path: Path) -> bytes:
    with _open_binary(path) as (_, data):
        return data


def _check_dependencies(fd: int, interpreter: str | None) -> None:
    """List the independently pinned executable's libraries, never its entry point."""
    if interpreter is None:
        fail("The pinned client requires a native glibc loader for dependency inspection.")
    try:
        result = subprocess.run(
            [interpreter, "--list", f"/proc/self/fd/{fd}"], pass_fds=(fd,),
            env={"LC_ALL": "C"}, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, errors="replace", timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        fail("Native client dependency inspection did not complete; check the target ELF loader "
             "and /proc availability, then retry receiver setup/start. No Spotify daemon was started.")
    # glibc --list can exit zero despite unresolved libraries or symbol versions.
    # Do not expose raw loader output, which can contain private filesystem paths.
    output = result.stdout + "\n" + result.stderr
    missing = sorted(set(re.findall(r"^\s*([A-Za-z0-9_.+-]+)\s+=> not found\s*$", output, re.MULTILINE)))
    if "libasound.so.2" in missing or "libasound.so.2: cannot open shared object file" in output:
        # Official package names: packages.debian.org/{bookworm,trixie} and
        # packages.ubuntu.com/noble. Choose for the installed image, not the board.
        fail("Client native dependency libasound.so.2 is missing. Install your distribution's native "
             "ALSA runtime (Debian 12: libasound2; Debian 13/Ubuntu 24.04: libasound2t64), then retry "
             "receiver setup/start. Do not copy shared libraries from another OS.")
    versions = sorted(set(re.findall(r"version [`'](GLIBC_[0-9.]+)' not found", output)))
    if missing or versions or result.returncode or result.stderr.strip() or "libasound.so.2 =>" not in result.stdout:
        detail = ": " + ", ".join(missing + versions) if missing or versions else ""
        fail(f"Client native dependencies could not be resolved{detail}. Use matching native glibc "
             "and ALSA runtime packages from the target distribution, then retry receiver setup/start. "
             "Do not copy shared libraries from another OS.")


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


def install_client(archive: str | None, output_dir: str, *, download: bool = False) -> dict:
    """Extract exactly one pinned binary into a fresh private client directory."""
    if os.geteuid() == 0:
        fail("Install the optional client as your normal user, never root.")
    if (archive is not None) == download:
        fail("Choose exactly one client source: --archive or --download.")
    output = _path(output_dir)
    machine = platform.machine().lower()
    if machine not in ARCHIVES:
        fail("Client installation supports native x86_64, aarch64 and ARMv7 hard-float userspace only.")
    # Reject an occupied/invalid destination before any network operation.
    with _directory(output.parent) as parent:
        try:
            os.stat(output.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            fail("Client output directory must be new; existing files were left unchanged.")
    expected = ARCHIVES[machine]
    payload = asyncio.run(_download_archive(machine)) if download else _binary(_path(archive))
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
    """Check provenance/ABI and loader dependencies before setup or Spotify start."""
    root = _path(directory)
    receipt = json.loads(_read(root / "client-receipt.json"))
    machine = platform.machine().lower()
    spec = ARCHIVES.get(machine)
    if (spec is None or receipt.get("format") != 1 or receipt.get("version") != UPSTREAM_VERSION
            or receipt.get("commit") != UPSTREAM_COMMIT or receipt.get("archive_sha256") != spec[1]):
        fail("Use an intact client installed from the pinned official archive.")
    binary = root / "go-librespot"
    with _open_binary(binary) as (fd, data):
        digest = hashlib.sha256(data).hexdigest()
        if (digest != BINARY_SHA256.get(machine) or digest != receipt.get("binary_sha256")
                or not os.access(binary, os.X_OK)):
            fail("The installed client changed or is not executable; reinstall into a new directory.")
        abi = elf_abi(data, machine)
        _check_dependencies(fd, abi["interpreter"])
    return binary, receipt
