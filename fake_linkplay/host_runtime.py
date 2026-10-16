#!/usr/bin/python3 -I
"""Native layout v1 ownership/process/IPC guards; no path from a request."""
import configparser
import shlex
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess
import sys
import time

VERSION="1"
OP="renderer-recover-stale-ipc"
HOME=Path.home()
UID=os.geteuid();GID=os.getegid()
BASE=HOME/".local/share/open-room-renderer"
CONFIG=HOME/".config/open-room-renderer/config.yaml"
UNIT_FILE=HOME/".config/systemd/user/open-room-renderer.service"
UNIT="open-room-renderer.service"
RUN=BASE/"run"
IPC=RUN/"mpv.sock"
LAYOUT="xu4-native-v1"
CGROOT=Path("/sys/fs/cgroup")
PROC=Path("/proc")
ID=re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,79}")
SHA=re.compile(r"[0-9a-f]{40}")
ENV={"GIT_CONFIG_NOSYSTEM":"1","GIT_CONFIG_GLOBAL":"/dev/null","GIT_TERMINAL_PROMPT":"0","PATH":"/usr/bin:/bin","HOME":str(HOME),"LANG":"C.UTF-8",
     "XDG_RUNTIME_DIR":"/run/user/"+str(UID),"DBUS_SESSION_BUS_ADDRESS":"unix:path=/run/user/"+str(UID)+"/bus"}
class Refused(Exception): pass

def utc(): return dt.datetime.now(dt.timezone.utc).isoformat()
def unique(pairs):
    result={}
    for k,v in pairs:
        if k in result: raise Refused("duplicate_field")
        result[k]=v
    return result

