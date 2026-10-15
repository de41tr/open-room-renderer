"""Real optional client and loopback HTTP; only audio is mocked."""
import pytest
from urllib.parse import quote
from test_local_client_integration import local_renderer
from test_media_uri_encoding import MEDIA_URI


@pytest.mark.asyncio
async def test_real_client_preserves_encoded_media_resource(local_renderer):
    await local_renderer.client.play_url(MEDIA_URI)
    assert local_renderer.backend.url == MEDIA_URI
    assert local_renderer.backend.calls == [("loadfile", MEDIA_URI)]
    assert "setPlayerCmd:play:" + MEDIA_URI in local_renderer.commands


@pytest.mark.asyncio
async def test_real_client_playlist_preserves_whole_resource(local_renderer):
    for uri in (local_renderer.base_url + "/list.m3u", MEDIA_URI + "&end=:3",
                "http://[::1]:8080/a:b.pls:3"):
        await local_renderer.client.play_playlist(uri)
        assert local_renderer.backend.url == uri
        assert local_renderer.backend.calls[-1] == ("loadfile", uri)
        assert "setPlayerCmd:playlist:" + uri in local_renderer.commands
        status = await local_renderer.client.get_player_status()
        assert status["uri"] == uri and status["play_status"] == "play"


@pytest.mark.asyncio
async def test_playlist_rejects_invalid_input_before_displacing_current_playback(local_renderer):
    api, backend, client = local_renderer.api, local_renderer.backend, local_renderer.client
    await client.play_url(MEDIA_URI)
    await client.pause()
    queue, generation = api.state.queue, api.state.generation
    fields = (api.queue_url, api.queue_title, api.state.current_url, api.state.track_metadata)
    calls = backend.calls[:]
    # Any accepted replacement would stop this existing native ownership first.
    api._native_current_id = 17
    try:
        for argument in ("", " ", "http://example.test/\x00bad.m3u",
                         *(quote(MEDIA_URI, safe="") + ":" + index
                           for index in ("3", "-1", "bogus", "", "0:1"))):
            async with local_renderer.session.get(local_renderer.base_url + "/httpapi.asp",
                    params={"command": "setPlayerCmd:playlist:" + argument}) as response:
                assert response.status == 400 and await response.text() == "Failed"
            assert backend.calls == calls and backend.paused and backend.url == MEDIA_URI
            assert api.state.queue is queue and api.state.generation == generation
            assert (api.queue_url, api.queue_title, api.state.current_url, api.state.track_metadata) == fields
            assert api._native_current_id == 17
    finally:
        api._native_current_id = None
    async with local_renderer.session.get(local_renderer.base_url + "/httpapi.asp",
            params={"command": "setPlayerCmd:playlist:" + quote(MEDIA_URI, safe="") + ":0"}) as response:
        assert response.status == 200 and await response.text() == "OK"
    assert backend.calls == calls + [("stop", None), ("loadfile", MEDIA_URI)]
