from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rich.console import Console
from rich.markup import escape

Status = Literal["PASS", "WARN", "FAIL"]


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    error: str | None = None


def _resolve_executable(
    name: str, override_variable: str, *, path: str | None = None
) -> str | None:
    override = os.environ.get(override_variable)
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.absolute())
        return None
    return shutil.which(name, path=path)


def _run(command: list[str], *, env: dict[str, str] | None = None) -> CommandResult:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return CommandResult(returncode=1, stdout="", error=str(error))
    return CommandResult(
        returncode=result.returncode,
        stdout=result.stdout,
        error=result.stderr.strip() or None,
    )


def _python_check() -> Check:
    version = sys.version_info
    detail = f"{version.major}.{version.minor}.{version.micro} at {sys.executable}"
    if version >= (3, 14):
        return Check("Python", "PASS", detail)
    return Check("Python", "FAIL", f"{detail}; Python 3.14 or newer is required")


def _platform_check() -> Check:
    if sys.platform.startswith("linux"):
        return Check("Linux host", "PASS", sys.platform)
    return Check("Linux host", "FAIL", f"{sys.platform}; pi-task requires Linux with systemd")


def _pi_check() -> Check:
    executable = _resolve_executable("pi", "PI_TASK_PI_EXECUTABLE")
    if executable is None:
        return Check(
            "Pi executable",
            "FAIL",
            "not found; install Pi or set PI_TASK_PI_EXECUTABLE",
        )
    result = _run([executable, "--version"])
    if result.returncode != 0:
        reason = result.error or f"exited with status {result.returncode}"
        return Check("Pi executable", "FAIL", f"{executable}: {reason}")
    version = result.stdout.strip().splitlines()
    detail = f"{executable} ({version[0]})" if version else executable
    return Check("Pi executable", "PASS", detail)


def _systemd_check() -> tuple[Check, str | None]:
    executable = _resolve_executable("systemctl", "PI_TASK_SYSTEMCTL_EXECUTABLE")
    if executable is None:
        return (
            Check("systemd user manager", "FAIL", "systemctl not found on PATH"),
            None,
        )
    result = _run([executable, "--user", "show-environment"])
    if result.returncode != 0:
        reason = result.error or f"systemctl exited with status {result.returncode}"
        return Check("systemd user manager", "FAIL", reason), None
    return Check("systemd user manager", "PASS", "reachable"), result.stdout


def _manager_pi_check(
    manager_environment: str | None,
) -> tuple[Check, str | None, dict[str, str] | None]:
    if manager_environment is None:
        return (
            Check(
                "Pi in systemd environment",
                "WARN",
                "not checked because the systemd user manager is unavailable",
            ),
            None,
            None,
        )

    variables = dict(line.split("=", 1) for line in manager_environment.splitlines() if "=" in line)
    process_override = os.environ.get("PI_TASK_PI_EXECUTABLE")
    manager_override = variables.get("PI_TASK_PI_EXECUTABLE")
    if process_override and manager_override != process_override:
        return (
            Check(
                "Pi in systemd environment",
                "FAIL",
                "PI_TASK_PI_EXECUTABLE is not imported into the systemd user manager",
            ),
            None,
            variables,
        )
    if manager_override:
        candidate = Path(manager_override).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            executable = str(candidate.absolute())
            return (
                Check("Pi in systemd environment", "PASS", f"configured as {executable}"),
                executable,
                variables,
            )
        return (
            Check(
                "Pi in systemd environment",
                "FAIL",
                "the manager's PI_TASK_PI_EXECUTABLE does not identify an executable file",
            ),
            None,
            variables,
        )
    manager_path = variables.get("PATH")
    if not manager_path:
        return (
            Check(
                "Pi in systemd environment",
                "FAIL",
                "the systemd user manager has no PATH; import an environment containing Pi",
            ),
            None,
            variables,
        )
    executable = shutil.which("pi", path=manager_path)
    if executable is None:
        return (
            Check(
                "Pi in systemd environment",
                "FAIL",
                "Pi is not on the systemd user manager PATH; "
                "run systemctl --user import-environment PATH",
            ),
            None,
            variables,
        )
    return Check("Pi in systemd environment", "PASS", executable), executable, variables


