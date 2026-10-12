"""Real pywiim/loopback HTTP checks for silent queue navigation; MOCK audio."""
from dataclasses import asdict

import pytest

from test_local_client_integration import (
    assert_transport, create_client_pair, local_renderer, pywiim,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["next_track", "previous_track"])
async def test_local_client_stopped_navigation_selects_then_explicit_resume_plays(local_renderer, method):
    client, backend = local_renderer.client, local_renderer.backend
    first, second = await create_client_pair(local_renderer)
    if method == "previous_track":
        await client.resume()
        await client.next_track()
        await client.stop()
    calls = list(backend.calls)
    await getattr(client, method)()
    status = await assert_transport(client, "stop")
    selected, index = (second, "2") if method == "next_track" else (first, "1")
    assert status["uri"] == selected
    assert backend.calls == calls and backend.idle and backend.url == ""
    assert (await client.send_command("getPlayerStatus"))["plicurr"] == index
    await client.resume()
    assert (await assert_transport(client, "play"))["uri"] == backend.url == selected
    assert [call for call in backend.calls[len(calls):] if call[0] == "loadfile"] == [("loadfile", selected)]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["next_track", "previous_track"])
async def test_local_client_paused_multi_item_navigation_fails_without_resume(local_renderer, method):
    client, backend = local_renderer.client, local_renderer.backend
    await create_client_pair(local_renderer)
    await client.resume()
    if method == "previous_track":
        await client.next_track()
    await client.pause()
    await assert_transport(client, "pause")
    before, calls = asdict(local_renderer.api.state), list(backend.calls)
    with pytest.raises(pywiim.WiiMError):
        await getattr(client, method)()
    await assert_transport(client, "pause")
    assert asdict(local_renderer.api.state) == before and backend.calls == calls
    assert backend.paused and not backend.idle
