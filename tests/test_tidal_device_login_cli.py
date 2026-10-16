"""Offline entry-point checks; the existing manager owns all auth/storage."""
import json

import pytest

from fake_linkplay.tidal_device_login_cli import run


class Manager:
    closed = False

    def __init__(self, saved):
        self.saved = saved

    async def start(self):
        return {"state": "pending", "active": True, "verification_uri": "https://link.tidal.com/ABCDE",
                "user_code": "ABCDE", "access_token": "must-not-print", "refresh_token": "must-not-print"}

    async def status(self):
        return {"state": "connected" if self.saved else "error", "active": False,
                "session_saved": self.saved, "account": "must-not-print"}

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("saved", [True, False])
@pytest.mark.asyncio
async def test_cli_only_reports_challenge_and_sanitized_completion(saved, capsys):
    manager = Manager(saved)
    assert await run(manager) == (0 if saved else 1)
    output = capsys.readouterr().out
    assert "must-not-print" not in output
    assert "access_token" not in output and "refresh_token" not in output
    rows = [json.loads(line) for line in output.splitlines()]
    assert rows[0]["verification_uri"] == "https://link.tidal.com/ABCDE"
    assert rows[-1]["session_saved"] is saved
    assert manager.closed


@pytest.mark.asyncio
async def test_cli_failure_still_cancels_manager():
    manager = Manager(False)
    async def failed():
        raise RuntimeError("synthetic-private-detail")
    manager.start = failed
    with pytest.raises(RuntimeError):
        await run(manager)
    assert manager.closed
