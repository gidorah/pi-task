from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def task_env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "commands.jsonl"
    fake = bin_dir / "fake-command"
    fake.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
with Path(os.environ["FAKE_COMMAND_LOG"]).open("a") as log:
    print(json.dumps([name, *sys.argv[1:]]), file=log)

if name == "pi":
    if "--list-models" in sys.argv:
        print(os.environ.get("FAKE_MODELS", "provider model context\\nacme rocket 128K"))
        raise SystemExit(int(os.environ.get("FAKE_MODELS_EXIT", "0")))
    print("pi 0.test")
    raise SystemExit(0)
if name == "systemd-analyze":
    if os.environ.get("FAKE_CALENDAR_EXIT"):
        print("Failed to parse calendar specification", file=sys.stderr)
        raise SystemExit(1)
    print("  Original form: " + sys.argv[-1])
    print("Normalized form: *-*-* 09:00:00")
    print("    Next elapse: Mon 2030-01-07 09:00:00 UTC")
    if os.environ.get("FAKE_CALENDAR_FAST"):
        print("   Iteration #2: Mon 2030-01-07 09:00:30 UTC")
        print("   Iteration #3: Mon 2030-01-07 09:01:00 UTC")
    else:
        print("   Iteration #2: Tue 2030-01-08 09:00:00 UTC")
        print("   Iteration #3: Wed 2030-01-09 09:00:00 UTC")
    raise SystemExit(0)
if name == "systemctl":
    if "show-environment" in sys.argv:
        environment = {
            "PATH": os.environ["FAKE_MANAGER_PATH"],
            "FAKE_COMMAND_LOG": os.environ["FAKE_COMMAND_LOG"],
            "FAKE_MODELS": os.environ.get(
                "FAKE_MANAGER_MODELS",
                os.environ.get("FAKE_MODELS", "provider model context\\nacme rocket 128K"),
            ),
        }
        if "--output=json" in sys.argv:
            print(json.dumps(environment))
        else:
            for key, value in environment.items():
                print(f"{key}={value}")
        raise SystemExit(0)
    if "is-enabled" in sys.argv:
        print(os.environ.get("FAKE_UNIT_ENABLED", "enabled"))
        raise SystemExit(0)
    if "is-active" in sys.argv:
        print(os.environ.get("FAKE_UNIT_ACTIVE", "active"))
        raise SystemExit(0)
    raise SystemExit(int(os.environ.get("FAKE_SYSTEMCTL_EXIT", "0")))
