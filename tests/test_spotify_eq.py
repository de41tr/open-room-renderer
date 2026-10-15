"""Narrow opt-in PCM source lifecycle checks; never starts a Spotify client."""
import asyncio
import json
from pathlib import Path

import pytest

from fake_linkplay import spotify_eq as eq
from fake_linkplay.config import Config
from fake_linkplay.http_api import HttpApi
from fake_linkplay.mpv_backend import BackendError
from fake_linkplay.queue import PlaybackQueue, QueueItem
from fake_linkplay.state import RendererState


def profile(tmp_path):
    return {"format": 1, "client_dir": str(tmp_path / "client"), "client_sha256": "a" * 64,
            "name": "Quiet Spotify", "bitrate": 160, "port": 3678, "device_id": "b" * 40}


def test_setup_is_private_nonactivating_and_uses_upstream_zeroconf(tmp_path, monkeypatch):
    monkeypatch.setattr(eq, "verify_client", lambda _: (Path('/not-executed'), {"binary_sha256": "a" * 64}))
    root = tmp_path / "spotify"
    result = eq.setup(str(root), str(tmp_path / "client"), "Quiet Spotify", 160, 3678)
    assert result["activated"] is False
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / 'spotify-eq.json').stat().st_mode & 0o777 == 0o600
    saved = eq._profile(root)
    cfg = eq._config(saved, root / 'synthetic-run')
    assert cfg['audio_backend'] == 'pipe' and 'audio_device' not in cfg
    assert cfg['credentials'] == {'type': 'zeroconf', 'zeroconf': {'persist_credentials': False}}
    assert cfg['initial_volume'] == 0 and cfg['ignore_last_volume'] is True
    assert cfg['server']['address'] == '127.0.0.1'
    before = (root / 'spotify-eq.json').read_bytes()
    with pytest.raises(FileExistsError):
        eq.setup(str(root), str(tmp_path / "client"), "Replacement", 320, 3678)
    assert (root / 'spotify-eq.json').read_bytes() == before


class Backend:
    gain_confirmed = True
    confirmed_dsp = (0.0, [])
    def __init__(self):
        self.path = ''
        self.idle = True
        self.stops = 0
        self.reject_stop = False
    async def start(self): pass
    async def snapshot(self):
        return {'idle-active': self.idle, 'path': self.path,
                'playlist': ([{'filename': self.path, 'id': 7, 'current': True, 'playing': True}]
                             if self.path else [])}
    async def loadfile(self, path, options):
        self.path, self.idle = path, False
        return {'error': 'success'}
    async def stop(self):
        self.stops += 1
        if self.reject_stop:
            raise BackendError('synthetic rejected stop')
        self.path, self.idle = '', True


class Child:
    pid = 123
    returncode = None
    def __init__(self):
        self.terminated = False
        self.release = asyncio.Event()
        self.release.set()
    def terminate(self): self.terminated = True
    def kill(self): self.release.set()
    async def wait(self):
        await self.release.wait()
        self.returncode = -15
        return self.returncode


class Source(eq.SpotifyEqSource):
    async def _spawn(self, binary, directory):
        self.child = Child()
        return self.child
    async def _read_provider(self):
        return {'status': None, 'track': None, 'session_observation': 'NO_SESSION'}


async def started(tmp_path, monkeypatch):
    root = tmp_path / 'profile'
    root.mkdir(mode=0o700)
    p = profile(tmp_path)
    monkeypatch.setattr(eq, 'verify_client', lambda _: (Path('/not-executed'), {'binary_sha256': p['client_sha256']}))
    cfg = Config(state_dir=str(tmp_path / 'state'), uuid='00000000-0000-4000-8000-000000000000')
    backend = Backend()
    api = HttpApi(cfg, RendererState.from_config(cfg), backend)
    source = Source(root, p)
    await source.start(api)
    return source, api, backend


@pytest.mark.asyncio
async def test_failed_reader_stop_reaps_producer_and_blocks_replacement(tmp_path, monkeypatch):
    source, api, backend = await started(tmp_path, monkeypatch)
    runtime = source.runtime
    backend.reject_stop = True
    try:
        with pytest.raises(BackendError, match='stop unconfirmed'):
            await api._replace_queue(PlaybackQueue(items=[QueueItem('https://example.invalid/own.wav')], index=1))
        assert source.child.terminated and source.child.returncode is not None
        assert source.active and source.failed and api.spotify_source is source
        assert api.state.current_url == eq.SOURCE_URL and runtime.exists()
        with pytest.raises(BackendError):
            await api._replace_queue(PlaybackQueue(items=[QueueItem('https://example.invalid/new.wav')], index=1))
        assert backend.stops == 1  # Unknown stop is not an automatic retry/new start.
    finally:
        # Fixture-only cleanup after explicitly repairing its simulated backend.
        backend.reject_stop = False
        source._stop_task = None
        await source.close()


@pytest.mark.asyncio
async def test_cancelled_stop_waiter_cannot_release_new_owner_early(tmp_path, monkeypatch):
    source, api, backend = await started(tmp_path, monkeypatch)
    runtime = source.runtime
    source.child.release.clear()
    waiter = asyncio.create_task(source.close())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert api.spotify_source is source and source.active and backend.stops == 0
    queued = PlaybackQueue(items=[QueueItem('https://example.invalid/own.wav')], index=1)
    replacement = asyncio.create_task(api._replace_queue(queued))
    await asyncio.sleep(0)
    assert not replacement.done() and api.state.current_url == eq.SOURCE_URL
    source.child.release.set()
    await replacement
    assert source.child.returncode is not None and backend.stops == 1
    assert not runtime.exists() and source.keepalive is None
    assert api.spotify_source is None and api.state.queue is queued
    with pytest.raises(BackendError, match='internally owned'):
        await api._play_url(eq.SOURCE_URL)


