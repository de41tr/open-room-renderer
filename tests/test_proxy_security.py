"""INTEGRATION/MOCK proxy safety: only fixture routing is mocked, never real WAN."""
import asyncio
import gzip
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from aiohttp import ClientSession, web

import fake_linkplay.http_api as module
from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.network_policy import ProxyTarget, resolve_proxy_target
from fake_linkplay.state import RendererState
from test_playback_generations import StatefulBackend


@pytest.fixture
async def proxy_fixture(tmp_path, monkeypatch):
    upstream_requests = []
    validations = []
    async def upstream(request):
        upstream_requests.append((request.path, dict(request.headers)))
        if request.path == "/redirect-private":
            return web.Response(status=302, headers={"Location": f"http://127.0.0.1:{port}/admin"})
        if request.path == "/redirect-public":
            return web.Response(status=302, headers={"Location": f"http://cdn.example.test:{port}/audio"})
        if request.path == "/loop":
            return web.Response(status=302, headers={"Location": "/loop"})
        if request.path.startswith("/chain/"):
            n = int(request.path.rsplit("/", 1)[-1])
            return web.Response(status=302, headers={"Location": f"/chain/{n + 1}"})
        if request.path == "/error":
            return web.Response(status=500, text="synthetic private upstream detail")
        data = b"0123456789"
        if request.path == "/gzip":
            return web.Response(body=gzip.compress(data, mtime=0), headers={"Content-Encoding": "gzip", "Content-Type": "application/octet-stream"})
        if request.headers.get("Range") == "bytes=2-5":
            return web.Response(status=206, body=data[2:6], headers={"Content-Range": "bytes 2-5/10", "Content-Type": "application/octet-stream"})
        return web.Response(body=data, content_type="application/octet-stream")
    up_app = web.Application()
    up_app.router.add_get("/{tail:.*}", upstream)
    up_runner = web.AppRunner(up_app)
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path), proxy_on_demand_audio=True)
    api = HttpApi(cfg, RendererState.from_config(cfg), StatefulBackend())
    runner = web.AppRunner(api.app())
    await up_runner.setup()
    await runner.setup()
    up_site, site = web.TCPSite(up_runner, "127.0.0.1", 0), web.TCPSite(runner, "127.0.0.1", 0)
    try:
        await up_site.start()
        await site.start()
        port = up_site._server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        async def fixture_resolution(url, allowed_lan_hosts=()):
            validations.append(url)
            parsed = urlsplit(url)
            # Explicit test-only routing: these two synthetic names terminate at
            # the local fixture. Every other destination uses the real policy.
            if parsed.hostname in {"media.example.test", "cdn.example.test"}:
                return ProxyTarget(url, parsed.hostname, parsed.port, ("127.0.0.1",))
            return await resolve_proxy_target(url, allowed_lan_hosts)
        monkeypatch.setattr(module, "resolve_proxy_target", fixture_resolution)
        def select(path):
            url = f"http://media.example.test:{port}{path}"
            api.state.current_url = url
            api.state.intent = "play"
            return url
        async with ClientSession() as client:
            yield SimpleNamespace(api=api, client=client, base=base, select=select,
                requests=upstream_requests, validations=validations, port=port)
    finally:
        await runner.cleanup()
        await up_runner.cleanup()


@pytest.mark.asyncio
async def test_selected_media_range_and_public_redirect_are_preserved(proxy_fixture):
    env = proxy_fixture
    url = env.select("/redirect-public")
    async with env.client.get(env.base + "/proxy/audio", params={"url": url}, headers={"Range": "bytes=2-5", "Authorization": "synthetic-not-forwarded", "Cookie": "synthetic-not-forwarded"}) as response:
        assert response.status == 206
        assert await response.read() == b"2345"
        assert response.headers["Content-Range"] == "bytes 2-5/10"
    assert [path for path, _ in env.requests] == ["/redirect-public", "/audio"]
    assert len(env.validations) == 2
    for _, headers in env.requests:
        assert headers["Range"] == "bytes=2-5"
        assert "Authorization" not in headers and "Cookie" not in headers
        assert headers["User-Agent"] == "OpenRoomRenderer/0.1"


