#!/usr/bin/python3 -I
"""Product-owned EQ driver, preserving the proven v1 config/lock/backend guards."""
import contextlib
import fcntl
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import types

VERSION="1"
HOME=Path.home()
UID=os.geteuid();GID=os.getegid()
BASE=HOME/".local/share/open-room-renderer"
CONFIG=HOME/".config/open-room-renderer/config.yaml"
ROOT=BASE/"components"
PROFILE=ROOT/"spotify-eq"
CLIENT=ROOT/"spotify-eq-client-v0.10.3"
BACKUPS=BASE/"spotify-eq-control-backups"
UNIT="open-room-renderer.service"
ID=re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,79}")
SHA=re.compile(r"[0-9a-f]{40}")
PIN="4dbc099f46da3529adaa85feb97c4f49deb0b130"
BINARY="58f9c34e807345c6d4924326f946d20c2f253ced10805a34aaffd92f1e7bafff"
PORT=3679
GUARD_SHA="379f8320acb24f2b9f663ee86fb2da79ec6129bb95eed85fc3e8fc82a746cf20"
ENV={"PATH":"/usr/bin:/bin","HOME":str(HOME),"LANG":"C.UTF-8",
     "XDG_RUNTIME_DIR":"/run/user/"+str(UID),"DBUS_SESSION_BUS_ADDRESS":"unix:path=/run/user/"+str(UID)+"/bus"}
class Refused(Exception):pass

def utc():return dt.datetime.now(dt.timezone.utc).isoformat()
def unique(pairs):
    result={}
    for k,v in pairs:
        if k in result:raise Refused("duplicate_field")
        result[k]=v
    return result
def request(raw):
    if len(raw)>1024:raise Refused("oversized_request")
    r=json.loads(raw,object_pairs_hook=unique)
    if type(r) is not dict or any(type(v) is not str for v in r.values()):raise Refused("invalid_fields")
    if set(r)=={"request_id","commit","profile"} and r["profile"]=="spotify-eq":
        pass
    elif set(r)=={"request_id","commit","operation"} and r["operation"] in {"spotify-eq-activate","spotify-eq-deactivate"}:
        pass
    else:raise Refused("invalid_fields")
    if not ID.fullmatch(r["request_id"]) or not SHA.fullmatch(r["commit"]):raise Refused("invalid_id_or_sha")
    return r
def load_guard():
    from . import host_runtime
    return host_runtime
def command(argv,timeout=30):
    # Never surface command output/exception text; bounded private scratch only.
    with tempfile.TemporaryFile() as out:
        p=subprocess.run([str(x) for x in argv],stdin=subprocess.DEVNULL,stdout=out,
                         stderr=subprocess.DEVNULL,env=ENV,timeout=timeout)
        if out.tell()>131072:raise Refused("command_output_limit")
        out.seek(0);data=out.read()
    if p.returncode:raise Refused("fixed_command_failed")
    return data
def native(info,snippet,*args):
    return json.loads(command([info["release"]/"venv/bin/python","-I","-c",snippet,*args]))
def cli(info,*args,timeout=45):
    return json.loads(command([info["release"]/"venv/bin/open-room-renderer","spotify",*args],timeout))
FEATURES='''import json
from fake_linkplay.config import Config
from fake_linkplay.spotify_eq import setup, _profile
from fake_linkplay.spotify_client import BINARY_SHA256
from fake_linkplay.spotify_exclusive import UPSTREAM_VERSION,UPSTREAM_COMMIT
assert "spotify_eq_profile" in Config.__dataclass_fields__
assert callable(setup) and callable(_profile)
assert UPSTREAM_VERSION=="v0.10.3" and UPSTREAM_COMMIT=="4dbc099f46da3529adaa85feb97c4f49deb0b130"
assert BINARY_SHA256["armv7l"]=="58f9c34e807345c6d4924326f946d20c2f253ced10805a34aaffd92f1e7bafff"
print('{"contract":1}')
'''
VERIFY_CLIENT='''import json,sys
from fake_linkplay.spotify_client import verify_client
_,r=verify_client(sys.argv[1])
print(json.dumps({k:r[k] for k in ("version","commit","binary_sha256")}))
'''
VERIFY_PROFILE='''import json,sys
from pathlib import Path
from fake_linkplay.spotify_eq import _profile
p=_profile(Path(sys.argv[1]))
assert p["client_dir"]==sys.argv[2] and p["client_sha256"]==sys.argv[3]
assert p["name"]=="XU4 Spotify EQ" and p["bitrate"]==160 and p["port"]==3679
print('{"profile":1}')
'''

