"""Configured favorites: actual local client/HTTP, deterministic fake audio."""
from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from fake_linkplay.capabilities import configured_presets
from fake_linkplay.config import Config, ExperimentalSource, PodcastPreset, load_config
from fake_linkplay.http_api import HttpApi, ParsedCommand
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.state import RendererState

@dataclass
class FakeAudioBackend:
    """No optional client dependency and no media/network access."""
    url: str = ""
    idle: bool = True
    fail_operation: str | None = None
    calls: list[tuple[str, object]] = field(default_factory=list)

    async def loadfile(self, url, options=None):
        self.calls.append(("loadfile", url))
        if self.fail_operation == "loadfile":
            raise BackendError("Synthetic load failure")
        self.url, self.idle = url, False

    async def stop(self):
        self.calls.append(("stop", None))
        self.url, self.idle = "", True

    async def snapshot(self):
        return {"path": self.url, "idle-active": self.idle, "pause": False,
                "playback-time": 0, "duration": 120 if self.url else None,
                "track-list": [] if self.idle else [{"type": "audio", "selected": True}]}



FIRST = "https://media.example.test/one.ogg?item=1&format=ogg"
SECOND = "https://media.example.test/two.wav?item=2&format=wav"


def configured(tmp_path, **changes):
    values = dict(state_dir=str(tmp_path), advertise_ip="127.0.0.1",
                  podcast_presets=[PodcastPreset("Été & 日本語", FIRST), PodcastPreset("Second", SECOND)])
    values.update(changes)
    return Config(**values)


@asynccontextmanager
async def running(cfg):
    backend = FakeAudioBackend()
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)
    async with TestServer(api.app()) as server, ClientSession() as session:
        yield api, backend, server, session


@pytest.mark.asyncio
async def test_real_client_lists_and_plays_two_distinct_configured_slots(tmp_path):
    pywiim = pytest.importorskip("pywiim")
    async with running(configured(tmp_path)) as (api, backend, server, session):
        client = pywiim.WiiMClient(host="127.0.0.1", port=server.port, protocol="http",
                                 timeout=5, session=session,
                                 capabilities={"retry_count": 1, "supports_metadata": False,
                                               "supports_presets": True})
        try:
            await client.get_status()  # Endpoint discovery must not replay a mutation.
            assert await client.get_max_preset_slots() == 2
            presets = await client.get_presets()
            assert [(p["number"], p["name"], p["url"]) for p in presets] == [
                (1, "Été & 日本語", FIRST), (2, "Second", SECOND)]
            await client.play_preset(1)
            assert backend.url == FIRST
            assert api.state.title == "Été & 日本語"
            await client.play_preset(2, index=1)
            assert backend.url == SECOND
            assert api.state.queue.current.id == "preset:2"
            assert api.state.plicount == api.state.plicurr == 1
            assert api.state.status == "play"
            assert [value for method, value in backend.calls if method == "loadfile"] == [FIRST, SECOND]
        finally:
            await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["0", "-1", "3", "abc", "1:2", "1:0", "1:1:1", "1.0", " 1", "01"])
async def test_invalid_slot_or_unsupported_playlist_index_does_not_change_owner(tmp_path, suffix):
    async with running(configured(tmp_path)) as (api, backend, server, session):
        await api.execute(ParsedCommand("MCUKeyShortClick:1"), "127.0.0.1")
        old = (api.state.generation, deepcopy(api.state.queue), list(backend.calls))
        async with session.get(server.make_url("/httpapi.asp"),
                               params={"command": "MCUKeyShortClick:" + suffix}) as response:
            assert response.status == 400
        assert (api.state.generation, api.state.queue, backend.calls) == old
        assert backend.url == FIRST


@pytest.mark.asyncio
async def test_failed_backend_preset_does_not_claim_playing(tmp_path):
    async with running(configured(tmp_path)) as (api, backend, server, session):
        backend.fail_operation = "loadfile"
        async with session.get(server.make_url("/httpapi.asp"),
                               params={"command": "MCUKeyShortClick:2"}) as response:
            assert response.status == 502
        assert api.state.transport_status == "ERROR_OCCURRED"
        assert api.state.status != "play"
        assert api.state.current_url == SECOND
        assert api.state.last_error == "Backend control failed"
        assert api.state.backend_url == api.state.backend_original_url == ""
        assert backend.idle and backend.url == ""
        assert backend.calls == [("loadfile", SECOND), ("stop", None)]


@pytest.mark.asyncio
async def test_no_presets_advertises_zero_and_has_no_phantom_selection(tmp_path):
    async with running(configured(tmp_path, podcast_presets=[])) as (api, backend, server, session):
        assert api.state.as_status_ex("127.0.0.1")["preset_key"] == "0"
        listing = await api.execute(ParsedCommand("getPresetInfo"), "127.0.0.1")
        assert listing == {"preset_num": "0", "preset_list": [], "presets": []}
        with pytest.raises(ValueError):
            await api.execute(ParsedCommand("MCUKeyShortClick:1"), "127.0.0.1")
        assert not backend.calls