def private_dir(path):
    # Open every component without symlink traversal; retain the final dirfd.
    path=Path(path)
    if not path.is_absolute() or ".." in path.parts: raise Refused("unexpected_directory")
    fd=os.open("/",os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            new=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=new
            s=os.fstat(fd)
            if s.st_uid not in (0,UID) or s.st_mode&0o022: raise Refused("unsafe_directory")
        s=os.fstat(fd)
        if s.st_uid!=UID or s.st_gid!=GID or stat.S_IMODE(s.st_mode)!=0o700:
            raise Refused("private_directory_required")
        return fd
    except BaseException:
        os.close(fd);raise

def regular(path,maximum=131072):
    p=Path(path);parent=private_dir(p.parent)
    try:
        s=os.stat(p.name,dir_fd=parent,follow_symlinks=False)
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=UID or s.st_mode&0o022 or s.st_size>maximum:
            raise Refused("unsafe_control_file")
        fd=os.open(p.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        try:
            current=os.fstat(fd)
            if (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid)!=(current.st_dev,current.st_ino,current.st_mode,current.st_uid,current.st_gid):
                raise Refused("control_file_changed")
            data=os.read(fd,maximum+1)
            if len(data)>maximum: raise Refused("control_file_limit")
            return data
        finally:os.close(fd)
    finally:os.close(parent)

def document(path): return json.loads(regular(path),object_pairs_hook=unique)
def command(action):
    if action not in ("show","stop","start"): raise Refused("invalid_internal_action")
    args=["/usr/bin/systemctl","--user","--no-pager",action,UNIT]
    if action=="show":
        args+=["--property=Id,LoadState,ActiveState,SubState,MainPID,ControlPID,Job,Result,FragmentPath,DropInPaths,NeedDaemonReload,ControlGroup,InvocationID,KillMode,SendSIGKILL,TimeoutStopUSec"]
    result=subprocess.run(args,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL,env=ENV,timeout=60 if action=="stop" else 30)
    if result.returncode or len(result.stdout)>65536: raise Refused("service_operation_unconfirmed")
    return result.stdout.decode("utf-8")
def service():
    values=dict(line.split("=",1) for line in command("show").splitlines() if "=" in line)
    if (values.get("Id")!=UNIT or values.get("LoadState")!="loaded"
            or values.get("FragmentPath")!=str(UNIT_FILE) or values.get("DropInPaths")
            or values.get("NeedDaemonReload")!="no" or values.get("KillMode")!="control-group"
            or values.get("SendSIGKILL")!="yes" or values.get("TimeoutStopUSec")!="45s"):
        raise Refused("unexpected_effective_unit")
    return values
def stopped(info):
    return (info.get("ActiveState")=="inactive" and info.get("SubState")=="dead"
            and info.get("MainPID")=="0" and info.get("ControlPID")=="0"
            and info.get("Job") in ("","0") and info.get("Result")=="success")
def cgroup_empty(group):
    if not group:return True
    prefix=f"/user.slice/user-{UID}.slice/user@{UID}.service/"
    if not group.startswith(prefix) or ".." in group.split("/") or not group.endswith("/"+UNIT):
        raise Refused("unexpected_cgroup")
    root=CGROOT/group.lstrip("/")
    if not (CGROOT/"cgroup.controllers").is_file():raise Refused("cgroup_v2_required")
    if not root.exists():return True
    paths=[root,*root.rglob("*")]
    if len(paths)>1024:raise Refused("cgroup_inspection_limit")
    for p in paths:
        if p.name!="cgroup.procs" and p!=root:continue
        target=p/"cgroup.procs" if p==root else p
        try:
            if target.read_text().strip():return False
        except FileNotFoundError:
            if root.exists():raise Refused("cgroup_inspection_raced")
    return not (root/"cgroup.procs").exists() or not (root/"cgroup.procs").read_text().strip()

def processes():
    result={}
    entries=list(PROC.iterdir())
    if len(entries)>8192:raise Refused("process_inspection_limit")
    for p in entries:
        if not p.name.isdecimal():continue
        try:
            raw=(p/"status").read_text()
            name=re.search(r"^Name:\s*(.*)$",raw,re.M)
            parent=re.search(r"^PPid:\s*(\d+)$",raw,re.M)
            uids=re.search(r"^Uid:\s*(.*)$",raw,re.M)
            if not (name and parent and uids):raise Refused("process_identity_unknown")
            result[int(p.name)]={"name":name[1],"ppid":int(parent[1]),"uids":tuple(map(int,uids[1].split()))}
        except FileNotFoundError:continue
        except PermissionError:raise Refused("process_visibility_unknown") from None
    return result
def mpvs(table):return {pid:p for pid,p in table.items() if p["name"]=="mpv"}
def process_group(pid):
    rows=(PROC/str(pid)/"cgroup").read_text().splitlines()
    if len(rows)!=1 or not rows[0].startswith("0::"):raise Refused("process_cgroup_unknown")
    return rows[0][3:]
def process_start(pid):
    raw=(PROC/str(pid)/"stat").read_text()
    return raw[raw.rfind(")")+2:].split()[19]

def renderer_identity(info,release):
    if info.get("ActiveState")!="active" or info.get("SubState")!="running":raise Refused("renderer_not_active")
    pid=int(info.get("MainPID","0"))
    if pid<=0 or not re.fullmatch(r"[0-9a-f]{32}",info.get("InvocationID","")):raise Refused("renderer_identity_unknown")
    p=processes().get(pid)
    if not p or p["uids"]!=(UID,)*4 or process_group(pid)!=info["ControlGroup"]:
        raise Refused("renderer_owner_unknown")
    if (PROC/str(pid)/"cwd").resolve(strict=True)!=release:raise Refused("running_release_mismatch")
    argv=(PROC/str(pid)/"cmdline").read_bytes().rstrip(b"\0").split(b"\0")
    expected=[os.fsencode(BASE/"current/venv/bin/open-room-renderer"),b"serve",b"--config",os.fsencode(CONFIG)]
    if argv[-4:]!=expected:raise Refused("unexpected_renderer_command")
    return pid,process_start(pid),info["InvocationID"],info["ControlGroup"]

def kernel_clear():
    rows=(PROC/"net/unix").read_text().splitlines()
    for row in rows[1:]:
        fields=row.split(maxsplit=7)
        if len(fields)>=8 and (fields[7]==str(IPC) or fields[7]=="mpv.sock" or fields[7].endswith("/mpv.sock")):
            raise Refused("kernel_socket_present_or_ambiguous")
def socket_stat(fd,*,new=False):
    s=os.stat("mpv.sock",dir_fd=fd,follow_symlinks=False)
    modes=(0o600,0o700) if new else (0o600,)
    if (not stat.S_ISSOCK(s.st_mode) or s.st_uid!=UID or s.st_gid!=GID
            or stat.S_IMODE(s.st_mode) not in modes or s.st_nlink!=1):
        raise Refused("unexpected_socket_object")
    return s
def socket_id(s):
    return s.st_dev,s.st_ino,s.st_uid,s.st_gid,s.st_mode,s.st_ctime_ns
def directory_stable(fd):
    s=RUN.lstat();held=os.fstat(fd)
    if not stat.S_ISDIR(s.st_mode) or (s.st_dev,s.st_ino)!=(held.st_dev,held.st_ino):
        raise Refused("runtime_directory_changed")

def state_hash(root):
    fd=private_dir(root);os.close(fd)
    entries=sorted(root.rglob("*"))
    if len(entries)>4096:raise Refused("state_inventory_limit")
    digest=hashlib.sha256();total=0
    for p in entries:
        s=p.lstat();digest.update(os.fsencode(p.relative_to(root)));digest.update(str((s.st_uid,s.st_gid,stat.S_IMODE(s.st_mode))).encode())
        if stat.S_ISLNK(s.st_mode):digest.update(os.fsencode(os.readlink(p)))
        elif stat.S_ISDIR(s.st_mode):continue
        elif stat.S_ISREG(s.st_mode):
            if s.st_size>10485760:raise Refused("state_inventory_limit")
            total+=s.st_size
            if total>20971520:raise Refused("state_inventory_limit")
            fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            try:
                if os.fstat(fd).st_ino!=s.st_ino:raise Refused("state_changed")
                digest.update(os.read(fd,s.st_size+1))
            finally:os.close(fd)
        else:raise Refused("state_special_entry")
    return digest.hexdigest()
def boot():
    p=UNIT_FILE.parent/"default.target.wants"/UNIT
    if p.is_symlink():return ("link",os.readlink(p))
    if os.path.lexists(p):raise Refused("unexpected_boot_entry")
    return ("absent",None)
def inspect():
    config_raw=regular(CONFIG);unit=regular(UNIT_FILE)
    validate_unit(unit)
    config=json.loads(config_raw,object_pairs_hook=unique)
    if (config.get("mpv_socket")!=str(IPC) or config.get("auto_spawn_mpv") is not True
            or config.get("state_dir")!=str(BASE/"state")):raise Refused("unexpected_runtime_config")
    release=(BASE/"current").resolve(strict=True)
    if release.parent!=BASE/"releases":raise Refused("unexpected_release")
    receipt=document(release/"installation-receipt.json")
    commit=receipt.get("source",{}).get("revision")
    if receipt.get("mode")!="installed" or type(commit) is not str or not SHA.fullmatch(commit):
        raise Refused("invalid_native_receipt")
    info=service();identity=renderer_identity(info,release)
    return {"config":config_raw,"unit":unit,"release":release,"receipt":regular(release/"installation-receipt.json"),
            "commit":commit,"boot":boot(),"state":state_hash(BASE/"state"),"identity":identity,"service":info}
def preserved(before):
    if (regular(CONFIG)!=before["config"] or regular(UNIT_FILE)!=before["unit"]
            or (BASE/"current").resolve(strict=True)!=before["release"]
            or regular(before["release"]/"installation-receipt.json")!=before["receipt"]
            or boot()!=before["boot"] or state_hash(BASE/"state")!=before["state"]):
        raise Refused("preserved_data_changed")

def volume_reply(connection):
    # One read-only query after SO_PEERCRED validation. Never send load/play/set.
    request_id=918273
    connection.sendall(json.dumps({"command":["get_property","volume"],"request_id":request_id}).encode()+b"\n")
    buffer=b"";deadline=time.monotonic()+5;lines=0
    while time.monotonic()<deadline:
        connection.settimeout(max(0.01,deadline-time.monotonic()))
        chunk=connection.recv(4096)
        if not chunk:raise Refused("ipc_reply_incomplete")
        buffer+=chunk
        if len(buffer)>65536:raise Refused("ipc_reply_limit")
        while b"\n" in buffer:
            raw,buffer=buffer.split(b"\n",1);lines+=1
            if lines>32:raise Refused("ipc_reply_limit")
            row=json.loads(raw,object_pairs_hook=unique)
            if type(row) is not dict:raise Refused("ipc_reply_invalid")
            if row.get("request_id")!=request_id:continue
            value=row.get("data")
            if row.get("error")!="success" or type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=100:
                raise Refused("volume_readback_failed")
            return value
    raise Refused("volume_readback_timeout")

def verify_backend(before,fd):
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        info=service()
        if info.get("ActiveState")=="failed":raise Refused("renderer_start_failed")
        if info.get("ActiveState")=="active":
            try:
                current=socket_stat(fd,new=True)
                table=processes();players=mpvs(table)
                if len(players)==1:break
            except FileNotFoundError:pass
        time.sleep(0.2)
    else:raise Refused("backend_readiness_timeout")
    identity=renderer_identity(info,before["release"])
    pid=next(iter(players));p=players[pid]
    if (p["uids"]!=(UID,)*4 or p["ppid"]!=identity[0] or process_group(pid)!=identity[3]
            or (PROC/str(pid)/"exe").resolve(strict=True)!=Path("/usr/bin/mpv").resolve(strict=True)):
        raise Refused("mpv_child_not_verified")
    mpv_started=process_start(pid)
    directory_stable(fd)
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
        connection.settimeout(2);connection.connect(str(IPC))
        peer=struct.unpack("3i",connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,struct.calcsize("3i")))
        if peer!=(pid,UID,GID):raise Refused("ipc_peer_not_verified")
        if socket_id(socket_stat(fd,new=True))!=socket_id(current):raise Refused("socket_identity_changed")
        value=volume_reply(connection)
        if (renderer_identity(service(),before["release"])!=identity or process_group(pid)!=identity[3]
                or process_start(pid)!=mpv_started
                or socket_id(socket_stat(fd,new=True))!=socket_id(current)):
            raise Refused("backend_changed_during_readback")
    preserved(before)
    return {"renderer_pid":identity[0],"mpv_pid":pid,"volume_readback":value,"running_commit":before["commit"]}



def validate_unit(raw):
    """Semantic layout contract; description/comments are not a release binding."""
    p=configparser.ConfigParser(interpolation=None,strict=True)
    p.optionxform=str
    try:p.read_string(raw.decode("utf-8"))
    except Exception:raise Refused("unsupported_unit_layout") from None
    expected={
      "Type":"simple","WorkingDirectory":str(BASE/"current"),
      "ExecStart":None,"Restart":"on-failure","RestartSec":"5",
      "TimeoutStopSec":"45","UMask":"0077","NoNewPrivileges":"yes",
      "Environment":"PYTHONUNBUFFERED=1"}
    if set(p.sections())!={"Unit","Service","Install"} or set(p["Service"])!=set(expected):
        raise Refused("unsupported_unit_layout")
    if (set(p["Unit"])!={"Description","After"} or p["Unit"]["After"]!="network.target"
            or dict(p["Install"])!={"WantedBy":"default.target"}):
        raise Refused("unsupported_unit_layout")
    for key,value in expected.items():
        if value is not None and p["Service"][key]!=value:raise Refused("unsupported_unit_layout")
    if shlex.split(p["Service"]["ExecStart"])!=[str(BASE/"current/venv/bin/open-room-renderer"),"serve","--config",str(CONFIG)]:
        raise Refused("unsupported_unit_layout")
