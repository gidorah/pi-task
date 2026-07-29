from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pi_task.db import (
    NewRun,
    RunCompletion,
    RunRecord,
    RunSource,
    RunStatus,
    abandon_orphaned_runs,
    finish_run,
    get_run,
    insert_run,
    open_db,
)
from pi_task.events import (
    StreamObservation,
    classify_run_status,
    consume_event_line,
    unexpected_output_diagnostic,
)
from pi_task.locks import LockConflict, RunLocks, acquire_run_locks, normalize_working_directory
from pi_task.notify import maybe_notify_run
from pi_task.tasks import (
    PROCESS_DRAIN_SECONDS,
    PROCESS_KILL_WAIT_SECONDS,
    PROCESS_TERM_GRACE_SECONDS,
    Task,
    TaskError,
    get_task,
    stop_user_unit,
    supervising_unit,
    unit_name_for_run,
)


@dataclass(frozen=True)
class TaskSnapshot:
    task_id: str
    name: str | None
    working_directory: str
    prompt_kind: str
    prompt: str
    schedule_kind: str
    schedule: str
    catch_up: bool
    model: str
    thinking: str
    timeout_seconds: int
    trust: str
    paused: bool

    @classmethod
    def from_task(cls, task: Task) -> TaskSnapshot:
        return cls(
            task_id=task.task_id,
            name=task.name,
            working_directory=str(task.working_directory),
            prompt_kind=task.prompt_kind,
            prompt=task.prompt,
            schedule_kind=task.schedule_kind,
            schedule=task.schedule,
            catch_up=task.catch_up,
            model=task.model,
            thinking=task.thinking,
            timeout_seconds=task.timeout_seconds,
            trust=task.trust,
            paused=task.paused,
        )


def resolve_pi() -> str:
    override = os.environ.get("PI_TASK_PI_EXECUTABLE")
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.absolute())
        raise TaskError("PI_TASK_PI_EXECUTABLE does not identify an executable file")
    executable = shutil.which("pi")
    if executable is None:
        raise TaskError("pi was not found on PATH")
    return executable


def _agent_dir() -> Path:
    configured = os.environ.get("PI_CODING_AGENT_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".pi" / "agent"


def resolve_prompt_text(task: Task) -> tuple[str, str]:
    if task.prompt_kind == "inline":
        text = task.prompt
    else:
        path = Path(task.prompt)
        try:
            text = path.read_text()
        except OSError as error:
            raise TaskError(f"could not read prompt file {path}: {error}") from error
    digest = hashlib.sha256(text.encode()).hexdigest()
    return text, digest


def _glob_escape(value: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", value)


def find_session_path(
    session_id: str,
    timestamp: str | None = None,
    cwd: str | None = None,
) -> Path | None:
    """Locate the ordinary Pi session file without moving it."""
    sessions_root = _agent_dir() / "sessions"
    safe_id = _glob_escape(session_id)
    if timestamp and cwd:
        safe_cwd = cwd.lstrip("/").replace("/", "-").replace("\\", "-").replace(":", "-")
        encoded = f"--{safe_cwd}--"
        file_timestamp = timestamp.replace(":", "-").replace(".", "-")
        expected = sessions_root / encoded / f"{file_timestamp}_{session_id}.jsonl"
        if expected.is_file():
            return expected
        session_dir = expected.parent
        if session_dir.is_dir():
            matches = sorted(session_dir.glob(f"*_{safe_id}.jsonl"))
            if matches:
                return matches[-1]
    if sessions_root.is_dir():
        matches = sorted(sessions_root.glob(f"**/*_{safe_id}.jsonl"))
        if matches:
            return matches[-1]
    return None


def build_pi_command(
    *,
    pi_executable: str,
    snapshot: TaskSnapshot,
    prompt_text: str,
    session_name: str,
) -> list[str]:
    command = [
        pi_executable,
        "--mode",
        "json",
        "--model",
        snapshot.model,
        "--thinking",
        snapshot.thinking,
        "--name",
        session_name,
    ]
    if snapshot.trust == "approve":
        command.append("--approve")
    elif snapshot.trust == "deny":
        command.append("--no-approve")
    command.append(prompt_text)
    return command


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# Short poll interval so SIGTERM cancel is observed without a long communicate().
_CANCEL_POLL_SECONDS = 0.25
# Brief wait after systemctl stop for the wrapper to finalize cooperatively.
_CANCEL_FINALIZE_WAIT_SECONDS = 2.0


def _stop_process_group(
    process: subprocess.Popen[str],
    *,
    grace_seconds: float = float(PROCESS_TERM_GRACE_SECONDS),
    kill_wait_seconds: float = float(PROCESS_KILL_WAIT_SECONDS),
) -> None:
    """Terminate Pi and its process group, escalating to kill after a short grace period."""
    if process.poll() is not None:
        return
    with suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    with suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(process.pid, signal.SIGKILL)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=kill_wait_seconds)


def _coerce_stream_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _drain_process(
    process: subprocess.Popen[str],
    *,
    timeout: float = float(PROCESS_DRAIN_SECONDS),
) -> tuple[str, str]:
    """Read remaining stdout/stderr without hanging forever after a forced stop."""
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return _coerce_stream_text(stdout), _coerce_stream_text(stderr)
    except subprocess.TimeoutExpired as exc:
        # Preserve any bytes already read; closing pipes after this is best-effort.
        stdout = _coerce_stream_text(exc.stdout)
        stderr = _coerce_stream_text(exc.stderr)
        with suppress(OSError):
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=1.0)
        return stdout, stderr


