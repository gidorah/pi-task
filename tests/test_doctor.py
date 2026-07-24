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
def doctor_env(tmp_path: Path) -> dict[str, str]:
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
        print(os.environ.get("FAKE_MODELS", "provider model\\nfake fake-model"))
        raise SystemExit(int(os.environ.get("FAKE_MODELS_EXIT", "0")))
    print("pi 0.test")
    raise SystemExit(int(os.environ.get("FAKE_PI_EXIT", "0")))
if name == "systemctl":
    environment = {"PATH": os.environ["FAKE_MANAGER_PATH"]}
    for variable in ("FAKE_COMMAND_LOG", "FAKE_MODELS", "FAKE_MODELS_EXIT"):
        if variable in os.environ:
            environment[variable] = os.environ[variable]
    if "FAKE_MANAGER_PI_OVERRIDE" in os.environ:
        environment["PI_TASK_PI_EXECUTABLE"] = os.environ["FAKE_MANAGER_PI_OVERRIDE"]
    if "--output=json" in sys.argv:
        if os.environ.get("FAKE_JSON_UNSUPPORTED") == "1":
            raise SystemExit(64)
        print(json.dumps(environment))
    else:
        for variable, value in environment.items():
            print(f"{variable}={value}")
        print("QUOTED=$'value\\x20'")
    raise SystemExit(int(os.environ.get("FAKE_SYSTEMCTL_EXIT", "0")))
if name == "loginctl":
    print(os.environ.get("FAKE_LINGER", "yes"))
    raise SystemExit(int(os.environ.get("FAKE_LOGINCTL_EXIT", "0")))
