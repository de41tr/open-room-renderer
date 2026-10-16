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
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MIN_PYTHON = (3, 11)
MIN_MPV = (0, 37)
MANIFEST = "installed-files.json"
WEB_ASSETS = ("index.html", "style.css", "controller.js", "app.js", "logo.png")


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


def checked_prefix(value: str, *, for_install: bool = True) -> Path:
    prefix = Path(value).expanduser().absolute()
    if str(prefix) != str(prefix).rstrip():
        raise ValueError("Installation path must not end with whitespace (systemd strips it).")
    if ".." in prefix.parts:
        raise ValueError("Installation path must not contain parent-directory traversal (..).")
    if any(ord(char) < 32 for char in str(prefix)) or any(char in str(prefix) for char in ("$", "\\")):
        raise ValueError("Installation path must not contain control characters, backslashes, or $ (systemd expansion).")
    # systemd checks the unquoted executable with string_is_safe(); escaping
    # cannot make literal quotes or DEL valid. Keep old-prefix cleanup possible.
    if for_install and any(char in str(prefix) for char in ("'", '"', "\x7f")):
        raise ValueError("Installation executable path must not contain quotes or DEL (systemd restriction).")
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
        "http_api_port": port, "upnp_port": 59152 if port == 8080 else port,
        "wiim_home_profile": True, "mirror_http_on_upnp_port": True, "mirror_port_80": False,
        "bind_address": "0.0.0.0", "advertise_ip": None,
        "mpv_socket": str(prefix / "run/mpv.sock"), "auto_spawn_mpv": True,
        "state_dir": str(prefix / "state"), "default_volume": 50,
        "default_mute": False, "enable_ssdp": True, "enable_mdns": True,
        "log_wiim_http_details": False, "proxy_allowed_lan_hosts": [],
        "podcast_presets": [], "experimental_sources": [],
    }


