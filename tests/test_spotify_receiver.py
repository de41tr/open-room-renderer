"""Small discriminating safety contracts; synthetic peers, no account/device."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from fake_linkplay import cli, spotify_client as client, spotify_receiver as receiver
from fake_linkplay.spotify_exclusive import ExclusiveProfileError
from test_spotify_exclusive import profile  # existing canonical native fixture


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


def test_switch_confirms_whole_stop_and_probe_before_one_start(ready, services):
    result = receiver.switch(str(ready), 'spotify')
    assert services.calls == [('stop_confirmed',), ('probe',), ('start', receiver.SPOTIFY_SERVICE), ('started', 'spotify')]
    assert result['service_state'] == 'PROCESS_STARTED' and result['audio_output'] == 'UNVERIFIED'


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


def test_return_starts_renderer_with_broken_spotify(ready, services, monkeypatch):
    (ready / 'sidecar/config.yml').write_text('damaged')
    monkeypatch.setattr(receiver, 'verify_client', lambda *_args: (_ for _ in ()).throw(ExclusiveProfileError('Broken Spotify')))
    assert receiver.switch(str(ready), 'renderer')['requested_mode'] == 'renderer'
    assert ('start', receiver.RENDERER_SERVICE) in services.calls


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