def ensure_private(p,g):
    if os.path.lexists(p):
        fd=g.private_dir(p);os.close(fd)
    else:
        p.mkdir(mode=0o700)
        fd=g.private_dir(p);os.close(fd)
@contextlib.contextmanager
def private_profile(g):
    """Admit only the settings and optional existing, demonstrably idle lock.

    Probe the renderer's actual flock protocol using a separate read-only
    descriptor. Never create, truncate, replace, unlink or chmod run.lock.
    The probe is released before the existing service acquires its own lock.
    """
    directory=g.private_dir(PROFILE);lock=None
    def identity(s):
        return s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_ctime_ns
    def verify():
        if identity(PROFILE.lstat())!=original_directory or set(os.listdir(directory))!=names:
            raise Refused("profile_changed_during_check")
        if lock is not None:
            named=os.stat("run.lock",dir_fd=directory,follow_symlinks=False)
            if identity(named)!=original_lock or identity(os.fstat(lock))!=original_lock:
                raise Refused("profile_lock_changed")
    try:
        original_directory=identity(os.fstat(directory))
        names=set(os.listdir(directory))
        if names not in ({"spotify-eq.json"},{"spotify-eq.json","run.lock"}):
            raise Refused("profile_not_quiescent")
        g.regular(PROFILE/"spotify-eq.json")
        if "run.lock" in names:
            entry=os.stat("run.lock",dir_fd=directory,follow_symlinks=False)
            if (not stat.S_ISREG(entry.st_mode) or (entry.st_uid,entry.st_gid)!=(UID,GID)
                    or stat.S_IMODE(entry.st_mode)!=0o600 or entry.st_nlink!=1):
                raise Refused("profile_lock_invalid")
            original_lock=identity(entry)
            lock=os.open("run.lock",os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK,dir_fd=directory)
            if identity(os.fstat(lock))!=original_lock:raise Refused("profile_lock_changed")
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise Refused("profile_lock_busy") from None
        verify()
        yield
        verify()
    finally:
        try:
            if lock is not None:os.close(lock)
        finally:os.close(directory)
def fixed_client(info):
    r=native(info,VERIFY_CLIENT,str(CLIENT))
    if r!={"version":"v0.10.3","commit":PIN,"binary_sha256":BINARY}:raise Refused("pinned_client_mismatch")
    return r
def profile(info,g):
    with private_profile(g):
        if native(info,VERIFY_PROFILE,str(PROFILE),str(CLIENT),BINARY)!={"profile":1}:raise Refused("profile_mismatch")
def exclusive_off():
    # Never touch/adopt the separate direct receiver or its private state.
    for p in (HOME/".local/share/orr-spotify",
              HOME/".config/systemd/user/open-room-renderer-spotify.service",
              HOME/".config/systemd/user/default.target.wants/open-room-renderer-spotify.service"):
        if os.path.lexists(p):raise Refused("exclusive_receiver_requires_review")
    rows=command(["/usr/bin/systemctl","--user","show","open-room-renderer-spotify.service",
                  "--property=LoadState,ActiveState,MainPID,ControlPID,Job"]).decode()
    values=dict(x.split("=",1) for x in rows.splitlines() if "=" in x)
    if (values.get("LoadState")!="not-found" or values.get("ActiveState")!="inactive"
            or values.get("MainPID")!="0" or values.get("ControlPID")!="0" or values.get("Job") not in ("","0")):
        raise Refused("exclusive_receiver_requires_review")
