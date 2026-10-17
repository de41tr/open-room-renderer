"""Selected-main release preparation and pointer changes in the existing layout.

No service operation during preparation. Never restore old config/state bytes.
"""
from __future__ import annotations
import contextlib
import datetime as dt
import time
import signal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from . import host_runtime as g
from .host_contract import Refused, SHA, descriptor

URL = "https://github.com/de41tr/open-room-renderer.git"

def run(argv, timeout=30, *, output=False):
    p = subprocess.run([str(x) for x in argv], env=g.ENV,
                       stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE if output else subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=timeout)
    if p.returncode:
        raise Refused("product_command_failed")
    if output:
        if len(p.stdout) > 65536:
            raise Refused("product_reply_limit")
        return p.stdout

BUILD_PHASES = frozenset({"venv", "build_tools", "pure_dependencies", "yaml",
                         "application", "runtime_install", "inventory", "launcher_validation"})

def run_installer(source, target, report):
    """One 660s attempt; keep safe last phase before failed staging is removed."""
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    begin = time.monotonic()
    process = subprocess.Popen(["/usr/bin/python3", str(source / "scripts/install-native.py"),
                                "--prefix", str(target)],
                               env=g.ENV, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    result = "success"
    try:
        process.wait(timeout=660)
        if process.returncode:
            result = "installer_failed"
    except subprocess.TimeoutExpired:
        result = "installer_timeout"
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
    finally:
        finished_at = dt.datetime.now(dt.timezone.utc).isoformat()
        duration = round(max(0, time.monotonic() - begin), 3)
        phase, phase_started = "unknown", None
        try:
            value = g.document(target / "build-progress.json")
            stamp = dt.datetime.fromisoformat(value["phase_started_at"])
            if (type(value) is dict and value.get("schema_version") == 1
                    and value.get("phase") in BUILD_PHASES and stamp.utcoffset() == dt.timedelta(0)
                    and dt.datetime.fromisoformat(started_at) <= stamp <= dt.datetime.fromisoformat(finished_at)):
                phase, phase_started = value["phase"], value["phase_started_at"]
        except Exception:
            pass
        code = process.returncode
        report["build"] = {"schema_version": 1, "phase": phase, "result": result,
            "started_at": started_at, "finished_at": finished_at, "duration_seconds": duration,
            "phase_started_at": phase_started,
            "exit_code": code if type(code) is int and code >= 0 else None,
            "signal": -code if type(code) is int and code < 0 else None}
    if result != "success":
        raise Refused(result)


def layout():
    raw = g.regular(g.CONFIG)
    unit = g.regular(g.UNIT_FILE)
    g.validate_unit(unit)
    config = json.loads(raw, object_pairs_hook=g.unique)
    release = (g.BASE / "current").resolve(strict=True)
    if release.parent != g.BASE / "releases":
        raise Refused("unsupported_release_layout")
    receipt = g.document(release / "installation-receipt.json")
    revision = receipt.get("source", {}).get("revision")
    if receipt.get("format") != 1 or receipt.get("mode") != "installed" or type(revision) is not str or not SHA.fullmatch(revision):
        raise Refused("invalid_release_receipt")
    if (config.get("state_dir") != str(g.BASE / "state")
            or config.get("mpv_socket") != str(g.IPC)
            or config.get("auto_spawn_mpv") is not True
            or config.get("mpv_audio_device") not in
                {"alsa/hw:CARD=KA17,DEV=0", "alsa/plughw:CARD=KA17,DEV=0"}):
        raise Refused("unsupported_native_layout")
    for directory in (g.BASE / "state", g.RUN):
        fd = g.private_dir(directory)
        os.close(fd)
    service = g.service()
    identity = g.renderer_identity(service, release) if service.get("ActiveState") == "active" else None
    return {"release": release, "commit": revision, "config": raw, "unit": unit,
            "config_data": config, "service": service, "identity": identity,
            "receipt": g.regular(release / "installation-receipt.json"),
            "state": g.state_hash(g.BASE / "state"), "boot": g.boot()}

def unchanged(info, *, process=True):
    if (g.regular(g.CONFIG) != info["config"] or g.regular(g.UNIT_FILE) != info["unit"]
            or (g.BASE / "current").resolve(strict=True) != info["release"]
            or g.boot() != info["boot"] or g.state_hash(g.BASE / "state") != info["state"]):
        raise Refused("concurrent_installation_change")
    if process and g.renderer_identity(g.service(), info["release"]) != info["identity"]:
        raise Refused("renderer_changed_during_preparation")

def private(path):
    if not path.exists():
        path.mkdir(mode=0o700)
    fd = g.private_dir(path)
    os.close(fd)

def save(path, data):
    temporary = path.with_name(path.name + ".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, separators=(",", ":"))
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()

def compatible(target, before):
    receipt = g.document(target / "installation-receipt.json")
    if receipt.get("format") != 1 or receipt.get("mode") != "installed":
        raise Refused("target_not_installed")
    # This is the target release's real configuration checker. It receives the
    # current external config, never a historical backup or generated default.
    try:
        run([target / "venv/bin/open-room-renderer", "config", "check", "--config", g.CONFIG])
    except Exception:
        raise Refused("target_configuration_incompatible") from None
    if (os.path.lexists(g.BASE / "components/spotify-receiver")
            or os.path.lexists(g.HOME/".local/share/orr-spotify")):
        raise Refused("direct_receiver_rebind_required")
    from . import host_eq as eq
    eq.exclusive_off()
    if before["config_data"].get("spotify_eq_profile") is not None:
        if before["config_data"]["spotify_eq_profile"] != str(eq.PROFILE):
            raise Refused("foreign_eq_profile")
        # EQ binds to a verified client pin, not a transient release pathname.
        candidate = {**before, "release": target}
        try:
            from .host_contract import component
            c = component("spotify-eq", descriptor((target/"source/fake_linkplay/host_components.json").read_bytes()))
            if c["driver"] != "spotify-eq-v1" or c["release_binding"] != "client-pin-v1":
                raise Refused("component_binding_incompatible")
            feature = eq.native(candidate, eq.FEATURES)
            eq.fixed_client(candidate)
            if feature != {"contract": 1}:
                raise Refused("component_contract_incompatible")
            if eq.native(candidate, eq.VERIFY_PROFILE, str(eq.PROFILE), str(eq.CLIENT), eq.BINARY) != {"profile": 1}:
                raise Refused("component_contract_incompatible")
        except Exception:
            raise Refused("active_component_target_incompatible") from None
    return receipt

def prepared_path(commit):
    return g.BASE / "management" / (commit + ".json")

def load_prepared(commit):
    data = g.document(prepared_path(commit))
    name = data.get("release_name")
    if (data.get("api_version") != 1 or data.get("commit") != commit
            or type(name) is not str or not name.startswith(commit[:12] + ".")
            or "/" in name or name in {".", ".."} or len(name) > 96):
        raise Refused("invalid_prepared_release")
    target = g.BASE / "releases" / name
    fd = g.private_dir(target)
    os.close(fd)
    if g.document(target / "installation-receipt.json").get("source", {}).get("revision") != commit:
        raise Refused("prepared_commit_mismatch")
    return target

def prepare(r, before, report):
    if before["identity"] is None:
        raise Refused("renderer_not_running")
    private(g.BASE / "management")
    private(g.BASE / "builds")
    private(g.BASE / "releases")
    record = prepared_path(r["commit"])
    if record.exists():
        target = load_prepared(r["commit"])
        compatible(target, before)
        unchanged(before)
        report.update(prepared=True, phase="already_prepared")
        return
    if shutil.disk_usage(g.BASE).free < 256 * 1024 * 1024:
        raise Refused("insufficient_free_space")
    build = Path(tempfile.mkdtemp(prefix="managed-", dir=g.BASE / "builds"))
    target = None
    success = False
    try:
        run(["/usr/bin/git", "clone", "--quiet", "--no-checkout", "--single-branch",
             "--branch", "main", URL, build / "source"], timeout=120)
        repo = build / "source"
        run(["/usr/bin/git", "-C", repo, "merge-base", "--is-ancestor", r["commit"], "refs/remotes/origin/main"])
        run(["/usr/bin/git", "-C", repo, "checkout", "--quiet", "--detach", r["commit"]])
        description = descriptor((repo / "fake_linkplay/host_components.json").read_bytes())
        if description["layout"] != g.LAYOUT:
            raise Refused("target_layout_incompatible")
        target = Path(tempfile.mkdtemp(prefix=r["commit"][:12] + ".", dir=g.BASE / "releases"))
        target.rmdir()
        report["phase"] = "building"
        run_installer(repo, target, report)
        receipt = compatible(target, before)
        if receipt.get("source", {}).get("revision") != r["commit"]:
            raise Refused("prepared_commit_mismatch")
        unchanged(before)
        save(record, {"api_version": 1, "commit": r["commit"], "release_name": target.name})
        report.update(prepared=True, phase="prepared")
        success = True
    finally:
        # Only our unique, never activated staging output. No config/state
        # restoration or removal of another operation's release.
        shutil.rmtree(build)
        if not success and target is not None and target.exists():
            named = [(g.BASE/name).resolve() for name in ("current","previous")]
            if target.parent == g.BASE/"releases" and target not in named:
                shutil.rmtree(target)

def pointer(name, target):
    link = g.BASE / (name + ".management-next")
    if os.path.lexists(link):
        raise Refused("leftover_pointer_transaction")
    link.symlink_to(target)
    os.replace(link, g.BASE / name)

def install(r, before, report):
    rollback = r["operation"] == "release-rollback"
    if rollback:
        target = (g.BASE / "previous").resolve(strict=True)
        if target.parent != g.BASE / "releases":
            raise Refused("invalid_previous_release")
    else:
        target = load_prepared(r["commit"])
    receipt = compatible(target, before)
    if receipt.get("source", {}).get("revision") != r["commit"]:
        raise Refused("target_commit_mismatch")
    unchanged(before)
    report.update(prepared=True, phase="compatible")
    if before["commit"] == r["commit"]:
        from . import host_eq as eq
        report.update(eq.backend(before, g, before["config_data"].get("spotify_eq_profile") is not None))
        report.update(installed=True, phase="already_installed", backend_state="verified")
        return
    if before["identity"] is None:
        raise Refused("renderer_not_running")
    # Persist only recovery metadata; never restore an obsolete private config.
    private(g.BASE / "management")
    save(g.BASE / "management" / (r["request_id"] + ".recovery.json"), {
        "api_version": 1, "previous_commit": before["commit"],
        "previous_release_name": before["release"].name, "commit": r["commit"],
        "config_sha256": hashlib.sha256(before["config"]).hexdigest()})
    from . import host_eq as eq
    report.update(phase="stop_requested", rollback="available_not_executed")
    eq.quiescent_stop(before, g)
    report["phase"] = "stopped"
    unchanged(before, process=False)
    pointer("previous", before["release"])
    pointer("current", target)
    report["phase"] = "pointer_changed"
    g.command("start")
    report["phase"] = "start_requested"
    candidate = {**before, "release": target, "commit": r["commit"],
                 "receipt": g.regular(target/"installation-receipt.json")}
    # Reuse the bounded backend confirmation before reading the new identity.
    # A successful systemctl start alone is not backend acceptance.
    report.update(eq.backend(candidate, g, before["config_data"].get("spotify_eq_profile") is not None))
    after = layout()
    if after["commit"] != r["commit"] or after["identity"] is None:
        raise Refused("running_target_unconfirmed")
    if (after["config"] != before["config"] or after["unit"] != before["unit"]
            or after["state"] != before["state"] or after["boot"] != before["boot"]):
        raise Refused("preserved_data_changed")
    report.update(installed=True, phase="installed", backend_state="verified",
                  rollback="completed" if rollback else "not_needed")
