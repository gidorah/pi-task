from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

RunSource = Literal["scheduled", "manual"]
RunStatus = Literal["running", "succeeded", "failed", "timed_out", "cancelled", "skipped"]

_SCHEMA_VERSION = 2

# SQL migrations must be idempotent: executescript commits and connections use
# isolation_level=None, so a crash between applying DDL and recording the version
# must not brick the next open.
_MIGRATIONS_SQL: dict[int, str] = {
    1: """
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        duration_ms INTEGER,
        session_id TEXT,
        session_path TEXT,
        session_name TEXT,
        prompt_hash TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        model TEXT NOT NULL,
        thinking TEXT NOT NULL,
        input_tokens INTEGER,
        output_tokens INTEGER,
        cache_read_tokens INTEGER,
        cache_write_tokens INTEGER,
        cost_total REAL,
        error TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_runs_task_started ON runs (task_id, started_at DESC);
    CREATE INDEX IF NOT EXISTS idx_runs_started ON runs (started_at DESC);
    """,
}


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _add_column_if_missing(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    if column not in _table_columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_v2(connection: sqlite3.Connection) -> None:
    """Add unit/invocation identity columns without failing if already present."""
    _add_column_if_missing(connection, "runs", "unit_name", "TEXT")
    _add_column_if_missing(connection, "runs", "invocation_id", "TEXT")


@dataclass(frozen=True)
class RunRecord:
    id: str
    task_id: str
    source: RunSource
    status: RunStatus
    started_at: str
    finished_at: str | None
    duration_ms: int | None
    session_id: str | None
    session_path: str | None
    session_name: str | None
    prompt_hash: str
    snapshot_json: str
    model: str
    thinking: str
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    cost_total: float | None
    error: str | None
    unit_name: str | None = None
    invocation_id: str | None = None


@dataclass(frozen=True)
class NewRun:
    id: str
    task_id: str
    source: RunSource
    started_at: str
    session_name: str
    prompt_hash: str
    snapshot_json: str
    model: str
    thinking: str
    unit_name: str | None = None
    invocation_id: str | None = None


@dataclass(frozen=True)
class RunCompletion:
    status: RunStatus
    finished_at: str
    duration_ms: int
    session_id: str | None
    session_path: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    cost_total: float | None
    error: str | None


def _state_home() -> Path:
    value = os.environ.get("XDG_STATE_HOME")
    return Path(value).expanduser() if value else Path.home() / ".local" / "state"


def database_path() -> Path:
    return _state_home() / "pi-task" / "runs.db"


def _connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, isolation_level=None, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    # WAL allows concurrent readers/writers when different tasks run together.
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def migrate(connection: sqlite3.Connection) -> None:
    # DDL steps commit independently under isolation_level=None; each version must
    # be safe to re-run if the version row was not recorded.
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY NOT NULL,"
        "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        ")"
    )
    current = connection.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
    ).fetchone()
    applied = int(current[0]) if current is not None else 0
    for version in range(applied + 1, _SCHEMA_VERSION + 1):
        if version in _MIGRATIONS_SQL:
            connection.executescript(_MIGRATIONS_SQL[version])
        elif version == 2:
            _migrate_v2(connection)
        else:
            raise RuntimeError(f"missing migration for schema version {version}")
        connection.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))


