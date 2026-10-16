"""Explicit local device-login entry point; no renderer or token output."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .tidal_device_auth import TidalDeviceLoginManager
from .tidal_session import TidalSessionStore


def _emit(status: dict) -> None:
    # Never print provider objects, account fields, exceptions or store contents.
    result = {key: status[key] for key in (
        "state", "active", "session_saved", "verification_uri", "user_code",
        "expires_in", "error",
    ) if key in status}
    print(json.dumps(result), flush=True)


async def run(manager: TidalDeviceLoginManager) -> int:
    try:
        status = await manager.start()
        _emit(status)
        previous = status.get("state")
        while status.get("active"):
            await asyncio.sleep(0.5)
            status = await manager.status()
            if status.get("state") != previous or not status.get("active"):
                _emit(status)
                previous = status.get("state")
        return 0 if status.get("session_saved") is True else 1
    finally:
        await manager.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--confirm-experimental-device-login", action="store_true", required=True)
    args = parser.parse_args()
    if not args.session_dir.is_absolute():
        parser.error("--session-dir must be an absolute private local directory")
    try:
        return asyncio.run(run(TidalDeviceLoginManager(TidalSessionStore(args.session_dir))))
    except KeyboardInterrupt:
        print('{"state":"cancelled","message":"Upstream polling may still be draining."}', flush=True)
        return 130
    except Exception:
        print('{"state":"error","error":"local_login_failed"}', flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
