"""Packaging metadata and install verification.

These checks exercise the built distribution without touching the developer's
real task definitions or systemd user manager.
"""

from __future__ import annotations

import os
import subprocess
import tomllib
import zipfile
from email import message_from_string
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]


def _project_table() -> dict[str, Any]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = data.get("project")
    assert isinstance(project, dict)
    return cast(dict[str, Any], project)


def test_project_metadata_is_complete_for_release() -> None:
    project = _project_table()
    assert project.get("name") == "pi-task"
    assert project.get("version")
    assert project.get("description")
    assert project.get("readme") == "README.md"
    assert project.get("requires-python") == ">=3.14"
    assert project.get("license") == "MIT"
    assert project.get("license-files") == ["LICENSE"]
    assert project.get("authors"), "authors must be declared for a public release"
    assert project.get("keywords"), "keywords help package discovery"
    classifiers = project.get("classifiers")
    assert isinstance(classifiers, list)
    joined = "\n".join(str(item) for item in classifiers)
    assert "License :: OSI Approved :: MIT License" in joined
    assert "Operating System :: POSIX :: Linux" in joined
    assert "Programming Language :: Python :: 3.14" in joined
    scripts = project.get("scripts")
    assert isinstance(scripts, dict)
    assert scripts.get("pi-task") == "pi_task.cli:app"
    urls = project.get("urls")
    assert isinstance(urls, dict)
    for key in ("Homepage", "Repository", "Issues"):
        assert key in urls, f"project.urls must include {key}"
        assert str(urls[key]).startswith("https://")


def test_license_file_is_mit() -> None:
    text = (ROOT / "LICENSE").read_text()
    assert "MIT License" in text
    assert "Permission is hereby granted" in text


def test_built_wheel_installs_and_exposes_cli(tmp_path: Path) -> None:
    """Build the wheel, install into a throwaway venv, and run --version."""
    dist_dir = tmp_path / "dist"
    venv_dir = tmp_path / "venv"
    build = subprocess.run(
        ["uv", "build", "--out-dir", str(dist_dir)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "NO_COLOR": "1"},
    )
    assert build.returncode == 0, build.stdout + build.stderr
    wheels = sorted(dist_dir.glob("*.whl"))
    assert wheels, "uv build must produce a wheel"
    wheel = wheels[-1]

    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = message_from_string(archive.read(metadata_name).decode())
        assert metadata.get("Name") == "pi-task"
        license_value = metadata.get("License-Expression") or metadata.get("License") or ""
        assert "MIT" in license_value
        assert any("LICENSE" in name for name in names), "wheel must ship the MIT license file"

    create = subprocess.run(
        ["uv", "venv", str(venv_dir)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert create.returncode == 0, create.stdout + create.stderr
    install = subprocess.run(
        ["uv", "pip", "install", "--python", str(venv_dir / "bin" / "python"), str(wheel)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    version = subprocess.run(
        [str(venv_dir / "bin" / "pi-task"), "--version"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "NO_COLOR": "1"},
    )
    assert version.returncode == 0, version.stdout + version.stderr
    assert version.stdout.strip().startswith("pi-task ")
    # Installation verification must never schedule real work.
    assert "Created" not in version.stdout
    assert "timer" not in version.stdout.lower()


def test_default_pytest_addopts_exclude_smoke_marker() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    pytest_cfg = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
    addopts = str(pytest_cfg.get("addopts", ""))
    assert "not smoke" in addopts
    markers = pytest_cfg.get("markers", [])
    assert any("smoke" in str(marker) for marker in markers)
