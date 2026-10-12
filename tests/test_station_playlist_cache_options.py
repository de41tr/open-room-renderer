"""UNIT/MOCK: bounded station-child policy ownership and native guard contract."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from fake_linkplay.mpv_backend import LIVE_STATION_BACK_BYTES, LIVE_STATION_HISTORY_OPTIONS, MpvBackend
from fake_linkplay.queue import PlaybackQueue, QueueItem
from fake_linkplay.state import PlaybackPhase
from test_station_recovery import api

SOURCE = 'https://radio.example.test/list.m3u'
CHILD = 'https://media.example.test/one.wav'
OTHER = 'https://media.example.test/finite.wav'


def snapshot(entry_id=2, path=CHILD):
    return {'idle-active': False, 'pause': False, 'path': path, 'playlist-path': SOURCE,
            'playback-time': 1., 'track-list': [{'type': 'audio', 'selected': True}],
            'playlist': [{'id': entry_id, 'filename': path, 'current': True, 'playing': True,
                          'playlist-path': SOURCE}]}


def prepare(api):
    api.state.queue = PlaybackQueue(items=[QueueItem(SOURCE, stream_type='station')], index=1)
    api.state.current_url = api.state.backend_original_url = api.state.backend_url = SOURCE
    api.state.intent = 'play'
    api.state.phase = PlaybackPhase.PLAYING
    api.mpv.get_property = AsyncMock(return_value=12)
    api.mpv.snapshot = AsyncMock(return_value=snapshot())
    api.mpv.apply_station_child_history = AsyncMock(return_value=True)


async def apply(api, snap=None):
    async with api._backend_lock:
        await api._apply_station_child_history_locked(api.state.generation, snap or snapshot())


async def test_one_attempt_per_child_reuses_observer_and_preserves_paused_owner(api):
    prepare(api)
    for _ in range(12):
        await api.sync_mpv_state()
    api.mpv.apply_station_child_history.assert_awaited_once_with(0, 2, 12)
    api._accept_playback_command('pause', preserve_native=True)
    api.mpv.snapshot.return_value['pause'] = True
    await api.sync_mpv_state()
    assert api.state.phase == PlaybackPhase.PAUSED
    assert api.mpv.apply_station_child_history.await_count == 1
    api.mpv.snapshot.return_value = snapshot(3, OTHER)
    await api.sync_mpv_state()
    assert api.mpv.apply_station_child_history.await_count == 2
    api.mpv.apply_station_child_history.assert_awaited_with(0, 3, 12)


@pytest.mark.parametrize('kind', ['finite', 'unknown', 'wrong_original', 'wrong_selected', 'stop', 'empty',
                                  'direct', 'nested', 'foreign', 'missing_id', 'bool_id', 'ambiguous',
                                  'inactive', 'foreign_tail', 'idle', 'backend_error', 'phase_error', 'uncertain'])
async def test_unproven_children_never_receive_policy(api, kind):
    prepare(api)
    snap = snapshot()
    item = api.state.queue.current
    if kind == 'finite': item.duration = 1
    elif kind == 'unknown': item.stream_type = ''
    elif kind == 'wrong_original': api.state.backend_original_url = OTHER
    elif kind == 'wrong_selected': api.state.current_url = OTHER
    elif kind == 'stop': api.state.intent = 'stop'
    elif kind == 'empty': api.state.queue = PlaybackQueue()
    elif kind == 'direct': snap['path'] = SOURCE
    elif kind == 'nested': snap['playlist-path'] = snap['playlist'][0]['playlist-path'] = CHILD
    elif kind == 'foreign': snap['playlist-path'] = OTHER
    elif kind == 'missing_id': del snap['playlist'][0]['id']
    elif kind == 'bool_id': snap['playlist'][0]['id'] = True
    elif kind == 'ambiguous': snap['playlist'].append({**snap['playlist'][0], 'id': 3})
    elif kind == 'inactive': snap['playlist'][0]['playing'] = False
    elif kind == 'foreign_tail': snap['playlist'].append({'id': 9, 'filename': OTHER})
    elif kind == 'idle': snap['idle-active'] = True
    elif kind == 'backend_error': snap['backend-error'] = True
    elif kind == 'phase_error': api.state.phase = PlaybackPhase.ERROR
    elif kind == 'uncertain': api._native_output_uncertain = True
    await apply(api, snap)
    api.mpv.get_property.assert_not_awaited()
    api.mpv.apply_station_child_history.assert_not_awaited()


@pytest.mark.parametrize('when', ['pid', 'fresh_snapshot', 'backend_lock'])
@pytest.mark.parametrize('change', ['pause', 'stop', 'new_item', 'new_queue', 'new_backend'])
async def test_delayed_observation_cannot_write_after_owner_changes(api, when, change):
    prepare(api)
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(result):
        entered.set()
        await release.wait()
        return result
    async def pid(*a): return await blocked(12)
    async def fresh(): return await blocked(snapshot())
    if when == 'pid': api.mpv.get_property.side_effect = pid
    elif when == 'fresh_snapshot': api.mpv.snapshot.side_effect = fresh
    if when == 'backend_lock':
        await api._backend_lock.acquire()
        task = asyncio.create_task(api.sync_mpv_state())
        await asyncio.sleep(0)
    else:
        task = asyncio.create_task(apply(api))
        await entered.wait()
    if change in {'pause', 'stop'}: api._accept_playback_command(change, preserve_native=True)
    elif change == 'new_item': api.state.queue.items[0] = QueueItem(OTHER, duration=30)
    elif change == 'new_queue':
        api._accept_playback_command('play')
        api.state.queue = PlaybackQueue(items=[QueueItem(SOURCE, stream_type='station')], index=1)
    elif change == 'new_backend': api.state.backend_url = OTHER
    release.set()
    if when == 'backend_lock': api._backend_lock.release()
    await task
    api.mpv.apply_station_child_history.assert_not_awaited()
    if change == 'pause' and when != 'backend_lock':
        # Superseded before native mutation must not permanently suppress the
        # same child's first attempt for the newer pause generation.
        api.mpv.get_property.side_effect = api.mpv.snapshot.side_effect = None
        await apply(api)
        api.mpv.apply_station_child_history.assert_awaited_once()


@pytest.mark.parametrize('change', ['id', 'path', 'parent', 'nested', 'core'])
async def test_fresh_native_ownership_proof_is_required(api, change):
    prepare(api)
    fresh = snapshot()
    if change == 'id': fresh['playlist'][0]['id'] = 4
    elif change == 'path': fresh['path'] = fresh['playlist'][0]['filename'] = OTHER
    elif change == 'parent': fresh['playlist-path'] = OTHER
    elif change == 'nested': fresh['playlist'][0]['playlist-path'] = OTHER
    elif change == 'core': api.mpv.get_property.return_value = None
    api.mpv.snapshot.return_value = fresh
    await apply(api)
    api.mpv.apply_station_child_history.assert_not_awaited()


@pytest.mark.parametrize('result', [False, OSError('unavailable')])
async def test_unconfirmed_optimization_is_visible_bounded_and_does_not_stop_audio(api, caplog, result):
    prepare(api)
    if result is False: api.mpv.apply_station_child_history.return_value = False
    else: api.mpv.apply_station_child_history.side_effect = result
    for _ in range(15): await apply(api)
    api.mpv.apply_station_child_history.assert_awaited_once()
    assert 'outcome=unconfirmed' in caplog.text
    assert caplog.text.count('outcome=unconfirmed') == 1
    assert api.state.phase == PlaybackPhase.PLAYING
    assert api.state.backend_url == SOURCE


@pytest.mark.parametrize('readback', [f'{LIVE_STATION_BACK_BYTES}|no|yes|yes',
    f'{LIVE_STATION_BACK_BYTES}|yes|yes|yes', f'{LIVE_STATION_BACK_BYTES}|no|yes|no',
    f'{LIVE_STATION_BACK_BYTES}|no|no|yes', '0|no|yes|yes', '', None])
async def test_native_guards_and_complete_readback_are_exact(readback):
    backend = MpvBackend('unused', auto_spawn=False)
    backend.command = AsyncMock(side_effect=[{'error': 'success'}, {'error': 'success'},
                                            {'error': 'success', 'data': readback}])
    assert await backend.apply_station_child_history(1, 21, 123) is (
        readback == f'{LIVE_STATION_BACK_BYTES}|no|yes|yes')
    calls = [call.args[0] for call in backend.command.await_args_list]
    for command, option in zip(calls[:2], ('demuxer-donate-buffer', 'demuxer-max-back-bytes')):
        assert command[:2] == ['expand-properties', 'set']
        assert command[2] == ('${?=pid==123:${?=playlist/1/id==21:${?=playlist/1/current==yes:'
                              '${?=playlist/1/playing==yes:file-local-options/' + option + '}}}}')
        assert command[3] == LIVE_STATION_HISTORY_OPTIONS[option]
    assert calls[2][0] == 'expand-text'
    for option in LIVE_STATION_HISTORY_OPTIONS:
        assert 'options/' + option in calls[2][1]
        assert 'option-info/' + option + '/set-locally' in calls[2][1]


async def test_partial_policy_is_unconfirmed_without_readback_or_unguarded_retry():
    backend = MpvBackend('unused', auto_spawn=False)
    backend.command = AsyncMock(side_effect=[{'error': 'success'}, {'error': 'error running command'}])
    assert not await backend.apply_station_child_history(0, 2, 12)
    assert backend.command.await_count == 2
    assert all(call.args[0][:2] == ['expand-properties', 'set'] for call in backend.command.await_args_list)


async def test_failed_native_guard_does_not_readback_or_retry():
    backend = MpvBackend('unused', auto_spawn=False)
    backend.command = AsyncMock(return_value={'error': 'property unavailable'})
    assert not await backend.apply_station_child_history(0, 2, 12)
    backend.command.assert_awaited_once()


@pytest.mark.parametrize('identity', [(True, 2, 3), (0, -1, 3), (0, 2, '3')])
async def test_guard_rejects_non_numeric_or_negative_identity(identity):
    backend = MpvBackend('unused', auto_spawn=False)
    backend.command = AsyncMock()
    with pytest.raises(ValueError): await backend.apply_station_child_history(*identity)
    backend.command.assert_not_awaited()


async def test_new_admitted_load_resets_attempt_even_when_native_id_is_reused(api):
    prepare(api)
    await apply(api)
    api.mpv.apply_station_child_history.assert_awaited_once()
    api.mpv.loadfile = AsyncMock(return_value={'error': 'success'})
    # Same canonical queue object, URL and child ID, but a new load may have
    # restarted the backend. It must get a new bounded application attempt.
    await api._play_url(SOURCE)
    assert api.mpv.apply_station_child_history.await_count == 2
    for _ in range(5): await apply(api)
    assert api.mpv.apply_station_child_history.await_count == 2


async def test_stop_releases_retained_queue_attempt(api):
    prepare(api)
    await apply(api)
    assert api._station_child_history_attempt is not None
    api._accept_playback_command('stop')
    assert api._station_child_history_attempt is None
    await apply(api)
    api.mpv.apply_station_child_history.assert_awaited_once()


async def test_backend_error_during_fresh_proof_warns_once_without_writing(api, caplog):
    prepare(api)
    api.mpv.snapshot.return_value = {'backend-error': True}
    await apply(api)
    api.mpv.snapshot.return_value = snapshot()
    await apply(api)
    api.mpv.apply_station_child_history.assert_not_awaited()
    assert caplog.text.count('outcome=unconfirmed') == 1


async def test_observer_refreshes_snapshot_after_delayed_policy_transaction(api):
    prepare(api)
    initial = snapshot()
    later = {**snapshot(), 'playback-time': 3., 'pause': True}
    api.mpv.snapshot.side_effect = [initial, initial, later]
    await api.sync_mpv_state()
    assert api.state.curpos == 3
    assert api.state.phase == PlaybackPhase.PAUSED
    assert api.mpv.snapshot.await_count == 3


async def test_policy_timeout_is_bounded_releases_lock_and_does_not_retry(api, caplog):
    prepare(api)
    async def stalled(*args):
        await asyncio.Event().wait()
    api.mpv.apply_station_child_history.side_effect = stalled
    await asyncio.wait_for(apply(api), timeout=3)
    assert not api._backend_lock.locked()
    assert api.state.phase == PlaybackPhase.PLAYING
    assert 'outcome=unconfirmed' in caplog.text
    await apply(api)
    api.mpv.apply_station_child_history.assert_awaited_once()


async def test_policy_cancellation_propagates_and_releases_lock(api):
    prepare(api)
    entered = asyncio.Event()
    async def stalled(*args):
        entered.set()
        await asyncio.Event().wait()
    api.mpv.apply_station_child_history.side_effect = stalled
    task = asyncio.create_task(apply(api))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not api._backend_lock.locked()
