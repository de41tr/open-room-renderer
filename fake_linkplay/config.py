"""Configuration loading and small persisted runtime settings.

New installations use WiiM-compatible discovery. Existing identities retain
their effective neutral defaults unless the operator explicitly changes profile.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import tempfile
import uuid as uuidlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from .audio_device import validate_audio_device
from .utils import detect_lan_ip, ensure_dir
from .persistence import load_active_dsp, load_gain
from .startup_state import _read_state


LEGACY_DISCOVERY_DEFAULTS = {
    "wiim_home_profile": False,
    "upnp_port": 8080,
    "enable_mdns": False,
    "mirror_http_on_upnp_port": False,
}
WIIM_IDENTITY_FILE = "uuid.wiim"


@dataclass(slots=True)
class PodcastPreset:
    """Configured direct audio URL shown through the local preset endpoint.

    Attributes:
        name: Human-readable preset label.
        url: DRM-free audio or playlist URL passed to the playback backend.
    """

    name: str
    url: str


@dataclass(slots=True)
class ExperimentalSource:
    """Optional source-advertising experiment loaded from configuration.

    These values are deliberately config-driven because app-facing source
    behavior is fragile and should be tested without editing code.
    """

    id: str
    name: str
    source: str = ""
    url: str = ""
    icon: str = ""
    type: str = "radio"
    enabled: bool = True


@dataclass(slots=True)
class Config:
    """Runtime configuration for HTTP, discovery, identity, and playback.

    The object is intentionally plain data so tests and scripts can construct
    variants without starting network services. Some properties derive
    protocol-facing identity strings from the persisted UUID.
    """

    friendly_name: str = "Open Room Renderer"
    manufacturer: str = "Independent"
    manufacturer_url: str = ""
    model_name: str = "Linux Room Renderer"
    model_number: str = "alpha-0.1"
    model_url: str = ""
    uuid: str | None = None
    http_api_port: int = 8080
    upnp_port: int = 59152
    mirror_port_80: bool = False
    bind_address: str = "0.0.0.0"
    advertise_ip: str | None = None
    mpv_socket: str = "/tmp/open-room-renderer-mpv.sock"
    auto_spawn_mpv: bool = True
    # Startup-only; None preserves mpv's existing configuration/defaults.
    mpv_audio_device: str | None = field(default=None, kw_only=True)
    # Explicit activation only; setup never writes/enables this setting.
    spotify_eq_profile: str | None = field(default=None, kw_only=True)
    default_volume: int = 50
    default_mute: bool = False
    log_level: str = "INFO"
    state_dir: str = ".state"
    enable_mdns: bool = True
    enable_ssdp: bool = True
    # Local IPv4 address for multicast membership/output; None keeps OS routing.
    # HTTP binding and advertised URLs do not select a multicast interface.
    discovery_interface: str | None = None
    # Publish app-compatible SSDP/mDNS by default. Explicit false retains
    # standard UUID/discovery metadata for clients needing the neutral profile.
    wiim_home_profile: bool = True
    mirror_http_on_upnp_port: bool = True
    mdns_instance_name: str | None = None
    discovery_uuid: str | None = None
    bootid: str = "000000000000000000000000000000000000"
    security: str = "http"
    log_wiim_http_details: bool = False
    source_experiment_variant: str = "none"
    tunein_streams_value: str = "200"
    tunein_stream_all_value: str | None = None
    legacy_capability_value: str = "0x0"
    legacy_plm_support_value: str = "0"
    tunein_minimal_capability: bool = False
    # Plex/TuneIn service flags are experimental WiiM Home compatibility knobs.
    # They advertise app-known service IDs; the actual third-party cloud flows
    # still happen in WiiM Home and are not fully controlled by this renderer.
    enable_plex_service: bool = False
    enable_plex_legacy_gate: bool = False
    plex_capability_mode: str = "normal"
    plex_user_info_mode: str = "not_login"
    enable_stream_service_sentinels: bool = False
    proxy_on_demand_audio: bool = False
    proxy_allowed_lan_hosts: list[str] = field(default_factory=list)
    range_seek_reload: bool = False
    # Experimental one-successor native queue staging; never changes mpv audio defaults.
    native_queue_staging: bool = False
    channel_balance: str = "0.0"
    podcast_presets: list[PodcastPreset] = field(default_factory=list)
    experimental_sources: list[ExperimentalSource] = field(default_factory=list)
    # Loader provenance only, never an accepted YAML setting. Omitted settings
    # can retain legacy defaults once the configured or saved identity is known.
    _implicit_discovery_settings: tuple[str, ...] = field(
        default=tuple(LEGACY_DISCOVERY_DEFAULTS), init=False, repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        """Normalize configured IDs without replacing their identity."""

        self.mpv_audio_device = validate_audio_device(self.mpv_audio_device, auto_spawn=self.auto_spawn_mpv)
        if self.spotify_eq_profile is not None:
            value = self.spotify_eq_profile
            if (type(value) is not str or not value.startswith("/") or len(value) > 4096
                    or str(Path(value)) != value or value == "/" or value.startswith("//")
                    or any(part in {".", ".."} for part in value.split("/"))
                    or any(not c.isprintable() or c in "$\\" for c in value)):
                raise ValueError("spotify_eq_profile must be an absolute normalized path or null")
        if type(self.native_queue_staging) is not bool:
            raise ValueError("native_queue_staging must be boolean")
        if type(self.default_mute) is not bool:
            raise ValueError("default_mute must be boolean")
        if self.discovery_interface is not None:
            try:
                if not isinstance(self.discovery_interface, str):
                    raise ValueError
                address = ipaddress.IPv4Address(self.discovery_interface)
                if address.is_unspecified or address.is_multicast or int(address) == 0xFFFFFFFF:
                    raise ValueError
            except ValueError as exc:
                raise ValueError("discovery_interface must be a literal local unicast IPv4 address") from exc
            self.discovery_interface = str(address)
        self.uuid = normalize_uuid(self.uuid) or None
        self.discovery_uuid = normalize_uuid(self.discovery_uuid).upper() or None

    @property
    def resolved_advertise_ip(self) -> str:
        """Return the IP address advertised to LAN clients."""

        return self.advertise_ip or detect_lan_ip()

    @property
    def api_base_url(self) -> str:
        """Return the base URL for the Linkplay-style HTTP API."""

        return f"http://{self.resolved_advertise_ip}:{self.http_api_port}"

    @property
    def description_url(self) -> str:
        """Return the UPnP device description URL advertised by discovery."""

        if self.wiim_home_profile:
            return f"http://{self.resolved_advertise_ip}:{self.http_api_port}/description.xml"
        return f"http://{self.resolved_advertise_ip}:{self.upnp_port}/description.xml"

    @property
    def upnp_alias_description_url(self) -> str:
        """Return the secondary description URL on the UPnP port."""

        return f"http://{self.resolved_advertise_ip}:{self.upnp_port}/description.xml"

    @property
    def mdns_description_url(self) -> str:
        """Return the description URL embedded in mDNS TXT metadata."""

        port = self.http_api_port if self.wiim_home_profile else self.upnp_port
        return f"http://{self.resolved_advertise_ip}:{port}/description.xml"

    @property
    def wiim_uuid(self) -> str:
        """Return the compact compatibility UUID used by app-facing profiles."""

        if self.discovery_uuid:
            return normalize_uuid(self.discovery_uuid).upper()
        # Preserve the legacy derived identity for existing/configured UUIDs.
        # Fresh compatibility installs already persist a full FF-prefixed ID.
        compact = normalize_uuid(self.uuid).replace("-", "").upper()
        return (compact + "000000000000000000000000000000000000")[:36]

    @property
    def wiim_txt_uuid(self) -> str:
        """Return the UUID formatted for mDNS TXT records."""

        return f"uuid:{self.wiim_uuid}"

    @property
    def upnp_uuid(self) -> str:
        """Return the UUID string used in UPnP UDN and SSDP USN fields."""

        return self.wiim_uuid if self.wiim_home_profile else normalize_uuid(self.uuid)

    @property
    def wiim_mac(self) -> str:
        """Return a deterministic locally administered MAC-like identifier."""

        # Use the whole identity: legacy padded tails are shared by many IDs.
        compact = self.identity_suffix[:10].upper()
        return "02:" + ":".join(compact[index : index + 2] for index in range(0, 10, 2))

    @property
    def identity_suffix(self) -> str:
        """Return a stable suffix derived from all of this renderer's ID."""

        return hashlib.sha256(self.upnp_uuid.upper().encode("utf-8")).hexdigest()[:12]

    @property
    def mdns_hostname(self) -> str:
        """Return an installation-specific hostname unaffected by renames."""

        return f"open-room-renderer-{self.identity_suffix}.local."

    @property
    def linkplay_instance_name(self) -> str:
        """Return the app-facing discovery instance name."""

        if self.mdns_instance_name:
            return self.mdns_instance_name
        suffix = self.identity_suffix[:6].upper()
        if self.wiim_home_profile:
            return f"LinkPlayA31_{suffix}"
        return f"{self.friendly_name}-{suffix}"


