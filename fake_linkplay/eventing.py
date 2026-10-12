"""GENA subscription and delivery lifecycle, independent of renderer control.

The only renderer input is a synchronous service-to-XML snapshot provider. It
must return a complete immutable body without I/O or scheduling. Each delivery
captures at the same point as before: before acquiring the delivery semaphore;
transient retries retain that body and SEQ until acknowledgement. This component
never receives renderer state, queue, configuration or backend objects.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import logging
import re
import time
from uuid import uuid4

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from .network_policy import NetworkPolicyError, validate_callback_url
from .soap import SERVICE_ALIASES

LOG = logging.getLogger(__name__)


@dataclass
class EventSubscriber:
    """UPnP event subscription tracked for NOTIFY callbacks."""

    sid: str
    callback_url: str
    service: str
    seq: int = 0
    peer_ip: str = ""
    expires_at: float = float("inf")
    dirty: bool = False
    task: asyncio.Task | None = field(default=None, repr=False, compare=False)


class EventSubscriptionManager:
    """Own leases, admission, coalescing, delivery tasks and their cleanup.

    All entry points run on the owner's asyncio loop. Subscription changes and
    snapshot capture are synchronous; no additional observer, lock or task is
    introduced. The application owner calls prune_expired at its existing tick
    and close at its final-listener cleanup point.
    """

    def __init__(self, snapshot_body: Callable[[str], str]) -> None:
        self._snapshot_body = snapshot_body
        self.subscribers: dict[str, EventSubscriber] = {}
        self.clock = time.monotonic
        self.retry_base = 0.5
        self._semaphore = asyncio.Semaphore(8)
        self.limit_total = 64
        self.limit_peer = 8

    async def close(self) -> None:
        """Clear admission, then cancel and drain the existing delivery tasks.

        The caller still owns listener reference counting and shutdown order.
        With no tasks this returns without suspending, just as the inline
        cleanup did. There is deliberately no new closed/reopen state machine.
        """
        tasks = [subscriber.task for subscriber in self.subscribers.values()
                 if subscriber.task and not subscriber.task.done()]
        self.subscribers.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def canonical_service(value: str) -> str | None:
        service = SERVICE_ALIASES.get(value.lower())
        return service if service in {"AVTransport", "RenderingControl", "ConnectionManager", "PlayQueue"} else None

    @staticmethod
    def lease_seconds(value: str) -> int:
        value = value or "Second-1800"
        if value.lower() == "second-infinite":
            return 1800
        match = re.fullmatch(r"Second-([0-9]{1,10})", value, re.IGNORECASE)
        if not match:
            raise ValueError("Invalid subscription timeout")
        return max(30, min(1800, int(match[1])))

    def _remove(self, sid: str) -> None:
        subscriber = self.subscribers.pop(sid, None)
        if subscriber and subscriber.task and subscriber.task is not asyncio.current_task():
            subscriber.task.cancel()

    def prune_expired(self) -> None:
        now = self.clock()
        for sid, subscriber in list(self.subscribers.items()):
            if subscriber.expires_at <= now:
                self._remove(sid)

    def subscribe(self, request: web.Request) -> web.Response:
        """Validate new subscriptions or renew a same-peer service lease."""
        service = self.canonical_service(request.match_info.get("service", ""))
        if service is None:
            return web.Response(status=404)
        self.prune_expired()
        peer = request.remote or ""
        sid = request.headers.get("SID", "")
        callback = request.headers.get("CALLBACK", "")
        nt = request.headers.get("NT", "")
        try:
            lease = self.lease_seconds(request.headers.get("TIMEOUT", ""))
        except ValueError:
            return web.Response(status=400)
        if sid:
            if callback or nt:
                return web.Response(status=400)
            subscriber = self.subscribers.get(sid)
            if subscriber is None or subscriber.peer_ip != peer or subscriber.service != service:
                return web.Response(status=412)
            subscriber.expires_at = self.clock() + lease
        else:
            if nt.lower() != "upnp:event" or not callback:
                return web.Response(status=400)
            try:
                callback = validate_callback_url(callback, peer)
            except NetworkPolicyError:
                return web.Response(status=412)
            subscriber = next((item for item in self.subscribers.values()
                if item.peer_ip == peer and item.service == service and item.callback_url == callback), None)
            if subscriber is None:
                if len(self.subscribers) >= self.limit_total or sum(item.peer_ip == peer for item in self.subscribers.values()) >= self.limit_peer:
                    return web.Response(status=503)
                sid = f"uuid:{uuid4()}"
                subscriber = EventSubscriber(sid=sid, callback_url=callback, service=service,
                    peer_ip=peer, expires_at=self.clock() + lease)
                self.subscribers[sid] = subscriber
            else:
                sid = subscriber.sid
                subscriber.expires_at = self.clock() + lease
            self._schedule(subscriber)
        return web.Response(status=200, body=b"", headers={"SID": sid,
            "TIMEOUT": f"Second-{lease}", "SERVER": "Linux UPnP/1.0 OpenRoomRenderer/0.1"})

    def unsubscribe(self, request: web.Request) -> web.Response:
        service = self.canonical_service(request.match_info.get("service", ""))
        if service is None:
            return web.Response(status=404)
        self.prune_expired()
        sid = request.headers.get("SID", "")
        if not sid or request.headers.get("CALLBACK") or request.headers.get("NT"):
            return web.Response(status=400)
        subscriber = self.subscribers.get(sid)
        if subscriber is None or subscriber.peer_ip != (request.remote or "") or subscriber.service != service:
            return web.Response(status=412)
        self._remove(sid)
        return web.Response(status=200)

    def drop_duplicates(self, callback_url: str, service: str, keep_sid: str | None = None) -> None:
        """Remove duplicate event subscriptions for the same callback/service."""

        service_key = service.lower()
        duplicate_sids = [
            sid
            for sid, subscriber in self.subscribers.items()
            if sid != keep_sid and subscriber.callback_url == callback_url and subscriber.service.lower() == service_key
        ]
        for sid in duplicate_sids:
            self.subscribers.pop(sid, None)
            LOG.info("dropped duplicate event subscriber service=%s sid=%s callback=%s", service, sid, callback_url)

    def notify(self, services: tuple[str, ...] | None = None) -> None:
        """Coalesce changes while keeping one ordered delivery task per SID."""
        self.prune_expired()
        selected = {self.canonical_service(service) for service in services} if services else set()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        for subscriber in list(self.subscribers.values()):
            if selected and self.canonical_service(subscriber.service) not in selected:
                continue
            self._schedule(subscriber)

    def _schedule(self, subscriber: EventSubscriber) -> None:
        subscriber.dirty = True
        if subscriber.task is None or subscriber.task.done():
            subscriber.task = asyncio.create_task(self._deliver(subscriber))

    def _active(self, subscriber: EventSubscriber) -> bool:
        return self.subscribers.get(subscriber.sid) is subscriber and subscriber.expires_at > self.clock()

    async def _deliver(self, subscriber: EventSubscriber) -> None:
        """Retry transient failures within the lease, without redirects or reordering."""
        failures = 0
        try:
            while self._active(subscriber) and (subscriber.dirty or failures):
                if not failures:
                    subscriber.dirty = False
                    body = self._snapshot_body(subscriber.service)
                headers = {"CONTENT-TYPE": 'text/xml; charset="utf-8"', "NT": "upnp:event",
                    "NTS": "upnp:propchange", "SID": subscriber.sid, "SEQ": str(subscriber.seq)}
                success = False
                try:
                    async with self._semaphore:
                        if not self._active(subscriber):
                            break
                        remaining = subscriber.expires_at - self.clock()
                        timeout = ClientTimeout(total=min(1.5, remaining))
                        async with ClientSession(timeout=timeout, trust_env=False) as session:
                            async with session.request("NOTIFY", subscriber.callback_url, data=body.encode("utf-8"), headers=headers, allow_redirects=False) as response:
                                await response.content.read(4096)
                                success = 200 <= response.status < 300
                                if response.status in {410, 412}:
                                    self._remove(subscriber.sid)
                                LOG.debug("GENA delivery service=%s status=%s seq=%s", subscriber.service, response.status, subscriber.seq)
                except (ClientError, TimeoutError, OSError):
                    pass
                if not self._active(subscriber):
                    break
                if success:
                    subscriber.seq = 1 if subscriber.seq == 0xFFFFFFFF else subscriber.seq + 1
                    failures = 0
                else:
                    failures += 1
                    delay = min(30.0, self.retry_base * 2 ** min(failures - 1, 8))
                    await asyncio.sleep(min(delay, max(0, subscriber.expires_at - self.clock())))
        finally:
            if subscriber.expires_at <= self.clock():
                self.subscribers.pop(subscriber.sid, None)
            if subscriber.task is asyncio.current_task():
                subscriber.task = None

