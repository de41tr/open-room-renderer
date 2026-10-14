"""Static safety/packaging checks and optional dependency-free JS unit tests.

Node tests exercise control flow, not an actual browser or responsive layout.
"""
from html.parser import HTMLParser
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
import subprocess
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "fake_linkplay" / "web_assets"


class Document(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.scripts = []
        self.labels = []
        self.handlers = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "script":
            self.scripts.append(attrs)
        if tag == "label":
            self.labels.append(attrs["for"])
        self.handlers.extend(key for key in attrs if key.lower().startswith("on"))


def test_web_assets_are_local_accessible_and_no_inline_handlers():
    document = Document()
    document.feed((ASSETS / "index.html").read_text())
    assert len(document.ids) == len(set(document.ids))
    assert not document.handlers
    assert {script["src"] for script in document.scripts} == {"/ui/controller.js", "/ui/app.js"}
    assert all("defer" in script for script in document.scripts)
    assert all(label in document.ids for label in document.labels)
    assert "no-referrer" in (ASSETS / "index.html").read_text()
    assert '<link rel="icon" href="/ui/favicon.ico">' in (ASSETS / "index.html").read_text()
    assert 'href="data:' not in (ASSETS / "index.html").read_text()
    assert "Trusted LAN only" in (ASSETS / "index.html").read_text()


def test_web_metadata_is_not_inserted_as_html_or_legacy_commands():
    sources = "\n".join((ASSETS / file).read_text() for file in ("app.js", "controller.js"))
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "httpapi.asp", "XMLHttpRequest", "eval("):
        assert forbidden not in sources
    assert 'redirect: "error"' in sources
    assert 'credentials: "same-origin"' in sources
    assert '"X-ORR-CSRF"' in sources
    assert 'referrerPolicy: "no-referrer"' in sources
    assert "expected_generation" in sources


def test_web_frontend_control_flow_node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional Node 18+ is not installed; no browser QA implied")
    # The bounded group runner emits this log only after the group has stopped.
    # Embedded timestamps distinguish late arrival from a Node subprocess stall.
    started = time.monotonic()
    print(f"\nFRONTEND_NODE_VERSION_START utc={datetime.now(timezone.utc).isoformat(timespec='milliseconds')} "
          f"elapsed={time.monotonic() - started:.3f}s", flush=True)
    version = subprocess.run([node, "--version"], check=True, capture_output=True, text=True).stdout
    if int(re.match(r"v(\d+)", version).group(1)) < 18:
        pytest.skip("Optional Node 18+ is required for dependency-free UI unit tests")
    node_version = re.match(r"v[0-9.]+", version).group(0)
    print(f"FRONTEND_NODE_SUITE_START utc={datetime.now(timezone.utc).isoformat(timespec='milliseconds')} "
          f"elapsed={time.monotonic() - started:.3f}s node={node_version}", flush=True)
    result = subprocess.run([node, "--test", str(ROOT / "tests" / "web_frontend.test.cjs"), str(ROOT / "tests" / "web_curve_transfer.test.cjs"), str(ROOT / "tests" / "web_favorites_transfer.test.cjs")], capture_output=True, text=True, timeout=60)
    print(f"FRONTEND_NODE_SUITE_RETURNED utc={datetime.now(timezone.utc).isoformat(timespec='milliseconds')} "
          f"elapsed={time.monotonic() - started:.3f}s", flush=True)
    assert result.returncode == 0, result.stdout + result.stderr
