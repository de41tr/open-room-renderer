"""Synthetic, local-only identity/discovery contract checks."""

import json
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from fake_linkplay.config import Config, load_config, load_or_create_uuid
from fake_linkplay.mdns import CustomMdnsProtocol, DNS_CLASS_IN, DNS_TYPE_PTR, DnsQuestion, MdnsAdvertiser
from fake_linkplay.ssdp import SsdpServer, response_packet
from fake_linkplay.state import RendererState
from fake_linkplay.upnp_description import control_device_info_json, description_xml


NATIVE_UUID = "00000000-0000-4000-8000-000000000001"
COMPAT_UUID = "FF0000000000000040008000000000000001"


def config_file(tmp_path: Path, **values: object) -> Path:
    path = tmp_path / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"state_dir": str(tmp_path / "state"), **values}))
    return path


def test_builtin_identity_is_neutral() -> None:
    cfg = Config()
    assert cfg.friendly_name == "Open Room Renderer"
    assert cfg.manufacturer == "Independent"
    assert cfg.model_name == "Linux Room Renderer"
    assert cfg.model_number == "alpha-0.1"
    assert cfg.mpv_socket == "/tmp/open-room-renderer-mpv.sock"
    assert not cfg.wiim_home_profile


def test_new_neutral_install_retains_standard_uuid_entropy(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("fake_linkplay.config.uuidlib.uuid4", lambda: UUID(NATIVE_UUID))
    path = config_file(tmp_path)
    cfg = load_config(path)
    assert cfg.uuid == NATIVE_UUID
    assert cfg.upnp_uuid == NATIVE_UUID
    assert load_config(path).uuid == cfg.uuid
    assert (tmp_path / "state" / "uuid").read_text().strip() == NATIVE_UUID


def test_new_compat_install_has_independent_ff_identity_and_restarts(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("fake_linkplay.config.uuidlib.uuid4", lambda: UUID(NATIVE_UUID))
    path = config_file(tmp_path, wiim_home_profile=True)
    cfg = load_config(path)
    assert cfg.upnp_uuid == COMPAT_UUID
    assert len(cfg.upnp_uuid) == 36
    assert cfg.uuid == COMPAT_UUID
    assert cfg.upnp_uuid[4:] == UUID(NATIVE_UUID).hex.upper()
    assert (tmp_path / "state" / "uuid").read_text().strip() == COMPAT_UUID
    restarted = load_config(path)
    assert restarted.upnp_uuid == cfg.upnp_uuid
    assert restarted.wiim_mac == cfg.wiim_mac
    assert restarted.linkplay_instance_name == cfg.linkplay_instance_name
    assert restarted.mdns_hostname == cfg.mdns_hostname


@pytest.mark.parametrize("profile", [False, True])
def test_two_installations_have_distinct_discovery_identity(tmp_path, profile) -> None:
    first = load_config(config_file(tmp_path / "first", wiim_home_profile=profile))
    second = load_config(config_file(tmp_path / "second", wiim_home_profile=profile))
    assert first.upnp_uuid != second.upnp_uuid
    assert first.wiim_mac != second.wiim_mac
    assert first.mdns_hostname != second.mdns_hostname
    assert first.linkplay_instance_name != second.linkplay_instance_name
    assert MdnsAdvertiser(first)._server_name() != MdnsAdvertiser(second)._server_name()


@pytest.mark.parametrize("persisted", [NATIVE_UUID, COMPAT_UUID])
def test_existing_persisted_identity_is_not_migrated(tmp_path, persisted) -> None:
    path = config_file(tmp_path, wiim_home_profile=True)
    state = tmp_path / "state"
    state.mkdir()
    saved = f" \nUuId: {persisted.lower()} \n"
    (state / "uuid").write_text(saved)
    cfg = load_config(path)
    expected = persisted.replace("-", "").upper().ljust(36, "0")
    assert cfg.upnp_uuid == expected
    assert load_config(path).upnp_uuid == expected
    assert (state / "uuid").read_text() == saved


def test_explicit_configured_identity_wins_without_rewriting_saved_id(tmp_path) -> None:
    path = config_file(tmp_path, uuid=NATIVE_UUID, wiim_home_profile=True, discovery_uuid=f" uUiD: {COMPAT_UUID.lower()} ")
    state = tmp_path / "state"
    state.mkdir()
    (state / "uuid").write_text("00000000-0000-4000-8000-000000000002\n")
    cfg = load_config(path)
    assert cfg.uuid == NATIVE_UUID
    assert cfg.discovery_uuid == COMPAT_UUID
    assert cfg.upnp_uuid == COMPAT_UUID
    assert (state / "uuid").read_text() == "00000000-0000-4000-8000-000000000002\n"


def test_empty_persisted_identity_fails_without_silent_replacement(tmp_path) -> None:
    (tmp_path / "uuid").write_text(" \n")
    with pytest.raises(ValueError, match="Persisted UUID is empty"):
        load_or_create_uuid(tmp_path, wiim_home_profile=True)
    assert (tmp_path / "uuid").read_text() == " \n"


def test_legacy_shared_padding_is_not_used_as_unique_suffix_or_mac() -> None:
    first = Config(uuid="10000000-0000-4000-8000-000000000000", wiim_home_profile=True)
    second = Config(uuid="20000000-0000-4000-8000-000000000000", wiim_home_profile=True)
    assert first.wiim_uuid[-10:] == second.wiim_uuid[-10:]
    assert first.linkplay_instance_name != second.linkplay_instance_name
    assert first.wiim_mac != second.wiim_mac
    assert first.mdns_hostname != second.mdns_hostname
    first_octet = int(first.wiim_mac.split(":")[0], 16)
    assert first_octet & 0x03 == 0x02  # locally administered and unicast


@pytest.mark.parametrize("profile", [False, True])
def test_uuid_normalization_agrees_across_discovery_and_status(profile) -> None:
    cfg = Config(
        uuid=f" \tUuId: {NATIVE_UUID.upper()}\n",
        discovery_uuid=f" uUiD: {COMPAT_UUID.lower()} \n",
        wiim_home_profile=profile,
        advertise_ip="192.0.2.10",
    )
    expected = COMPAT_UUID if profile else NATIVE_UUID
    udn = f"uuid:{expected}"
    assert cfg.upnp_uuid == expected
    assert f"<UDN>{udn}</UDN>" in description_xml(cfg)
    for payload in (RendererState.from_config(cfg).as_status_ex("192.0.2.10"), json.loads(control_device_info_json(cfg, "192.0.2.10"))):
        assert payload["uuid"] == expected
        assert payload["upnp_uuid"] == udn
    server = SsdpServer(cfg)
    assert server._targets_for_search(udn.swapcase()) == (udn,)
    assert f"USN: {udn}\r\n" in response_packet(cfg, udn).decode()
    properties = list(MdnsAdvertiser(cfg)._services())[0][3]
    assert properties["uuid"] == (udn if profile else expected)
    if profile:
        packet = CustomMdnsProtocol(cfg)._response_for(0, DnsQuestion("_linkplay._tcp.local.", DNS_TYPE_PTR, DNS_CLASS_IN))
        assert packet is not None
        assert f"uuid={udn}".encode() in packet


def test_persisted_rename_changes_name_without_identity(tmp_path) -> None:
    path = config_file(tmp_path, wiim_home_profile=True)
    cfg = load_config(path)
    (tmp_path / "state" / "device_name").write_text("Küche 🔊\n")
    renamed = load_config(path)
    assert renamed.friendly_name == "Küche 🔊"
    assert renamed.linkplay_instance_name == "Küche 🔊"
    assert renamed.upnp_uuid == cfg.upnp_uuid
    assert renamed.wiim_mac == cfg.wiim_mac
    assert renamed.mdns_hostname == cfg.mdns_hostname


def test_explicit_instance_and_device_metadata_are_preserved() -> None:
    cfg = Config(uuid=NATIVE_UUID, manufacturer="Example maker", model_name="Test renderer", mdns_instance_name="My own room", wiim_home_profile=True)
    assert cfg.manufacturer == "Example maker"
    assert cfg.model_name == "Test renderer"
    assert cfg.linkplay_instance_name == "My own room"