def user_unit(prefix: Path) -> str:
    # Load this dependency-free helper from the trusted installer checkout.
    # The copied uninstall tool does not call the template generator.
    spec = importlib.util.spec_from_file_location("native_profile", ROOT / "fake_linkplay/native_profile.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.user_unit(prefix)


def source_files(root: Path = ROOT) -> list[Path]:
    files = [root / name for name in ("pyproject.toml", "README.md", "LICENSE")]
    files += sorted((root / "fake_linkplay").rglob("*.py"))
    files.append(root / "fake_linkplay/host_components.json")
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


def is_user_data(path: Path, prefix: Path) -> bool:
    relative = path.relative_to(prefix)
    return relative == Path("config.yaml") or relative.parts[0] in {"state", "migrated-profile"}


def write_manifest(prefix: Path) -> None:
    # User configuration/state never becomes installer-owned, even during recovery.
    files = []
    directories = []
    for directory, children, names in os.walk(prefix, followlinks=False):
        parent = Path(directory)
        for name in list(children):
            path = parent / name
            if is_user_data(path, prefix):
                children.remove(name)
            elif path.is_symlink():
                files.append(file_entry(path, prefix))
                children.remove(name)
            else:
                directories.append(str(path.relative_to(prefix)))
        files.extend(file_entry(parent / name, prefix) for name in names
                     if parent / name != prefix / MANIFEST and not is_user_data(parent / name, prefix))
    (prefix / MANIFEST).write_text(json.dumps({"format": 1, "files": files, "directories": directories}, indent=2) + "\n")


def manifest_path(prefix: Path, relative: str) -> Path:
    path = Path(relative)
    if not relative or path.is_absolute() or any(part in {".", ".."} for part in relative.split("/")):
        raise ValueError("Unsafe path in installation manifest; no files removed.")
    result = prefix / path
    if any(parent.is_symlink() for parent in result.parents if parent != prefix and prefix in parent.parents):
        raise ValueError("A manifest path traverses a symlink; no files removed.")
    return result


def check_receiver_dependency(prefix: Path) -> None:
    # Keep the copied uninstaller standalone. Inspect only the two links made
    # by receiver install, not arbitrary units, processes or historical receipts.
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    units = Path(base) / "systemd/user"
    for relative in (Path("open-room-renderer-spotify.service"),
                     Path("open-room-renderer.service.d/90-spotify-receiver.conf")):
        link = units / relative
        try:
            info = link.lstat()
        except FileNotFoundError:
            continue
        try:
            if not stat.S_ISLNK(info.st_mode):
                raise ValueError("Receiver link was replaced")
            target = Path(os.readlink(link))
            if not target.is_absolute() or target.parts[-len(relative.parts):] != relative.parts:
                raise ValueError("Changed receiver link target")
            root = target.parents[len(relative.parts) - 1]
            # A missing unit target is still an installed link. Read the setup
            # metadata directly, with a size/type bound even when it is damaged.
            fd = os.open(root / "receiver.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError("Receiver metadata is not a regular file")
                raw = os.read(fd, 262145)
            finally:
                os.close(fd)
            if len(raw) > 262144:
                raise ValueError("Receiver metadata is too large")
            profile = json.loads(raw)
            native = profile.get("renderer_prefix") if isinstance(profile, dict) else None
            if (not isinstance(native, str) or not Path(native).is_absolute()
                    or str(Path(native)) != native or ".." in Path(native).parts
                    or profile.get("format") != 1 or profile.get("directory") != str(root)):
                raise ValueError("Receiver metadata cannot identify its native prefix")
        except (OSError, ValueError) as exc:
            raise ValueError(f"Cannot verify the native dependency of receiver link {link}. "
                             "Restore/review that receiver setup and uninstall it first; no files removed.") from exc
        if Path(native) == prefix:
            raise ValueError(f"An installed Spotify receiver still uses this native prefix. "
                             f"Run its 'spotify receiver --directory DIR uninstall' command first "
                             f"with DIR set to {root}, while the native executable is intact; no files removed.")


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
    check_receiver_dependency(prefix)
    preserved = []
    removable = []
    for entry, path in paths:
        # Older format-1 manifests may claim user data; ownership never overrides it.
        if is_user_data(path, prefix):
            continue
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
            if is_user_data(path, prefix):
                continue
            try:
                path.rmdir()  # Only empty owned directories, never foreign/runtime files.
            except OSError:
                pass
        try:
            prefix.rmdir()
        except OSError:
            pass
    print("User data (config.yaml, state/, migrated-profile/), unknown files, modified files, "
          "and any manually copied service unit are preserved.")


def install(prefix: Path, *, port: int, stage_only: bool, dry_run: bool, require_alsa: bool) -> None:
    prefix = checked_prefix(str(prefix))
    info = environment(require_mpv=not stage_only, require_alsa=require_alsa)
    config = user_config(prefix, port)
    if prefix.exists():
        raise ValueError("Installation directory already exists. Use a new dedicated --prefix; existing files are never overwritten.")
    print(json.dumps(info, indent=2))
    if not info["alsa_pcm_accessible"]:
        print("WARNING: no accessible ALSA PCM device detected; physical sound is unverified (session audio may still work).")
    print(f"{'Stage files only' if stage_only else 'Install native package'} at {prefix}; HTTP port {port}; SOAP/UPnP port {config['upnp_port']}")
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
        config_bytes = (json.dumps(config, indent=2) + "\n").encode("utf-8")
        (prefix / "config.yaml").write_bytes(config_bytes)
        # Verification metadata only: runtime config is never manifest-owned.
        receipt["initial_config_sha256"] = hashlib.sha256(config_bytes).hexdigest()
        (prefix / "open-room-renderer.service").write_text(user_unit(prefix))
        shutil.copyfile(__file__, prefix / "uninstall.py")
        if stage_only:
            receipt["mode"] = "staged-only"
        else:
            subprocess.run([sys.executable, "-m", "venv", str(prefix / "venv")], check=True)
            python = str(prefix / "venv/bin/python")
            subprocess.run([python, "-m", "pip", "install", "--report", str(prefix / "pip-install-report.json"), str(prefix / "source")], check=True)
            # Keep pip warnings on stderr, separate from the JSON package inventory.
            receipt["packages"] = json.loads(subprocess.check_output(
                [python, "-m", "pip", "list", "--format=json"], text=True))
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
        prefix = checked_prefix(args.prefix, for_install=not args.uninstall)
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
