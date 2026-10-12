"""Fast negative-sensitivity checks for the optional native stability fixture."""
from types import SimpleNamespace
import asyncio
import json

import pytest

from libmpv_stability_support import Events, _eventually, resources, within_budget
from native_stability_transport import UnixEvents


def test_unix_observer_counts_events_without_playback_commands():
    observer = UnixEvents()
    for source in ("synthetic-a.wav", "synthetic-a.wav", "synthetic-b.wav"):
        observer.observe({"event": "start-file", "playlist_entry_id": observer.loads + 1})
        observer.observe({"event": "property-change", "name": "path", "data": source})
        observer.observe({"event": "property-change", "name": "path", "data": None})
    assert observer.loads == 3 and observer.source_changes == 2
    with pytest.raises(AssertionError, match="overflowed"):
        observer.observe({"event": "queue-overflow"})


@pytest.mark.asyncio
async def test_callback_cannot_acknowledge_held_retry_before_mutation():
    events = Events()
    events.expected_seq["synthetic-sid"] = 7
    events.fail_next.add("RenderingControl")
    events.retry_hold.add("RenderingControl")
    body = '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0"><e:property><Mute>0</Mute></e:property></e:propertyset>'
    async def text():
        return body
    request = SimpleNamespace(remote="127.0.0.1", match_info={"service": "RenderingControl"},
                              headers={"SID": "synthetic-sid", "SEQ": "7", "NT": "upnp:event", "NTS": "upnp:propchange"}, text=text)
    assert (await events.callback(request)).status == 503
    for _ in range(3):
        assert (await events.callback(request)).status == 503
        assert events.expected_seq["synthetic-sid"] == 7
        assert events.retried == 0 and "synthetic-sid" in events.pending_digest
    events.retry_hold.remove("RenderingControl")
    assert (await events.callback(request)).status == 200
    assert events.expected_seq["synthetic-sid"] == 8 and events.retried == 1
    assert not events.errors and not events.pending_digest


@pytest.mark.asyncio
async def test_unix_observer_rejects_malformed_stream_and_resolves_ack():
    observer = UnixEvents()
    observer.reader = asyncio.StreamReader()
    future = asyncio.get_running_loop().create_future()
    observer.pending[1] = future
    observer.reader.feed_data((json.dumps({"request_id":1, "error":"success"}) + '\nnot-json\n').encode())
    observer.reader.feed_eof()
    await observer.consume()
    assert (await future)["error"] == "success"
    assert observer.error and not observer.pending


@pytest.mark.asyncio
async def test_async_predicates_and_controller_child_budgets():
    async def yes():
        return True
    assert await _eventually("async predicate", yes) is True
    assert await _eventually("sync predicate", lambda: True) is True
    baseline = {"tasks":3, "fds":10, "rss_kib":100000, "threads":1, "children":1}
    within_budget(baseline, baseline, {"tasks":0, "fds":0}, expected_children=1)
    with pytest.raises(AssertionError):
        within_budget({**baseline, "children":2}, baseline, {"tasks":0}, expected_children=1)


@pytest.mark.asyncio
async def test_child_resource_metrics_are_separate_and_child_is_reaped():
    import sys
    from pathlib import Path
    from native_stability_transport import UnixPlayer

    if sys.platform != "linux":
        pytest.skip("Per-process /proc metrics require Linux")
    child = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)",
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    observer_task = asyncio.create_task(asyncio.sleep(60))
    player = UnixPlayer("unused-unit-fixture")
    player.process = child
    player.backend = SimpleNamespace(process=None)
    player.observer.task = observer_task
    try:
        async def ready():
            return resources(child.pid)["rss_kib"] > 0
        await _eventually("synthetic child resource visibility", ready)
        controller, native_child = resources(), resources(child.pid)
        assert "tasks" in controller and "tasks" not in native_child
        assert controller["children"] == 1 and native_child["children"] == 0
        assert native_child["threads"] == 1 and native_child["fds"] >= 3
        player.assert_alive()
    finally:
        observer_task.cancel()
        await asyncio.gather(observer_task, return_exceptions=True)
        if child.returncode is None:
            child.terminate()
        await asyncio.wait_for(child.wait(), 3)
    assert not Path(f"/proc/{child.pid}").exists()
    assert resources()["children"] == 0


def test_common_evidence_gates_reject_suppressed_native_observations():
    from copy import deepcopy
    from libmpv_stability_support import validate_evidence

    epochs = [dict(cycles=8, loads=21, source_changes=17, callback_retries=8,
                   subscription_rotations=6, renewals=18, agreements=41,
                   inflight_gain_retries=2, samples=5, natural_eofs=1, confirmed_eof_events=1)
              for _ in range(3)]
    good = {"lane": "PRODUCTION_UNIX_IPC/null bounded stability", "epochs": epochs,
            "totals": {key: sum(epoch[key] for epoch in epochs) for key in epochs[0]}}
    validate_evidence(good)
    for key in ("loads", "source_changes", "callback_retries", "confirmed_eof_events"):
        bad = deepcopy(good)
        for epoch in bad["epochs"]:
            epoch[key] = 0
        bad["totals"][key] = 0
        with pytest.raises(AssertionError):
            validate_evidence(bad)


def test_native_eof_counter_rejects_stop_error_and_redirect_reasons():
    observer = UnixEvents()
    for reason in ("stop", "error", "redirect", "quit"):
        observer.observe({"event":"end-file", "reason":reason})
    assert observer.eof_events == 0
    observer.observe({"event":"end-file", "reason":"eof"})
    assert observer.eof_events == 1


@pytest.mark.asyncio
async def test_unix_cleanup_does_not_mask_last_reload_observer_failure():
    from native_stability_transport import UnixPlayer

    player = UnixPlayer("unused-unit-fixture")
    player.observer.error = "Synthetic last-reload observation failure"
    with pytest.raises(AssertionError, match="last-reload observation failure"):
        await player.close()
    assert player.closed
