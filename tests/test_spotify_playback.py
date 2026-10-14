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
from test_spotify_receiver import ready, refresh_services, stopped

TRACK = {"name": "Synthetic track", "artist_names": ["Synthetic artist", "Another artist"],
         "album_name": "Synthetic album", "duration": 180000, "position": 42500,
         "uri": SECRET, "artist_uris": [SECRET], "album_uri": SECRET,
         "album_cover_url": "https://example.test/" + SECRET, "raw_error": SECRET}
PLAY_ID = "0" * 22  # Syntactically valid synthetic ID; never sent to Spotify.


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


@pytest.mark.parametrize("behavior", ["buffering", "stopped", "not_ready"])
def test_stop_playback_owned_cli_allows_cancelling_load_and_inactive_reset(
        ready, tmp_path, monkeypatch, capsys, behavior):
    with peer(tmp_path, behavior) as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        monkeypatch.setattr(receiver, "_write", lambda *_args: pytest.fail("stop saved metadata"))
        monkeypatch.setattr(receiver, "switch", lambda *_args: pytest.fail("stop switched services"))
        monkeypatch.setattr(receiver, "probe_alsa", lambda *_args: pytest.fail("stop probed output"))
        assert cli.main(["spotify", "receiver", "--directory", str(ready), "stop-playback"]) == 0
        captured = capsys.readouterr()
        assert not captured.err and SECRET not in captured.out and "Synthetic track" not in captured.out
        result = json.loads(captured.out)
        assert result["command"] == "stop-playback" and result["request_outcome"] == "ACKNOWLEDGED"
        assert result["playback_effect"] == result["audio_output"] == "UNVERIFIED"
        assert result["spotify_bypasses_orr_dsp"] and "provider_playback_ready" not in result
        assert result["provider_before"]["session_observation"] == "SESSION_PRESENT"
        # The unchanged inert peer ignores stop. Even unchanged buffering or
        # active state must not be promoted from acknowledgement to effect.
        assert result["provider_after"] == result["provider_before"]
        state = result["provider_after"]["status"]
        assert state["stopped"] is (behavior == "stopped")
        assert state["buffering"] is (behavior == "buffering")
        assert records(log) == [["GET", "/status", None], ["POST", "/player/stop", None],
                                ["GET", "/status", None]]


def test_stop_playback_requires_present_session_without_start_or_post(ready, tmp_path, monkeypatch, capsys):
    with peer(tmp_path, "no_session") as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        assert cli.main(["spotify", "receiver", "--directory", str(ready), "stop-playback"]) == 1
        captured = capsys.readouterr()
        assert not captured.out and "no session" in captured.err and SECRET not in captured.err
        assert records(log) == [["GET", "/status", None]]


@pytest.mark.parametrize("code, body", [(204, b""), (200, b'{"private_error":"' + SECRET.encode() + b'"}')])
def test_stop_playback_requires_pinned_null_acknowledgement_without_retry(tmp_path, monkeypatch, code, body):
    request = playback._request
    def changed(connection, method, path, payload=None):
        response = request(connection, method, path, payload)
        return (code, body) if method == "POST" else response
    with peer(tmp_path) as (child, port, log):
        monkeypatch.setattr(playback, "_request", changed)
        result = playback.control("127.0.0.1", port, str(child.pid), "stop-playback", None, lambda: None)
        assert result["request_outcome"] == "UNCONFIRMED" and "error" in result
        assert result["provider_after"] is None and SECRET not in json.dumps(result)
        assert result["playback_effect"] == result["audio_output"] == "UNVERIFIED"
        assert records(log) == [["GET", "/status", None], ["POST", "/player/stop", None]]


