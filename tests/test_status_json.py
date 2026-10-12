import pytest
from html import escape
from unittest.mock import AsyncMock
from uuid import uuid4

from fake_linkplay.config import Config, ExperimentalSource, load_config
from fake_linkplay.http_api import EventSubscriber, HttpApi, ParsedCommand
from fake_linkplay.mpv_backend import MpvBackend, redact_sensitive as redact_mpv_sensitive
from fake_linkplay.radio import RadioResolution
from fake_linkplay.state import RendererState


class DummyMpv(MpvBackend):
    def __init__(self) -> None:
        super().__init__("/tmp/nonexistent", False)
        self.loaded_url = ""
        self.idle = True
        self.paused = False
        self.load_count = 0
        self.load_options: dict[str, str] | None = None
        self.seeked_to = 0
        self.duration = 3600
        self.channel_balance_value = 0.0
        self.peq_bands: list[dict[str, float]] = []

    async def snapshot(self):  # type: ignore[override]
        return {
            "idle-active": self.idle,
            "core-idle": self.idle,
            "pause": self.paused,
            "filename": self.loaded_url,
            "path": self.loaded_url,
            "playback-time": self.seeked_to,
            "duration": self.duration if self.loaded_url else None,
            "track-list": [{"type": "audio", "selected": True}],
        }

    async def volume(self, value: int) -> None:  # type: ignore[override]
        return None

    async def loadfile(self, url: str, options: dict[str, str] | None = None) -> None:  # type: ignore[override]
        self.loaded_url = url
        self.idle = False
        self.paused = False
        self.load_options = options
        self.load_count += 1

    async def pause(self) -> None:  # type: ignore[override]
        self.paused = True

    async def resume(self) -> None:  # type: ignore[override]
        self.paused = False

    async def stop(self) -> None:  # type: ignore[override]
        self.idle = True

    async def seek(self, seconds: int) -> None:  # type: ignore[override]
        self.seeked_to = seconds

    async def channel_balance(self, value: float) -> None:  # type: ignore[override]
        self.channel_balance_value = value

    async def peq(self, bands: list[dict[str, float]]) -> None:  # type: ignore[override]
        self.peq_bands = bands


def make_api() -> HttpApi:
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", advertise_ip="127.0.0.1", state_dir=f"/tmp/fake-linkplay-test-{uuid4()}")
    return HttpApi(cfg, RendererState.from_config(cfg), DummyMpv())


@pytest.mark.asyncio
async def test_status_ex_contains_expected_keys() -> None:
    payload = await make_api().execute(ParsedCommand("getStatusEx"), "127.0.0.1")
    assert isinstance(payload, dict)
    for key in ("uuid", "DeviceName", "volume", "vol", "status", "mode", "curpos", "totlen", "stream_url"):
        assert key in payload


def test_renderer_state_basic_transitions() -> None:
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", advertise_ip="127.0.0.1")
    state = RendererState.from_config(cfg)
    state.mark_playing("https://media.example.test/stream.mp3")
    assert state.status == "play"
    assert state.mode == "10"
    assert state.track_source == ""  # Playback alone cannot identify a provider.
    state.mark_pause()
    assert state.status == "pause"
    state.mark_transitioning()
    assert state.status == "transition"
    state.curpos = 42
    state.mark_stop()
    assert state.status == "stop"
    assert state.curpos == 0


@pytest.mark.asyncio
async def test_wiim_home_profile_status_uses_discovery_uuid_and_security() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        wiim_home_profile=True,
        discovery_uuid="FF310001BD4CE65900AF9543000000000000",
        friendly_name="LinkPlayA31_542C",
        manufacturer="Linkplay Technology Inc.",
        model_name="MUZO Cobblestone",
    )
    api = HttpApi(cfg, RendererState.from_config(cfg), DummyMpv())
    payload = await api.execute(ParsedCommand("getStatusEx"), "127.0.0.1")
    assert isinstance(payload, dict)
    assert payload["uuid"] == "FF310001BD4CE65900AF9543000000000000"
    assert payload["upnp_uuid"] == "uuid:FF310001BD4CE65900AF9543000000000000"
    assert payload["security"] == "http"
    assert payload["model"] == "MUZO Cobblestone"
    assert payload["project"] == "Open_Room_Renderer"
    assert payload["MAC"] == cfg.wiim_mac
    assert payload["wmrm_capability"] == 0
    assert payload["capability"] == "0x0"
    assert payload["BatteryFlag"] == "0"
    assert payload["BatteryMains"] == "0"
    assert payload["BatteryPercent"] == "0"
    assert payload["portable"] == "0"
    assert payload["securemode"] == "0"
    assert payload["upnp_version"] == "1000"
    assert payload["VersionUpdate"] == "0"
    assert payload["NewVer"] == "0.1.0"
    assert payload["preset_key"] == "0"


@pytest.mark.asyncio
async def test_legacy_capability_values_are_configurable_for_wiim_home_diagnostics() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        wiim_home_profile=True,
        legacy_capability_value="0x7ffffffe",
        legacy_plm_support_value="0x7ffffffe",
    )
    api = HttpApi(cfg, RendererState.from_config(cfg), DummyMpv())
    payload = await api.execute(ParsedCommand("getStatusEx"), "127.0.0.1")
    assert isinstance(payload, dict)
    assert payload["capability"] == "0x7ffffffe"
    assert payload["capabilities"] == "0x7ffffffe"
    assert payload["plm_support"] == "0x7ffffffe"


@pytest.mark.asyncio
async def test_player_status_contains_expected_keys() -> None:
    payload = await make_api().execute(ParsedCommand("getPlayerStatus"), "127.0.0.1")
    assert isinstance(payload, dict)
    for key in ("type", "mode", "status", "curpos", "totlen", "vol", "mute", "Title"):
        assert key in payload


@pytest.mark.asyncio
async def test_unknown_commands_do_not_crash() -> None:
    payload = await make_api().execute(ParsedCommand("bogus"), "127.0.0.1")
    assert isinstance(payload, dict)
    assert payload["error"] == "Failed"


