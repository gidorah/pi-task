"""Invocation-scoped journal access for recorded runs."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta

from pi_task.db import RunRecord
from pi_task.tasks import TaskError, resolve_executable, supervising_unit

# Pad time windows so unit+time fallback still captures nearby journal lines.
# Best-effort only: without INVOCATION_ID, adjacent activations of a shared
# scheduled unit may still bleed if they finish within this padding.
_SINCE_PADDING = timedelta(seconds=5)
_UNTIL_PADDING = timedelta(seconds=30)


def _parse_moment(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_journal_time(moment: datetime) -> str:
    # journalctl accepts ISO-8601; normalize to UTC with second precision.
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def journal_selection_label(record: RunRecord) -> str:
    """Human-readable description of how journal lines are selected for a run."""
    if record.invocation_id:
        return f"invocation {record.invocation_id}"
    unit = supervising_unit(
        recorded_unit=record.unit_name,
        task_id=record.task_id,
        run_id=record.id,
        source=record.source,
    )
    return f"unit {unit} around run start"


def build_journalctl_args(record: RunRecord) -> list[str]:
    """Build journalctl match arguments for one recorded run.

    Prefer ``_SYSTEMD_INVOCATION_ID`` when the wrapper recorded it (exact
    activation for shared scheduled units and collected transient manuals).
    Otherwise fall back to supervising unit plus a tight time window. Fail
    closed when unit fallback cannot time-bound the query.
    """
    if record.invocation_id:
        return [f"_SYSTEMD_INVOCATION_ID={record.invocation_id}"]

    unit = supervising_unit(
        recorded_unit=record.unit_name,
        task_id=record.task_id,
        run_id=record.id,
        source=record.source,
    )
    started = _parse_moment(record.started_at)
    if started is None:
        raise TaskError(
            f"run {record.id!r} has no usable start time for journal selection; "
            "cannot safely bound logs for unit "
            f"{unit} without mixing other activations"
        )
    since = started - _SINCE_PADDING
    finished = _parse_moment(record.finished_at) if record.finished_at else None
    until = (finished or datetime.now(UTC)) + _UNTIL_PADDING
    return [
        f"--unit={unit}",
        f"--since={_format_journal_time(since)}",
        f"--until={_format_journal_time(until)}",
    ]


def read_run_journal(record: RunRecord) -> str:
    """Fetch journal lines for a run. Never mutates SQLite run history.

    Returns stdout text (possibly empty). Raises TaskError on hard journalctl
    failures; empty stdout means no retained journal lines for the selection.
    """
    arguments = build_journalctl_args(record)
    journalctl = resolve_executable("journalctl", "PI_TASK_JOURNALCTL_EXECUTABLE")
    command = [
        journalctl,
        "--user",
        "--no-pager",
        "--output=short-iso",
        *arguments,
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
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if not detail:
            detail = f"journalctl exited with status {result.returncode}"
        lowered = detail.lower()
        # Treat "no entries" style failures as empty rather than hard errors.
        if "no entries" in lowered or "no match" in lowered:
            return ""
        raise TaskError(detail)
    return text


def missing_journal_message(record: RunRecord) -> str:
    """Explain an empty journal without implying run history was lost."""
    return (
        f"No journal entries found for run {record.id} "
        f"({journal_selection_label(record)}). "
        "Run history in SQLite is unchanged; journald may have rotated or expired "
        "these lines, or the run may not have produced journal output."
    )
