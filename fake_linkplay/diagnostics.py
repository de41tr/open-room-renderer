"""Private, leased source diagnostics. No request/body serialization or playback I/O."""
from __future__ import annotations
import asyncio
import contextvars
import datetime as dt
from enum import Enum
import functools
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import uuid

from .queue_diagnostics import QueueRejectionReason
from .soap import READ_ACTIONS, MUTATING_ACTIONS, SERVICE_ALIASES

SCHEMA = 1
WINDOW = 120
MAX_EVENTS = 1000
MAX_BYTES = 983040  # <=960 KiB; leave 64 KiB for the host's compact summary.
LEASE = "diagnostic-window.json"
BUFFER = "diagnostic-buffer.json"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,79}")
CODE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
SHA = re.compile(r"[0-9a-f]{40}")
PHASES = {"COMMAND_ACCEPTED","LOADING","BUFFERING","PLAYING","PAUSED","STOPPED","ERROR"}
HTTP_READS = frozenset("getStatusEx getStatus getPlayerStatus getPlayerStatusEx getVolume getMute getDeviceList getNetworkStatus getSlaveList getMultiroom getSafety getAudioOutput getLanguage getPresetInfo getKeyMapping getDeviceName getMetaInfo".split())
# Fixed command names authored in execute(); arguments are never labels.
HTTP_ACTIONS = HTTP_READS | frozenset(['Button_Enable_GET', 'GetAcousticCapability', 'GetFadeFeature', 'LED_SWITCH_STATUS_GET', 'Reload_Button_GET', 'VOL_LED_SWITCH_GET', 'alexaLanguageListGet', 'audio_cast:get_speaker_list', 'checkBackupSettingState', 'getAlexaCountry', 'getAlexaProfile', 'getAllRoutines', 'getAsrStatus', 'getAudioInputEnable', 'getAudioInputStreamState', 'getAvsDevInfo', 'getChannelBalance', 'getCoaxOutMaxCap', 'getDebugInfo', 'getDeviceInfo', 'getDeviceNameChangeable', 'getDigitalInputAudioTypeSupport', 'getEndTrackEnable', 'getLMPFilterCapability', 'getLowPriorityPromptDisable', 'getMetaInfo', 'getModeRename', 'getMvRemoteSilenceUpdateTime', 'getMvRemoteUpdateStart', 'getMvRemoteUpdateStartCheck', 'getMvRemoteUpdateStatus', 'getNewAudioOutputHardwareMode', 'getNewVersion', 'getPlayerStatus', 'getPlayerStatusEx', 'getPowerModeTime', 'getPowerState', 'getPresetInfo', 'getRoutineCapability', 'getShutdown', 'getSpdifOutMaxCap', 'getSpdifOutSwitchDelayMs', 'getStaticIpInfo', 'getStatus', 'getStatusEx', 'getSubLPF', 'getTotalAudioInsertState', 'getTriggeroutStatus', 'get_bt_latency', 'getbthistory', 'getbtstatus', 'loopmode', 'm3u_play', 'mute', 'next', 'onepause', 'pause', 'play', 'playlist', 'prev', 'resume', 'seek', 'setTimezone', 'setTuneinLocation', 'stop', 'volume'])
HTTP_PREFIXES = ('SetStreamServicesCapability:Deactive:', 'SetStreamServicesCapability:Active:', 'getAudioOutputVrmsSupportList', 'getAuxVoltageSupportList', 'setTuneinLocation:', 'setChannelBalance:', 'MCUKeyShortClick:', 'setHexDeviceName:', 'setDeviceName:', 'getStatus:ip:', 'setTimezone:', 'EQ')
SUBCOMMANDS = frozenset("play playlist m3u_play loopmode seek volume mute pause resume onepause stop next prev".split())
ENDPOINTS = {"PlayQueue":"play_queue","AVTransport":"av_transport","RenderingControl":"rendering_control","ConnectionManager":"connection_manager"}
CURRENT = contextvars.ContextVar("orr_diagnostic_request", default=None)

