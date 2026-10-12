"""Local HTTP/MOCK evidence for the optional bitrate-derived seek proxy.

These tests verify bytes and HTTP coordinates, not accurate VBR timestamps.
"""
from test_proxy_security import proxy_fixture


async def test_ignored_shifted_range_cannot_return_full_source_as_seek_segment(proxy_fixture):
    env = proxy_fixture
    url = env.select("/audio")
    env.api._byte_offset_for_seconds = lambda *_: 2
    async with env.client.get(env.base + "/proxy/audio", params={"url": url, "start": "120"}) as response:
        assert response.status == 502
        assert "not confirmed" in await response.text()
    assert env.requests[0][1]["Range"] == "bytes=2-"


async def test_confirmed_shifted_bytes_have_segment_local_response_coordinates(proxy_fixture):
    env = proxy_fixture
    url = env.select("/audio")
    env.api._byte_offset_for_seconds = lambda *_: 2
    async with env.client.get(env.base + "/proxy/audio", params={"url": url, "start": "120"},
                              headers={"Range": "bytes=0-3"}) as response:
        assert response.status == 206
        assert await response.read() == b"2345"
        assert response.headers["Content-Range"] == "bytes 0-3/8"
        assert response.headers["Content-Length"] == "4"
    assert env.requests[0][1]["Range"] == "bytes=2-5"
