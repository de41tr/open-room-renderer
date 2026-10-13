"""Offline, credential-free artifacts for a manually gated native Spotify mode.

No subprocess, network, service-manager, mpv, socket or audio-device operations.
The proposal deliberately does not inspect effective units or claim ownership.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import fields
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys

import yaml

from .config import Config, ExperimentalSource, PodcastPreset
from .native_profile import systemd_quote, user_unit

UPSTREAM_VERSION = "v0.10.3"
UPSTREAM_COMMIT = "4dbc099f46da3529adaa85feb97c4f49deb0b130"
UPSTREAM = "https://github.com/devgianlu/go-librespot/blob/" + UPSTREAM_COMMIT
RENDERER_SERVICE = "open-room-renderer.service"
SPOTIFY_SERVICE = "open-room-renderer-spotify.service"
MAX_INPUT = 262144
GATES = ["TERMS_PENDING", "ACCOUNT_PENDING", "HARDWARE_PENDING", "ARBITRATION_PENDING"]


class ExclusiveProfileError(ValueError):
    """Diagnostics contain only fixed, operator-safe messages."""


def _fail(message: str) -> None:
    raise ExclusiveProfileError(message)


def _path(value: object) -> Path:
    if not isinstance(value, (str, Path)):
        _fail("Use explicit absolute, normalized paths without symlinks or expansion characters.")
    text = str(value)
    path = Path(text)
    if (not path.is_absolute() or text.startswith("//") or str(path) != text or path == Path("/")
            or any(part in {".", ".."} for part in text.split("/"))
            or any(ord(c) < 32 or ord(c) == 127 or c in "$\\" for c in text)
            or text != text.rstrip()):
        _fail("Use explicit absolute, normalized paths without symlinks or expansion characters.")
    return path


@contextmanager
def _directory(path: Path):
    """Open each ancestor without following links; retain a directory capability."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def _read(path: Path, *, binary: bool = False) -> bytes:
    with _directory(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022
                    or (not binary and info.st_uid != os.geteuid())):
                _fail("Inputs must be owned regular files, without group/other write access or symlinks.")
            if binary:
                if not info.st_mode & 0o111 or info.st_mode & 0o6000 or os.read(fd, 4) != b"\x7fELF":
                    _fail("The separately installed client must be a regular Linux ELF executable, not a wrapper.")
                return b""  # Do not execute, import, hash or inspect client contents.
            if info.st_size > MAX_INPUT:
                _fail("A profile input exceeds the bounded size limit.")
            data = os.read(fd, MAX_INPUT + 1)
            if len(data) > MAX_INPUT:
                _fail("A profile input exceeds the bounded size limit.")
            return data
        finally:
            os.close(fd)


class _UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if type(key) is not str or key in result:
            _fail("Profile mappings must have unique string keys; duplicate keys and YAML merges are unsupported.")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _document(data: bytes) -> dict:
    text = data.decode("utf-8")
    depth = count = 0
    for event in yaml.parse(text, Loader=yaml.SafeLoader):
        count += 1
        if count > 10000 or isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
            _fail("Profile aliases, anchors and excessively complex documents are unsupported.")
        if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
            depth += 1
            if depth > 16:
                _fail("Profile nesting exceeds the bounded depth limit.")
        elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
            depth -= 1
    result = yaml.load(text, Loader=_UniqueLoader)
    if type(result) is not dict:
        _fail("A profile input must be a mapping.")
    return result


def _private_directory(path: Path) -> None:
    with _directory(path) as fd:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            _fail("The native prefix, state and mpv runtime directories must be owned by you and mode 0700.")


