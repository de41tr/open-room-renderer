"""In-place receiver continuity after a synthetic stopped native handover."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

import pytest

from fake_linkplay import cli, spotify_exclusive as exclusive, spotify_receiver as receiver
from fake_linkplay.native_profile import user_unit
from fake_linkplay.spotify_exclusive import ExclusiveProfileError
from test_spotify_receiver import profile, ready, startup_ready, stopped


def handover(context, monkeypatch, original='canonical'):
    root, saved, wanted, _global, _runtime, manager = context
    target = {'canonical': saved['renderer_unit'], 'relative': '../' + receiver.RENDERER_SERVICE}.get(original)
    if target:
        (wanted / receiver.RENDERER_SERVICE).symlink_to(target)
    receiver.startup(str(root), 'spotify')
    receiver.uninstall(str(root))
    old = Path(saved['renderer_prefix'])
    new = old.parent / 'n'
    shutil.copytree(old, new)
    config = new / 'config.yaml'
    raw = json.loads(config.read_text())
    raw.update(mpv_socket=str(new / 'run/mpv.sock'), state_dir=str(new / 'state'))
    config.write_text(json.dumps(raw))
    unit = new / receiver.RENDERER_SERVICE
    unit.write_text(user_unit(new))
    native = wanted.parent / receiver.RENDERER_SERVICE
    native.unlink()
    if original is None:
        shutil.copyfile(unit, native)  # The native install guide also supports an exact copied unit.
    else:
        native.symlink_to(unit)
    if original == 'canonical':
        (wanted / receiver.RENDERER_SERVICE).unlink()
        (wanted / receiver.RENDERER_SERVICE).symlink_to(unit)
    states = {mode: stopped() | {'LoadState': 'loaded' if mode == 'renderer' else 'not-found',
        'NeedDaemonReload': 'no', 'DropInPaths': '', 'FragmentPath': str(unit) if mode == 'renderer' else ''}
        for mode in receiver.UNITS}
    monkeypatch.setattr(manager, 'show', lambda _self, mode: states[mode])
    monkeypatch.setattr(receiver, 'probe_alsa', lambda *_args: pytest.fail('unrequested output probe'))
    command = manager.command
    def manager_command(self, *args, **kwargs):
        if args == ('show', '--property=Version'):
            self.calls.append(args)
            return 'Version=synthetic\n'
        return command(self, *args, **kwargs)
    monkeypatch.setattr(manager, 'command', manager_command)
    manager.calls.clear()
    options = dict(renderer_prefix=str(new), renderer_config=str(config), renderer_unit=str(unit))
    return root, saved, options, states, manager


@pytest.mark.parametrize('original', ['canonical', 'relative', None])
def test_rebind_preserves_account_files_and_native_startup_choice(startup_ready, monkeypatch, original):
    root, saved, options, _states, manager = handover(startup_ready, monkeypatch, original)
    saved.update(device_name='Studio 北', preferred_bitrate=320)
    receiver._write(root / 'receiver.json', saved)
    receiver._write(root / 'sidecar/config.yml', receiver._sidecar_config(root, saved))
    for name in ('credentials.json', 'state.json'):
        (root / 'sidecar' / name).write_bytes(b'synthetic-account-and-device-id\x00\xff')
    preserved = [*Path(saved['renderer_prefix']).rglob('*'), *Path(options['renderer_prefix']).rglob('*'),
                 *root.joinpath('sidecar').iterdir(), root / 'installation.json', root / 'last-cgroups.json']
    before = {p: (p.stat().st_ino, p.read_bytes()) for p in preserved if p.is_file()}
    read = receiver._read
    def private_read(path, **kwargs):
        assert path.name not in {'credentials.json', 'state.json'}
        return read(path, **kwargs)
    monkeypatch.setattr(receiver, '_read', private_read)
    monkeypatch.setattr(exclusive, '_read', private_read)
    result = receiver.rebind_renderer(str(root), **options)
    assert result['renderer_rebind'] == 'UPDATED' and result['installation'] == 'UNINSTALLED'
    assert result['services_started'] is False and result['sidecar_state'] == 'RETAINED_IN_PLACE'
    _, updated = receiver.load_profile(root, start='spotify')
    assert {key: value for key, value in updated.items() if key not in {*options, 'renderer', 'renderer_paths_in_units'}} == {
        key: value for key, value in saved.items() if key not in {*options, 'renderer', 'renderer_paths_in_units'}}
    assert receiver._target(root, updated, 'spotify') == receiver._target(root, saved, 'spotify')
    assert receiver._target(root, updated, 'renderer')[2:] == ['--config', options['renderer_config']]
    record = receiver._startup_record(root, updated)
    assert record['state'] == 'RESTORED'
    assert record['original_renderer'] == {'canonical': options['renderer_unit'],
        'relative': '../' + receiver.RENDERER_SERVICE}.get(original)
    assert all((p.stat().st_ino, p.read_bytes()) == value for p, value in before.items())
    assert all(call == ('show', '--property=UnitPath') for call in manager.calls)
    assert receiver.rebind_renderer(str(root), **options)['renderer_rebind'] == 'UNCHANGED'
    # Reinstall is a separate explicit command. Its receipt and strict profile
    # admission still accept the retained setup, and it never starts a source.
    assert receiver.install(str(root))['installation'] == 'INSTALLED'
    assert not any(call[0] == 'start' for call in manager.calls)


@pytest.mark.parametrize('boundary', ['second-proposal', 'final-profile'])
def test_rebind_split_write_only_resumes_requested_paths(startup_ready, monkeypatch, boundary):
    root, saved, options, _states, _manager = handover(startup_ready, monkeypatch)
    write = receiver._write
    target = receiver.DROPIN if boundary == 'second-proposal' else 'receiver.json'
    def interrupted(path, value):
        if path.name == target:
            raise OSError('synthetic interruption')
        write(path, value)
    monkeypatch.setattr(receiver, '_write', interrupted)
    with pytest.raises(OSError, match='synthetic interruption'):
        receiver.rebind_renderer(str(root), **options)
    assert receiver._json(root / 'receiver.json') == saved
    with pytest.raises(ExclusiveProfileError, match='proposals changed'):
        receiver.load_profile(root, start='spotify')
    with pytest.raises(ExclusiveProfileError, match='proposals differ'):
        receiver.rebind_renderer(str(root), **{key: saved[key] for key in options})
    # Same prefix, different supported config/unit paths must not hide a changed retry.
    migrated = Path(options['renderer_prefix']) / 'migrated-profile'
    migrated.mkdir(mode=0o700)
    config = migrated / 'config.yaml'
    shutil.copyfile(options['renderer_config'], config)
    unit = migrated / receiver.RENDERER_SERVICE
    unit.write_text(user_unit(Path(options['renderer_prefix']), config=config))
    with pytest.raises(ExclusiveProfileError, match='proposals differ'):
        receiver.rebind_renderer(str(root), **(options | {'renderer_config': str(config), 'renderer_unit': str(unit)}))
    monkeypatch.setattr(receiver, '_write', write)
    assert receiver.rebind_renderer(str(root), **options)['renderer_rebind'] == 'UPDATED'
    _, updated = receiver.load_profile(root, start='spotify')
    assert receiver._startup_record(root, updated)['original_renderer'] == options['renderer_unit']


def test_rebind_refuses_foreign_native_or_changed_startup_without_writes(startup_ready, monkeypatch):
    root, _saved, options, _states, _manager = handover(startup_ready, monkeypatch)
    wanted = startup_ready[2]
    native = wanted.parent / receiver.RENDERER_SERVICE
    native.unlink()
    native.symlink_to('/foreign/keep.service')
    monkeypatch.setattr(receiver, '_write', lambda *_args: pytest.fail('rejected rebind wrote files'))
    with pytest.raises(ExclusiveProfileError, match='exact new native unit'):
        receiver.rebind_renderer(str(root), **options)
    assert os.readlink(native) == '/foreign/keep.service'
    native.unlink()
    native.symlink_to(options['renderer_unit'])
    startup = wanted / receiver.RENDERER_SERVICE
    startup.unlink()  # Changing enabled to off is not a native upgrade.
    with pytest.raises(ExclusiveProfileError, match='changed the restored startup choice'):
        receiver.rebind_renderer(str(root), **options)
    startup.symlink_to('/foreign/keep.service')
    with pytest.raises(ExclusiveProfileError, match='unfamiliar startup link'):
        receiver.rebind_renderer(str(root), **options)
    assert os.readlink(startup) == '/foreign/keep.service'


@pytest.mark.parametrize('case', ['stale-manager', 'active', 'saved-cgroup'])
def test_rebind_requires_current_stopped_ownership(startup_ready, monkeypatch, case):
    root, _saved, options, states, manager = handover(startup_ready, monkeypatch)
    if case == 'stale-manager':
        states['renderer']['NeedDaemonReload'] = 'yes'
    elif case == 'active':
        states['renderer'].update(ActiveState='active', SubState='running', MainPID='42')
    else:
        receiver._write(root / 'last-cgroups.json', {'renderer': '', 'spotify': 'saved-descendant'})
        monkeypatch.setattr(manager, 'cgroup_empty', lambda _self, group: group != 'saved-descendant')
    monkeypatch.setattr(receiver, '_write', lambda *_args: pytest.fail('unproven rebind wrote files'))
    with pytest.raises(ExclusiveProfileError, match='stopped native handover'):
        receiver.rebind_renderer(str(root), **options)


def test_rebind_cli_rejects_new_output_without_changing_sidecar(startup_ready, monkeypatch, capsys):
    root, _saved, options, _states, manager = handover(startup_ready, monkeypatch)
    config = Path(options['renderer_config'])
    raw = json.loads(config.read_text()) | {'mpv_audio_device': 'alsa/hw:CARD=OTHER,DEV=0'}
    config.write_text(json.dumps(raw))
    command = ['spotify', 'receiver', '--directory', str(root), 'rebind-renderer']
    for key, value in options.items():
        command.extend(['--' + key.replace('_', '-'), value])
    monkeypatch.setattr(receiver, '_write', lambda *_args: pytest.fail('route change wrote files'))
    assert cli.main(command) == 1
    output = capsys.readouterr()
    assert 'same direct ALSA output' in output.err and not output.out and not manager.calls
    config.write_text('malformed: [\nsynthetic-private-config-marker')
    assert cli.main(command) == 1
    output = capsys.readouterr()
    assert 'explicitly repeat the same renderer paths' in output.err and not output.out
    assert 'synthetic-private-config-marker' not in output.err and not manager.calls
