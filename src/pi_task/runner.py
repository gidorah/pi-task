from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pi_task.db import (
    NewRun,
    RunCompletion,
    RunSource,
    RunStatus,
    finish_run,
    insert_run,
    open_db,
)
from pi_task.events import StreamObservation, classify_run_status, consume_event_line
from pi_task.tasks import Task, TaskError, get_task


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


def _stop_process_group(process: subprocess.Popen[str], *, grace_seconds: float = 5.0) -> None:
    """Terminate Pi and its process group, escalating to kill after a short grace period."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait()


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def execute_task_run(task_id: str, *, source: RunSource) -> int:
    task = get_task(task_id)
    snapshot = TaskSnapshot.from_task(task)
    prompt_text, prompt_hash = resolve_prompt_text(task)
    run_id = str(uuid.uuid4())
    session_name = f"pi-task:{task.task_id}:{run_id[:8]}"
    started = _now()
    snapshot_json = json.dumps(asdict(snapshot), ensure_ascii=True, sort_keys=True)

    with open_db() as connection:
        insert_run(
            connection,
            NewRun(
                id=run_id,
                task_id=task.task_id,
                source=source,
                started_at=_iso(started),
                session_name=session_name,
                prompt_hash=prompt_hash,
                snapshot_json=snapshot_json,
                model=snapshot.model,
                thinking=snapshot.thinking,
            ),
        )

    _log(f"run {run_id}: starting {source} run for task {task.task_id}")
    pi_executable = resolve_pi()
    command = build_pi_command(
        pi_executable=pi_executable,
        snapshot=snapshot,
        prompt_text=prompt_text,
        session_name=session_name,
    )

    observation = StreamObservation()
    timed_out = False
    cancelled = False
    exit_code: int | None = None
    error: str | None = None
    process: subprocess.Popen[str] | None = None
    previous_handlers: list[tuple[signal.Signals, Any]] = []

    def _request_cancel(signum: int, _frame: object) -> None:
        nonlocal cancelled
        cancelled = True
        _log(f"run {run_id}: received signal {signum}; cancelling")
        if process is not None and process.poll() is None:
            with suppress(ProcessLookupError):
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
            try:
                stdout, stderr = process.communicate(timeout=task.timeout_seconds)
            except subprocess.TimeoutExpired:
                if cancelled:
                    _stop_process_group(process)
                    stdout, stderr = process.communicate()
                else:
                    timed_out = True
                    _stop_process_group(process)
                    stdout, stderr = process.communicate()
                    error = f"timed out after {task.timeout_seconds} seconds"
                    _log(f"run {run_id}: {error}")
            exit_code = process.returncode
            for line in stdout.splitlines():
                consume_event_line(observation, line)
            # Keep Pi diagnostics brief; never forward the JSON event stream.
            if stderr.strip():
                for line in stderr.splitlines():
                    if line.strip():
                        _log(f"run {run_id}: pi: {line.strip()}")
        except Exception as exc:
            error = f"wrapper failed: {exc}"
            _log(f"run {run_id}: {error}")
            if process.poll() is None:
                _stop_process_group(process)
                process.communicate()
            return _finalize(
                run_id=run_id,
                started=started,
                status="failed",
                observation=observation,
                error=error,
            )

        status = classify_run_status(
            process_exit_code=exit_code,
            timed_out=timed_out,
            cancelled=cancelled,
            observation=observation,
        )
        if status == "cancelled":
            error = error or "run cancelled"
        elif status != "succeeded" and error is None:
            if observation.final_stop_reason and observation.final_stop_reason != "stop":
                error = f"final stop reason: {observation.final_stop_reason}"
            elif observation.malformed_line:
                error = "malformed Pi JSON event stream"
            elif not observation.saw_assistant:
                error = "missing final assistant response"
            elif exit_code not in (0, None):
                error = f"Pi exited with status {exit_code}"
            else:
                error = f"run ended with status {status}"

        return _finalize(
            run_id=run_id,
            started=started,
            status=status,
            observation=observation,
            error=error,
        )
    except BaseException as exc:
        # Ensure the run never remains stuck in "running" after wrapper death.
        if isinstance(exc, Exception):
            status: RunStatus = "cancelled" if cancelled else "failed"
            error = error or f"wrapper interrupted: {exc}"
            _log(f"run {run_id}: {error}")
            return _finalize(
                run_id=run_id,
                started=started,
                status=status,
                observation=observation,
                error=error,
            )
        status = "cancelled" if cancelled else "failed"
        error = error or f"wrapper interrupted by {exc.__class__.__name__}"
        _log(f"run {run_id}: {error}")
        _finalize(
            run_id=run_id,
            started=started,
            status=status,
            observation=observation,
            error=error,
        )
        raise
    finally:
        for signum, handler in previous_handlers:
            signal.signal(signum, handler)


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
    _log(f"run {run_id}: finished in {duration_ms}ms{usage_suffix}")
    return 0 if status == "succeeded" else 1
