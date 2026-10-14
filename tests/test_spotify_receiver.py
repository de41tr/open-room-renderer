"""Small discriminating safety contracts; synthetic peers, no account/device."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat

import pytest
from aiohttp import web

from fake_linkplay import cli, spotify_client as client, spotify_receiver as receiver
from fake_linkplay import spotify_exclusive as exclusive
from fake_linkplay.spotify_exclusive import ExclusiveProfileError
from test_spotify_exclusive import profile  # existing canonical native fixture
from test_spotify_diagnostic import peer, STATUS, SECRET


@pytest.fixture
def ready(profile, monkeypatch):
    root = Path(profile['output_dir'])
    binary = Path(profile['go_librespot_binary'])
    receipt = {'version': 'v0.10.3', 'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest()}
    monkeypatch.setattr(receiver, '_check_mpv', lambda: None)
    monkeypatch.setattr(receiver, 'verify_client', lambda _path: (binary, receipt))
    args = argparse.Namespace(directory=str(root), renderer_prefix=profile['renderer_prefix'],
        renderer_config=profile['renderer_config'], renderer_unit=profile['renderer_unit'],
        client_dir=str(binary.parent / 'client'), host='127.0.0.1', port=3678)
    receiver.setup(args)
    return root


def stopped():
    return {key: value for key, value in {'ActiveState': 'inactive', 'SubState': 'dead', 'MainPID': '0',
        'ControlPID': '0', 'Job': '', 'ControlGroup': '', 'Result': 'success', 'InvocationID': '1' * 32}.items()}


class Services:
    calls = []
    failure = None
    stopped = receiver.UserServices.stopped
    def __init__(self, root, profile):
        self.root, self.profile = root, profile
    def validate(self):
        return {mode: stopped() | ({'ActiveState': 'active', 'SubState': 'running', 'MainPID': '42'}
                                 if self.calls and self.calls[-1] == ('started', mode) else {})
                for mode in receiver.UNITS}
    def cgroup_empty(self, group):
        return True
    def stop_all(self, before):
        self.calls.append(('stop_confirmed',))
        if self.failure == 'stop':
            raise ExclusiveProfileError('Unknown stop.')
    def command(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] == 'start' and self.failure == 'start':
            raise ExclusiveProfileError('Unknown start.')
    def wait_started(self, mode):
        self.calls.append(('started', mode))
        return '1' * 32, '42'


@pytest.fixture
def services(monkeypatch):
    Services.calls = []
    Services.failure = None
    monkeypatch.setattr(receiver, 'UserServices', Services)
    monkeypatch.setattr(receiver, 'probe_alsa', lambda _route: Services.calls.append(('probe',)))
    return Services


def test_setup_private_and_independent_renderer_return(ready, monkeypatch):
    for path in [ready, *ready.rglob('*')]:
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)
    (ready / 'sidecar/config.yml').unlink()
    monkeypatch.setattr(receiver, 'verify_client', lambda _path: (_ for _ in ()).throw(ExclusiveProfileError('Broken Spotify')))
    receiver.load_profile(ready, start='renderer')
    receiver.load_profile(ready)  # stop/status never need intact Spotify state
    with pytest.raises(ExclusiveProfileError, match='Broken Spotify'):
        receiver.load_profile(ready, start='spotify')


def test_setup_name_round_trips_without_changing_other_sidecar_settings(ready, profile):
    name = 'Studio: "北" 100%'
    root = ready.parent / 'named'
    args = cli.build_parser().parse_args([
        'spotify', 'receiver', '--directory', str(root), 'setup', '--name', name,
        '--renderer-prefix', profile['renderer_prefix'], '--renderer-config', profile['renderer_config'],
        '--renderer-unit', profile['renderer_unit'], '--client-dir', str(ready.parent / 'client'),
        '--port', '3678'])
    receiver.setup(args)
    _, saved = receiver.load_profile(root, start='spotify')
    assert saved['device_name'] == name
    sidecar = receiver._json(root / 'sidecar/config.yml')
    expected = receiver._json(ready / 'sidecar/config.yml')
    expected['device_name'] = name
    expected['cache']['dir'] = str(root / 'sidecar/cache')
    assert sidecar == expected  # No device ID, account or unrelated option changes.
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    args.name = 'A different receiver'
    with pytest.raises(FileExistsError):
        receiver.setup(args)
    assert all(p.read_bytes() == data for p, data in before.items())
    sidecar['device_name'] = args.name
    receiver._write(root / 'sidecar/config.yml', sidecar)
    with pytest.raises(ExclusiveProfileError, match='sidecar configuration changed'):
        receiver.load_profile(root, start='spotify')


def test_setup_name_default_legacy_profile_and_offline_config_bytes(ready, profile):
    saved = receiver._json(ready / 'receiver.json')
    assert 'preferred_bitrate' not in saved
    assert 'bitrate' not in receiver._json(ready / 'sidecar/config.yml')
    assert saved.pop('device_name') == 'Open Room Renderer Spotify'
    # Recreate pre-name metadata; loading it must not migrate or rewrite it.
    receiver._write(ready / 'receiver.json', saved)
    before = {p: p.read_bytes() for p in ready.rglob('*') if p.is_file()}
    receiver.load_profile(ready, start='spotify')
    assert all(p.read_bytes() == data for p, data in before.items())
    normalized = before[ready / 'sidecar/config.yml'].replace(str(ready).encode(), b'/synthetic/receiver')
    # Exact config bytes from the pre-name implementation at 1730713f, with only
    # the fixture's variable output directory replaced by /synthetic/receiver.
    assert hashlib.sha256(normalized).hexdigest() == 'ce6742081c485bc82ae19072b5e0ea17115326bc0a6fffa64be095c62b8b8bf1'
    output = ready.parent / 'offline'
    exclusive.prepare_exclusive(**(profile | {'output_dir': str(output)}))
    normalized = (output / 'sidecar/config.yml').read_bytes().replace(str(output).encode(), b'/synthetic/receiver')
    assert hashlib.sha256(normalized).hexdigest() == '9b26902c934028c6b94ea9c266d8652d82174e633be2ea56b54a5aa195b9bb54'


def test_setup_bitrate_preferences_map_exactly_for_start_and_current_controls(ready, profile, services, monkeypatch):
    from fake_linkplay import spotify_playback as playback
    calls = []
    monkeypatch.setattr(playback, 'control', lambda *args: calls.append(args[:5]) or {'command': args[3]})
    for bitrate in (96, 160, 320):
        root = ready.parent / f'b{bitrate}'
        args = cli.build_parser().parse_args([
            'spotify', 'receiver', '--directory', str(root), 'setup', '--bitrate', str(bitrate),
            '--renderer-prefix', profile['renderer_prefix'], '--renderer-config', profile['renderer_config'],
            '--renderer-unit', profile['renderer_unit'], '--client-dir', str(ready.parent / 'client'),
            '--port', '3678'])
        receiver.setup(args)
        _, saved = receiver.load_profile(root, start='spotify')
        assert type(saved['preferred_bitrate']) is int and saved['preferred_bitrate'] == bitrate
        actual = receiver._json(root / 'sidecar/config.yml')
        expected = receiver._json(ready / 'sidecar/config.yml') | {'bitrate': bitrate}
        expected['cache']['dir'] = str(root / 'sidecar/cache')
        assert actual == expected and type(actual['bitrate']) is int
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in (root / 'receiver.json', root / 'sidecar/config.yml'))
        before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
        services.calls = [('started', 'spotify')]
        assert receiver.playback_control(str(root), 'pause')['command'] == 'pause'
        assert all(p.read_bytes() == data for p, data in before.items())
        args.bitrate = 96 if bitrate == 320 else 320
        with pytest.raises(FileExistsError):
            receiver.setup(args)
        assert all(p.read_bytes() == data for p, data in before.items())
    assert calls == [('127.0.0.1', 3678, '42', 'pause', None)] * 3


def test_setup_bitrate_rejects_invalid_values_privately_without_blocking_return(ready, monkeypatch, capsys):
    for value in ('128', '320.0', SECRET, 'lossless'):
        with pytest.raises(SystemExit) as exc:
            cli.main(['spotify', 'receiver', '--directory', str(ready.parent / 'invalid'),
                      'setup', '--bitrate', value])
        assert exc.value.code == 2
        output = capsys.readouterr()
        assert not output.out and value not in output.err
    saved = receiver._json(ready / 'receiver.json')
    monkeypatch.setattr(receiver, 'UserServices', lambda *_args: pytest.fail('invalid preference reached services'))
    for value in (True, 320.0, '320', 128, [], {}):
        root = ready.parent / 'invalid'
        args = argparse.Namespace(spotify_cmd='receiver', receiver_cmd='setup', directory=str(root), bitrate=value)
        assert receiver.run_receiver(args) == 1
        output = capsys.readouterr()
        assert output.out == '' and output.err == 'Receiver: Use a preferred Spotify bitrate of 96, 160 or 320 kbps.\n'
        assert not root.exists()
    for value in (None, True, 320.0, '320', 128, [], {}):
        receiver._write(ready / 'receiver.json', saved | {'preferred_bitrate': value})
        with pytest.raises(ExclusiveProfileError, match='preferred Spotify bitrate'):
            receiver.load_profile(ready, start='spotify')
        with pytest.raises(ExclusiveProfileError, match='preferred Spotify bitrate'):
            receiver.playback_control(str(ready), 'pause')
        receiver.load_profile(ready)  # Stop/off remains independent of Spotify preferences.
        receiver.load_profile(ready, start='renderer')


def test_bitrate_sidecar_must_match_stored_integer_preference_for_start_and_controls(ready, monkeypatch):
    saved = receiver._json(ready / 'receiver.json')
    original = receiver._json(ready / 'sidecar/config.yml')
    receiver._write(ready / 'receiver.json', saved | {'preferred_bitrate': 320})
    receiver._write(ready / 'sidecar/config.yml', original | {'bitrate': 320})
    receiver.load_profile(ready, start='spotify')
    monkeypatch.setattr(receiver, 'UserServices', lambda *_args: pytest.fail('altered sidecar reached services'))
    altered = [original, *(original | {'bitrate': v} for v in (160, 320.0, '320', None, True)),
               original | {'bitrate': 320, 'flac_enabled': True}]
    for actual in altered:
        receiver._write(ready / 'sidecar/config.yml', actual)
        with pytest.raises(ExclusiveProfileError, match='sidecar configuration changed'):
            receiver.load_profile(ready, start='spotify')
        with pytest.raises(ExclusiveProfileError, match='sidecar configuration changed'):
            receiver.playback_control(str(ready), 'pause')
    # Legacy omission is exact too: even explicitly adding 160 needs a stored preference.
    receiver._write(ready / 'receiver.json', saved)
    receiver._write(ready / 'sidecar/config.yml', original | {'bitrate': 160})
    with pytest.raises(ExclusiveProfileError, match='sidecar configuration changed'):
        receiver.load_profile(ready, start='spotify')


def test_setup_name_rejects_invalid_input_privately_before_writes(ready, capsys):
    invalid = ('', ' edge', 'edge ', 'private-name\nline', 'private-name\x00',
               'private-name\u202e', '\ud800', 'x' * 129, None, 42)
    for name in invalid:
        root = ready.parent / 'invalid-name'
        args = argparse.Namespace(spotify_cmd='receiver', receiver_cmd='setup', directory=str(root), name=name)
        assert receiver.run_receiver(args) == 1
        out = capsys.readouterr()
        assert out.out == ''
        assert out.err == ('Receiver: Use a printable Spotify device name of 1 to 128 characters '
                           'without surrounding whitespace.\n')
        assert not root.exists()
    # The same validation protects a stored name before start, but not stop.
    saved = receiver._json(ready / 'receiver.json')
    saved['device_name'] = invalid[3]
    receiver._write(ready / 'receiver.json', saved)
    with pytest.raises(ExclusiveProfileError, match='printable Spotify device name'):
        receiver.load_profile(ready, start='spotify')
    receiver.load_profile(ready)
    receiver.load_profile(ready, start='renderer')
    assert receiver._device_name('北' * 128) == '北' * 128


def test_switch_confirms_whole_stop_and_probe_before_one_start(ready, services):
    result = receiver.switch(str(ready), 'spotify')
    assert services.calls == [('stop_confirmed',), ('probe',), ('start', receiver.SPOTIFY_SERVICE), ('started', 'spotify')]
    assert result['service_state'] == 'PROCESS_STARTED' and result['audio_output'] == 'UNVERIFIED'


@pytest.mark.parametrize('damage', ['client', 'config'])
def test_invalid_target_preserves_healthy_renderer(ready, services, monkeypatch, damage):
    services.calls.append(('started', 'renderer'))
    previous = {'requested_mode': 'renderer', 'service_state': 'PROCESS_STARTED'}
    receiver._write(ready / 'last-operation.json', previous)
    if damage == 'client':
        monkeypatch.setattr(receiver, 'verify_client', lambda *_args: receiver.fail('Broken Spotify'))
    else:
        (ready / 'sidecar/config.yml').write_text('{}')
    with pytest.raises(ExclusiveProfileError):
        receiver.switch(str(ready), 'spotify')
    assert services.calls == [('started', 'renderer')]
    assert receiver._json(ready / 'last-operation.json') == previous


@pytest.mark.parametrize('source', ['spotify', 'off'])
def test_existing_renderer_socket_preserves_known_source(ready, services, source):
    if source == 'spotify':
        services.calls.append(('started', source))
    calls = services.calls.copy()
    receiver._write(ready / 'last-operation.json', {'requested_mode': source})
    receipt = ready / 'last-operation.json'
    previous = receipt.read_bytes()
    _, profile = receiver.load_profile(ready)
    path = Path(profile['renderer_prefix']) / 'run/mpv.sock'
    # A real socket inode, without opening a listener or using any audio device.
    os.mknod(path, stat.S_IFSOCK | 0o600)
    inode = path.stat().st_ino
    with pytest.raises(ExclusiveProfileError, match='existing mpv socket cannot be adopted'):
        receiver.switch(str(ready), 'renderer')
    assert services.calls == calls
    assert receipt.read_bytes() == previous
    assert path.is_socket() and path.stat().st_ino == inode


def test_renderer_restart_does_not_preflight_its_current_socket(ready, services):
    services.calls.append(('started', 'renderer'))
    _, profile = receiver.load_profile(ready)
    path = Path(profile['renderer_prefix']) / 'run/mpv.sock'
    os.mknod(path, stat.S_IFSOCK | 0o600)
    # The fixture handles the restart; this checks admission before stop only.
    assert receiver.switch(str(ready), 'renderer')['requested_mode'] == 'renderer'
    assert services.calls == [('started', 'renderer'), ('stop_confirmed',), ('probe',),
                              ('start', receiver.RENDERER_SERVICE), ('started', 'renderer')]
    assert path.is_socket()


@pytest.mark.parametrize('source', ['healthy', 'conflicting'])
def test_renderer_socket_guard_still_rejects_after_stop(ready, services, monkeypatch, source):
    services.calls.append(('started', 'spotify'))
    _, profile = receiver.load_profile(ready)
    path = Path(profile['renderer_prefix']) / 'run/mpv.sock'
    if source == 'conflicting':
        os.mknod(path, stat.S_IFSOCK | 0o600)
    stop_all = services.stop_all
    def stop(self, before):
        stop_all(self, before)
        if source == 'healthy':
            os.mknod(path, stat.S_IFSOCK | 0o600)  # Appeared after preflight.
    monkeypatch.setattr(services, 'stop_all', stop)
    group = '/synthetic/' + receiver.RENDERER_SERVICE
    validate = services.validate
    def states(self):
        result = validate(self)
        if self.calls[-1] == ('started', 'renderer'):
            result['renderer'].update(MainPID=str(os.getpid()), ControlGroup=group)
        elif source == 'conflicting' and self.calls[-1] == ('started', 'spotify'):
            result['renderer'] = result['spotify'].copy()
        return result
    monkeypatch.setattr(services, 'validate', states)
    original = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda path, *args, **kwargs:
                        '0::' + group if str(path) == '/proc/self/cgroup' else original(path, *args, **kwargs))
    def guarded_start(self, mode):
        self.calls.append(('started', mode))
        receiver.run_guard(str(self.root), mode)
        pytest.fail('The existing socket passed the renderer guard')
    monkeypatch.setattr(services, 'wait_started', guarded_start)
    monkeypatch.setattr(receiver.os, 'execv', lambda *_args: pytest.fail('Native exec attempted'))
    with pytest.raises(ExclusiveProfileError, match='existing mpv socket cannot be adopted'):
        receiver.switch(str(ready), 'renderer')
    assert services.calls == [('started', 'spotify'), ('stop_confirmed',), ('probe',),
                              ('start', receiver.RENDERER_SERVICE), ('started', 'renderer'),
                              ('probe',), ('stop', *receiver.UNITS.values())]
    assert receiver._json(ready / 'last-operation.json')['service_state'] == 'UNKNOWN'
    assert path.is_socket()


def test_target_change_after_stop_still_blocks_start_and_cleans_up(ready, services, monkeypatch):
    services.calls.append(('started', 'renderer'))
    stop_all = services.stop_all
    def change_after_stop(self, before):
        stop_all(self, before)
        (ready / 'sidecar/config.yml').write_text('{}')
    monkeypatch.setattr(services, 'stop_all', change_after_stop)
    with pytest.raises(ExclusiveProfileError, match='sidecar configuration changed'):
        receiver.switch(str(ready), 'spotify')
    assert services.calls == [('started', 'renderer'), ('stop_confirmed',), ('stop', *receiver.UNITS.values())]
    assert receiver._json(ready / 'last-operation.json')['service_state'] == 'UNKNOWN'


@pytest.mark.parametrize('unsafe', ['conflict', 'pending-job', 'saved-cgroup', 'malformed-saved-cgroup'])
def test_invalid_target_keeps_stop_path_for_unproven_source(ready, services, monkeypatch, unsafe):
    services.calls.append(('started', 'renderer'))
    (ready / 'sidecar/config.yml').write_text('{}')
    states = services(ready, {}).validate()
    if unsafe == 'conflict':
        states['spotify'] = states['renderer'].copy()
    elif unsafe == 'pending-job':
        states['renderer']['Job'] = '17'
    elif unsafe == 'saved-cgroup':
        receiver._write(ready / 'last-cgroups.json', {'renderer': '', 'spotify': 'old-descendant'})
        monkeypatch.setattr(services, 'cgroup_empty', lambda _self, group: group != 'old-descendant')
    else:
        (ready / 'last-cgroups.json').write_text('{')
    monkeypatch.setattr(services, 'validate', lambda _self: states)
    with pytest.raises(ExclusiveProfileError, match='sidecar configuration changed'):
        receiver.switch(str(ready), 'spotify')
    assert services.calls == [('started', 'renderer'), ('stop_confirmed',), ('stop', *receiver.UNITS.values())]
    assert receiver._json(ready / 'last-operation.json')['service_state'] == 'UNKNOWN'


def test_unverified_units_never_enter_switch_transition(ready, services, monkeypatch):
    services.calls.append(('started', 'renderer'))
    monkeypatch.setattr(services, 'validate', lambda _self: receiver.fail('Unverified units'))
    with pytest.raises(ExclusiveProfileError, match='Unverified units'):
        receiver.switch(str(ready), 'spotify')
    assert services.calls == [('started', 'renderer')]
    assert not (ready / 'last-operation.json').exists()


def test_unknown_stop_never_starts_and_metadata_failure_cannot_skip_cleanup(ready, services, monkeypatch):
    services.failure = 'stop'
    monkeypatch.setattr(receiver, '_write', lambda *_args: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(ExclusiveProfileError):
        receiver.switch(str(ready), 'spotify')
    assert not any(call[0] == 'start' for call in services.calls)
    assert services.calls[-1] == ('stop', *receiver.UNITS.values())


def test_uncertain_start_is_stopped_even_when_error_receipt_cannot_be_written(ready, services, monkeypatch):
    services.failure = 'start'
    monkeypatch.setattr(receiver, '_write', lambda *_args: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(ExclusiveProfileError):
        receiver.switch(str(ready), 'spotify')
    assert services.calls[-1] == ('stop', *receiver.UNITS.values())
    assert len([call for call in services.calls if call[0] == 'start']) == 1


@pytest.mark.parametrize('mode', ['renderer', 'off'])
def test_return_or_off_needs_no_healthy_spotify(ready, services, monkeypatch, mode):
    (ready / 'sidecar/config.yml').write_text('damaged')
    monkeypatch.setattr(receiver, 'verify_client', lambda *_args: (_ for _ in ()).throw(ExclusiveProfileError('Broken Spotify')))
    assert receiver.switch(str(ready), mode)['requested_mode'] == mode
    assert ('stop_confirmed',) in services.calls
    assert [call for call in services.calls if call[0] == 'start'] == (
        [('start', receiver.RENDERER_SERVICE)] if mode == 'renderer' else [])


def test_duplicate_or_unsafe_control_locks_fail_closed(ready):
    with receiver._lock(ready, 'control.lock'):
        with pytest.raises(ExclusiveProfileError, match='Another receiver operation'):
            with receiver._lock(ready, 'control.lock'):
                pytest.fail('duplicate controller admitted')
    (ready / 'foreign').write_text('unchanged')
    (ready / 'control.lock').unlink()
    (ready / 'control.lock').symlink_to(ready / 'foreign')
    with pytest.raises(OSError):
        with receiver._lock(ready, 'control.lock'):
            pytest.fail('followed a lock symlink')
    assert (ready / 'foreign').read_text() == 'unchanged'


def test_moving_cgroup_tree_is_unknown_not_recursive(monkeypatch, tmp_path):
    services = receiver.UserServices(tmp_path, {})
    group = f'/user.slice/user-{os.geteuid()}.slice/user@{os.geteuid()}.service/app.slice/{receiver.SPOTIFY_SERVICE}'
    monkeypatch.setattr(Path, 'is_file', lambda _self: True)
    monkeypatch.setattr(Path, 'rglob', lambda *_args: [])
    monkeypatch.setattr(Path, 'is_dir', lambda _self: True)
    monkeypatch.setattr(Path, 'read_text', lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(Path, 'exists', lambda _self: True)
    assert services.cgroup_empty(group) is False


def test_client_receipt_cannot_forge_executable_provenance(tmp_path):
    executable = tmp_path / 'go-librespot'
    shutil.copyfile('/usr/bin/true', executable)
    executable.chmod(0o700)
    spec = client.ARCHIVES[client.platform.machine()]
    (tmp_path / 'client-receipt.json').write_text(json.dumps({'format': 1, 'version': client.UPSTREAM_VERSION,
        'commit': client.UPSTREAM_COMMIT, 'archive_sha256': spec[1],
        'binary_sha256': hashlib.sha256(executable.read_bytes()).hexdigest()}))
    with pytest.raises(ExclusiveProfileError, match='changed'):
        client.verify_client(tmp_path)


def test_real_client_pins_are_complete_and_not_architecture_filename_claims():
    assert set(client.ARCHIVES) == set(client.BINARY_SHA256)
    for name, spec in client.ARCHIVES.items():
        assert len(spec[1]) == len(client.BINARY_SHA256[name]) == 64
        assert spec[1] != client.BINARY_SHA256[name]


def test_client_cli_requires_one_source_and_preserves_offline_path(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(receiver, 'install_client', lambda *args, **kwargs: calls.append((args, kwargs)) or {})
    base = ['spotify', 'install-client', '--output-dir', str(tmp_path / 'client')]
    for source in ([], ['--download', '--archive', '/archive.tar.gz']):
        with pytest.raises(SystemExit) as exc:
            cli.main(base + source)
        assert exc.value.code == 2
    assert not calls
    assert cli.main(base + ['--download']) == 0
    assert cli.main(base + ['--archive', '/archive.tar.gz']) == 0
    assert calls == [((None, str(tmp_path / 'client')), {'download': True}),
                     (('/archive.tar.gz', str(tmp_path / 'client')), {'download': False})]


def test_download_still_requires_pinned_archive_and_fresh_destination(monkeypatch, tmp_path):
    calls = []
    async def download(machine):
        calls.append(machine)
        return b'corrupt or incomplete archive'
    monkeypatch.setattr(client, '_download_archive', download)
    output = tmp_path / 'client'
    with pytest.raises(ExclusiveProfileError, match='SHA-256'):
        client.install_client(None, str(output), download=True)
    assert calls == [client.platform.machine().lower()] and not output.exists()
    output.mkdir()
    (output / 'keep').write_text('unchanged')
    with pytest.raises(ExclusiveProfileError, match='must be new'):
        client.install_client(None, str(output), download=True)
    assert len(calls) == 1 and (output / 'keep').read_text() == 'unchanged'


@pytest.mark.asyncio
@pytest.mark.parametrize('case,error', [('redirect', 'official HTTPS'), ('oversize', 'size limit'),
                                       ('timeout', 'not completed')])
async def test_download_rejects_external_redirect_oversize_and_stall(monkeypatch, case, error):
    calls = []
    class Response:
        status = 302 if case == 'redirect' else 200
        headers = {'Location': 'https://unrelated.example/client'} if case == 'redirect' else {}
        content_length = None
        @property
        def content(self): return self
        async def iter_chunked(self, _size):
            if case == 'timeout':
                await asyncio.sleep(1)
            yield b'x' * 9
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
    class Session:
        def __init__(self, **kwargs):
            assert kwargs['trust_env'] is False and kwargs['auto_decompress'] is False
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        def get(self, url, **kwargs):
            assert kwargs['allow_redirects'] is False
            calls.append(url)
            return Response()
    monkeypatch.setattr(client.aiohttp, 'ClientSession', Session)
    monkeypatch.setattr(client, 'MAX_ARCHIVE', 8)
    monkeypatch.setattr(client, 'DOWNLOAD_SECONDS', 0.01)
    with pytest.raises(ExclusiveProfileError, match=error):
        await client._download_archive('armv7l')
    assert calls == [f'https://github.com/devgianlu/go-librespot/releases/download/{client.UPSTREAM_VERSION}/go-librespot_linux_armv6.tar.gz']


def test_install_can_resume_owned_partial_links(ready, monkeypatch, tmp_path):
    units = tmp_path / 'user-units'
    units.mkdir()
    monkeypatch.setattr(receiver, '_user_units', lambda: units)
    root, profile = receiver.load_profile(ready)
    class Manager:
        def __init__(self, *_args): pass
        def command(self, *_args, **_kwargs): return ''
        def show(self, mode): return stopped() | {'LoadState': 'not-found'}
        def validate(self): return {}
    monkeypatch.setattr(receiver, 'UserServices', Manager)
    links = receiver._install_links(root, profile)
    (units / (receiver.RENDERER_SERVICE + '.d')).mkdir()
    receiver._write(root / 'installation.json', {'format': 1, 'state': 'INSTALLING',
        'native_link_created': True, 'links': {str(p): str(t) for p, t in links.items()}})
    first = next(iter(links))
    first.symlink_to(links[first])
    assert receiver.install(str(root))['installation'] == 'INSTALLED'
    assert all(receiver._exact_link(p, t) for p, t in links.items())
    assert json.loads((root / 'installation.json').read_text())['state'] == 'INSTALLED'


def test_status_does_not_call_transition_or_failure_off(ready, monkeypatch):
    class Manager:
        def __init__(self, *_args): pass
        stopped = receiver.UserServices.stopped
        def validate(self): return {'renderer': stopped(), 'spotify': stopped() | {'ActiveState': 'failed', 'Result': 'exit-code'}}
    monkeypatch.setattr(receiver, 'UserServices', Manager)
    assert receiver.status(str(ready))['selected_service'] == 'UNKNOWN'


def test_playback_snapshot_is_opt_in_and_skipped_when_spotify_is_not_selected(ready, services, monkeypatch):
    monkeypatch.setattr(receiver, 'SpotifyDiagnosticClient',
                        lambda *_args: pytest.fail('unrequested API read'))
    services.calls = [('started', 'spotify')]
    assert 'provider_playback' not in receiver.status(str(ready))
    services.calls = [('started', 'renderer')]
    result = receiver.status(str(ready), playback=True)
    assert result['selected_service'] == 'renderer'
    assert result['provider_playback']['observation'] == 'not_observed'
    assert result['provider_playback']['status'] is None
    assert result['connect_session'] == 'UNKNOWN'
    assert services.calls == [('started', 'renderer')]


@pytest.mark.asyncio
@pytest.mark.parametrize('response_code', [200, 204, 503])
async def test_receiver_playback_snapshot_uses_profile_and_preserves_services(
        ready, services, monkeypatch, capsys, response_code):
    async def handler(request):
        if request.path == '/':
            return web.json_response({'playback_ready': True})
        assert request.path == '/status'
        return (web.json_response(STATUS) if response_code == 200 else
                web.Response(status=response_code, text=SECRET if response_code == 503 else None))

    monkeypatch.setattr(cli, 'api_get', lambda *_args: pytest.fail('renderer HTTP contacted'))
    services.calls = [('started', 'spotify')]
    async with peer(handler) as (port, requests):
        data = receiver._json(ready / 'receiver.json')
        receiver._write(ready / 'receiver.json', data | {'port': port})
        code = await asyncio.to_thread(cli.main, ['spotify', 'receiver', '--directory', str(ready),
                                                  'status', '--playback'])
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert captured.err == ''
    result = json.loads(captured.out)
    observed = result['provider_playback']
    assert result['selected_service'] == 'spotify'
    assert result['services']['spotify']['ActiveState'] == 'active'
    assert result['audio_output'] == 'UNVERIFIED'
    assert observed['audio_output'] == observed['listener_ownership'] == 'unverified'
    assert [(method, path) for method, path, _ in requests] == [('GET', '/'), ('GET', '/status')]
    assert services.calls == [('started', 'spotify')]
    if response_code == 503:
        assert code == 1 and observed['observation'] == 'unavailable'
        assert observed['status'] is None and result['connect_session'] == 'UNKNOWN'
    else:
        assert code == 0 and observed['observation'] == 'sidecar_reported'
        assert observed['snapshot_freshness'] == 'snapshot_only'
        assert result['connect_session'] == ('SESSION_PRESENT' if response_code == 200 else 'NO_SESSION')
        assert (observed['status']['paused'] is True and observed['status']['volume'] == 25
                if response_code == 200 else observed['status'] is None)


def test_receiver_dispatch_does_not_contact_renderer_http(ready, monkeypatch):
    called = []
    monkeypatch.setattr(receiver, 'switch', lambda directory, mode: called.append((directory, mode)) or {'service_state': 'STOPPED'})
    monkeypatch.setattr(cli, 'api_get', lambda *_args: pytest.fail('receiver depends on stopped renderer HTTP'))
    assert cli.main(['spotify', 'receiver', '--directory', str(ready), 'switch', 'renderer']) == 0
    assert called == [(str(ready), 'renderer')]


def test_exec_readiness_requires_the_selected_target_and_returns_invocation(ready, monkeypatch):
    root, profile = receiver.load_profile(ready)
    services = receiver.UserServices(root, profile)
    identity = {'ActiveState': 'active', 'MainPID': '12345', 'InvocationID': 'a' * 32}
    monkeypatch.setattr(services, 'show', lambda _mode: identity)
    argv = receiver._target(root, profile, 'spotify')
    original = Path.read_bytes
    monkeypatch.setattr(Path, 'read_bytes', lambda path: b'\0'.join(os.fsencode(v) for v in argv) + b'\0'
                        if str(path) == '/proc/12345/cmdline' else original(path))
    assert services.wait_started('spotify') == ('a' * 32, '12345')
    # Type=simple can be active while the old Python guard is still running.
    argv[:] = [str(Path(profile['renderer_prefix']) / 'venv/bin/open-room-renderer'),
               'spotify', 'receiver', '--directory', str(root), 'run', 'spotify']
    ticks = iter([0, 0, 6])
    monkeypatch.setattr(receiver.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(receiver.time, 'sleep', lambda _n: None)
    with pytest.raises(ExclusiveProfileError, match='exec was not confirmed'):
        services.wait_started('spotify')


def test_uninstall_resumes_only_recorded_absent_links(ready, monkeypatch, tmp_path):
    units = tmp_path / 'user-units'
    units.mkdir()
    monkeypatch.setattr(receiver, '_user_units', lambda: units)
    root, profile = receiver.load_profile(ready)
    links = receiver._install_links(root, profile)
    (units / (receiver.RENDERER_SERVICE + '.d')).mkdir()
    for path, target in links.items():
        path.symlink_to(target)
    receiver._write(root / 'installation.json', {'format': 1, 'state': 'INSTALLED',
        'links': {str(p): str(t) for p, t in links.items()}})
    class Manager:
        def __init__(self, *_args): pass
        stopped = receiver.UserServices.stopped
        def command(self, *_args, **_kwargs): return ''
        def show(self, mode): return stopped() | {'LoadState': 'loaded'}
        def validate(self): return {mode: self.show(mode) for mode in receiver.UNITS}
        def stop_all(self, _before): receiver._write(root / 'last-cgroups.json', dict.fromkeys(receiver.UNITS, ''))
        def cgroup_empty(self, _group): return True
    monkeypatch.setattr(receiver, 'UserServices', Manager)
    original = Path.unlink
    second = list(links)[1]
    injected = False
    def unlink(path, *args, **kwargs):
        nonlocal injected
        if path == second and not injected:
            injected = True
            raise OSError('injected second unlink failure')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', unlink)
    with pytest.raises(OSError):
        receiver.uninstall(str(root))
    assert json.loads((root / 'installation.json').read_text())['state'] == 'UNINSTALLING'
    assert receiver.uninstall(str(root))['installation'] == 'UNINSTALLED'
    assert not any(os.path.lexists(path) for path in links)
    assert (root / 'sidecar/config.yml').exists()


def test_stop_retains_and_checks_previously_known_cgroup(ready, monkeypatch):
    root, profile = receiver.load_profile(ready)
    services = receiver.UserServices(root, profile)
    previous = f'/user.slice/user-{os.geteuid()}.slice/user@{os.geteuid()}.service/app.slice/{receiver.SPOTIFY_SERVICE}'
    receiver._write(root / 'last-cgroups.json', {'renderer': '', 'spotify': previous})
    calls = []
    monkeypatch.setattr(services, 'command', lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(services, 'validate', lambda: dict.fromkeys(receiver.UNITS, stopped()))
    monkeypatch.setattr(services, 'cgroup_empty', lambda group: group != previous)
    with pytest.raises(ExclusiveProfileError, match='cgroup release'):
        services.stop_all(dict.fromkeys(receiver.UNITS, stopped()))
    assert calls == [('stop', *receiver.UNITS.values())]
    assert json.loads((root / 'last-cgroups.json').read_text())['spotify'] == previous


def test_recovery_does_not_reset_successful_inactive_peer(ready, monkeypatch):
    calls = []
    failed = True
    class Manager:
        def __init__(self, *_args): pass
        stopped = receiver.UserServices.stopped
        def validate(self):
            return {'renderer': stopped() | ({'ActiveState': 'failed', 'SubState': 'failed', 'Result': 'timeout'} if failed else {}),
                    'spotify': stopped()}
        def cgroup_empty(self, _group): return True
        def command(self, *args, **_kwargs):
            nonlocal failed
            calls.append(args)
            if args[0] == 'reset-failed':
                assert args == ('reset-failed', receiver.RENDERER_SERVICE)
                failed = False
    monkeypatch.setattr(receiver, 'UserServices', Manager)
    assert receiver.recover(str(ready))['recovery'] == 'OFF_ONLY'
    assert calls == [('stop', *receiver.UNITS.values()), ('reset-failed', receiver.RENDERER_SERVICE)]


@pytest.fixture
def refresh_services(ready, monkeypatch):
    root, profile = receiver.load_profile(ready)
    states = {mode: stopped() | {'Id': unit, 'LoadState': 'loaded', 'NeedDaemonReload': 'no',
        'FragmentPath': profile['renderer_unit'] if mode == 'renderer' else str(root / unit),
        'DropInPaths': str(root / (unit + '.d') / receiver.DROPIN) if mode == 'renderer' else '',
        'KillMode': 'control-group', 'SendSIGKILL': 'yes', 'Restart': 'no', 'TimeoutStopUSec': '45s'}
        for mode, unit in receiver.UNITS.items()}
    busy, inspected = set(), []
    monkeypatch.setattr(receiver.UserServices, 'show', lambda _self, mode: states[mode])
    monkeypatch.setattr(receiver.UserServices, 'command', lambda *_args, **_kwargs: pytest.fail('service mutation'))
    def empty(_self, group):
        inspected.append(group)
        return group not in busy
    monkeypatch.setattr(receiver.UserServices, 'cgroup_empty', empty)
    monkeypatch.setattr(receiver, 'probe_alsa', lambda *_args: pytest.fail('physical output probe'))
    return states, busy, inspected


def test_refresh_cli_changes_only_native_config_fingerprint_without_spotify(ready, refresh_services, monkeypatch, capsys):
    profile = receiver._json(ready / 'receiver.json')
    config = Path(profile['renderer_config'])
    config.write_text(config.read_text() + '\n# Same direct output, edited native YAML.\n')
    (ready / 'sidecar/config.yml').write_text('damaged config')
    for name in ('sidecar/credentials.json', 'startup.json', 'installation.json', 'last-operation.json'):
        (ready / name).write_text('preserve this private state\n')
    receiver._write(ready / 'last-cgroups.json', {'renderer': 'saved-renderer', 'spotify': 'saved-spotify'})
    states, _busy, inspected = refresh_services
    states['renderer']['ControlGroup'] = 'current-renderer'
    states['spotify']['ControlGroup'] = 'current-spotify'
    before = {path: path.read_bytes() for path in ready.rglob('*') if path.is_file()}
    monkeypatch.setattr(receiver, 'verify_client', lambda *_args: pytest.fail('Spotify health dependency'))
    monkeypatch.setattr(receiver, '_check_mpv', lambda: pytest.fail('unrequested executable check'))
    assert cli.main(['spotify', 'receiver', '--directory', str(ready), 'refresh-renderer']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {'renderer_refresh': 'UPDATED', 'service_state': 'STOPPED',
                      'services_started': False, 'audio_output': 'UNVERIFIED'}
    updated = receiver._json(ready / 'receiver.json')
    assert updated == profile | {'renderer': profile['renderer'] | {
        'config_sha256': hashlib.sha256(config.read_bytes()).hexdigest()}}
    assert ready.joinpath('receiver.json').stat().st_mode & 0o777 == 0o600
    assert all(path.read_bytes() == data for path, data in before.items() if path.name != 'receiver.json')
    assert inspected == ['current-renderer', 'saved-renderer', 'current-spotify', 'saved-spotify']
    monkeypatch.setattr(receiver, '_write', lambda *_args: pytest.fail('unchanged refresh wrote metadata'))
    assert receiver.refresh_renderer(str(ready))['renderer_refresh'] == 'UNCHANGED'
    monkeypatch.setattr(receiver, '_check_mpv', lambda: None)
    receiver.load_profile(ready, start='renderer')


@pytest.mark.parametrize('case', ['active', 'result', 'job', 'current-cgroup', 'saved-cgroup', 'ownership', 'saved-record'])
def test_refresh_refuses_unproven_stop_or_ownership_without_writes(ready, refresh_services, monkeypatch, case):
    states, busy, _inspected = refresh_services
    profile = receiver._json(ready / 'receiver.json')
    config = Path(profile['renderer_config'])
    config.write_text(config.read_text() + '\n# Edited YAML.\n')
    saved = dict.fromkeys(receiver.UNITS, '')
    if case == 'active':
        states['spotify'].update(ActiveState='active', SubState='running', MainPID='42')
    elif case == 'result':
        states['renderer']['Result'] = 'timeout'
    elif case == 'job':
        states['spotify']['Job'] = '41'
    elif case == 'current-cgroup':
        states['renderer']['ControlGroup'] = 'current-descendant'
        busy.add('current-descendant')
    elif case == 'saved-cgroup':
        saved['spotify'] = 'saved-descendant'
        busy.add('saved-descendant')
    elif case == 'ownership':
        states['spotify']['NeedDaemonReload'] = 'yes'
    else:
        saved.pop('renderer')
    receiver._write(ready / 'last-cgroups.json', saved)
    before = (ready / 'receiver.json').read_bytes()
    monkeypatch.setattr(receiver, '_write', lambda *_args: pytest.fail('unproven refresh wrote metadata'))
    with pytest.raises(ExclusiveProfileError):
        receiver.refresh_renderer(str(ready))
    assert (ready / 'receiver.json').read_bytes() == before


@pytest.mark.parametrize('case', ['route', 'unit', 'config-path', 'duplicate-yaml', 'invalid-yaml'])
def test_refresh_rechecks_native_admission_and_refuses_migration(ready, refresh_services, monkeypatch, case):
    profile = receiver._json(ready / 'receiver.json')
    config = Path(profile['renderer_config'])
    if case == 'route':
        raw = json.loads(config.read_text())
        config.write_text(json.dumps(raw | {'mpv_audio_device': 'alsa/hw:CARD=OTHER,DEV=0'}))
    elif case == 'unit':
        Path(profile['renderer_unit']).write_text('# Customized service\n')
    elif case == 'config-path':
        migrated = config.parent / 'migrated-profile'
        migrated.mkdir(mode=0o700)
        moved = migrated / 'config.yaml'
        moved.write_bytes(config.read_bytes())
        profile['renderer_config'] = str(moved)
        Path(profile['renderer_unit']).write_text(receiver.user_unit(config.parent, config=moved))
        receiver._write(ready / 'receiver.json', profile)
    elif case == 'duplicate-yaml':
        config.write_text('auto_spawn_mpv: true\nauto_spawn_mpv: false\n')
    else:
        config.write_text('auto_spawn_mpv: [unterminated\n')
    before = (ready / 'receiver.json').read_bytes()
    monkeypatch.setattr(receiver, '_write', lambda *_args: pytest.fail('unsupported refresh wrote metadata'))
    with pytest.raises(ExclusiveProfileError, match='malformed' if case == 'invalid-yaml' else 'receiver setup'):
        receiver.refresh_renderer(str(ready))
    assert (ready / 'receiver.json').read_bytes() == before


def test_refresh_requires_control_lock_and_changed_start_points_to_command(ready, refresh_services, monkeypatch):
    profile = receiver._json(ready / 'receiver.json')
    config = Path(profile['renderer_config'])
    config.write_text(config.read_text() + '\n# Edited YAML.\n')
    with pytest.raises(ExclusiveProfileError, match='--directory DIR refresh-renderer'):
        receiver.load_profile(ready, start='renderer')
    monkeypatch.setattr(receiver, '_write', lambda *_args: pytest.fail('locked refresh wrote metadata'))
    with receiver._lock(ready, 'control.lock'):
        with pytest.raises(ExclusiveProfileError, match='Another receiver operation'):
            receiver.refresh_renderer(str(ready))


@pytest.fixture
def startup_ready(ready, monkeypatch, tmp_path):
    units, global_units, runtime_units = [tmp_path / name for name in ('user-units', 'global-units', 'runtime-units')]
    for path in (units, global_units, runtime_units):
        path.mkdir()
    monkeypatch.setattr(receiver, '_user_units', lambda: units)
    root, profile = receiver.load_profile(ready)
    links = receiver._install_links(root, profile)
    (units / (receiver.RENDERER_SERVICE + '.d')).mkdir()
    for path, target in links.items():
        path.symlink_to(target)
    (units / receiver.RENDERER_SERVICE).symlink_to(profile['renderer_unit'])
    wanted = units / 'default.target.wants'
    wanted.mkdir()
    receiver._write(root / 'installation.json', {'format': 1, 'state': 'INSTALLED',
        'links': {str(p): str(t) for p, t in links.items()}})
    class Manager:
        calls = []
        stopped = receiver.UserServices.stopped
        def __init__(self, root, profile): self.root, self.profile = root, profile
        def command(self, *args, **_kwargs):
            self.calls.append(args)
            if args == ('show', '--property=UnitPath'):
                return f'UnitPath={units} {global_units} {runtime_units}\n'
            assert args == ('daemon-reload',)
            return ''
        def show(self, _mode): return stopped() | {'LoadState': 'loaded'}
        def validate(self): return {mode: self.show(mode) for mode in receiver.UNITS}
        def stop_all(self, _before): receiver._write(root / 'last-cgroups.json', dict.fromkeys(receiver.UNITS, ''))
        def cgroup_empty(self, _group): return True
    monkeypatch.setattr(receiver, 'UserServices', Manager)
    return root, profile, wanted, global_units, runtime_units, Manager


@pytest.mark.parametrize('original', [None, 'canonical', 'relative'])
def test_startup_selection_preserves_exact_native_choice_without_activation(startup_ready, original):
    root, profile, wanted, _global, _runtime, manager = startup_ready
    native, spotify = [wanted / unit for unit in receiver.UNITS.values()]
    original = {'canonical': profile['renderer_unit'], 'relative': '../' + receiver.RENDERER_SERVICE}.get(original)
    if original:
        native.symlink_to(original)
    proposals = {name: (root / name).read_bytes() for name in receiver._unit_parts(root, profile)}
    with pytest.raises(ExclusiveProfileError, match='Choose startup'):
        receiver.startup(str(root), 'unexpected')
    assert not (root / 'startup.json').exists() and not manager.calls
    assert receiver.startup(str(root), 'status')['startup_state'] == 'UNCHANGED'
    assert not (root / 'startup.json').exists()
    assert receiver.startup(str(root), 'spotify')['selected_startup'] == 'spotify'
    assert spotify.is_symlink() and not os.path.lexists(native)
    assert receiver.startup(str(root), 'renderer')['selected_startup'] == 'renderer'
    assert os.readlink(native) == (original or profile['renderer_unit'])
    assert receiver.startup(str(root), 'off')['selected_startup'] == 'off'
    assert not os.path.lexists(native) and not os.path.lexists(spotify)
    result = receiver.startup(str(root), 'restore')
    assert result['startup_state'] == 'RESTORED' and result['services_started'] is False
    assert (os.readlink(native) if original else None) == original
    assert bool(os.path.lexists(native)) == bool(original) and not os.path.lexists(spotify)
    assert {name: (root / name).read_bytes() for name in proposals} == proposals
    assert set(call[0] for call in manager.calls) <= {'show', 'daemon-reload'}


@pytest.mark.parametrize('case', ['global', 'runtime', 'custom', 'alias', 'foreign', 'unreceipted'])
def test_startup_refuses_unfamiliar_enablement_without_mutation(startup_ready, case):
    root, profile, wanted, global_units, runtime_units, manager = startup_ready
    native = wanted / receiver.RENDERER_SERVICE
    native.symlink_to(profile['renderer_unit'])
    if case in {'global', 'runtime', 'custom'}:
        base = {'global': global_units, 'runtime': runtime_units, 'custom': wanted.parent}[case]
        directory = base / ('custom.target.wants' if case == 'custom' else 'default.target.wants')
        directory.mkdir()
        foreign = directory / receiver.RENDERER_SERVICE
        foreign.symlink_to(profile['renderer_unit'])
    elif case == 'alias':
        foreign = wanted.parent / 'other-name.service'
        foreign.symlink_to(profile['renderer_unit'])
    else:
        native.unlink()
        foreign = native if case == 'foreign' else wanted / receiver.SPOTIFY_SERVICE
        foreign.symlink_to('/unrelated/service' if case == 'foreign' else root / receiver.SPOTIFY_SERVICE)
    before = os.readlink(foreign)
    with pytest.raises(ExclusiveProfileError):
        receiver.startup(str(root), 'off')
    assert os.readlink(foreign) == before and not (root / 'startup.json').exists()
    assert not any(call[0] == 'daemon-reload' for call in manager.calls)


@pytest.mark.parametrize('resume', ['spotify', 'restore', 'uninstall'])
def test_startup_interruption_resumes_explicitly_and_restore_needs_no_client(startup_ready, monkeypatch, resume):
    root, profile, wanted, _global, _runtime, _manager = startup_ready
    native = wanted / receiver.RENDERER_SERVICE
    original = '../' + receiver.RENDERER_SERVICE
    native.symlink_to(original)
    original_symlink = os.symlink
    def interrupted(source, destination, **kwargs):
        if destination == receiver.SPOTIFY_SERVICE:
            raise OSError('injected interrupted startup apply')
        return original_symlink(source, destination, **kwargs)
    monkeypatch.setattr(os, 'symlink', interrupted)
    with pytest.raises(OSError):
        receiver.startup(str(root), 'spotify')
    assert not any(wanted.iterdir())
    assert receiver.startup(str(root), 'status')['startup_state'] == 'APPLYING'
    with pytest.raises(ExclusiveProfileError, match='pending startup'):
        receiver.startup(str(root), 'off')
    monkeypatch.setattr(os, 'symlink', original_symlink)
    if resume != 'spotify':
        (root / 'sidecar/config.yml').write_text('broken client config')
        monkeypatch.setattr(receiver, 'verify_client', lambda *_args: pytest.fail('restore needs Spotify'))
    if resume == 'uninstall':
        assert receiver.uninstall(str(root))['installation'] == 'UNINSTALLED'
        assert (root / 'sidecar/config.yml').read_text() == 'broken client config'
    else:
        assert receiver.startup(str(root), resume)['startup_state'] != 'APPLYING'
    if resume != 'spotify':
        assert os.readlink(native) == original
        assert not os.path.lexists(wanted / receiver.SPOTIFY_SERVICE)


def test_startup_and_uninstall_preserve_a_changed_foreign_link(startup_ready):
    root, profile, wanted, _global, _runtime, _manager = startup_ready
    receiver.startup(str(root), 'spotify')
    foreign = wanted / receiver.SPOTIFY_SERVICE
    foreign.unlink()
    foreign.symlink_to('/foreign/keep.service')
    for action in ('restore', 'uninstall'):
        with pytest.raises(ExclusiveProfileError, match='unfamiliar startup link'):
            receiver.uninstall(str(root)) if action == 'uninstall' else receiver.startup(str(root), action)
        assert os.readlink(foreign) == '/foreign/keep.service'
        assert (wanted.parent / receiver.SPOTIFY_SERVICE).is_symlink()


def test_startup_cli_dispatch_is_explicit(ready, monkeypatch):
    called = []
    monkeypatch.setattr(receiver, 'startup', lambda directory, selection: called.append((directory, selection)) or {})
    assert cli.main(['spotify', 'receiver', '--directory', str(ready), 'startup', 'restore']) == 0
    assert called == [(str(ready), 'restore')]


def test_guard_exec_failure_has_private_historical_cli_channel(ready, services, monkeypatch, capsys):
    states = {mode: stopped() for mode in receiver.UNITS}
    group = f'/user.slice/user-{os.geteuid()}.slice/user@{os.geteuid()}.service/app.slice/{receiver.SPOTIFY_SERVICE}'
    states['spotify'].update(ActiveState='active', SubState='running', MainPID=str(os.getpid()), ControlGroup=group)
    monkeypatch.setattr(services, 'validate', lambda _self: states)
    original = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda path, *args, **kwargs:
                        '0::' + group if str(path) == '/proc/self/cgroup' else original(path, *args, **kwargs))
    monkeypatch.setattr(receiver.os, 'umask', lambda _mask: None)
    attempts = []
    def fail_exec(*args):
        attempts.append(args)
        raise OSError(SECRET)
    monkeypatch.setattr(receiver.os, 'execv', fail_exec)
    command = ['spotify', 'receiver', '--directory', str(ready)]
    assert cli.main(command + ['run', 'spotify']) == 1
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert len(attempts) == 1
    record = ready / 'last-guard-error.json'
    assert record.exists(), 'generic exec failure currently leaves no startup error record'
    assert record.stat().st_mode & 0o777 == 0o600
    assert receiver._json(record) == {'mode': 'spotify', 'category': 'EXEC_FAILED'}
    assert cli.main(command + ['last-start-error']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {'record_scope': 'HISTORICAL_NOT_CURRENT', 'last_start_error':
                      receiver._json(record) | {'hint': receiver.START_FAILURE_HINTS['EXEC_FAILED']}}


@pytest.mark.parametrize('error,category', [(ExclusiveProfileError, 'GUARD_REJECTED'), (ValueError, 'STARTUP_FAILED')])
def test_guard_failure_record_and_output_never_echo_exception(ready, monkeypatch, capsys, error, category):
    monkeypatch.setattr(receiver, 'run_guard', lambda *_args: (_ for _ in ()).throw(error(SECRET)))
    assert cli.main(['spotify', 'receiver', '--directory', str(ready), 'run', 'renderer']) == 1
    record = (ready / 'last-guard-error.json').read_text()
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err + record
    assert json.loads(record) == {'mode': 'renderer', 'category': category}


def test_start_failure_read_is_independent_historical_and_best_effort(ready, services, monkeypatch, capsys):
    command = ['spotify', 'receiver', '--directory', str(ready), 'last-start-error']
    assert cli.main(command) == 0
    assert json.loads(capsys.readouterr().out) == {'record_scope': 'HISTORICAL_NOT_CURRENT', 'last_start_error': None}
    record = {'mode': 'spotify', 'category': 'EXEC_FAILED'}
    receiver._write(ready / 'last-guard-error.json', record)
    with monkeypatch.context() as patch:
        patch.setattr(receiver, 'run_guard', lambda *_args: (_ for _ in ()).throw(ValueError(SECRET)))
        patch.setattr(receiver, '_write', lambda *_args: (_ for _ in ()).throw(OSError(SECRET)))
        assert cli.main(['spotify', 'receiver', '--directory', str(ready), 'run', 'renderer']) == 1
    # A later failure whose write failed, and successful return/off, do not make
    # the historical record current or introduce any dependency on its contents.
    assert receiver._json(ready / 'last-guard-error.json') == record
    (ready / 'sidecar/config.yml').unlink()
    receiver.switch(str(ready), 'renderer')
    receiver.switch(str(ready), 'off')
    monkeypatch.setattr(receiver, 'load_profile', lambda *_args, **_kwargs: pytest.fail('read depends on profile health'))
    assert cli.main(command) == 0
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    result = json.loads(captured.out)
    assert result['record_scope'] == 'HISTORICAL_NOT_CURRENT'
    assert result['last_start_error']['category'] == 'EXEC_FAILED'
    assert services.calls == [('stop_confirmed',), ('probe',), ('start', receiver.RENDERER_SERVICE),
                              ('started', 'renderer'), ('stop_confirmed',)]


@pytest.mark.parametrize('data', [
    '{',
    json.dumps({'mode': 'spotify', 'category': SECRET}),
    json.dumps({'mode': 'spotify', 'category': 'EXEC_FAILED', 'error': SECRET}),
    '{"mode":"spotify","category":"EXEC_FAILED","category":"EXEC_FAILED"}',
    json.dumps([['mode', 'spotify'], ['category', 'EXEC_FAILED']]),
    json.dumps({'mode': ['spotify'], 'category': 'EXEC_FAILED'}),
    ' ' * 257 + '{}',
])
def test_start_failure_read_rejects_malformed_or_untrusted_records(ready, monkeypatch, capsys, data):
    path = ready / 'last-guard-error.json'
    path.write_text(data)
    monkeypatch.setattr(receiver, 'UserServices', lambda *_args: pytest.fail('error read contacted service manager'))
    assert cli.main(['spotify', 'receiver', '--directory', str(ready), 'last-start-error']) == 1
    output = capsys.readouterr()
    assert not output.out and SECRET not in output.err and str(ready) not in output.err
    assert path.read_text() == data
