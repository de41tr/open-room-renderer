"""Deterministic guard coverage for native playlist acceptance readiness."""
from copy import deepcopy

import pytest

from fake_linkplay.mpv_backend import BackendError
from test_libmpv_station_playlist_cache import _child_playback_time


CHILD = 'http://127.0.0.1/child.wav'
IDENTITY = (0, 7, 11)
UNAVAILABLE = BackendError('libmpv -10: property unavailable')


class ClockBackend:
    def __init__(self, values, mutate=None):
        self.values = iter(values)
        self.value = UNAVAILABLE
        self.mutate = mutate
        self.properties = {
            'path': CHILD, 'pid': 11, 'playlist-pos': 0,
            'playlist': [{'filename': CHILD, 'id': 7, 'current': True, 'playing': True}],
            'idle-active': False, 'core-idle': True, 'eof-reached': False,
            'track-list': [], 'current-ao': 'null',
        }

    def read_property(self, name):
        if name != 'playback-time':
            return deepcopy(self.properties[name])
        self.value = next(self.values, self.value)
        if self.mutate:
            self.mutate(self.properties)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def owner():
    pass


async def test_owned_child_clock_waits_for_readiness_and_accepts_zero():
    backend = ClockBackend([UNAVAILABLE, UNAVAILABLE, -.35, 0.0, .1, .3])
    assert await _child_playback_time(backend, CHILD, IDENTITY, owner) == 0.0
    assert await _child_playback_time(backend, CHILD, IDENTITY, owner, after=0.0) == .3


async def test_owned_child_clock_unavailable_is_bounded_and_diagnostic():
    with pytest.raises(AssertionError, match='Timed out waiting for owned native child clock') as failure:
        await _child_playback_time(ClockBackend([UNAVAILABLE]), CHILD, IDENTITY, owner, timeout=.04)
    text = str(failure.value)
    assert 'property unavailable' in text and CHILD in text and 'track-list' in text
    assert "'entry':" in text and "'pid': 11" in text


async def test_owned_child_clock_requires_decoded_progress():
    with pytest.raises(AssertionError, match='Timed out'):
        await _child_playback_time(ClockBackend([0.0, .1, .2]), CHILD, IDENTITY, owner, after=0.0, timeout=.08)


@pytest.mark.parametrize('value', [None, True, '0', float('nan'), float('inf')])
async def test_owned_child_clock_rejects_invalid_native_values(value):
    with pytest.raises(AssertionError):
        await _child_playback_time(ClockBackend([value]), CHILD, IDENTITY, owner)


async def test_owned_child_clock_does_not_swallow_other_native_errors():
    with pytest.raises(BackendError, match='-8'):
        await _child_playback_time(ClockBackend([BackendError('libmpv -8: property not found')]), CHILD, IDENTITY, owner)


@pytest.mark.parametrize('mutation', [
    lambda p: p.update(path='http://127.0.0.1/replacement.wav'),
    lambda p: p.update(pid=12),
    lambda p: p.update({'playlist-pos': 1}),
    lambda p: p['playlist'][0].update(id=8),
    lambda p: p['playlist'][0].update(current=False),
    lambda p: p['playlist'][0].update(playing=False),
    lambda p: p['playlist'][0].update(filename='http://127.0.0.1/replacement.wav'),
    lambda p: p.update(playlist=[]),
])
@pytest.mark.parametrize('value', [UNAVAILABLE, .5])
async def test_owned_child_clock_rejects_lost_child_during_native_sample(mutation, value):
    with pytest.raises(AssertionError, match='Native child ownership lost'):
        await _child_playback_time(ClockBackend([value], mutation), CHILD, IDENTITY, owner)


async def test_owned_child_clock_rechecks_renderer_ownership_while_waiting():
    checks = 0
    def assert_owner():
        nonlocal checks
        checks += 1
        assert checks < 3, 'Renderer ownership lost'
    with pytest.raises(AssertionError, match='Renderer ownership lost'):
        await _child_playback_time(ClockBackend([UNAVAILABLE, .5]), CHILD, IDENTITY, assert_owner)


async def test_owned_child_clock_permanent_negative_preroll_is_not_ready():
    with pytest.raises(AssertionError, match='Timed out'):
        await _child_playback_time(ClockBackend([-.35]), CHILD, IDENTITY, owner, timeout=.04)
