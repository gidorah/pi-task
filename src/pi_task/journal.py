"""Invocation-scoped journal access for recorded runs."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pi_task.db import RunRecord
from pi_task.tasks import TaskError

# Pad time windows so unit+time fallback still captures nearby journal lines.
_SINCE_PADDING = timedelta(seconds=5)
_UNTIL_PADDING = timedelta(seconds=30)


@dataclass(frozen=True)
class JournalQuery:
    """How journalctl should select lines for one run."""

    arguments: list[str]
    description: str


@dataclass(frozen=True)
class JournalResult:
    """Outcome of reading journald for a run without mutating run history."""

    text: str
    empty: bool
    query: JournalQuery


def resolve_journalctl() -> str:
    override = os.environ.get("PI_TASK_JOURNALCTL_EXECUTABLE")
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.absolute())
        raise TaskError("PI_TASK_JOURNALCTL_EXECUTABLE does not identify an executable file")
    executable = shutil.which("journalctl")
    if executable is None:
        raise TaskError("journalctl was not found on PATH")
    return executable


def _parse_moment(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_journal_time(moment: datetime) -> str:
    # journalctl accepts ISO-8601; normalize to UTC with second precision.
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def build_journal_query(record: RunRecord) -> JournalQuery:
    """Select the correct systemd invocation for a recorded run.

    Prefer ``_SYSTEMD_INVOCATION_ID`` when the wrapper ran under a unit. Fall back
    to unit name plus a tight time window for older rows or non-systemd starts that
    still recorded a unit name. Repeated scheduled activations of the same unit are
    distinguished by invocation id; collected transient manual units remain
    addressable the same way after ``--collect`` removes the unit object.
    """
    if record.invocation_id:
        return JournalQuery(
            arguments=[f"_SYSTEMD_INVOCATION_ID={record.invocation_id}"],
            description=f"invocation {record.invocation_id}",
        )

    if record.unit_name:
        arguments = [f"--unit={record.unit_name}"]
        started = _parse_moment(record.started_at)
        if started is not None:
            since = started - _SINCE_PADDING
            arguments.append(f"--since={_format_journal_time(since)}")
            finished = _parse_moment(record.finished_at) if record.finished_at else None
            until = (finished or datetime.now(UTC)) + _UNTIL_PADDING
            arguments.append(f"--until={_format_journal_time(until)}")
        return JournalQuery(
            arguments=arguments,
            description=f"unit {record.unit_name} around run start",
        )

    raise TaskError(
        f"run {record.id!r} has no recorded systemd invocation or unit; "
        "journal selection is unavailable (the run may have started outside systemd)"
    )


def read_run_journal(record: RunRecord) -> JournalResult:
    """Fetch journal lines for a run. Never mutates SQLite run history."""
    query = build_journal_query(record)
    journalctl = resolve_journalctl()
    command = [
        journalctl,
        "--user",
        "--no-pager",
        "--output=short-iso",
        *query.arguments,
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
            timeout=30,
        )
    except OSError as error:
        raise TaskError(f"could not read journal: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise TaskError("journalctl timed out while reading run logs") from error

    text = result.stdout
    # journalctl returns 0 for empty matches; non-zero usually means access/runtime errors.
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if not detail:
            detail = f"journalctl exited with status {result.returncode}"
        # Treat "no entries" style failures as empty rather than hard errors when possible.
        lowered = detail.lower()
        if "no entries" in lowered or "no match" in lowered:
            return JournalResult(text="", empty=True, query=query)
        raise TaskError(detail)

    empty = not text.strip()
    return JournalResult(text=text, empty=empty, query=query)


def missing_journal_message(record: RunRecord, query: JournalQuery) -> str:
    """Explain an empty journal without implying run history was lost."""
    return (
        f"No journal entries found for run {record.id} ({query.description}). "
        "Run history in SQLite is unchanged; journald may have rotated or expired "
        "these lines, or the run may not have produced journal output."
    )
