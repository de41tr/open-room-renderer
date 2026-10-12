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
        print(json.dumps(evidence, sort_keys=True))