@pytest.mark.parametrize("behavior", ["close", "slow_ack"])
def test_stop_playback_closed_or_slow_response_never_reconnects_or_repeats(tmp_path, monkeypatch, behavior):
    monkeypatch.setattr(playback, "HTTP_DEADLINE_SECONDS", .2)
    with peer(tmp_path, behavior) as (child, port, log):
        started = time.monotonic()
        result = playback.control("127.0.0.1", port, str(child.pid), "stop-playback", None, lambda: None)
        assert time.monotonic() - started < 1
        assert result["request_outcome"] == ("ACKNOWLEDGED" if behavior == "close" else "UNCONFIRMED")
        assert "error" in result and result["provider_after"] is None
        assert records(log) == [["GET", "/status", None], ["POST", "/player/stop", None]]


def test_native_yaml_edit_keeps_active_controls_but_blocks_switch_and_guard(ready, tmp_path, monkeypatch):
    with peer(tmp_path) as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        saved = receiver._json(ready / "receiver.json")
        config = Path(saved["renderer_config"])
        edited = json.loads(config.read_text()) | {"friendly_name": "Edited native room"}
        config.write_text(json.dumps(edited) + "\n# Same direct output, edited native YAML.\n")
        # A running Spotify process owns the API; these requests do not start
        # either source or admit the stopped renderer's new configuration.
        manager = receiver.UserServices
        validate = manager.validate
        def running(self):
            states = validate(self)
            states["spotify"]["SubState"] = "running"
            return states
        monkeypatch.setattr(manager, "validate", running)
        monkeypatch.setattr(receiver, "_write", lambda *_args: pytest.fail("control wrote setup metadata"))
        monkeypatch.setattr(receiver, "probe_alsa", lambda *_args: pytest.fail("control probed output"))
        muted = receiver.playback_control(str(ready), "volume", 0)
        assert muted["request_outcome"] == "ACKNOWLEDGED"
        assert muted["provider_after"]["status"]["volume"] == 0
        paused = receiver.playback_control(str(ready), "pause")
        assert paused["request_outcome"] == "ACKNOWLEDGED"
        assert paused["provider_after"]["status"]["paused"] is True
        observed = receiver.playback_control(str(ready), "now-playing")
        assert observed["track"]["title"] == "Synthetic track"
        assert observed["listener_ownership"] == "owned_connection"
        assert all(result["audio_output"] == "UNVERIFIED" for result in (muted, paused, observed))
        assert records(log) == [
            ["GET", "/status", None], ["POST", "/player/volume", {"volume": 0, "relative": False}],
            ["GET", "/status", None], ["GET", "/status", None], ["POST", "/player/pause", None],
            ["GET", "/status", None], ["GET", "/status", None]]
        for mode in ("spotify", "renderer"):
            with pytest.raises(ExclusiveProfileError, match="native renderer profile changed"):
                receiver.switch(str(ready), mode)
            with pytest.raises(ExclusiveProfileError, match="native renderer profile changed"):
                receiver.run_guard(str(ready), mode)
        assert receiver._json(ready / "receiver.json") == saved
        # An unavailable stopped renderer executable likewise cannot strand
        # current controls, but remains a blocker for every future start.
        monkeypatch.setattr(receiver, "_check_mpv", lambda: receiver.fail("Unavailable mpv fixture"))
        assert receiver.playback_control(str(ready), "now-playing")["track"]["title"] == "Synthetic track"
        for mode in ("spotify", "renderer"):
            with pytest.raises(ExclusiveProfileError, match="Unavailable mpv fixture"):
                receiver.run_guard(str(ready), mode)


@pytest.mark.parametrize("damage, message", [
    ("client", "pinned client changed"), ("sidecar", "sidecar configuration changed"),
    ("private-directory", "mode 0700"), ("native-unit", "native renderer service has changed"),
    ("loaded-unit", "exact loaded receiver units")])
