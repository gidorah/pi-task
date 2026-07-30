# pi-task

Schedule local [Pi](https://pi.dev) agent prompts with systemd user timers.

pi-task is a local command-line tool for recurring and on-demand Pi agent work on a single Linux machine. Each run creates an isolated, resumable Pi session. Task configuration stays in readable TOML; run history lives in SQLite; durable scheduling and process supervision are delegated to the systemd user manager.

## Support boundaries

| Supported | Not supported (first version) |
| --- | --- |
| Linux with a systemd **user** manager | macOS, Windows, non-systemd schedulers |
| Python **3.14** | Older Python versions |
| Single-user local operation | Multi-user / shared-host orchestration |
| Calendar and interval schedules | Per-task time zones |
| Manual runs, pause/resume, cancel | Scheduler-level automatic retries |
| Project trust: inherit / approve / deny | Custom permission systems or tool allowlists |
| Journald operational logs; optional ntfy push Notifications | GUI / TUI, desktop notification daemons |
| Persistent Pi sessions per run | Shared or auto-continued sessions across runs |

Also out of scope: prompt templating, automatic history/session pruning, automatic provider credential management, parallel runs inside one working directory, and recording activations that systemd suppresses before the wrapper starts.

Arch Linux is the primary development platform; behavior is built on portable systemd user interfaces rather than distribution-specific tooling.

## Prerequisites

1. **Linux + systemd user manager** that can run `systemctl --user`.
2. **Python 3.14**.
3. **[uv](https://docs.astral.sh/uv/)** for installation (recommended).
4. **[Pi](https://pi.dev)** on `PATH`, already authenticated for the models you intend to use.
5. **User lingering** when tasks must run without an active login session (recommended; see [Lingering](#lingering)).

Provider credentials stay with Pi (or your user-manager environment). pi-task never copies secrets into task storage.

## Install

Install the command into uv's user-level tool directory from this repository:

```console
uv tool install .
pi-task --version
pi-task doctor
```

From a built wheel (release verification path):

```console
uv build
uv tool install dist/pi_task-*.whl
pi-task --version
```

`doctor` reports required failures with a nonzero exit status. Fix FAIL items before relying on scheduled work. WARN items are actionable but not fatal.

### Shell completion

Typer provides shell completion for bash, zsh, and fish:

```console
pi-task --install-completion
# or print the script to review / install manually:
pi-task --show-completion
```

Restart the shell (or source your rc file) after installing.

## Lingering

Scheduled tasks run under the **systemd user manager**. When you log out, that manager may stop unless lingering is enabled.

```console
loginctl enable-linger "$USER"
pi-task doctor
```

Disabled lingering is an actionable warning: tasks can still run while you are logged in, but unattended runs after logout may not fire. Enable lingering when automation must survive logout.

## Quick examples

Guided creation:

```console
pi-task add
```

Scripted calendar task (weekdays at 09:00 local time):

```console
pi-task add daily-review \
  --name "Daily review" \
  --working-directory ~/src/project \
  --prompt-file ~/prompts/review.md \
  --calendar "Mon..Fri 09:00" \
  --model openai-codex/gpt-5.4 \
  --thinking high \
  --timeout 30m \
  --trust inherit \
  --yes
```

Interval task (first fire one interval after activation; minimum interval is one minute):

```console
pi-task add heartbeat \
  -C ~/src/project \
  --prompt "Summarize git status in one sentence." \
  --interval 15m \
  --model openai-codex/gpt-5.4 \
  --thinking low \
  --yes
```

Create configuration without enabling the timer (validate first):

```console
pi-task add trial -C ~/src/project --prompt "ping" --calendar "daily" \
  --model openai-codex/gpt-5.4 --paused --yes
pi-task show trial
pi-task resume trial
```

Common day-to-day commands:

```console
pi-task list
pi-task show daily-review
pi-task edit daily-review --thinking low --interval 1h
pi-task pause daily-review
pi-task resume daily-review
pi-task sync
pi-task run daily-review
pi-task runs
pi-task runs daily-review
pi-task logs RUN_ID
pi-task cancel RUN_ID
pi-task resume-session daily-review
pi-task remove daily-review --yes
pi-task notify
pi-task notify --show
```

## Create a task

Every task needs:

| Field | Notes |
| --- | --- |
| **Task ID** | Immutable lowercase slug (`daily-review`). Used in units, locks, and filenames. |
| **Working directory** | Explicit project path; Pi runs there. |
| **Prompt** | Exactly one of `--prompt` or `--prompt-file`. Submitted exactly as stored (no templating). |
| **Schedule** | Exactly one of `--calendar` or `--interval`. |
| **Model** | Available `provider/model` as seen by the systemd user-manager environment. |
| **Thinking level** | Pi thinking level (`off` … `max`). Default `medium`. |
| **Timeout** | Default `30m`. Wrapper enforces it; systemd has a slightly longer hard backstop. |
| **Trust** | `inherit` (default), `approve`, or `deny`. |

Optional: `--name` for a human-readable label, `--catch-up` / `--no-catch-up` for calendar persistence, `--paused` to create without activating the timer, `--yes` to skip confirmation.

`add` validates the model in the user-manager environment, validates the schedule, previews upcoming calendar occurrences, and writes the TOML definition before reconciling generated units. Inherited project trust emits a warning when Pi has no saved decision for the working directory or a parent.

### Schedule forms

**Calendar** — systemd calendar syntax in the machine's local time zone (for example `Mon..Fri 09:00`, `daily`, `*-*-* 12:00:00`). Catch-up after machine downtime is on by default and uses systemd persistence; multiple missed occurrences coalesce into one.

**Interval** — friendly durations such as `15m` or `2h`. First fire is one interval after activation. Missed interval occurrences are never replayed. Intervals faster than one minute are rejected.

### Project trust

| Policy | Behavior |
| --- | --- |
| `inherit` | Use Pi's existing saved trust decision for the path (default). |
| `approve` | Explicitly allow project resources for unattended runs. |
| `deny` | Explicitly refuse project resources. |

Directories are never silently approved. Prefer `deny` or an already-trusted path for unattended automation.

### Credentials

pi-task does **not** store provider API keys. Use Pi's normal login state, or supply credentials through the systemd user-manager environment if that is how you already run Pi. `doctor` helps confirm the Pi executable and models are visible where scheduled services will run.

## Manage tasks

| Command | Effect |
| --- | --- |
| `list` / `show` | Inspect configuration and scheduling state. |
| `edit` | Validate the full resulting config and apply atomically. Active runs keep their original snapshot. |
| `pause` | Suppress future scheduled runs and clear calendar persistence. Does **not** cancel an active run. Occurrences missed while paused are skipped. |
| `resume` | Schedule only future work. Interval schedules restart their interval from resume time. |
| `sync` | Regenerate units from definitions; remove generated orphans. |
| `remove` | End future scheduling; delete the definition and generated units. **Preserves** run history and Pi sessions. |
| `notify` | Configure global ntfy Notifications for terminal Runs (see below). |

Task definitions live under `$XDG_CONFIG_HOME/pi-task/tasks` (default `~/.config/pi-task/tasks`). Generated units under `$XDG_CONFIG_HOME/systemd/user` are marked as generated — do not edit them by hand; change the TOML and run `sync` or `edit`.

## Runs: scheduled, manual, cancel, history

When a timer fires, the generated service runs `pi-task _run-scheduled TASK_ID --source scheduled`. The wrapper snapshots the task, takes exclusive same-task and same-working-directory locks under `$XDG_RUNTIME_DIR/pi-task/locks`, hashes the resolved prompt, invokes Pi once in `--mode json`, and records the run plus session path in `$XDG_STATE_HOME/pi-task/runs.db`.

| Concern | Behavior |
| --- | --- |
| Overlap | Same task cannot overlap; tasks sharing a normalized working directory are serialized; different directories may run concurrently. |
| Scheduled lock conflict | Recorded as `skipped` when the wrapper starts. Activations systemd suppresses before the wrapper starts are not recorded. |
| Manual run | `pi-task run TASK_ID` starts a uniquely named transient user service (`--collect`) and returns immediately with its Run ID. Inspect the eventual result with `pi-task runs TASK_ID` or `pi-task logs RUN_ID`. **Never** starts, stops, or modifies the recurring timer. |
| Timeout | Wrapper records `timed_out`. Units also set `RuntimeMaxSec` slightly above the task timeout as a hung-wrapper backstop. After upgrades, run `pi-task sync`. |
| Cancel | `pi-task cancel RUN_ID` stops the supervising unit. Distinct from pause. Status `cancelled`; partial sessions remain. |
| Success | Normal Pi process exit with a final assistant stop reason indicating completion. Recovered tool errors do not override final success. |
| Retries | pi-task does not automatically replay failed prompts. |

```console
pi-task runs
pi-task runs daily-review --limit 50
pi-task logs RUN_ID
pi-task resume-session daily-review
```

`runs` shows source, status (`succeeded` / `failed` / `skipped` / `cancelled` / `timed_out`), start time, duration, model, thinking, prompt hash, tokens, cost when available, session id/path, and supervising unit / invocation when known.

`logs RUN_ID` reads journald for that run. With a stored `INVOCATION_ID`, selection is exact; otherwise it falls back to unit name plus a tight time window. Missing or expired journal lines are explained without changing SQLite history. Concise lifecycle lines go to the journal; the full Pi JSON stream does not — the Pi session remains the canonical detailed history.

`resume-session TASK_ID` opens the newest run for that task with `pi --session PATH`, whether the run succeeded or ended unsuccessfully. Active runs cannot be opened interactively. Failed, cancelled, and timed-out sessions remain available once the run is no longer active.

## Notifications (ntfy)

Optional machine-global push Notifications when a Run finishes. Delivery is best-effort and never changes Run status.

```console
pi-task notify
pi-task notify --url https://ntfy.example --topic pi-task --on fail --no-test
pi-task notify --show
```

| Piece | Behavior |
| --- | --- |
| Config | `$XDG_CONFIG_HOME/pi-task/notify.toml` — base URL, topic, optional bearer token, triggers |
| Triggers | `success` (`succeeded` only), `fail` (any other terminal status), `both`, or `none` (soft off) |
| Scope | Every terminal Run (`scheduled` and `manual`), including lock-conflict terminals |
| Title | `{Task name or id}: {Succeeded\|Failed}` |
| Body | Status, source, duration; error when present |
| Tags | `white_check_mark` / `x` |

Absent config means no publish attempts. Re-run `notify` to edit; saved values are defaults. After save, the wizard can send a test Notification (skippable; failure warns but keeps the config).

## Storage and environment

| Location | Content |
| --- | --- |
| `$XDG_CONFIG_HOME/pi-task/tasks` | Human-readable task TOML (source of truth) |
| `$XDG_CONFIG_HOME/pi-task/notify.toml` | Optional global ntfy Notification config |
| `$XDG_CONFIG_HOME/systemd/user` | Generated `pi-task-*.service` / `.timer` units |
| `$XDG_STATE_HOME/pi-task/runs.db` | Run and session metadata |
| `$XDG_RUNTIME_DIR/pi-task/locks` | Same-task and working-directory locks |
| Pi's normal session storage | Conversation transcripts |

For isolated testing, dependency executables can be selected through `PATH` or:

`PI_TASK_PI_EXECUTABLE`, `PI_TASK_SYSTEMCTL_EXECUTABLE`, `PI_TASK_SYSTEMD_ANALYZE_EXECUTABLE`, `PI_TASK_SYSTEMD_RUN_EXECUTABLE`, `PI_TASK_EXECUTABLE`, `PI_TASK_JOURNALCTL_EXECUTABLE`, `PI_TASK_LOGINCTL_EXECUTABLE`.

A Pi override must also be present in the systemd user manager environment for scheduled runs. Point `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `XDG_DATA_HOME`, and `XDG_RUNTIME_DIR` at temporary directories to keep diagnostics and automated tests out of real user locations.

**No automated test or release command silently creates real scheduled work.** The default test suite uses fakes and isolated XDG directories. The optional smoke test creates only a paused disposable task and always removes it (see below).

## Development

```console
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest
uv build
```

The automated suite uses isolated XDG directories and fake Pi / systemd-family executables. It does not call real Pi or modify the developer's systemd state. Smoke tests are excluded by default (`-m 'not smoke'`).

### Opt-in smoke test

Exercise a disposable **paused** task against the real user manager and Pi (never part of normal CI):

```console
PI_TASK_SMOKE=1 uv run pytest -m smoke
# optional model pin:
PI_TASK_SMOKE=1 PI_TASK_SMOKE_MODEL=provider/model uv run pytest -m smoke
```

Requires a working Pi login, models visible to Pi, and a running systemd user manager. The test runs one manual invocation and removes the task afterward. It will not leave an enabled timer.

### Continuous integration

GitHub Actions runs lockfile sync, Ruff, ty, pytest (without smoke), packaging build, and installation verification on Python 3.14 only.

## License

[MIT](LICENSE)
