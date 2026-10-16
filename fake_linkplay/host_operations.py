"""Fixed stdin-only product operations for the protected v1 host boundary."""
from __future__ import annotations
import datetime as dt
import fcntl
import json
import importlib.util
import shutil
import os
from pathlib import Path
import re
import sys
from . import host_contract as contract
from . import host_release as releases
from . import host_runtime as g

HELPER_VERSION = "product-v1.1"
MUTATIONS = frozenset({"release-prepare", "release-install", "release-rollback",
    "component-prepare", "component-install", "component-activate", "component-deactivate"})

def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def state(report, info):
    report.update(installed_commit=info["commit"],
                  running_commit=info["commit"] if info["identity"] else None,
                  renderer_pid=info["identity"][0] if info["identity"] else None,
                  service_running=info["identity"] is not None,
                  activated=info["config_data"].get("spotify_eq_profile") is not None)


def eq_driver(r):
    from . import host_eq as eq
    inner = {"request_id": r["request_id"], "commit": r["commit"]}
    if r["operation"] in {"component-prepare", "component-install"}:
        inner["profile"] = "spotify-eq"
    else:
        inner["operation"] = "spotify-eq-activate" if r["operation"] == "component-activate" else "spotify-eq-deactivate"
    return eq.execute(inner)

# New fixed drivers are reviewed product code. Their data descriptors cannot
# choose imports or obtain new host permissions.
DRIVERS = {"spotify-eq-v1": eq_driver}

def execute(r):
    report = {"api_version": 1, "helper_version": HELPER_VERSION, "layout": g.LAYOUT,
              "request_id": r["request_id"], "operation": r["operation"],
              "profile": r["profile"], "requested_commit": r["commit"],
              "started_at": utc(), "finished_at": None, "phase": "preflight",
              "accepted": False, "prepared": False, "installed": False,
              "activated": None, "service_running": None, "checks_passed": False,
              "installed_commit": None, "running_commit": None, "renderer_pid": None,
              "audio_acceptance": "not_performed", "account_test": "not_performed",
              "rollback": "not_needed", "result": "rejected", "automatic_retry": False}
    code = 2
    try:
        lock = open(g.BASE / "update.lock", "r")
        with lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            d = contract.installed_descriptor()
            info = releases.layout()
            state(report, info)
            op = r["operation"]
            dependencies = {"python": sys.version_info >= (3,11),
                            "git": shutil.which("git") is not None,
                            "mpv": shutil.which("mpv") is not None,
                            "venv": importlib.util.find_spec("venv") is not None,
                            "ensurepip": importlib.util.find_spec("ensurepip") is not None}
            report["dependencies"] = dependencies
            report["required_steps"] = (
                ["build-private-release","validate-current-config"] if op=="release-prepare" else
                ["validate-current-config","stop-owned-renderer","switch-current","start-owned-renderer","verify-backend"] if op in {"release-install","release-rollback"} else
                ["prepare-private-component"] if op in {"component-prepare","component-install"} else
                ["validate-current-config","stop-owned-renderer","edit-owned-key","start-owned-renderer","verify-backend"] if op.startswith("component-") else [])
            if op in MUTATIONS and not all(dependencies.values()):
                raise contract.Refused("required_dependency_missing")
            if op in {"status", "capabilities"}:
                report.update(backend_checked=False, backend_state="unknown",
                              audio_device_present="KA17" in Path("/proc/asound/cards").read_text())
                if info["identity"] is not None:
                    try:
                        from .host_eq import backend
                        report.update(backend(info, g, report["activated"]))
                        report.update(backend_checked=True, backend_state="verified")
                    except Exception:
                        report.update(backend_checked=False, backend_state="unverified")
                report.update(capabilities=contract.capabilities(d), accepted=True,
                              phase="observed", result="success", checks_passed=True)
            elif op.startswith("component-"):
                if info["commit"] != r["commit"]:
                    raise contract.Refused("requested_commit_not_installed_and_running")
                item = contract.component(r["profile"], d)
                driver = DRIVERS.get(item["driver"])
                if driver is None:
                    raise contract.Refused("component_driver_not_supported")
                code, response = driver(r)
                for key in ("phase", "accepted", "installed", "activated",
                            "service_running", "checks_passed", "installed_commit",
                            "running_commit", "renderer_pid", "mpv_pid", "spotify_pid", "volume_readback", "backend_state",
                            "component_version", "component_commit", "config_changed",
                            "rollback", "error", "installation_result"):
                    if key in response:
                        report[key] = response[key]
                report.update(result=response["result"], component_contract_version=item["version"],
                              prepared=response.get("profile_prepared") is True)
                # Exact product outcome, including blocked/partial/unknown.
                return finish(code, report)
            elif op in {"release-prepare", "release-install", "release-rollback"}:
                report["accepted"] = True
                if op == "release-prepare":
                    releases.prepare(r, info, report)
                else:
                    releases.install(r, info, report)
                state(report, releases.layout())
                report.update(result="success", checks_passed=True)
            else:
                # Diagnostics and isolated hardware tests belong to fixed host
                # operations, not commands executed with production-user access.
                raise contract.Refused("operation_requires_host_route")
            code = 0
    except Exception as e:
        partial = report["phase"] in {"stop_requested", "stopped", "pointer_changed", "key_changed", "start_requested"}
        report.update(result="partial" if partial else "rejected",
                      error=str(e) if isinstance(e, (contract.Refused, g.Refused))
                           and re.fullmatch(r"[a-z_]{1,80}", str(e)) else "operation_unconfirmed",
                      checks_passed=False)
        if partial:
            report["rollback"] = "manual_review_required"
        try:
            state(report, releases.layout())
        except Exception:
            report.update(service_running=None, running_commit=None, renderer_pid=None)
    return finish(code, report)

def finish(code, report):
    report.update(finished_at=utc(), exit_code=code)
    return code, report

def main():
    if (os.geteuid() == 0 or sys.argv[1:]
            or "NoNewPrivs:\t1" not in Path("/proc/self/status").read_text()):
        return 2
    os.umask(0o077)
    try:
        r = contract.request(sys.stdin.buffer.read(1025))
        code, report = execute(r)
    except Exception:
        code, report = 2, {"api_version": 1, "result": "rejected",
                          "error": "invalid_request_or_environment",
                          "checks_passed": False, "audio_acceptance": "not_performed"}
    print(json.dumps(report, separators=(",", ":")))
    return code

if __name__ == "__main__":
    sys.exit(main())