@pytest.mark.asyncio
async def test_experimental_sources_are_added_to_status() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        source_experiment_variant="new_source",
        experimental_sources=[ExperimentalSource(id="ardsounds", name="ARD Sounds", source="ARD Sounds", url="https://www.ardsounds.de/")],
    )
    api = HttpApi(cfg, RendererState.from_config(cfg), DummyMpv())
    payload = await api.execute(ParsedCommand("getStatusEx"), "127.0.0.1")
    assert isinstance(payload, dict)
    assert payload["source_list"][0]["name"] == "ARD Sounds"
    assert payload["music_services"][0]["url"] == "https://www.ardsounds.de/"


@pytest.mark.asyncio
async def test_experimental_sources_can_be_exposed_as_presets() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        source_experiment_variant="preset",
        experimental_sources=[ExperimentalSource(id="ardsounds", name="ARD Sounds", source="ARD Sounds", url="https://www.ardsounds.de/")],
    )
    api = HttpApi(cfg, RendererState.from_config(cfg), DummyMpv())
    payload = await api.execute(ParsedCommand("getPresetInfo"), "127.0.0.1")
    assert isinstance(payload, dict)
    assert payload["preset_num"] == "1"
    assert payload["presets"][0]["source"] == "ARD Sounds"


@pytest.mark.asyncio
async def test_stream_services_capability_includes_known_control_and_experimental_ids() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        source_experiment_variant="new_source",
        experimental_sources=[ExperimentalSource(id="ARDSounds", name="ARD Sounds", source="ARD Sounds", url="https://www.ardsounds.de/")],
    )
    api = HttpApi(cfg, RendererState.from_config(cfg), DummyMpv())
    payload = await api.execute(ParsedCommand("getStreamServicesCapability"), "127.0.0.1")
    assert isinstance(payload, dict)
    ids = [item["id"] for item in payload["streamServices"]]
    assert ids == ["ARDSounds"]
    assert payload["streamServices"][0]["experimental"] is True



@pytest.mark.asyncio
async def test_tunein_experiment_adds_tunein_without_experimental_sources() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        source_experiment_variant="tunein",
    )
    api = HttpApi(cfg, RendererState.from_config(cfg), DummyMpv())
    payload = await api.execute(ParsedCommand("getStatusEx"), "127.0.0.1")
    assert isinstance(payload, dict)
    assert "streamServicesCapability" in payload
    assert payload["streams"] == "200"
    assert "streamAll" not in payload
    assert "streams_all" not in payload
    assert "newTuneIn" in payload["StreamCapability"]
    assert payload["music_services"] == [{"id": "newTuneIn", "name": "TuneIn", "source": "newTuneIn", "enabled": True}]


@pytest.mark.asyncio
async def test_tunein_stream_values_are_configurable_for_ui_probe() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        source_experiment_variant="tunein",
        tunein_streams_value="8440200",
        tunein_stream_all_value="0",
    )
    api = HttpApi(cfg, RendererState.from_config(cfg), DummyMpv())
    payload = await api.execute(ParsedCommand("getStatusEx"), "127.0.0.1")
    assert isinstance(payload, dict)
    assert payload["streams"] == "8440200"
    assert payload["streams_all"] == "0"
    assert "streamAll" not in payload


@pytest.mark.asyncio
async def test_tunein_minimal_capability_exposes_only_new_tunein() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        source_experiment_variant="tunein",
        tunein_minimal_capability=True,
    )
    api = HttpApi(cfg, RendererState.from_config(cfg), DummyMpv())
    payload = await api.execute(ParsedCommand("getStreamServicesCapability"), "127.0.0.1")
    assert isinstance(payload, dict)
    assert payload["streamServices"] == [
        {"id": "newTuneIn", "version": "1.1", "active": True, "configurable": False, "removable": False}
    ]


@pytest.mark.asyncio
async def test_plex_service_can_be_added_to_minimal_tunein_capability() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        source_experiment_variant="tunein",
        tunein_minimal_capability=True,
        enable_plex_service=True,
    )
    api = HttpApi(cfg, RendererState.from_config(cfg), DummyMpv())
    status = await api.execute(ParsedCommand("getStatusEx"), "127.0.0.1")
    assert isinstance(status, dict)
    ids = [item["id"] for item in status["streamServicesCapability"]["streamServices"]]
    assert ids == ["newTuneIn", "Plex"]
    assert status["streamServicesCapability"]["version"] == "1.4"
    plex = status["streamServicesCapability"]["streamServices"][1]
    assert plex["version"] == "1.4"
    assert plex["removable"] is True
    assert plex["replayGain"] is False
    assert status["streamServicesCapability"]["streamServices"][0]["version"] == "1.4"
    assert status["supported_services"] == "newTuneIn,Plex"
    assert status["streams"] == "200"
    assert "streamAll" not in status
    assert "streams_all" not in status
    assert {"id": "Plex", "name": "Plex", "source": "Plex", "enabled": True} in status["music_services"]


@pytest.mark.asyncio
async def test_plex_exact_capability_uses_only_camel_case_shape() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        source_experiment_variant="tunein",
        tunein_minimal_capability=True,
        enable_plex_service=True,
        plex_capability_mode="exact",
    )
    api = HttpApi(cfg, RendererState.from_config(cfg), DummyMpv())
    status = await api.execute(ParsedCommand("getStatusEx"), "127.0.0.1")
    assert isinstance(status, dict)
    capability = status["streamServicesCapability"]
    assert "status" not in capability
    assert "stream_services" not in capability
    assert [item["id"] for item in capability["streamServices"]] == ["Plex", "newTuneIn"]
    plex = capability["streamServices"][0]
    assert plex["version"] == "1.4"
    assert "name" not in plex


@pytest.mark.asyncio
async def test_stream_service_sentinels_can_be_added_for_manage_gate_diagnostics() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        source_experiment_variant="tunein",
        tunein_minimal_capability=True,
        enable_plex_service=True,
        plex_capability_mode="exact",
        enable_stream_service_sentinels=True,
    )
    api = HttpApi(cfg, RendererState.from_config(cfg), DummyMpv())
    status = await api.execute(ParsedCommand("getStatusEx"), "127.0.0.1")
    assert isinstance(status, dict)
    services = status["streamServicesCapability"]["streamServices"]
    assert [item["id"] for item in services] == ["Plex", "newTuneIn", "Deezer2", "SoundCloud", "OPS", "LPRP", "HM", "LPDZ", "SC"]
    assert all(item["version"] == "1.4" for item in services)
    assert status["supported_services"] == "Plex,newTuneIn"
    assert [item["id"] for item in status["music_services"]] == ["Plex", "newTuneIn"]
    assert all(not item["active"] for item in services[2:])


