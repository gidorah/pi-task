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
        if "FAKE_MODELS_EXIT" in os.environ:
            environment["FAKE_MODELS_EXIT"] = os.environ["FAKE_MODELS_EXIT"]
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
            "XDG_DATA_HOME": str(tmp_path / "data"),
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


def _commands(env: dict[str, str]) -> list[list[str]]:
    return [json.loads(line) for line in Path(env["FAKE_COMMAND_LOG"]).read_text().splitlines()]


def _clear_commands(env: dict[str, str]) -> None:
    Path(env["FAKE_COMMAND_LOG"]).write_text("")


def _add_task(
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
    env: dict[str, str],
    task_id: str,
    *extra: str,
) -> subprocess.CompletedProcess[str]:
    return run_cli(
        "add",
        task_id,
        "--working-directory",
        env["TEST_PROJECT"],
        "--prompt",
        "Inspect the project.",
        "--model",
        "acme/rocket",
        "--trust",
        "deny",
        "--yes",
        *extra,
        env=env,
    )


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
    assert f"WorkingDirectory={task_env['TEST_PROJECT']}\n" in service
    assert "ExecStart=" in service
    assert "_run-scheduled daily-review --source scheduled" in service
    # 20m task timeout (1200s) + 120s hung-wrapper grace
    assert "RuntimeMaxSec=1320" in service
    assert "TimeoutStopSec=30" in service
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
            f"{task_env['TEST_PROMPT_FILE']}\ncalendar\ndaily\nacme/rocket\n"
            # Accept defaults for thinking / timeout / trust.
            "\n\n\ny\n"
        ),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    # Prompt labels include examples and allowed options (issue #22).
    assert "Task ID" in result.stdout
    assert "daily-review" in result.stdout
    assert "Working directory" in result.stdout
    assert "~" in result.stdout
    assert "Prompt source" in result.stdout
    assert "inline" in result.stdout and "file" in result.stdout
    assert "Prompt file" in result.stdout
    assert "Schedule kind" in result.stdout
    assert "calendar" in result.stdout and "interval" in result.stdout
    assert "Calendar schedule" in result.stdout
    assert "daily" in result.stdout
    assert "Available models" in result.stdout
    assert "acme/rocket" in result.stdout
    assert "Model" in result.stdout
    assert "provider/model" in result.stdout
    assert "Thinking" in result.stdout
    assert "off" in result.stdout and "medium" in result.stdout
    assert "Timeout" in result.stdout
    assert "30m" in result.stdout
    assert "Trust" in result.stdout
    assert "inherit" in result.stdout and "approve" in result.stdout
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


