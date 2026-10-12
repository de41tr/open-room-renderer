"""Real optional client and loopback HTTP; only audio is mocked."""
import pytest
from test_local_client_integration import local_renderer
from test_media_uri_encoding import MEDIA_URI


@pytest.mark.asyncio
async def test_real_client_preserves_encoded_media_resource(local_renderer):
    await local_renderer.client.play_url(MEDIA_URI)
    assert local_renderer.backend.url == MEDIA_URI
    assert local_renderer.backend.calls == [("loadfile", MEDIA_URI)]
    assert "setPlayerCmd:play:" + MEDIA_URI in local_renderer.commands
