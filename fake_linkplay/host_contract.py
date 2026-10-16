"""Version-one data contract for the existing unprivileged native installation.

No dispatch-provided paths, commands, URLs, environments or import names.
"""
from __future__ import annotations
import json
from pathlib import Path
import re

API_VERSION = 1
LAYOUT = "xu4-native-v1"
OPERATIONS = frozenset({
    "status", "capabilities", "release-prepare", "release-install",
    "release-rollback", "component-prepare", "component-install",
    "component-activate", "component-deactivate", "diagnose-start",
    "diagnose-collect", "test",
})
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,79}")
SHA = re.compile(r"[0-9a-f]{40}")
NAME = re.compile(r"[a-z][a-z0-9-]{0,47}")

class Refused(ValueError):
    """Only fixed developer-authored error codes cross the return channel."""

def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refused("duplicate_field")
        result[key] = value
    return result

def request(raw: bytes) -> dict:
    if len(raw) > 1024:
        raise Refused("oversized_request")
    try:
        r = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise Refused("invalid_json") from None
    fields = {"api_version", "request_id", "commit", "operation", "profile"}
    if type(r) is not dict:
        raise Refused("invalid_fields")
    if r.get("operation") == "diagnose-collect":
        fields.add("test_id")
    if set(r) != fields or type(r.get("api_version")) is not int or r["api_version"] != 1:
        raise Refused("unsupported_request_schema")
    if any(type(v) is not str for k, v in r.items() if k != "api_version"):
        raise Refused("invalid_field_type")
    if not ID.fullmatch(r["request_id"]) or not SHA.fullmatch(r["commit"]):
        raise Refused("invalid_id_or_sha")
    op, profile = r["operation"], r["profile"]
    if op not in OPERATIONS or not NAME.fullmatch(profile):
        raise Refused("unsupported_operation_or_profile")
    if op in {"status", "capabilities", "release-prepare", "release-install", "release-rollback"} and profile != "renderer":
        raise Refused("unsupported_profile")
    if op.startswith("diagnose-") and profile != "network":
        raise Refused("unsupported_profile")
    if op == "diagnose-collect" and not ID.fullmatch(r["test_id"]):
        raise Refused("invalid_test_id")
    if op == "test" and profile not in {"environment", "contract"}:
        raise Refused("unsupported_test_profile")
    return r

def descriptor(raw: bytes) -> dict:
    if len(raw) > 16384:
        raise Refused("component_description_limit")
    try:
        d = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise Refused("invalid_component_description") from None
    if (type(d) is not dict or set(d) != {"api_version", "layout", "components"}
            or type(d["api_version"]) is not int or d["api_version"] != 1
            or d["layout"] != LAYOUT or type(d["components"]) is not list
            or len(d["components"]) > 16):
        raise Refused("unsupported_component_contract")
    names = set()
    for item in d["components"]:
        if (type(item) is not dict or set(item) != {"name", "version", "driver", "rights", "release_binding"}
                or any(type(v) is not str or not NAME.fullmatch(v) for v in item.values())
                or item["rights"] != "existing-user-v1" or item["name"] in names):
            raise Refused("unsupported_component_rights_or_fields")
        names.add(item["name"])
    return d

def installed_descriptor() -> dict:
    return descriptor(Path(__file__).with_name("host_components.json").read_bytes())

def component(name: str, d: dict | None = None) -> dict:
    items = (d if d is not None else installed_descriptor())["components"]
    for item in items:
        if item["name"] == name:
            return item
    raise Refused("component_not_supported_by_release")

def capabilities(d: dict | None = None) -> dict:
    d = installed_descriptor() if d is None else d
    return {
        "api_version": API_VERSION, "layout": LAYOUT,
        "operations": sorted(OPERATIONS),
        "components": [c["name"] for c in d["components"]],
        "test_profiles": ["environment", "contract"],
        "pr_tests": "ci_only", "extra_rights": False,
    }
