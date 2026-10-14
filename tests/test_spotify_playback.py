"""Real owned inert HTTP process; no Spotify daemon, session or audio."""
from contextlib import contextmanager
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


def test_owned_cli_pause_resume_and_absolute_volume(ready, tmp_path, monkeypatch, capsys):
    with peer(tmp_path) as (child, port, log):
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


def test_foreign_listener_gets_no_http_bytes(tmp_path):
    with peer(tmp_path) as (_child, port, log):
        with pytest.raises(ExclusiveProfileError, match="ownership was not confirmed"):
            playback.control("127.0.0.1", port, str(os.getpid()), "pause", None, lambda: None)
        assert records(log) == []


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
    state = STATUS | {"paused": False, "volume": 0}
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
            self.send_header("Content-Length", str(len(encoded) if code == 200 else 0))
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if code == 200:
                self.wfile.write(encoded)
        def do_GET(self):
            self.record()
            self.respond(state, 204 if behavior == "no_session" else 200)
        def do_POST(self):
            encoded = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            payload = json.loads(encoded) if encoded else None
            self.record(payload)
            if behavior == "drop":
                self.close_connection = True
                return
            if behavior != "ignore":
                if self.path == "/player/volume": state["volume"] = payload["volume"]
                else: state["paused"] = self.path == "/player/pause"
            self.respond(None, close=behavior == "close")
    server = HTTPServer(("127.0.0.1", 0), Handler)
    print(server.server_port, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    run_peer(sys.argv[2], sys.argv[3])
