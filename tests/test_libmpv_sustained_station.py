"""Opt-in uninterrupted station soak; default tests are native-independent.

ORR_TEST_LIBMPV_SUSTAINED=1 ORR_TEST_LIBMPV_PATH=/path/to/libmpv.so.2 \
    PYTHONPATH=. python -m pytest -q -s tests/test_libmpv_sustained_station.py

Default smoke baseline is 10 seconds. Set ORR_TEST_LIBMPV_SUSTAINED_SECONDS=3600
for one full uninterrupted hour after warmup. Explicit opt-in fails if Linux,
libmpv, or FFmpeg is missing. Run files remain in pytest's temporary directory;
--basetemp can preserve aggregate progress/evidence outside the repository.
Sources are copied/hashed and a process-group supervisor enforces an outer
seconds+90 deadline including native C calls, with bounded teardown.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import locale
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from test_libmpv_stability import freeze_sources, source_digest
from tools.native_probe_supervisor import run_probe


def freeze_sustained_sources(source: Path, target: Path) -> str:
    freeze_sources(source, target)
    for name in ('tests/test_libmpv_sustained_station.py', 'tests/sustained_station_support.py',
                 'tools/native_probe_supervisor.py'):
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((source / name).read_bytes())
    manifest = {path.relative_to(target).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(target.rglob('*.py'))}
    (target / 'snapshot-manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    return source_digest(target)


def test_sustained_adts_rejects_invalid_or_unbounded_media() -> None:
    from sustained_station_support import adts_frames
    for data in (b'', b'not AAC', b'\0' * 65537, b'\xff\xf1\x60\x40\x01\x3f\xfcX'):
        with pytest.raises(AssertionError):
            adts_frames(data)


def test_sustained_adts_fixed_frame_duration() -> None:
    from sustained_station_support import FRAME_SECONDS, adts_frames
    # Parser-only synthetic frame, not playable media. Native tests exclusively
    # use FFmpeg's actual encoder, never this structural unit fixture.
    frame = b'\xff\xf1\x60\x40\x01\x1f\xfcX'
    assert adts_frames(frame * 16) == (frame,) * 16
    assert FRAME_SECONDS == .064
    for index, mask in ((1, 1), (2, 4), (3, 64), (6, 1)):
        invalid = bytearray(frame * 16)
        invalid[index] ^= mask
        with pytest.raises(AssertionError):
            adts_frames(bytes(invalid))


def test_sustained_hms_position() -> None:
    from sustained_station_support import hms_seconds
    assert hms_seconds('01:00:05') == 3605


def test_sustained_position_compares_observations_not_quantized_lag() -> None:
    from fake_linkplay.http_api import HttpApi
    from types import SimpleNamespace
    from sustained_station_support import validate_observed_position
    api = HttpApi.__new__(HttpApi)
    api.state, api._playback_offset_sec = SimpleNamespace(curpos=0), 0
    observation = {'raw_position': 249.999,
                   'published_position': api._adjusted_playback_time({'playback-time': 249.999}),
                   'read_started': 100.0, 'read_completed': 100.002, 'applied_at': 100.002}
    # Actual production quantization can correctly exceed the old 1.5s raw
    # lag bound after one normal monitor interval plus 2ms of snapshot work.
    assert abs(observation['published_position'] - 250.501) > 1.5
    assert validate_observed_position(observation, now=100.502, native=250.501, monitor_interval=.5) == pytest.approx(.502)
    mutations = ({'published_position': 250}, {'read_started': 99.0}, {'raw_position': 240.0},
                 {'read_completed': 100.6, 'applied_at': 100.6}, {'raw_position': float('nan')})
    for mutation in mutations:
        with pytest.raises(AssertionError):
            validate_observed_position({**observation, **mutation}, now=100.7, native=250.699, monitor_interval=.5)
    with pytest.raises(AssertionError, match='Stale monitor'):
        validate_observed_position(observation, now=101.01, native=251.009, monitor_interval=.5)
    for native, updates in ((250.501, {'raw_position': 240.0, 'published_position': 240}),
                            (249.5, {})):
        with pytest.raises(AssertionError, match='Stale/jumped native'):
            validate_observed_position({**observation, **updates}, now=100.502,
                                       native=native, monitor_interval=.5)


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux /proc sensitivity check')
@pytest.mark.asyncio
async def test_sustained_resources_detect_actual_child() -> None:
    from sustained_station_support import resources
    from libmpv_stability_support import LIVE_BUDGET, within_budget
    before = resources()
    assert before['children'] == 0
    child = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(30)',
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        sample = resources()
        assert sample['children'] == 1
        with pytest.raises(AssertionError, match='child-process'):
            within_budget(sample, before, LIVE_BUDGET)
    finally:
        if child.returncode is None:
            child.terminate()
        await asyncio.wait_for(child.wait(), 3)
    assert resources()['children'] == 0
    assert not Path(f'/proc/{child.pid}').exists()


@pytest.mark.skipif(os.environ.get('ORR_TEST_LIBMPV_SUSTAINED') != '1',
                    reason='Requires explicit ORR_TEST_LIBMPV_SUSTAINED=1')
def test_real_libmpv_uninterrupted_station(tmp_path: Path) -> None:
    assert sys.platform == 'linux' and Path('/proc/self/status').is_file(), 'Sustained lane requires Linux /proc'
    value = os.environ.get('ORR_TEST_LIBMPV_PATH', '')
    assert value and Path(value).is_file(), 'Explicit sustained lane requires a real ORR_TEST_LIBMPV_PATH'
    library = Path(value).resolve()
    ffmpeg = shutil.which('ffmpeg')
    assert ffmpeg, 'Explicit sustained lane requires FFmpeg with AAC encoding'
    seconds = int(os.environ.get('ORR_TEST_LIBMPV_SUSTAINED_SECONDS', '10'))
    assert 5 <= seconds <= 3600, 'Sustained baseline must be 5..3600 seconds'
    snapshot, media = tmp_path / 'snapshot', tmp_path / 'media'
    media.mkdir()
    digest = freeze_sustained_sources(Path(__file__).resolve().parents[1], snapshot)
    env = os.environ.copy()
    env['PYTHONPATH'] = str(snapshot) + os.pathsep + str(snapshot / 'tests')
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    for name in tuple(env):
        if name.lower() in {'http_proxy', 'https_proxy', 'all_proxy'}:
            env.pop(name)
    env['NO_PROXY'] = env['no_proxy'] = '127.0.0.1,localhost'
    generation_env = env.copy()
    # A supplied old libmpv prefix must not override the system FFmpeg's own ABI.
    generation_env.pop('LD_LIBRARY_PATH', None)
    generation = run_probe([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=16000', '-af', 'volume=0.25',
        '-t', '2', '-ac', '1', '-c:a', 'aac', '-b:a', '32k', '-f', 'adts', str(media / 'quiet.aac')],
        cwd=snapshot, env=generation_env, timeout=15)
    assert generation.returncode == 0, 'Synthetic FFmpeg AAC generation failed: ' + generation.stderr
    version = subprocess.run([ffmpeg, '-version'], capture_output=True, text=True, timeout=5,
                             env=generation_env, check=True).stdout.splitlines()[0]
    completed = run_probe([sys.executable, str(snapshot / 'tests/test_libmpv_sustained_station.py'), '--probe',
        str(library), str(media), str(seconds), digest], cwd=snapshot, env=env, timeout=seconds + 90)
    assert completed.returncode == 0, f'Sustained native station failed\n{completed.stdout}\n{completed.stderr}'
    result = json.loads(completed.stdout)
    assert result['source_sha256'] == digest == source_digest(snapshot)
    assert result['baseline_elapsed_seconds'] >= seconds
    assert result['native_progress_seconds'] >= seconds - 2
    assert result['loads'] == result['source_changes'] == result['station_requests'] == 1
    assert result['http_disconnects'] == 0 and result['samples'] >= seconds / 3
    result['ffmpeg_version'] = version
    result['libmpv_sha256'] = hashlib.sha256(library.read_bytes()).hexdigest()
    result['production_sha256'] = source_digest(snapshot / 'fake_linkplay')
    (media / 'evidence.json').write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    assert len(sys.argv) == 6 and sys.argv[1] == '--probe'
    locale.setlocale(locale.LC_NUMERIC, 'C')
    from sustained_station_support import exercise

    async def main() -> None:
        root = Path(__file__).resolve().parents[1]
        import fake_linkplay.http_api as production
        assert Path(production.__file__).resolve().is_relative_to(root)
        assert source_digest(root) == sys.argv[5]
        async with asyncio.timeout(int(sys.argv[4]) + 60):
            result = await exercise(sys.argv[2], Path(sys.argv[3]), int(sys.argv[4]), sys.argv[5])
        assert source_digest(root) == sys.argv[5]
        print(json.dumps(result, sort_keys=True), flush=True)

    asyncio.run(main())
