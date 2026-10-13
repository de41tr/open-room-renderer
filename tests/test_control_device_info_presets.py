"""Regression coverage for clients reading preset_key from SOAP Status JSON."""
import json
from xml.etree import ElementTree as ET

import pytest

from fake_linkplay.config import Config, PodcastPreset
from fake_linkplay.state import RendererState
from fake_linkplay.upnp_description import get_control_device_info_response_fields, soap_response


def status_from_soap(cfg, state=None):
    fields = get_control_device_info_response_fields(cfg, '192.0.2.10', state)
    root = ET.fromstring(soap_response('GetControlDeviceInfo', 'rendercontrol1', fields))
    return json.loads(root.find('.//Status').text)


@pytest.mark.parametrize('count', [0, 2])
def test_configured_preset_count_is_available_without_runtime_state(count):
    cfg = Config(uuid='00000000-0000-4000-8000-000000000001',
                 podcast_presets=[PodcastPreset(name=f'Favorite {index}',
                                url=f'https://media.example.test/{index}.wav')
                                  for index in range(count)])
    assert status_from_soap(cfg)['preset_key'] == str(count)


def test_managed_favorites_count_matches_http_status_and_tracks_changes():
    cfg = Config(uuid='00000000-0000-4000-8000-000000000001',
                 wiim_home_profile=True,
                 podcast_presets=[PodcastPreset(name='Configured', url='https://media.example.test/config.wav')])
    state = RendererState.from_config(cfg)
    favorites = [{'name': 'Saved favorite', 'url': 'https://media.example.test/saved.wav'}]
    state.preset_view = lambda: list(favorites)
    for count in (1, 2, 0):
        favorites[:] = [{'name': f'Saved {index}', 'url': f'https://media.example.test/{index}.wav'}
                        for index in range(count)]
        soap_status = status_from_soap(cfg, state)
        assert soap_status['preset_key'] == state.as_status_ex('192.0.2.10')['preset_key'] == str(count)
        assert soap_status['uuid'] == cfg.upnp_uuid
