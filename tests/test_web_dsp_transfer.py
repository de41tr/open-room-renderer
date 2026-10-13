"""Saved-only DSP transfer: strict adapter and two independent MOCK rooms.

Requests exercise production admission in memory; no listening/native sockets,
provider accounts, browser acceptance or physical-audio claim.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiohttp.test_utils import make_mocked_request
from multidict import CIMultiDict
import pytest

from fake_linkplay.config import Config
from fake_linkplay.eq_presets import DEFAULT_EQ_PLUGIN, default_eq_bands
from fake_linkplay.eq_transfer import decode_eq_transfer, encode_eq_transfer
from fake_linkplay.http_api import HttpApi
from fake_linkplay.persistence import AtomicWriteCommittedError, atomic_write_text
from fake_linkplay.state import RendererState
from fake_linkplay.web_ui import (CURVE_EXPORT_FILENAME, MAX_REQUEST_BYTES, PREFIX,
    WebAdmissionError, decode_command, decode_dsp_export, prepare_dsp_curve_export)
from test_web_ui import WebBackend

ORIGIN = "http://127.0.0.1:8099"
PLUGIN = 'private:synthetic-plugin?token=NOT_REAL&label="音"'
NAME = 'Saved café %2F curve'


def document(name=NAME, plugin=PLUGIN, bands=None):
    return encode_eq_transfer(name, plugin, default_eq_bands() if bands is None else bands).decode()


async def room(path, *, plugin=PLUGIN, source="", gain=-6):
    cfg = Config(uuid="00000000-0000-4000-8000-000000000001", state_dir=str(path),
        advertise_ip="127.0.0.1", default_volume=43)
    api = HttpApi(cfg, RendererState.from_config(cfg), WebBackend())
    await api.initialize_channel_balance()
    # Synthetic confirmed state, independent of saved library. No media loaded.
    bands = default_eq_bands(); bands[1]["value"] = gain
    api.eq_active_plugin_uri = plugin
    api.eq_active_source_name = source
    if source:
        api.eq_source_bands_by_plugin[source] = {plugin: deepcopy(bands)}
    else:
        api.eq_bands_by_plugin[plugin] = deepcopy(bands)
    api.eq_enabled = False
    api.channel_balance = "0.250"
    api.cfg.channel_balance = "0.250"
    api.mpv.balance = .25
    api._persist_active_dsp()
    return api


def editor(api):
    return api.web_api.snapshot()["dsp"]["editor"]


def export_body(api, name=NAME, action="prepare", **fields):
    current = editor(api)
    item = next(p for p in current["presets"] if p["id"] ==
        api.web_api.dsp_preset_id(api.eq_active_plugin_uri, name))
    return {"action": action, "expected_dsp": current["revision"], "preset_id": item["id"], **fields}


def import_body(api, text=None, **fields):
    return {"action": "dsp_preset_import", "expected_dsp": editor(api)["revision"],
        "text": document() if text is None else text, "private_import_confirmed": True, **fields}


def request(api, body, *, export=False, method="POST", extra_headers=None, raw=None):
    encoded = raw if raw is not None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    headers = CIMultiDict({"Host": "127.0.0.1:8099", "Origin": ORIGIN,
        "X-ORR-CSRF": api.web_api._csrf_token, "Content-Type": "application/json",
        "Content-Length": str(len(encoded))})
    if extra_headers:
        headers.update(extra_headers)
    transport = Mock()
    transport.get_extra_info.side_effect = lambda key, default=None: (
        ("127.0.0.1", 8099) if key == "sockname" else default)
    return make_mocked_request(method, PREFIX + ("/dsp/export" if export else "/command"),
        headers=headers, transport=transport,
        payload=SimpleNamespace(readexactly=AsyncMock(return_value=encoded)))


async def send(api, body, **kwargs):
    return await api.web_api.handle(request(api, body, **kwargs))


def protected(api):
    fields = ("eq_bands_by_plugin", "eq_source_bands_by_plugin", "eq_names_by_plugin",
        "eq_channel_by_plugin", "eq_active_plugin_uri", "eq_active_source_name",
        "eq_enabled", "eq_current_preset", "channel_balance", "_pending_dsp_restore",
        "_dsp_state_uncertain", "_dsp_active_confirmed", "_dsp_backend_confirmed",
        "_dsp_backend_attempted", "_dsp_library_committed")
    return {"fields": {key: deepcopy(getattr(api, key)) for key in fields},
        "active": api._active_dsp_snapshot(), "cfg": asdict(api.cfg),
        "state": asdict(api.state), "calls": deepcopy(api.mpv.calls),
        "backend": (api.mpv.balance, deepcopy(api.mpv.bands), api.mpv.actual_volume, api.mpv.actual_mute),
        "files": {p.name: p.read_bytes() for p in Path(api.cfg.state_dir).iterdir()
                  if p.is_file() and p.name != "eq_presets.json"}}


def unchanged(api, before, library=None, encoded=None):
    assert protected(api) == before
    if library is not None:
        assert api.eq_presets == library
    if encoded is not None:
        assert (Path(api.cfg.state_dir) / "eq_presets.json").read_bytes() == encoded


def failure(response, code, status=409, outcome="REJECTED"):
    assert response.status == status
    body = json.loads(response.body)
    assert body["error"] == code and body["outcome"] == outcome
    assert "NOT_REAL" not in response.text and PLUGIN not in response.text
    assert "Access-Control-Allow-Origin" not in response.headers
    assert response.headers["Cache-Control"] == "no-store"
    return body


async def test_two_independent_rooms_transfer_saved_current_context_without_active_effects(tmp_path, monkeypatch):
    source = await room(tmp_path / "source", source="wifi", gain=-9)
    destination = await room(tmp_path / "destination", source="", gain=4)
    saved = default_eq_bands(); saved[1]["value"] = -3
    source._eq_library.save(NAME, PLUGIN, saved)
    source._eq_library.save(NAME, "other:private?token=OTHER", default_eq_bands())
    source._eq_library.save("Untouched source", PLUGIN, default_eq_bands())
    destination._eq_library.save("Existing destination", PLUGIN, default_eq_bands())
    for api in (source, destination):
        api._eq_library.persist(api.cfg.state_dir)
    source_before, destination_before = protected(source), protected(destination)
    source_library, destination_library = deepcopy(source.eq_presets), deepcopy(destination.eq_presets)
    source_bytes = (Path(source.cfg.state_dir) / "eq_presets.json").read_bytes()
    monkeypatch.setattr(source, "_persist_active_dsp", lambda: pytest.fail("Export wrote active DSP"))
    monkeypatch.setattr(destination, "_persist_active_dsp", lambda: pytest.fail("Import wrote active DSP"))
    prepared = await send(source, export_body(source), export=True)
    assert prepared.status == 200
    summary = json.loads(prepared.body)
    assert set(summary) == {"version", "dsp_revision", "preset_id", "revision", "size_bytes",
        "filename", "scope", "shared", "band_count"}
    assert summary["shared"] is True and summary["scope"] == "current_plugin" and summary["band_count"] == 40
    assert summary["filename"] == CURVE_EXPORT_FILENAME
    assert NAME not in prepared.text and "NOT_REAL" not in prepared.text and PLUGIN not in prepared.text
    exported = await send(source, export_body(source, action="download", expected_export=summary["revision"],
        private_export_confirmed=True), export=True)
    assert exported.status == 200 and len(exported.body) == summary["size_bytes"]
    assert exported.headers["Content-Disposition"] == f'attachment; filename="{CURVE_EXPORT_FILENAME}"'
    assert json.loads(exported.body) == {"version": 1, "presets": {NAME: {PLUGIN: saved}}}
    assert decode_eq_transfer(exported.body).bands == saved
    imported = await send(destination, import_body(destination, exported.body.decode()))
    assert imported.status == 200 and json.loads(imported.body) == {"request_acknowledged": True}
    assert destination.eq_presets == {**destination_library, NAME: {PLUGIN: saved}}
    assert json.loads((Path(destination.cfg.state_dir) / "eq_presets.json").read_bytes())["presets"] == destination.eq_presets
    unchanged(source, source_before, source_library, source_bytes)
    unchanged(destination, destination_before)


@pytest.mark.parametrize("change", ["revision", "plugin", "source", "library", "balance", "enabled", "restart"])
async def test_stale_export_import_refused_before_mutation(tmp_path, change):
    api = await room(tmp_path)
    api._eq_library.save(NAME, PLUGIN, default_eq_bands())
    export = export_body(api)
    imp = import_body(api, document("New"))
    if change == "revision":
        export["expected_dsp"] = imp["expected_dsp"] = "0" * 64
    elif change == "plugin": api.eq_active_plugin_uri = DEFAULT_EQ_PLUGIN
    elif change == "source": api.eq_active_source_name = "wifi"
    elif change == "library": api._eq_library.save("Elsewhere", "elsewhere", default_eq_bands())
    elif change == "balance": api.channel_balance = "-0.5"
    elif change == "enabled": api.eq_enabled = True
    else: api.web_api._dsp_key = b"new process key"
    before, library = protected(api), deepcopy(api.eq_presets)
    failure(await send(api, export, export=True), "stale_dsp")
    failure(await send(api, imp), "stale_dsp")
    unchanged(api, before, library)


@pytest.mark.parametrize("export", [False, True])
async def test_revision_checked_after_existing_dsp_lock_wait(tmp_path, export):
    api = await room(tmp_path)
    api._eq_library.save(NAME, PLUGIN, default_eq_bands())
    body = export_body(api) if export else import_body(api, document("New"))
    await api._dsp_command_lock.acquire()
    task = asyncio.create_task(send(api, body, export=export))
    await asyncio.sleep(0)
    assert not task.done()
    api._eq_library.save("Other context", "elsewhere", default_eq_bands())
    before, library = protected(api), deepcopy(api.eq_presets)
    api._dsp_command_lock.release()
    failure(await task, "stale_dsp")
    unchanged(api, before, library)


@pytest.mark.parametrize("kind", ["pending_restore", "unconfirmed", "uncertain", "backend_unknown"])
async def test_transfer_does_not_reconcile_or_mutate_unconfirmed_active_state(tmp_path, monkeypatch, kind):
    api = await room(tmp_path)
    api._eq_library.save(NAME, PLUGIN, default_eq_bands())
    exp, imp = export_body(api), import_body(api, document("New"))
    if kind == "pending_restore": api._pending_dsp_restore = api._active_dsp_snapshot()
    elif kind == "unconfirmed": api._dsp_active_confirmed = False
    elif kind == "uncertain": api._dsp_state_uncertain = True
    else: api.mpv._dsp_configuration_confirmed = False
    monkeypatch.setattr(api, "_refresh_restored_dsp", lambda: pytest.fail("Saved transfer reconciled active DSP"))
    before, library = protected(api), deepcopy(api.eq_presets)
    failure(await send(api, exp, export=True), "unavailable")
    failure(await send(api, imp), "unavailable")
    unchanged(api, before, library)


@pytest.mark.parametrize("retained", [False, True])
async def test_unavailable_storage_latch_blocks_both_and_never_exports_fallback(tmp_path, retained):
    api = await room(tmp_path)
    api._eq_library.save(NAME, PLUGIN, default_eq_bands())
    exp, imp = export_body(api), import_body(api, document("New"))
    path = tmp_path / "eq_presets.json"; path.write_text('{"broken":true}')
    with pytest.raises(ValueError): api._eq_library.load(tmp_path)
    if not retained: api.eq_presets = {}
    before, library, damaged = protected(api), deepcopy(api.eq_presets), path.read_bytes()
    failure(await send(api, exp, export=True), "preset_library_unavailable")
    failure(await send(api, imp), "preset_library_unavailable")
    unchanged(api, before, library, damaged)
    assert api._eq_library.unavailable


@pytest.mark.parametrize("kind", ["mismatch", "collision_other_plugin", "active_orphan"])
async def test_import_refuses_context_collision_and_active_name_reinterpretation(tmp_path, kind):
    api = await room(tmp_path)
    text = document()
    if kind == "mismatch": text = document(plugin="other")
    elif kind == "collision_other_plugin": api._eq_library.save(NAME, "other", default_eq_bands())
    else:
        api.eq_names_by_plugin[PLUGIN] = NAME
        text = document(bands=api._active_eq_records())
    body = import_body(api, text)
    before, library = protected(api), deepcopy(api.eq_presets)
    failure(await send(api, body), "curve_context_mismatch" if kind == "mismatch" else "preset_conflict")
    unchanged(api, before, library)


@pytest.mark.parametrize("name", ["Flat", "Bass Boost", "Treble Boost", "https://private.invalid/?token=NOT_REAL",
    "%2568%2574%2574%2570%253a%252f%252fprivate.invalid"])
async def test_unsafe_or_builtin_saved_export_requires_safe_explicit_rename(tmp_path, name):
    api = await room(tmp_path)
    api._eq_library.save(name, PLUGIN, default_eq_bands())
    body = {"action": "prepare", "expected_dsp": editor(api)["revision"],
        "preset_id": api.web_api.dsp_preset_id(PLUGIN, name)}
    before, library = protected(api), deepcopy(api.eq_presets)
    response = await send(api, body, export=True)
    assert response.status == 409
    assert json.loads(response.body)["error"] in {"curve_export_unavailable", "stale_dsp"}
    assert "NOT_REAL" not in response.text
    unchanged(api, before, library)


@pytest.mark.parametrize("committed", [False, True])
async def test_write_failure_preserves_exact_precommit_rollback_and_postreplace_unknown(tmp_path, monkeypatch, committed):
    api = await room(tmp_path)
    api._eq_library.save("Existing", PLUGIN, default_eq_bands()); api._eq_library.persist(tmp_path)
    before, library = protected(api), deepcopy(api.eq_presets)
    old_bytes = (tmp_path / "eq_presets.json").read_bytes()
    def fail_write(path, value):
        assert path.name == "eq_presets.json"
        if committed:
            atomic_write_text(path, value)
            raise AtomicWriteCommittedError("synthetic private failure NOT_REAL")
        raise OSError("synthetic private failure NOT_REAL")
    monkeypatch.setattr("fake_linkplay.http_api.atomic_write_text", fail_write)
    response = await send(api, import_body(api))
    result = failure(response, "command_failed", 502, "UNKNOWN")
    assert result["effect"] == ("LIBRARY_COMMITTED_DURABILITY_UNKNOWN" if committed else "UNKNOWN")
    unchanged(api, before)
    if committed:
        assert NAME in api.eq_presets
        assert json.loads((tmp_path / "eq_presets.json").read_bytes())["presets"] == api.eq_presets
    else:
        assert api.eq_presets == library
        assert (tmp_path / "eq_presets.json").read_bytes() == old_bytes


async def test_full_library_refuses_before_writer_and_rolls_back(tmp_path, monkeypatch):
    api = await room(tmp_path)
    # Existing library close to its own 1 MiB limit; input itself remains tiny.
    empty = {"version": 1, "presets": {"Existing": {"": default_eq_bands()}}}
    overhead = len((json.dumps(empty, indent=2, sort_keys=True) + "\n").encode())
    api._eq_library.save("Existing", "x" * (1024 * 1024 - overhead - 100), default_eq_bands())
    api._eq_library.persist(tmp_path)
    before, library = protected(api), deepcopy(api.eq_presets)
    old = (tmp_path / "eq_presets.json").read_bytes()
    monkeypatch.setattr("fake_linkplay.http_api.atomic_write_text", lambda *args: pytest.fail("Oversized library reached writer"))
    failure(await send(api, import_body(api)), "curve_library_full")
    unchanged(api, before, library, old)


async def test_download_requires_current_export_revision_and_fresh_consent(tmp_path):
    api = await room(tmp_path)
    api._eq_library.save(NAME, PLUGIN, default_eq_bands())
    before, library = protected(api), deepcopy(api.eq_presets)
    failure(await send(api, export_body(api, action="download", expected_export="0"*64,
        private_export_confirmed=True), export=True), "stale_dsp")
    for consent in (False, 1, "true", None):
        failure(await send(api, export_body(api, action="download", expected_export="0"*64,
            private_export_confirmed=consent), export=True), "invalid_request", 400)
        failure(await send(api, import_body(api, document("New"), private_import_confirmed=consent)), "invalid_request", 400)
    unchanged(api, before, library)


@pytest.mark.parametrize("export", [False, True])
@pytest.mark.parametrize("headers,status", [({"Origin": "https://evil.invalid"}, 403),
    ({"X-ORR-CSRF": "bad"}, 403), ({"Host": "127.0.0.1:9999"}, 403),
    ({"Sec-Fetch-Site": "cross-site"}, 403), ({"Content-Encoding": "gzip"}, 415),
    ({"Transfer-Encoding": "chunked"}, 411)])
async def test_existing_admission_keeps_origin_csrf_port_and_encoding_boundaries(tmp_path, export, headers, status):
    api = await room(tmp_path)
    api._eq_library.save(NAME, PLUGIN, default_eq_bands())
    before, library = protected(api), deepcopy(api.eq_presets)
    response = await send(api, export_body(api) if export else import_body(api), export=export, extra_headers=headers)
    assert response.status == status
    assert "Access-Control-Allow-Origin" not in response.headers
    unchanged(api, before, library)


def test_exact_escaped_envelope_roundtrip_limit_and_private_repr():
    value = prepare_dsp_curve_export(NAME, PLUGIN, default_eq_bands())
    envelope = json.dumps({"action": "dsp_preset_import", "expected_dsp": "f"*64,
        "text": value.decode(), "private_import_confirmed": True}, ensure_ascii=False, separators=(",", ":")).encode()
    command = decode_command(envelope)
    assert command.curve == decode_eq_transfer(value)
    assert "NOT_REAL" not in repr(command)
    # File fits, but nested backslashes double again and overflow the request.
    plugin = "\\" * 8000
    encoded = encode_eq_transfer(NAME, plugin, default_eq_bands())
    assert len(encoded) <= MAX_REQUEST_BYTES
    with pytest.raises(WebAdmissionError) as exc:
        prepare_dsp_curve_export(NAME, plugin, default_eq_bands())
    assert exc.value.code == "curve_export_unavailable"


async def test_request_overflow_and_malformed_import_do_not_mutate(tmp_path):
    api = await room(tmp_path)
    before, library = protected(api), deepcopy(api.eq_presets)
    bad_documents = ["{}", document().replace('"version": 1', '"version":1,"version":1'),
        document().replace('"value": 30.0', '"value": NaN'), document().replace('"presets"', '"extra"'),
        document().rstrip()[:-1], '"' + "a" * MAX_REQUEST_BYTES + '"']
    for raw in bad_documents:
        response = await send(api, import_body(api, raw))
        assert response.status in {400, 413}
        unchanged(api, before, library)
    response = await send(api, import_body(api), raw=b" " * (MAX_REQUEST_BYTES + 1))
    failure(response, "request_too_large", 413)


def test_export_decoder_exact_schema_duplicate_fields_and_token_types():
    body = {"action": "prepare", "expected_dsp": "a"*64, "preset_id": "b"*64}
    assert decode_dsp_export(json.dumps(body).encode()).action == "prepare"
    for change in ({"extra": 1}, {"action": "other"}, {"expected_dsp": 1}, {"preset_id": "secret"}):
        with pytest.raises(WebAdmissionError): decode_dsp_export(json.dumps({**body, **change}).encode())
    with pytest.raises(WebAdmissionError): decode_dsp_export(b'{"action":"prepare","action":"download"}')


def test_export_proves_exact_32768_byte_envelope_and_rejects_one_byte_more():
    def envelope(name, plugin):
        return json.dumps({"action": "dsp_preset_import", "expected_dsp": "f"*64,
            "text": encode_eq_transfer(name, plugin, default_eq_bands()).decode(),
            "private_import_confirmed": True}, ensure_ascii=False, separators=(",", ":")).encode()
    base = len(envelope("A", "\\"))
    extra_plugins, extra_name = divmod(MAX_REQUEST_BYTES - base, 4)
    name, plugin = "A" + "x" * extra_name, "\\" * (1 + extra_plugins)
    assert len(envelope(name, plugin)) == MAX_REQUEST_BYTES
    assert len(envelope(name + "x", plugin)) == MAX_REQUEST_BYTES + 1
    encoded = prepare_dsp_curve_export(name, plugin, default_eq_bands())
    assert decode_command(envelope(name, plugin)).curve == decode_eq_transfer(encoded)
    with pytest.raises(WebAdmissionError):
        prepare_dsp_curve_export(name + "x", plugin, default_eq_bands())
    with pytest.raises(WebAdmissionError):
        decode_command(envelope(name + "x", plugin))


async def test_cancelled_lock_wait_has_no_saved_or_active_effects(tmp_path):
    api = await room(tmp_path)
    body = import_body(api)
    before, library = protected(api), deepcopy(api.eq_presets)
    await api._dsp_command_lock.acquire()
    task = asyncio.create_task(send(api, body))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    api._dsp_command_lock.release()
    unchanged(api, before, library)
    assert not (tmp_path / "eq_presets.json").exists()


async def test_repeated_import_is_stale_then_collision_and_never_overwrites(tmp_path):
    api = await room(tmp_path)
    body = import_body(api)
    assert (await send(api, body)).status == 200
    before, library = protected(api), deepcopy(api.eq_presets)
    encoded = (tmp_path / "eq_presets.json").read_bytes()
    failure(await send(api, body), "stale_dsp")
    failure(await send(api, import_body(api)), "preset_conflict")
    unchanged(api, before, library, encoded)
