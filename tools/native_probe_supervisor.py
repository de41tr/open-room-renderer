"""Standard-library-only lifecycle guard for opt-in native acceptance probes.

Workers and their native descendants must inherit the created session/process
group. This helper is intentionally outside the production playback code.
"""
from __future__ import annotations

import math
import os
import signal
import subprocess
import time


def run_probe(command, *, timeout, cwd=None, env=None, capture_output=True):
    """Return CompletedProcess, cleaning the owned group on every exit path.

    Call on the Linux main thread. Signals are recorded while spawning, so an
    interruption cannot abandon a detached child before Popen returns its PID.
    Neither worker nor descendants inherit a blocked signal mask.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('A positive hard deadline is required')
    handled = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {}
    process = None
    pending_signal = None
    stdout = stderr = None

    def interrupted(signum, _frame):
        nonlocal pending_signal
        pending_signal = signum

    try:
        for signum in handled:
            previous[signum] = signal.signal(signum, interrupted)
        process = subprocess.Popen(command, cwd=cwd, env=env, text=True,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None, start_new_session=True)
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if pending_signal is not None:
                raise SystemExit(128 + pending_signal)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                if capture_output:
                    process.communicate(timeout=min(.2, remaining))
                else:
                    process.wait(timeout=min(.2, remaining))
            except subprocess.TimeoutExpired:
                pass
            # A failed worker can leave grandchildren holding its pipes open.
            # Poll its PID, too: EOF alone cannot tell us it has already died.
        if pending_signal is not None:
            raise SystemExit(128 + pending_signal)
    finally:
        try:
            # A second termination request must not interrupt cleanup itself.
            for signum in previous:
                signal.signal(signum, signal.SIG_IGN)
            if process is not None:
                # Normal or abnormal worker exit does not prove descendants
                # exited. Target only the new group, even after its leader died.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if capture_output:
                    stdout, stderr = process.communicate(timeout=5)
                else:
                    process.wait(timeout=5)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