def _error_for_status(
    status: RunStatus,
    *,
    timeout_seconds: int,
    observation: StreamObservation,
    exit_code: int | None,
    error: str | None,
) -> str | None:
    """Align error text with the terminal status used in history."""
    if status == "succeeded":
        return None
    if status == "timed_out":
        if error and "timed out" in error.lower():
            return error
        return f"timed out after {timeout_seconds} seconds"
    if status == "cancelled":
        if error and "cancel" in error.lower():
            return error
        return "run cancelled"
    if error:
        return error
    if observation.final_stop_reason and observation.final_stop_reason != "stop":
        return f"final stop reason: {observation.final_stop_reason}"
    if exit_code not in (0, None):
        return f"Pi exited with status {exit_code}"
    if not observation.saw_assistant:
        return "missing final assistant response"
    if diagnostic := unexpected_output_diagnostic(observation):
        return diagnostic
    return f"run ended with status {status}"


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def current_invocation_id() -> str | None:
    """Return systemd's per-activation invocation id when the wrapper runs under a unit."""
    value = os.environ.get("INVOCATION_ID", "").strip()
    return value or None


@dataclass(frozen=True)
class PreparedRun:
    """Immutable startup context for one wrapper invocation."""

    task: Task
    snapshot: TaskSnapshot
    prompt_text: str
    prompt_hash: str
    snapshot_json: str
    source: RunSource
    run_id: str
    session_name: str
    started: datetime
    unit_name: str
    invocation_id: str | None

    @classmethod
    def build(
        cls,
        task_id: str,
        *,
        source: RunSource,
        run_id: str | None = None,
    ) -> PreparedRun:
        task = get_task(task_id)
        snapshot = TaskSnapshot.from_task(task)
        prompt_text, prompt_hash = resolve_prompt_text(task)
        resolved_id = run_id or str(uuid.uuid4())
        return cls(
            task=task,
            snapshot=snapshot,
            prompt_text=prompt_text,
            prompt_hash=prompt_hash,
            snapshot_json=json.dumps(asdict(snapshot), ensure_ascii=True, sort_keys=True),
            source=source,
            run_id=resolved_id,
            session_name=f"pi-task:{task.task_id}:{resolved_id[:8]}",
            started=_now(),
            unit_name=unit_name_for_run(task_id=task.task_id, run_id=resolved_id, source=source),
            invocation_id=current_invocation_id(),
        )

    def as_new_run(self) -> NewRun:
        return NewRun(
            id=self.run_id,
            task_id=self.task.task_id,
            source=self.source,
            started_at=_iso(self.started),
            session_name=self.session_name,
            prompt_hash=self.prompt_hash,
            snapshot_json=self.snapshot_json,
            model=self.snapshot.model,
            thinking=self.snapshot.thinking,
            unit_name=self.unit_name,
            invocation_id=self.invocation_id,
        )