def test_active_controls_require_pinned_sidecar_and_owned_units(
        ready, refresh_services, monkeypatch, damage, message):
    states, _busy, _inspected = refresh_services
    states["spotify"].update(ActiveState="active", SubState="running", MainPID="42")
    saved = receiver._json(ready / "receiver.json")
    if damage == "client":
        receiver._write(ready / "receiver.json", saved | {"client_sha256": "f" * 64})
    elif damage == "sidecar":
        config = receiver._json(ready / "sidecar/config.yml")
        receiver._write(ready / "sidecar/config.yml", config | {"audio_device": "hw:CARD=OTHER,DEV=0"})
    elif damage == "private-directory":
        (ready / "sidecar").chmod(0o755)
    elif damage == "native-unit":
        unit = Path(saved["renderer_unit"])
        unit.write_text(unit.read_text() + "\n# Altered native unit.\n")
    else:
        states["spotify"]["NeedDaemonReload"] = "yes"
    monkeypatch.setattr(receiver.UserServices, "wait_started",
                        lambda *_args: pytest.fail("invalid profile reached process inspection"))
    monkeypatch.setattr(playback, "_owned_connection", lambda *_args: pytest.fail("invalid profile reached API"))
    with pytest.raises(ExclusiveProfileError, match=message):
        receiver.playback_control(str(ready), "volume", 0)


def test_play_accepts_three_uri_kinds_without_echo_persistence_or_effect_claim(
        ready, tmp_path, monkeypatch, capsys):
    with peer(tmp_path, "launch_ignore") as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        monkeypatch.setattr(receiver, "_write", lambda *_args: pytest.fail("play persisted metadata"))
        expected = []
        for content_type in ("track", "album", "playlist"):
            uri = "spotify:" + content_type + ":" + PLAY_ID
            assert cli.main(["spotify", "receiver", "--directory", str(ready), "play", uri]) == 0
            captured = capsys.readouterr()
            assert not captured.err and all(value not in captured.out for value in (uri, PLAY_ID, SECRET, "Synthetic track"))
            result = json.loads(captured.out)
            assert result["command"] == "play" and result["requested_content_type"] == content_type
            assert result["replace_current"] is False
            assert result["provider_playback_ready"] is True and result["request_outcome"] == "ACKNOWLEDGED"
            # The peer acknowledges without applying the command. No retry or
            # playback success is inferred, even when readback stays stopped.
            assert result["provider_before"]["status"]["stopped"] is True
            assert result["provider_after"]["status"]["stopped"] is True
            assert result["audio_output"] == result["playback_effect"] == "UNVERIFIED"
            assert result["spotify_bypasses_orr_dsp"] is True
            expected.extend([["GET", "/", None], ["GET", "/status", None],
                             ["POST", "/player/play", {"uri": uri}], ["GET", "/status", None]])
            assert records(log) == expected


def test_play_replace_current_is_explicit_for_playing_and_paused(
        ready, tmp_path, monkeypatch, capsys):
    with peer(tmp_path) as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        receiver.playback_control(str(ready), "volume", 7)
        receiver.playback_control(str(ready), "shuffle", True)
        monkeypatch.setattr(receiver, "_write", lambda *_args: pytest.fail("play persisted metadata"))
        monkeypatch.setattr(receiver, "probe_alsa", lambda *_args: pytest.fail("play probed output"))
        for paused, content_type in ((False, "track"), (True, "album")):
            if paused:
                receiver.playback_control(str(ready), "pause")
            uri = "spotify:" + content_type + ":" + PLAY_ID
            command = ["spotify", "receiver", "--directory", str(ready), "play"]
            offset = len(records(log))
            assert cli.main([*command, uri]) == 1
            captured = capsys.readouterr()
            assert not captured.out and "--replace-current" in captured.err
            assert all(value not in captured.err for value in (uri, PLAY_ID, SECRET))
            assert records(log)[offset:] == [["GET", "/", None], ["GET", "/status", None]]
            offset = len(records(log))
            assert cli.main([*command, "--replace-current", uri]) == 0
            captured = capsys.readouterr()
            assert not captured.err and all(value not in captured.out for value in (uri, PLAY_ID, SECRET, "Synthetic track"))
            result = json.loads(captured.out)
            assert result["replace_current"] is True and result["requested_content_type"] == content_type
            assert result["provider_playback_ready"] is True and result["request_outcome"] == "ACKNOWLEDGED"
            before = result["provider_before"]["status"]
            assert before["paused"] is paused and before["stopped"] is False
            assert before["volume"] == 7 and before["shuffle_context"] is True
            # The inert peer ignores play: acknowledgement cannot prove the
            # new content started or resumed. No URI/option/volume is inferred.
            assert result["provider_after"]["status"] == before
            assert result["playback_effect"] == result["audio_output"] == "UNVERIFIED"
            assert records(log)[offset:] == [["GET", "/", None], ["GET", "/status", None],
                ["POST", "/player/play", {"uri": uri}], ["GET", "/status", None]]