def _overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _renderer(prefix: Path, config: Path, unit: Path, port: int) -> dict:
    if config not in {prefix / "config.yaml", prefix / "migrated-profile/config.yaml"}:
        _fail("Only root or migrated-profile configuration in the existing native prefix is supported.")
    if unit not in {prefix / RENDERER_SERVICE, prefix / "migrated-profile" / RENDERER_SERVICE}:
        _fail("Select the canonical native unit proposal in the existing prefix; custom units are unsupported.")
    _private_directory(prefix)
    _private_directory(prefix / "run")
    receipt = _document(_read(prefix / "installation-receipt.json"))
    if receipt.get("format") != 1 or receipt.get("mode") != "installed":
        _fail("The renderer prefix must have a completed native installation receipt.")
    _read(prefix / "venv/bin/open-room-renderer")
    unit_data = _read(unit)
    if unit_data.decode("utf-8") != user_unit(prefix, config=config):
        _fail("The selected unit is customized or unsupported; use the exact canonical native unit proposal.")
    source = _read(config)
    raw = _document(source)
    if set(raw) - {field.name for field in fields(Config)}:
        _fail("Renderer configuration contains unsupported settings.")
    for key, model in (("podcast_presets", PodcastPreset), ("experimental_sources", ExperimentalSource)):
        items = raw.get(key, [])
        if (type(items) is not list or any(type(item) is not dict or set(item) - {f.name for f in fields(model)} for item in items)):
            _fail("Renderer configuration contains unsupported preset or source settings.")
    if raw.get("auto_spawn_mpv") is not True or raw.get("mpv_socket") != str(prefix / "run/mpv.sock"):
        _fail("Require auto_spawn_mpv: true and the explicit native prefix/run/mpv.sock path.")
    if len(os.fsencode(prefix / "run/mpv.sock")) >= 108:
        _fail("The explicit native mpv socket path exceeds the Linux path limit.")
    state = _path(raw.get("state_dir"))
    if state not in {prefix / "state", prefix / "migrated-profile/state"}:
        _fail("Renderer state must use its private root or migrated-profile state directory.")
    _private_directory(state)
    route = raw.get("mpv_audio_device")
    if (type(route) is not str or not re.fullmatch(r"alsa/hw:CARD=[A-Za-z0-9_-]{1,64},DEV=(?:0|[1-9][0-9]{0,2})", route)):
        _fail("Require an explicit direct route: mpv_audio_device: alsa/hw:CARD=NAME,DEV=N.")
    if raw.get("mirror_port_80", False) is not False:
        _fail("Only the unprivileged native HTTP profile is supported.")
    for key in ("http_api_port", "upnp_port"):
        number = raw.get(key)
        if type(number) is not int or not 1024 <= number <= 65535 or number == port:
            _fail("Renderer and Spotify ports must be explicit, unprivileged and noncolliding.")
    return {"audio_device": route.removeprefix("alsa/"),
            "config_sha256": hashlib.sha256(source).hexdigest(),
            "unit_sha256": hashlib.sha256(unit_data).hexdigest()}


def _spotify_config(route: str, host: str, port: int, output: Path) -> dict:
    return {
        "device_name": "Open Room Renderer Spotify", "device_type": "speaker",
        "audio_backend": "alsa", "audio_device": route, "mixer_device": "",
        "external_volume": False, "volume_steps": 100, "initial_volume": 0,
        "ignore_last_volume": True, "disable_autoplay": True,
        "crossfade_duration": 0, "zeroconf_enabled": False, "mpris_enabled": False,
        "credentials": {"type": "device_auth"},
        "server": {"enabled": True, "address": host, "port": port, "allow_origin": "",
                   "cert_file": "", "key_file": ""},
        "cache": {"enabled": False, "dir": str(output / "sidecar/cache"), "size_limit": "100MB"},
        "metadata": {"enabled": False, "context_sweep": False},
        "log_level": "info",
    }


def _spotify_unit(binary: Path, output: Path) -> str:
    return f"""# Manual proposal only; no boot enablement. Same user manager as the renderer.
[Unit]
Description=Open Room Renderer optional Spotify exclusive mode
Conflicts={RENDERER_SERVICE}
After={RENDERER_SERVICE}

[Service]
Type=simple
ExecStart={systemd_quote(binary)} --config_dir {systemd_quote(output / 'sidecar')}
KillMode=control-group
SendSIGKILL=yes
TimeoutStopSec=45
Restart=no
UMask=0077
NoNewPrivileges=yes
"""


RENDERER_DROPIN = """# Manual proposal for the existing canonical renderer, not a second renderer.
[Service]
KillMode=control-group
SendSIGKILL=yes
TimeoutStopSec=45
Restart=no
"""


