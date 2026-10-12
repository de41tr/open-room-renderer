"""Configuration loading and small persisted runtime settings.

The public examples use neutral values. Some compatibility profiles still use
specific protocol-facing strings because client behavior depends on them; keep
those changes behind explicit config flags.
"""

from __future__ import annotations

import hashlib
import uuid as uuidlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .utils import detect_lan_ip, ensure_dir
from .persistence import load_gain


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
    upnp_port: int = 8080
    mirror_port_80: bool = False
    bind_address: str = "0.0.0.0"
    advertise_ip: str | None = None
    mpv_socket: str = "/tmp/open-room-renderer-mpv.sock"
    auto_spawn_mpv: bool = True
    default_volume: int = 50
    default_mute: bool = False
    log_level: str = "INFO"
    state_dir: str = ".state"
    enable_mdns: bool = False
    enable_ssdp: bool = True
    # The WiiM Home profile intentionally changes identity, ports, and service
    # metadata to match behavior observed in app captures. Keep the normal
    # pywiim/dev path separate so the portable acceptance gate stays simple.
    wiim_home_profile: bool = False
    mirror_http_on_upnp_port: bool = False
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
    channel_balance: str = "0.0"
    podcast_presets: list[PodcastPreset] = field(default_factory=list)
    experimental_sources: list[ExperimentalSource] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Normalize configured IDs without replacing their identity."""

        if type(self.default_mute) is not bool:
            raise ValueError("default_mute must be boolean")
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
    presets = [PodcastPreset(**item) for item in raw.pop("podcast_presets", [])]
    experimental_sources = [ExperimentalSource(**item) for item in raw.pop("experimental_sources", [])]
    cfg = Config(**raw, podcast_presets=presets, experimental_sources=experimental_sources)
    cfg.uuid = cfg.uuid or load_or_create_uuid(cfg.state_dir, wiim_home_profile=cfg.wiim_home_profile)
    load_persistent_runtime_state(cfg)
    return cfg


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
        Creates the state directory and writes a UUID file on first run.
    """

    directory = ensure_dir(state_dir)
    uuid_path = directory / "uuid"
    if uuid_path.exists():
        value = normalize_uuid(uuid_path.read_text())
        if not value:
            raise ValueError(f"Persisted UUID is empty: {uuid_path}")
        return value
    entropy = uuidlib.uuid4()
    # FF is a compatibility discriminator, not an OEM identifier. The remaining
    # UUID entropy belongs to this installation; do not clone a hardware ID.
    value = f"FF00{entropy.hex.upper()}" if wiim_home_profile else str(entropy)
    try:
        with uuid_path.open("x") as stream:
            stream.write(value + "\n")
    except FileExistsError:
        return load_or_create_uuid(state_dir, wiim_home_profile=wiim_home_profile)
    return value


def load_persistent_runtime_state(cfg: Config) -> None:
    """Apply safe user-visible settings persisted by app controls.

    Args:
        cfg: Config object to mutate with device name and balance overrides.

    Side Effects:
        Reads small text files from ``cfg.state_dir``.
    """

    directory = ensure_dir(cfg.state_dir)
    gain = load_gain(directory)
    if gain is not None:
        cfg.default_volume, cfg.default_mute = gain
    device_name_path = directory / "device_name"
    if device_name_path.exists():
        device_name = sanitize_persistent_text(device_name_path.read_text())
        if device_name:
            cfg.friendly_name = device_name
            cfg.mdns_instance_name = device_name
    channel_balance_path = directory / "channel_balance"
    if channel_balance_path.exists():
        cfg.channel_balance = sanitize_persistent_text(channel_balance_path.read_text()) or "0.0"


def sanitize_persistent_text(value: str) -> str:
    """Normalize short persisted strings before using them in config fields."""

    return value.strip().replace("\x00", "")[:128]
