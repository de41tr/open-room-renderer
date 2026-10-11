"""UNIT tests of confirmed local gain persistence; no audio or account data."""
import json
from pathlib import Path

import pytest

from fake_linkplay.config import Config, load_config
from fake_linkplay.persistence import atomic_write_text, load_gain, save_gain
from fake_linkplay.state import RendererState


def test_gain_round_trip_initializes_renderer_state(tmp_path):
    save_gain(tmp_path / 'state', 25, True)
    config_path = tmp_path / 'renderer.yaml'
    config_path.write_text(f'state_dir: {tmp_path / "state"}\ndefault_volume: 50\n')
    cfg = load_config(config_path)
    state = RendererState.from_config(cfg)
    assert (cfg.default_volume, cfg.default_mute) == (25, True)
    assert (state.volume, state.mute) == (25, True)
    assert json.loads((tmp_path / 'state/gain.json').read_text()) == {
        'version': 1, 'volume': 25, 'mute': True,
    }


def test_missing_gain_keeps_explicit_config(tmp_path):
    assert load_gain(tmp_path) is None
    cfg = Config(default_volume=12, default_mute=True)
    assert (RendererState.from_config(cfg).volume, RendererState.from_config(cfg).mute) == (12, True)


@pytest.mark.parametrize('value', [0, 1, 'false', None, []])
def test_config_mute_requires_boolean(value):
    with pytest.raises(ValueError):
        Config(default_mute=value)


@pytest.mark.parametrize('payload', [
    {}, [], None, {'version': 2, 'volume': 25, 'mute': True},
    {'version': True, 'volume': 25, 'mute': True},
    {'version': 1, 'volume': True, 'mute': False},
    {'version': 1, 'volume': 101, 'mute': False},
    {'version': 1, 'volume': -1, 'mute': False},
    {'version': 1, 'volume': 25.0, 'mute': False},
    {'version': 1, 'volume': 25, 'mute': 'false'},
    {'version': 1, 'volume': 25, 'mute': False, 'unknown': 'unsupported'},
])
def test_invalid_gain_fails_closed_without_rewriting(tmp_path, payload):
    path = tmp_path / 'gain.json'
    before = json.dumps(payload)
    path.write_text(before)
    with pytest.raises(ValueError):
        load_gain(tmp_path)
    assert path.read_text() == before


@pytest.mark.parametrize('encoded', [b'', b'{', b'\xff', b' ' * 4097])
def test_corrupt_or_oversized_gain_fails_closed(tmp_path, encoded):
    (tmp_path / 'gain.json').write_bytes(encoded)
    with pytest.raises(ValueError):
        load_gain(tmp_path)


def test_atomic_replace_failure_preserves_previous_gain(tmp_path, monkeypatch):
    save_gain(tmp_path, 25, True)
    path = tmp_path / 'gain.json'
    before = path.read_bytes()
    def fail_replace(*args):
        raise OSError('Synthetic replacement failure')
    monkeypatch.setattr('fake_linkplay.persistence.os.replace', fail_replace)
    with pytest.raises(OSError):
        save_gain(tmp_path, 50, False)
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_atomic_write_fsync_failure_preserves_previous_file(tmp_path, monkeypatch):
    path = tmp_path / 'setting'
    path.write_text('old')
    def fail_sync(*args):
        raise OSError('Synthetic synchronization failure')
    monkeypatch.setattr('fake_linkplay.persistence.os.fsync', fail_sync)
    with pytest.raises(OSError):
        atomic_write_text(path, 'new')
    assert path.read_text() == 'old'
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('volume,mute', [(-1, False), (101, True), (True, False), (25, 0), (float('nan'), False)])
def test_invalid_gain_cannot_create_state(tmp_path, volume, mute):
    with pytest.raises(ValueError):
        save_gain(tmp_path, volume, mute)
    assert not list(tmp_path.iterdir())
