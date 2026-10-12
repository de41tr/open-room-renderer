"""UNIT: bounded, immutable shuffle traversal over canonical queue positions."""
from dataclasses import FrozenInstanceError
import random

import pytest

from fake_linkplay.queue import PlaybackQueue, QueueItem, parse_queue_context, serialize_queue_context
from fake_linkplay.queue_policy import (SHUFFLE_ONCE, SHUFFLE_REPEAT_ALL, natural_successor,
    parse_loop_mode, parse_play_mode, play_mode_name, shuffle_available, shuffle_cycle,
    shuffle_exhausted, shuffle_step)


class ReverseRandom:
    def __init__(self):
        self.calls = 0

    def shuffle(self, values):
        self.calls += 1
        values.reverse()


@pytest.mark.parametrize("count,index", [(0, 0), (1, 1), (2, 2), (4, 2), (1000, 500)])
def test_cycle_is_complete_unique_anchored_and_seeded(count, index):
    state = shuffle_cycle(index, count, random.Random(817))
    assert state == shuffle_cycle(index, count, random.Random(817))
    assert state.index == index and sorted(state.path) == list(range(1, count + 1))
    assert len(set(state.path)) == count
    with pytest.raises(FrozenInstanceError):
        state.cursor = 4


@pytest.mark.parametrize("mode", [SHUFFLE_ONCE, SHUFFLE_REPEAT_ALL])
def test_wire_projection_is_explicitly_lossy_and_history_is_not_serialized(mode):
    queue = PlaybackQueue(items=[QueueItem("https://media.example.test/duplicate.wav") for _ in range(4)],
                          index=2, loop=mode)
    queue._shuffle = shuffle_cycle(2, 4, ReverseRandom())
    assert queue._shuffle.path == (2, 4, 3, 1)
    assert parse_loop_mode(str(mode)) == mode
    assert play_mode_name(mode) == "SHUFFLE" and parse_play_mode("SHUFFLE") == 3
    parsed = parse_queue_context(serialize_queue_context(queue))
    assert parsed.loop == mode and parsed.index == 2 and parsed._shuffle is None
    assert [item.url for item in parsed.items] == [item.url for item in queue.items]
    with pytest.raises(ValueError, match="history"):
        natural_successor(2, 4, mode)


def test_once_exhaustion_is_recorded_only_after_natural_completion():
    rng = ReverseRandom()
    state = shuffle_cycle(2, 4, rng)
    for index in (4, 3, 1):
        state = shuffle_step(state, 3, 1, rng)
        assert state.index == index and not state.exhausted
    for _ in range(5):
        assert not shuffle_available(state, 3)
        assert shuffle_available(state, 2)
    assert rng.calls == 1
    assert shuffle_step(state, 3, 1, rng) is None
    completed = shuffle_exhausted(state, 3)
    assert completed.exhausted and not state.exhausted
    previous = shuffle_step(completed, 3, -1, rng)
    assert previous.index == 3 and not previous.exhausted
    assert shuffle_step(previous, 3, 1, rng).index == 1
    assert rng.calls == 1


@pytest.mark.parametrize("count", [1, 2, 4, 1000])
def test_repeat_cycles_have_bounded_history_and_previous_next_replays(count):
    rng = random.Random(73)
    state = shuffle_cycle(1, count, rng)
    for cycle in range(6):
        selected = [state.index]
        for _ in range(count - 1):
            state = shuffle_step(state, 2, 1, rng)
            selected.append(state.index)
        assert sorted(selected) == list(range(1, count + 1))
        last = state.index
        state = shuffle_step(state, 2, 1, rng)
        assert len(state.path) <= 2 * count
        assert state.index != last or count == 1
        before, rng_state = state, rng.getstate()
        previous = shuffle_step(state, 2, -1, rng)
        assert previous.index == last
        state = shuffle_step(previous, 2, 1, rng)
        assert state == before and rng.getstate() == rng_state
    for _ in range(state.cursor):
        state = shuffle_step(state, 2, -1, rng)
    assert shuffle_step(state, 2, -1, rng) is None


def test_empty_and_singleton_peeks_never_draw_rng():
    rng = ReverseRandom()
    for count in (0, 1):
        state = shuffle_cycle(count, count, rng)
        assert not shuffle_available(state, 3)
        assert shuffle_available(state, 2) == bool(count)
        assert not shuffle_available(state, 2, -1)
    assert rng.calls == 0
