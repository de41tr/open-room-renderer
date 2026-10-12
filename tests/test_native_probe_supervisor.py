"""Bounded lifecycle regressions; no installed/native audio backend required."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from tools.native_probe_supervisor import run_probe


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux opt-in probe supervisor')
@pytest.mark.parametrize('capture_output', [True, False])
@pytest.mark.parametrize('scenario', ['SIGTERM', 'SIGHUP', 'SIGINT', 'timeout', 'abnormal', 'normal'])
def test_owned_group_cleanup(tmp_path, scenario, capture_output):
    # The test harness alone becomes a subreaper, so diagnostic cleanup can
    # reap orphaned grandchildren without altering the pytest host process.
    worker = tmp_path / 'worker.py'
    worker.write_text('''import os, signal, subprocess, sys, time
from pathlib import Path
path = Path(sys.argv[1])
(path / 'worker.pid').write_text(str(os.getpid()))
child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])
(path / 'descendant.pid').write_text(str(child.pid))
if sys.argv[2] in ('normal', 'abnormal'):
    time.sleep(.1)
    os._exit(0 if sys.argv[2] == 'normal' else 23)
time.sleep(30)
''')
    supervisor = tmp_path / 'supervisor.py'
    supervisor.write_text('''import json, os, signal, subprocess, sys
from pathlib import Path
from tools.native_probe_supervisor import run_probe
path = Path(sys.argv[1])
previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
result = {}
try:
    completed = run_probe([sys.executable, str(path / 'worker.py'), str(path), sys.argv[2]],
                          timeout=.7 if sys.argv[2] == 'timeout' else 20,
                          cwd=path, env=os.environ.copy(), capture_output=sys.argv[3] == 'True')
    result['returncode'] = completed.returncode
except BaseException as exc:
    result['exception'] = type(exc).__name__
    result['code'] = exc.code if isinstance(exc, SystemExit) else None
finally:
    result['handlers_restored'] = all(signal.getsignal(signum) == handler for signum, handler in previous.items())
    (path / 'result.json').write_text(json.dumps(result))
''')
    harness = tmp_path / 'harness.py'
    harness.write_text('''import ctypes, json, os, signal, subprocess, sys, time
from pathlib import Path
assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
path = Path(sys.argv[1])
started = time.monotonic()
process = subprocess.Popen([sys.executable, str(path / 'supervisor.py'), *sys.argv[1:]])
pids = []
try:
    deadline = time.monotonic() + 5
    while not (path / 'descendant.pid').exists():
        assert time.monotonic() < deadline and process.poll() is None, 'Worker never became ready'
        time.sleep(.01)
    pids = [int((path / name).read_text()) for name in ('worker.pid', 'descendant.pid')]
    if sys.argv[2].startswith('SIG'):
        process.send_signal(getattr(signal, sys.argv[2]))
    process.wait(timeout=4)
    deadline = time.monotonic() + 2
    # Successful cleanup must leave no running worker or descendant; reaping
    # belongs to this explicitly adopted diagnostic process, not the host init.
    while True:
        alive = []
        for pid in pids:
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                done = 0
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except ProcessLookupError:
                pass
        if not alive or time.monotonic() >= deadline:
            break
        time.sleep(.01)
    result = json.loads((path / 'result.json').read_text())
    result['remaining_owned_processes'] = len(alive)
    result['elapsed'] = time.monotonic() - started
    print(json.dumps(result))
finally:
    if (path / 'worker.pid').exists():
        try:
            os.killpg(int((path / 'worker.pid').read_text()), signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
        process.wait(timeout=3)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            done, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if not done:
            time.sleep(.01)
''')
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'PYTHONPATH': str(root) + os.pathsep + os.environ.get('PYTHONPATH', '')}
    completed = subprocess.run([sys.executable, str(harness), str(tmp_path), scenario, str(capture_output)],
                               env=env, capture_output=True, text=True, timeout=12)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result['handlers_restored'] is True
    assert result['remaining_owned_processes'] == 0
    assert result['elapsed'] < 4
    if scenario.startswith('SIG'):
        assert result['exception'] == 'SystemExit'
        assert result['code'] == 128 + getattr(signal, scenario)
    elif scenario == 'timeout':
        assert result['exception'] == 'TimeoutExpired'
    else:
        assert result['returncode'] == (23 if scenario == 'abnormal' else 0)


@pytest.mark.parametrize('failure', [KeyboardInterrupt, RuntimeError])
@pytest.mark.parametrize('group_exists', [True, False])
def test_python_interruption_cleans_group_and_restores_handlers(monkeypatch, failure, group_exists):
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    killed = []
    class Process:
        pid = 765432
        returncode = -9
        calls = 0
        def poll(self):
            return None
        def communicate(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise failure('interrupted')
            return '', ''
    def kill_group(pid, signum):
        killed.append((pid, signum))
        if not group_exists:
            raise ProcessLookupError()
    monkeypatch.setattr(subprocess, 'Popen', lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(os, 'killpg', kill_group)
    with pytest.raises(failure):
        run_probe(['synthetic'], timeout=1)
    assert killed == [(765432, signal.SIGKILL)]
    assert all(signal.getsignal(signum) == handler for signum, handler in previous.items())


def test_spawn_failure_restores_signal_handlers(monkeypatch):
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    def missing(*_args, **_kwargs):
        raise FileNotFoundError('synthetic missing executable')
    monkeypatch.setattr(subprocess, 'Popen', missing)
    with pytest.raises(FileNotFoundError):
        run_probe(['missing'], timeout=1)
    assert all(signal.getsignal(signum) == handler for signum, handler in previous.items())


def test_signal_during_spawn_is_recorded_until_owned_pid_is_available(monkeypatch):
    killed = []
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    class Process:
        pid = 765432
        def poll(self):
            return 0
        def communicate(self, **_kwargs):
            # A repeated signal during teardown is ignored until cleanup ends.
            signal.raise_signal(signal.SIGHUP)
            return '', ''
    def launch(*_args, **_kwargs):
        signal.raise_signal(signal.SIGTERM)
        return Process()
    monkeypatch.setattr(subprocess, 'Popen', launch)
    monkeypatch.setattr(os, 'killpg', lambda *args: killed.append(args))
    with pytest.raises(SystemExit) as caught:
        run_probe(['synthetic'], timeout=1)
    assert caught.value.code == 128 + signal.SIGTERM
    assert killed == [(765432, signal.SIGKILL)]
    assert all(signal.getsignal(signum) == handler for signum, handler in previous.items())


@pytest.mark.parametrize('timeout', [0, -1, float('nan'), float('inf')])
def test_deadline_must_be_positive_and_finite(timeout):
    with pytest.raises(ValueError, match='positive hard deadline'):
        run_probe(['never spawned'], timeout=timeout)
