"""Explicit real loopback multicast discovery, with two independent identities.

Run ORR_TEST_DISCOVERY=1 PYTHONPATH=. .venv/bin/pytest -q -s
    tests/test_discovery_native.py

This starts actual production SSDP/custom-mDNS sockets and loops. It verifies
multicast M-SEARCH (unicast responses), multicast DNS-SD QM queries/responses,
advertised HTTP description identity, and the read-only discovery console CLI.
It does not exercise audio/IPC or
prove physical LAN/app interoperability. Missing native capability FAILS after
opt-in, never falls back to unicast discovery or skips.
"""
import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import platform
import socket
import struct
import sys

from aiohttp import ClientSession, ClientTimeout, web
import pytest

from fake_linkplay.config import Config
from fake_linkplay.mdns import CustomMdnsProtocol, MdnsAdvertiser, parse_questions
from fake_linkplay.upnp_description import description_xml
from native_discovery_support import SSDP, discover, live_discovery, multicast_socket

pytestmark = pytest.mark.skipif(os.environ.get("ORR_TEST_DISCOVERY") != "1",
                               reason="Native multicast discovery requires ORR_TEST_DISCOVERY=1")


async def _discover_console_cli(configs, directory):
    """Exercise the installed entry point against real responders, without mocks."""
    root = Path(__file__).resolve().parents[1]
    executable = Path(sys.executable).with_name("open-room-renderer")
    assert executable.is_file() and os.access(executable, os.X_OK), (
        "Explicit native discovery requires the installed open-room-renderer entry point"
    )
    # A group-bound observer cannot mistake unicast traffic for M-SEARCH.
    # IP_RECVTTL is Linux's socket option 12 (not exposed by every Python build).
    observer = multicast_socket(*SSDP, listen=True)
    process = None
    searches = []
    try:
        observer.setsockopt(socket.IPPROTO_IP, getattr(socket, "IP_RECVTTL", 12), 1)
        process = await asyncio.create_subprocess_exec(
            str(executable), "discover", "--interface", "127.0.0.1", "--timeout", "2",
            cwd=directory,
            env={**os.environ, "PYTHONPATH": str(root), "PYTHONUTF8": "1",
                 "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(directory),
                 "XDG_CONFIG_HOME": str(directory)},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 8)
        assert process.returncode == 0, (stdout, stderr)
        assert stderr == b"" and len(stdout) < 4096
        result = json.loads(stdout)
        expected = [{"address": "127.0.0.1", "description_port": cfg.upnp_port,
                     "description_url": cfg.description_url,
                     "identity": f"uuid:{cfg.upnp_uuid}"} for cfg in configs]
        assert sorted(result.pop("candidates"), key=lambda item: item["identity"]) == expected
        assert result == {"verification": "unverified_unauthenticated",
                          "status": "candidates_observed", "truncated": False,
                          "limits_reached": [], "oversized_datagrams": 0}
        # Drain a fixed number of loopback datagrams, then require the actual
        # CLI's two multicast searches and their kernel-reported TTL of zero.
        for _ in range(64):
            try:
                packet, ancillary, flags, peer = observer.recvmsg(4096, socket.CMSG_SPACE(4))
            except BlockingIOError:
                break
            if packet.startswith(b"M-SEARCH * HTTP/1.1\r\n"):
                assert not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
                assert peer[0] == "127.0.0.1"
                assert b"\r\nST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n" in packet
                ttls = [struct.unpack("i", value)[0] for level, kind, value in ancillary
                        if level == socket.IPPROTO_IP and kind == socket.IP_TTL]
                assert ttls == [0]
                searches.append(peer)
        else:
            pytest.fail("Native CLI discovery observer exceeded its 64-datagram cap")
        assert len(searches) == 2 and searches[0] == searches[1]
        assert list(directory.iterdir()) == [], "Discovery must not persist configuration or state"
    finally:
        observer.close()
        if process is not None and process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), 3)
        assert observer.fileno() == -1
    assert process is not None and process.returncode == 0
    return {"console_entry_point": "open-room-renderer", "known_candidates": len(expected),
            "verification": result["verification"], "ttl_zero_multicast_searches": len(searches),
            "child_reaped": True, "observer_closed": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("force_unicast", [False, True])
async def test_real_multicast_two_renderers_and_http_descriptions(monkeypatch, tmp_path, force_unicast):
    if force_unicast:
        # A response with the right source/records but the wrong destination
        # must not satisfy this multicast lane.
        def unicast_only(self, data, addr):
            packet_id, questions = parse_questions(data)
            for question in questions:
                response = self._response_for(packet_id, question)
                if response and self.transport:
                    self.transport.sendto(response, addr)
        monkeypatch.setattr(CustomMdnsProtocol, "datagram_received", unicast_only)
    configs = []
    sockets = []
    http_requests = []
    @web.middleware
    async def record_http_request(request, handler):
        # Includes description/status reads, SOAP control, unknown paths and
        # methods. The original HTTP-description checks run only after the CLI.
        http_requests.append((request.method, request.path_qs))
        return await handler(request)
    async with asyncio.timeout(25), AsyncExitStack() as stack:
        for index in (1, 2):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(sock)
            stack.callback(sock.close)
            sock.bind(("127.0.0.1", 0))
            sock.listen(socket.SOMAXCONN)
            sock.setblocking(False)
            port = sock.getsockname()[1]
            cfg = Config(uuid=f"00000000-0000-4000-8000-00000000000{index}",
                friendly_name=f"Synthetic room {index}", advertise_ip="127.0.0.1",
                bind_address="127.0.0.1", discovery_interface="127.0.0.1",
                http_api_port=port, upnp_port=port, wiim_home_profile=True, enable_mdns=True)
            configs.append(cfg)
            app = web.Application(middlewares=[record_http_request])
            async def description(request, cfg=cfg):
                return web.Response(text=description_xml(cfg), content_type="text/xml")
            app.router.add_get("/description.xml", description)
            runner = web.AppRunner(app, shutdown_timeout=1)
            stack.push_async_callback(runner.cleanup)
            await runner.setup()
            await web.SockSite(runner, sock).start()
        await stack.enter_async_context(live_discovery(configs))
        client = await stack.enter_async_context(ClientSession(timeout=ClientTimeout(total=5), trust_env=False))
        if force_unicast:
            with pytest.raises(TimeoutError):
                await discover(configs, client, timeout=0.5)
        else:
            cli_evidence = await _discover_console_cli(configs, tmp_path)
            assert http_requests == [], "Discovery CLI fetched HTTP data or sent a control request"
            found = await discover(configs, client)
            assert len(found) == 2
    assert all(sock.fileno() == -1 for sock in sockets)
    if force_unicast:
        print(json.dumps({"lane": "DISCOVERY_PROVENANCE_REGRESSION", "unicast_mdns_mutant_rejected": True}))
        return
    print(json.dumps({"lane": "PRODUCTION_DISCOVERY/loopback-multicast", "architecture": platform.machine(),
        "ssdp": "multicast M-SEARCH with unicast response", "mdns": "custom responder multicast QM/PTR/SRV/TXT/A",
        "independent_renderers": 2, "description_udn_and_discovery_agree": True,
        "discovery_cli": {**cli_evidence, "http_requests_during_cli": 0},
        "physical_lan_or_app_interoperability": False, "all_listeners_and_tasks_closed": True}, sort_keys=True))


@pytest.mark.asyncio
async def test_generic_explicit_interface_real_owned_socket_isolation():
    import zeroconf
    cfg = Config(uuid="00000000-0000-4000-8000-000000000004", wiim_home_profile=False, enable_mdns=True,
                 advertise_ip="127.0.0.1", discovery_interface="127.0.0.1")
    advertiser = MdnsAdvertiser(cfg)
    sockets = []
    async with asyncio.timeout(15):
        try:
            await advertiser.start()
            engine = advertiser._zeroconf.zeroconf.engine
            sockets = [reader.transport.get_extra_info("socket") for reader in engine.readers]
            assert sockets and engine.senders
            assert all(sock.family == socket.AF_INET and sock.getsockopt(socket.IPPROTO_IP, 49) == 0
                       for sock in sockets)
            assert all(sender.transport.get_extra_info("socket").getsockname()[0] == "127.0.0.1"
                       for sender in engine.senders)
        finally:
            await advertiser.stop()
    assert advertiser._zeroconf is None and all(sock.fileno() == -1 for sock in sockets)
    print(json.dumps({"lane": "PRODUCTION_ZEROCONF/interface-isolation", "zeroconf_version": zeroconf.__version__,
                      "owned_ipv4_readers_isolated": len(sockets), "selected_sender_verified": True,
                      "owned_sockets_closed": True}, sort_keys=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["error", "cancel"])
async def test_real_loopback_unregister_failure_closes_owned_sockets(monkeypatch, fault):
    """Only unregister is MOCK-injected; all owned resources/closure are real."""
    import sys
    from unittest.mock import AsyncMock

    advertiser = MdnsAdvertiser(Config(
        uuid="00000000-0000-4000-8000-000000000009", wiim_home_profile=False, enable_mdns=True,
        advertise_ip="127.0.0.1", discovery_interface="127.0.0.1",
        mdns_instance_name="Synthetic teardown fixture",
    ))
    assert sys.platform == "linux", "Explicit discovery resource lane requires Linux"
    owner = None
    sockets = []
    try:
        async with asyncio.timeout(15):
            await advertiser.start()
            owner = advertiser._zeroconf
            engine = owner.zeroconf.engine
            transports = {wrapper.transport for wrapper in (*engine.readers, *engine.senders)}
            sockets = [transport.get_extra_info("socket") for transport in transports]
            assert sockets and all(sock.fileno() >= 0 for sock in sockets)
            original_fds = {sock.fileno() for sock in sockets}
            selector = asyncio.get_running_loop()._selector
            assert original_fds <= set(selector.get_map())
            assert all(sender.transport.get_extra_info("socket").getsockname()[0] == "127.0.0.1"
                       for sender in engine.senders)
            # MOCK failure injection only. Zeroconf's instance, startup, sockets,
            # service registration, and async_close remain real production code.
            if fault == "error":
                monkeypatch.setattr(owner, "async_unregister_service",
                                    AsyncMock(side_effect=OSError("injected unregister failure")))
                with pytest.raises(OSError, match="injected unregister failure"):
                    await advertiser.stop()
            else:
                entered = asyncio.Event()
                async def stalled_unregister(_info):
                    entered.set()
                    await asyncio.Future()
                monkeypatch.setattr(owner, "async_unregister_service", stalled_unregister)
                stopping = asyncio.create_task(advertiser.stop())
                await asyncio.wait_for(entered.wait(), 1)
                stopping.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await stopping
            await asyncio.sleep(0)
            assert all(sock.fileno() == -1 for sock in sockets)
            assert all(transport.is_closing() for transport in transports)
            assert owner.zeroconf.done
            assert not original_fds.intersection(selector.get_map())
            assert advertiser._zeroconf is None and advertiser._infos == []
            await advertiser.stop()
            print(json.dumps({"lane": "PRODUCTION_ZEROCONF/loopback-teardown-with-MOCK-failure",
                              "injected_failure": f"async_unregister_service {fault}",
                              "owned_sockets_closed": len(sockets),
                              "owned_selector_readers_removed": len(original_fds),
                              "repeated_stop_is_noop": True,
                              "physical_lan_or_app_interoperability": False}, sort_keys=True))
    finally:
        # The failing pre-fix regression must not leak its actual sockets.
        if owner is not None:
            async with asyncio.timeout(5):
                await owner.async_close()
