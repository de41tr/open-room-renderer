"""HTTP, UPnP-ish, queue, settings, and EQ control surface.

This is the largest module because the prototype grew from observed client
interactions. New contributors should prefer adding small tests around a narrow
behavior before refactoring. Raw request logging is intentionally opt-in and
redacted before it reaches normal logs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import asdict
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from html import escape
from html import unescape as html_unescape
from pathlib import Path
from urllib.parse import quote
from urllib.parse import unquote
from uuid import uuid4

from aiohttp import ClientError
from aiohttp import ClientSession
from aiohttp import ClientTimeout
from aiohttp import web

from .config import Config
from .mpv_backend import MpvBackend
from .state import RendererState
from .upnp_description import add_plex_stream_service, add_stream_service_sentinels, avtransport_response_fields, description_xml, get_control_device_info_response_fields, new_tunein_stream_service, playqueue_response_fields, scpd_xml, soap_response, stream_mask_with_plex, stream_services_capability

LOG = logging.getLogger(__name__)
PLEX_CALLBACK_URL = "com.linkplay.wiimhome://callback?category=oauth&service=plex"
PLEX_CALLBACK_URL_ENCODED = "com.linkplay.wiimhome%3A%2F%2Fcallback%3Fcategory%3Doauth%26service%3Dplex"

DEFAULT_EQ_PLUGIN = "http://moddevices.com/plugins/caps/EqNp"
DEFAULT_EQ_BAND_FREQS = {
    "a": 30.0,
    "b": 62.5,
    "c": 125.0,
    "d": 250.0,
    "e": 500.0,
    "f": 1000.0,
    "g": 2000.0,
    "h": 4000.0,
    "i": 8000.0,
    "j": 16000.0,
}
LOG_BODY_LIMIT = 5000


@dataclass(frozen=True)
class ParsedCommand:
    """Normalized representation of a Linkplay-style HTTP command."""

    name: str
    args: tuple[str, ...] = ()


@dataclass
class EventSubscriber:
    """UPnP event subscription tracked for NOTIFY callbacks."""

    sid: str
    callback_url: str
    service: str
    seq: int = 0


def parse_command(command: str) -> ParsedCommand:
    """Parse the HTTP ``command`` query parameter into a stable command name.

    Args:
        command: Raw command value from ``/httpapi.asp``.

    Returns:
        ``ParsedCommand`` with URL-bearing arguments decoded where needed.
    """

    if command.startswith("setPlayerCmd:play:"):
        return ParsedCommand("play", (unquote(command.removeprefix("setPlayerCmd:play:")),))
    if command.startswith("setPlayerCmd:playlist:"):
        rest = command.removeprefix("setPlayerCmd:playlist:")
        url, _, index = rest.rpartition(":")
        return ParsedCommand("playlist", (unquote(url), index or "0"))
    if command.startswith("setPlayerCmd:m3u:play:"):
        return ParsedCommand("m3u_play", (unquote(command.removeprefix("setPlayerCmd:m3u:play:")),))
    if command.startswith("setPlayerCmd:seek:"):
        return ParsedCommand("seek", (command.rsplit(":", 1)[-1],))
    if command.startswith("setPlayerCmd:vol:"):
        return ParsedCommand("volume", (command.rsplit(":", 1)[-1],))
    if command.startswith("setPlayerCmd:mute:"):
        return ParsedCommand("mute", (command.rsplit(":", 1)[-1],))
    if command in {"setPlayerCmd:pause", "setPlayerCmd:resume", "setPlayerCmd:onepause", "setPlayerCmd:stop"}:
        return ParsedCommand(command.rsplit(":", 1)[-1])
    return ParsedCommand(command)


class HttpApi:
    """aiohttp control surface for HTTP API, UPnP stubs, and app events.

    The class owns app-facing queue state, EQ presets, service capability
    experiments, and event subscribers. It delegates actual playback and DSP to
    ``MpvBackend`` and serializes visible state through ``RendererState``.
    """

    def __init__(self, cfg: Config, state: RendererState, mpv: MpvBackend) -> None:
        """Create an API handler bound to config, renderer state, and mpv."""

        self.cfg = cfg
        self.state = state
        self.mpv = mpv
        self.queue_url = ""
        self.queue_title = ""
        self.queue_name = "CurrentQueue"
        self.queue_context = ""
        self.queue_resolved_url = ""
        self.queue_source = ""
        self.queue_search_url = ""
        self.queue_pic_url = ""
        self.queue_quality = ""
        self.queue_stream_type = ""
        self.queue_content_type = ""
        self.queue_is_live = True
        self.queue_duration_sec = 0
        self._last_resolve_duration_sec = 0
        self._play_generation = 0
        self._playback_offset_sec = 0
        self._pending_seek_target: int | None = None
        self._pending_seek_generation = 0
        self._pending_seek_final_status = "play"
        self.eq_bands_by_plugin: dict[str, list[dict[str, object]]] = {}
        self.eq_source_bands_by_plugin: dict[str, dict[str, list[dict[str, object]]]] = {}
        self.eq_names_by_plugin: dict[str, str] = {}
        self.eq_channel_by_plugin: dict[str, str] = {}
        self.eq_active_plugin_uri = DEFAULT_EQ_PLUGIN
        self.eq_active_source_name = ""
        self.eq_enabled = True
        self.eq_presets: dict[str, dict[str, list[dict[str, object]]]] = {}
        self.eq_current_preset = ""
        self._load_persisted_eq_presets()
        self.event_subscribers: dict[str, EventSubscriber] = {}
        self.channel_balance = cfg.channel_balance
        self.device_name_override = ""
        self.logged_in_stream_services: set[str] = set()
        self.plex_client_id = self._load_or_create_plex_client_id()
        self.plex_user_info = self._load_plex_user_info()

    def app(self) -> web.Application:
        """Build the aiohttp application and route table."""

        app = web.Application()
        app.add_routes(
            [
                web.get("/httpapi.asp", self.handle_httpapi),
                web.get("/description.xml", self.description),
                web.get("/upnp/AVTransport.xml", self.avtransport_xml),
                web.get("/upnp/rendertransportSCPD.xml", self.avtransport_xml),
                web.get("/upnp/RenderingControl.xml", self.rendering_xml),
                web.get("/upnp/rendercontrolSCPD.xml", self.rendering_xml),
                web.get("/upnp/ConnectionManager.xml", self.connmanager_xml),
                web.get("/upnp/renderconnmgrSCPD.xml", self.connmanager_xml),
                web.get("/upnp/PlayQueue.xml", self.playqueue_xml),
                web.get("/upnp/PlayQueueSCPD.xml", self.playqueue_xml),
                web.get("/icons/{name}", self.icon),
                web.get("/proxy/audio", self.proxy_audio),
                web.get("/deezer", self.deezer_root),
                web.get("/deezer/{tail:.*}", self.deezer_api),
                web.get("/upnp/control/{service}", self.soap),
                web.post("/upnp/control/{service}", self.soap),
                web.route("SUBSCRIBE", "/upnp/event/{service}", self.event_subscribe),
                web.route("UNSUBSCRIBE", "/upnp/event/{service}", self.event_unsubscribe),
            ]
        )
        return app

    async def sync_mpv_state(self) -> None:
        """Refresh visible transport position and title from mpv snapshot."""

        snap = await self.mpv.snapshot()
        if isinstance(snap.get("playback-time"), (int, float)):
            playback_time = self._adjusted_playback_time(snap)
            if self._pending_seek_target is None or abs(playback_time - self._pending_seek_target) <= 2:
                self.state.curpos = playback_time
        if self.queue_is_live:
            self.state.totlen = 0
        elif self.queue_duration_sec > 0:
            self.state.totlen = self.queue_duration_sec
        elif isinstance(snap.get("duration"), (int, float)) and int(snap["duration"]) > 0:
            self.state.totlen = int(snap["duration"])
        if snap.get("media-title"):
            self.state.title = str(snap["media-title"])

    async def handle_httpapi(self, request: web.Request) -> web.Response:
        """Handle ``/httpapi.asp`` commands and serialize text/JSON results."""

        command = request.query.get("command", "")
        parsed = parse_command(command)
        LOG.info(
            "HTTP API remote=%s host=%s path=%s ua=%s command=%s parsed=%s",
            request.remote,
            request.host,
            self._redact_sensitive_text(request.path_qs),
            request.headers.get("User-Agent", ""),
            self._redact_sensitive_text(command),
            self._redact_sensitive_text(str(parsed)),
        )
        forced_failure_commands = {"GetAcousticCapability"}
        if os.environ.get("WIIM_HOME_AUDIO_OUTPUT_CAPABILITY_MODE") == "fail":
            forced_failure_commands.update({"getSpdifOutMaxCap", "getCoaxOutMaxCap", "GetFadeFeature", "getSpdifOutSwitchDelayMs"})
        force_failure = parsed.name in forced_failure_commands or (
            (
                parsed.name.startswith("getAudioOutputVrmsSupportList")
                or parsed.name.startswith("getAuxVoltageSupportList")
            )
            and os.environ.get("WIIM_HOME_AUDIO_OUTPUT_CAPABILITY_MODE") == "fail"
        )
        if force_failure and os.environ.get("WIIM_HOME_ACOUSTIC_CAPABILITY_MODE") == "fail":
            LOG.info("HTTP API forced failure for %s", parsed.name)
            self._log_http_detail(request, "", 404, "Failed")
            return web.Response(status=404, text="Failed", content_type="text/plain")
        try:
            payload = await self.execute(parsed, request.remote or self.cfg.resolved_advertise_ip)
        except Exception:
            LOG.exception("command failed")
            return web.Response(text="Failed", content_type="text/plain")
        if isinstance(payload, dict):
            LOG.info("HTTP API response=json keys=%s", ",".join(payload.keys()))
            self._log_http_detail(request, "", 200, json.dumps(payload, separators=(",", ":")))
            return web.json_response(payload)
        LOG.info("HTTP API response=text")
        self._log_http_detail(request, "", 200, str(payload))
        return web.Response(text=str(payload), content_type="text/plain")

    async def execute(self, parsed: ParsedCommand, remote: str) -> dict[str, object] | str:
        """Execute a parsed HTTP command.

        Args:
            parsed: Normalized command from ``parse_command``.
            remote: Client address used where a command needs peer context.

        Returns:
            Either a JSON-serializable mapping or a plain text response.

        Side Effects:
            May mutate renderer state, persist settings, schedule UPnP events,
            or issue mpv playback/DSP commands.
        """

        name = parsed.name
        if name in {"getStatusEx", "getStatus"}:
            await self.sync_mpv_state()
            return self._with_experimental_sources(self.state.as_status_ex(self.cfg.resolved_advertise_ip))
        if name.startswith("getStatus:ip:"):
            await self.sync_mpv_state()
            return self._with_experimental_sources(self.state.as_status_ex(self.cfg.resolved_advertise_ip))
        if name == "getPlayerStatus":
            await self.sync_mpv_state()
            return self.state.as_player_status()
        if name == "getMetaInfo":
            return self.state.as_meta_info()
        if name == "getPresetInfo":
            presets: list[dict[str, object]] = [asdict(p) for p in self.cfg.podcast_presets]
            if self.cfg.source_experiment_variant in {"preset", "open_network_favorite", "all"}:
                presets.extend(self._experimental_preset_payloads())
            return {"preset_num": str(len(presets)), "presets": presets}
        if name == "getPowerState":
            return {"power": "on", "status": "on"}
        if name == "getDeviceInfo":
            return self._with_experimental_sources(self.state.as_status_ex(self.cfg.resolved_advertise_ip))
        if name == "getNewVersion":
            return {"status": "0", "version": self.state.as_status_ex(self.cfg.resolved_advertise_ip)["firmware"]}
        if name == "getDebugInfo":
            return {"status": "OK", "debug": "fake-linkplay"}
        if name.startswith("SetStreamServicesCapability:Active:") or name.startswith("SetStreamServicesCapability:Deactive:"):
            LOG.info("StreamServicesCapability toggle command=%s", name)
            return {"status": "0"}
        if self._is_stream_services_capability_command(name):
            return self._stream_services_capability()
        if name in {"getMvRemoteUpdateStartCheck", "getMvRemoteUpdateStart", "getMvRemoteUpdateStatus"}:
            return {"status": "0", "update": "none", "progress": "0"}
        if name in {"setTimezone", "setTuneinLocation"} or name.startswith("setTimezone:") or name.startswith("setTuneinLocation:"):
            return {"status": "0"}
        if name.startswith("setDeviceName:") or name.startswith("setHexDeviceName:"):
            requested_name = self._decode_device_name_command(name)
            if requested_name:
                self.device_name_override = requested_name
                self.cfg.friendly_name = requested_name
                self.cfg.mdns_instance_name = requested_name
                self._persist_runtime_value("device_name", requested_name)
                LOG.info("Persisted WiiM Home device rename request name=%s", requested_name)
                self._schedule_event_notify()
            return "OK"
        if name == "getbtstatus":
            return {"status": "0", "bt_status": "0", "connect": "0"}
        if name == "getbthistory":
            return {"status": "0", "history": [], "list": []}
        if name == "GetAcousticCapability":
            if os.environ.get("WIIM_HOME_ACOUSTIC_CAPABILITY_MODE") == "peq":
                return {
                    "Version": "4.3",
                    "EQBlock": {"Version": "4.3"},
                    "GEQ": {"Version": "4.3"},
                    "PEQ": {"Version": "4.3", "Filters": ["LS", "PK", "HS", "LP", "HP", "OFF"]},
                    "RC": {"Version": "1.1", "LevelMatch": {"Version": "1.1"}},
                    "SoundMode": {"Version": "1.0", "Modes": [], "DefaultMode": ""},
                    "OutputDelay": {
                        "Version": "1.0",
                        "PerOutputDelay": False,
                        "EnableMicroDelay": False,
                        "MinDelayUs": 0,
                        "MaxDelayUs": 0,
                        "StepDelayUs": 1,
                    },
                    "SubLPF": {"Version": "0"},
                }
            return {
                "Version": "1.0",
                "GEQ": {"Version": "0"},
                "PEQ": {"Version": "0", "Filters": []},
                "SubLPF": {"Version": "0"},
                "EQBlock": {"Version": "0", "Blocks": []},
                "HeadphoneEQ": {"version": "0"},
                "DynamicBass": {"Version": "0", "Min": 0, "Max": 0, "Step": 1},
                "Evaluation": {"Version": "0"},
                "SoundMode": {"version": "0", "Modes": [], "DefaultMode": ""},
                "OutputDelay": {
                    "version": "0",
                    "PerOutputDelay": False,
                    "EnableMicroDelay": False,
                    "MinDelayUs": 0,
                    "MaxDelayUs": 0,
                    "StepDelayUs": 1000,
                },
                "RC": {"Version": "0", "LevelMatch": None},
            }
        if name == "getLMPFilterCapability":
            return {"version": "0"}
        if name == "getDigitalInputAudioTypeSupport":
            return "0x0"
        if name == "getModeRename":
            return {}
        if name == "getAudioInputEnable":
            return {
                "ver": "1.0",
                "audioInput": [
                    {"mode": "wifi", "enable": 1},
                ],
            }
        if name == "getAudioInputStreamState":
            return {"status": "0", "state": "idle", "source": ""}
        if name.startswith("getAudioOutputVrmsSupportList"):
            return {"ver": "1.1", "voltage": []}
        if name.startswith("getAuxVoltageSupportList"):
            return {"ver": "1.1", "voltage": []}
        if name in {"getSpdifOutMaxCap", "getCoaxOutMaxCap"}:
            return {"ver": "1.0", "sampleRate": "48000", "bitDepth": "16", "cfgType": "pcm", "multiList": []}
        if name == "getSpdifOutSwitchDelayMs":
            return {"status": "0", "delayMs": "0", "delay": "0"}
        if name == "GetFadeFeature":
            return {"FadeFeature": 0}
        if name == "getChannelBalance":
            return self.channel_balance
        if name.startswith("setChannelBalance:"):
            self.channel_balance = self._normalize_channel_balance(name.split(":", 1)[1] or "0.0")
            self.cfg.channel_balance = self.channel_balance
            self._persist_runtime_value("channel_balance", self.channel_balance)
            await self.mpv.channel_balance(float(self.channel_balance))
            return "OK"
        if name == "getTotalAudioInsertState":
            return {"status": "0", "insert": "0", "state": "0"}
        if name == "getEndTrackEnable":
            return {"status": "0", "enable": "0"}
        if name == "getShutdown":
            return {"status": "0", "enable": "0", "shutdown": "0"}
        if name == "get_bt_latency":
            return {"status": "0", "latency": "0"}
        if name == "checkBackupSettingState":
            return {"status": "0", "state": "0"}
        if name == "getMvRemoteSilenceUpdateTime":
            return {"status": "0", "time": "0"}
        if name == "getNewAudioOutputHardwareMode":
            return {"status": "0", "mode": "0", "list": []}
        if name == "getDeviceNameChangeable":
            return {"status": "0", "changeable": "1"}
        if name == "getPowerModeTime":
            return {"status": "0", "time": "0", "mode": "0"}
        if name == "getSubLPF":
            return {"status": "0", "enable": "0", "freq": "0"}
        if name == "getTriggeroutStatus":
            return {"status": "0", "enable": "0", "status": "0"}
        if name == "getLowPriorityPromptDisable":
            return {"status": "0", "disable": "0"}
        if name == "Button_Enable_GET":
            return {"status": "0", "enable": "0"}
        if name == "Reload_Button_GET":
            return {"status": "0", "reload": "0"}
        if name == "LED_SWITCH_STATUS_GET":
            return {"status": "0", "enable": "0", "status": "0"}
        if name == "VOL_LED_SWITCH_GET":
            return {"status": "0", "enable": "0", "status": "0"}
        if name.startswith("EQSetLV2SourceBand:"):
            result = self._set_eq_bands(name, source_specific=True)
            if result.get("status") == "OK":
                await self._apply_eq_to_mpv()
            return result
        if name.startswith("EQSetLV2Band:"):
            result = self._set_eq_bands(name)
            if result.get("status") == "OK":
                await self._apply_eq_to_mpv()
            return result
        if name.startswith("EQGetLV2SourceBandEx:"):
            return self._get_eq_bands(name, source_specific=True)
        if name.startswith("EQGetLV2SourceBand:"):
            return self._get_eq_bands(name, source_specific=True)
        if name.startswith("EQGetLV2Band"):
            return self._get_eq_bands(name)
        if name.startswith("EQChangeSourceFX:"):
            payload = self._json_payload(name, "EQChangeSourceFX:")
            self.eq_active_source_name = str(payload.get("source_name") or payload.get("source") or self.eq_active_source_name)
            plugin_uri = str(payload.get("pluginURI") or payload.get("plugin") or self.eq_active_plugin_uri or DEFAULT_EQ_PLUGIN)
            self.eq_active_plugin_uri = plugin_uri
            self.eq_enabled = True
            self._ensure_eq_plugin(plugin_uri, source_specific=True, source_name=self.eq_active_source_name or "wifi")
            await self._apply_eq_to_mpv()
            return {"status": "OK"}
        if name.startswith("EQChangeFX:"):
            plugin_uri = unquote(name.removeprefix("EQChangeFX:")).strip() or DEFAULT_EQ_PLUGIN
            self.eq_active_plugin_uri = plugin_uri
            self.eq_active_source_name = ""
            self.eq_enabled = True
            self._ensure_eq_plugin(plugin_uri)
            LOG.info("EQChangeFX selected plugin=%s", plugin_uri)
            await self._apply_eq_to_mpv()
            return {"status": "OK"}
        if name.startswith("EQv2GetList:") or name.startswith("EQv2GetNewList:"):
            return self._get_eq_preset_list(name)
        if name.startswith("EQSourceSave:"):
            self._save_eq_preset_from_json(name, "EQSourceSave:")
            self._persist_eq_presets()
            return {"status": "OK"}
        if name.startswith("EQSave:"):
            preset_name = unquote(name.removeprefix("EQSave:")) or "Custom"
            self.eq_current_preset = preset_name
            self.eq_presets[preset_name] = {plugin: [dict(band) for band in bands] for plugin, bands in self.eq_bands_by_plugin.items()}
            for plugin in self.eq_bands_by_plugin:
                self.eq_names_by_plugin[plugin] = preset_name
            self._persist_eq_presets()
            LOG.info("EQSave stored preset=%s plugins=%s", preset_name, ",".join(self.eq_presets[preset_name].keys()))
            return {"status": "0"}
        if name.startswith("EQv2SourceLoad:"):
            self._load_eq_preset(name, "EQv2SourceLoad:", source_specific=True)
            self.eq_enabled = True
            await self._apply_eq_to_mpv()
            return self._get_eq_bands(self._eq_get_command_for_active(source_specific=True), source_specific=True)
        if name.startswith("EQv2Load:"):
            self._load_eq_preset(name, "EQv2Load:")
            self.eq_enabled = True
            await self._apply_eq_to_mpv()
            return self._get_eq_bands(self._eq_get_command_for_active(), source_specific=False)
        if name.startswith("EQSourceOff:"):
            payload = self._json_payload(name, "EQSourceOff:")
            self.eq_active_source_name = str(payload.get("source_name") or payload.get("source") or self.eq_active_source_name or "wifi")
            self.eq_active_plugin_uri = str(payload.get("pluginURI") or self.eq_active_plugin_uri or DEFAULT_EQ_PLUGIN)
            self.eq_enabled = False
            await self._apply_eq_to_mpv()
            return {"status": "OK"}
        if name.startswith("EQv2Delete:"):
            self._delete_eq_preset(name)
            self._persist_eq_presets()
            return {"status": "0"}
        if name.startswith("EQv2Rename:"):
            self._rename_eq_preset(name)
            self._persist_eq_presets()
            return {"status": "0"}
        if name in {"EQOn", "EQOff"}:
            self.eq_enabled = name == "EQOn"
            await self._apply_eq_to_mpv()
            return {"status": "0"}
        if name.startswith("EQSetChannelMode:"):
            self._set_eq_channel_mode(name)
            return {"status": "0"}
        if name == "audio_cast:get_speaker_list":
            return {"status": "0", "speakers": [], "list": []}
        if name == "getStaticIpInfo":
            return {
                "wlanStaticIpEnable": 0,
                "wlanStaticIp": "0.0.0.0/0",
                "ethStaticIpEnable": 0,
                "ethStaticIp": "0.0.0.0/0",
            }
        if name == "getRoutineCapability":
            return {"status": "0", "support": "0", "routines": []}
        if name == "getAllRoutines":
            return {"status": "0", "routines": []}
        if name == "getAlexaProfile":
            return {"status": 0, "login": 0, "loggedIn": False, "profile": {}}
        if name in {"getAvsDevInfo", "getAlexaCountry", "alexaLanguageListGet", "getAsrStatus"}:
            return {"status": 0, "enable": 0, "login": 0}
        if name.startswith("get") and ("Capability" in name or "Support" in name):
            return {"status": 0, "result": "OK"}
        if name in {"play", "playlist", "m3u_play"}:
            url = parsed.args[0]
            self.queue_url = url
            self.queue_title = url.rsplit("/", 1)[-1] or "Network stream"
            self.queue_source = "RadioParadise" if "radioparadise.com" in url else "CustomRadio"
            self.queue_search_url = url
            self.queue_pic_url = ""
            self.state.track_source = self.queue_source
            self.state.play_medium = "STATION-NETWORK"
            self.state.album_art_uri = ""
            self.state.track_metadata = self._didl_metadata(url, self.queue_title, self.queue_source)
            await self._play_url(url)
            self._schedule_event_notify()
            return "OK"
        if name == "pause":
            self._pending_seek_generation += 1
            self._pending_seek_target = None
            self.state.mark_pause()
            await self.mpv.pause()
            self._schedule_event_notify()
            return "OK"
        if name in {"resume", "onepause"}:
            self._pending_seek_generation += 1
            self._pending_seek_target = None
            await self.mpv.resume()
            self.state.status = "play"
            self._schedule_event_notify()
            return "OK"
        if name == "stop":
            self._pending_seek_generation += 1
            self._pending_seek_target = None
            self.state.mark_stop()
            await self.mpv.stop()
            self._schedule_event_notify()
            return "OK"
        if name == "seek":
            seconds = max(0, int(parsed.args[0]))
            await self.mpv.seek(seconds)
            self.state.curpos = seconds
            return "OK"
        if name == "volume":
            value = max(0, min(100, int(parsed.args[0])))
            self.state.volume = value
            await self.mpv.volume(value)
            self._schedule_event_notify(("rendercontrol1", "RenderingControl"))
            return "OK"
        if name == "mute":
            value = parsed.args[0] == "1"
            self.state.mute = value
            await self.mpv.mute(value)
            self._schedule_event_notify(("rendercontrol1", "RenderingControl"))
            return "OK"
        return {"error": "Failed", "command": name}

    async def _apply_eq_to_mpv(self) -> None:
        """Translate active EQ state into mpv PEQ bands."""

        bands: list[dict[str, float]] = []
        if self.eq_enabled:
            for raw_band in self._active_eq_bands():
                gain = raw_band["gain"]
                if abs(gain) >= 0.01:
                    bands.append(raw_band)
        await self.mpv.peq(bands)

    def _active_eq_bands(self) -> list[dict[str, float]]:
        """Return normalized EQ bands for the active plugin/source context."""

        plugin_uri = self.eq_active_plugin_uri or next(iter(self.eq_bands_by_plugin), "")
        if self.eq_active_source_name:
            source_plugins = self.eq_source_bands_by_plugin.get(self.eq_active_source_name, {})
            plugin_uri = self.eq_active_plugin_uri if self.eq_active_plugin_uri in source_plugins else next(iter(source_plugins), plugin_uri)
            if plugin_uri and plugin_uri in source_plugins:
                return self._eq_filter_bands(source_plugins[plugin_uri])
        if not plugin_uri:
            return []
        return self._eq_filter_bands(self.eq_bands_by_plugin.get(plugin_uri, []))

    def _ensure_eq_plugin(self, plugin_uri: str, source_specific: bool = False, source_name: str = "wifi") -> None:
        """Ensure an EQ plugin has a default 10-band parameter set."""

        if source_specific:
            source_plugins = self.eq_source_bands_by_plugin.setdefault(source_name, {})
            source_plugins.setdefault(plugin_uri, self.eq_bands_by_plugin.get(plugin_uri, self._default_eq_bands()))
        else:
            self.eq_bands_by_plugin.setdefault(plugin_uri, self._default_eq_bands())

    @staticmethod
    def _eq_filter_bands(bands: list[dict[str, object]]) -> list[dict[str, float]]:
        """Convert app EQ parameter records into mpv filter band records."""

        grouped: dict[str, dict[str, float]] = {}
        for item in bands:
            param_name = item.get("param_name")
            if not isinstance(param_name, str) or "_" not in param_name:
                continue
            band_name, suffix = param_name.rsplit("_", 1)
            if suffix not in {"freq", "gain", "q", "mode"}:
                continue
            try:
                value = float(item.get("value", 0.0))
            except (TypeError, ValueError):
                continue
            grouped.setdefault(band_name, {})[suffix] = value
        result: list[dict[str, float]] = []
        for band_name in sorted(grouped):
            values = grouped[band_name]
            mode = int(values.get("mode", 1.0))
            if mode == -1:
                continue
            result.append(
                {
                    "freq": max(10.0, values.get("freq", 1000.0)),
                    "gain": max(-24.0, min(24.0, values.get("gain", 0.0))),
                    "q": max(0.1, min(30.0, values.get("q", 1.0))),
                    "mode": float(mode),
                }
            )
        return result

    def _persist_runtime_value(self, name: str, value: str) -> None:
        """Persist a small runtime setting under the configured state dir."""

        try:
            state_dir = Path(self.cfg.state_dir)
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / name).write_text(value.strip() + "\n")
        except OSError as exc:
            LOG.warning("could not persist runtime value %s: %s", name, exc)

    @staticmethod
    def _decode_device_name_command(command: str) -> str:
        """Decode plain or hex-encoded device rename commands."""

        if command.startswith("setHexDeviceName:"):
            raw_hex = command.removeprefix("setHexDeviceName:").strip()
            try:
                return bytes.fromhex(raw_hex).decode("utf-8").strip()
            except ValueError:
                LOG.warning("invalid setHexDeviceName payload: %s", raw_hex)
                return ""
            except UnicodeDecodeError:
                LOG.warning("invalid UTF-8 setHexDeviceName payload: %s", raw_hex)
                return ""
        return unquote(command.removeprefix("setDeviceName:")).strip()

    @staticmethod
    def _normalize_channel_balance(raw: str) -> str:
        """Normalize balance input to a stable string in the -1.0..1.0 range."""

        try:
            value = float(unquote(raw))
        except ValueError:
            value = 0.0
        if abs(value) > 1.0 and abs(value) <= 100.0:
            value = value / 100.0
        value = max(-1.0, min(1.0, value))
        return f"{value:.3f}".rstrip("0").rstrip(".") if value else "0.0"

    def _set_eq_bands(self, command: str, source_specific: bool = False) -> dict[str, object]:
        """Store EQ band updates from an app JSON command."""

        prefix = "EQSetLV2SourceBand:" if source_specific else "EQSetLV2Band:"
        raw = unquote(command.removeprefix(prefix))
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            LOG.info("EQSetLV2Band invalid JSON: %s", exc)
            return {"status": "1", "error": "invalid json"}
        plugin_uri = str(payload.get("pluginURI") or DEFAULT_EQ_PLUGIN)
        self.eq_active_plugin_uri = plugin_uri
        source_name = str(payload.get("source_name") or payload.get("source") or "wifi")
        if not source_specific:
            self.eq_active_source_name = ""
        self.eq_names_by_plugin[plugin_uri] = str(payload.get("Name") or self.eq_names_by_plugin.get(plugin_uri) or "Flat")
        self.eq_channel_by_plugin[plugin_uri] = str(payload.get("channelMode") or self.eq_channel_by_plugin.get(plugin_uri) or "Stereo")
        bands = payload.get("EQBand")
        if not isinstance(bands, list):
            bands = []
        if source_specific:
            source_plugins = self.eq_source_bands_by_plugin.setdefault(source_name, {})
            normalized = self._merge_eq_bands(source_plugins.get(plugin_uri), bands)
            source_plugins[plugin_uri] = normalized
            self.eq_active_source_name = source_name
        else:
            normalized = self._merge_eq_bands(self.eq_bands_by_plugin.get(plugin_uri), bands)
            self.eq_bands_by_plugin[plugin_uri] = normalized
        preset_name = str(payload.get("Name") or "")
        if not preset_name:
            preset_name = "Flat" if self._is_flat_eq_bands(normalized) else "Custom"
        self.eq_current_preset = preset_name
        self.eq_names_by_plugin[plugin_uri] = preset_name
        LOG.info("EQSetLV2Band stored source=%s plugin=%s bands=%s", source_name if source_specific else "", plugin_uri, len(normalized))
        return {"status": "OK"}

    def _get_eq_bands(self, command: str, source_specific: bool = False) -> dict[str, object]:
        """Return EQ band state for the requested or active plugin."""

        plugin_uri = ""
        source_name = ""
        prefix = "EQGetLV2SourceBandEx:" if command.startswith("EQGetLV2SourceBandEx:") else "EQGetLV2SourceBand:" if source_specific else "EQGetLV2Band:"
        if command.startswith(prefix):
            raw = unquote(command.removeprefix(prefix))
            if raw.startswith("{"):
                payload = self._json_payload(command, prefix)
                plugin_uri = str(payload.get("pluginURI") or "")
                source_name = str(payload.get("source_name") or payload.get("source") or "")
            else:
                plugin_uri = raw
        if not plugin_uri:
            source_plugins = self.eq_source_bands_by_plugin.get(source_name or self.eq_active_source_name or "wifi", {})
            if self.eq_active_plugin_uri in source_plugins or not source_plugins:
                plugin_uri = self.eq_active_plugin_uri
            else:
                plugin_uri = next(iter(source_plugins), self.eq_active_plugin_uri)
            plugin_uri = plugin_uri or next(iter(self.eq_bands_by_plugin), DEFAULT_EQ_PLUGIN)
        self.eq_active_plugin_uri = plugin_uri
        if source_specific:
            source_name = source_name or self.eq_active_source_name or "wifi"
            bands = self.eq_source_bands_by_plugin.setdefault(source_name, {}).setdefault(plugin_uri, self._default_eq_bands())
        else:
            bands = self.eq_bands_by_plugin.setdefault(plugin_uri, self._default_eq_bands())
        payload: dict[str, object] = {
            "status": "OK",
            "EQStat": "On" if self.eq_enabled else "Off",
            "Name": self.eq_names_by_plugin.get(plugin_uri, self.eq_current_preset or "Flat"),
            "pluginURI": plugin_uri,
            "channelMode": self.eq_channel_by_plugin.get(plugin_uri, "Stereo"),
            "EQBand": bands,
            "EQBandL": [],
            "EQBandR": [],
        }
        if source_specific:
            payload["source_name"] = source_name
        else:
            payload["source_name"] = ""
        return payload

    def _get_eq_preset_list(self, command: str) -> dict[str, object]:
        """Return built-in and user-saved EQ presets for the active plugin."""

        raw = unquote(command.split(":", 1)[1]) if ":" in command else ""
        if raw.startswith("{"):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
            plugin_uri = str(payload.get("pluginURI") or payload.get("plugin") or self.eq_active_plugin_uri or DEFAULT_EQ_PLUGIN)
        else:
            plugin_uri = raw or self.eq_active_plugin_uri or DEFAULT_EQ_PLUGIN
        self._ensure_eq_plugin(plugin_uri)
        names = [self._normalize_eq_preset_name(name) for name in sorted(self.eq_presets.keys())]
        if plugin_uri.endswith("/Eq4p") or plugin_uri.endswith("/Eq10HP"):
            return {"status": "OK", "preset": ["Flat", "Bass Boost", "Treble Boost"], "custom": names, "pluginURI": plugin_uri}
        presets = [self._eq_list_item(name, "preset") for name in ("Flat", "Bass Boost", "Treble Boost")]
        custom = [self._eq_list_item(name, "custom") for name in names]
        return {"status": "OK", "preset": presets, "custom": custom, "pluginURI": plugin_uri}

    def _save_eq_preset_from_json(self, command: str, prefix: str) -> None:
        """Persist the currently active EQ bands under a user-visible name."""

        payload = self._json_payload(command, prefix)
        name = self._normalize_eq_preset_name(str(payload.get("Name") or payload.get("name") or "Custom"))
        plugin_uri = str(payload.get("pluginURI") or self.eq_active_plugin_uri or DEFAULT_EQ_PLUGIN)
        source_name = str(payload.get("source_name") or payload.get("source") or self.eq_active_source_name or "")
        self.eq_current_preset = name
        self.eq_active_plugin_uri = plugin_uri
        if source_name:
            self.eq_active_source_name = source_name
            source_plugins = self.eq_source_bands_by_plugin.get(source_name, {})
            bands = source_plugins.get(plugin_uri, self.eq_bands_by_plugin.get(plugin_uri, self._default_eq_bands()))
        else:
            bands = self.eq_bands_by_plugin.get(plugin_uri, self._default_eq_bands())
        self.eq_presets.setdefault(name, {})[plugin_uri] = [dict(band) for band in bands]
        self.eq_names_by_plugin[plugin_uri] = name
        self.eq_channel_by_plugin[plugin_uri] = str(payload.get("channelMode") or self.eq_channel_by_plugin.get(plugin_uri) or "Stereo")
        LOG.info("EQSourceSave stored preset=%s plugin=%s source=%s", name, plugin_uri, source_name)

    def _load_eq_preset(self, command: str, prefix: str, source_specific: bool = False) -> None:
        """Load a saved or built-in EQ preset into the active plugin context."""

        payload = self._json_payload(command, prefix)
        name = self._normalize_eq_preset_name(str(payload.get("Name") or payload.get("name") or self.eq_current_preset or "Flat"))
        plugin_uri = str(payload.get("pluginURI") or DEFAULT_EQ_PLUGIN)
        source_name = str(payload.get("source_name") or payload.get("source") or self.eq_active_source_name or "wifi")
        self.eq_active_plugin_uri = plugin_uri
        if source_specific:
            self.eq_active_source_name = source_name
        else:
            self.eq_active_source_name = ""
        self.eq_current_preset = name
        if name in self.eq_presets and plugin_uri in self.eq_presets[name]:
            bands = [dict(band) for band in self.eq_presets[name][plugin_uri]]
        elif name == "Flat":
            bands = self._default_eq_bands()
        elif name == "Bass Boost":
            bands = self._preset_eq_bands({"a_gain": 5.0, "b_gain": 3.0})
        elif name == "Treble Boost":
            bands = self._preset_eq_bands({"i_gain": 3.0, "j_gain": 5.0})
        else:
            bands = self.eq_bands_by_plugin.get(plugin_uri, self._default_eq_bands())
        if source_specific:
            self.eq_source_bands_by_plugin.setdefault(source_name, {})[plugin_uri] = bands
        else:
            self.eq_bands_by_plugin[plugin_uri] = bands
        self.eq_names_by_plugin[plugin_uri] = name
        self.eq_channel_by_plugin[plugin_uri] = str(payload.get("channelMode") or self.eq_channel_by_plugin.get(plugin_uri) or "Stereo")

    def _eq_get_command_for_active(self, source_specific: bool = False) -> str:
        if source_specific:
            payload = {
                "source_name": self.eq_active_source_name or "wifi",
                "pluginURI": self.eq_active_plugin_uri or DEFAULT_EQ_PLUGIN,
            }
            return "EQGetLV2SourceBand:" + json.dumps(payload, separators=(",", ":"))
        return "EQGetLV2Band:" + (self.eq_active_plugin_uri or DEFAULT_EQ_PLUGIN)

    def _eq_list_item(self, name: str, item_type: str) -> dict[str, object]:
        return {
            "Name": name,
            "Type": item_type,
            "channelMode": "Stereo",
            "UpdateAt": "0",
        }

    @staticmethod
    def _normalize_eq_preset_name(name: str) -> str:
        name = name.strip()
        if name.startswith("{") and name.endswith("}") and "Name=" in name:
            match = re.search(r"(?:^|\{|,\s*)Name=([^,}]+)", name)
            if match:
                return match.group(1).strip() or "Custom"
        return name or "Custom"

    def _load_persisted_eq_presets(self) -> None:
        try:
            path = Path(self.cfg.state_dir) / "eq_presets.json"
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        presets = payload.get("presets")
        if isinstance(presets, dict):
            loaded: dict[str, dict[str, list[dict[str, object]]]] = {}
            for name, plugins in presets.items():
                if not isinstance(name, str) or not isinstance(plugins, dict):
                    continue
                clean_name = self._normalize_eq_preset_name(name)
                loaded.setdefault(clean_name, {})
                for plugin_uri, bands in plugins.items():
                    if isinstance(plugin_uri, str) and isinstance(bands, list):
                        loaded[clean_name][plugin_uri] = [dict(band) for band in bands if isinstance(band, dict)]
            self.eq_presets = loaded
        current = payload.get("current")
        if isinstance(current, str):
            self.eq_current_preset = self._normalize_eq_preset_name(current)

    def _persist_eq_presets(self) -> None:
        try:
            state_dir = Path(self.cfg.state_dir)
            state_dir.mkdir(parents=True, exist_ok=True)
            payload = {"current": self.eq_current_preset, "presets": self.eq_presets}
            (state_dir / "eq_presets.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        except OSError as exc:
            LOG.warning("could not persist EQ presets: %s", exc)

    def _delete_eq_preset(self, command: str) -> None:
        payload = self._json_payload(command, "EQv2Delete:")
        name = str(payload.get("Name") or payload.get("name") or "")
        if name:
            self.eq_presets.pop(name, None)
            if self.eq_current_preset == name:
                self.eq_current_preset = ""

    def _rename_eq_preset(self, command: str) -> None:
        payload = self._json_payload(command, "EQv2Rename:")
        old_name = str(payload.get("Name") or payload.get("name") or "")
        new_name = str(payload.get("newName") or payload.get("NewName") or "")
        if old_name and new_name and old_name in self.eq_presets:
            self.eq_presets[new_name] = self.eq_presets.pop(old_name)
            if self.eq_current_preset == old_name:
                self.eq_current_preset = new_name
            for plugin, preset_name in list(self.eq_names_by_plugin.items()):
                if preset_name == old_name:
                    self.eq_names_by_plugin[plugin] = new_name

    def _set_eq_channel_mode(self, command: str) -> None:
        raw = unquote(command.removeprefix("EQSetChannelMode:"))
        payload = self._json_payload(command, "EQSetChannelMode:") if raw.startswith("{") else {}
        plugin_uri = str(payload.get("pluginURI") or DEFAULT_EQ_PLUGIN)
        self.eq_active_plugin_uri = plugin_uri
        channel_mode = str(payload.get("channelMode") or raw or "Stereo")
        self.eq_channel_by_plugin[plugin_uri] = channel_mode

    @staticmethod
    def _json_payload(command: str, prefix: str) -> dict[str, object]:
        raw = unquote(command.removeprefix(prefix))
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _default_eq_bands() -> list[dict[str, object]]:
        bands: list[dict[str, object]] = []
        for letter, freq in DEFAULT_EQ_BAND_FREQS.items():
            bands.extend(
                [
                    {"param_name": f"{letter}_freq", "value": freq},
                    {"param_name": f"{letter}_gain", "value": 0.0},
                    {"param_name": f"{letter}_q", "value": 0.25},
                    {"param_name": f"{letter}_mode", "value": 1},
                ]
            )
        return bands

    def _preset_eq_bands(self, updates: dict[str, float]) -> list[dict[str, object]]:
        bands = self._default_eq_bands()
        for item in bands:
            param_name = item.get("param_name")
            if isinstance(param_name, str) and param_name in updates:
                item["value"] = updates[param_name]
        return bands

    @staticmethod
    def _is_flat_eq_bands(bands: list[dict[str, object]]) -> bool:
        for item in bands:
            param_name = item.get("param_name")
            if not isinstance(param_name, str):
                continue
            try:
                value = float(item.get("value", 0.0))
            except (TypeError, ValueError):
                continue
            if param_name.endswith("_gain") and abs(value) >= 0.01:
                return False
            if param_name.endswith("_mode") and value not in {-1.0, 1.0}:
                return False
        return True

    def _merge_eq_bands(self, current: list[dict[str, object]] | None, updates: list[object]) -> list[dict[str, object]]:
        """Merge partial EQ updates while preserving canonical 10-band order."""

        merged = {str(item.get("param_name")): dict(item) for item in (current or self._default_eq_bands()) if isinstance(item.get("param_name"), str)}
        for item in updates:
            if not isinstance(item, dict):
                continue
            param_name = item.get("param_name")
            if isinstance(param_name, str):
                merged[param_name] = dict(item)
        ordered: list[dict[str, object]] = []
        for letter in DEFAULT_EQ_BAND_FREQS:
            for suffix in ("freq", "gain", "q", "mode"):
                key = f"{letter}_{suffix}"
                if key in merged:
                    ordered.append(merged[key])
        for key, item in merged.items():
            if not re.fullmatch(r"[a-j]_(freq|gain|q|mode)", key):
                ordered.append(item)
        return ordered

    def _enabled_experimental_sources(self) -> list[dict[str, object]]:
        """Return enabled configured source experiments as app-facing records."""

        sources: list[dict[str, object]] = []
        for source in self.cfg.experimental_sources:
            if not source.enabled:
                continue
            source_name = source.source or source.name
            icon = source.icon
            if icon:
                icon = icon.replace("{host}", self.cfg.resolved_advertise_ip).replace("{port}", str(self.cfg.http_api_port))
            else:
                icon = f"http://{self.cfg.resolved_advertise_ip}:{self.cfg.http_api_port}/icons/{source.id}.svg"
            sources.append(
                {
                    "id": source.id,
                    "source_id": source.id,
                    "name": source.name,
                    "title": source.name,
                    "source": source_name,
                    "SourceName": source_name,
                    "service": source_name,
                    "url": source.url,
                    "uri": source.url,
                    "icon": icon,
                    "icon_url": icon,
                    "type": source.type,
                    "enabled": True,
                }
            )
        return sources

    def _experimental_preset_payloads(self) -> list[dict[str, object]]:
        presets: list[dict[str, object]] = []
        for index, source in enumerate(self._enabled_experimental_sources(), start=1):
            presets.append(
                {
                    "id": source["id"],
                    "name": source["name"],
                    "title": source["name"],
                    "url": source["url"],
                    "source": source["source"],
                    "type": source["type"],
                    "icon": source["icon"],
                    "index": str(index),
                }
            )
        return presets

    def _with_experimental_sources(self, payload: dict[str, object]) -> dict[str, object]:
        """Add configured source/service experiment fields to a status payload."""

        variant = self.cfg.source_experiment_variant
        sources = self._enabled_experimental_sources()
        if variant == "tunein":
            payload = dict(payload)
            payload.update(self._tunein_capability_fields())
            return payload
        if variant == "none" or not sources:
            return payload
        payload = dict(payload)
        if variant in {"new_source", "music_service", "all"}:
            payload["source_list"] = sources
            payload["sourceList"] = sources
            payload["sources"] = sources
            payload["service_list"] = sources
            payload["services"] = sources
            payload["music_services"] = sources
            payload["musicServices"] = sources
            payload["streamServicesCapability"] = self._stream_services_capability()
            payload["supported_sources"] = ",".join(str(item["source"]) for item in sources)
            payload["supported_services"] = ",".join(str(item["service"]) for item in sources)
        if variant in {"radioparadise_replacement", "all"}:
            first = sources[0]
            payload["RadioParadise"] = first["name"]
            payload["radio_paradise_name"] = first["name"]
            payload["radio_paradise_url"] = first["url"]
        if variant in {"linkplay_radio_replacement", "all"}:
            first = sources[0]
            payload["LinkplayRadio"] = first["name"]
            payload["linkplay_radio_name"] = first["name"]
            payload["linkplay_radio_url"] = first["url"]
        return payload

    @staticmethod
    def _is_stream_services_capability_command(command: str) -> bool:
        lowered = command.lower()
        return (
            "streamservice" in lowered
            or "stream_service" in lowered
            or "musicservice" in lowered
            or "music_service" in lowered
        ) and ("capability" in lowered or command.lower().startswith("get"))

    def _stream_services_capability(self) -> dict[str, object]:
        """Build the StreamServicesCapability response advertised to clients."""

        services: list[dict[str, object]] = [
            {
                "id": "RadioParadise2",
                "version": "1.0",
                "active": True,
                "configurable": False,
                "removable": False,
            },
            {
                "id": "newTuneIn",
                "version": "1.1",
                "active": True,
                "configurable": False,
                "removable": False,
            },
            {
                "id": "tunein",
                "version": "1.0",
                "active": True,
                "configurable": False,
                "removable": False,
            },
            {
                "id": "Samba",
                "version": "1.0",
                "active": True,
                "configurable": False,
                "removable": False,
            },
            {
                "id": "Roon",
                "version": "1.0",
                "active": False,
                "configurable": True,
                "removable": False,
            },
            {
                "id": "Squeezelite",
                "version": "1.2",
                "active": False,
                "configurable": True,
                "removable": False,
            },
        ]
        if self.cfg.tunein_minimal_capability:
            services = [new_tunein_stream_service(self.cfg, modern=self.cfg.enable_plex_service)]
        if self.cfg.enable_plex_service:
            add_plex_stream_service(self.cfg, services)
        add_stream_service_sentinels(self.cfg, services)
        for source in self._enabled_experimental_sources():
            services.append(
                {
                    "id": source["id"],
                    "version": "1.0",
                    "active": True,
                    "configurable": True,
                    "removable": False,
                    "name": source["name"],
                    "brand": source["name"],
                    "url": source["url"],
                    "icon": source["icon"],
                }
            )
        return stream_services_capability(self.cfg, services, "1.4" if self.cfg.enable_plex_service else "1.0")

    def _tunein_capability_fields(self) -> dict[str, object]:
        """Return legacy and modern capability fields that expose TuneIn."""

        capability = self._stream_services_capability()
        plex_enabled = self.cfg.enable_plex_service
        plex_legacy_gate = plex_enabled and self.cfg.enable_plex_legacy_gate
        supported_services = [str(item["id"]) for item in capability["streamServices"] if item.get("active")]
        if plex_legacy_gate and "PLEX" not in supported_services:
            supported_services.append("PLEX")
        music_service_ids = {"newTuneIn", "Plex"}
        if self.cfg.enable_stream_service_sentinels:
            music_service_ids.update(str(item["id"]) for item in capability["streamServices"] if item.get("active"))
        music_services = [
            {"id": item["id"], "name": item.get("name") or ("TuneIn" if item["id"] == "newTuneIn" else item["id"]), "source": item["id"], "enabled": True}
            for item in capability["streamServices"]
            if item.get("active") and item["id"] in music_service_ids
        ]
        if plex_legacy_gate:
            music_services.append({"id": "PLEX", "name": "Plex", "source": "Plex", "enabled": True})
        streams_value = str(self.cfg.tunein_streams_value)
        if plex_legacy_gate:
            streams_value = stream_mask_with_plex(streams_value)
        payload: dict[str, object] = {
            "streams": streams_value,
            "StreamCapability": json.dumps(capability, separators=(",", ":")),
            "streamServicesCapability": capability,
            "servicesCapability": capability,
            "supported_services": ",".join(supported_services),
            "music_services": music_services,
            "musicServices": music_services,
        }
        if self.cfg.tunein_stream_all_value is not None:
            payload["streams_all"] = stream_mask_with_plex(self.cfg.tunein_stream_all_value) if plex_legacy_gate else str(self.cfg.tunein_stream_all_value)
        elif plex_legacy_gate:
            payload["streams_all"] = streams_value
        return payload

    async def _play_url(self, url: str) -> None:
        """Resolve, load, and wait for an audio URL to become playable in mpv."""

        self._play_generation += 1
        self._pending_seek_generation += 1
        self._pending_seek_target = None
        generation = self._play_generation
        self.state.mark_transitioning()
        self._schedule_event_notify(("rendertransport1", "AVTransport"))
        direct_url = self.queue_resolved_url if self.queue_url == url and self.queue_resolved_url else await self._resolve_playback_url(url)
        if generation != self._play_generation:
            LOG.info("discarding stale play request generation=%s current=%s url=%s", generation, self._play_generation, url)
            return
        if direct_url != url:
            LOG.info("resolved playback url original=%s resolved=%s", url, direct_url)
        playback_url = self._backend_playback_url(url, direct_url)
        mpv_options = self._mpv_playback_options(url, direct_url)
        self._playback_offset_sec = 0
        self.state.resolved_url = direct_url
        LOG.info(
            "starting playback generation=%s original=%s resolved=%s backend=%s mpv_options=%s source=%s title=%s",
            generation,
            url,
            direct_url,
            playback_url,
            mpv_options,
            self.state.track_source,
            self.state.title,
        )
        response = await self.mpv.loadfile(playback_url, mpv_options)
        if isinstance(response, dict) and response.get("error") not in {None, "success"}:
            LOG.warning("mpv refused playback original=%s resolved=%s backend=%s response=%s", url, direct_url, playback_url, response)
            self.state.mark_stop()
            return
        ready = await self._wait_for_mpv_audio_ready(generation, playback_url)
        if not ready:
            LOG.warning("mpv did not report audio-ready original=%s resolved=%s backend=%s", url, direct_url, playback_url)
            self.state.mark_stop()
            self._schedule_event_notify(("rendertransport1", "AVTransport"))
            return
        await self.sync_mpv_state()
        self.state.mark_playing(url)

    def _backend_playback_url(self, original_url: str, direct_url: str) -> str:
        # WiiM Home can hand us service profile URLs rather than final media
        # URLs. For on-demand audio we optionally proxy the resolved media so
        # range seeks can be translated into fresh upstream HTTP requests.
        if (
            self.cfg.proxy_on_demand_audio
            and not self.queue_is_live
            and direct_url.startswith(("http://", "https://"))
            and "api.radiotime.com/" not in direct_url
            and original_url != direct_url
        ):
            proxied = self._local_audio_proxy_url(direct_url)
            LOG.info("using local proxy for on-demand playback direct=%s proxy=%s", direct_url, proxied)
            return proxied
        return direct_url

    def _mpv_playback_options(self, original_url: str, direct_url: str) -> dict[str, str]:
        """Return per-load mpv options for normal playback.

        Args are kept because experimental branches may need to choose options
        based on the service URL shape.
        """

        return {}

    @staticmethod
    def _mpv_seek_reload_options() -> dict[str, str]:
        return {
            "demuxer": "lavf",
            "force-seekable": "yes",
            "cache": "no",
        }

    def _should_reload_for_large_seek(self, target: int, previous_pos: int) -> bool:
        """Return whether a seek should reload via byte-range proxy."""

        direct_url = self.queue_resolved_url or self.state.resolved_url
        if not self.cfg.range_seek_reload:
            return False
        if self.queue_is_live or self.cfg.proxy_on_demand_audio:
            return False
        if not direct_url.startswith(("http://", "https://")):
            return False
        if abs(target - previous_pos) < 60:
            return False
        return self._bitrate_bps_from_url_or_headers(direct_url, {}) > 0

    def _local_audio_proxy_url(self, url: str) -> str:
        return f"http://127.0.0.1:{self.cfg.http_api_port}/proxy/audio?url={quote(url, safe='')}"

    def _seek_audio_proxy_url(self, url: str, start_seconds: int) -> str:
        return f"{self._local_audio_proxy_url(url)}&start={max(0, int(start_seconds))}"

    async def _resolve_playback_url(self, url: str) -> str:
        """Resolve service/profile URLs to direct media URLs when possible."""

        self._last_resolve_duration_sec = 0
        if "api.radiotime.com/" not in url:
            return url
        try:
            timeout = ClientTimeout(total=8)
            async with ClientSession(timeout=timeout, headers={"User-Agent": "Linkplay/4.8"}) as session:
                async with session.get(url, allow_redirects=True) as response:
                    final_url = str(response.url)
                    content_type = response.headers.get("Content-Type", "")
                    text = await response.text(errors="ignore")
        except (ClientError, TimeoutError, OSError) as exc:
            LOG.warning("could not resolve TuneIn/Radiotime URL %s: %s", url, exc)
            return url
        self._last_resolve_duration_sec = self._json_duration_seconds(text)
        if final_url and "api.radiotime.com/" not in final_url and not content_type.lower().startswith(("text/", "application/json", "application/xml")):
            return final_url
        candidate = self._first_stream_url(text)
        if candidate and "api.radiotime.com/" not in candidate:
            return candidate
        LOG.info("TuneIn/Radiotime response did not contain a direct stream URL: status content_type=%s preview=%s", content_type, self._preview(text))
        return final_url or url

    @staticmethod
    def _json_duration_seconds(text: str) -> int:
        """Extract the largest plausible duration value from JSON text."""

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return 0
        candidates: list[int] = []

        def add(value: object) -> None:
            if isinstance(value, (int, float)) and value > 0:
                candidates.append(HttpApi._normalize_duration_seconds(value))
            elif isinstance(value, str):
                stripped = value.strip()
                if re.fullmatch(r"\d+(\.\d+)?", stripped):
                    candidates.append(HttpApi._normalize_duration_seconds(float(stripped)))
                elif re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", stripped):
                    candidates.append(HttpApi._hms_to_seconds(stripped))

        def walk(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key.lower() in {"duration", "durationsec", "durationsecs", "durationseconds", "seconds", "totaltime", "totalseconds"}:
                        add(child)
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(payload)
        return max(candidates) if candidates else 0

    async def _wait_for_mpv_audio_ready(self, generation: int, playback_url: str, timeout: float = 10.0) -> bool:
        """Poll mpv until the loaded item exposes a selected audio track."""

        deadline = asyncio.get_running_loop().time() + timeout
        last_snapshot: dict[str, object] = {}
        while asyncio.get_running_loop().time() < deadline:
            if generation != self._play_generation:
                return False
            await asyncio.sleep(0.5)
            snapshot = await self.mpv.snapshot()
            last_snapshot = snapshot
            if self._mpv_snapshot_has_audio(snapshot):
                LOG.info("mpv audio-ready playback=%s snapshot=%s", playback_url, self._compact_snapshot(snapshot))
                return True
        LOG.warning("mpv audio-ready timeout playback=%s snapshot=%s", playback_url, self._compact_snapshot(last_snapshot))
        return False

    @staticmethod
    def _mpv_snapshot_has_audio(snapshot: dict[str, object]) -> bool:
        """Return whether an mpv snapshot looks like active audio playback."""

        track_list = snapshot.get("track-list")
        if isinstance(track_list, list):
            for track in track_list:
                if isinstance(track, dict) and track.get("type") == "audio" and track.get("selected", True):
                    return True
        if snapshot.get("idle-active") is False or snapshot.get("core-idle") is False:
            if isinstance(snapshot.get("playback-time"), (int, float)):
                return True
            duration = snapshot.get("duration")
            if isinstance(duration, (int, float)) and duration > 0:
                return True
        return False

    async def _finish_seek_after_buffering(self, seek_generation: int, target: int, previous_pos: int) -> None:
        """Complete a seek once mpv reports enough data for stable playback."""

        ready = await self._wait_for_seek_ready(seek_generation, target, previous_pos)
        if seek_generation != self._pending_seek_generation or self._pending_seek_target != target:
            return
        if ready:
            await self.sync_mpv_state()
            self._pending_seek_target = None
            if self._pending_seek_final_status == "pause":
                self.state.mark_pause()
            else:
                self.state.status = "play"
        else:
            self.state.curpos = target
            self.state.mark_transitioning()
            LOG.warning("SOAP Seek not ready; keeping TRANSITIONING position=%s", target)
            self._schedule_event_notify(("rendertransport1", "AVTransport"))
            asyncio.create_task(self._finish_seek_after_buffering(seek_generation, target, target))
            return
        LOG.info("SOAP Seek ready position=%s ready=%s status=%s", self.state.curpos, ready, self.state.status)
        self._schedule_event_notify(("rendertransport1", "AVTransport"))

    async def _wait_for_seek_ready(self, seek_generation: int, target: int, previous_pos: int) -> bool:
        """Poll mpv until a seek target appears to be buffered and audible."""

        distance = abs(target - previous_pos)
        large_seek = distance >= 60
        timeout = 20.0 if large_seek else 8.0
        min_elapsed = 4.0 if large_seek else 0.7
        required_advance = 1.5 if large_seek else 0.6
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + timeout
        last_snapshot: dict[str, object] = {}
        LOG.info(
            "SOAP Seek waiting target=%s previous=%s distance=%s timeout=%.1f min_elapsed=%.1f",
            target,
            previous_pos,
            distance,
            timeout,
            min_elapsed,
        )
        while True:
            if seek_generation != self._pending_seek_generation:
                return False
            await asyncio.sleep(0.35)
            snapshot = await self.mpv.snapshot()
            last_snapshot = snapshot
            elapsed = loop.time() - started
            if self._snapshot_is_seek_ready(snapshot, target, required_advance, large_seek, elapsed, min_elapsed):
                return True
            if loop.time() >= deadline:
                break
        LOG.info(
            "SOAP Seek ready timeout target=%s previous=%s distance=%s snapshot=%s",
            target,
            previous_pos,
            distance,
            self._compact_snapshot(last_snapshot),
        )
        return False

    def _snapshot_is_seek_ready(
        self,
        snapshot: dict[str, object],
        target: int,
        required_advance: float,
        large_seek: bool,
        elapsed: float,
        min_elapsed: float,
    ) -> bool:
        """Evaluate whether one mpv snapshot satisfies seek-complete criteria."""

        if elapsed < min_elapsed:
            return False
        playback_raw = snapshot.get("playback-time")
        if not isinstance(playback_raw, (int, float)):
            return False
        if snapshot.get("core-idle") is True or snapshot.get("idle-active") is True:
            return False
        buffering = snapshot.get("cache-buffering-state")
        if isinstance(buffering, (int, float)) and 0 <= buffering < 100:
            return False
        if not self._mpv_snapshot_has_audio(snapshot):
            return False

        playback_time = self._adjusted_playback_time(snapshot)
        if playback_time >= target + required_advance:
            return True
        if not large_seek and abs(playback_time - target) <= 2:
            return True

        cache_state = snapshot.get("demuxer-cache-state")
        if isinstance(cache_state, dict) and cache_state.get("underrun") is True:
            return False
        if large_seek and playback_time >= target + 0.5 and self._snapshot_has_seek_range(snapshot, target):
            return True
        return False

    def _adjusted_playback_time(self, snapshot: dict[str, object]) -> int:
        """Return mpv playback time adjusted for proxy reload offsets."""

        playback_time = snapshot.get("playback-time")
        if not isinstance(playback_time, (int, float)):
            return self.state.curpos
        return max(0, int(playback_time) + self._playback_offset_sec)

    @staticmethod
    def _snapshot_is_buffering(snapshot: dict[str, object]) -> bool:
        if snapshot.get("core-idle") is True or snapshot.get("idle-active") is True:
            return True
        buffering = snapshot.get("cache-buffering-state")
        if isinstance(buffering, (int, float)) and 0 <= buffering < 100:
            return True
        cache_state = snapshot.get("demuxer-cache-state")
        if isinstance(cache_state, dict) and cache_state.get("underrun") is True:
            return True
        return False

    @staticmethod
    def _snapshot_has_seek_range(snapshot: dict[str, object], target: int) -> bool:
        cache_state = snapshot.get("demuxer-cache-state")
        if not isinstance(cache_state, dict):
            return False
        ranges = cache_state.get("seekable-ranges")
        if not isinstance(ranges, list):
            return False
        for item in ranges:
            if not isinstance(item, dict):
                continue
            start = item.get("start")
            end = item.get("end")
            if isinstance(start, (int, float)) and isinstance(end, (int, float)) and start <= target <= end:
                return True
        return False

    @staticmethod
    def _compact_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
        """Return a compact mpv snapshot suitable for diagnostic logs."""

        compact: dict[str, object] = {}
        for key in (
            "playback-time",
            "duration",
            "media-title",
            "filename",
            "path",
            "stream-open-filename",
            "core-idle",
            "idle-active",
            "pause",
            "cache-buffering-state",
            "demuxer-cache-state",
        ):
            value = snapshot.get(key)
            if value not in (None, ""):
                compact[key] = value
        track_list = snapshot.get("track-list")
        if isinstance(track_list, list):
            compact["track-list"] = [
                {k: v for k, v in track.items() if k in {"type", "selected", "codec", "lang", "title"}}
                for track in track_list
                if isinstance(track, dict)
            ]
        return compact

    @staticmethod
    def _redacted_headers(request: web.Request) -> dict[str, str]:
        """Return request headers with credentials removed or masked."""

        blocked = {"authorization", "cookie", "proxy-authorization"}
        redacted = {}
        for key, value in request.headers.items():
            lowered = key.lower()
            if lowered in blocked:
                continue
            if lowered in {"x-plex-token", "x-plex-client-identifier"}:
                redacted[key] = "***"
            else:
                redacted[key] = HttpApi._redact_sensitive_text(value)
        return redacted

    @classmethod
    def _redacted_query(cls, request: web.Request) -> dict[str, str]:
        """Return query parameters with sensitive values masked."""

        return {key: cls._redact_sensitive_text(value) for key, value in request.query.items()}

    @classmethod
    def _preview(cls, text: str) -> str:
        """Return a redacted, bounded-size preview for logs."""

        text = cls._redact_sensitive_text(text)
        if len(text) <= LOG_BODY_LIMIT:
            return text
        return text[:LOG_BODY_LIMIT] + f"...<truncated {len(text) - LOG_BODY_LIMIT} chars>"

    @staticmethod
    def _redact_sensitive_text(text: object) -> str:
        """Mask credentials, local identifiers, and private addresses in logs."""

        value = str(text)
        # Detail logging is essential for WiiM Home reverse engineering, but
        # captures can contain real cloud-service tokens and login material.
        # Keep this list intentionally broad; false-positive redaction is fine.
        patterns = [
            (r"(?i)(X-Plex-Token=)[^&\s\"'<>]+", r"\1***"),
            (r'(?i)("X-Plex-Token"\s*:\s*")[^"]+', r"\1***"),
            (r"(?i)('X-Plex-Token'\s*:\s*')[^']+", r"\1***"),
            (r"(?i)(x-plex-token:\s*)[^\r\n]+", r"\1***"),
            (r"(?is)(<UserName>).*?(</UserName>)", r"\1***\2"),
            (r"(?is)(<PassWord>).*?(</PassWord>)", r"\1***\2"),
            (r"(?is)(<Password>).*?(</Password>)", r"\1***\2"),
            (r"(?is)(<Token>).*?(</Token>)", r"\1***\2"),
            (r"(?is)(<RefreshToken>).*?(</RefreshToken>)", r"\1***\2"),
            (r"(?is)(<Code>).*?(</Code>)", r"\1***\2"),
            (r"(?is)(<CodeVerifier>).*?(</CodeVerifier>)", r"\1***\2"),
            (r'(?i)("userName"\s*:\s*")[^"]+', r"\1***"),
            (r'(?i)("password"\s*:\s*")[^"]+', r"\1***"),
            (r'(?i)("authToken"\s*:\s*")[^"]+', r"\1***"),
            (r"(?i)(accessToken[\"'=:\s]+)[^,&\"'\s<>}]+", r"\1***"),
            (r"(?i)(refreshToken[\"'=:\s]+)[^,&\"'\s<>}]+", r"\1***"),
            (r"(?i)(token[\"'=:\s]+)[^,&\"'\s<>}]+", r"\1***"),
            (r"\b(?:10|127)\.(?:\d{1,3}\.){2}\d{1,3}\b", "[private-ip]"),
            (r"\b192\.168\.\d{1,3}\.\d{1,3}\b", "[private-ip]"),
            (r"\b172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}\b", "[private-ip]"),
            (r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b", "[mac]"),
            (r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", "[uuid]"),
        ]
        for pattern, repl in patterns:
            value = re.sub(pattern, repl, value)
        return value

    def _log_http_detail(self, request: web.Request, request_body: str, status: int, response_body: str) -> None:
        """Emit opt-in detailed HTTP diagnostics with redacted payloads."""

        if not self.cfg.log_wiim_http_details:
            return
        LOG.info(
            "WIIM_HTTP_DETAIL %s",
            json.dumps(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "remote": request.remote,
                    "method": request.method,
                    "path": request.path,
                    "query": self._redacted_query(request),
                    "headers": self._redacted_headers(request),
                    "request_body": self._preview(request_body),
                    "response_status": status,
                    "response_body": self._preview(response_body),
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )

    @staticmethod
    def _xml_text(body: str, tag: str) -> str:
        """Extract unescaped text from a simple XML tag."""

        match = re.search(rf"<(?:\w+:)?{re.escape(tag)}[^>]*>(.*?)</(?:\w+:)?{re.escape(tag)}>", body, re.IGNORECASE | re.DOTALL)
        return html_unescape(match.group(1).strip()) if match else ""

    @staticmethod
    def _first_stream_url(text: str) -> str:
        """Find the most likely playable URL in JSON, XML, or plain text."""

        expanded = text
        for _ in range(3):
            unescaped = html_unescape(expanded)
            if unescaped == expanded:
                break
            expanded = unescaped
        candidates = HttpApi._json_stream_candidates(expanded)
        patterns = [
            r"<URL>(.*?)</URL>",
            r"<res[^>]*>(https?://.*?)</res>",
            r"(https?://[^\s<\"']+)",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, expanded, re.IGNORECASE | re.DOTALL):
                candidate = html_unescape(match.group(1).strip())
                if candidate not in candidates:
                    candidates.append(candidate)
        if not candidates:
            return ""
        candidates.sort(key=HttpApi._stream_candidate_rank)
        return candidates[0]

    @staticmethod
    def _json_stream_candidates(text: str) -> list[str]:
        """Collect playable-looking URLs from nested JSON structures."""

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []
        candidates: list[str] = []

        def add(value: object) -> None:
            if isinstance(value, str) and value.startswith(("http://", "https://")) and value not in candidates:
                candidates.append(value)

        def walk(value: object) -> None:
            if isinstance(value, dict):
                play = value.get("Play")
                if isinstance(play, dict) and play.get("CanPlay") is not False:
                    add(play.get("PlayUrl"))
                    add(play.get("Url"))
                    add(play.get("URL"))
                for key in ("PlayUrl", "StreamUrl", "Stream", "Url", "URL", "url", "media", "playlist", "File1"):
                    add(value.get(key))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(payload)
        return candidates

    @staticmethod
    def _stream_candidate_rank(url: str) -> tuple[int, int]:
        """Rank extracted URLs so media/playlist candidates beat artwork."""

        lower = url.lower()
        if re.search(r"\.(png|jpe?g|webp|gif|svg)(\?|$)", lower) or "/images/" in lower:
            return (90, len(url))
        if re.search(r"\.(m3u8?|pls|asx)(\?|$)", lower):
            return (0, len(url))
        if re.search(r"\.(mp3|aac|m4a|ogg|opus|flac|wav)(\?|$)", lower):
            return (1, len(url))
        if any(marker in lower for marker in ("/media?", "/stream", "streamtheworld", "icecast", "shoutcast")):
            return (2, len(url))
        if "api.radiotime.com/" in lower:
            return (5, len(url))
        return (20, len(url))

    def _didl_metadata(self, url: str, title: str, source: str = "", art_url: str = "", is_live: bool = True) -> str:
        """Build minimal DIDL-Lite metadata for the current queue item."""

        protocol = "audio/aac" if "radioparadise.com" in url else "audio/mpeg"
        origin_source = source or ("RadioParadise" if "radioparadise.com" in url else "CustomRadio")
        return (
            f'<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
            f'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            f'xmlns:song="www.wiimu.com/song/" '
            f'xmlns:custom="www.linkplay.com/custom/" '
            f'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
            f"<item id=\"{escape(title)}\">"
            f"<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
            f"<song:isLive>{'1' if is_live else '0'}</song:isLive>"
            f"<song:originSource>{escape(origin_source)}</song:originSource>"
            f'<res protocolInfo="http-get:*:{protocol}:*" duration="0">{escape(url)}</res>'
            f"<dc:title>{escape(title)}</dc:title>"
            f"<upnp:artist>{escape('ARD' if origin_source == 'CustomRadio' else title)}</upnp:artist>"
            f"<upnp:creator>{escape('ARD' if origin_source == 'CustomRadio' else title)}</upnp:creator>"
            f"<upnp:album></upnp:album>"
            f"<upnp:albumArtURI>{escape(art_url)}</upnp:albumArtURI>"
            f"<song:image>{escape(art_url)}</song:image>"
            f"</item>"
            f"</DIDL-Lite>"
        )

    def _metadata_art_url(self, metadata: str) -> str:
        """Extract the best artwork URL from DIDL-like metadata."""

        return self._best_art_url(self._xml_text(metadata, "image") or self._xml_text(metadata, "albumArtURI") or self._xml_text(metadata, "PicUrl"))

    @staticmethod
    def _best_art_url(art_url: str) -> str:
        """Prefer larger TuneIn artwork when the URL follows known patterns."""

        if "cdn-profiles.tunein.com/" not in art_url:
            return art_url
        return re.sub(r"/logoq(\.[a-zA-Z0-9]+)(\?|$)", r"/logog\1\2", art_url, count=1)

    def _metadata_with_art(self, metadata: str, art_url: str, is_live: bool | None = None) -> str:
        """Insert or replace artwork and live-state tags in metadata."""

        if not metadata:
            return metadata

        def upsert_tag(text: str, tag: str, value: str) -> str:
            escaped_value = escape(value)
            pattern = rf"<(?:\w+:)?{re.escape(tag)}[^>]*>.*?</(?:\w+:)?{re.escape(tag)}>"
            replacement = f"<{tag}>{escaped_value}</{tag}>"
            if re.search(pattern, text, re.IGNORECASE | re.DOTALL):
                return re.sub(pattern, replacement, text, count=1, flags=re.IGNORECASE | re.DOTALL)
            insert_at = re.search(r"</(?:\w+:)?item>", text, re.IGNORECASE)
            if insert_at:
                return text[: insert_at.start()] + replacement + text[insert_at.start() :]
            return text + replacement

        updated = metadata
        if art_url:
            updated = upsert_tag(updated, "upnp:albumArtURI", art_url)
            updated = upsert_tag(updated, "song:image", art_url)
        if is_live is not None:
            updated = upsert_tag(updated, "song:isLive", "1" if is_live else "0")
        return updated

    @staticmethod
    def _metadata_with_url(metadata: str, url: str) -> str:
        """Replace the first DIDL ``res`` URL with the resolved media URL."""

        if not metadata or not url:
            return metadata

        def replace(match: re.Match[str]) -> str:
            return f"{match.group(1)}{escape(url)}{match.group(3)}"

        return re.sub(r"(<res\b[^>]*>)(.*?)(</res>)", replace, metadata, count=1, flags=re.IGNORECASE | re.DOTALL)

    @staticmethod
    def _metadata_with_duration(metadata: str, duration_sec: int) -> str:
        """Insert or update DIDL duration metadata in seconds."""

        if not metadata:
            return metadata
        value = str(max(0, duration_sec))
        if re.search(r"<res\b[^>]*\bduration=", metadata, re.IGNORECASE):
            return re.sub(r'(<res\b[^>]*\bduration=")[^"]*(")', rf"\g<1>{value}\2", metadata, count=1, flags=re.IGNORECASE)
        return re.sub(r"(<res\b)", rf'\1 duration="{value}"', metadata, count=1, flags=re.IGNORECASE)

    @staticmethod
    def _hms_to_seconds(value: str) -> int:
        """Parse HH:MM:SS, MM:SS, or numeric seconds into an integer."""

        parts = value.strip().split(":")
        try:
            if len(parts) == 3:
                hours, minutes, seconds = (int(part) for part in parts)
            elif len(parts) == 2:
                hours = 0
                minutes, seconds = (int(part) for part in parts)
            else:
                return max(0, int(float(value)))
        except ValueError:
            return 0
        return max(0, hours * 3600 + minutes * 60 + seconds)

    async def proxy_audio(self, request: web.Request) -> web.StreamResponse:
        """Proxy on-demand audio with optional second-to-byte range shifting."""

        url = request.query.get("url", "")
        if not url.startswith(("http://", "https://")):
            return web.Response(status=400, text="missing or invalid url")
        range_header = request.headers.get("Range", "")
        start_seconds = self._parse_nonnegative_int(request.query.get("start", "0"))
        base_offset = self._byte_offset_for_seconds(url, start_seconds)
        upstream_range = self._shift_range_header(range_header, base_offset)
        if not upstream_range and base_offset > 0:
            upstream_range = f"bytes={base_offset}-"
        LOG.info(
            "audio proxy remote=%s range=%s upstream_range=%s start=%s base_offset=%s url=%s",
            request.remote,
            range_header,
            upstream_range,
            start_seconds,
            base_offset,
            url,
        )
        timeout = ClientTimeout(total=None, sock_connect=10, sock_read=30)
        upstream_headers = {"User-Agent": "Linkplay/4.8"}
        if upstream_range:
            upstream_headers["Range"] = upstream_range
        session = ClientSession(timeout=timeout, headers=upstream_headers)
        upstream = None
        try:
            upstream = await session.get(url, allow_redirects=True)
            if upstream.status >= 400:
                text = await upstream.text(errors="ignore")
                LOG.warning("audio proxy upstream error status=%s url=%s body=%s", upstream.status, url, self._preview(text))
                upstream.release()
                await session.close()
                return web.Response(status=502, text=f"upstream status {upstream.status}")
            content_type = upstream.headers.get("Content-Type", "audio/mpeg")
            headers = {
                "Content-Type": content_type,
                "Cache-Control": "no-store",
                "Accept-Ranges": upstream.headers.get("Accept-Ranges", "bytes"),
            }
            for header in ("Content-Length", "Content-Range"):
                if upstream.headers.get(header):
                    headers[header] = upstream.headers[header]
            response = web.StreamResponse(status=206 if upstream.status == 206 else 200, headers=headers)
            await response.prepare(request)
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                await response.write(chunk)
            await response.write_eof()
            return response
        except (ClientError, TimeoutError, OSError, ConnectionResetError) as exc:
            LOG.warning("audio proxy failed url=%s error=%s", self._redact_sensitive_text(url), exc)
            return web.Response(status=502, text=f"proxy failed: {exc}")
        finally:
            if upstream is not None:
                upstream.release()
            await session.close()

    async def deezer_root(self, request: web.Request) -> web.Response:
        """Return the synthetic Deezer experiment root payload."""

        payload = self._deezer_payload("root")
        self._log_http_detail(request, "", 200, json.dumps(payload, separators=(",", ":")))
        return web.json_response(payload)

    async def deezer_api(self, request: web.Request) -> web.Response:
        """Return a synthetic Deezer experiment section payload."""

        section = request.match_info.get("tail", "")
        payload = self._deezer_payload(section)
        self._log_http_detail(request, "", 200, json.dumps(payload, separators=(",", ":")))
        return web.json_response(payload)

    def _deezer_payload(self, section: str) -> dict[str, object]:
        """Build synthetic, DRM-free data for service-shape experiments."""

        base = f"http://{self.cfg.resolved_advertise_ip}:{self.cfg.http_api_port}/deezer"
        art = f"http://{self.cfg.resolved_advertise_ip}:{self.cfg.http_api_port}/icons/deezer.svg"
        test_url = self.cfg.podcast_presets[0].url if self.cfg.podcast_presets else "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"
        test_track = {
            "id": "fake-deezer-track-1",
            "type": "track",
            "title": "DRM-free test stream",
            "name": "DRM-free test stream",
            "readable": True,
            "explicit_lyrics": False,
            "duration": 0,
            "rank": 1,
            "link": test_url,
            "url": test_url,
            "stream": test_url,
            "streamUrl": test_url,
            "playUrl": test_url,
            "trackUrl": test_url,
            "searchUrl": test_url,
            "source": "Deezer2",
            "sourceName": "Deezer2",
            "picture": art,
            "picture_small": art,
            "picture_medium": art,
            "picture_big": art,
            "album": {"id": "fake-deezer-album", "title": "Local test content", "cover": art, "cover_big": art},
            "artist": {"id": "fake-deezer-artist", "name": "Fake Linkplay"},
        }
        collection = {
            "id": "fake-deezer-collection",
            "type": "playlist",
            "title": "Local DRM-free tests",
            "name": "Local DRM-free tests",
            "readable": True,
            "link": f"{base}/collection",
            "url": f"{base}/collection",
            "trackUrl": f"{base}/collection",
            "searchUrl": f"{base}/collection",
            "source": "Deezer2",
            "sourceName": "Deezer2",
            "picture": art,
            "picture_small": art,
            "picture_medium": art,
            "picture_big": art,
            "tracks": {"data": [test_track]},
            "children": [test_track],
        }
        if section in {"collection", "search"}:
            data = [test_track]
            title = "Collection"
        else:
            data = [collection, test_track]
            title = "Deezer"
        return {
            "status": "success",
            "msg": "success",
            "source": "Deezer2",
            "accountSource": "Deezer2",
            "title": title,
            "total": len(data),
            "data": data,
            "results": data,
            "items": data,
            "children": data,
            "body": [{"type": "children", "text": title, "children": data}],
            "collection": f"{base}/collection",
            "search": f"{base}/search",
            "next": "",
        }

    @staticmethod
    def _parse_nonnegative_int(value: str) -> int:
        """Parse a non-negative integer from loose string input."""

        try:
            return max(0, int(float(value)))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _byte_offset_for_seconds(cls, url: str, seconds: int) -> int:
        """Estimate byte offset for constant-bitrate HTTP audio."""

        if seconds <= 0:
            return 0
        bitrate_bps = cls._bitrate_bps_from_url_or_headers(url, {})
        if bitrate_bps <= 0:
            return 0
        return max(0, int(seconds * bitrate_bps / 8))

    @staticmethod
    def _shift_range_header(range_header: str, base_offset: int) -> str:
        """Add a base byte offset to an HTTP Range header."""

        if base_offset <= 0 or not range_header:
            return range_header
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header.strip())
        if not match:
            return range_header
        start = base_offset + int(match.group(1))
        end_raw = match.group(2)
        if end_raw:
            return f"bytes={start}-{base_offset + int(end_raw)}"
        return f"bytes={start}-"

    async def description(self, request: web.Request) -> web.Response:
        """Serve the UPnP device description XML."""

        LOG.info("HTTP description remote=%s host=%s path=%s ua=%s", request.remote, request.host, request.path_qs, request.headers.get("User-Agent", ""))
        return web.Response(text=description_xml(self.cfg), content_type="application/xml")

    async def avtransport_xml(self, request: web.Request) -> web.Response:
        """Serve the AVTransport SCPD XML."""

        LOG.info("HTTP SCPD remote=%s host=%s path=%s", request.remote, request.host, request.path_qs)
        return web.Response(text=scpd_xml("AVTransport"), content_type="application/xml")

    async def rendering_xml(self, request: web.Request) -> web.Response:
        """Serve the RenderingControl SCPD XML."""

        LOG.info("HTTP SCPD remote=%s host=%s path=%s", request.remote, request.host, request.path_qs)
        return web.Response(text=scpd_xml("RenderingControl"), content_type="application/xml")

    async def connmanager_xml(self, request: web.Request) -> web.Response:
        """Serve the ConnectionManager SCPD XML."""

        LOG.info("HTTP SCPD remote=%s host=%s path=%s", request.remote, request.host, request.path_qs)
        return web.Response(text=scpd_xml("ConnectionManager"), content_type="application/xml")

    async def playqueue_xml(self, request: web.Request) -> web.Response:
        """Serve the PlayQueue SCPD XML."""

        LOG.info("HTTP SCPD remote=%s host=%s path=%s", request.remote, request.host, request.path_qs)
        return web.Response(text=scpd_xml("PlayQueue"), content_type="application/xml")

    async def icon(self, request: web.Request) -> web.Response:
        """Serve a generated placeholder SVG icon for source experiments."""

        name = request.match_info.get("name", "source")
        label = "ARD" if "ard" in name.lower() else "SRC"
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">'
            '<rect width="256" height="256" rx="32" fill="#1d4ed8"/>'
            '<circle cx="190" cy="66" r="34" fill="#f97316"/>'
            f'<text x="128" y="148" text-anchor="middle" font-family="Arial, sans-serif" font-size="56" font-weight="700" fill="#ffffff">{escape(label)}</text>'
            '</svg>'
        )
        self._log_http_detail(request, "", 200, svg)
        return web.Response(text=svg, content_type="image/svg+xml")

    async def soap(self, request: web.Request) -> web.Response:
        """Handle UPnP SOAP actions and return small XML response envelopes."""

        action = request.headers.get("SOAPACTION", "OK").strip('"').rsplit("#", 1)[-1]
        body = await request.text()
        LOG.info(
            "SOAP remote=%s host=%s path=%s service=%s action=%s ua=%s",
            request.remote,
            request.host,
            request.path_qs,
            request.match_info.get("service"),
            action,
            request.headers.get("User-Agent", ""),
        )
        await self._execute_soap_side_effect(action, body)
        result = None
        if action == "GetControlDeviceInfo":
            result = get_control_device_info_response_fields(self.cfg, self.cfg.resolved_advertise_ip, self.state)
        elif action == "StreamServicesCapability":
            capability = json.dumps(self._stream_services_capability(), separators=(",", ":"))
            result = f"<StreamCapability>{escape(capability)}</StreamCapability>"
        elif request.match_info.get("service", "").lower() in {"rendercontrol1", "renderingcontrol"}:
            result = self._rendering_response_fields(action)
        elif request.match_info.get("service", "").lower() in {"rendertransport1", "avtransport"}:
            if action in {"GetInfoEx", "GetPositionInfo", "GetMediaInfo", "GetTransportInfo"}:
                await self.sync_mpv_state()
            result = avtransport_response_fields(action, self.state)
        elif request.match_info.get("service", "").lower() in {"playqueue1", "playqueue"}:
            result = self._playqueue_response_fields(action, body)
        response_text = soap_response(action or "OK", request.match_info.get("service", ""), result)
        self._log_http_detail(request, body, 200, response_text)
        return web.Response(text=response_text, content_type="text/xml")

    def _playqueue_response_fields(self, action: str, body: str = "") -> str | None:
        """Return PlayQueue SOAP response fields for supported actions."""

        if action in {"BrowseQueue", "BrowseQueueEx"}:
            # WiiM Home polls BrowseQueue heavily and uses this queue context to
            # decide whether the now-playing controls are actionable. Returning
            # an empty queue after CreateQueue makes play/pause appear broken.
            queue_context = self.queue_context or self._build_queue_context()
            if queue_context:
                return f"<QueueContext>{escape(queue_context)}</QueueContext>"
            return playqueue_response_fields(action)
        if action == "UserLogin":
            account_source = self._xml_text(body, "AccountSource")
            if account_source == "Plex":
                user_info = self._current_plex_user_info() or self._plex_not_login_user_info()
                result = json.dumps(user_info, separators=(",", ":"))
                return f"<Result>{escape(result)}</Result>"
            if account_source == "Deezer2" and self.cfg.enable_stream_service_sentinels:
                self.logged_in_stream_services.add(account_source)
                result = json.dumps(self._stream_service_user_info(account_source, logged_in=True), separators=(",", ":"))
                return f"<Result>{escape(result)}</Result>"
            return playqueue_response_fields(action)
        if action == "GetUserInfo":
            account_source = self._xml_text(body, "AccountSource")
            if account_source in {"newTuneIn", "tunein"}:
                result = json.dumps(
                    {
                        "sourceVersion": "1.1",
                        "capabilityVersion": "1.0",
                        "userId": "anonymous",
                        "userName": "anonymous",
                        "showName": "TuneIn",
                        "expires_in": 0,
                        "accountSource": account_source,
                    },
                    separators=(",", ":"),
                )
                return f"<Result>{escape(result)}</Result>"
            if account_source == "Plex":
                # Plex browsing is performed by WiiM Home itself after this
                # login gate. The renderer can provide a token/profile, but it
                # cannot force the app to prefer Plex Relay over an unreachable
                # local PMS connection returned by plex.tv/resources.
                current_plex_user_info = self._current_plex_user_info()
                if current_plex_user_info:
                    user_info = current_plex_user_info
                elif self.cfg.plex_user_info_mode == "debug_login":
                    user_info = {
                        "source": "Plex",
                        "msg": "success",
                        "userId": "debug-user",
                        "userName": "debug-user",
                        "showName": "Debug Plex",
                        "subscription": "0",
                        "accessToken": "",
                        "refreshToken": "",
                        "uuid": self.cfg.upnp_uuid,
                        "sourceVersion": "1.4",
                        "capabilityVersion": "1.4",
                        "replayGain": True,
                        "redirectUrl": PLEX_CALLBACK_URL_ENCODED,
                    }
                else:
                    user_info = self._plex_not_login_user_info()
                result = json.dumps(user_info, separators=(",", ":"))
                return f"<Result>{escape(result)}</Result>"
            if self.cfg.enable_stream_service_sentinels and account_source in {"Deezer2", "SoundCloud"}:
                user_info = self._stream_service_user_info(account_source, logged_in=account_source in self.logged_in_stream_services)
                result = json.dumps(user_info, separators=(",", ":"))
                return f"<Result>{escape(result)}</Result>"
            result = json.dumps(
                {"id": account_source or "", "userId": "", "showName": "", "anonUserId": "", "version": "1.0"},
                separators=(",", ":"),
            )
            return f"<Result>{escape(result)}</Result>"
        if action == "GetKeyMapping":
            return "<QueueContext>&lt;KeyMapping&gt;&lt;/KeyMapping&gt;</QueueContext>"
        if action == "GetQueueIndex":
            return "\n".join(
                [
                    f"<CurrentIndex>{self.state.plicurr}</CurrentIndex>",
                    f"<PreloadingIndex>{self.state.plicurr}</PreloadingIndex>",
                    "<CurrentPage>1</CurrentPage>",
                    f"<TrackNums>{self.state.plicount}</TrackNums>",
                ]
            )
        if action == "GetQueueLoopMode":
            return f"<LoopMode>{escape(str(self.state.loop))}</LoopMode>"
        if action in {"SetQueueLoopMode", "SetQueuePolicy", "ReplaceQueue"}:
            return ""
        return playqueue_response_fields(action)

    def _stream_service_user_info(self, account_source: str, *, logged_in: bool) -> dict[str, object]:
        """Build synthetic user-info payloads for service experiments."""

        if logged_in:
            return {
                "source": account_source,
                "accountSource": account_source,
                "msg": "success",
                "userId": f"local-{account_source.lower()}-user",
                "userName": f"Local {account_source}",
                "showName": f"Local {account_source}",
                "email": "local@example.invalid",
                "anonUserId": "",
                "subscription": "premium",
                "accessToken": f"local-{account_source.lower()}-access-token",
                "refreshToken": f"local-{account_source.lower()}-refresh-token",
                "expires": 4102444800,
                "country": "US",
                "baseUrl": f"http://{self.cfg.resolved_advertise_ip}:{self.cfg.http_api_port}/",
                "collection": f"http://{self.cfg.resolved_advertise_ip}:{self.cfg.http_api_port}/deezer/collection",
                "search": f"http://{self.cfg.resolved_advertise_ip}:{self.cfg.http_api_port}/deezer/search",
                "sourceVersion": "1.4",
                "capabilityVersion": "1.4",
                "replayGain": True,
                "redirectUrl": "",
            }
        return {
            "source": account_source,
            "accountSource": account_source,
            "msg": "not login",
            "userId": "",
            "userName": "",
            "showName": account_source,
            "anonUserId": "",
            "subscription": "0",
            "sourceVersion": "1.4",
            "capabilityVersion": "1.4",
            "replayGain": True,
            "redirectUrl": "",
        }

    def _plex_not_login_user_info(self) -> dict[str, object]:
        """Return the Plex user-info shape for a logged-out account."""

        return {
            "source": "Plex",
            "accountSource": "Plex",
            "msg": "NOT_LOGIN",
            "userId": "",
            "userName": "",
            "showName": "",
            "subscription": "0",
            "accessToken": "",
            "refreshToken": "",
            "uuid": self.cfg.upnp_uuid,
            "sourceVersion": "1.4",
            "capabilityVersion": "1.4",
            "replayGain": True,
            "redirectUrl": PLEX_CALLBACK_URL_ENCODED,
        }

    def _plex_logged_in_user_info(self, token: str, pin_payload: dict[str, object]) -> dict[str, object]:
        """Build a Plex logged-in user-info payload from PIN auth data."""

        user_id = str(pin_payload.get("userID") or pin_payload.get("userId") or pin_payload.get("id") or "plex-user")
        username = str(pin_payload.get("username") or pin_payload.get("login") or pin_payload.get("title") or "Plex User")
        subscription = "1" if self._plex_payload_has_pass(pin_payload) else str(pin_payload.get("subscription") or "0")
        return {
            "source": "Plex",
            "accountSource": "Plex",
            "msg": "success",
            "userId": user_id,
            "userName": username,
            "showName": username,
            "email": str(pin_payload.get("email") or ""),
            "accessToken": token,
            "refreshToken": "",
            "expires": 4102444800,
            "subscription": subscription,
            "subscriptionDescription": str(pin_payload.get("subscriptionDescription") or ""),
            "country": str(pin_payload.get("country") or "US"),
            "uuid": self.cfg.upnp_uuid,
            "clientIdentifier": self.plex_client_id,
            "sourceVersion": "1.4",
            "capabilityVersion": "1.4",
            "replayGain": True,
            "redirectUrl": PLEX_CALLBACK_URL_ENCODED,
        }

    def _load_or_create_plex_client_id(self) -> str:
        """Return a persisted Plex client identifier for local experiments."""

        path = Path(self.cfg.state_dir) / "plex_client_id"
        try:
            if path.exists():
                existing = re.sub(r"[^A-Za-z0-9_.:-]", "", path.read_text().strip())
                if existing:
                    return existing
            value = f"fake-linkplay-{self.cfg.upnp_uuid or self.cfg.uuid or uuid4()}"
            self._persist_text_secure(path, value)
            return value
        except OSError as exc:
            LOG.warning("could not load Plex client id: %s", exc)
            return f"fake-linkplay-{self.cfg.upnp_uuid or uuid4()}"

    def _load_plex_user_info(self) -> dict[str, object] | None:
        """Load persisted Plex user information if it contains a token."""

        path = Path(self.cfg.state_dir) / "plex_user_info.json"
        try:
            if not path.exists():
                return None
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("could not load Plex user info: %s", exc)
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("msg") == "success" and payload.get("userId") and payload.get("accessToken"):
            return payload
        return None

    def _current_plex_user_info(self) -> dict[str, object] | None:
        """Return cached Plex user info, loading it lazily if needed."""

        if self.plex_user_info is None:
            self.plex_user_info = self._load_plex_user_info()
        return self.plex_user_info

    def _persist_plex_user_info(self, user_info: dict[str, object]) -> None:
        """Persist Plex user information with owner-readable permissions."""

        path = Path(self.cfg.state_dir) / "plex_user_info.json"
        try:
            self._persist_text_secure(path, json.dumps(user_info, indent=2, sort_keys=True) + "\n")
        except OSError as exc:
            LOG.warning("could not persist Plex user info: %s", exc)

    @staticmethod
    def _persist_text_secure(path: Path, value: str) -> None:
        """Write text to a local state file and chmod it to 0600."""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        path.chmod(0o600)

    def _rendering_response_fields(self, action: str) -> str | None:
        """Return RenderingControl SOAP response fields."""

        if action == "GetVolume":
            return f"<CurrentVolume>{escape(str(self.state.volume))}</CurrentVolume>"
        if action == "GetMute":
            return f"<CurrentMute>{'1' if self.state.mute else '0'}</CurrentMute>"
        if action == "GetChannel":
            return "<CurrentChannel>0</CurrentChannel>"
        if action == "ListPresets":
            return "<CurrentPresetNameList>FactoryDefaults</CurrentPresetNameList>"
        if action in {"SelectPreset", "SetChannel"}:
            return ""
        return None

    def _build_queue_context(self) -> str:
        """Build the Linkplay-style QueueContext XML for the current item."""

        url = self.queue_url or self.state.current_url
        if not url:
            return ""
        title = self.queue_title or self.state.title or "Network stream"
        source = self.queue_source or self.state.track_source or self._xml_text(self.queue_context, "SourceName") or self._xml_text(self.queue_context, "Source") or "radio"
        if "radioparadise.com" in url:
            source = "RadioParadise"
        search_url = self.queue_search_url or self._xml_text(self.queue_context, "SearchUrl") or url
        pic_url = self.queue_pic_url or self._xml_text(self.queue_context, "PicUrl")
        quality = self.queue_quality or self._xml_text(self.queue_context, "Quality") or "0"
        stream_type = self.queue_stream_type or self._xml_text(self.queue_context, "StreamType") or "station"
        content_type = self.queue_content_type or self._xml_text(self.queue_context, "ContentType") or "audio"
        metadata = self.state.track_metadata or self._didl_metadata(url, title, source, pic_url, self.queue_is_live)
        track_url = self.queue_resolved_url or url
        metadata = self._metadata_with_url(metadata, track_url)
        return "\n".join(
            [
                '<?xml version="1.0" ?>',
                "<PlayList>",
                f"  <QueueName>{escape(self.queue_name)}</QueueName>",
                f"  <ListName>{escape(title)}</ListName>",
                "  <ListInfo>",
                "    <Radio>1</Radio>",
                f"    <SourceName>{escape(source)}</SourceName>",
                f"    <TrackSource>{escape(source)}</TrackSource>",
                f"    <StartPlayIndex>{self.state.plicurr}</StartPlayIndex>",
                f"    <LastPlayIndex>{self.state.plicurr}</LastPlayIndex>",
                f"    <TrackNumber>{self.state.plicount}</TrackNumber>",
                f"    <SearchUrl>{escape(search_url)}</SearchUrl>",
                f"    <PicUrl>{escape(pic_url)}</PicUrl>",
                f"    <Quality>{escape(quality)}</Quality>",
                "  </ListInfo>",
                "  <Tracks>",
                "    <Track1>",
                f"      <TrackNumber>{self.state.plicurr}</TrackNumber>",
                f"      <Title>{escape(title)}</Title>",
                f"      <Artist>{escape(self.state.artist)}</Artist>",
                f"      <Album>{escape(self.state.album)}</Album>",
                f"      <Source>{escape(source)}</Source>",
                f"      <SourceName>{escape(source)}</SourceName>",
                f"      <TrackSource>{escape(source)}</TrackSource>",
                f"      <URL>{escape(track_url)}</URL>",
                f"      <SearchUrl>{escape(search_url)}</SearchUrl>",
                f"      <PicUrl>{escape(pic_url)}</PicUrl>",
                f"      <Metadata>{escape(metadata)}</Metadata>",
                "      <Radio>1</Radio>",
                f"      <StreamType>{escape(stream_type)}</StreamType>",
                f"      <ContentType>{escape(content_type)}</ContentType>",
                "    </Track1>",
                "  </Tracks>",
                "</PlayList>",
            ]
        )

    async def _execute_soap_side_effect(self, action: str, body: str) -> None:
        """Apply state or mpv side effects implied by a SOAP action."""

        if action == "UserLogin":
            account_source = self._xml_text(body, "AccountSource")
            if account_source == "Plex":
                pin_id = self._xml_text(body, "Code")
                if pin_id:
                    user_info = await self._plex_user_info_from_pin(pin_id)
                    if user_info:
                        self.plex_user_info = user_info
                        self._persist_plex_user_info(user_info)
                        LOG.info("SOAP UserLogin Plex stored userId=%s clientIdentifier=%s", user_info.get("userId"), self.plex_client_id)
                    else:
                        LOG.warning("SOAP UserLogin Plex did not obtain a token for pin id %s", pin_id)
                else:
                    LOG.warning("SOAP UserLogin Plex missing Code pin id")
            return
        if action == "SetVolume":
            raw = self._xml_text(body, "DesiredVolume")
            if raw:
                value = max(0, min(100, int(float(raw))))
                self.state.volume = value
                await self.mpv.volume(value)
                LOG.info("SOAP SetVolume applied value=%s", value)
                self._schedule_event_notify(("rendercontrol1", "RenderingControl"))
            return
        if action == "SetMute":
            raw = self._xml_text(body, "DesiredMute")
            if raw:
                value = raw.strip() in {"1", "true", "True"}
                self.state.mute = value
                await self.mpv.mute(value)
                LOG.info("SOAP SetMute applied value=%s", value)
                self._schedule_event_notify(("rendercontrol1", "RenderingControl"))
            return
        if action == "SetChannel":
            LOG.info("SOAP SetChannel accepted as no-op")
            self._schedule_event_notify(("rendercontrol1", "RenderingControl"))
            return
        if action == "SetAVTransportURI":
            url = self._xml_text(body, "CurrentURI")
            if url:
                self.queue_url = url
                self.state.current_url = url
                self.state.title = url.rsplit("/", 1)[-1] or "Network stream"
                self.state.track_metadata = self._xml_text(body, "CurrentURIMetaData") or self._didl_metadata(url, self.state.title, "DLNA")
                self.state.album_art_uri = self._metadata_art_url(self.state.track_metadata)
                self.state.track_metadata = self._metadata_with_art(self.state.track_metadata, self.state.album_art_uri)
                self.state.play_medium = "STATION-NETWORK" if "radioparadise.com" in url else "THIRD-DLNA"
                self.state.track_source = "RadioParadise" if "radioparadise.com" in url else "DLNA"
                LOG.info("SOAP SetAVTransportURI stored url=%s", url)
                self._schedule_event_notify(("rendertransport1", "AVTransport"))
            return
        if action == "Play":
            self._pending_seek_generation += 1
            self._pending_seek_target = None
            url = self.queue_url or self.state.current_url
            if url:
                await self._play_url(url)
                LOG.info("SOAP Play loaded url=%s", url)
            else:
                await self.mpv.resume()
                self.state.status = "play"
            self._schedule_event_notify()
            return
        if action == "Pause":
            self._pending_seek_generation += 1
            self._pending_seek_target = None
            self.state.mark_pause()
            await self.mpv.pause()
            self._schedule_event_notify()
            return
        if action == "Stop":
            self._play_generation += 1
            self._pending_seek_generation += 1
            self._pending_seek_target = None
            self.state.mark_stop()
            await self.mpv.stop()
            self._schedule_event_notify()
            return
        if action == "Next":
            LOG.info("SOAP Next accepted as no-op plicurr=%s plicount=%s", self.state.plicurr, self.state.plicount)
            self._schedule_event_notify(("rendertransport1", "AVTransport"))
            return
        if action == "Previous":
            self.state.curpos = 0
            if self.state.current_url and self.state.status in {"play", "pause"}:
                await self.mpv.seek(0)
            LOG.info("SOAP Previous restarted current track")
            self._schedule_event_notify(("rendertransport1", "AVTransport"))
            return
        if action == "SetPlayMode":
            mode = self._xml_text(body, "NewPlayMode") or self._xml_text(body, "PlayMode") or "NORMAL"
            self.state.loop = self._loop_mode_value(mode)
            LOG.info("SOAP SetPlayMode mode=%s loop=%s", mode, self.state.loop)
            self._schedule_event_notify(("rendertransport1", "AVTransport"))
            return
        if action in {"Seek", "SeekForward", "SeekBackward"}:
            await self.sync_mpv_state()
            final_status = self.state.status if self.state.status in {"play", "pause"} else "play"
            previous_pos = self.state.curpos
            if action == "SeekForward":
                seconds = self.state.curpos + 30
            elif action == "SeekBackward":
                seconds = self.state.curpos - 30
            else:
                unit = self._xml_text(body, "Unit")
                target = self._xml_text(body, "Target")
                if unit and unit != "REL_TIME":
                    LOG.info("SOAP Seek unsupported unit=%s target=%s; treating as no-op", unit, target)
                    self._schedule_event_notify(("rendertransport1", "AVTransport"))
                    return
                seconds = self._hms_to_seconds(target)
            if not self.queue_is_live and self.state.totlen > 0:
                seconds = min(seconds, max(0, self.state.totlen - 1))
            seconds = max(0, int(seconds))
            self.state.curpos = seconds
            self.state.mark_transitioning()
            self._pending_seek_target = seconds
            self._pending_seek_final_status = final_status
            self._pending_seek_generation += 1
            seek_generation = self._pending_seek_generation
            self._schedule_event_notify(("rendertransport1", "AVTransport"))
            if self._should_reload_for_large_seek(seconds, previous_pos):
                direct_url = self.queue_resolved_url or self.state.resolved_url
                seek_url = self._seek_audio_proxy_url(direct_url, seconds)
                self._playback_offset_sec = seconds
                LOG.info(
                    "SOAP %s reloading range proxy position=%s previous=%s direct=%s proxy=%s",
                    action,
                    seconds,
                    previous_pos,
                    direct_url,
                    seek_url,
                )
                await self.mpv.loadfile(seek_url, self._mpv_seek_reload_options())
            else:
                await self.mpv.seek(seconds)
            LOG.info("SOAP %s applied position=%s duration=%s", action, seconds, self.state.totlen)
            asyncio.create_task(self._finish_seek_after_buffering(seek_generation, seconds, previous_pos))
            return
        if action in {"CreateQueue", "ReplaceQueue"}:
            # Built-in services such as TuneIn and Radio Paradise usually pass
            # a Linkplay QueueContext. Preserve source/search/art metadata so
            # later BrowseQueue, GetInfoEx, artwork, and transport state agree.
            queue_context = self._xml_text(body, "QueueContext")
            url = self._first_stream_url(queue_context or body)
            title = self._xml_text(queue_context, "ListName") or self._xml_text(queue_context, "ShowName") or self._xml_text(queue_context, "Id")
            queue_name = title or self._xml_text(body, "QueueName") or "CurrentQueue"
            self.queue_name = queue_name
            if url:
                self.queue_url = url
                self.queue_resolved_url = ""
                self.queue_title = title
                self.state.current_url = url
                self.state.title = title or (url.rsplit("/", 1)[-1] or "Network stream")
                source_name = self._xml_text(queue_context, "SourceName") or self._xml_text(queue_context, "Source") or "radio"
                self.queue_source = "RadioParadise" if "radioparadise.com" in url else source_name
                self.queue_search_url = self._xml_text(queue_context, "SearchUrl") or url
                self.queue_pic_url = self._best_art_url(self._xml_text(queue_context, "PicUrl"))
                self.queue_quality = self._xml_text(queue_context, "Quality")
                self.queue_stream_type = self._xml_text(queue_context, "StreamType") or "station"
                self.queue_content_type = self._xml_text(queue_context, "ContentType") or "audio"
                self.queue_is_live = not self._queue_item_is_episode(url, self.queue_stream_type, self.queue_content_type, queue_context)
                self.queue_duration_sec = self._metadata_duration_seconds(self._xml_text(queue_context, "Metadata"))
                if "api.radiotime.com/" in url:
                    resolved = await self._resolve_playback_url(url)
                    if resolved and "api.radiotime.com/" not in resolved:
                        self.queue_resolved_url = resolved
                        self.state.resolved_url = resolved
                        if self._last_resolve_duration_sec > 0:
                            self.queue_duration_sec = self._last_resolve_duration_sec
                if not self.queue_is_live and self.queue_duration_sec <= 0 and self.queue_resolved_url:
                    self.queue_duration_sec = await self._probe_audio_duration_seconds(self.queue_resolved_url)
                if not self.queue_is_live and self.queue_duration_sec <= 0:
                    self.queue_duration_sec = 3600
                self.state.totlen = self.queue_duration_sec if not self.queue_is_live else 0
                original_metadata = self._xml_text(queue_context, "Metadata")
                self.state.album_art_uri = self.queue_pic_url or self._metadata_art_url(original_metadata)
                self.state.track_metadata = original_metadata or self._didl_metadata(
                    self.queue_resolved_url or url,
                    self.state.title,
                    source_name,
                    self.state.album_art_uri,
                    self.queue_is_live,
                )
                if not self.state.album_art_uri:
                    self.state.album_art_uri = self._metadata_art_url(self.state.track_metadata)
                self.state.track_metadata = self._metadata_with_art(self.state.track_metadata, self.state.album_art_uri, self.queue_is_live)
                if self.queue_resolved_url:
                    self.state.track_metadata = self._metadata_with_url(self.state.track_metadata, self.queue_resolved_url)
                if not self.queue_is_live and self.queue_duration_sec > 0:
                    self.state.track_metadata = self._metadata_with_duration(self.state.track_metadata, self.queue_duration_sec)
                self.state.play_medium = "STATION-NETWORK" if "radioparadise.com" in url else self._xml_text(queue_context, "PlayMedium") or "RADIO-NETWORK"
                self.state.track_source = self.queue_source
                self.state.plicount = 1
                self.state.plicurr = 1
                self.queue_context = self._build_queue_context()
                LOG.info("SOAP CreateQueue stored title=%s url=%s resolved=%s live=%s", self.state.title, url, self.queue_resolved_url, self.queue_is_live)
                self._schedule_event_notify(("PlayQueue1", "PlayQueue"))
            elif queue_context:
                self.queue_context = queue_context
                self._schedule_event_notify(("PlayQueue1", "PlayQueue"))
            else:
                LOG.info("SOAP %s did not contain a stream URL", action)
            return
        if action == "PlayQueueWithIndex":
            index = self._xml_text(body, "Index")
            if index:
                try:
                    self.state.plicurr = max(1, int(index))
                except ValueError:
                    LOG.info("SOAP PlayQueueWithIndex ignored non-integer index=%s", index)
            if self.queue_url:
                if self.queue_title:
                    self.state.title = self.queue_title
                await self._play_url(self.queue_url)
                LOG.info("SOAP PlayQueueWithIndex loaded title=%s url=%s", self.state.title, self.queue_url)
                self._schedule_event_notify()
            return
        if action == "SetQueueLoopMode":
            mode = self._xml_text(body, "LoopMode") or self._xml_text(body, "QueueLoopMode") or "0"
            self.state.loop = self._loop_mode_value(mode)
            LOG.info("SOAP SetQueueLoopMode mode=%s loop=%s", mode, self.state.loop)
            self._schedule_event_notify(("PlayQueue1", "PlayQueue", "rendertransport1", "AVTransport"))
            return
        if action in {"SetQueuePolicy", "AppendQueue", "AppendTracksInQueue", "AppendTracksInQueueEx", "RemoveTracksInQueue"}:
            LOG.info("SOAP %s accepted as no-op", action)
            self._schedule_event_notify(("PlayQueue1", "PlayQueue"))
            return

    async def _plex_user_info_from_pin(self, pin_id: str) -> dict[str, object] | None:
        """Poll Plex PIN auth and convert a successful result to user info."""

        url = f"https://plex.tv/api/v2/pins/{quote(pin_id.strip())}"
        headers = self._plex_headers()
        timeout = ClientTimeout(total=20, sock_connect=8, sock_read=8)
        try:
            async with ClientSession(timeout=timeout, headers=headers) as session:
                for attempt in range(4):
                    async with session.get(url) as response:
                        text = await response.text()
                        if response.status >= 400:
                            LOG.warning("Plex PIN poll failed status=%s attempt=%s body=%s", response.status, attempt + 1, self._preview(text))
                            return None
                        try:
                            payload = json.loads(text)
                        except json.JSONDecodeError:
                            LOG.warning("Plex PIN poll returned non-JSON body=%s", self._preview(text))
                            return None
                    if isinstance(payload, dict):
                        token = str(payload.get("authToken") or payload.get("auth_token") or "")
                        if token:
                            profile = await self._plex_user_profile(session, token)
                            if profile:
                                payload = {**payload, **profile}
                            return self._plex_logged_in_user_info(token, payload)
                    await asyncio.sleep(1)
        except (ClientError, TimeoutError, OSError) as exc:
            LOG.warning("Plex PIN poll error pin=%s error=%s", pin_id, exc)
        return None

    async def _plex_user_profile(self, session: ClientSession, token: str) -> dict[str, object]:
        """Fetch optional Plex profile details for a valid token."""

        headers = {"X-Plex-Token": token}
        try:
            async with session.get("https://plex.tv/api/v2/user", headers=headers) as response:
                text = await response.text()
                if response.status >= 400:
                    LOG.warning("Plex user profile failed status=%s body=%s", response.status, self._preview(text))
                    return {}
                payload = json.loads(text)
                if isinstance(payload, dict):
                    return payload
        except (ClientError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            LOG.warning("Plex user profile error: %s", exc)
        return {}

    @staticmethod
    def _plex_payload_has_pass(payload: dict[str, object]) -> bool:
        """Return whether Plex profile data indicates Plex Pass."""

        roles = payload.get("roles")
        if isinstance(roles, list) and any(str(role).lower() == "plexpass" for role in roles):
            return True
        description = str(payload.get("subscriptionDescription") or "").lower()
        return "plex pass" in description

    def _plex_headers(self) -> dict[str, str]:
        """Return headers used for Plex PIN/profile requests."""

        return {
            "Accept": "application/json",
            "X-Plex-Client-Identifier": self.plex_client_id,
            "X-Plex-Product": "Fake Linkplay Renderer",
            "X-Plex-Version": "0.1.0",
            "X-Plex-Platform": "Linux",
            "X-Plex-Device-Name": self.cfg.friendly_name,
        }

    @staticmethod
    def _loop_mode_value(mode: str) -> str:
        """Normalize app loop mode names to renderer loop codes."""

        normalized = mode.strip().upper()
        if normalized in {"0", "NORMAL", "NONE"}:
            return "0"
        if normalized in {"1", "REPEAT_ONE", "REPEAT_TRACK", "SINGLE"}:
            return "1"
        if normalized in {"2", "REPEAT_ALL", "REPEAT"}:
            return "2"
        if normalized in {"3", "SHUFFLE", "RANDOM"}:
            return "3"
        return mode.strip() or "0"

    @staticmethod
    def _queue_item_is_episode(url: str, stream_type: str, content_type: str, queue_context: str) -> bool:
        """Infer whether a queue item is on-demand audio rather than live."""

        haystack = " ".join([url, stream_type, content_type, queue_context]).lower()
        if re.search(r"/profiles/t\d+/media", haystack):
            return True
        return any(marker in haystack for marker in ("<streamtype>episode", "streamtype&gt;episode", "podcast", "on-demand", "ondemand"))

    @staticmethod
    def _metadata_duration_seconds(metadata: str) -> int:
        """Extract duration seconds from a DIDL ``res`` element."""

        if not metadata:
            return 0
        match = re.search(r"<res\b[^>]*\bduration=\"([^\"]+)\"", metadata, re.IGNORECASE)
        if not match:
            return 0
        return HttpApi._hms_to_seconds(match.group(1))

    async def _probe_audio_duration_seconds(self, url: str) -> int:
        """Best-effort duration probe using HTTP headers and range requests."""

        try:
            timeout = ClientTimeout(total=8)
            async with ClientSession(timeout=timeout, headers={"User-Agent": "Linkplay/4.8"}) as session:
                async with session.head(url, allow_redirects=True) as response:
                    duration = self._duration_from_headers(url, response.headers)
                    if duration > 0:
                        LOG.info("audio duration from HEAD url=%s duration=%s", url, duration)
                        return duration
                async with session.get(url, headers={"Range": "bytes=0-0"}, allow_redirects=True) as response:
                    duration = self._duration_from_headers(url, response.headers)
                    if duration > 0:
                        LOG.info("audio duration from range probe url=%s duration=%s", url, duration)
                        return duration
        except (ClientError, TimeoutError, OSError) as exc:
            LOG.info("could not probe audio duration url=%s error=%s", url, exc)
        return 0

    @classmethod
    def _duration_from_headers(cls, url: str, headers: object) -> int:
        """Estimate duration from explicit headers or byte length/bitrate."""

        get = getattr(headers, "get", None)
        if not callable(get):
            return 0
        for key in ("X-Duration", "Duration", "X-Content-Duration", "X-Playback-Duration"):
            duration = cls._duration_value_seconds(get(key))
            if duration > 0:
                return duration
        total_bytes = cls._content_length_from_headers(headers)
        bitrate_bps = cls._bitrate_bps_from_url_or_headers(url, headers)
        if total_bytes <= 0 or bitrate_bps <= 0:
            return 0
        return max(1, int(total_bytes * 8 / bitrate_bps))

    @classmethod
    def _content_length_from_headers(cls, headers: object) -> int:
        """Return total byte length from Content-Range or Content-Length."""

        get = getattr(headers, "get", None)
        if not callable(get):
            return 0
        content_range = str(get("Content-Range") or "")
        match = re.search(r"/(\d+)\s*$", content_range)
        if match:
            return int(match.group(1))
        content_length = str(get("Content-Length") or "")
        return int(content_length) if re.fullmatch(r"\d+", content_length) else 0

    @classmethod
    def _bitrate_bps_from_url_or_headers(cls, url: str, headers: object) -> int:
        """Infer bitrate from ICY headers or common URL filename patterns."""

        get = getattr(headers, "get", None)
        if callable(get):
            icy_br = str(get("icy-br") or get("Icy-Br") or "")
            if re.fullmatch(r"\d{2,4}", icy_br):
                return int(icy_br) * 1000
        lowered = unquote(url).lower()
        matches = re.findall(r"(?:mp3|aac|m4a|opus|ogg|audio|_|-)(\d{2,4})k(?:d|bps)?", lowered)
        for value in matches:
            bitrate_kbps = int(value)
            if 32 <= bitrate_kbps <= 512:
                return bitrate_kbps * 1000
        return 0

    @classmethod
    def _duration_value_seconds(cls, value: object) -> int:
        """Normalize one loose duration value into seconds."""

        if isinstance(value, (int, float)) and value > 0:
            return cls._normalize_duration_seconds(value)
        if not isinstance(value, str):
            return 0
        stripped = value.strip()
        if re.fullmatch(r"\d+(\.\d+)?", stripped):
            return cls._normalize_duration_seconds(float(stripped))
        if re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", stripped):
            return cls._hms_to_seconds(stripped)
        return 0

    @staticmethod
    def _normalize_duration_seconds(value: float) -> int:
        """Normalize seconds or millisecond-like duration values."""

        seconds = int(value)
        if seconds > 24 * 60 * 60:
            as_milliseconds = seconds // 1000
            if 0 < as_milliseconds <= 24 * 60 * 60:
                return as_milliseconds
        return seconds

    async def event_subscribe(self, request: web.Request) -> web.Response:
        """Register or renew a UPnP event callback subscription."""

        service = request.match_info.get("service", "")
        LOG.info(
            "SUBSCRIBE remote=%s host=%s path=%s service=%s callback=%s timeout=%s",
            request.remote,
            request.host,
            request.path_qs,
            service,
            request.headers.get("Callback", ""),
            request.headers.get("Timeout", ""),
        )
        sid = request.headers.get("SID") or f"uuid:{uuid4()}"
        callback = self._callback_url(request.headers.get("Callback", ""))
        if callback:
            self._drop_duplicate_subscribers(callback, service, keep_sid=sid)
            self.event_subscribers[sid] = EventSubscriber(sid=sid, callback_url=callback, service=service)
            self._schedule_event_notify((service,))
        return web.Response(
            status=200,
            body=b"",
            headers={
                "SID": sid,
                "TIMEOUT": "Second-1800",
                "SERVER": "Linux/2.6.21 UPnP/1.0 Linkplay/1.0",
                "Content-Length": "0",
            },
        )

    async def event_unsubscribe(self, request: web.Request) -> web.Response:
        """Remove a UPnP event callback subscription."""

        LOG.info("UNSUBSCRIBE remote=%s host=%s path=%s sid=%s", request.remote, request.host, request.path_qs, request.headers.get("SID", ""))
        sid = request.headers.get("SID", "")
        if sid:
            self.event_subscribers.pop(sid, None)
        return web.Response(status=200)

    @staticmethod
    def _callback_url(header: str) -> str:
        """Extract the URL from a UPnP Callback header."""

        match = re.search(r"<([^>]+)>", header)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _default_gateway_for_ip(ip: str) -> str:
        """Return a conventional .1 gateway for an IPv4 address string."""

        parts = ip.split(".")
        if len(parts) == 4 and all(part.isdigit() for part in parts):
            return ".".join(parts[:3] + ["1"])
        return "0.0.0.0"

    def _drop_duplicate_subscribers(self, callback_url: str, service: str, keep_sid: str | None = None) -> None:
        """Remove duplicate event subscriptions for the same callback/service."""

        service_key = service.lower()
        duplicate_sids = [
            sid
            for sid, subscriber in self.event_subscribers.items()
            if sid != keep_sid and subscriber.callback_url == callback_url and subscriber.service.lower() == service_key
        ]
        for sid in duplicate_sids:
            self.event_subscribers.pop(sid, None)
            LOG.info("dropped duplicate event subscriber service=%s sid=%s callback=%s", service, sid, callback_url)

    def _transport_state(self) -> str:
        """Map internal transport state to UPnP AVTransport state names."""

        return {"play": "PLAYING", "pause": "PAUSED_PLAYBACK", "stop": "STOPPED", "transition": "TRANSITIONING"}.get(self.state.status, "STOPPED")

    def _schedule_event_notify(self, services: tuple[str, ...] | None = None) -> None:
        """Schedule asynchronous UPnP NOTIFY calls for matching subscribers."""

        if not self.event_subscribers:
            return
        selected = {service.lower() for service in services} if services else set()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for subscriber in list(self.event_subscribers.values()):
            if selected and subscriber.service.lower() not in selected:
                continue
            loop.create_task(self._notify_event_subscriber(subscriber))

    async def _notify_event_subscriber(self, subscriber: EventSubscriber) -> None:
        """Send one UPnP event notification to a subscriber callback."""

        body = self._event_body(subscriber.service)
        seq = subscriber.seq
        subscriber.seq += 1
        headers = {
            "HOST": re.sub(r"^https?://", "", subscriber.callback_url).split("/", 1)[0],
            "CONTENT-TYPE": 'text/xml; charset="utf-8"',
            "NT": "upnp:event",
            "NTS": "upnp:propchange",
            "SID": subscriber.sid,
            "SEQ": str(seq),
        }
        try:
            timeout = ClientTimeout(total=1.5)
            async with ClientSession(timeout=timeout) as session:
                async with session.request("NOTIFY", subscriber.callback_url, data=body.encode("utf-8"), headers=headers) as response:
                    await response.read()
                    LOG.info("NOTIFY service=%s sid=%s callback=%s status=%s seq=%s", subscriber.service, subscriber.sid, subscriber.callback_url, response.status, seq)
                    if response.status in {404, 410, 412}:
                        self.event_subscribers.pop(subscriber.sid, None)
                        LOG.info("dropped rejected event subscriber service=%s sid=%s status=%s", subscriber.service, subscriber.sid, response.status)
        except (ClientError, TimeoutError, OSError) as exc:
            LOG.info("NOTIFY failed service=%s sid=%s callback=%s error=%s", subscriber.service, subscriber.sid, subscriber.callback_url, exc)

    def _event_body(self, service: str) -> str:
        """Build the XML propertyset body for a UPnP event service."""

        service_key = service.lower()
        device_name = self.cfg.linkplay_instance_name if self.cfg.wiim_home_profile else self.cfg.friendly_name
        if service_key in {"rendercontrol1", "renderingcontrol"}:
            last_change = (
                '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/RCS/">'
                '<InstanceID val="0">'
                f'<Volume channel="Master" val="{escape(str(self.state.volume))}"/>'
                f'<Mute channel="Master" val="{"1" if self.state.mute else "0"}"/>'
                "</InstanceID></Event>"
            )
            return self._event_propertyset(
                {
                    "LastChange": last_change,
                    "Volume": str(self.state.volume),
                    "Mute": "1" if self.state.mute else "0",
                    "DeviceName": device_name,
                    "GroupName": device_name,
                }
            )
        if service_key in {"playqueue1", "playqueue"}:
            queue_context = self.queue_context or self._build_queue_context()
            return self._event_propertyset(
                {
                    "QueueContext": queue_context,
                    "TrackSource": self.state.track_source,
                    "TrackURI": self.state.resolved_url or self.state.current_url,
                    "TrackTitle": self.state.title,
                    "DeviceName": device_name,
                    "GroupName": device_name,
                }
            )

        transport_state = self._transport_state()
        metadata = self.state.track_metadata if self.state.current_url else ""
        if self.state.current_url and not metadata:
            metadata = self._didl_metadata(self.state.current_url, self.state.title or "Network stream", self.state.track_source, self.state.album_art_uri)
        track_uri = self.state.resolved_url or self.state.current_url
        track_duration = self._seconds_to_hms(self.state.totlen)
        rel_time = self._seconds_to_hms(self.state.curpos)
        last_change = (
            '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/">'
            '<InstanceID val="0">'
            f'<TransportState val="{escape(transport_state)}"/>'
            '<TransportStatus val="OK"/>'
            '<CurrentTrack val="1"/>'
            f'<CurrentTrackDuration val="{escape(track_duration)}"/>'
            f'<CurrentTrackURI val="{escape(track_uri)}"/>'
            f'<CurrentTrackMetaData val="{escape(metadata, quote=True)}"/>'
            f'<RelativeTimePosition val="{escape(rel_time)}"/>'
            f'<A_ARG_TYPE_SeekTarget val="{escape(rel_time)}"/>'
            '<CurrentTransportActions val="Play,Stop,Pause,Seek,Next,Previous"/>'
            f'<PlaybackStorageMedium val="{escape(self.state.play_medium or "NETWORK")}"/>'
            "</InstanceID></Event>"
        )
        return self._event_propertyset(
            {
                "LastChange": last_change,
                "TransportState": transport_state,
                "CurrentTransportState": transport_state,
                "CurrentTrackDuration": track_duration,
                "RelativeTimePosition": rel_time,
                "A_ARG_TYPE_SeekTarget": rel_time,
                "TrackURI": track_uri,
                "TrackMetaData": metadata,
                "TrackSource": self.state.track_source,
                "PlayMedium": self.state.play_medium,
                "DeviceName": device_name,
                "GroupName": device_name,
            }
        )

    @staticmethod
    def _event_propertyset(properties: dict[str, str]) -> str:
        """Wrap event property values in a UPnP event propertyset."""

        items = ["<?xml version=\"1.0\" encoding=\"utf-8\"?>", '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">']
        for key, value in properties.items():
            items.append(f"<e:property><{key}>{escape(value)}</{key}></e:property>")
        items.append("</e:propertyset>")
        return "".join(items)

    @staticmethod
    def _seconds_to_hms(value: int) -> str:
        """Format seconds as HH:MM:SS for UPnP transport fields."""

        total = max(0, int(value))
        hours = total // 3600
        minutes = (total % 3600) // 60
        seconds = total % 60
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
