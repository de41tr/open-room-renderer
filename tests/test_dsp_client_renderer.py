"""Real DSP client/CLI HTTP with a deterministic synthetic mpv IPC peer.

HttpApi, persistence, and MpvBackend's complete filter transactions are real.
The peer models command/readback behavior only: no native mpv, decoder, audio
device, external media, or acoustic result is involved.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys

from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from fake_linkplay import dsp_client as client
from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.state import RendererState
from test_backend_dsp import DspPeer, OTHER_FILTER, add, make_backend
from test_dsp_api import band_command, execute, snapshot


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DETAIL = "https://private.example.test/dsp?token=synthetic-private-token"
EQ_FILTER = "equalizer=f=1000.000:width_type=q:width=1.000:g=-6.000"


@dataclass
class DspRenderer:
    api: HttpApi
    peer: DspPeer
    server: TestServer
    requests: list[tuple[str, int]]

    @property
    def client(self):
        return client.DspClient("127.0.0.1", self.server.port)

    async def cli(self, *arguments):
        command = [sys.executable, "-m", "fake_linkplay.cli", "dsp", *arguments,
                   "--host", "127.0.0.1", "--port", str(self.server.port)]
        process = await asyncio.create_subprocess_exec(
            *command, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode,
                                           stdout.decode(), stderr.decode())

    def persisted_files(self):
        return {path.name: path.read_bytes() for path in Path(self.api.cfg.state_dir).iterdir()
                if path.is_file()}

    def active_snapshot(self):
        return json.loads((Path(self.api.cfg.state_dir) / "active_dsp.json").read_text())


@pytest.fixture
async def renderer(tmp_path):
    cfg = Config(uuid="00000000-0000-4000-8000-000000000000",
                 state_dir=str(tmp_path), advertise_ip="127.0.0.1", enable_ssdp=False)
    backend, peer = make_backend()
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)
    # As in serve(), reconcile DSP before opening the listener. Only native
    # IPC is replaced; HTTP dispatch, validation, graph building and disk I/O
    # remain real throughout these tests.
    await api.initialize_channel_balance()
    requests = []

    @web.middleware
    async def record_request(request, handler):
        assert request.path == "/httpapi.asp"
        assert set(request.query) == {"command"}
        response = await handler(request)
        requests.append((request.query["command"], response.status))
        return response

    app = api.app()
    app.middlewares.append(record_request)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        yield DspRenderer(api, peer, server, requests)
    finally:
        await server.close()
    assert api._state_monitor_task is None


def success(result):
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return json.loads(result.stdout)


async def seed_curve(renderer):
    await execute(renderer.api, band_command(a_freq=1000, a_gain=-6, a_q=1, a_mode=1))
    await execute(renderer.api, "setChannelBalance:0.25")
    assert renderer.peer.graph == EQ_FILTER + ",pan=stereo|c0=0.750*c0|c1=1.000*c1"
    renderer.requests.clear()
    renderer.peer.calls.clear()


async def test_balance_client_readback_and_cli_set_preserve_eq_and_persist_complete_state(renderer):
    assert await asyncio.to_thread(renderer.client.get_balance) == 0.0
    assert renderer.requests == [("getChannelBalance", 200)]
    await seed_curve(renderer)
    original = renderer.active_snapshot()
    result = await renderer.cli("balance", "set", "-0.375")
    assert success(result) == {
        "request_acknowledged": True, "command": "balance set", "value": -0.375,
    }
    assert renderer.requests == [("setChannelBalance:-0.375", 200)]
    assert renderer.peer.graph == EQ_FILTER + ",pan=stereo|c0=1.000*c0|c1=0.625*c1"
    assert renderer.peer.filters[0] == OTHER_FILTER
    assert renderer.active_snapshot() == {**original, "balance": -0.375}
    assert (Path(renderer.api.cfg.state_dir) / "channel_balance").read_text() == "-0.375\n"
    calls = deepcopy(renderer.peer.calls)
    assert await asyncio.to_thread(renderer.client.get_balance) == -0.375
    assert success(await renderer.cli("balance", "get")) == {"balance": -0.375}
    assert renderer.requests == [("setChannelBalance:-0.375", 200),
                                 ("getChannelBalance", 200), ("getChannelBalance", 200)]
    assert renderer.peer.calls == calls  # Getters cannot change the graph.


async def test_eq_bypass_and_balance_changes_preserve_each_other_and_cached_curve(renderer):
    await seed_curve(renderer)
    original = renderer.active_snapshot()
    result = await renderer.cli("eq", "off")
    assert success(result) == {"request_acknowledged": True, "command": "eq", "value": "off"}
    assert renderer.requests == [("EQOff", 200)]
    assert renderer.peer.graph == "pan=stereo|c0=0.750*c0|c1=1.000*c1"
    assert renderer.active_snapshot() == {**original, "enabled": False}
    assert (Path(renderer.api.cfg.state_dir) / "channel_balance").read_text() == "0.25\n"
    assert await asyncio.to_thread(renderer.client.get_balance) == 0.25

    assert await asyncio.to_thread(renderer.client.set_balance, -0.25) == -0.25
    assert renderer.peer.graph == "pan=stereo|c0=1.000*c0|c1=0.750*c1"
    assert renderer.active_snapshot() == {**original, "enabled": False, "balance": -0.25}
    assert (Path(renderer.api.cfg.state_dir) / "channel_balance").read_text() == "-0.25\n"
    assert await asyncio.to_thread(renderer.client.set_eq, "on") is None
    assert renderer.requests == [("EQOff", 200), ("getChannelBalance", 200),
                                 ("setChannelBalance:-0.25", 200), ("EQOn", 200)]
    assert renderer.peer.graph == EQ_FILTER + ",pan=stereo|c0=1.000*c0|c1=0.750*c1"
    assert renderer.peer.filters[0] == OTHER_FILTER
    assert renderer.active_snapshot() == {**original, "balance": -0.25}


async def test_client_balance_canonicalization_round_trips_renderer_resolution(renderer):
    await seed_curve(renderer)
    original = renderer.active_snapshot()
    assert await asyncio.to_thread(renderer.client.set_balance, 0.12349) == 0.123
    assert renderer.requests == [("setChannelBalance:0.123", 200)]
    assert await asyncio.to_thread(renderer.client.get_balance) == 0.123
    assert renderer.active_snapshot() == {**original, "balance": 0.123}
    assert renderer.peer.graph == EQ_FILTER + ",pan=stereo|c0=0.877*c0|c1=1.000*c1"


@pytest.mark.parametrize("operation", ["balance", "eq"])
@pytest.mark.parametrize("failure", ["backend_rejection", "persistence_after_application"])
async def test_same_502_is_unknown_whether_dsp_changed_or_rejection_preserved_it(
        renderer, monkeypatch, operation, failure):
    await seed_curve(renderer)
    before = snapshot(renderer.api)
    original_graph = renderer.peer.graph
    original_files = renderer.persisted_files()
    original_active = renderer.active_snapshot()
    if operation == "balance":
        arguments = ("balance", "set", "-0.4")
        wire = "setChannelBalance:-0.4"
        candidate = EQ_FILTER + ",pan=stereo|c0=1.000*c0|c1=0.600*c1"
    else:
        arguments = ("eq", "off")
        wire = "EQOff"
        candidate = "pan=stereo|c0=0.750*c0|c1=1.000*c1"
    if failure == "backend_rejection":
        renderer.peer.reply(add(candidate), {"error": PRIVATE_DETAIL})
    else:
        def fail_storage(*_args):
            raise OSError(PRIVATE_DETAIL)
        monkeypatch.setattr("fake_linkplay.http_api.atomic_write_text", fail_storage)

    result = await renderer.cli(*arguments)
    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr == client._UNKNOWN + "\n"
    assert PRIVATE_DETAIL not in result.stderr
    # Both real server paths intentionally return the same response. A 502 is
    # not proof of rollback or permission to automatically retry a mutation.
    assert renderer.requests == [(wire, 502)]
    assert [call for call in renderer.peer.calls if call[:2] == ["af", "add"]] == [add(candidate)]
    assert renderer.persisted_files() == original_files
    assert renderer.peer.filters[0] == OTHER_FILTER
    if failure == "backend_rejection":
        assert snapshot(renderer.api) == before
        assert renderer.peer.graph == original_graph
        assert renderer.api.cfg.channel_balance == "0.25"
    else:
        assert renderer.peer.graph == candidate
        assert renderer.api.eq_enabled is (operation == "balance")
        assert renderer.api.channel_balance == ("-0.4" if operation == "balance" else "0.25")
        assert renderer.api.cfg.channel_balance == renderer.api.channel_balance
        assert renderer.api._active_dsp_snapshot() == {
            **original_active,
            **({"balance": -0.4} if operation == "balance" else {"enabled": False}),
        }
    # Explicit readback can report the live state even when saving it failed.
    expected = -0.4 if operation == "balance" and failure == "persistence_after_application" else 0.25
    assert await asyncio.to_thread(renderer.client.get_balance) == expected
    assert renderer.requests == [(wire, 502), ("getChannelBalance", 200)]


async def test_unconfirmed_backend_rollback_blocks_reads_until_explicit_recovery(renderer):
    await seed_curve(renderer)
    original_graph = renderer.peer.graph
    original_files = renderer.persisted_files()
    candidate = EQ_FILTER + ",pan=stereo|c0=1.000*c0|c1=0.600*c1"
    # Model a candidate applied without an acknowledgement, then a rejected
    # rollback. The old frontend balance is no longer safe to advertise.
    renderer.peer.reply(add(candidate), {"error": PRIVATE_DETAIL}, apply=True)
    renderer.peer.reply(add(original_graph), {"error": PRIVATE_DETAIL})
    with pytest.raises(client.DspClientError) as error:
        await asyncio.to_thread(renderer.client.set_balance, -0.4)
    assert str(error.value) == client._UNKNOWN
    assert renderer.requests == [("setChannelBalance:-0.4", 502)]
    assert renderer.peer.graph == candidate
    assert renderer.api.channel_balance == "0.25"
    assert renderer.api.mpv.confirmed_dsp is None
    assert renderer.persisted_files() == original_files
    calls = deepcopy(renderer.peer.calls)

    with pytest.raises(client.DspClientError) as error:
        await asyncio.to_thread(renderer.client.get_balance)
    assert str(error.value) == client._READ_FAILED
    result = await renderer.cli("balance", "get")
    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr == client._READ_FAILED + "\n"
    assert renderer.requests == [("setChannelBalance:-0.4", 502),
                                 ("getChannelBalance", 502), ("getChannelBalance", 502)]
    assert renderer.peer.calls == calls  # Failed reads cannot retry or restore.

    assert await asyncio.to_thread(renderer.client.set_balance, 0.25) == 0.25
    assert await asyncio.to_thread(renderer.client.get_balance) == 0.25
    assert renderer.peer.graph == original_graph
    assert renderer.api.mpv.confirmed_dsp is not None
    assert renderer.persisted_files() == original_files
    assert renderer.requests[-2:] == [("setChannelBalance:0.25", 200), ("getChannelBalance", 200)]


@pytest.mark.parametrize("value", ["25", "1.001", "-1.001", "NaN", "Infinity", PRIVATE_DETAIL])
async def test_cli_invalid_normalized_balance_fails_before_any_http_request(renderer, value):
    await seed_curve(renderer)
    before = snapshot(renderer.api)
    original_graph = renderer.peer.graph
    original_files = renderer.persisted_files()
    result = await renderer.cli("balance", "set", value)
    assert result.returncode == 2 and result.stdout == ""
    assert result.stderr == "Invalid client command arguments. Run open-room-renderer --help.\n"
    assert renderer.requests == []
    assert renderer.peer.calls == []
    assert snapshot(renderer.api) == before
    assert renderer.peer.graph == original_graph
    assert renderer.persisted_files() == original_files
