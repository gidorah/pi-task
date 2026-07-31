from __future__ import annotations

import subprocess
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path
from typing import Literal, NoReturn

import typer
from rich.console import Console
from rich.table import Table

from pi_task.db import RunRecord, get_run, latest_run_for_task, list_runs, open_db
from pi_task.doctor import run_doctor
from pi_task.journal import missing_journal_message, read_run_journal
from pi_task.notify import (
    NotificationPayload,
    NotifyConfig,
    NotifyError,
    format_config_for_display,
    load_notify_config,
    parse_trigger_choice,
    publish_notification,
    save_notify_config,
    trigger_choice,
)
from pi_task.runner import cancel_run, execute_task_run, heal_orphaned_run, resolve_pi
from pi_task.tasks import (
    THINKING_LEVELS,
    TRUST_POLICIES,
    Task,
    TaskError,
    all_tasks,
    create_task,
    get_task,
    has_saved_project_trust,
    list_available_models,
    parse_timeout,
    pause_task,
    remove_task,
    resume_task,
    scheduling_state,
    start_manual_run,
    sync_tasks,
    update_task,
    validate_task,
)

# Interactive prompt labels: short enough for a terminal, with examples or choices.
_PROMPT_TASK_ID = "Task ID (e.g. daily-review; lowercase letters, numbers, hyphens)"
_PROMPT_WORKING_DIRECTORY = "Working directory (e.g. ~/projects/app; ~ is expanded)"
_PROMPT_SOURCE = "Prompt source (inline|file)"
_PROMPT_INLINE = "Prompt"
_PROMPT_FILE = "Prompt file (e.g. ~/prompts/review.md; ~ is expanded)"
_PROMPT_SCHEDULE_KIND = "Schedule kind (calendar|interval)"
_PROMPT_CALENDAR = "Calendar schedule (e.g. daily, Mon..Fri 09:00)"
_PROMPT_INTERVAL = "Interval (e.g. 15m, 2h; minimum 1m)"
_PROMPT_MODEL = "Model (provider/model, e.g. anthropic/claude-sonnet-4-5)"
_PROMPT_MODEL_CHOICE = "Model (number or provider/model)"
_PROMPT_THINKING = f"Thinking ({'|'.join(THINKING_LEVELS)})"
_PROMPT_TIMEOUT = "Timeout (e.g. 30m, 2h, 45s)"
_PROMPT_TRUST = f"Trust ({'|'.join(TRUST_POLICIES)})"

app = typer.Typer(
    help="Schedule local Pi agent prompts with systemd user timers.",
    no_args_is_help=True,
)