def _guide(prefix: Path, config: Path, unit: Path, binary: Path, output: Path, host: str, port: int) -> str:
    q = shlex.quote
    return f"""# Manual exclusive-mode proposal: OFFLINE_ARTIFACT_ONLY

TERMS_PENDING / ACCOUNT_PENDING / HARDWARE_PENDING / ARBITRATION_PENDING.
This is not legal clearance, playback-ready evidence, an installed service, or
an automatic fail-closed supervisor. Never run both engines to test ownership.

## What was prepared

- Existing native prefix: {prefix}
- Explicit renderer config: {config}
- Checked proposal (NOT the installed effective unit): {unit}
- Separately installed client path (NOT executed/version-verified): {binary}
- Expected client: go-librespot {UPSTREAM_VERSION}, commit {UPSTREAM_COMMIT}
- Same user manager; canonical renderer name: {RENDERER_SERVICE}
- Private sidecar config/state: {output / 'sidecar'}
- Loopback API: {host}, port {port}; empty CORS is not authentication.

The Spotify stream goes directly to the same ALSA hw device. It bypasses ORR
mpv EQ/balance and has independent volume. Initial volume is zero on every
start; this does not prevent a remote Connect controller from raising it.
The generator reads only the supplied profile/config and client ELF header.
It does not read saved renderer state or import sidecar/account credentials.
Keep this whole directory private (0700; files 0600), at this absolute path.
Receipt hashes describe artifact completeness only, not crash/power-loss
durability. Recheck all hashes before manual installation.
Future state.json, credentials.json and lockfile belong only in sidecar/.
Never include sidecar state or logs/pairing codes in renderer exports/support bundles.

## Before manual installation or any client execution

1. Resolve the provider terms/account eligibility and obtain explicit consent
   for real authentication, network traffic and device testing. Review the
   exact pinned upstream source and separately installed binary provenance,
   version, Linux/ARM ABI and dependencies. An ELF header is NOT provenance or
   version verification. No binary is distributed here.
2. Use your normal Linux account, never sudo or a system-level unit. Verify the
   existing service really uses this canonical executable/config in the same
   user manager. Customized units, wrappers, extra arguments, multiple renderer
   profiles/managers, shared audio servers and independent launch paths are
   outside this proposal. Inspect all effective units and drop-ins, including
   ExecStart, ExecStop, Restart, KillMode, SendSIGKILL, stop deadlines, conflicts
   and ordering. If anything differs, stop and reconcile manually.
3. Manually disable any prior renderer boot enablement before installing this
   proposal: systemctl --user disable {RENDERER_SERVICE}
   Disable independent Spotify/renderer autostart, timers, sockets, desktop
   launchers, cron, supervisors, lingering-triggered startup or alternate units.
   Do not enable Spotify at boot. No [Install] section is supplied for it.
   Avoid simultaneous or queued starts. Do not kill foreign processes.
4. Complete the stop-and-release procedure below for BOTH engines. On the first
   installation only, if the Spotify unit is genuinely absent, verify that
   absence in the intended user manager (including aliases/drop-ins), verify no
   independently launched Spotify process or foreign DAC owner, and complete
   the renderer stop/cgroup/physical-release checks. Do not call an absent unit
   a successful stop or use this exception for an unknown/failed installed unit.
   While both engines are confirmed off, manually review/copy {q(str(output / SPOTIFY_SERVICE))} to
   ~/.config/systemd/user/{SPOTIFY_SERVICE}, and the supplied
   {q(str(output / (RENDERER_SERVICE + '.d') / '90-spotify-exclusive.conf'))}
   to ~/.config/systemd/user/{RENDERER_SERVICE}.d/90-spotify-exclusive.conf.
   This drop-in modifies only the existing canonical renderer. Do not create
   or enable a second renderer. Do not overwrite existing files/drop-ins blindly.
5. Manually run systemctl --user daemon-reload and inspect effective units again
   with systemctl --user cat and systemctl --user show. Confirm the Spotify unit
   has Conflicts={RENDERER_SERVICE} and exactly one After={RENDERER_SERVICE}
   edge, not a reverse ordering cycle. Both services must have Restart=no,
   KillMode=control-group, SendSIGKILL=yes and TimeoutStopSec=45. Additional
   effective overrides or dependencies need manual review before any start.

## Every handover, in either direction (including first pairing/start)

1. Stop both services, and wait for the blocking command to finish:
   systemctl --user stop {RENDERER_SERVICE} {SPOTIFY_SERVICE}
2. Inspect BOTH effective services using systemctl --user show (ActiveState,
   SubState, Result, MainPID, ControlGroup) and systemctl --user status. Require
   completed successful stops, no pending jobs, no main process, and empty owned
   cgroups (including descendants). Check actual cgroup membership, not just a
   process name, PID file or a stale API reply. A stop timeout, failed stop,
   inaccessible cgroup or uncertain identity means leave both off and investigate.
3. Verify no independent launch path or foreign process owns the DAC, and verify
   physical device release on the target hardware with appropriate host tools.
   Empty owned cgroups and Conflicts/ordering are defense in depth; neither
   proves universal physical ownership or protects against another user,
   system manager, audio server, escaped process or external start request.
   Do not assume pause, inactive, HTTP success, API readiness or process absence
   proves release. Never terminate an unrelated owner to force handover.
4. Only after all checks pass, manually start exactly ONE selected service:
   systemctl --user start {SPOTIFY_SERVICE}
   OR, for the reverse handover after repeating steps 1–3:
   systemctl --user start {RENDERER_SERVICE}
   Start failure, interruption, API loss, device-busy errors or any uncertainty:
   stop both, investigate, remain off. Never automatically fall back to ORR.

## User-operated account setup and later observation

Auth mode is device_auth only. The generator accepts no credentials, tokens,
client keys, account identifiers, pairing codes or credential-import paths.
Actual pairing is a separate user-operated action after the gates and complete
stop/release procedure. A first manual start may initiate provider traffic and
print a private pairing code/link. Follow the pinned upstream instructions
personally; never put credentials in this config or share the private logs.
Zeroconf and MPRIS are off; audio cache and metadata prefetch are disabled.
Normal daemon shutdown uses systemd signals/cgroup termination, never REST stop.
A later explicit read-only observation can use:
open-room-renderer spotify diagnose --host {q(host)} --port {port}
That proves only a sidecar-reported snapshot; it cannot clear the pending gates.
Test real DAC handover/failure paths, session recovery, XU4 ABI and resource use
separately before calling this a productive installation. There is no claim of
shared DSP, automatic safe switching or maintenance-free unattended operation.

## Removing this proposal

Stop both and repeat the release checks first. Manually remove only the copied
Spotify unit and this exact drop-in after inspecting ownership, then reload the
user manager. Keep credentials private; any removal of real account state is a
separate user decision. Review renderer behavior before deliberately restoring
its previous restart/boot settings; this generator restores nothing automatically.

## Pinned evidence

- [CLI flags/config selection]({UPSTREAM}/cmd/daemon/cli_config.go): use
  --config_dir (pflag's long form); config.yaml takes priority over config.yml.
  Keep sidecar/config.yml as the only config and do not add overrides.
- [README]({UPSTREAM}/README.md): device_auth pairing and separate state.
- [Schema]({UPSTREAM}/config_schema.json): ALSA, API, cache, metadata and MPRIS.
- [Main]({UPSTREAM}/cmd/daemon/main.go): state.json and credentials.json files.
- [API listener]({UPSTREAM}/daemon/api_server.go): this version formats address
  and port directly, so the generator deliberately supports IPv4 loopback only.
"""