raise SystemExit(64)
"""
    )
    fake.chmod(0o755)
    for name in ("pi", "systemctl", "systemd-analyze"):
        (bin_dir / name).symlink_to(fake)
    (bin_dir / "python3").symlink_to(sys.executable)

    project = tmp_path / "project"
    project.mkdir()
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("Review the repository.\n")

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "FAKE_COMMAND_LOG": str(log_path),
            "FAKE_MANAGER_PATH": str(bin_dir),
            "PI_CODING_AGENT_DIR": str(tmp_path / "pi-agent"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "NO_COLOR": "1",
        }
    )
    for variable in (
        "PI_TASK_PI_EXECUTABLE",
        "PI_TASK_SYSTEMCTL_EXECUTABLE",
        "PI_TASK_SYSTEMD_ANALYZE_EXECUTABLE",
    ):
        env.pop(variable, None)
    env["TEST_PROJECT"] = str(project)
    env["TEST_PROMPT_FILE"] = str(prompt_file)
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


def test_add_list_and_show_an_enabled_calendar_task(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    result = run_cli(
        "add",
        "daily-review",
        "--name",
        "Daily review",
        "--working-directory",
        task_env["TEST_PROJECT"],
        "--prompt-file",
        task_env["TEST_PROMPT_FILE"],
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
        env=task_env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Upcoming occurrences" in result.stdout
    assert "2030-01-07 09:00:00 UTC" in result.stdout
    assert "Created enabled task daily-review" in result.stdout
    assert "project has no saved Pi trust decision" not in result.stdout

    task_file = Path(task_env["XDG_CONFIG_HOME"]) / "pi-task/tasks/daily-review.toml"
    task_text = task_file.read_text()
    assert 'id = "daily-review"' in task_text
    assert 'model = "acme/rocket"' in task_text
    assert "timeout_seconds = 1200" in task_text
    assert '[prompt]\nfile = "' in task_text
    assert '[schedule]\nkind = "calendar"' in task_text

    unit_dir = Path(task_env["XDG_CONFIG_HOME"]) / "systemd/user"
    service = (unit_dir / "pi-task-daily-review.service").read_text()
    timer = (unit_dir / "pi-task-daily-review.timer").read_text()
    assert service.startswith("# Generated by pi-task. Do not edit.\n")
    assert "ExecStart=" in service
    assert "_run-scheduled daily-review" in service
    assert timer.startswith("# Generated by pi-task. Do not edit.\n")
    assert "OnCalendar=Mon..Fri 09:00" in timer

    commands = [
        json.loads(line) for line in Path(task_env["FAKE_COMMAND_LOG"]).read_text().splitlines()
    ]
    assert ["pi", "--list-models"] in commands
    assert ["systemd-analyze", "calendar", "--iterations=3", "Mon..Fri 09:00"] in commands
    assert ["systemctl", "--user", "daemon-reload"] in commands
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "pi-task-daily-review.timer",
    ] in commands

    listed = run_cli("list", env=task_env)
    assert listed.returncode == 0
    assert "daily-review" in listed.stdout
    assert "Daily review" in listed.stdout
    assert "enabled" in listed.stdout

    shown = run_cli("show", "daily-review", env=task_env)
    assert shown.returncode == 0
    assert "Working directory" in shown.stdout
    assert task_env["TEST_PROJECT"] in shown.stdout
    assert "acme/rocket" in shown.stdout
    assert "Mon..Fri 09:00" in shown.stdout
    assert "enabled (active)" in shown.stdout


def test_list_includes_timer_activity_in_current_state(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    created = run_cli(
        "add",
        "failed-timer",
        "--working-directory",
        task_env["TEST_PROJECT"],
        "--prompt",
        "Inspect the project.",
        "--calendar",
        "daily",
        "--model",
        "acme/rocket",
        "--trust",
        "deny",
        "--yes",
        env=task_env,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    task_env["FAKE_UNIT_ACTIVE"] = "failed"

    listed = run_cli("list", env=task_env)

    assert listed.returncode == 0
    assert "enabled (failed)" in listed.stdout


def test_add_guides_omitted_required_values_and_can_create_paused(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    result = run_cli(
        "add",
        "--paused",
        env=task_env,
        input=(
            f"guided-task\n{task_env['TEST_PROJECT']}\nfile\n"
            f"{task_env['TEST_PROMPT_FILE']}\ndaily\nacme/rocket\ny\n"
        ),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Task ID" in result.stdout
    assert "Working directory" in result.stdout
    assert "Prompt source" in result.stdout
    assert "Prompt file" in result.stdout
    assert "Calendar schedule" in result.stdout
    assert "Model (provider/model)" in result.stdout
    assert "Created paused task guided-task" in result.stdout
    assert "project has no saved Pi trust decision" in result.stdout

    task_file = Path(task_env["XDG_CONFIG_HOME"]) / "pi-task/tasks/guided-task.toml"
    task_text = task_file.read_text()
    assert "paused = true" in task_text
    assert '[prompt]\nfile = "' in task_text
    commands = [
        json.loads(line) for line in Path(task_env["FAKE_COMMAND_LOG"]).read_text().splitlines()
    ]
    assert ["systemctl", "--user", "daemon-reload"] in commands
    assert not any("enable" in command for command in commands)

    task_env["FAKE_UNIT_ENABLED"] = "disabled"
    task_env["FAKE_UNIT_ACTIVE"] = "inactive"
    shown = run_cli("show", "guided-task", env=task_env)
    assert shown.returncode == 0
    assert "State: paused" in shown.stdout

    task_env["FAKE_UNIT_ENABLED"] = "enabled"
    task_env["FAKE_UNIT_ACTIVE"] = "active"
    drifted = run_cli("show", "guided-task", env=task_env)
    assert "State: paused (timer is enabled, active)" in drifted.stdout


@pytest.mark.parametrize(
    ("arguments", "environment", "message"),
    [
        (("Bad_ID",), {}, "task ID must be a lowercase slug"),
        (("missing-directory", "--working-directory", "/does/not/exist"), {}, "does not exist"),
        (
            (
                "two-prompts",
                "--prompt",
                "inline",
                "--prompt-file",
                "PROMPT_FILE",
            ),
            {},
            "exactly one",
        ),
        (("missing-model",), {"FAKE_MODELS": "provider model\\nother model"}, "not available"),
        (("rapid",), {"FAKE_CALENDAR_FAST": "1"}, "no faster than once per minute"),
        (("bad-calendar",), {"FAKE_CALENDAR_EXIT": "1"}, "invalid calendar schedule"),
    ],
)
def test_invalid_task_input_leaves_no_configuration_or_units(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
    arguments: tuple[str, ...],
    environment: dict[str, str],
    message: str,
) -> None:
    common = (
        "--working-directory",
        task_env["TEST_PROJECT"],
        "--prompt",
        "Inspect the project.",
        "--calendar",
        "daily",
        "--model",
        "acme/rocket",
        "--yes",
    )
    resolved = tuple(
        task_env["TEST_PROMPT_FILE"] if value == "PROMPT_FILE" else value for value in arguments
    )
    task_env.update(environment)

    result = run_cli("add", *common, *resolved, env=task_env)

    assert result.returncode == 1
    assert message in result.stderr
    config_home = Path(task_env["XDG_CONFIG_HOME"])
    assert not list(config_home.glob("pi-task/tasks/*.toml"))
    assert not list(config_home.glob("systemd/user/pi-task-*"))


def test_model_must_be_available_to_the_systemd_user_manager(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    task_env["FAKE_MANAGER_MODELS"] = "provider model context\\nother model 128K"

    result = run_cli(
        "add",
        "manager-model",
        "--working-directory",
        task_env["TEST_PROJECT"],
        "--prompt",
        "Inspect the project.",
        "--calendar",
        "daily",
        "--model",
        "acme/rocket",
        "--yes",
        env=task_env,
    )

    assert result.returncode == 1
    assert "not available to the systemd user manager" in result.stderr
    assert not list(Path(task_env["XDG_CONFIG_HOME"]).rglob("pi-task-*"))


def test_saved_parent_trust_decision_suppresses_inherit_warning(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    agent_dir = Path(task_env["PI_CODING_AGENT_DIR"])
    agent_dir.mkdir()
    project_parent = Path(task_env["TEST_PROJECT"]).parent
    (agent_dir / "trust.json").write_text(json.dumps({str(project_parent): True}))

    result = run_cli(
        "add",
        "trusted-project",
        "--working-directory",
        task_env["TEST_PROJECT"],
        "--prompt",
        "Inspect the project.",
        "--calendar",
        "daily",
        "--model",
        "acme/rocket",
        "--paused",
        "--yes",
        env=task_env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "project has no saved Pi trust decision" not in result.stdout


def test_working_directory_with_control_characters_is_rejected(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
    tmp_path: Path,
) -> None:
    unsafe_directory = tmp_path / "unsafe\ndirectory"
    unsafe_directory.mkdir()

    result = run_cli(
        "add",
        "unsafe-path",
        "--working-directory",
        str(unsafe_directory),
        "--prompt",
        "Inspect the project.",
        "--calendar",
        "daily",
        "--model",
        "acme/rocket",
        "--yes",
        env=task_env,
    )

    assert result.returncode == 1
    assert "working directory contains control characters" in result.stderr
    assert not list(Path(task_env["XDG_CONFIG_HOME"]).rglob("pi-task-*"))


def test_show_includes_the_inline_prompt(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    created = run_cli(
        "add",
        "inline-task",
        "--working-directory",
        task_env["TEST_PROJECT"],
        "--prompt",
        "Inspect the release checklist.",
        "--calendar",
        "daily",
        "--model",
        "acme/rocket",
        "--paused",
        "--yes",
        env=task_env,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    task_env["FAKE_UNIT_ENABLED"] = "disabled"
    task_env["FAKE_UNIT_ACTIVE"] = "inactive"

    shown = run_cli("show", "inline-task", env=task_env)

    assert shown.returncode == 0
    assert "Prompt: Inspect the release checklist." in shown.stdout


def test_staging_failure_removes_temporary_files(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    task_env["PI_TASK_EXECUTABLE"] = "/missing/pi-task"

    result = run_cli(
        "add",
        "staging-failure",
        "--working-directory",
        task_env["TEST_PROJECT"],
        "--prompt",
        "Inspect the project.",
        "--calendar",
        "daily",
        "--model",
        "acme/rocket",
        "--yes",
        env=task_env,
    )

    assert result.returncode == 1
    config_home = Path(task_env["XDG_CONFIG_HOME"])
    leftover_files = [path for path in config_home.rglob("*") if path.is_file()]
    assert leftover_files == []


def test_activation_failure_rolls_back_task_and_generated_units(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    task_env["FAKE_SYSTEMCTL_EXIT"] = "1"

    result = run_cli(
        "add",
        "rollback",
        "--working-directory",
        task_env["TEST_PROJECT"],
        "--prompt",
        "Inspect the project.",
        "--calendar",
        "daily",
        "--model",
        "acme/rocket",
        "--yes",
        env=task_env,
    )

    assert result.returncode == 1
    assert "systemctl exited with status 1" in result.stderr
    config_home = Path(task_env["XDG_CONFIG_HOME"])
    assert not list(config_home.glob("pi-task/tasks/*.toml"))
    assert not list(config_home.glob("systemd/user/pi-task-*"))