class StartupConfigError(ValueError):
    """An operator-safe startup diagnostic, containing no supplied values."""


CONFIG_YAML_ERROR = "Configuration YAML could not be parsed. Check syntax and indentation."
CONFIG_STRUCTURE_ERROR = (
    "Configuration structure is invalid. Compare supported settings and preset/source "
    "entries with the example YAML."
)
CONFIG_DUPLICATE_ERROR = (
    "Configuration contains duplicate top-level settings. "
    "Keep only one explicit value for each setting."
)


class _StartupSafeLoader(yaml.SafeLoader):
    """Reject repeated explicit root settings before constructing YAML values."""

    def construct_document(self, node: yaml.Node) -> Any:
        if isinstance(node, yaml.MappingNode):
            seen: set[str] = set()
            # Inspect the original root pairs before SafeLoader flattens merges.
            # A merged default plus one explicit override is not a duplicate;
            # nested mappings retain their existing SafeLoader behavior.
            for key, _value in node.value:
                # SafeLoader also normalizes YAML's value-tag mapping keys to
                # strings while flattening; include that spelling here too.
                if isinstance(key, yaml.ScalarNode) and key.tag in {
                    "tag:yaml.org,2002:str", "tag:yaml.org,2002:value",
                }:
                    if key.value in seen:
                        raise StartupConfigError(CONFIG_DUPLICATE_ERROR)
                    seen.add(key.value)
        return super().construct_document(node)