@pytest.mark.parametrize("behavior, reads", [("not_ready", 1), ("no_session", 2), ("buffering", 2)])
def test_replace_current_still_requires_ready_nonbuffering_session(
        ready, tmp_path, monkeypatch, capsys, behavior, reads):
    with peer(tmp_path, behavior) as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        assert cli.main(["spotify", "receiver", "--directory", str(ready),
                         "play", "--replace-current", "spotify:playlist:" + PLAY_ID]) == 1
        captured = capsys.readouterr()
        assert not captured.out and captured.err and SECRET not in captured.err and PLAY_ID not in captured.err
        assert records(log) == [["GET", "/", None], ["GET", "/status", None]][:reads]


def test_play_rejects_noncanonical_or_private_selectors_before_connecting(monkeypatch):
    monkeypatch.setattr(playback, "_owned_connection", lambda *_args: pytest.fail("invalid URI connected"))
    uri = "spotify:track:" + PLAY_ID
    invalid = [None, 1, True, {}, "", "https://open.spotify.com/track/" + PLAY_ID,
               "file:///tmp/" + SECRET, "spotify:local:" + SECRET, "spotify:user:" + SECRET,
               "spotify:collection:tracks", "spotify:episode:" + PLAY_ID,
               "spotify:artist:" + PLAY_ID, "SPOTIFY:track:" + PLAY_ID, uri + "\n",
               " " + uri, uri + "?token=" + SECRET, uri + ":" + SECRET,
               uri[:-1], uri + "0", "spotify:track:" + "é" * 22,
               "spotify:track:" + "0" * 19 + "%30"]
    for value in invalid:
        with pytest.raises(ExclusiveProfileError) as exc:
            playback.control("127.0.0.1", 1, "1", "play", value, lambda: None)
        assert SECRET not in str(exc.value) and PLAY_ID not in str(exc.value)


@pytest.mark.parametrize("behavior, reads", [("launch_not_ready", 1), ("launch_bad_ready", 1),
    ("launch_no_session", 2), ("normal", 2), ("launch_buffering", 2), ("launch_close_root", 1)])
def test_play_requires_ready_stopped_session_and_same_connection(
        ready, tmp_path, monkeypatch, capsys, behavior, reads):
    with peer(tmp_path, behavior) as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        assert cli.main(["spotify", "receiver", "--directory", str(ready),
                         "play", "spotify:track:" + PLAY_ID]) == 1
        captured = capsys.readouterr()
        assert not captured.out and captured.err and SECRET not in captured.err and PLAY_ID not in captured.err
        assert records(log) == [["GET", "/", None], ["GET", "/status", None]][:reads]


@pytest.mark.parametrize("replace_current", [False, True])
def test_play_rechecks_invocation_after_readiness_and_status(
        ready, tmp_path, monkeypatch, capsys, replace_current):
    with peer(tmp_path, "normal" if replace_current else "launch") as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        manager = receiver.UserServices
        validate = manager.validate
        checks = []
        def changed(self):
            checks.append(True)
            states = validate(self)
            if len(checks) == 3:
                states["spotify"]["InvocationID"] = "b" * 32
            return states
        monkeypatch.setattr(manager, "validate", changed)
        options = ["--replace-current"] if replace_current else []
        assert cli.main(["spotify", "receiver", "--directory", str(ready),
                         "play", *options, "spotify:track:" + PLAY_ID]) == 1
        captured = capsys.readouterr()
        assert not captured.out and captured.err and PLAY_ID not in captured.err
        assert records(log) == [["GET", "/", None], ["GET", "/status", None]]


