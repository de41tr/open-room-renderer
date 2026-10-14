"""Exclusive, opt-in go-librespot/renderer user-service lifecycle.

There is no second playback queue. systemd owns whole process cgroups; this
independent CLI admits one service only after both previous cgroups are empty.
Unknown stop outcomes never authorize a competing start.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import ctypes
import ctypes.util
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time

from yaml import YAMLError

from .device_name import validate_device_name
from .native_profile import systemd_quote, user_unit
from .spotify_client import fail, install_client, verify_client
from .spotify_diagnostic import SpotifyDiagnosticClient, SpotifyDiagnosticError
from .spotify_exclusive import (DEFAULT_DEVICE_NAME, ExclusiveProfileError, RENDERER_SERVICE,
                                SPOTIFY_SERVICE, _directory, _path,
                                _private_directory, _read, _renderer,
                                _spotify_config)

UNITS = {"renderer": RENDERER_SERVICE, "spotify": SPOTIFY_SERVICE}
DROPIN = "90-spotify-receiver.conf"
PROPERTIES = ("Id", "LoadState", "ActiveState", "SubState", "MainPID", "ControlPID", "InvocationID",
              "ControlGroup", "Result", "Job", "FragmentPath", "DropInPaths",
              "NeedDaemonReload", "KillMode", "SendSIGKILL", "Restart", "TimeoutStopUSec")
STOP_TIMEOUT = 55
GATES = ["ACCOUNT_PENDING", "HARDWARE_PENDING", "PROVIDER_POLICY_PENDING"]
START_FAILURE_HINTS = {
    "EXEC_FAILED": "Check the native executable, execute permissions and native loader/dependencies before explicitly retrying.",
    "GUARD_REJECTED": "Check private setup, exact owned service units and exclusive output; use off-only recover before explicitly retrying.",
    "STARTUP_FAILED": "Inspect private setup and service status; use off-only recover before explicitly retrying.",
}


class _ExecFailed(OSError):
    """The native executable did not replace the guard."""


def _device_name(value: object) -> str:
    # ORR's bounded display-name policy, not an upstream/provider length limit.
    # Preserve the chosen Unicode text exactly; never inherit the room name.
    if type(value) is not str or not value.isprintable() or value != value.strip():
        fail("Use a printable Spotify device name of 1 to 128 characters without surrounding whitespace.")
    try:
        return validate_device_name(value)
    except ValueError:
        fail("Use a printable Spotify device name of 1 to 128 characters without surrounding whitespace.")


def _preferred_bitrate(value: object) -> int:
    if type(value) is not int or value not in (96, 160, 320):
        fail("Use a preferred Spotify bitrate of 96, 160 or 320 kbps.")
    return value


def _sidecar_config(root: Path, profile: dict) -> dict:
    name = _device_name(profile.get("device_name", DEFAULT_DEVICE_NAME))
    config = _spotify_config(profile["renderer"]["audio_device"], profile["host"], profile["port"], root,
                             device_name=name)
    # Absence preserves legacy bytes and the pinned upstream's 160 preference.
    if "preferred_bitrate" in profile:
        config["bitrate"] = _preferred_bitrate(profile["preferred_bitrate"])
    return config


def _json(path: Path) -> dict:
    result = json.loads(_read(path))
    if type(result) is not dict:
        fail("Receiver metadata must be an intact object.")
    return result


def _write(path: Path, value: dict) -> None:
    # Private mutable control metadata, never account state. Replace only a
    # regular owned file; do not follow a link left at the destination.
    with _directory(path.parent) as parent:
        try:
            before = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid():
                fail("Receiver control metadata has an unexpected owner or file type.")
        except FileNotFoundError:
            pass
        name = f".{path.name}.{os.getpid()}.tmp"
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(value, handle, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(name, dir_fd=parent)
            except FileNotFoundError:
                pass


@contextmanager
def _lock(root: Path, name: str):
    _private_directory(root)
    with _directory(root) as directory:
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            fail("Receiver lock must be a private regular file owned by you.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail("Another receiver operation or output owner is active; no competing start was issued.")
        yield fd
    finally:
        os.close(fd)


def _unit_parts(root: Path, profile: dict) -> dict[str, str]:
    launcher = Path(profile["renderer_prefix"]) / "venv/bin/open-room-renderer"
    call = f"{systemd_quote(launcher)} spotify receiver --directory {systemd_quote(root)} run"
    common = """KillMode=control-group
SendSIGKILL=yes
TimeoutStopSec=45
Restart=no
UMask=0077
NoNewPrivileges=yes
Slice=app.slice
StandardOutput=null
StandardError=null
UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH PYTHONPATH PYTHONHOME ALSA_CONFIG_PATH ALSA_CONFIG_DIR ALSA_PLUGIN_DIR
Environment=PATH=/usr/bin:/bin
"""
    return {
        SPOTIFY_SERVICE: f"""# Owned by ORR's explicit exclusive receiver setup. No boot enablement.
[Unit]
Description=Open Room Renderer exclusive Spotify receiver
Conflicts={RENDERER_SERVICE}
After={RENDERER_SERVICE}
[Service]
Type=simple
ExecStart={call} spotify
{common}""",
        RENDERER_SERVICE + ".d/" + DROPIN: f"""# Only this exact drop-in is supported by the receiver controller.