raise SystemExit(64)
"""
    )
    fake.chmod(0o755)
    for name in ("pi", "systemctl", "loginctl"):
        (bin_dir / name).symlink_to(fake)
    (bin_dir / "python3").symlink_to(sys.executable)

    xdg_root = tmp_path / "xdg"
    config = xdg_root / "config"
    state = xdg_root / "state"
    runtime = xdg_root / "runtime"
    runtime.mkdir(parents=True)

    env = os.environ.copy()
    for variable in (
        "PI_TASK_PI_EXECUTABLE",
        "PI_TASK_SYSTEMCTL_EXECUTABLE",
        "PI_TASK_LOGINCTL_EXECUTABLE",
    ):
        env.pop(variable, None)
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "FAKE_COMMAND_LOG": str(log_path),
            "FAKE_MANAGER_PATH": str(bin_dir),
            "FAKE_LINGER": "yes",
            "XDG_CONFIG_HOME": str(config),
            "XDG_STATE_HOME": str(state),
            "XDG_RUNTIME_DIR": str(runtime),
            "NO_COLOR": "1",
        }
    )
    return env


@pytest.fixture
def run_doctor() -> Callable[[dict[str, str]], subprocess.CompletedProcess[str]]:
    def run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["pi-task", "doctor"],
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )

    return run


def test_doctor_reports_a_ready_isolated_environment(
    doctor_env: dict[str, str],
    run_doctor: Callable[[dict[str, str]], subprocess.CompletedProcess[str]],
) -> None:
    result = run_doctor(doctor_env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS  Python" in result.stdout
    assert "PASS  pi-task executable" in result.stdout
    assert "PASS  Pi executable" in result.stdout
    assert "PASS  systemd user manager" in result.stdout
    assert "PASS  Pi models" in result.stdout
    assert "PASS  user lingering" in result.stdout
    assert "PASS  XDG config location" in result.stdout
    assert "PASS  XDG state location" in result.stdout
    assert "PASS  XDG runtime location" in result.stdout
    assert "Ready to run pi-task" in result.stdout

    commands = [
        json.loads(line) for line in Path(doctor_env["FAKE_COMMAND_LOG"]).read_text().splitlines()
    ]
    assert ["pi", "--version"] in commands
    assert ["systemctl", "--user", "--output=json", "show-environment"] in commands
    assert ["pi", "--list-models"] in commands
    assert any(command[:2] == ["loginctl", "show-user"] for command in commands)

    assert not Path(doctor_env["XDG_CONFIG_HOME"]).exists()
    assert not Path(doctor_env["XDG_STATE_HOME"]).exists()
    assert not (Path(doctor_env["XDG_RUNTIME_DIR"]) / "pi-task").exists()


def test_doctor_supports_systemd_without_json_environment_output(
    doctor_env: dict[str, str],
    run_doctor: Callable[[dict[str, str]], subprocess.CompletedProcess[str]],
) -> None:
    doctor_env["FAKE_JSON_UNSUPPORTED"] = "1"

    result = run_doctor(doctor_env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS  systemd user manager: reachable (legacy output)" in result.stdout


def test_doctor_returns_failure_when_a_required_capability_is_missing(
    doctor_env: dict[str, str],
    run_doctor: Callable[[dict[str, str]], subprocess.CompletedProcess[str]],
) -> None:
    doctor_env["FAKE_SYSTEMCTL_EXIT"] = "1"

    result = run_doctor(doctor_env)

    assert result.returncode == 1
    assert "FAIL  systemd user manager" in result.stdout
    assert "Not ready: 1 required check failed" in result.stdout


def test_doctor_treats_disabled_lingering_as_an_actionable_warning(
    doctor_env: dict[str, str],
    run_doctor: Callable[[dict[str, str]], subprocess.CompletedProcess[str]],
) -> None:
    doctor_env["FAKE_LINGER"] = "no"

    result = run_doctor(doctor_env)

    assert result.returncode == 0
    assert "WARN  user lingering" in result.stdout
    assert "loginctl enable-linger" in result.stdout
    assert "Ready with 1 warning" in result.stdout


def test_doctor_uses_a_pi_override_from_the_user_manager(
    doctor_env: dict[str, str],
    run_doctor: Callable[[dict[str, str]], subprocess.CompletedProcess[str]],
) -> None:
    fake_bin = Path(doctor_env["PATH"].split(os.pathsep)[0])
    doctor_env["FAKE_MANAGER_PI_OVERRIDE"] = str(fake_bin / "pi")

    result = run_doctor(doctor_env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS  Pi in systemd environment" in result.stdout
    assert "configured as" in result.stdout


def test_doctor_rejects_a_pi_override_missing_from_the_user_manager(
    doctor_env: dict[str, str],
    run_doctor: Callable[[dict[str, str]], subprocess.CompletedProcess[str]],
) -> None:
    doctor_env["PI_TASK_PI_EXECUTABLE"] = str(Path(doctor_env["PATH"].split(os.pathsep)[0]) / "pi")

    result = run_doctor(doctor_env)

    assert result.returncode == 1
    assert "FAIL  Pi in systemd environment" in result.stdout
    assert "not imported" in result.stdout


def test_doctor_fails_when_no_authenticated_pi_model_is_available(
    doctor_env: dict[str, str],
    run_doctor: Callable[[dict[str, str]], subprocess.CompletedProcess[str]],
) -> None:
    doctor_env["FAKE_MODELS"] = "provider model"

    result = run_doctor(doctor_env)

    assert result.returncode == 1
    assert "FAIL  Pi models" in result.stdout
    assert "authenticate at least one provider" in result.stdout


def test_doctor_fails_when_pi_is_not_available_to_the_user_manager(
    doctor_env: dict[str, str],
    run_doctor: Callable[[dict[str, str]], subprocess.CompletedProcess[str]],
    tmp_path: Path,
) -> None:
    doctor_env["FAKE_MANAGER_PATH"] = str(tmp_path / "empty-bin")

    result = run_doctor(doctor_env)

    assert result.returncode == 1
    assert "FAIL  Pi in systemd environment" in result.stdout


def test_doctor_uses_only_the_isolated_xdg_locations(
    doctor_env: dict[str, str],
    run_doctor: Callable[[dict[str, str]], subprocess.CompletedProcess[str]],
    tmp_path: Path,
) -> None:
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("blocked")
    doctor_env["XDG_STATE_HOME"] = str(blocked)

    result = run_doctor(doctor_env)

    assert result.returncode == 1
    assert "FAIL  XDG state location" in result.stdout
    assert str(blocked / "pi-task") in result.stdout