def producer_pids(g):
    return {pid:p for pid,p in g.processes().items() if p["name"]=="go-librespot"}
def port_clear():
    for p in (Path("/proc/net/tcp"),Path("/proc/net/tcp6")):
        for row in p.read_text().splitlines()[1:]:
            f=row.split()
            if len(f)>3 and int(f[1].rsplit(":",1)[1],16)==PORT:
                raise Refused("control_port_in_use")
def inspect(r,g,report=None):
    try:
        info=g.inspect()
    except Exception:
        # Explicit deactivation is also a safe-return path after a failed start.
        # Never use this branch for preparation or activation.
        if r.get("operation")!="spotify-eq-deactivate":raise
        state=g.service()
        if (state.get("ActiveState") not in ("inactive","failed") or state.get("MainPID")!="0"
                or state.get("ControlPID")!="0" or state.get("Job") not in ("","0")
                or not g.cgroup_empty(state.get("ControlGroup",""))):
            raise Refused("inactive_service_ownership_unknown")
        unit=g.regular(g.UNIT_FILE);raw=g.regular(CONFIG)
        g.validate_unit(unit)
        release=(BASE/"current").resolve(strict=True)
        if release.parent!=BASE/"releases":raise Refused("unexpected_release")
        receipt=g.document(release/"installation-receipt.json")
        commit=receipt.get("source",{}).get("revision")
        config=json.loads(raw,object_pairs_hook=unique)
        if (receipt.get("mode")!="installed" or config.get("mpv_socket")!=str(g.IPC)
                or config.get("state_dir")!=str(BASE/"state") or config.get("auto_spawn_mpv") is not True):
            raise Refused("unexpected_runtime_config")
        info={"commit":commit,"config":raw,"unit":unit,"release":release,
              "receipt":g.regular(release/"installation-receipt.json"),"identity":None,
              "service":state,"boot":g.boot(),"state":g.state_hash(BASE/"state")}
    if info["commit"]!=r["commit"]:raise Refused("requested_commit_not_installed_and_running")
    config=json.loads(info["config"],object_pairs_hook=unique)
    current_config=CONFIG.lstat()
    if (current_config.st_uid,current_config.st_gid)!=(UID,GID):raise Refused("config_owner_mismatch")
    if config.get("spotify_eq_profile") not in (None,str(PROFILE)):raise Refused("foreign_eq_profile")
    if (config.get("mpv_audio_device") not in ("alsa/hw:CARD=KA17,DEV=0","alsa/plughw:CARD=KA17,DEV=0")
            or config.get("mirror_port_80") is not False
            or config.get("bind_address") not in ("0.0.0.0","192.168.178.96")
            or any(type(config.get(k)) is not int or not 1024<=config[k]<=65535 or config[k]==PORT
                   for k in ("http_api_port","upnp_port"))):raise Refused("fixed_output_or_listener_conflict")
    if report is not None:
        version=next((p.get("version") for p in json.loads(info["receipt"]).get("packages",[]) if p.get("name")=="open-room-renderer"),None)
        report.update(installed_commit=info["commit"],running_commit=info["commit"] if info["identity"] else None,
                      service_running=info["identity"] is not None,
                      renderer_service="active" if info["identity"] else info["service"]["ActiveState"],
                      orr_version=version if type(version) is str and re.fullmatch(r"[0-9][A-Za-z0-9.+-]{0,47}",version) else None)
    # No imported PR code: this interpreter belongs to the verified running
    # native release; root independently verifies its requested SHA on main.
    try:
        supported=native(info,FEATURES)=={"contract":1}
    except Exception:
        supported=False
    if not supported:raise Refused("installed_eq_contract_unverified")
    exclusive_off()
    info["config_data"]=config
    return info