@pytest.mark.asyncio
async def test_deezer_sentinel_cannot_claim_login_or_premium() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        http_api_port=59152,
        enable_stream_service_sentinels=True,
    )
    api = HttpApi(cfg, RendererState.from_config(cfg), DummyMpv())
    login_body = "<u:UserLogin><AccountSource>Deezer2</AccountSource></u:UserLogin>"
    from fake_linkplay.soap import SoapFault
    with pytest.raises(SoapFault):
        api._playqueue_response_fields("UserLogin", login_body)
    info_body = "<u:GetUserInfo><AccountSource>Deezer2</AccountSource></u:GetUserInfo>"
    info_response = api._playqueue_response_fields("GetUserInfo", info_body)
    assert "not login" in info_response
    assert "premium" not in info_response
    assert "access-token" not in info_response


def test_deezer_http_stub_contains_collection_and_drm_free_track() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        http_api_port=59152,
    )
    api = HttpApi(cfg, RendererState.from_config(cfg), DummyMpv())
    root = api._deezer_payload("root")
    collection = api._deezer_payload("collection")
    assert root["collection"] == "http://127.0.0.1:59152/deezer/collection"
    assert root["search"] == "http://127.0.0.1:59152/deezer/search"
    assert root["data"]
    assert collection["data"]
    first = collection["data"][0]
    assert isinstance(first, dict)
    assert first["source"] == "Deezer2"
    assert first["type"] == "track"
    assert str(first["playUrl"]).startswith("https://")


def test_plex_user_info_is_persisted_and_reloaded(tmp_path) -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        state_dir=str(tmp_path),
    )
    api = HttpApi(cfg, RendererState.from_config(cfg), DummyMpv())
    user_info = api._plex_logged_in_user_info("sample-plex-token", {"userID": "42", "username": "Local Plex"})
    api._persist_plex_user_info(user_info)

    path = tmp_path / "plex_user_info.json"
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600

    reloaded = HttpApi(cfg, RendererState.from_config(cfg), DummyMpv())
    assert reloaded.plex_user_info is not None
    assert reloaded.plex_user_info["userId"] == "42"
    assert reloaded.plex_user_info["accessToken"] == "sample-plex-token"
    assert reloaded.plex_user_info["redirectUrl"] == "com.linkplay.wiimhome%3A%2F%2Fcallback%3Fcategory%3Doauth%26service%3Dplex"

    response = reloaded._playqueue_response_fields("GetUserInfo", "<GetUserInfo><AccountSource>Plex</AccountSource></GetUserInfo>")
    assert response is not None
    assert "Local Plex" in response
    assert "sample-plex-token" in response


@pytest.mark.asyncio
async def test_plex_legacy_gate_is_explicit_opt_in() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        source_experiment_variant="tunein",
        tunein_minimal_capability=True,
        enable_plex_service=True,
        enable_plex_legacy_gate=True,
    )
    api = HttpApi(cfg, RendererState.from_config(cfg), DummyMpv())
    status = await api.execute(ParsedCommand("getStatusEx"), "127.0.0.1")
    assert isinstance(status, dict)
    assert status["supported_services"] == "newTuneIn,Plex,PLEX"
    assert int(status["streams"]) & (1 << 43)
    assert int(status["streams_all"]) & (1 << 43)
    assert "streamAll" not in status
    assert {"id": "PLEX", "name": "Plex", "source": "Plex", "enabled": True} in status["music_services"]


@pytest.mark.asyncio
async def test_stream_services_active_toggle_returns_status_only() -> None:
    api = make_api()
    payload = await api.execute(ParsedCommand("SetStreamServicesCapability:Active:Roon"), "127.0.0.1")
    assert isinstance(payload, dict)
    assert payload["error"] == "Failed"


@pytest.mark.asyncio
async def test_tunein_create_queue_resolves_station_and_preserves_provider_metadata() -> None:
    api = make_api()
    direct_url = "https://media.example.test/station.mp3"
    search_url = "http://api.radiotime.com/profiles/s12345/media?partnerId=synthetic&latlon=,&version=2.0"
    artwork = "http://cdn-profiles.tunein.com/s12345/images/logoq.png"
    context = (
        "<PlayList><ListName>Synthetic Station</ListName><ListInfo>"
        "<SourceName>newTuneIn</SourceName>"
        f"<SearchUrl>{escape(search_url)}</SearchUrl><PicUrl>{artwork}</PicUrl>"
        "</ListInfo></PlayList>"
    )
    api._resolve_station_id = AsyncMock(return_value=RadioResolution(direct_url, "s12345"))
    api._resolve_playback_url = _identity_resolver  # type: ignore[method-assign]
    await api._execute_soap_side_effect("CreateQueue", f"<CreateQueue><QueueContext>{escape(context)}</QueueContext></CreateQueue>")
    api._resolve_station_id.assert_awaited_once_with("s12345")
    assert api.queue_url == direct_url
    assert api.queue_source == "newTuneIn"
    assert api.queue_search_url == search_url
    assert api.state.album_art_uri == artwork
    assert "<SourceName>newTuneIn</SourceName>" in api.queue_context
    assert "<TrackSource>newTuneIn</TrackSource>" in api.queue_context
    assert f"<PicUrl>{artwork}</PicUrl>" in api.queue_context
    assert f"<upnp:albumArtURI>{artwork}</upnp:albumArtURI>" in api.state.track_metadata
    assert f"<song:image>{artwork}</song:image>" in api.state.track_metadata
    assert f"<SearchUrl>{escape(search_url)}</SearchUrl>" in api.queue_context
    assert api.state.totlen == 0
    meta = api.state.as_meta_info()
    assert meta["albumArtURI"] == artwork
    assert meta["picurl"] == artwork
    await api._execute_soap_side_effect("PlayQueueWithIndex", "<PlayQueueWithIndex><Index>1</Index></PlayQueueWithIndex>")
    assert api.mpv.loaded_url == direct_url
    assert api.state.track_source == "newTuneIn"


