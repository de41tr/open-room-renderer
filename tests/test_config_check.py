"""Explicit startup-input checks only, with synthetic files and forbidden effects."""
from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
from unittest.mock import Mock

import pytest
import yaml

from fake_linkplay import cli, config

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = "https://user:synthetic-secret@media.example.test/audio?token=private-value"
SUCCESS = ("Startup input checks passed. Runtime settings are not fully validated; "
           "saved state, ports, backend and audio devices were not checked.\n")
ARGUMENT_ERROR = "Invalid client command arguments. Run open-room-renderer --help.\n"


def forbid_effects(monkeypatch, path):
    """Permit only reading the selected YAML; stop service/resource entry points."""
    original_open = Path.open
    opened = []

    def only_config(self, mode="r", *args, **kwargs):
        assert self == path and mode == "r", "unexpected filesystem access"
        opened.append(self)
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", only_config)
    forbidden = Mock(side_effect=AssertionError("service or state side effect reached"))
    for name in ("_load_config_state", "load_or_create_uuid", "load_persistent_runtime_state",
                 "_read_state", "ensure_dir", "detect_lan_ip"):
        monkeypatch.setattr(config, name, forbidden)
    monkeypatch.setattr(cli.RendererState, "from_config", forbidden)
    for name in ("setup_logging", "MpvBackend", "HttpApi", "MdnsAdvertiser", "SsdpServer",
                 "serve", "load_config", "load_startup_config"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(cli.web, "AppRunner", forbidden)
    monkeypatch.setattr(cli.web, "TCPSite", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    return opened, forbidden


def test_check_valid_input_has_no_identity_state_network_or_backend_effects(tmp_path, monkeypatch, capsys):
    path = tmp_path / "renderer.yaml"
    state = tmp_path / "absent-state"
    path.write_text(yaml.safe_dump({
        "state_dir": str(state), "auto_spawn_mpv": True, "enable_mdns": True,
        "enable_ssdp": True, "friendly_name": PRIVATE,
        "podcast_presets": [{"name": "Synthetic", "url": PRIVATE}],
    }))
    opened, forbidden = forbid_effects(monkeypatch, path)
    assert cli.main(["config", "check", "--config", str(path)]) == 0
    assert capsys.readouterr() == (SUCCESS, "")
    assert opened == [path]
    forbidden.assert_not_called()
    assert not state.exists()


def test_check_does_not_claim_later_validation_or_apply_persisted_overrides(tmp_path, monkeypatch, capsys):
    path = tmp_path / "renderer.yaml"
    state = tmp_path / "saved-state"
    state.mkdir()
    saved = state / "active_dsp.json"
    saved.write_text("deliberately unreadable-as-state synthetic contents")
    before = saved.read_bytes()
    # Existing startup-input checks deliberately do not validate these settings;
    # final balance depends on persisted precedence during real service startup.
    path.write_text(yaml.safe_dump({
        "state_dir": str(state), "channel_balance": PRIVATE, "http_api_port": PRIVATE,
    }))
    with monkeypatch.context() as guarded:
        opened, forbidden = forbid_effects(guarded, path)
        assert cli.main(["config", "check", "--config", str(path)]) == 0
        assert capsys.readouterr() == (SUCCESS, "")
        assert opened == [path]
        forbidden.assert_not_called()
    assert saved.read_bytes() == before
    assert sorted(item.name for item in state.iterdir()) == [saved.name]


def test_check_bad_input_has_private_diagnostics_before_any_effect(tmp_path, monkeypatch, capsys):
    path = tmp_path / "private-filename.yaml"
    for content, message in (
        (f'friendly_name: "{PRIVATE}\n', config.CONFIG_YAML_ERROR),
        (yaml.safe_dump({"default_volume": PRIVATE}), config.CONFIG_STRUCTURE_ERROR),
    ):
        path.write_text(content)
        with monkeypatch.context() as guarded:
            opened, forbidden = forbid_effects(guarded, path)
            assert cli.main(["config", "check", "--config", str(path)]) == 2
            assert capsys.readouterr() == ("", f"Configuration check failed: {message}\n")
            assert opened == [path]
            forbidden.assert_not_called()
        assert path.read_text() == content


def test_check_real_process_requires_file_and_never_falls_back(tmp_path):
    path = tmp_path / "synthetic-private-filename.yaml"
    path.write_text("{}\n")
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
    args = [sys.executable, "-m", "fake_linkplay.cli", "config", "check", "--config", str(path)]
    result = subprocess.run(args, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    assert (result.returncode, result.stdout, result.stderr) == (0, SUCCESS, "")
    path.unlink()
    result = subprocess.run(args, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    assert (result.returncode, result.stdout, result.stderr) == (
        2, "", "Configuration check failed: Configuration file not found. Check --config or ORR_CONFIG.\n",
    )
    assert list(tmp_path.iterdir()) == []


def test_check_argument_failures_never_echo_supplied_values(monkeypatch, capsys):
    forbidden = Mock(side_effect=AssertionError("argument error reached execution"))
    monkeypatch.setattr(cli, "run_config_check", forbidden)
    for arguments in (
        ["config", "check"],
        ["config", PRIVATE],
        ["config", "check", "--config", PRIVATE, "--unexpected", PRIVATE],
        ["--unexpected", PRIVATE, "config", "check", "--config", PRIVATE],
    ):
        with pytest.raises(SystemExit) as caught:
            cli.main(arguments)
        assert caught.value.code == 2
        assert capsys.readouterr() == ("", ARGUMENT_ERROR)
    forbidden.assert_not_called()


def test_config_named_legacy_values_keep_existing_dispatch_and_argument_errors(monkeypatch, capsys):
    request = Mock(return_value=0)
    monkeypatch.setattr(cli, "api_get", request)
    assert cli.main(["play", "config"]) == 0
    request.assert_called_once_with("127.0.0.1", 8080, "setPlayerCmd:play:config")
    for arguments in (["volume", "config"], ["status", "--host", "config", "--port", "invalid"]):
        with pytest.raises(SystemExit) as caught:
            cli.main(arguments)
        assert caught.value.code == 2
        error = capsys.readouterr().err
        assert "invalid int value" in error
        assert error != ARGUMENT_ERROR


def test_check_help_explains_scope_and_required_config(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["config", "check", "--help"])
    assert caught.value.code == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert "--config CONFIG" in output.out
    assert "no default fallback" in output.out
    assert "channel balance" in output.out
    assert "saved-state overrides" in " ".join(output.out.split())
