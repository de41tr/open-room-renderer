"""Optional REAL_BACKEND/digital-PCM DSP sample acceptance.

Opt in with ORR_TEST_LIBMPV_PATH=/absolute/path/to/libmpv.so.2 and run:
    PYTHONPATH=. .venv/bin/pytest -q -s tests/test_libmpv_dsp.py

HTTP DSP/gain commands use production HttpApi and inherited MpvBackend controls;
a test-only ctypes command adapter connects them to actual libmpv. Generated,
finite stereo tones have peak <= 500/32768 and volume is explicitly set to 25
(amplitude 1/64). Only negative EQ gain is used. Output is a local native-float
PCM file; no physical device, external media, or network media is opened.

The PCM AO can finish before HTTP play-readiness polling. Source loads therefore
use direct inherited MpvBackend.loadfile, not HTTP play; this lane validates DSP
controls and decoded samples, not HTTP playback readiness, production UNIX IPC,
REAL_AUDIO/hardware, audible output, latency, or arbitrary filter compatibility.
Readback of idle mpv 'af' proves configuration, not successful graph activation.
Measured captures additionally prove activation for the tested configurations.

A rejected same-label native filter replacement uses a deliberately nonexistent
mpv filter. This really exercises libmpv rejection and retained output, but does
not claim every invalid lavfi graph is rejected while idle (many are deferred to
playback). Native calls run in a child interpreter with a hard timeout.
"""

from __future__ import annotations

from array import array
import asyncio
import json
import locale
import math
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
from typing import Any
import wave

import pytest
from aiohttp import ClientSession, ClientTimeout, web

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.state import RendererState
from test_libmpv_integration import _LibmpvBackend, _eventually


_RATE = 16000
_CHANNELS = 2
_SOURCE_PEAK = 500
_SAFE_VOLUME = 25
_SAFE_GAIN = (_SAFE_VOLUME / 100) ** 3
_PLUGIN = "http://moddevices.com/plugins/caps/Eq4p"  # Identifier only; never fetched.
_LABEL = "fake_linkplay_dsp"


def _write_signal(path: Path, frequencies: tuple[int, int]) -> list[float]:
    """Generate exactly one second with distinguishable left/right tones."""
    values = [
        round(_SOURCE_PEAK * math.sin(2 * math.pi * frequency * index / _RATE))
        for index in range(_RATE) for frequency in frequencies
    ]
    assert max(abs(value) for value in values) <= _SOURCE_PEAK
    with wave.open(str(path), "wb") as output:
        output.setparams((_CHANNELS, 2, _RATE, 0, "NONE", "not compressed"))
        output.writeframes(struct.pack(f"<{len(values)}h", *values))
    return [value / 32768 for value in values]