def test_enqueue_owned_cli_appends_one_track_without_echo_or_effect_claim(
        ready, tmp_path, monkeypatch, capsys):
    with peer(tmp_path) as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        monkeypatch.setattr(receiver, "_write", lambda *_args: pytest.fail("enqueue persisted metadata"))
        monkeypatch.setattr(receiver, "probe_alsa", lambda *_args: pytest.fail("enqueue probed output"))
        uri = "spotify:track:" + PLAY_ID
        for paused in (False, True):
            if paused:
                receiver.playback_control(str(ready), "pause")
            offset = len(records(log))
            assert cli.main(["spotify", "receiver", "--directory", str(ready), "enqueue", uri]) == 0
            captured = capsys.readouterr()
            assert not captured.err and all(value not in captured.out for value in (uri, PLAY_ID, SECRET, "Synthetic track"))
            result = json.loads(captured.out)
            assert result["command"] == "enqueue" and result["requested_content_type"] == "track"
            assert result["provider_playback_ready"] is True and result["request_outcome"] == "ACKNOWLEDGED"
            before = result["provider_before"]["status"]
            assert before["paused"] is paused and before["stopped"] is False
            assert result["provider_after"]["status"] == before
            # This existing inert peer ignores enqueue. A 200 and unchanged
            # status prove neither append nor eventual playback or position.
            assert result["queue_effect"] == result["playback_effect"] == result["audio_output"] == "UNVERIFIED"
            assert result["spotify_bypasses_orr_dsp"] is True
            assert records(log)[offset:] == [["GET", "/", None], ["GET", "/status", None],
                ["POST", "/player/add_to_queue", {"uri": uri}], ["GET", "/status", None]]


def test_enqueue_rejects_nontrack_or_noncanonical_selectors_before_connecting(monkeypatch):
    monkeypatch.setattr(playback, "_owned_connection", lambda *_args: pytest.fail("invalid enqueue connected"))
    uri = "spotify:track:" + PLAY_ID
    for value in (None, True, "spotify:album:" + PLAY_ID, "spotify:playlist:" + PLAY_ID,
                  "https://open.spotify.com/track/" + PLAY_ID, uri + "\n", uri[:-1], uri + "?" + SECRET):
        with pytest.raises(ExclusiveProfileError, match="canonical Spotify track URI") as exc:
            playback.control("127.0.0.1", 1, "1", "enqueue", value, lambda: None)
        assert SECRET not in str(exc.value) and PLAY_ID not in str(exc.value)


@pytest.mark.parametrize("behavior", ["not_ready", "no_session", "stopped", "buffering", "no_track"])
def test_enqueue_requires_ready_existing_nonbuffering_playback(
        ready, tmp_path, monkeypatch, capsys, behavior):
    with peer(tmp_path, behavior) as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        assert cli.main(["spotify", "receiver", "--directory", str(ready),
                         "enqueue", "spotify:track:" + PLAY_ID]) == 1
        captured = capsys.readouterr()
        assert not captured.out and captured.err and PLAY_ID not in captured.err and SECRET not in captured.err
        expected = [["GET", "/", None], ["GET", "/status", None]]
        assert records(log) == (expected[:1] if behavior == "not_ready" else expected)


def test_enqueue_rechecks_invocation_before_mutation(tmp_path):
    checks = []
    def fence():
        checks.append(True)
        if len(checks) == 2:
            raise ExclusiveProfileError("Fixture invocation changed.")
    with peer(tmp_path) as (child, port, log):
        with pytest.raises(ExclusiveProfileError, match="invocation changed"):
            playback.control("127.0.0.1", port, str(child.pid), "enqueue", "spotify:track:" + PLAY_ID, fence)
        assert records(log) == [["GET", "/", None], ["GET", "/status", None]]