def _write_package(output: Path, artifacts: dict[str, str]) -> None:
    """Create a new private root through non-following dirfds; never overwrite.

    On I/O failure the partial, inert private directory is retained for manual
    inspection. A successful receipt is written last. No recursive cleanup.
    """
    with _directory(output.parent) as parent:
        os.mkdir(output.name, mode=0o700, dir_fd=parent)
        root = os.open(output.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        try:
            os.fchmod(root, 0o700)
            for directory in ("sidecar", RENDERER_SERVICE + ".d"):
                os.mkdir(directory, mode=0o700, dir_fd=root)
            for name, body in artifacts.items():
                parts = name.split("/")
                child = None
                parent_fd = root
                if len(parts) == 2:
                    child = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root)
                    parent_fd = child
                try:
                    fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent_fd)
                    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                        os.fchmod(handle.fileno(), 0o600)
                        handle.write(body)
                finally:
                    if child is not None:
                        os.close(child)
        finally:
            os.close(root)


def prepare_exclusive(*, renderer_prefix: str, renderer_config: str, renderer_unit: str,
                      go_librespot_binary: str, output_dir: str, port: int,
                      host: str = "127.0.0.1") -> dict:
    """Produce inert proposals. Validation never proves effective units or audio."""
    try:
        if sys.platform != "linux" or os.geteuid() == 0:
            _fail("Run this Linux-only offline generator as your normal unprivileged account.")
        if type(port) is not int or not 1024 <= port <= 65535:
            _fail("Use a fixed unprivileged Spotify API port from 1024 through 65535.")
        address = ipaddress.ip_address(host)
        if (address.version != 4 or not address.is_loopback or str(address) != host or "%" in host
                or getattr(address, "ipv4_mapped", None) is not None):
            _fail("Use only a canonical numeric IPv4 loopback API address; this generator supports IPv4 only.")
        prefix, config, unit, binary, output = map(_path, (
            renderer_prefix, renderer_config, renderer_unit, go_librespot_binary, output_dir))
        if _overlap(prefix, output) or _overlap(binary, output) or _overlap(prefix, binary):
            _fail("The sidecar output, renderer prefix and separately installed client must not overlap.")
        if any(any(char in str(path) for char in ("\"", "'")) for path in (prefix, binary)):
            _fail("Executable paths must not contain single or double quotes; systemd rejects those executable names.")
        if binary.name != "go-librespot":
            _fail("Select the separately installed go-librespot executable; wrappers are unsupported.")
        _read(binary, binary=True)
        profile = _renderer(prefix, config, unit, port)
        sidecar = _spotify_config(profile["audio_device"], host, port, output)
        artifacts = {
            "sidecar/config.yml": json.dumps(sidecar, indent=2, sort_keys=True) + "\n",
            SPOTIFY_SERVICE: _spotify_unit(binary, output),
            RENDERER_SERVICE + ".d/90-spotify-exclusive.conf": RENDERER_DROPIN,
            "MANUAL_HANDOVER.md": _guide(prefix, config, unit, binary, output, host, port),
        }
        receipt = {
            "format": 1, "evidence": "OFFLINE_ARTIFACT_ONLY", "gates": list(GATES),
            "receipt_scope": "artifact_completeness_only", "crash_durability_verified": False,
            "expected_upstream": {"version": UPSTREAM_VERSION, "commit": UPSTREAM_COMMIT,
                                  "binary_version_verified": False},
            "renderer_config_sha256": profile["config_sha256"], "renderer_unit_sha256": profile["unit_sha256"],
            "effective_units_inspected": False, "same_user_manager_required": True,
            "service_activation_performed": False, "client_execution_performed": False,
            "account_access_performed": False, "physical_output_verified": False,
            "automatic_switching": False, "audio_bypasses_renderer_eq_balance": True,
            "files": {name: hashlib.sha256(body.encode("utf-8")).hexdigest() for name, body in artifacts.items()},
        }
        artifacts["receipt.json"] = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
        _write_package(output, artifacts)
        return receipt
    except ExclusiveProfileError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, yaml.YAMLError, RecursionError,
            AttributeError, KeyError, IndexError, OverflowError):
        raise ExclusiveProfileError("Offline preparation failed. Check supported private inputs and a new output directory; no existing files are overwritten. A partial inert output may remain.") from None


