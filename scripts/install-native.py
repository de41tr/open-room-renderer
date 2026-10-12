#!/usr/bin/env python3
"""Install an isolated native user profile; never change system services/settings."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MIN_PYTHON = (3, 11)
MIN_MPV = (0, 37)
MANIFEST = "installed-files.json"
WEB_ASSETS = ("index.html", "style.css", "controller.js", "app.js")


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


def environment(*, require_mpv: bool = True, require_alsa: bool = False) -> dict:
    if sys.version_info < MIN_PYTHON:
        raise ValueError("Python 3.11 or newer is required; choose python3.11/python3.12 explicitly.")
    if platform.system() != "Linux":
        raise ValueError("This native installation profile requires Linux.")
    machine = platform.machine().lower()
    support = {
        "x86_64": "native CI", "aarch64": "native AArch64 CI; hardware/systemd untested",
        "arm64": "native AArch64 CI; hardware/systemd untested",
        "armv7l": "ARMv7 untested; native dependency builds may be required",
        "armv8l": "32-bit ARM untested; native dependency builds may be required",
    }
    if machine not in support:
        raise ValueError(f"Architecture {machine!r} has no installation profile.")
    if require_mpv and any(importlib.util.find_spec(name) is None for name in ("venv", "ensurepip")):
        raise ValueError("Python venv/ensurepip support is missing; install your distribution's python3-venv package.")
    executable = shutil.which("mpv") if require_mpv else None
    mpv = None
    lines = []
    if executable:
        lines = run(executable, "--no-config", "--version").splitlines()
        mpv = lines[0] if lines else ""
        match = re.search(r"\bmpv\s+v?(\d+)\.(\d+)", mpv)
        if not match or tuple(map(int, match.groups())) < MIN_MPV:
            raise ValueError("The native profile requires identifiable mpv >= 0.37 for playlist ancestry checks.")
    elif require_mpv:
        raise ValueError("Missing mpv. Install your distribution's native mpv package first.")
    alsa = [p for p in Path("/dev/snd").glob("pcm*") if os.access(p, os.R_OK | os.W_OK)]
    if require_alsa and not alsa:
        raise ValueError("No accessible ALSA PCM device under /dev/snd; check the audio device/session permissions.")
    bits = 64 if sys.maxsize > 2**32 else 32
    validation = support[machine]
    if bits == 32 and machine in {"aarch64", "arm64", "x86_64"}:
        family = "ARM" if machine != "x86_64" else "x86"
        validation = f"32-bit {family} userspace on a 64-bit kernel; untested"
    return {
        "python": platform.python_version(), "machine": machine,
        "python_bits": bits,
        "libc": list(platform.libc_ver()), "validation": validation,
        "mpv": mpv, "mpv_build": lines, "alsa_pcm_accessible": bool(alsa),
    }


def checked_prefix(value: str) -> Path:
    prefix = Path(value).expanduser().absolute()
    if str(prefix) != str(prefix).rstrip():
        raise ValueError("Installation path must not end with whitespace (systemd strips it).")
    if ".." in prefix.parts:
        raise ValueError("Installation path must not contain parent-directory traversal (..).")
    if any(ord(char) < 32 for char in str(prefix)) or any(char in str(prefix) for char in ("$", "\\")):
        raise ValueError("Installation path must not contain control characters, backslashes, or $ (systemd expansion).")
    if prefix == Path("/") or prefix == Path.home():
        raise ValueError("Choose a dedicated installation directory, not / or your home directory.")
    for path in (prefix, *prefix.parents):
        if path.is_symlink():
            raise ValueError(f"Installation paths must not traverse symlinks: {path}")
    # Linux sockaddr_un.sun_path is 108 bytes including the terminator.
    if len(os.fsencode(prefix / "run/mpv.sock")) >= 108:
        raise ValueError("Installation path is too long for mpv's Unix socket; choose a shorter --prefix.")
    return prefix


def user_config(prefix: Path, port: int) -> dict:
    if not 1024 <= port <= 65535:
        raise ValueError("The user profile requires an unprivileged port (1024-65535). See the optional system port-80 example.")
    return {
        "friendly_name": "Open Room Renderer", "manufacturer": "Independent",
        "model_name": "Linux Room Renderer", "uuid": None,
        "http_api_port": port, "upnp_port": port, "mirror_port_80": False,
        "bind_address": "0.0.0.0", "advertise_ip": None,
        "mpv_socket": str(prefix / "run/mpv.sock"), "auto_spawn_mpv": True,
        "state_dir": str(prefix / "state"), "default_volume": 50,
        "default_mute": False, "enable_ssdp": True, "enable_mdns": True,
        "log_wiim_http_details": False, "proxy_allowed_lan_hosts": [],
        "podcast_presets": [], "experimental_sources": [],
    }


def systemd_quote(value: Path) -> str:
    # systemd specifier expansion applies even inside quotes.
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def user_unit(prefix: Path) -> str:
    return f"""# Copy to ~/.config/systemd/user/open-room-renderer.service after review.