@pytest.mark.asyncio
async def test_favorites_keep_configured_order_across_restart_and_rename(tmp_path):
    config = tmp_path / "renderer.yaml"
    config.write_text(f'''state_dir: {tmp_path / "state"}\nadvertise_ip: 127.0.0.1\npodcast_presets:\n  - name: First\n    url: "{FIRST}"\n  - name: Second\n    url: "{SECOND}"\n''')
    cfg = load_config(config)
    async with running(cfg) as (api, backend, server, session):
        await api.execute(ParsedCommand("setDeviceName:Été 日本語"), "127.0.0.1")
        await api.execute(ParsedCommand("MCUKeyShortClick:2"), "127.0.0.1")
    restored = load_config(config)
    assert restored.uuid == cfg.uuid
    assert restored.friendly_name == "Été 日本語"
    assert configured_presets(restored) == configured_presets(cfg)
    async with running(restored) as (api, backend, server, session):
        await api.execute(ParsedCommand("MCUKeyShortClick:2:1"), "127.0.0.1")
        assert backend.url == SECOND


def test_experimental_favorites_are_opt_in_and_follow_regular_slots(tmp_path):
    sources = [ExperimentalSource("demo", "Demo", url=SECOND),
               ExperimentalSource("disabled", "Disabled", url=FIRST, enabled=False)]
    cfg = configured(tmp_path, experimental_sources=sources)
    assert len(configured_presets(cfg)) == 2
    cfg.source_experiment_variant = "preset"
    records = configured_presets(cfg)
    assert [p["number"] for p in records] == [1, 2, 3]
    assert records[2]["index"] == "3"
    assert records[2]["experimental"] is True
    assert RendererState.from_config(cfg).as_status_ex("127.0.0.1")["preset_key"] == "3"


def test_invalid_configured_entries_are_not_advertised_as_selectable_slots(tmp_path):
    cfg = configured(tmp_path, podcast_presets=[PodcastPreset("Blank", " "),
        PodcastPreset("Null", None), PodcastPreset("Invalid XML", "bad\x00target"),
        PodcastPreset("Valid", SECOND)], source_experiment_variant="preset",
        experimental_sources=[ExperimentalSource("empty", "Empty"),
                              ExperimentalSource("valid", "Valid experiment", url=FIRST)])
    records = configured_presets(cfg)
    assert [(p["number"], p["url"]) for p in records] == [(1, SECOND), (2, FIRST)]
    assert RendererState.from_config(cfg).as_status_ex("127.0.0.1")["preset_key"] == "2"


@pytest.mark.asyncio
async def test_appended_experimental_slot_selects_its_actual_target(tmp_path):
    cfg = configured(tmp_path, source_experiment_variant="preset", experimental_sources=[
        ExperimentalSource("empty", "Empty"),
        ExperimentalSource("disabled", "Disabled", url=FIRST, enabled=False),
        ExperimentalSource("chosen", "Chosen", url=SECOND)])
    async with running(cfg) as (api, backend, server, session):
        await api.execute(ParsedCommand("MCUKeyShortClick:3"), "127.0.0.1")
        assert backend.url == SECOND
        assert api.state.title == "Chosen"
        assert api.state.queue.current.id == "preset:3"
        assert api.state.album_art_uri.endswith("/icons/chosen.svg")


@pytest.mark.asyncio
async def test_failed_replacement_keeps_new_selected_target_but_reports_error(tmp_path):
    async with running(configured(tmp_path)) as (api, backend, server, session):
        await api.execute(ParsedCommand("MCUKeyShortClick:1"), "127.0.0.1")
        backend.fail_operation = "loadfile"
        with pytest.raises(BackendError):
            await api.execute(ParsedCommand("MCUKeyShortClick:2"), "127.0.0.1")
        assert api.state.current_url == SECOND
        assert api.state.transport_status == "ERROR_OCCURRED"
        assert api.state.status != "play"
        # A load error cannot prove that the requested mutation was unapplied.
        # Checked cleanup silences it without reverting the selected preset.
        assert backend.idle and backend.url == ""
        assert api.state.backend_url == api.state.backend_original_url == ""
        assert api.state.last_error == "Backend control failed"
        assert backend.calls == [("loadfile", FIRST), ("stop", None), ("loadfile", SECOND), ("stop", None)]


@pytest.mark.asyncio
async def test_delayed_preset_resolution_cannot_override_newer_selection(tmp_path):
    import asyncio
    async with running(configured(tmp_path)) as (api, backend, server, session):
        entered, release = asyncio.Event(), asyncio.Event()
        original = api._resolve_playback_url
        async def resolve(url):
            if url == FIRST:
                entered.set()
                await release.wait()
            return await original(url)
        api._resolve_playback_url = resolve
        old = asyncio.create_task(api.execute(ParsedCommand("MCUKeyShortClick:1"), "127.0.0.1"))
        await asyncio.wait_for(entered.wait(), 2)
        try:
            await api.execute(ParsedCommand("MCUKeyShortClick:2"), "127.0.0.1")
            release.set()
            with pytest.raises(BackendError):
                await asyncio.wait_for(old, 2)
            assert api.state.current_url == backend.url == SECOND
            assert api.state.queue.current.id == "preset:2"
            assert api.state.status == "play"
        finally:
            release.set()
            if not old.done():
                old.cancel()
            await asyncio.gather(old, return_exceptions=True)
