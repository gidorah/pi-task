from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def lock_env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "commands.jsonl"
    agent_dir = tmp_path / "pi-agent"
    agent_dir.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    fake = bin_dir / "fake-command"
    fake.write_text(
        r"""#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

name = Path(sys.argv[0]).name
with Path(os.environ["FAKE_COMMAND_LOG"]).open("a") as log:
    print(json.dumps([name, *sys.argv[1:]]), file=log)

if name == "pi":
    if "--list-models" in sys.argv:
        print(os.environ.get("FAKE_MODELS", "provider model context\nacme rocket 128K"))
        raise SystemExit(0)
    if "--session" in sys.argv:
        raise SystemExit(0)

    ready = os.environ.get("FAKE_PI_READY")
    gate = os.environ.get("FAKE_PI_GATE")
    if ready:
        Path(ready).write_text(str(os.getpid()))
    if gate:
        deadline = time.time() + 30
        while not Path(gate).exists():
            if time.time() > deadline:
                raise SystemExit(99)
            time.sleep(0.05)

    cwd = Path.cwd().resolve()
    agent_dir = Path(os.environ["PI_CODING_AGENT_DIR"])
    session_id = os.environ.get(
        "FAKE_SESSION_ID",
        "019f0000-1111-2222-3333-444455556666",
    )
    timestamp = os.environ.get("FAKE_SESSION_TIMESTAMP", "2030-01-07T09:00:00.000Z")
    safe = "--" + str(cwd).lstrip("/").replace("/", "-").replace("\\", "-").replace(":", "-") + "--"
    session_dir = agent_dir / "sessions" / safe
    session_dir.mkdir(parents=True, exist_ok=True)
    file_ts = timestamp.replace(":", "-").replace(".", "-")
    session_path = session_dir / f"{file_ts}_{session_id}.jsonl"
    header = {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": timestamp,
        "cwd": str(cwd),
    }
    assistant = {
        "role": "assistant",
        "content": [{"type": "text", "text": "done"}],
        "provider": "acme",
        "model": "rocket",
        "usage": {
            "input": 11,
            "output": 7,
            "cacheRead": 3,
            "cacheWrite": 1,
            "totalTokens": 22,
            "cost": {
                "input": 0.01,
                "output": 0.02,
                "cacheRead": 0.0,
                "cacheWrite": 0.0,
                "total": 0.03,
            },
        },
        "stopReason": "stop",
        "timestamp": 1,
    }
    user_message = {
        "type": "message",
        "message": {"role": "user", "content": "hi", "timestamp": 1},
    }
    session_path.write_text(
        json.dumps(header)
        + "\n"
        + json.dumps(user_message)
        + "\n"
        + json.dumps({"type": "message", "message": assistant})
        + "\n"
    )
    print(json.dumps(header), flush=True)
    print(json.dumps({"type": "agent_start"}), flush=True)
    print(json.dumps({"type": "message_end", "message": assistant}), flush=True)
    print(json.dumps({"type": "agent_end", "messages": [assistant]}), flush=True)
    raise SystemExit(0)

if name == "systemd-analyze":
    print("  Original form: " + sys.argv[-1])
    print("Normalized form: *-*-* 09:00:00")
    print("    Next elapse: Mon 2030-01-07 09:00:00 UTC")
    print("   Iteration #2: Tue 2030-01-08 09:00:00 UTC")
    print("   Iteration #3: Wed 2030-01-09 09:00:00 UTC")
    raise SystemExit(0)
if name == "systemctl":
    if "show-environment" in sys.argv:
        environment = {
            "PATH": os.environ["FAKE_MANAGER_PATH"],
            "FAKE_COMMAND_LOG": os.environ["FAKE_COMMAND_LOG"],
            "FAKE_MODELS": os.environ.get(
                "FAKE_MODELS",
                "provider model context\nacme rocket 128K",
            ),
        }
        if "--output=json" in sys.argv:
            print(json.dumps(environment))
        else:
            for key, value in environment.items():
                print(f"{key}={value}")
        raise SystemExit(0)
    if "is-enabled" in sys.argv:
        print("enabled")
        raise SystemExit(0)
    if "is-active" in sys.argv:
        print("active")
        raise SystemExit(0)
    raise SystemExit(0)
if name == "systemd-run":
    import subprocess

    args = sys.argv[1:]
    wait = "--wait" in args
    command: list[str] = []
    for index, arg in enumerate(args):
        if arg == "--":
            command = args[index + 1 :]
            break
        if not arg.startswith("-"):
            command = args[index:]
            break
    if wait and command:
        raise SystemExit(subprocess.call(command))
    raise SystemExit(0)
raise SystemExit(64)
"""
    )
    fake.chmod(0o755)
    for name in ("pi", "systemctl", "systemd-analyze", "systemd-run"):
        (bin_dir / name).symlink_to(fake)
    (bin_dir / "python3").symlink_to(sys.executable)

    project = tmp_path / "project"
    project.mkdir()
    other = tmp_path / "other-project"
    other.mkdir()

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "FAKE_COMMAND_LOG": str(log_path),
            "FAKE_MANAGER_PATH": str(bin_dir),
            "PI_CODING_AGENT_DIR": str(agent_dir),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_RUNTIME_DIR": str(runtime),
            "NO_COLOR": "1",
            "TEST_PROJECT": str(project),
            "TEST_OTHER_PROJECT": str(other),
            "FAKE_PI_READY": str(tmp_path / "pi-ready"),
            "FAKE_PI_GATE": str(tmp_path / "pi-gate"),
        }
    )
    for variable in (
        "PI_TASK_PI_EXECUTABLE",
        "PI_TASK_SYSTEMCTL_EXECUTABLE",
        "PI_TASK_SYSTEMD_ANALYZE_EXECUTABLE",
        "PI_TASK_SYSTEMD_RUN_EXECUTABLE",
        "PI_TASK_EXECUTABLE",
    ):
        env.pop(variable, None)
    return env


