"""Bounded local queue traversal and explicit public-client mode adapters.

Numeric modes follow the pywiim HTTP/PlayQueue dialect. AVTransport SHUFFLE
selects shuffle-once; its readback intentionally cannot express repeat-all.
This module does not mutate a queue, perform I/O, or own playback state.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

NORMAL = 4
REPEAT_ONE = 1
REPEAT_ALL = 0
SHUFFLE_ONCE = 3
SHUFFLE_REPEAT_ALL = 2
SUPPORTED_MODES = (NORMAL, REPEAT_ONE, REPEAT_ALL, SHUFFLE_ONCE, SHUFFLE_REPEAT_ALL)
PLAY_MODES = {"NORMAL": NORMAL, "REPEAT_ONE": REPEAT_ONE, "REPEAT_ALL": REPEAT_ALL,
              "SHUFFLE": SHUFFLE_ONCE}


class ShuffleRandom(Protocol):
    """Only permutation generation is needed from the injected random source."""

    def shuffle(self, values: list[int]) -> None: ...


@dataclass(frozen=True)
class ShuffleTraversal:
    """Current cycle plus at most one previous cycle, using canonical indices."""

    path: tuple[int, ...]
    cursor: int
    cycle_start: int = 0
    exhausted: bool = False

    @property
    def index(self) -> int:
        return self.path[self.cursor] if self.path else 0


def is_shuffle(mode: int) -> bool:
    return mode in (SHUFFLE_ONCE, SHUFFLE_REPEAT_ALL)


def parse_loop_mode(value: str) -> int:
    """Accept only supported numeric wire values, without coercion."""
    if type(value) is not str or value not in {str(mode) for mode in SUPPORTED_MODES}:
        raise ValueError("Unsupported queue loop mode")
    return int(value)


def parse_play_mode(value: str) -> int:
    """Accept standard AVTransport names; SHUFFLE explicitly chooses mode 3."""
    if type(value) is not str or value not in PLAY_MODES:
        raise ValueError("Unsupported play mode")
    return PLAY_MODES[value]


def play_mode_name(mode: int) -> str:
    """Project combined shuffle-repeat into AVTransport's lossy SHUFFLE name."""
    if type(mode) is not int or mode not in SUPPORTED_MODES:
        raise ValueError("Unsupported queue loop mode")
    if is_shuffle(mode):
        return "SHUFFLE"
    return next(name for name, value in PLAY_MODES.items() if value == mode)


def natural_successor(index: int, count: int, mode: int) -> int | None:
    """Choose a sequential/repeat EOF index; shuffle requires its own history."""
    play_mode_name(mode)
    if is_shuffle(mode):
        raise ValueError("Shuffle requires traversal history")
    if count == 0:
        return None
    if mode == REPEAT_ONE:
        return index
    if index < count:
        return index + 1
    return 1 if mode == REPEAT_ALL else None


def shuffle_cycle(index: int, count: int, rng: ShuffleRandom) -> ShuffleTraversal:
    """Start an immutable cycle anchored at the current one-based selection."""
    if count == 0 and index == 0:
        return ShuffleTraversal((), -1)
    if not 1 <= index <= count:
        raise ValueError("Invalid shuffle anchor")
    remaining = [position for position in range(1, count + 1) if position != index]
    if len(remaining) > 1:
        rng.shuffle(remaining)
    return ShuffleTraversal((index, *remaining), 0)


def shuffle_available(traversal: ShuffleTraversal, mode: int, delta: int = 1) -> bool:
    """Non-consuming peek, including at a repeat boundary: never draw RNG."""
    if not traversal.path:
        return False
    if delta == -1:
        return traversal.cursor > 0
    if delta != 1:
        raise ValueError("Shuffle direction must be next or previous")
    return traversal.cursor + 1 < len(traversal.path) or mode == SHUFFLE_REPEAT_ALL


def shuffle_step(traversal: ShuffleTraversal, mode: int, delta: int,
                 rng: ShuffleRandom) -> ShuffleTraversal | None:
    """Commit a selection in history, drawing only for a new repeat cycle.

    The caller must finish asynchronous ownership/Stop checks before calling.
    A failed subsequent load still retains this selection for an explicit retry.
    """
    if not shuffle_available(traversal, mode, delta):
        return None
    cursor = traversal.cursor + delta
    if cursor < len(traversal.path):
        return replace(traversal, cursor=cursor, exhausted=False)
    cycle = list(range(1, len(traversal.path) - traversal.cycle_start + 1))
    if len(cycle) > 1:
        rng.shuffle(cycle)
        if cycle[0] == traversal.index:
            cycle[0], cycle[1] = cycle[1], cycle[0]
    previous = traversal.path[traversal.cycle_start:]
    return ShuffleTraversal((*previous, *cycle), len(previous), len(previous))


def shuffle_exhausted(traversal: ShuffleTraversal, mode: int) -> ShuffleTraversal:
    """Record natural shuffle-once completion, without treating Stop as EOF."""
    if mode == SHUFFLE_ONCE and traversal.path and not shuffle_available(traversal, mode):
        return replace(traversal, exhausted=True)
    return traversal