[Service]
ExecStart=
ExecStart={call} renderer
{common}""",
    }


def _check_mpv() -> None:
    # The managed service never searches a user-imported PATH. Accept only the
    # distribution's immutable native ELF, not a script or another player.
    path = Path("/usr/bin/mpv")
    _read(path, binary=True)
    if path.stat().st_uid != 0:
        fail("Receiver mode requires the distribution-owned /usr/bin/mpv executable.")


def _read_sidecar(root: Path, profile: dict) -> dict:
    """Verify the pinned client and private sidecar without touching account state."""
    _, receipt = verify_client(profile["client_dir"])
    if receipt["binary_sha256"] != profile["client_sha256"]:
        fail("The pinned client changed; restore the verified client before using this receiver setup.")
    _private_directory(root / "sidecar")
    actual = _json(root / "sidecar/config.yml")
    if (("bitrate" in actual and type(actual["bitrate"]) is not int)
            or os.path.lexists(root / "sidecar/config.yaml")):
        fail("The owned sidecar configuration changed or is shadowed by config.yaml.")
    return actual


def _same_config(actual: dict, expected: dict) -> bool:
    # Do not equate JSON true/1 or integers/floats in otherwise fixed fields.
    return json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)


def _check_spotify(root: Path, profile: dict) -> None:
    """Validate the pinned client and unchanged private sidecar, not a start."""
    if not _same_config(_read_sidecar(root, profile), _sidecar_config(root, profile)):
        fail("The owned sidecar configuration changed or is shadowed by config.yaml.")


def load_profile(directory: str | Path, *, start: str | None = None) -> tuple[Path, dict]:
    root = _path(directory)
    _private_directory(root)
    profile = _json(root / "receiver.json")
    if profile.get("format") != 1 or profile.get("directory") != str(root):
        fail("Receiver setup metadata is missing or belongs to another directory.")
    for field in ("renderer_prefix", "renderer_config", "renderer_unit", "client_dir"):
        _path(profile.get(field))
    for name, body in _unit_parts(root, profile).items():
        if _read(root / name).decode() != body:
            fail("Receiver unit proposals changed; stop remains available only with provable service ownership.")
    # Stop/off does not depend on client/config health. Damaged credentials or
    # an upgraded binary must never strand an already owned Spotify process.
    if start:
        _check_mpv()
        prefix = Path(profile["renderer_prefix"])
        actual = _renderer(prefix, Path(profile["renderer_config"]), Path(profile["renderer_unit"]), profile["port"])
        if actual != profile["renderer"]:
            fail("The native renderer profile changed. For same-route YAML edits, stop both services, then run "
                 "open-room-renderer spotify receiver --directory DIR refresh-renderer "
                 "with DIR set to this receiver setup directory. Changed paths, unit or output require a fresh setup.")
        if start == "spotify":
            _check_spotify(root, profile)
    return root, profile


class UserServices:
    """Bounded systemd user-manager operations; never signals a raw PID."""

    def __init__(self, root: Path, profile: dict):
        self.root, self.profile = root, profile

    def command(self, *args: str, timeout: float = 8) -> str:
        try:
            result = subprocess.run(["/usr/bin/systemctl", "--user", "--no-pager", *args],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=timeout,
                                    env={**os.environ, "LC_ALL": "C", "SYSTEMD_COLORS": "0"},
                                    check=False)
        except (OSError, subprocess.TimeoutExpired):
            fail("The user service-manager outcome is unknown; no competing start is allowed.")
        if result.returncode != 0 or len(result.stdout) > 131072:
            fail("The user service manager refused or could not confirm the operation.")
        return result.stdout.decode("utf-8", errors="strict")

    def show(self, mode: str) -> dict[str, str]:
        raw = self.command("show", UNITS[mode], "--property=" + ",".join(PROPERTIES))
        pairs = [line.split("=", 1) for line in raw.splitlines() if "=" in line]
        info = dict(pairs)
        if len(info) != len(pairs) or not set(PROPERTIES) <= info.keys() or info["Id"] != UNITS[mode]:
            fail("The user service-manager state is incomplete or ambiguous.")
        return info

    def validate(self) -> dict[str, dict]:
        result = {mode: self.show(mode) for mode in UNITS}
        expected_files = {
            "renderer": Path(self.profile["renderer_unit"]),
            "spotify": self.root / SPOTIFY_SERVICE,
        }
        for mode, info in result.items():
            # DropInPaths includes generic service.d and name-prefix drop-ins.
            expected_dropins = [str(self.root / (RENDERER_SERVICE + ".d") / DROPIN)] if mode == "renderer" else []
            fragment = Path(info["FragmentPath"])
            try:
                actual_fragment = fragment.resolve(strict=True)
                actual_dropins = [str(Path(p).resolve(strict=True)) for p in shlex.split(info["DropInPaths"])]
            except (OSError, ValueError):
                fail("An effective service fragment or drop-in cannot be verified.")
            if (info["LoadState"] != "loaded" or info["NeedDaemonReload"] != "no"
                    or actual_fragment not in ({expected_files[mode], _user_units() / RENDERER_SERVICE}
                                               if mode == "renderer" else {expected_files[mode]})
                    or actual_dropins != expected_dropins
                    or info["KillMode"] != "control-group" or info["SendSIGKILL"] != "yes"
                    or info["Restart"] != "no" or info["TimeoutStopUSec"] != "45s"):
                fail("Only the exact loaded receiver units and cgroup-stop policy are supported; reload or repair them first.")
            if mode == "renderer" and _read(actual_fragment).decode() != user_unit(
                    Path(self.profile["renderer_prefix"]), config=Path(self.profile["renderer_config"])):
                fail("The native renderer service has changed; its process ownership is unverified.")
        return result

    def cgroup_empty(self, group: str) -> bool:
        if type(group) is not str:
            fail("An owned cgroup record is malformed; output ownership is unknown.")
        if not group:
            return True
        prefix = f"/user.slice/user-{os.geteuid()}.slice/user@{os.geteuid()}.service/"
        if (not group.startswith(prefix) or any(part in {".", ".."} for part in group.split("/"))
                or not any(group.endswith("/" + unit) for unit in UNITS.values())):
            fail("A service reported an unexpected cgroup; no process will be killed or adopted.")
        mount = Path("/sys/fs/cgroup")
        if not (mount / "cgroup.controllers").is_file():
            fail("This receiver requires the real Linux cgroup-v2 user service manager.")
        target = mount / group.lstrip("/")
        try:
            paths = [target, *target.rglob("*")]
            if len(paths) > 1024:
                fail("The owned cgroup tree exceeds the bounded inspection limit.")
            for path in paths:
                if path.is_dir():
                    data = (path / "cgroup.procs").read_text()
                    if data.strip():
                        return False
        except FileNotFoundError:
            # Removed cgroup == empty. A disappearing descendant is likewise
            # empty; re-inspect the parent below to cover concurrent movement.
            if target.exists():
                return False  # A racing tree is unknown, never an endless retry.
        except OSError:
            fail("The whole owned cgroup cannot be inspected; output ownership is unknown.")
        return True

    def stopped(self, info: dict) -> bool:
        return (info["ActiveState"] == "inactive" and info["SubState"] == "dead"
                and info["MainPID"] == "0" and info["ControlPID"] == "0"
                and info["Job"] in {"", "0"})

    def stop_all(self, before: dict[str, dict]) -> None:
        prior_path = self.root / "last-cgroups.json"
        prior = _json(prior_path) if prior_path.exists() else {}
        # systemd may clear ControlGroup on a later inactive observation. Never
        # discard an earlier owned path before checking that its descendants
        # really disappeared, including after a previous uncertain stop.
        saved = {mode: info["ControlGroup"] or prior.get(mode, "") for mode, info in before.items()}
        _write(self.root / "last-cgroups.json", saved)
        self.command("stop", *UNITS.values(), timeout=STOP_TIMEOUT)
        after = self.validate()
        for mode, info in after.items():
            if (not self.stopped(info) or info["Result"] != "success"
                    or not self.cgroup_empty(saved[mode]) or not self.cgroup_empty(info["ControlGroup"])):
                fail("A whole-service stop or cgroup release is unconfirmed; both modes remain blocked.")

    def wait_started(self, mode: str) -> tuple[str, str]:
        """Observe the guard's successful exec, not merely Type=simple active."""
        expected = _target(self.root, self.profile, mode)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            info = self.show(mode)
            if info["ActiveState"] in {"failed", "inactive", "deactivating"}:
                break
            if info["ActiveState"] == "active" and info["MainPID"].isdigit() and int(info["MainPID"]) > 0:
                try:
                    raw = Path("/proc") / info["MainPID"] / "cmdline"
                    argv = raw.read_bytes().rstrip(b"\0").split(b"\0")
                    wanted = [os.fsencode(value) for value in expected]
                    # A Python console entry has its interpreter before the
                    # script path; the native Spotify executable has no prefix.
                    if argv == wanted or (mode == "renderer" and argv[1:] == wanted):
                        if not re.fullmatch(r"[0-9a-f]{32}", info["InvocationID"]):
                            fail("The started service has no valid invocation identity.")
                        return info["InvocationID"], info["MainPID"]
                except (FileNotFoundError, ProcessLookupError):
                    pass
            time.sleep(0.05)
        fail("The guarded client exec was not confirmed; both services will be stopped.")


