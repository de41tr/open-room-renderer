from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tools.acceptance_pywiim import PywiimAcceptance


@pytest.fixture
def adapter() -> PywiimAcceptance:
    return PywiimAcceptance("192.0.2.1", 80, "Synthetic Renderer", "https://example.test/audio.mp3", 15)


@pytest.mark.parametrize("field", ["play_status", "status", "state", "player_state"])
@pytest.mark.parametrize("state", ["play", "pause", "stop"])
@pytest.mark.parametrize("action, expected", [("play", "play"), ("pause", "pause"), ("resume", "play"), ("stop", "stop")])
async def test_verify_exact_transport_state(adapter: PywiimAcceptance, field: str, state: str, action: str, expected: str) -> None:
    adapter.device = SimpleNamespace(get_player_status=AsyncMock(return_value={field: state}))

    assert await adapter._verify_state(action) is (state == expected)


@pytest.mark.parametrize("method_name", ["get_player_status", "get_status", "send_command"])
@pytest.mark.parametrize("action, state", [("play", "play"), ("pause", "pause"), ("resume", "play"), ("stop", "stop")])
async def test_verify_status_apis(adapter: PywiimAcceptance, method_name: str, action: str, state: str) -> None:
    field = "status" if method_name == "send_command" else "play_status"
    method = AsyncMock(return_value={field: state})
    adapter.device = SimpleNamespace(**{method_name: method})

    assert await adapter._verify_state(action)
    if method_name == "send_command":
        method.assert_awaited_once_with("getPlayerStatus")
    else:
        method.assert_awaited_once_with()


@pytest.mark.parametrize("action", ["play", "resume", "pause", "stop"])
async def test_metadata_cannot_supply_expected_state(adapter: PywiimAcceptance, action: str) -> None:
    adapter.device = SimpleNamespace(
        get_player_status=AsyncMock(return_value={"play_status": "buffering", "title": "play pause stop"})
    )

    assert not await adapter._verify_state(action)


@pytest.mark.parametrize(
    "status",
    [
        {"play_status": "stop"},
        {"play_status": "playing"},
        {"play_status": "not playing"},
        {"play_status": "play error"},
        {"play_status": None},
        {"play_status": True},
        {"play_status": ["play"]},
        {"play_status": {"state": "play"}},
        {"title": "play", "url": "https://example.test/play.mp3"},
        {"metadata": {"play_status": "play"}},
        {"play_status": "play", "status": "stop"},
        {},
        None,
        "play",
        '{"play_status": "play"}',
        [{"play_status": "play"}],
    ],
)
async def test_verify_play_rejects_missing_invalid_or_conflicting_state(adapter: PywiimAcceptance, status: object) -> None:
    adapter.device = SimpleNamespace(get_player_status=AsyncMock(return_value=status))

    assert not await adapter._verify_state("play")


async def test_verify_state_is_case_insensitive(adapter: PywiimAcceptance) -> None:
    adapter.device = SimpleNamespace(get_player_status=AsyncMock(return_value={"play_status": "PLAY"}))

    assert await adapter._verify_state("play")


async def test_consistent_transport_fields_match(adapter: PywiimAcceptance) -> None:
    adapter.device = SimpleNamespace(get_player_status=AsyncMock(return_value={"play_status": "play", "status": "play"}))

    assert await adapter._verify_state("play")


async def test_verify_state_falls_back_after_request_error(adapter: PywiimAcceptance) -> None:
    adapter.device = SimpleNamespace(
        get_player_status=AsyncMock(side_effect=RuntimeError("Synthetic request failure")),
        get_status=AsyncMock(return_value={"play_status": "pause"}),
    )

    assert await adapter._verify_state("pause")
    adapter.device.get_status.assert_awaited_once_with()


async def test_mismatched_player_state_does_not_fall_back(adapter: PywiimAcceptance) -> None:
    adapter.device = SimpleNamespace(
        get_player_status=AsyncMock(return_value={"play_status": "stop"}),
        get_status=AsyncMock(return_value={"play_status": "play"}),
    )

    assert not await adapter._verify_state("play")
    adapter.device.get_status.assert_not_awaited()


async def test_no_status_api_fails(adapter: PywiimAcceptance) -> None:
    adapter.device = SimpleNamespace()

    assert not await adapter._verify_state("play")