@pytest.mark.parametrize("behavior", ["drop", "close"])
def test_enqueue_unknown_outcome_warns_about_duplicates_without_retry(
        ready, tmp_path, monkeypatch, capsys, behavior):
    with peer(tmp_path, behavior) as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        assert cli.main(["spotify", "receiver", "--directory", str(ready),
                         "enqueue", "spotify:track:" + PLAY_ID]) == 1
        captured = capsys.readouterr()
        assert not captured.err and PLAY_ID not in captured.out and SECRET not in captured.out
        result = json.loads(captured.out)
        assert result["request_outcome"] == ("UNCONFIRMED" if behavior == "drop" else "ACKNOWLEDGED")
        assert result["provider_after"] is None and result["queue_effect"] == "UNVERIFIED"
        assert "inspect the queue in Spotify" in result["error"] and "duplicates" in result["error"]
        assert records(log) == [["GET", "/", None], ["GET", "/status", None],
            ["POST", "/player/add_to_queue", {"uri": "spotify:track:" + PLAY_ID}]]


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
                          ("volume", 101), ("volume", False), ("next", 1), ("previous", 0), ("stop-playback", True),
                          ("repeat-context", 1), ("repeat-track", 0), ("shuffle", "on"), ("shuffle", None)):
        with pytest.raises(ExclusiveProfileError):
            playback.control("127.0.0.1", 1, "1", action, value, lambda: None)
    for action, replace_current in (("play", 1), ("play", "true"), ("play", None), ("pause", True),
                                    ("now-playing", True), ("pairing-code", True)):
        with pytest.raises(ExclusiveProfileError, match="boolean opt-in for play only"):
            playback.control("127.0.0.1", 1, "1", action, None, lambda: None, replace_current=replace_current)
        with pytest.raises(ExclusiveProfileError, match="boolean opt-in for play only"):
            receiver.playback_control("/unused", action, replace_current=replace_current)


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
                                   "album": "Synthetic album", "duration_ms": 180000, "position_ms": 42500,
                                   "decoded_file": {"format": None, "codec": None, "nominal_bitrate_kbps": None,
                                                    "decoder_sample_rate_hz": None, "source_bit_depth_bits": None}}
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
                                     "duration_ms": None, "position_ms": None,
                                     "decoded_file": {"format": None, "codec": None, "nominal_bitrate_kbps": None,
                                                      "decoder_sample_rate_hz": None, "source_bit_depth_bits": None}
                                     } if behavior == "partial_track" else None)
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


@pytest.mark.parametrize("reported, expected", [
    ({"format": "OGG_VORBIS_160", "codec": "vorbis", "bitrate": 160, "sample_rate": 44100, "bit_depth": None},
     {"format": "OGG_VORBIS_160", "codec": "vorbis", "nominal_bitrate_kbps": 160,
      "decoder_sample_rate_hz": 44100, "source_bit_depth_bits": None}),
    # Format names do not supply missing numeric facts, even when they contain numbers.
    ({"format": "AAC_48", "codec": "aac", "bitrate": None},
     {"format": "AAC_48", "codec": "aac", "nominal_bitrate_kbps": None,
      "decoder_sample_rate_hz": None, "source_bit_depth_bits": None}),
    ({"format": "", "codec": "unknown", "bit_depth": 24},
     {"format": None, "codec": "unknown", "nominal_bitrate_kbps": None,
      "decoder_sample_rate_hz": None, "source_bit_depth_bits": 24}),
])
def test_now_playing_decoded_file_reports_only_supplied_fields(reported, expected):
    value = STATUS | {"track": TRACK | reported, "next_track": {"format": SECRET},
                      "preferred_bitrate": 320, "audio_output": {"sample_rate": 192000, "bit_depth": 32}}
    result = playback._project_track(value)
    assert result["track"]["decoded_file"] == expected
    assert SECRET not in json.dumps(result)
    # Normal status continues ignoring all track/technical metadata, even if malformed.
    assert playback._project_status(value) == playback._project_status(STATUS)
    value["track"]["codec"] = {"raw_error": SECRET}
    assert playback._project_status(value) == playback._project_status(STATUS)


