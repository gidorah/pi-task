from __future__ import annotations

import subprocess
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path
from typing import Literal, NoReturn

import typer
from rich.console import Console
from rich.table import Table

from pi_task.db import RunRecord, get_run, list_runs, open_db
from pi_task.doctor import run_doctor
from pi_task.runner import execute_task_run, resolve_pi
from pi_task.tasks import (
    Task,
    TaskError,
    all_tasks,
    create_task,
    get_task,
    has_saved_project_trust,
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


def _required(value: str | None, label: str) -> str:
    return value if value is not None else typer.prompt(label)


def _task_error(error: TaskError) -> NoReturn:
    typer.echo(f"Error: {error}", err=True)
    raise typer.Exit(code=1)


def _echo_run_summary(record: RunRecord, *, verbose: bool = False) -> None:
    """Print a compact run summary shared by `run` and `runs`."""
    duration = f"{record.duration_ms / 1000:.1f}s" if record.duration_ms is not None else "unknown"
    typer.echo(f"Run: {record.id}")
    typer.echo(f"Task: {record.task_id}")
    typer.echo(f"Source: {record.source}")
    typer.echo(f"Status: {record.status}")
    typer.echo(f"Duration: {duration}")
    if verbose:
        if record.input_tokens is None and record.output_tokens is None:
            tokens = "unavailable"
        else:
            tokens = f"in={record.input_tokens or 0} out={record.output_tokens or 0}"
        cost = "unavailable" if record.cost_total is None else str(record.cost_total)
        typer.echo(f"Model: {record.model}")
        typer.echo(f"Tokens: {tokens}")
        typer.echo(f"Cost: {cost}")
        typer.echo(f"Session: {record.session_id or ''}")
        typer.echo(f"Session path: {record.session_path or ''}")
    else:
        if record.session_id:
            typer.echo(f"Session: {record.session_id}")
        if record.session_path:
            typer.echo(f"Session path: {record.session_path}")
        if record.error:
            typer.echo(f"Error: {record.error}")


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
        source = typer.prompt("Prompt source", default="inline").strip().lower()
        if source == "inline":
            prompt = typer.prompt("Prompt")
        elif source == "file":
            prompt_file = typer.prompt("Prompt file")
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
    return "calendar", _required(None, "Calendar schedule")


@app.command()
def add(
    task_id: str | None = typer.Argument(None, help="Lowercase machine-safe task ID."),
    name: str | None = typer.Option(None, "--name", help="Optional display name."),
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
    thinking: str = typer.Option("medium", "--thinking", help="Pi thinking level."),
    timeout: str = typer.Option("30m", "--timeout", help="Run timeout, such as 30m or 2h."),
    trust: str = typer.Option(
        "inherit", "--trust", help="Project trust: inherit, approve, or deny."
    ),
    paused: bool = typer.Option(False, "--paused", help="Create without activating the timer."),
    accept: bool = typer.Option(False, "--yes", "-y", help="Accept the schedule preview."),
) -> None:
    """Create and optionally activate a scheduled task."""
    try:
        resolved_id = _required(task_id, "Task ID")
        resolved_directory = Path(_required(working_directory, "Working directory")).expanduser()
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
        task = Task(
            task_id=resolved_id,
            name=name,
            working_directory=resolved_directory.resolve(),
            prompt_kind=prompt_kind,
            prompt=prompt_value,
            schedule_kind=schedule_kind,
            schedule=schedule_value,
            catch_up=resolved_catch_up,
            model=_required(model, "Model (provider/model)"),
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
    detach: bool = typer.Option(
        False,
        "--detach",
        help="Return after submitting the transient service without waiting.",
    ),
) -> None:
    """Run a task once via a transient systemd user service without touching its timer."""
    try:
        submission = start_manual_run(task_id, detach=detach)
    except TaskError as error:
        _task_error(error)
        raise
    if detach:
        typer.echo(
            f"Submitted manual run {submission.run_id} for task {task_id} (unit {submission.unit})."
        )
        raise typer.Exit()
    try:
        with open_db() as connection:
            record = get_run(connection, submission.run_id)
    except TaskError as error:
        _task_error(error)
        raise
    if record is None:
        detail = submission.service_detail or (
            f"manual run {submission.run_id} produced no history "
            f"(service exit {submission.service_exit_code})"
        )
        typer.echo(f"Error: {detail}", err=True)
        raise typer.Exit(code=1)
    _echo_run_summary(record)
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


@app.command("resume-session")
def resume_session(
    run_id: str = typer.Argument(..., help="Run ID whose Pi session should be opened."),
) -> None:
    """Open a completed run through Pi's normal interactive session interface."""
    try:
        with open_db() as connection:
            record = get_run(connection, run_id)
        if record is None:
            raise TaskError(f"run {run_id!r} does not exist")
        if record.status == "running":
            raise TaskError(f"run {run_id!r} is still active")
        if not record.session_path:
            raise TaskError(f"run {run_id!r} has no recorded Pi session")
        session_path = Path(record.session_path)
        if not session_path.is_file():
            raise TaskError(f"recorded session file is missing: {session_path}")
        pi = resolve_pi()
    except TaskError as error:
        _task_error(error)
        raise
    raise typer.Exit(code=subprocess.call([pi, "--session", str(session_path)]))
