"""Unit tests for journal selection and schema migration."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pi_task.db import RunRecord, open_db
from pi_task.journal import build_journalctl_args, journal_selection_label, missing_journal_message
from pi_task.tasks import TaskError


def _record(**overrides: object) -> RunRecord:
    base: dict[str, object] = {
        "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "task_id": "daily-review",
        "source": "scheduled",
        "status": "succeeded",
        "started_at": "2030-01-07T09:00:00.000Z",
        "finished_at": "2030-01-07T09:05:00.000Z",
        "duration_ms": 300_000,
        "session_id": None,
        "session_path": None,
        "session_name": "n",
        "prompt_hash": "abc",
        "snapshot_json": "{}",
        "model": "acme/rocket",
        "thinking": "high",
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "cost_total": None,
        "error": None,
        "unit_name": "pi-task-daily-review.service",
        "invocation_id": None,
    }
    base.update(overrides)
    return RunRecord(**base)  # type: ignore[arg-type]


def test_build_journalctl_args_prefers_invocation_id() -> None:
    record = _record(invocation_id="ffffffffffffffffffffffffffffffff")
    args = build_journalctl_args(record)
    assert args == ["_SYSTEMD_INVOCATION_ID=ffffffffffffffffffffffffffffffff"]


def test_build_journalctl_args_unit_time_window() -> None:
    record = _record(invocation_id=None)
    args = build_journalctl_args(record)
    assert args[0] == "--unit=pi-task-daily-review.service"
    assert args[1].startswith("--since=")
    assert args[2].startswith("--until=")
    assert "2030-01-07 08:59:55 UTC" in args[1]
    assert "2030-01-07 09:05:30 UTC" in args[2]


def test_build_journalctl_args_recomputes_unit_when_null() -> None:
    record = _record(unit_name=None, invocation_id=None, source="scheduled", task_id="old")
    args = build_journalctl_args(record)
    assert args[0] == "--unit=pi-task-old.service"


def test_build_journalctl_args_recomputes_manual_unit() -> None:
    run_id = "12345678-1234-1234-1234-123456789abc"
    record = _record(
        id=run_id,
        unit_name=None,
        invocation_id=None,
        source="manual",
        task_id="manual-me",
    )
    args = build_journalctl_args(record)
    assert args[0] == "--unit=pi-task-run-manual-me-12345678123412341234123456789abc.service"


def test_build_journalctl_args_fails_closed_on_bad_started_at() -> None:
    record = _record(started_at="not-a-timestamp", invocation_id=None)
    with pytest.raises(TaskError, match="no usable start time"):
        build_journalctl_args(record)


def test_missing_journal_message_mentions_sqlite_unchanged() -> None:
    record = _record(invocation_id="aa" * 16)
    message = missing_journal_message(record)
    assert "No journal entries found" in message
    assert "Run history in SQLite is unchanged" in message
    assert journal_selection_label(record) in message


def test_migrate_v1_to_v2_adds_columns_and_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "runs.db"
    # Hand-build a v1 database without unit/invocation columns.
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        CREATE TABLE runs (
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
        INSERT INTO schema_migrations (version) VALUES (1);
        INSERT INTO runs (
            id, task_id, source, status, started_at, finished_at, duration_ms,
            session_id, session_path, session_name, prompt_hash, snapshot_json,
            model, thinking, input_tokens, output_tokens, cache_read_tokens,
            cache_write_tokens, cost_total, error
        ) VALUES (
            'r1', 't1', 'scheduled', 'succeeded', '2030-01-01T00:00:00.000Z',
            '2030-01-01T00:01:00.000Z', 60000, NULL, NULL, 'n', 'hash', '{}',
            'acme/rocket', 'high', NULL, NULL, NULL, NULL, NULL, NULL
        );
        """
    )
    connection.close()

    with open_db(db_path) as migrated:
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(runs)").fetchall()}
        assert "unit_name" in columns
        assert "invocation_id" in columns
        version = migrated.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        assert version == 2
        row = migrated.execute(
            "SELECT id, unit_name, invocation_id, prompt_hash FROM runs WHERE id = 'r1'"
        ).fetchone()
        assert row[0] == "r1"
        assert row[1] is None
        assert row[2] is None
        assert row[3] == "hash"

    # Re-open is a no-op even if columns already exist (partial-apply recovery).
    with open_db(db_path) as again:
        version = again.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        assert version == 2


def test_migrate_recovers_when_columns_exist_without_version_row(tmp_path: Path) -> None:
    """Crash between ALTER and version insert must not brick the next open."""
    db_path = tmp_path / "runs.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        CREATE TABLE runs (
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
            error TEXT,
            unit_name TEXT,
            invocation_id TEXT
        );
        INSERT INTO schema_migrations (version) VALUES (1);
        """
    )
    connection.close()

    with open_db(db_path) as migrated:
        version = migrated.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        assert version == 2
