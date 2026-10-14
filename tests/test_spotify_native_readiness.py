"""Native mpv readiness diagnostics; inert inputs only, never execute a player."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from fake_linkplay import cli, spotify_receiver as receiver
from fake_linkplay.spotify_exclusive import ExclusiveProfileError
from test_spotify_exclusive import profile
from test_spotify_receiver import ready, services


CHECK_MPV = receiver._check_mpv
MPV = Path('/usr/bin/mpv')
HINT = ("Install or repair the distribution's mpv package at /usr/bin/mpv "
        "before receiver setup or start.")
UNREADABLE = 'Native mpv is missing or unreadable. ' + HINT
UNSAFE = ('Native mpv must be a root-owned regular Linux ELF executable '
          'with safe permissions and no symlinks. ' + HINT)
SECRET = '/private/native-dependency-secret'


@pytest.mark.parametrize('failure', ['missing', 'invalid'])
def test_setup_native_error_is_fixed_and_precedes_private_state(tmp_path, monkeypatch, capsys, failure):
    root, native, client = (tmp_path / name for name in ('receiver', 'native', 'client'))
    calls = []

    def read_mpv(path, *, binary=False):
        assert path == MPV and binary is True
        calls.append(path)
        if failure == 'missing':
            raise FileNotFoundError(2, SECRET, SECRET)
        raise ExclusiveProfileError(SECRET)

    monkeypatch.setattr(receiver, '_read', read_mpv)
    monkeypatch.setattr(receiver, 'verify_client', lambda *_args: pytest.fail('client check after rejected mpv'))
    monkeypatch.setattr(receiver, '_renderer', lambda *_args: pytest.fail('native profile check after rejected mpv'))
    result = cli.main(['spotify', 'receiver', '--directory', str(root), 'setup',
                      '--renderer-prefix', str(native), '--renderer-config', str(native / 'config.yaml'),
                      '--renderer-unit', str(native / receiver.RENDERER_SERVICE),
                      '--client-dir', str(client), '--port', '3678'])
    output = capsys.readouterr()
    assert result == 1 and output.out == ''
    assert output.err == 'Receiver: ' + (UNSAFE if failure == 'invalid' else UNREADABLE) + '\n'
    assert SECRET not in output.err and calls == [MPV]
    assert not any(path.exists() for path in (root, native, client))


@pytest.mark.parametrize('kind', ['elf', 'wrapper', 'owner'])
def test_native_mpv_retains_reader_and_root_ownership_checks(tmp_path, monkeypatch, kind):
    candidate = tmp_path / 'inert-native'
    candidate.write_bytes(b'#!/bin/sh\n' if kind == 'wrapper' else b'\x7fELF')
    candidate.chmod(0o755)
    original_read, original_stat = receiver._read, Path.stat
    calls = []

    def read_mpv(path, *, binary=False):
        assert path == MPV and binary is True
        calls.append(path)
        return original_read(candidate, binary=binary)

    def native_owner(path, *args, **kwargs):
        if path == MPV:
            info = list(original_stat(candidate, *args, **kwargs))
            info[4] = 1000 if kind == 'owner' else 0
            return os.stat_result(info)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(receiver, '_read', read_mpv)
    monkeypatch.setattr(Path, 'stat', native_owner)
    if kind == 'elf':
        assert receiver._check_mpv() is None
    else:
        with pytest.raises(ExclusiveProfileError) as error:
            receiver._check_mpv()
        assert str(error.value) == UNSAFE
        assert error.value.__suppress_context__ is True
    assert calls == [MPV]


def test_missing_mpv_keeps_healthy_source_and_off_available(ready, services, monkeypatch):
    services.calls.append(('started', 'renderer'))
    previous = {'requested_mode': 'renderer', 'service_state': 'PROCESS_STARTED'}
    receiver._write(ready / 'last-operation.json', previous)
    original_read = receiver._read

    def missing_mpv(path, **kwargs):
        if path == MPV:
            raise FileNotFoundError(2, SECRET, SECRET)
        return original_read(path, **kwargs)

    monkeypatch.setattr(receiver, '_check_mpv', CHECK_MPV)
    monkeypatch.setattr(receiver, '_read', missing_mpv)
    with pytest.raises(ExclusiveProfileError) as error:
        receiver.switch(str(ready), 'spotify')
    assert str(error.value) == UNREADABLE
    assert services.calls == [('started', 'renderer')]
    assert receiver._json(ready / 'last-operation.json') == previous
    assert receiver.switch(str(ready), 'off')['service_state'] == 'STOPPED'
    assert services.calls == [('started', 'renderer'), ('stop_confirmed',)]
