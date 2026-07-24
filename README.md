# pi-task

Schedule local [Pi](https://pi.dev) agent prompts with systemd user timers.

> **Status:** Early development. Environment diagnostics are available; task scheduling is not yet implemented.

## Goals

- Calendar and interval schedules
- Per-task model and thinking-level selection
- Persistent, resumable Pi sessions
- Pause, resume, and independent manual runs
- Local execution through systemd user services
- Task and run management through a friendly CLI

## Platform

pi-task targets Linux systems with systemd and requires Python 3.14, Pi, and a systemd user manager.

## Install

Install the command into uv's user-level tool directory:

```console
uv tool install .
pi-task doctor
```

`doctor` reports required failures with a nonzero exit status. Disabled user lingering is an actionable warning because scheduled tasks can still run while the user is logged in.

For isolated testing, dependency executables can be selected through `PATH` or the `PI_TASK_PI_EXECUTABLE`, `PI_TASK_SYSTEMCTL_EXECUTABLE`, and `PI_TASK_LOGINCTL_EXECUTABLE` environment variables. A Pi override must also be present in the systemd user manager environment, as it will be for scheduled runs. Set `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, and `XDG_RUNTIME_DIR` to keep diagnostics out of real user locations.

## Development

```console
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest
```

The automated tests use isolated XDG directories and fake Pi and systemd-family executables. They do not call real Pi or modify the developer's systemd state.

## License

[MIT](LICENSE)