def parse_startup_yaml(source: str) -> Any:
    """Reject duplicate root settings and keep parser diagnostics private."""

    try:
        return yaml.load(source, Loader=_StartupSafeLoader)
    except StartupConfigError:
        raise
    except (yaml.YAMLError, ValueError, TypeError, AttributeError, KeyError, IndexError,
            OverflowError, RecursionError):
        # Explicit !!int/!!bool/!!timestamp tags can raise ordinary Python
        # exceptions containing raw values rather than yaml.YAMLError.
        raise StartupConfigError(CONFIG_YAML_ERROR) from None


def read_startup_config(
    path: str | Path, *, missing_ok: bool = False, captured_text: str | None = None,
) -> Config:
    """Read startup input without creating identity/state or probing resources.

    Offline tools may supply securely bounded captured_text to validate that exact
    snapshot without reopening a pathname. No state or resources are accessed.

    Only the implicit CLI default may use the historical missing-file fallback.
    Error text is owned here: YAML exceptions can include raw source lines,
    unknown setting names, file paths, and credential-bearing URLs.

    This checks document/construction shape and the two synchronous logging/gain
    conversions used at startup, not a comprehensive field type/range schema.
    """

    try:
        source = (Path(path).read_text(encoding="utf-8")
                  if captured_text is None else captured_text)
    except FileNotFoundError:
        if not missing_ok:
            raise StartupConfigError(
                "Configuration file not found. Check --config or ORR_CONFIG."
            ) from None
        # A missing target may still have a selected leaf (for example, a
        # dangling symlink). Fall back only when lstat also reports it missing.
        try:
            Path(path).lstat()
        except FileNotFoundError:
            source = ""
        except OSError:
            raise StartupConfigError(
                "Configuration file could not be read. Check the path and read permissions."
            ) from None
        else:
            raise StartupConfigError(
                "Configuration file could not be read. Check the path and read permissions."
            ) from None
    except IsADirectoryError:
        raise StartupConfigError(
            "Configuration path is a directory. Select a YAML file."
        ) from None
    except OSError:
        raise StartupConfigError(
            "Configuration file could not be read. Check the path and read permissions."
        ) from None
    except UnicodeError:
        raise StartupConfigError(
            "Configuration text could not be decoded. Save the YAML file as UTF-8."
        ) from None
    raw = parse_startup_yaml(source)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise StartupConfigError(CONFIG_STRUCTURE_ERROR)
    try:
        cfg = _config_from_mapping(raw)
        # Match setup_logging's unknown-name fallback and RendererState's int
        # coercion, without configuring logging or reading/writing saved state.
        level = getattr(logging, cfg.log_level.upper(), logging.INFO)
        if not isinstance(level, int):
            raise ValueError
        int(cfg.default_volume)
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise StartupConfigError(CONFIG_STRUCTURE_ERROR) from None
    return cfg