# Generated unit does not install, enable, start, or configure login lingering.
[Unit]
Description=Open Room Renderer (unprivileged native profile)
After=network.target

[Service]
Type=simple
WorkingDirectory={str(prefix).replace("%", "%%")}
ExecStart={systemd_quote(prefix / 'venv/bin/open-room-renderer')} serve --config {systemd_quote(prefix / 'config.yaml')}
Restart=on-failure
RestartSec=5
TimeoutStopSec=45
UMask=0077
NoNewPrivileges=yes
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""


def source_files(root: Path = ROOT) -> list[Path]:
    files = [root / name for name in ("pyproject.toml", "README.md", "LICENSE")]
    files += sorted((root / "fake_linkplay").rglob("*.py"))
    # Keep the installation snapshot narrow: no arbitrary files from web_assets.
    files += [root / "fake_linkplay/web_assets" / name for name in WEB_ASSETS]
    for source in files:
        if any(path.is_symlink() for path in (source, *source.parents)
               if path == root or root in path.parents):
            raise ValueError(f"Installation source must not traverse symlinks: {source}")
        if not source.is_file():
            raise ValueError(f"Missing regular installation source file: {source}")
    return files


def source_receipt(snapshot: Path = ROOT) -> dict:
    digest = hashlib.sha256()
    for path in source_files(snapshot):
        digest.update(str(path.relative_to(snapshot)).encode() + b"\0" + path.read_bytes() + b"\0")
    revision, dirty = None, None
    try:
        revision = run("git", "-C", str(ROOT), "rev-parse", "HEAD")
        dirty = bool(run("git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"))
    except (OSError, subprocess.CalledProcessError):
        pass
    return {"revision": revision, "tracked_checkout_dirty": dirty, "source_sha256": digest.hexdigest()}


def file_entry(path: Path, prefix: Path) -> dict:
    entry = {"path": str(path.relative_to(prefix))}
    if path.is_symlink():
        entry["symlink"] = os.readlink(path)
    elif path.is_file():
        entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    else:
        raise ValueError(f"Unexpected non-file in installation: {path}")
    return entry


def write_manifest(prefix: Path) -> None:
    # Installation begins in a new directory; runtime state is created only later.
    files = []
    directories = []
    for directory, children, names in os.walk(prefix, followlinks=False):
        parent = Path(directory)
        for name in list(children):
            path = parent / name
            if path.is_symlink():
                files.append(file_entry(path, prefix))
                children.remove(name)
            else:
                directories.append(str(path.relative_to(prefix)))
        files.extend(file_entry(parent / name, prefix) for name in names if parent / name != prefix / MANIFEST)
    (prefix / MANIFEST).write_text(json.dumps({"format": 1, "files": files, "directories": directories}, indent=2) + "\n")


def manifest_path(prefix: Path, relative: str) -> Path:
    path = Path(relative)
    if not relative or path.is_absolute() or any(part in {".", ".."} for part in relative.split("/")):
        raise ValueError("Unsafe path in installation manifest; no files removed.")
    result = prefix / path
    if any(parent.is_symlink() for parent in result.parents if parent != prefix and prefix in parent.parents):
        raise ValueError("A manifest path traverses a symlink; no files removed.")
    return result


