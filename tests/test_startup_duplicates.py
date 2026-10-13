"""Root-only duplicate startup settings, using synthetic input and no sockets."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import socket
import subprocess
from unittest.mock import Mock

import pytest
import yaml

from fake_linkplay import cli, config

PRIVATE = "synthetic-private-setting"
PRIVATE_URL = f"https://user:password@media.example.test/audio?token={PRIVATE}"
DUPLICATE_ERROR = (
    "Configuration contains duplicate top-level settings. "
    "Keep only one explicit value for each setting."
)

DUPLICATE_DOCUMENTS = [
    pytest.param("mpv_audio_device: alsa/plughw:CARD=Synthetic,DEV=0\n"
                 "mpv_audio_device: null\n", id="output-cleared"),
    pytest.param("mpv_audio_device: null\n'mpv_audio_device': auto\n", id="quoted-output"),
    pytest.param(f"state_dir: /{PRIVATE}/first\nstate_dir: /{PRIVATE}/second\n", id="state"),
    pytest.param("http_api_port: 8080\nhttp_api_port: 8090\n", id="http-port"),
    pytest.param('upnp_port: 8080\n"upnp_\\x70ort": 8090\n', id="escaped-port"),
    pytest.param("{http_api_port: 8080, http_api_port: 8090}\n", id="flow-mapping"),
    pytest.param("default_volume: 37\n!!str default_volume: 37\n", id="same-value"),
    pytest.param("&setting mpv_audio_device: auto\n*setting: null\n", id="aliased-key"),
    pytest.param("mpv_audio_device: auto\n!!value mpv_audio_device: null\n", id="value-tag-later"),
    pytest.param("!!value mpv_audio_device: auto\nmpv_audio_device: null\n", id="value-tag-first"),
    pytest.param("!!value mpv_audio_device: auto\n!!value mpv_audio_device: null\n", id="value-tag-both"),
    pytest.param("<<: {http_api_port: 8070}\nhttp_api_port: 8080\n"
                 "http_api_port: 8090\n", id="explicit-after-merge"),
    pytest.param(f"'{PRIVATE}': '{PRIVATE_URL}'\n'{PRIVATE}': null\n", id="private-key-value"),
]


def forbid_effects(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("startup construction or effect reached"))
    for name in ("_config_from_mapping", "_load_config_state", "load_or_create_uuid",
                 "load_persistent_runtime_state", "_read_state", "ensure_dir", "detect_lan_ip"):
        monkeypatch.setattr(config, name, forbidden)
    monkeypatch.setattr(config.uuidlib, "uuid4", forbidden)
    monkeypatch.setattr(cli.RendererState, "from_config", forbidden)
    for name in ("setup_logging", "MpvBackend", "HttpApi", "MdnsAdvertiser", "SsdpServer"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(cli.web, "AppRunner", forbidden)
    monkeypatch.setattr(cli.web, "TCPSite", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    return forbidden


@pytest.mark.parametrize("document", DUPLICATE_DOCUMENTS)
def test_captured_duplicates_fail_privately_before_construction_or_path_access(monkeypatch, document):
    forbidden = forbid_effects(monkeypatch)
    monkeypatch.setattr(Path, "read_text", forbidden)
    with pytest.raises(config.StartupConfigError) as caught:
        config.read_startup_config(f"/{PRIVATE}/absent.yaml", captured_text=document)
    assert str(caught.value) == DUPLICATE_ERROR
    assert caught.value.__suppress_context__ or caught.value.__context__ is None
    forbidden.assert_not_called()


def run_before_first_await(coroutine):
    """Exercise real serve validation without creating an event loop/self-pipe."""
    try:
        coroutine.send(None)
    finally:
        coroutine.close()
    raise AssertionError("startup unexpectedly reached an await")


@pytest.mark.parametrize("document", DUPLICATE_DOCUMENTS)
@pytest.mark.parametrize("command,prefix", [
    (["config", "check"], "Configuration check failed"),
    (["serve"], "Startup error"),
])
def test_cli_duplicate_failures_are_private_and_leave_state_untouched(
    tmp_path, monkeypatch, capsys, document, command, prefix,
):
    state = tmp_path / "saved-state"
    state.mkdir()
    saved = state / "gain.json"
    saved.write_bytes(b"synthetic saved bytes must never be read or changed")
    path = tmp_path / f"{PRIVATE}.yaml"
    path.write_text(document)
    original_open = Path.open
    reads = []

    def only_config(self, mode="r", *args, **kwargs):
        assert self == path and mode == "r", "unexpected filesystem access"
        reads.append(self)
        return original_open(self, mode, *args, **kwargs)

    with monkeypatch.context() as guarded:
        forbidden = forbid_effects(guarded)
        guarded.setattr(Path, "open", only_config)
        guarded.setattr(cli.asyncio, "run", run_before_first_await)
        assert cli.main([*command, "--config", str(path)]) == 2
        assert capsys.readouterr() == ("", f"{prefix}: {DUPLICATE_ERROR}\n")
        assert reads == [path]
        forbidden.assert_not_called()
    assert path.read_text() == document
    assert saved.read_bytes() == b"synthetic saved bytes must never be read or changed"
    assert sorted(item.name for item in tmp_path.iterdir()) == sorted([path.name, state.name])
    assert list(state.iterdir()) == [saved]


@pytest.mark.parametrize("document", [
    "", "# only a comment\n", "null\n", "{}\n",
    "mpv_audio_device: null\nhttp_api_port: '8080'\n",
    "default_volume: '37'\nlog_level: not-a-logging-level\n",
    "default_volume: 37.8\nlog_level: debug\n",
    "http_api_port: &port 8087\nupnp_port: *port\n",
    "&setting friendly_name: Synthetic renderer\n",
    "!!value mpv_audio_device: auto\n",
    "<<: {!!value http_api_port: 8070}\n!!value http_api_port: 8080\n",
    "<<: &defaults {http_api_port: 8070, upnp_port: 8070}\nhttp_api_port: 8080\n",
    "http_api_port: 8080\n<<: {http_api_port: 8070, upnp_port: 8070}\n",
    "<<: [{http_api_port: 8070}, {http_api_port: 8090, upnp_port: 8090}]\n",
    "<<: [&defaults {http_api_port: 8070}, *defaults]\nhttp_api_port: 8080\n",
    # The duplicate policy is deliberately root-only, before merge flattening.
    "<<: {http_api_port: 8070, http_api_port: 8080}\n",
    "<<: {http_api_port: 8070}\n<<: {upnp_port: 8090}\n",
    "podcast_presets: [{name: First, name: Second, url: 'https://media.example.test/a'}]\n",
    "experimental_sources: [{id: synthetic, name: First, name: Second}]\n",
    "podcast_presets: [&preset {name: Synthetic, url: 'https://media.example.test/a'}, *preset]\n",
])
def test_unique_settings_keep_safe_loader_defaults_coercions_aliases_and_merges(monkeypatch, document):
    expected = config._config_from_mapping(yaml.safe_load(document) or {})
    forbidden = Mock(side_effect=AssertionError("captured input touched paths or state"))
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(config, "_load_config_state", forbidden)
    actual = config.read_startup_config("absent.yaml", captured_text=document)
    assert asdict(actual) == asdict(expected)
    forbidden.assert_not_called()


def test_recursive_value_alias_is_preserved_without_recursive_duplicate_checks():
    cfg = config.read_startup_config("absent.yaml", captured_text="friendly_name: &name [*name]\n")
    assert cfg.friendly_name[0] is cfg.friendly_name


@pytest.mark.parametrize("document,field,expected", [
    ("mpv_audio_device: auto\nmpv_audio_device: null\n", "mpv_audio_device", None),
    ("state_dir: /synthetic/first\nstate_dir: /synthetic/second\n", "state_dir", "/synthetic/second"),
    ("http_api_port: 8080\nhttp_api_port: 8090\n", "http_api_port", 8090),
])
def test_public_loader_keeps_legacy_duplicate_last_value_contract(tmp_path, monkeypatch, document, field, expected):
    path = tmp_path / "legacy.yaml"
    path.write_text(document)
    # Inspect the Config passed to the unchanged persisted-state boundary.
    hydrate = Mock(side_effect=lambda cfg: cfg)
    monkeypatch.setattr(config, "_load_config_state", hydrate)
    actual = config.load_config(path)
    assert getattr(actual, field) == expected
    hydrate.assert_called_once_with(actual)


@pytest.mark.parametrize("document", [
    "mpv_audio_device: auto\nmpv_audio_device: !!int synthetic-private-value\n",
    "mpv_audio_device: !!bool synthetic-private-value\nmpv_audio_device: null\n",
])
def test_duplicates_are_checked_before_scalar_value_construction(document):
    with pytest.raises(config.StartupConfigError) as caught:
        config.parse_startup_yaml(document)
    assert str(caught.value) == DUPLICATE_ERROR