def execute_task_run(
    task_id: str,
    *,
    source: RunSource,
    run_id: str | None = None,
) -> int:
    prepared = PreparedRun.build(task_id, source=source, run_id=run_id)
    try:
        with acquire_run_locks(prepared.task.task_id, prepared.task.working_directory):
            return _execute_locked_run(prepared)
    except LockConflict as conflict:
        return _record_lock_conflict(prepared, conflict)
    except (OSError, RuntimeError) as error:
        raise TaskError(f"could not acquire run locks: {error}") from error


def _reap_orphaned_runs(prepared: PreparedRun) -> None:
    """Clear history rows left running after a crashed wrapper for these locks."""
    working_directory = normalize_working_directory(prepared.task.working_directory)
    with open_db() as connection:
        abandoned = abandon_orphaned_runs(
            connection,
            task_id=prepared.task.task_id,
            working_directory=working_directory,
            except_run_id=prepared.run_id,
        )
    for orphan_id in abandoned:
        _log(f"run {orphan_id}: abandoned after stale lock recovery")
        _notify_terminal_run(orphan_id)


def heal_orphaned_run(run_id: str) -> bool:
    """If a run is marked running but its locks are free, abandon it and return True.

    Locks are held for the whole abandon so a concurrent live wrapper cannot be
    misclassified between probe and history update.
    """
    with open_db() as connection:
        record = get_run(connection, run_id)
        if record is None or record.status != "running":
            return False
        try:
            snapshot = json.loads(record.snapshot_json)
        except json.JSONDecodeError:
            return False
        working_directory = snapshot.get("working_directory")
        if not isinstance(working_directory, str):
            return False
        directory = Path(working_directory)
        locks = RunLocks(task_id=record.task_id, working_directory=directory)
        try:
            locks.acquire()
        except LockConflict:
            return False
        try:
            abandoned = abandon_orphaned_runs(
                connection,
                task_id=record.task_id,
                working_directory=normalize_working_directory(directory),
            )
        finally:
            locks.release()
        for orphan_id in abandoned:
            _notify_terminal_run(orphan_id)
        return run_id in abandoned


def _require_running_run(run_id: str) -> RunRecord:
    with open_db() as connection:
        record = get_run(connection, run_id)
    if record is None:
        raise TaskError(f"run {run_id!r} does not exist")
    if record.status != "running":
        raise TaskError(f"run {run_id!r} is not active (status: {record.status})")
    return record


def _force_cancel_terminal(record: RunRecord) -> bool:
    """If the wrapper is dead (locks free), record cancelled for a stopped unit.

    Used when systemctl stop succeeded but the wrapper never finalized (for
    example after TimeoutStopSec SIGKILL). Avoids orphan heal rewriting cancel
    as a generic failed abandonment.
    """
    try:
        snapshot = json.loads(record.snapshot_json)
    except json.JSONDecodeError:
        return False
    working_directory = snapshot.get("working_directory")
    if not isinstance(working_directory, str):
        return False
    directory = Path(working_directory)
    locks = RunLocks(task_id=record.task_id, working_directory=directory)
    try:
        locks.acquire()
    except LockConflict:
        return False
    try:
        finished = _now()
        try:
            start_moment = datetime.fromisoformat(record.started_at.replace("Z", "+00:00"))
            duration_ms = max(0, int((finished - start_moment).total_seconds() * 1000))
        except ValueError:
            duration_ms = 0
        with open_db() as connection:
            updated = finish_run(
                connection,
                record.id,
                RunCompletion(
                    status="cancelled",
                    finished_at=_iso(finished),
                    duration_ms=duration_ms,
                    session_id=record.session_id,
                    session_path=record.session_path,
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                    cache_read_tokens=record.cache_read_tokens,
                    cache_write_tokens=record.cache_write_tokens,
                    cost_total=record.cost_total,
                    error="run cancelled (unit stopped; wrapper did not finalize)",
                ),
                only_if_running=True,
            )
        if updated:
            _notify_terminal_run(record.id)
        return updated
    finally:
        locks.release()


