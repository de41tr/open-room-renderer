#!/usr/bin/env python3
"""Real user-systemd/cgroup + synthetic exclusive PCM-output acceptance.

No Spotify client, account, network provider or audio device is used. Fixture
services call the production switch/guard/stop/recover code against real systemd;
only profile admission, fixture unit paths, output probe and exec target are
replaced with explicit synthetic equivalents. Never run on a production host.
"""
from __future__ import annotations

import fcntl
import argparse
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fake_linkplay import spotify_receiver as receiver

SCRIPT = str(Path(__file__).resolve())
PYTHON = sys.executable


def event(root, kind, mode):
    with (root / 'events.jsonl').open('a') as handle:
        handle.write(json.dumps({'event': kind, 'mode': mode, 'pid': os.getpid(), 'ns': time.monotonic_ns()}) + '\n')


def output_probe(root):
    with (root / 'pcm-device.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            receiver.fail('Synthetic exclusive PCM output is owned by another process.')


def fixture(root):
    data = json.loads((root / 'fixture.json').read_text())
    receiver.UNITS = data['units']
    profile = {'renderer_prefix': str(root), 'renderer_config': str(root / 'renderer.json'),
               'client_dir': str(root / 'client'), 'renderer': {'audio_device': 'synthetic-exclusive-pcm'}}
    receiver.load_profile = lambda *_args, **_kwargs: (root, profile)
    receiver.probe_alsa = lambda _route: output_probe(root)

    class FixtureServices(receiver.UserServices):
        def command(self, *args, **kwargs):
            try:
                return super().command(*args, **kwargs)
            except receiver.ExclusiveProfileError:
                # These are explicitly synthetic units, never provider logs or
                # private product units. Keep bounded failure diagnostics.
                for unit in receiver.UNITS.values():
                    info = subprocess.run(['/usr/bin/systemctl', '--user', 'show', unit,
                        '--property=LoadState,ActiveState,SubState,Result,MainPID,ControlGroup'],
                        capture_output=True, timeout=3)
                    print('FIXTURE STATE', unit, info.stdout[:4096].decode(errors='replace'), file=sys.stderr)
                raise

        def validate(self):
            result = {mode: self.show(mode) for mode in receiver.UNITS}
            for mode, info in result.items():
                assert info['LoadState'] == 'loaded' and info['NeedDaemonReload'] == 'no', info
                assert Path(info['FragmentPath']).resolve() == root / receiver.UNITS[mode], info
                assert info['DropInPaths'] == '', info
                assert info['KillMode'] == 'control-group' and info['SendSIGKILL'] == 'yes', info
                assert info['Restart'] == 'no' and info['TimeoutStopUSec'] == '2s', info
            return result

        def wait_started(self, mode):
            expected = [os.fsencode(x) for x in [PYTHON, SCRIPT, '--writer', str(root), mode]]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                info = self.show(mode)
                if info['ActiveState'] == 'active' and int(info['MainPID']) > 0:
                    try:
                        argv = (Path('/proc') / info['MainPID'] / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
                        if argv == expected and (root / f'{mode}.ready').exists():
                            return info['InvocationID'], info['MainPID']
                    except FileNotFoundError:
                        pass
                time.sleep(.03)
            receiver.fail('Fixture exec/output startup not confirmed.')

    receiver.UserServices = FixtureServices
    return profile


def child(root, mode, foreign=False):
    stopping = False
    def stop(*_args):
        nonlocal stopping
        if not (root / 'stubborn').exists():
            stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with (root / 'pcm-device.lock').open('a') as device:
        fcntl.flock(device, fcntl.LOCK_EX | fcntl.LOCK_NB)
        event(root, 'output_open', mode)
        (root / f'{mode}.ready').write_text(str(os.getpid()))
        # Synthetic PCM, no copyrighted audio. The real child owns the output
        # until delayed signal cleanup completes, even after its parent dies.
        frame = struct.pack('<hh', 8192 if mode == 'renderer' else -8192, 0)
        with (root / f'{mode}.pcm').open('ab') as audio:
            while not stopping:
                audio.write(frame * 480)
                audio.flush()
                time.sleep(.01)
            time.sleep(.3)
        event(root, 'output_closed', mode)
        (root / f'{mode}.ready').unlink(missing_ok=True)


def writer(root, mode):
    # setsid does not escape systemd's cgroup. This is intentionally harder than
    # stopping only a parent or a Unix process group.
    proc = subprocess.Popen([PYTHON, SCRIPT, '--child', str(root), mode], start_new_session=True)
    stopping = False
    def stop(*_args):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while proc.poll() is None:
        if stopping:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            break
        time.sleep(.02)
    proc.wait()


def invoke(root, mode, expect=0):
    process = subprocess.run([PYTHON, SCRIPT, '--switch', str(root), mode],
                             capture_output=True, timeout=35)
    assert process.returncode == expect, (mode, process.returncode, process.stdout.decode(), process.stderr.decode())
    return process


def production_install_acceptance(root):
    """Real install/admission/removal with an inert native-prefix fixture."""
    manager = receiver.UserServices(root, {})
    assert all(manager.show(mode)['LoadState'] == 'not-found' for mode in receiver.UNITS), 'Refuse an existing ORR installation.'
    prefix, client, setup = [root / name for name in ('native-prefix', 'client-fixture', 'managed-setup')]
    for path in (prefix, client):
        path.mkdir(mode=0o700)
    for name in ('run', 'state', 'venv', 'venv/bin'):
        (prefix / name).mkdir(mode=0o700)
    launcher = prefix / 'venv/bin/open-room-renderer'
    launcher.write_text('#!/bin/sh\nexit 99\n')
    launcher.chmod(0o700)
    (prefix / 'installation-receipt.json').write_text('{"format":1,"mode":"installed"}')
    cfg = {'auto_spawn_mpv': True, 'mpv_socket': str(prefix / 'run/mpv.sock'),
           'state_dir': str(prefix / 'state'), 'mpv_audio_device': 'alsa/hw:CARD=SYNTHETIC,DEV=0',
           'http_api_port': 8080, 'upnp_port': 8080}
    (prefix / 'config.yaml').write_text(json.dumps(cfg))
    native = prefix / receiver.RENDERER_SERVICE
    native.write_text(receiver.user_unit(prefix))
    # Only the native package/client admission is inert. The actual production
    # setup, filesystem transaction, effective-unit checks and removal run here.
    old_mpv, old_client = receiver._check_mpv, receiver.verify_client
    receiver._check_mpv = lambda: None
    receiver.verify_client = lambda _path: (client / 'go-librespot',
                                           {'version': 'synthetic', 'binary_sha256': '0' * 64})
    units = receiver._user_units()
    native_link = units / receiver.RENDERER_SERVICE
    try:
        args = argparse.Namespace(directory=str(setup), renderer_prefix=str(prefix),
            renderer_config=str(prefix / 'config.yaml'), renderer_unit=str(native),
            client_dir=str(client), host='127.0.0.1', port=3678)
        receiver.setup(args)
        assert receiver.install(str(setup))['installation'] == 'INSTALLED'
        managed_root, profile = receiver.load_profile(setup)
        owned = receiver.UserServices(managed_root, profile)
        states = owned.validate()
        assert all(owned.stopped(info) for info in states.values())
        assert states['renderer']['Restart'] == states['spotify']['Restart'] == 'no'
        assert receiver.uninstall(str(setup))['installation'] == 'UNINSTALLED'
        restored = owned.show('renderer')
        assert restored['DropInPaths'] == '' and restored['Restart'] == 'on-failure'
        assert owned.stopped(restored)
        assert owned.show('spotify')['LoadState'] == 'not-found'
        assert (setup / 'sidecar/config.yml').exists()
        assert native_link.is_symlink() and native_link.resolve() == native
        print('PASS: production setup/install/effective-unit/uninstall against real manager; no service activation', flush=True)
    finally:
        receiver._check_mpv, receiver.verify_client = old_mpv, old_client
        # All target names were proved absent before this isolated fixture.
        # Preserve anything that no longer exactly matches our recorded links.
        targets = {units / receiver.SPOTIFY_SERVICE: setup / receiver.SPOTIFY_SERVICE,
                   units / (receiver.RENDERER_SERVICE + '.d') / receiver.DROPIN:
                       setup / (receiver.RENDERER_SERVICE + '.d') / receiver.DROPIN,
                   native_link: native}
        to_stop = []
        for mode, unit in receiver.UNITS.items():
            info = manager.show(mode)
            expected = native if mode == 'renderer' else setup / receiver.SPOTIFY_SERVICE
            if info['LoadState'] == 'loaded' and Path(info['FragmentPath']).resolve() == expected:
                to_stop.append(unit)
        if to_stop:
            manager.command('stop', *to_stop, timeout=12)
        for path, target in targets.items():
            if receiver._exact_link(path, target):
                path.unlink()
        dropdir = units / (receiver.RENDERER_SERVICE + '.d')
        if dropdir.exists():
            dropdir.rmdir()
        manager.command('daemon-reload')


def main():
    assert os.geteuid() != 0, 'Run as the disposable CI user, never root.'
    assert (Path('/sys/fs/cgroup') / 'cgroup.controllers').is_file(), 'Real cgroup v2 is required.'
    root = Path(tempfile.mkdtemp(prefix='orr-receiver-'))
    root.chmod(0o700)
    production_install_acceptance(root)
    names = {mode: f'orr-receiver-test-{os.getpid()}-{mode}.service' for mode in ('renderer', 'spotify')}
    (root / 'fixture.json').write_text(json.dumps({'units': names}))
    (root / 'run').mkdir()
    config = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'systemd/user'
    config.mkdir(parents=True, exist_ok=True)
    links = []
    profile = fixture(root)
    services = receiver.UserServices(root, profile)
    try:
        for mode, name in names.items():
            peer = names['spotify' if mode == 'renderer' else 'renderer']
            body = f'''[Unit]
Description=ORR disposable synthetic receiver acceptance
Conflicts={peer}
'''
            if mode == 'spotify':
                body += f'After={peer}\n'
            body += f'''[Service]
Type=simple
ExecStart={PYTHON} {SCRIPT} --guard {root} {mode}
KillMode=control-group
SendSIGKILL=yes
Restart=no
TimeoutStopSec=2
Slice=app.slice
UMask=0077
StandardOutput=null
StandardError=journal
'''
            (root / name).write_text(body)
            link = config / name
            assert not os.path.lexists(link)
            link.symlink_to(root / name)
            links.append(link)
        services.command('daemon-reload')
        services.validate()
        for mode in ('renderer', 'spotify', 'renderer', 'off'):
            invoke(root, mode)
        events = [json.loads(line) for line in (root / 'events.jsonl').read_text().splitlines()]
        assert [e['event'] for e in events] == ['output_open', 'output_closed'] * 3, events
        assert all((root / f'{m}.pcm').stat().st_size > 0 for m in names)
        assert all(services.stopped(info) for info in services.validate().values())
        print('PASS: independent bidirectional CLI switch, real cgroup descendants and ordered synthetic PCM ownership', flush=True)

        # An unrelated output owner is never signalled or replaced.
        foreign = subprocess.Popen([PYTHON, SCRIPT, '--child', str(root), 'foreign'])
        try:
            until = time.monotonic() + 3
            while not (root / 'foreign.ready').exists() and time.monotonic() < until:
                time.sleep(.01)
            assert (root / 'foreign.ready').exists()
            invoke(root, 'spotify', expect=1)
            assert foreign.poll() is None
            assert all(services.stopped(info) for info in services.validate().values())
        finally:
            foreign.terminate()
            foreign.wait(timeout=3)
        print('PASS: occupied synthetic output fails closed and leaves the foreign owner untouched', flush=True)

        invoke(root, 'renderer')
        a = subprocess.Popen([PYTHON, SCRIPT, '--switch', str(root), 'spotify'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(.05)
        b = subprocess.Popen([PYTHON, SCRIPT, '--switch', str(root), 'renderer'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert sorted([a.wait(timeout=35), b.wait(timeout=35)]) == [0, 1]
        invoke(root, 'off')
        print('PASS: concurrent opposite switches have one authority', flush=True)

        # A stubborn escaped-process-group child forces systemd's timeout/KILL.
        # Even when the kernel finally frees output, the failed stop does not
        # authorize another start. Explicit off-only recovery is required.
        invoke(root, 'renderer')
        (root / 'stubborn').touch()
        invoke(root, 'spotify', expect=1)
        (root / 'stubborn').unlink()
        assert not (root / 'spotify.ready').exists()
        receiver.recover(str(root))
        invoke(root, 'spotify')
        invoke(root, 'renderer')
        invoke(root, 'off')
        print('PASS: failed whole-cgroup stop blocks competing start; explicit off-only recovery and return work', flush=True)
        print(json.dumps({'evidence': 'REAL_SYSTEMD_CGROUP_SYNTHETIC_PCM', 'spotify_client_executed': False,
                          'account': 'PENDING', 'physical_audio': 'PENDING', 'provider_policy': 'PENDING'}))
    finally:
        try:
            services.command('stop', *names.values(), timeout=12)
            failed = [unit for mode, unit in names.items()
                      if services.show(mode)['ActiveState'] == 'failed']
            if failed:
                services.command('reset-failed', *failed)
        finally:
            for link in links:
                if link.is_symlink() and os.readlink(link) == str(root / link.name):
                    link.unlink()
            services.command('daemon-reload')


if __name__ == '__main__':
    if len(sys.argv) > 1:
        action, root, mode = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
        if action == '--child':
            child(root, mode)
        elif action == '--writer':
            writer(root, mode)
        else:
            fixture(root)
            if action == '--guard':
                original = os.execv
                def execute(_path, argv):
                    assert argv == receiver._target(root, receiver.load_profile(root)[1], mode)
                    (root / f'{mode}.ready').unlink(missing_ok=True)
                    original(PYTHON, [PYTHON, SCRIPT, '--writer', str(root), mode])
                os.execv = execute
                receiver.run_guard(str(root), mode)
            elif action == '--switch':
                try:
                    print(json.dumps(receiver.switch(str(root), mode)))
                except receiver.ExclusiveProfileError as exc:
                    print(str(exc), file=sys.stderr)
                    sys.exit(1)
    else:
        main()