def probe_alsa(route: str) -> None:
    """Open/close the exact single-subdevice hw PCM without sending samples."""
    if not re.fullmatch(r"hw:CARD=[A-Za-z0-9_-]{1,64},DEV=(?:0|[1-9][0-9]{0,2})", route):
        fail("Require an explicit direct ALSA hardware output.")
    library = ctypes.util.find_library("asound")
    if not library:
        fail("Native libasound is required for the direct-output release check.")
    lib = ctypes.CDLL(library)
    pointer = ctypes.c_void_p
    lib.snd_pcm_open.argtypes = [ctypes.POINTER(pointer), ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
    lib.snd_pcm_open.restype = ctypes.c_int
    lib.snd_pcm_close.argtypes = [pointer]
    lib.snd_pcm_close.restype = ctypes.c_int
    lib.snd_pcm_info_malloc.argtypes = [ctypes.POINTER(pointer)]
    lib.snd_pcm_info_malloc.restype = ctypes.c_int
    lib.snd_pcm_info.argtypes = [pointer, pointer]
    lib.snd_pcm_info.restype = ctypes.c_int
    lib.snd_pcm_info_get_subdevices_count.argtypes = [pointer]
    lib.snd_pcm_info_get_subdevices_count.restype = ctypes.c_uint
    lib.snd_pcm_type.argtypes = [pointer]
    lib.snd_pcm_type.restype = ctypes.c_int
    lib.snd_card_get_index.argtypes = [ctypes.c_char_p]
    lib.snd_card_get_index.restype = ctypes.c_int
    lib.snd_pcm_info_get_card.argtypes = [pointer]
    lib.snd_pcm_info_get_card.restype = ctypes.c_int
    lib.snd_pcm_info_get_device.argtypes = [pointer]
    lib.snd_pcm_info_get_device.restype = ctypes.c_uint
    lib.snd_pcm_info_free.argtypes = [pointer]
    pcm, info = pointer(), pointer()
    if lib.snd_pcm_open(ctypes.byref(pcm), route.encode("ascii"), 0, 1) < 0:
        fail("The direct ALSA output is busy, absent or inaccessible; no service was started.")
    try:
        if (lib.snd_pcm_info_malloc(ctypes.byref(info)) < 0 or lib.snd_pcm_info(pcm, info) < 0
                or lib.snd_pcm_info_get_subdevices_count(info) != 1):
            fail("Require a verified single-subdevice ALSA playback endpoint.")
        card_name, device = route.removeprefix("hw:CARD=").split(",DEV=")
        card = lib.snd_card_get_index(card_name.encode("ascii"))
        # A user's .asoundrc can redefine the symbol 'hw' to a mixer/null/pipe.
        # Inspect the opened PCM, without rewriting any user ALSA settings.
        if (lib.snd_pcm_type(pcm) != 0 or card < 0  # SND_PCM_TYPE_HW
                or lib.snd_pcm_info_get_card(info) != card
                or lib.snd_pcm_info_get_device(info) != int(device)):
            fail("The opened PCM is not the requested direct ALSA hardware endpoint.")
    finally:
        if info:
            lib.snd_pcm_info_free(info)
        result = lib.snd_pcm_close(pcm)
        if result < 0:
            fail("ALSA release could not be confirmed; no service was started.")


def switch(directory: str, mode: str) -> dict:
    root, profile = load_profile(directory)
    with _lock(root, "control.lock"):
        services = UserServices(root, profile)
        before = services.validate()
        if mode != "off":
            # Preserve a healthy exclusive source when the target is already
            # inadmissible. Unknown/conflicting ownership keeps the stop path.
            preflight = False
            try:
                saved_path = root / "last-cgroups.json"
                saved = _json(saved_path) if os.path.lexists(saved_path) else dict.fromkeys(UNITS, "")
                preflight = (set(saved) == set(UNITS)
                    and sum(info["ActiveState"] == "active" for info in before.values()) <= 1
                    and all(info["Result"] == "success" and info["ControlPID"] == "0"
                        and info["Job"] in {"", "0"}
                        and ((info["ActiveState"] == "active" and info["SubState"] == "running"
                              and info["MainPID"].isdigit() and int(info["MainPID"]) > 0)
                             or (services.stopped(info)
                                 and services.cgroup_empty(info["ControlGroup"])
                                 and services.cgroup_empty(saved[name])))
                        for name, info in before.items()))
            except (ExclusiveProfileError, OSError, ValueError, TypeError, KeyError):
                pass  # Unavailable ownership proof must not bypass stop/cleanup.
            if preflight:
                load_profile(root, start=mode)
        try:
            services.stop_all(before)
            if mode != "off":
                load_profile(root, start=mode)
                probe_alsa(profile["renderer"]["audio_device"])
                services.command("start", UNITS[mode], timeout=15)
                started = services.wait_started(mode)
                after = services.validate()
                peer = "spotify" if mode == "renderer" else "renderer"
                if (after[mode]["ActiveState"] != "active" or after[mode]["MainPID"] == "0"
                    or (after[mode]["InvocationID"], after[mode]["MainPID"]) != started
                    or not services.stopped(after[peer])):
                    fail("The selected service did not remain active; no playback success is claimed.")
            result = {"requested_mode": mode, "service_state": "STOPPED" if mode == "off" else "PROCESS_STARTED",
                      "audio_output": "UNVERIFIED", "spotify_bypasses_orr_dsp": True,
                      "gates": GATES}
            _write(root / "last-operation.json", result)
            return result
        except (ExclusiveProfileError, OSError, ValueError, TypeError, KeyError, KeyboardInterrupt):
            # Never start a fallback. A timed-out start can still finish in
            # systemd; explicitly queue stops and retain UNKNOWN if that fails.
            try:
                services.command("stop", *UNITS.values(), timeout=STOP_TIMEOUT)
            except ExclusiveProfileError:
                pass
            try:
                _write(root / "last-operation.json", {"service_state": "UNKNOWN", "competing_start_blocked": True})
            except (OSError, ExclusiveProfileError):
                pass
            raise


def _target(root: Path, profile: dict, mode: str) -> list[str]:
    if mode == "renderer":
        return [str(Path(profile["renderer_prefix"]) / "venv/bin/open-room-renderer"),
                "serve", "--config", profile["renderer_config"]]
    return [str(Path(profile["client_dir"]) / "go-librespot"), "--config_dir", str(root / "sidecar")]


def run_guard(directory: str, mode: str) -> None:
    root, profile = load_profile(directory, start=mode)
    services = UserServices(root, profile)
    states = services.validate()
    # Invocation is allowed only from our own actual service cgroup. A user
    # running this private subcommand in a shell cannot impersonate that unit.
    own = Path("/proc/self/cgroup").read_text().strip()
    if (not own.startswith("0::") or own[3:] != states[mode]["ControlGroup"]
            or states[mode]["MainPID"] != str(os.getpid())):
        fail("The receiver guard must be launched by its exact owned user service.")
    peer = "spotify" if mode == "renderer" else "renderer"
    sibling = str(Path(own[3:]).parent / UNITS[peer])
    if (not services.stopped(states[peer]) or not services.cgroup_empty(states[peer]["ControlGroup"])
            or not services.cgroup_empty(sibling)):
        fail("The other complete service cgroup has not released ownership.")
    with _lock(root, "output.lock") as lock:
        probe_alsa(profile["renderer"]["audio_device"])
        if mode == "renderer":
            socket = Path(profile["renderer_prefix"]) / "run/mpv.sock"
            if os.path.lexists(socket):
                fail("An existing mpv socket cannot be adopted; inspect the stopped installation first.")
        argv = _target(root, profile, mode)
        os.umask(0o077)
        os.set_inheritable(lock, True)
        try:
            os.execv(argv[0], argv)
        except OSError:
            raise _ExecFailed from None


def recover(directory: str) -> dict:
    """An explicit, bounded off-only recovery after an earlier failed unit."""
    root, profile = load_profile(directory)
    with _lock(root, "control.lock"):
        services = UserServices(root, profile)
        before = services.validate()
        saved_path = root / "last-cgroups.json"
        saved = _json(saved_path) if saved_path.exists() else {}
        services.command("stop", *UNITS.values(), timeout=STOP_TIMEOUT)
        after = services.validate()
        for mode, info in after.items():
            if (info["ActiveState"] not in {"inactive", "failed"} or info["MainPID"] != "0"
                    or info["ControlPID"] != "0" or info["Job"] not in {"", "0"}
                    or not services.cgroup_empty(before[mode]["ControlGroup"])
                    or not services.cgroup_empty(saved.get(mode, ""))
                    or not services.cgroup_empty(info["ControlGroup"])):
                fail("Recovery cannot prove both complete cgroups stopped; no restart was issued.")
        failed = [UNITS[mode] for mode, info in after.items()
                  if info["ActiveState"] == "failed" or info["Result"] != "success"]
        if failed:
            try:
                services.command("reset-failed", *failed)
            except ExclusiveProfileError:
                # A failed unit can be cleared/collected by another operator
                # between observation and ResetFailedUnit. Only the complete
                # observed postcondition below can establish recovery.
                states = services.validate()
                if not all(services.stopped(info) and info["Result"] == "success"
                           and services.cgroup_empty(info["ControlGroup"])
                           and services.cgroup_empty(saved.get(mode, ""))
                           and services.cgroup_empty(before[mode]["ControlGroup"])
                           for mode, info in states.items()):
                    raise
        states = services.validate()
        if not all(services.stopped(info) and info["Result"] == "success"
                   and services.cgroup_empty(info["ControlGroup"])
                   and services.cgroup_empty(saved.get(mode, ""))
                   and services.cgroup_empty(before[mode]["ControlGroup"])
                   for mode, info in states.items()):
            fail("The failed service state could not be reconciled; no restart was issued.")
        result = {"service_state": "STOPPED", "recovery": "OFF_ONLY", "services_started": False}
        _write(root / "last-operation.json", result)
        return result


def refresh_renderer(directory: str) -> dict:
    """Admit edited native YAML on the same route, without touching services."""
    root = _path(directory)
    with _lock(root, "control.lock"):
        root, profile = load_profile(root)
        guidance = ("Renderer refresh cannot change native paths, unit or ALSA output. "
                    "Restore the owned paths/unit before receiver uninstall, then use receiver setup "
                    "in a new private directory for the new profile.")
        try:
            actual = _renderer(Path(profile["renderer_prefix"]), Path(profile["renderer_config"]),
                               Path(profile["renderer_unit"]), profile["port"])
        except YAMLError:
            fail("The edited native YAML is malformed; repair it before running refresh-renderer again.")
        except ExclusiveProfileError as exc:
            fail(str(exc) + " " + guidance)
        previous = profile["renderer"]
        if (type(previous) is not dict
                or actual != previous | {"config_sha256": actual["config_sha256"]}):
            fail(guidance)
        services = UserServices(root, profile)
        states = services.validate()
        saved_path = root / "last-cgroups.json"
        saved = _json(saved_path) if os.path.lexists(saved_path) else dict.fromkeys(UNITS, "")
        if set(saved) != set(UNITS):
            fail("The saved service cgroup record is incomplete; inspect the stopped setup before refreshing.")
        for mode, info in states.items():
            if (not services.stopped(info) or info["Result"] != "success"
                    or not services.cgroup_empty(info["ControlGroup"])
                    or not services.cgroup_empty(saved[mode])):
                fail("Renderer refresh requires both owned services successfully stopped with empty complete cgroups; "
                     "use receiver switch off, or receiver recover for a failed service, before refreshing.")
        if actual != previous:
            _write(root / "receiver.json", profile | {"renderer": actual})
        return {"renderer_refresh": "UPDATED" if actual != previous else "UNCHANGED",
                "service_state": "STOPPED", "services_started": False, "audio_output": "UNVERIFIED"}


def configure(directory: str, *, name: str | None = None, bitrate: int | None = None) -> dict:
    """Change only explicit Connect settings while both owned services are off."""
    changes: dict[str, str | int] = {}
    if name is not None:
        changes["device_name"] = _device_name(name)
    if bitrate is not None:
        changes["preferred_bitrate"] = _preferred_bitrate(bitrate)
    if not changes:
        fail("Choose --name and/or --bitrate explicitly; omitted settings are retained.")
    root = _path(directory)
    with _lock(root, "control.lock"):
        root, profile = load_profile(root)
        updated = profile | changes
        previous, desired = _sidecar_config(root, profile), _sidecar_config(root, updated)
        actual = _read_sidecar(root, profile)
        # One atomic sidecar replacement means the only recoverable split is
        # the exact requested config alongside the previous profile. Refuse
        # any retry that no longer describes that exact pending config.
        if not (_same_config(actual, previous) or _same_config(actual, desired)):
            fail("The owned sidecar configuration differs from both saved and requested settings. "
                 "After an interrupted configure, repeat the same explicit --name/--bitrate settings.")
        links = _install_links(root, profile)
        installation = _json(root / "installation.json")
        if (installation.get("state") != "INSTALLED"
                or installation.get("links") != {str(p): str(t) for p, t in links.items()}
                or any(not _exact_link(path, target) for path, target in links.items())):
            fail("Receiver configure requires this setup's intact installed service links.")
        services = UserServices(root, profile)
        states = services.validate()
        saved_path = root / "last-cgroups.json"
        saved = _json(saved_path) if os.path.lexists(saved_path) else dict.fromkeys(UNITS, "")
        if set(saved) != set(UNITS):
            fail("The saved service cgroup record is incomplete; inspect the stopped setup before configuring.")
        for mode, info in states.items():
            if (not services.stopped(info) or info["Result"] != "success"
                    or not services.cgroup_empty(info["ControlGroup"])
                    or not services.cgroup_empty(saved[mode])):
                fail("Receiver configure requires both owned services successfully stopped with empty complete cgroups; "
                     "use receiver switch off, or receiver recover for a failed service, before configuring.")
        changed = not _same_config(actual, desired) or updated != profile
        # Sidecar first, with file+directory fsync before metadata replacement.
        # An interrupted split fails normal Spotify admission; no rollback or
        # automatic retry. Explicitly repeating these settings completes it.
        if not _same_config(actual, desired):
            _write(root / "sidecar/config.yml", desired)
        if updated != profile:
            _write(root / "receiver.json", updated)
        return {"receiver_configuration": "UPDATED" if changed else "UNCHANGED",
                "service_state": "STOPPED", "services_started": False,
                "provider_settings": "UNVERIFIED",
                "next": "Explicitly switch to Spotify when ready; verify settings in the real client."}


def last_start_error(directory: str) -> dict:
    """Read only a fixed-category historical failure, independently of setup health."""
    root = _path(directory)
    _private_directory(root)
    result = {"record_scope": "HISTORICAL_NOT_CURRENT", "last_start_error": None}
    try:
        data = _read(root / "last-guard-error.json")
    except FileNotFoundError:
        return result
    if len(data) > 256:
        fail("The startup failure record is invalid; no record content was displayed.")
    # Tuple pairs distinguish an object from an array and reject duplicate keys.
    pairs = json.loads(data, object_pairs_hook=tuple)
    if type(pairs) is not tuple or len(pairs) != 2:
        fail("The startup failure record is invalid; no record content was displayed.")
    record = dict(pairs)
    if (set(record) != {"mode", "category"} or type(record["mode"]) is not str or record["mode"] not in UNITS
            or type(record["category"]) is not str or record["category"] not in START_FAILURE_HINTS):
        fail("The startup failure record is invalid; no record content was displayed.")
    return result | {"last_start_error": record | {"hint": START_FAILURE_HINTS[record["category"]]}}


def status(directory: str, *, playback: bool = False) -> dict:
    root, profile = load_profile(directory)
    services = UserServices(root, profile)
    states = services.validate()
    active = [mode for mode, info in states.items() if info["ActiveState"] == "active"]
    all_stopped = all(services.stopped(info) for info in states.values())
    selected = active[0] if len(active) == 1 else "off" if all_stopped else "UNKNOWN"
    result = {"selected_service": selected if len(active) < 2 else "CONFLICT",
            "services": {mode: {key: info[key] for key in ("ActiveState", "SubState", "Result")}
                         for mode, info in states.items()},
            "audio_output": "UNVERIFIED", "connect_session": "USE_SEPARATE_SPOTIFY_DIAGNOSE",
            "spotify_bypasses_orr_dsp": True, "gates": GATES}
    if playback:
        # Read only when Spotify is the sole selected service. A loopback API
        # report is still not proof that the owned process supplied it, nor an
        # atomic snapshot with systemd or evidence of audible output.
        observed = {"observation": "not_observed", "session_observation": "UNKNOWN",
                    "status": None, "snapshot_freshness": "unknown", "audio_output": "unverified",
                    "reason": "spotify_service_not_exclusive"}
        if selected == "spotify" and services.stopped(states["renderer"]):
            try:
                client = SpotifyDiagnosticClient(profile.get("host"), profile.get("port"))
                observed = asyncio.run(client.diagnose())
            except SpotifyDiagnosticError as exc:
                # Preserve the useful service snapshot even when the optional
                # API is unavailable or its response cannot be interpreted.
                observed.update(observation="unavailable", reason=str(exc))
        observed["listener_ownership"] = "unverified"
        result["provider_playback"] = observed
        result["connect_session"] = observed["session_observation"]
    return result


def playback_control(directory: str, action: str, value: int | bool | str | None = None,
                     *, replace_current: bool = False) -> dict:
    from .spotify_playback import control, now_playing
    from .spotify_pairing import pairing_code
    if type(replace_current) is not bool or (replace_current and action != "play"):
        fail("Spotify replace-current is a boolean opt-in for play only.")
    root, profile = load_profile(directory)
    with _lock(root, "control.lock"):
        # Current controls admit no new output owner. Use the stored renderer
        # route only to verify the unchanged sidecar; every source start still
        # requires fresh native profile/mpv validation in load_profile(start=).
        _check_spotify(root, profile)
        services = UserServices(root, profile)
        states = services.validate()
        if states["spotify"]["ActiveState"] != "active" or not services.stopped(states["renderer"]):
            fail("Select Spotify exclusively before using its playback commands; no request was sent.")
        try:
            identity = services.wait_started("spotify")
        except ExclusiveProfileError:
            fail("The owned Spotify process could not be confirmed; no request was sent.")

        def validate_invocation() -> None:
            current = services.validate()
            spotify = current["spotify"]
            if (spotify["ActiveState"] != "active" or (spotify["InvocationID"], spotify["MainPID"]) != identity
                    or not services.stopped(current["renderer"])
                    or not services.cgroup_empty(current["renderer"]["ControlGroup"])
                    or (Path("/proc") / identity[1] / "cgroup").read_text().strip()
                       != "0::" + spotify["ControlGroup"]):
                fail("The exclusive Spotify service invocation changed during the playback request.")

        if action == "play":
            result = control(profile["host"], profile["port"], identity[1], action, value, validate_invocation,
                             replace_current=replace_current)
        elif action == "pairing-code":
            result = pairing_code(profile["host"], profile["port"], identity[1], validate_invocation)
        elif action == "now-playing":
            result = now_playing(profile["host"], profile["port"], identity[1], validate_invocation)
        else:
            result = control(profile["host"], profile["port"], identity[1], action, value, validate_invocation)
        return result | {"spotify_bypasses_orr_dsp": True, "gates": GATES}


def setup(args: argparse.Namespace) -> dict:
    name = _device_name(getattr(args, "name", DEFAULT_DEVICE_NAME))
    bitrate = getattr(args, "bitrate", None)
    if bitrate is not None:
        bitrate = _preferred_bitrate(bitrate)
    root = _path(args.directory)
    prefix, config, unit, client = map(_path, (args.renderer_prefix, args.renderer_config,
                                              args.renderer_unit, args.client_dir))
    address = ipaddress.ip_address(args.host)
    if (address.version != 4 or not address.is_loopback or str(address) != args.host
            or not 1024 <= args.port <= 65535):
        fail("Use canonical IPv4 loopback and an explicit unprivileged Spotify API port.")
    if any(root == path or root in path.parents or path in root.parents for path in (prefix, client)):
        fail("The receiver setup, native renderer and client must be separate private directories.")
    if any(c in str(path) for path in (root, prefix, client) for c in "\"'"):
        fail("Executable and setup paths cannot contain literal quote characters.")
    _check_mpv()
    _, receipt = verify_client(client)
    renderer = _renderer(prefix, config, unit, args.port)
    profile = {"format": 1, "directory": str(root), "renderer_prefix": str(prefix),
               "renderer_config": str(config), "renderer_unit": str(unit), "client_dir": str(client),
               "client_sha256": receipt["binary_sha256"], "host": args.host, "port": args.port,
               "renderer": renderer, "gates": GATES, "device_name": name}
    if bitrate is not None:
        profile["preferred_bitrate"] = bitrate
    files = _unit_parts(root, profile)
    files["sidecar/config.yml"] = json.dumps(_sidecar_config(root, profile), indent=2) + "\n"
    files["receiver.json"] = json.dumps(profile, indent=2) + "\n"
    with _directory(root.parent) as parent:
        os.mkdir(root.name, 0o700, dir_fd=parent)
    for name in ("sidecar", RENDERER_SERVICE + ".d"):
        (root / name).mkdir(mode=0o700)
    for name, body in files.items():
        with _directory((root / name).parent) as directory:
            fd = os.open(Path(name).name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, "w") as handle:
                handle.write(body)
    return {"setup": "PREPARED", "client": receipt["version"], "services_started": False,
            "gates": GATES, "next": "Review setup, then run receiver install explicitly."}


def _user_units() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return _path(base) / "systemd/user"


def _ensure_user_directory(path: Path) -> None:
    """Create only missing path components without following ancestor links."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            fail("The user unit directory must be owned by you and not writable by other users.")
    finally:
        os.close(fd)


def _exact_link(path: Path, target: Path) -> bool:
    return path.is_symlink() and os.readlink(path) == str(target)


def _install_links(root: Path, profile: dict) -> dict[Path, Path]:
    units = _user_units()
    return {units / SPOTIFY_SERVICE: root / SPOTIFY_SERVICE,
            units / (RENDERER_SERVICE + ".d") / DROPIN: root / (RENDERER_SERVICE + ".d") / DROPIN}


def install(directory: str) -> dict:
    root, profile = load_profile(directory, start="spotify")
    with _lock(root, "control.lock"):
        services = UserServices(root, profile)
        services.command("show", "--property=Version")
        units = _user_units()
        _ensure_user_directory(units)
        with _directory(units):
            pass
        native = units / RENDERER_SERVICE
        canonical = Path(profile["renderer_unit"])
        if os.path.lexists(native) and not _exact_link(native, canonical):
            if native.is_symlink() or _read(native) != _read(canonical):
                fail("The existing renderer unit is not the exact native unit; preserve and review it manually first.")
        links = _install_links(root, profile)
        expected_links = {str(p): str(t) for p, t in links.items()}
        previous = _json(root / "installation.json") if (root / "installation.json").exists() else None
        resume = previous is not None and previous.get("state") in {"INSTALLING", "INSTALLED"}
        if previous is not None and (previous.get("links") != expected_links
                                     or previous.get("state") not in {"INSTALLING", "INSTALLED", "UNINSTALLED"}):
            fail("A different installation receipt already exists; no service files were changed.")
        dropdir = units / (RENDERER_SERVICE + ".d")
        if dropdir.exists() and (not dropdir.is_dir() or any(
                path != dropdir / DROPIN or not resume or not _exact_link(path, links[path])
                for path in dropdir.iterdir())):
            fail("An existing renderer drop-in must be reviewed and removed manually before installation.")
        if any(os.path.lexists(path) and (not resume or not _exact_link(path, target)) for path, target in links.items()):
            fail("Receiver service destinations already exist; do not overwrite another installation.")
        existing = services.show("renderer")
        if existing["LoadState"] == "loaded":
            dropins = [str(Path(p).resolve()) for p in shlex.split(existing["DropInPaths"])]
            permitted = [str(root / (RENDERER_SERVICE + ".d") / DROPIN)]
            if (Path(existing["FragmentPath"]).resolve() not in {canonical, native}
                    or (dropins and (not resume or dropins != permitted))
                    or (existing["NeedDaemonReload"] != "no" and not resume)
                    or _read(canonical).decode() != user_unit(
                        Path(profile["renderer_prefix"]), config=Path(profile["renderer_config"]))):
                fail("The currently loaded renderer service is not the unchanged native installation.")
            if not services.stopped(existing) or existing["Result"] != "success":
                fail("Stop the existing native renderer and confirm its whole cgroup is empty before installing receiver mode.")
            if not services.cgroup_empty(existing["ControlGroup"]):
                fail("The original renderer still owns processes; installation remains blocked.")
        elif existing["LoadState"] != "not-found":
            fail("The original renderer's service state is unknown.")
        spotify = services.show("spotify")
        if spotify["LoadState"] != "not-found" and (not resume
                or spotify["LoadState"] != "loaded"
                or Path(spotify["FragmentPath"]).resolve() != root / SPOTIFY_SERVICE
                or spotify["DropInPaths"] or not services.stopped(spotify)
                or not services.cgroup_empty(spotify["ControlGroup"])):
            fail("A Spotify unit already exists in this user manager; it is not owned by this setup.")
        created_native = not os.path.lexists(native)
        _write(root / "installation.json", {"format": 1, "state": "INSTALLING",
                                            "native_link_created": created_native or bool(previous and previous.get("native_link_created")),
                                            "links": expected_links})
        if created_native:
            native.symlink_to(canonical)
        dropdir.mkdir(exist_ok=True, mode=0o700)
        with _directory(dropdir):
            pass
        for path, target in links.items():
            if not os.path.lexists(path):
                path.symlink_to(target)
        services.command("daemon-reload")
        services.validate()
        record = _json(root / "installation.json")
        _write(root / "installation.json", record | {"state": "INSTALLED"})
        return {"installation": "INSTALLED", "services_started": False, "boot_policy": "UNCHANGED", "gates": GATES}


def _startup_targets(root: Path, profile: dict) -> dict[str, set[str]]:
    return {"renderer": {profile["renderer_unit"], str(_user_units() / RENDERER_SERVICE),
                         "../" + RENDERER_SERVICE},
            "spotify": {str(root / SPOTIFY_SERVICE)}}


def _startup_snapshot(services: UserServices) -> dict[str, str | None]:
    """Admit only our two persistent default.target links, never global policy."""
    units = _user_units()
    wanted = units / "default.target.wants"
    targets = _startup_targets(services.root, services.profile)
    observed = dict.fromkeys(UNITS)
    if os.path.lexists(wanted):
        with _directory(wanted) as directory:
            info = os.fstat(directory)
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                fail("Startup links require an owned, non-shared default.target.wants directory.")
            for mode, unit in UNITS.items():
                try:
                    info = os.stat(unit, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid():
                    fail("An unfamiliar startup link must be reviewed manually; no policy was changed.")
                value = os.readlink(unit, dir_fd=directory)
                if value not in targets[mode]:
                    fail("An unfamiliar startup link must be reviewed manually; no policy was changed.")
                observed[mode] = value
    raw = services.command("show", "--property=UnitPath")
    if not raw.startswith("UnitPath=") or len(raw.splitlines()) != 1:
        fail("The user manager's unit lookup paths cannot be verified.")
    paths = [_path(value) for value in shlex.split(raw.strip().removeprefix("UnitPath="))]
    if units not in paths or len(paths) > 64:
        fail("The selected user unit directory is not a recognized manager lookup path.")
    fragments = {Path(services.profile["renderer_unit"]), services.root / SPOTIFY_SERVICE}
    expected = {units / unit for unit in UNITS.values()} | {wanted / unit for unit in UNITS.values()}
    count = 0
    for base in paths:
        if not base.exists():
            continue
        for entry in base.iterdir():
            count += 1
            if count > 16384:
                fail("The unit lookup layout exceeds the bounded startup inspection limit.")
            # Include aliases and all dependency-link kinds, including runtime
            # and global user-unit directories from the actual manager.
            entries = entry.iterdir() if entry.name.endswith((".wants", ".requires", ".upholds")) else (entry,)
            for path in entries:
                count += 1
                if count > 16384:
                    fail("The unit lookup layout exceeds the bounded startup inspection limit.")
                try:
                    relevant = path.name in UNITS.values() or (path.is_symlink() and (
                        Path(os.readlink(path)).name in UNITS.values() or path.resolve() in fragments))
                except (OSError, RuntimeError):
                    fail("A unit lookup link cannot be inspected; review startup policy manually before selecting a source.")
                if relevant and path not in expected:
                    fail("Global, runtime, aliased or custom receiver enablement needs manual review; no policy was changed.")
    if sum(value is not None for value in observed.values()) > 1:
        fail("Both receiver startup links are present; review them manually before selecting a source.")
    return observed


def _startup_record(root: Path, profile: dict) -> dict | None:
    path = root / "startup.json"
    if not os.path.lexists(path):
        return None
    record = _json(path)
    targets = _startup_targets(root, profile)
    if (record.get("format") != 1 or record.get("unit_directory") != str(_user_units())
            or record.get("state") not in {"APPLYING", "SELECTED", "RESTORED"}
            or record.get("selection") not in {*UNITS, "off", "restore"}
            or record.get("original_renderer") not in {None, *targets["renderer"]}):
        fail("The startup ownership receipt is invalid; preserve it for manual review.")
    for key in ("from", "to"):
        value = record.get(key)
        if (type(value) is not dict or set(value) != set(UNITS)
                or any(value[mode] not in {None, *targets[mode]} for mode in UNITS)
                or sum(item is not None for item in value.values()) > 1):
            fail("The startup ownership receipt is invalid; preserve it for manual review.")
    target = _startup_choice(root, profile, record["selection"], record["original_renderer"])
    if (record["to"] != target or (record["state"] == "RESTORED" and record["selection"] != "restore")
            or (record["state"] == "SELECTED" and record["selection"] == "restore")):
        fail("The startup ownership receipt is invalid; preserve it for manual review.")
    return record


def _startup_choice(root: Path, profile: dict, selection: str, original: str | None) -> dict[str, str | None]:
    target = dict.fromkeys(UNITS)
    if selection == "restore":
        target["renderer"] = original
    elif selection == "renderer":
        target["renderer"] = original or profile["renderer_unit"]
    elif selection == "spotify":
        target["spotify"] = str(root / SPOTIFY_SERVICE)
    return target


def _startup_change(services: UserServices, selection: str) -> dict:
    root, profile = services.root, services.profile
    observed = _startup_snapshot(services)
    record = _startup_record(root, profile)
    if record is None:
        if observed["spotify"] is not None:
            fail("Spotify startup has no ownership receipt; review its existing link manually.")
        original = observed["renderer"]
    else:
        original = record["original_renderer"]
        if record["state"] == "APPLYING":
            if any(observed[mode] not in {None, record["from"][mode], record["to"][mode]} for mode in UNITS):
                fail("A pending startup link changed outside this setup; no policy was changed.")
            if selection not in {"status", "restore", record["selection"]}:
                fail("Repeat the pending startup selection or use startup restore before choosing another source.")
        elif observed != record["to"]:
            fail("Startup links changed outside this setup; preserve them for manual review.")
    current = next((mode for mode, value in observed.items() if value is not None), "off")
    if selection == "status":
        return {"selected_startup": current, "startup_state": record["state"] if record else "UNCHANGED",
                "requested_startup": record["selection"] if record else None,
                "original_renderer_enabled": original is not None, "services_started": False}
    target = _startup_choice(root, profile, selection, original)
    record = {"format": 1, "unit_directory": str(_user_units()), "original_renderer": original,
              "state": "APPLYING", "selection": selection, "from": observed, "to": target}
    _write(root / "startup.json", record)
    wanted = _user_units() / "default.target.wants"
    _ensure_user_directory(wanted)
    with _directory(wanted) as directory:
        for mode, unit in UNITS.items():
            if observed[mode] is not None:
                info = os.stat(unit, dir_fd=directory, follow_symlinks=False)
                if (not stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid()
                        or os.readlink(unit, dir_fd=directory) != observed[mode]):
                    fail("A startup link changed during selection; resume only after manual review.")
                os.unlink(unit, dir_fd=directory)
                os.fsync(directory)
        # Removal precedes creation: interruption can leave both off, never two
        # enabled peers. A new foreign destination is never overwritten.
        for mode, value in target.items():
            if value is not None:
                os.symlink(value, UNITS[mode], dir_fd=directory)
                os.fsync(directory)
    services.command("daemon-reload")
    if _startup_snapshot(services) != target:
        fail("Startup selection could not be confirmed; inspect startup status before resuming.")
    state = "RESTORED" if selection == "restore" else "SELECTED"
    _write(root / "startup.json", record | {"state": state})
    return {"selected_startup": next((mode for mode, value in target.items() if value is not None), "off"),
            "startup_state": state, "services_started": False, "current_services": "UNCHANGED",
            "applies_to": "NEXT_USER_MANAGER_START", "lingering": "UNCHANGED"}


def startup(directory: str, selection: str) -> dict:
    if selection not in {"status", "renderer", "spotify", "off", "restore"}:
        fail("Choose startup status, renderer, spotify, off or restore explicitly.")
    root, profile = load_profile(directory)
    with _lock(root, "control.lock"):
        services = UserServices(root, profile)
        links = _install_links(root, profile)
        installation = _json(root / "installation.json")
        if (installation.get("state") != "INSTALLED"
                or installation.get("links") != {str(p): str(t) for p, t in links.items()}
                or any(not _exact_link(path, target) for path, target in links.items())):
            fail("Startup selection requires this setup's intact installed service links.")
        services.validate()
        if selection in UNITS:
            load_profile(root, start=selection)
        return _startup_change(services, selection)


def uninstall(directory: str) -> dict:
    root, profile = load_profile(directory)
    with _lock(root, "control.lock"):
        services = UserServices(root, profile)
        links = _install_links(root, profile)
        record = _json(root / "installation.json")
        if record.get("links") != {str(p): str(t) for p, t in links.items()}:
            fail("Receiver installation ownership cannot be verified; no files were removed.")
        resuming = record.get("state") == "UNINSTALLING"
        if any(not _exact_link(path, target) and (not resuming or os.path.lexists(path))
               for path, target in links.items()):
            fail("An installed receiver link changed; preserve it for manual review.")
        if not resuming:
            services.stop_all(services.validate())
            if os.path.lexists(root / "startup.json"):
                _startup_change(services, "restore")
            _write(root / "installation.json", record | {"state": "UNINSTALLING"})
        else:
            # Some links may already be absent and systemd may need a reload.
            # Do not infer full unit admission from that partial layout. Only
            # continue removal while every observed/saved cgroup is still off.
            saved = _json(root / "last-cgroups.json")
            for mode in UNITS:
                info = services.show(mode)
                if (info["LoadState"] not in {"loaded", "not-found"} or not services.stopped(info)
                        or not services.cgroup_empty(info["ControlGroup"])
                        or not services.cgroup_empty(saved.get(mode, ""))):
                    fail("Interrupted uninstall cannot confirm both services remain stopped; no more links were removed.")
            if os.path.lexists(root / "startup.json"):
                startup_record = _startup_record(root, profile)
                if (startup_record["state"] != "RESTORED"
                        or _startup_snapshot(services) != startup_record["to"]):
                    fail("Interrupted uninstall cannot verify restored native startup; no more links were removed.")
        for path in links:
            if os.path.lexists(path):
                path.unlink()
        try:
            (next(p for p in links if p.name == DROPIN)).parent.rmdir()
        except OSError:
            pass
        services.command("daemon-reload")
        _write(root / "installation.json", record | {"state": "UNINSTALLED"})
        return {"installation": "UNINSTALLED", "services_started": False,
                "credentials_and_config": "RETAINED", "renderer": "original native unit restored, stopped"}


def add_receiver_parser(commands) -> None:
    client = commands.add_parser("install-client", help="verify and privately extract a pinned official archive; no execution")
    source = client.add_mutually_exclusive_group(required=True)
    source.add_argument("--archive", help="existing pinned official archive for offline installation")
    source.add_argument("--download", action="store_true", help="download the pinned official release for this native userspace")
    client.add_argument("--output-dir", required=True)
    receiver = commands.add_parser("receiver", help="explicit exclusive receiver user-service lifecycle")
    receiver.add_argument("--directory", required=True, help="private receiver setup directory")
    actions = receiver.add_subparsers(dest="receiver_cmd", required=True)
    inspect = actions.add_parser("status", help="show service state without contacting either player")
    inspect.add_argument("--playback", action="store_true",
                         help="also read a sanitized snapshot from the configured Spotify loopback API")
    actions.add_parser("last-start-error", help="read the last recorded guard failure; historical, not current health")
    for action in ("install", "uninstall", "recover"):
        actions.add_parser(action)
    actions.add_parser("refresh-renderer", help="accept same-route native YAML edits after both owned services are stopped")
    settings = actions.add_parser("configure", help="change this receiver's name and/or preferred bitrate while both owned services are stopped")
    settings.add_argument("--name", help="Spotify Connect device name; omission keeps the saved name")
    settings.add_argument("--bitrate", type=int, choices=(96, 160, 320),
                          help="preferred Spotify bitrate in kbps; omission keeps the saved preference")
    boot = actions.add_parser("startup", help="explicitly select the next user-manager session; no immediate activation")
    boot.add_argument("selection", choices=("status", "renderer", "spotify", "off", "restore"))
    play = actions.add_parser("play", help="launch a Spotify track, album or playlist URI; stopped-only unless explicitly replacing")
    play.add_argument("value", metavar="SPOTIFY_URI")
    play.add_argument("--replace-current", action="store_true",
                      help="also allow playing or paused content to be replaced and played at the current Spotify volume")
    enqueue = actions.add_parser("enqueue", help="append one track to Spotify's existing queue without starting or resuming playback")
    enqueue.add_argument("value", metavar="SPOTIFY_TRACK_URI")
    for action in ("pause", "resume"):
        actions.add_parser(action, help=f"request {action} from the already selected Spotify client")
    actions.add_parser("next", help="request the next track in Spotify's existing context; may resume paused playback")
    actions.add_parser("previous", help="request Spotify's previous track or rewind the current track after 3 seconds")
    seek = actions.add_parser("seek", help="request an absolute position within Spotify's reported track duration")
    seek.add_argument("value", type=int, metavar="MILLISECONDS")
    actions.add_parser("now-playing", help="explicitly display the owned Spotify client's current track and playback state")
    actions.add_parser("pairing-code", help="display an already-issued temporary user code; no authentication start or retry")
    volume = actions.add_parser("volume", help="set Spotify's own absolute volume; raise cautiously from zero")
    volume.add_argument("value", type=int, metavar="0..100")
    for action in ("repeat-context", "repeat-track", "shuffle"):
        option = actions.add_parser(action, help=f"set Spotify's {action} option for existing playback")
        option.add_argument("value", choices=("on", "off"))
    change = actions.add_parser("switch", help="stop both complete services before starting one; off starts neither")
    change.add_argument("mode", choices=("renderer", "spotify", "off"))
    guard = actions.add_parser("run", help=argparse.SUPPRESS)
    guard.add_argument("mode", choices=("renderer", "spotify"))
    setup = actions.add_parser("setup", help="create private runnable setup; no service activation")
    for name in ("renderer-prefix", "renderer-config", "renderer-unit", "client-dir"):
        setup.add_argument("--" + name, required=True)
    setup.add_argument("--port", type=int, required=True)
    setup.add_argument("--host", default="127.0.0.1")
    setup.add_argument("--name", default=DEFAULT_DEVICE_NAME,
                       help="Spotify Connect device name for this fresh setup (default: %(default)s)")
    setup.add_argument("--bitrate", type=int, choices=(96, 160, 320),
                       help="preferred Spotify bitrate in kbps for this fresh setup; omission keeps upstream's 160 preference")


def run_receiver(args: argparse.Namespace) -> int:
    try:
        if sys.platform != "linux" or os.geteuid() == 0:
            fail("Use the exclusive receiver as a normal Linux user, never root.")
        if args.spotify_cmd == "install-client":
            result = install_client(args.archive, args.output_dir, download=args.download)
        elif args.receiver_cmd == "switch":
            result = switch(args.directory, args.mode)
        elif args.receiver_cmd == "status":
            result = status(args.directory, playback=args.playback)
        elif args.receiver_cmd == "last-start-error":
            result = last_start_error(args.directory)
        elif args.receiver_cmd == "startup":
            result = startup(args.directory, args.selection)
        elif args.receiver_cmd in {"play", "enqueue", "pause", "resume", "next", "previous", "seek", "volume", "now-playing", "pairing-code",
                                    "repeat-context", "repeat-track", "shuffle"}:
            value = getattr(args, "value", None)
            if args.receiver_cmd in {"repeat-context", "repeat-track", "shuffle"}:
                value = value == "on"
            if args.receiver_cmd == "play":
                result = playback_control(args.directory, args.receiver_cmd, value,
                                          replace_current=args.replace_current)
            else:
                result = playback_control(args.directory, args.receiver_cmd, value)
        elif args.receiver_cmd == "recover":
            result = recover(args.directory)
        elif args.receiver_cmd == "refresh-renderer":
            result = refresh_renderer(args.directory)
        elif args.receiver_cmd == "configure":
            result = configure(args.directory, name=args.name, bitrate=args.bitrate)
        elif args.receiver_cmd == "run":
            run_guard(args.directory, args.mode)
            return 1  # exec never returns on success
        elif args.receiver_cmd == "setup":
            result = setup(args)
        elif args.receiver_cmd == "install":
            result = install(args.directory)
        else:
            result = uninstall(args.directory)
        print(json.dumps(result, sort_keys=True))
        return 1 if (result.get("provider_playback", {}).get("observation") == "unavailable" or
                     "error" in result or result.get("request_outcome", "ACKNOWLEDGED") != "ACKNOWLEDGED") else 0
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, subprocess.SubprocessError) as exc:
        guard = getattr(args, "receiver_cmd", None) == "run"
        if guard:
            try:
                root = _path(args.directory)
                _private_directory(root)
                _write(root / "last-guard-error.json", {"mode": args.mode,
                    "category": "EXEC_FAILED" if isinstance(exc, _ExecFailed) else
                    "GUARD_REJECTED" if isinstance(exc, ExclusiveProfileError) else "STARTUP_FAILED"})
            except (OSError, ExclusiveProfileError):
                pass
        message = ("Receiver: " + str(exc) if isinstance(exc, ExclusiveProfileError) and not guard else
                   "Receiver operation failed; inspect private setup and service state. No playback success is claimed.")
        if getattr(args, "receiver_cmd", None) == "configure" and not isinstance(exc, ExclusiveProfileError):
            message = ("Receiver configure failed; inspect private setup and confirm both services stopped, "
                       "then explicitly repeat the same --name/--bitrate settings. No automatic retry was issued.")
        print(message, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        message = ("Receiver configure interrupted; confirm both services stopped, then explicitly repeat "
                   "the same --name/--bitrate settings. No automatic retry was issued."
                   if getattr(args, "receiver_cmd", None) == "configure" else
                   "Receiver interrupted; inspect service and playback status before retrying.")
        print(message, file=sys.stderr)
        return 130
