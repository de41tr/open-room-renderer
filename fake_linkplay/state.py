"""In-memory renderer state and status payload helpers.

The HTTP layer mutates this dataclass, then serializes it into the redundant
status shapes expected by local renderer clients.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable

from .capabilities import capability_fields, configured_presets
from .config import Config
from .queue import PlaybackQueue


class PlaybackPhase(StrEnum):
    COMMAND_ACCEPTED = "COMMAND_ACCEPTED"
    LOADING = "LOADING"
    BUFFERING = "BUFFERING"
    PLAYING = "PLAYING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


@dataclass
class RendererState:
    """Mutable playback state serialized into app-facing status payloads.

    Attributes mirror the loose, string-heavy shapes used by Linkplay-style
    clients. The HTTP layer mutates this state after commands and mpv snapshots.
    """

    cfg: Config
    volume: int
    mute: bool = False
    gain_confirmed: bool = True
    phase: PlaybackPhase = PlaybackPhase.STOPPED
    generation: int = 0
    intent: str = "stop"
    backend_url: str = ""
    backend_original_url: str = ""
    last_error: str = ""
    mode: str = "0"
    current_url: str = ""
    resolved_url: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    album_art_uri: str = ""
    track_metadata: str = ""
    track_source: str = ""
    play_medium: str = ""
    curpos: int = 0
    totlen: int = 0
    queue: PlaybackQueue = field(default_factory=PlaybackQueue)
    preset_view: Callable[[], list[dict[str, object]]] | None = field(default=None, repr=False, compare=False)

    def effective_presets(self) -> list[dict[str, object]]:
        """Use the service-owned library view, with a config-only standalone fallback."""
        return self.preset_view() if self.preset_view is not None else configured_presets(self.cfg)

    @property
    def loop(self) -> str:
        """Numeric wire value derived from the authoritative queue policy."""
        return str(self.queue.loop)

    @property
    def plicount(self) -> int:
        return self.queue.count

    @property
    def plicurr(self) -> int:
        return self.queue.index

    @property
    def status(self) -> str:
        """Legacy wire state derived from the authoritative playback phase."""
        return {
            PlaybackPhase.PLAYING: "play", PlaybackPhase.PAUSED: "pause",
            PlaybackPhase.STOPPED: "stop", PlaybackPhase.ERROR: "stop",
        }.get(self.phase, "transition")

    @status.setter
    def status(self, value: str) -> None:
        # Compatibility for existing internal helpers; new code sets a phase.
        self.phase = {"play": PlaybackPhase.PLAYING, "pause": PlaybackPhase.PAUSED,
                      "stop": PlaybackPhase.STOPPED, "transition": PlaybackPhase.BUFFERING}[value]

    @property
    def transport_state(self) -> str:
        return {"play": "PLAYING", "pause": "PAUSED_PLAYBACK", "stop": "STOPPED",
                "transition": "TRANSITIONING"}[self.status]

    @property
    def transport_status(self) -> str:
        return "ERROR_OCCURRED" if self.phase == PlaybackPhase.ERROR else "OK"

    def accept_command(self, intent: str) -> int:
        """Invalidate pending observations before issuing a newer owner command."""
        self.generation += 1
        self.intent = intent
        self.last_error = ""
        return self.generation

    def mark_error(self, message: str = "Backend operation failed") -> None:
        self.phase = PlaybackPhase.ERROR
        self.last_error = message

    @classmethod
    def from_config(cls, cfg: Config) -> "RendererState":
        """Create initial state with the configured volume clamped to 0..100."""

        return cls(cfg=cfg, volume=max(0, min(100, int(cfg.default_volume))), mute=cfg.default_mute)

    def mark_playing(self, url: str) -> None:
        """Mark the renderer as playing ``url`` and fill basic metadata."""

        self.current_url = url
        self.phase = PlaybackPhase.PLAYING
        self.intent = "play"
        self.mode = "10"
        if not self.title:
            self.title = url.rsplit("/", 1)[-1] or "Network stream"
        if not self.play_medium:
            self.play_medium = "STATION-NETWORK"

    def mark_pause(self) -> None:
        """Mark the transport as paused without changing position."""

        self.phase = PlaybackPhase.PAUSED
        self.intent = "pause"

    def mark_transitioning(self) -> None:
        """Mark the transport as buffering/loading for event subscribers."""

        self.phase = PlaybackPhase.BUFFERING

    def mark_stop(self) -> None:
        """Mark playback stopped and reset the visible position."""

        self.phase = PlaybackPhase.STOPPED
        self.intent = "stop"
        self.curpos = 0

    def as_status_ex(self, ip: str) -> dict[str, Any]:
        """Return the redundant status document expected by HTTP clients.

        Args:
            ip: LAN IP address to expose in app-facing status fields.

        Returns:
            Dictionary containing Linkplay-style status and capability keys.
        """

        common = {
            "uuid": self.cfg.upnp_uuid,
            "DeviceName": self.cfg.linkplay_instance_name if self.cfg.wiim_home_profile else self.cfg.friendly_name,
            "GroupName": self.cfg.linkplay_instance_name if self.cfg.wiim_home_profile else self.cfg.friendly_name,
            "upnp_uuid": f"uuid:{self.cfg.upnp_uuid}",
            "ssid": self.cfg.linkplay_instance_name if self.cfg.wiim_home_profile else "fake-linkplay",
            "firmware": "0.1.0",
            "build": "0.1.0",
            "project": "Open_Room_Renderer",
            "project_build_name": self.cfg.model_number,
            "new_tunein_preset_and_alarm": "0",
            "Release": "0.1.0",
            "FW_Release_version": "0.1.0",
            "PCB_version": "1",
            "hardware": self.cfg.model_name,
            "model": self.cfg.model_name,
            "security": self.cfg.security,
            "securemode": "0",
            "bootid": self.cfg.bootid,
            "internet": "0",
            "netstat": "2",
            "eth": "0",
            "wifi_channel": "0",
            "RSSI": "0",
            "apcli0": ip,
            "ip": ip,
            "MAC": self.cfg.wiim_mac,
            "mac": self.cfg.wiim_mac,
            "language": "en_us",
            "region": "US",
            "volume": str(self.volume),
            "vol": str(self.volume),
            "gain_confirmed": self.gain_confirmed,
            "mute": "1" if self.mute else "0",
            "status": self.status,
            "playback_phase": self.phase.value,
            "playback_generation": str(self.generation),
            "transport_error": self.last_error,
            "play_state": {"play": "playing", "pause": "pause", "stop": "stop", "transition": "transitioning"}.get(self.status, self.status),
            "mode": self.mode,
            "loop": self.loop,
            "curpos": str(int(self.curpos)),
            "totlen": str(int(self.totlen)),
            "plicount": str(self.plicount),
            "plicurr": str(self.plicurr),
            "source": "URL" if self.current_url else "none",
            "dlna_track_source": self.track_source,
            "dlna_play_medium": self.play_medium,
            "stream_url": self.current_url,
            "resolved_stream_url": self.resolved_url,
            "uri": self.current_url,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "albumArtURI": self.album_art_uri,
            "picurl": self.album_art_uri,
            "capability": self.cfg.legacy_capability_value,
            "capabilities": self.cfg.legacy_capability_value,
            "wmrm_capability": 0,
            "upnp_version": "1000",
            "VersionUpdate": "0",
            "NewVer": "0.1.0",
            "service_version": "1.4",
            "preset_key": str(len(self.effective_presets())),
            "prompt_status": "0",
            "indicator_light": "0",
            "LightApiVersion": "",
            "EQ_support": "1",
            "EQVersion": "4.3",
            "BleRemoteControl": "",
            "BleRemoteConnected": "0",
            "BleRemoteBatterylevel": "0",
            "BleRemoteException": "0",
            "update_check_count": "0",
            "ota_interface_ver": "",
            "battery": "0",
            "BatteryFlag": "0",
            "BatteryMains": "0",
            "BatteryPercent": "0",
            "battery_flag": "0",
            "battery_mains": "0",
            "battery_percent": "0",
            "is_battery_powered": "0",
            "portable": "0",
            "security_version": "0",
            "security_capabilities": "0",
            "streams": "0",
            "plm_support": self.cfg.legacy_plm_support_value,
            "cap1": "0",
        }
        common.update(capability_fields(self.cfg))
        return common

    def as_player_status(self) -> dict[str, Any]:
        """Return the compact player status payload."""

        return {
            "type": "0",
            "ch": "0",
            "mode": self.mode,
            "loop": self.loop,
            "status": self.status,
            "playback_phase": self.phase.value,
            "playback_generation": str(self.generation),
            "transport_error": self.last_error,
            "curpos": str(int(self.curpos)),
            "totlen": str(int(self.totlen)),
            "plicount": str(self.plicount),
            "plicurr": str(self.plicurr),
            "vol": str(self.volume),
            "gain_confirmed": self.gain_confirmed,
            "mute": "1" if self.mute else "0",
            "Title": self.title,
            "Artist": self.artist,
            "Album": self.album,
            "AlbumArtURI": self.album_art_uri,
            "albumArtURI": self.album_art_uri,
            "picurl": self.album_art_uri,
            "TrackSource": self.track_source,
            "PlayMedium": self.play_medium,
            "dlna_track_source": self.track_source,
            "dlna_play_medium": self.play_medium,
            "source": self.track_source,
            "play_state": {"play": "playing", "pause": "pause", "stop": "stop", "transition": "transitioning"}.get(self.status, self.status),
            "uri": self.current_url,
            "stream_url": self.current_url,
            "resolved_stream_url": self.resolved_url,
        }

    def as_meta_info(self) -> dict[str, Any]:
        """Return now-playing metadata in the shape used by getMetaInfo."""

        return {
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "albumArtURI": self.album_art_uri,
            "sampleRate": "0",
            "bitDepth": "0",
            "stream_url": self.current_url,
            "resolved_stream_url": self.resolved_url,
            "uri": self.current_url,
            "source": self.track_source,
            "picurl": self.album_art_uri,
            "cover": self.album_art_uri,
            "art": self.album_art_uri,
            "TrackSource": self.track_source,
            "PlayMedium": self.play_medium,
            "dlna_track_source": self.track_source,
            "dlna_play_medium": self.play_medium,
        }
