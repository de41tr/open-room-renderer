"""INTEGRATION/MOCK: real local subscription and callback HTTP lifecycle."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET

import pytest
from aiohttp import ClientSession, web

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.queue import PlaybackQueue, QueueItem
from fake_linkplay.state import RendererState
from test_playback_generations import StatefulBackend


async def eventually(check):
    async with asyncio.timeout(2):
        while not check():
            await asyncio.sleep(0.005)


@pytest.fixture
async def local_events(tmp_path):
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path), advertise_ip="127.0.0.1")
    api = HttpApi(cfg, RendererState.from_config(cfg), StatefulBackend())
    api._event_retry_base = 0.01
    api._monitor_interval = 0.01
    api._wait_for_mpv_audio_ready = AsyncMock(return_value=True)
    receipts = []
    replies = []
    targets = []
    gates = []
    async def callback(request):
        receipts.append((dict(request.headers), await request.text(), request.path))
        if gates:
            await gates[0].wait()
        reply = replies.pop(0) if replies else 200
        if reply == 302:
            return web.Response(status=302, headers={"Location": callback_base + "/forbidden"})
        return web.Response(status=reply)
    async def forbidden(request):
        targets.append(request.path)
        return web.Response()
    callback_app = web.Application()
    callback_app.router.add_route("*", "/forbidden", forbidden)
    callback_app.router.add_route("NOTIFY", "/events/{tail:.*}", callback)
    callback_runner, api_runner = web.AppRunner(callback_app), web.AppRunner(api.app())
    await callback_runner.setup()
    await api_runner.setup()
    callback_site, api_site = web.TCPSite(callback_runner, "127.0.0.1", 0), web.TCPSite(api_runner, "127.0.0.1", 0)
    try:
        await callback_site.start()
        await api_site.start()
        callback_base = f"http://127.0.0.1:{callback_site._server.sockets[0].getsockname()[1]}"
        base = f"http://127.0.0.1:{api_site._server.sockets[0].getsockname()[1]}"
        async with ClientSession() as client:
            async def subscribe(service="AVTransport", headers=None, tail="default"):
                values = {"NT": "upnp:event", "CALLBACK": f"<{callback_base}/events/{tail}>", "TIMEOUT": "Second-60"} if headers is None else headers
                async with client.request("SUBSCRIBE", base + "/upnp/event/" + service, headers=values) as response:
                    return response.status, dict(response.headers)
            async def unsubscribe(sid, service="AVTransport", extra=None):
                async with client.request("UNSUBSCRIBE", base + "/upnp/event/" + service, headers={"SID": sid, **(extra or {})}) as response:
                    return response.status
            yield SimpleNamespace(api=api, client=client, base=base, callback_base=callback_base, receipts=receipts,
                replies=replies, targets=targets, gates=gates, subscribe=subscribe, unsubscribe=unsubscribe)
    finally:
        for gate in gates:
            gate.set()
        await api_runner.cleanup()
        await callback_runner.cleanup()
    assert not api.event_subscribers
    assert api._state_monitor_task is None


def properties(body):
    return {node.tag: node.text or "" for prop in ET.fromstring(body) for node in prop}


@pytest.mark.asyncio
async def test_initial_renewal_duplicate_and_unsubscribe(local_events):
    env = local_events
    status, headers = await env.subscribe(service="rendercontrol1")
    assert status == 200 and headers["TIMEOUT"] == "Second-60"
    sid = headers["SID"]
    await eventually(lambda: len(env.receipts) == 1)
    event_headers, body, _ = env.receipts[0]
    assert event_headers["SID"] == sid and event_headers["SEQ"] == "0"
    assert event_headers["NT"] == "upnp:event" and event_headers["NTS"] == "upnp:propchange"
    assert properties(body)["Volume"] == "50"
    status, renewed = await env.subscribe("RenderingControl", {"SID": sid, "TIMEOUT": "Second-120"})
    assert status == 200 and renewed["SID"] == sid and renewed["TIMEOUT"] == "Second-120"
    status, duplicate = await env.subscribe("RenderingControl")
    assert status == 200 and duplicate["SID"] == sid
    assert len(env.api.event_subscribers) == 1
    await eventually(lambda: len(env.receipts) == 2)
    assert env.receipts[1][0]["SEQ"] == "1"
    assert await env.unsubscribe(sid, "RenderingControl") == 200
    assert await env.unsubscribe(sid, "RenderingControl") == 412
    assert (await env.subscribe("RenderingControl", {"SID": sid}))[0] == 412


@pytest.mark.asyncio
async def test_transient_500_preserves_lease_and_sequence(local_events):
    env = local_events
    env.replies[:] = [500, 200]
    status, headers = await env.subscribe("PlayQueue")
    sid = headers["SID"]
    assert status == 200
    await eventually(lambda: len(env.receipts) >= 2 and env.api.event_subscribers[sid].seq == 1)
    assert [entry[0]["SEQ"] for entry in env.receipts[:2]] == ["0", "0"]
    assert (await env.subscribe("PlayQueue", {"SID": sid}))[0] == 200
    env.api.state.queue = PlaybackQueue(name="A & <B>", items=[QueueItem("https://media.example.test/one.wav"), QueueItem("https://media.example.test/two.wav")], index=2)
    env.api._schedule_event_notify(("PlayQueue1",))
    await eventually(lambda: len(env.receipts) >= 3)
    assert env.receipts[-1][0]["SEQ"] == "1"
    props = properties(env.receipts[-1][1])
    assert props["CurrentIndex"] == "2" and props["CurrentPlayListName"] == "A & <B>"
    inner = ET.fromstring(props["LastChange"])
    assert inner.tag == "{urn:schemas-wiimu-com:metadata-1-0/PlayQueue/}Event"
    assert 'val="A &amp; &lt;B&gt;"' in props["LastChange"]
    assert " />" not in props["LastChange"] and "/>\r\n" in props["LastChange"]
    assert "/&gt;&#13;\n" in env.receipts[-1][1]


@pytest.mark.asyncio
async def test_expired_sid_cannot_be_renewed_or_delivered(local_events):
    env = local_events
    now = [100.0]
    env.api._event_clock = lambda: now[0]
    status, headers = await env.subscribe(headers={"NT": "upnp:event", "CALLBACK": f"<{env.callback_base}/events/expiry>", "TIMEOUT": "Second-1"})
    assert status == 200 and headers["TIMEOUT"] == "Second-30"
    sid = headers["SID"]
    await eventually(lambda: len(env.receipts) == 1)
    now[0] = 131
    assert (await env.subscribe(headers={"SID": sid}))[0] == 412
    env.api._schedule_event_notify()
    assert sid not in env.api.event_subscribers


@pytest.mark.asyncio
@pytest.mark.parametrize("headers,status", [
    ({}, 400), ({"SID": "uuid:unknown"}, 412),
    ({"SID": "uuid:unknown", "NT": "upnp:event"}, 400),
    ({"NT": "wrong", "CALLBACK": "<http://127.0.0.1/events>"}, 400),
    ({"NT": "upnp:event", "CALLBACK": "<http://192.0.2.1/events>"}, 412),
    ({"NT": "upnp:event", "CALLBACK": "<http://169.254.169.254/latest/meta-data>"}, 412),
    ({"NT": "upnp:event", "CALLBACK": "<http://localhost/events>"}, 412),
    ({"NT": "upnp:event", "CALLBACK": "<http://127.0.0.1/a><http://127.0.0.1/b>"}, 412),
    ({"NT": "upnp:event", "CALLBACK": "<http://127.0.0.1/events>", "TIMEOUT": "forever"}, 400),
])
async def test_invalid_subscription_has_no_outbound_side_effect(local_events, headers, status):
    env = local_events
    assert (await env.subscribe(headers=headers))[0] == status
    assert not env.api.event_subscribers and not env.receipts


@pytest.mark.asyncio
async def test_wrong_service_and_forwarded_peer_cannot_hijack_lease(local_events):
    env = local_events
    assert (await env.subscribe("ConnectionManager"))[0] == 404
    status, headers = await env.subscribe()
    sid = headers["SID"]
    assert status == 200
    assert (await env.subscribe("PlayQueue", {"SID": sid}))[0] == 412
    assert await env.unsubscribe(sid, "PlayQueue") == 412
    bad = {"NT": "upnp:event", "CALLBACK": "<http://192.0.2.9/events>", "X-Forwarded-For": "192.0.2.9"}
    assert (await env.subscribe(headers=bad))[0] == 412
    assert sid in env.api.event_subscribers


@pytest.mark.asyncio
async def test_subscription_limit_and_duplicate_do_not_leak_slots(local_events):
    env = local_events
    env.api._event_limit_peer = 2
    assert (await env.subscribe(tail="one"))[0] == 200
    assert (await env.subscribe(tail="one"))[0] == 200
    assert len(env.api.event_subscribers) == 1
    assert (await env.subscribe(tail="two"))[0] == 200
    assert (await env.subscribe(tail="three"))[0] == 503
    assert len(env.api.event_subscribers) == 2


@pytest.mark.asyncio
async def test_callback_redirect_is_never_followed(local_events):
    env = local_events
    env.replies[:] = [302]
    _, headers = await env.subscribe()
    await eventually(lambda: len(env.receipts) >= 1)
    await env.unsubscribe(headers["SID"])
    assert not env.targets


@pytest.mark.asyncio
async def test_sequence_wraps_to_one_after_maximum(local_events):
    env = local_events
    _, headers = await env.subscribe()
    sid = headers["SID"]
    await eventually(lambda: env.api.event_subscribers[sid].seq == 1)
    subscriber = env.api.event_subscribers[sid]
    subscriber.seq = 0xFFFFFFFF
    env.api._schedule_event_notify()
    await eventually(lambda: len(env.receipts) == 2 and subscriber.seq == 1)
    assert env.receipts[-1][0]["SEQ"] == str(0xFFFFFFFF)
    env.api._schedule_event_notify()
    await eventually(lambda: len(env.receipts) == 3)
    assert env.receipts[-1][0]["SEQ"] == "1"


@pytest.mark.asyncio
async def test_inflight_changes_are_ordered_and_coalesced(local_events):
    env = local_events
    gate = asyncio.Event()
    env.gates.append(gate)
    _, headers = await env.subscribe("RenderingControl")
    sid = headers["SID"]
    await eventually(lambda: len(env.receipts) == 1)
    for volume in range(1, 8):
        env.api.state.volume = volume
        env.api._schedule_event_notify(("rendercontrol1",))
    assert len(env.receipts) == 1
    gate.set()
    await eventually(lambda: len(env.receipts) == 2 and env.api.event_subscribers[sid].seq == 2)
    assert [entry[0]["SEQ"] for entry in env.receipts] == ["0", "1"]
    assert properties(env.receipts[-1][1])["Volume"] == "7"


@pytest.mark.asyncio
async def test_lease_expiry_cancels_a_retry_task(local_events):
    env = local_events
    now = [100.0]
    env.api._event_clock = lambda: now[0]
    env.api._event_retry_base = 1
    env.replies[:] = [500]
    _, headers = await env.subscribe()
    await eventually(lambda: len(env.receipts) == 1)
    subscriber = env.api.event_subscribers[headers["SID"]]
    task = subscriber.task
    now[0] = subscriber.expires_at + 1
    env.api._prune_expired_subscribers()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not env.api.event_subscribers


@pytest.mark.asyncio
async def test_retry_retains_payload_until_ack_then_sends_new_state(local_events):
    env = local_events
    env.api._event_retry_base = 0.05
    env.replies[:] = [500, 200, 200]
    _, headers = await env.subscribe("RenderingControl")
    sid = headers["SID"]
    await eventually(lambda: len(env.receipts) == 1)
    env.api.state.volume = 7
    env.api._schedule_event_notify(("RenderingControl",))
    await eventually(lambda: len(env.receipts) == 3 and env.api.event_subscribers[sid].seq == 2)
    assert [entry[0]["SEQ"] for entry in env.receipts] == ["0", "0", "1"]
    assert env.receipts[0][1] == env.receipts[1][1]
    assert properties(env.receipts[2][1])["Volume"] == "7"


@pytest.mark.asyncio
async def test_new_callback_does_not_reemit_to_existing_callback(local_events):
    env = local_events
    _, first = await env.subscribe(tail="one")
    await eventually(lambda: len(env.receipts) == 1 and env.api.event_subscribers[first["SID"]].task is None)
    _, second = await env.subscribe(tail="two")
    await eventually(lambda: len(env.receipts) == 2)
    assert [entry[0]["SID"] for entry in env.receipts] == [first["SID"], second["SID"]]
    assert env.api.event_subscribers[first["SID"]].seq == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("response_status", [410, 412])
async def test_explicit_permanent_rejection_removes_lease(local_events, response_status):
    env = local_events
    env.replies[:] = [response_status]
    _, headers = await env.subscribe()
    await eventually(lambda: len(env.receipts) == 1 and headers["SID"] not in env.api.event_subscribers)
    assert (await env.subscribe(headers={"SID": headers["SID"]}))[0] == 412


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["A\x00B", "A\x01B", "A\nB", "", "A" * 129])
async def test_invalid_rename_cannot_corrupt_xml_or_persist(local_events, name):
    env = local_events
    original = env.api.cfg.friendly_name
    async with env.client.get(env.base + "/httpapi.asp", params={"command": "setDeviceName:" + name}) as response:
        assert response.status == 400
    assert env.api.cfg.friendly_name == original
    from pathlib import Path
    assert not (Path(env.api.cfg.state_dir) / "device_name").exists()
    for service in ["RenderingControl", "AVTransport", "PlayQueue"]:
        ET.fromstring(env.api._event_body(service))


def test_invalid_xml_from_backend_metadata_is_replaced_only_at_event_boundary():
    xml = HttpApi._event_propertyset({"Title": "Café & α\x00<one>"})
    assert properties(xml)["Title"] == "Café & α\ufffd<one>"


def test_queue_name_whitespace_survives_text_and_lastchange_attribute(tmp_path):
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path))
    api = HttpApi(cfg, RendererState.from_config(cfg), StatefulBackend())
    api.state.queue.name = "A\nB\rC\tD"
    props = properties(api._event_body("PlayQueue"))
    assert props["CurrentPlayListName"] == "A\nB\rC\tD"
    event = ET.fromstring(props["LastChange"])
    name = next(node for node in event.iter() if node.tag.endswith("CurrentPlayListName"))
    assert name.attrib["val"] == props["CurrentPlayListName"]