@contextmanager
def open_db(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    connection = _connect(path)
    try:
        migrate(connection)
        yield connection
    finally:
        connection.close()


def insert_run(connection: sqlite3.Connection, run: NewRun) -> None:
    connection.execute(
        """
        INSERT INTO runs (
            id, task_id, source, status, started_at, finished_at, duration_ms,
            session_id, session_path, session_name, prompt_hash, snapshot_json,
            model, thinking, input_tokens, output_tokens, cache_read_tokens,
            cache_write_tokens, cost_total, error, unit_name, invocation_id
        ) VALUES (
            ?, ?, ?, 'running', ?, NULL, NULL,
            NULL, NULL, ?, ?, ?,
            ?, ?, NULL, NULL, NULL,
            NULL, NULL, NULL, ?, ?
        )
        """,
        (
            run.id,
            run.task_id,
            run.source,
            run.started_at,
            run.session_name,
            run.prompt_hash,
            run.snapshot_json,
            run.model,
            run.thinking,
            run.unit_name,
            run.invocation_id,
        ),
    )


def finish_run(
    connection: sqlite3.Connection,
    run_id: str,
    completion: RunCompletion,
    *,
    only_if_running: bool = False,
) -> bool:
    """Apply a terminal status. Returns True if a row was updated.

    When ``only_if_running`` is True, skip rows already finalized (avoids
    overwriting a live wrapper's later completion in rare races).
    """
    query = """
        UPDATE runs SET
            status = ?,
            finished_at = ?,
            duration_ms = ?,
            session_id = ?,
            session_path = ?,
            input_tokens = ?,
            output_tokens = ?,
            cache_read_tokens = ?,
            cache_write_tokens = ?,
            cost_total = ?,
            error = ?
        WHERE id = ?
        """
    if only_if_running:
        query += " AND status = 'running'"
    cursor = connection.execute(
        query,
        (
            completion.status,
            completion.finished_at,
            completion.duration_ms,
            completion.session_id,
            completion.session_path,
            completion.input_tokens,
            completion.output_tokens,
            completion.cache_read_tokens,
            completion.cache_write_tokens,
            completion.cost_total,
            completion.error,
            run_id,
        ),
    )
    return cursor.rowcount > 0


def _row_to_run(row: sqlite3.Row) -> RunRecord:
    # open_db always migrates before use; v2 columns are always present (may be NULL).
    return RunRecord(
        id=row["id"],
        task_id=row["task_id"],
        source=cast("RunSource", row["source"]),
        status=cast("RunStatus", row["status"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        duration_ms=row["duration_ms"],
        session_id=row["session_id"],
        session_path=row["session_path"],
        session_name=row["session_name"],
        prompt_hash=row["prompt_hash"],
        snapshot_json=row["snapshot_json"],
        model=row["model"],
        thinking=row["thinking"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cache_read_tokens=row["cache_read_tokens"],
        cache_write_tokens=row["cache_write_tokens"],
        cost_total=row["cost_total"],
        error=row["error"],
        unit_name=row["unit_name"],
        invocation_id=row["invocation_id"],
    )


def list_runs(
    connection: sqlite3.Connection,
    *,
    task_id: str | None = None,
    limit: int = 50,
    oldest_first: bool = False,
) -> list[RunRecord]:
    """List up to ``limit`` runs, optionally displaying them chronologically."""
    where = " WHERE task_id = ?" if task_id is not None else ""
    params: tuple[str | int, ...] = (task_id, limit) if task_id is not None else (limit,)
    if oldest_first:
        query = (
            "SELECT * FROM ("
            f"SELECT * FROM runs{where} "
            "ORDER BY started_at DESC, id DESC LIMIT ?"
            ") ORDER BY started_at ASC, id ASC"
        )
    else:
        query = f"SELECT * FROM runs{where} ORDER BY started_at DESC, id DESC LIMIT ?"
    rows = connection.execute(query, params).fetchall()
    return [_row_to_run(row) for row in rows]


def get_run(connection: sqlite3.Connection, run_id: str) -> RunRecord | None:
    row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _row_to_run(row) if row is not None else None


def latest_run_for_task(connection: sqlite3.Connection, task_id: str) -> RunRecord | None:
    """Return the newest run for a task by start time, regardless of status."""
    rows = list_runs(connection, task_id=task_id, limit=1)
    return rows[0] if rows else None


def list_running_runs(connection: sqlite3.Connection) -> list[RunRecord]:
    rows = connection.execute("SELECT * FROM runs WHERE status = 'running'").fetchall()
    return [_row_to_run(row) for row in rows]


def _snapshot_working_directory(snapshot_json: str) -> str | None:
    try:
        data = json.loads(snapshot_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("working_directory")
    return value if isinstance(value, str) else None


def abandon_orphaned_runs(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    working_directory: str,
    finished_at: str | None = None,
    except_run_id: str | None = None,
) -> list[str]:
    """Mark running rows proven dead by holding their locks as failed.

    Holding the task and working-directory locks means no live wrapper owns those
    scopes, so any still-``running`` history for the same task or normalized
    working directory is an interrupted-wrapper orphan.
    """
    finished = finished_at or datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    abandoned: list[str] = []
    for record in list_running_runs(connection):
        if except_run_id is not None and record.id == except_run_id:
            continue
        snapshot_cwd = _snapshot_working_directory(record.snapshot_json)
        same_task = record.task_id == task_id
        if snapshot_cwd is not None:
            try:
                normalized_snapshot = str(Path(snapshot_cwd).expanduser().resolve())
            except OSError:
                normalized_snapshot = snapshot_cwd
        else:
            normalized_snapshot = None
        same_directory = (
            normalized_snapshot is not None and normalized_snapshot == working_directory
        )
        if not same_task and not same_directory:
            continue
        started = record.started_at
        try:
            start_moment = datetime.fromisoformat(started.replace("Z", "+00:00"))
            end_moment = datetime.fromisoformat(finished.replace("Z", "+00:00"))
            duration_ms = max(0, int((end_moment - start_moment).total_seconds() * 1000))
        except ValueError:
            duration_ms = 0
        updated = finish_run(
            connection,
            record.id,
            RunCompletion(
                status="failed",
                finished_at=finished,
                duration_ms=duration_ms,
                session_id=record.session_id,
                session_path=record.session_path,
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                cache_read_tokens=record.cache_read_tokens,
                cache_write_tokens=record.cache_write_tokens,
                cost_total=record.cost_total,
                error="wrapper interrupted; run abandoned after stale lock recovery",
            ),
            only_if_running=True,
        )
        if updated:
            abandoned.append(record.id)
    return abandoned
