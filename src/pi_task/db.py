from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

RunSource = Literal["scheduled", "manual"]
RunStatus = Literal["running", "succeeded", "failed", "timed_out", "cancelled"]

_SCHEMA_VERSION = 1

_MIGRATIONS: dict[int, str] = {
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
    return connection


def migrate(connection: sqlite3.Connection) -> None:
    # executescript() issues its own commits, so migrations themselves must be idempotent.
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
        connection.executescript(_MIGRATIONS[version])
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
            cache_write_tokens, cost_total, error
        ) VALUES (
            ?, ?, ?, 'running', ?, NULL, NULL,
            NULL, NULL, ?, ?, ?,
            ?, ?, NULL, NULL, NULL,
            NULL, NULL, NULL
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
        ),
    )


def finish_run(connection: sqlite3.Connection, run_id: str, completion: RunCompletion) -> None:
    connection.execute(
        """
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
        """,
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


def _row_to_run(row: sqlite3.Row) -> RunRecord:
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
    )


def list_runs(
    connection: sqlite3.Connection,
    *,
    task_id: str | None = None,
    limit: int = 50,
) -> list[RunRecord]:
    if task_id is None:
        rows = connection.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT * FROM runs WHERE task_id = ? ORDER BY started_at DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
    return [_row_to_run(row) for row in rows]


def get_run(connection: sqlite3.Connection, run_id: str) -> RunRecord | None:
    row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _row_to_run(row) if row is not None else None
