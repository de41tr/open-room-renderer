"""Unit checks for explicit discovery interface selection and safe cleanup."""
import asyncio
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace
import socket
import sys

import pytest

from fake_linkplay.config import Config, load_config
from fake_linkplay.mdns import CustomMdnsResponder, MdnsAdvertiser, MDNS_GROUP
from fake_linkplay.ssdp import SsdpServer, SSDP_ADDR


@pytest.mark.parametrize("value", ["", "localhost", "lo", "::1", "0.0.0.0", "224.0.0.251", "255.255.255.255", "127.0.0.01", 1, True, []])
def test_discovery_interface_rejects_non_literal_or_non_unicast(value):
    with pytest.raises(ValueError, match="discovery_interface"):
        Config(discovery_interface=value)


def test_discovery_interface_default_and_yaml(tmp_path):
    assert Config().discovery_interface is None
    path = tmp_path / "config.yaml"
    path.write_text(f"state_dir: {tmp_path / 'state'}\ndiscovery_interface: 127.0.0.1\n")
    assert load_config(path).discovery_interface == "127.0.0.1"


@pytest.mark.asyncio
@pytest.mark.parametrize("interface", [None, "127.0.0.1", "192.0.2.10"])
async def test_ssdp_selects_group_and_outbound_interface_then_awaits_tasks(monkeypatch, interface):
    sock = MagicMock()
    monkeypatch.setattr("fake_linkplay.ssdp.socket.socket", lambda *args: sock)
    server = SsdpServer(Config(discovery_interface=interface, advertise_ip="127.0.0.1"))
    async def pending():
        await asyncio.Future()
    monkeypatch.setattr(server, "_listen", pending)
    monkeypatch.setattr(server, "_notify_loop", pending)
    await server.start()
    assert len(server.tasks) == 2
    sock.setsockopt.assert_any_call(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
        socket.inet_aton(SSDP_ADDR) + socket.inet_aton(interface or "0.0.0.0"))
    if interface:
        sock.setsockopt.assert_any_call(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
        if sys.platform.startswith("linux"):
            sock.setsockopt.assert_any_call(socket.IPPROTO_IP, getattr(socket, "IP_MULTICAST_ALL", 49), 0)
    else:
        assert not any(call.args[1] == socket.IP_MULTICAST_IF for call in sock.setsockopt.call_args_list)
    await server.stop()
    assert server.sock is None and all(task.done() for task in server.tasks)
    sock.close.assert_called_once()
    await server.stop()
    sock.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("interface", [None, "127.0.0.1"])
async def test_ssdp_start_failure_closes_socket_and_explicit_mode_fails(monkeypatch, interface):
    sock = MagicMock()
    sock.bind.side_effect = OSError("synthetic bind failure")
    monkeypatch.setattr("fake_linkplay.ssdp.socket.socket", lambda *args: sock)
    server = SsdpServer(Config(discovery_interface=interface, advertise_ip="127.0.0.1"))
    if interface:
        with pytest.raises(OSError, match="synthetic bind failure"):
            await server.start()
    else:
        await server.start()
    assert server.sock is None and server.tasks == []
    sock.close.assert_called_once()
    await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("interface", [None, "127.0.0.1"])
async def test_custom_mdns_interface_and_stop(monkeypatch, interface):
    sock, transport, protocol = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr("fake_linkplay.mdns.socket.socket", lambda *args: sock)
    endpoint = AsyncMock(return_value=(transport, protocol))
    monkeypatch.setattr(asyncio.get_running_loop(), "create_datagram_endpoint", endpoint)
    responder = CustomMdnsResponder(Config(discovery_interface=interface, advertise_ip="127.0.0.1"))
    await responder.start()
    sock.setsockopt.assert_any_call(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
        socket.inet_aton(MDNS_GROUP) + socket.inet_aton(interface or "0.0.0.0"))
    if interface:
        sock.setsockopt.assert_any_call(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
        if sys.platform.startswith("linux"):
            sock.setsockopt.assert_any_call(socket.IPPROTO_IP, getattr(socket, "IP_MULTICAST_ALL", 49), 0)
    await responder.stop()
    assert responder._transport is responder._protocol is None
    transport.close.assert_called_once()


@pytest.mark.asyncio
async def test_custom_mdns_partial_start_failure_closes_socket(monkeypatch):
    sock = MagicMock()
    sock.bind.side_effect = OSError("synthetic bind failure")
    monkeypatch.setattr("fake_linkplay.mdns.socket.socket", lambda *args: sock)
    responder = CustomMdnsResponder(Config(discovery_interface="127.0.0.1"))
    with pytest.raises(OSError, match="synthetic bind failure"):
        await responder.start()
    sock.close.assert_called_once()
    assert responder._transport is None
    await responder.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("interface", [None, "127.0.0.1"])
async def test_generic_zeroconf_uses_only_selected_ipv4_interface(monkeypatch, interface):
    from zeroconf import IPVersion
    factory = MagicMock()
    async_factory = MagicMock()
    monkeypatch.setattr("zeroconf.Zeroconf", factory)
    monkeypatch.setattr("zeroconf.asyncio.AsyncZeroconf", async_factory)
    advertiser = MdnsAdvertiser(Config(enable_mdns=True, discovery_interface=interface))
    monkeypatch.setattr(advertiser, "_register_services", AsyncMock())
    configure = AsyncMock()
    monkeypatch.setattr(advertiser, "_configure_selected_interface", configure)
    await advertiser.start()
    if interface:
        configure.assert_awaited_once()
        factory.assert_called_once_with(interfaces=[interface], ip_version=IPVersion.V4Only)
    else:
        configure.assert_not_awaited()
        factory.assert_called_once_with()


@pytest.mark.asyncio
async def test_ssdp_cancelled_stop_still_closes_socket():
    cleanup_started = asyncio.Event()
    async def listener():
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await asyncio.Future()
    task = asyncio.create_task(listener())
    await asyncio.sleep(0)
    sock = MagicMock()
    server = SsdpServer(Config(advertise_ip="127.0.0.1"), sock=sock, tasks=[task])
    stopping = asyncio.create_task(server.stop())
    await asyncio.wait_for(cleanup_started.wait(), 1)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert task.done() and server.sock is None
    sock.close.assert_called_once()


@pytest.mark.asyncio
async def test_discovery_partial_client_acquisition_closes_first_socket(monkeypatch):
    import native_discovery_support as support
    sock = MagicMock()
    sock.fileno.return_value = -1
    def acquire(group, port, *, listen):
        if listen:
            raise OSError("synthetic mDNS socket failure")
        return sock
    monkeypatch.setattr(support, "multicast_socket", acquire)
    with pytest.raises(OSError, match="synthetic mDNS"):
        await support.discover([], MagicMock())
    sock.close.assert_called_once()


def fake_zeroconf():
    sock = MagicMock()
    sock.family = socket.AF_INET
    sock.fileno.return_value = 3
    sock.getsockname.return_value = ("127.0.0.1", 5353)
    sock.getsockopt.side_effect = lambda level, option, *size: socket.inet_aton("127.0.0.1") if size else 0
    wrapper = SimpleNamespace(transport=MagicMock())
    wrapper.transport.get_extra_info.return_value = sock
    engine = SimpleNamespace(readers=[wrapper], senders=[wrapper])
    zc = SimpleNamespace(engine=engine, async_wait_for_start=AsyncMock())
    azc = SimpleNamespace(zeroconf=zc, async_close=AsyncMock())
    return sock, wrapper, engine, zc, azc


@pytest.mark.asyncio
async def test_generic_explicit_mode_isolates_owned_sockets_before_registering(monkeypatch):
    sock, wrapper, engine, zc, azc = fake_zeroconf()
    monkeypatch.setattr("zeroconf.Zeroconf", MagicMock(return_value=zc))
    monkeypatch.setattr("zeroconf.asyncio.AsyncZeroconf", MagicMock(return_value=azc))
    advertiser = MdnsAdvertiser(Config(enable_mdns=True, discovery_interface="127.0.0.1"))
    async def register():
        zc.async_wait_for_start.assert_awaited_once()
        if sys.platform.startswith("linux"):
            sock.setsockopt.assert_called_once_with(socket.IPPROTO_IP, getattr(socket, "IP_MULTICAST_ALL", 49), 0)
    monkeypatch.setattr(advertiser, "_register_services", register)
    await advertiser.start()
    await advertiser.stop()
    azc.async_close.assert_awaited_once()
    assert advertiser._zeroconf is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing_engine", "no_readers", "no_senders", "missing_socket", "ipv6", "closed", "wrong_bind", "wrong_output", "set_failure", "start_failure"])
async def test_generic_explicit_mode_fails_closed_and_cleans_up(monkeypatch, fault):
    sock, wrapper, engine, zc, azc = fake_zeroconf()
    if fault == "missing_engine":
        del zc.engine
    elif fault == "no_readers":
        engine.readers = []
    elif fault == "no_senders":
        engine.senders = []
    elif fault == "missing_socket":
        wrapper.transport.get_extra_info.return_value = None
    elif fault == "ipv6":
        sock.family = socket.AF_INET6
    elif fault == "closed":
        sock.fileno.return_value = -1
    elif fault == "wrong_bind":
        sock.getsockname.return_value = ("192.0.2.1", 5353)
    elif fault == "wrong_output":
        sock.getsockopt.side_effect = lambda *args: socket.inet_aton("0.0.0.0")
    elif fault == "set_failure":
        if not sys.platform.startswith("linux"):
            pytest.skip("Linux multicast receive isolation")
        sock.setsockopt.side_effect = OSError("synthetic isolation failure")
    elif fault == "start_failure":
        zc.async_wait_for_start.side_effect = TimeoutError("synthetic startup failure")
    monkeypatch.setattr("zeroconf.Zeroconf", MagicMock(return_value=zc))
    monkeypatch.setattr("zeroconf.asyncio.AsyncZeroconf", MagicMock(return_value=azc))
    advertiser = MdnsAdvertiser(Config(enable_mdns=True, discovery_interface="127.0.0.1"))
    register = AsyncMock()
    monkeypatch.setattr(advertiser, "_register_services", register)
    with pytest.raises(RuntimeError, match="zeroconf"):
        await advertiser.start()
    register.assert_not_awaited()
    azc.async_close.assert_awaited_once()
    assert advertiser._zeroconf is None
