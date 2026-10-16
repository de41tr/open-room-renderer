#!/usr/bin/env python3
"""Two fixed offline profiles; no production files, network, devices or service bus."""
import importlib
import json
from pathlib import Path
import platform
import struct
import sys

def check(profile):
    if profile == "environment":
        assert platform.system() == "Linux"
        assert struct.calcsize("P") * 8 in (32, 64)
    elif profile == "contract":
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root))
        from fake_linkplay import host_contract as c, host_eq as eq, host_runtime as g
        d = c.installed_descriptor()
        assert d["layout"] == "xu4-native-v1"
        sample = {"api_version": 1, "request_id": "offline-contract-01",
                  "commit": "a" * 40, "operation": "status", "profile": "renderer"}
        assert c.request(json.dumps(sample).encode()) == sample
        for bad in ({**sample, "command": "PRIVATE_MARKER"},
                    {**sample, "profile": "https://PRIVATE_MARKER"},
                    {**sample, "commit": "main"}):
            try:
                c.request(json.dumps(bad).encode())
            except c.Refused:
                pass
            else:
                raise AssertionError("invalid request accepted")
        raw = b'{"friendly_name":"fixture","spotify_eq_profile":null,"volume":37}'
        removed = eq.config_edit(raw, None)
        assert json.loads(removed) == {"friendly_name": "fixture", "volume": 37}
        from fake_linkplay.native_profile import user_unit
        g.validate_unit(user_unit(g.BASE / "current", config=g.CONFIG).encode())
    else:
        raise ValueError("unsupported_profile")
    return {"profile": profile, "checks_passed": True}

if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("invalid_profile")
        print(json.dumps(check(sys.argv[1]), separators=(",", ":")))
    except Exception:
        print('{"checks_passed":false,"error":"isolated_contract_check_failed"}')
        sys.exit(2)