def version_callback(value: bool) -> None:
    if value:
        typer.echo(f"pi-task {version('pi-task')}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Manage recurring local Pi tasks."""


@app.command()
def doctor() -> None:
    """Check whether this machine is ready to run pi-task."""
    if not run_doctor():
        raise typer.Exit(code=1)


def _notify_error(error: NotifyError) -> NoReturn:
    typer.echo(f"Error: {error}", err=True)
    raise typer.Exit(code=1)


@app.command()
def notify(
    show: bool = typer.Option(
        False,
        "--show",
        help="Print the current notify config (token redacted) and exit.",
    ),
    url: str | None = typer.Option(
        None,
        "--url",
        help="ntfy base URL (e.g. https://ntfy.example).",
    ),
    topic: str | None = typer.Option(None, "--topic", help="ntfy topic name."),
    token: str | None = typer.Option(
        None,
        "--token",
        help="Optional bearer token for the ntfy server.",
    ),
    clear_token: bool = typer.Option(
        False,
        "--clear-token",
        help="Remove a stored bearer token.",
    ),
    on: str | None = typer.Option(
        None,
        "--on",
        help="Triggers to enable: success, fail, both, or none.",
    ),
    send_test: bool | None = typer.Option(
        None,
        "--test/--no-test",
        help="Send a test notification after saving (default: prompt interactively).",
        show_default=False,
    ),
) -> None:
    """Configure global Run Notifications via ntfy."""
    try:
        existing = load_notify_config()
        if show:
            if existing is None:
                typer.echo("Notifications are not configured.")
                raise typer.Exit()
            typer.echo(format_config_for_display(existing))
            raise typer.Exit()

        if token is not None and clear_token:
            raise NotifyError("provide at most one of --token and --clear-token")

        flagged = (
            any(value is not None for value in (url, topic, token, on, send_test)) or clear_token
        )
        interactive = not flagged

        resolved_url = url
        resolved_topic = topic
        resolved_token: str | None
        if clear_token:
            resolved_token = None
        elif token is not None:
            resolved_token = token
        else:
            resolved_token = existing.token if existing is not None else None
        resolved_on = on

        if interactive:
            default_topic = existing.topic if existing is not None else ""
            default_triggers = trigger_choice(existing) if existing is not None else "fail"
            if existing is not None:
                resolved_url = typer.prompt(
                    "ntfy base URL",
                    default=existing.base_url,
                ).strip()
            else:
                resolved_url = typer.prompt(
                    "ntfy base URL (e.g. https://ntfy.example)",
                ).strip()
            topic_prompt = "ntfy topic"
            if default_topic:
                resolved_topic = typer.prompt(topic_prompt, default=default_topic).strip()
            else:
                resolved_topic = typer.prompt(topic_prompt).strip()
            token_hint = "set" if existing is not None and existing.token else "not set"
            token_entered = typer.prompt(
                f"Bearer token (optional; currently {token_hint}; empty keeps, '-' clears)",
                default="",
                show_default=False,
            ).strip()
            if token_entered == "-":
                resolved_token = None
            elif token_entered:
                resolved_token = token_entered
            resolved_on = typer.prompt(
                "Notify on (success|fail|both|none)",
                default=default_triggers,
            ).strip()

        if resolved_url is None:
            if existing is None:
                raise NotifyError("base URL is required")
            resolved_url = existing.base_url
        if resolved_topic is None:
            if existing is None:
                raise NotifyError("topic is required")
            resolved_topic = existing.topic
        if resolved_on is None:
            if existing is None:
                on_success, on_fail = parse_trigger_choice("fail")
            else:
                on_success, on_fail = existing.on_success, existing.on_fail
        else:
            on_success, on_fail = parse_trigger_choice(resolved_on)

        config = save_notify_config(
            NotifyConfig(
                base_url=resolved_url,
                topic=resolved_topic,
                token=resolved_token,
                on_success=on_success,
                on_fail=on_fail,
            )
        )
        typer.echo("Saved notification config:")
        typer.echo(format_config_for_display(config))

        should_test = send_test
        if should_test is None:
            should_test = interactive and typer.confirm("Send test notification?", default=True)
        if should_test:
            payload = NotificationPayload(
                title="pi-task: notify configured",
                body=(f"Test notification from pi-task.\nTriggers: {trigger_choice(config)}"),
                tags=("white_check_mark",),
            )
            try:
                publish_notification(config, payload)
            except NotifyError as error:
                typer.echo(f"Warning: test notification failed: {error}", err=True)
            else:
                typer.echo("Test notification sent.")
    except NotifyError as error:
        _notify_error(error)


def _required(value: str | None, label: str) -> str:
    return value if value is not None else typer.prompt(label)


def _task_error(error: TaskError) -> NoReturn:
    typer.echo(f"Error: {error}", err=True)
    raise typer.Exit(code=1)


def _echo_run_summary(record: RunRecord, *, verbose: bool = False) -> None:
    """Print a run summary shared by `run` and `runs`.

    Distinguishes source and terminal status, and surfaces snapshot identity,
    timing, prompt hash, usage, and session fields when present.
    """
    duration = f"{record.duration_ms / 1000:.1f}s" if record.duration_ms is not None else "unknown"
    if record.input_tokens is None and record.output_tokens is None:
        tokens = "unavailable"
    else:
        tokens = f"in={record.input_tokens or 0} out={record.output_tokens or 0}"
    cost = "unavailable" if record.cost_total is None else str(record.cost_total)

    # (label, value, always_print) — empty optional fields are omitted unless always.
    fields: list[tuple[str, str | None, bool]] = [
        ("Run", record.id, True),
        ("Task", record.task_id, True),
        ("Source", record.source, True),
        ("Status", record.status, True),
        ("Started", record.started_at, True),
        ("Duration", duration, True),
    ]
    if verbose:
        fields.extend(
            [
                ("Model", record.model, True),
                ("Thinking", record.thinking, True),
                ("Prompt hash", record.prompt_hash, True),
                ("Tokens", tokens, True),
                ("Cost", cost, True),
                ("Session", record.session_id, True),
                ("Session path", record.session_path, True),
                ("Unit", record.unit_name, False),
                ("Invocation", record.invocation_id, False),
                ("Error", record.error, False),
            ]
        )
    else:
        fields.extend(
            [
                ("Session", record.session_id, False),
                ("Session path", record.session_path, False),
                ("Error", record.error, False),
            ]
        )
    for label, value, always in fields:
        if value is None or value == "":
            if always:
                typer.echo(f"{label}:")
            continue
        typer.echo(f"{label}: {value}")


def _resolve_prompt(
    prompt: str | None,
    prompt_file: str | None,
    *,
    interactive: bool,
) -> tuple[Literal["inline", "file"], str]:
    if prompt is not None and prompt_file is not None:
        raise TaskError("provide exactly one of --prompt and --prompt-file")
    if prompt is None and prompt_file is None:
        if not interactive:
            raise TaskError("provide exactly one of --prompt and --prompt-file")
        source = typer.prompt(_PROMPT_SOURCE, default="inline").strip().lower()
        if source == "inline":
            prompt = typer.prompt(_PROMPT_INLINE)
        elif source == "file":
            prompt_file = typer.prompt(_PROMPT_FILE)
        else:
            raise TaskError("prompt source must be inline or file")
    if prompt_file is not None:
        return "file", str(Path(prompt_file).expanduser().resolve())
    return "inline", prompt or ""


def _resolve_schedule(
    calendar: str | None,
    interval: str | None,
    *,
    interactive: bool,
) -> tuple[Literal["calendar", "interval"], str]:
    if calendar is not None and interval is not None:
        raise TaskError("provide exactly one of --calendar and --interval")
    if interval is not None:
        return "interval", interval
    if calendar is not None:
        return "calendar", calendar
    if not interactive:
        raise TaskError("provide exactly one of --calendar and --interval")
    kind = typer.prompt(_PROMPT_SCHEDULE_KIND, default="calendar").strip().lower()
    if kind == "calendar":
        return "calendar", _required(None, _PROMPT_CALENDAR)
    if kind == "interval":
        return "interval", _required(None, _PROMPT_INTERVAL)
    raise TaskError("schedule kind must be calendar or interval")


def _resolve_model(model: str | None) -> str:
    """Resolve ``--model`` or prompt interactively with a live inventory.

    Non-interactive callers that already passed ``--model`` get that value
    unchanged. When the flag is omitted, list models from the user-manager
    environment so the user can pick by index or type ``provider/model``.
    Listing failures are explained but still allow manual entry.
    """
    if model is not None:
        return model
    try:
        available = list_available_models()
    except TaskError as error:
        typer.echo(f"Could not list models: {error}", err=True)
        typer.echo(
            "Authenticate providers with Pi, run `pi-task doctor`, "
            "or type provider/model manually.",
            err=True,
        )
        return typer.prompt(_PROMPT_MODEL).strip()

    typer.echo("Available models:")
    for index, name in enumerate(available, start=1):
        typer.echo(f"  {index}. {name}")
    choice = typer.prompt(_PROMPT_MODEL_CHOICE).strip()
    if choice.isdigit():
        selected = int(choice)
        if 1 <= selected <= len(available):
            return available[selected - 1]
        raise TaskError(
            f"model selection must be between 1 and {len(available)} "
            f"(or type a full provider/model name)"
        )
    return choice


@app.command()
def add(
    task_id: str | None = typer.Argument(
        None,
        help="Lowercase machine-safe task ID (e.g. daily-review).",
    ),
    name: str | None = typer.Option(None, "--name", help="Optional display name."),
    working_directory: str | None = typer.Option(
        None,
        "--working-directory",
        "-C",
        help="Directory in which Pi will work (e.g. ~/projects/app; ~ is expanded).",
    ),
    prompt: str | None = typer.Option(None, "--prompt", help="Inline prompt text."),
    prompt_file: str | None = typer.Option(
        None,
        "--prompt-file",
        help="Path to a prompt file (e.g. ~/prompts/review.md; ~ is expanded).",
    ),
    calendar: str | None = typer.Option(
        None,
        "--calendar",
        help="systemd calendar expression (e.g. daily, Mon..Fri 09:00).",
    ),
    interval: str | None = typer.Option(
        None,
        "--interval",
        help="Elapsed interval duration such as 15m or 2h (minimum 1m).",
    ),
    catch_up: bool | None = typer.Option(
        None,
        "--catch-up/--no-catch-up",
        help="For calendar tasks, coalesce missed occurrences after downtime.",
        show_default=False,
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Available model as provider/model (e.g. anthropic/claude-sonnet-4-5).",
    ),
    thinking: str = typer.Option(
        "medium",
        "--thinking",
        help=f"Pi thinking level: {', '.join(THINKING_LEVELS)}.",
    ),
    timeout: str = typer.Option(
        "30m",
        "--timeout",
        help="Run timeout duration such as 30m, 2h, or 45s.",
    ),
    trust: str = typer.Option(
        "inherit",
        "--trust",
        help=f"Project trust: {', '.join(TRUST_POLICIES)}.",
    ),
    paused: bool = typer.Option(False, "--paused", help="Create without activating the timer."),
    accept: bool = typer.Option(False, "--yes", "-y", help="Accept the schedule preview."),
) -> None:
    """Create and optionally activate a scheduled task."""
    try:
        resolved_id = _required(task_id, _PROMPT_TASK_ID)
        resolved_directory = Path(
            _required(working_directory, _PROMPT_WORKING_DIRECTORY)
        ).expanduser()
        interactive = task_id is None or working_directory is None
        prompt_kind, prompt_value = _resolve_prompt(
            prompt,
            prompt_file,
            interactive=interactive or (prompt is None and prompt_file is None),
        )
        schedule_kind, schedule_value = _resolve_schedule(
            calendar,
            interval,
            interactive=interactive or (calendar is None and interval is None),
        )
        if schedule_kind == "interval":
            if catch_up is True:
                raise TaskError("interval schedules do not support calendar catch-up")
            resolved_catch_up = False
        else:
            resolved_catch_up = True if catch_up is None else catch_up
        resolved_model = _resolve_model(model)
        if interactive:
            # Surface defaults and allowed values/examples in the guided wizard.
            thinking = typer.prompt(_PROMPT_THINKING, default=thinking).strip()
            timeout = typer.prompt(_PROMPT_TIMEOUT, default=timeout).strip()
            trust = typer.prompt(_PROMPT_TRUST, default=trust).strip()
        task = Task(
            task_id=resolved_id,
            name=name,
            working_directory=resolved_directory.resolve(),
            prompt_kind=prompt_kind,
            prompt=prompt_value,
            schedule_kind=schedule_kind,
            schedule=schedule_value,
            catch_up=resolved_catch_up,
            model=resolved_model,
            thinking=thinking,
            timeout_seconds=parse_timeout(timeout),
            trust=trust,
            paused=paused,
        )
        preview = validate_task(task)
        if task.trust == "inherit" and not has_saved_project_trust(task.working_directory):
            typer.echo(
                "Warning: project has no saved Pi trust decision; "
                "the non-interactive trust default will apply."
            )
        if preview is not None:
            typer.echo("Upcoming occurrences:")
            for occurrence in preview.occurrences:
                typer.echo(f"  {occurrence}")
        else:
            typer.echo(
                f"Interval schedule: every {task.schedule} "
                "(first fire one interval after activation)."
            )
        if not accept and not typer.confirm("Create this task?"):
            typer.echo("Task was not created.")
            raise typer.Exit()
        create_task(task)
    except TaskError as error:
        _task_error(error)
    state = "paused" if task.paused else "enabled"
    typer.echo(f"Created {state} task {task.task_id}.")


@app.command("list")
def list_tasks() -> None:
    """List configured tasks and their scheduling state."""
    try:
        tasks = all_tasks()
    except TaskError as error:
        _task_error(error)
    if not tasks:
        typer.echo("No tasks configured.")
        return
    table = Table()
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Schedule")
    table.add_column("State")
    for task in tasks:
        state = scheduling_state(task)
        table.add_row(task.task_id, task.name or "", task.schedule_summary, state.summary)
    Console().print(table)


@app.command()
def show(task_id: str = typer.Argument(..., help="Task ID to inspect.")) -> None:
    """Show one task's configuration and current scheduling state."""
    try:
        task = get_task(task_id)
        state = scheduling_state(task)
    except TaskError as error:
        _task_error(error)
    schedule_label = "Schedule"
    schedule_value = task.schedule_summary
    if task.schedule_kind == "calendar":
        schedule_value = f"{task.schedule} (catch-up {'on' if task.catch_up else 'off'})"
    rows = (
        ("ID", task.task_id),
        ("Name", task.name or ""),
        ("Working directory", str(task.working_directory)),
        ("Prompt file", task.prompt) if task.prompt_kind == "file" else ("Prompt", task.prompt),
        (schedule_label, schedule_value),
        ("Model", task.model),
        ("Thinking", task.thinking),
        ("Timeout", f"{task.timeout_seconds} seconds"),
        ("Trust", task.trust),
        ("State", state.detail),
    )
    for label, value in rows:
        typer.echo(f"{label}: {value}")


@app.command()
def edit(
    task_id: str = typer.Argument(..., help="Task ID to edit."),
    name: str | None = typer.Option(None, "--name", help="Optional display name."),
    clear_name: bool = typer.Option(False, "--clear-name", help="Remove the display name."),
    working_directory: str | None = typer.Option(
        None,
        "--working-directory",
        "-C",
        help="Directory in which Pi will work.",
    ),
    prompt: str | None = typer.Option(None, "--prompt", help="Inline prompt text."),
    prompt_file: str | None = typer.Option(None, "--prompt-file", help="Path to a prompt file."),
    calendar: str | None = typer.Option(None, "--calendar", help="systemd calendar expression."),
    interval: str | None = typer.Option(
        None,
        "--interval",
        help="Elapsed interval duration such as 15m or 2h.",
    ),
    catch_up: bool | None = typer.Option(
        None,
        "--catch-up/--no-catch-up",
        help="For calendar tasks, coalesce missed occurrences after downtime.",
        show_default=False,
    ),
    model: str | None = typer.Option(None, "--model", help="Available model as provider/model."),
    thinking: str | None = typer.Option(None, "--thinking", help="Pi thinking level."),
    timeout: str | None = typer.Option(None, "--timeout", help="Run timeout, such as 30m or 2h."),
    trust: str | None = typer.Option(
        None, "--trust", help="Project trust: inherit, approve, or deny."
    ),
) -> None:
    """Validate and apply configuration changes atomically."""
    try:
        if name is not None and clear_name:
            raise TaskError("provide at most one of --name and --clear-name")
        if prompt is not None and prompt_file is not None:
            raise TaskError("provide exactly one of --prompt and --prompt-file")
        if calendar is not None and interval is not None:
            raise TaskError("provide exactly one of --calendar and --interval")
        previous = get_task(task_id)
        updated = previous
        if clear_name:
            updated = replace(updated, name=None)
        elif name is not None:
            updated = replace(updated, name=name)
        if working_directory is not None:
            updated = replace(
                updated,
                working_directory=Path(working_directory).expanduser().resolve(),
            )
        if prompt is not None or prompt_file is not None:
            prompt_kind, prompt_value = _resolve_prompt(prompt, prompt_file, interactive=False)
            updated = replace(updated, prompt_kind=prompt_kind, prompt=prompt_value)
        if calendar is not None:
            updated = replace(
                updated,
                schedule_kind="calendar",
                schedule=calendar,
                catch_up=previous.catch_up if previous.schedule_kind == "calendar" else True,
            )
        if interval is not None:
            updated = replace(
                updated,
                schedule_kind="interval",
                schedule=interval,
                catch_up=False,
            )
        if catch_up is not None:
            if updated.schedule_kind != "calendar":
                raise TaskError("catch-up applies only to calendar schedules")
            updated = replace(updated, catch_up=catch_up)
        if model is not None:
            updated = replace(updated, model=model)
        if thinking is not None:
            updated = replace(updated, thinking=thinking)
        if timeout is not None:
            updated = replace(updated, timeout_seconds=parse_timeout(timeout))
        if trust is not None:
            updated = replace(updated, trust=trust)
        if updated == previous:
            raise TaskError("no changes requested")
        validate_task(updated)
        update_task(previous, updated)
    except TaskError as error:
        _task_error(error)
    typer.echo(f"Updated task {task_id}.")


@app.command()
def pause(task_id: str = typer.Argument(..., help="Task ID to pause.")) -> None:
    """Suppress future scheduled runs without cancelling an active run."""
    try:
        pause_task(task_id)
    except TaskError as error:
        _task_error(error)
    typer.echo(f"Paused task {task_id}.")


@app.command()
def resume(task_id: str = typer.Argument(..., help="Task ID to resume.")) -> None:
    """Schedule only future occurrences for a paused task."""
    try:
        resume_task(task_id)
    except TaskError as error:
        _task_error(error)
    typer.echo(f"Resumed task {task_id}.")


@app.command()
def remove(
    task_id: str = typer.Argument(..., help="Task ID to remove."),
    accept: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
) -> None:
    """Stop scheduling and delete the task definition and generated units."""
    try:
        get_task(task_id)
        if not accept and not typer.confirm(f"Remove task {task_id}?"):
            typer.echo("Task was not removed.")
            raise typer.Exit()
        remove_task(task_id)
    except TaskError as error:
        _task_error(error)
    typer.echo(f"Removed task {task_id}.")


@app.command()
def sync() -> None:
    """Reconcile generated units with task definitions and remove orphans."""
    try:
        result = sync_tasks()
    except TaskError as error:
        _task_error(error)
    typer.echo(
        f"Synchronized {result.tasks} task{'s' if result.tasks != 1 else ''}"
        f" and removed {result.orphans_removed} orphan unit"
        f"{'s' if result.orphans_removed != 1 else ''}."
    )


@app.command("run")
def run_task(
    task_id: str = typer.Argument(..., help="Task ID to run immediately."),
) -> None:
    """Start a task once in the background without touching its timer."""
    try:
        submission = start_manual_run(task_id)
    except TaskError as error:
        _task_error(error)
        raise
    typer.echo(f"Started manual run {submission.run_id} for task {task_id} in the background.")


@app.command()
def cancel(run_id: str = typer.Argument(..., help="Active run ID to stop.")) -> None:
    """Stop an active scheduled or manual run through its systemd unit."""
    try:
        record, unit = cancel_run(run_id)
    except TaskError as error:
        _task_error(error)
        raise
    if record.status == "cancelled":
        typer.echo(f"Cancelled run {run_id} (unit {unit}).")
        raise typer.Exit()
    if record.status == "running":
        typer.echo(
            f"Stop requested for run {run_id} (unit {unit}); "
            "the run has not yet recorded a terminal status.",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(f"Stopped run {run_id} (unit {unit}); status: {record.status}.")
    raise typer.Exit(code=0 if record.status == "succeeded" else 1)


@app.command("_run-scheduled", hidden=True)
def run_scheduled(
    task_id: str = typer.Argument(..., help="Task ID to run."),
    source: str = typer.Option(
        "scheduled",
        "--source",
        help="Run source recorded in history: scheduled or manual.",
    ),
    run_id: str | None = typer.Option(
        None,
        "--run-id",
        help="Optional run identifier; generated when omitted.",
    ),
) -> None:
    """Run one task through the thin Pi wrapper (scheduled or manual entrypoint)."""
    try:
        if source == "scheduled":
            code = execute_task_run(task_id, source="scheduled", run_id=run_id)
        elif source == "manual":
            code = execute_task_run(task_id, source="manual", run_id=run_id)
        else:
            raise TaskError("run source must be scheduled or manual")
    except TaskError as error:
        _task_error(error)
        raise
    raise typer.Exit(code=code)


@app.command()
def runs(
    task_id: str | None = typer.Argument(
        None,
        help="Optional task ID to filter run history.",
    ),
    limit: int = typer.Option(20, "--limit", help="Maximum runs to display."),
) -> None:
    """List recorded runs and their Pi session information."""
    try:
        with open_db() as connection:
            records = list_runs(connection, task_id=task_id, limit=limit)
    except TaskError as error:
        _task_error(error)
    if not records:
        typer.echo("No runs recorded.")
        return
    for index, record in enumerate(records):
        if index:
            typer.echo("")
        _echo_run_summary(record, verbose=True)


@app.command()
def logs(
    run_id: str = typer.Argument(..., help="Run ID whose journal lines should be shown."),
) -> None:
    """Show journald output for one run's systemd invocation.

    Prefers the recorded ``INVOCATION_ID`` so repeated scheduled activations of the
    same unit and collected transient manual units stay distinct. Missing or
    expired journal entries are explained without changing retained run history.
    """
    try:
        with open_db() as connection:
            record = get_run(connection, run_id)
        if record is None:
            raise TaskError(f"run {run_id!r} does not exist")
        text = read_run_journal(record)
    except TaskError as error:
        _task_error(error)
        raise
    if not text.strip():
        typer.echo(missing_journal_message(record), err=True)
        raise typer.Exit(code=1)
    typer.echo(text.rstrip("\n"))


@app.command("resume-session")
def resume_session(
    task_id: str = typer.Argument(
        ...,
        help="Task ID whose newest run's Pi session should be opened.",
    ),
) -> None:
    """Open the newest run for a task through Pi's interactive session interface.

    Selection is by recency only — succeeded, failed, cancelled, and timed-out
    runs are all eligible when they still have a recorded session file.
    """
    try:
        with open_db() as connection:
            record = latest_run_for_task(connection, task_id)
        if record is None:
            raise TaskError(
                f"task {task_id!r} has no runs; run it first with `pi-task run {task_id}`"
            )
        if record.status == "running":
            # Interrupted wrappers leave running rows; free locks mean no live writer.
            # Always re-read: another process may have reaped the row already.
            heal_orphaned_run(record.id)
            with open_db() as connection:
                record = latest_run_for_task(connection, task_id)
            if record is None:
                raise TaskError(
                    f"task {task_id!r} has no runs; run it first with `pi-task run {task_id}`"
                )
            if record.status == "running":
                raise TaskError(f"newest run {record.id!r} for task {task_id!r} is still active")
        if not record.session_path:
            raise TaskError(
                f"newest run {record.id!r} for task {task_id!r} "
                f"({record.status}) has no recorded Pi session to resume; "
                f"inspect with `pi-task runs {task_id}`"
            )
        session_path = Path(record.session_path)
        if not session_path.is_file():
            raise TaskError(
                f"recorded session file is missing for newest run {record.id!r}: "
                f"{session_path}; inspect with `pi-task runs {task_id}`"
            )
        pi = resolve_pi()
    except TaskError as error:
        _task_error(error)
        raise
    raise typer.Exit(code=subprocess.call([pi, "--session", str(session_path)]))
