from __future__ import annotations

import hashlib
import json
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from harness import add_task, clear_commands, commands

if TYPE_CHECKING:
    from collections.abc import Callable


def _commands(env: dict[str, str]) -> list[list[str]]:
    return commands(env)


def _clear_commands(env: dict[str, str]) -> None:
    clear_commands(env)


def _add_task(
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
    env: dict[str, str],
    task_id: str = "daily-review",
    *,
    timeout: str = "20m",
) -> None:
    add_task(env, task_id, timeout=timeout, cli=run_cli)


def test_scheduled_run_records_succeeded_session_and_lists_it(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    _add_task(run_cli, run_env)
    _clear_commands(run_env)

    executed = run_cli("_run-scheduled", "daily-review", env=run_env)
    assert executed.returncode == 0, executed.stdout + executed.stderr
    # Full Pi event stream must not land in journal-facing output.
    assert '"type": "agent_start"' not in executed.stdout
    assert '"type": "message_end"' not in executed.stdout
    assert '"type": "agent_start"' not in executed.stderr
    assert "session" in executed.stderr.lower() or "succeeded" in executed.stderr.lower()
    assert "tokens in=11 out=7" in executed.stderr
    assert "cost=0.03" in executed.stderr

    pi_commands = [command for command in _commands(run_env) if command[0] == "pi"]
    assert len(pi_commands) == 1
    pi = pi_commands[0]
    assert "--mode" in pi and pi[pi.index("--mode") + 1] == "json"
    assert "--model" in pi and pi[pi.index("--model") + 1] == "acme/rocket"
    assert "--thinking" in pi and pi[pi.index("--thinking") + 1] == "high"
    assert "--no-approve" in pi
    assert "--name" in pi
    assert "Inspect the project." in pi
    session_id = "019f0000-1111-2222-3333-444455556666"
    expected_hash = hashlib.sha256(b"Inspect the project.").hexdigest()
    listed = run_cli("runs", env=run_env)
    assert listed.returncode == 0, listed.stdout + listed.stderr
    assert "daily-review" in listed.stdout
    assert "succeeded" in listed.stdout
    assert "scheduled" in listed.stdout
    assert session_id in listed.stdout
    assert "in=11 out=7" in listed.stdout
    assert "0.03" in listed.stdout
    # History presents start time, prompt hash, model, and session for audit.
    assert "Started:" in listed.stdout
    assert f"Prompt hash: {expected_hash}" in listed.stdout
    assert "Model: acme/rocket" in listed.stdout
    assert "Unit: pi-task-daily-review.service" in listed.stdout
    assert "Thinking: high" in listed.stdout

    db_path = Path(run_env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
    assert db_path.is_file()
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT task_id, source, status, session_id, session_path, prompt_hash, "
            "snapshot_json, model, thinking, duration_ms, "
            "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_total, "
            "unit_name, invocation_id "
            "FROM runs"
        ).fetchone()
    assert row is not None
    (
        task_id,
        source,
        status,
        stored_session_id,
        session_path,
        prompt_hash,
        snapshot_json,
        model,
        thinking,
        duration_ms,
        input_tokens,
        output_tokens,
        cache_read,
        cache_write,
        cost_total,
        unit_name,
        invocation_id,
    ) = row
    assert task_id == "daily-review"
    assert source == "scheduled"
    assert status == "succeeded"
    assert stored_session_id == session_id
    assert session_path is not None and Path(session_path).is_file()
    # Session path must encode the task working directory and remain in Pi storage.
    assert run_env["TEST_PROJECT"].lstrip("/").replace("/", "-") in session_path
    assert str(Path(run_env["PI_CODING_AGENT_DIR"]) / "sessions") in session_path
    assert prompt_hash == expected_hash
    snapshot = json.loads(snapshot_json)
    assert snapshot["task_id"] == "daily-review"
    assert snapshot["model"] == "acme/rocket"
    assert snapshot["thinking"] == "high"
    assert model == "acme/rocket"
    assert thinking == "high"
    assert duration_ms is not None and duration_ms >= 0
    assert input_tokens == 11
    assert output_tokens == 7
    assert cache_read == 3
    assert cache_write == 1
    assert cost_total == pytest.approx(0.03)
    assert unit_name == "pi-task-daily-review.service"
    # Direct wrapper invocation outside systemd has no INVOCATION_ID.
    assert invocation_id is None

    # Migrations remain safe when commands start repeatedly.
    again = run_cli("runs", env=run_env)
    assert again.returncode == 0, again.stdout + again.stderr
    third = run_cli("runs", env=run_env)
    assert third.returncode == 0, third.stdout + third.stderr


def test_resume_session_opens_recorded_pi_session(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    _add_task(run_cli, run_env, "resume-me")
    _clear_commands(run_env)
    executed = run_cli("_run-scheduled", "resume-me", env=run_env)
    assert executed.returncode == 0, executed.stdout + executed.stderr

    db_path = Path(run_env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
    with sqlite3.connect(db_path) as connection:
        run_id, session_path = connection.execute("SELECT id, session_path FROM runs").fetchone()

    _clear_commands(run_env)
    resumed = run_cli("resume-session", run_id, env=run_env)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    pi_commands = [command for command in _commands(run_env) if command[0] == "pi"]
    assert pi_commands == [["pi", "--session", session_path]]


def test_service_unit_invokes_wrapper_with_task_identity(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    _add_task(run_cli, run_env, "wired")
    service = (Path(run_env["XDG_CONFIG_HOME"]) / "systemd/user/pi-task-wired.service").read_text()
    assert "_run-scheduled wired --source scheduled" in service
    # 20m timeout (1200s) + 120s hung-wrapper grace
    assert "RuntimeMaxSec=1320" in service
    assert "TimeoutStopSec=30" in service


def test_scheduled_entrypoint_rejects_unknown_source(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    _add_task(run_cli, run_env, "source-check")
    result = run_cli("_run-scheduled", "source-check", "--source", "mystery", env=run_env)
    assert result.returncode != 0
    assert "source" in result.stderr.lower()


def test_run_waits_records_manual_source_and_reports_status(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    _add_task(run_cli, run_env, "manual-wait")
    _clear_commands(run_env)

    executed = run_cli("run", "manual-wait", env=run_env)
    assert executed.returncode == 0, executed.stdout + executed.stderr
    assert "succeeded" in executed.stdout
    assert "manual-wait" in executed.stdout

    commands = _commands(run_env)
    systemd_run = [command for command in commands if command[0] == "systemd-run"]
    assert len(systemd_run) == 1
    invocation = systemd_run[0]
    assert "--user" in invocation
    assert "--collect" in invocation
    assert "--wait" in invocation
    unit_args = [arg for arg in invocation if arg.startswith("--unit=")]
    assert len(unit_args) == 1
    assert unit_args[0].startswith("--unit=pi-task-run-manual-wait-")
    # Full UUID hex suffix (32 chars) keeps unit names unique without hyphens.
    assert len(unit_args[0].removeprefix("--unit=pi-task-run-manual-wait-")) == 32
    assert "--property=RuntimeMaxSec=1320" in invocation
    assert "--property=TimeoutStopSec=30" in invocation
    assert "_run-scheduled" in invocation
    assert "manual-wait" in invocation
    source_index = invocation.index("--source")
    assert invocation[source_index + 1] == "manual"

    systemctl = [command for command in commands if command[0] == "systemctl"]
    timer_mutations = [
        command
        for command in systemctl
        if any(verb in command for verb in ("enable", "disable", "restart", "start", "stop"))
        and any(arg.endswith(".timer") for arg in command)
    ]
    assert timer_mutations == []

    listed = run_cli("runs", "manual-wait", env=run_env)
    assert listed.returncode == 0, listed.stdout + listed.stderr
    assert "manual" in listed.stdout
    assert "succeeded" in listed.stdout

    db_path = Path(run_env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
    with sqlite3.connect(db_path) as connection:
        source, status, session_path = connection.execute(
            "SELECT source, status, session_path FROM runs"
        ).fetchone()
    assert source == "manual"
    assert status == "succeeded"
    assert session_path is not None and Path(session_path).is_file()

    unit_dir = Path(run_env["XDG_CONFIG_HOME"]) / "systemd" / "user"
    transient_units = list(unit_dir.glob("pi-task-run-*"))
    assert transient_units == []


def test_run_detach_submits_without_waiting(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    _add_task(run_cli, run_env, "manual-detach")
    _clear_commands(run_env)

    executed = run_cli("run", "manual-detach", "--detach", env=run_env)
    assert executed.returncode == 0, executed.stdout + executed.stderr
    assert "submitted" in executed.stdout.lower() or "started" in executed.stdout.lower()

    commands = _commands(run_env)
    systemd_run = [command for command in commands if command[0] == "systemd-run"]
    assert len(systemd_run) == 1
    invocation = systemd_run[0]
    assert "--wait" not in invocation
    assert "--collect" in invocation
    assert "--user" in invocation
    source_index = invocation.index("--source")
    assert invocation[source_index + 1] == "manual"

    # Detached submission does not run the wrapper in the fake manager, so no DB row yet.
    listed = run_cli("runs", "manual-detach", env=run_env)
    assert listed.returncode == 0, listed.stdout + listed.stderr
    assert "No runs recorded" in listed.stdout or "manual" not in listed.stdout


def test_run_unknown_task_fails_without_starting_service(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    _clear_commands(run_env)
    result = run_cli("run", "missing-task", env=run_env)
    assert result.returncode != 0
    assert "does not exist" in result.stderr
    commands = _commands(run_env)
    assert [command for command in commands if command[0] == "systemd-run"] == []


def _wait_for_running_run(env: dict[str, str], *, timeout: float = 10.0) -> str:
    import time

    db_path = Path(env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if db_path.is_file():
            try:
                with sqlite3.connect(db_path) as connection:
                    row = connection.execute(
                        "SELECT id FROM runs WHERE status = 'running'"
                    ).fetchone()
            except sqlite3.OperationalError:
                # Wrapper may create the file before migrations finish.
                row = None
            if row is not None:
                pi_commands = [
                    command
                    for command in _commands(env)
                    if command[0] == "pi" and "--list-models" not in command
                ]
                if pi_commands:
                    return str(row[0])
        time.sleep(0.05)
    raise AssertionError("run never reached Pi with a running history row")


def test_sigterm_marks_run_cancelled(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    run_env = {**run_env, "FAKE_PI_SLEEP": "30", "FAKE_PI_PARTIAL_SESSION": "1"}
    _add_task(run_cli, run_env, "cancel-me")
    _clear_commands(run_env)

    proc = subprocess.Popen(
        ["pi-task", "_run-scheduled", "cancel-me"],
        env=run_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_running_run(run_env)
    except AssertionError:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=5)
        raise AssertionError(f"run never reached Pi: {stdout}{stderr}") from None

    proc.send_signal(signal.SIGTERM)
    stdout, stderr = proc.communicate(timeout=15)
    assert proc.returncode != 0, stdout + stderr

    db_path = Path(run_env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT status, session_id, session_path, error FROM runs"
        ).fetchone()
    assert row is not None, "run row missing after cancel"
    status, session_id, session_path, error = row
    assert status == "cancelled"
    assert session_id == "019f0000-1111-2222-3333-444455556666"
    assert session_path is not None and Path(session_path).is_file()
    assert error and "cancel" in error.lower()


def test_wrapper_timeout_records_timed_out_with_partial_session(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    run_env = {
        **run_env,
        "FAKE_PI_SLEEP": "30",
        "FAKE_PI_PARTIAL_SESSION": "1",
        "FAKE_SESSION_ID": "019f0000-tttt-tttt-tttt-tttttttttttt",
    }
    _add_task(run_cli, run_env, "timeout-me", timeout="1s")
    _clear_commands(run_env)

    executed = run_cli("_run-scheduled", "timeout-me", env=run_env)
    assert executed.returncode != 0, executed.stdout + executed.stderr
    assert "timed out" in executed.stderr.lower()

    db_path = Path(run_env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
    with sqlite3.connect(db_path) as connection:
        status, session_id, session_path, error = connection.execute(
            "SELECT status, session_id, session_path, error FROM runs"
        ).fetchone()
    assert status == "timed_out"
    assert session_id == "019f0000-tttt-tttt-tttt-tttttttttttt"
    assert session_path is not None and Path(session_path).is_file()
    assert error and "timed out" in error.lower()

    # Partial timed-out sessions remain openable through resume-session.
    with sqlite3.connect(db_path) as connection:
        run_id = connection.execute("SELECT id FROM runs").fetchone()[0]
    resumed = run_cli("resume-session", run_id, env=run_env)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr


def test_failed_run_keeps_partial_session_and_classifies_stop_reasons(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    run_env = {
        **run_env,
        "FAKE_STOP_REASON": "length",
        "FAKE_SESSION_ID": "019f0000-ffff-ffff-ffff-ffffffffffff",
    }
    _add_task(run_cli, run_env, "fail-length")
    _clear_commands(run_env)

    executed = run_cli("_run-scheduled", "fail-length", env=run_env)
    assert executed.returncode != 0, executed.stdout + executed.stderr

    db_path = Path(run_env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
    with sqlite3.connect(db_path) as connection:
        status, session_id, session_path, error = connection.execute(
            "SELECT status, session_id, session_path, error FROM runs"
        ).fetchone()
    assert status == "failed"
    assert session_id == "019f0000-ffff-ffff-ffff-ffffffffffff"
    assert session_path is not None and Path(session_path).is_file()
    assert error and "length" in error.lower()

    with sqlite3.connect(db_path) as connection:
        run_id = connection.execute("SELECT id FROM runs").fetchone()[0]
    resumed = run_cli("resume-session", run_id, env=run_env)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr


def test_cancel_stops_active_scheduled_run_through_systemd_unit(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    import time

    run_env = {**run_env, "FAKE_PI_SLEEP": "30", "FAKE_PI_PARTIAL_SESSION": "1"}
    _add_task(run_cli, run_env, "stop-me")
    _clear_commands(run_env)

    proc = subprocess.Popen(
        ["pi-task", "_run-scheduled", "stop-me"],
        env=run_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        run_id = _wait_for_running_run(run_env)
    except AssertionError:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=5)
        raise AssertionError(f"run never reached Pi: {stdout}{stderr}") from None

    cancel_env = {**run_env, "FAKE_SYSTEMCTL_STOP_PID": str(proc.pid)}
    _clear_commands(cancel_env)
    cancelled = run_cli("cancel", run_id, env=cancel_env)
    stdout, stderr = proc.communicate(timeout=15)
    assert cancelled.returncode == 0, cancelled.stdout + cancelled.stderr
    assert "Cancelled" in cancelled.stdout
    assert "pi-task-stop-me.service" in cancelled.stdout
    assert proc.returncode != 0, stdout + stderr

    stop_commands = [
        command
        for command in _commands(cancel_env)
        if command[0] == "systemctl" and "stop" in command
    ]
    assert stop_commands == [
        ["systemctl", "--user", "stop", "pi-task-stop-me.service"],
    ]

    db_path = Path(run_env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
    deadline = time.time() + 5
    status = None
    while time.time() < deadline:
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                "SELECT status, session_path FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is not None and row[0] != "running":
            status, session_path = row
            break
        time.sleep(0.05)
    assert status == "cancelled"
    assert session_path is not None and Path(session_path).is_file()


def test_cancel_stops_active_manual_run_unit(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    import time
    import uuid

    run_env = {**run_env, "FAKE_PI_SLEEP": "30", "FAKE_PI_PARTIAL_SESSION": "1"}
    _add_task(run_cli, run_env, "manual-stop")
    run_id = str(uuid.uuid4())
    _clear_commands(run_env)

    proc = subprocess.Popen(
        [
            "pi-task",
            "_run-scheduled",
            "manual-stop",
            "--source",
            "manual",
            "--run-id",
            run_id,
        ],
        env=run_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert _wait_for_running_run(run_env) == run_id
    except AssertionError:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=5)
        raise AssertionError(f"run never reached Pi: {stdout}{stderr}") from None

    cancel_env = {**run_env, "FAKE_SYSTEMCTL_STOP_PID": str(proc.pid)}
    _clear_commands(cancel_env)
    cancelled = run_cli("cancel", run_id, env=cancel_env)
    proc.communicate(timeout=15)
    assert cancelled.returncode == 0, cancelled.stdout + cancelled.stderr
    expected_unit = f"pi-task-run-manual-stop-{run_id.replace('-', '')}.service"
    assert expected_unit in cancelled.stdout

    stop_commands = [
        command
        for command in _commands(cancel_env)
        if command[0] == "systemctl" and "stop" in command
    ]
    assert stop_commands == [["systemctl", "--user", "stop", expected_unit]]

    db_path = Path(run_env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
    deadline = time.time() + 5
    status = None
    while time.time() < deadline:
        with sqlite3.connect(db_path) as connection:
            row = connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is not None and row[0] != "running":
            status = row[0]
            break
        time.sleep(0.05)
    assert status == "cancelled"


def test_cancel_rejects_inactive_and_missing_runs(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    _add_task(run_cli, run_env, "idle-cancel")
    finished = run_cli("_run-scheduled", "idle-cancel", env=run_env)
    assert finished.returncode == 0, finished.stdout + finished.stderr
    db_path = Path(run_env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
    with sqlite3.connect(db_path) as connection:
        run_id = connection.execute("SELECT id FROM runs").fetchone()[0]

    inactive = run_cli("cancel", run_id, env=run_env)
    assert inactive.returncode != 0
    assert "not active" in inactive.stderr.lower()

    missing = run_cli("cancel", "00000000-0000-0000-0000-000000000000", env=run_env)
    assert missing.returncode != 0
    assert "does not exist" in missing.stderr.lower()


def test_cancel_heals_orphaned_running_row_without_systemctl_stop(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """A running history row with free locks is abandoned, not systemctl-stopped."""
    import uuid

    _add_task(run_cli, run_env, "orphan-cancel")
    # Ensure schema exists.
    run_cli("runs", env=run_env)
    run_id = str(uuid.uuid4())
    snapshot = {
        "task_id": "orphan-cancel",
        "working_directory": run_env["TEST_PROJECT"],
        "model": "acme/rocket",
        "thinking": "high",
        "timeout_seconds": 1200,
    }
    db_path = Path(run_env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO runs (
                id, task_id, source, status, started_at, finished_at, duration_ms,
                session_id, session_path, session_name, prompt_hash, snapshot_json,
                model, thinking, input_tokens, output_tokens, cache_read_tokens,
                cache_write_tokens, cost_total, error
            ) VALUES (
                ?, 'orphan-cancel', 'scheduled', 'running', '2030-01-01T00:00:00.000Z',
                NULL, NULL, NULL, NULL, 'orphan', 'abc', ?, 'acme/rocket', 'high',
                NULL, NULL, NULL, NULL, NULL, NULL
            )
            """,
            (run_id, json.dumps(snapshot)),
        )
        connection.commit()

    _clear_commands(run_env)
    cancelled = run_cli("cancel", run_id, env=run_env)
    assert cancelled.returncode != 0, cancelled.stdout + cancelled.stderr
    assert "no longer active" in cancelled.stderr.lower() or "abandoned" in cancelled.stderr.lower()
    stop_commands = [
        command for command in _commands(run_env) if command[0] == "systemctl" and "stop" in command
    ]
    assert stop_commands == []

    with sqlite3.connect(db_path) as connection:
        status, error = connection.execute(
            "SELECT status, error FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
    assert status == "failed"
    assert error and "stale lock" in error.lower()


def test_cancel_reports_still_running_when_stop_does_not_kill_wrapper(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Without a real unit mapping, stop is a no-op while locks are held."""
    import time

    run_env = {**run_env, "FAKE_PI_SLEEP": "30", "FAKE_PI_PARTIAL_SESSION": "1"}
    _add_task(run_cli, run_env, "still-running")
    _clear_commands(run_env)

    proc = subprocess.Popen(
        ["pi-task", "_run-scheduled", "still-running"],
        env=run_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        run_id = _wait_for_running_run(run_env)
    except AssertionError:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=5)
        raise AssertionError(f"run never reached Pi: {stdout}{stderr}") from None

    # No FAKE_SYSTEMCTL_STOP_PID: stop succeeds as a log entry but does not kill.
    _clear_commands(run_env)
    cancelled = run_cli("cancel", run_id, env=run_env)
    assert cancelled.returncode != 0, cancelled.stdout + cancelled.stderr
    assert "not yet recorded" in cancelled.stderr.lower() or "still" in cancelled.stderr.lower()
    assert proc.poll() is None

    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=15)
    # Allow cooperative finalize after we kill the wrapper ourselves.
    deadline = time.time() + 5
    db_path = Path(run_env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
    while time.time() < deadline:
        with sqlite3.connect(db_path) as connection:
            row = connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is not None and row[0] != "running":
            break
        time.sleep(0.05)


def test_cancel_force_finalizes_when_wrapper_killed_without_finalize(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """SIGKILL during stop leaves free locks; cancel records cancelled, not failed."""
    run_env = {**run_env, "FAKE_PI_SLEEP": "30", "FAKE_PI_PARTIAL_SESSION": "1"}
    _add_task(run_cli, run_env, "force-cancel")
    _clear_commands(run_env)

    proc = subprocess.Popen(
        ["pi-task", "_run-scheduled", "force-cancel"],
        env=run_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        run_id = _wait_for_running_run(run_env)
    except AssertionError:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=5)
        raise AssertionError(f"run never reached Pi: {stdout}{stderr}") from None

    cancel_env = {
        **run_env,
        "FAKE_SYSTEMCTL_STOP_PID": str(proc.pid),
        "FAKE_SYSTEMCTL_STOP_SIGNAL": "KILL",
    }
    _clear_commands(cancel_env)
    cancelled = run_cli("cancel", run_id, env=cancel_env)
    proc.communicate(timeout=5)
    assert cancelled.returncode == 0, cancelled.stdout + cancelled.stderr
    assert "Cancelled" in cancelled.stdout

    db_path = Path(run_env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
    with sqlite3.connect(db_path) as connection:
        status, error = connection.execute(
            "SELECT status, error FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
    assert status == "cancelled"
    assert error and "cancel" in error.lower()


def test_drain_process_preserves_partial_stdout_on_timeout(tmp_path: Path) -> None:
    from pi_task.runner import _drain_process

    script = tmp_path / "slow_writer.py"
    script.write_text(
        "import sys, time\n"
        'print(\'{"type": "session", "id": "partial-session"}\', flush=True)\n'
        "time.sleep(30)\n"
    )
    process = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Give the child time to emit the session line before we drain with a short budget.
        time_module = __import__("time")
        time_module.sleep(0.2)
        stdout, _stderr = _drain_process(process, timeout=0.3)
    finally:
        process.kill()
        process.wait(timeout=5)
    assert "partial-session" in stdout


def test_stop_budget_constants_align() -> None:
    from pi_task.tasks import (
        PROCESS_DRAIN_SECONDS,
        PROCESS_FINALIZE_SLACK_SECONDS,
        PROCESS_KILL_WAIT_SECONDS,
        PROCESS_TERM_GRACE_SECONDS,
        SYSTEMCTL_STOP_TIMEOUT_SECONDS,
        TIMEOUT_STOP_SECONDS,
    )

    assert TIMEOUT_STOP_SECONDS == (
        PROCESS_TERM_GRACE_SECONDS
        + PROCESS_KILL_WAIT_SECONDS
        + PROCESS_DRAIN_SECONDS
        + PROCESS_FINALIZE_SLACK_SECONDS
    )
    assert SYSTEMCTL_STOP_TIMEOUT_SECONDS > TIMEOUT_STOP_SECONDS


def test_runs_lists_failed_timed_out_and_skipped(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Operational list remains useful across abnormal terminal statuses."""
    _add_task(run_cli, run_env, "status-fail")
    fail_env = {**run_env, "FAKE_PI_EXIT": "2", "FAKE_STOP_REASON": "error"}
    assert run_cli("_run-scheduled", "status-fail", env=fail_env).returncode != 0

    timeout_env = {**run_env, "FAKE_PI_SLEEP": "30"}
    _add_task(run_cli, timeout_env, "status-timeout", timeout="1s")
    timed = run_cli("_run-scheduled", "status-timeout", env=timeout_env)
    assert timed.returncode != 0

    # Two tasks sharing a working directory: second scheduled activation is skipped.
    _add_task(run_cli, run_env, "status-skip-a")
    _add_task(run_cli, run_env, "status-skip-b")
    hold = {**run_env, "FAKE_PI_SLEEP": "30", "FAKE_PI_PARTIAL_SESSION": "1"}
    proc = subprocess.Popen(
        ["pi-task", "_run-scheduled", "status-skip-a"],
        env=hold,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Wait until the first run holds locks.
        import time

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            db = Path(run_env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
            if db.is_file():
                with sqlite3.connect(db) as connection:
                    row = connection.execute(
                        "SELECT status FROM runs WHERE task_id = 'status-skip-a'"
                    ).fetchone()
                if row and row[0] == "running":
                    break
            time.sleep(0.05)
        else:
            proc.kill()
            raise AssertionError("status-skip-a never reached running")

        skipped = run_cli("_run-scheduled", "status-skip-b", env=run_env)
        assert skipped.returncode == 0, skipped.stdout + skipped.stderr
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=10)

    listed = run_cli("runs", "--limit", "20", env=run_env)
    assert listed.returncode == 0, listed.stdout + listed.stderr
    assert "Status: failed" in listed.stdout
    assert "Status: timed_out" in listed.stdout
    assert "Status: skipped" in listed.stdout
    assert "Prompt hash:" in listed.stdout
    assert "Started:" in listed.stdout
    assert "Thinking: high" in listed.stdout