@pytest.mark.asyncio
async def test_play_after_stop_reloads_existing_queue_url() -> None:
    api = make_api()
    direct_url = "https://media.example.test/station.mp3"
    context = (
        "<PlayList><ListName>Synthetic Station</ListName>"
        "<ListInfo><SourceName>newTuneIn</SourceName><StreamId>s12345</StreamId>"
        "</ListInfo></PlayList>"
    )
    api._resolve_station_id = AsyncMock(return_value=RadioResolution(direct_url, "s12345"))
    api._resolve_playback_url = _identity_resolver  # type: ignore[method-assign]
    await api._execute_soap_side_effect("CreateQueue", f"<CreateQueue><QueueContext>{escape(context)}</QueueContext></CreateQueue>")
    await api._execute_soap_side_effect("PlayQueueWithIndex", "<PlayQueueWithIndex><Index>1</Index></PlayQueueWithIndex>")
    await api._execute_soap_side_effect("Stop", "<Stop />")
    await api._execute_soap_side_effect("Play", "<Play />")
    assert api.mpv.load_count == 2
    assert api.mpv.loaded_url == direct_url
    assert api.state.status == "play"
    assert api.state.track_source == "newTuneIn"
    api._resolve_station_id.assert_awaited_once_with("s12345")


@pytest.mark.asyncio
async def test_direct_http_play_uses_custom_radio_source() -> None:
    api = make_api()
    await api.execute(ParsedCommand("play", ("https://example.test/stream.mp3",)), "127.0.0.1")
    assert api.queue_source == "CustomRadio"
    assert api.state.track_source == "CustomRadio"
    assert "CustomRadio" in api._build_queue_context()


def test_avtransport_event_body_contains_current_play_state_and_metadata() -> None:
    api = make_api()
    api.state.current_url = "https://media.example.test/station.mp3"
    api.state.title = "The JOY FM"
    api.state.track_source = "newTuneIn"
    api.state.play_medium = "RADIO-NETWORK"
    api.state.album_art_uri = "http://cdn-profiles.tunein.com/s29850/images/logoq.png"
    api.state.track_metadata = api._didl_metadata(api.state.current_url, api.state.title, api.state.track_source, api.state.album_art_uri)
    api.state.mark_playing(api.state.current_url)
    body = api._event_body("rendertransport1")
    assert "<LastChange>" in body
    assert "TransportState" in body
    assert "PLAYING" in body
    assert "newTuneIn" in body
    assert "RADIO-NETWORK" in body
    assert "song:isLive" in body
    assert "song:image" in body


def test_tunein_artwork_url_prefers_large_logo_variant() -> None:
    api = make_api()
    assert (
        api._best_art_url("http://cdn-profiles.tunein.com/s25224/images/logoq.png?t=638466830600000000")
        == "http://cdn-profiles.tunein.com/s25224/images/logog.png?t=638466830600000000"
    )
    assert (
        api._best_art_url("http://cdn-profiles.tunein.com/s23169/images/logoq.jpg?t=637783044850000000")
        == "http://cdn-profiles.tunein.com/s23169/images/logog.jpg?t=637783044850000000"
    )


def test_stream_url_parser_prefers_audio_over_artwork_urls() -> None:
    text = (
        '{"image":"http://cdn-profiles.tunein.com/s25224/images/logoq.png",'
        '"stream":"https://example.test/live/stream.m3u8",'
        '"fallback":"https://example.test/audio.mp3"}'
    )
    assert HttpApi._first_stream_url(text) == "https://example.test/live/stream.m3u8"


def test_stream_url_parser_prefers_tunein_play_url() -> None:
    text = (
        '{"Items":[{"Actions":{"Play":{"CanPlay":true,"IsLive":false,'
        '"PlayUrl":"https://media.example.test/show.mp3"}},'
        '"PicUrl":"http://cdn-profiles.tunein.com/p235703/images/logog.png"}]}'
    )
    assert HttpApi._first_stream_url(text) == "https://media.example.test/show.mp3"


def test_audio_duration_can_be_estimated_from_headers_and_bitrate_url() -> None:
    headers = {"Content-Length": str(92_160_000)}
    duration = HttpApi._duration_from_headers("https://media.example.test/show_mp3-256kd.mp3", headers)
    assert duration == 2880


def test_audio_duration_can_use_content_range_total_size() -> None:
    headers = {"Content-Range": "bytes 0-0/92160000"}
    duration = HttpApi._duration_from_headers("https://media.example.test/show_mp3-256kd.mp3", headers)
    assert duration == 2880


def test_plex_tokens_are_redacted_from_log_previews() -> None:
    raw = (
        'https://plex.example/library/parts/1.mp3?X-Plex-Token=sample-token&x=1 '
        '{"accessToken":"sample-access","refreshToken":"sample-refresh"}'
    )
    redacted = HttpApi._redact_sensitive_text(raw)
    assert "sample-token" not in redacted
    assert "sample-access" not in redacted
    assert "sample-refresh" not in redacted
    assert "X-Plex-Token=***" in redacted


def test_private_network_and_device_ids_are_redacted_from_log_previews() -> None:
    raw = "peer 10.0.0.75 server 172.16.0.49 mac 02:00:00:00:00:01 uuid 00000000-0000-4000-8000-000000000000"
    redacted = HttpApi._redact_sensitive_text(raw)
    assert "10.0.0.75" not in redacted
    assert "172.16.0.49" not in redacted
    assert "02:00:00:00:00:01" not in redacted
    assert "00000000-0000-4000-8000-000000000000" not in redacted
    assert "[private-ip]" in redacted
    assert "[mac]" in redacted
    assert "[uuid]" in redacted


def test_mpv_url_redaction_hides_tokens_and_private_ips() -> None:
    redacted = redact_mpv_sensitive("http://172.16.1.2/track.mp3?X-Plex-Token=sample-token")
    assert "172.16.1.2" not in redacted
    assert "sample-token" not in redacted
    assert "[private-ip]" in redacted
    assert "X-Plex-Token=***" in redacted