def unchanged(info,g,raw):
    # The sole permitted config difference is handled explicitly by caller.
    if g.regular(CONFIG)!=raw:raise Refused("concurrent_config_change")
    if (g.regular(g.UNIT_FILE)!=info["unit"] or (BASE/"current").resolve(strict=True)!=info["release"]
            or g.regular(info["release"]/"installation-receipt.json")!=info["receipt"]
            or g.boot()!=info["boot"] or g.state_hash(BASE/"state")!=info["state"]):
        raise Refused("preserved_data_changed")

def config_edit(raw,value):
    """Set the fixed key, or remove it entirely when disabling (value=None).

    Splice only the top-level member and its required separator; all unrelated
    value tokens/formatting survive. Never restore an earlier config snapshot.
    """
    text=raw.decode("utf-8");obj=json.loads(text,object_pairs_hook=unique)
    if type(obj) is not dict:raise Refused("native_json_config_required")
    decoder=json.JSONDecoder();i=text.index("{")+1;member=None;previous=None
    while True:
        while text[i].isspace():i+=1
        if text[i]=="}":break
        key_start=i;key,i=decoder.raw_decode(text,i)
        while text[i].isspace():i+=1
        if text[i]!=":":raise Refused("invalid_config")
        i+=1
        while text[i].isspace():i+=1
        value_start=i;_,i=decoder.raw_decode(text,i);value_end=i
        while text[i].isspace():i+=1
        following=i if text[i]=="," else None
        if key=="spotify_eq_profile":
            member=(key_start,value_start,value_end,previous,following)
        if text[i]=="}":break
        if following is None:raise Refused("invalid_config")
        previous=i;i+=1
    expected=dict(obj)
    if value is None:
        expected.pop("spotify_eq_profile",None)
        if member is None:return raw
        start,_,end,preceding,following=member
        if following is not None:end=following+1
        elif preceding is not None:start=preceding
        out=text[:start]+text[end:]
    else:
        encoded=json.dumps(value)
        if member:
            _,start,end,_,_=member;out=text[:start]+encoded+text[end:]
        else:out=text[:i]+("," if obj else "")+'\n  "spotify_eq_profile": '+encoded+text[i:]
        expected["spotify_eq_profile"]=value
    if json.loads(out,object_pairs_hook=unique)!=expected:raise Refused("config_projection_failed")
    return out.encode()