def cancel_run(run_id: str) -> tuple[RunRecord, str]:
    """Stop a recorded active run through its systemd user unit.

    Returns the latest run record and the unit that was stopped. Pausing a task
    does not cancel work; only this path targets an active unit. SIGTERM from
    stop is recorded as cancelled (distinct from wrapper timeout).
    """
    _require_running_run(run_id)

    # Free locks with a still-running row means a crashed wrapper, not live work.
    if heal_orphaned_run(run_id):
        with open_db() as connection:
            healed = get_run(connection, run_id)
        if healed is None:
            raise TaskError(f"run {run_id!r} does not exist")
        raise TaskError(
            f"run {run_id!r} was no longer active (status: {healed.status}); "
            "abandoned after stale lock recovery"
        )

    record = _require_running_run(run_id)
    unit = supervising_unit(
        recorded_unit=record.unit_name,
        task_id=record.task_id,
        run_id=record.id,
        source=record.source,
    )
    # systemctl stop is synchronous for oneshot services; TimeoutStopSec bounds
    # the unit, and the client timeout sits above that.
    stop_user_unit(unit)

    # Prefer the wrapper's cooperative finalize when it still runs.
    poll_deadline = time.monotonic() + _CANCEL_FINALIZE_WAIT_SECONDS
    finished: RunRecord | None = None
    while time.monotonic() < poll_deadline:
        with open_db() as connection:
            finished = get_run(connection, run_id)
        if finished is None:
            raise TaskError(f"run {run_id!r} disappeared after stop")
        if finished.status != "running":
            return finished, unit
        time.sleep(0.05)

    with open_db() as connection:
        finished = get_run(connection, run_id)
    if finished is None:
        raise TaskError(f"run {run_id!r} disappeared after stop")
    if finished.status != "running":
        return finished, unit

    # Unit is stopped but history still running: claim cancelled if locks are free.
    if _force_cancel_terminal(finished):
        with open_db() as connection:
            claimed = get_run(connection, run_id)
        if claimed is not None:
            return claimed, unit
    with open_db() as connection:
        finished = get_run(connection, run_id)
    if finished is None:
        raise TaskError(f"run {run_id!r} disappeared after stop")
    return finished, unit