def test_stream_service_login_fields_are_redacted_from_log_previews() -> None:
    raw = (
        "<UserLogin><UserName>person@example.test</UserName><PassWord>sample-password</PassWord>"
        "<CodeVerifier>sample-verifier</CodeVerifier><Token>sample-token</Token></UserLogin>"
    )
    redacted = HttpApi._redact_sensitive_text(raw)
    assert "person@example.test" not in redacted
    assert "sample-password" not in redacted
    assert "sample-verifier" not in redacted
    assert "sample-token" not in redacted
    assert "<UserName>***</UserName>" in redacted
    assert "<PassWord>***</PassWord>" in redacted


def test_tunein_json_duration_accepts_actions_play_duration_milliseconds() -> None:
    text = '{"Actions":{"Play":{"Duration":2880000,"IsLive":false,"Stream":"https://media.example.test/show.mp3"}}}'
    assert HttpApi._json_duration_seconds(text) == 2880


def _episode_queue_body(duration: int | None = 2880) -> str:
    """A supplied playable episode, with provider hints only in ListInfo."""
    duration_field = f"<Duration>{duration}</Duration>" if duration is not None else ""
    context = (
        "<PlayList><ListName>Synthetic Episode</ListName><ListInfo>"
        "<SourceName>newTuneIn</SourceName><StreamId>t12345</StreamId>"
        "<SearchUrl>http://api.radiotime.com/profiles/t12345/media?partnerId=synthetic</SearchUrl>"
        "<PicUrl>https://art.example.test/logoq.png</PicUrl>"
        "<StreamType>episode</StreamType><ContentType>radio</ContentType>"
        "</ListInfo><Tracks><Track1><Title>Synthetic Episode</Title>"
        f"<URL>https://media.example.test/show.mp3</URL>{duration_field}"
        "</Track1></Tracks></PlayList>"
    )
    return f"<CreateQueue><QueueContext>{escape(context)}</QueueContext></CreateQueue>"


@pytest.mark.asyncio
async def test_tunein_episode_queue_marks_metadata_not_live() -> None:
    api = make_api()
    await api._execute_soap_side_effect("CreateQueue", _episode_queue_body())
    assert api.queue_is_live is False
    assert api.state.queue.current.stream_type == "episode"
    assert api.state.queue.current.content_type == "radio"
    assert "<StreamType>episode</StreamType>" in api.queue_context
    assert "<song:isLive>0</song:isLive>" in api.state.track_metadata
    assert api.state.totlen == 2880


@pytest.mark.asyncio
async def test_tunein_episode_queue_uses_supplied_audio_url_in_queue_context() -> None:
    api = make_api()
    api._resolve_station_id = AsyncMock(side_effect=AssertionError("Direct episodes need no station lookup"))
    await api._execute_soap_side_effect("CreateQueue", _episode_queue_body())
    assert api.queue_url == "https://media.example.test/show.mp3"
    assert "<URL>https://media.example.test/show.mp3</URL>" in api.queue_context
    await api._execute_soap_side_effect("PlayQueueWithIndex", "<PlayQueueWithIndex><Index>1</Index></PlayQueueWithIndex>")
    assert api.mpv.loaded_url == "https://media.example.test/show.mp3"
    assert api.mpv.load_options == {}
    api._resolve_station_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_tunein_episode_can_use_local_audio_proxy_when_enabled() -> None:
    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000",
        advertise_ip="127.0.0.1",
        http_api_port=59152,
        proxy_on_demand_audio=True,
    )
    api = HttpApi(cfg, RendererState.from_config(cfg), DummyMpv())
    await api._execute_soap_side_effect("CreateQueue", _episode_queue_body())
    await api._execute_soap_side_effect("PlayQueueWithIndex", "<PlayQueueWithIndex><Index>1</Index></PlayQueueWithIndex>")
    assert api.mpv.loaded_url == "http://127.0.0.1:59152/proxy/audio?url=https%3A%2F%2Fmedia.example.test%2Fshow.mp3"
    assert api.mpv.load_options == {}


@pytest.mark.asyncio
async def test_episode_seek_rel_time_updates_position_and_preserves_supplied_duration() -> None:
    api = make_api()
    await api._execute_soap_side_effect("CreateQueue", _episode_queue_body())
    assert api.state.totlen == 2880
    assert 'duration="2880"' in api.state.track_metadata
    await api._execute_soap_side_effect("PlayQueueWithIndex", "<PlayQueueWithIndex><Index>1</Index></PlayQueueWithIndex>")
    await api._execute_soap_side_effect("Seek", "<Seek><Unit>REL_TIME</Unit><Target>00:00:30</Target></Seek>")
    assert api.mpv.seeked_to == 30
    assert api.state.curpos == 30
    assert api.state.totlen == 2880


@pytest.mark.asyncio
async def test_episode_without_supplied_duration_does_not_invent_one_hour() -> None:
    api = make_api()
    await api._execute_soap_side_effect("CreateQueue", _episode_queue_body(duration=None))
    assert api.queue_is_live is False
    assert api.state.queue.current.duration == api.queue_duration_sec == api.state.totlen == 0
    assert 'duration="3600"' not in api.state.track_metadata


def test_seek_ready_accepts_near_target_despite_stale_demux_underrun() -> None:
    api = make_api()
    snapshot = {
        "playback-time": 2734.6,
        "core-idle": False,
        "idle-active": False,
        "pause": False,
        "cache-buffering-state": 100,
        "track-list": [{"type": "audio", "selected": True}],
        "demuxer-cache-state": {"underrun": True},
    }
    assert api._snapshot_is_seek_ready(snapshot, 2734)


def test_seek_ready_rejects_idle_snapshot() -> None:
    api = make_api()
    snapshot = {
        "playback-time": 2734.6,
        "core-idle": True,
        "idle-active": True,
        "cache-buffering-state": 100,
        "track-list": [{"type": "audio", "selected": True}],
    }
    assert not api._snapshot_is_seek_ready(snapshot, 2734)


