"""Synthetic pinned inputs; no provider daemon or real shared library is loaded."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile

import pytest

from fake_linkplay import spotify_client as client
from fake_linkplay.spotify_exclusive import ExclusiveProfileError


@pytest.fixture
def pinned(monkeypatch, tmp_path):
    data = b'SYNTHETIC CLIENT, NEVER EXECUTED\n' * 4
    digest = hashlib.sha256(data).hexdigest()
    machine = client.platform.machine().lower()
    monkeypatch.setitem(client.BINARY_SHA256, machine, digest)
    monkeypatch.setattr(client, 'elf_abi', lambda *_args: {'interpreter': '/synthetic/ld-linux.so'})
    binary = tmp_path / 'go-librespot'
    binary.write_bytes(data)
    binary.chmod(0o700)
    receipt = {'format': 1, 'version': client.UPSTREAM_VERSION, 'commit': client.UPSTREAM_COMMIT,
               'archive_sha256': client.ARCHIVES[machine][1], 'binary_sha256': digest}
    (tmp_path / 'client-receipt.json').write_text(json.dumps(receipt))
    return tmp_path, binary, data


def test_loader_uses_verified_fd_and_clean_environment(pinned, monkeypatch):
    root, binary, data = pinned
    monkeypatch.setenv('LD_PRELOAD', '/private/injected.so')
    monkeypatch.setenv('LD_LIBRARY_PATH', '/private/libraries')
    monkeypatch.setenv('LD_AUDIT', '/private/audit.so')
    monkeypatch.setenv('GLIBC_TUNABLES', 'private=setting')
    calls = []

    def run(argv, **kwargs):
        fd, = kwargs['pass_fds']
        assert argv == ['/synthetic/ld-linux.so', '--list', f'/proc/self/fd/{fd}']
        assert kwargs['env'] == {'LC_ALL': 'C'}
        assert kwargs['stdin'] == subprocess.DEVNULL and kwargs['timeout'] == 5
        # Replacing the pathname after validation must not replace the input
        # being inspected by the loader. No fixture executable is invoked.
        binary.unlink()
        binary.write_text('UNVERIFIED REPLACEMENT')
        assert Path(argv[-1]).read_bytes() == data
        calls.append(fd)
        return subprocess.CompletedProcess(argv, 0, '\tlibasound.so.2 => /lib/libasound.so.2 (0x1234)\n', '')

    monkeypatch.setattr(client.subprocess, 'run', run)
    assert client.verify_client(root)[0] == binary
    assert len(calls) == 1
    with pytest.raises(OSError):
        os.fstat(calls[0])


@pytest.mark.parametrize('kind', ['pin', 'abi'])
def test_pin_and_abi_rejection_precede_loader(pinned, monkeypatch, kind):
    root, binary, _ = pinned
    monkeypatch.setattr(client.subprocess, 'run', lambda *_args, **_kwargs: pytest.fail('loader ran'))
    if kind == 'pin':
        binary.write_bytes(b'unrecognized executable')
    else:
        monkeypatch.setattr(client, 'elf_abi', lambda *_args: client.fail('ABI mismatch'))
    with pytest.raises(ExclusiveProfileError, match='changed|ABI mismatch'):
        client.verify_client(root)


@pytest.mark.parametrize('code,stdout,stderr,expected', [
    (0, '\tlibasound.so.2 => not found\n', '', 'Debian 13/Ubuntu 24.04: libasound2t64'),
    (127, '', '/private/client: error while loading shared libraries: libasound.so.2: cannot open shared object file: No such file or directory', 'Debian 12: libasound2'),
    (0, '\tlibasound.so.2 => /lib/libasound.so.2 (0x1234)\n', "/private/client: /private/libc.so.6: version `GLIBC_2.17' not found (required by /private/client)", 'GLIBC_2.17'),
    (0, '\tlibc.so.6 => not found\n', '', 'libc.so.6'),
    (1, '', 'loader failed at /private/client', 'could not be resolved'),
    (0, '', '', 'could not be resolved'),
])
def test_missing_or_incompatible_dependencies_fail_without_raw_output(pinned, monkeypatch, code, stdout, stderr, expected):
    monkeypatch.setattr(client.subprocess, 'run', lambda argv, **_kwargs: subprocess.CompletedProcess(argv, code, stdout, stderr))
    with pytest.raises(ExclusiveProfileError, match=expected) as caught:
        client.verify_client(pinned[0])
    assert '/private/' not in str(caught.value)
    assert 'Do not copy shared libraries' in str(caught.value)


@pytest.mark.parametrize('error', [OSError('private detail'), subprocess.TimeoutExpired('loader', 5)])
def test_failed_loader_inspection_is_not_readiness(pinned, monkeypatch, error):
    def run(*_args, **_kwargs):
        raise error
    monkeypatch.setattr(client.subprocess, 'run', run)
    with pytest.raises(ExclusiveProfileError, match='inspection did not complete') as caught:
        client.verify_client(pinned[0])
    assert 'private detail' not in str(caught.value)


def test_installation_remains_static(pinned, monkeypatch):
    root, _, data = pinned
    archive = root / 'client.tar.gz'
    with tarfile.open(archive, 'w:gz') as bundle:
        entry = tarfile.TarInfo('go-librespot')
        entry.size = len(data)
        bundle.addfile(entry, io.BytesIO(data))
    machine = client.platform.machine().lower()
    spec = client.ARCHIVES[machine]
    monkeypatch.setitem(client.ARCHIVES, machine, (spec[0], hashlib.sha256(archive.read_bytes()).hexdigest(), *spec[2:]))
    monkeypatch.setattr(client.subprocess, 'run', lambda *_args, **_kwargs: pytest.fail('installer ran a process'))
    receipt = client.install_client(str(archive), str(root / 'installed'))
    assert receipt['client_executed'] is False
    assert (root / 'installed/go-librespot').read_bytes() == data