class Reason(Enum):
    UNKNOWN = "unknown"
    OBSERVED = "request_observed"
    COMPLETED = "request_completed"
    STATE = "state_observed"
    ACCEPTED = "command_accepted"
    INVALID_ACTION = "invalid_action"
    INVALID_ARGS = "invalid_args"
    TRANSITION = "transition_unavailable"
    BACKEND = "backend_operation_failed"
    CONFIRMATION = "backend_confirmation_failed"
    SOURCE_RESOLUTION = "source_resolution"
    SOURCE_UNREADY = "source_unready"
    SOURCE_EOF = "source_eof"
    CONFIRMED = "backend_transport_confirmed"
    READY = "backend_audio_ready_observed"
    OWNERSHIP = "source_ownership_lost"
    CANCELLED = "cancelled"
    API = "api_failure"
    CAPABILITY = "capability_unavailable"
    SPOTIFY_MEDIA_RESTRICTED = "spotify_media_restricted"
    SPOTIFY_KEY_REFUSED_1 = "spotify_key_refused_code_1"
    SPOTIFY_KEY_REFUSED_OTHER = "spotify_key_refused_other_code"
    SPOTIFY_RESTRICTED_UNKNOWN = "spotify_load_restricted_unknown"
    SPOTIFY_ERROR_OTHER = "spotify_playback_error_other"
    SPOTIFY_EVENTS_CONNECTED = "spotify_events_connected"
    SPOTIFY_EVENTS_UNAVAILABLE = "spotify_events_unavailable"
    SPOTIFY_EVENTS_DISCONNECTED = "spotify_events_disconnected"
    SPOTIFY_EVENTS_LIMIT = "spotify_events_limit"

def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def phase(state):
    value = str(getattr(state, "phase", ""))
    return value if value in PHASES else "unknown"

def generation(state):
    value = getattr(state, "generation", None)
    return value if type(value) is int and 0 <= value <= 10**9 else None

def reason_value(reason):
    # New fixed members of these checked enums need no privileged host allowlist update.
    value = reason.value if isinstance(reason, (Reason, QueueRejectionReason)) else "unknown"
    return value if type(value) is str and CODE.fullmatch(value) else "unknown"