@pytest.fixture
def run_cli() -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(*arguments: str, env: dict[str, str], input: str | None = None):
        return subprocess.run(
            ["pi-task", *arguments],
            env=env,
            input=input,
            check=False,
            capture_output=True,
            text=True,
        )

    return run


def _clear_commands(env: dict[str, str]) -> None:
    Path(env["FAKE_COMMAND_LOG"]).write_text("")


def _commands(env: dict[str, str]) -> list[list[str]]:
    return [json.loads(line) for line in Path(env["FAKE_COMMAND_LOG"]).read_text().splitlines()]


def _pi_runs(env: dict[str, str]) -> list[list[str]]:
    return [
        command
        for command in _commands(env)
        if command[0] == "pi" and "--list-models" not in command and "--session" not in command
    ]


def _add_task(
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
    env: dict[str, str],
    task_id: str,
    *,
    working_directory: str | None = None,
    prompt: str = "Inspect the project.",
    model: str = "acme/rocket",
) -> None:
    result = run_cli(
        "add",
        task_id,
        "--name",
        task_id,
        "--working-directory",
        working_directory or env["TEST_PROJECT"],
        "--prompt",
        prompt,
        "--calendar",
        "Mon..Fri 09:00",
        "--model",
        model,
        "--thinking",
        "high",
        "--timeout",
        "20m",
        "--trust",
        "deny",
        "--yes",
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _immediate_env(env: dict[str, str], label: str) -> dict[str, str]:
    """Environment where Pi finishes immediately (gate already open)."""
    ready = Path(env["XDG_RUNTIME_DIR"]) / f"{label}-ready"
    gate = Path(env["XDG_RUNTIME_DIR"]) / f"{label}-gate"
    ready.unlink(missing_ok=True)
    gate.write_text("go")
    return {**env, "FAKE_PI_READY": str(ready), "FAKE_PI_GATE": str(gate)}


def _start_gated_run(
    env: dict[str, str],
    task_id: str,
    *,
    source: str = "scheduled",
) -> subprocess.Popen[str]:
    ready = Path(env["FAKE_PI_READY"])
    gate = Path(env["FAKE_PI_GATE"])
    ready.unlink(missing_ok=True)
    gate.unlink(missing_ok=True)
    proc = subprocess.Popen(
        ["pi-task", "_run-scheduled", task_id, "--source", source],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        if ready.is_file():
            return proc
        if proc.poll() is not None:
            stdout, stderr = proc.communicate()
            raise AssertionError(f"gated run exited early: {stdout}{stderr}")
        time.sleep(0.05)
    proc.kill()
    stdout, stderr = proc.communicate(timeout=5)
    raise AssertionError(f"gated run never reached Pi: {stdout}{stderr}")


def _release_gate(env: dict[str, str]) -> None:
    Path(env["FAKE_PI_GATE"]).write_text("go")


def _db_rows(env: dict[str, str]) -> list[tuple]:
    db_path = Path(env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT task_id, source, status, session_id, error, snapshot_json, model "
            "FROM runs ORDER BY started_at, id"
        ).fetchall()


def test_same_task_scheduled_overlap_records_skipped_without_pi(
    lock_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    _add_task(run_cli, lock_env, "daily-review")
    _clear_commands(lock_env)

    first = _start_gated_run(lock_env, "daily-review")
    try:
        second = run_cli(
            "_run-scheduled",
            "daily-review",
            env=_immediate_env(lock_env, "second"),
        )
        assert second.returncode == 0, second.stdout + second.stderr
        assert "skip" in second.stderr.lower()
        # The gated first run holds the only Pi invocation so far.
        assert len(_pi_runs(lock_env)) == 1

        rows = _db_rows(lock_env)
        statuses = [row[2] for row in rows]
        assert "skipped" in statuses
        skipped = next(row for row in rows if row[2] == "skipped")
        assert skipped[0] == "daily-review"
        assert skipped[1] == "scheduled"
        assert skipped[3] is None
        assert skipped[4] is not None
        assert "lock" in skipped[4].lower() or "already" in skipped[4].lower()
    finally:
        _release_gate(lock_env)
        stdout, stderr = first.communicate(timeout=15)
        assert first.returncode == 0, stdout + stderr

    rows = _db_rows(lock_env)
    assert sorted(row[2] for row in rows) == ["skipped", "succeeded"]
    assert len(_pi_runs(lock_env)) == 1


def test_manual_overlap_fails_clearly(
    lock_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    _add_task(run_cli, lock_env, "manual-block")
    _clear_commands(lock_env)

    first = _start_gated_run(lock_env, "manual-block")
    try:
        blocked = run_cli(
            "_run-scheduled",
            "manual-block",
            "--source",
            "manual",
            "--run-id",
            "manual-block-second",
            env=_immediate_env(lock_env, "manual"),
        )
        assert blocked.returncode != 0, blocked.stdout + blocked.stderr
        combined = (blocked.stdout + blocked.stderr).lower()
        assert (
            "already" in combined
            or "lock" in combined
            or "running" in combined
            or "busy" in combined
            or "overlap" in combined
        )

        rows = _db_rows(lock_env)
        terminal = [row for row in rows if row[2] != "running"]
        assert any(row[1] == "manual" and row[2] == "failed" for row in terminal)
        manual = next(row for row in terminal if row[1] == "manual")
        assert manual[3] is None
        assert manual[4] is not None
    finally:
        _release_gate(lock_env)
        stdout, stderr = first.communicate(timeout=15)
        assert first.returncode == 0, stdout + stderr


def test_same_working_directory_serializes_different_tasks(
    lock_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    _add_task(run_cli, lock_env, "alpha")
    _add_task(run_cli, lock_env, "beta")
    _clear_commands(lock_env)

    first = _start_gated_run(lock_env, "alpha")
    try:
        second = run_cli("_run-scheduled", "beta", env=_immediate_env(lock_env, "beta"))
        assert second.returncode == 0, second.stdout + second.stderr
        assert "skip" in second.stderr.lower()
        assert len(_pi_runs(lock_env)) == 1

        rows = _db_rows(lock_env)
        skipped = next(row for row in rows if row[2] == "skipped")
        assert skipped[0] == "beta"
        error = (skipped[4] or "").lower()
        assert "directory" in error or "working" in error or "busy" in error
    finally:
        _release_gate(lock_env)
        stdout, stderr = first.communicate(timeout=15)
        assert first.returncode == 0, stdout + stderr


def test_different_working_directories_may_run_concurrently(
    lock_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
    tmp_path: Path,
) -> None:
    _add_task(run_cli, lock_env, "left")
    _add_task(run_cli, lock_env, "right", working_directory=lock_env["TEST_OTHER_PROJECT"])
    # Prime the SQLite schema so concurrent first-run migrations cannot race.
    primed = run_cli("runs", env=lock_env)
    assert primed.returncode == 0, primed.stdout + primed.stderr
    _clear_commands(lock_env)

    left_ready = tmp_path / "left-ready"
    right_ready = tmp_path / "right-ready"
    left_gate = tmp_path / "left-gate"
    right_gate = tmp_path / "right-gate"
    for path in (left_ready, right_ready, left_gate, right_gate):
        path.unlink(missing_ok=True)

    left_env = {**lock_env, "FAKE_PI_READY": str(left_ready), "FAKE_PI_GATE": str(left_gate)}
    right_env = {**lock_env, "FAKE_PI_READY": str(right_ready), "FAKE_PI_GATE": str(right_gate)}

    left = subprocess.Popen(
        ["pi-task", "_run-scheduled", "left"],
        env=left_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    right = subprocess.Popen(
        ["pi-task", "_run-scheduled", "right"],
        env=right_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 10
    try:
        while time.time() < deadline:
            if left_ready.is_file() and right_ready.is_file():
                break
            if left.poll() is not None and right.poll() is not None:
                break
            time.sleep(0.05)
        assert left_ready.is_file() and right_ready.is_file(), (
            f"expected concurrent Pi starts; left={left.poll()} right={right.poll()}"
        )
    finally:
        left_gate.write_text("go")
        right_gate.write_text("go")
        left_out, left_err = left.communicate(timeout=15)
        right_out, right_err = right.communicate(timeout=15)

    assert left.returncode == 0, left_out + left_err
    assert right.returncode == 0, right_out + right_err
    rows = _db_rows(lock_env)
    assert sorted((row[0], row[2]) for row in rows) == [
        ("left", "succeeded"),
        ("right", "succeeded"),
    ]


def test_run_keeps_startup_snapshot_after_task_edit(
    lock_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    _add_task(run_cli, lock_env, "snapshot-me", model="acme/rocket", prompt="Original prompt.")
    lock_env["FAKE_MODELS"] = "provider model context\nacme rocket 128K\nacme comet 128K"
    _clear_commands(lock_env)

    first = _start_gated_run(lock_env, "snapshot-me")
    try:
        edited = run_cli(
            "edit",
            "snapshot-me",
            "--model",
            "acme/comet",
            "--prompt",
            "Edited prompt.",
            env=lock_env,
        )
        assert edited.returncode == 0, edited.stdout + edited.stderr
        paused = run_cli("pause", "snapshot-me", env=lock_env)
        assert paused.returncode == 0, paused.stdout + paused.stderr
    finally:
        _release_gate(lock_env)
        stdout, stderr = first.communicate(timeout=15)
        assert first.returncode == 0, stdout + stderr

    rows = _db_rows(lock_env)
    assert len(rows) == 1
    _task_id, _source, status, _session_id, _error, snapshot_json, model = rows[0]
    assert status == "succeeded"
    assert model == "acme/rocket"
    snapshot = json.loads(snapshot_json)
    assert snapshot["model"] == "acme/rocket"
    assert snapshot["prompt"] == "Original prompt."
    assert snapshot["paused"] is False


def test_resume_session_errors_when_newest_run_has_no_session(
    lock_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Newest skipped run must not fall back to an older successful session."""
    _add_task(run_cli, lock_env, "no-session")
    _clear_commands(lock_env)

    first = _start_gated_run(lock_env, "no-session")
    try:
        second = run_cli(
            "_run-scheduled",
            "no-session",
            env=_immediate_env(lock_env, "second"),
        )
        assert second.returncode == 0, second.stdout + second.stderr
    finally:
        _release_gate(lock_env)
        stdout, stderr = first.communicate(timeout=15)
        assert first.returncode == 0, stdout + stderr

    rows = _db_rows(lock_env)
    assert sorted(row[2] for row in rows) == ["skipped", "succeeded"]

    _clear_commands(lock_env)
    missing = run_cli("resume-session", "no-session", env=lock_env)
    assert missing.returncode != 0
    assert "no-session" in missing.stderr
    assert "no recorded" in missing.stderr.lower() or "no session" in missing.stderr.lower()
    session_opens = [
        command for command in _commands(lock_env) if command[0] == "pi" and "--session" in command
    ]
    assert session_opens == []


def test_resume_session_refuses_active_run(
    lock_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    _add_task(run_cli, lock_env, "active-session")
    _clear_commands(lock_env)

    first = _start_gated_run(lock_env, "active-session")
    try:
        db_path = Path(lock_env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
        deadline = time.time() + 10
        run_id = None
        while time.time() < deadline:
            if db_path.is_file():
                with sqlite3.connect(db_path) as connection:
                    row = connection.execute(
                        "SELECT id FROM runs WHERE status = 'running'"
                    ).fetchone()
                if row is not None:
                    run_id = row[0]
                    break
            time.sleep(0.05)
        assert run_id is not None

        refused = run_cli("resume-session", "active-session", env=lock_env)
        assert refused.returncode != 0
        assert "active" in refused.stderr.lower() or "running" in refused.stderr.lower()
        session_opens = [
            command
            for command in _commands(lock_env)
            if command[0] == "pi" and "--session" in command
        ]
        assert session_opens == []
    finally:
        _release_gate(lock_env)
        stdout, stderr = first.communicate(timeout=15)
        assert first.returncode == 0, stdout + stderr


def test_stale_lock_does_not_block_future_runs(
    lock_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    _add_task(run_cli, lock_env, "recover")
    runtime = Path(lock_env["XDG_RUNTIME_DIR"]) / "pi-task" / "locks"
    (runtime / "task").mkdir(parents=True)
    (runtime / "working-directory").mkdir(parents=True)
    (runtime / "task" / "recover.lock").write_text("stale")

    _clear_commands(lock_env)
    first = _start_gated_run(lock_env, "recover")
    first.send_signal(signal.SIGKILL)
    first.wait(timeout=5)

    recovered = run_cli("_run-scheduled", "recover", env=_immediate_env(lock_env, "recover"))
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    rows = _db_rows(lock_env)
    statuses = sorted(row[2] for row in rows)
    assert "succeeded" in statuses
    # Interrupted wrapper row must not stay running forever.
    assert "running" not in statuses
    assert any(row[2] == "failed" and row[4] and "abandon" in row[4].lower() for row in rows)


def test_run_keeps_startup_snapshot_after_task_remove(
    lock_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    _add_task(run_cli, lock_env, "remove-me", prompt="Keep this prompt.")
    _clear_commands(lock_env)

    first = _start_gated_run(lock_env, "remove-me")
    try:
        removed = run_cli("remove", "remove-me", "--yes", env=lock_env)
        assert removed.returncode == 0, removed.stdout + removed.stderr
        assert not (Path(lock_env["XDG_CONFIG_HOME"]) / "pi-task/tasks/remove-me.toml").is_file()
    finally:
        _release_gate(lock_env)
        stdout, stderr = first.communicate(timeout=15)
        assert first.returncode == 0, stdout + stderr

    rows = _db_rows(lock_env)
    assert len(rows) == 1
    _task_id, _source, status, _session_id, _error, snapshot_json, _model = rows[0]
    assert status == "succeeded"
    snapshot = json.loads(snapshot_json)
    assert snapshot["prompt"] == "Keep this prompt."
    assert snapshot["task_id"] == "remove-me"


def test_resume_session_heals_orphaned_running_row(
    lock_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """After a killed wrapper, resume-session should not forever treat the run as active."""
    _add_task(run_cli, lock_env, "orphan-resume")
    _clear_commands(lock_env)

    first = _start_gated_run(lock_env, "orphan-resume")
    db_path = Path(lock_env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
    deadline = time.time() + 10
    run_id = None
    while time.time() < deadline:
        if db_path.is_file():
            with sqlite3.connect(db_path) as connection:
                row = connection.execute("SELECT id FROM runs WHERE status = 'running'").fetchone()
            if row is not None:
                run_id = row[0]
                break
        time.sleep(0.05)
    assert run_id is not None
    first.send_signal(signal.SIGKILL)
    first.wait(timeout=5)

    # No session was finalized; heal must clear running so the refusal is not permanent.
    refused_or_missing = run_cli("resume-session", "orphan-resume", env=lock_env)
    assert refused_or_missing.returncode != 0
    assert "active" not in refused_or_missing.stderr.lower()
    assert (
        "no recorded" in refused_or_missing.stderr.lower()
        or "no session" in refused_or_missing.stderr.lower()
        or "missing" in refused_or_missing.stderr.lower()
    )
    with sqlite3.connect(db_path) as connection:
        status = connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert status is not None
    assert status[0] != "running"
