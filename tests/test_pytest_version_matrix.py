from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess  # nosec B404
from typing import cast

import pytest


PYTEST_8_VERSIONS = ("8.0.2", "8.1.1", "8.2.2", "8.3.5", "8.4.2")


def test_isolated_matrix_reports_process_failure_before_reading_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch a missing artifact hiding an isolated uv/pytest startup failure."""
    completed = subprocess.CompletedProcess(
        args=("uv", "run"),
        returncode=2,
        stdout="isolated stdout",
        stderr="isolated stderr",
    )
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: completed)

    with pytest.raises(AssertionError, match="isolated pytest failed") as captured:
        run_isolated_pytest_project(tmp_path, "8.0.2")

    assert "isolated stdout" in str(captured.value)
    assert "isolated stderr" in str(captured.value)


def run_isolated_pytest_project(
    tmp_path: Path, pytest_version: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    """Run the raw-artifact plugin in one cached isolated pytest environment."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "test_sample.py").write_text(
        """import pytest


def test_pass():
    assert True


@pytest.mark.xfail(reason="expected failure")
def test_xfail():
    assert False


@pytest.mark.xfail(reason="strict unexpected pass", strict=True)
def test_strict_xpass():
    assert True


@pytest.mark.xfail(reason="unexpected pass")
def test_non_strict_xpass():
    assert True


def test_deselected():
    assert True
""",
        encoding="utf-8",
    )
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    artifact_path = artifact_dir / "pytest.json"
    writer_dir = artifact_dir / "writers"
    writer_dir.mkdir()
    module_dir = tmp_path / "plugin"
    module_dir.mkdir()
    module_name = "pytest_evidence_plugin"
    shutil.copyfile(
        Path(__file__).parents[1] / "src/pyrepo_check/_pytest_report_plugin.py",
        module_dir / f"{module_name}.py",
    )
    environment = os.environ | {
        "PYREPO_CHECK_PYTEST_JSON": str(artifact_path),
        "PYREPO_CHECK_PYTEST_WRITER_DIR": str(writer_dir),
        "PYTHONPATH": os.pathsep.join(
            filter(None, (os.environ.get("PYTHONPATH"), str(module_dir)))
        ),
    }
    completed = subprocess.run(  # nosec B603
        (
            "uv",
            "run",
            "--isolated",
            "--python",
            "3.13.15",
            "--with",
            f"pytest=={pytest_version}",
            "python",
            "-m",
            "pytest",
            "-p",
            module_name,
            "--deselect",
            "test_sample.py::test_deselected",
        ),
        cwd=project,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 1, (
        f"isolated pytest failed for pytest {pytest_version}\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    return completed, json.loads(artifact_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("pytest_version", PYTEST_8_VERSIONS)
def test_raw_artifact_has_one_common_shape_across_supported_pytest_8_minors(
    tmp_path: Path,
    pytest_version: str,
) -> None:
    """Catch a pytest-minor-specific raw artifact or setup failure."""
    completed, artifact = run_isolated_pytest_project(tmp_path, pytest_version)

    assert completed.returncode == 1, completed.stderr
    assert artifact["schema_version"] == 1
    assert artifact["state"] == "finalized"
    assert artifact["pytest_version"] == pytest_version
    assert artifact["session"] == {
        "starts": 1,
        "finishes": 1,
        "exit_code": 1,
        "collection_completed": True,
        "stopped_early": False,
    }
    collection = cast(dict[str, object], artifact["collection"])
    assert collection["deselected_nodeids"] == ["test_sample.py::test_deselected"]
    assert collection["final_nodeids"] == [
        "test_sample.py::test_pass",
        "test_sample.py::test_xfail",
        "test_sample.py::test_strict_xpass",
        "test_sample.py::test_non_strict_xpass",
    ]
    reports = cast(list[dict[str, object]], artifact["reports"])
    assert [
        (report["nodeid"], report["when"], report["outcome"], report["wasxfail"])
        for report in reports
        if report["when"] == "call"
    ] == [
        ("test_sample.py::test_pass", "call", "passed", None),
        ("test_sample.py::test_xfail", "call", "skipped", "expected failure"),
        ("test_sample.py::test_strict_xpass", "call", "failed", None),
        ("test_sample.py::test_non_strict_xpass", "call", "passed", "unexpected pass"),
    ]
