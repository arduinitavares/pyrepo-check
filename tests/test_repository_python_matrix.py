from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess  # nosec B404
import sys
from typing import Any

import pytest

from tests.support import repository_test_environment, write_locked_repository_fixture


pytestmark = pytest.mark.integration


def _tracked_bytes(repository: Path) -> dict[str, bytes]:
    git = shutil.which("git")
    assert git is not None, "real Git is required for the Repository Python matrix"
    completed = subprocess.run(  # nosec B603
        (str(Path(git).resolve()), "ls-files", "-z"),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return {
        relative: (repository / relative).read_bytes()
        for relative in completed.stdout.decode().split("\0")
        if relative
    }


def _dependency_statuses(report: dict[str, Any]) -> dict[str, str]:
    return {
        dependency["name"]: dependency["status"]
        for dependency in report["repository_environment"]["dependencies"]
    }


@pytest.mark.skipif(
    "PYREPO_CHECK_REPOSITORY_PYTHON" not in os.environ,
    reason="PYREPO_CHECK_REPOSITORY_PYTHON selects one explicit matrix case",
)
def test_global_controller_runs_complete_gate_on_selected_repository_python(
    tmp_path: Path,
) -> None:
    request = os.environ["PYREPO_CHECK_REPOSITORY_PYTHON"]
    repository = write_locked_repository_fixture(tmp_path, python=request)
    before = _tracked_bytes(repository)
    environment = repository_test_environment(tmp_path / "uv-storage")
    environment["UV_PYTHON_DOWNLOADS"] = os.environ.get(
        "UV_PYTHON_DOWNLOADS",
        "never",
    )
    controller_path = str(tmp_path / "controller-pythonpath")
    environment["PYTHONPATH"] = controller_path
    environment["PYREPO_CHECK_CONTROLLER_PATH_SENTINEL"] = controller_path

    completed = subprocess.run(  # nosec B603
        (
            sys.executable,
            "-m",
            "pyrepo_check.cli",
            "--root",
            str(repository),
            "--python",
            request,
            "--format",
            "json",
            "--all",
        ),
        check=False,
        capture_output=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr.decode()
    report: dict[str, Any] = json.loads(completed.stdout)
    assert report["schema_version"] == 2
    assert report["kind"] == "run"
    assert report["complete"] is True
    assert report["overall_status"] == "passed"
    assert report["tool_environment"]["python"]["version"] == [3, 13, 15]
    assert report["repository_environment"]["python"]["version"][:2] == [
        int(piece) for piece in request.split(".")
    ][:2]
    assert report["repository_environment"]["lock"]["status"] == "current"
    assert report["repository_environment"]["mutation_protection"] == "tracked_files"
    assert _dependency_statuses(report) == {
        "ruff": "available",
        "ty": "available",
        "bandit": "available",
        "pytest": "available",
        "coverage": "available",
    }
    assert all(
        check["execution_environment"] == "repository"
        and check["start_evidence"] is not None
        for check in report["checks"]
    )
    for check in report["checks"]:
        if check["name"] in {"ruff", "annotations", "ty"}:
            assert check["analysis_python_authority"] == {
                "authority": "repository_tool",
                "pyrepo_check_override": None,
            }
        else:
            assert check["analysis_python_authority"] is None
    assert report["pytest"]["status"] == "passed"
    assert report["pytest"]["complete"] is True
    assert report["pytest"]["evidence"] is not None
    assert report["coverage"]["status"] == "passed"
    assert report["coverage"]["evidence_complete"] is True
    assert report["coverage"]["threshold"] == {
        "configured": True,
        "value": 100,
        "evaluated": True,
        "passed": True,
        "skipped_reason": None,
    }
    assert _tracked_bytes(repository) == before
