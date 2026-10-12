"""UNIT/MOCK DSP transactions; no decoder, device, or audio output is used.

The synthetic af list models mpv's named replacement and node readback. It
proves command ordering and state handling, not acceptance by a decode chain.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from fake_linkplay.mpv_backend import BackendError, MpvBackend


LABEL = "fake_linkplay_dsp"
DEFAULT = object()
OTHER_FILTER = {"name": "lavfi", "label": "unrelated", "enabled": True, "params": {"graph": "anull"}}


def band(**changes):
    return {"freq": 1000.0, "gain": -6.0, "q": 1.0, "mode": 1.0, **changes}


def entry(graph):
    return {"name": "lavfi", "label": LABEL, "enabled": True, "params": {"graph": graph}}


def add(graph):
    return ["af", "add", f"@{LABEL}:lavfi=[{graph}]"]


class DspPeer:
    """A stateful synthetic peer with one-shot failures before/after writes."""

    def __init__(self):
        self.filters = [deepcopy(OTHER_FILTER)]
        self.properties = {"volume": 50, "mute": False, "pause": False}
        self.calls = []
        self.loads = []
        self.responses = defaultdict(deque)

    def reply(self, args, response, *, apply=False):
        self.responses[tuple(args)].append((response, apply))

    @property
    def graph(self):
        return next((item["params"]["graph"] for item in self.filters if item.get("label") == LABEL), "")

    async def command(self, args, timeout=5.0):
        self.calls.append(deepcopy(args))
        await asyncio.sleep(0)
        override, apply = DEFAULT, False
        if self.responses[tuple(args)]:
            override, apply = self.responses[tuple(args)].popleft()
        if override is not DEFAULT and not apply:
            return deepcopy(override)
        operation, *arguments = args
        if operation == "af":
            action, specification = arguments
            if action == "add":
                prefix = f"@{LABEL}:lavfi=["
                assert specification.startswith(prefix) and specification.endswith("]")
                replacement = entry(specification[len(prefix):-1])
                for index, item in enumerate(self.filters):
                    if item.get("label") == LABEL:
                        self.filters[index] = replacement
                        break
                else:
                    self.filters.append(replacement)
            elif action == "remove":
                assert specification == f"@{LABEL}"
                if not self.graph:
                    return {"error": "synthetic label does not exist"}
                self.filters = [item for item in self.filters if item.get("label") != LABEL]
            else:
                raise AssertionError(args)
        elif operation == "get_property":
            data = self.filters if arguments[0] == "af" else self.properties[arguments[0]]
            return {"error": "success", "data": deepcopy(data)}
        elif operation == "set_property":
            self.properties[arguments[0]] = arguments[1]
        elif operation == "loadfile":
            self.loads.append((arguments[0], deepcopy(self.filters), self.properties.copy()))
        elif operation not in {"stop", "seek"}:
            raise AssertionError(args)
        return {"error": "success"} if override is DEFAULT else deepcopy(override)


def make_backend():
    backend = MpvBackend("/unused-synthetic-dsp", auto_spawn=False)
    peer = DspPeer()
    backend.command = AsyncMock(side_effect=peer.command)
    return backend, peer


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [
    -1.01, 1.01, float("nan"), float("inf"), -float("inf"), 10 ** 400,
    True, False, None, "0.5", [], {},
])
async def test_invalid_balance_has_no_backend_or_cache_effect(value):
    backend, peer = make_backend()
    with pytest.raises(BackendError):
        await backend.channel_balance(value)
    assert peer.calls == []
    assert backend._channel_balance == 0.0
    assert backend._peq_bands is None
    assert not backend._dsp_configuration_confirmed


@pytest.mark.asyncio
@pytest.mark.parametrize("bands", [
    None, {}, (), "bands", [band()] * 11, [None], [1], [[]], [{}],
    [{"freq": 1000, "gain": -6, "q": 1}], [band(unexpected=1)],
    *[[band(**{key: value})] for key in ("freq", "gain", "q", "mode")
      for value in (None, True, "1", float("nan"), float("inf"), -float("inf"), 10 ** 400)],
    *[[band(**{key: value})] for key, value in (
        ("freq", 9.99), ("freq", 24000.01), ("gain", -24.01), ("gain", 24.01),
        ("q", 0.09), ("q", 30.01), ("mode", -2), ("mode", 6),
        ("mode", 4), ("mode", 1.5), ("mode", -0.5),
    )],
])
async def test_invalid_peq_has_no_backend_or_cache_effect(bands):
    backend, peer = make_backend()
    with pytest.raises(BackendError):
        await backend.peq(bands)
    assert peer.calls == []
    assert backend._peq_bands is None
    assert backend._channel_balance == 0.0


@pytest.mark.parametrize("mode,filter_name", [
    (-1, ""), (0, "bass"), (1, "equalizer"), (2, "treble"), (3, "lowpass"), (5, "highpass"),
])
def test_supported_modes_map_to_documented_filters(mode, filter_name):
    backend, _ = make_backend()
    backend._peq_bands = [band(mode=mode)]
    graph = backend._audio_filter_graph()
    assert graph.startswith(f"{filter_name}=") if filter_name else graph == ""


@pytest.mark.parametrize("mode,expected", [
    (-1, ""), (0, ""), (1, ""), (2, ""),
    (3, "lowpass=f=1000.000:width_type=q:width=1.000"),
    (5, "highpass=f=1000.000:width_type=q:width=1.000"),
])
def test_zero_gain_preserves_pass_filters(mode, expected):
    backend, _ = make_backend()
    backend._peq_bands = [band(gain=0, mode=mode)]
    assert backend._audio_filter_graph() == expected


@pytest.mark.asyncio
async def test_ten_bands_and_numeric_boundary_values_are_accepted():
    backend, peer = make_backend()
    bands = [band(freq=10, gain=-24, q=0.1), band(freq=24000, gain=24, q=30)] + [band(mode=-1)] * 8
    # Positive gain here is only a generated command string, never audio.
    await backend.peq(bands)
    assert backend._peq_bands == bands
    assert peer.graph.count("equalizer=") == 2
    assert "f=10.000" in peer.graph and "f=24000.000" in peer.graph
    await backend.channel_balance(-1)
    assert "pan=stereo|c0=1.000*c0|c1=0.000*c1" in peer.graph
    await backend.channel_balance(1)
    assert "pan=stereo|c0=0.000*c0|c1=1.000*c1" in peer.graph


@pytest.mark.asyncio
async def test_named_replacement_never_removes_working_graph_first():
    backend, peer = make_backend()
    await backend.channel_balance(0.25)
    previous = peer.graph
    peer.calls.clear()
    await backend.peq([band()])
    candidate = backend._audio_filter_graph()
    assert previous in candidate
    assert peer.calls == [["get_property", "af"], add(candidate), ["get_property", "af"]]
    assert peer.graph == candidate
    assert peer.filters[0] == OTHER_FILTER
    assert len(peer.filters) == 2
    assert backend._dsp_configuration_confirmed


@pytest.mark.asyncio
async def test_bypass_removes_only_present_owned_label_and_never_unrelated_filters():
    backend, peer = make_backend()
    await backend.peq([])
    assert peer.calls == [["get_property", "af"]]
    await backend.channel_balance(0.5)
    peer.calls.clear()
    await backend.channel_balance(0)
    assert peer.calls == [["get_property", "af"], ["af", "remove", f"@{LABEL}"], ["get_property", "af"]]
    assert peer.filters == [OTHER_FILTER]
    peer.calls.clear()
    await backend.channel_balance(0)
    assert peer.calls == [["get_property", "af"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{}, {"error": None}, {"error": "rejected"}, None, [], {"data": "success"}])
@pytest.mark.parametrize("apply", [False, True])
async def test_failed_or_missing_add_ack_restores_prior_graph_and_cache(response, apply):
    backend, peer = make_backend()
    await backend.channel_balance(0.25)
    previous = peer.graph
    candidate = backend._build_audio_filter_graph(0.25, [band()])
    peer.reply(add(candidate), response, apply=apply)
    peer.calls.clear()
    with pytest.raises(BackendError, match="DSP operation failed"):
        await backend.peq([band()])
    assert backend._peq_bands is None
    assert backend._channel_balance == 0.25
    assert peer.graph == previous
    assert backend._dsp_configuration_confirmed
    assert not any(call[:2] == ["af", "remove"] for call in peer.calls)
    assert [call for call in peer.calls if call[:2] == ["af", "add"]] == (
        [add(candidate), add(previous)] if apply else [add(candidate)]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("apply", [False, True])
async def test_failed_remove_ack_restores_prior_graph_and_cache(apply):
    backend, peer = make_backend()
    await backend.channel_balance(0.25)
    previous = peer.graph
    peer.reply(["af", "remove", f"@{LABEL}"], {}, apply=apply)
    with pytest.raises(BackendError):
        await backend.channel_balance(0)
    assert peer.graph == previous
    assert backend._channel_balance == 0.25
    assert backend._dsp_configuration_confirmed


BAD_READBACKS = [
    {}, {"error": "success"}, {"error": "success", "data": None},
    {"error": "success", "data": {}}, {"error": "success", "data": [None]},
    {"error": "success", "data": [entry("wrong graph")]},
    {"error": "success", "data": []},
    {"error": "success", "data": [entry("wrong graph"), entry("wrong graph")]},
    *[{"error": "success", "data": [{**entry("wrong graph"), **changes}]}
      for changes in ({"enabled": False}, {"enabled": 1}, {"name": "unrelated"},
                      {"params": {}}, {"params": {"graph": None}},
                      {"params": {"graph": ""}}, {"params": {"graph": "wrong graph", "o": "extra"}})],
]


@pytest.mark.asyncio
@pytest.mark.parametrize("readback", BAD_READBACKS)
async def test_bad_candidate_readback_rolls_back_to_previous_configuration(readback):
    backend, peer = make_backend()
    await backend.channel_balance(-0.25)
    previous = peer.graph
    peer.reply(["get_property", "af"], DEFAULT)
    peer.reply(["get_property", "af"], readback)
    with pytest.raises(BackendError):
        await backend.peq([band()])
    assert peer.graph == previous
    assert backend._peq_bands is None
    assert backend._channel_balance == -0.25
    assert backend._dsp_configuration_confirmed
    assert peer.filters[0] == OTHER_FILTER


@pytest.mark.asyncio
@pytest.mark.parametrize("readback", [
    {}, {"error": "property unavailable"}, {"error": "success", "data": None},
    {"error": "success", "data": [entry("x"), entry("x")]},
    {"error": "success", "data": [{**entry("x"), "enabled": False}]},
])
async def test_unreadable_preflight_never_changes_filters(readback):
    backend, peer = make_backend()
    peer.reply(["get_property", "af"], readback)
    with pytest.raises(BackendError):
        await backend.channel_balance(0.25)
    assert peer.calls == [["get_property", "af"]]
    assert peer.filters == [OTHER_FILTER]
    assert backend._channel_balance == 0


@pytest.mark.asyncio
async def test_initial_uncertain_add_is_removed_only_after_its_presence_is_read_back():
    backend, peer = make_backend()
    candidate = backend._build_audio_filter_graph(0, [band()])
    peer.reply(add(candidate), {}, apply=True)
    with pytest.raises(BackendError):
        await backend.peq([band()])
    assert peer.calls == [
        ["get_property", "af"], add(candidate), ["get_property", "af"],
        ["af", "remove", f"@{LABEL}"], ["get_property", "af"],
    ]
    assert peer.filters == [OTHER_FILTER]
    assert backend._peq_bands is None


@pytest.mark.asyncio
async def test_unconfirmed_rollback_is_an_error_and_never_publishes_candidate():
    backend, peer = make_backend()
    await backend.channel_balance(0.25)
    previous = peer.graph
    candidate = backend._build_audio_filter_graph(0.25, [band()])
    peer.reply(add(candidate), {}, apply=True)
    peer.reply(add(previous), {"error": "synthetic rollback rejected"})
    with pytest.raises(BackendError, match="restoration is unconfirmed"):
        await backend.peq([band()])
    assert peer.graph == candidate  # No claim that the failed rollback worked.
    assert backend._peq_bands is None
    assert backend._channel_balance == 0.25
    assert not backend._dsp_configuration_confirmed
    await backend.apply_audio_filters()
    assert peer.graph == previous
    assert backend._dsp_configuration_confirmed


@pytest.mark.asyncio
async def test_uncertain_initial_add_and_unreadable_presence_never_removes_blindly():
    backend, peer = make_backend()
    candidate = backend._build_audio_filter_graph(0, [band()])
    peer.reply(["get_property", "af"], DEFAULT)
    peer.reply(["get_property", "af"], {"error": "synthetic readback unavailable"})
    peer.reply(add(candidate), {}, apply=True)
    with pytest.raises(BackendError, match="restoration is unconfirmed"):
        await backend.peq([band()])
    assert peer.graph == candidate
    assert backend._peq_bands is None
    assert not backend._dsp_configuration_confirmed
    assert not any(call[:2] == ["af", "remove"] for call in peer.calls)


@pytest.mark.asyncio
async def test_uncertain_replacement_can_restore_known_prior_graph_without_pre_read():
    backend, peer = make_backend()
    await backend.channel_balance(0.25)
    previous = peer.graph
    candidate = backend._build_audio_filter_graph(0.25, [band()])
    peer.reply(["get_property", "af"], DEFAULT)
    peer.reply(["get_property", "af"], {"error": "synthetic readback unavailable"})
    peer.reply(add(candidate), {}, apply=True)
    with pytest.raises(BackendError, match="DSP operation failed"):
        await backend.peq([band()])
    assert peer.graph == previous
    assert backend._peq_bands is None
    assert backend._dsp_configuration_confirmed


@pytest.mark.asyncio
async def test_remove_success_ack_without_actual_bypass_is_rejected():
    backend, peer = make_backend()
    await backend.channel_balance(0.25)
    previous = peer.graph
    peer.reply(["af", "remove", f"@{LABEL}"], {"error": "success"})
    with pytest.raises(BackendError, match="readback did not confirm"):
        await backend.channel_balance(0)
    assert peer.graph == previous
    assert backend._channel_balance == 0.25
    assert backend._dsp_configuration_confirmed


@pytest.mark.asyncio
async def test_validation_failure_preserves_previously_confirmed_configuration():
    backend, peer = make_backend()
    await backend.channel_balance(-0.25)
    await backend.peq([band()])
    previous = peer.graph
    peer.calls.clear()
    with pytest.raises(BackendError):
        await backend.channel_balance(float("nan"))
    with pytest.raises(BackendError):
        await backend.peq([band(mode=1.5)])
    assert peer.calls == []
    assert peer.graph == previous
    assert backend._channel_balance == -0.25
    assert backend._peq_bands == [band()]
    assert backend._dsp_configuration_confirmed


@pytest.mark.asyncio
async def test_peq_input_cannot_mutate_confirmed_cache_after_call():
    backend, peer = make_backend()
    bands = [band()]
    await backend.peq(bands)
    confirmed = deepcopy(backend._peq_bands)
    bands[0]["gain"] = -20
    bands.append(band(freq=2000))
    await backend.channel_balance(0.5)
    assert backend._peq_bands == confirmed
    assert "g=-6.000" in peer.graph and "g=-20.000" not in peer.graph


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["balance", "peq"])
async def test_concurrent_peq_balance_combines_both_confirmed_candidates(first):
    backend, peer = make_backend()
    entered, release = asyncio.Event(), asyncio.Event()

    async def command(args, timeout=5.0):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await peer.command(args, timeout)

    backend.command = command
    operations = [backend.channel_balance(-0.5), backend.peq([band()])]
    if first == "peq":
        operations.reverse()
    task = asyncio.create_task(operations[0])
    await entered.wait()
    next_task = asyncio.create_task(operations[1])
    await asyncio.sleep(0)
    assert peer.calls == []
    release.set()
    await asyncio.gather(task, next_task)
    assert backend._channel_balance == -0.5
    assert backend._peq_bands == [band()]
    assert peer.graph == backend._audio_filter_graph()
    assert "equalizer=" in peer.graph and "c1=0.500*c1" in peer.graph
    assert [call[0] for call in peer.calls] == ["get_property", "af", "get_property"] * 2


@pytest.mark.asyncio
async def test_cancelled_write_restores_graph_before_next_candidate_runs():
    backend, peer = make_backend()
    await backend.channel_balance(0.25)
    previous = peer.graph
    entered, release = asyncio.Event(), asyncio.Event()

    async def command(args, timeout=5.0):
        result = await peer.command(args, timeout)
        if args[:2] == ["af", "add"] and not entered.is_set():
            entered.set()
            await release.wait()
        return result

    backend.command = command
    pending = asyncio.create_task(backend.peq([band()]))
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert peer.graph == previous
    assert backend._peq_bands is None
    assert backend._dsp_configuration_confirmed
    await backend.channel_balance(-0.25)
    assert peer.graph == backend._audio_filter_graph()
    assert "equalizer=" not in peer.graph


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_release_lock_during_rollback():
    backend, peer = make_backend()
    await backend.channel_balance(0.25)
    previous = peer.graph
    candidate = backend._build_audio_filter_graph(0.25, [band()])
    entered, rolling_back, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def command(args, timeout=5.0):
        if args == add(candidate):
            result = await peer.command(args, timeout)
            entered.set()
            await asyncio.Event().wait()
            return result
        if args == add(previous):
            rolling_back.set()
            await release.wait()
        return await peer.command(args, timeout)

    backend.command = command
    baseline = asyncio.all_tasks()
    pending = asyncio.create_task(backend.peq([band()]))
    await entered.wait()
    pending.cancel()
    await rolling_back.wait()
    next_candidate = asyncio.create_task(backend.channel_balance(-0.25))
    await asyncio.sleep(0)
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done() and not next_candidate.done()
    assert backend._dsp_lock.locked()
    assert peer.graph == candidate
    assert backend._channel_balance == 0.25 and backend._peq_bands is None
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await next_candidate
    assert backend._channel_balance == -0.25 and backend._peq_bands is None
    assert peer.graph == backend._audio_filter_graph()
    assert backend._dsp_configuration_confirmed
    assert asyncio.all_tasks() == baseline


@pytest.mark.asyncio
async def test_cancellation_while_waiting_for_lock_cannot_interrupt_active_transaction():
    backend, peer = make_backend()
    entered, release = asyncio.Event(), asyncio.Event()

    async def command(args, timeout=5.0):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await peer.command(args, timeout)

    backend.command = command
    active = asyncio.create_task(backend.peq([band()]))
    await entered.wait()
    waiting = asyncio.create_task(backend.channel_balance(-0.5))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    release.set()
    await active
    assert backend._channel_balance == 0
    assert backend._peq_bands == [band()]
    assert peer.graph == backend._audio_filter_graph()


@pytest.mark.asyncio
async def test_timed_out_caller_waits_for_confirmed_rollback():
    backend, peer = make_backend()
    await backend.channel_balance(0.25)
    previous = peer.graph
    candidate = backend._build_audio_filter_graph(0.25, [band()])

    async def command(args, timeout=5.0):
        response = await peer.command(args, timeout)
        if args == add(candidate):
            await asyncio.Event().wait()
        return response

    backend.command = command
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(backend.peq([band()]), timeout=0.01)
    assert peer.graph == previous
    assert backend._peq_bands is None
    assert backend._dsp_configuration_confirmed


@pytest.mark.asyncio
async def test_source_changes_and_gain_controls_keep_confirmed_graph():
    backend, peer = make_backend()
    await backend.volume(29)
    await backend.mute(True)
    await backend.peq([band()])
    await backend.channel_balance(-0.5)
    graph = peer.graph
    # DSP does not reset gain, and inherited load/gain code does not clear af.
    assert peer.properties["volume"] == 29
    assert peer.properties["mute"] is True
    for source in ("https://media.example.test/one.wav", "https://media.example.test/two.wav"):
        assert (await backend.loadfile(source))["error"] == "success"
        assert peer.graph == graph
        assert peer.properties["volume"] == 29 and peer.properties["mute"] is True
    await backend.pause()
    await backend.resume()
    await backend.stop()
    assert peer.graph == graph
    assert backend._peq_bands == [band()]
    assert backend._channel_balance == -0.5
    assert len(peer.loads) == 2
