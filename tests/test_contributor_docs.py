from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_contributor_docs_exist() -> None:
    for relative in (
        "README.md",
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "SECURITY.md",
        "ROADMAP.md",
        "docs/architecture.md",
        "docs/app-service-overview.md",
        "docs/clean-room-linkplay-renderer-spec.md",
        "docs/developer-map.md",
        "docs/demo.md",
        "docs/github-labels.md",
        ".github/pull_request_template.md",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        ".github/ISSUE_TEMPLATE/good_first_issue.yml",
    ):
        assert (ROOT / relative).is_file(), relative


def test_public_example_config_uses_neutral_identity_and_no_private_ips() -> None:
    text = (ROOT / "configs/config.example.yaml").read_text()
    data = yaml.safe_load(text)

    assert data["friendly_name"] == "Open Room Renderer"
    assert data["manufacturer"] == "Independent"
    assert data["model_name"] == "Linux Room Renderer"
    assert not re.search(r"\b(?:10|127)\.(?:\d{1,3}\.){2}\d{1,3}\b", text)
    assert not re.search(r"\b192\.168\.\d{1,3}\.\d{1,3}\b", text)
    assert not re.search(r"\b172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}\b", text)


def test_app_compat_config_advertises_only_working_tunein_surface() -> None:
    data = yaml.safe_load((ROOT / "configs/config.app-compat.yaml").read_text())

    assert data["enable_mdns"] is True
    assert data["enable_ssdp"] is True
    assert data["mirror_port_80"] is True
    assert data["source_experiment_variant"] == "tunein"
    assert data["tunein_streams_value"] == "200"
    assert data["enable_plex_service"] is False
    assert data["enable_plex_legacy_gate"] is False
    assert data["enable_stream_service_sentinels"] is False


def test_issue_template_warns_against_sensitive_logs() -> None:
    text = (ROOT / ".github/ISSUE_TEMPLATE/bug_report.yml").read_text()

    for phrase in ("tokens", "MAC addresses", "UUIDs", "raw protocol dumps", "private IPs"):
        assert phrase in text


def test_public_clean_room_docs_do_not_reference_decompilation_artifacts() -> None:
    combined = "\n".join(
        [
            (ROOT / "docs/clean-room-linkplay-renderer-spec.md").read_text(),
            (ROOT / "docs/app-service-overview.md").read_text(),
        ]
    ).lower()

    for risky in ("decompil", "jadx", "frida", "x-plex-token", "apk patch"):
        assert risky not in combined
