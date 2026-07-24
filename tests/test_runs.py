from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def run_env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "commands.jsonl"
    agent_dir = tmp_path / "pi-agent"
    agent_dir.mkdir()
    fake = bin_dir / "fake-command"
    fake.write_text(
        r"""#!/usr/bin/env python3
import json
import os
import signal
import sys
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

    # One-shot JSON mode: create a normal Pi session file and emit lifecycle events.
    cwd = Path.cwd().resolve()
    agent_dir = Path(os.environ["PI_CODING_AGENT_DIR"])
    session_id = os.environ.get("FAKE_SESSION_ID", "019f0000-1111-2222-3333-444455556666")
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
        "stopReason": os.environ.get("FAKE_STOP_REASON", "stop"),
        "timestamp": 1,
    }
    user_message = {"type": "message", "message": {
        "role": "user", "content": "hi", "timestamp": 1,
    }}
    session_path.write_text(
        json.dumps(header) + "\n"
        + json.dumps(user_message) + "\n"
        + json.dumps({"type": "message", "message": assistant}) + "\n"
    )
    print(json.dumps(header), flush=True)
    print(json.dumps({"type": "agent_start"}), flush=True)
    print(json.dumps({"type": "message_end", "message": assistant}), flush=True)
    print(json.dumps({"type": "agent_end", "messages": [assistant]}), flush=True)
    if os.environ.get("FAKE_PI_STDERR"):
        print(os.environ["FAKE_PI_STDERR"], file=sys.stderr)
    raise SystemExit(int(os.environ.get("FAKE_PI_EXIT", "0")))

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
            "NO_COLOR": "1",
            "TEST_PROJECT": str(project),
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


def _commands(env: dict[str, str]) -> list[list[str]]:
    return [json.loads(line) for line in Path(env["FAKE_COMMAND_LOG"]).read_text().splitlines()]


def _clear_commands(env: dict[str, str]) -> None:
    Path(env["FAKE_COMMAND_LOG"]).write_text("")


def _add_task(
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
    env: dict[str, str],
    task_id: str = "daily-review",
) -> None:
    result = run_cli(
        "add",
        task_id,
        "--name",
        "Daily review",
        "--working-directory",
        env["TEST_PROJECT"],
        "--prompt",
        "Inspect the project.",
        "--calendar",
        "Mon..Fri 09:00",
        "--model",
        "acme/rocket",
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
    listed = run_cli("runs", env=run_env)
    assert listed.returncode == 0, listed.stdout + listed.stderr
    assert "daily-review" in listed.stdout
    assert "succeeded" in listed.stdout
    assert "scheduled" in listed.stdout
    assert session_id in listed.stdout
    assert "in=11 out=7" in listed.stdout
    assert "0.03" in listed.stdout

    db_path = Path(run_env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
    assert db_path.is_file()
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT task_id, source, status, session_id, session_path, prompt_hash, "
            "snapshot_json, model, thinking, duration_ms, "
            "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_total "
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
    ) = row
    assert task_id == "daily-review"
    assert source == "scheduled"
    assert status == "succeeded"
    assert stored_session_id == session_id
    assert session_path is not None and Path(session_path).is_file()
    # Session path must encode the task working directory and remain in Pi storage.
    assert run_env["TEST_PROJECT"].lstrip("/").replace("/", "-") in session_path
    assert str(Path(run_env["PI_CODING_AGENT_DIR"]) / "sessions") in session_path
    assert prompt_hash == hashlib.sha256(b"Inspect the project.").hexdigest()
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


def test_sigterm_marks_run_cancelled(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
    tmp_path: Path,
) -> None:
    # Replace fake pi with a slow process that ignores nothing and dies on SIGTERM.
    import time

    bin_dir = Path(run_env["PATH"].split(os.pathsep)[0])
    slow = bin_dir / "pi"
    slow.unlink()
    slow.write_text(
        """#!/usr/bin/env python3
import json, os, signal, sys, time
from pathlib import Path
with Path(os.environ["FAKE_COMMAND_LOG"]).open("a") as log:
    print(json.dumps(["pi", *sys.argv[1:]]), file=log)
if "--list-models" in sys.argv:
    print("provider model context\\nacme rocket 128K")
    raise SystemExit(0)
signal.signal(signal.SIGTERM, signal.SIG_DFL)
time.sleep(30)
raise SystemExit(0)
"""
    )
    slow.chmod(0o755)
    _add_task(run_cli, run_env, "cancel-me")
    _clear_commands(run_env)

    proc = subprocess.Popen(
        ["pi-task", "_run-scheduled", "cancel-me"],
        env=run_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Wait until the wrapper has a running row and has started Pi (handlers installed).
    db_path = Path(run_env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
    deadline = time.time() + 10
    started = False
    while time.time() < deadline:
        if db_path.is_file():
            with sqlite3.connect(db_path) as connection:
                row = connection.execute("SELECT 1 FROM runs WHERE status = 'running'").fetchone()
            if row is not None:
                pi_commands = [
                    command
                    for command in _commands(run_env)
                    if command[0] == "pi" and "--list-models" not in command
                ]
                if pi_commands:
                    started = True
                    break
        time.sleep(0.05)
    if not started:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=5)
        raise AssertionError(f"run never reached Pi: {stdout}{stderr}")

    proc.send_signal(signal.SIGTERM)
    stdout, stderr = proc.communicate(timeout=15)
    assert proc.returncode != 0, stdout + stderr

    with sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT status FROM runs").fetchone()
    assert row is not None, "run row missing after cancel"
    assert row[0] == "cancelled"