def replace_config(g,expected,new):
    # Same-user cooperative locks are required; no arbitrary destination.
    original=CONFIG.lstat()
    if g.regular(CONFIG)!=expected:raise Refused("concurrent_config_change")
    fd,tmp=tempfile.mkstemp(prefix=".orr-eq-",dir=CONFIG.parent)
    try:
        os.fchmod(fd,stat.S_IMODE(original.st_mode))
        with os.fdopen(fd,"wb") as f:f.write(new);f.flush();os.fsync(f.fileno())
        now=CONFIG.lstat()
        if (now.st_dev,now.st_ino,now.st_uid,now.st_gid,now.st_mode)!=(original.st_dev,original.st_ino,original.st_uid,original.st_gid,original.st_mode) or g.regular(CONFIG)!=expected:
            raise Refused("concurrent_config_change")
        os.replace(tmp,CONFIG)
        d=os.open(CONFIG.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(d)
        finally:os.close(d)
    finally:
        if os.path.lexists(tmp):os.unlink(tmp)
def backup(r,g,raw):
    ensure_private(BACKUPS,g)
    p=BACKUPS/r["request_id"];p.mkdir(mode=0o700)
    f=p/"config.before";fd=os.open(f,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,"wb") as h:h.write(raw);h.flush();os.fsync(h.fileno())
    return p

def backend(info,g,enabled):
    # SO_PEERCRED/get_property only; guarded helper never sends play/load/set.
    fd=g.private_dir(g.RUN)
    try:verified=g.verify_backend(info,fd)
    finally:os.close(fd)
    procs=producer_pids(g);identity=g.renderer_identity(g.service(),info["release"])
    if enabled:
        deadline=time.monotonic()+15
        while not procs and time.monotonic()<deadline:
            if g.renderer_identity(g.service(),info["release"])!=identity:raise Refused("renderer_changed_during_verification")
            time.sleep(0.2);procs=producer_pids(g)
        if len(procs)!=1:raise Refused("eq_child_not_verified")
        pid,p=next(iter(procs.items()))
        if (p["ppid"]!=identity[0] or p["uids"]!=(UID,)*4 or g.process_group(pid)!=identity[3]
                or (g.PROC/str(pid)/"exe").resolve(strict=True)!=(CLIENT/"go-librespot").resolve(strict=True)):
            raise Refused("eq_child_not_verified")
        started=g.process_start(pid)
        if g.renderer_identity(g.service(),info["release"])!=identity or g.process_start(pid)!=started:
            raise Refused("eq_child_changed_during_verification")
        verified["spotify_pid"]=pid
    elif procs:raise Refused("eq_child_still_present")
    return verified
def quiescent_stop(info,g):
    g.command("stop")
    now=g.service()
    if not g.stopped(now) or not g.cgroup_empty(info["identity"][3] if info["identity"] else info["service"].get("ControlGroup","")) or not g.cgroup_empty(now.get("ControlGroup","")):
        raise Refused("whole_service_stop_unconfirmed")
    if g.mpvs(g.processes()) or producer_pids(g):raise Refused("foreign_audio_process")
    g.kernel_clear()
def prepare(r,info,g,report):
    if info["config_data"].get("spotify_eq_profile") is not None:raise Refused("eq_must_be_disabled_for_install")
    if producer_pids(g):raise Refused("existing_receiver_process")
    ensure_private(ROOT,g)
    if not os.path.lexists(CLIENT):
        cli(info,"install-client","--download","--output-dir",str(CLIENT),timeout=90)
    client=fixed_client(info);report.update(component_version=client["version"],component_commit=client["commit"])
    if os.path.lexists(PROFILE):
        profile(info,g);result="already_prepared"
    else:
        result=cli(info,"eq","--directory",str(PROFILE),"setup","--client-dir",str(CLIENT),
                   "--name","XU4 Spotify EQ","--bitrate","160","--port",str(PORT))
        if result.get("spotify_eq")!="PREPARED" or result.get("activated") is not False:
            raise Refused("preparation_unconfirmed")
        profile(info,g);result="prepared"
    unchanged(info,g,info["config"])
    if g.renderer_identity(g.service(),info["release"])!=info["identity"] or producer_pids(g):
        raise Refused("nonactivating_preparation_changed_service")
    report.update(installed=True,profile_prepared=True,installation_result=result,activated=False,phase="prepared")
def control(r,info,g,report):
    enabled=r["operation"]=="spotify-eq-activate";old=info["config_data"].get("spotify_eq_profile")
    if enabled:
        client=fixed_client(info)
        report.update(component_version=client["version"],component_commit=client["commit"])
        if native(info,VERIFY_PROFILE,str(PROFILE),str(CLIENT),BINARY)!={"profile":1}:raise Refused("profile_mismatch")
        # Active profile can contain only runtime created by the owned service;
        # no adoption/rewrite. Already-active requests are verified, not restarted.
        if old is None:profile(info,g);port_clear()
    if ((enabled and old==str(PROFILE)) or (not enabled and "spotify_eq_profile" not in info["config_data"])) and info["identity"] is not None:
        report.update(backend(info,g,enabled));report["backend_state"]="verified"
        unchanged(info,g,info["config"])
        report.update(profile_prepared=True if enabled else None,activated=enabled,phase="already_active" if enabled else "already_disabled")
        return
    if old is None and producer_pids(g):raise Refused("existing_receiver_process")
    new=config_edit(info["config"],str(PROFILE) if enabled else None)
    backup(r,g,info["config"]);report["rollback"]="private_backup_available"
    unchanged(info,g,info["config"])
    report["phase"]="stop_requested"
    quiescent_stop(info,g)
    report["phase"]="stopped"
    unchanged(info,g,info["config"])
    if new!=info["config"]:
        replace_config(g,info["config"],new);report["config_changed"]=True
    report["phase"]="key_changed"
    # Config object remains exactly equal except the one startup-only key.
    expected=dict(info["config_data"])
    if enabled:expected["spotify_eq_profile"]=str(PROFILE)
    else:expected.pop("spotify_eq_profile",None)
    if json.loads(g.regular(CONFIG),object_pairs_hook=unique)!=expected:
        raise Refused("config_projection_failed")
    g.command("start");report["phase"]="start_requested"
    updated={**info,"config":new}
    report.update(backend(updated,g,enabled));report["backend_state"]="verified"
    unchanged(info,g,new)
    report.update(profile_prepared=True if enabled else None,activated=enabled,phase="activated" if enabled else "deactivated",rollback="not_needed")

def execute(r,preflight=False):
    op="component-install" if "profile" in r else "spotify-eq-control"
    report={**r,"operation":op,"requested_operation":r.get("operation",op),"schema_version":1,
        "dispatcher_version":VERSION,"started_at":utc(),"finished_at":None,"result":"unknown",
        "phase":"preflight","accepted":False,"checks_passed":False,"installed":False if op=="component-install" else None,"profile_prepared":None,"activated":None,
        "installation_result":"not_started","installed_commit":None,"running_commit":None,"service_running":None,
        "renderer_service":"unknown","orr_version":None,"backend_state":"not_checked","component_version":None,"component_commit":None,
        "config_changed":False,"rollback":"not_needed","automatic_retry":False,
        "account_test":"not_performed","hearing_test":"not_performed","discovery_readiness":"unknown",
        "preflight_only":preflight}
    code=2;g=None;info=None
    try:
        g=load_guard();info=inspect(r,g,report)
        report.update(installed_commit=info["commit"],running_commit=info["commit"] if info["identity"] else None,
                      service_running=info["identity"] is not None,
                      renderer_service="active" if info["identity"] else info["service"]["ActiveState"])
        if preflight:
            report.update(result="static_preflight_passed",activated=info["config_data"].get("spotify_eq_profile") is not None)
            code=0
        else:
            report.update(accepted=True,phase="preparing" if op=="component-install" else "control_preflight")
            if op=="component-install":prepare(r,info,g,report)
            else:control(r,info,g,report)
            report.update(result="success",checks_passed=True)
            code=0
    except Exception as e:
        report["result"]="blocked_preflight" if report["phase"]=="preflight" else "partial" if report["phase"] in {"stop_requested","stopped","key_changed","start_requested"} else "failed"
        report["error"]=str(e) if isinstance(e,(Refused,getattr(g,"Refused",Refused))) and re.fullmatch(r"[a-z_]+",str(e)) else "operation_unconfirmed"
        if report["phase"] in {"stop_requested","stopped","key_changed","start_requested"}:
            report.update(rollback="manual_review_required",activated=None)
    if g is not None and info is not None:
        try:
            state=g.service();active=state.get("ActiveState")=="active"
            report.update(renderer_service=state.get("ActiveState") if state.get("ActiveState") in {"active","inactive","failed","activating","deactivating"} else "unknown",
                          service_running=active,running_commit=info["commit"] if active and
                          g.renderer_identity(state,info["release"]) else None)
        except Exception:report.update(service_running=None,running_commit=None,renderer_service="unknown")
    report.update(finished_at=utc(),exit_code=code)
    return code,report
def main():
    if os.geteuid()==0 or "NoNewPrivs:\t1" not in Path("/proc/self/status").read_text() or sys.argv[1:] not in ([],["--preflight"]):return 2
    os.umask(0o077)
    r=request(sys.stdin.buffer.read(1025));code,report=execute(r,preflight=sys.argv[1:]==["--preflight"])
    print(json.dumps(report,separators=(",",":")));return code
if __name__=="__main__":
    try:sys.exit(main())
    except Exception:
        print('{"result":"rejected","error":"invalid_request_or_environment","checks_passed":false}');sys.exit(2)