def test_now_playing_decoded_file_null_and_empty_remain_unknown():
    for reported in ({}, {"format": "", "codec": ""},
                     {field: None for field in ("format", "codec", "bitrate", "sample_rate", "bit_depth")}):
        result = playback._project_track({"track": TRACK | reported})["track"]["decoded_file"]
        assert result == {"format": None, "codec": None, "nominal_bitrate_kbps": None,
                          "decoder_sample_rate_hz": None, "source_bit_depth_bits": None}


def test_now_playing_decoded_file_rejects_invalid_fields_without_private_details():
    invalid = [(field, bad) for field in ("bitrate", "sample_rate", "bit_depth")
               for bad in (True, 1.5, "44100", 0, -1, 2**31, {"raw_error": SECRET})]
    invalid += [(field, bad) for field in ("format", "codec")
                for bad in (False, 160, [], {"raw_error": SECRET}, "https://example.test/" + SECRET)]
    invalid += [("format", "OGG_VORBIS_160\n"), ("codec", "VORBIS")]
    for field, bad in invalid:
        with pytest.raises(ExclusiveProfileError) as exc:
            playback._project_track({"track": TRACK | {field: bad}})
        assert str(exc.value) == "Spotify now-playing audio format could not be interpreted."


@pytest.mark.parametrize("bitrate", [160, True])
def test_now_playing_decoded_file_cli_is_one_private_read(ready, tmp_path, monkeypatch, capsys, bitrate):
    request = playback._request
    def reported(connection, method, path, payload=None):
        code, encoded = request(connection, method, path, payload)
        value = json.loads(encoded)
        # Inject only the new synthetic fields after the unchanged inert peer's read.
        value["track"].update(format="OGG_VORBIS_160", codec="vorbis", bitrate=bitrate,
                              sample_rate=44100, bit_depth=None)
        return code, json.dumps(value).encode()
    with peer(tmp_path) as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        monkeypatch.setattr(playback, "_request", reported)
        monkeypatch.setattr(receiver, "_write", lambda *_args: pytest.fail("now-playing persisted metadata"))
        assert cli.main(["spotify", "receiver", "--directory", str(ready), "now-playing"]) == (1 if bitrate is True else 0)
        captured = capsys.readouterr()
        assert SECRET not in captured.out + captured.err
        if bitrate is True:
            assert not captured.out and "audio format could not be interpreted" in captured.err
            assert "Synthetic track" not in captured.err
        else:
            result = json.loads(captured.out)
            assert not captured.err and result["audio_output"] == "UNVERIFIED"
            assert result["track"]["decoded_file"] == {
                "format": "OGG_VORBIS_160", "codec": "vorbis", "nominal_bitrate_kbps": 160,
                "decoder_sample_rate_hz": 44100, "source_bit_depth_bits": None}
        assert records(log) == [["GET", "/status", None]]


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


@pytest.mark.parametrize("action", ["resume", "stop-playback"])
def test_invocation_change_between_status_and_post_blocks_mutation(tmp_path, action):
    checks = []
    def fence():
        checks.append(True)
        if len(checks) == 2:
            raise ExclusiveProfileError("Fixture invocation changed.")
    with peer(tmp_path) as (child, port, log):
        with pytest.raises(ExclusiveProfileError, match="invocation changed"):
            playback.control("127.0.0.1", port, str(child.pid), action, None, fence)
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
    if behavior.startswith("launch"):
        state["stopped"] = True
        behavior = behavior.removeprefix("launch_")
    if behavior == "buffering": state["buffering"] = True
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
            if self.path == "/" and not behavior.startswith("pairing"):
                payload = {"playback_ready": {"not_ready": False, "bad_ready": "true"}.get(behavior, True),
                           "private_extra": SECRET}
                self.respond(payload, close=behavior == "close_root")
                return
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
