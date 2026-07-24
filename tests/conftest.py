"""Shared pytest fixtures for pi-task CLI tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from harness import make_run_env
from harness import run_cli as harness_run_cli

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable
    from pathlib import Path


@pytest.fixture
def run_env(tmp_path: Path) -> dict[str, str]:
    return make_run_env(tmp_path)


@pytest.fixture
def run_cli() -> Callable[..., subprocess.CompletedProcess[str]]:
    return harness_run_cli
