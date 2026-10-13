"""Room rename adapter: loopback HTTP + synthetic persistence/discovery faults.

Existing rename transaction tests own disk durability/restart behavior. These
checks cover the typed Web boundary and do not claim real mDNS/browser QA.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from fake_linkplay.http_api import ParsedCommand
from fake_linkplay.persistence import AtomicWriteCommittedError, atomic_write_text
from fake_linkplay.web_ui import WebAdmissionError, decode_command
from test_web_ui import api, command, serving, state


def rename(snapshot, name):
    return {"action": "device_rename", "name": name, "expected_device": snapshot["device"]["revision"]}


@pytest.mark.parametrize("name", ["  Küche 🔊 %2F%2525  ", "界" * 128, "🔊" * 128, " \ufeff "])
async def test_web_name_preserves_literal_percent_and_legacy_unicode_limit(api, name):
    before = api.state.generation, api.cfg.upnp_uuid, api.cfg.mdns_hostname
    api.on_rename = AsyncMock()
    async with serving(api) as (session, base):
        old = await state(session, base)
        code, body = await command(session, base, old["csrf_token"], rename(old, name))
        current = await state(session, base)
        assert code == 200 and body == {"request_acknowledged": True, "device_revision": current["device"]["revision"]}
        assert current["device"]["name"] == name.strip()
        assert current["device"]["name_visible"] is True
        assert current["device"]["revision"] != old["device"]["revision"]
        assert Path(api.cfg.state_dir, "device_name").read_text() == name.strip() + "\n"
        assert api.cfg.friendly_name == api.cfg.mdns_instance_name == name.strip()
    api.on_rename.assert_awaited_once_with()
    assert (api.state.generation, api.cfg.upnp_uuid, api.cfg.mdns_hostname) == before
    assert not api.mpv.calls


def test_web_name_validation_keeps_legacy_contract_without_transport_decoding():
    for name in ("", "  ", "a" * 129, "bad\x00name", "bad\x85name", "bad\ud800name", "bad\uffffname", None, 42):
        with pytest.raises(WebAdmissionError):
            decode_command(json.dumps({"action": "device_rename", "name": name, "expected_device": "a" * 64}).encode())
    # Legacy decoding still strips and URL-decodes once; typed input does not.
    from fake_linkplay.http_api import HttpApi
    assert HttpApi._decode_device_name_command("setDeviceName: Room%2F%2525 ") == "Room/%25"
    command = decode_command(json.dumps({"action": "device_rename", "name": " Room%2F%2525%00 ", "expected_device": "a" * 64}).encode())
    assert command.name == "Room%2F%2525%00"


async def test_web_name_requires_current_csrf_strict_schema_and_name_revision(api):
    async with serving(api) as (session, base):
        old = await state(session, base)
        for csrf, payload, status in (
                ("wrong-session", rename(old, "New"), 403),
                (old["csrf_token"], {**rename(old, "New"), "unexpected": True}, 400),
                (old["csrf_token"], {**rename(old, "New"), "expected_device": "bad"}, 400),
                (old["csrf_token"], {**rename(old, "New"), "expected_device": "0" * 64}, 409)):
            code, body = await command(session, base, csrf, payload)
            assert code == status and body["outcome"] == "REJECTED"
        assert not Path(api.cfg.state_dir, "device_name").exists()
        assert (await state(session, base))["device"] == old["device"]


async def test_web_name_compare_and_write_share_existing_rename_lock(api):
    entered, browser_waiting, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    lock = api._rename_lock
    class PausingFirstLock:
        first = True
        async def __aenter__(self):
            if lock.locked():
                browser_waiting.set()
            await lock.acquire()
            if self.first:
                self.first = False
                entered.set()
                await release.wait()
        async def __aexit__(self, *_args):
            lock.release()
    # Pause the preceding real legacy transaction after acquisition, before
    # its write. The browser initially sees the old name, then waits behind it.
    api._rename_lock = PausingFirstLock()
    async with serving(api) as (session, base):
        old = await state(session, base)
        legacy = asyncio.create_task(api.execute(ParsedCommand("setDeviceName:Other controller"), "127.0.0.1"))
        await asyncio.wait_for(entered.wait(), 1)
        waiting = asyncio.create_task(command(session, base, old["csrf_token"], rename(old, "Stale browser")))
        try:
            await asyncio.wait_for(browser_waiting.wait(), 1)
            assert not waiting.done()
            assert api.cfg.friendly_name == old["device"]["name"]
        finally:
            release.set()
        assert await legacy == "OK"
        code, body = await asyncio.wait_for(waiting, 1)
        assert code == 409 and body["error"] == "stale_device" and body["outcome"] == "REJECTED"
        assert (await state(session, base))["device"]["name"] == "Other controller"
        assert Path(api.cfg.state_dir, "device_name").read_text() == "Other controller\n"


@pytest.mark.parametrize("fault", ["precommit", "durability", "discovery"])
async def test_web_name_failures_never_acknowledge_changed_readback(api, monkeypatch, caplog, fault):
    private = "https://private.invalid/room?token=DO_NOT_LOG"
    async def callback():
        if fault == "discovery":
            raise ValueError(private)
    api.on_rename = AsyncMock(side_effect=callback)
    def write(path, value):
        if fault == "precommit":
            raise OSError(private)
        atomic_write_text(path, value)
        if fault == "durability":
            raise AtomicWriteCommittedError(private)
    monkeypatch.setattr("fake_linkplay.http_api.atomic_write_text", write)
    caplog.set_level("INFO", logger="fake_linkplay.http_api")
    async with serving(api) as (session, base):
        old = await state(session, base)
        code, body = await command(session, base, old["csrf_token"], rename(old, "Changed room"))
        current = await state(session, base)
        assert code == 502 and body["outcome"] == "UNKNOWN"
        assert "request_acknowledged" not in body
        assert body.get("committed", False) is (fault != "precommit")
        assert current["device"]["name"] == (old["device"]["name"] if fault == "precommit" else "Changed room")
        assert "DO_NOT_LOG" not in json.dumps(body) + caplog.text
        assert not api.mpv.calls


async def test_web_name_cancellation_keeps_commit_and_never_returns_ack(api):
    async def callback():
        raise asyncio.CancelledError
    api.on_rename = callback
    old = api.web_api.snapshot()
    with pytest.raises(asyncio.CancelledError):
        await api._web_dispatch(decode_command(json.dumps(rename(old, "Committed before cancel")).encode()))
    assert api.web_api.snapshot()["device"]["name"] == "Committed before cancel"
    assert Path(api.cfg.state_dir, "device_name").read_text() == "Committed before cancel\n"
    assert not api._rename_lock.locked()


@pytest.mark.parametrize("name", ["https://private.invalid/room?token=DO_NOT_LOG", "Room token%3DDO_NOT_LOG"])
async def test_web_name_redacted_names_verify_without_error_or_log_echo(api, caplog, name):
    caplog.set_level("INFO", logger="fake_linkplay.http_api")
    async with serving(api) as (session, base):
        old = await state(session, base)
        code, body = await command(session, base, old["csrf_token"], rename(old, name))
        current = await state(session, base)
        assert code == 200
        assert body["device_revision"] == current["device"]["revision"] != old["device"]["revision"]
        assert current["device"]["name_visible"] is False
        assert api.cfg.friendly_name == name  # Valid names are not filtered.
        assert "DO_NOT_LOG" not in json.dumps(body) + json.dumps(current) + caplog.text
        assert "Persisted WiiM Home device rename request" in caplog.text
