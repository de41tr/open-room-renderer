"""Resource teardown regressions; failures below are deliberately injected."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from fake_linkplay.config import Config
from fake_linkplay.mdns import MdnsAdvertiser


@pytest.mark.asyncio
async def test_unregister_failure_still_closes_owned_zeroconf():
    advertiser = MdnsAdvertiser(Config(wiim_home_profile=False, enable_mdns=True, advertise_ip="127.0.0.1"))
    owner = AsyncMock()
    owner.async_unregister_service.side_effect = OSError("injected unregister failure")
    advertiser._zeroconf = owner
    advertiser._infos = [object(), object()]
    with pytest.raises(OSError, match="injected unregister failure"):
        await advertiser.stop()
    owner.async_close.assert_awaited_once()
    assert advertiser._zeroconf is None and advertiser._infos == []


@pytest.mark.asyncio
async def test_cancelled_unregister_still_closes_and_preserves_cancellation():
    advertiser = MdnsAdvertiser(Config(wiim_home_profile=False, enable_mdns=True))
    owner = AsyncMock()
    entered = asyncio.Event()
    async def unregister(_info):
        entered.set()
        await asyncio.Future()
    owner.async_unregister_service.side_effect = unregister
    advertiser._zeroconf = owner
    advertiser._infos = [object()]
    stopping = asyncio.create_task(advertiser.stop())
    await asyncio.wait_for(entered.wait(), 1)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    owner.async_close.assert_awaited_once()
    assert advertiser._zeroconf is None and advertiser._infos == []
    await advertiser.stop()
    owner.async_close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("unregister_fails", [False, True])
async def test_failed_close_retains_owned_handle_for_retry(unregister_fails):
    advertiser = MdnsAdvertiser(Config(wiim_home_profile=False, enable_mdns=True))
    owner = AsyncMock()
    unregister_error = OSError("injected unregister failure")
    close_error = OSError("injected close failure")
    if unregister_fails:
        owner.async_unregister_service.side_effect = unregister_error
    owner.async_close.side_effect = close_error
    advertiser._zeroconf = owner
    info = object()
    advertiser._infos = [info]
    with pytest.raises(OSError, match="injected close failure") as raised:
        await advertiser.stop()
    assert raised.value is close_error
    if unregister_fails:
        assert raised.value.__context__ is unregister_error
    assert advertiser._zeroconf is owner and advertiser._infos == [info]
    owner.async_unregister_service.side_effect = None
    owner.async_close.side_effect = None
    await advertiser.stop()
    assert owner.async_close.await_count == 2
    assert advertiser._zeroconf is None and advertiser._infos == []


@pytest.mark.asyncio
async def test_cancelled_close_retains_owned_handle_for_retry():
    advertiser = MdnsAdvertiser(Config(wiim_home_profile=False, enable_mdns=True))
    owner = AsyncMock()
    entered = asyncio.Event()
    async def close():
        entered.set()
        await asyncio.Future()
    owner.async_close.side_effect = close
    advertiser._zeroconf = owner
    stopping = asyncio.create_task(advertiser.stop())
    await asyncio.wait_for(entered.wait(), 1)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert advertiser._zeroconf is owner
    owner.async_close.side_effect = None
    await advertiser.stop()
    assert owner.async_close.await_count == 2
    assert advertiser._zeroconf is None


@pytest.mark.asyncio
async def test_outer_unregister_timeout_cannot_make_close_unbounded(monkeypatch):
    monkeypatch.setattr("fake_linkplay.mdns.ZEROCONF_CLOSE_TIMEOUT", 0.02)
    advertiser = MdnsAdvertiser(Config(wiim_home_profile=False, enable_mdns=True))
    owner = AsyncMock()
    async def stalled(*_args):
        await asyncio.Future()
    owner.async_unregister_service.side_effect = stalled
    owner.async_close.side_effect = stalled
    advertiser._zeroconf = owner
    advertiser._infos = [object()]
    async def outer_timeout():
        async with asyncio.timeout(0.02):
            await advertiser.stop()
    stopping = asyncio.create_task(outer_timeout())
    try:
        done, _pending = await asyncio.wait([stopping], timeout=1)
        assert stopping in done, "close exceeded its bounded cleanup allowance"
        with pytest.raises(TimeoutError):
            await stopping
    finally:
        # A regression in the close bound must fail rather than hang the suite.
        if not stopping.done():
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)
    owner.async_close.assert_awaited_once()
    assert advertiser._zeroconf is owner
    owner.async_unregister_service.side_effect = None
    owner.async_close.side_effect = None
    await advertiser.stop()
    assert advertiser._zeroconf is None


@pytest.mark.asyncio
@pytest.mark.parametrize("register_error", [OSError("injected register failure"), asyncio.CancelledError()])
async def test_partial_start_still_cleans_up_when_unregister_fails(monkeypatch, register_error):
    owner = AsyncMock()
    owner.async_register_service.side_effect = [None, register_error]
    unregister_error = OSError("injected unregister failure")
    owner.async_unregister_service.side_effect = unregister_error
    monkeypatch.setattr("zeroconf.Zeroconf", lambda: object())
    monkeypatch.setattr("zeroconf.asyncio.AsyncZeroconf", lambda **_kwargs: owner)
    advertiser = MdnsAdvertiser(Config(wiim_home_profile=False, enable_mdns=True, advertise_ip="127.0.0.1"))
    with pytest.raises(OSError, match="injected unregister failure") as raised:
        await advertiser.start()
    assert raised.value.__context__ is register_error
    owner.async_close.assert_awaited_once()
    assert advertiser._zeroconf is None and advertiser._infos == []
    await advertiser.stop()
    owner.async_close.assert_awaited_once()
