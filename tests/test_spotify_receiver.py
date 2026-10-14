"""Small discriminating safety contracts; synthetic peers, no account/device."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest
from aiohttp import web

from fake_linkplay import cli, spotify_client as client, spotify_receiver as receiver
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
