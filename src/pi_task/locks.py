from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Literal

LockKind = Literal["task", "working_directory"]


class LockConflict(Exception):
    """Raised when a required run lock cannot be acquired without waiting."""

    def __init__(self, kind: LockKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def _runtime_home() -> Path:
    value = os.environ.get("XDG_RUNTIME_DIR")
    if not value:
        raise RuntimeError("XDG_RUNTIME_DIR is not set")
    return Path(value).expanduser() / "pi-task"


def lock_directory() -> Path:
    return _runtime_home() / "locks"


def task_lock_path(task_id: str) -> Path:
    return lock_directory() / "task" / f"{task_id}.lock"


def normalize_working_directory(working_directory: Path) -> str:
    return str(working_directory.expanduser().resolve())


def working_directory_lock_path(working_directory: Path) -> Path:
    digest = hashlib.sha256(normalize_working_directory(working_directory).encode()).hexdigest()
    return lock_directory() / "working-directory" / f"{digest}.lock"


def _try_lock(path: Path) -> IO[str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise
    except OSError:
        handle.close()
        raise
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
    except OSError:
        # Ownership of the lock still holds; PID annotation is best-effort.
        pass
    return handle


@dataclass
class RunLocks:
    """Exclusive same-task and same-working-directory locks for one run."""

    task_id: str
    working_directory: Path
    _handles: list[IO[str]] = field(default_factory=list, repr=False)

    def acquire(self) -> None:
        """Take task then working-directory locks, or raise LockConflict."""
        try:
            self._handles.append(_try_lock(task_lock_path(self.task_id)))
        except BlockingIOError as error:
            raise LockConflict(
                "task",
                f"task {self.task_id!r} is already running",
            ) from error

        try:
            self._handles.append(_try_lock(working_directory_lock_path(self.working_directory)))
        except BlockingIOError as error:
            # Drop the task lock before surfacing the directory conflict.
            self.release()
            raise LockConflict(
                "working_directory",
                f"working directory {normalize_working_directory(self.working_directory)!r} "
                f"is busy with another task",
            ) from error

    def release(self) -> None:
        while self._handles:
            handle = self._handles.pop()
            with suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            with suppress(OSError):
                handle.close()

    def try_probe(self) -> bool:
        """Return True if both locks can be taken briefly (no live holder)."""
        try:
            self.acquire()
        except LockConflict:
            return False
        self.release()
        return True


@contextmanager
def acquire_run_locks(task_id: str, working_directory: Path) -> Iterator[RunLocks]:
    """Acquire task then working-directory locks, or raise LockConflict.

    Acquisition order is fixed (task, then working directory) to avoid deadlocks.
    Locks use non-blocking flock and are released when the process exits, so stale
    lock files left after a crash do not permanently block future runs.
    """
    held = RunLocks(task_id=task_id, working_directory=working_directory)
    try:
        held.acquire()
        yield held
    finally:
        held.release()
