from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess  # nosec B404
import sys
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
    captured_command: tuple[str, ...] | None = None

    def fake_run(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal captured_command
        captured_command = command
        return completed

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(AssertionError, match="isolated pytest failed") as captured:
        run_isolated_pytest_project(tmp_path, "8.0.2")

    assert "isolated stdout" in str(captured.value)
    assert "isolated stderr" in str(captured.value)
    assert captured_command is not None
    assert captured_command[4] == sys.executable


def run_isolated_pytest_project(
    tmp_path: Path,
    pytest_version: str,
    *,
    conftest_source: str | None = None,
    plugin_source: str | None = None,
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
    if conftest_source is not None:
        (project / "conftest.py").write_text(conftest_source, encoding="utf-8")
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
    consumer_plugin_name = "consumer_plugin"
    if plugin_source is not None:
        (module_dir / f"{consumer_plugin_name}.py").write_text(
            plugin_source,
            encoding="utf-8",
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
            sys.executable,
            "--with",
            f"pytest=={pytest_version}",
            "python",
            "-m",
            "pytest",
            "-p",
            module_name,
            *(("-p", consumer_plugin_name) if plugin_source is not None else ()),
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
def test_equal_priority_outer_sessionfinish_report_is_not_hidden(
    tmp_path: Path,
    pytest_version: str,
) -> None:
    """Catch an outer wrapper emitting a report after an inner final publication."""
    _completed, artifact = run_isolated_pytest_project(
        tmp_path,
        pytest_version,
        conftest_source="""
import pytest
from _pytest.reports import TestReport


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    del exitstatus
    yield
    session.config.hook.pytest_runtest_logreport(
        report=TestReport(
            nodeid="test_sample.py::test_pass",
            location=("test_sample.py", 0, "test_pass"),
            keywords={},
            outcome="passed",
            longrepr=None,
            when="call",
            sections=(),
            duration=0.0,
            start=0.0,
            stop=0.0,
            user_properties=[],
        )
    )
""",
    )

    flags = cast(dict[str, object], artifact["flags"])
    assert flags["unsupported_retries"] is True


@pytest.mark.parametrize("pytest_version", PYTEST_8_VERSIONS)
def test_ordinary_collection_semantic_mutation_is_not_hidden(
    tmp_path: Path,
    pytest_version: str,
) -> None:
    """Catch an ordinary collection hook mutating options after the first snapshot."""
    _completed, artifact = run_isolated_pytest_project(
        tmp_path,
        pytest_version,
        conftest_source="""
def pytest_collection_modifyitems(config, items):
    del items
    config.option.keyword = "test"
""",
    )

    semantic_options = cast(dict[str, object], artifact["semantic_options"])
    assert semantic_options["keyword"] == "test"


@pytest.mark.parametrize("pytest_version", PYTEST_8_VERSIONS)
def test_equal_priority_outer_collection_argument_mutation_is_not_hidden(
    tmp_path: Path,
    pytest_version: str,
) -> None:
    """Catch a later outer wrapper appending an external scope argument."""
    _completed, artifact = run_isolated_pytest_project(
        tmp_path,
        pytest_version,
        plugin_source="""
import pytest


_ARGS = None


@pytest.hookimpl(wrapper=True)
def pytest_load_initial_conftests(early_config, parser, args):
    del early_config, parser
    global _ARGS
    _ARGS = args
    yield


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_modifyitems(items):
    del items
    yield
    assert _ARGS is not None
    _ARGS.append("--external-filter")
""",
    )

    effective_args = cast(list[str], artifact["effective_args"])
    assert "--external-filter" in effective_args


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


@pytest.mark.parametrize("pytest_version", PYTEST_8_VERSIONS)
@pytest.mark.parametrize(
    "sessionfinish_hook",
    (
        """
@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    del exitstatus
    emit_duplicate_report(session)
""",
        """
@pytest.hookimpl(wrapper=True)
def pytest_sessionfinish(session, exitstatus):
    del exitstatus
    yield
    emit_duplicate_report(session)
""",
    ),
    ids=("ordinary-trylast", "wrapper-teardown"),
)
def test_terminal_sessionfinish_reports_are_captured_across_supported_pytest_8_minors(
    tmp_path: Path,
    pytest_version: str,
    sessionfinish_hook: str,
) -> None:
    completed, artifact = run_isolated_pytest_project(
        tmp_path,
        pytest_version,
        conftest_source=(
            """
import pytest
from _pytest.reports import TestReport


def emit_duplicate_report(session):
    session.config.hook.pytest_runtest_logreport(
        report=TestReport(
            nodeid="test_sample.py::test_pass",
            location=("test_sample.py", 0, "test_pass"),
            keywords={},
            outcome="passed",
            longrepr=None,
            when="call",
            sections=(),
            duration=0.0,
            start=0.0,
            stop=0.0,
            user_properties=[],
        )
    )
"""
            + sessionfinish_hook
        ),
    )

    assert completed.returncode == 1, completed.stderr
    assert artifact["state"] == "finalized"
    flags = cast(dict[str, object], artifact["flags"])
    assert flags["unsupported_retries"] is True
    reports = cast(list[dict[str, object]], artifact["reports"])
    assert [
        (report["nodeid"], report["when"])
        for report in reports
        if report["nodeid"] == "test_sample.py::test_pass"
        and report["when"] == "call"
    ] == [
        ("test_sample.py::test_pass", "call"),
        ("test_sample.py::test_pass", "call"),
    ]