@pytest.mark.parametrize('value', ['', 'relative', '/a/../b', '/a//b', '/a/\n', 3, True])
def test_profile_setting_rejects_nonabsolute_or_ambiguous_values(value):
    assert Config().spotify_eq_profile is None
    with pytest.raises(ValueError):
        Config(spotify_eq_profile=value)


@pytest.mark.asyncio
async def test_existing_listener_collision_precedes_state_or_backend(tmp_path, monkeypatch):
    import socket
    import yaml
    from fake_linkplay import cli
    from fake_linkplay.config import StartupConfigError
    root = tmp_path / 'spotify'; root.mkdir(mode=0o700)
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    p = profile(tmp_path)
    (root / 'spotify-eq.json').write_text(json.dumps(p))
    monkeypatch.setattr(eq, 'verify_client', lambda _: (Path('/not-executed'), {'binary_sha256': p['client_sha256']}))
    with socket.socket() as existing:
        existing.bind(('127.0.0.1', 0)); existing.listen()
        port = existing.getsockname()[1]
        path = tmp_path / 'config.yaml'
        path.write_text(yaml.safe_dump({'spotify_eq_profile': str(root),
            'state_dir': str(tmp_path / 'absent-state'), 'mpv_socket': str(run / 'mpv.sock'),
            'mpv_audio_device': 'alsa/plughw:CARD=Synthetic,DEV=0',
            'bind_address': '127.0.0.1', 'http_api_port': port, 'upnp_port': port}))
        monkeypatch.setattr(cli, 'MpvBackend', lambda *a, **k: pytest.fail('backend must not be touched'))
        with pytest.raises(StartupConfigError, match='preflight failed'):
            await cli.serve(str(path))
        assert not (tmp_path / 'absent-state').exists()
        assert not (root / 'run.lock').exists()
        assert existing.fileno() >= 0


@pytest.mark.asyncio
async def test_config_opt_in_uses_same_service_and_holds_listeners(tmp_path, monkeypatch):
    import socket
    import yaml
    from fake_linkplay import cli
    root = tmp_path / 'spotify'; root.mkdir(mode=0o700)
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    p = profile(tmp_path)
    (root / 'spotify-eq.json').write_text(json.dumps(p))
    monkeypatch.setattr(eq, 'verify_client', lambda _: (Path('/not-executed'), {'binary_sha256': p['client_sha256']}))
    with socket.socket() as unused:
        unused.bind(('127.0.0.1', 0)); port = unused.getsockname()[1]
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump({'spotify_eq_profile': str(root),
        'state_dir': str(tmp_path / 'state'), 'mpv_socket': str(run / 'mpv.sock'),
        'mpv_audio_device': 'alsa/plughw:CARD=Synthetic,DEV=0',
        'bind_address': '127.0.0.1', 'http_api_port': port, 'upnp_port': port}))
    before = path.read_bytes()
    called = []
    async def service(cfg, *, spotify_source, listener_sockets):
        called.append(spotify_source)
        assert cfg.state_dir == str(tmp_path / 'state')
        assert spotify_source.root == root and not spotify_source.active
        assert set(listener_sockets) == {port}
        with socket.socket() as collision:
            collision.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(OSError): collision.bind(('127.0.0.1', port))
    monkeypatch.setattr(cli, '_serve', service)
    await cli.serve(str(path))
    assert len(called) == 1 and path.read_bytes() == before
    with socket.socket() as released:
        released.bind(('127.0.0.1', port))


@pytest.mark.asyncio
async def test_stop_interrupts_own_producer_before_waiting_for_control_lock(tmp_path, monkeypatch):
    source, api, backend = await started(tmp_path, monkeypatch)
    await source._lock.acquire()  # A bounded provider request is still in flight.
    waiter = asyncio.create_task(source.close())
    await asyncio.sleep(0)
    assert source.child.terminated and source.stopping
    for _ in range(10):
        await asyncio.sleep(0)
        if source.child.returncode is not None:
            break
    assert source.child.returncode is not None  # Reaped before the lock is free.
    assert api.spotify_source is source and source.active and backend.stops == 0
    source._lock.release()
    await waiter
    assert source.child.returncode is not None and backend.stops == 1
    assert not source.active and api.spotify_source is None


@pytest.mark.asyncio
@pytest.mark.parametrize('superseded', [False, 'before_resume', 'during_gain'])
async def test_spotify_app_resume_releases_only_current_paused_reader(tmp_path, monkeypatch, superseded):
    from unittest.mock import AsyncMock
    source, api, backend = await started(tmp_path, monkeypatch)
    native_snapshot = backend.snapshot
    paused = True
    async def snapshot():
        return {**await native_snapshot(), 'pause': paused, 'core-idle': paused,
                'audio-params': {'samplerate': 44100}, 'paused-for-cache': False}
    async def resume(*, current):
        nonlocal paused
        if superseded == 'during_gain':
            api._accept_playback_command('pause')
        if not await current():
            raise BackendError('synthetic current guard rejected stale resume')
        paused = False
        return {'error': 'success'}
    async def provider():
        if superseded == 'before_resume':
            api._accept_playback_command('pause')
        return {'status': {'paused': False, 'stopped': False, 'buffering': False}, 'track': None}
    backend.snapshot = snapshot
    backend.resume = AsyncMock(side_effect=resume)
    source._read_provider = provider
    try:
        await source.observe()
        if superseded:
            if superseded == 'before_resume':
                backend.resume.assert_not_awaited()
            else:
                backend.resume.assert_awaited_once()
            assert paused and not source.failed
        else:
            backend.resume.assert_awaited_once()
            assert not paused and api.state.gain_confirmed
    finally:
        await source.close()