def _execute_locked_run(prepared: PreparedRun) -> int:
    _reap_orphaned_runs(prepared)
    with open_db() as connection:
        insert_run(connection, prepared.as_new_run())

    task = prepared.task
    snapshot = prepared.snapshot
    run_id = prepared.run_id
    started = prepared.started
    _log(
        f"run {run_id}: starting {prepared.source} run for task {task.task_id} "
        f"(unit={prepared.unit_name}"
        + (f", invocation={prepared.invocation_id}" if prepared.invocation_id else "")
        + f", prompt_hash={prepared.prompt_hash[:12]})"
    )
    pi_executable = resolve_pi()
    command = build_pi_command(
        pi_executable=pi_executable,
        snapshot=snapshot,
        prompt_text=prepared.prompt_text,
        session_name=prepared.session_name,
    )

    observation = StreamObservation()
    timed_out = False
    cancelled = False
    exit_code: int | None = None
    error: str | None = None
    process: subprocess.Popen[str] | None = None
    previous_handlers: list[tuple[signal.Signals, Any]] = []
    # Filled once Pi starts so signal handlers can map late TERM to timed_out.
    deadline_at: list[float | None] = [None]

    def _request_cancel(signum: int, _frame: object) -> None:
        nonlocal cancelled, timed_out
        # RuntimeMaxSec and other SIGTERMs after the task deadline are timeouts,
        # not explicit user cancellation via pi-task cancel.
        at = deadline_at[0]
        if at is not None and time.monotonic() >= at:
            timed_out = True
            _log(f"run {run_id}: received signal {signum} after deadline; timing out")
        else:
            cancelled = True
            _log(f"run {run_id}: received signal {signum}; cancelling")
        if process is not None and process.poll() is None:
            # Best-effort async stop from a signal handler: TERM only here.
            # The main thread escalates via _stop_process_group after communicate.
            with suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(process.pid, signal.SIGTERM)

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers.append((signum, signal.getsignal(signum)))
        signal.signal(signum, _request_cancel)

    try:
        try:
            process = subprocess.Popen(
                command,
                cwd=str(task.working_directory),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            error = f"could not start Pi: {exc}"
            _log(f"run {run_id}: {error}")
            return _finalize(
                run_id=run_id,
                started=started,
                status="failed",
                observation=observation,
                error=error,
            )

        try:
            assert process.stdout is not None
            assert process.stderr is not None
            # Poll in short slices so a SIGTERM cancel flag is observed promptly.
            # A single long communicate() can delay signal handling until timeout.
            deadline = time.monotonic() + task.timeout_seconds
            deadline_at[0] = deadline
            stdout = ""
            stderr = ""
            while True:
                remaining = deadline - time.monotonic()
                # Deadline first: sticky timeout beats a concurrent cancel flag.
                if remaining <= 0 or timed_out:
                    timed_out = True
                    _stop_process_group(process)
                    stdout, stderr = _drain_process(process)
                    error = f"timed out after {task.timeout_seconds} seconds"
                    _log(f"run {run_id}: {error}")
                    break
                if cancelled:
                    _stop_process_group(process)
                    stdout, stderr = _drain_process(process)
                    break
                try:
                    stdout, stderr = process.communicate(
                        timeout=min(_CANCEL_POLL_SECONDS, max(remaining, 0.01))
                    )
                    break
                except subprocess.TimeoutExpired:
                    continue
            exit_code = process.returncode
            for line in (stdout or "").splitlines():
                consume_event_line(observation, line)
            if diagnostic := unexpected_output_diagnostic(observation):
                _log(f"run {run_id}: {diagnostic}")
            # Keep Pi diagnostics brief; never forward the JSON event stream.
            if (stderr or "").strip():
                for line in stderr.splitlines():
                    if line.strip():
                        _log(f"run {run_id}: pi: {line.strip()}")
        except Exception as exc:
            if not timed_out and not cancelled:
                error = error or f"wrapper failed: {exc}"
            _log(f"run {run_id}: {error or exc}")
            if process.poll() is None:
                _stop_process_group(process)
                _drain_process(process)
            status = classify_run_status(
                process_exit_code=process.returncode,
                timed_out=timed_out,
                cancelled=cancelled,
                observation=observation,
            )
            return _finalize(
                run_id=run_id,
                started=started,
                status=status,
                observation=observation,
                error=_error_for_status(
                    status,
                    timeout_seconds=task.timeout_seconds,
                    observation=observation,
                    exit_code=process.returncode,
                    error=error,
                ),
            )

        status = classify_run_status(
            process_exit_code=exit_code,
            timed_out=timed_out,
            cancelled=cancelled,
            observation=observation,
        )
        return _finalize(
            run_id=run_id,
            started=started,
            status=status,
            observation=observation,
            error=_error_for_status(
                status,
                timeout_seconds=task.timeout_seconds,
                observation=observation,
                exit_code=exit_code,
                error=error,
            ),
        )
    except BaseException as exc:
        # Ensure the run never remains stuck in "running" after wrapper death.
        status = classify_run_status(
            process_exit_code=exit_code if process is not None else None,
            timed_out=timed_out,
            cancelled=cancelled,
            observation=observation,
        )
        if not timed_out and not cancelled:
            if isinstance(exc, Exception):
                error = error or f"wrapper interrupted: {exc}"
            else:
                error = error or f"wrapper interrupted by {exc.__class__.__name__}"
        aligned = _error_for_status(
            status,
            timeout_seconds=task.timeout_seconds,
            observation=observation,
            exit_code=exit_code if process is not None else None,
            error=error,
        )
        _log(f"run {run_id}: {aligned or exc}")
        if isinstance(exc, Exception):
            return _finalize(
                run_id=run_id,
                started=started,
                status=status,
                observation=observation,
                error=aligned,
            )
        _finalize(
            run_id=run_id,
            started=started,
            status=status,
            observation=observation,
            error=aligned,
        )
        raise
    finally:
        for signum, handler in previous_handlers:
            signal.signal(signum, handler)


def _record_lock_conflict(prepared: PreparedRun, conflict: LockConflict) -> int:
    """Record a lock conflict without starting Pi.

    Scheduled conflicts are skipped (exit 0). Manual conflicts fail clearly (exit 1).
    """
    if prepared.source == "scheduled":
        status: RunStatus = "skipped"
        error = f"skipped: {conflict.message}"
        exit_code = 0
    else:
        status = "failed"
        error = conflict.message
        exit_code = 1

    _log(f"run {prepared.run_id}: {error}")
    with open_db() as connection:
        insert_run(connection, prepared.as_new_run())
        finished = _now()
        duration_ms = max(0, int((finished - prepared.started).total_seconds() * 1000))
        finish_run(
            connection,
            prepared.run_id,
            RunCompletion(
                status=status,
                finished_at=_iso(finished),
                duration_ms=duration_ms,
                session_id=None,
                session_path=None,
                input_tokens=None,
                output_tokens=None,
                cache_read_tokens=None,
                cache_write_tokens=None,
                cost_total=None,
                error=error,
            ),
        )
    _log(f"run {prepared.run_id}: finished status={status} in {duration_ms}ms")
    _notify_terminal_run(prepared.run_id)
    return exit_code


def _notify_terminal_run(run_id: str) -> None:
    """Push a Notification for a terminal Run when configured. Best-effort."""
    with open_db() as connection:
        record = get_run(connection, run_id)
    if record is not None:
        maybe_notify_run(record)


def _finalize(
    *,
    run_id: str,
    started: datetime,
    status: RunStatus,
    observation: StreamObservation,
    error: str | None,
) -> int:
    finished = _now()
    duration_ms = max(0, int((finished - started).total_seconds() * 1000))
    session_path: str | None = None
    if observation.session_id:
        found = find_session_path(
            observation.session_id,
            observation.session_timestamp,
            observation.session_cwd,
        )
        if found is not None:
            session_path = str(found)

    usage = observation.usage
    input_tokens = usage.input_tokens if observation.saw_assistant else None
    output_tokens = usage.output_tokens if observation.saw_assistant else None
    cache_read = usage.cache_read_tokens if observation.saw_assistant else None
    cache_write = usage.cache_write_tokens if observation.saw_assistant else None
    with open_db() as connection:
        finish_run(
            connection,
            run_id,
            RunCompletion(
                status=status,
                finished_at=_iso(finished),
                duration_ms=duration_ms,
                session_id=observation.session_id,
                session_path=session_path,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                cost_total=usage.cost_total,
                error=error,
            ),
        )

    if observation.session_id:
        if session_path:
            _log(f"run {run_id}: session {observation.session_id} at {session_path}")
        else:
            _log(f"run {run_id}: session {observation.session_id} (path not found)")
    usage_parts: list[str] = []
    if input_tokens is not None or output_tokens is not None:
        usage_parts.append(f"tokens in={input_tokens or 0} out={output_tokens or 0}")
    if usage.cost_total is not None:
        usage_parts.append(f"cost={usage.cost_total}")
    usage_suffix = f" ({', '.join(usage_parts)})" if usage_parts else ""
    _log(f"run {run_id}: finished status={status} in {duration_ms}ms{usage_suffix}")
    _notify_terminal_run(run_id)
    return 0 if status == "succeeded" else 1
