#!/usr/bin/env python3
"""Run two, or explicitly three, pytest groups concurrently on one POSIX runner."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from typing import TextIO


def _signal_group(process: subprocess.Popen, signum: int) -> bool:
    # start_new_session gives each direct child its own process group, including
    # inherited native probes. Never signal an unowned group or search by name.
    try:
        os.killpg(process.pid, signum)
        return True
    except ProcessLookupError:
        return False


def _progress_tail(path: Path, previous_size: int, group: int, elapsed: float) -> int:
    """Read only a bounded changed tail through an independent file offset."""
    try:
        with path.open("rb") as source:
            size = source.seek(0, os.SEEK_END)
            if size == previous_size:
                return previous_size
            source.seek(max(0, size - 8192))
            recent = source.read(8192)
            marker = recent.rfind(b"Timeout (")
            excerpt = (recent[marker:marker + 1024] + recent[-1024:]
                       if marker >= 0 and len(recent) - marker > 2048 else recent[-2048:])
            tail = excerpt.decode("utf-8", errors="replace")
    except OSError:
        return previous_size
    print(f"\n=== Native progress elapsed={elapsed:.1f}s group={group} tail<=2048B ===",
          flush=True)
    print(tail, end="" if tail.endswith("\n") else "\n", flush=True)
    return size


def run_groups(commands: list[list[str]], log_dir: Path, timeout: float) -> int:
    """Keep all groups running after a failure; bound and clean up their trees."""
    if len(commands) not in (2, 3) or any(not command for command in commands) or not 0 < timeout < math.inf:
        raise ValueError("Two or three nonempty commands and a positive timeout are required")
    log_dir.mkdir(parents=True, exist_ok=True)
    interrupted = 0

    def interrupt(signum: int, _frame: object) -> None:
        nonlocal interrupted
        interrupted = signum

    previous = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    children: list[tuple[subprocess.Popen | None, TextIO]] = []
    timed_out = cleanup_failed = False
    started = time.monotonic()
    deadline, next_progress = started + timeout, started + 30
    progress_sizes = [0] * len(commands)
    with ExitStack() as stack:
        try:
            for index, command in enumerate(commands, 1):
                log = stack.enter_context((log_dir / f"group-{index}.log").open(
                    "w+", encoding="utf-8", errors="replace"))
                try:
                    process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                               start_new_session=True)
                except OSError as exc:
                    log.write(f"Could not start group {index}: {exc}\n")
                    process = None
                children.append((process, log))
            while any(process is not None and process.poll() is None for process, _ in children):
                now = time.monotonic()
                timed_out = now >= deadline
                if interrupted or timed_out:
                    break
                if now >= next_progress:
                    for index, (process, log) in enumerate(children):
                        if process is not None and process.poll() is None:
                            progress_sizes[index] = _progress_tail(
                                Path(log.name), progress_sizes[index], index + 1, now - started)
                    next_progress = now + 30
                time.sleep(0.05)
        finally:
            # Also clean descendants left behind by an already-exited pytest.
            for process, _ in children:
                if process is not None:
                    _signal_group(process, signal.SIGTERM)
            grace = time.monotonic() + 2
            while time.monotonic() < grace:
                for process, _ in children:
                    if process is not None:
                        process.poll()
                if not any(process is not None and _signal_group(process, 0)
                           for process, _ in children):
                    break
                time.sleep(0.05)
            for process, _ in children:
                if process is not None:
                    _signal_group(process, signal.SIGKILL)
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        cleanup_failed = True
                        print(f"Owned group {process.pid} did not exit after SIGKILL.", flush=True)
            for index, (process, log) in enumerate(children, 1):
                print(f"\n=== Native group {index} (exit {process.returncode if process else 127}) ===",
                      flush=True)
                log.flush()
                log.seek(0)
                shutil.copyfileobj(log, sys.stdout)
                sys.stdout.flush()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    if interrupted:
        print(f"Native groups interrupted by signal {interrupted}.", flush=True)
        return 128 + interrupted
    if timed_out:
        print(f"Native groups exceeded {timeout:g}s timeout.", flush=True)
        return 124
    return int(cleanup_failed or any(process is None or process.returncode
                                     for process, _ in children))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=360)
    parser.add_argument("--groups", type=int, choices=(2, 3), default=2,
                        help="Three groups are opt-in for the default suite; native lanes use two")
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("files", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    files = args.files[1:] if args.files[:1] == ["--"] else args.files
    if files.count(":::") != args.groups - 1:
        parser.error(f"Separate exactly {args.groups} explicit file groups with :::")
    groups: list[list[str]] = [[]]
    for file in files:
        if file == ":::":
            groups.append([])
        else:
            groups[-1].append(file)
    entries = [file for group in groups for file in group]
    if not all(groups) or len(set(entries)) != len(entries) or not 0 < args.timeout < math.inf:
        parser.error("Groups must be nonempty, contain unique files, and have a positive timeout")
    return run_groups([[sys.executable, "-m", "pytest", "-q", "-s", *group]
                       for group in groups], args.log_dir, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