@pytest.mark.asyncio
async def test_episode_sync_ignores_proxy_chunk_duration() -> None:
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", advertise_ip="127.0.0.1", http_api_port=59152)
    mpv = DummyMpv()
    mpv.duration = 3
    api = HttpApi(cfg, RendererState.from_config(cfg), mpv)
    api.queue_is_live = False
    api.queue_duration_sec = 3600
    api.state.totlen = 3600
    await mpv.loadfile("http://127.0.0.1:59152/proxy/audio?url=https%3A%2F%2Fmedia.example.test%2Fshow.mp3")
    await api.sync_mpv_state()
    assert api.state.totlen == 3600


@pytest.mark.asyncio
async def test_live_sync_ignores_mpv_segment_duration() -> None:
    api = make_api()
    api.queue_is_live = True
    api.state.totlen = 0
    api.mpv.duration = 35
    await api.mpv.loadfile("https://example.test/live.mp3")
    await api.sync_mpv_state()
    assert api.state.totlen == 0


@pytest.mark.asyncio
async def test_set_play_mode_updates_loop_state() -> None:
    api = make_api()
    from fake_linkplay.soap import SoapFault
    with pytest.raises(SoapFault):
        await api._execute_soap_side_effect("SetPlayMode", "<SetPlayMode><NewPlayMode>REPEAT_ALL</NewPlayMode></SetPlayMode>")
    await api._execute_soap_side_effect("SetPlayMode", "<SetPlayMode><NewPlayMode>NORMAL</NewPlayMode></SetPlayMode>")
    assert api.state.loop == "0"


@pytest.mark.asyncio
async def test_previous_restarts_current_track() -> None:
    api = make_api()
    await api._play_url("https://example.test/audio.mp3")
    api.mpv.seeked_to = api.state.curpos = 45
    await api._execute_soap_side_effect("Previous", "<Previous />")
    assert api.state.curpos == 0
    assert api.mpv.seeked_to == 0


def test_rendering_response_fields_reflect_volume_and_mute() -> None:
    api = make_api()
    api.state.gain_confirmed = True  # Serializer fixture supplies confirmed values.
    api.state.volume = 42
    api.state.mute = True
    assert "<CurrentVolume>42</CurrentVolume>" == api._rendering_response_fields("GetVolume")
    assert "<CurrentMute>1</CurrentMute>" == api._rendering_response_fields("GetMute")


@pytest.mark.asyncio
async def test_hardware_only_settings_are_disabled() -> None:
    api = make_api()
    assert await api.execute(ParsedCommand("LED_SWITCH_STATUS_GET"), "127.0.0.1") == {"status": "0", "enable": "0"}
    assert await api.execute(ParsedCommand("VOL_LED_SWITCH_GET"), "127.0.0.1") == {"status": "0", "enable": "0"}
    assert await api.execute(ParsedCommand("Button_Enable_GET"), "127.0.0.1") == {"status": "0", "enable": "0"}


@pytest.mark.asyncio
async def test_settings_queries_use_wiim_home_model_shapes() -> None:
    api = make_api()
    assert await api.execute(ParsedCommand("getStaticIpInfo"), "127.0.0.1") == {
        "wlanStaticIpEnable": 0,
        "wlanStaticIp": "0.0.0.0/0",
        "ethStaticIpEnable": 0,
        "ethStaticIp": "0.0.0.0/0",
    }
    assert await api.execute(ParsedCommand("getDigitalInputAudioTypeSupport"), "127.0.0.1") == "0x0"
    assert await api.execute(ParsedCommand("getModeRename"), "127.0.0.1") == {}
    assert await api.execute(ParsedCommand("getLMPFilterCapability"), "127.0.0.1") == {"version": "0"}
    audio_input = await api.execute(ParsedCommand("getAudioInputEnable"), "127.0.0.1")
    assert audio_input == {
        "ver": "1.0",
        "audioInput": [
            {"mode": "wifi", "enable": 1},
        ],
    }
    acoustic = await api.execute(ParsedCommand("GetAcousticCapability"), "127.0.0.1")
    assert acoustic["Version"] == "1.0"
    assert acoustic["PEQ"]["Filters"] == []
    assert acoustic["OutputDelay"]["EnableMicroDelay"] is False
    assert acoustic["RC"]["LevelMatch"] is None


@pytest.mark.asyncio
async def test_audio_output_capability_queries_are_typed_disabled_models() -> None:
    api = make_api()
    assert await api.execute(ParsedCommand("getSpdifOutMaxCap"), "127.0.0.1") == {
        "ver": "0",
        "sampleRate": "0",
        "bitDepth": "0",
        "cfgType": "",
        "multiList": [],
    }
    assert await api.execute(ParsedCommand("getCoaxOutMaxCap"), "127.0.0.1") == {
        "ver": "0",
        "sampleRate": "0",
        "bitDepth": "0",
        "cfgType": "",
        "multiList": [],
    }
    assert await api.execute(ParsedCommand("GetFadeFeature"), "127.0.0.1") == {"FadeFeature": 0}
    assert await api.execute(ParsedCommand("getAudioOutputVrmsSupportList:{\"ver\":\"1.1\"}"), "127.0.0.1") == {
        "ver": "1.1",
        "voltage": [],
    }
    assert await api.execute(ParsedCommand("getAuxVoltageSupportList"), "127.0.0.1") == {"ver": "1.1", "voltage": []}
    assert await api.execute(ParsedCommand("getSpdifOutSwitchDelayMs"), "127.0.0.1") == {
        "status": "0",
        "delayMs": "0",
        "delay": "0",
    }


@pytest.mark.asyncio
async def test_wiim_home_settings_mutations_are_acknowledged() -> None:
    api = make_api()
    assert await api.execute(ParsedCommand("getChannelBalance"), "127.0.0.1") == "0.0"
    assert await api.execute(ParsedCommand("setChannelBalance:0.3"), "127.0.0.1") == "OK"
    assert await api.execute(ParsedCommand("getChannelBalance"), "127.0.0.1") == "0.3"
    assert await api.execute(ParsedCommand("getbthistory"), "127.0.0.1") == {"status": "0", "history": [], "list": []}
    assert await api.execute(ParsedCommand("setDeviceName:Dachboden"), "127.0.0.1") == "OK"


