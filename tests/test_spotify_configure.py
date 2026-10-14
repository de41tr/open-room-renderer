"""Stopped-only settings/output and explicit split-write retry, with inert services."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fake_linkplay import cli, spotify_exclusive as exclusive, spotify_receiver as receiver
from fake_linkplay.spotify_exclusive import ExclusiveProfileError
from test_spotify_exclusive import config_change
from test_spotify_receiver import Services, profile, ready, refresh_services


def installed(root, monkeypatch):
    """Install only synthetic links; never call the actual service manager."""
    units = root.parent / 'user-units'
    units.mkdir()
    (units / (receiver.RENDERER_SERVICE + '.d')).mkdir()
    monkeypatch.setattr(receiver, '_user_units', lambda: units)
    saved = receiver._json(root / 'receiver.json')
    links = receiver._install_links(root, saved)
    for path, target in links.items():
        path.symlink_to(target)
    receiver._write(root / 'installation.json', {'format': 1, 'state': 'INSTALLED',
        'links': {str(path): str(target) for path, target in links.items()}})
    return links


def test_configure_cli_preserves_private_state_and_omitted_settings(ready, refresh_services, monkeypatch, capsys):
    links = installed(ready, monkeypatch)
    for name in ('sidecar/credentials.json', 'sidecar/state.json', 'startup.json', 'last-operation.json'):
        (ready / name).write_bytes(b'private-synthetic-account-and-device-id\x00\xff\n')
    receiver._write(ready / 'last-cgroups.json', {'renderer': 'saved-renderer', 'spotify': 'saved-spotify'})
    saved = receiver._json(ready / 'receiver.json')
    saved.pop('device_name')  # Older profiles retain implicit defaults unless selected.
    saved['future_metadata'] = {'preserve': True}
    receiver._write(ready / 'receiver.json', saved)
    states, _busy, inspected = refresh_services
    states['renderer']['ControlGroup'] = 'current-renderer'
    states['spotify']['ControlGroup'] = 'current-spotify'
    before = {path: path.read_bytes() for path in ready.rglob('*') if path.is_file()}
    old_sidecar = receiver._json(ready / 'sidecar/config.yml')
    read = receiver._read
    def no_account_reads(path, **kwargs):
        assert path.name not in {'credentials.json', 'state.json'}
        return read(path, **kwargs)
    monkeypatch.setattr(receiver, '_read', no_account_reads)
    monkeypatch.setattr(receiver, '_check_mpv', lambda: pytest.fail('unrequested mpv inspection'))
    name = 'Studio: "北" 100%'
    command = ['spotify', 'receiver', '--directory', str(ready), 'configure']
    assert cli.main(command + ['--name', name]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['receiver_configuration'] == 'UPDATED'
    assert result['service_state'] == 'STOPPED' and result['services_started'] is False
    assert result['provider_settings'] == 'UNVERIFIED' and name not in json.dumps(result)
    assert receiver._json(ready / 'receiver.json') == saved | {'device_name': name}
    assert receiver._json(ready / 'sidecar/config.yml') == old_sidecar | {'device_name': name}
    assert inspected == ['current-renderer', 'saved-renderer', 'current-spotify', 'saved-spotify']
    assert cli.main(command + ['--bitrate', '320']) == 0
    capsys.readouterr()
    assert receiver._json(ready / 'receiver.json') == saved | {'device_name': name, 'preferred_bitrate': 320}
    assert receiver._json(ready / 'sidecar/config.yml') == old_sidecar | {'device_name': name, 'bitrate': 320}
    changed = {ready / 'receiver.json', ready / 'sidecar/config.yml'}
    assert all(path.read_bytes() == data for path, data in before.items() if path not in changed)
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in changed)
    assert all(receiver._exact_link(path, target) for path, target in links.items())
    monkeypatch.setattr(receiver, '_write', lambda *_args: pytest.fail('idempotent configure wrote files'))
    assert receiver.configure(str(ready), name=name, bitrate=320)['receiver_configuration'] == 'UNCHANGED'
    monkeypatch.setattr(receiver, '_check_mpv', lambda: None)
    receiver.load_profile(ready, start='spotify')
    native = ready.parent / 'r/config.yaml'
    native.write_text(native.read_text() + '\n# Edited native YAML.\n')
    assert receiver.configure(str(ready), bitrate=320)['receiver_configuration'] == 'UNCHANGED'
    with pytest.raises(ExclusiveProfileError, match='refresh-renderer'):
        receiver.load_profile(ready, start='spotify')


@pytest.mark.parametrize('boundary', ['before-sidecar', 'between-files', 'after-profile'])
def test_configure_interruption_requires_explicit_same_settings_retry(ready, refresh_services, monkeypatch, capsys, boundary):
    installed(ready, monkeypatch)
    saved = receiver._json(ready / 'receiver.json')
    previous = receiver._json(ready / 'sidecar/config.yml')
    desired = previous | {'device_name': 'New receiver', 'bitrate': 320}
    write, writes = receiver._write, []
    def interrupted(path, value):
        writes.append(path.name)
        if boundary == 'before-sidecar':
            raise OSError('private-synthetic-error')
        if boundary == 'between-files' and path.name == 'receiver.json':
            raise KeyboardInterrupt
        write(path, value)
        if boundary == 'after-profile' and path.name == 'receiver.json':
            raise OSError('private-synthetic-error')
    monkeypatch.setattr(receiver, '_write', interrupted)
    assert cli.main(['spotify', 'receiver', '--directory', str(ready), 'configure',
                     '--name', 'New receiver', '--bitrate', '320']) == (130 if boundary == 'between-files' else 1)
    output = capsys.readouterr()
    assert not output.out and 'explicitly repeat the same --name/--bitrate settings' in output.err
    assert 'private-synthetic-error' not in output.err
    assert writes == (['config.yml'] if boundary == 'before-sidecar' else ['config.yml', 'receiver.json'])
    if boundary == 'between-files':
        assert receiver._json(ready / 'receiver.json') == saved
        assert receiver._json(ready / 'sidecar/config.yml') == desired
        with pytest.raises(ExclusiveProfileError, match='sidecar configuration changed'):
            receiver.load_profile(ready, start='spotify')
        # Stop and native renderer return do not depend on the Spotify pair.
        receiver.load_profile(ready)
        receiver.load_profile(ready, start='renderer')
        for changes in ({'name': 'New receiver'}, {'bitrate': 320}, {'name': 'Different', 'bitrate': 320}):
            with pytest.raises(ExclusiveProfileError, match='repeat the same explicit'):
                receiver.configure(str(ready), **changes)
    else:
        receiver.load_profile(ready, start='spotify')
    writes.clear()
    def record_write(path, value):
        writes.append(path.name)
        write(path, value)
    monkeypatch.setattr(receiver, '_write', record_write)
    result = receiver.configure(str(ready), name='New receiver', bitrate=320)
    expected_writes = {'before-sidecar': ['config.yml', 'receiver.json'],
                       'between-files': ['receiver.json'], 'after-profile': []}
    assert writes == expected_writes[boundary]
    assert result['receiver_configuration'] == ('UNCHANGED' if boundary == 'after-profile' else 'UPDATED')
    assert receiver._json(ready / 'receiver.json') == saved | {'device_name': 'New receiver', 'preferred_bitrate': 320}
    assert receiver._json(ready / 'sidecar/config.yml') == desired
    receiver.load_profile(ready, start='spotify')


@pytest.mark.parametrize('case', ['active', 'job', 'failed', 'current-cgroup', 'saved-cgroup',
                                  'saved-record', 'ownership', 'link', 'receipt'])
@pytest.mark.parametrize('operation', ['configure', 'output'])
def test_stopped_changes_refuse_unproven_stop_or_installation(ready, refresh_services, monkeypatch, case, operation):
    links = installed(ready, monkeypatch)
    states, busy, _inspected = refresh_services
    saved = dict.fromkeys(receiver.UNITS, '')
    if case == 'active':
        states['spotify'].update(ActiveState='active', SubState='running', MainPID='42')
    elif case == 'job':
        states['renderer']['Job'] = '41'
    elif case == 'failed':
        states['spotify']['Result'] = 'timeout'
    elif case == 'current-cgroup':
        states['renderer']['ControlGroup'] = 'current-descendant'
        busy.add('current-descendant')
    elif case == 'saved-cgroup':
        saved['spotify'] = 'saved-descendant'
        busy.add('saved-descendant')
    elif case == 'saved-record':
        saved.pop('spotify')
    elif case == 'ownership':
        states['spotify']['NeedDaemonReload'] = 'yes'
    elif case == 'link':
        next(iter(links)).unlink()
    else:
        receiver._write(ready / 'installation.json', {'state': 'INSTALLING'})
    receiver._write(ready / 'last-cgroups.json', saved)
    if operation == 'output':
        config_change(receiver._json(ready / 'receiver.json'), mpv_audio_device='alsa/hw:CARD=OTHER,DEV=1')
    before = {path: path.read_bytes() for path in ready.rglob('*') if path.is_file()}
    monkeypatch.setattr(receiver, '_write', lambda *_args: pytest.fail('unproven operation wrote files'))
    with pytest.raises(ExclusiveProfileError):
        if operation == 'output':
            receiver.refresh_renderer(str(ready), accept_output_change=True)
        else:
            receiver.configure(str(ready), name='New receiver')
    assert all(path.read_bytes() == data for path, data in before.items())


def test_configure_rejects_unrelated_edits_bad_inputs_lock_and_client(ready, refresh_services, monkeypatch, capsys):
    installed(ready, monkeypatch)
    saved = receiver._json(ready / 'receiver.json')
    original = receiver._json(ready / 'sidecar/config.yml')
    write = receiver._write
    monkeypatch.setattr(receiver, '_write', lambda *_args: pytest.fail('rejected configure wrote files'))
    for actual in (original | {'device_name': 'Unrequested'}, original | {'bitrate': 320},
                   original | {'device_name': 'New receiver', 'initial_volume': 12},
                   original | {'zeroconf_enabled': 0}, original | {'bitrate': 320.0}):
        write(ready / 'sidecar/config.yml', actual)
        with pytest.raises(ExclusiveProfileError, match='sidecar configuration'):
            receiver.configure(str(ready), name='New receiver')
    write(ready / 'sidecar/config.yml', original)
    command = ['spotify', 'receiver', '--directory', str(ready), 'configure']
    assert cli.main(command) == 1
    assert 'Choose --name and/or --bitrate explicitly' in capsys.readouterr().err
    assert cli.main(command + ['--name', 'private-name\nline']) == 1
    assert 'private-name' not in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(command + ['--bitrate', '128'])
    capsys.readouterr()
    for bitrate in (True, 320.0, '320'):
        with pytest.raises(ExclusiveProfileError, match='preferred Spotify bitrate'):
            receiver.configure(str(ready), bitrate=bitrate)
    with receiver._lock(ready, 'control.lock'):
        with pytest.raises(ExclusiveProfileError, match='Another receiver operation'):
            receiver.configure(str(ready), name='New receiver')
    # Even a dangling shadow filename is not admitted.
    shadow = ready / 'sidecar/config.yaml'
    shadow.symlink_to(ready / 'absent')
    with pytest.raises(ExclusiveProfileError, match='shadowed'):
        receiver.configure(str(ready), name='New receiver')
    shadow.unlink()
    monkeypatch.setattr(receiver, 'verify_client', lambda *_args: (None, {'binary_sha256': 'changed'}))
    with pytest.raises(ExclusiveProfileError, match='pinned client changed'):
        receiver.configure(str(ready), name='New receiver')
    assert receiver._json(ready / 'receiver.json') == saved
    assert receiver._json(ready / 'sidecar/config.yml') == original


@pytest.mark.parametrize('legacy', [False, True])
def test_output_change_cli_preserves_receiver_and_native_state(ready, refresh_services, monkeypatch, capsys, legacy):
    links = installed(ready, monkeypatch)
    saved = receiver._json(ready / 'receiver.json')
    if legacy:
        saved.pop('device_name')
    else:
        saved.update(device_name='Studio 北', preferred_bitrate=320)
    saved['future_metadata'] = {'preserve': True}
    receiver._write(ready / 'receiver.json', saved)
    receiver._write(ready / 'sidecar/config.yml', receiver._sidecar_config(ready, saved))
    for name in ('sidecar/credentials.json', 'sidecar/state.json', 'startup.json', 'last-operation.json'):
        (ready / name).write_bytes(b'private-synthetic-account-and-device-id\x00\xff\n')
    native = Path(saved['renderer_prefix'])
    (native / 'state/identity.json').write_bytes(b'private-synthetic-native-identity\x00\xff\n')
    config_change(saved, mpv_audio_device='alsa/hw:CARD=OTHER,DEV=1')
    receiver._write(ready / 'last-cgroups.json', {'renderer': 'saved-renderer', 'spotify': 'saved-spotify'})
    states, _busy, inspected = refresh_services
    states['renderer']['ControlGroup'] = 'current-renderer'
    states['spotify']['ControlGroup'] = 'current-spotify'
    before = {p: (p.stat().st_ino, p.read_bytes()) for p in [*ready.rglob('*'), *native.rglob('*')] if p.is_file()}
    previous = receiver._json(ready / 'sidecar/config.yml')
    read = receiver._read
    def no_account_reads(path, **kwargs):
        assert path.name not in {'credentials.json', 'state.json', 'identity.json'}
        return read(path, **kwargs)
    monkeypatch.setattr(receiver, '_read', no_account_reads)
    monkeypatch.setattr(exclusive, '_read', no_account_reads)
    monkeypatch.setattr(receiver, '_check_mpv', lambda: pytest.fail('unrequested mpv inspection'))
    command = ['spotify', 'receiver', '--directory', str(ready), 'refresh-renderer']
    assert cli.main(command) == 1  # A changed route always requires explicit opt-in.
    assert '--accept-output-change' in capsys.readouterr().err
    assert cli.main(command + ['--accept-output-change']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {'renderer_refresh': 'UPDATED', 'service_state': 'STOPPED',
                      'services_started': False, 'audio_output': 'UNVERIFIED'}
    actual = receiver._renderer(native, Path(saved['renderer_config']), Path(saved['renderer_unit']), saved['port'])
    assert receiver._json(ready / 'receiver.json') == saved | {'renderer': actual}
    assert receiver._json(ready / 'sidecar/config.yml') == previous | {'audio_device': 'hw:CARD=OTHER,DEV=1'}
    assert inspected == ['current-renderer', 'saved-renderer', 'current-spotify', 'saved-spotify']
    changed = {ready / 'receiver.json', ready / 'sidecar/config.yml'}
    assert all((p.stat().st_ino, p.read_bytes()) == value for p, value in before.items() if p not in changed)
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in changed)
    assert all(receiver._exact_link(path, target) for path, target in links.items())
    monkeypatch.setattr(receiver, '_write', lambda *_args: pytest.fail('idempotent output change wrote files'))
    assert receiver.refresh_renderer(str(ready), accept_output_change=True)['renderer_refresh'] == 'UNCHANGED'
    monkeypatch.setattr(receiver, '_check_mpv', lambda: None)
    _, updated = receiver.load_profile(ready, start='spotify')
    assert receiver._target(ready, updated, 'spotify') == receiver._target(ready, saved, 'spotify')
    receiver.load_profile(ready, start='renderer')


@pytest.mark.parametrize('boundary', ['before-sidecar', 'between-files', 'after-profile'])
def test_output_change_interruption_requires_explicit_same_output_retry(ready, refresh_services, monkeypatch, capsys, boundary):
    installed(ready, monkeypatch)
    saved = receiver._json(ready / 'receiver.json')
    previous = receiver._json(ready / 'sidecar/config.yml')
    desired = previous | {'audio_device': 'hw:CARD=OTHER,DEV=1'}
    config_change(saved, mpv_audio_device='alsa/' + desired['audio_device'])
    actual = receiver._renderer(Path(saved['renderer_prefix']), Path(saved['renderer_config']),
                                Path(saved['renderer_unit']), saved['port'])
    write, writes = receiver._write, []
    def interrupted(path, value):
        writes.append(path.name)
        if boundary == 'before-sidecar':
            raise OSError('private-synthetic-error')
        if boundary == 'between-files' and path.name == 'receiver.json':
            raise KeyboardInterrupt
        write(path, value)
        if boundary == 'after-profile' and path.name == 'receiver.json':
            raise OSError('private-synthetic-error')
    monkeypatch.setattr(receiver, '_write', interrupted)
    command = ['spotify', 'receiver', '--directory', str(ready), 'refresh-renderer', '--accept-output-change']
    assert cli.main(command) == (130 if boundary == 'between-files' else 1)
    output = capsys.readouterr()
    assert not output.out and 'explicitly repeat refresh-renderer --accept-output-change' in output.err
    assert 'private-synthetic-error' not in output.err
    assert writes == (['config.yml'] if boundary == 'before-sidecar' else ['config.yml', 'receiver.json'])
    if boundary != 'after-profile':
        assert receiver._json(ready / 'receiver.json') == saved
        assert receiver._json(ready / 'sidecar/config.yml') == (desired if boundary == 'between-files' else previous)
        for mode in receiver.UNITS:
            with pytest.raises(ExclusiveProfileError, match='native renderer profile changed'):
                receiver.load_profile(ready, start=mode)
        receiver.load_profile(ready)  # Off remains independent of the pending pair.
        with pytest.raises(ExclusiveProfileError, match='--accept-output-change'):
            receiver.refresh_renderer(str(ready))
    if boundary == 'between-files':
        for route in ('hw:CARD=ANOTHER,DEV=2', saved['renderer']['audio_device']):
            config_change(saved, mpv_audio_device='alsa/' + route)
            with pytest.raises(ExclusiveProfileError, match='differs from both saved and requested output'):
                receiver.refresh_renderer(str(ready), accept_output_change=True)
        config_change(saved, mpv_audio_device='alsa/' + desired['audio_device'])
    writes.clear()
    def record_write(path, value):
        writes.append(path.name)
        write(path, value)
    monkeypatch.setattr(receiver, '_write', record_write)
    result = receiver.refresh_renderer(str(ready), accept_output_change=True)
    assert writes == {'before-sidecar': ['config.yml', 'receiver.json'],
                      'between-files': ['receiver.json'], 'after-profile': []}[boundary]
    assert result['renderer_refresh'] == ('UNCHANGED' if boundary == 'after-profile' else 'UPDATED')
    assert receiver._json(ready / 'receiver.json') == saved | {'renderer': actual}
    assert receiver._json(ready / 'sidecar/config.yml') == desired
    receiver.load_profile(ready, start='spotify')


def test_output_change_rejects_unrelated_sidecar_client_and_lock(ready, refresh_services, monkeypatch):
    installed(ready, monkeypatch)
    saved = receiver._json(ready / 'receiver.json')
    previous = receiver._json(ready / 'sidecar/config.yml')
    config_change(saved, mpv_audio_device='alsa/hw:CARD=OTHER,DEV=1')
    desired = previous | {'audio_device': 'hw:CARD=OTHER,DEV=1'}
    write = receiver._write
    monkeypatch.setattr(receiver, '_write', lambda *_args: pytest.fail('rejected output change wrote files'))
    with pytest.raises(ExclusiveProfileError, match='explicit boolean opt-in'):
        receiver.refresh_renderer(str(ready), accept_output_change=1)
    for sidecar in (previous | {'device_name': 'Unrequested'}, desired | {'initial_volume': 12},
                    desired | {'zeroconf_enabled': 0}, desired | {'bitrate': 320.0},
                    previous | {'audio_device': 'hw:CARD=ANOTHER,DEV=2'}):
        write(ready / 'sidecar/config.yml', sidecar)
        with pytest.raises(ExclusiveProfileError, match='sidecar configuration'):
            receiver.refresh_renderer(str(ready), accept_output_change=True)
    write(ready / 'sidecar/config.yml', previous)
    with receiver._lock(ready, 'control.lock'):
        with pytest.raises(ExclusiveProfileError, match='Another receiver operation'):
            receiver.refresh_renderer(str(ready), accept_output_change=True)
    shadow = ready / 'sidecar/config.yaml'
    shadow.symlink_to(ready / 'absent')
    with pytest.raises(ExclusiveProfileError, match='shadowed'):
        receiver.refresh_renderer(str(ready), accept_output_change=True)
    shadow.unlink()
    monkeypatch.setattr(receiver, 'verify_client', lambda *_args: (None, {'binary_sha256': 'changed'}))
    with pytest.raises(ExclusiveProfileError, match='pinned client changed'):
        receiver.refresh_renderer(str(ready), accept_output_change=True)
    assert receiver._json(ready / 'receiver.json') == saved
    assert receiver._json(ready / 'sidecar/config.yml') == previous


@pytest.mark.parametrize('case', ['alias', 'subdevice', 'unit', 'path', 'receipt'])
def test_output_opt_in_does_not_relax_native_or_installed_admission(ready, refresh_services, monkeypatch, case):
    installed(ready, monkeypatch)
    saved = receiver._json(ready / 'receiver.json')
    config_change(saved, mpv_audio_device='alsa/hw:CARD=OTHER,DEV=1')
    if case in {'alias', 'subdevice'}:
        config_change(saved, mpv_audio_device='alsa/default' if case == 'alias' else 'alsa/hw:CARD=OTHER,DEV=1,SUBDEV=0')
    elif case == 'unit':
        Path(saved['renderer_unit']).write_text('# Customized native unit\n')
    elif case == 'path':
        config = Path(saved['renderer_config'])
        moved = config.parent / 'migrated-profile'
        moved.mkdir(mode=0o700)
        (moved / 'config.yaml').write_bytes(config.read_bytes())
        saved['renderer_config'] = str(moved / 'config.yaml')
        Path(saved['renderer_unit']).write_text(receiver.user_unit(config.parent, config=moved / 'config.yaml'))
        receiver._write(ready / 'receiver.json', saved)
    else:
        receipt = receiver._json(ready / 'installation.json')
        receiver._write(ready / 'installation.json', receipt | {'format': 2})
    before = {p: p.read_bytes() for p in ready.rglob('*') if p.is_file()}
    monkeypatch.setattr(receiver, '_write', lambda *_args: pytest.fail('inadmissible output change wrote files'))
    with pytest.raises(ExclusiveProfileError):
        receiver.refresh_renderer(str(ready), accept_output_change=True)
    assert all(p.read_bytes() == data for p, data in before.items())


@pytest.mark.parametrize('mode', ['renderer', 'spotify'])
def test_output_change_keeps_later_explicit_switch_gate(ready, refresh_services, monkeypatch, mode):
    installed(ready, monkeypatch)
    saved = receiver._json(ready / 'receiver.json')
    config_change(saved, mpv_audio_device='alsa/hw:CARD=OTHER,DEV=1')
    receiver.refresh_renderer(str(ready), accept_output_change=True)
    Services.calls, Services.failure = [], None
    monkeypatch.setattr(receiver, 'UserServices', Services)
    def busy(route):
        Services.calls.append(('probe', route))
        raise ExclusiveProfileError('Synthetic direct output still busy')
    monkeypatch.setattr(receiver, 'probe_alsa', busy)
    with pytest.raises(ExclusiveProfileError, match='output still busy'):
        receiver.switch(str(ready), mode)
    assert Services.calls == [('stop_confirmed',), ('probe', 'hw:CARD=OTHER,DEV=1'),
                              ('stop', *receiver.UNITS.values())]