def _measure(raw: bytes, source: list[float]) -> dict[str, Any]:
    """Compare steady-state PCM with the exact input, accounting for safe gain.

    Skip the first quarter-second for biquad startup transients. Both generated
    frequencies complete integer cycles in the remaining window. RMS tolerates
    legitimate filter phase shift; bypass/balance also check sample identity.
    """
    samples = array("f")
    assert samples.itemsize == 4
    samples.frombytes(raw)
    assert len(samples) == len(source), (len(samples), len(source))
    assert all(math.isfinite(value) for value in samples)
    channels = []
    start = (_RATE // 4) * _CHANNELS
    for channel in range(_CHANNELS):
        reference = source[start + channel::_CHANNELS]
        actual = samples[start + channel::_CHANNELS]
        complete_channel = samples[channel::_CHANNELS]
        reference_energy = sum(value * value for value in reference)
        assert reference_energy > 0
        rms_ratio = math.sqrt(sum(value * value for value in actual) / reference_energy) / _SAFE_GAIN
        channels.append({
            "normalized_rms_ratio": rms_ratio,
            "attenuation_db": 20 * math.log10(rms_ratio) if rms_ratio > 0 else None,
            "peak": max(abs(value) for value in complete_channel),
            "nonzero_samples": sum(value != 0.0 for value in complete_channel),
            "bypass_max_residual": max(abs(output - input_ * _SAFE_GAIN)
                                       for input_, output in zip(source[channel::_CHANNELS], complete_channel)),
        })
    return {"frames": len(samples) // _CHANNELS, "steady_state_frames": len(reference), "channels": channels}


def _assert_identity(measured: dict[str, Any], expected: tuple[float, float]) -> None:
    for channel, ratio in zip(measured["channels"], expected):
        assert channel["normalized_rms_ratio"] == pytest.approx(ratio, rel=1e-4, abs=1e-9)
        if ratio == 0:
            assert channel["nonzero_samples"] == 0, "Balance endpoint must be exact digital silence"
        else:
            assert channel["bypass_max_residual"] < 1e-7


async def _exercise_digital_dsp(
    library_path: str, directory: Path, cancel_ready: asyncio.Event | None = None,
) -> dict[str, Any]:
    baseline_tasks = asyncio.all_tasks()
    directory.mkdir(parents=True, exist_ok=True)
    tracks = (directory / "quiet-1000-4000.wav", directory / "quiet-4000-1000.wav")
    frequencies = {tracks[0]: (1000, 4000), tracks[1]: (4000, 1000)}
    sources = {track: _write_signal(track, frequencies[track]) for track in tracks}
    sink = directory / "dsp-native-float.pcm"
    assert not sink.exists(), "A fresh sink is required; captures must not be mixed"
    backend = _LibmpvBackend(library_path, tracks, pcm_output=sink)
    runner: web.AppRunner | None = None
    session: ClientSession | None = None
    listener: socket.socket | None = None
    monitor: asyncio.Task | None = None
    evidence: dict[str, Any] = {
        "lane": "REAL_BACKEND/digital-PCM-DSP", "library": library_path,
        "control_transport": "HTTP -> HttpApi -> MpvBackend -> test ctypes -> libmpv",
        "load_transport": "direct inherited MpvBackend.loadfile; no HTTP play/readiness assertion",
        "output": "local raw native-float PCM; no hardware/DAC",
        "source_peak": _SOURCE_PEAK / 32768,
        "safe_volume": _SAFE_VOLUME, "safe_amplitude_gain": _SAFE_GAIN,
        "captures": {},
    }
    try:
        evidence["mpv_version"] = backend.read_property("mpv-version")
        assert [entry["name"] for entry in backend.read_property("options/ao")] == ["pcm"]
        assert backend.read_property("options/audio-format") == "float"
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(socket.SOMAXCONN)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        cfg = Config(
            uuid="00000000-0000-4000-8000-000000000000",
            advertise_ip="127.0.0.1", bind_address="127.0.0.1",
            http_api_port=port, upnp_port=port, state_dir=str(directory / "state"),
            auto_spawn_mpv=False, enable_ssdp=False, enable_mdns=False,
        )
        api = HttpApi(cfg, RendererState.from_config(cfg), backend)
        await api.initialize_channel_balance()
        runner = web.AppRunner(api.app(), shutdown_timeout=1)
        await runner.setup()
        await web.SockSite(runner, listener).start()
        monitor = api._state_monitor_task
        assert monitor is not None and not monitor.done()
        offset = 0
        async with ClientSession(timeout=ClientTimeout(total=5), trust_env=False) as session:
            async def command(value: str, expected_status: int = 200) -> Any:
                async with session.get(f"http://127.0.0.1:{port}/httpapi.asp", params={"command": value}) as response:
                    body = await response.text()
                    assert response.status == expected_status, (value, response.status, body)
                    return json.loads(body) if response.content_type == "application/json" else body

            def graph() -> Any:
                return backend.read_property("af")

            def own_graph() -> str:
                entries = [entry for entry in graph() if entry.get("label") == _LABEL]
                assert len(entries) == 1, entries
                assert entries[0]["name"] == "lavfi" and entries[0]["enabled"] is True
                return entries[0]["params"]["graph"]

            async def set_band(*, freq: float, gain: float, mode: int, q: float = 0.707) -> None:
                assert gain <= 0, "This lane never enables positive EQ gain"
                payload = {"pluginURI": _PLUGIN, "EQBand": [
                    {"param_name": f"a_{name}", "value": value}
                    for name, value in {"freq": freq, "gain": gain, "q": q, "mode": mode}.items()
                ]}
                response = await command("EQSetLV2Band:" + json.dumps(payload))
                assert response == {"status": "OK"}, response
                current = await command("EQGetLV2Band:" + _PLUGIN)
                assert current["EQStat"] == "On"
                reported = {entry["param_name"]: entry["value"] for entry in current["EQBand"]}
                for entry in payload["EQBand"]:
                    assert reported[entry["param_name"]] == entry["value"]

            async def capture(name: str, track: Path = tracks[0]) -> dict[str, Any]:
                nonlocal offset
                assert backend.read_property("volume") == pytest.approx(_SAFE_VOLUME)
                assert backend.read_property("mute") is False
                configured = graph()
                size = len(sources[track]) * 4
                response = await backend.loadfile(str(track))
                assert response.get("error") == "success", response
                await _eventually(
                    f"{name} complete PCM capture and EOF",
                    lambda: sink.exists() and sink.stat().st_size >= offset + size
                    and backend.read_property("idle-active") is True,
                    timeout=5,
                )
                assert sink.stat().st_size == offset + size, (name, sink.stat().st_size, offset + size)
                with sink.open("rb") as output:
                    output.seek(offset)
                    raw = output.read(size)
                offset += size
                assert graph() == configured, "Source load changed configured filter chain"
                measured = _measure(raw, sources[track])
                evidence["captures"][name] = {
                    "source": track.name, "frequencies_hz": frequencies[track],
                    "af": configured, **measured,
                }
                return measured

            # Explicit controls make sample comparisons independent of startup
            # configuration or libmpv's native default volume of 100.
            assert await command(f"setPlayerCmd:vol:{_SAFE_VOLUME}") == "OK"
            assert await command("setPlayerCmd:mute:0") == "OK"
            assert await command("setChannelBalance:0") == "OK"
            assert await command("EQOff") == {"status": "0"}
            assert graph() == []
            _assert_identity(await capture("bypass"), (1, 1))
            if cancel_ready is not None:
                cancel_ready.set()
                await asyncio.Future()

            assert await command("EQChangeFX:" + _PLUGIN) == {"status": "OK"}
            await set_band(freq=1000, gain=-12, mode=1, q=1)
            assert "equalizer=f=1000.000" in own_graph()
            cut_graph = graph()
            cut = await capture("eq_minus_12db")
            expected_cut = 10 ** (-12 / 20)
            assert cut["channels"][0]["normalized_rms_ratio"] == pytest.approx(expected_cut, rel=0.02)
            assert cut["channels"][1]["normalized_rms_ratio"] > 0.8
            reloaded = await capture("eq_retained_new_source", tracks[1])
            assert graph() == cut_graph
            assert reloaded["channels"][1]["normalized_rms_ratio"] == pytest.approx(expected_cut, rel=0.02)
            assert reloaded["channels"][0]["normalized_rms_ratio"] > 0.8

            assert await command("setChannelBalance:0.5") == "OK"
            assert "equalizer=" in own_graph() and "pan=" in own_graph()
            combined = await capture("eq_balance_combined")
            assert combined["channels"][0]["normalized_rms_ratio"] == pytest.approx(expected_cut / 2, rel=0.02)
            assert combined["channels"][1]["normalized_rms_ratio"] > 0.8
            assert await command("setChannelBalance:0") == "OK"
            assert graph() == cut_graph

            # Invalid API candidates must fail without changing reported EQ,
            # the real graph, or measured attenuation. This is validation
            # rejection, distinguished from the native replacement below.
            prior_reported = await command("EQGetLV2Band:" + _PLUGIN)
            rejected_payload = {"pluginURI": _PLUGIN, "EQBand": [
                {"param_name": "a_mode", "value": 4},
            ]}
            assert await command("EQSetLV2Band:" + json.dumps(rejected_payload), 400) == "Failed"
            assert await command("EQGetLV2Band:" + _PLUGIN) == prior_reported
            assert graph() == cut_graph
            rejected = await capture("eq_retained_after_http_validation_rejection")
            assert rejected["channels"][0]["normalized_rms_ratio"] == pytest.approx(expected_cut, rel=0.02)
            evidence["rejected_http_candidate"] = {
                "input": "unsupported a_mode=4", "http_status": 400,
                "reported_state_retained": True, "prior_graph_retained": True,
            }

            # This is an actual failed native command, not a fabricated error.
            # Invalid lavfi internals may be accepted while idle, so use an
            # unknown mpv filter for a deterministic native parse-time failure.
            failed = await backend.command(["af", "add", f"@{_LABEL}:orr_nonexistent_filter"])
            assert failed.get("error") not in {None, "success"}, failed
            assert graph() == cut_graph
            retained = await capture("eq_retained_after_native_rejection")
            assert retained["channels"][0]["normalized_rms_ratio"] == pytest.approx(expected_cut, rel=0.02)
            evidence["failed_replacement"] = {
                "transport": "direct acknowledged native af add; not an HTTP candidate",
                "response": failed, "prior_graph_retained": graph() == cut_graph,
                "limitation": "Does not imply idle libmpv validates arbitrary lavfi graph internals",
            }

            assert await command("EQOff") == {"status": "0"}
            assert graph() == []
            _assert_identity(await capture("eq_bypass_restored"), (1, 1))
            for value, expected in ((-1, (1, 0)), (1, (0, 1)), (0, (1, 1))):
                assert await command(f"setChannelBalance:{value}") == "OK"
                assert float(await command("getChannelBalance")) == value
                _assert_identity(await capture(f"balance_{value}"), expected)
            assert graph() == []

            assert await command("EQChangeFX:" + _PLUGIN) == {"status": "OK"}
            await set_band(freq=2000, gain=0, mode=3)
            assert "lowpass=f=2000.000" in own_graph()
            lowpass = await capture("zero_gain_lowpass")
            assert lowpass["channels"][0]["normalized_rms_ratio"] > 0.85
            assert lowpass["channels"][1]["normalized_rms_ratio"] < 0.25
            await set_band(freq=2000, gain=0, mode=5)
            assert "highpass=f=2000.000" in own_graph()
            highpass = await capture("zero_gain_highpass")
            assert highpass["channels"][0]["normalized_rms_ratio"] < 0.3
            assert highpass["channels"][1]["normalized_rms_ratio"] > 0.85
            assert await command("EQOff") == {"status": "0"}
            assert graph() == []
            _assert_identity(await capture("final_bypass"), (1, 1))
            evidence["captured_bytes"] = offset
    finally:
        try:
            if runner is not None:
                await runner.cleanup()
        finally:
            if listener is not None:
                listener.close()
            backend.close()
        await asyncio.sleep(0)
        pending = [task for task in asyncio.all_tasks() - baseline_tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        assert not pending, f"Digital DSP fixture leaked tasks: {pending!r}"
        assert session is None or session.closed
        assert listener is None or listener.fileno() == -1
        assert runner is None or not runner.sites
        assert backend._handle is None and backend.process is None
        assert monitor is None or monitor.done(), "HttpApi monitor survived cleanup"
    evidence["cleanup"] = {"handle_closed": True, "monitor_done": True, "no_pending_tasks": True}
    return evidence


@pytest.mark.skipif(not os.environ.get("ORR_TEST_LIBMPV_PATH"), reason="REAL_BACKEND/digital-PCM-DSP requires explicit ORR_TEST_LIBMPV_PATH")
def test_http_api_real_libmpv_digital_dsp(tmp_path: Path) -> None:
    library_path = Path(os.environ["ORR_TEST_LIBMPV_PATH"]).expanduser().resolve()
    assert library_path.is_file(), f"Configured libmpv does not exist: {library_path}"
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--probe", str(library_path), str(tmp_path)],
        cwd=root, env=env, text=True, capture_output=True, timeout=45, check=False,
    )
    assert completed.returncode == 0, f"REAL_BACKEND/digital-PCM-DSP failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    evidence = json.loads(completed.stdout)
    assert evidence["lane"] == "REAL_BACKEND/digital-PCM-DSP"
    assert evidence["cleanup"] == {"handle_closed": True, "monitor_done": True, "no_pending_tasks": True}
    assert evidence["cleanup_after_cancellation"] is True
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 4 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")

    async def main() -> None:
        async with asyncio.timeout(35):
            directory = Path(sys.argv[3])
            evidence = await _exercise_digital_dsp(sys.argv[2], directory / "dsp")
            cancel_ready = asyncio.Event()
            probe = asyncio.create_task(_exercise_digital_dsp(sys.argv[2], directory / "cancel", cancel_ready))
            try:
                await asyncio.wait_for(cancel_ready.wait(), timeout=8)
                probe.cancel()
                try:
                    await probe
                except asyncio.CancelledError:
                    evidence["cleanup_after_cancellation"] = True
                else:
                    raise AssertionError("Probe did not propagate cancellation")
            finally:
                if not probe.done():
                    probe.cancel()
                    await asyncio.gather(probe, return_exceptions=True)
        print(json.dumps(evidence, sort_keys=True))

    asyncio.run(main())
