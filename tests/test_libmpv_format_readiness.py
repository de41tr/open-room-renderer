"""Deterministic fixture-only readiness checks; no native/backend evidence."""

from types import SimpleNamespace
from typing import Any

import pytest

import test_libmpv_integration as integration
from test_libmpv_formats import _decoder_progress


_EXPECTED = "http://127.0.0.1:12345/tone.aac"
_PREVIOUS = "http://127.0.0.1:12345/tone.mp3"


def _frame(*, path: str | tuple[str, str] = _EXPECTED, codec: str = "aac",
           position: Any = 0.0, selected: bool = True, ao: str = "null") -> dict[str, Any]:
    return {"path": path, "track-list": [{"type": "audio", "selected": selected, "codec": codec}],
            "playback-time": position, "current-ao": ao}


class _ScriptedDecoder:
    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self.frames = frames
        self.index = 0
        self.path_reads = 0
        self.elapsed = 0.0

    def read_property(self, name: str) -> Any:
        value = self.frames[min(self.index, len(self.frames) - 1)][name]
        if name == "path" and isinstance(value, tuple):
            value = value[min(self.path_reads, len(value) - 1)]
            self.path_reads += 1
        return value

    async def sleep(self, seconds: float) -> None:
        self.elapsed += seconds
        self.index += 1
        self.path_reads = 0


def _decoder(monkeypatch: pytest.MonkeyPatch, frames: list[dict[str, Any]]) -> _ScriptedDecoder:
    decoder = _ScriptedDecoder(frames)
    # Exercise the real polling/deadline helper using a deterministic clock.
    monkeypatch.setattr(integration, "asyncio", SimpleNamespace(
        get_running_loop=lambda: SimpleNamespace(time=lambda: decoder.elapsed),
        sleep=decoder.sleep,
    ))
    return decoder


@pytest.mark.parametrize("unready", [
    _frame(path=_PREVIOUS, codec="mp3", position=9.0),
    _frame(path=(_EXPECTED, _PREVIOUS), position=9.0),
    _frame(codec="mp3", position=9.0),
    _frame(selected=False),
    _frame(ao="alsa"),
    *[_frame(position=value) for value in (None, float("nan"), float("inf"), -1.0, True)],
], ids=["stale-selected-audio", "source-changes-during-sample", "stale-codec", "unselected",
        "wrong-output", "missing-position", "nan-position", "infinite-position", "negative-position",
        "boolean-position"])
async def test_decoder_progress_waits_for_expected_source_sample(monkeypatch, unready):
    decoder = _decoder(monkeypatch, [unready, _frame(), _frame(position=0.5)])
    evidence = await _decoder_progress(decoder, _EXPECTED, "aac")
    assert evidence == {"path": _EXPECTED, "codec": "aac", "before": 0.0, "after": 0.5, "ao": "null"}
    assert decoder.index == 2


@pytest.mark.parametrize("foreign", [
    _frame(path=_PREVIOUS, position=9.0),
    _frame(path=(_EXPECTED, _PREVIOUS), position=9.0),
    _frame(codec="mp3", position=9.0),
], ids=["other-source", "source-changes-during-progress", "other-codec"])
async def test_decoder_progress_rejects_foreign_progress(monkeypatch, foreign):
    decoder = _decoder(monkeypatch, [_frame(), foreign, _frame(position=0.5)])
    evidence = await _decoder_progress(decoder, _EXPECTED, "aac")
    assert evidence["before"] == 0.0 and evidence["after"] == 0.5
    assert decoder.index == 2


@pytest.mark.parametrize("frame, description", [
    (_frame(path=_PREVIOUS, position=9.0), "selected decoded audio"),
    (_frame(), "advancing decoded radio audio"),
], ids=["permanently-wrong-source", "no-progress"])
async def test_decoder_progress_preserves_absolute_timeout(monkeypatch, frame, description):
    decoder = _decoder(monkeypatch, [frame])
    with pytest.raises(AssertionError, match=f"Timed out waiting for {description}"):
        await _decoder_progress(decoder, _EXPECTED, "aac")
    assert decoder.elapsed == pytest.approx(3.0, abs=0.025)
