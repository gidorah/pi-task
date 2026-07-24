# pi-task

Schedule local [Pi](https://pi.dev) agent prompts with systemd user timers.

> **Status:** Early development. Scheduled and manual runs into resumable Pi sessions are available with overlap protection, timeouts, and cancellation.

## Goals

- Calendar and interval schedules
- Per-task model and thinking-level selection
- Persistent, resumable Pi sessions
- Pause, resume, and independent manual runs
- Local runs through systemd user services
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

## Create a task

Run `pi-task add` without arguments for guided creation, or provide all values for scripted use:

```console
pi-task add daily-review \
  --name "Daily review" \
  --working-directory ~/src/project \
  --prompt-file ~/prompts/review.md \
  --calendar "Mon..Fri 09:00" \
  --model openai-codex/gpt-5.4 \
  --thinking high \
  --timeout 30m \
  --trust inherit
```

Use `--interval 15m` instead of `--calendar` for elapsed interval schedules. Intervals must be at least one minute and first fire one interval after activation. Calendar tasks enable catch-up after machine downtime by default (`--catch-up` / `--no-catch-up`); missed occurrences coalesce through systemd persistence. Interval schedules never replay missed time.

The command validates model availability in the systemd user-manager environment, validates the schedule, previews upcoming calendar occurrences, and asks for confirmation before writing the task definition and activating its user timer. Use exactly one of `--prompt` or `--prompt-file`, exactly one of `--calendar` or `--interval`, `--yes` for non-interactive confirmation, or `--paused` to create configuration and generated units without scheduling it. Inherited project trust emits a warning when Pi has no saved decision for the working directory or one of its parents.

## Manage tasks

Inspect tasks with `pi-task list` and `pi-task show TASK_ID`.

```console
pi-task edit daily-review --thinking low --interval 1h
pi-task pause daily-review
pi-task resume daily-review
pi-task sync
pi-task remove daily-review --yes
```

`edit` validates the full resulting configuration and applies it atomically. Editing or resuming an interval restarts the interval from that moment. `pause` suppresses future scheduled runs without cancelling an active run and clears calendar persistence so paused occurrences are skipped. `resume` schedules only future occurrences. `sync` regenerates units from task definitions and removes generated orphan units. `remove` stops future scheduling and deletes the definition and generated units without touching run or session history.

When a timer fires, the generated service runs `pi-task _run-scheduled TASK_ID --source scheduled`. The wrapper snapshots the task, takes exclusive same-task and same-working-directory locks under `$XDG_RUNTIME_DIR/pi-task/locks`, hashes the resolved prompt, invokes Pi once in `--mode json` with the task model, thinking level, trust policy, working directory, timeout, and a useful session name, then records the run and ordinary Pi session path in SQLite under `$XDG_STATE_HOME/pi-task/runs.db`. A scheduled lock conflict is recorded as skipped without starting Pi; a blocked manual run fails clearly. Tasks in different working directories may run concurrently. Concise lifecycle lines go to the journal; the full JSON event stream does not. An active run's session cannot be opened interactively until the run finishes.

```console
pi-task run daily-review
pi-task run daily-review --detach
pi-task cancel RUN_ID
pi-task runs
pi-task runs daily-review
pi-task logs RUN_ID
pi-task resume-session RUN_ID
```

`run` starts a uniquely named transient systemd user service (`--collect`) that invokes the same wrapper as a scheduled activation with source `manual`. Waiting for completion is the default and prints the final run status; `--detach` returns after successful submission. Manual runs never enable, disable, restart, or otherwise modify the recurring timer.

`cancel` stops a recorded active scheduled or manual run by stopping its systemd user unit. The wrapper forwards termination to Pi with a short grace period, records status `cancelled` (distinct from `timed_out` and `failed`), and keeps any partial Pi session for later inspection or `resume-session`. Pausing a task only suppresses future schedule activations and does not cancel work already running.

The wrapper enforces each task's timeout (default 30 minutes) and records `timed_out` runs. Generated services and manual transient units also set `RuntimeMaxSec` slightly above the task timeout so systemd can kill a hung wrapper as a backstop, and `TimeoutStopSec` so unit stop during cancel stays bounded. After upgrading, run `pi-task sync` so existing scheduled units pick up these service properties. Failed, cancelled, and timed-out runs retain discovered session paths when Pi wrote a session before exiting. pi-task does not automatically replay failed prompts.

`runs` lists recorded status (scheduled/manual × succeeded/failed/skipped/cancelled/timed_out), start time, duration, model, thinking level, prompt hash, token usage, cost when available, session identifiers, and the supervising systemd unit and invocation when known. `logs RUN_ID` reads journald for that run's exact systemd invocation (`_SYSTEMD_INVOCATION_ID` when the wrapper recorded it), including repeated activations of the same scheduled unit and collected transient manual units. If journal lines have expired or are missing, `logs` explains the gap without changing retained SQLite run history. `resume-session` opens a completed run through Pi's normal interactive session interface (`pi --session PATH`) without moving the session out of Pi's standard storage. Sessions from failed, cancelled, and timed-out runs remain available once the run is no longer active.

Task definitions are stored below `$XDG_CONFIG_HOME/pi-task/tasks`; generated units below `$XDG_CONFIG_HOME/systemd/user` should not be edited directly.

For isolated testing, dependency executables can be selected through `PATH` or the `PI_TASK_PI_EXECUTABLE`, `PI_TASK_SYSTEMCTL_EXECUTABLE`, `PI_TASK_SYSTEMD_ANALYZE_EXECUTABLE`, `PI_TASK_SYSTEMD_RUN_EXECUTABLE`, `PI_TASK_EXECUTABLE`, `PI_TASK_JOURNALCTL_EXECUTABLE`, and `PI_TASK_LOGINCTL_EXECUTABLE` environment variables. A Pi override must also be present in the systemd user manager environment, as it will be for scheduled runs. Set `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `XDG_DATA_HOME`, and `XDG_RUNTIME_DIR` to keep diagnostics out of real user locations.

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
