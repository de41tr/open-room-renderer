"""Clocked digital-only queue probe, launched by test_clocked_queue_transition.

The private PulseAudio daemon loads only a null sink and a local protocol socket;
no physical device, host audio server, system service or external media is used.
The monitor records idle zero frames continuously, unlike mpv's untimed PCM AO.
The fixture disables null-sink rewinds to simplify its clocked capture path.
Official clock description:
https://www.freedesktop.org/wiki/Software/PulseAudio/Documentation/User/Modules/#module-null-sink

A successful measurement is NOT a gapless certification. The report identifies
whether this one transition exceeds 2 ms. Rates refer to source WAVs; the sink
and recorder stay at 48 kHz. Mixed-rate coverage does not test DAC rate switching,
encoder delay/padding, network media, or hardware. Generated WAVs have no codec
padding metadata. Queue titles/durations and delivered GENA events are checked.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from html import escape
import json
import math
import os
from pathlib import Path
import platform
import shutil
import socket
import struct
import sys
import tempfile
import time
from typing import Any
import wave
from xml.etree import ElementTree as ET

from aiohttp import ClientSession, ClientTimeout, web

from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.mpv_backend import BackendError, MpvBackend
from fake_linkplay.state import RendererState

RATE = 48000
AMPLITUDE = 256 / 32768  # -42.1 dBFS; digital-only output regardless.
GAP_TOLERANCE_FRAMES = 96  # 2 ms, including the boundary classification tolerance.
RIGHT_SEQUENCE_MIN_MATCH = .95  # Allows resampled square-wave edges, rejects DC/inversion.
DURATION = 2


def source_frames(rate: int, sign: int, seconds: float = DURATION) -> bytes:
    """Defined nonzero stereo sequence: signed DC marker plus a 400 Hz square.

    DC polarity distinguishes A from B without zero-crossing ambiguity. The
    independent right channel checks channel order and unexpected corruption.
    Both two-second tracks contain integral periods, so their right-channel
    sequence is adjacent in time even when their source sample rates differ.
    """
    assert sign in (-1, 1)
    return b"".join(struct.pack("<hh", sign * 256, 128 if (index * 800 // rate) % 2 == 0 else -128)
                    for index in range(round(rate * seconds)))


def write_source(path: Path, rate: int, sign: int, *, calibration: bool = False) -> None:
    raw = source_frames(rate, sign)
    if calibration:
        raw = source_frames(rate, 1, .25) + bytes(round(rate * .125) * 4) + source_frames(rate, -1, .25)
    with wave.open(str(path), "wb") as output:
        output.setparams((2, 2, rate, 0, "NONE", "not compressed"))
        output.writeframes(raw)


def capture_diagnostics(raw: bytes, *, expected_frames: int) -> dict[str, Any]:
    """Bounded, assertion-free marker evidence retained even for invalid takes."""
    complete = len(raw) - len(raw) % 8
    frames = list(struct.iter_unpack("<ff", raw[:complete]))
    markers = []
    for name, sign in (("a", 1), ("b", -1)):
        positions = [index for index, (left, _) in enumerate(frames) if sign * left > AMPLITUDE / 2]
        def edge_summary(indices):
            return {"frames": len(indices),
                "left_min": min((frames[index][0] for index in indices), default=None),
                "left_max": max((frames[index][0] for index in indices), default=None),
                "right_min": min((frames[index][1] for index in indices), default=None),
                "right_max": max((frames[index][1] for index in indices), default=None)}
        markers.append({"marker": name, "signal_frames": len(positions),
            "frame_count_delta": len(positions) - expected_frames,
            "first_frame": positions[0] if positions else None,
            "last_frame": positions[-1] if positions else None,
            "internal_nonsignal_frames": positions[-1] - positions[0] + 1 - len(positions) if positions else None,
            # Only aggregates leave the process. Raw PCM and sample traces
            # stay private and are discarded with the fixture.
            "first_edge_summary": edge_summary(positions[:96]),
            "last_edge_summary": edge_summary(positions[-96:]),
            "right_sequence_sign_match_fraction": sum(
                (frames[index][1] > 0) == ((ordinal // 60) % 2 == 0)
                for ordinal, index in enumerate(positions)) / len(positions) if positions else None})
    active = [index for index, frame in enumerate(frames) if any(abs(value) > 1 / 32768 for value in frame)]
    return {"capture_frames": len(frames), "partial_bytes": len(raw) % 8,
        "expected_marker_frames": expected_frames, "markers": markers,
        "weak_positive_frames": sum(1 / 32768 < left <= AMPLITUDE / 2 for left, _ in frames),
        "weak_negative_frames": sum(-AMPLITUDE / 2 <= left < -1 / 32768 for left, _ in frames),
        "leading_quiet_frames": active[0] if active else len(frames),
        "trailing_quiet_frames": len(frames) - active[-1] - 1 if active else len(frames),
        "marker_separation_frames": markers[1]["first_frame"] - markers[0]["last_frame"] - 1
            if markers[0]["last_frame"] is not None and markers[1]["first_frame"] is not None else None}


def measure_capture(raw: bytes, *, expected_frames: int = RATE * DURATION) -> dict[str, Any]:
    """Measure ordered marker spans and digital silence in a clocked recording.

    No wall-clock gap is inferred from callback arrival times. Those are used
    separately to validate recorder pacing. All transition measurements below
    are counts in the continuously sampled monitor stream.
    """
    assert raw and len(raw) % 8 == 0, "Capture is empty or has a partial stereo float frame"
    frames = list(struct.iter_unpack("<ff", raw))
    assert all(math.isfinite(value) for frame in frames for value in frame), "Nonfinite capture"
    a = [index for index, (left, _) in enumerate(frames) if left > AMPLITUDE / 2]
    b = [index for index, (left, _) in enumerate(frames) if left < -AMPLITUDE / 2]
    assert a and b, "Both generated tracks must reach the sink"
    assert a[-1] < b[0], "Track markers overlap or arrive out of order"
    boundary = frames[a[-1] + 1:b[0]]
    # The source has no silent samples. Near-zero output is introduced in the
    # playback path, not confused with the generated signal's zero crossings.
    silence_threshold = 1 / 32768
    silent = [abs(left) <= silence_threshold and abs(right) <= silence_threshold for left, right in boundary]
    longest = current = 0
    for quiet in silent:
        current = current + 1 if quiet else 0
        longest = max(longest, current)
    tracks = []
    for marker, sign in ((a, 1), (b, -1)):
        # Permit <=2 ms at the edge for resampling/filter transients; still
        # report exact frame count and loss/extra samples rather than hide it.
        assert abs(len(marker) - expected_frames) <= GAP_TOLERANCE_FRAMES, {"error": "incomplete marker",
            **capture_diagnostics(raw, expected_frames=expected_frames)}
        interior = marker[96:-96]
        assert interior, "Track too short to validate"
        left_error = max(abs(frames[index][0] - sign * AMPLITUDE) for index in interior)
        right_good = sum(abs(frames[index][1]) >= AMPLITUDE / 4 for index in interior) / len(interior)
        right_peak = max(abs(frames[index][1]) for index in interior)
        sequence_match = sum(
            (frames[index][1] > 0) == ((ordinal // 60) % 2 == 0)
            for ordinal, index in enumerate(marker)
        ) / len(marker)
        assert left_error < 2 / 32768, f"Wrong marker gain/channel: {left_error}"
        assert right_good > .9, f"Right-channel defined waveform missing: {right_good}"
        assert right_peak < AMPLITUDE / 2 + 2 / 32768, f"Unexpected right-channel gain: {right_peak}"
        # Nearest-neighbor conversion of the 44.1 kHz fixture to this sink
        # yields ~98.54% sign agreement; permit edge interpolation without
        # accepting DC (50%) or a sign-inverted square (approximately 0%).
        assert sequence_match >= RIGHT_SEQUENCE_MIN_MATCH, f"Right-channel sequence corrupted: {sequence_match}"
        holes = marker[-1] - marker[0] + 1 - len(marker)
        tracks.append({"first_frame": marker[0], "last_frame": marker[-1], "signal_frames": len(marker),
                       "frame_count_delta": len(marker) - expected_frames, "internal_nonsignal_frames": holes,
                       "max_interior_left_error": left_error, "right_signal_fraction": right_good,
                       "right_sequence_sign_match_fraction": sequence_match,
                       "right_sequence_min_match_fraction": RIGHT_SEQUENCE_MIN_MATCH})
    gap = len(boundary)
    outcome = ("GAP_OBSERVED" if longest > GAP_TOLERANCE_FRAMES else
               "WITHIN_TOLERANCE" if gap <= GAP_TOLERANCE_FRAMES else "NON_SILENT_BOUNDARY")
    return {"capture_frames": len(frames), "sink_rate": RATE, "gap_frames": gap,
            "gap_ms": gap * 1000 / RATE, "silence_frames": sum(silent),
            "longest_silence_frames": longest, "longest_silence_ms": longest * 1000 / RATE,
            "non_silent_boundary_frames": len(silent) - sum(silent),
            "measurement": "marker separation and near-digital-zero frames at the clocked sink",
            "tolerance_frames": GAP_TOLERANCE_FRAMES,
            "tolerance_ms": GAP_TOLERANCE_FRAMES * 1000 / RATE,
            "outcome": outcome,
            "tracks": tracks}


async def eventually(description, check, timeout: float = 8):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = await check()
            if last:
                return last
        except BackendError as exc:
            last = str(exc)
        await asyncio.sleep(.02)
    raise AssertionError(f"Timed out waiting for {description}: {last!r}")


async def sample_audio_parameters(prop, *, timeout: float = .2) -> dict[str, Any]:
    """Bound optional metadata reads during lazy AO creation.

    idle-active=false means a file is loading, not that audio-params already
    exists. Capture starts before load and remains authoritative; unavailable
    diagnostic properties must never abort or replace sample validation.
    """
    values: dict[str, Any] = {"source": None, "output": None}
    names = {"source": "audio-params", "output": "audio-out-params"}
    started = time.monotonic()
    try:
        async with asyncio.timeout(timeout):
            while any(value is None for value in values.values()):
                for key, name in names.items():
                    if values[key] is None:
                        try:
                            values[key] = await prop(name)
                        except BackendError:
                            # Retry only these optional observations. Native
                            # sample/count/sequence checks still fail normally.
                            pass
                if any(value is None for value in values.values()):
                    await asyncio.sleep(.01)
    except TimeoutError:
        pass
    return {**values, "unavailable": [key for key, value in values.items() if value is None],
            "sampling_seconds": time.monotonic() - started}


async def reap(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), 3)
    except TimeoutError:
        process.kill()
        await asyncio.wait_for(process.wait(), 3)
    assert process.returncode is not None


class Capture:
    def __init__(self, process):
        self.process = process
        self.raw = bytearray()
        self.task = asyncio.create_task(self.read())

    async def read(self):
        while block := await self.process.stdout.read(8192):
            self.raw.extend(block)

    @property
    def frames(self):
        return len(self.raw) // 8

    async def wait_frames(self, count: int):
        async def enough():
            assert self.process.returncode is None, "Clocked recorder exited"
            return self.frames >= count
        await eventually("continuous monitor frames", enough)


async def probe(directory: Path) -> dict[str, Any]:
    baseline_tasks = asyncio.all_tasks()
    executables = {name: shutil.which(name) for name in ("mpv", "pulseaudio", "parec", "pactl")}
    assert all(executables.values()), f"Explicit clocked lane needs native packages mpv, pulseaudio, pulseaudio-utils: {executables}"
    processes = []
    capture = None
    backend = None
    runner = None
    listener = None
    monitor = None
    evidence: dict[str, Any] = {"lane": "PRODUCTION_UNIX_IPC/clocked-PulseAudio-null-monitor",
        "architecture": platform.machine(), "sink_rate": RATE, "source_peak": AMPLITUDE,
        "scope": "generated stereo WAVs, private system-clocked digital sink; no hardware or codec-gapless-metadata claim",
        "cases": {}}
    async with AsyncExitStack() as files:
        async def spawn(name, *args, env):
            log = files.enter_context((directory / f"{name}.log").open("wb"))
            process = await asyncio.create_subprocess_exec(*args, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE if name == "recorder" else log, stderr=log)
            processes.append(process)
            return process

        try:
            home = directory / "home"
            home.mkdir(mode=0o700)
            runtime = directory / "runtime"
            runtime.mkdir(mode=0o700)
            config = directory / "config" / "pulse"
            config.mkdir(parents=True)
            (config / "daemon.conf").write_text("default-sample-rate = 48000\nalternate-sample-rate = 48000\n"
                "default-sample-format = float32le\nresample-method = trivial\navoid-resampling = no\n", encoding="utf-8")
            server = f"unix:{runtime / 'native'}"
            # Anonymous private fixture only: never consult a real user's
            # inherited PulseAudio cookie or legacy per-user server settings.
            cookie = config / "fixture-cookie"
            cookie.write_bytes(bytes(256))
            cookie.chmod(0o600)
            (config / "client.conf").write_text(f"autospawn = no\ndefault-server = {server}\nenable-shm = no\ncookie-file = {cookie}\n", encoding="utf-8")
            clean_env = {key: value for key, value in os.environ.items() if not key.startswith("PULSE_")}
            env = {**clean_env, "HOME": str(home), "PULSE_COOKIE": str(cookie), "PULSE_SERVER": server, "PULSE_RUNTIME_PATH": str(runtime),
                "PULSE_STATE_PATH": str(directory / "pulse-state"), "XDG_CONFIG_HOME": str(config.parent),
                "PULSE_CLIENTCONFIG": str(config / "client.conf"),
                "PULSE_CONFIG": str(config / "daemon.conf"), "LC_ALL": "C"}
            # A caller must not redirect the private daemon to another
            # configuration or inject alternate modules through the search path.
            env.pop("PULSE_DLPATH", None)
            pulse = await spawn("pulse", executables["pulseaudio"], "-n", "--daemonize=no", "--use-pid-file=no",
                "--exit-idle-time=-1", "--disable-shm=yes", "--realtime=no", "--high-priority=no",
                "--log-target=stderr", "--log-level=warning",
                f"--load=module-native-protocol-unix socket={runtime / 'native'} auth-anonymous=yes auth-cookie={cookie}",
                # PulseAudio v16.1 module-null-sink.c exposes norewinds and
                # sets max_rewind=0. Remove rewind accounting as a variable
                # when validating the append-only clocked capture. The native
                # protocol also compensates rewindable sources; a count loss
                # alone does not establish rewinds as the cause.
                # https://github.com/pulseaudio/pulseaudio/blob/v16.1/src/modules/module-null-sink.c
                "--load=module-null-sink sink_name=orr_transition rate=48000 channels=2 format=float32le norewinds=true",
                env=env)

            async def pulse_ready():
                assert pulse.returncode is None, (directory / "pulse.log").read_text(errors="replace")
                return (runtime / "native").is_socket()
            await eventually("private PulseAudio socket", pulse_ready)

            async def pactl(*args):
                process = await asyncio.create_subprocess_exec(executables["pactl"], "--server", server, *args,
                    env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                processes.append(process)
                stdout, stderr = await asyncio.wait_for(process.communicate(), 4)
                assert process.returncode == 0, stderr.decode(errors="replace")
                return stdout.decode()
            sinks = await pactl("list", "short", "sinks")
            assert len(sinks.strip().splitlines()) == 1 and "orr_transition" in sinks and "48000Hz" in sinks, sinks
            modules = await pactl("list", "short", "modules")
            assert {line.split("\t")[1] for line in modules.strip().splitlines()} == {"module-null-sink", "module-native-protocol-unix"}, modules
            assert "norewinds=true" in modules, modules
            evidence["sink"] = sinks.strip()
            evidence["sink_rewinds"] = False
            recorder = await spawn("recorder", executables["parec"], "--server", server,
                "--device=orr_transition.monitor", "--raw", "--format=float32le", "--rate=48000",
                "--channels=2", "--latency-msec=20", env=env)
            capture = Capture(recorder)
            await capture.wait_frames(RATE // 4)
            start_frames, started = capture.frames, time.monotonic()
            await asyncio.sleep(.75)
            elapsed = time.monotonic() - started
            idle_frames = capture.frames - start_frames
            ratio = idle_frames / (elapsed * RATE)
            assert .85 < ratio < 1.15, f"Monitor not continuously clocked: {ratio}"
            assert all(value == 0 for value in capture.raw), "Private idle sink is not silent"
            evidence["clock_check"] = {"wall_seconds": elapsed, "idle_frames": idle_frames, "frame_clock_ratio": ratio}

            endpoint = directory / "mpv.sock"
            player = await spawn("mpv", executables["mpv"], "--no-config", "--load-scripts=no", "--ytdl=no",
                "--no-video", "--idle=yes", "--terminal=no", "--input-default-bindings=no",
                "--ao=pulse", f"--pulse-host={server}", "--audio-device=pulse/orr_transition",
                "--pulse-buffer=50", "--audio-format=float", f"--input-ipc-server={endpoint}", env=env)
            backend = MpvBackend(str(endpoint), auto_spawn=False, initial_volume=100)
            async def prop(name):
                return (await backend.checked_command(["get_property", name]))["data"]
            async def player_ready():
                assert player.returncode is None, (directory / "mpv.log").read_text(errors="replace")
                if not endpoint.is_socket():
                    return False
                await backend.start(timeout=1)
                return await prop("idle-active") is True
            await eventually("native mpv startup", player_ready)
            await backend.initialize_gain()
            evidence["mpv_version"] = await prop("mpv-version")
            evidence["gapless_audio_option"] = await prop("options/gapless-audio")
            evidence["buffer_ms"] = 50

            async def finish_capture(offset, expected_frames=RATE * DURATION, *, label="capture"):
                async def ended():
                    return await prop("idle-active") is True
                await eventually("native playback EOF", ended, timeout=12)
                def diagnostics():
                    return capture_diagnostics(bytes(capture.raw[offset * 8:]), expected_frames=expected_frames)
                before = diagnostics()
                print("CLOCKED_CAPTURE_EOF " + json.dumps({"label": label, **before}, sort_keys=True),
                      file=sys.stderr, flush=True)
                drain_start = time.monotonic()
                # Wait for half a second of independently clocked monitor
                # frames after EOF, then require a quarter second quiet tail.
                # This is capture draining, not artificial source guard audio.
                await capture.wait_frames(capture.frames + RATE // 2)
                async def quiet_tail():
                    raw = bytes(capture.raw[-(RATE // 4) * 8:])
                    return len(raw) == (RATE // 4) * 8 and all(
                        abs(value) <= 1 / 32768 for frame in struct.iter_unpack("<ff", raw) for value in frame)
                await eventually("quiet recorder tail after EOF", quiet_tail, timeout=2)
                after = diagnostics()
                diagnostic = {"label": label, "at_native_eof": before, "after_drain": after,
                              "drain_wall_seconds": time.monotonic() - drain_start}
                # Emit before strict validation so CI failures retain marker
                # shortfalls, edges, weak/ramped samples and drain data. The
                # periodic waveform cannot locate loss of whole periods.
                print("CLOCKED_CAPTURE " + json.dumps(diagnostic, sort_keys=True), file=sys.stderr, flush=True)
                result = measure_capture(bytes(capture.raw[offset * 8:]), expected_frames=expected_frames)
                result["capture_diagnostics"] = diagnostic
                return result

            # Positive control: the analyser must see a known 125 ms silence.
            calibration = directory / "known-125ms-silence.wav"
            write_source(calibration, RATE, 1, calibration=True)
            offset = capture.frames
            await backend.loadfile(str(calibration))
            async def started():
                return await prop("idle-active") is False
            await eventually("calibration started", started)
            evidence["calibration_audio_params"] = await sample_audio_parameters(prop)
            print("CLOCKED_AUDIO_PARAMS " + json.dumps(evidence["calibration_audio_params"], sort_keys=True), file=sys.stderr, flush=True)
            measured = await finish_capture(offset, RATE // 4, label="known_125ms_silence")
            assert abs(measured["gap_frames"] - 6000) <= GAP_TOLERANCE_FRAMES, measured
            assert measured["longest_silence_frames"] >= 6000 - GAP_TOLERANCE_FRAMES, measured
            evidence["known_gap_control"] = measured

            paths = (directory / "a-48000.wav", directory / "b-48000.wav", directory / "b-44100.wav")
            for path, rate, sign in zip(paths, (48000, 48000, 44100), (1, -1, -1)):
                write_source(path, rate, sign)
            # Diagnostic comparison only. This directly queues two native mpv
            # entries; it is not an ORR queue success and cannot certify ORR.
            offset = capture.frames
            await backend.loadfile(str(paths[0]))
            await backend.checked_command(["loadfile", str(paths[1]), "append"])
            await eventually("native playlist started", started)
            measured = await finish_capture(offset, label="native_playlist_comparison")
            evidence["native_playlist_comparison"] = {"control": "direct mpv playlist; bypasses ORR queue", **measured}

            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(socket.SOMAXCONN)
            listener.setblocking(False)
            port = listener.getsockname()[1]
            base = f"http://127.0.0.1:{port}"
            cfg = Config(uuid="00000000-0000-4000-8000-000000000000", advertise_ip="127.0.0.1",
                bind_address="127.0.0.1", http_api_port=port, upnp_port=port, default_volume=100,
                auto_spawn_mpv=False, enable_ssdp=False, enable_mdns=False, state_dir=str(directory / "state"))
            api = HttpApi(cfg, RendererState.from_config(cfg), backend)
            notifications = []
            async def receive_event(request):
                props = {node.tag: node.text or "" for prop_ in ET.fromstring(await request.text()) for node in prop_}
                notifications.append(props)
                return web.Response()
            app = api.app()
            app.router.add_route("NOTIFY", "/_fixture_events", receive_event)
            runner = web.AppRunner(app, shutdown_timeout=1)
            await runner.setup()
            await web.SockSite(runner, listener).start()
            monitor = api._state_monitor_task
            evidence["orr_monitor_interval_seconds"] = api._monitor_interval
            async with ClientSession(timeout=ClientTimeout(total=10), trust_env=False) as session:
                async def command(value):
                    async with session.get(base + "/httpapi.asp", params={"command": value}) as response:
                        body = await response.text()
                        assert response.status == 200, (value, body)
                        return json.loads(body) if response.content_type == "application/json" else body
                async def soap(action, fields="", service="AVTransport"):
                    namespace = "urn:schemas-wiimu-com:service:PlayQueue:1" if service == "PlayQueue" else "urn:schemas-upnp-org:service:AVTransport:1"
                    envelope = f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><u:{action} xmlns:u="{namespace}">{fields}</u:{action}></s:Body></s:Envelope>'
                    async with session.post(base + f"/upnp/control/{service}", data=envelope.encode(), headers={"SOAPACTION": f'"{namespace}#{action}"', "Content-Type": "text/xml"}) as response:
                        body = await response.text()
                        assert response.status == 200, (action, body)
                        return body
                async with session.request("SUBSCRIBE", base + "/upnp/event/PlayQueue", headers={"NT": "upnp:event", "CALLBACK": f"<{base}/_fixture_events>", "TIMEOUT": "Second-60"}) as response:
                    assert response.status == 200
                cases = [
                    ("equal_rate", paths[1], False, False, None),
                    ("mixed_rate", paths[2], False, False, None),
                    ("pause_near_transition", paths[1], True, False, None),
                    ("phase_off_equal", paths[1], False, False, .35),
                    ("phase_off_mixed", paths[2], False, False, .35),
                    ("phase_on_equal", paths[1], False, True, .35),
                    ("phase_on_mixed", paths[2], False, True, .35),
                    ("phase_on_pause", paths[1], True, True, .35),
                ]
                for name, second, pause, staging, start_phase in cases:
                    cfg.native_queue_staging = staging
                    notifications.clear()
                    queue = '<PlayList><QueueName>Clocked pair</QueueName><ListInfo><SourceName>UPnPServer</SourceName><TrackNumber>2</TrackNumber></ListInfo><Tracks>' + ''.join(
                        f'<Track{i}><Title>Marker {i}</Title><URL>{escape(str(path))}</URL><Duration>{DURATION}</Duration></Track{i}>'
                        for i, path in enumerate((paths[0], second), 1)) + '</Tracks></PlayList>'
                    await soap("CreateQueue", f"<QueueContext>{escape(queue)}</QueueContext>", "PlayQueue")
                    if start_phase is not None:
                        # Fix the start offset relative to the unchanged 0.5 s
                        # production monitor. Only fixture scheduling changes;
                        # there is no HTTP status polling across native EOF.
                        old_monitor = api._state_monitor_task
                        old_monitor.cancel()
                        await asyncio.gather(old_monitor, return_exceptions=True)
                        api._state_monitor_task = asyncio.create_task(api._monitor_backend())
                        monitor = api._state_monitor_task
                        await asyncio.sleep(start_phase)
                    offset = capture.frames
                    await soap("PlayQueueWithIndex", "<QueueName>Clocked pair</QueueName><Index>1</Index>", "PlayQueue")
                    if staging:
                        assert api._native_stage is not None and api._native_stage.confirmed
                        assert api.state.plicurr == 1, "Append ACK must not publish the next item"
                    assert await prop("current-ao") == "pulse"
                    assert abs(await prop("duration") - DURATION) < .001
                    assert await prop("audio-out-params/samplerate") == RATE
                    paused = {}
                    if pause:
                        async def near_end():
                            return await prop("playback-time") >= 1.6
                        await eventually("350-400 ms before transition", near_end)
                        await command("setPlayerCmd:pause")
                        position = await prop("playback-time")
                        assert position < DURATION, "Pause did not precede EOF"
                        await asyncio.sleep(.65)
                        assert await prop("pause") is True and api.state.plicurr == 1
                        assert abs(await prop("playback-time") - position) < .05
                        await command("setPlayerCmd:resume")
                        paused = {"position_seconds": position, "hold_seconds": .65, "owner_pause_preserved": True}
                    async def second_playing():
                        # Do not poll HTTP status across EOF: that would change
                        # the production monitor's queue transition latency.
                        return api.state.plicurr == 2 and api.state.status == "play" and await prop("path") == str(second)
                    await eventually("background ORR queue transition", second_playing)
                    status = await command("getPlayerStatus")
                    assert status["plicurr"] == "2" and status["plicount"] == "2", status
                    assert api.state.title == "Marker 2"
                    assert abs(await prop("duration") - DURATION) < .001
                    assert "<Track>2</Track>" in await soap("GetPositionInfo")
                    async def got_event():
                        return any(event.get("CurrentIndex") == "2" and event.get("TrackURI") == str(second) and event.get("TrackTitle") == "Marker 2" for event in notifications)
                    await eventually("delivered second-track GENA metadata", got_event)
                    measured = await finish_capture(offset, label=name)
                    assert all(track["frame_count_delta"] == 0 for track in measured["tracks"]), measured
                    if name == "phase_on_equal":
                        assert measured["outcome"] == "WITHIN_TOLERANCE", measured
                    if pause:
                        assert measured["tracks"][0]["internal_nonsignal_frames"] >= RATE * .4, measured
                    else:
                        assert all(track["internal_nonsignal_frames"] <= GAP_TOLERANCE_FRAMES for track in measured["tracks"]), measured
                    evidence["cases"][name] = {"source_rates": [RATE, 44100 if second == paths[2] else RATE],
                        "queue_index": 2, "duration_seconds": DURATION, "delivered_gena_metadata": True,
                        "native_queue_staging": staging, "start_phase_seconds": start_phase,
                        "pause": paused, **measured}
                    await command("setPlayerCmd:stop")
            evidence["sink_after_cases"] = (await pactl("list", "short", "sinks")).strip()
            assert "48000Hz" in evidence["sink_after_cases"]
        finally:
            try:
                if runner:
                    await asyncio.wait_for(runner.cleanup(), 6)
            finally:
                if listener:
                    listener.close()
                try:
                    if backend:
                        await asyncio.wait_for(backend.stop_process(), 6)
                finally:
                    # Even a production shutdown failure must reap every
                    # fixture-owned native child before surfacing the error.
                    results = await asyncio.gather(*(reap(process) for process in reversed(processes)), return_exceptions=True)
                    if capture:
                        await asyncio.wait_for(capture.task, 3)
                    failures = [result for result in results if isinstance(result, BaseException)]
                    assert not failures, f"Native cleanup failed: {failures!r}"
        await asyncio.sleep(0)
        pending = [task for task in asyncio.all_tasks() - baseline_tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        assert not pending, f"Leaked probe tasks: {pending!r}"
        assert monitor is not None and monitor.done()
        assert all(process.returncode is not None for process in processes)
        evidence["cleanup"] = {"children_reaped": True, "http_monitor_done": True, "no_pending_tasks": True}
    return evidence


if __name__ == "__main__":
    async def main():
        assert sys.platform == "linux"
        with tempfile.TemporaryDirectory(prefix="orr-clock-") as directory:
            try:
                async with asyncio.timeout(80):
                    result = await probe(Path(directory))
            except BaseException:
                for log in sorted(Path(directory).glob("*.log")):
                    print(f"{log.name}:\n{log.read_text(errors='replace')[-12000:]}", file=sys.stderr)
                raise
            print(json.dumps(result, sort_keys=True))
    asyncio.run(main())
