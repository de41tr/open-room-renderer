"""Real owned inert HTTP process; no Spotify daemon, session or audio."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from fake_linkplay import cli, spotify_playback as playback, spotify_receiver as receiver
from fake_linkplay.spotify_exclusive import ExclusiveProfileError
from test_spotify_diagnostic import STATUS, SECRET
from test_spotify_exclusive import profile
from test_spotify_receiver import ready, stopped

TRACK = {"name": "Synthetic track", "artist_names": ["Synthetic artist", "Another artist"],
         "album_name": "Synthetic album", "duration": 180000, "position": 42500,
         "uri": SECRET, "artist_uris": [SECRET], "album_uri": SECRET,
         "album_cover_url": "https://example.test/" + SECRET, "raw_error": SECRET}


@contextmanager
def peer(tmp_path, behavior="normal"):
    log = tmp_path / "requests.jsonl"
    log.touch()
    child = subprocess.Popen([sys.executable, __file__, "--peer", str(log), behavior],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        yield child, int(child.stdout.readline()), log
    finally:
        child.terminate()
        child.wait(timeout=3)


def records(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def configure_owned_peer(ready, monkeypatch, child, port):
    class Manager:
        stopped = receiver.UserServices.stopped
        def __init__(self, *_args): pass
        def wait_started(self, mode): return "a" * 32, str(child.pid)
        def cgroup_empty(self, group): return True
        def validate(self):
            return {"renderer": stopped(), "spotify": stopped() | {
                "ActiveState": "active", "InvocationID": "a" * 32, "MainPID": str(child.pid),
                "ControlGroup": Path(f"/proc/{child.pid}/cgroup").read_text().strip()[3:]}}

    monkeypatch.setattr(receiver, "UserServices", Manager)
    monkeypatch.setattr(cli, "api_get", lambda *_args: pytest.fail("renderer HTTP contacted"))
    data = receiver._json(ready / "receiver.json")
    receiver._write(ready / "receiver.json", data | {"port": port})
    config = receiver._json(ready / "sidecar/config.yml")
    config["server"]["port"] = port
    receiver._write(ready / "sidecar/config.yml", config)


def test_owned_cli_pause_resume_and_absolute_volume(ready, tmp_path, monkeypatch, capsys):
    with peer(tmp_path) as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        for command in (["pause"], ["resume"], ["volume", "7"]):
            assert cli.main(["spotify", "receiver", "--directory", str(ready), *command]) == 0
            captured = capsys.readouterr()
            assert SECRET not in captured.out + captured.err and not captured.err
            result = json.loads(captured.out)
            assert result["request_outcome"] == "ACKNOWLEDGED"
            assert result["audio_output"] == "UNVERIFIED" and result["spotify_bypasses_orr_dsp"]
            after = result["provider_after"]["status"]
            assert after["volume"] == 7 if command[0] == "volume" else after["paused"] == (command[0] == "pause")
        requests = records(log)
        assert [(r[0], r[1]) for r in requests] == [
            pair for action in ("pause", "resume", "volume")
            for pair in (("GET", "/status"), ("POST", "/player/" + action), ("GET", "/status"))]
        assert requests[-2][2] == {"volume": 7, "relative": False}


def test_owned_cli_next_previous_and_absolute_seek(ready, tmp_path, monkeypatch, capsys):
    with peer(tmp_path) as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        monkeypatch.setattr(receiver, "_write", lambda *_args: pytest.fail("control persisted metadata"))
        commands = (["next"], ["previous"], ["seek", "0"], ["seek", "45000"], ["seek", "180000"])
        for command in commands:
            assert cli.main(["spotify", "receiver", "--directory", str(ready), *command]) == 0
            captured = capsys.readouterr()
            assert not captured.err and SECRET not in captured.out and "Synthetic track" not in captured.out
            result = json.loads(captured.out)
            assert result["command"] == command[0] and result["request_outcome"] == "ACKNOWLEDGED"
            assert result["playback_effect"] == result["audio_output"] == "UNVERIFIED"
            if command[0] == "seek":
                assert result["requested_position_ms"] == int(command[1])
                assert result["provider_after"]["status"]["position_ms"] == int(command[1])
                assert result["provider_before"]["status"]["duration_ms"] == 180000
        requests = records(log)
        assert requests[1::3] == [["POST", "/player/next", None], ["POST", "/player/prev", None],
                                  *[["POST", "/player/seek", {"position": value, "relative": False}]
                                    for value in (0, 45000, 180000)]]
        assert all(r == ["GET", "/status", None] for r in requests[0::3] + requests[2::3])


def test_control_rejects_invalid_values_before_connecting(monkeypatch):
    monkeypatch.setattr(playback, "_owned_connection", lambda *_args: pytest.fail("invalid value connected"))
    for action, value in (("seek", -1), ("seek", True), ("seek", 1.5), ("seek", 2**63), ("seek", None),
                          ("volume", 101), ("volume", False), ("next", 1), ("previous", 0),
                          ("repeat-context", 1), ("repeat-track", 0), ("shuffle", "on"), ("shuffle", None)):
        with pytest.raises(ExclusiveProfileError):
            playback.control("127.0.0.1", 1, "1", action, value, lambda: None)


def test_owned_cli_explicit_repeat_and_shuffle_options(ready, tmp_path, monkeypatch, capsys):
    with peer(tmp_path) as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        monkeypatch.setattr(receiver, "_write", lambda *_args: pytest.fail("option persisted metadata"))
        options = {"repeat-context": "repeat_context", "repeat-track": "repeat_track", "shuffle": "shuffle_context"}
        expected = {field: STATUS[field] for field in options.values()}
        commands = [(action, value) for action in options for value in ("on", "off")]
        commands.insert(1, ("repeat-context", "on"))  # Explicit set, never a read/invert toggle.
        for action, value in commands:
            assert cli.main(["spotify", "receiver", "--directory", str(ready), action, value]) == 0
            captured = capsys.readouterr()
            assert not captured.err and SECRET not in captured.out and "Synthetic track" not in captured.out
            result = json.loads(captured.out)
            field = options[action]
            assert result["command"] == action and result["requested_" + field] is (value == "on")
            assert result["request_outcome"] == "ACKNOWLEDGED"
            assert result["playback_effect"] == result["audio_output"] == "UNVERIFIED"
            assert {key: result["provider_before"]["status"][key] for key in expected} == expected
            expected[field] = value == "on"
            after = result["provider_after"]["status"]
            assert {key: after[key] for key in expected} == expected
            assert set(after) == set(expected) | {"stopped", "paused", "buffering", "volume", "volume_steps", "volume_fraction"}
        requests = records(log)
        assert requests[1::3] == [["POST", "/player/" + options[action], {options[action]: value == "on"}]
                                  for action, value in commands]
        assert all(r == ["GET", "/status", None] for r in requests[0::3] + requests[2::3])


def test_option_cli_requires_explicit_on_or_off(monkeypatch, capsys):
    monkeypatch.setattr(receiver, "playback_control", lambda *_args: pytest.fail("invalid option reached control"))
    for command in (["repeat-context"], ["repeat-track", "toggle"], ["shuffle", SECRET]):
        with pytest.raises(SystemExit) as exc:
            cli.main(["spotify", "receiver", "--directory", "/unused", *command])
        captured = capsys.readouterr()
        assert exc.value.code == 2 and not captured.out and SECRET not in captured.err


@pytest.mark.parametrize("behavior, command", [("no_session", "repeat-context"), ("stopped", "shuffle")])
def test_options_require_existing_playback(ready, tmp_path, monkeypatch, capsys, behavior, command):
    with peer(tmp_path, behavior) as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        assert cli.main(["spotify", "receiver", "--directory", str(ready), command, "on"]) == 1
        captured = capsys.readouterr()
        assert not captured.out and captured.err and SECRET not in captured.err
        assert records(log) == [["GET", "/status", None]]


@pytest.mark.parametrize("behavior, command, field", [("ignore", "shuffle", "shuffle_context"),
    ("close", "repeat-context", "repeat_context"), ("bad_option_after", "repeat-track", "repeat_track")])
def test_option_acknowledgement_preserves_readback_limits_without_retry(
        ready, tmp_path, monkeypatch, capsys, behavior, command, field):
    with peer(tmp_path, behavior) as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        assert cli.main(["spotify", "receiver", "--directory", str(ready), command, "on"]) == (0 if behavior == "ignore" else 1)
        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert not captured.err and SECRET not in captured.out and "Synthetic track" not in captured.out
        assert result["request_outcome"] == "ACKNOWLEDGED"
        assert result["playback_effect"] == result["audio_output"] == "UNVERIFIED"
        if behavior == "ignore":
            assert result["provider_after"]["status"][field] is False
        else:
            assert result["provider_after"] is None and "error" in result
        assert len(records(log)) == (2 if behavior == "close" else 3)
        assert records(log)[1] == ["POST", "/player/" + field, {field: True}]


@pytest.mark.parametrize("behavior, value", [("normal", "-1"), ("normal", str(2**63)),
                                          ("normal", "180001"), ("no_session", "1000"),
                                          ("stopped", "1000"), ("missing_track", "1000"),
                                          ("partial_track", "1000")])
def test_seek_cli_rejects_invalid_or_unknown_target_without_post(ready, tmp_path, monkeypatch, capsys, behavior, value):
    with peer(tmp_path, behavior) as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        assert cli.main(["spotify", "receiver", "--directory", str(ready), "seek", value]) == 1
        captured = capsys.readouterr()
        assert not captured.out and captured.err and SECRET not in captured.err
        assert records(log) == ([] if value in {"-1", str(2**63)} else [["GET", "/status", None]])


@pytest.mark.parametrize("behavior", ["ignore", "drop", "close"])
def test_seek_cli_acknowledgement_never_claims_effect_or_retries(ready, tmp_path, monkeypatch, capsys, behavior):
    with peer(tmp_path, behavior) as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        assert cli.main(["spotify", "receiver", "--directory", str(ready), "seek", "45000"]) == (0 if behavior == "ignore" else 1)
        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert not captured.err and SECRET not in captured.out
        assert result["playback_effect"] == result["audio_output"] == "UNVERIFIED"
        assert result["request_outcome"] == ("UNCONFIRMED" if behavior == "drop" else "ACKNOWLEDGED")
        if behavior == "ignore":
            assert result["provider_after"]["status"]["position_ms"] == TRACK["position"]
        else:
            assert result["provider_after"] is None and "error" in result
        assert len(records(log)) == (3 if behavior == "ignore" else 2)
        assert records(log)[1] == ["POST", "/player/seek", {"position": 45000, "relative": False}]


def test_owned_now_playing_cli_reads_only_status_and_never_saves_metadata(ready, tmp_path, monkeypatch, capsys):
    with peer(tmp_path) as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        monkeypatch.setattr(receiver, "_write", lambda *_args: pytest.fail("now-playing persisted metadata"))
        assert cli.main(["spotify", "receiver", "--directory", str(ready), "now-playing"]) == 0
        captured = capsys.readouterr()
        assert SECRET not in captured.out + captured.err and not captured.err
        result = json.loads(captured.out)
        assert result["track"] == {"title": "Synthetic track", "artists": ["Synthetic artist", "Another artist"],
                                   "album": "Synthetic album", "duration_ms": 180000, "position_ms": 42500}
        assert result["status"]["paused"] is False and result["status"]["stopped"] is False
        assert result["command"] == "now-playing" and result["track_observation"] == "PRESENT"
        assert result["session_observation"] == "SESSION_PRESENT"
        assert result["observation"] == "sidecar_reported" and result["snapshot_freshness"] == "snapshot_only"
        assert result["listener_ownership"] == "owned_connection" and result["audio_output"] == "UNVERIFIED"
        assert result["spotify_bypasses_orr_dsp"]
        assert records(log) == [["GET", "/status", None]]


@pytest.mark.parametrize("behavior, observation", [("no_session", "NO_TRACK"), ("no_track", "NO_TRACK"),
                                                  ("missing_track", "UNKNOWN"), ("partial_track", "PRESENT")])
def test_now_playing_keeps_absent_data_unknown(tmp_path, behavior, observation):
    with peer(tmp_path, behavior) as (child, port, log):
        result = playback.now_playing("127.0.0.1", port, str(child.pid), lambda: None)
        assert result["track_observation"] == observation
        assert result["track"] == ({"title": "Synthetic track", "artists": None, "album": None,
                                     "duration_ms": None, "position_ms": None} if behavior == "partial_track" else None)
        assert result["session_observation"] == ("NO_SESSION" if behavior == "no_session" else "SESSION_PRESENT")
        assert len(records(log)) == 1


def test_now_playing_projection_rejects_wrong_types_and_ignores_unrequested_fields():
    for field, value in (("name", {"raw_error": SECRET}), ("album_name", []), ("artist_names", SECRET),
                         ("artist_names", [None]), ("duration", True), ("position", -1), ("duration", 2**63)):
        with pytest.raises(ExclusiveProfileError) as exc:
            playback._project_track({"track": TRACK | {field: value}})
        assert SECRET not in str(exc.value)
    result = playback._project_track({"track": TRACK, "next_track": {"name": SECRET}})
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("read_only", [False, True])
def test_foreign_listener_gets_no_http_bytes(tmp_path, read_only):
    with peer(tmp_path) as (_child, port, log):
        with pytest.raises(ExclusiveProfileError, match="ownership was not confirmed"):
            if read_only:
                playback.now_playing("127.0.0.1", port, str(os.getpid()), lambda: None)
            else:
                playback.control("127.0.0.1", port, str(os.getpid()), "pause", None, lambda: None)
        assert records(log) == []


@pytest.mark.parametrize("fail_at", [1, 2])
def test_now_playing_invocation_change_discards_metadata(tmp_path, fail_at):
    checks = []
    def fence():
        checks.append(True)
        if len(checks) == fail_at:
            raise ExclusiveProfileError("Fixture invocation changed.")
    with peer(tmp_path) as (child, port, log):
        with pytest.raises(ExclusiveProfileError, match="invocation changed"):
            playback.now_playing("127.0.0.1", port, str(child.pid), fence)
        assert len(records(log)) == fail_at - 1


@pytest.mark.parametrize("failure", ["not_selected", "renderer_cgroup", "restarted"])
def test_now_playing_service_fences_block_unselected_or_changed_client(ready, tmp_path, monkeypatch, capsys, failure):
    with peer(tmp_path) as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        manager = receiver.UserServices
        validate = manager.validate
        checks = []
        def changed(self):
            checks.append(True)
            states = validate(self)
            if failure == "not_selected":
                states["spotify"] = stopped()
            elif failure == "restarted" and len(checks) == 3:
                states["spotify"]["InvocationID"] = "b" * 32
            return states
        monkeypatch.setattr(manager, "validate", changed)
        if failure == "renderer_cgroup":
            monkeypatch.setattr(manager, "cgroup_empty", lambda *_args: False)
        assert cli.main(["spotify", "receiver", "--directory", str(ready), "now-playing"]) == 1
        captured = capsys.readouterr()
        assert not captured.out and SECRET not in captured.err and "Synthetic track" not in captured.err
        assert len(records(log)) == (1 if failure == "restarted" else 0)


@pytest.mark.parametrize("behavior", ["bad_track", "http_error", "redirect", "slow_status"])
def test_now_playing_failure_has_no_metadata_raw_error_or_retry(ready, tmp_path, monkeypatch, capsys, behavior):
    with peer(tmp_path, behavior) as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        monkeypatch.setattr(playback, "HTTP_DEADLINE_SECONDS", .2)
        assert cli.main(["spotify", "receiver", "--directory", str(ready), "now-playing"]) == 1
        captured = capsys.readouterr()
        assert not captured.out and SECRET not in captured.err and "Synthetic track" not in captured.err
        assert len(records(log)) == 1


def test_invocation_change_between_status_and_post_blocks_mutation(tmp_path):
    checks = []
    def fence():
        checks.append(True)
        if len(checks) == 2:
            raise ExclusiveProfileError("Fixture invocation changed.")
    with peer(tmp_path) as (child, port, log):
        with pytest.raises(ExclusiveProfileError, match="invocation changed"):
            playback.control("127.0.0.1", port, str(child.pid), "resume", None, fence)
        assert [(r[0], r[1]) for r in records(log)] == [("GET", "/status")]


@pytest.mark.parametrize("behavior", ["ignore", "drop", "close", "no_session"])
def test_acknowledgement_is_separate_from_effect_and_never_retries(tmp_path, behavior):
    with peer(tmp_path, behavior) as (child, port, log):
        if behavior == "no_session":
            with pytest.raises(ExclusiveProfileError, match="no session"):
                playback.control("127.0.0.1", port, str(child.pid), "pause", None, lambda: None)
            assert len(records(log)) == 1
            return
        result = playback.control("127.0.0.1", port, str(child.pid), "pause", None, lambda: None)
        assert SECRET not in json.dumps(result)
        assert result["request_outcome"] == ("UNCONFIRMED" if behavior == "drop" else "ACKNOWLEDGED")
        assert result["audio_output"] == "UNVERIFIED"
        if behavior == "ignore":
            assert result["provider_after"]["status"]["paused"] is False
            assert len(records(log)) == 3
        else:
            assert result["provider_after"] is None and "error" in result
            assert len(records(log)) == 2  # No reconnect, repeated POST or status retry.


@pytest.mark.parametrize("behavior", ["slow_status", "slow_ack"])
def test_slow_drip_headers_cannot_extend_the_operation_deadline(tmp_path, monkeypatch, behavior):
    monkeypatch.setattr(playback, "HTTP_DEADLINE_SECONDS", .2)
    with peer(tmp_path, behavior) as (child, port, log):
        started = time.monotonic()
        if behavior == "slow_status":
            with pytest.raises(ExclusiveProfileError, match="no control was sent"):
                playback.control("127.0.0.1", port, str(child.pid), "pause", None, lambda: None)
        else:
            result = playback.control("127.0.0.1", port, str(child.pid), "pause", None, lambda: None)
            assert result["request_outcome"] == "UNCONFIRMED" and "error" in result
        assert time.monotonic() - started < 1
        assert len(records(log)) == (1 if behavior == "slow_status" else 2)


def run_peer(log, behavior):
    state = STATUS | {"paused": False, "volume": 0, "track": TRACK, "next_track": {"name": SECRET}}
    if behavior == "no_track": state["track"] = None
    elif behavior == "missing_track": del state["track"]
    elif behavior == "partial_track": state["track"] = {"name": "Synthetic track"}
    elif behavior == "bad_track": state["track"] = SECRET
    elif behavior == "stopped": state["stopped"] = True
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *_args): pass
        def record(self, payload=None):
            with open(log, "a") as handle:
                handle.write(json.dumps([self.command, self.path, payload]) + "\n")
        def respond(self, payload, code=200, close=False):
            if ((behavior == "slow_status" and self.command == "GET") or
                    (behavior == "slow_ack" and self.command == "POST")):
                for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n":
                    self.wfile.write(bytes([byte]))
                    time.sleep(.03)
                return
            encoded = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(encoded) if code != 204 else 0))
            if code == 302:
                self.send_header("Location", "https://example.test/" + SECRET)
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if code != 204:
                self.wfile.write(encoded)
        def do_GET(self):
            self.record()
            if behavior.startswith("pairing"):
                if self.path == "/":
                    self.respond({"playback_ready": False})
                    return
                if self.path == "/auth/code":
                    expiry = datetime.now(timezone.utc) + timedelta(minutes=-1 if behavior == "pairing_expired" else 5)
                    payload = {"code": "TEST-CODE", "url": "https://example.test/" + SECRET,
                               "expires_at": expiry.isoformat()}
                    if behavior == "pairing_bad_code": payload["code"] = "\x1b[31m" + SECRET
                    if behavior == "pairing_bad_expiry": payload["expires_at"] = SECRET
                    if behavior == "pairing_extra": payload.update(device_code=SECRET, access_token=SECRET, username=SECRET)
                    code = {"pairing_absent": 204, "pairing_redirect": 302}.get(behavior, 200)
                    self.respond(payload, code)
                    return
            code = {"no_session": 204, "http_error": 401, "redirect": 302}.get(behavior, 200)
            self.respond(state, code)
        def do_POST(self):
            encoded = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            payload = json.loads(encoded) if encoded else None
            self.record(payload)
            if behavior == "drop":
                self.close_connection = True
                return
            if behavior != "ignore":
                if self.path == "/player/volume": state["volume"] = payload["volume"]
                elif self.path == "/player/seek": state["track"]["position"] = payload["position"]
                elif self.path in {"/player/pause", "/player/resume"}: state["paused"] = self.path == "/player/pause"
                elif self.path in {"/player/repeat_context", "/player/repeat_track", "/player/shuffle_context"}:
                    field = self.path.removeprefix("/player/")
                    state[field] = SECRET if behavior == "bad_option_after" else payload[field]
            self.respond(None, close=behavior == "close")
    server = HTTPServer(("127.0.0.1", 0), Handler)
    print(server.server_port, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    run_peer(sys.argv[2], sys.argv[3])
