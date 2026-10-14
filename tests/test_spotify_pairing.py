"""Synthetic inert HTTP only: no go-librespot, provider, login or account."""
import json
import os

import pytest

from fake_linkplay import cli, spotify_receiver as receiver, spotify_pairing as pairing
from fake_linkplay.spotify_exclusive import ExclusiveProfileError
from test_spotify_diagnostic import SECRET
from test_spotify_exclusive import profile
from test_spotify_receiver import ready
from test_spotify_playback import peer, records, configure_owned_peer


def test_explicit_pairing_cli_displays_only_user_code_and_fixed_official_url(ready, tmp_path, monkeypatch, capsys):
    with peer(tmp_path, "pairing") as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        monkeypatch.setattr(receiver, "_write", lambda *_args: pytest.fail("pairing data persisted"))
        assert cli.main(["spotify", "receiver", "--directory", str(ready), "pairing-code"]) == 0
        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert not captured.err and SECRET not in captured.out and "example.test" not in captured.out
        assert result == {"command": "pairing-code", "pairing_observation": "AVAILABLE",
                          "user_code": "TEST-CODE", "verification_url": "https://www.spotify.com/pair",
                          "listener_ownership": "owned_connection", "authentication_started": False,
                          "spotify_bypasses_orr_dsp": True, "gates": receiver.GATES}
        assert records(log) == [["GET", "/auth/code", None]]


@pytest.mark.parametrize("behavior", ["absent", "expired", "bad_code", "bad_expiry", "extra", "redirect"])
def test_pairing_absent_expired_or_unavailable_never_exposes_or_retries(
        ready, tmp_path, monkeypatch, capsys, behavior):
    with peer(tmp_path, "pairing_" + behavior) as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        outcome = cli.main(["spotify", "receiver", "--directory", str(ready), "pairing-code"])
        captured = capsys.readouterr()
        assert SECRET not in captured.out + captured.err and "TEST-CODE" not in captured.out + captured.err
        if behavior in {"absent", "expired"}:
            assert outcome == 0 and not captured.err
            assert json.loads(captured.out)["pairing_observation"] == behavior.upper()
        else:
            assert outcome == 1 and not captured.out and "unavailable" in captured.err
        assert records(log) == [["GET", "/auth/code", None]]


@pytest.mark.parametrize("failure", ["foreign_listener", "invocation_changed"])
def test_pairing_ownership_and_post_read_fence_discard_code(tmp_path, failure):
    with peer(tmp_path, "pairing") as (child, port, log):
        checks = []
        def fence():
            checks.append(True)
            if len(checks) == 2:
                raise ExclusiveProfileError(SECRET)
        pid = str(os.getpid() if failure == "foreign_listener" else child.pid)
        with pytest.raises(ExclusiveProfileError, match="pairing code unavailable") as exc:
            pairing.pairing_code("127.0.0.1", port, pid, fence)
        assert SECRET not in str(exc.value) and "TEST-CODE" not in str(exc.value)
        assert records(log) == ([] if failure == "foreign_listener" else [["GET", "/auth/code", None]])


def test_ordinary_reads_never_request_pairing_code(ready, tmp_path, monkeypatch, capsys):
    with peer(tmp_path, "pairing") as (child, port, log):
        configure_owned_peer(ready, monkeypatch, child, port)
        for command in (["receiver", "--directory", str(ready), "status"],
                        ["receiver", "--directory", str(ready), "status", "--playback"],
                        ["receiver", "--directory", str(ready), "now-playing"],
                        ["diagnose", "--port", str(port)]):
            assert cli.main(["spotify", *command]) == 0
            captured = capsys.readouterr()
            assert not captured.err and "TEST-CODE" not in captured.out and SECRET not in captured.out
        assert records(log) == [["GET", path, None] for path in ("/", "/status", "/status", "/", "/status")]