def _models_check(
    manager_pi_executable: str | None, manager_environment: dict[str, str] | None
) -> Check:
    if manager_pi_executable is None or manager_environment is None:
        return Check(
            "Pi models",
            "WARN",
            "not checked because Pi is unavailable to the systemd user manager",
        )

    result = _run(
        [manager_pi_executable, "--list-models"],
        env=manager_environment,
    )
    if result.returncode != 0:
        reason = result.error or f"Pi exited with status {result.returncode}"
        return Check("Pi models", "FAIL", reason)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        return Check(
            "Pi models",
            "FAIL",
            "no models are available; authenticate at least one provider with Pi",
        )
    return Check("Pi models", "PASS", f"{len(lines) - 1} available")


def _lingering_check() -> Check:
    executable = _resolve_executable("loginctl", "PI_TASK_LOGINCTL_EXECUTABLE")
    suggestion = f"run `loginctl enable-linger {os.environ.get('USER', '<user>')}`"
    if executable is None:
        return Check("user lingering", "WARN", f"loginctl not found; {suggestion}")
    result = _run([executable, "show-user", str(os.getuid()), "--property=Linger", "--value"])
    if result.returncode != 0:
        reason = result.error or f"loginctl exited with status {result.returncode}"
        return Check("user lingering", "WARN", f"could not check ({reason}); {suggestion}")
    if result.stdout.strip().lower() == "yes":
        return Check("user lingering", "PASS", "enabled")
    return Check(
        "user lingering",
        "WARN",
        f"disabled; scheduled runs do not occur after the login session ends; {suggestion}",
    )


def _xdg_path(variable: str, fallback: Path | None) -> Path | None:
    value = os.environ.get(variable)
    if value:
        return Path(value).expanduser()
    return fallback


def _writable_check(name: str, path: Path | None, *, create: bool) -> Check:
    if path is None:
        return Check(name, "FAIL", "XDG_RUNTIME_DIR is not set")
    try:
        if create:
            path.mkdir(parents=True, exist_ok=True)
        elif not path.is_dir():
            return Check(name, "FAIL", f"{path} does not exist or is not a directory")
        with tempfile.NamedTemporaryFile(prefix=".pi-task-doctor-", dir=path):
            pass
    except OSError as error:
        return Check(name, "FAIL", f"{path} is not writable: {error.strerror or error}")
    return Check(name, "PASS", str(path))


def _xdg_checks() -> list[Check]:
    home = Path.home()
    config = _xdg_path("XDG_CONFIG_HOME", home / ".config")
    state = _xdg_path("XDG_STATE_HOME", home / ".local" / "state")
    runtime = _xdg_path("XDG_RUNTIME_DIR", None)
    return [
        _writable_check("XDG config location", config / "pi-task" if config else None, create=True),
        _writable_check("XDG state location", state / "pi-task" if state else None, create=True),
        _writable_check(
            "XDG runtime location", runtime / "pi-task" if runtime else None, create=True
        ),
        _writable_check(
            "systemd unit location",
            config / "systemd" / "user" if config else None,
            create=True,
        ),
    ]


def collect_checks() -> list[Check]:
    pi_check = _pi_check()
    systemd_check, manager_environment = _systemd_check()
    manager_pi_check, manager_pi_executable, manager_variables = _manager_pi_check(
        manager_environment
    )
    return [
        _python_check(),
        _platform_check(),
        pi_check,
        systemd_check,
        manager_pi_check,
        _models_check(manager_pi_executable, manager_variables),
        _lingering_check(),
        *_xdg_checks(),
    ]


def run_doctor(*, console: Console | None = None) -> bool:
    output = console or Console()
    checks = collect_checks()
    styles = {"PASS": "green", "WARN": "yellow", "FAIL": "bold red"}
    for check in checks:
        style = styles[check.status]
        output.print(
            f"[{style}]{check.status}[/{style}]  {check.name}: {escape(check.detail)}",
            soft_wrap=True,
        )

    failures = sum(check.status == "FAIL" for check in checks)
    warnings = sum(check.status == "WARN" for check in checks)
    output.print()
    if failures:
        failure_word = "check" if failures == 1 else "checks"
        warning_detail = f", {warnings} warning{'s' if warnings != 1 else ''}" if warnings else ""
        summary = f"Not ready: {failures} required {failure_word} failed{warning_detail}."
        output.print(f"[bold red]{summary}[/bold red]")
        return False
    if warnings:
        warning_word = "warning" if warnings == 1 else "warnings"
        output.print(f"[yellow]Ready with {warnings} {warning_word}.[/yellow]")
        return True
    output.print("[green]Ready to run pi-task.[/green]")
    return True
