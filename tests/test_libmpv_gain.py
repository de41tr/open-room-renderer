"""Optional REAL_BACKEND/digital-PCM gain and mute sample acceptance.

Opt in with ORR_TEST_LIBMPV_PATH=/absolute/path/to/libmpv.so.2, then run:
    PYTHONPATH=. .venv/bin/pytest -q -s tests/test_libmpv_gain.py

Real loopback HTTP commands use production HttpApi and inherited MpvBackend
volume/mute controls. The existing test-only ctypes transport feeds libmpv;
its decoder and mixer write raw float PCM to a local file, never a device/DAC.
Sources are generated locally with peak <= 500/32768 (about -36.3 dBFS).

The PCM file sink can finish faster than HTTP playback-readiness polling. Gain
and mute are therefore set over HTTP BEFORE each direct MpvBackend.loadfile.
Load acknowledgements and captured samples are checked; this lane makes no
claim that an HTTP play request reached ready/play. Production UNIX JSON IPC,
audible output, hardware volume, device compatibility and latency are untested.
The null-output lane separately exercises HTTP play/pause/readiness behavior.
Persisted gain is also checked by closing the first fixture and reconstructing
fresh Config/RendererState/backend objects before decoding a new first source;
this is not a full CLI, process, or systemd restart.

mpv's volume percentage is cubic in amplitude, not linear: 25 -> 1/64,
50 -> 1/8, and 100 -> 1. Native calls run in a child interpreter with a hard
timeout because cancelling asyncio cannot interrupt synchronous C calls.
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

from fake_linkplay.config import Config, load_config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.persistence import load_gain
from fake_linkplay.state import RendererState
from test_libmpv_integration import _LibmpvBackend, _eventually


_RATE = 16000
_CHANNELS = 2
_SOURCE_PEAK = 500


def _write_quiet_tone(path: Path, frequencies: tuple[int, int]) -> list[float]:
    """One second of distinct stereo PCM, returning exact normalized samples."""
    integers = [
        round(_SOURCE_PEAK * math.sin(2 * math.pi * frequency * index / _RATE))
        for index in range(_RATE) for frequency in frequencies
    ]
    assert max(abs(value) for value in integers) <= _SOURCE_PEAK
    with wave.open(str(path), "wb") as output:
        output.setparams((_CHANNELS, 2, _RATE, 0, "NONE", "not compressed"))
        output.writeframes(struct.pack(f"<{len(integers)}h", *integers))
    return [value / 32768 for value in integers]


def _measure_samples(raw: bytes, source: list[float], expected_gain: float) -> dict[str, Any]:
    """Measure both channels, not a property or the renderer's cached state."""
    samples = array("f")  # The PCM sink's float format is native-endian.
    assert samples.itemsize == 4, "This lane requires IEEE 32-bit native floats"
    samples.frombytes(raw)
    assert len(samples) == len(source), (len(samples), len(source))
    assert all(math.isfinite(sample) for sample in samples)
    measurements = []
    for channel in range(_CHANNELS):
        reference = source[channel::_CHANNELS]
        actual = samples[channel::_CHANNELS]
        source_energy = sum(sample * sample for sample in reference)
        assert source_energy > 0
        gain = sum(left * right for left, right in zip(reference, actual)) / source_energy
        rms_gain = math.sqrt(sum(sample * sample for sample in actual) / source_energy)
        residual = max(abs(output - expected_gain * input_) for input_, output in zip(reference, actual))
        nonzero = sum(sample != 0.0 for sample in actual)
        if expected_gain == 0:
            assert nonzero == 0, f"Channel {channel} is not exact digital silence"
            assert gain == 0 and rms_gain == 0 and residual == 0
        else:
            assert nonzero > 0
            assert gain == pytest.approx(expected_gain, rel=1e-4, abs=1e-9)
            assert rms_gain == pytest.approx(expected_gain, rel=1e-4, abs=1e-9)
            # Float capture keeps this tighter than one s16 quantization step,
            # even for the very quiet 25-percent output. Also proves identity
            # and channel order, rather than merely similar aggregate energy.
            assert residual < 1e-7, (channel, residual)
        measurements.append({
            "gain": gain, "rms_gain": rms_gain,
            "gain_db": 20 * math.log10(gain) if gain > 0 else None,
            "peak": max(abs(sample) for sample in actual),
            "nonzero_samples": nonzero, "max_residual": residual,
        })
    return {"frames": len(samples) // _CHANNELS, "channels": measurements}


async def _exercise_digital_gain(
    library_path: str, directory: Path, cancel_ready: asyncio.Event | None = None,
    *, restore_config: Path | None = None,
) -> dict[str, Any]:
    baseline_tasks = asyncio.all_tasks()
    directory.mkdir(parents=True, exist_ok=True)
    if restore_config is None:
        cfg = Config(
            uuid="00000000-0000-4000-8000-000000000000",
            advertise_ip="127.0.0.1", bind_address="127.0.0.1",
            state_dir=str(directory / "state"),
            auto_spawn_mpv=False, enable_ssdp=False, enable_mdns=False,
        )
    else:
        # load_config calls production load_persistent_runtime_state. A new
        # object must recover the HTTP-confirmed values from disk, not retain
        # the previous live Config's mutated defaults or renderer state.
        cfg = load_config(restore_config)
        assert (cfg.default_volume, cfg.default_mute) == (25, True)
    state = RendererState.from_config(cfg)
    tracks = (directory / "quiet-440-733.wav", directory / "quiet-660-997.wav")
    sources = {path: _write_quiet_tone(path, frequencies) for path, frequencies in
               zip(tracks, ((440, 733), (660, 997)))}
    sink = directory / "decoded-native-float.pcm"
    assert not sink.exists(), "A fresh sink is required; captures must not be mixed"
    backend = _LibmpvBackend(
        library_path, tracks, pcm_output=sink,
        initial_volume=state.volume, initial_mute=state.mute,
    )
    runner: web.AppRunner | None = None
    session: ClientSession | None = None
    listener: socket.socket | None = None
    monitor: asyncio.Task | None = None
    evidence: dict[str, Any] = {
        "lane": "REAL_BACKEND/digital-PCM", "library": library_path,
        "control_transport": "HTTP -> HttpApi -> MpvBackend -> test ctypes -> libmpv",
        "load_transport": "direct inherited MpvBackend.loadfile; no HTTP play/readiness assertion",
        "output": "local raw native-float PCM; no hardware/DAC",
        "source_peak": _SOURCE_PEAK / 32768,
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
        cfg.http_api_port = cfg.upnp_port = port
        api = HttpApi(cfg, state, backend)
        runner = web.AppRunner(api.app(), shutdown_timeout=1)
        await runner.setup()
        await web.SockSite(runner, listener).start()
        monitor = api._state_monitor_task
        assert monitor is not None and not monitor.done()
        offset = 0
        async with ClientSession(timeout=ClientTimeout(total=5), trust_env=False) as session:
            async def command(value: str) -> Any:
                async with session.get(f"http://127.0.0.1:{port}/httpapi.asp", params={"command": value}) as response:
                    body = await response.text()
                    assert response.status == 200, (value, response.status, body)
                    return json.loads(body) if response.content_type == "application/json" else body

            async def controls(volume: int, mute: bool) -> dict[str, Any]:
                status = await command("getPlayerStatus")
                assert status["vol"] == str(volume), status
                assert status["mute"] == str(int(mute)), status
                assert backend.read_property("volume") == pytest.approx(volume, abs=0.001)
                assert backend.read_property("mute") is mute
                return {"reported_volume": int(status["vol"]), "reported_mute": bool(int(status["mute"]))}

            async def capture(name: str, track: Path, gain: float) -> None:
                nonlocal offset
                size = len(sources[track]) * 4
                # PCM AO may reach EOF before the first poll. Requiring new
                # complete output AND idle prevents an initial-idle false pass.
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
                measured = _measure_samples(raw, sources[track], gain)
                evidence["captures"][name] = {"source": track.name, "expected_gain": gain, **measured}

            # HttpApi construction is not CLI startup orchestration. Expose the
            # raw boundary, then use the production gain-initialization hook.
            # Never change native defaults to make the fixture seem consistent.
            initial = await command("getPlayerStatus")
            initial_volume = backend.read_property("volume")
            initial_mute = backend.read_property("mute")
            evidence["raw_construction"] = {
                "api_volume": int(initial["vol"]), "backend_volume": initial_volume,
                "api_mute": bool(int(initial["mute"])), "backend_mute": initial_mute,
                "production_startup_exercised": False,
            }
            await backend.initialize_gain()
            initial_controls = await controls(cfg.default_volume, cfg.default_mute)
            evidence["initialized_startup"] = {
                "hook": "MpvBackend.initialize_gain; CLI orchestration is not exercised",
                **initial_controls,
                "backend_volume": backend.read_property("volume"),
                "backend_mute": backend.read_property("mute"),
            }
            first_gain = 0.0 if cfg.default_mute else (cfg.default_volume / 100) ** 3
            first_capture = "restored_first_load_muted" if restore_config else "initialized_startup"
            await capture(first_capture, tracks[0], first_gain)
            await controls(cfg.default_volume, cfg.default_mute)

            if cancel_ready is not None:
                # Cancellation occurs with the native handle, HTTP listener
                # and monitor alive. The same finally block verifies teardown.
                cancel_ready.set()
                await asyncio.Future()

            if restore_config is not None:
                evidence["reconstruction"] = {
                    "scope": "fresh Config, RendererState and native backend; not CLI/process/systemd restart",
                    "config_loader": "load_config -> load_persistent_runtime_state",
                    "restored_volume": 25, "restored_mute": True,
                }
                assert await command("setPlayerCmd:mute:0") == "OK"
                await controls(25, False)
                await capture("restored_unmuted_retains_volume_25", tracks[1], 1 / 64)
                await controls(25, False)
            else:
                assert await command("setPlayerCmd:mute:0") == "OK"
                for volume, gain in ((100, 1.0), (25, 1 / 64), (50, 1 / 8), (0, 0.0)):
                    assert await command(f"setPlayerCmd:vol:{volume}") == "OK"
                    await controls(volume, False)
                    await capture(f"volume_{volume}", tracks[0], gain)
                    await controls(volume, False)

                assert await command("setPlayerCmd:vol:100") == "OK"
                assert await command("setPlayerCmd:mute:1") == "OK"
                await controls(100, True)
                await capture("muted_at_100", tracks[0], 0.0)
                assert await command("setPlayerCmd:vol:25") == "OK"
                await controls(25, True)
                await capture("muted_after_volume_25", tracks[0], 0.0)
                await controls(25, True)
                await capture("muted_after_new_source", tracks[1], 0.0)
                await controls(25, True)
                assert await command("setPlayerCmd:mute:0") == "OK"
                await controls(25, False)
                await capture("unmuted_retains_volume_25", tracks[1], 1 / 64)
                await controls(25, False)

                # Leave HTTP-confirmed 25 + mute persisted, then close every
                # original live object before reconstructing from this file.
                assert await command("setPlayerCmd:mute:1") == "OK"
                await controls(25, True)
                assert load_gain(cfg.state_dir) == (25, True)
                (directory / "reconstruction.yaml").write_text(json.dumps({
                    "uuid": cfg.uuid, "advertise_ip": "127.0.0.1",
                    "bind_address": "127.0.0.1", "state_dir": cfg.state_dir,
                    "auto_spawn_mpv": False, "enable_ssdp": False, "enable_mdns": False,
                    # Persisted values must override these differing defaults.
                    "default_volume": 50, "default_mute": False,
                }), encoding="utf-8")
                evidence["persisted_before_reconstruction"] = {"volume": 25, "mute": True}
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
        assert not pending, f"Digital gain fixture leaked tasks: {pending!r}"
        assert session is None or session.closed
        assert listener is None or listener.fileno() == -1
        assert runner is None or not runner.sites
        assert backend._handle is None
        assert backend.process is None
        assert monitor is None or monitor.done(), "HttpApi monitor survived cleanup"
    evidence["cleanup"] = {"handle_closed": True, "monitor_done": True, "no_pending_tasks": True}
    return evidence


@pytest.mark.skipif(not os.environ.get("ORR_TEST_LIBMPV_PATH"), reason="REAL_BACKEND/digital-PCM requires explicit ORR_TEST_LIBMPV_PATH")
def test_http_api_real_libmpv_digital_gain(tmp_path: Path) -> None:
    library_path = Path(os.environ["ORR_TEST_LIBMPV_PATH"]).expanduser().resolve()
    assert library_path.is_file(), f"Configured libmpv does not exist: {library_path}"
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    # subprocess.run kills AND waits on timeout. No mpv child, scripts or IPC
    # server is launched; the C library lives only in this bounded interpreter.
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--probe", str(library_path), str(tmp_path)],
        cwd=root, env=env, text=True, capture_output=True, timeout=40, check=False,
    )
    assert completed.returncode == 0, f"REAL_BACKEND/digital-PCM failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    evidence = json.loads(completed.stdout)
    assert evidence["lane"] == "REAL_BACKEND/digital-PCM"
    assert evidence["cleanup"] == {"handle_closed": True, "monitor_done": True, "no_pending_tasks": True}
    assert evidence["cleanup_after_cancellation"] is True
    restored = evidence["persisted_reconstruction"]
    assert restored["cleanup"] == evidence["cleanup"]
    assert restored["reconstruction"]["restored_volume"] == 25
    assert restored["reconstruction"]["restored_mute"] is True
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 4 and sys.argv[1] == "--probe"
    locale.setlocale(locale.LC_NUMERIC, "C")

    async def main() -> None:
        async with asyncio.timeout(30):
            directory = Path(sys.argv[3])
            evidence = await _exercise_digital_gain(sys.argv[2], directory / "gain")
            evidence["persisted_reconstruction"] = await _exercise_digital_gain(
                sys.argv[2], directory / "restored",
                restore_config=directory / "gain" / "reconstruction.yaml",
            )
            cancel_ready = asyncio.Event()
            probe = asyncio.create_task(_exercise_digital_gain(sys.argv[2], directory / "cancel", cancel_ready))
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
