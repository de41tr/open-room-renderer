"""UNIT: repeat policy is pure and its wire dialect is explicit."""
import pytest

from fake_linkplay.queue import PlaybackQueue, QueueError, parse_queue_context, serialize_queue_context
from fake_linkplay.queue_policy import natural_successor, parse_loop_mode, parse_play_mode, play_mode_name
from fake_linkplay.state import RendererState
from fake_linkplay.config import Config


@pytest.mark.parametrize("mode, expected", [(4, [2, 3, None]), (1, [1, 2, 3]), (0, [2, 3, 1])])
def test_natural_successor_for_each_position(mode, expected):
    assert [natural_successor(index, 3, mode) for index in (1, 2, 3)] == expected
    assert natural_successor(0, 0, mode) is None
    assert natural_successor(1, 1, mode) == (None if mode == 4 else 1)


@pytest.mark.parametrize("mode, name", [(4, "NORMAL"), (1, "REPEAT_ONE"), (0, "REPEAT_ALL")])
def test_modes_round_trip_without_a_second_mutable_state(mode, name):
    assert parse_loop_mode(str(mode)) == parse_play_mode(name) == mode
    assert play_mode_name(mode) == name
    queue = parse_queue_context(f"<PlayList><LoopMode>{mode}</LoopMode></PlayList>")
    assert parse_queue_context(serialize_queue_context(queue)).loop == mode
    state = RendererState.from_config(Config())
    state.queue = queue
    assert state.loop == str(mode)
    with pytest.raises(AttributeError):
        state.loop = "4"


def test_missing_mode_preserves_sequential_default():
    assert PlaybackQueue().loop == parse_queue_context("<PlayList/>").loop == 4


@pytest.mark.parametrize("value", [2, 3, True, False, None, "", "5", "01", "00", "4.0", "-1", " 4", "4 ", "4:0", "NORMAL", "SHUFFLE", "RANDOM", [], {}])
def test_numeric_adapter_rejects_aliases_coercions_and_unsupported_modes(value):
    with pytest.raises(ValueError):
        parse_loop_mode(value)


@pytest.mark.parametrize("value", [0, 1, 4, True, None, "", "0", "4", "normal", "REPEAT", "REPEAT_TRACK", "RANDOM", " NORMAL", [], {}])
def test_standard_adapter_is_separate_and_strict(value):
    with pytest.raises(ValueError):
        parse_play_mode(value)


@pytest.mark.parametrize("value", [True, False, 0.0, 1.0, 4.0, "4", 5, -1, None])
def test_mutable_queue_rejects_unsupported_or_noninteger_mode(value):
    with pytest.raises(QueueError, match="loop mode"):
        PlaybackQueue(loop=value)
