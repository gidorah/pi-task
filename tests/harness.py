"""Shared CLI test harness for pi-task integration tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_FAKE_COMMAND = Path(__file__).with_name("_fake_command.py")


def make_run_env(tmp_path: Path) -> dict[str, str]:
    """Isolated XDG dirs plus fake pi/systemctl/journalctl on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "commands.jsonl"
    agent_dir = tmp_path / "pi-agent"
    agent_dir.mkdir()
    fake = bin_dir / "fake-command"
    fake.write_text(_FAKE_COMMAND.read_text())
    fake.chmod(0o755)
    for name in ("pi", "systemctl", "systemd-analyze", "systemd-run", "journalctl"):
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
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
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
        "PI_TASK_JOURNALCTL_EXECUTABLE",
        # GitHub Actions (and systemd units) set INVOCATION_ID; keep tests
        # deterministic unless a case opts in by re-adding the variable.
        "INVOCATION_ID",
    ):
        env.pop(variable, None)
    return env


def run_cli(
    *arguments: str,
    env: dict[str, str],
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["pi-task", *arguments],
        env=env,
        input=input,
        check=False,
        capture_output=True,
        text=True,
    )


def commands(env: dict[str, str]) -> list[list[str]]:
    path = Path(env["FAKE_COMMAND_LOG"])
    if not path.is_file() or not path.read_text().strip():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def clear_commands(env: dict[str, str]) -> None:
    Path(env["FAKE_COMMAND_LOG"]).write_text("")


def add_task(
    env: dict[str, str],
    task_id: str = "daily-review",
    *,
    timeout: str = "20m",
    cli: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> None:
    runner = cli or run_cli
    result = runner(
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
        timeout,
        "--trust",
        "deny",
        "--yes",
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def latest_run_id(env: dict[str, str]) -> str:
    import sqlite3

    db_path = Path(env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT id FROM runs ORDER BY started_at DESC, id DESC LIMIT 1"
        ).fetchone()
    assert row is not None, "expected a run row"
    return str(row[0])
