"""Real Web projection/admission in memory with a guarded synthetic backend.

No event loop, listening socket, HTTP client, native IPC, or physical audio is
used. The exact production Web response bytes feed the DSP-show parser. The
stable seeded states below have no effects except the Web observation revision;
they do not claim that Web reads never reconcile a matching pending restore.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import socket
from types import SimpleNamespace
from unittest.mock import patch

from multidict import CIMultiDict
import pytest

from fake_linkplay import eq_presets, favorites, http_api, persistence
from fake_linkplay.config import Config
from fake_linkplay.eq_presets import default_eq_bands
from fake_linkplay.http_api import HttpApi
from fake_linkplay.queue import PlaybackQueue, QueueItem
from fake_linkplay.state import RendererState
from fake_linkplay.web_ui import PREFIX

# requests imports urllib3, whose optional IPv6 feature probe otherwise creates
# a socket even though these tests never use HTTP transport. Deny that probe
# before importing the real parser; urllib3 handles this ordinary OSError.
with patch.object(socket, "socket", side_effect=OSError("Synthetic socket denial")):
    from fake_linkplay.dsp_read_client import parse_dsp_snapshot


PLUGIN = "private:synthetic-plugin?token=NOT_REAL"
SAVED_NAME = "Synthetic private saved curve"
HOST = "127.0.0.1:8099"
ORIGIN = "http://" + HOST
GENERATION = 470321
WEB_REVISION_START = 90437


def forbidden(*args, **kwargs):
    pytest.fail("DSP observation attempted a command, persistence, backend I/O, or new task")


def finish_synchronously(coroutine):
    """Require completion before any suspension, without creating a loop."""
    try:
        try:
            coroutine.send(None)
        except StopIteration as done:
            return done.value
        pytest.fail("Read-only DSP projection unexpectedly suspended")
    finally:
        coroutine.close()


class GuardedBackend:
    """Existing confirmed graph is a synthetic cache, never native proof."""

    gain_confirmed = True
    loadfile = pause = resume = stop = seek = snapshot = forbidden
    channel_balance = peq = volume = mute = forbidden

    def __init__(self):
        self.balance = 0.0
        self.bands = []
        self.confirmed = True

    @property
    def confirmed_dsp(self):
        return (self.balance, deepcopy(self.bands)) if self.confirmed else None


class InMemoryTransport:
    def get_extra_info(self, name, default=None):
        return ("127.0.0.1", 8099) if name == "sockname" else default


def request(*, headers=None):
    """Only request/transport data are synthetic; admission is never replaced."""
    return SimpleNamespace(
        method="GET", path=PREFIX + "/state", raw_path=PREFIX + "/state",
        headers=CIMultiDict({"Host": HOST} if headers is None else headers),
        transport=InMemoryTransport(), secure=False, query_string="", content_length=None,
    )


def response(api, *, headers=None):
    return finish_synchronously(api.web_api.handle(request(headers=headers)))


def projection(api, *, headers=None):
    result = response(api, headers=headers)
    assert result.status == 200
    assert result.content_type == "application/json"
    assert result.headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in result.headers
    wire, summary = json.loads(result.body), parse_dsp_snapshot(result.body)
    assert wire["queue"]["count"] == 3 and wire["queue"]["index"] == 2
    assert wire["generation"] == GENERATION
    assert wire["media"]["title"] == api.state.title
    assert wire["device"]["name"] == api.cfg.friendly_name
    assert wire["csrf_token"] == api.web_api._csrf_token
    assert wire["session_id"] == api.web_api._session_id
    encoded = json.dumps(summary)
    for private in (
        api.cfg.friendly_name, api.state.queue.name, api.state.title,
        api.state.artist, api.state.album, api.state.current_url,
        wire["csrf_token"], wire["session_id"],
        *(item.title for item in api.state.queue.items),
        *(item.url for item in api.state.queue.items),
    ):
        assert private and private not in encoded
    assert str(wire["generation"]) not in encoded
    assert str(wire["revision"]) not in encoded
    assert not {"csrf_token", "session_id", "revision", "generation", "device", "media", "queue"} & summary.keys()
    return wire, summary


def bands_for(source):
    records = default_eq_bands()
    modes = (-1, 0, 1, 2, 3, 5, 1, 2, 3, 5)
    for index in range(10):
        records[index * 4]["value"] += 0.5
        records[index * 4 + 1]["value"] = index - (6.25 if source == "wifi" else 4.75)
        records[index * 4 + 2]["value"] = round(0.2 + index * 0.125, 3)
        records[index * 4 + 3]["value"] = modes[index]
    return records


@pytest.fixture
def api(tmp_path, monkeypatch):
    # The fixture directly seeds synthetic state instead of applying startup
    # commands merely to make a read test look confirmed.
    for name in (
        "handle_httpapi", "execute", "sync_mpv_state", "initialize_channel_balance",
        "_run_dsp_command", "_web_dispatch", "_apply_eq_to_mpv", "_set_confirmed_gain",
        "_persist_active_dsp", "_persist_eq_presets", "_persist_text_secure",
        "_schedule_event_notify",
    ):
        monkeypatch.setattr(HttpApi, name, forbidden)
    monkeypatch.setattr(HttpApi, "_load_or_create_plex_client_id", lambda self: "synthetic-client")
    for module in (http_api, persistence, eq_presets, favorites):
        monkeypatch.setattr(module, "atomic_write_text", forbidden)
    for name in ("persist", "save", "rename", "delete"):
        monkeypatch.setattr(eq_presets.EqPresetLibrary, name, forbidden)
    for name in ("run", "new_event_loop", "create_task", "ensure_future"):
        monkeypatch.setattr(asyncio, name, forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "socketpair", forbidden)

    cfg = Config(
        uuid="00000000-0000-4000-8000-000000000000", state_dir=str(tmp_path),
        advertise_ip="127.0.0.1", bind_address="127.0.0.1", enable_mdns=True,
        friendly_name="Synthetic private room",
    )
    result = HttpApi(cfg, RendererState.from_config(cfg), GuardedBackend())
    # A selected multi-item queue and nonzero generation make preservation
    # checks meaningful without loading media, activation, or state syncing.
    result.state.queue = PlaybackQueue(
        name="SyntheticPrivateQueue",
        items=[QueueItem(
            url=f"https://media.example.test/private-{number}.wav?token=SYNTHETIC_MEDIA_{number}",
            title=f"Synthetic private queue track {number}",
            artist="Synthetic private artist", album="Synthetic private album", duration=180,
        ) for number in range(1, 4)],
        index=2,
    )
    selected = result.state.queue.current
    result.state.generation = GENERATION
    result.state.current_url = selected.url
    result.state.title = selected.title
    result.state.artist = selected.artist
    result.state.album = selected.album
    result.state.totlen = 180
    result.state.curpos = 37
    result.web_api._revision = WEB_REVISION_START
    result.eq_active_plugin_uri = PLUGIN
    result.eq_bands_by_plugin = {PLUGIN: bands_for("global")}
    result.eq_source_bands_by_plugin = {"wifi": {PLUGIN: bands_for("wifi")}}
    result.eq_names_by_plugin = {PLUGIN: SAVED_NAME}
    result.eq_channel_by_plugin = {PLUGIN: "Stereo"}
    result.eq_presets = {SAVED_NAME: {PLUGIN: bands_for("global")}}
    result._dsp_active_confirmed = True
    result._dsp_backend_confirmed = True
    result._dsp_backend_attempted = True
    result._dsp_state_uncertain = False
    result._pending_dsp_restore = None
    assert not list(tmp_path.iterdir())
    return result


def seed(api, *, source="global", enabled=True, balance=-0.375):
    api.eq_active_source_name = "" if source == "global" else source
    api.eq_enabled = enabled
    api.channel_balance = api.cfg.channel_balance = str(balance)
    api.mpv.balance = balance
    api.mpv.bands = api._snapshot_eq_filters(api._active_dsp_snapshot())


def protected(api):
    fields = (
        "eq_active_plugin_uri", "eq_active_source_name", "eq_bands_by_plugin",
        "eq_source_bands_by_plugin", "eq_names_by_plugin", "eq_channel_by_plugin",
        "eq_enabled", "eq_current_preset", "channel_balance", "_pending_dsp_restore",
        "_dsp_backend_confirmed", "_dsp_backend_attempted", "_dsp_state_uncertain",
        "_dsp_active_confirmed", "_dsp_library_committed", "_gain_operations",
        "_native_owned_entries", "_native_stage", "_native_current_id",
        "_native_waiting_for_audio", "_native_output_uncertain", "_seek_tasks",
        "_state_monitor_task", "_queue_advance_task", "_station_recovery_task", "_app_users",
    )
    return {
        "fields": {name: deepcopy(getattr(api, name)) for name in fields},
        "cfg": asdict(api.cfg), "state": asdict(api.state),
        "backend": deepcopy(vars(api.mpv)), "library": deepcopy(vars(api._eq_library)),
        "locks": [lock.locked() for lock in
                  (api._dsp_command_lock, api._backend_lock, api._gain_command_lock)],
        "web": {name: getattr(api.web_api, name) for name in
                ("_csrf_token", "_session_id", "_dsp_key", "_export_key", "_output_inspection_busy")},
        "files": {str(path.relative_to(api.cfg.state_dir)): path.read_bytes()
                  for path in Path(api.cfg.state_dir).rglob("*") if path.is_file()},
    }


def expected(*, status="CONFIRMED", source="global", enabled=True, balance=-0.375):
    confirmed = status == "CONFIRMED"
    return {
        "version": 1, "status": status,
        "balance": balance if confirmed else None,
        "eq_enabled": enabled if confirmed else None,
        "source": source if confirmed else None,
        "channel_mode": "Stereo" if confirmed else None,
        "bands": bands_for(source) if confirmed else None,
        "observation_only": True, "physical_output_verified": False,
    }


@pytest.mark.parametrize("source", ["global", "wifi"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("balance", [-1.0, -0.375, 0.0, 0.625, 1.0])
def test_actual_web_bytes_preserve_active_context_all_records_and_signed_balance(
    api, source, enabled, balance,
):
    seed(api, source=source, enabled=enabled, balance=balance)
    before = protected(api)
    old_revision = api.web_api._revision
    wire, summary = projection(api)
    assert wire["dsp"] == api._web_dsp_snapshot()
    assert summary == expected(source=source, enabled=enabled, balance=balance)
    assert len(summary["bands"]) == 40
    assert {record["value"] for record in summary["bands"]
            if record["param_name"].endswith("_mode")} == {-1, 0, 1, 2, 3, 5}
    if not enabled:
        assert api.mpv.bands == []
        assert any(record["value"] for record in summary["bands"]
                   if record["param_name"].endswith("_gain"))
    assert protected(api) == before
    assert wire["revision"] == old_revision + 1
    assert api.web_api._revision == old_revision + 1

    # A caller can edit its detached result without affecting a later read.
    summary["bands"][1]["value"] = 23.0
    again, fresh = projection(api)
    assert fresh == expected(source=source, enabled=enabled, balance=balance)
    assert again["revision"] == old_revision + 2
    assert protected(api) == before


def test_locked_dsp_is_pending_without_waiting_or_exposing_proposal(api):
    seed(api, source="wifi", enabled=False, balance=0.625)
    assert finish_synchronously(api._dsp_command_lock.acquire())
    assert finish_synchronously(api._backend_lock.acquire())
    try:
        before = protected(api)
        revision = api.web_api._revision
        wire, summary = projection(api)
        assert wire["dsp"] == {
            "status": "PENDING", "balance": None, "eq_enabled": None, "name": "", "editor": None,
        }
        assert summary == expected(status="PENDING")
        assert protected(api) == before
        assert api.web_api._revision == revision + 1
    finally:
        api._backend_lock.release()
        api._dsp_command_lock.release()


@pytest.mark.parametrize("cause", [
    "backend_unknown", "active_unconfirmed", "uncertain_mismatch", "restore_mismatch",
    "invalid_balance", "invalid_source", "invalid_bands", "invalid_enabled",
])
def test_unknown_projection_never_fabricates_cached_confirmed_values(api, cause):
    seed(api, source="wifi", enabled=False, balance=0.625)
    if cause == "backend_unknown":
        api.mpv.confirmed = False
    elif cause == "active_unconfirmed":
        api._dsp_active_confirmed = False
    elif cause == "uncertain_mismatch":
        api._dsp_state_uncertain = True
        api.mpv.balance = -0.5
    elif cause == "restore_mismatch":
        api._pending_dsp_restore = api._active_dsp_snapshot()
        api._pending_dsp_restore["balance"] = -0.5
    elif cause == "invalid_balance":
        api.channel_balance = "invalid"
    elif cause == "invalid_source":
        api.eq_active_source_name = "unsupported"
    elif cause == "invalid_bands":
        api.eq_source_bands_by_plugin["wifi"][PLUGIN].pop()
    else:
        api.eq_enabled = "false"
    before = protected(api)
    revision = api.web_api._revision
    wire, summary = projection(api)
    assert wire["dsp"] == {
        "status": "UNKNOWN", "balance": None, "eq_enabled": None, "name": "", "editor": None,
    }
    assert summary == expected(status="UNKNOWN")
    assert protected(api) == before
    assert api.web_api._revision == revision + 1


@pytest.mark.parametrize("retained_library", [False, True])
def test_unavailable_saved_library_does_not_hide_valid_active_dsp(api, retained_library):
    seed(api, enabled=False)
    # Synthetic failed-load latch with either retained library or empty fallback.
    api._eq_library._unavailable = True
    if not retained_library:
        api.eq_presets = {}
    before = protected(api)
    wire, summary = projection(api)
    assert wire["dsp"]["status"] == "CONFIRMED"
    assert wire["dsp"]["editor"]["library_unavailable"] is True
    assert summary == expected(enabled=False)
    assert "library_unavailable" not in summary
    assert "library" not in summary
    assert SAVED_NAME not in json.dumps(summary)
    assert PLUGIN not in json.dumps(summary)
    assert protected(api) == before
    assert api.web_api._revision == WEB_REVISION_START + 1


def test_existing_matching_restore_reconciles_in_memory_without_backend_or_persistence(api):
    seed(api)
    desired = {
        "version": 1, "plugin": PLUGIN, "source": "wifi", "enabled": False,
        "name": "Custom", "balance": 0.625, "bands": bands_for("wifi"),
    }
    api._pending_dsp_restore = deepcopy(desired)
    api._dsp_state_uncertain = True
    api._dsp_active_confirmed = False
    api.mpv.balance, api.mpv.bands = 0.625, []
    before = protected(api)
    wire, summary = projection(api)
    assert summary == expected(source="wifi", enabled=False, balance=0.625)
    assert wire["dsp"]["status"] == "CONFIRMED"

    # Existing server semantics publish only the already-confirmed cached graph.
    # This explicitly records the permitted in-memory reconciliation, rather
    # than implying every GET preserves an unconfirmed frontend DSP cache.
    before["fields"].update(
        eq_active_source_name="wifi", eq_enabled=False, eq_current_preset="Custom",
        eq_names_by_plugin={PLUGIN: "Custom"}, channel_balance="0.625",
        _pending_dsp_restore=None, _dsp_state_uncertain=False, _dsp_active_confirmed=True,
    )
    before["cfg"]["channel_balance"] = "0.625"
    before["state"]["cfg"]["channel_balance"] = "0.625"
    assert protected(api) == before
    assert api.web_api._revision == WEB_REVISION_START + 1


@pytest.mark.parametrize("host", [HOST, "localhost:8099", "configured_mdns"])
@pytest.mark.parametrize("with_origin", [False, True])
def test_real_admission_accepts_loopback_and_configured_host(api, host, with_origin):
    seed(api)
    if host == "configured_mdns":
        host = api.cfg.mdns_hostname.rstrip(".") + ":8099"
    headers = {"Host": host}
    if with_origin:
        headers["Origin"] = "http://" + host
    before = protected(api)
    _, summary = projection(api, headers=headers)
    assert summary == expected()
    assert protected(api) == before
    assert api.web_api._revision == WEB_REVISION_START + 1


@pytest.mark.parametrize("headers", [
    [],
    [("Host", HOST), ("Host", HOST)],
    [("Host", "127.1:8099")],
    [("Host", "2130706433:8099")],
    [("Host", "127.0.0.2:8099")],
    [("Host", "alias.example.test:8099")],
    [("Host", "renderer.example.test:80")],
    [("Host", "user@127.0.0.1:8099")],
    [("Host", "127.0.0.1:8099/")],
    [("Host", "127.0.0.1 :8099")],
    [("Host", "127.0.0.1:8099?query")],
    [("Host", HOST), ("Origin", "http://localhost:8099")],
    [("Host", HOST), ("Origin", "http://alias.example.test:8099")],
    [("Host", HOST), ("Origin", ORIGIN + "/")],
    [("Host", HOST), ("Origin", "null")],
    [("Host", HOST), ("Origin", ORIGIN), ("Origin", ORIGIN)],
    [("Host", HOST), ("Sec-Fetch-Site", "cross-site")],
])
def test_real_admission_rejects_malformed_alias_hosts_and_origins_before_projection(
    api, monkeypatch, headers,
):
    seed(api)
    before = protected(api)
    revision = api.web_api._revision
    monkeypatch.setattr(api.web_api, "_current_state", forbidden)
    monkeypatch.setattr(api.web_api, "_dsp_snapshot", forbidden)
    result = response(api, headers=headers)
    assert result.status == 403
    assert json.loads(result.body)["error"] == "forbidden"
    assert result.headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in result.headers
    assert protected(api) == before
    assert api.web_api._revision == revision