@pytest.mark.asyncio
async def test_device_name_and_channel_balance_are_persistent_and_effective(tmp_path) -> None:
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", advertise_ip="127.0.0.1", state_dir=str(tmp_path))
    mpv = DummyMpv()
    api = HttpApi(cfg, RendererState.from_config(cfg), mpv)

    assert await api.execute(ParsedCommand("setDeviceName:Dachboden"), "127.0.0.1") == "OK"
    assert cfg.friendly_name == "Dachboden"
    assert cfg.mdns_instance_name == "Dachboden"
    assert (tmp_path / "device_name").read_text().strip() == "Dachboden"

    assert await api.execute(ParsedCommand("setChannelBalance:0.3"), "127.0.0.1") == "OK"
    assert api.channel_balance == "0.3"
    assert mpv.channel_balance_value == 0.3
    assert (tmp_path / "channel_balance").read_text().strip() == "0.3"
    assert await api.execute(ParsedCommand("setChannelBalance:30"), "127.0.0.1") == "OK"
    assert api.channel_balance == "0.3"

    assert await api.execute(ParsedCommand("setHexDeviceName:44616368626f64656e23"), "127.0.0.1") == "OK"
    assert cfg.friendly_name == "Dachboden#"


def test_persistent_runtime_state_is_loaded(tmp_path) -> None:
    (tmp_path / "config.yaml").write_text(f"state_dir: {tmp_path}\nuuid: 00000000-0000-4000-8000-000000000000\n")
    (tmp_path / "device_name").write_text("Dachboden\n")
    (tmp_path / "channel_balance").write_text("-0.25\n")

    cfg = load_config(tmp_path / "config.yaml")

    assert cfg.friendly_name == "Dachboden"
    assert cfg.mdns_instance_name == "Dachboden"
    assert cfg.channel_balance == "-0.25"


def test_event_subscriber_dedupe_and_gateway_defaults() -> None:
    api = make_api()
    api.event_subscribers["uuid:old"] = EventSubscriber(
        sid="uuid:old",
        callback_url="http://192.0.2.75:22809/event",
        service="rendertransport1",
    )
    api.event_subscribers["uuid:other-service"] = EventSubscriber(
        sid="uuid:other-service",
        callback_url="http://192.0.2.75:22809/event",
        service="rendercontrol1",
    )
    api._drop_duplicate_subscribers("http://192.0.2.75:22809/event", "rendertransport1")
    assert "uuid:old" not in api.event_subscribers
    assert "uuid:other-service" in api.event_subscribers
    assert api._default_gateway_for_ip("192.0.2.143") == "192.0.2.1"


async def _identity_resolver(url: str) -> str:
    return url


@pytest.mark.asyncio
async def test_out_of_range_volume_does_not_raise_gain() -> None:
    api = make_api()
    with pytest.raises(ValueError):
        await api.execute(ParsedCommand("volume", ("150",)), "127.0.0.1")
    assert api.state.volume == 50


@pytest.mark.asyncio
async def test_eq_lv2_bands_are_stored_and_returned() -> None:
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", advertise_ip="127.0.0.1")
    mpv = DummyMpv()
    api = HttpApi(cfg, RendererState.from_config(cfg), mpv)
    command = (
        'EQSetLV2Band:{"pluginURI":"http://moddevices.com/plugins/caps/Eq4p",'
        '"EQBand":[{"param_name":"a_freq","value":125.0},'
        '{"param_name":"a_gain","value":0.6},'
        '{"param_name":"a_q","value":1.2}]}'
    )
    saved = await api.execute(ParsedCommand(command), "127.0.0.1")
    assert isinstance(saved, dict)
    assert saved["status"] == "OK"
    assert mpv.peq_bands == [{"freq": 125.0, "gain": 0.6, "q": 1.2, "mode": 1.0}]

    payload = await api.execute(ParsedCommand("EQGetLV2Band:http://moddevices.com/plugins/caps/Eq4p"), "127.0.0.1")
    assert isinstance(payload, dict)
    assert payload["status"] == "OK"
    assert payload["EQStat"] == "On"
    assert payload["Name"] == "Custom"
    assert payload["pluginURI"] == "http://moddevices.com/plugins/caps/Eq4p"
    assert payload["channelMode"] == "Stereo"
    assert payload["EQBandL"] == []
    assert payload["EQBandR"] == []
    bands = payload["EQBand"]
    assert isinstance(bands, list)
    assert len(bands) == 40
    assert {"param_name": "a_gain", "value": 0.6} in bands

    off = await api.execute(ParsedCommand("EQOff"), "127.0.0.1")
    assert off == {"status": "0"}
    assert mpv.peq_bands == []

    on = await api.execute(ParsedCommand("EQChangeFX:http://moddevices.com/plugins/caps/Eq4p"), "127.0.0.1")
    assert on == {"status": "OK"}
    assert api.eq_enabled is True
    assert mpv.peq_bands == [{"freq": 125.0, "gain": 0.6, "q": 1.2, "mode": 1.0}]

    preset_saved = await api.execute(ParsedCommand("EQSave:test"), "127.0.0.1")
    assert preset_saved == {"status": "0"}

    flat = await api.execute(
        ParsedCommand(
            'EQSetLV2Band:{"pluginURI":"http://moddevices.com/plugins/caps/Eq4p",'
            '"EQBand":[{"param_name":"a_gain","value":0.0}]}'
        ),
        "127.0.0.1",
    )
    assert flat == {"status": "OK"}
    assert mpv.peq_bands == []

    restored = await api.execute(
        ParsedCommand('EQv2Load:{"pluginURI":"http://moddevices.com/plugins/caps/Eq4p","Name":"test"}'),
        "127.0.0.1",
    )
    assert isinstance(restored, dict)
    assert restored["status"] == "OK"
    assert restored["Name"] == "test"
    assert restored["pluginURI"] == "http://moddevices.com/plugins/caps/Eq4p"
    assert mpv.peq_bands == [{"freq": 125.0, "gain": 0.6, "q": 1.2, "mode": 1.0}]
    active = await api.execute(ParsedCommand("EQGetLV2Band:"), "127.0.0.1")
    assert isinstance(active, dict)
    assert active["pluginURI"] == "http://moddevices.com/plugins/caps/Eq4p"
    assert active["Name"] == "test"