def test_add_guides_interval_schedule_with_examples(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    result = run_cli(
        "add",
        "--paused",
        env=task_env,
        input=(
            f"interval-guided\n{task_env['TEST_PROJECT']}\ninline\n"
            f"Inspect the project.\ninterval\n15m\nacme/rocket\n"
            "\n\n\ny\n"
        ),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Schedule kind" in result.stdout
    assert "Interval" in result.stdout
    assert "15m" in result.stdout
    assert "Timeout" in result.stdout
    assert "Created paused task interval-guided" in result.stdout
    shown = run_cli("show", "interval-guided", env=task_env)
    assert shown.returncode == 0
    assert "every 15m" in shown.stdout


def test_interactive_add_lists_available_models_and_accepts_full_name(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    task_env["FAKE_MODELS"] = (
        "provider model context\nacme rocket 128K\nopenai gpt-4 128K\nzed spark 64K"
    )

    result = run_cli(
        "add",
        "--paused",
        env=task_env,
        input=(
            f"list-models\n{task_env['TEST_PROJECT']}\ninline\n"
            f"Inspect the project.\ncalendar\ndaily\nacme/rocket\n"
            "\n\n\ny\n"
        ),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Available models" in result.stdout
    assert "1." in result.stdout and "acme/rocket" in result.stdout
    assert "openai/gpt-4" in result.stdout
    assert "zed/spark" in result.stdout
    assert "Created paused task list-models" in result.stdout
    assert ["pi", "--list-models"] in _commands(task_env)
    task_text = (Path(task_env["XDG_CONFIG_HOME"]) / "pi-task/tasks/list-models.toml").read_text()
    assert 'model = "acme/rocket"' in task_text


def test_interactive_add_selects_model_by_list_index(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    task_env["FAKE_MODELS"] = (
        "provider model context\nacme rocket 128K\nopenai gpt-4 128K\nzed spark 64K"
    )

    result = run_cli(
        "add",
        "--paused",
        env=task_env,
        input=(
            f"pick-by-index\n{task_env['TEST_PROJECT']}\ninline\n"
            f"Inspect the project.\ncalendar\ndaily\n2\n"
            "\n\n\ny\n"
        ),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Available models" in result.stdout
    assert "Created paused task pick-by-index" in result.stdout
    task_text = (Path(task_env["XDG_CONFIG_HOME"]) / "pi-task/tasks/pick-by-index.toml").read_text()
    # Sorted inventory: acme/rocket, openai/gpt-4, zed/spark
    assert 'model = "openai/gpt-4"' in task_text


def test_interactive_add_explains_when_model_listing_fails(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    task_env["FAKE_MODELS_EXIT"] = "1"

    result = run_cli(
        "add",
        "--paused",
        env=task_env,
        input=(
            f"list-fail\n{task_env['TEST_PROJECT']}\ninline\n"
            f"Inspect the project.\ncalendar\ndaily\nacme/rocket\n"
            "\n\n\ny\n"
        ),
    )

    combined = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert "could not list models" in combined or "could not inspect models" in combined
    assert "doctor" in combined or "authenticate" in combined
    # Manual entry is still offered instead of a silent empty chooser.
    assert "Model" in result.stdout
    assert "Available models" not in result.stdout


def test_add_help_lists_examples_and_choice_values(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    result = run_cli("add", "--help", env=task_env)

    assert result.returncode == 0, result.stdout + result.stderr
    help_text = result.stdout
    # Rich wraps option help; assert distinctive tokens rather than full phrases.
    assert "daily-review" in help_text
    assert "Mon..Fri" in help_text and "09:00" in help_text
    assert "15m" in help_text and "minimum" in help_text
    assert "provider/model" in help_text
    assert "off" in help_text and "xhigh" in help_text and "max" in help_text
    assert "30m" in help_text and "45s" in help_text
    assert "inherit" in help_text and "approve" in help_text and "deny" in help_text


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
        (("missing-model",), {"FAKE_MODELS": "provider model\nother model"}, "not available"),
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
    task_env["FAKE_MANAGER_MODELS"] = "provider model context\nother model 128K"

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


def test_add_interval_task_fires_after_one_interval(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    result = _add_task(run_cli, task_env, "heartbeat", "--interval", "15m")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Created enabled task heartbeat" in result.stdout
    assert "every 15m" in result.stdout

    task_text = (Path(task_env["XDG_CONFIG_HOME"]) / "pi-task/tasks/heartbeat.toml").read_text()
    assert 'kind = "interval"' in task_text
    assert 'every = "15m"' in task_text
    assert "catch_up" not in task_text

    timer = (Path(task_env["XDG_CONFIG_HOME"]) / "systemd/user/pi-task-heartbeat.timer").read_text()
    assert "OnActiveSec=15m" in timer
    assert "OnUnitActiveSec=15m" in timer
    assert "OnCalendar=" not in timer
    assert "Persistent=" not in timer

    listed = run_cli("list", env=task_env)
    assert listed.returncode == 0
    assert "every 15m" in listed.stdout

    shown = run_cli("show", "heartbeat", env=task_env)
    assert shown.returncode == 0
    assert "Schedule: every 15m" in shown.stdout


def test_interval_faster_than_one_minute_is_rejected(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    result = _add_task(run_cli, task_env, "too-fast", "--interval", "30s")

    assert result.returncode == 1
    assert "at least one minute" in result.stderr
    assert not list(Path(task_env["XDG_CONFIG_HOME"]).glob("pi-task/tasks/*.toml"))


def test_calendar_catch_up_is_configurable(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    enabled = _add_task(run_cli, task_env, "catch-up", "--calendar", "daily", "--catch-up")
    assert enabled.returncode == 0, enabled.stdout + enabled.stderr
    timer = (Path(task_env["XDG_CONFIG_HOME"]) / "systemd/user/pi-task-catch-up.timer").read_text()
    assert "Persistent=true" in timer
    task_text = (Path(task_env["XDG_CONFIG_HOME"]) / "pi-task/tasks/catch-up.toml").read_text()
    assert "catch_up = true" in task_text

    disabled = _add_task(run_cli, task_env, "no-catch-up", "--calendar", "daily", "--no-catch-up")
    assert disabled.returncode == 0, disabled.stdout + disabled.stderr
    timer = (
        Path(task_env["XDG_CONFIG_HOME"]) / "systemd/user/pi-task-no-catch-up.timer"
    ).read_text()
    assert "Persistent=true" not in timer
    assert "Persistent=false" in timer or "Persistent=" not in timer


def test_pause_suppresses_future_runs_and_clears_calendar_persistence(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    created = _add_task(run_cli, task_env, "pausable", "--calendar", "daily")
    assert created.returncode == 0, created.stdout + created.stderr

    stamp = Path(task_env["XDG_DATA_HOME"]) / "systemd/timers/stamp-pi-task-pausable.timer"
    stamp.parent.mkdir(parents=True)
    stamp.write_text("stale")
    _clear_commands(task_env)

    paused = run_cli("pause", "pausable", env=task_env)

    assert paused.returncode == 0, paused.stdout + paused.stderr
    assert "Paused task pausable" in paused.stdout
    assert (
        "paused = true"
        in (Path(task_env["XDG_CONFIG_HOME"]) / "pi-task/tasks/pausable.toml").read_text()
    )
    assert not stamp.exists()
    commands = _commands(task_env)
    assert ["systemctl", "--user", "disable", "--now", "pi-task-pausable.timer"] in commands
    assert not any(
        command[:3] == ["systemctl", "--user", "stop"] and command[-1].endswith(".service")
        for command in commands
    )

    task_env["FAKE_UNIT_ENABLED"] = "disabled"
    task_env["FAKE_UNIT_ACTIVE"] = "inactive"
    shown = run_cli("show", "pausable", env=task_env)
    assert "State: paused" in shown.stdout

    stamp.write_text("stale-again")
    task_env["FAKE_UNIT_ENABLED"] = "enabled"
    task_env["FAKE_UNIT_ACTIVE"] = "active"
    _clear_commands(task_env)
    healed = run_cli("pause", "pausable", env=task_env)
    assert healed.returncode == 0, healed.stdout + healed.stderr
    assert not stamp.exists()
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "pi-task-pausable.timer",
    ] in _commands(task_env)


def test_resume_schedules_only_future_occurrences_and_restarts_intervals(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    calendar = _add_task(run_cli, task_env, "resume-cal", "--calendar", "daily", "--paused")
    interval = _add_task(run_cli, task_env, "resume-int", "--interval", "1h", "--paused")
    assert calendar.returncode == 0, calendar.stdout + calendar.stderr
    assert interval.returncode == 0, interval.stdout + interval.stderr
    _clear_commands(task_env)

    resumed_calendar = run_cli("resume", "resume-cal", env=task_env)
    resumed_interval = run_cli("resume", "resume-int", env=task_env)

    assert resumed_calendar.returncode == 0, resumed_calendar.stdout + resumed_calendar.stderr
    assert resumed_interval.returncode == 0, resumed_interval.stdout + resumed_interval.stderr
    assert (
        "paused = false"
        in (Path(task_env["XDG_CONFIG_HOME"]) / "pi-task/tasks/resume-cal.toml").read_text()
    )
    assert (
        "paused = false"
        in (Path(task_env["XDG_CONFIG_HOME"]) / "pi-task/tasks/resume-int.toml").read_text()
    )
    commands = _commands(task_env)
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "pi-task-resume-cal.timer",
    ] in commands
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "pi-task-resume-int.timer",
    ] in commands

    _clear_commands(task_env)
    task_env["FAKE_UNIT_ENABLED"] = "disabled"
    healed = run_cli("resume", "resume-cal", env=task_env)
    assert healed.returncode == 0, healed.stdout + healed.stderr
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "pi-task-resume-cal.timer",
    ] in _commands(task_env)


def test_interval_rejects_explicit_catch_up(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    result = _add_task(run_cli, task_env, "bad-catch-up", "--interval", "15m", "--catch-up")

    assert result.returncode == 1
    assert "do not support calendar catch-up" in result.stderr
    assert not list(Path(task_env["XDG_CONFIG_HOME"]).glob("pi-task/tasks/*.toml"))


def test_edit_validates_atomically_and_restarts_interval_schedules(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    created = _add_task(run_cli, task_env, "editable", "--interval", "1h")
    assert created.returncode == 0, created.stdout + created.stderr
    original = (Path(task_env["XDG_CONFIG_HOME"]) / "pi-task/tasks/editable.toml").read_text()

    invalid = run_cli(
        "edit",
        "editable",
        "--model",
        "missing/model",
        env=task_env,
    )
    assert invalid.returncode == 1
    assert "not available" in invalid.stderr
    assert (
        Path(task_env["XDG_CONFIG_HOME"]) / "pi-task/tasks/editable.toml"
    ).read_text() == original

    _clear_commands(task_env)
    valid = run_cli(
        "edit",
        "editable",
        "--name",
        "Editable task",
        "--interval",
        "2h",
        "--thinking",
        "low",
        env=task_env,
    )
    assert valid.returncode == 0, valid.stdout + valid.stderr
    assert "Updated task editable" in valid.stdout

    task_text = (Path(task_env["XDG_CONFIG_HOME"]) / "pi-task/tasks/editable.toml").read_text()
    assert 'name = "Editable task"' in task_text
    assert 'every = "2h"' in task_text
    assert 'thinking = "low"' in task_text
    timer = (Path(task_env["XDG_CONFIG_HOME"]) / "systemd/user/pi-task-editable.timer").read_text()
    assert "OnActiveSec=2h" in timer
    assert "OnUnitActiveSec=2h" in timer

    commands = _commands(task_env)
    assert ["systemctl", "--user", "daemon-reload"] in commands
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "pi-task-editable.timer",
    ] in commands
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "pi-task-editable.timer",
    ] in commands


def test_edit_preserves_calendar_timer_when_schedule_is_unchanged(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    created = _add_task(run_cli, task_env, "stable-cal", "--calendar", "daily")
    assert created.returncode == 0, created.stdout + created.stderr
    stamp = Path(task_env["XDG_DATA_HOME"]) / "systemd/timers/stamp-pi-task-stable-cal.timer"
    stamp.parent.mkdir(parents=True)
    stamp.write_text("catch-up")
    _clear_commands(task_env)

    edited = run_cli("edit", "stable-cal", "--thinking", "high", env=task_env)

    assert edited.returncode == 0, edited.stdout + edited.stderr
    assert stamp.read_text() == "catch-up"
    commands = _commands(task_env)
    assert ["systemctl", "--user", "daemon-reload"] in commands
    assert not any("disable" in command for command in commands)
    assert not any(
        command[:4] == ["systemctl", "--user", "enable", "--now"] for command in commands
    )


def test_remove_stops_scheduling_and_keeps_definition_history_out_of_scope(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    created = _add_task(run_cli, task_env, "removable", "--calendar", "daily")
    assert created.returncode == 0, created.stdout + created.stderr
    _clear_commands(task_env)

    removed = run_cli("remove", "removable", "--yes", env=task_env)

    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert "Removed task removable" in removed.stdout
    config_home = Path(task_env["XDG_CONFIG_HOME"])
    assert not (config_home / "pi-task/tasks/removable.toml").exists()
    assert not (config_home / "systemd/user/pi-task-removable.service").exists()
    assert not (config_home / "systemd/user/pi-task-removable.timer").exists()
    commands = _commands(task_env)
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "pi-task-removable.timer",
    ] in commands
    assert not any(command[-1].endswith(".service") and "stop" in command for command in commands)


def test_sync_reconciles_units_and_removes_generated_orphans(
    task_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    created = _add_task(run_cli, task_env, "syncable", "--calendar", "daily")
    assert created.returncode == 0, created.stdout + created.stderr

    unit_dir = Path(task_env["XDG_CONFIG_HOME"]) / "systemd/user"
    timer_path = unit_dir / "pi-task-syncable.timer"
    timer_path.write_text(timer_path.read_text().replace("OnCalendar=daily", "OnCalendar=hourly"))

    orphan_service = unit_dir / "pi-task-orphan.service"
    orphan_timer = unit_dir / "pi-task-orphan.timer"
    orphan_service.write_text(
        "# Generated by pi-task. Do not edit.\n[Service]\nExecStart=/bin/true\n"
    )
    orphan_timer.write_text("# Generated by pi-task. Do not edit.\n[Timer]\nOnCalendar=daily\n")
    foreign = unit_dir / "pi-task-foreign.service"
    foreign.write_text("[Service]\nExecStart=/bin/true\n")
    _clear_commands(task_env)

    synced = run_cli("sync", env=task_env)

    assert synced.returncode == 0, synced.stdout + synced.stderr
    assert "Synchronized" in synced.stdout
    assert "OnCalendar=daily" in timer_path.read_text()
    assert "Persistent=true" in timer_path.read_text()
    assert not orphan_service.exists()
    assert not orphan_timer.exists()
    assert foreign.exists()
    commands = _commands(task_env)
    assert ["systemctl", "--user", "daemon-reload"] in commands
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "pi-task-orphan.timer",
    ] in commands
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "pi-task-syncable.timer",
    ] in commands
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "pi-task-syncable.timer",
    ] in commands