def load_startup_config(
    path: str | Path, *, missing_ok: bool = False,
    before_state: Callable[[Config], None] | None = None,
) -> Config:
    """Validate startup input before applying the existing persisted state path."""

    cfg = read_startup_config(path, missing_ok=missing_ok)
    if before_state is not None:
        before_state(cfg)
    return _load_config_state(cfg, _startup=True)


def _config_from_mapping(raw: dict[str, Any]) -> Config:
    implicit = tuple(name for name in LEGACY_DISCOVERY_DEFAULTS if name not in raw)
    presets = [PodcastPreset(**item) for item in raw.pop("podcast_presets", [])]
    experimental_sources = [ExperimentalSource(**item) for item in raw.pop("experimental_sources", [])]
    cfg = Config(**raw, podcast_presets=presets, experimental_sources=experimental_sources)
    cfg._implicit_discovery_settings = implicit
    preserve_discovery_identity(cfg)
    return cfg


def preserve_discovery_identity(cfg: Config, *, wiim_identity: str | None = None) -> None:
    """Keep all omitted legacy discovery defaults for an existing identity.

    An explicit profile choice remains authoritative. Only saved identities with
    matching first-install provenance inherit the new WiiM defaults; unchanged
    UUID spelling does not imply an unchanged mDNS instance or profile. Offline
    callers supply captured provenance without probing state. Existing config
    and UUID files are never rewritten to record this effective choice.
    """

    if "wiim_home_profile" not in cfg._implicit_discovery_settings or not cfg.uuid:
        return
    if wiim_identity and normalize_uuid(wiim_identity) == normalize_uuid(cfg.uuid):
        return
    for name in cfg._implicit_discovery_settings:
        setattr(cfg, name, LEGACY_DISCOVERY_DEFAULTS[name])


def _load_config_state(cfg: Config, *, _startup: bool = False) -> Config:
    if not cfg.uuid:
        cfg.uuid = _read_state(
            lambda: load_or_create_uuid(cfg.state_dir, wiim_home_profile=cfg.wiim_home_profile),
            "identity", startup=_startup,
        )
        if "wiim_home_profile" in cfg._implicit_discovery_settings:
            wiim_identity = _read_state(
                lambda: _read_optional_persistent_text(Path(cfg.state_dir) / WIIM_IDENTITY_FILE),
                "identity", startup=_startup,
            )
            preserve_discovery_identity(cfg, wiim_identity=wiim_identity)
    load_persistent_runtime_state(cfg, _startup=_startup)
    return cfg


def load_config(path: str | Path) -> Config:
    """Load YAML configuration and persisted local runtime overrides.

    Args:
        path: YAML config path. Missing files are treated as an empty config.

    Returns:
        A populated ``Config`` with generated UUID and persisted settings.

    Side Effects:
        Creates ``state_dir`` and a UUID file when no UUID exists yet.
    """

    raw: dict[str, Any] = {}
    p = Path(path)
    if p.exists():
        raw = yaml.safe_load(p.read_text()) or {}
    return _load_config_state(_config_from_mapping(raw))


def normalize_uuid(value: str | None) -> str:
    """Strip optional UUID prefix/whitespace and normalize identity casing.

    Standard hyphenated UUIDs retain their conventional lower-case spelling;
    compact compatibility IDs use upper case. No entropy is added or removed.
    """

    value = "".join((value or "").split())
    if value[:5].lower() == "uuid:":
        value = value[5:]
    return value.lower() if "-" in value else value.upper()


