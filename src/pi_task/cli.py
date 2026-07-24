from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from pi_task.doctor import run_doctor
from pi_task.tasks import (
    Task,
    TaskError,
    all_tasks,
    create_task,
    get_task,
    has_saved_project_trust,
    parse_timeout,
    scheduling_state,
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


def _task_error(error: TaskError) -> None:
    typer.echo(f"Error: {error}", err=True)
    raise typer.Exit(code=1)


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
    model: str | None = typer.Option(None, "--model", help="Available model as provider/model."),
    thinking: str = typer.Option("medium", "--thinking", help="Pi thinking level."),
    timeout: str = typer.Option("30m", "--timeout", help="Run timeout, such as 30m or 2h."),
    trust: str = typer.Option(
        "inherit", "--trust", help="Project trust: inherit, approve, or deny."
    ),
    paused: bool = typer.Option(False, "--paused", help="Create without activating the timer."),
    accept: bool = typer.Option(False, "--yes", "-y", help="Accept the schedule preview."),
) -> None:
    """Create and optionally activate a calendar task."""
    try:
        resolved_id = _required(task_id, "Task ID")
        resolved_directory = Path(_required(working_directory, "Working directory")).expanduser()
        if prompt is not None and prompt_file is not None:
            raise TaskError("provide exactly one of --prompt and --prompt-file")
        if prompt is None and prompt_file is None:
            source = typer.prompt("Prompt source", default="inline").strip().lower()
            if source == "inline":
                prompt = typer.prompt("Prompt")
            elif source == "file":
                prompt_file = typer.prompt("Prompt file")
            else:
                raise TaskError("prompt source must be inline or file")
        if prompt_file is not None:
            prompt_kind = "file"
            prompt_value = str(Path(prompt_file).expanduser().resolve())
        else:
            prompt_kind = "inline"
            prompt_value = prompt or ""
        task = Task(
            task_id=resolved_id,
            name=name,
            working_directory=resolved_directory.resolve(),
            prompt_kind=prompt_kind,
            prompt=prompt_value,
            calendar=_required(calendar, "Calendar schedule"),
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
        typer.echo("Upcoming occurrences:")
        for occurrence in preview.occurrences:
            typer.echo(f"  {occurrence}")
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
        table.add_row(task.task_id, task.name or "", task.calendar, state.summary)
    Console().print(table)


@app.command()
def show(task_id: str = typer.Argument(..., help="Task ID to inspect.")) -> None:
    """Show one task's configuration and current scheduling state."""
    try:
        task = get_task(task_id)
        state = scheduling_state(task)
    except TaskError as error:
        _task_error(error)
    rows = (
        ("ID", task.task_id),
        ("Name", task.name or ""),
        ("Working directory", str(task.working_directory)),
        ("Prompt file", task.prompt) if task.prompt_kind == "file" else ("Prompt", task.prompt),
        ("Calendar", task.calendar),
        ("Model", task.model),
        ("Thinking", task.thinking),
        ("Timeout", f"{task.timeout_seconds} seconds"),
        ("Trust", task.trust),
        ("State", state.detail),
    )
    for label, value in rows:
        typer.echo(f"{label}: {value}")


@app.command("_run-scheduled", hidden=True)
def run_scheduled(task_id: str) -> None:
    """Scheduled run boundary implemented by the run-history slice."""
    typer.echo(f"Scheduled runs are not implemented for {task_id}.", err=True)
    raise typer.Exit(code=1)
