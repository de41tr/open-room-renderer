"""Same-runner scheduling preserves both groups and owns every cleanup signal."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import shlex
import subprocess
import sys
import time

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run-native-groups.py"
pytestmark = pytest.mark.skipif(os.name != "posix", reason="Native CI uses POSIX process groups")


@pytest.fixture
def runner():
    spec = importlib.util.spec_from_file_location("native_group_runner", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def command(code):
    return [sys.executable, "-c", code]


def test_groups_overlap_and_retain_complete_separate_logs(runner, tmp_path, capsys):
    commands = []
    for index in range(2):
        commands.append(command(
            "from pathlib import Path; import sys, time\n"
            f"Path({str(tmp_path / str(index))!r}).touch()\n"
            "deadline = time.monotonic() + 3\n"
            f"while not Path({str(tmp_path / str(1-index))!r}).exists():\n"
            " assert time.monotonic() < deadline, 'Other group never started'\n"
            " time.sleep(.01)\n"
            f"print('stdout-{index}'); print('stderr-{index}', file=sys.stderr)\n"))
    assert runner.run_groups(commands, tmp_path / "logs", 5) == 0
    output = capsys.readouterr().out
    for index in range(2):
        log = (tmp_path / "logs" / f"group-{index + 1}.log").read_text()
        assert f"stdout-{index}" in log and f"stderr-{index}" in log
        assert log in output
        assert f"Native group {index + 1} (exit 0)" in output


@pytest.mark.parametrize("failed_group", [0, 1])
def test_one_failure_does_not_skip_or_terminate_other_group(runner, tmp_path, capsys, failed_group):
    commands = [command("import time; time.sleep(.15); print('finished-success')")] * 2
    commands[failed_group] = command("print('finished-failure'); raise SystemExit(7)")
    assert runner.run_groups(commands, tmp_path, 3) == 1
    output = capsys.readouterr().out
    assert "finished-success" in output and "finished-failure" in output
    assert "(exit 7)" in output and "(exit 0)" in output


def test_launch_failure_still_runs_other_group(runner, tmp_path, capsys):
    assert runner.run_groups([[str(tmp_path / "missing")], command("print('other-completed')")],
                             tmp_path, 3) == 1
    output = capsys.readouterr().out
    assert "Could not start group 1" in output and "other-completed" in output


def tree_commands(tmp_path, count):
    script = tmp_path / "tree.py"
    script.write_text(
        "from pathlib import Path\nimport os, signal, subprocess, sys, time\n"
        "root = Path(sys.argv[1])\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "if len(sys.argv) == 2:\n"
        " child = subprocess.Popen([sys.executable, __file__, str(root), 'descendant'])\n"
        " while not root.with_suffix('.descendant').exists(): time.sleep(.01)\n"
        " root.with_suffix('.parent').write_text(str(os.getpid()))\n"
        " print('tree-ready-' + root.name, flush=True)\n"
        "else:\n"
        " root.with_suffix('.descendant').write_text(str(os.getpid()))\n"
        "time.sleep(60)\n")
    return [[sys.executable, str(script), str(tmp_path / f"tree-{i}")] for i in range(count)]


@pytest.fixture
def trees(tmp_path):
    return tree_commands(tmp_path, 2)


def assert_trees_stopped(tmp_path, count=2):
    pids = list(tmp_path.glob("tree-*.parent")) + list(tmp_path.glob("tree-*.descendant"))
    assert len(pids) == count * 2, "All groups and their descendants must have started"
    for pid_file in pids:
        pid = int(pid_file.read_text())
        # Container PID 1 may defer orphan zombie reaping; a zombie cannot run.
        state = Path(f"/proc/{pid}/stat")
        deadline = time.monotonic() + 2
        while state.exists():
            try:
                if state.read_text().split()[2] == "Z":
                    break
            except FileNotFoundError:
                break
            assert time.monotonic() < deadline, f"Owned process {pid} remains live"
            time.sleep(.01)


def test_timeout_kills_owned_trees_and_preserves_unrelated_process(runner, trees, tmp_path, capsys):
    unrelated = subprocess.Popen(command("import time; time.sleep(60)"), start_new_session=True)
    try:
        start = time.monotonic()
        assert runner.run_groups(trees, tmp_path / "logs", 2) == 124
        assert time.monotonic() - start < 8
        assert unrelated.poll() is None
        assert_trees_stopped(tmp_path)
        output = capsys.readouterr().out
        assert "tree-ready-tree-0" in output and "tree-ready-tree-1" in output
        assert "exceeded 2s timeout" in output
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=3)


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_interrupt_cleans_both_trees_and_prints_both_logs(trees, tmp_path, signum):
    code = ("import json, runpy, sys; from pathlib import Path; "
            "ns = runpy.run_path(sys.argv[1]); "
            "raise SystemExit(ns['run_groups'](json.loads(sys.argv[2]), Path(sys.argv[3]), 30))")
    process = subprocess.Popen([sys.executable, "-c", code, str(SCRIPT), json.dumps(trees),
                                str(tmp_path / "logs")], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while len(list(tmp_path.glob("tree-*.parent"))) != 2:
            assert time.monotonic() < deadline
            assert process.poll() is None
            time.sleep(.02)
        process.send_signal(signum)
        output, _ = process.communicate(timeout=8)
        assert process.returncode == 128 + signum, output
        assert "tree-ready-tree-0" in output and "tree-ready-tree-1" in output
        assert_trees_stopped(tmp_path)
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=8)


def test_workflow_keeps_explicit_native_groups_and_existing_budget():
    workflow = yaml.safe_load((ROOT / ".github/workflows/renderer-tests.yml").read_text())
    jobs = workflow["jobs"]
    native = jobs["renderer-tests"]
    assert native["timeout-minutes"] == 15
    assert jobs["python-floor"]["timeout-minutes"] == 8
    assert native["strategy"]["matrix"]["runner"] == ["ubuntu-24.04", "ubuntu-24.04-arm"]
    lane = next(step for step in native["steps"] if step["name"] ==
                "Run explicit real libmpv null and digital-PCM lanes")
    assert lane["run"].count(":::") == 1
    assert "--timeout 360" in lane["run"]
    stability = next(step for step in native["steps"] if "across repeated playback" in step["name"])
    assert stability["timeout-minutes"] == 6
    assert stability["env"]["ORR_TEST_UNIX_STABILITY_SECONDS"] == "180"


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_timeout_must_be_positive_and_finite(runner, tmp_path, timeout):
    with pytest.raises(ValueError):
        runner.run_groups([command("pass"), command("pass")], tmp_path, timeout)


def test_floor_keeps_all_default_files_and_existing_step_budget():
    workflow = yaml.safe_load((ROOT / ".github/workflows/renderer-tests.yml").read_text())
    floor = workflow["jobs"]["python-floor"]
    step = next(step for step in floor["steps"] if step["name"] ==
                "Run default unit and local-client suite on Python 3.11")
    assert step["timeout-minutes"] == 3
    assert "--timeout 165" in step["run"]
    assert "tests -rs --ignore=tests/test_queue_cli.py --ignore=tests/test_playlist_cli.py" in step["run"]
    assert "::: tests/test_queue_cli.py tests/test_playlist_cli.py" in step["run"]
    assert "--ignore=tests/test_favorite_play_cli.py" in step["run"]
    assert "--ignore=tests/test_favorites_cli.py" in step["run"]
    assert step["run"].rstrip().endswith("tests/test_favorites_cli.py")
    assert "::: tests/test_queue_cli.py tests/test_playlist_cli.py tests/test_favorite_play_cli.py" in step["run"]
    assert floor["runs-on"] == "ubuntu-24.04"
    assert floor["timeout-minutes"] == 8


def test_three_groups_overlap_with_separate_complete_logs(runner, tmp_path, capsys):
    commands = []
    ready = [str(tmp_path / str(index)) for index in range(3)]
    for index in range(3):
        commands.append(command(
            "from pathlib import Path; import sys, time\n"
            f"Path({ready[index]!r}).touch()\n"
            "deadline = time.monotonic() + 3\n"
            f"while not all(Path(p).exists() for p in {ready!r}):\n"
            " assert time.monotonic() < deadline, 'Not all groups started'\n"
            " time.sleep(.01)\n"
            f"print('stdout-{index}'); print('stderr-{index}', file=sys.stderr)\n"))
    assert runner.run_groups(commands, tmp_path / "logs", 5) == 0
    output = capsys.readouterr().out
    for index in range(3):
        log = (tmp_path / "logs" / f"group-{index + 1}.log").read_text()
        assert f"stdout-{index}" in log and f"stderr-{index}" in log
        assert log in output
        assert f"Native group {index + 1} (exit 0)" in output


@pytest.mark.parametrize("failed_group", range(3))
@pytest.mark.parametrize("launch_failure", [False, True])
def test_three_group_failure_retains_both_other_groups(runner, tmp_path, capsys, failed_group, launch_failure):
    commands = [command(f"import time; time.sleep(.15); print('finished-{index}')")
                for index in range(3)]
    commands[failed_group] = ([str(tmp_path / "missing")] if launch_failure else
                             command("print('finished-failure'); raise SystemExit(7)"))
    assert runner.run_groups(commands, tmp_path / "logs", 3) == 1
    output = capsys.readouterr().out
    for index in range(3):
        log = (tmp_path / "logs" / f"group-{index + 1}.log").read_text()
        assert log in output
        if index == failed_group:
            assert (f"Could not start group {index + 1}" if launch_failure else "finished-failure") in log
            assert f"Native group {index + 1} (exit {127 if launch_failure else 7})" in output
        else:
            assert f"finished-{index}" in log
            assert f"Native group {index + 1} (exit 0)" in output


def test_three_group_shared_timeout_cleans_owned_trees_only(runner, tmp_path, capsys):
    commands = tree_commands(tmp_path, 3)
    unrelated = subprocess.Popen(command("import time; time.sleep(60)"), start_new_session=True)
    try:
        start = time.monotonic()
        assert runner.run_groups(commands, tmp_path / "logs", 2) == 124
        assert time.monotonic() - start < 8
        assert unrelated.poll() is None
        assert_trees_stopped(tmp_path, 3)
        output = capsys.readouterr().out
        for index in range(3):
            assert f"tree-ready-tree-{index}" in output
        assert "exceeded 2s timeout" in output
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=3)


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_interrupt_cleans_three_owned_trees_only(tmp_path, signum):
    commands = tree_commands(tmp_path, 3)
    code = ("import json, runpy, sys; from pathlib import Path; "
            "ns = runpy.run_path(sys.argv[1]); "
            "raise SystemExit(ns['run_groups'](json.loads(sys.argv[2]), Path(sys.argv[3]), 30))")
    unrelated = subprocess.Popen(command("import time; time.sleep(60)"), start_new_session=True)
    process = subprocess.Popen([sys.executable, "-c", code, str(SCRIPT), json.dumps(commands),
                                str(tmp_path / "logs")], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while len(list(tmp_path.glob("tree-*.parent"))) != 3:
            assert time.monotonic() < deadline
            assert process.poll() is None
            time.sleep(.02)
        process.send_signal(signum)
        output, _ = process.communicate(timeout=8)
        assert process.returncode == 128 + signum, output
        for index in range(3):
            assert f"tree-ready-tree-{index}" in output
            log = (tmp_path / "logs" / f"group-{index + 1}.log").read_text()
            assert log in output
        assert unrelated.poll() is None
        assert_trees_stopped(tmp_path, 3)
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=8)
        unrelated.terminate()
        unrelated.wait(timeout=3)


@pytest.mark.parametrize("count", [0, 1, 4])
def test_runner_rejects_unbounded_or_single_group_counts(runner, tmp_path, count):
    with pytest.raises(ValueError):
        runner.run_groups([command("pass")] * count, tmp_path, 1)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("count", [2, 3])
def test_runner_rejects_empty_commands(runner, tmp_path, count):
    with pytest.raises(ValueError):
        runner.run_groups([command("pass")] * (count - 1) + [[]], tmp_path, 1)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_three_group_timeout_must_be_positive_and_finite(runner, tmp_path, timeout):
    with pytest.raises(ValueError):
        runner.run_groups([command("pass")] * 3, tmp_path, timeout)


@pytest.mark.parametrize("count,options", [(2, []), (2, ["--groups", "2"]), (3, ["--groups", "3"])])
def test_cli_preserves_two_group_default_and_explicit_three_opt_in(runner, tmp_path, monkeypatch, count, options):
    files = [f"tests/test_{index}.py" for index in range(count)]
    arguments = []
    for file in files:
        if arguments:
            arguments.append(":::")
        arguments.append(file)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), *options, "--timeout", "165", "--log-dir", str(tmp_path), "--", *arguments])
    calls = []
    def run(commands, log_dir, timeout):
        calls.append((commands, log_dir, timeout))
        return 7
    monkeypatch.setattr(runner, "run_groups", run)
    assert runner.main() == 7
    assert calls == [([[sys.executable, "-m", "pytest", "-q", "-s", file] for file in files], tmp_path, 165)]


@pytest.mark.parametrize("arguments", [
    ["a.py", ":::"], [":::", "b.py"], ["a.py", ":::", "a.py"],
    ["a.py", ":::", "b.py", ":::", "c.py"],
    ["--groups", "3", "--", "a.py", ":::", "b.py"],
    ["--groups", "3", "--", "a.py", ":::", ":::", "c.py"],
    ["--groups", "3", "--", "a.py", ":::", "b.py", ":::"],
    ["--groups", "3", "--", "a.py", ":::", "b.py", ":::", "a.py"],
    ["--groups", "4", "--", "a.py", ":::", "b.py", ":::", "c.py", ":::", "d.py"],
])
def test_cli_rejects_incomplete_duplicate_or_unbounded_groups(runner, tmp_path, monkeypatch, arguments):
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--log-dir", str(tmp_path), *arguments])
    monkeypatch.setattr(runner, "run_groups", lambda *_args: pytest.fail("invalid groups reached execution"))
    with pytest.raises(SystemExit) as caught:
        runner.main()
    assert caught.value.code == 2
    assert not list(tmp_path.iterdir())


def workflow_groups(step):
    arguments = shlex.split(step["run"].split("scripts/run-native-groups.py", 1)[1].replace("\\\n", ""))
    split = arguments.index("--")
    groups = [[]]
    for argument in arguments[split + 1:]:
        if argument == ":::":
            groups.append([])
        else:
            groups[-1].append(argument)
    return arguments[:split], groups


def test_both_default_lanes_partition_every_file_once_and_keep_budgets():
    jobs = yaml.safe_load((ROOT / ".github/workflows/renderer-tests.yml").read_text())["jobs"]
    partitions = []
    for job_name, timeout in (("python-floor", "165"), ("renderer-tests", "900")):
        step = next(step for step in jobs[job_name]["steps"]
                    if step["name"].startswith("Run default unit and local-client suite"))
        options, groups = workflow_groups(step)
        assert options[options.index("--groups") + 1] == "3"
        assert options[options.index("--timeout") + 1] == timeout
        assert len(groups) == 3 and all(groups)
        assert groups[0][:2] == ["tests", "-rs"]
        ignored = [entry.removeprefix("--ignore=") for entry in groups[0][2:]]
        assert all(entry.startswith("--ignore=tests/test_") for entry in groups[0][2:])
        explicit = groups[1] + groups[2]
        assert len(ignored) == len(set(ignored))
        assert len(explicit) == len(set(explicit))
        assert set(ignored) == set(explicit)
        all_files = {str(path.relative_to(ROOT)) for path in (ROOT / "tests").rglob("test_*.py")}
        remainder = all_files - set(ignored)
        assert remainder and set(explicit) <= all_files
        selected = [remainder, set(groups[1]), set(groups[2])]
        assert set.union(*selected) == all_files
        for index, group in enumerate(selected):
            assert not any(group & other for other in selected[index + 1:])
        partitions.append(groups)
    assert partitions[0] == partitions[1]
    assert jobs["python-floor"]["timeout-minutes"] == 8
    assert jobs["renderer-tests"]["timeout-minutes"] == 15


def test_native_opt_in_remains_exactly_two_unchanged_groups():
    native = yaml.safe_load((ROOT / ".github/workflows/renderer-tests.yml").read_text())["jobs"]["renderer-tests"]
    step = next(step for step in native["steps"] if step["name"] ==
                "Run explicit real libmpv null and digital-PCM lanes")
    options, groups = workflow_groups(step)
    assert "--groups" not in options
    assert options[options.index("--timeout") + 1] == "360"
    assert step["timeout-minutes"] == 7
    assert groups == [
        [f"tests/test_libmpv_{name}.py" for name in (
            "integration", "gain", "dsp", "recovery", "dsp_restore", "formats", "live_metadata",
            "live_hls", "invalid_media", "repeat_queue", "shuffle_queue")],
        [f"tests/test_libmpv_{name}.py" for name in (
            "load_options", "station_cache", "load_cleanup", "station_playlist_cache",
            "stall_recovery", "queue_staging", "queue_navigation_intent", "favorites", "queue_cli",
            "web_controls", "standard_dmr", "source_ownership", "admitted_load_root",
            "station_refresh", "transport_uncertainty")],
    ]
    stability = next(step for step in native["steps"] if "across repeated playback" in step["name"])
    assert stability["timeout-minutes"] == 6
    assert stability["env"] == {"ORR_TEST_UNIX_STABILITY": "1", "ORR_TEST_UNIX_STABILITY_SECONDS": "180"}
