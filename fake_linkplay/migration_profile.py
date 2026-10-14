"""Prepare a separate, offline native upgrade profile. Never activate services.

Run with the new installation's Python: ``python -m fake_linkplay.migration_profile``.
This deliberately accepts only a narrow same-user native layout, not general backups.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

import yaml

from .config import LEGACY_DISCOVERY_DEFAULTS, normalize_uuid, preserve_discovery_identity, read_startup_config
from .native_profile import user_unit

STATE_FILES = frozenset({"uuid", "device_name", "channel_balance", "gain.json",
                         "active_dsp.json", "eq_presets.json", "favorites.json"})
SKIP_FILES = frozenset({"plex_client_id", "plex_user_info.json"})
MAX_FILE_BYTES = 1024 * 1024
PROFILE = "migrated-profile"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW


class PreparationError(ValueError):
    """Fixed diagnostics never echo private configuration or input filenames."""


class ProfileCommittedError(PreparationError):
    """The config was published and must not be removed/retried automatically."""


def _path(value: str | Path) -> Path:
    text = str(value)
    path = Path(text)
    if (path.anchor != "/" or any(part in {".", ".."} for part in text.split("/"))
            or text != str(path) or text != text.rstrip()
            or any(ord(char) < 32 or char in "$\\" for char in text)
            or path == Path("/")):
        raise PreparationError("Use explicit absolute, normalized native paths without symlinks or expansion characters.")
    return path


def _disjoint(left: Path, right: Path) -> bool:
    return left != right and left not in right.parents and right not in left.parents


def _fingerprint(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _owned(info: os.stat_result, *, directory: bool = False, private: bool = False) -> None:
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not correct_type or info.st_uid != os.geteuid() or info.st_mode & 0o022
            or (private and info.st_mode & 0o077)
            or (not directory and info.st_nlink != 1)):
        raise PreparationError("Inputs must be same-user regular files without hardlinks, inside private native directories.")


class _Tree:
    """Pin directories, refuse symlink components, and detect input replacement."""

    def __init__(self, root: Path):
        self.root = root
        self.fds: dict[str, int] = {}
        self.observed: dict[str, tuple] = {}
        descriptor = os.open("/", _DIR_FLAGS)
        try:
            for component in root.parts[1:]:
                child = os.open(component, _DIR_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            _owned(os.fstat(descriptor), directory=True, private=True)
            self.fds[""] = descriptor
            self.observed[""] = _fingerprint(os.fstat(descriptor))
        except BaseException:
            os.close(descriptor)
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for descriptor in self.fds.values():
            os.close(descriptor)

    def directory(self, relative: str, *, private: bool = False) -> int:
        if relative not in self.fds:
            path = Path(relative)
            parent = self.directory(str(path.parent) if path.parent != Path(".") else "")
            descriptor = os.open(path.name, _DIR_FLAGS, dir_fd=parent)
            try:
                _owned(os.fstat(descriptor), directory=True, private=private)
            except BaseException:
                os.close(descriptor)
                raise
            self.fds[relative] = descriptor
            self.observed[relative] = _fingerprint(os.fstat(descriptor))
        descriptor = self.fds[relative]
        _owned(os.fstat(descriptor), directory=True, private=private)
        return descriptor

    def parent(self, relative: str) -> tuple[int, str]:
        path = Path(relative)
        return self.directory(str(path.parent) if path.parent != Path(".") else ""), path.name

    def entries(self, relative: str, *, private: bool = False) -> set[str]:
        descriptor = self.directory(relative, private=private)
        # A native state/run directory is bounded by the known state allowlist.
        result = set()
        with os.scandir(descriptor) as entries:
            for entry in entries:
                result.add(entry.name)
                if len(result) > len(STATE_FILES | SKIP_FILES):
                    raise PreparationError("Native state/run contains unexpected entries; review it manually.")
        return result

    def read(self, relative: str, *, skip_contents: bool = False) -> bytes:
        parent, name = self.parent(relative)
        # Even skipped Plex state must have a safe type, but is never opened/read.
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        _owned(before)
        if skip_contents:
            self.observed[relative] = _fingerprint(before)
            return b""
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent)
        try:
            opened = os.fstat(descriptor)
            _owned(opened)
            if _fingerprint(before) != _fingerprint(opened) or opened.st_size > MAX_FILE_BYTES:
                raise PreparationError("Input changed or exceeds the one MiB per-file preparation limit.")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
            after = os.fstat(descriptor)
            if len(data) > MAX_FILE_BYTES or _fingerprint(opened) != _fingerprint(after):
                raise PreparationError("Input changed or exceeds the one MiB per-file preparation limit.")
            self.observed[relative] = _fingerprint(after)
            return data
        finally:
            os.close(descriptor)

    def unchanged(self, *, output_created: bool = False) -> None:
        # Reopen from / to also detect replacement of any prefix component.
        with _Tree(self.root) as current:
            for relative, expected in self.observed.items():
                if relative == "":
                    actual = _fingerprint(os.fstat(current.fds[""]))
                    # Creating our output changes only the destination root's times/size.
                    if output_created:
                        actual, expected = actual[:4], expected[:4]
                else:
                    parent, name = current.parent(relative)
                    actual = _fingerprint(os.stat(name, dir_fd=parent, follow_symlinks=False))
                if actual != expected:
                    raise PreparationError("An input changed during preparation; keep all instances offline and use a fresh destination.")


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise PreparationError("The installation receipt contains duplicate fields.")
        value[key] = item
    return value


def _receipt(tree: _Tree, *, destination: bool = False) -> dict:
    try:
        receipt = json.loads(tree.read("installation-receipt.json"), object_pairs_hook=_unique_pairs)
        if (not isinstance(receipt, dict) or type(receipt.get("format")) is not int or receipt["format"] != 1
                or receipt.get("mode") not in {"installed", "staged-only"}
                or not isinstance(receipt.get("environment"), dict)
                or not isinstance(receipt.get("source"), dict)
                or not re.fullmatch(r"[a-f0-9]{64}", receipt["source"].get("source_sha256", ""))
                or (receipt["mode"] == "installed" and not isinstance(receipt.get("packages"), list))
                or (destination and not re.fullmatch(r"[a-f0-9]{64}", receipt.get("initial_config_sha256", "")))):
            raise ValueError
        return receipt
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PreparationError("A complete native receipt is required; the fresh destination also needs initial-config digest metadata.") from None


class _StrictLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent) or getattr(self.peek_event(), "anchor", None) is not None:
            raise PreparationError("YAML anchors, aliases, and merge keys are not supported for preparation.")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        keys = set()
        for key_node, _ in node.value:
            if key_node.tag != "tag:yaml.org,2002:str" or key_node.value == "<<" or key_node.value in keys:
                raise PreparationError("YAML mappings must have unique string keys without merges.")
            keys.add(key_node.value)
        return super().construct_mapping(node, deep=deep)


def _config(data: bytes, path: Path) -> tuple[dict, object]:
    try:
        text = data.decode("utf-8")
        raw = yaml.load(text, Loader=_StrictLoader)
        if not isinstance(raw, dict):
            raise ValueError
        config = read_startup_config(path, captured_text=text)
        # Require a JSON-compatible, unambiguous mapping. This also rejects YAML
        # timestamps, binary/set values and non-finite floats before any writes.
        json.dumps(raw, allow_nan=False)
        return raw, config
    except (yaml.YAMLError, ValueError, TypeError, UnicodeError, AttributeError, KeyError, IndexError,
            OverflowError, RecursionError):
        raise PreparationError("Configuration is invalid or ambiguous; use unique YAML settings without aliases or merges.") from None


def _write(directory: int, name: str, data: bytes) -> None:
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def prepare_profile(*, source: str | Path, source_config: str | Path,
                    destination: str | Path, offline_acknowledged: bool,
                    original_prefix: str | Path | None = None, dry_run: bool = False) -> dict:
    """Capture bounded offline state and publish a separate config last, no-clobber.

    Socket absence cannot prove that any instance is stopped. The caller must
    explicitly acknowledge that the source and destination are offline throughout.
    Same-user malicious concurrent writers are outside this offline trust model;
    observed changes, unsafe paths and ordinary replacement races fail closed.
    """
    committed = False
    try:
        if offline_acknowledged is not True:
            raise PreparationError("Explicit offline acknowledgment is required; socket absence cannot prove that instances are stopped.")
        source, source_config, destination = _path(source), _path(source_config), _path(destination)
        # Only the destination becomes an ExecStart executable. systemd rejects
        # literal quotes and DEL even after unquoting; source/config names need not.
        if any(char in str(destination) for char in ("'", '"', "\x7f")):
            raise PreparationError("Destination executable path must not contain quotes or DEL (systemd restriction).")
        original = source if original_prefix is None else _path(original_prefix)
        if not _disjoint(source, destination) or not _disjoint(original, destination):
            raise PreparationError("Source, original installation, and destination must be disjoint native prefixes.")
        if not source_config.is_relative_to(source) or source_config == source:
            raise PreparationError("Select the actual config inside the source installation or snapshot.")
        profile = destination / PROFILE
        if len(os.fsencode(destination / "run/mpv.sock")) >= 108:
            raise PreparationError("The destination native mpv socket path exceeds the Unix socket limit.")
        with _Tree(source) as old, _Tree(destination) as new:
            _receipt(old)
            receipt = _receipt(new, destination=True)
            source_raw, cfg = _config(old.read(str(source_config.relative_to(source))), source_config)
            fresh_bytes = new.read("config.yaml")
            if hashlib.sha256(fresh_bytes).hexdigest() != receipt["initial_config_sha256"]:
                raise PreparationError("The destination config has changed since installation; use a fresh destination.")
            fresh_raw, fresh_cfg = _config(fresh_bytes, destination / "config.yaml")
            if (fresh_cfg.state_dir != str(destination / "state")
                    or fresh_cfg.mpv_socket != str(destination / "run/mpv.sock")
                    or fresh_cfg.auto_spawn_mpv is not True or fresh_cfg.uuid):
                raise PreparationError("The destination must retain its fresh native config and empty identity/state.")
            state_value = source_raw.get("state_dir")
            if not isinstance(state_value, str):
                raise PreparationError("The source must explicitly configure an absolute prefix-local state_dir.")
            original_state = _path(state_value)
            if original_state == original or not original_state.is_relative_to(original):
                raise PreparationError("The source must explicitly configure an absolute prefix-local state_dir.")
            state_relative = str(original_state.relative_to(original))
            if (source_raw.get("mpv_socket") != str(original / "run/mpv.sock")
                    or cfg.auto_spawn_mpv is not True):
                raise PreparationError("Only the owned native mpv socket and auto-spawn profile can be prepared.")
            if not _disjoint(source / state_relative, source_config):
                raise PreparationError("The source config must be separate from its state directory.")
            if old.entries("run", private=True) or new.entries("run", private=True):
                raise PreparationError("A runtime entry remains; stop all renderer/mpv instances and review their native run directories.")
            if new.entries("state", private=True):
                raise PreparationError("The destination state must be empty; use a fresh installation.")
            try:
                os.stat(PROFILE, dir_fd=new.fds[""], follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise PreparationError("A migration profile already exists; never resume or merge it. Use a fresh destination.")
            names = old.entries(state_relative, private=True)
            if names - (STATE_FILES | SKIP_FILES):
                raise PreparationError("Source state contains unexpected entries; review them manually. No persisted queue is supported.")
            captured = {}
            for name in sorted(names):
                data = old.read(f"{state_relative}/{name}", skip_contents=name in SKIP_FILES)
                if name in STATE_FILES:
                    captured[name] = data
            if not cfg.uuid:
                try:
                    identity = normalize_uuid(captured.get("uuid", b"").decode("utf-8"))
                except UnicodeError:
                    identity = ""
                if not identity:
                    raise PreparationError("A configured identity or existing nonempty UUID is required; preparation never creates one.")
                cfg.uuid = identity
            preserve_discovery_identity(cfg)
            migrated = dict(source_raw)
            # Pin only omitted discovery settings in this new output profile,
            # using the captured identity. Never rewrite the source config.
            for name in LEGACY_DISCOVERY_DEFAULTS:
                migrated.setdefault(name, getattr(cfg, name))
            migrated["state_dir"] = str(profile / "state")
            migrated["mpv_socket"] = str(destination / "run/mpv.sock")
            encoded = yaml.safe_dump(migrated, sort_keys=False, allow_unicode=True).encode("utf-8")
            # Revalidate the exact serialized output without touching identity/state.
            read_startup_config(profile / "config.yaml", captured_text=encoded.decode("utf-8"))
            result = {"status": "dry-run" if dry_run else "prepared", "state_files": len(captured),
                      "skipped_plex_files": len(names & SKIP_FILES), "activated": False}
            old.unchanged()
            new.unchanged()
            if dry_run:
                return result
            committed = False
            created = False
            with ExitStack() as stack:
                try:
                    os.mkdir(PROFILE, 0o700, dir_fd=new.fds[""])
                    created = True
                    directory = os.open(PROFILE, _DIR_FLAGS, dir_fd=new.fds[""])
                    stack.callback(os.close, directory)
                    os.fchmod(directory, 0o700)
                    os.mkdir("state", 0o700, dir_fd=directory)
                    state = os.open("state", _DIR_FLAGS, dir_fd=directory)
                    stack.callback(os.close, state)
                    os.fchmod(state, 0o700)
                    for name, data in captured.items():
                        _write(state, name, data)
                    os.fsync(state)
                    _write(directory, "open-room-renderer.service", user_unit(destination, config=profile / "config.yaml").encode("utf-8"))
                    # The temporary file is not a runnable config path. Like identity
                    # and playlist publication, a same-directory hard link commits
                    # fully fsynced bytes without replacing any existing config.
                    _write(directory, ".config.pending", encoded)
                    os.fsync(directory)
                    os.fsync(new.fds[""])
                    old.unchanged()
                    new.unchanged(output_created=True)
                    info = os.stat(PROFILE, dir_fd=new.fds[""], follow_symlinks=False)
                    if (info.st_dev, info.st_ino) != (os.fstat(directory).st_dev, os.fstat(directory).st_ino):
                        raise PreparationError("The output directory changed during preparation; use a fresh destination.")
                    os.link(".config.pending", "config.yaml", src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                    committed = True
                    os.unlink(".config.pending", dir_fd=directory)
                    os.fsync(directory)
                    os.fsync(new.fds[""])
                except (OSError, ValueError):
                    if committed:
                        raise ProfileCommittedError("Profile config committed; directory durability unconfirmed. Keep both instances offline and inspect the profile; do not retry or delete it automatically.") from None
                    if created:
                        raise PreparationError("Preparation stopped before config publication. Private partial output may remain; keep it offline, inspect/remove only that partial profile manually, and use a fresh destination. No automatic resume or merge.") from None
                    raise
            return result
    except (OSError, ValueError):
        # Context-manager descriptor cleanup can fail after the link committed,
        # too. Keep the same publication-aware result across the whole operation.
        if committed:
            raise ProfileCommittedError("Profile config committed; directory durability unconfirmed. Keep both instances offline and inspect the profile; do not retry or delete it automatically.") from None
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="absolute native installation or relocated snapshot prefix")
    parser.add_argument("--source-config", required=True, help="actual config inside SOURCE; never guessed")
    parser.add_argument("--destination", required=True, help="fresh native installation prefix")
    parser.add_argument("--original-prefix", help="original absolute prefix recorded by a relocated snapshot's config")
    parser.add_argument("--acknowledge-offline", action="store_true", help="confirm all source/destination renderer and mpv instances remain stopped")
    parser.add_argument("--dry-run", action="store_true", help="validate and capture in memory without writing")
    args = parser.parse_args(argv)
    try:
        result = prepare_profile(source=args.source, source_config=args.source_config,
                                 destination=args.destination, original_prefix=args.original_prefix,
                                 offline_acknowledged=args.acknowledge_offline, dry_run=args.dry_run)
    except ProfileCommittedError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except PreparationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print("Preparation refused: check native paths, ownership, permissions and receipts. No service was contacted or changed.", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    print("Review the separate profile and unit before any manual activation. Socket absence does not prove that an instance is stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
