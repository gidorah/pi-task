"""Opt-in smoke test against the real systemd user manager and Pi.

Never runs in the default suite or CI. Enable explicitly:

    PI_TASK_SMOKE=1 uv run pytest -m smoke

Requirements:
- Linux with a running systemd user manager
- An installed `pi` that can complete a short prompt (credentials already set)
- Optional `PI_TASK_SMOKE_MODEL` (defaults to the first model Pi lists)

Safety:
- Creates only a **paused** disposable task (no enabled timer / no scheduled work)
- Runs once via a transient manual unit
- Always removes the task definition and generated units in a finally block
- Run history rows may remain (product `remove` preserves history by design)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(
        os.environ.get("PI_TASK_SMOKE") != "1",
        reason="set PI_TASK_SMOKE=1 to run the real systemd/Pi smoke test",
    ),
]


def _config_home() -> Path:
    value = os.environ.get("XDG_CONFIG_HOME")
    return Path(value).expanduser() if value else Path.home() / ".config"


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _require_host() -> None:
    if shutil.which("systemctl") is None:
        pytest.skip("systemctl is required for smoke testing")
    if shutil.which("systemd-run") is None:
        pytest.skip("systemd-run is required for smoke testing")
    if shutil.which("pi") is None and not os.environ.get("PI_TASK_PI_EXECUTABLE"):
        pytest.skip("pi is required for smoke testing")
    status = _run("systemctl", "--user", "is-system-running")
    # is-system-running returns 0 for running/degraded and non-zero for offline.
    if (
        status.returncode != 0
        and "running" not in status.stdout
        and "degraded" not in status.stdout
    ):
        pytest.skip(f"systemd user manager is not available: {status.stdout or status.stderr}")


def _resolve_model(env: dict[str, str]) -> str:
    configured = os.environ.get("PI_TASK_SMOKE_MODEL")
    if configured:
        return configured
    listed = _run("pi", "--list-models", env=env)
    if listed.returncode != 0:
        pytest.skip(f"could not list Pi models: {listed.stderr or listed.stdout}")
    for line in listed.stdout.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("provider"):
            continue
        # Common formats: "provider model" or "provider/model"
        if "/" in line and " " not in line:
            return line
        parts = line.split()
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
        if len(parts) == 1 and "/" in parts[0]:
            return parts[0]
    pytest.skip("no Pi models available for smoke testing")


def test_disposable_paused_task_manual_run_against_real_systemd_and_pi(
    tmp_path: Path,
) -> None:
    _require_host()

    task_id = f"pi-task-smoke-{uuid.uuid4().hex[:10]}"
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("# smoke\n")

    # Use the real user XDG locations. Manual runs execute inside a transient
    # systemd user service that inherits the user-manager environment, so the
    # CLI must read the same config/state paths the wrapper writes.
    env = os.environ.copy()
    env["NO_COLOR"] = "1"

    model = _resolve_model(env)
    created = False
    try:
        doctor = _run("pi-task", "doctor", env=env)
        # Doctor may warn (for example lingering), but required checks should pass.
        assert doctor.returncode == 0, doctor.stdout + doctor.stderr

        add = _run(
            "pi-task",
            "add",
            task_id,
            "--name",
            "pi-task smoke disposable",
            "--working-directory",
            str(project),
            "--prompt",
            "Reply with exactly the single word: ok",
            "--interval",
            "1h",
            "--model",
            model,
            "--thinking",
            "off",
            "--timeout",
            "5m",
            "--trust",
            "deny",
            "--paused",
            "--yes",
            env=env,
        )
        assert add.returncode == 0, add.stdout + add.stderr
        assert f"Created paused task {task_id}." in add.stdout
        created = True

        # Paused creation must not leave an enabled timer scheduling real work.
        enabled = _run("systemctl", "--user", "is-enabled", f"pi-task-{task_id}.timer", env=env)
        assert enabled.returncode != 0 or "enabled" not in enabled.stdout

        run = _run("pi-task", "run", task_id, env=env)
        assert run.returncode == 0, run.stdout + run.stderr
        assert "Status: succeeded" in run.stdout
        match = re.search(r"^Run: (.+)$", run.stdout, re.MULTILINE)
        assert match is not None, run.stdout
        run_id = match.group(1).strip()

        show_runs = _run("pi-task", "runs", task_id, "--limit", "5", env=env)
        assert show_runs.returncode == 0, show_runs.stdout + show_runs.stderr
        assert run_id in show_runs.stdout
        assert "succeeded" in show_runs.stdout
    finally:
        if created:
            remove = _run("pi-task", "remove", task_id, "--yes", env=env)
            assert remove.returncode == 0, remove.stdout + remove.stderr
            config = _config_home()
            leftover = config / "pi-task" / "tasks" / f"{task_id}.toml"
            assert not leftover.exists(), f"task definition leaked: {leftover}"
            for suffix in (".service", ".timer"):
                unit = config / "systemd" / "user" / f"pi-task-{task_id}{suffix}"
                assert not unit.exists(), f"unit leaked: {unit}"
