"""Exercise a separately installed controller against a locked Windows project."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess  # nosec B404
import sys
from typing import Any

import pytest


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.name != "nt", reason="native Windows installed-controller regression"),
]


def _run(
    command: list[str], *, root: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603
        command, cwd=root, env=environment, capture_output=True, text=True, check=False,
        timeout=180,
    )


def _source_digests(root: Path) -> dict[str, str]:
    names = ("pyproject.toml", "uv.lock", "src/__init__.py", "src/sample.py", "tests/test_sample.py")
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}


def test_installed_windows_controller_runs_all_checks_and_preserves_repository(
    tmp_path: Path,
) -> None:
    uv = shutil.which("uv")
    assert uv is not None, "uv is required for the installed-controller regression"
    checkout = Path(__file__).resolve().parents[1]
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("UV_", "PYREPO_CHECK_")) and key != "PYTHONPATH"
    }
    environment["UV_TOOL_DIR"] = str(tmp_path / "tools")
    environment["UV_TOOL_BIN_DIR"] = str(tmp_path / "bin")
    installed = _run(
        [uv, "tool", "install", "--python", sys.executable, str(checkout)],
        root=tmp_path, environment=environment,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    controller = tmp_path / "bin" / "pyrepo-check.exe"
    assert controller.is_file()

    root = tmp_path / "repository"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src/__init__.py").write_text("", encoding="utf-8")
    (root / "src/sample.py").write_text(
        "def add(left: int, right: int) -> int:\n    return left + right\n", encoding="utf-8"
    )
    (root / "tests/test_sample.py").write_text(
        "from src.sample import add\n\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        """[project]
name = "windows-controller-smoke"
version = "0"
requires-python = ">=3.10,<3.14"
[dependency-groups]
dev = ["ruff>=0.15,<1", "ty>=0.0.35,<0.1", "bandit>=1.9,<2", "pytest>=8,<9", "coverage[toml]>=7.15,<8"]
[tool.ruff]
line-length = 100
[tool.coverage.run]
source = ["src"]
branch = true
[tool.coverage.report]
fail_under = 100
[tool.bandit]
exclude_dirs = [".venv"]
[tool.bandit.assert_used]
skips = ["./tests/*"]
""", encoding="utf-8"
    )
    repository_python = "3.13"
    locked = _run(
        [uv, "lock", "--python", repository_python], root=root, environment=environment
    )
    assert locked.returncode == 0, locked.stdout + locked.stderr
    before = _source_digests(root)
    completed = _run(
        [str(controller), "--python", repository_python, "--format", "json", "--all"],
        root=root, environment=environment,
    )
    report: dict[str, Any] = json.loads(completed.stdout)
    diagnostics = {
        check["name"]: {
            "status": check["status"],
            "error": check["error"],
            "output": [process["stdout"] for process in check["processes"]],
        }
        for check in report.get("checks", [])
    }
    assert completed.returncode == 0, json.dumps(diagnostics) + completed.stderr
    assert report["overall_status"] == "passed"
    assert all(check["start_evidence"] is not None for check in report["checks"])
    assert report["coverage"]["gate_eligible"] is True
    assert _source_digests(root) == before

    source = root / "src/sample.py"
    source.write_text("import os\n" + source.read_text(encoding="utf-8"), encoding="utf-8")
    before_failed_check = _source_digests(root)
    failed = _run(
        [str(controller), "--python", repository_python, "--format", "json", "ruff", "src/sample.py"],
        root=root, environment=environment,
    )
    failed_report: dict[str, Any] = json.loads(failed.stdout)
    assert failed.returncode != 0
    assert failed_report["checks"][0]["status"] == "failed", failed.stdout
    primary = failed_report["checks"][0]["processes"][0]
    assert primary["exit_code"] == 1
    assert "F401" in primary["stdout"]["text"]
    assert _source_digests(root) == before_failed_check
