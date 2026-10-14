"""Default installations announce themselves to WiiM-style clients."""
from pathlib import Path

import pytest
import yaml

from fake_linkplay.config import Config, load_config, read_startup_config
from fake_linkplay.mdns import CustomMdnsProtocol, DNS_CLASS_IN, DNS_TYPE_PTR, DnsQuestion
from fake_linkplay.ssdp import response_packet


def test_core_defaults_enable_wiim_ssdp_and_mdns_without_privileged_ports() -> None:
    cfg = Config()
    assert cfg.wiim_home_profile and cfg.enable_ssdp and cfg.enable_mdns
    assert cfg.http_api_port == 8080 and cfg.upnp_port == 59152
    assert cfg.mirror_http_on_upnp_port and not cfg.mirror_port_80
    assert cfg.manufacturer == 'Independent' and cfg.model_name == 'Linux Room Renderer'


def test_minimal_config_publishes_stable_app_identity(tmp_path: Path) -> None:
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump({'state_dir': str(tmp_path / 'state'), 'advertise_ip': '192.0.2.10'}))
    cfg = load_config(path)
    original = path.read_bytes()
    assert cfg.upnp_uuid.startswith('FF')
    restarted = load_config(path)
    assert restarted.upnp_uuid == cfg.upnp_uuid
    assert restarted.linkplay_instance_name == cfg.linkplay_instance_name
    assert restarted.mdns_hostname == cfg.mdns_hostname and restarted.wiim_mac == cfg.wiim_mac
    assert restarted.wiim_home_profile and restarted.enable_mdns and restarted.upnp_port == 59152
    assert path.read_bytes() == original
    assert (tmp_path / 'state/uuid.wiim').read_text() == (tmp_path / 'state/uuid').read_text()
    packet = response_packet(cfg, 'urn:schemas-upnp-org:device:MediaRenderer:1').decode()
    assert 'ServiceName: ##hexStr##' in packet and 'Bootid.upnp.org:' in packet
    mdns = CustomMdnsProtocol(cfg)._response_for(0, DnsQuestion('_linkplay._tcp.local.', DNS_TYPE_PTR, DNS_CLASS_IN))
    assert mdns is not None and f'uuid=uuid:{cfg.upnp_uuid}'.encode() in mdns


def test_explicit_neutral_discovery_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump({'state_dir': str(tmp_path / 'state'), 'wiim_home_profile': False,
                                   'enable_mdns': False, 'upnp_port': 8080, 'advertise_ip': '192.0.2.10'}))
    cfg = load_config(path)
    assert not cfg.wiim_home_profile and not cfg.enable_mdns
    assert '-' in cfg.upnp_uuid
    packet = response_packet(cfg, 'urn:schemas-upnp-org:device:MediaRenderer:1').decode()
    assert 'ServiceName:' not in packet


@pytest.mark.parametrize('name', ['configs/config.example.yaml', 'examples/config.example.yaml'])
def test_public_default_templates_match_runtime_discovery(name: str) -> None:
    path = Path(__file__).resolve().parents[1] / name
    cfg = read_startup_config(path)
    assert cfg.wiim_home_profile and cfg.enable_ssdp and cfg.enable_mdns
    assert cfg.http_api_port == 8080 and cfg.upnp_port == 59152