def uninstall(prefix: Path, *, dry_run: bool) -> None:
    manifest = prefix / MANIFEST
    if manifest.is_symlink():
        raise ValueError("The installation manifest must not be a symlink.")
    data = json.loads(manifest.read_text())
    if data.get("format") != 1:
        raise ValueError("Unknown installation manifest format; no files removed.")
    # Validate the complete manifest before removing anything. Never recurse-delete.
    paths = [(entry, manifest_path(prefix, entry["path"])) for entry in data["files"]]
    directories = [manifest_path(prefix, path) for path in data["directories"]]
    if (prefix / "run/mpv.sock").exists():
        raise ValueError("An mpv socket remains. Stop the renderer and its mpv process before uninstalling.")
    preserved = []
    removable = []
    for entry, path in paths:
        if not path.exists() and not path.is_symlink():
            continue
        matches = (path.is_symlink() and entry.get("symlink") == os.readlink(path)) if "symlink" in entry else (
            path.is_file() and not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest() == entry.get("sha256")
        )
        if not matches:
            preserved.append(str(path))
            continue
        removable.append(path)
    # Keep the removal tool with the retained manifest when edits need review.
    for path in removable:
        if preserved and path == prefix / "uninstall.py":
            continue
        print(f"{'Would remove' if dry_run else 'Removing'} {path}")
        if not dry_run:
            path.unlink()
    for path in preserved:
        print(f"Preserved modified file: {path}")
    if not dry_run and not preserved:
        manifest.unlink()
    if not dry_run:
        for path in sorted(directories, key=lambda p: len(p.parts), reverse=True):
            try:
                path.rmdir()  # Only empty owned directories, never foreign/runtime files.
            except OSError:
                pass
        try:
            prefix.rmdir()
        except OSError:
            pass
    print("Runtime state, unknown files, modified files, and any manually copied service unit are preserved.")


def install(prefix: Path, *, port: int, stage_only: bool, dry_run: bool, require_alsa: bool) -> None:
    info = environment(require_mpv=not stage_only, require_alsa=require_alsa)
    config = user_config(prefix, port)
    if prefix.exists():
        raise ValueError("Installation directory already exists. Use a new dedicated --prefix; existing files are never overwritten.")
    print(json.dumps(info, indent=2))
    if not info["alsa_pcm_accessible"]:
        print("WARNING: no accessible ALSA PCM device detected; physical sound is unverified (session audio may still work).")
    print(f"{'Stage files only' if stage_only else 'Install native package'} at {prefix}; HTTP/UPnP port {port}")
    if dry_run:
        print("Dry run: no files written, packages installed, services changed, or ports opened.")
        return
    if os.geteuid() == 0:
        raise ValueError("Run the user installer as your normal account, without sudo.")
    prefix.mkdir(parents=True, mode=0o700)
    receipt = {"format": 1, "mode": "installation-incomplete", "environment": info, "source": None}
    try:
        for directory in ("run", "state", "source"):
            (prefix / directory).mkdir(mode=0o700)
        for source in source_files():
            target = prefix / "source" / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        # Hash the files actually copied, not a concurrently edited working tree.
        receipt["source"] = source_receipt(prefix / "source")
        # JSON is a YAML subset, readable by the renderer's existing YAML loader.
        (prefix / "config.yaml").write_text(json.dumps(config, indent=2) + "\n")
        (prefix / "open-room-renderer.service").write_text(user_unit(prefix))
        shutil.copyfile(__file__, prefix / "uninstall.py")
        if stage_only:
            receipt["mode"] = "staged-only"
        else:
            subprocess.run([sys.executable, "-m", "venv", str(prefix / "venv")], check=True)
            python = str(prefix / "venv/bin/python")
            subprocess.run([python, "-m", "pip", "install", "--report", str(prefix / "pip-install-report.json"), str(prefix / "source")], check=True)
            receipt["packages"] = json.loads(run(python, "-m", "pip", "list", "--format=json"))
            subprocess.run([str(prefix / "venv/bin/open-room-renderer"), "--help"], check=True, stdout=subprocess.DEVNULL)
            receipt["mode"] = "installed"
    finally:
        (prefix / "installation-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        write_manifest(prefix)
    print(f"Receipt: {prefix / 'installation-receipt.json'}")
    print("No service installed or enabled. Review GETTING_STARTED.md before starting.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", default=str(Path.home() / ".local/share/open-room-renderer"))
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stage-only", action="store_true", help="stage templates/source only; do not create a venv or install packages")
    parser.add_argument("--require-alsa", action="store_true", help="fail unless an ALSA PCM device is readable and writable")
    parser.add_argument("--uninstall", action="store_true", help="remove only unchanged manifest-owned files; stop the service first")
    args = parser.parse_args(argv)
    try:
        prefix = checked_prefix(args.prefix)
        if args.uninstall:
            uninstall(prefix, dry_run=args.dry_run)
        else:
            install(prefix, port=args.port, stage_only=args.stage_only, dry_run=args.dry_run, require_alsa=args.require_alsa)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Installation error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
