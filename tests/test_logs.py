"""CLI tests for invocation-scoped journal access."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from harness import add_task, clear_commands, commands, latest_run_id

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable


def test_run_records_invocation_id_under_systemd_env(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    invocation = "0123456789abcdef0123456789abcdef"
    run_env = {**run_env, "INVOCATION_ID": invocation}
    add_task(run_env, "with-invocation", cli=run_cli)
    clear_commands(run_env)

    executed = run_cli("_run-scheduled", "with-invocation", env=run_env)
    assert executed.returncode == 0, executed.stdout + executed.stderr
    assert "status=succeeded" in executed.stderr

    db_path = Path(run_env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
    with sqlite3.connect(db_path) as connection:
        run_id, unit_name, stored = connection.execute(
            "SELECT id, unit_name, invocation_id FROM runs"
        ).fetchone()
    assert unit_name == "pi-task-with-invocation.service"
    assert stored == invocation

    listed = run_cli("runs", env=run_env)
    assert listed.returncode == 0
    assert f"Invocation: {invocation}" in listed.stdout
    assert run_id in listed.stdout


def test_logs_selects_journal_by_invocation_id(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    invocation = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    run_env = {
        **run_env,
        "INVOCATION_ID": invocation,
        "FAKE_JOURNAL_OUTPUT": (
            "run some-id: starting scheduled run for task logs-target\n"
            "run some-id: finished status=succeeded in 12ms\n"
        ),
    }
    add_task(run_env, "logs-target", cli=run_cli)
    executed = run_cli("_run-scheduled", "logs-target", env=run_env)
    assert executed.returncode == 0, executed.stdout + executed.stderr

    run_id = latest_run_id(run_env)
    clear_commands(run_env)
    logs = run_cli("logs", run_id, env=run_env)
    assert logs.returncode == 0, logs.stdout + logs.stderr
    assert "starting scheduled run" in logs.stdout
    assert "finished status=succeeded" in logs.stdout

    journal_commands = [c for c in commands(run_env) if c[0] == "journalctl"]
    assert len(journal_commands) == 1
    journal = journal_commands[0]
    assert "--user" in journal
    assert f"_SYSTEMD_INVOCATION_ID={invocation}" in journal
    assert not any(arg == "-u" or arg.startswith("--unit=") for arg in journal)


def test_logs_selects_manual_unit_by_invocation(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    invocation = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    run_env = {
        **run_env,
        "INVOCATION_ID": invocation,
        "FAKE_JOURNAL_OUTPUT": "manual run journal line\n",
    }
    add_task(run_env, "manual-logs", cli=run_cli)
    clear_commands(run_env)
    ran = run_cli("run", "manual-logs", env=run_env)
    assert ran.returncode == 0, ran.stdout + ran.stderr

    db_path = Path(run_env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
    with sqlite3.connect(db_path) as connection:
        run_id, unit_name, stored = connection.execute(
            "SELECT id, unit_name, invocation_id FROM runs"
        ).fetchone()
    assert stored == invocation
    assert unit_name is not None
    assert unit_name.startswith("pi-task-run-manual-logs-")
    assert unit_name.endswith(".service")

    clear_commands(run_env)
    logs = run_cli("logs", run_id, env=run_env)
    assert logs.returncode == 0, logs.stdout + logs.stderr
    assert "manual run journal line" in logs.stdout
    journal = next(c for c in commands(run_env) if c[0] == "journalctl")
    assert f"_SYSTEMD_INVOCATION_ID={invocation}" in journal


def test_logs_explains_missing_journal_without_damaging_history(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    invocation = "cccccccccccccccccccccccccccccccc"
    run_env = {
        **run_env,
        "INVOCATION_ID": invocation,
        "FAKE_JOURNAL_EMPTY": "1",
    }
    add_task(run_env, "expired-logs", cli=run_cli)
    executed = run_cli("_run-scheduled", "expired-logs", env=run_env)
    assert executed.returncode == 0

    db_path = Path(run_env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
    with sqlite3.connect(db_path) as connection:
        before = connection.execute(
            "SELECT id, status, prompt_hash, session_id FROM runs"
        ).fetchone()
    run_id = before[0]

    logs = run_cli("logs", run_id, env=run_env)
    assert logs.returncode == 1, logs.stdout + logs.stderr
    assert "No journal entries found" in logs.stderr
    assert "Run history in SQLite is unchanged" in logs.stderr
    assert run_id in logs.stderr

    with sqlite3.connect(db_path) as connection:
        after = connection.execute(
            "SELECT id, status, prompt_hash, session_id FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    assert after == before


def test_logs_falls_back_to_unit_and_time_without_invocation(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    run_env = {
        **run_env,
        "FAKE_JOURNAL_OUTPUT": "fallback journal for scheduled unit\n",
    }
    add_task(run_env, "fallback-logs", cli=run_cli)
    executed = run_cli("_run-scheduled", "fallback-logs", env=run_env)
    assert executed.returncode == 0

    db_path = Path(run_env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
    with sqlite3.connect(db_path) as connection:
        run_id, unit_name, invocation_id = connection.execute(
            "SELECT id, unit_name, invocation_id FROM runs"
        ).fetchone()
    assert unit_name == "pi-task-fallback-logs.service"
    assert invocation_id is None

    clear_commands(run_env)
    logs = run_cli("logs", run_id, env=run_env)
    assert logs.returncode == 0, logs.stdout + logs.stderr
    assert "fallback journal" in logs.stdout
    journal = next(c for c in commands(run_env) if c[0] == "journalctl")
    assert "--user" in journal
    assert f"--unit={unit_name}" in journal
    assert any(a.startswith("--since=") for a in journal)
    assert any(a.startswith("--until=") for a in journal)


def test_logs_recomputes_unit_for_pre_v2_null_identity_columns(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Pre-upgrade rows with NULL unit/invocation still select via recompute."""
    run_env = {
        **run_env,
        "FAKE_JOURNAL_OUTPUT": "legacy row journal\n",
    }
    # Ensure schema exists.
    run_cli("runs", env=run_env)
    run_id = "11111111-1111-1111-1111-111111111111"
    db_path = Path(run_env["XDG_STATE_HOME"]) / "pi-task" / "runs.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO runs (
                id, task_id, source, status, started_at, finished_at, duration_ms,
                session_id, session_path, session_name, prompt_hash, snapshot_json,
                model, thinking, input_tokens, output_tokens, cache_read_tokens,
                cache_write_tokens, cost_total, error, unit_name, invocation_id
            ) VALUES (
                ?, 'legacy-task', 'scheduled', 'succeeded',
                '2030-01-07T09:00:00.000Z', '2030-01-07T09:01:00.000Z', 60000,
                NULL, NULL, 'legacy', 'abc', '{}', 'acme/rocket', 'high',
                NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL
            )
            """,
            (run_id,),
        )
        connection.commit()

    clear_commands(run_env)
    logs = run_cli("logs", run_id, env=run_env)
    assert logs.returncode == 0, logs.stdout + logs.stderr
    assert "legacy row journal" in logs.stdout
    journal = next(c for c in commands(run_env) if c[0] == "journalctl")
    assert "--unit=pi-task-legacy-task.service" in journal
    assert any(a.startswith("--since=") for a in journal)
    assert any(a.startswith("--until=") for a in journal)


def test_logs_rejects_unknown_run(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    run_cli("runs", env=run_env)
    missing = run_cli("logs", "00000000-0000-0000-0000-000000000000", env=run_env)
    assert missing.returncode != 0
    assert "does not exist" in missing.stderr.lower()


def test_logs_soft_no_entries_journalctl_exit(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Non-zero journalctl with 'no entries' text is treated as empty, not hard fail."""
    run_env = {
        **run_env,
        "INVOCATION_ID": "dddddddddddddddddddddddddddddddd",
        "FAKE_JOURNAL_EXIT": "1",
        "FAKE_JOURNAL_OUTPUT": "No entries\n",
    }
    # Fake prints output then exits non-zero; journal treats as empty on "no entries".
    # Actually FAKE_JOURNAL_OUTPUT is printed to stdout; "no entries" detection is on stderr/detail.
    # Adjust: when exit non-zero, detail is stderr or stdout.
    add_task(run_env, "soft-empty", cli=run_cli)
    run_cli("_run-scheduled", "soft-empty", env=run_env)
    run_id = latest_run_id(run_env)

    # Reconfigure fake: empty output, non-zero exit with no-entries via env used as stdout.
    soft_env = {
        **run_env,
        "FAKE_JOURNAL_EXIT": "1",
        "FAKE_JOURNAL_OUTPUT": "No entries",
        "FAKE_JOURNAL_EMPTY": "0",
    }
    # Clear empty flag if set
    soft_env.pop("FAKE_JOURNAL_EMPTY", None)
    logs = run_cli("logs", run_id, env=soft_env)
    assert logs.returncode == 1
    assert "No journal entries found" in logs.stderr
    assert "Run history in SQLite is unchanged" in logs.stderr