def load_or_create_uuid(state_dir: str | Path, *, wiim_home_profile: bool = False) -> str:
    """Read the stable renderer UUID or create one under ``state_dir``.

    Args:
        state_dir: Directory used for local runtime state.
        wiim_home_profile: Generate a compact FF-prefixed ID on first install.
            Existing persisted or explicitly configured identities never migrate.

    Returns:
        UUID string persisted in ``state_dir/uuid``.

    Side Effects:
        Creates the state directory and atomically publishes a fully written,
        fsynced UUID on first run. Requires hard links and directory fsync.
        Publication is never rolled back, even if directory sync fails.
        New WiiM identities first publish uuid.wiim as explicit provenance,
        then publish the same complete value to uuid. An interrupted publication
        resumes from that record; no existing UUID is replaced.
    """

    directory = ensure_dir(state_dir)
    uuid_path = directory / "uuid"
    try:
        return _read_uuid(uuid_path)
    except FileNotFoundError:
        pass
    wiim_path = directory / WIIM_IDENTITY_FILE
    try:
        value = _read_uuid(wiim_path)
    except FileNotFoundError:
        value = None
    # Recover a fully published provenance record before proposing another ID.
    published = value is not None
    temporary = None
    identity_temporary = None
    try:
        try:
            if value is None:
                entropy = uuidlib.uuid4()
                # FF is a compatibility discriminator, not an OEM identifier.
                value = f"FF00{entropy.hex.upper()}" if wiim_home_profile else str(entropy)
                temporary = _stage_uuid(directory, value)
                if wiim_home_profile:
                    try:
                        os.link(temporary, wiim_path)
                    except FileExistsError:
                        value = _read_uuid(wiim_path)
                    published = True
            if published:
                # Make provenance durable before uuid can become visible: a
                # crash must not leave the new UUID with an implicit old profile.
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                # Separate inodes keep provenance immutable if an operator edits
                # uuid in place, and satisfy offline migration's no-hardlink rule.
                identity_temporary = _stage_uuid(directory, value)
            try:
                # Atomic no-clobber publication. Replacing a competing creator's
                # UUID would silently change the installation's identity.
                os.link(identity_temporary or temporary, uuid_path)
            except FileExistsError:
                value = _read_uuid(uuid_path)
            published = True
        finally:
            # Remove only this creator's private name, never the published ID.
            # Sync afterward so successful cleanup is included in the sync.
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
            if identity_temporary is not None:
                Path(identity_temporary).unlink(missing_ok=True)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            # A losing creator also confirms the directory before returning.
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        if published:
            raise OSError("UUID published; directory durability unconfirmed") from exc
        raise
    return value


def _stage_uuid(directory: Path, value: str) -> str:
    """Write complete private bytes before either identity name is published."""

    descriptor, temporary = tempfile.mkstemp(prefix=".uuid.", dir=directory)
    try:
        try:
            # Retain ownership even if constructing the text stream fails.
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
                stream.write(value + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return temporary


def _read_uuid(path: Path) -> str:
    value = normalize_uuid(path.read_text())
    if not value:
        raise ValueError(f"Persisted UUID is empty: {path}")
    return value


def load_persistent_runtime_state(cfg: Config, *, _startup: bool = False) -> None:
    """Apply safe user-visible settings persisted by app controls.

    Args:
        cfg: Config object to mutate with device name and balance overrides.

    Side Effects:
        Reads small text files from ``cfg.state_dir``.
    """

    directory = _read_state(lambda: ensure_dir(cfg.state_dir), "directory", startup=_startup)
    gain = _read_state(lambda: load_gain(directory), "gain", startup=_startup)
    if gain is not None:
        cfg.default_volume, cfg.default_mute = gain
    device_name_path = directory / "device_name"
    device_name = _read_state(
        lambda: _read_optional_persistent_text(device_name_path), "device_name", startup=_startup,
    )
    if device_name is not None:
        device_name = sanitize_persistent_text(device_name)
        if device_name:
            cfg.friendly_name = device_name
            cfg.mdns_instance_name = device_name
    channel_balance_path = directory / "channel_balance"
    active_dsp = _read_state(lambda: load_active_dsp(directory), "active_dsp", startup=_startup)
    if active_dsp is not None:
        cfg.channel_balance = str(active_dsp["balance"])
    else:
        channel_balance = _read_state(
            lambda: _read_optional_persistent_text(channel_balance_path),
            "channel_balance", startup=_startup,
        )
        if channel_balance is not None:
            cfg.channel_balance = sanitize_persistent_text(channel_balance) or "0.0"


def _read_optional_persistent_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except FileNotFoundError:
        # exists() follows links and would treat a saved dangling link as absent.
        # An uncertain inspection must also fail rather than select defaults.
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        raise


def sanitize_persistent_text(value: str) -> str:
    """Normalize short persisted strings before using them in config fields."""

    return value.strip().replace("\x00", "")[:128]
