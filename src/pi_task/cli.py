from __future__ import annotations

import typer

from pi_task import __version__
from pi_task.doctor import run_doctor

app = typer.Typer(
    help="Schedule local Pi agent prompts with systemd user timers.",
    no_args_is_help=True,
)


def version_callback(value: bool) -> None:
    if value:
        typer.echo(f"pi-task {__version__}")
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