@pytest.mark.asyncio
async def test_redirect_to_private_admin_never_connects(proxy_fixture):
    env = proxy_fixture
    url = env.select("/redirect-private")
    async with env.client.get(env.base + "/proxy/audio", params={"url": url}) as response:
        assert response.status == 403
    assert [path for path, _ in env.requests] == ["/redirect-private"]
    assert len(env.validations) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin", "http://169.254.169.254/latest/meta-data/",
    "http://192.168.1.1/admin", "http://[::1]/admin", "http://2130706433/admin",
    "http://0x7f000001/admin", "file:///etc/passwd", "http://user:password@8.8.8.8/audio",
])
async def test_forbidden_destinations_are_refused_before_connection(proxy_fixture, url):
    env = proxy_fixture
    env.api.state.current_url = url
    env.api.state.intent = "play"
    async with env.client.get(env.base + "/proxy/audio", params={"url": url}) as response:
        assert response.status == 403
        assert "password" not in await response.text()
    assert not env.requests


@pytest.mark.asyncio
async def test_unselected_disabled_or_stopped_proxy_does_not_resolve(proxy_fixture):
    env = proxy_fixture
    url = env.select("/audio")
    async with env.client.get(env.base + "/proxy/audio", params={"url": url + "?other=1"}) as response:
        assert response.status == 403
    env.api.state.intent = "stop"
    async with env.client.get(env.base + "/proxy/audio", params={"url": url}) as response:
        assert response.status == 403
    env.api.cfg.proxy_on_demand_audio = False
    async with env.client.get(env.base + "/proxy/audio", params={"url": url}) as response:
        assert response.status == 404
    assert not env.validations and not env.requests


@pytest.mark.asyncio
async def test_nonlocal_caller_cannot_use_internal_proxy(proxy_fixture):
    env = proxy_fixture
    url = env.select("/audio")
    request = SimpleNamespace(remote="192.0.2.5", query={"url": url}, headers={})
    assert (await env.api.proxy_audio(request)).status == 403
    assert not env.validations


@pytest.mark.asyncio
@pytest.mark.parametrize("path,max_requests", [("/loop", 1), ("/chain/0", 6)])
async def test_redirects_are_bounded(proxy_fixture, path, max_requests):
    env = proxy_fixture
    url = env.select(path)
    async with env.client.get(env.base + "/proxy/audio", params={"url": url}) as response:
        assert response.status == 403
    assert len(env.requests) == max_requests


@pytest.mark.asyncio
async def test_source_change_during_resolution_prevents_connection(proxy_fixture, monkeypatch):
    env = proxy_fixture
    url = env.select("/audio")
    original = module.resolve_proxy_target
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(*args):
        entered.set()
        await release.wait()
        return await original(*args)
    monkeypatch.setattr(module, "resolve_proxy_target", delayed)
    request = asyncio.create_task(env.client.get(env.base + "/proxy/audio", params={"url": url}))
    await entered.wait()
    env.api.state.intent = "stop"
    release.set()
    response = await request
    try:
        assert response.status == 403
    finally:
        response.release()
    assert not env.requests


@pytest.mark.asyncio
async def test_upstream_errors_are_sanitized(proxy_fixture):
    env = proxy_fixture
    url = env.select("/error")
    async with env.client.get(env.base + "/proxy/audio", params={"url": url}) as response:
        assert response.status == 502
        assert "private" not in await response.text()


@pytest.mark.asyncio
async def test_proxy_concurrency_limit_fails_fast(proxy_fixture):
    env = proxy_fixture
    url = env.select("/audio")
    for _ in range(4):
        await env.api._proxy_semaphore.acquire()
    try:
        async with env.client.get(env.base + "/proxy/audio", params={"url": url}) as response:
            assert response.status == 503
        assert not env.validations
    finally:
        for _ in range(4):
            env.api._proxy_semaphore.release()


@pytest.mark.asyncio
async def test_compressed_media_bytes_match_forwarded_headers(proxy_fixture):
    env = proxy_fixture
    url = env.select("/gzip")
    async with env.client.get(env.base + "/proxy/audio", params={"url": url}, auto_decompress=False) as response:
        data = await response.read()
        assert response.status == 200 and response.headers["Content-Encoding"] == "gzip"
        assert int(response.headers["Content-Length"]) == len(data)
        assert gzip.decompress(data) == b"0123456789"
    assert env.api._proxy_semaphore._value == 4