def read_private(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o600 or s.st_size > maximum:
            raise ValueError("invalid_private_diagnostic")
        with os.fdopen(fd, "rb", closefd=False) as f:
            raw = f.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError("oversized_private_diagnostic")
        return json.loads(raw)
    finally:
        os.close(fd)

def write_private(path, data):
    fd, name = tempfile.mkstemp(prefix=".diagnostic-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(data)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def software():
    try:
        version = importlib.metadata.version("open-room-renderer")
    except importlib.metadata.PackageNotFoundError:
        version = "0.1.0"
    if not re.fullmatch(r"[0-9][A-Za-z0-9.+-]{0,47}", version):
        version = "0.1.0"
    commit = None
    try:
        p = Path.cwd() / "installation-receipt.json"
        if p.stat().st_size <= 32768:
            value = json.loads(p.read_text())["source"]["revision"]
            if type(value) is str and SHA.fullmatch(value):
                commit = value
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return version, commit

class Trace:
    def __init__(self, owner, protocol, endpoint, action, subcommand, peer, read):
        self.owner = owner
        self.test_id = owner.window["test_id"]
        owner.serial += 1
        self.request_id = owner.process + "-" + str(owner.serial)
        self.protocol, self.endpoint, self.action = protocol, endpoint, action
        self.subcommand, self.peer, self.read = subcommand, peer, read
        self.started = time.monotonic()
        self.before = phase(owner.state())
        self.before_generation = generation(owner.state())
        self.reason = Reason.COMPLETED
        self.soap_code = None
        self.uncertain = False

    def event(self, stage, outcome, reason, http_code=None):
        self.owner.add(self, stage, outcome, reason, http_code)

    def finish(self, status):
        outcome = ("unknown" if self.uncertain or status is None or self.action == "unknown"
                   else "rejected" if status >= 400 else "completed")
        reason = Reason.UNKNOWN if self.action == "unknown" or status is not None and status>=400 and self.reason is Reason.COMPLETED else self.reason
        if (self.read and outcome=="completed" and status==200 and self.soap_code is None
                and self.reason is Reason.COMPLETED and self.before==phase(self.owner.state())
                and self.before_generation==generation(self.owner.state())):
            self.owner.remove_read_start(self)
        self.event("command", outcome, reason, status)

class Diagnostics:
    def __init__(self, state_dir, state, *, version=None, commit=None):
        self.directory = Path(state_dir)
        self.state = state
        found_version, found_commit = software()
        self.version = found_version if version is None else version
        self.commit = found_commit if commit is None else commit
        self.process = uuid.uuid4().hex[:16]
        self.serial = 0
        self.window = None
        self.events = []
        self.sizes = []
        self.size = 0
        self.compacted = self.dropped = self.overwritten = self.io_failures = 0
        self.deadline = 0.0
        self.links = {}
        self.last_state = None
        self.task = None
        self.next_tick = 0.0
        self.dirty = False
        self.salt = os.urandom(16)
        self.blocked_test = None

    def activate(self, lease):
        if type(lease) is not dict or set(lease) != {"test_id","started_at","expires_at","commit"}:
            return False
        if type(lease["test_id"]) is not str or not ID.fullmatch(lease["test_id"]) or type(lease["commit"]) is not str or not SHA.fullmatch(lease["commit"]) or lease["commit"] != self.commit:
            return False
        start = dt.datetime.fromisoformat(lease["started_at"])
        end = dt.datetime.fromisoformat(lease["expires_at"])
        now = dt.datetime.now(dt.timezone.utc)
        if start.tzinfo is None or end.tzinfo is None or (end-start).total_seconds() != WINDOW or not start <= now < end:
            return False
        self.window = {**lease, "ready_at":utc(), "finished_at":None, "status":"recording"}
        self.deadline = time.monotonic() + min(WINDOW, (end-now).total_seconds())
        self.events, self.sizes, self.links = [], [], {}
        self.salt = os.urandom(16)
        self.size = self.compacted = self.dropped = self.overwritten = 0
        self.io_failures = 0
        self.last_state = (generation(self.state()), phase(self.state()))
        self.dirty = True
        return True

    def active(self):
        if self.window and self.window["status"] == "recording" and time.monotonic() >= self.deadline:
            self.window["status"] = "completed"
            self.window["finished_at"] = utc()
            self.dirty = True
        return bool(self.window and self.window["status"] == "recording")

    def tick(self):
        # One bounded I/O task, never awaited by command/backend paths.
        now = time.monotonic()
        if now < self.next_tick or self.task is not None and not self.task.done():
            return
        self.next_tick = now + 0.5
        self.task = asyncio.create_task(self.pump())

    def close(self):
        # The existing final-listener cleanup owns this one I/O task too.
        # Never wait for diagnostic storage on the playback/shutdown path.
        if self.task is not None and not self.task.done():
            self.task.cancel()

    async def pump(self):
        try:
            try:
                lease = await asyncio.to_thread(read_private, self.directory/LEASE, 1024)
            except (OSError, ValueError, TypeError):
                lease = None
            if type(lease) is dict and set(lease)=={"test_id","commit","started_at","expires_at"}:
                end=dt.datetime.fromisoformat(lease["expires_at"])
                if end.tzinfo is not None and (dt.datetime.now(dt.timezone.utc)-end).total_seconds()>7*86400:
                    await asyncio.to_thread((self.directory/BUFFER).unlink,missing_ok=True)
                    await asyncio.to_thread((self.directory/LEASE).unlink,missing_ok=True)
                    self.window=None;self.blocked_test=None
                    return
            if lease and lease.get("test_id") != self.blocked_test and (not self.window or self.window["test_id"] != lease.get("test_id")):
                try:
                    old = await asyncio.to_thread(read_private,self.directory/BUFFER,MAX_BYTES)
                except (OSError,ValueError,TypeError):
                    old = None
                if type(old) is dict and old.get("test_id")==lease.get("test_id") and old.get("process_id")!=self.process:
                    self.blocked_test = lease["test_id"]  # Preserve a pre-crash partial snapshot.
                else:
                    self.activate(lease)
            self.active()
            if self.window and self.dirty:
                data = self.snapshot()
                self.dirty = False
                await asyncio.to_thread(write_private, self.directory/BUFFER, data)
            # Same seven-day, prune-on-next-observation retention as host recordings.
            if self.window and self.window["status"] != "recording":
                end = dt.datetime.fromisoformat(self.window["expires_at"])
                if (dt.datetime.now(dt.timezone.utc)-end).total_seconds() > 7*86400:
                    await asyncio.to_thread((self.directory/BUFFER).unlink, missing_ok=True)
                    self.window = None
        except Exception:
            self.io_failures += 1
            self.dirty = True  # Never propagate diagnostic errors into playback.

    def begin(self, protocol, request):
        if not self.active():
            return None
        endpoint, action, subcommand, read = "unknown", "unknown", None, False
        if protocol == "soap":
            service = SERVICE_ALIASES.get(request.match_info.get("service","").lower())
            if not service:
                service = {"/upnp/transport":"AVTransport","/upnp/rendering":"RenderingControl"}.get(getattr(request,"path",""))
            endpoint = ENDPOINTS.get(service, "unknown")
            candidate = request.headers.get("SOAPACTION","").strip().strip('"').rsplit("#",1)[-1]
            if service and candidate in READ_ACTIONS[service] | MUTATING_ACTIONS[service]:
                action, read = candidate, candidate in READ_ACTIONS[service]
        elif protocol == "web":
            from .web_ui import PREFIX
            if getattr(request, "path", "") != PREFIX + "/command":
                return None  # No assets, state polls, exports or output inspection.
            protocol, endpoint = "http", "web_control"
        else:
            endpoint = "linkplay_http"
            # The renderer's parser already separates URL arguments. No values survive.
            from .http_api import parse_command
            candidate = parse_command(request.query.get("command","")).name
            known = candidate if candidate in HTTP_ACTIONS else next((p.rstrip(":") for p in HTTP_PREFIXES if candidate.startswith(p)), "unknown")
            if known != "unknown":
                action, read = known, known in HTTP_READS
                if request.query.get("command","").startswith("setPlayerCmd:") and candidate in SUBCOMMANDS:
                    subcommand = candidate
        remote = getattr(request,"remote",None)
        peer = "unknown" if type(remote) is not str else hashlib.sha256(self.salt+remote.encode()).hexdigest()[:12]
        trace = Trace(self, protocol, endpoint, action, subcommand, peer, read)
        trace.event("command","unknown",Reason.OBSERVED)
        return trace

    def remove_read_start(self, trace):
        if not self.active():
            return
        for i in range(len(self.events)-1,-1,-1):
            e=self.events[i]
            if e["request_id"]==trace.request_id and e["reason"]==Reason.OBSERVED.value and e["read_only"]:
                self.events.pop(i);self.size-=self.sizes.pop(i);self.compacted+=1
                self.dirty=True
                break

    def add(self, trace, stage, outcome, reason, http_code=None):
        if not self.active() or trace.test_id != self.window["test_id"]:
            return
        at = utc()
        current = self.state()
        event = {"schema_version":SCHEMA, "test_id":self.window["test_id"], "commit":self.commit,
                 "software_version":self.version, "read_only":trace.read, "at":at, "request_id":trace.request_id,
                 "last_request_id":trace.request_id, "protocol":trace.protocol,
                 "endpoint":trace.endpoint, "action":trace.action, "subcommand":trace.subcommand,
                 "peer":trace.peer, "stage":stage, "outcome":outcome, "reason":reason_value(reason),
                 "http_code":http_code, "soap_code":trace.soap_code,
                 "duration_ms":round(max(0,time.monotonic()-trace.started)*1000,3),
                 "generation_before":trace.before_generation, "generation_after":generation(current),
                 "state_before":trace.before, "state_after":phase(current),
                 "count":1, "first_at":at, "last_at":at}
        compact = (trace.read and stage=="command" and outcome=="completed" and http_code==200
                   and trace.soap_code is None and event["generation_before"]==event["generation_after"]
                   and event["state_before"]==event["state_after"])
        ignored = {"at","request_id","last_request_id","duration_ms","count","first_at","last_at"}
        if compact and self.events:
            previous = self.events[-1]
            if all(previous.get(k)==v for k,v in event.items() if k not in ignored):
                self.size -= self.sizes[-1]
                previous["count"] += 1
                previous["last_at"], previous["last_request_id"] = at, trace.request_id
                previous["duration_ms"] = max(previous["duration_ms"], event["duration_ms"])
                self.sizes[-1] = len(json.dumps(previous,separators=(",",":")).encode())
                self.size += self.sizes[-1]
                self.compacted += 1
                self.dirty = True
                return
        raw_size = len(json.dumps(event,separators=(",",":")).encode())
        if raw_size > 2048:
            self.dropped += 1
            self.dirty = True
            return
        while self.events and (len(self.events)>=MAX_EVENTS or self.size+raw_size>MAX_BYTES-8192):
            victim = next((i for i,e in enumerate(self.events) if e["outcome"]=="completed" and
                           e["stage"]=="command" and e["read_only"]), None)
            if victim is None and compact:
                self.dropped += 1
                self.dirty = True
                return
            victim = 0 if victim is None else victim
            self.overwritten += self.events[victim]["count"]
            self.events.pop(victim); self.size -= self.sizes.pop(victim)
        self.events.append(event); self.sizes.append(raw_size); self.size += raw_size
        self.dirty = True

    def context_trace(self):
        trace = self.links.get(generation(self.state())) or CURRENT.get()
        if trace and trace.owner is self and self.window and trace.test_id==self.window["test_id"]:
            return trace
        if self.active():
            trace = Trace(self,"backend","unknown","unknown",None,"unknown",False)
            if self.last_state:
                trace.before_generation,trace.before = self.last_state
            return trace
        return None

    def accepted(self):
        trace = self.context_trace()
        if trace:
            gen = generation(self.state())
            if len(self.links)>=32: self.links.pop(next(iter(self.links)))
            self.links[gen] = trace
            trace.event("command","accepted",Reason.ACCEPTED)

    def observed(self):
        if not self.active():
            return
        current = (generation(self.state()),phase(self.state()))
        if current != self.last_state:
            trace = self.context_trace()
            if trace:
                trace.event("state","unknown",Reason.STATE)
            self.last_state = current

    def failure(self, reason):
        trace = self.context_trace()
        if trace:
            trace.uncertain = True
            trace.event("playback","unknown",reason)

    def background(self, reason, test_id):
        # Never associate independently observed provider events with a nearby
        # HTTP command, or admit an observation from a previous diagnostic lease.
        if self.active() and self.window["test_id"] == test_id:
            Trace(self, "backend", "unknown", "unknown", None, "unknown", True).event(
                "playback", "unknown", reason)

    def snapshot(self):
        data = {"schema_version":SCHEMA, "test_id":self.window["test_id"],
                "commit":self.commit, "software_version":self.version, "process_id":self.process,
                **{k:self.window[k] for k in ("started_at","ready_at","expires_at","finished_at","status")},
                "complete":self.window["status"]=="completed", "events":self.events,
                "statistics":{"event_count":len(self.events),"compacted_events":self.compacted,
                              "dropped_events":self.dropped,"overwritten_events":self.overwritten,
                              "io_failures":self.io_failures}}
        raw = json.dumps(data,separators=(",",":"),ensure_ascii=True).encode()
        if len(raw)>MAX_BYTES:
            raise ValueError("source_snapshot_limit")
        return raw

def safe(owner, method, *args):
    try:
        recorder = getattr(owner,"diagnostics",None)
        if recorder is not None:
            return getattr(recorder,method)(*args)
    except Exception:
        return None

def note(reason, *, soap_code=None, uncertain=False, stage=None):
    try:
        trace = CURRENT.get()
        if trace is not None:
            trace.reason = reason
            if type(soap_code) is int and 0<=soap_code<=99999:
                trace.soap_code = soap_code
            trace.uncertain |= uncertain
            if stage:
                trace.event(stage,"unknown" if uncertain else "completed",reason)
    except Exception:
        pass

def source_reason(category):
    # Only categories already authored by SourcePlaybackError, never its message.
    if type(category) is not str:
        return Reason.UNKNOWN
    return {Reason.SOURCE_RESOLUTION.value: Reason.SOURCE_RESOLUTION,
            Reason.SOURCE_UNREADY.value: Reason.SOURCE_UNREADY,
            Reason.SOURCE_EOF.value: Reason.SOURCE_EOF}.get(category, Reason.UNKNOWN)

def backend_failure(exc, *, soap_code=None):
    from .http_api import SourcePlaybackError
    reason = source_reason(exc.category) if isinstance(exc, SourcePlaybackError) else Reason.BACKEND
    note(reason, soap_code=soap_code, uncertain=True)

def web_command(command):
    try:
        from .web_ui import _SCHEMAS
        trace = CURRENT.get()
        if trace is not None and trace.endpoint == "web_control" and command.action in _SCHEMAS:
            trace.action = command.action  # Decoded, fixed vocabulary; no arguments.
    except Exception:
        pass

def uncertain():
    try:
        trace = CURRENT.get()
        if trace is not None:
            trace.uncertain = True
            if trace.reason is Reason.COMPLETED:
                trace.reason = Reason.UNKNOWN
    except Exception:
        pass

def soap_failure(exc):
    reason = exc.queue_rejection if isinstance(exc.queue_rejection,QueueRejectionReason) else {
        401:Reason.INVALID_ACTION,402:Reason.INVALID_ARGS,701:Reason.TRANSITION}.get(exc.code,Reason.UNKNOWN)
    note(reason,soap_code=exc.code)

def diagnosed(protocol):
    def decorate(method):
        @functools.wraps(method)
        async def wrapped(owner, request):
            trace = safe(owner,"begin",protocol,request)
            if trace is None:
                return await method(owner,request)
            token = CURRENT.set(trace)
            try:
                response = await method(owner,request)
                try: trace.finish(response.status)
                except Exception: pass
                return response
            except BaseException as exc:
                try:
                    trace.uncertain = True; trace.reason = Reason.CANCELLED if isinstance(exc,asyncio.CancelledError) else Reason.UNKNOWN
                    trace.finish(None)
                except Exception: pass
                raise
            finally:
                CURRENT.reset(token)
        return wrapped
    return decorate
