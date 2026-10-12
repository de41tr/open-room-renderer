"""Typed DSP editor boundary: UNIT + loopback HTTP/MOCK, no audio claims."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import traceback
from pathlib import Path
from urllib.parse import quote

import pytest

from fake_linkplay.eq_presets import DEFAULT_EQ_PLUGIN, default_eq_bands
from fake_linkplay.http_api import ParsedCommand
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.persistence import AtomicWriteCommittedError, atomic_write_text
from fake_linkplay.web_ui import WebAdmissionError, WebDspCommandError, decode_command
from test_web_ui import api, command, serving, state


def payload(editor, action, **fields):
    return {"action": action, "expected_dsp": editor["revision"], **fields}


def changed_bands(editor, **changes):
    return [{**record, "value": changes.get(record["param_name"], record["value"])}
            for record in editor["bands"]]


def preset(editor, name):
    return next(record for record in editor["presets"] if record["name"] == name)


async def legacy(api, name, fields=None):
    if fields is not None:
        name += ":" + quote(json.dumps(fields), safe="")
    return await api.execute(ParsedCommand(name), "127.0.0.1")


async def test_complete_confirmed_editor_is_detached_and_tokens_do_not_change_on_poll(api):
    assert api.web_api.snapshot()["dsp"]["editor"] is None
    await api.initialize_channel_balance()
    before = api.web_api.snapshot()["dsp"]["editor"]
    assert before == api.web_api.snapshot()["dsp"]["editor"]
    assert before["source"] == "global" and before["channel_mode"] == "Stereo"
    assert before["bands"] == default_eq_bands()
    assert len(before["revision"]) == 64
    assert {entry["name"] for entry in before["presets"]} == {"Flat", "Bass Boost", "Treble Boost"}
    assert all(entry["kind"] == "builtin" and not entry["shared"] for entry in before["presets"])
    before["bands"][1]["value"] = 23
    assert api.web_api.snapshot()["dsp"]["editor"]["bands"][1]["value"] == 0


async def test_apply_save_load_rename_delete_preserve_wifi_context_and_bypass(api):
    await api.initialize_channel_balance()
    plugin = "private-custom-plugin%20identifier"
    await legacy(api, "EQSetLV2SourceBand", {"pluginURI": plugin, "source_name": "wifi",
                 "EQBand": [{"param_name": "a_gain", "value": -3}]})
    await legacy(api, "EQOff")
    async with serving(api) as (session, base):
        before = await state(session, base)
        csrf, editor = before["csrf_token"], before["dsp"]["editor"]
        bands = changed_bands(editor, a_freq=45, a_gain=-6, a_q=2, a_mode=0, b_mode=3)
        code, body = await command(session, base, csrf, payload(editor, "dsp_bands", bands=bands))
        assert (code, body) == (200, {"request_acknowledged": True})
        fresh = await state(session, base)
        assert fresh["dsp"]["eq_enabled"] is False and api.mpv.bands == []
        assert fresh["dsp"]["editor"]["bands"] == bands
        assert api.eq_active_source_name == "wifi" and api.eq_active_plugin_uri == plugin
        assert DEFAULT_EQ_PLUGIN not in api.eq_bands_by_plugin
        editor = fresh["dsp"]["editor"]
        assert (await command(session, base, csrf, payload(editor, "dsp_preset_save", name="A%20literal")))[0] == 200
        editor = (await state(session, base))["dsp"]["editor"]
        saved_id = preset(editor, "A%20literal")["id"]
        assert (await command(session, base, csrf, payload(editor, "dsp_preset_load",
                preset_id=preset(editor, "Flat")["id"])))[0] == 200
        editor = (await state(session, base))["dsp"]["editor"]
        assert (await command(session, base, csrf, payload(editor, "dsp_preset_load", preset_id=saved_id)))[0] == 200
        fresh = await state(session, base)
        assert fresh["dsp"]["eq_enabled"] is True
        assert fresh["dsp"]["editor"]["bands"] == bands
        assert api.mpv.bands == [{"freq": 45., "gain": -6., "q": 2., "mode": 0.},
                                 {"freq": 62.5, "gain": 0., "q": .25, "mode": 3.}]
        editor = fresh["dsp"]["editor"]
        assert (await command(session, base, csrf, payload(editor, "dsp_preset_rename",
                preset_id=saved_id, name="Renamed%2Fstill-literal")))[0] == 200
        editor = (await state(session, base))["dsp"]["editor"]
        renamed = preset(editor, "Renamed%2Fstill-literal")
        assert renamed["id"] != saved_id and renamed["kind"] == "saved"
        assert (await command(session, base, csrf, payload(editor, "dsp_preset_delete", preset_id=renamed["id"])))[0] == 200
        assert not api.eq_presets
        persisted = json.loads((Path(api.cfg.state_dir) / "active_dsp.json").read_text())
        assert persisted["source"] == "wifi" and persisted["plugin"] == plugin and persisted["bands"] == bands
        assert json.loads((Path(api.cfg.state_dir) / "eq_presets.json").read_text())["presets"] == {}


@pytest.mark.parametrize("change", ["curve", "plugin", "source", "library", "balance", "bypass"])
async def test_stale_snapshot_rejects_context_or_library_change_before_effects(api, change):
    await api.initialize_channel_balance()
    async with serving(api) as (session, base):
        old = await state(session, base)
        editor = old["dsp"]["editor"]
        if change == "curve":
            await legacy(api, "EQSetLV2Band", {"EQBand": [{"param_name": "a_gain", "value": -4}]})
        elif change == "plugin":
            await legacy(api, "EQChangeFX:another-plugin")
        elif change == "source":
            await legacy(api, "EQChangeSourceFX", {"source_name": "wifi"})
        elif change == "library":
            # A wholly inactive plugin edit still changes the library guard.
            await legacy(api, "EQSourceSave", {"Name": "Elsewhere", "pluginURI": "inactive"})
        elif change == "balance":
            await legacy(api, "setChannelBalance:0.125")
        else:
            await legacy(api, "EQOff")
        calls = deepcopy(api.mpv.calls)
        active = api._active_dsp_snapshot()
        library = deepcopy(api.eq_presets)
        files = {path.name: path.read_bytes() for path in Path(api.cfg.state_dir).iterdir() if path.is_file()}
        code, body = await command(session, base, old["csrf_token"], payload(editor, "dsp_bands",
                            bands=changed_bands(editor, a_gain=9)))
        assert code == 409 and body["outcome"] == "REJECTED" and body["error"] == "stale_dsp"
        assert api.mpv.calls == calls and api._active_dsp_snapshot() == active and api.eq_presets == library
        assert {path.name: path.read_bytes() for path in Path(api.cfg.state_dir).iterdir() if path.is_file()} == files


async def test_guard_is_checked_after_waiting_lock_and_pending_proposals_stay_hidden(api):
    await api.initialize_channel_balance()
    started, release = asyncio.Event(), asyncio.Event()
    original = api.mpv.peq
    async def slow(bands):
        started.set()
        await release.wait()
        await original(bands)
    api.mpv.peq = slow
    async with serving(api) as (session, base):
        old = await state(session, base)
        editor = old["dsp"]["editor"]
        task = asyncio.create_task(legacy(api, "EQSetLV2Band", {"EQBand": [{"param_name": "a_gain", "value": -7}]}))
        await asyncio.wait_for(started.wait(), 1)
        pending = await state(session, base)
        try:
            assert pending["dsp"]["status"] == "PENDING" and pending["dsp"]["editor"] is None
            waiter = asyncio.create_task(command(session, base, old["csrf_token"], payload(editor,
                        "dsp_preset_save", name="MustNotSave")))
            await asyncio.sleep(.02)
            assert not waiter.done()
        finally:
            release.set()
        await task
        code, body = await waiter
        assert code == 409 and body["error"] == "stale_dsp"
        assert not api.eq_presets and not (Path(api.cfg.state_dir) / "eq_presets.json").exists()


async def test_cancelled_browser_command_keeps_lock_until_confirmation_and_never_retries(api):
    await api.initialize_channel_balance()
    editor = api.web_api.snapshot()["dsp"]["editor"]
    entered, release = asyncio.Event(), asyncio.Event()
    original = api.mpv.peq
    async def slow(bands):
        entered.set()
        await release.wait()
        await original(bands)
    api.mpv.peq = slow
    first = asyncio.create_task(api._web_dispatch(decode_command(json.dumps(payload(editor,
            "dsp_bands", bands=changed_bands(editor, a_gain=-5))).encode())))
    await asyncio.wait_for(entered.wait(), 1)
    first.cancel()
    await asyncio.sleep(0)
    assert api._dsp_command_lock.locked() and not first.done()
    second = asyncio.create_task(api._web_dispatch(decode_command(json.dumps(payload(editor,
            "dsp_preset_save", name="MustNotSave")).encode())))
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(WebAdmissionError) as exc:
        await second
    assert exc.value.code == "stale_dsp"
    assert len([call for call in api.mpv.calls if call[0] == "peq"]) == 1
    assert api.web_api.snapshot()["dsp"]["editor"]["bands"][1]["value"] == -5
    assert not api.eq_presets and not api._dsp_command_lock.locked()


@pytest.mark.parametrize("fault,effect", [
    ("backend", "UNKNOWN"), ("active", "BACKEND_APPLIED_PERSISTENCE_FAILED"),
    ("active_committed", "BACKEND_APPLIED_PERSISTENCE_FAILED"),
    ("library_before", "UNKNOWN"), ("library_committed", "LIBRARY_COMMITTED_DURABILITY_UNKNOWN"),
    ("library_active", "LIBRARY_COMMITTED_ACTIVE_PERSISTENCE_FAILED"),
])
async def test_failure_effects_are_captured_under_lock_without_conflating_commit(api, monkeypatch, fault, effect):
    await api.initialize_channel_balance()
    private = "https://private.example.test/failure?token=DO_NOT_EXPOSE"
    def write(path, value):
        if fault == "active" and path.name == "active_dsp.json":
            raise OSError(private)
        if fault == "active_committed" and path.name == "active_dsp.json":
            atomic_write_text(path, value)
            raise AtomicWriteCommittedError(private)
        if fault == "library_before" and path.name == "eq_presets.json":
            raise OSError(private)
        if fault == "library_committed" and path.name == "eq_presets.json":
            atomic_write_text(path, value)
            raise AtomicWriteCommittedError(private)
        if fault == "library_active" and path.name == "active_dsp.json":
            raise OSError(private)
        atomic_write_text(path, value)
    monkeypatch.setattr("fake_linkplay.http_api.atomic_write_text", write)
    if fault == "backend":
        api.mpv.dsp_error = BackendError(private)
        api.mpv._dsp_configuration_confirmed = False
    async with serving(api) as (session, base):
        # A browser may hold a known old revision after backend truth is lost.
        if fault == "backend":
            api.mpv._dsp_configuration_confirmed = True
        old = await state(session, base)
        editor = old["dsp"]["editor"]
        action = "dsp_preset_save" if fault.startswith("library") else "dsp_bands"
        fields = {"name": "Saved"} if action.endswith("save") else {"bands": changed_bands(editor, a_gain=-8)}
        if fault == "backend":
            original = api.mpv.peq
            async def uncertain(bands):
                api.mpv._dsp_configuration_confirmed = False
                await original(bands)
            api.mpv.peq = uncertain
        code, body = await command(session, base, old["csrf_token"], payload(editor, action, **fields))
        assert code == 502 and body["outcome"] == "UNKNOWN" and body["effect"] == effect
        assert "DO_NOT_EXPOSE" not in json.dumps(body)
        fresh = await state(session, base)
        if fault == "backend":
            assert fresh["dsp"]["status"] == "UNKNOWN" and fresh["dsp"]["editor"] is None
            code, rejected = await command(session, base, old["csrf_token"], payload(editor, "dsp_preset_save", name="Unsafe"))
            assert code == 409 and rejected["error"] == "unavailable" and not api.eq_presets
        elif fault.startswith("active"):
            assert fresh["dsp"]["editor"]["bands"][1]["value"] == -8
            assert len(api.mpv.bands) == 1 and not api.eq_presets
        elif fault == "library_before":
            assert api.eq_presets == {} and not (Path(api.cfg.state_dir) / "eq_presets.json").exists()
        else:
            assert set(api.eq_presets) == {"Saved"}
            assert set(json.loads((Path(api.cfg.state_dir) / "eq_presets.json").read_text())["presets"]) == {"Saved"}
        assert not api._dsp_command_lock.locked()


async def test_private_ids_names_are_hidden_shared_preset_semantics_and_conflicts_are_explicit(api):
    await api.initialize_channel_balance()
    secret_plugin = "https://plugins.test/?token=PLUGIN_SECRET"
    secret_name = "https://presets.test/?token=PRESET_SECRET"
    await legacy(api, "EQSetLV2Band", {"pluginURI": secret_plugin, "EQBand": []})
    await legacy(api, "EQSave:" + quote(secret_name, safe=""))
    await legacy(api, "EQSourceSave", {"Name": secret_name, "pluginURI": "other"})
    await legacy(api, "EQSave:Existing")
    async with serving(api) as (session, base):
        old = await state(session, base)
        encoded = json.dumps(old)
        assert "SECRET" not in encoded and "plugins.test" not in encoded and "presets.test" not in encoded
        editor = old["dsp"]["editor"]
        hidden = preset(editor, "Saved preset")
        assert hidden["shared"] is True and hidden["kind"] == "saved"
        for action, fields in (("dsp_preset_save", {"name": "Existing"}),
                               ("dsp_preset_rename", {"preset_id": hidden["id"], "name": "Existing"})):
            code, body = await command(session, base, old["csrf_token"], payload(editor, action, **fields))
            assert code == 409 and body["error"] == "preset_conflict"
        code, body = await command(session, base, old["csrf_token"], payload(editor, "dsp_preset_rename",
                                  preset_id=hidden["id"], name="Shared"))
        assert code == 200 and set(api.eq_presets["Shared"]) == {secret_plugin, "other"}
        editor = (await state(session, base))["dsp"]["editor"]
        assert (await command(session, base, old["csrf_token"], payload(editor, "dsp_preset_delete",
                        preset_id=preset(editor, "Shared")["id"])))[0] == 200
        assert set(api.eq_presets) == {"Existing"}


@pytest.mark.parametrize("mutation", ["extra", "missing", "duplicate", "nan", "string", "bool", "frequency", "gain", "q", "mode", "plugin", "source", "name", "builtin", "id"])
def test_strict_editor_schema_rejects_ambiguous_or_out_of_range_requests(mutation):
    value = {"action": "dsp_bands", "expected_dsp": "a" * 64, "bands": default_eq_bands()}
    if mutation == "extra": value["bands"][0]["extra"] = True
    elif mutation == "missing": value["bands"].pop()
    elif mutation == "duplicate": value["bands"][-1] = value["bands"][0]
    elif mutation == "nan": value["bands"][0]["value"] = float("nan")
    elif mutation == "string": value["bands"][0]["value"] = "30"
    elif mutation == "bool": value["bands"][0]["value"] = True
    elif mutation == "frequency": value["bands"][0]["value"] = 24000.1
    elif mutation == "gain": value["bands"][1]["value"] = -24.1
    elif mutation == "q": value["bands"][2]["value"] = .099
    elif mutation == "mode": value["bands"][3]["value"] = 4
    elif mutation == "plugin": value["pluginURI"] = "arbitrary"
    elif mutation == "source": value["source"] = "wifi"
    elif mutation == "name": value = {"action": "dsp_preset_save", "expected_dsp": "a" * 64, "name": "{Name=Other}"}
    elif mutation == "builtin": value = {"action": "dsp_preset_save", "expected_dsp": "a" * 64, "name": "Flat"}
    elif mutation == "id": value = {"action": "dsp_preset_load", "expected_dsp": "a" * 64, "preset_id": "raw name"}
    with pytest.raises(WebAdmissionError):
        decode_command(json.dumps(value).encode())


async def test_saved_builtin_reappears_after_delete_and_opaque_ids_are_process_scoped(api):
    await api.initialize_channel_balance()
    await legacy(api, "EQSave:Flat")
    async with serving(api) as (session, base):
        old = await state(session, base)
        editor = old["dsp"]["editor"]
        saved = preset(editor, "Flat")
        assert saved["kind"] == "saved"
        assert (await command(session, base, old["csrf_token"], payload(editor,
                "dsp_preset_delete", preset_id=saved["id"])))[0] == 200
        fresh = (await state(session, base))["dsp"]["editor"]
        assert preset(fresh, "Flat")["id"] == saved["id"]
        assert preset(fresh, "Flat")["kind"] == "builtin"
        code, body = await command(session, base, old["csrf_token"], payload(fresh,
                "dsp_preset_delete", preset_id=saved["id"]))
        assert code == 409 and body["error"] == "unavailable"
        api.web_api._dsp_key = b"synthetic restarted process key"
        restarted = (await state(session, base))["dsp"]["editor"]
        assert restarted["revision"] != fresh["revision"]
        assert preset(restarted, "Flat")["id"] != saved["id"]
        code, body = await command(session, base, old["csrf_token"], payload(fresh,
                "dsp_preset_load", preset_id=saved["id"]))
        assert code == 409 and body["error"] == "stale_dsp"
        code, body = await command(session, base, old["csrf_token"], payload(restarted,
                "dsp_preset_load", preset_id=saved["id"]))
        assert code == 409 and body["error"] == "stale_dsp"


async def test_encoded_token_names_are_hidden_in_active_and_library_displays(api):
    await api.initialize_channel_balance()
    name = "%68%74%74%70%73%3A%2F%2Fpreset.test%2F%3Ftoken%3DENCODED_SECRET"
    await legacy(api, "EQSave:" + quote(name, safe=""))
    value = api.web_api.snapshot()
    assert "ENCODED_SECRET" not in json.dumps(value) and value["dsp"]["name"] == ""
    assert any(record["name"] == "Saved preset" for record in value["dsp"]["editor"]["presets"])
    with pytest.raises(WebAdmissionError):
        decode_command(json.dumps(payload(value["dsp"]["editor"], "dsp_preset_save", name=name)).encode())


async def test_web_failure_wrapper_hides_private_cause_but_legacy_exception_stays_exact(api):
    await api.initialize_channel_balance()
    editor = api.web_api.snapshot()["dsp"]["editor"]
    error = BackendError("https://private.test/failure?token=TRACE_SECRET")
    api.mpv.dsp_error = error
    decoded = decode_command(json.dumps(payload(editor, "dsp_bands",
                bands=changed_bands(editor, a_gain=-2))).encode())
    with pytest.raises(WebDspCommandError) as wrapped:
        await api._web_dispatch(decoded)
    assert wrapped.value.__cause__ is None and wrapped.value.__suppress_context__ is True
    assert "TRACE_SECRET" not in "".join(traceback.format_exception(wrapped.value))
    with pytest.raises(BackendError) as legacy_error:
        await legacy(api, "EQOff")
    assert legacy_error.value is error


async def test_post_apply_read_failure_does_not_claim_persistence_failed(api):
    await api.initialize_channel_balance()
    plugin = "private%22custom?token=PLUGIN_SECRET"
    await legacy(api, "EQSetLV2SourceBand", {"source_name": "wifi", "pluginURI": plugin,
                 "EQBand": [{"param_name": "a_gain", "value": -3}]})
    async with serving(api) as (session, base):
        before = await state(session, base)
        editor = before["dsp"]["editor"]
        code, body = await command(session, base, before["csrf_token"], payload(editor,
                "dsp_preset_load", preset_id=preset(editor, "Flat")["id"]))
        # Existing legacy source readback can fail on a literal percent escape
        # after applying and persisting successfully. Do not diagnose disk loss.
        assert code == 502 and body["outcome"] == "UNKNOWN"
        assert body["effect"] == "BACKEND_APPLIED_FOLLOWUP_UNKNOWN"
        assert "PLUGIN_SECRET" not in json.dumps(body)
        saved = json.loads((Path(api.cfg.state_dir) / "active_dsp.json").read_text())
        assert saved["plugin"] == plugin and saved["bands"][1]["value"] == 0
        fresh = (await state(session, base))["dsp"]
        assert fresh["status"] == "CONFIRMED" and fresh["editor"]["bands"][1]["value"] == 0
        assert len([call for call in api.mpv.calls if call[0] == "peq"]) == 2