class _Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        seen = getattr(namespace, "_exclusive_seen", set())
        if self.dest in seen:
            parser.error("Repeated exclusive-profile options are unsupported.")
        setattr(namespace, "_exclusive_seen", seen | {self.dest})
        setattr(namespace, self.dest, values)


def add_prepare_parser(commands) -> None:
    parser = commands.add_parser("prepare-exclusive", help="write an offline manual exclusive-mode proposal",
        description="Credential-free artifacts only. No install, execution, account, service, socket or device operations.",
        allow_abbrev=False)
    for name, help_text in (
        ("renderer-prefix", "existing completed unprivileged native installation"),
        ("renderer-config", "explicit root or migrated-profile config.yaml"),
        ("renderer-unit", "explicit canonical native unit proposal inside that prefix"),
        ("go-librespot-binary", "explicit separately installed go-librespot Linux ELF path (never run)"),
        ("output-dir", "new private directory outside the renderer prefix; parent must exist"),
    ):
        parser.add_argument("--" + name, required=True, action=_Once, help=help_text)
    parser.add_argument("--port", type=int, required=True, action=_Once, help="fixed unprivileged API port")
    parser.add_argument("--host", default="127.0.0.1", action=_Once, help="canonical numeric IPv4 loopback address")


def run_prepare_exclusive(args: argparse.Namespace) -> int:
    try:
        prepare_exclusive(**{key: getattr(args, key) for key in (
            "renderer_prefix", "renderer_config", "renderer_unit", "go_librespot_binary", "output_dir", "port", "host")})
    except ExclusiveProfileError as exc:
        print("Spotify preparation failed: " + str(exc), file=sys.stderr)
        return 2
    print("OFFLINE_ARTIFACT_ONLY: private proposals written. TERMS_PENDING / ACCOUNT_PENDING / HARDWARE_PENDING / ARBITRATION_PENDING. Review MANUAL_HANDOVER.md before any manual action.")
    return 0