@pytest.mark.asyncio
async def test_eq_save_and_list_are_ok() -> None:
    api = make_api()
    saved = await api.execute(ParsedCommand("EQSave:test"), "127.0.0.1")
    assert isinstance(saved, dict)
    assert saved["status"] == "0"

    payload = await api.execute(ParsedCommand("EQv2GetList:http://moddevices.com/plugins/caps/Eq4p"), "127.0.0.1")
    assert isinstance(payload, dict)
    assert payload["preset"] == ["Flat", "Bass Boost", "Treble Boost"]
    assert payload["custom"] == []  # Saved on EqNp, so unavailable for Eq4p.

    weird = await api.execute(
        ParsedCommand('EQv2Load:{"pluginURI":"http://moddevices.com/plugins/caps/EqNp","Name":"{Name=test, Type=custom, channelMode=Stereo, UpdateAt=0}"}'),
        "127.0.0.1",
    )
    assert isinstance(weird, dict)
    assert weird["Name"] == "test"

    modern = await api.execute(ParsedCommand("EQv2GetList:http://moddevices.com/plugins/caps/EqNp"), "127.0.0.1")
    assert isinstance(modern, dict)
    assert [item["Name"] for item in modern["preset"]] == ["Flat", "Bass Boost", "Treble Boost"]
    assert modern["custom"] == [{"Name": "test", "Type": "custom", "channelMode": "Stereo", "UpdateAt": "0"}]

    new_list = await api.execute(
        ParsedCommand('EQv2GetNewList:{"EQLevel":1,"pluginURI":"http://moddevices.com/plugins/caps/EqNp"}'),
        "127.0.0.1",
    )
    assert isinstance(new_list, dict)
    assert [item["Name"] for item in new_list["preset"]] == ["Flat", "Bass Boost", "Treble Boost"]


@pytest.mark.asyncio
async def test_modern_eqnp_source_save_and_load_returns_band_info() -> None:
    api = make_api()
    set_payload = (
        'EQSetLV2SourceBand:{"source_name":"wifi","pluginURI":"http://moddevices.com/plugins/caps/EqNp",'
        '"EQBand":[{"param_name":"a_freq","value":30.0},'
        '{"param_name":"a_gain","value":4.0},'
        '{"param_name":"a_q","value":0.7},'
        '{"param_name":"a_mode","value":1.0}]}'
    )
    assert await api.execute(ParsedCommand(set_payload), "127.0.0.1") == {"status": "OK"}

    save = await api.execute(
        ParsedCommand(
            'EQSourceSave:{"EQLevel":2,"source_name":"wifi","channelMode":"Stereo",'
            '"pluginURI":"http://moddevices.com/plugins/caps/EqNp","Name":"Road"}'
        ),
        "127.0.0.1",
    )
    assert save == {"status": "OK"}

    load = await api.execute(
        ParsedCommand(
            'EQv2SourceLoad:{"EQLevel":2,"source_name":"wifi","channelMode":"Stereo",'
            '"pluginURI":"http://moddevices.com/plugins/caps/EqNp","Name":"Road"}'
        ),
        "127.0.0.1",
    )
    assert isinstance(load, dict)
    assert load["status"] == "OK"
    assert load["Name"] == "Road"
    assert load["pluginURI"] == "http://moddevices.com/plugins/caps/EqNp"
    assert load["source_name"] == "wifi"
    assert {"param_name": "a_gain", "value": 4.0} in load["EQBand"]


@pytest.mark.asyncio
async def test_eq_lv2_source_band_json_payload_is_supported() -> None:
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", advertise_ip="127.0.0.1", state_dir=f"/tmp/fake-linkplay-test-{uuid4()}")
    mpv = DummyMpv()
    api = HttpApi(cfg, RendererState.from_config(cfg), mpv)

    command = (
        'EQSetLV2SourceBand:{"source_name":"wifi","pluginURI":"http://moddevices.com/plugins/caps/EqNp",'
        '"EQBand":[{"param_name":"b_mode","value":0},'
        '{"param_name":"b_freq","value":80.0},'
        '{"param_name":"b_gain","value":3.0},'
        '{"param_name":"b_q","value":0.7}]}'
    )
    saved = await api.execute(ParsedCommand(command), "127.0.0.1")
    assert saved == {"status": "OK"}
    assert mpv.peq_bands == [{"freq": 80.0, "gain": 3.0, "q": 0.7, "mode": 0.0}]

    payload = await api.execute(
        ParsedCommand('EQGetLV2SourceBand:{"source_name":"wifi","pluginURI":"http://moddevices.com/plugins/caps/EqNp"}'),
        "127.0.0.1",
    )
    assert isinstance(payload, dict)
    assert payload["status"] == "OK"
    assert payload["source_name"] == "wifi"
    assert payload["pluginURI"] == "http://moddevices.com/plugins/caps/EqNp"


def test_mpv_audio_filter_graph_maps_ten_band_peq_and_balance() -> None:
    mpv = MpvBackend("/tmp/nonexistent", False)
    mpv._peq_bands = [
        {"freq": 31.0, "gain": 1.0, "q": 0.7, "mode": 1.0},
        {"freq": 62.0, "gain": -1.0, "q": 0.8, "mode": 1.0},
        {"freq": 125.0, "gain": 2.0, "q": 0.9, "mode": 1.0},
        {"freq": 250.0, "gain": -2.0, "q": 1.0, "mode": 1.0},
        {"freq": 500.0, "gain": 3.0, "q": 1.1, "mode": 1.0},
        {"freq": 1000.0, "gain": -3.0, "q": 1.2, "mode": 1.0},
        {"freq": 2000.0, "gain": 4.0, "q": 1.3, "mode": 1.0},
        {"freq": 4000.0, "gain": -4.0, "q": 1.4, "mode": 1.0},
        {"freq": 8000.0, "gain": 5.0, "q": 1.5, "mode": 1.0},
        {"freq": 16000.0, "gain": -5.0, "q": 1.6, "mode": 1.0},
    ]
    mpv._channel_balance = 0.25
    graph = mpv._audio_filter_graph()
    assert graph.count("equalizer=") == 10
    assert "equalizer=f=31.000:width_type=q:width=0.700:g=1.000" in graph
    assert "equalizer=f=16000.000:width_type=q:width=1.600:g=-5.000" in graph
    assert "pan=stereo|c0=0.750*c0|c1=1.000*c1" in graph
