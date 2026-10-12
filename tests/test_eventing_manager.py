"""UNIT/MOCK: independent event owner and renderer lifecycle compatibility."""
import asyncio
from types import SimpleNamespace

import pytest

from fake_linkplay.config import Config
from fake_linkplay.eventing import EventSubscriber, EventSubscriptionManager
from fake_linkplay.http_api import EventSubscriber as CompatibilitySubscriber, HttpApi
from fake_linkplay.state import RendererState
from test_playback_generations import StatefulBackend


async def eventually(check):
    async with asyncio.timeout(2):
        while not check():
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_snapshot_provider_is_lazy_and_retry_retains_body(monkeypatch):
    calls, attempts = [], []
    current = ["old"]
    first_read, release = asyncio.Event(), asyncio.Event()

    def snapshot(service):
        calls.append((service, current[0]))
        return current[0]

    class Session:
        def __init__(self, *, timeout, trust_env):
            assert trust_env is False
            assert 0 < timeout.total <= 1.5
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def request(self, method, url, *, data, headers, allow_redirects):
            assert method == "NOTIFY" and allow_redirects is False
            index = len(attempts)
            attempts.append((data, headers["SEQ"]))
            async def read(limit):
                assert limit == 4096
                if index == 0:
                    first_read.set()
                    await release.wait()
                return b""
            return Response(500 if index == 0 else 200, read)

    class Response:
        def __init__(self, status, read):
            self.status = status
            self.content = SimpleNamespace(read=read)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("fake_linkplay.eventing.ClientSession", Session)
    manager = EventSubscriptionManager(snapshot)
    manager.retry_base = 0.001
    subscriber = EventSubscriber("uuid:one", "http://127.0.0.1/events", "RenderingControl")
    manager.subscribers[subscriber.sid] = subscriber
    try:
        manager.notify()
        assert calls == []  # Scheduling never captures renderer state.
        await asyncio.wait_for(first_read.wait(), timeout=2)
        current[0] = "new"
        manager.notify()
        release.set()
        await eventually(lambda: subscriber.task is None)
        assert calls == [("RenderingControl", "old"), ("RenderingControl", "new")]
        assert attempts == [(b"old", "0"), (b"old", "0"), (b"new", "1")]
        assert subscriber.seq == 2
    finally:
        release.set()
        await manager.close()


@pytest.mark.asyncio
async def test_empty_close_does_not_suspend_and_preserves_registry_identity():
    manager = EventSubscriptionManager(lambda service: "body")
    registry = manager.subscribers
    marker = []
    asyncio.get_running_loop().call_soon(marker.append, "ran")
    await manager.close()
    assert marker == []
    assert manager.subscribers is registry
    await asyncio.sleep(0)
    assert marker == ["ran"]


@pytest.mark.asyncio
async def test_close_cancels_semaphore_waiter_without_network(monkeypatch):
    captured = []
    manager = EventSubscriptionManager(lambda service: captured.append(service) or "body")
    manager._semaphore = asyncio.Semaphore(0)
    def forbidden(*args, **kwargs):
        pytest.fail("A semaphore waiter must not open a session")
    monkeypatch.setattr("fake_linkplay.eventing.ClientSession", forbidden)
    subscriber = EventSubscriber("uuid:wait", "http://127.0.0.1/events", "PlayQueue")
    registry = manager.subscribers
    registry[subscriber.sid] = subscriber
    manager.notify()
    task = subscriber.task
    await eventually(lambda: captured == ["PlayQueue"])
    assert not task.done()
    await manager.close()
    assert task.cancelled() and subscriber.task is None
    assert manager.subscribers is registry and not registry


def test_registry_and_tuning_compatibility(tmp_path):
    cfg = Config(state_dir=str(tmp_path))
    api = HttpApi(cfg, RendererState.from_config(cfg), StatefulBackend())
    assert CompatibilitySubscriber is EventSubscriber
    assert api.event_subscribers is api._eventing.subscribers
    replacement = {}
    api.event_subscribers = replacement
    assert api.event_subscribers is replacement is api._eventing.subscribers
    clock = lambda: 123.0
    semaphore = asyncio.Semaphore(2)
    for name, target, value in (("_event_clock", "clock", clock),
                                ("_event_retry_base", "retry_base", 0.125),
                                ("_event_semaphore", "_semaphore", semaphore),
                                ("_event_limit_total", "limit_total", 12),
                                ("_event_limit_peer", "limit_peer", 3)):
        setattr(api, name, value)
        assert getattr(api, name) is value
        assert getattr(api._eventing, target) is value
    api._event_body = lambda service: "replacement:" + service
    assert api._eventing._snapshot_body("PlayQueue") == "replacement:PlayQueue"


@pytest.mark.asyncio
async def test_two_listener_contexts_share_owner_and_only_last_drains(tmp_path):
    cfg = Config(state_dir=str(tmp_path))
    api = HttpApi(cfg, RendererState.from_config(cfg), StatefulBackend())
    api._monitor_interval = 3600
    first = api._state_monitor_context(api.app())
    second = api._state_monitor_context(api.app())
    await first.__anext__()
    monitor = api._state_monitor_task
    await second.__anext__()
    assert api._app_users == 2 and api._state_monitor_task is monitor
    api._event_semaphore = asyncio.Semaphore(0)
    subscriber = EventSubscriber("uuid:shared", "http://127.0.0.1/events", "AVTransport")
    registry = api.event_subscribers
    registry[subscriber.sid] = subscriber
    api._schedule_event_notify()
    task = subscriber.task
    try:
        await asyncio.sleep(0)  # Ensure delivery entered its cancellation handler.
        await first.aclose()
        assert api._app_users == 1 and api._state_monitor_task is monitor
        assert registry[subscriber.sid] is subscriber and not task.done()
    finally:
        await first.aclose()
        await second.aclose()
    assert api._app_users == 0 and api._state_monitor_task is None
    assert monitor.cancelled() and task.cancelled()
    assert subscriber.task is None
    assert api.event_subscribers is registry and not registry
