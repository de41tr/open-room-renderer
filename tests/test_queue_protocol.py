"""The shared authored-queue contract must not depend on client transport."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("first_module", ["queue_protocol", "playlist_edits", "queue_playlist"])
def test_shared_contract_and_adapters_load_without_queue_client(first_module):
    # A fresh interpreter ensures the import boundary is not hidden by another
    # test having already imported the CLI, requests or the renderer.
    result = subprocess.run([sys.executable, "-I", "-c", r'''
import importlib
import importlib.abc
import json
import sys

sys.path.insert(0, sys.argv[1])
blocked = ("requests", "fake_linkplay.queue_client", "fake_linkplay.cli",
           "fake_linkplay.http_api")

class NoClientTransport(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise AssertionError("Unexpected client/renderer dependency: " + fullname)
        return None

sys.meta_path.insert(0, NoClientTransport())
importlib.import_module("fake_linkplay." + sys.argv[2])
from fake_linkplay import playlist_edits, queue_playlist, queue_protocol
from fake_linkplay.queue import parse_queue_context
from xml.etree import ElementTree as ET

url = "https://media.example.test/音声/a%2fb?q=synthetic&a=1+2#片"
queue = queue_protocol.explicit_queue("Café & 夜", [url, url],
                                      start_index=2, mode="repeat-all")
request = queue_protocol.prepare_replace(queue)
restored = parse_queue_context(ET.fromstring(request)[0][0].findtext("QueueContext"))
assert restored.name == queue.name and restored.index == 2
assert [item.url for item in restored.items] == [url, url]
payload = json.loads(queue_playlist.encode_current_playlist(queue))
assert payload == {"version": 1, "name": queue.name, "index": 2,
                   "mode": "repeat-all", "urls": [url, url]}
assert playlist_edits.remove_playlist(payload, 1) == {**payload, "index": 1, "urls": [url]}
assert queue.count == 2 and queue.index == 2
assert not any(name in sys.modules for name in blocked)
''', str(Path(__file__).resolve().parents[1]), first_module],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_legacy_client_exports_use_the_same_contract_and_exception_identity():
    from fake_linkplay import playlist_cli, playlist_client, playlist_edits, queue_client
    from fake_linkplay import queue_playlist, queue_protocol

    for name in ("QueueClientError", "MODES", "validate_name", "validate_url",
                 "explicit_queue", "prepare_replace"):
        assert getattr(queue_client, name) is getattr(queue_protocol, name)
    for adapter in (playlist_cli, playlist_client, playlist_edits, queue_playlist):
        assert adapter.explicit_queue is queue_protocol.explicit_queue
    for adapter in (playlist_cli, playlist_client, queue_playlist):
        assert adapter.prepare_replace is queue_protocol.prepare_replace

    with pytest.raises(queue_client.QueueClientError) as error:
        playlist_edits.append_playlist({"version": 1, "name": "Queue", "index": 1,
            "mode": "normal", "urls": ["https://media.example.test/track"]},
            ["https://user:synthetic-secret@media.example.test/track"])
    assert type(error.value) is queue_protocol.QueueClientError
    assert str(error.value) == queue_client._INVALID_INPUT
    assert error.value.__suppress_context__ is True
