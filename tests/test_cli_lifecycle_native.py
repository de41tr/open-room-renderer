"""Native real CLI process/restart acceptance; skips only without exact opt-in."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

import pytest

from tools.native_probe_supervisor import run_probe


@pytest.mark.skipif(os.environ.get("ORR_TEST_CLI_LIFECYCLE") != "1",
                   reason="Real CLI lifecycle requires ORR_TEST_CLI_LIFECYCLE=1")
@pytest.mark.parametrize("ownership", ["owned", "external"])
def test_real_cli_serve_signals_restart_and_persistence(ownership):
    assert sys.platform == "linux", "Explicit CLI lifecycle lane requires Linux"
    executable = shutil.which(os.environ.get("ORR_TEST_MPV_BINARY", "mpv"))
    assert executable, "Explicit CLI lifecycle lane requires real mpv"
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="orr-cli-") as directory:
        command = [sys.executable, str(root / "tools/acceptance_cli_lifecycle.py"),
                   "--workspace", directory, "--mpv", str(Path(executable).resolve()),
                   "--ownership", ownership]
        cli = os.environ.get("ORR_TEST_CLI_EXECUTABLE")
        if cli:
            path = Path(cli)
            assert path.is_absolute() and path.is_file() and os.access(path, os.X_OK)
            command.extend(("--cli", str(path)))
        result = run_probe(command, timeout=90, cwd=root, env=os.environ.copy())
        assert result.returncode == 0, f"CLI native acceptance failed\n{result.stdout}\n{result.stderr}"
        evidence = json.loads(result.stdout)
        assert evidence["lane"] == "REAL_CLI_SERVE_UNIX_IPC/null"
        assert evidence["launch"] == ("installed" if cli else "module")
        assert evidence["ownership"] == ownership
        assert evidence["same_ports_and_socket_restart"] and evidence["fixture_child_reaped"]
        assert {cycle["signal"] for cycle in evidence["cycles"]} == {"SIGTERM", "SIGINT", "SIGHUP"}
        assert len(evidence["cycles"]) == 3
        for cycle in evidence["cycles"]:
            assert cycle["exit_status"] == 0 and cycle["decoded_null_output"] and cycle["http_ports_closed"]
            assert cycle["owned_child_reaped"] is (ownership == "owned")
            assert cycle["external_peer_preserved"] is (ownership == "external")
            assert set(cycle["web_assets"]) == {"/ui/", "/ui/index.html", "/ui/style.css",
                                               "/ui/controller.js", "/ui/app.js"}
        print(json.dumps(evidence, sort_keys=True))


@pytest.mark.skipif(os.environ.get("ORR_TEST_CLI_LIFECYCLE") != "1",
                   reason="Real Unix IPC regression requires ORR_TEST_CLI_LIFECYCLE=1")
def test_owned_ipc_survives_simulated_boot_change_after_abrupt_child_exit():
    """Real mpv/Unix IPC; boot identity is simulated, no host reboot or DAC."""
    root = Path(__file__).resolve().parents[1]
    executable = shutil.which(os.environ.get("ORR_TEST_MPV_BINARY", "mpv"))
    assert executable and sys.platform == "linux"
    cli = os.environ.get("ORR_TEST_CLI_EXECUTABLE")
    python = str(Path(cli).parent / "python") if cli else sys.executable
    # The CI installed lane imports the installed package with -I, rather
    # than accidentally testing the checkout in place of the shipped code.
    isolation = ["-I"] if cli else []
    program = r'''
import asyncio, json, os, sys
from pathlib import Path
from fake_linkplay import ipc_provenance
from fake_linkplay.mpv_backend import BackendError, MpvBackend

async def verify():
    path = Path(sys.argv[1]) / "mpv.sock"
    ipc_provenance.boot_id = lambda: "11111111-1111-4111-8111-111111111111"
    first = MpvBackend(str(path), audio_device="null")
    await first.start()
    await first.initialize_gain()
    old_pid = first.process.pid
    assert len(list(path.parent.glob(".orr-mpv-*.json"))) == 1
    first.process.kill()
    await first.process.wait()
    assert path.is_socket()  # Abrupt exit leaves a genuine filesystem socket.
    same_boot = MpvBackend(str(path), audio_device="null")
    try:
        await same_boot.start()
        raise AssertionError("same-boot socket was adopted")
    except BackendError:
        assert same_boot.process is None and path.is_socket()
    finally:
        await same_boot.stop_process()
    ipc_provenance.boot_id = lambda: "22222222-2222-4222-8222-222222222222"
    restarted = MpvBackend(str(path), audio_device="null")
    try:
        await restarted.start()
        assert restarted.process.pid != old_pid
        await restarted.initialize_gain()
        assert (await restarted.command(["get_property", "volume"]))["error"] == "success"
        receipts = list(path.parent.glob(".orr-mpv-*.json"))
        assert len(receipts) == 1
        assert json.loads(receipts[0].read_text())["boot_id"] == ipc_provenance.boot_id()
    finally:
        await restarted.stop_process()
    assert not path.exists() and not list(path.parent.glob(".orr-mpv-*.json"))
    print(json.dumps({"lane":"REAL_MPV_UNIX_IPC/null", "simulated_boot_change":True,
                      "same_boot_refused":True, "new_owned_child_confirmed":True,
                      "clean_shutdown":True, "physical_reboot":False}))
asyncio.run(verify())
'''
    with tempfile.TemporaryDirectory(prefix="orr-boot-") as directory:
        result = run_probe([python, *isolation, "-c", program, directory],
                           timeout=35, cwd=root, env=os.environ.copy())
        assert result.returncode == 0, f"Native reboot IPC regression failed\n{result.stdout}\n{result.stderr}"
        evidence = json.loads(result.stdout)
        assert evidence["new_owned_child_confirmed"] and evidence["same_boot_refused"]
        assert evidence["clean_shutdown"] and not evidence["physical_reboot"]
        print(json.dumps(evidence, sort_keys=True))
