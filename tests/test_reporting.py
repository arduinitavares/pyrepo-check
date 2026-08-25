from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, replace
import os
from pathlib import Path
from typing import Any, cast

import pytest

import pyrepo_check.pytest_execution as pytest_execution
import pyrepo_check.reporting as reporting
from pyrepo_check.execution import (
    CAPTURE_LIMIT_BYTES,
    CapturedBytes,
    ExecutedCheck,
    ExecutedProcess,
    ExecutionResult,
    execute_plan,
)
from pyrepo_check.planning import (
    CheckName,
    CoverageExecutionPlan,
    OutputFormat,
    PlannedCheck,
    PlannedTestScope,
    RunMode,
    RunPlan,
    PytestExecutionPlan,
)
from pyrepo_check.pytest_execution import (
    PytestArtifactObservation,
    PytestExecutionObservation,
    PytestPreflightObservation,
    PytestPreflightRecord,
)
from pyrepo_check.coverage_execution import (
    CoverageArtifactObservation,
    CoverageArtifactState,
    CoverageExecutionObservation,
    CoveragePreflightClassification,
    CoveragePreflightObservation,
    CoveragePreflightRecord,
)
from pyrepo_check.coverage_evidence import (
    CoverageCounts,
    CoverageError,
    CoverageFile,
    CoverageResult,
    CoverageThreshold,
    CoverageTotals,
    FileBranchCoverage,
    FileStatementCoverage,
)
from pyrepo_check.pytest_evidence import (
    CollectionIssue,
    PytestCounts,
    PytestError,
    PytestEvidence,
    SlowTest,
    SpecialTestOutcome,
)
from pyrepo_check.reporting import (
    Advisory,
    AgentReportV1,
    CapturedText,
    CheckError,
    CheckResult,
    PlanningError,
    PlanningErrorReportV1,
    PlannedCoverageScope,
    ProcessResult,
    ReportingError,
    RunReportV1,
    Selection,
    build_planning_error_report,
    build_run_report,
    render_terminal,
    select_exit_code,
    serialize_json,
    validate_report_v1,
)


def planned_check(root: Path, name: CheckName) -> PlannedCheck:
    pytest_plan = PytestExecutionPlan(("uv", "run", "python"), ()) if name == "pytest" else None
    return PlannedCheck(
        name=name,
        command=("uv", "run", "python", "-m", name),
        cwd=root,
        pytest=pytest_plan,
    )


def executed_check(
    planned: PlannedCheck,
    returncode: int | None,
    *,
    duration_ms: int = 7,
    stdout: bytes | None = b"",
    stderr: bytes | None = b"",
    spawn_error: str | None = None,
) -> ExecutedCheck:
    primary = ExecutedProcess(
        role="primary",
        command=planned.command,
        cwd=planned.cwd,
        returncode=returncode,
        duration_ms=duration_ms,
        stdout=_captured_bytes(stdout),
        stderr=_captured_bytes(stderr),
        spawn_error=spawn_error,
    )
    if planned.name == "pytest":
        return ExecutedCheck(
            planned=planned,
            processes=(pytest_preflight_process(planned), primary),
            pytest=finalized_pytest_execution_observation(exit_code=returncode or 0),
        )
    return ExecutedCheck(
        planned=planned,
        processes=(primary,),
    )


def _captured_bytes(raw: bytes | None) -> CapturedBytes | None:
    if raw is None:
        return None
    tail = raw[-CAPTURE_LIMIT_BYTES:]
    return CapturedBytes(tail, len(raw) - len(tail))


def run_plan(
    checks: tuple[PlannedCheck, ...],
    *,
    targets: tuple[str, ...] = (),
    mode: RunMode = "focused",
    output_format: OutputFormat = "json",
    test_shortcut: str | None = None,
    pytest_args: tuple[str, ...] | None = None,
    planned_test_scope: PlannedTestScope | None = None,
    planned_coverage_scope: PlannedCoverageScope = "not_requested",
) -> RunPlan:
    pytest_selected = any(check.name == "pytest" for check in checks)
    if pytest_args is None:
        pytest_args = targets if pytest_selected else None
    if planned_test_scope is None:
        planned_test_scope = (
            "not_selected" if not pytest_selected else "partial" if targets else "complete"
        )
    return RunPlan(
        mode=mode,
        targets=targets,
        checks=checks,
        output_format=output_format,
        test_shortcut=test_shortcut,
        pytest_args=pytest_args,
        planned_test_scope=planned_test_scope,
        planned_coverage_scope=planned_coverage_scope,
    )


def coverage_result() -> CoverageResult:
    return CoverageResult(
        status="passed",
        scope="complete",
        evidence_complete=True,
        coverage_version="7.15.2",
        gate_eligible=True,
        threshold=CoverageThreshold(True, 90, True, True, None),
        totals=CoverageTotals(CoverageCounts(4, 1), CoverageCounts(1, 1)),
        files=(
            CoverageFile(
                "src/example.py",
                FileStatementCoverage(4, 1, (7,)),
                FileBranchCoverage(1, 1, ((7, 8),)),
            ),
        ),
        error=None,
    )


def coverage_execution_observation(
    content: bytes,
    *,
    json_exit_code: int | None = 0,
) -> CoverageExecutionObservation:
    return CoverageExecutionObservation(
        preflight=CoveragePreflightObservation(
            "supported", CoveragePreflightRecord((3, 13, 15), True, "7.15.2"), None
        ),
        artifact=CoverageArtifactObservation("snapshot", content, None),
        json_exit_code=json_exit_code,
    )


def coverage_enabled_pytest_check(
    root: Path,
    *,
    fail_under: int | float | None = 90,
) -> PlannedCheck:
    check = pytest_planned_check(root)
    assert check.pytest is not None
    return replace(
        check,
        pytest=replace(
            check.pytest,
            coverage=CoverageExecutionPlan(
                check.pytest.consumer_python,
                root / "pyproject.toml",
                fail_under,
            ),
        ),
    )


def coverage_process(
    check: PlannedCheck,
    role: str,
    returncode: int | None = 0,
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    spawn_error: str | None = None,
) -> ExecutedProcess:
    return ExecutedProcess(
        role=cast(Any, role),
        command=("coverage", role),
        cwd=check.cwd,
        returncode=returncode,
        duration_ms=1,
        stdout=CapturedBytes(stdout, 0),
        stderr=CapturedBytes(stderr, 0),
        spawn_error=spawn_error,
    )


def coverage_json_content(source: Path) -> bytes:
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("value = 1\n")
    return reporting.json.dumps(
        {
            "meta": {"format": 3, "version": "7.15.2", "branch_coverage": True},
            "files": {
                str(source): {
                    "summary": {
                        "covered_lines": 1,
                        "missing_lines": 0,
                        "num_statements": 1,
                        "covered_branches": 0,
                        "missing_branches": 0,
                        "num_branches": 0,
                    },
                    "missing_lines": [],
                    "missing_branches": [],
                }
            },
            "totals": {
                "covered_lines": 1,
                "missing_lines": 0,
                "num_statements": 1,
                "covered_branches": 0,
                "missing_branches": 0,
                "num_branches": 0,
            },
        }
    ).encode()


def build_coverage_report(
    tmp_path: Path,
    *,
    mode: RunMode = "strict_aggregate",
    targets: tuple[str, ...] = (),
    fail_under: int | float | None = 90,
    coverage_json_exit_code: int | None = 0,
    coverage_json_stdout: bytes = b"",
    coverage_json_stderr: bytes = b"",
    coverage_content: bytes | None = None,
) -> RunReportV1:
    check = coverage_enabled_pytest_check(tmp_path, fail_under=fail_under)
    source = tmp_path / "src" / "example.py"
    primary = ExecutedProcess(
        role="primary",
        command=check.command,
        cwd=check.cwd,
        returncode=0,
        duration_ms=1,
        stdout=CapturedBytes(b"", 0),
        stderr=CapturedBytes(b"", 0),
        spawn_error=None,
    )
    return build_run_report(
        tmp_path,
        run_plan(
            (check,),
            mode=mode,
            targets=targets,
            planned_coverage_scope="partial" if targets else "complete",
        ),
        ExecutionResult(
            (
                ExecutedCheck(
                    planned=check,
                    processes=(
                        pytest_preflight_process(check),
                        coverage_process(check, "coverage_preflight"),
                        primary,
                        coverage_process(
                            check,
                            "coverage_json",
                            coverage_json_exit_code,
                            stdout=coverage_json_stdout,
                            stderr=coverage_json_stderr,
                        ),
                    ),
                    pytest=finalized_pytest_execution_observation(),
                    coverage=coverage_execution_observation(
                        coverage_json_content(source)
                        if coverage_content is None
                        else coverage_content,
                        json_exit_code=coverage_json_exit_code,
                    ),
                ),
            ),
            coverage_json_exit_code or 0,
        ),
    )


def coverage_primary_process(check: PlannedCheck) -> ExecutedProcess:
    return ExecutedProcess(
        role="primary",
        command=check.command,
        cwd=check.cwd,
        returncode=0,
        duration_ms=1,
        stdout=CapturedBytes(b"", 0),
        stderr=CapturedBytes(b"", 0),
        spawn_error=None,
    )


def coverage_stage_preflight(
    classification: CoveragePreflightClassification,
    *,
    version: str | None = "7.15.2",
    diagnostic: str = "coverage stage diagnostic",
) -> CoveragePreflightObservation:
    record: CoveragePreflightRecord | None
    if classification == "module_unavailable":
        record = CoveragePreflightRecord((3, 13, 15), False, None)
    elif classification == "unsupported_python":
        record = CoveragePreflightRecord((3, 13, 14), True, version)
    elif classification in {"supported", "unsupported_version"}:
        record = CoveragePreflightRecord((3, 13, 15), True, version)
    else:
        record = None
    return CoveragePreflightObservation(classification, record, diagnostic)


def coverage_stage_observation(
    *,
    preflight: CoveragePreflightClassification = "supported",
    artifact: CoverageArtifactState = "not_attempted",
    version: str | None = "7.15.2",
    json_exit_code: int | None = None,
) -> CoverageExecutionObservation:
    return CoverageExecutionObservation(
        coverage_stage_preflight(preflight, version=version),
        CoverageArtifactObservation(artifact, None, f"{artifact} diagnostic"),
        json_exit_code,
    )


def unstarted_pytest_observation() -> PytestExecutionObservation:
    return replace(
        finalized_pytest_execution_observation(),
        artifact=PytestArtifactObservation("not_attempted", None, (), None),
    )


def failed_pytest_preflight_observation() -> PytestExecutionObservation:
    return PytestExecutionObservation(
        PytestPreflightObservation("preflight_invalid", None, "pytest preflight failed"),
        PytestArtifactObservation("not_attempted", None, (), None),
        None,
    )


def unsupported_parallelism_pytest_observation() -> PytestExecutionObservation:
    observation = finalized_pytest_execution_observation()
    assert observation.artifact.content is not None
    document = cast(dict[str, Any], reporting.json.loads(observation.artifact.content))
    flags = cast(dict[str, Any], document["flags"])
    flags["unsupported_parallelism"] = True
    return replace(
        observation,
        artifact=replace(
            observation.artifact,
            content=reporting.json.dumps(document).encode(),
        ),
    )


def build_coverage_error_stage_report(tmp_path: Path, case: str) -> RunReportV1:
    check = coverage_enabled_pytest_check(tmp_path)
    pytest_preflight = pytest_preflight_process(check)
    coverage_preflight = coverage_process(check, "coverage_preflight")
    primary = coverage_primary_process(check)
    processes: tuple[ExecutedProcess, ...]
    pytest_observation: PytestExecutionObservation
    coverage_observation: CoverageExecutionObservation | None

    if case == "setup-without-process":
        processes = ()
        pytest_observation = PytestExecutionObservation(
            PytestPreflightObservation("not_started", None, "pytest setup prevented preflight"),
            PytestArtifactObservation("not_attempted", None, (), None),
            None,
        )
        coverage_observation = None
    elif case == "setup-after-pytest-preflight":
        processes = (pytest_preflight,)
        pytest_observation = unstarted_pytest_observation()
        coverage_observation = None
    elif case.startswith("typed-"):
        classifications = {
            "typed-unsupported-python": "unsupported_python",
            "typed-module-unavailable": "module_unavailable",
            "typed-stable-unsupported-version": "unsupported_version",
            "typed-untrusted-unsupported-version": "unsupported_version",
            "typed-preflight-invalid": "preflight_invalid",
        }
        classification = cast(CoveragePreflightClassification, classifications[case])
        version = (
            "7.14.9"
            if case == "typed-stable-unsupported-version"
            else None
            if case == "typed-untrusted-unsupported-version"
            else "7.15.2"
        )
        processes = (pytest_preflight, coverage_preflight)
        pytest_observation = unstarted_pytest_observation()
        coverage_observation = coverage_stage_observation(
            preflight=classification,
            version=version,
        )
    elif case == "coverage-preflight-spawn":
        processes = (
            pytest_preflight,
            coverage_process(
                check,
                "coverage_preflight",
                None,
                spawn_error="coverage preflight did not start",
            ),
        )
        pytest_observation = unstarted_pytest_observation()
        coverage_observation = coverage_stage_observation(preflight="spawn_failed")
    elif case == "coverage-preflight-signal":
        processes = (pytest_preflight, coverage_process(check, "coverage_preflight", -9))
        pytest_observation = unstarted_pytest_observation()
        coverage_observation = coverage_stage_observation(preflight="terminated_by_signal")
    elif case == "pytest-preflight-failed-coverage-supported":
        processes = (
            replace(pytest_preflight, returncode=1),
            coverage_preflight,
        )
        pytest_observation = failed_pytest_preflight_observation()
        coverage_observation = coverage_stage_observation(artifact="data_missing")
    elif case == "data-missing-without-primary":
        processes = (pytest_preflight, coverage_preflight)
        pytest_observation = unstarted_pytest_observation()
        coverage_observation = coverage_stage_observation(artifact="data_missing")
    elif case == "data-missing-after-primary":
        processes = (pytest_preflight, coverage_preflight, primary)
        pytest_observation = finalized_pytest_execution_observation()
        coverage_observation = coverage_stage_observation(artifact="data_missing")
    elif case == "unsupported-parallelism":
        processes = (pytest_preflight, coverage_preflight, primary)
        pytest_observation = unsupported_parallelism_pytest_observation()
        coverage_observation = coverage_stage_observation(artifact="unsupported_parallelism")
    elif case == "unexpected-parallel-before-json":
        processes = (pytest_preflight, coverage_preflight, primary)
        pytest_observation = finalized_pytest_execution_observation()
        coverage_observation = coverage_stage_observation(artifact="unexpected_parallel_data")
    elif case == "coverage-json-spawn":
        processes = (
            pytest_preflight,
            coverage_preflight,
            primary,
            coverage_process(
                check,
                "coverage_json",
                None,
                spawn_error="coverage JSON did not start",
            ),
        )
        pytest_observation = finalized_pytest_execution_observation()
        coverage_observation = coverage_stage_observation(
            artifact="spawn_failed",
            json_exit_code=None,
        )
    elif case == "coverage-json-signal":
        processes = (
            pytest_preflight,
            coverage_preflight,
            primary,
            coverage_process(check, "coverage_json", -9),
        )
        pytest_observation = finalized_pytest_execution_observation()
        coverage_observation = coverage_stage_observation(
            artifact="terminated_by_signal",
            json_exit_code=-9,
        )
    else:
        artifact, json_exit_code = {
            "generation-failed": ("generation_failed", 1),
            "artifact-missing-zero": ("artifact_missing", 0),
            "artifact-invalid-zero": ("artifact_invalid", 0),
            "artifact-missing-threshold": ("artifact_missing", 2),
            "artifact-invalid-threshold": ("artifact_invalid", 2),
            "unexpected-parallel-after-json": ("unexpected_parallel_data", 0),
        }[case]
        processes = (
            pytest_preflight,
            coverage_preflight,
            primary,
            coverage_process(check, "coverage_json", json_exit_code),
        )
        pytest_observation = finalized_pytest_execution_observation()
        coverage_observation = coverage_stage_observation(
            artifact=cast(CoverageArtifactState, artifact),
            json_exit_code=json_exit_code,
        )

    return build_run_report(
        tmp_path,
        run_plan((check,), mode="strict_aggregate", planned_coverage_scope="complete"),
        ExecutionResult(
            (
                ExecutedCheck(
                    check,
                    processes,
                    pytest_observation,
                    coverage_observation,
                ),
            ),
            0,
        ),
    )


def test_terminal_and_serialize_project_the_exact_coverage_schema_v1_result(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir()
    source.write_text("one = 1\ntwo = 2\n")
    check = pytest_planned_check(tmp_path)
    assert check.pytest is not None
    coverage_plan = CoverageExecutionPlan(
        check.pytest.consumer_python, tmp_path / "pyproject.toml", 70
    )
    check = replace(check, pytest=replace(check.pytest, coverage=coverage_plan))
    coverage_json = {
        "meta": {"format": 3, "version": "7.15.2", "branch_coverage": True},
        "files": {
            str(source): {
                "summary": {
                    "covered_lines": 4,
                    "missing_lines": 1,
                    "num_statements": 5,
                    "covered_branches": 1,
                    "missing_branches": 1,
                    "num_branches": 2,
                },
                "missing_lines": [7],
                "missing_branches": [[7, 8]],
            }
        },
        "totals": {
            "covered_lines": 4,
            "missing_lines": 1,
            "num_statements": 5,
            "covered_branches": 1,
            "missing_branches": 1,
            "num_branches": 2,
        },
    }
    primary_observation = executed_check(check, 0)
    coverage_preflight_process = ExecutedProcess(
        role="coverage_preflight",
        command=("coverage", "preflight"),
        cwd=tmp_path,
        returncode=0,
        duration_ms=1,
        stdout=CapturedBytes(b"", 0),
        stderr=CapturedBytes(b"", 0),
        spawn_error=None,
    )
    coverage_json_process = ExecutedProcess(
        role="coverage_json",
        command=("coverage", "json"),
        cwd=tmp_path,
        returncode=0,
        duration_ms=1,
        stdout=CapturedBytes(b"", 0),
        stderr=CapturedBytes(b"", 0),
        spawn_error=None,
    )
    report = build_run_report(
        tmp_path,
        run_plan(
            (check,),
            mode="strict_aggregate",
            planned_coverage_scope="complete",
        ),
        ExecutionResult(
            (
                ExecutedCheck(
                    planned=check,
                    processes=(
                        primary_observation.processes[0],
                        coverage_preflight_process,
                        primary_observation.processes[1],
                        coverage_json_process,
                    ),
                    pytest=primary_observation.pytest,
                    coverage=coverage_execution_observation(
                        reporting.json.dumps(coverage_json).encode()
                    ),
                ),
            ),
            0,
        ),
    )

    assert report.coverage == replace(
        coverage_result(),
        threshold=CoverageThreshold(True, 70, True, True, None),
    )
    assert tuple(report.selection.__dataclass_fields__) == (
        "checks",
        "targets",
        "test_shortcut",
        "pytest_args",
        "planned_test_scope",
        "planned_coverage_scope",
    )
    assert tuple(report.__dataclass_fields__) == (
        "schema_version",
        "kind",
        "project_root",
        "mode",
        "overall_status",
        "complete",
        "selection",
        "checks",
        "pytest",
        "coverage",
        "advisories",
    )
    terminal = render_terminal(report)
    assert terminal.startswith(
        "\n==> pyrepo-check summary: passed (strict aggregate)\n"
        "    coverage: passed (complete); minimum 70% passed\n"
        "    coverage:\n"
    )
    assert "Name" in terminal
    assert "Stmts" in terminal
    assert "Miss" in terminal
    assert "Branch" in terminal
    assert "BrMiss" in terminal
    assert "Cover" in terminal
    assert "src/example.py" in terminal
    assert "TOTAL" in terminal
    assert "71.43%" in terminal
    assert "coverage: missing lines:" not in terminal
    assert "coverage: missing branches:" not in terminal
    assert "7->8" not in terminal
    assert (
        "    coverage details: use --format json for exact missing lines and branches\n"
        in terminal
    )
    assert terminal.endswith("    passed: pytest\n")
    payload = reporting.json.loads(serialize_json(report))
    assert list(payload["coverage"]) == [
        "status",
        "scope",
        "evidence_complete",
        "coverage_version",
        "gate_eligible",
        "threshold",
        "totals",
        "files",
        "error",
    ]
    assert payload["coverage"]["files"][0]["branches"]["missing_arcs"] == [[7, 8]]
    assert report.coverage is not None
    threshold_failure = replace(
        report.coverage,
        status="failed",
        threshold=replace(report.coverage.threshold, passed=False),
    )
    threshold_process = replace(report.checks[0].processes[-1], exit_code=2)
    threshold_check = replace(
        report.checks[0], processes=(*report.checks[0].processes[:-1], threshold_process)
    )
    threshold_report = replace(
        report,
        overall_status="failed",
        checks=(threshold_check,),
        coverage=threshold_failure,
    )

    assert threshold_report.checks[0].status == "passed"
    assert validate_report_v1(threshold_report) is None
    assert select_exit_code(threshold_report) == 2


def test_terminal_coverage_summary_caps_ranked_files_and_keeps_exact_json_gaps(
    tmp_path: Path,
) -> None:
    source_directory = tmp_path / "src"
    source_directory.mkdir()
    long_alpha_name = f"{'very_long_' * 12}alpha.py"
    sources = {
        "alpha.py": source_directory / long_alpha_name,
        "beta.py": source_directory / "beta.py",
        "gamma.py": source_directory / "gamma.py",
        "delta.py": source_directory / "delta.py",
    }
    for source in sources.values():
        source.write_text("value = 1\n", encoding="utf-8")
    coverage_json = {
        "meta": {"format": 3, "version": "7.15.2", "branch_coverage": True},
        "files": {
            str(sources["alpha.py"]): {
                "summary": {
                    "covered_lines": 10,
                    "missing_lines": 6,
                    "num_statements": 16,
                    "covered_branches": 6,
                    "missing_branches": 4,
                    "num_branches": 10,
                },
                "missing_lines": [101, 102, 103, 104, 105, 106],
                "missing_branches": [[201, -1], [202, 203], [204, 205], [206, 207]],
            },
            str(sources["beta.py"]): {
                "summary": {
                    "covered_lines": 10,
                    "missing_lines": 5,
                    "num_statements": 15,
                    "covered_branches": 6,
                    "missing_branches": 3,
                    "num_branches": 9,
                },
                "missing_lines": [301, 302, 303, 304, 305],
                "missing_branches": [[401, 402], [403, 404], [405, 406]],
            },
            str(sources["gamma.py"]): {
                "summary": {
                    "covered_lines": 10,
                    "missing_lines": 4,
                    "num_statements": 14,
                    "covered_branches": 6,
                    "missing_branches": 2,
                    "num_branches": 8,
                },
                "missing_lines": [501, 502, 503, 504],
                "missing_branches": [[601, 602], [603, 604]],
            },
            str(sources["delta.py"]): {
                "summary": {
                    "covered_lines": 10,
                    "missing_lines": 3,
                    "num_statements": 13,
                    "covered_branches": 6,
                    "missing_branches": 1,
                    "num_branches": 7,
                },
                "missing_lines": [701, 702, 703],
                "missing_branches": [[801, 802]],
            },
        },
        "totals": {
            "covered_lines": 40,
            "missing_lines": 18,
            "num_statements": 58,
            "covered_branches": 24,
            "missing_branches": 10,
            "num_branches": 34,
        },
    }
    report = build_coverage_report(
        tmp_path,
        mode="focused",
        targets=("tests/test_reporting.py",),
        coverage_content=reporting.json.dumps(coverage_json).encode(),
    )

    terminal = render_terminal(report)

    assert terminal.startswith(
        "\n==> pyrepo-check summary: passed (focused)\n"
        "    coverage: guidance (partial); minimum 90% not applied\n"
        "    coverage:\n"
    )
    alpha_path = f"src/{long_alpha_name}"
    assert alpha_path not in terminal
    assert f"...{alpha_path[-45:]}" in terminal
    assert "src/beta.py" in terminal
    assert "src/gamma.py" in terminal
    assert "src/delta.py" not in terminal
    assert "      ... 1 more file with gaps\n" in terminal
    assert "201->-1" not in terminal
    assert "coverage: missing lines:" not in terminal
    assert "coverage: missing branches:" not in terminal
    assert max(len(line) for line in terminal.splitlines()) <= 100
    payload = reporting.json.loads(serialize_json(report))
    assert payload["coverage"]["files"][0]["statements"]["missing_lines"] == [
        101,
        102,
        103,
        104,
        105,
        106,
    ]
    assert payload["coverage"]["files"][0]["branches"]["missing_arcs"][0] == [
        201,
        -1,
    ]


@pytest.mark.parametrize(
    ("covered", "missing", "expected"),
    (
        (99_999, 1, "99.99%"),
        (1, 99_999, "0.01%"),
        (1, 0, "100.00%"),
        (0, 1, "0.00%"),
    ),
)
def test_coverage_percentage_reserves_zero_and_hundred_for_exact_values(
    covered: int,
    missing: int,
    expected: str,
) -> None:
    assert reporting._coverage_percentage(covered, missing) == expected


@pytest.mark.skipif(os.name == "nt", reason="POSIX permits literal backslashes and drive-like names")
@pytest.mark.parametrize(
    ("raw_path", "path_parts"),
    (
        ("src\\example.py", ("src\\example.py",)),
        ("C:/example.py", ("C:", "example.py")),
    ),
    ids=("literal-backslash", "drive-like-prefix"),
)
def test_coverage_path_artifacts_become_typed_errors_before_json_serialization(
    tmp_path: Path,
    raw_path: str,
    path_parts: tuple[str, ...],
) -> None:
    source = tmp_path.joinpath(*path_parts)
    content = reporting.json.loads(coverage_json_content(source))
    content["files"][raw_path] = content["files"].pop(str(source))

    report = build_coverage_report(
        tmp_path,
        coverage_content=reporting.json.dumps(content).encode(),
    )

    assert report.coverage is not None
    assert report.coverage.status == "error"
    assert report.coverage.error is not None
    assert report.coverage.error.code == "artifact_invalid"
    payload = reporting.json.loads(serialize_json(report))
    assert payload["coverage"]["error"]["code"] == "artifact_invalid"


def test_unavailable_coverage_is_null_and_adds_its_exact_advisory(tmp_path: Path) -> None:
    check = pytest_planned_check(tmp_path)
    report = build_run_report(
        tmp_path,
        run_plan((check,), mode="strict_aggregate", planned_coverage_scope="unavailable"),
        ExecutionResult((executed_check(check, 0),), 0),
    )

    assert report.coverage is None
    assert report.advisories == (
        Advisory(
            "coverage_not_configured",
            "Coverage guidance is unavailable because native Coverage.py configuration is absent.",
            None,
        ),
    )
    assert validate_report_v1(report) is None


@pytest.mark.parametrize(
    ("mode", "planned_coverage_scope"),
    (
        ("focused", "not_requested"),
        ("strict_aggregate", "unavailable"),
    ),
    ids=("focused-not-requested", "strict-aggregate-unavailable"),
)
def test_validation_accepts_planner_mode_and_coverage_scope_pairs(
    tmp_path: Path,
    mode: RunMode,
    planned_coverage_scope: PlannedCoverageScope,
) -> None:
    check = pytest_planned_check(tmp_path)
    report = build_run_report(
        tmp_path,
        run_plan((check,), mode=mode, planned_coverage_scope=planned_coverage_scope),
        ExecutionResult((executed_check(check, 0),), 0),
    )

    assert validate_report_v1(report) is None


@pytest.mark.parametrize(
    ("mode", "planned_coverage_scope"),
    (
        ("focused", "unavailable"),
        ("strict_aggregate", "not_requested"),
    ),
    ids=("focused-unavailable", "strict-aggregate-not-requested"),
)
def test_validation_rejects_impossible_mode_and_coverage_scope_pairs(
    tmp_path: Path,
    mode: RunMode,
    planned_coverage_scope: PlannedCoverageScope,
) -> None:
    check = pytest_planned_check(tmp_path)
    report = build_run_report(
        tmp_path,
        run_plan((check,), mode=mode, planned_coverage_scope=planned_coverage_scope),
        ExecutionResult((executed_check(check, 0),), 0),
    )

    with pytest.raises(ReportingError, match=r"^invalid report:"):
        validate_report_v1(report)


@pytest.mark.parametrize(
    ("mode", "targets", "expected_scope"),
    (
        ("focused", (), "complete"),
        ("strict_aggregate", ("tests/test_example.py",), "partial"),
    ),
    ids=("focused", "direct-target"),
)
def test_unconfigured_coverage_guidance_builds_valid_report_and_renders(
    tmp_path: Path,
    mode: RunMode,
    targets: tuple[str, ...],
    expected_scope: str,
) -> None:
    report = build_coverage_report(
        tmp_path,
        mode=mode,
        targets=targets,
        fail_under=None,
    )

    assert report.coverage is not None
    assert report.coverage.status == "guidance"
    assert report.coverage.scope == expected_scope
    assert report.coverage.threshold == CoverageThreshold(
        False, None, False, None, "not_configured"
    )
    assert validate_report_v1(report) is None
    terminal = render_terminal(report)
    assert f"coverage: guidance ({expected_scope}); no minimum configured" in terminal
    assert "src/example.py" not in terminal
    assert "TOTAL" in terminal
    payload = reporting.json.loads(serialize_json(report))
    assert payload["coverage"]["threshold"] == {
        "configured": False,
        "value": None,
        "evaluated": False,
        "passed": None,
        "skipped_reason": "not_configured",
    }


def test_coverage_preflight_failure_owns_the_pytest_check_before_primary(tmp_path: Path) -> None:
    check = pytest_planned_check(tmp_path)
    assert check.pytest is not None
    check = replace(
        check,
        pytest=replace(
            check.pytest,
            coverage=CoverageExecutionPlan(
                check.pytest.consumer_python, tmp_path / "pyproject.toml", 90
            ),
        ),
    )
    pytest_preflight = pytest_preflight_process(check)
    coverage_preflight = ExecutedProcess(
        role="coverage_preflight",
        command=("coverage", "preflight"),
        cwd=tmp_path,
        returncode=1,
        duration_ms=1,
        stdout=CapturedBytes(b"", 0),
        stderr=CapturedBytes(b"missing", 0),
        spawn_error=None,
    )
    coverage = CoverageExecutionObservation(
        CoveragePreflightObservation("preflight_invalid", None, "coverage probe failed"),
        CoverageArtifactObservation("not_attempted", None, None),
    )
    pytest_observation = PytestExecutionObservation(
        PytestPreflightObservation(
            "supported", PytestPreflightRecord((3, 13, 15), True, (8, 4, 2)), None
        ),
        PytestArtifactObservation("not_attempted", None, (), None),
        None,
    )
    report = build_run_report(
        tmp_path,
        run_plan((check,), mode="strict_aggregate", planned_coverage_scope="complete"),
        ExecutionResult(
            (
                ExecutedCheck(
                    check,
                    (pytest_preflight, coverage_preflight),
                    pytest_observation,
                    coverage,
                ),
            ),
            1,
        ),
    )

    assert report.checks[0].error == CheckError(
        "coverage_preflight_failed", "coverage probe failed"
    )
    assert report.coverage is not None
    assert report.coverage.error == CoverageError("preflight_invalid", "coverage probe failed")
    assert validate_report_v1(report) is None


def test_pytest_preflight_failure_owns_dual_preflight_failure(tmp_path: Path) -> None:
    check = coverage_enabled_pytest_check(tmp_path)
    pytest_preflight = replace(
        pytest_preflight_process(check), returncode=1, stderr=CapturedBytes(b"pytest", 0)
    )
    coverage_preflight = coverage_process(
        check, "coverage_preflight", 1, stderr=b"coverage"
    )
    pytest_observation = PytestExecutionObservation(
        PytestPreflightObservation("preflight_invalid", None, "pytest probe failed"),
        PytestArtifactObservation("not_attempted", None, (), None),
        None,
    )
    coverage_observation = CoverageExecutionObservation(
        CoveragePreflightObservation("preflight_invalid", None, "coverage probe failed"),
        CoverageArtifactObservation("not_attempted", None, None),
    )

    report = build_run_report(
        tmp_path,
        run_plan((check,), mode="strict_aggregate", planned_coverage_scope="complete"),
        ExecutionResult(
            (
                ExecutedCheck(
                    check,
                    (pytest_preflight, coverage_preflight),
                    pytest_observation,
                    coverage_observation,
                ),
            ),
            1,
        ),
    )

    assert report.checks[0].error == CheckError("pytest_preflight_failed", "pytest probe failed")
    assert report.coverage is not None
    assert report.coverage.error == CoverageError("preflight_invalid", "coverage probe failed")
    assert validate_report_v1(report) is None


def test_pytest_launcher_failure_owns_check_when_coverage_preflight_is_unattempted(
    tmp_path: Path,
) -> None:
    check = coverage_enabled_pytest_check(tmp_path)
    pytest_preflight = replace(
        pytest_preflight_process(check),
        returncode=None,
        spawn_error="FileNotFoundError: python",
    )
    pytest_observation = PytestExecutionObservation(
        PytestPreflightObservation("spawn_failed", None, "FileNotFoundError: python"),
        PytestArtifactObservation("not_attempted", None, (), None),
        None,
    )
    coverage_observation = CoverageExecutionObservation(
        CoveragePreflightObservation(
            "preflight_invalid", None, "coverage preflight was not attempted"
        ),
        CoverageArtifactObservation("not_attempted", None, None),
    )

    report = build_run_report(
        tmp_path,
        run_plan((check,), mode="strict_aggregate", planned_coverage_scope="complete"),
        ExecutionResult(
            (
                ExecutedCheck(
                    check,
                    (pytest_preflight,),
                    pytest_observation,
                    coverage_observation,
                ),
            ),
            2,
        ),
    )

    assert report.checks[0].error == CheckError(
        "spawn_failed", "Could not start pytest: FileNotFoundError: python"
    )
    assert report.coverage is not None
    assert report.coverage.error == CoverageError(
        "preflight_invalid", "coverage preflight was not attempted"
    )
    assert validate_report_v1(report) is None


def test_malformed_zero_exit_coverage_preflight_owns_not_started_pytest_check(
    tmp_path: Path,
) -> None:
    check = coverage_enabled_pytest_check(tmp_path)
    coverage_preflight = coverage_process(
        check, "coverage_preflight", 0, stderr=b"malformed probe"
    )
    coverage_observation = CoverageExecutionObservation(
        CoveragePreflightObservation("preflight_invalid", None, "malformed coverage probe"),
        CoverageArtifactObservation("not_attempted", None, None),
    )

    report = build_run_report(
        tmp_path,
        run_plan((check,), mode="strict_aggregate", planned_coverage_scope="complete"),
        ExecutionResult(
            (
                ExecutedCheck(
                    check,
                    (pytest_preflight_process(check), coverage_preflight),
                    finalized_pytest_execution_observation(),
                    coverage_observation,
                ),
            ),
            2,
        ),
    )

    assert report.pytest is not None
    assert report.pytest.error == PytestError(
        "not_started", "pytest primary process was not observed"
    )
    assert report.checks[0].error == CheckError(
        "coverage_preflight_failed", "malformed coverage probe"
    )
    assert validate_report_v1(report) is None


def test_validation_rejects_primary_after_typed_coverage_preflight_failure(
    tmp_path: Path,
) -> None:
    check = coverage_enabled_pytest_check(tmp_path)
    primary = ExecutedProcess(
        role="primary",
        command=check.command,
        cwd=check.cwd,
        returncode=0,
        duration_ms=1,
        stdout=CapturedBytes(b"", 0),
        stderr=CapturedBytes(b"", 0),
        spawn_error=None,
    )
    coverage_observation = CoverageExecutionObservation(
        CoveragePreflightObservation("preflight_invalid", None, "malformed coverage probe"),
        CoverageArtifactObservation("not_attempted", None, None),
    )
    report = build_run_report(
        tmp_path,
        run_plan((check,), mode="strict_aggregate", planned_coverage_scope="complete"),
        ExecutionResult(
            (
                ExecutedCheck(
                    check,
                    (
                        pytest_preflight_process(check),
                        coverage_process(check, "coverage_preflight"),
                        primary,
                    ),
                    finalized_pytest_execution_observation(),
                    coverage_observation,
                ),
            ),
            2,
        ),
    )

    assert report.coverage is not None
    assert report.coverage.error == CoverageError("preflight_invalid", "malformed coverage probe")
    with pytest.raises(ReportingError, match=r"^invalid report:"):
        validate_report_v1(report)


def test_report_validation_recomputes_coverage_context_and_process_invariants(
    tmp_path: Path,
) -> None:
    strict = build_coverage_report(tmp_path / "strict")
    focused = build_coverage_report(
        tmp_path / "focused", mode="focused", fail_under=None
    )
    direct = build_coverage_report(
        tmp_path / "direct",
        targets=("tests/test_example.py",),
        fail_under=None,
    )
    failed = build_coverage_report(tmp_path / "failed", coverage_json_exit_code=2)
    assert strict.coverage is not None
    assert focused.coverage is not None
    assert direct.coverage is not None
    assert failed.coverage is not None

    focused_eligible = replace(
        focused,
        coverage=replace(focused.coverage, status="passed", gate_eligible=True),
    )
    focused_lower_precedence = replace(
        focused,
        coverage=replace(
            focused.coverage,
            threshold=replace(focused.coverage.threshold, skipped_reason="focused_run"),
        ),
    )
    direct_complete_scope = replace(direct, coverage=replace(direct.coverage, scope="complete"))
    missing_coverage_json = replace(
        strict,
        checks=(replace(strict.checks[0], processes=strict.checks[0].processes[:-1]),),
    )
    wrong_coverage_json_exit = replace(
        strict,
        checks=(
            replace(
                strict.checks[0],
                processes=(
                    *strict.checks[0].processes[:-1],
                    replace(strict.checks[0].processes[-1], exit_code=1),
                ),
            ),
        ),
    )
    primary_after_failed_coverage_preflight = replace(
        strict,
        checks=(
            replace(
                strict.checks[0],
                processes=(
                    strict.checks[0].processes[0],
                    replace(strict.checks[0].processes[1], exit_code=1),
                    *strict.checks[0].processes[2:],
                ),
            ),
        ),
    )
    failed_with_zero_json_exit = replace(
        failed,
        checks=(
            replace(
                failed.checks[0],
                processes=(
                    *failed.checks[0].processes[:-1],
                    replace(failed.checks[0].processes[-1], exit_code=0),
                ),
            ),
        ),
    )

    for invalid in (
        focused_eligible,
        focused_lower_precedence,
        direct_complete_scope,
        missing_coverage_json,
        wrong_coverage_json_exit,
        primary_after_failed_coverage_preflight,
        failed_with_zero_json_exit,
    ):
        with pytest.raises(ReportingError, match=r"^invalid report:"):
            validate_report_v1(invalid)


@pytest.mark.parametrize(
    ("case", "expected_code"),
    (
        ("setup-without-process", "preflight_invalid"),
        ("setup-after-pytest-preflight", "preflight_invalid"),
        ("typed-unsupported-python", "unsupported_python"),
        ("typed-module-unavailable", "module_unavailable"),
        ("typed-stable-unsupported-version", "unsupported_version"),
        ("typed-untrusted-unsupported-version", "unsupported_version"),
        ("typed-preflight-invalid", "preflight_invalid"),
        ("coverage-preflight-spawn", "spawn_failed"),
        ("coverage-preflight-signal", "terminated_by_signal"),
        ("pytest-preflight-failed-coverage-supported", "data_missing"),
        ("data-missing-without-primary", "data_missing"),
        ("data-missing-after-primary", "data_missing"),
        ("unsupported-parallelism", "unsupported_parallelism"),
        ("unexpected-parallel-before-json", "unexpected_parallel_data"),
        ("coverage-json-spawn", "spawn_failed"),
        ("coverage-json-signal", "terminated_by_signal"),
        ("generation-failed", "generation_failed"),
        ("artifact-missing-zero", "artifact_missing"),
        ("artifact-invalid-zero", "artifact_invalid"),
        ("artifact-missing-threshold", "artifact_missing"),
        ("artifact-invalid-threshold", "artifact_invalid"),
        ("unexpected-parallel-after-json", "unexpected_parallel_data"),
    ),
)
def test_report_validation_accepts_every_coverage_error_stage(
    tmp_path: Path,
    case: str,
    expected_code: str,
) -> None:
    report = build_coverage_error_stage_report(tmp_path, case)

    assert report.coverage is not None
    assert report.coverage.error is not None
    assert report.coverage.error.code == expected_code
    validate_report_v1(report)


def test_report_validation_rejects_combined_coverage_error_stage_defects(
    tmp_path: Path,
) -> None:
    generation_failed = build_coverage_error_stage_report(
        tmp_path / "generation", "generation-failed"
    )
    artifact_invalid = build_coverage_error_stage_report(
        tmp_path / "artifact", "artifact-invalid-zero"
    )
    coverage_json_spawn = build_coverage_error_stage_report(
        tmp_path / "spawn", "coverage-json-spawn"
    )
    unsupported_parallelism = build_coverage_error_stage_report(
        tmp_path / "parallel", "unsupported-parallelism"
    )
    strict_artifact_exit_two = build_coverage_error_stage_report(
        tmp_path / "strict-threshold", "artifact-invalid-threshold"
    )
    successful_json = build_coverage_report(tmp_path / "json").checks[0].processes[-1]

    generation_check = generation_failed.checks[0]
    generation_json = generation_check.processes[-1]
    generation_without_json = replace(
        generation_failed,
        checks=(replace(generation_check, processes=generation_check.processes[:-1]),),
    )
    generation_with_zero_exit = replace(
        generation_failed,
        checks=(
            replace(
                generation_check,
                processes=(
                    *generation_check.processes[:-1],
                    replace(generation_json, exit_code=0),
                ),
            ),
        ),
    )
    generation_with_signal = replace(
        generation_failed,
        checks=(
            replace(
                generation_check,
                processes=(
                    *generation_check.processes[:-1],
                    replace(
                        generation_json,
                        outcome="signaled",
                        exit_code=None,
                        signal=9,
                        error_message="Process terminated by signal 9.",
                    ),
                ),
            ),
        ),
    )
    artifact_check = artifact_invalid.checks[0]
    artifact_after_exit_one = replace(
        artifact_invalid,
        checks=(
            replace(
                artifact_check,
                processes=(
                    *artifact_check.processes[:-1],
                    replace(artifact_check.processes[-1], exit_code=1),
                ),
            ),
        ),
    )
    spawn_check = coverage_json_spawn.checks[0]
    spawn_with_all_exited = replace(
        coverage_json_spawn,
        checks=(
            replace(
                spawn_check,
                processes=(
                    *spawn_check.processes[:-1],
                    replace(
                        spawn_check.processes[-1],
                        outcome="exited",
                        exit_code=0,
                        signal=None,
                        error_message=None,
                    ),
                ),
            ),
        ),
    )
    parallel_check = unsupported_parallelism.checks[0]
    parallel_with_successful_json = replace(
        unsupported_parallelism,
        checks=(replace(parallel_check, processes=(*parallel_check.processes, successful_json)),),
    )
    focused_artifact_exit_two = replace(strict_artifact_exit_two, mode="focused")

    for invalid in (
        generation_without_json,
        generation_with_zero_exit,
        generation_with_signal,
        artifact_after_exit_one,
        spawn_with_all_exited,
        parallel_with_successful_json,
        focused_artifact_exit_two,
    ):
        with pytest.raises(ReportingError, match=r"^invalid report:"):
            validate_report_v1(invalid)


def test_report_validation_correlates_pre_json_parallelism_with_pytest_evidence(
    tmp_path: Path,
) -> None:
    report = build_coverage_error_stage_report(tmp_path, "unsupported-parallelism")

    assert report.pytest is not None
    assert report.pytest.error is not None
    assert report.pytest.error.code == "unsupported_parallelism"
    assert report.coverage is not None
    assert report.coverage.error is not None
    assert report.coverage.error.code == "unsupported_parallelism"
    validate_report_v1(report)

    pytest_without_parallelism = replace(
        report,
        pytest=replace(
            report.pytest,
            error=PytestError("unsupported_retries", "pytest artifact reports retries"),
        ),
    )
    coverage_without_parallelism = replace(
        report,
        coverage=replace(
            report.coverage,
            error=CoverageError("data_missing", "coverage data missing"),
        ),
    )

    for invalid in (pytest_without_parallelism, coverage_without_parallelism):
        with pytest.raises(ReportingError, match=r"^invalid report:"):
            validate_report_v1(invalid)


def test_report_validation_rejects_parallel_pytest_evidence_after_coverage_json(
    tmp_path: Path,
) -> None:
    parallelism = build_coverage_error_stage_report(
        tmp_path / "parallelism", "unsupported-parallelism"
    )
    generation_failed = build_coverage_error_stage_report(
        tmp_path / "generation", "generation-failed"
    )

    assert parallelism.pytest is not None
    assert parallelism.pytest.error == PytestError(
        "unsupported_parallelism", "pytest artifact reports unsupported parallel execution"
    )
    assert parallelism.checks[0].error is not None
    assert parallelism.checks[0].error.code == "pytest_evidence_error"
    assert generation_failed.coverage is not None
    assert generation_failed.coverage.error == CoverageError(
        "generation_failed", "generation_failed diagnostic"
    )
    escaped = replace(
        parallelism,
        coverage=generation_failed.coverage,
        checks=(
            replace(
                parallelism.checks[0],
                processes=(
                    *parallelism.checks[0].processes,
                    generation_failed.checks[0].processes[-1],
                ),
            ),
        ),
    )

    with pytest.raises(ReportingError, match=r"^invalid report:"):
        validate_report_v1(escaped)


def test_report_validation_rejects_preprimary_parallel_data_without_primary(
    tmp_path: Path,
) -> None:
    report = build_coverage_error_stage_report(tmp_path, "data-missing-without-primary")

    assert report.coverage is not None
    assert report.coverage.error == CoverageError("data_missing", "data_missing diagnostic")
    validate_report_v1(report)

    unexpected_parallel_data = replace(
        report,
        coverage=replace(
            report.coverage,
            error=CoverageError("unexpected_parallel_data", "parallel data was found"),
        ),
    )

    with pytest.raises(ReportingError, match=r"^invalid report:"):
        validate_report_v1(unexpected_parallel_data)


def test_report_validation_requires_typed_coverage_preflight_success(
    tmp_path: Path,
) -> None:
    for case in (
        "typed-unsupported-python",
        "typed-module-unavailable",
        "typed-stable-unsupported-version",
    ):
        report = build_coverage_error_stage_report(tmp_path / case, case)
        check = report.checks[0]
        failed_preflight = replace(check.processes[1], exit_code=1)
        invalid = replace(
            report,
            checks=(replace(check, processes=(check.processes[0], failed_preflight)),),
        )

        with pytest.raises(ReportingError, match=r"^invalid report:"):
            validate_report_v1(invalid)

    preflight_invalid = build_coverage_error_stage_report(
        tmp_path / "preflight-invalid", "typed-preflight-invalid"
    )
    check = preflight_invalid.checks[0]
    positive_exit = replace(check.processes[1], exit_code=1)
    validate_report_v1(
        replace(
            preflight_invalid,
            checks=(replace(check, processes=(check.processes[0], positive_exit)),),
        )
    )


def test_report_validation_rejects_eligible_exit_two_generation_failure(
    tmp_path: Path,
) -> None:
    generation_failed = build_coverage_error_stage_report(
        tmp_path / "strict", "generation-failed"
    )
    check = generation_failed.checks[0]
    eligible_exit_two = replace(check.processes[-1], exit_code=2)
    strict_invalid = replace(
        generation_failed,
        checks=(
            replace(
                check,
                processes=(*check.processes[:-1], eligible_exit_two),
            ),
        ),
    )

    with pytest.raises(ReportingError, match=r"^invalid report:"):
        validate_report_v1(strict_invalid)

    validate_report_v1(replace(strict_invalid, mode="focused"))


def test_terminal_renders_post_primary_coverage_json_diagnostics_once(tmp_path: Path) -> None:
    report = build_coverage_report(
        tmp_path,
        coverage_json_exit_code=1,
        coverage_json_stdout=b"coverage stdout\n",
        coverage_json_stderr=b"coverage stderr\n",
    )

    assert report.coverage is not None
    assert report.coverage.status == "error"
    assert report.checks[0].status == "passed"
    assert render_terminal(report) == (
        "\n==> pyrepo-check summary: error (strict aggregate, incomplete)\n"
        "    error: coverage evidence: coverage JSON generation exited with code 1\n"
        "    diagnostic: pytest coverage_json stdout: coverage stdout\n"
        "    diagnostic: pytest coverage_json stderr: coverage stderr\n"
        "    advisory: Configured coverage threshold was not applied to this run.\n"
        "    passed: pytest\n"
    )


def test_builds_exact_run_report_and_preserves_planned_order(tmp_path: Path) -> None:
    unresolved_root = tmp_path / "project" / ".." / "project"
    ruff = planned_check(unresolved_root, "ruff")
    ty = planned_check(unresolved_root, "ty")
    bandit = planned_check(unresolved_root, "bandit")
    pytest_check = planned_check(unresolved_root, "pytest")
    plan = run_plan(
        (ruff, ty, bandit, pytest_check),
        targets=("tests/a.py", "tests/a.py"),
    )
    execution = ExecutionResult(
        checks=(
            executed_check(ruff, 0, duration_ms=11, stdout=b"ruff ok\n"),
            executed_check(ty, 3, duration_ms=12, stderr=b"type failure\n"),
            executed_check(bandit, -9, duration_ms=13, stdout=b"partial"),
            executed_check(
                pytest_check,
                None,
                duration_ms=14,
                stdout=None,
                stderr=None,
                spawn_error="FileNotFoundError: uv",
            ),
        ),
        exit_code=3,
    )

    report = build_run_report(unresolved_root, plan, execution)

    assert report.selection == Selection(
        checks=("ruff", "ty", "bandit", "pytest"),
        targets=("tests/a.py", "tests/a.py"),
        test_shortcut=None,
        pytest_args=("tests/a.py", "tests/a.py"),
        planned_test_scope="partial",
        planned_coverage_scope="not_requested",
    )
    assert report.checks[:3] == (
        CheckResult(
            name="ruff",
            status="passed",
            processes=(
                ProcessResult(
                    role="primary",
                    argv=ruff.command,
                    cwd=str(unresolved_root.resolve()),
                    outcome="exited",
                    exit_code=0,
                    signal=None,
                    duration_ms=11,
                    stdout=CapturedText(True, "ruff ok\n", False, 0),
                    stderr=CapturedText(True, "", False, 0),
                    error_message=None,
                ),
            ),
            error=None,
        ),
        CheckResult(
            name="ty",
            status="failed",
            processes=(
                ProcessResult(
                    role="primary",
                    argv=ty.command,
                    cwd=str(unresolved_root.resolve()),
                    outcome="exited",
                    exit_code=3,
                    signal=None,
                    duration_ms=12,
                    stdout=CapturedText(True, "", False, 0),
                    stderr=CapturedText(True, "type failure\n", False, 0),
                    error_message=None,
                ),
            ),
            error=None,
        ),
        CheckResult(
            name="bandit",
            status="error",
            processes=(
                ProcessResult(
                    role="primary",
                    argv=bandit.command,
                    cwd=str(unresolved_root.resolve()),
                    outcome="signaled",
                    exit_code=None,
                    signal=9,
                    duration_ms=13,
                    stdout=CapturedText(True, "partial", False, 0),
                    stderr=CapturedText(True, "", False, 0),
                    error_message="Process terminated by signal 9.",
                ),
            ),
            error=CheckError(
                "terminated_by_signal",
                "Primary process terminated by signal 9.",
            ),
        ),
    )
    assert report.checks[3].status == "error"
    assert [process.role for process in report.checks[3].processes] == [
        "pytest_preflight",
        "primary",
    ]
    assert report.checks[3].error == CheckError(
        "spawn_failed", "Could not start pytest: FileNotFoundError: uv"
    )
    assert report.pytest is not None
    assert report.pytest.error == reporting.PytestError("spawn_failed", "FileNotFoundError: uv")
    assert report.coverage is None
    assert report.advisories == ()


def test_terminal_observations_are_explicitly_uncaptured(tmp_path: Path) -> None:
    check = planned_check(tmp_path, "ruff")
    plan = run_plan((check,), output_format="terminal")
    execution = ExecutionResult(
        checks=(executed_check(check, 0, stdout=None, stderr=None),),
        exit_code=0,
    )

    report = build_run_report(tmp_path, plan, execution)

    process = report.checks[0].processes[0]
    assert process.stdout == CapturedText(False, "", False, 0)
    assert process.stderr == CapturedText(False, "", False, 0)


def test_json_spawn_failure_streams_are_captured_empty(tmp_path: Path) -> None:
    check = planned_check(tmp_path, "ruff")
    observation = executed_check(
        check,
        None,
        stdout=None,
        stderr=None,
        spawn_error="FileNotFoundError: uv",
    )

    report = build_run_report(
        tmp_path,
        run_plan((check,), output_format="json"),
        ExecutionResult((observation,), 2),
    )

    process = report.checks[0].processes[0]
    assert process.stdout == CapturedText(True, "", False, 0)
    assert process.stderr == CapturedText(True, "", False, 0)


def test_capture_failure_projects_existing_code_without_false_spawn_wording(
    tmp_path: Path,
) -> None:
    check = planned_check(tmp_path, "ruff")
    diagnostic = "stdout drain failed: OSError: synthetic drain failure"
    observation = executed_check(
        check,
        None,
        stdout=None,
        stderr=None,
        spawn_error=diagnostic,
    )

    report = build_run_report(
        tmp_path,
        run_plan((check,), output_format="json"),
        ExecutionResult((observation,), 2),
    )

    assert report.checks[0].processes[0].outcome == "spawn_failed"
    assert report.checks[0].error == CheckError(
        "spawn_failed", f"Process execution failed: {diagnostic}"
    )
    assert validate_report_v1(report) is None


def test_pytest_capture_failure_does_not_claim_pytest_never_started(
    tmp_path: Path,
) -> None:
    check = pytest_planned_check(tmp_path)
    diagnostic = "stderr drain failed: OSError: synthetic drain failure"
    observation = executed_check(check, None, spawn_error=diagnostic)

    report = build_run_report(
        tmp_path,
        run_plan((check,)),
        ExecutionResult((observation,), 2),
    )

    assert report.pytest is not None
    assert report.pytest.error == PytestError("spawn_failed", diagnostic)
    assert report.checks[0].error == CheckError(
        "spawn_failed", f"Pytest execution failed: {diagnostic}"
    )
    assert validate_report_v1(report) is None


@pytest.mark.parametrize(
    ("returncode", "status", "complete", "overall_status"),
    (
        (0, "passed", True, "passed"),
        (2, "failed", True, "failed"),
        (-15, "error", False, "error"),
        (None, "error", False, "error"),
    ),
)
def test_projects_process_outcome_and_run_completeness(
    tmp_path: Path,
    returncode: int | None,
    status: str,
    complete: bool,
    overall_status: str,
) -> None:
    check = planned_check(tmp_path, "ruff")
    observation = executed_check(
        check,
        returncode,
        spawn_error="PermissionError: denied" if returncode is None else None,
    )

    report = build_run_report(
        tmp_path,
        run_plan((check,)),
        ExecutionResult((observation,), 0),
    )

    assert report.checks[0].status == status
    assert report.complete is complete
    assert report.overall_status == overall_status


@pytest.mark.parametrize(
    ("targets", "planned_test_scope", "pytest_args"),
    (
        ((), "complete", ()),
        (("tests/test_cli.py::test_main",), "partial", ("tests/test_cli.py::test_main",)),
    ),
)
def test_selected_pytest_projects_exact_arguments_and_scope(
    tmp_path: Path,
    targets: tuple[str, ...],
    planned_test_scope: str,
    pytest_args: tuple[str, ...],
) -> None:
    check = planned_check(tmp_path, "pytest")

    report = build_run_report(
        tmp_path,
        run_plan((check,), targets=targets),
        ExecutionResult((executed_check(check, 0),), 0),
    )

    assert report.selection.pytest_args == pytest_args
    assert report.selection.planned_test_scope == planned_test_scope
    assert report.pytest is not None
    assert report.pytest.scope == ("complete" if not targets else "partial")
    assert report.coverage is None


def test_unselected_pytest_projects_null_arguments_and_scope(tmp_path: Path) -> None:
    check = planned_check(tmp_path, "ty")

    report = build_run_report(
        tmp_path,
        run_plan((check,), targets=("src/a.py", "src/a.py")),
        ExecutionResult((executed_check(check, 0),), 0),
    )

    assert report.selection.targets == ("src/a.py", "src/a.py")
    assert report.selection.pytest_args is None
    assert report.selection.planned_test_scope == "not_selected"
    assert report.selection.planned_coverage_scope == "not_requested"


def test_missing_observation_stays_in_planned_position(tmp_path: Path) -> None:
    ruff = planned_check(tmp_path, "ruff")
    ty = planned_check(tmp_path, "ty")

    report = build_run_report(
        tmp_path,
        run_plan((ruff, ty)),
        ExecutionResult((executed_check(ty, 0),), 0),
    )

    assert report.checks == (
        CheckResult(
            name="ruff",
            status="error",
            processes=(),
            error=CheckError(
                "missing_primary_process",
                "No primary process observation was recorded.",
            ),
        ),
        report.checks[1],
    )
    assert report.checks[1].name == "ty"
    assert report.checks[1].status == "passed"
    assert report.complete is False
    assert report.overall_status == "error"


def test_overall_error_precedes_failure(tmp_path: Path) -> None:
    ruff = planned_check(tmp_path, "ruff")
    ty = planned_check(tmp_path, "ty")

    report = build_run_report(
        tmp_path,
        run_plan((ruff, ty)),
        ExecutionResult(
            (
                executed_check(ruff, 1),
                executed_check(ty, -9),
            ),
            1,
        ),
    )

    assert tuple(check.status for check in report.checks) == ("failed", "error")
    assert report.overall_status == "error"
    assert report.complete is False


def test_builds_exact_planning_error_without_run_fields() -> None:
    report = build_planning_error_report(
        "unknown_check",
        "Unknown check(s): mypy",
        hint="Available checks: ruff, annotations, annotations-fix, ty, bandit, pytest",
    )

    assert report == PlanningErrorReportV1(
        schema_version=1,
        kind="planning_error",
        overall_status="error",
        complete=False,
        error=PlanningError(
            code="unknown_check",
            message="Unknown check(s): mypy",
            hint="Available checks: ruff, annotations, annotations-fix, ty, bandit, pytest",
        ),
    )
    assert asdict(report) == {
        "schema_version": 1,
        "kind": "planning_error",
        "overall_status": "error",
        "complete": False,
        "error": {
            "code": "unknown_check",
            "message": "Unknown check(s): mypy",
            "hint": "Available checks: ruff, annotations, annotations-fix, ty, bandit, pytest",
        },
    }


def test_render_terminal_snapshot_for_passed_run(tmp_path: Path) -> None:
    ruff = planned_check(tmp_path, "ruff")
    ty = planned_check(tmp_path, "ty")
    report = build_run_report(
        tmp_path,
        run_plan((ruff, ty)),
        ExecutionResult((executed_check(ruff, 0), executed_check(ty, 0)), 0),
    )

    assert render_terminal(report) == (
        "\n==> pyrepo-check summary: passed (focused)\n    passed: ruff, ty\n"
    )


def test_render_terminal_snapshot_orders_errors_failures_advisories_and_passes(
    tmp_path: Path,
) -> None:
    ruff = planned_check(tmp_path, "ruff")
    annotations = planned_check(tmp_path, "annotations")
    ty = planned_check(tmp_path, "ty")
    report = build_run_report(
        tmp_path,
        run_plan((ruff, annotations, ty)),
        ExecutionResult(
            (
                executed_check(
                    ruff,
                    None,
                    stdout=None,
                    stderr=None,
                    spawn_error="FileNotFoundError: uv",
                ),
                executed_check(annotations, 1, stderr=b"x" * 65_537),
                executed_check(ty, 0),
            ),
            1,
        ),
    )

    assert render_terminal(report) == (
        "\n"
        "==> pyrepo-check summary: error (focused, incomplete)\n"
        "    error: ruff: Could not start process: FileNotFoundError: uv\n"
        "    failed: annotations (exit 1)\n"
        "    advisory: annotations process 1 (primary) stderr omitted 1 byte(s); "
        "only the final 65536 bytes are included.\n"
        "    passed: ty\n"
    )


@pytest.mark.parametrize(
    ("hint", "expected"),
    (
        (
            "Available checks: ruff, annotations, annotations-fix, ty, bandit, pytest",
            (
                "Unknown check(s): mypy\n"
                "Hint: Available checks: ruff, annotations, annotations-fix, ty, bandit, pytest\n"
            ),
        ),
        (None, "Unknown check(s): mypy\n"),
    ),
)
def test_render_terminal_planning_errors_are_stderr_ready(
    hint: str | None,
    expected: str,
) -> None:
    report = build_planning_error_report("unknown_check", "Unknown check(s): mypy", hint=hint)

    assert render_terminal(report) == expected


def test_serialize_json_returns_exact_utf8_planning_error_bytes() -> None:
    report = build_planning_error_report(
        "unknown_check",
        "Unknown check(s): caf\u00e9",
        hint="Use the configured caf\u00e9 check.",
    )

    expected = (
        b'{"schema_version":1,"kind":"planning_error","overall_status":"error",'
        b'"complete":false,"error":{"code":"unknown_check","message":'
        b'"Unknown check(s): caf\xc3\xa9","hint":"Use the configured caf\xc3\xa9 check."}}\n'
    )

    assert serialize_json(report) == expected
    assert serialize_json(report) == expected
    assert expected.endswith(b"\n")
    assert b"\n" not in expected[:-1]
    assert b"\\u00e9" not in expected


def test_serialize_json_projects_exact_run_members_in_normative_order(tmp_path: Path) -> None:
    ruff = planned_check(tmp_path, "ruff")
    root = str(tmp_path.resolve()).encode("utf-8")
    report = build_run_report(
        tmp_path,
        run_plan((ruff,), output_format="json"),
        ExecutionResult((executed_check(ruff, 0, stdout="snowman \u2603".encode("utf-8")),), 0),
    )

    expected = (
        b'{"schema_version":1,"kind":"run","project_root":"'
        + root
        + b'","mode":"focused","overall_status":"passed","complete":true,'
        b'"selection":{"checks":["ruff"],"targets":[],"test_shortcut":null,'
        b'"pytest_args":null,"planned_test_scope":"not_selected",'
        b'"planned_coverage_scope":"not_requested"},"checks":[{"name":"ruff",'
        b'"status":"passed","processes":[{"role":"primary","argv":["uv","run",'
        b'"python","-m","ruff"],"cwd":"'
        + root
        + b'","outcome":"exited","exit_code":0,"signal":null,"duration_ms":7,'
        b'"stdout":{"captured":true,"text":"snowman \xe2\x98\x83","truncated":false,'
        b'"omitted_bytes":0},"stderr":{"captured":true,"text":"","truncated":false,'
        b'"omitted_bytes":0},"error_message":null}],"error":null}],"pytest":null,'
        b'"coverage":null,"advisories":[]}\n'
    )

    assert serialize_json(report) == expected
    assert serialize_json(report) == serialize_json(report)


def test_serialize_json_projects_test_shortcut_selection_in_normative_order(
    tmp_path: Path,
) -> None:
    pytest_check = planned_check(tmp_path, "pytest")
    root = str(tmp_path.resolve()).encode("utf-8")
    report = build_run_report(
        tmp_path,
        run_plan(
            (pytest_check,),
            test_shortcut="unit",
            pytest_args=("tests/unit", "-m", "not slow"),
            planned_test_scope="partial",
        ),
        ExecutionResult((executed_check(pytest_check, 0),), 0),
    )

    assert report.selection == Selection(
        checks=("pytest",),
        targets=(),
        test_shortcut="unit",
        pytest_args=("tests/unit", "-m", "not slow"),
        planned_test_scope="partial",
        planned_coverage_scope="not_requested",
    )
    payload = reporting.json.loads(serialize_json(report))
    assert list(payload) == [
        "schema_version",
        "kind",
        "project_root",
        "mode",
        "overall_status",
        "complete",
        "selection",
        "checks",
        "pytest",
        "coverage",
        "advisories",
    ]
    assert payload["project_root"] == root.decode()
    assert [process["role"] for process in payload["checks"][0]["processes"]] == [
        "pytest_preflight",
        "primary",
    ]
    assert payload["pytest"]["scope_reasons"] == ["planned_selector"]
    assert payload["coverage"] is None


@pytest.mark.parametrize(
    "code",
    ("unknown_test_shortcut", "invalid_test_shortcut", "coverage_configuration_required"),
)
def test_serialize_json_accepts_test_shortcut_planning_errors(code: str) -> None:
    report = build_planning_error_report(cast(Any, code), "shortcut planning failed")

    assert validate_report_v1(report) is None
    assert serialize_json(report).startswith(b'{"schema_version":1,"kind":"planning_error"')


def test_serialize_json_validates_before_encoding_and_returns_no_bytes_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = build_planning_error_report("unknown_check", "Unknown check(s): mypy")

    def fail_validation(_: AgentReportV1) -> None:
        raise ReportingError("validation failed")

    monkeypatch.setattr(reporting, "validate_report_v1", fail_validation)

    with pytest.raises(ReportingError, match=r"^validation failed$"):
        serialize_json(report)


def test_serialize_json_propagates_encoder_failure_before_returning_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = build_planning_error_report("unknown_check", "Unknown check(s): mypy")

    def fail_encoding(*_: object, **__: object) -> str:
        raise ValueError("encoding failed")

    monkeypatch.setattr(reporting.json, "dumps", fail_encoding)

    with pytest.raises(ValueError, match=r"^encoding failed$"):
        serialize_json(report)


def test_select_exit_code_preserves_first_positive_process_code(tmp_path: Path) -> None:
    ruff = planned_check(tmp_path, "ruff")
    annotations = planned_check(tmp_path, "annotations")
    ty = planned_check(tmp_path, "ty")
    report = build_run_report(
        tmp_path,
        run_plan((ruff, annotations, ty)),
        ExecutionResult(
            (executed_check(ruff, 0), executed_check(annotations, 7), executed_check(ty, 3)),
            7,
        ),
    )

    assert select_exit_code(report) == 7


def test_select_exit_code_uses_valid_report_evidence_and_error_fallback(
    tmp_path: Path,
) -> None:
    passed_check = planned_check(tmp_path, "ruff")
    passed = build_run_report(
        tmp_path,
        run_plan((passed_check,)),
        ExecutionResult((executed_check(passed_check, 0),), 0),
    )
    failed_check = planned_check(tmp_path, "ty")
    failed = build_run_report(
        tmp_path,
        run_plan((failed_check,)),
        ExecutionResult((executed_check(failed_check, 1),), 1),
    )
    missing_check = planned_check(tmp_path, "bandit")
    errored = build_run_report(
        tmp_path,
        run_plan((missing_check,)),
        ExecutionResult((), 0),
    )
    planning_error = build_planning_error_report("unknown_check", "Unknown check(s): mypy")

    assert select_exit_code(passed) == 0
    assert select_exit_code(failed) == 1
    assert select_exit_code(errored) == 2
    assert select_exit_code(planning_error) == 2


def test_rejects_extra_execution_observation(tmp_path: Path) -> None:
    ruff = planned_check(tmp_path, "ruff")
    bandit = planned_check(tmp_path, "bandit")

    with pytest.raises(ReportingError):
        build_run_report(
            tmp_path,
            run_plan((ruff,)),
            ExecutionResult((executed_check(ruff, 0), executed_check(bandit, 0)), 0),
        )


def test_rejects_duplicate_execution_observation(tmp_path: Path) -> None:
    ruff = planned_check(tmp_path, "ruff")

    with pytest.raises(ReportingError):
        build_run_report(
            tmp_path,
            run_plan((ruff,)),
            ExecutionResult((executed_check(ruff, 0), executed_check(ruff, 0)), 0),
        )


def test_rejects_mismatched_planned_execution_observation(tmp_path: Path) -> None:
    ruff = planned_check(tmp_path, "ruff")
    mismatched = PlannedCheck(
        name="ruff",
        command=(*ruff.command, "--diff"),
        cwd=ruff.cwd,
    )

    with pytest.raises(ReportingError):
        build_run_report(
            tmp_path,
            run_plan((ruff,)),
            ExecutionResult((executed_check(mismatched, 0),), 0),
        )


def test_rejects_out_of_order_execution_observations(tmp_path: Path) -> None:
    ruff = planned_check(tmp_path, "ruff")
    ty = planned_check(tmp_path, "ty")

    with pytest.raises(ReportingError):
        build_run_report(
            tmp_path,
            run_plan((ruff, ty)),
            ExecutionResult((executed_check(ty, 0), executed_check(ruff, 0)), 0),
        )


def test_rejects_non_primary_ordinary_process_observation(tmp_path: Path) -> None:
    ruff = planned_check(tmp_path, "ruff")
    observation = ExecutedCheck(
        planned=ruff,
        processes=(
            ExecutedProcess(
                role="pytest_preflight",
                command=ruff.command,
                cwd=ruff.cwd,
                returncode=0,
                duration_ms=1,
                stdout=CapturedBytes(b"", 0),
                stderr=CapturedBytes(b"", 0),
                spawn_error=None,
            ),
        ),
    )

    with pytest.raises(
        ReportingError, match="ordinary check must contain exactly one primary process"
    ):
        build_run_report(
            tmp_path,
            run_plan((ruff,)),
            ExecutionResult((observation,), 0),
        )


def pytest_execution_observation() -> PytestExecutionObservation:
    return PytestExecutionObservation(
        preflight=PytestPreflightObservation("supported", None, None),
        artifact=PytestArtifactObservation("not_attempted", None, (), None),
        cleanup_error=None,
    )


def finalized_pytest_execution_observation(
    *, cleanup_error: str | None = None, exit_code: int = 0
) -> PytestExecutionObservation:
    artifact = {
        "schema_version": 1,
        "state": "finalized",
        "writer_id": "writer-1",
        "pytest_version": "8.4.2",
        "session": {
            "starts": 1,
            "finishes": 1,
            "exit_code": exit_code,
            "collection_completed": True,
            "stopped_early": False,
        },
        "effective_args": [],
        "semantic_options": {
            "collection_paths": [],
            "keyword": "",
            "markexpr": "",
            "deselect": [],
            "ignore": [],
            "ignore_glob": [],
            "lf": False,
            "pyargs": False,
            "collectonly": False,
            "setuponly": False,
            "setupplan": False,
        },
        "collection": {
            "initial_nodeids": [],
            "final_nodeids": [],
            "deselected_nodeids": [],
            "uncovered_removed_nodeids": [],
            "errors": [],
            "skips": [],
        },
        "reports": [],
        "flags": {
            "unsupported_parallelism": False,
            "unsupported_retries": False,
            "worker_metadata": False,
        },
    }
    return PytestExecutionObservation(
        preflight=PytestPreflightObservation(
            "supported",
            PytestPreflightRecord((3, 13, 15), True, (8, 4, 2)),
            None,
        ),
        artifact=PytestArtifactObservation(
            "snapshot", reporting.json.dumps(artifact).encode(), ("writer-1",), None
        ),
        cleanup_error=cleanup_error,
    )


def pytest_planned_check(root: Path) -> PlannedCheck:
    pytest_plan = PytestExecutionPlan(("uv", "run", "python"), ("tests",))
    return PlannedCheck(
        name="pytest",
        command=(*pytest_plan.consumer_python, "-m", "pytest", *pytest_plan.pytest_args),
        cwd=root,
        pytest=pytest_plan,
    )


def pytest_preflight_process(check: PlannedCheck) -> ExecutedProcess:
    return ExecutedProcess(
        role="pytest_preflight",
        command=("uv", "run", "python", "-c", "probe"),
        cwd=check.cwd,
        returncode=0,
        duration_ms=1,
        stdout=CapturedBytes(b"preflight", 0),
        stderr=CapturedBytes(b"", 0),
        spawn_error=None,
    )


def pytest_report_for_exit(
    tmp_path: Path,
    exit_code: int,
    *,
    observation: PytestExecutionObservation | None = None,
    targets: tuple[str, ...] = (),
) -> RunReportV1:
    check = pytest_planned_check(tmp_path)
    primary = ExecutedProcess(
        role="primary",
        command=check.command,
        cwd=check.cwd,
        returncode=exit_code,
        duration_ms=1,
        stdout=CapturedBytes(b"", 0),
        stderr=CapturedBytes(b"", 0),
        spawn_error=None,
    )
    return build_run_report(
        tmp_path,
        run_plan((check,), targets=targets),
        ExecutionResult(
            (
                ExecutedCheck(
                    planned=check,
                    processes=(pytest_preflight_process(check), primary),
                    pytest=observation
                    if observation is not None
                    else finalized_pytest_execution_observation(exit_code=exit_code),
                ),
            ),
            exit_code,
        ),
    )


def test_pytest_execution_bridge_projects_structured_evidence_and_both_processes(
    tmp_path: Path,
) -> None:
    check = pytest_planned_check(tmp_path)
    primary = ExecutedProcess(
        role="primary",
        command=check.command,
        cwd=check.cwd,
        returncode=0,
        duration_ms=7,
        stdout=CapturedBytes(b"primary", 0),
        stderr=CapturedBytes(b"", 0),
        spawn_error=None,
    )
    observation = ExecutedCheck(
        planned=check,
        processes=(pytest_preflight_process(check), primary),
        pytest=finalized_pytest_execution_observation(),
    )

    report = build_run_report(
        tmp_path,
        run_plan((check,)),
        ExecutionResult((observation,), 0),
    )

    assert report.pytest is not None
    assert report.pytest.status == "passed"
    assert report.pytest.complete is True
    assert report.pytest.exit_code == 0
    assert [process.role for process in report.checks[0].processes] == [
        "pytest_preflight",
        "primary",
    ]
    assert report.checks[0].processes[1].argv == check.command


def test_pytest_execution_bridge_projects_missing_primary_as_not_started(tmp_path: Path) -> None:
    check = pytest_planned_check(tmp_path)
    observation = ExecutedCheck(
        planned=check,
        processes=(pytest_preflight_process(check),),
        pytest=finalized_pytest_execution_observation(),
    )

    report = build_run_report(
        tmp_path,
        run_plan((check,)),
        ExecutionResult((observation,), 2),
    )

    assert report.pytest is not None
    assert report.pytest.error is not None
    assert report.pytest.error.code == "not_started"
    assert report.checks[0].error == CheckError(
        "missing_primary_process",
        "No primary process observation was recorded.",
    )


def test_selected_pytest_without_observation_is_not_started_and_incomplete(tmp_path: Path) -> None:
    check = pytest_planned_check(tmp_path)

    report = build_run_report(tmp_path, run_plan((check,)), ExecutionResult((), 0))

    assert report.overall_status == "error"
    assert report.complete is False
    assert report.pytest is not None
    assert report.pytest.error == reporting.PytestError(
        "not_started", "pytest execution was not observed"
    )
    assert report.checks[0].processes == ()
    assert report.checks[0].error == CheckError(
        "missing_primary_process", "No primary process observation was recorded."
    )


def test_pytest_cleanup_error_overrides_check_but_preserves_finalized_result(
    tmp_path: Path,
) -> None:
    check = pytest_planned_check(tmp_path)
    primary = ExecutedProcess(
        role="primary",
        command=check.command,
        cwd=check.cwd,
        returncode=0,
        duration_ms=1,
        stdout=CapturedBytes(b"", 0),
        stderr=CapturedBytes(b"", 0),
        spawn_error=None,
    )
    observation = ExecutedCheck(
        planned=check,
        processes=(pytest_preflight_process(check), primary),
        pytest=finalized_pytest_execution_observation(cleanup_error="PermissionError: denied"),
    )

    report = build_run_report(tmp_path, run_plan((check,)), ExecutionResult((observation,), 0))

    assert report.pytest is not None
    assert report.pytest.status == "passed"
    assert report.pytest.complete is True
    assert report.checks[0].status == "error"
    assert report.checks[0].error == CheckError(
        "cleanup_failed", "Could not clean up pytest evidence: PermissionError: denied"
    )
    assert report.complete is False


def test_terminal_renders_structured_pytest_special_slow_and_sorted_advisories(
    tmp_path: Path,
) -> None:
    check = pytest_planned_check(tmp_path)
    primary = ExecutedProcess(
        role="primary",
        command=check.command,
        cwd=check.cwd,
        returncode=0,
        duration_ms=1,
        stdout=CapturedBytes(b"", 0),
        stderr=CapturedBytes(b"", 0),
        spawn_error=None,
    )
    observation = ExecutedCheck(
        planned=check,
        processes=(pytest_preflight_process(check), primary),
        pytest=finalized_pytest_execution_observation(),
    )
    report = build_run_report(tmp_path, run_plan((check,)), ExecutionResult((observation,), 0))
    assert report.pytest is not None
    evidence = PytestEvidence(
        effective_args=(),
        collected=2,
        deselected=0,
        counts=PytestCounts(0, 0, 0, 2, 0, 0),
        collection_errors=(),
        collection_skips=(),
        slowest=(SlowTest("z::slow", 20), SlowTest("a::slow", 10)),
        special_outcomes=(
            SpecialTestOutcome("a::skip", "skipped", None, None, False, 10),
            SpecialTestOutcome("z::skip", "skipped", "because", None, False, 20),
        ),
    )
    pytest_result = replace(report.pytest, evidence=evidence)
    report = replace(
        report,
        pytest=pytest_result,
        advisories=reporting._build_advisories(report.checks, pytest_result),
    )

    assert render_terminal(report) == (
        "\n==> pyrepo-check summary: passed (focused)\n"
        "    special: pytest skipped: a::skip\n"
        "    special: pytest skipped: z::skip (because)\n"
        "    slow: pytest z::slow (20 ms)\n"
        "    slow: pytest a::slow (10 ms)\n"
        "    advisory: pytest skipped has no reason: a::skip.\n"
        "    passed: pytest\n"
    )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda report: replace(report, pytest=None),
        lambda report: replace(
            report,
            pytest=replace(
                report.pytest, complete=True, error=PytestError("internal_error", "bad")
            ),
        ),
        lambda report: replace(
            report,
            pytest=replace(
                report.pytest,
                evidence=replace(
                    report.pytest.evidence,
                    counts=replace(report.pytest.evidence.counts, passed=cast(Any, True)),
                ),
            ),
        ),
    ),
    ids=("selected-null", "complete-error", "boolean-count"),
)
def test_validation_rejects_malformed_public_pytest_models(
    tmp_path: Path, mutate: Callable[[RunReportV1], RunReportV1]
) -> None:
    check = pytest_planned_check(tmp_path)
    primary = ExecutedProcess(
        role="primary",
        command=check.command,
        cwd=check.cwd,
        returncode=0,
        duration_ms=1,
        stdout=CapturedBytes(b"", 0),
        stderr=CapturedBytes(b"", 0),
        spawn_error=None,
    )
    observation = ExecutedCheck(
        planned=check,
        processes=(pytest_preflight_process(check), primary),
        pytest=finalized_pytest_execution_observation(),
    )
    report = build_run_report(tmp_path, run_plan((check,)), ExecutionResult((observation,), 0))

    with pytest.raises(ReportingError, match=r"^invalid report:"):
        validate_report_v1(mutate(report))


def test_validation_rejects_malformed_pytest_nested_values_as_reporting_error(
    tmp_path: Path,
) -> None:
    check = pytest_planned_check(tmp_path)
    primary = ExecutedProcess(
        role="primary",
        command=check.command,
        cwd=check.cwd,
        returncode=0,
        duration_ms=1,
        stdout=CapturedBytes(b"", 0),
        stderr=CapturedBytes(b"", 0),
        spawn_error=None,
    )
    report = build_run_report(
        tmp_path,
        run_plan((check,)),
        ExecutionResult(
            (
                ExecutedCheck(
                    planned=check,
                    processes=(pytest_preflight_process(check), primary),
                    pytest=finalized_pytest_execution_observation(),
                ),
            ),
            0,
        ),
    )
    assert report.pytest is not None
    assert report.pytest.evidence is not None
    malformed = replace(
        report,
        pytest=replace(
            report.pytest,
            evidence=replace(report.pytest.evidence, slowest=(cast(Any, object()),)),
        ),
    )

    with pytest.raises(ReportingError, match=r"^invalid report:"):
        validate_report_v1(malformed)


def test_validation_rejects_pytest_primary_exit_mismatch(tmp_path: Path) -> None:
    check = pytest_planned_check(tmp_path)
    primary = ExecutedProcess(
        role="primary",
        command=check.command,
        cwd=check.cwd,
        returncode=0,
        duration_ms=1,
        stdout=CapturedBytes(b"", 0),
        stderr=CapturedBytes(b"", 0),
        spawn_error=None,
    )
    report = build_run_report(
        tmp_path,
        run_plan((check,)),
        ExecutionResult(
            (
                ExecutedCheck(
                    planned=check,
                    processes=(pytest_preflight_process(check), primary),
                    pytest=finalized_pytest_execution_observation(),
                ),
            ),
            0,
        ),
    )
    assert report.pytest is not None

    with pytest.raises(ReportingError, match=r"^invalid report:"):
        validate_report_v1(replace(report, pytest=replace(report.pytest, exit_code=1)))


def test_validation_rejects_planned_selector_scope_mismatch(tmp_path: Path) -> None:
    check = pytest_planned_check(tmp_path)
    primary = ExecutedProcess(
        role="primary",
        command=check.command,
        cwd=check.cwd,
        returncode=0,
        duration_ms=1,
        stdout=CapturedBytes(b"", 0),
        stderr=CapturedBytes(b"", 0),
        spawn_error=None,
    )
    report = build_run_report(
        tmp_path,
        run_plan((check,), targets=("tests/test_one.py",)),
        ExecutionResult(
            (
                ExecutedCheck(
                    planned=check,
                    processes=(pytest_preflight_process(check), primary),
                    pytest=finalized_pytest_execution_observation(),
                ),
            ),
            0,
        ),
    )
    assert report.pytest is not None
    mismatched = replace(report.pytest, scope="complete", scope_reasons=())

    with pytest.raises(ReportingError, match=r"^invalid report:"):
        validate_report_v1(replace(report, pytest=mismatched))


def test_validation_rejects_artifact_scope_reasons_when_evidence_is_null(
    tmp_path: Path,
) -> None:
    check = pytest_planned_check(tmp_path)
    primary = ExecutedProcess(
        role="primary",
        command=check.command,
        cwd=check.cwd,
        returncode=0,
        duration_ms=1,
        stdout=CapturedBytes(b"", 0),
        stderr=CapturedBytes(b"", 0),
        spawn_error=None,
    )
    report = build_run_report(
        tmp_path,
        run_plan((check,)),
        ExecutionResult(
            (
                ExecutedCheck(
                    planned=check,
                    processes=(pytest_preflight_process(check), primary),
                    pytest=finalized_pytest_execution_observation(),
                ),
            ),
            0,
        ),
    )
    assert report.pytest is not None
    invalid_result = replace(
        report.pytest,
        status="error",
        complete=False,
        scope="partial",
        scope_reasons=("effective_narrowing_option", "incomplete_session"),
        evidence=None,
        error=PytestError("artifact_missing", "pytest artifact is missing"),
    )
    invalid_check = replace(
        report.checks[0],
        status="error",
        error=CheckError("pytest_evidence_error", "pytest artifact is missing"),
    )
    invalid_report = replace(
        report,
        overall_status="error",
        complete=False,
        checks=(invalid_check,),
        pytest=invalid_result,
    )

    with pytest.raises(ReportingError, match=r"^invalid report:"):
        validate_report_v1(invalid_report)


def test_terminal_renders_pytest_incomplete_helper_diagnostic_and_cleanup_failure(
    tmp_path: Path,
) -> None:
    check = pytest_planned_check(tmp_path)
    failed_primary = ExecutedProcess(
        role="primary",
        command=check.command,
        cwd=check.cwd,
        returncode=1,
        duration_ms=1,
        stdout=CapturedBytes(b"", 0),
        stderr=CapturedBytes(b"", 0),
        spawn_error=None,
    )
    cleanup_report = build_run_report(
        tmp_path,
        run_plan((check,)),
        ExecutionResult(
            (
                ExecutedCheck(
                    planned=check,
                    processes=(pytest_preflight_process(check), failed_primary),
                    pytest=finalized_pytest_execution_observation(
                        cleanup_error="PermissionError: denied", exit_code=1
                    ),
                ),
            ),
            1,
        ),
    )

    assert render_terminal(cleanup_report) == (
        "\n==> pyrepo-check summary: error (focused, incomplete)\n"
        "    error: pytest: Could not clean up pytest evidence: PermissionError: denied\n"
        "    diagnostic: pytest pytest_preflight stdout: preflight\n"
        "    failed: pytest (exit 1)\n"
    )

    preflight = ExecutedCheck(
        planned=check,
        processes=(pytest_preflight_process(check),),
        pytest=pytest_execution_observation(),
    )
    preflight_report = build_run_report(
        tmp_path, run_plan((check,)), ExecutionResult((preflight,), 2)
    )

    assert render_terminal(preflight_report) == (
        "\n==> pyrepo-check summary: error (focused, incomplete)\n"
        "    error: pytest evidence: supported preflight has no pytest version\n"
        "    diagnostic: pytest pytest_preflight stdout: preflight\n"
    )


def test_terminal_projects_each_logical_pytest_error_once(tmp_path: Path) -> None:
    interrupted = pytest_report_for_exit(tmp_path, 2)
    artifact_observation = finalized_pytest_execution_observation()
    artifact_missing = pytest_report_for_exit(
        tmp_path,
        0,
        observation=replace(
            artifact_observation,
            artifact=PytestArtifactObservation("missing", None, (), None),
        ),
    )
    check = pytest_planned_check(tmp_path)
    not_started = build_run_report(tmp_path, run_plan((check,)), ExecutionResult((), 0))

    assert render_terminal(interrupted) == (
        "\n==> pyrepo-check summary: error (focused, incomplete)\n"
        "    error: pytest evidence: pytest execution was interrupted\n"
        "    diagnostic: pytest pytest_preflight stdout: preflight\n"
    )
    assert render_terminal(artifact_missing) == (
        "\n==> pyrepo-check summary: error (focused, incomplete)\n"
        "    error: pytest evidence: pytest artifact is missing\n"
        "    diagnostic: pytest pytest_preflight stdout: preflight\n"
    )
    assert render_terminal(not_started) == (
        "\n==> pyrepo-check summary: error (focused, incomplete)\n"
        "    error: pytest evidence: pytest execution was not observed\n"
    )


def test_missing_test_reason_advisories_include_empty_reasons_and_deduplicate(
    tmp_path: Path,
) -> None:
    evidence = PytestEvidence(
        effective_args=(),
        collected=1,
        deselected=0,
        counts=PytestCounts(0, 0, 0, 1, 0, 0),
        collection_errors=(),
        collection_skips=(),
        slowest=(),
        special_outcomes=(SpecialTestOutcome("test_empty", "skipped", "", None, False, 1),),
    )
    pytest_result = reporting.PytestResult(
        status="passed",
        complete=True,
        scope="complete",
        scope_reasons=(),
        pytest_version="8.4.2",
        exit_code=0,
        evidence=evidence,
        error=None,
    )
    truncated = ProcessResult(
        role="primary",
        argv=("pytest",),
        cwd=str(tmp_path.resolve()),
        outcome="exited",
        exit_code=0,
        signal=None,
        duration_ms=0,
        stdout=CapturedText(True, "", True, 1),
        stderr=CapturedText(True, "", False, 0),
        error_message=None,
    )
    check = CheckResult("ruff", "passed", (truncated,), None)

    advisories = reporting._build_advisories((check, check), pytest_result)

    assert advisories == (
        Advisory("missing_test_reason", "pytest skipped has no reason: test_empty.", None),
        Advisory(
            "output_truncated",
            "ruff process 1 (primary) stdout omitted 1 byte(s); only the final 65536 bytes are included.",
            None,
        ),
    )


@pytest.mark.parametrize(
    ("exit_code", "status", "complete", "error_code"),
    (
        (0, "passed", True, None),
        (1, "failed", True, None),
        (2, "error", False, "interrupted"),
        (3, "error", False, "internal_error"),
        (4, "error", False, "usage_error"),
        (5, "failed", True, None),
        (9, "error", False, "unknown_exit_code"),
    ),
)
def test_pytest_exit_matrix_projects_every_authoritative_primary_exit(
    tmp_path: Path,
    exit_code: int,
    status: str,
    complete: bool,
    error_code: str | None,
) -> None:
    report = pytest_report_for_exit(tmp_path, exit_code)

    assert report.pytest is not None
    assert report.pytest.status == status
    assert report.pytest.complete is complete
    assert report.pytest.exit_code == exit_code
    assert report.pytest.error is None or report.pytest.error.code == error_code
    assert report.checks[0].status == status
    assert validate_report_v1(report) is None
    assert select_exit_code(report) == (exit_code if exit_code else 0)


def test_serialize_json_projects_exact_nested_pytest_exit_five_shape(tmp_path: Path) -> None:
    report = pytest_report_for_exit(tmp_path, 5)

    payload = reporting.json.loads(serialize_json(report))

    assert payload["pytest"] == {
        "status": "failed",
        "complete": True,
        "scope": "complete",
        "scope_reasons": [],
        "pytest_version": "8.4.2",
        "exit_code": 5,
        "evidence": {
            "effective_args": [],
            "collected": 0,
            "deselected": 0,
            "counts": {
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "xfailed": 0,
                "xpassed": 0,
            },
            "collection_errors": [],
            "collection_skips": [],
            "slowest": [],
            "special_outcomes": [],
        },
        "error": None,
    }
    assert payload["coverage"] is None


def test_serialize_json_projects_exact_nonempty_pytest_evidence_members(tmp_path: Path) -> None:
    report = pytest_report_for_exit(tmp_path, 0)
    assert report.pytest is not None
    evidence = PytestEvidence(
        effective_args=(),
        collected=4,
        deselected=0,
        counts=PytestCounts(0, 0, 0, 1, 1, 2),
        collection_errors=(
            CollectionIssue("tests/test_alpha.py", "ImportError: alpha"),
            CollectionIssue("tests/test_zeta.py", "ImportError: zeta"),
        ),
        collection_skips=(
            CollectionIssue("tests/test_alpha.py", "skip alpha"),
            CollectionIssue("tests/test_zeta.py", "skip zeta"),
        ),
        slowest=(
            SlowTest("tests/test_zeta.py::test_strict_xpass", 40),
            SlowTest("tests/test_beta.py::test_xfail", 30),
            SlowTest("tests/test_delta.py::test_non_strict_xpass", 20),
            SlowTest("tests/test_alpha.py::test_skip", 10),
        ),
        special_outcomes=(
            SpecialTestOutcome(
                "tests/test_alpha.py::test_skip", "skipped", None, None, False, 10
            ),
            SpecialTestOutcome(
                "tests/test_beta.py::test_xfail", "xfailed", "expected", None, False, 30
            ),
            SpecialTestOutcome(
                "tests/test_delta.py::test_non_strict_xpass",
                "xpassed",
                "non-strict",
                False,
                False,
                20,
            ),
            SpecialTestOutcome(
                "tests/test_zeta.py::test_strict_xpass", "xpassed", "strict", True, True, 40
            ),
        ),
    )
    pytest_result = replace(
        report.pytest,
        complete=False,
        scope="partial",
        scope_reasons=("incomplete_session",),
        evidence=evidence,
    )
    report = replace(
        report,
        overall_status="error",
        complete=False,
        pytest=pytest_result,
        advisories=reporting._build_advisories(report.checks, pytest_result),
    )

    payload = reporting.json.loads(serialize_json(report))

    assert payload["pytest"] == {
        "status": "passed",
        "complete": False,
        "scope": "partial",
        "scope_reasons": ["incomplete_session"],
        "pytest_version": "8.4.2",
        "exit_code": 0,
        "evidence": {
            "effective_args": [],
            "collected": 4,
            "deselected": 0,
            "counts": {
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "skipped": 1,
                "xfailed": 1,
                "xpassed": 2,
            },
            "collection_errors": [
                {"nodeid": "tests/test_alpha.py", "message": "ImportError: alpha"},
                {"nodeid": "tests/test_zeta.py", "message": "ImportError: zeta"},
            ],
            "collection_skips": [
                {"nodeid": "tests/test_alpha.py", "message": "skip alpha"},
                {"nodeid": "tests/test_zeta.py", "message": "skip zeta"},
            ],
            "slowest": [
                {"nodeid": "tests/test_zeta.py::test_strict_xpass", "duration_ms": 40},
                {"nodeid": "tests/test_beta.py::test_xfail", "duration_ms": 30},
                {
                    "nodeid": "tests/test_delta.py::test_non_strict_xpass",
                    "duration_ms": 20,
                },
                {"nodeid": "tests/test_alpha.py::test_skip", "duration_ms": 10},
            ],
            "special_outcomes": [
                {
                    "nodeid": "tests/test_alpha.py::test_skip",
                    "outcome": "skipped",
                    "reason": None,
                    "strict": None,
                    "affects_exit": False,
                    "duration_ms": 10,
                },
                {
                    "nodeid": "tests/test_beta.py::test_xfail",
                    "outcome": "xfailed",
                    "reason": "expected",
                    "strict": None,
                    "affects_exit": False,
                    "duration_ms": 30,
                },
                {
                    "nodeid": "tests/test_delta.py::test_non_strict_xpass",
                    "outcome": "xpassed",
                    "reason": "non-strict",
                    "strict": False,
                    "affects_exit": False,
                    "duration_ms": 20,
                },
                {
                    "nodeid": "tests/test_zeta.py::test_strict_xpass",
                    "outcome": "xpassed",
                    "reason": "strict",
                    "strict": True,
                    "affects_exit": True,
                    "duration_ms": 40,
                },
            ],
        },
        "error": None,
    }


def test_pytest_early_stop_keeps_partial_failed_evidence_and_terminal_attention(
    tmp_path: Path,
) -> None:
    observation = finalized_pytest_execution_observation(exit_code=1)
    artifact = observation.artifact
    assert artifact.content is not None
    document = reporting.json.loads(artifact.content)
    document["session"]["stopped_early"] = True
    observation = replace(
        observation,
        artifact=replace(artifact, content=reporting.json.dumps(document).encode()),
    )

    report = pytest_report_for_exit(tmp_path, 1, observation=observation)

    assert report.pytest == reporting.PytestResult(
        status="failed",
        complete=False,
        scope="partial",
        scope_reasons=("incomplete_session",),
        pytest_version="8.4.2",
        exit_code=1,
        evidence=PytestEvidence(
            effective_args=(),
            collected=0,
            deselected=0,
            counts=PytestCounts(0, 0, 0, 0, 0, 0),
            collection_errors=(),
            collection_skips=(),
            slowest=(),
            special_outcomes=(),
        ),
        error=PytestError(
            "session_incomplete", "pytest session stopped before all selected tests completed"
        ),
    )
    assert report.overall_status == "error"
    assert report.complete is False
    assert render_terminal(report) == (
        "\n==> pyrepo-check summary: error (focused, incomplete)\n"
        "    error: pytest evidence: pytest session stopped before all selected tests completed\n"
        "    failed: pytest (exit 1)\n"
    )


def test_incomplete_trusted_pytest_evidence_gets_attention_after_exit_zero(
    tmp_path: Path,
) -> None:
    observation = finalized_pytest_execution_observation()
    artifact = observation.artifact
    assert artifact.content is not None
    document = reporting.json.loads(artifact.content)
    document["collection"]["errors"] = [
        {"nodeid": "test_collect", "message": "ImportError: missing dependency"}
    ]
    observation = replace(
        observation,
        artifact=replace(artifact, content=reporting.json.dumps(document).encode()),
    )

    report = pytest_report_for_exit(tmp_path, 0, observation=observation)

    assert report.pytest is not None
    assert report.pytest.status == "passed"
    assert report.pytest.complete is False
    assert report.pytest.error is None
    assert report.pytest.evidence is not None
    assert report.pytest.evidence.collection_errors == (
        CollectionIssue("test_collect", "ImportError: missing dependency"),
    )
    assert render_terminal(report) == (
        "\n==> pyrepo-check summary: error (focused, incomplete)\n"
        "    error: pytest evidence is incomplete.\n"
        "    passed: pytest\n"
    )


@pytest.mark.parametrize(
    ("returncode", "spawn_error", "pytest_error", "check_error"),
    (
        (None, "FileNotFoundError: uv", "spawn_failed", "spawn_failed"),
        (-15, None, "terminated_by_signal", "terminated_by_signal"),
    ),
)
def test_pytest_primary_spawn_and_signal_keep_process_and_result_evidence(
    tmp_path: Path,
    returncode: int | None,
    spawn_error: str | None,
    pytest_error: str,
    check_error: str,
) -> None:
    check = pytest_planned_check(tmp_path)
    report = build_run_report(
        tmp_path,
        run_plan((check,)),
        ExecutionResult(
            (executed_check(check, returncode, spawn_error=spawn_error),),
            2,
        ),
    )

    assert report.pytest is not None
    assert report.pytest.status == "error"
    assert report.pytest.complete is False
    assert report.pytest.exit_code is None
    assert report.pytest.evidence is None
    assert report.pytest.error is not None
    assert report.pytest.error.code == pytest_error
    assert report.checks[0].error is not None
    assert report.checks[0].error.code == check_error
    assert validate_report_v1(report) is None
    assert select_exit_code(report) == 2


def test_pytest_preflight_failure_has_no_primary_and_preserves_helper_diagnostic(
    tmp_path: Path,
) -> None:
    check = pytest_planned_check(tmp_path)
    observation = ExecutedCheck(
        planned=check,
        processes=(pytest_preflight_process(check),),
        pytest=pytest_execution_observation(),
    )
    report = build_run_report(
        tmp_path,
        run_plan((check,)),
        ExecutionResult((observation,), 2),
    )

    assert report.pytest is not None
    assert report.pytest.error == PytestError(
        "preflight_invalid", "supported preflight has no pytest version"
    )
    assert report.checks[0].error == CheckError(
        "pytest_preflight_failed", "supported preflight has no pytest version"
    )
    assert len(report.checks[0].processes) == 1
    assert validate_report_v1(report) is None
    assert "pytest_preflight stdout: preflight" in render_terminal(report)


def test_pytest_missing_artifact_projects_null_evidence_without_artifact_scope_reasons(
    tmp_path: Path,
) -> None:
    observation = finalized_pytest_execution_observation()
    observation = replace(
        observation,
        artifact=PytestArtifactObservation("missing", None, (), None),
    )

    report = pytest_report_for_exit(tmp_path, 0, observation=observation)

    assert report.pytest == reporting.PytestResult(
        status="error",
        complete=False,
        scope="partial",
        scope_reasons=("incomplete_session",),
        pytest_version="8.4.2",
        exit_code=0,
        evidence=None,
        error=PytestError("artifact_missing", "pytest artifact is missing"),
    )
    assert report.checks[0].error == CheckError(
        "pytest_evidence_error", "pytest artifact is missing"
    )
    assert validate_report_v1(report) is None


def test_terminal_does_not_render_empty_test_reason_parentheses(tmp_path: Path) -> None:
    report = pytest_report_for_exit(tmp_path, 0)
    assert report.pytest is not None
    evidence = PytestEvidence(
        effective_args=(),
        collected=1,
        deselected=0,
        counts=PytestCounts(0, 0, 0, 1, 0, 0),
        collection_errors=(),
        collection_skips=(),
        slowest=(SlowTest("test_empty", 1),),
        special_outcomes=(SpecialTestOutcome("test_empty", "skipped", "", None, False, 1),),
    )
    pytest_result = replace(report.pytest, evidence=evidence)
    report = replace(
        report,
        pytest=pytest_result,
        advisories=reporting._build_advisories(report.checks, pytest_result),
    )

    terminal = render_terminal(report)

    assert terminal == (
        "\n==> pyrepo-check summary: passed (focused)\n"
        "    special: pytest skipped: test_empty\n"
        "    slow: pytest test_empty (1 ms)\n"
        "    advisory: pytest skipped has no reason: test_empty.\n"
        "    passed: pytest\n"
    )


def test_validation_rejects_pytest_cross_field_and_nested_cardinality_mutations(
    tmp_path: Path,
) -> None:
    report = pytest_report_for_exit(tmp_path, 0)
    assert report.pytest is not None
    assert report.pytest.evidence is not None
    preflight = report.checks[0].processes[0]

    missing_primary = replace(report, checks=(replace(report.checks[0], processes=(preflight,)),))
    complete_with_incomplete_reason = replace(
        report,
        pytest=replace(
            report.pytest,
            scope="partial",
            scope_reasons=("incomplete_session",),
        ),
    )
    non_cleanup_error = replace(
        report,
        overall_status="error",
        complete=False,
        checks=(
            replace(
                report.checks[0],
                status="error",
                error=CheckError("pytest_evidence_error", "not a cleanup failure"),
            ),
        ),
    )
    duplicate_special_evidence = PytestEvidence(
        effective_args=(),
        collected=2,
        deselected=0,
        counts=PytestCounts(0, 0, 0, 2, 0, 0),
        collection_errors=(),
        collection_skips=(),
        slowest=(SlowTest("test_a", 1), SlowTest("test_b", 1)),
        special_outcomes=(
            SpecialTestOutcome("test_a", "skipped", None, None, False, 1),
            SpecialTestOutcome("test_a", "skipped", None, None, False, 1),
        ),
    )
    duplicate_special = replace(
        report, pytest=replace(report.pytest, evidence=duplicate_special_evidence)
    )
    duplicate_issue_evidence = replace(
        report.pytest.evidence,
        collection_errors=(
            CollectionIssue("test_collect", "bad import"),
            CollectionIssue("test_collect", "bad import"),
        ),
    )
    duplicate_issue = replace(
        report, pytest=replace(report.pytest, evidence=duplicate_issue_evidence)
    )
    missing_slow_test_evidence = replace(
        report.pytest.evidence,
        collected=1,
        counts=PytestCounts(1, 0, 0, 0, 0, 0),
        slowest=(),
    )
    missing_slow_test = replace(
        report, pytest=replace(report.pytest, evidence=missing_slow_test_evidence)
    )
    malformed_process = replace(
        report,
        checks=(replace(report.checks[0], processes=(cast(Any, object()),)),),
    )

    for invalid in (
        missing_primary,
        complete_with_incomplete_reason,
        non_cleanup_error,
        duplicate_special,
        duplicate_issue,
        missing_slow_test,
        malformed_process,
    ):
        with pytest.raises(ReportingError, match=r"^invalid report:"):
            validate_report_v1(invalid)


def test_validation_rejects_not_started_when_preflight_has_spawn_evidence(tmp_path: Path) -> None:
    check = pytest_planned_check(tmp_path)
    report = build_run_report(tmp_path, run_plan((check,)), ExecutionResult((), 0))
    preflight_spawn_failure = ProcessResult(
        role="pytest_preflight",
        argv=("uv", "run", "python", "-c", "probe"),
        cwd=str(tmp_path.resolve()),
        outcome="spawn_failed",
        exit_code=None,
        signal=None,
        duration_ms=0,
        stdout=CapturedText(True, "", False, 0),
        stderr=CapturedText(True, "", False, 0),
        error_message="FileNotFoundError: uv",
    )
    invalid = replace(
        report,
        checks=(replace(report.checks[0], processes=(preflight_spawn_failure,)),),
    )

    with pytest.raises(ReportingError, match=r"^invalid report:"):
        validate_report_v1(invalid)


def test_validation_rejects_primary_pytest_result_without_trusted_version(tmp_path: Path) -> None:
    report = pytest_report_for_exit(tmp_path, 0)
    assert report.pytest is not None
    invalid = replace(report, pytest=replace(report.pytest, pytest_version=None))

    with pytest.raises(ReportingError, match=r"^invalid report:"):
        validate_report_v1(invalid)


def test_validation_rejects_not_started_pytest_version_without_matching_preflight(
    tmp_path: Path,
) -> None:
    check = pytest_planned_check(tmp_path)
    no_preflight = build_run_report(tmp_path, run_plan((check,)), ExecutionResult((), 0))
    successful_preflight = build_run_report(
        tmp_path,
        run_plan((check,)),
        ExecutionResult(
            (
                ExecutedCheck(
                    planned=check,
                    processes=(pytest_preflight_process(check),),
                    pytest=finalized_pytest_execution_observation(),
                ),
            ),
            2,
        ),
    )
    assert no_preflight.pytest is not None
    assert successful_preflight.pytest is not None
    invalid_without_preflight = replace(
        no_preflight,
        pytest=replace(no_preflight.pytest, pytest_version="8.4.2"),
    )
    invalid_after_successful_preflight = replace(
        successful_preflight,
        pytest=replace(successful_preflight.pytest, pytest_version=None),
    )

    for invalid in (invalid_without_preflight, invalid_after_successful_preflight):
        with pytest.raises(ReportingError, match=r"^invalid report:"):
            validate_report_v1(invalid)


@pytest.mark.parametrize(
    "boundary",
    ("run-directory", "plugin-copy", "writer-directory", "environment"),
)
def test_pytest_setup_not_started_projects_schema_valid_json_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    check = pytest_planned_check(tmp_path)

    if boundary == "run-directory":
        def fail_run_directory(_consumer_root: Path) -> Path:
            raise PermissionError("temporary directory denied")

        monkeypatch.setattr(
            pytest_execution,
            "_create_run_directory",
            fail_run_directory,
        )
    elif boundary == "plugin-copy":
        def fail_copy(
            _source: Path,
            _destination_name: str,
            *,
            run_descriptor: int,
        ) -> None:
            del run_descriptor
            raise PermissionError("plugin copy denied")

        monkeypatch.setattr(pytest_execution, "_copy_plugin_source", fail_copy)
    elif boundary == "writer-directory":
        original_mkdir = pytest_execution.os.mkdir

        def fail_writer_directory(
            path: str | bytes | os.PathLike[str],
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> None:
            if os.fsdecode(path) == "writers":
                raise PermissionError("writer directory denied")
            original_mkdir(path, mode=mode, dir_fd=dir_fd)

        monkeypatch.setattr(pytest_execution.os, "mkdir", fail_writer_directory)
    else:
        def fail_environment(
            _run_directory: Path,
            _artifact_path: Path,
            _writer_directory: Path,
        ) -> dict[str, str]:
            raise PermissionError("environment denied")

        monkeypatch.setattr(
            pytest_execution,
            "_isolated_environment",
            fail_environment,
        )

    execution = execute_plan(run_plan((check,)))
    report = build_run_report(tmp_path, run_plan((check,)), execution)
    validate_report_v1(report)
    payload = reporting.json.loads(serialize_json(report))

    assert execution.checks[0].processes == ()
    assert execution.checks[0].pytest is not None
    assert execution.checks[0].pytest.preflight.classification == "not_started"
    assert report.pytest is not None
    assert report.pytest.error is not None
    assert report.pytest.error.code == "not_started"
    assert report.checks[0].error == CheckError(
        "missing_primary_process",
        "No primary process observation was recorded.",
    )
    assert payload["pytest"]["error"]["code"] == "not_started"
    assert payload["checks"][0]["error"]["code"] == "missing_primary_process"


@pytest.mark.parametrize(
    "processes",
    [
        lambda check: (),
        lambda check: (
            ExecutedProcess(
                role="primary",
                command=check.command,
                cwd=check.cwd,
                returncode=0,
                duration_ms=1,
                stdout=CapturedBytes(b"", 0),
                stderr=CapturedBytes(b"", 0),
                spawn_error=None,
            ),
            pytest_preflight_process(check),
        ),
        lambda check: (
            pytest_preflight_process(check),
            ExecutedProcess(
                role="primary",
                command=check.command,
                cwd=check.cwd,
                returncode=0,
                duration_ms=1,
                stdout=CapturedBytes(b"", 0),
                stderr=CapturedBytes(b"", 0),
                spawn_error=None,
            ),
            ExecutedProcess(
                role="primary",
                command=check.command,
                cwd=check.cwd,
                returncode=0,
                duration_ms=1,
                stdout=CapturedBytes(b"", 0),
                stderr=CapturedBytes(b"", 0),
                spawn_error=None,
            ),
        ),
    ],
    ids=("empty-supported-observation", "primary-before-preflight", "two-primary-processes"),
)
def test_pytest_execution_bridge_rejects_noncanonical_internal_order(
    tmp_path: Path,
    processes: Callable[[PlannedCheck], tuple[ExecutedProcess, ...]],
) -> None:
    check = pytest_planned_check(tmp_path)
    observation = ExecutedCheck(
        planned=check,
        processes=processes(check),
        pytest=pytest_execution_observation(),
    )

    with pytest.raises(ReportingError, match="pytest execution process order"):
        build_run_report(
            tmp_path,
            run_plan((check,)),
            ExecutionResult((observation,), 2),
        )


@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
@pytest.mark.parametrize(
    ("raw", "expected_text", "truncated", "omitted_bytes"),
    (
        pytest.param(b"", "", False, 0, id="empty"),
        pytest.param(b"a" * 65_535, "a" * 65_535, False, 0, id="one-below-limit"),
        pytest.param(b"a" * 65_536, "a" * 65_536, False, 0, id="at-limit"),
        pytest.param(
            b"x" + b"a" * 65_536,
            "a" * 65_536,
            True,
            1,
            id="one-above-limit",
        ),
        pytest.param(
            b"discarded-prefix" + b"tail" * reporting.CAPTURE_LIMIT_BYTES,
            "tail" * (reporting.CAPTURE_LIMIT_BYTES // len(b"tail")),
            True,
            len(b"discarded-prefix") + 3 * reporting.CAPTURE_LIMIT_BYTES,
            id="large-exact-tail",
        ),
        pytest.param(
            b"\xc3\xa9" + b"a" * 65_535,
            "\ufffd" + "a" * 65_535,
            True,
            1,
            id="invalid-utf8-at-retained-boundary",
        ),
    ),
)
def test_capture_boundaries_apply_independently_to_each_stream(
    tmp_path: Path,
    stream_name: str,
    raw: bytes,
    expected_text: str,
    truncated: bool,
    omitted_bytes: int,
) -> None:
    check = planned_check(tmp_path, "ruff")
    streams = {"stdout": b"other", "stderr": b"other", stream_name: raw}

    report = build_run_report(
        tmp_path,
        run_plan((check,)),
        ExecutionResult(
            (
                executed_check(
                    check,
                    0,
                    stdout=streams["stdout"],
                    stderr=streams["stderr"],
                ),
            ),
            0,
        ),
    )

    captured = getattr(report.checks[0].processes[0], stream_name)
    assert captured == CapturedText(True, expected_text, truncated, omitted_bytes)


@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
@pytest.mark.parametrize(
    ("raw", "expected_text"),
    (
        pytest.param(
            b"plain \x1b[31mred\x1b[0m end",
            "plain red end",
            id="csi-color",
        ),
        pytest.param(
            b"before\x1b]0;title\x07after",
            "beforeafter",
            id="osc-bel",
        ),
        pytest.param(
            b"before\x1b]0;title\x1b\\after",
            "beforeafter",
            id="osc-st",
        ),
        pytest.param(
            b"before\x1b]unfinished diagnostic",
            "before\x1b]unfinished diagnostic",
            id="incomplete-osc",
        ),
        pytest.param(
            b"before\x1b]unfinished \x1b[31mred",
            "before\x1b]unfinished \x1b[31mred",
            id="incomplete-osc-preserves-nested-csi",
        ),
    ),
)
def test_terminal_sequence_sanitization_applies_independently_to_each_stream(
    tmp_path: Path,
    stream_name: str,
    raw: bytes,
    expected_text: str,
) -> None:
    check = planned_check(tmp_path, "ruff")
    streams = {"stdout": b"other", "stderr": b"other", stream_name: raw}

    report = build_run_report(
        tmp_path,
        run_plan((check,)),
        ExecutionResult(
            (
                executed_check(
                    check,
                    0,
                    stdout=streams["stdout"],
                    stderr=streams["stderr"],
                ),
            ),
            0,
        ),
    )

    captured = getattr(report.checks[0].processes[0], stream_name)
    assert captured == CapturedText(True, expected_text, False, 0)


def test_truncated_stream_advisories_are_exact_and_sorted(tmp_path: Path) -> None:
    ty = planned_check(tmp_path, "ty")
    ruff = planned_check(tmp_path, "ruff")

    report = build_run_report(
        tmp_path,
        run_plan((ty, ruff)),
        ExecutionResult(
            (
                executed_check(ty, 0, stdout=b"x" * 65_539, stderr=b"y" * 65_538),
                executed_check(ruff, 0, stdout=b"ok", stderr=b"z" * 65_537),
            ),
            0,
        ),
    )
    assert report.advisories == (
        Advisory(
            code="output_truncated",
            message=(
                "ruff process 1 (primary) stderr omitted 1 byte(s); only the final "
                "65536 bytes are included."
            ),
            hint=None,
        ),
        Advisory(
            code="output_truncated",
            message=(
                "ty process 1 (primary) stderr omitted 2 byte(s); only the final "
                "65536 bytes are included."
            ),
            hint=None,
        ),
        Advisory(
            code="output_truncated",
            message=(
                "ty process 1 (primary) stdout omitted 3 byte(s); only the final "
                "65536 bytes are included."
            ),
            hint=None,
        ),
    )


def test_validation_accepts_a_builder_report(tmp_path: Path) -> None:
    check = planned_check(tmp_path, "ruff")
    report = build_run_report(
        tmp_path,
        run_plan((check,)),
        ExecutionResult((executed_check(check, 0),), 0),
    )

    assert validate_report_v1(report) is None


INVALID_REPORT_CASES = (
    "schema-version",
    "kind-enum",
    "mode-enum",
    "overall-status-enum",
    "selection-check-name-enum",
    "check-name-enum",
    "planned-test-scope-enum",
    "planned-coverage-scope-enum",
    "check-status-enum",
    "process-role-enum",
    "process-outcome-enum",
    "check-error-code-enum",
    "advisory-code-enum",
    "planning-error-code-enum",
    "non-integer-schema-version",
    "boolean-schema-version",
    "negative-duration",
    "non-integer-duration",
    "boolean-duration",
    "negative-omitted-bytes",
    "non-integer-omitted-bytes",
    "boolean-omitted-bytes",
    "relative-project-root",
    "relative-process-cwd",
    "exited-missing-exit-code",
    "exited-negative-exit-code",
    "exited-non-integer-exit-code",
    "exited-boolean-exit-code",
    "exited-unexpected-signal",
    "exited-unexpected-error-message",
    "signaled-unexpected-exit-code",
    "signaled-missing-signal",
    "signaled-zero-signal",
    "signaled-non-integer-signal",
    "signaled-boolean-signal",
    "signaled-missing-error-message",
    "spawn-failed-unexpected-exit-code",
    "spawn-failed-unexpected-signal",
    "spawn-failed-missing-error-message",
    "uncaptured-nonempty-text",
    "uncaptured-truncated",
    "uncaptured-omitted-bytes",
    "captured-untruncated-omitted-bytes",
    "captured-truncated-without-omission",
    "error-check-without-error",
    "passed-check-with-error",
    "failed-check-with-error",
    "complete-error-run",
    "incomplete-passed-run",
    "failed-run-marked-passed",
    "error-run-marked-failed",
    "passed-run-marked-error",
    "planning-overall-status",
    "planning-complete",
    "nonnull-pytest",
    "nonnull-coverage",
    "selection-does-not-match-checks",
    "selection-is-not-canonical",
    "emitted-checks-are-not-canonical",
    "duplicate-selected-check",
    "shortcut-without-pytest",
    "shortcut-with-direct-targets",
    "shortcut-with-second-check",
    "shortcut-with-null-pytest-args",
    "shortcut-with-empty-pytest-args",
    "shortcut-with-invalid-name",
    "shortcut-with-non-tuple-string-pytest-args",
    "shortcut-with-non-partial-scope",
    "selected-pytest-null-arguments",
    "selected-pytest-wrong-arguments",
    "selected-pytest-wrong-scope",
    "unselected-pytest-has-arguments",
    "unselected-pytest-wrong-scope",
    "non-b-planned-coverage-scope",
    "ordinary-check-without-primary",
    "ordinary-check-with-two-primary-processes",
    "ordinary-check-with-c-only-role",
    "positive-exit-claimed-passed",
    "zero-exit-claimed-failed",
    "signal-with-wrong-error-code",
    "spawn-failure-with-wrong-error-code",
    "missing-primary-with-wrong-error-code",
)


@pytest.mark.parametrize("case", INVALID_REPORT_CASES)
def test_rejects_invalid_producer_report(tmp_path: Path, case: str) -> None:
    invalid_report = make_invalid_report(tmp_path, case)

    with pytest.raises(ReportingError, match=r"^invalid report:"):
        validate_report_v1(invalid_report)


@pytest.mark.parametrize("targets", (None, False, "", []))
def test_rejects_shortcut_reports_with_malformed_falsy_targets(
    tmp_path: Path,
    targets: object,
) -> None:
    pytest_check = planned_check(tmp_path, "pytest")
    report = build_run_report(
        tmp_path,
        run_plan(
            (pytest_check,),
            test_shortcut="unit",
            pytest_args=("tests/unit",),
            planned_test_scope="partial",
        ),
        ExecutionResult((executed_check(pytest_check, 0),), 0),
    )

    assert validate_report_v1(report) is None
    selection = replace(report.selection, targets=cast(Any, targets))
    with pytest.raises(ReportingError, match=r"^invalid report:"):
        validate_report_v1(replace(report, selection=selection))


def make_invalid_report(tmp_path: Path, case: str) -> AgentReportV1:
    check = planned_check(tmp_path, "ruff")
    plan = run_plan((check,))
    passed = build_run_report(
        tmp_path,
        plan,
        ExecutionResult(
            (executed_check(check, 0, stdout=b"x" * 65_537),),
            0,
        ),
    )
    failed = build_run_report(
        tmp_path,
        plan,
        ExecutionResult((executed_check(check, 1),), 1),
    )
    signaled = build_run_report(
        tmp_path,
        plan,
        ExecutionResult((executed_check(check, -9),), 2),
    )
    spawn_failed = build_run_report(
        tmp_path,
        plan,
        ExecutionResult(
            (
                executed_check(
                    check,
                    None,
                    stdout=None,
                    stderr=None,
                    spawn_error="FileNotFoundError: uv",
                ),
            ),
            2,
        ),
    )
    planning_error = build_planning_error_report("unknown_check", "Unknown check: mypy")
    process = passed.checks[0].processes[0]
    signal_process = signaled.checks[0].processes[0]
    spawn_process = spawn_failed.checks[0].processes[0]

    def shortcut_report(
        checks: tuple[PlannedCheck, ...],
        *,
        targets: tuple[str, ...] = (),
        test_shortcut: object = "unit",
        pytest_args: object = ("tests/unit",),
        planned_test_scope: object = "partial",
    ) -> RunReportV1:
        report = build_run_report(
            tmp_path,
            run_plan(checks, targets=targets),
            ExecutionResult(tuple(executed_check(check, 0) for check in checks), 0),
        )
        selection = replace(
            report.selection,
            test_shortcut=cast(Any, test_shortcut),
            pytest_args=cast(Any, pytest_args),
            planned_test_scope=cast(Any, planned_test_scope),
        )
        return replace(report, selection=selection)

    if case == "schema-version":
        return replace(passed, schema_version=2)
    if case == "kind-enum":
        return replace(passed, kind=cast(Any, "other"))
    if case == "mode-enum":
        return replace(passed, mode=cast(Any, "other"))
    if case == "overall-status-enum":
        return replace(passed, overall_status=cast(Any, "other"))
    if case == "selection-check-name-enum":
        selection = replace(passed.selection, checks=(cast(Any, "mypy"),))
        return replace(passed, selection=selection)
    if case == "check-name-enum":
        result = replace(passed.checks[0], name=cast(Any, "mypy"))
        return replace(passed, checks=(result,))
    if case == "planned-test-scope-enum":
        selection = replace(passed.selection, planned_test_scope=cast(Any, "other"))
        return replace(passed, selection=selection)
    if case == "planned-coverage-scope-enum":
        selection = replace(passed.selection, planned_coverage_scope=cast(Any, "other"))
        return replace(passed, selection=selection)
    if case == "check-status-enum":
        result = replace(passed.checks[0], status=cast(Any, "other"))
        return replace(passed, checks=(result,))
    if case == "process-role-enum":
        return replace_process(passed, replace(process, role=cast(Any, "other")))
    if case == "process-outcome-enum":
        return replace_process(passed, replace(process, outcome=cast(Any, "other")))
    if case == "check-error-code-enum":
        error = signaled.checks[0].error
        assert error is not None
        result = replace(signaled.checks[0], error=replace(error, code=cast(Any, "other")))
        return replace(signaled, checks=(result,))
    if case == "advisory-code-enum":
        advisory = replace(passed.advisories[0], code=cast(Any, "other"))
        return replace(passed, advisories=(advisory,))
    if case == "planning-error-code-enum":
        error = replace(planning_error.error, code=cast(Any, "other"))
        return replace(planning_error, error=error)
    if case == "non-integer-schema-version":
        return replace(passed, schema_version=cast(Any, 1.0))
    if case == "boolean-schema-version":
        return replace(passed, schema_version=cast(Any, True))
    if case == "negative-duration":
        return replace_process(passed, replace(process, duration_ms=-1))
    if case == "non-integer-duration":
        return replace_process(passed, replace(process, duration_ms=cast(Any, 1.0)))
    if case == "boolean-duration":
        return replace_process(passed, replace(process, duration_ms=cast(Any, True)))
    if case == "negative-omitted-bytes":
        stdout = replace(process.stdout, omitted_bytes=-1)
        return replace_process(passed, replace(process, stdout=stdout))
    if case == "non-integer-omitted-bytes":
        stdout = replace(process.stdout, omitted_bytes=cast(Any, 1.0))
        return replace_process(passed, replace(process, stdout=stdout))
    if case == "boolean-omitted-bytes":
        stdout = replace(process.stdout, omitted_bytes=cast(Any, True))
        return replace_process(passed, replace(process, stdout=stdout))
    if case == "relative-project-root":
        return replace(passed, project_root="relative/project")
    if case == "relative-process-cwd":
        return replace_process(passed, replace(process, cwd="relative/project"))
    if case == "exited-missing-exit-code":
        return replace_process(passed, replace(process, exit_code=None))
    if case == "exited-negative-exit-code":
        return replace_process(passed, replace(process, exit_code=-1))
    if case == "exited-non-integer-exit-code":
        return replace_process(passed, replace(process, exit_code=cast(Any, 1.0)))
    if case == "exited-boolean-exit-code":
        return replace_process(passed, replace(process, exit_code=cast(Any, True)))
    if case == "exited-unexpected-signal":
        return replace_process(passed, replace(process, signal=9))
    if case == "exited-unexpected-error-message":
        return replace_process(passed, replace(process, error_message="unexpected"))
    if case == "signaled-unexpected-exit-code":
        return replace_process(signaled, replace(signal_process, exit_code=1))
    if case == "signaled-missing-signal":
        return replace_process(signaled, replace(signal_process, signal=None))
    if case == "signaled-zero-signal":
        return replace_process(signaled, replace(signal_process, signal=0))
    if case == "signaled-non-integer-signal":
        return replace_process(signaled, replace(signal_process, signal=cast(Any, 9.0)))
    if case == "signaled-boolean-signal":
        return replace_process(signaled, replace(signal_process, signal=cast(Any, True)))
    if case == "signaled-missing-error-message":
        return replace_process(signaled, replace(signal_process, error_message=None))
    if case == "spawn-failed-unexpected-exit-code":
        return replace_process(spawn_failed, replace(spawn_process, exit_code=1))
    if case == "spawn-failed-unexpected-signal":
        return replace_process(spawn_failed, replace(spawn_process, signal=9))
    if case == "spawn-failed-missing-error-message":
        return replace_process(spawn_failed, replace(spawn_process, error_message=None))
    if case == "uncaptured-nonempty-text":
        stderr = CapturedText(False, "unexpected", False, 0)
        return replace_process(passed, replace(process, stderr=stderr))
    if case == "uncaptured-truncated":
        stderr = CapturedText(False, "", True, 1)
        return replace_process(passed, replace(process, stderr=stderr))
    if case == "uncaptured-omitted-bytes":
        stderr = CapturedText(False, "", False, 1)
        return replace_process(passed, replace(process, stderr=stderr))
    if case == "captured-untruncated-omitted-bytes":
        stdout = replace(process.stdout, truncated=False, omitted_bytes=1)
        return replace_process(passed, replace(process, stdout=stdout))
    if case == "captured-truncated-without-omission":
        stdout = replace(process.stdout, truncated=True, omitted_bytes=0)
        return replace_process(passed, replace(process, stdout=stdout))
    if case == "error-check-without-error":
        result = replace(signaled.checks[0], error=None)
        return replace(signaled, checks=(result,))
    if case == "passed-check-with-error":
        result = replace(
            passed.checks[0],
            error=CheckError("missing_primary_process", "unexpected"),
        )
        return replace(passed, checks=(result,))
    if case == "failed-check-with-error":
        result = replace(
            failed.checks[0],
            error=CheckError("missing_primary_process", "unexpected"),
        )
        return replace(failed, checks=(result,))
    if case == "complete-error-run":
        return replace(signaled, complete=True)
    if case == "incomplete-passed-run":
        return replace(passed, complete=False)
    if case == "failed-run-marked-passed":
        return replace(failed, overall_status="passed")
    if case == "error-run-marked-failed":
        return replace(signaled, overall_status="failed")
    if case == "passed-run-marked-error":
        return replace(passed, overall_status="error")
    if case == "planning-overall-status":
        return replace(planning_error, overall_status=cast(Any, "passed"))
    if case == "planning-complete":
        return replace(planning_error, complete=cast(Any, True))
    if case == "nonnull-pytest":
        return replace(passed, pytest=cast(Any, object()))
    if case == "nonnull-coverage":
        return replace(passed, coverage=cast(Any, object()))
    if case == "selection-does-not-match-checks":
        selection = replace(passed.selection, checks=("ty",))
        return replace(passed, selection=selection)
    if case == "selection-is-not-canonical":
        ruff = planned_check(tmp_path, "ruff")
        ty = planned_check(tmp_path, "ty")
        report = build_run_report(
            tmp_path,
            run_plan((ty, ruff)),
            ExecutionResult((executed_check(ty, 0), executed_check(ruff, 0)), 0),
        )
        return report
    if case == "emitted-checks-are-not-canonical":
        ruff = planned_check(tmp_path, "ruff")
        ty = planned_check(tmp_path, "ty")
        report = build_run_report(
            tmp_path,
            run_plan((ruff, ty)),
            ExecutionResult((executed_check(ruff, 0), executed_check(ty, 0)), 0),
        )
        selection = replace(report.selection, checks=("ty", "ruff"))
        return replace(report, selection=selection, checks=tuple(reversed(report.checks)))
    if case == "duplicate-selected-check":
        selection = replace(passed.selection, checks=("ruff", "ruff"))
        result = replace(passed.checks[0], name=cast(Any, "ruff"))
        return replace(passed, selection=selection, checks=(result, result))
    if case == "shortcut-without-pytest":
        return shortcut_report((check,))
    if case == "shortcut-with-direct-targets":
        pytest_check = planned_check(tmp_path, "pytest")
        return shortcut_report((pytest_check,), targets=("tests/unit",))
    if case == "shortcut-with-second-check":
        pytest_check = planned_check(tmp_path, "pytest")
        return shortcut_report((check, pytest_check))
    if case == "shortcut-with-null-pytest-args":
        pytest_check = planned_check(tmp_path, "pytest")
        return shortcut_report((pytest_check,), pytest_args=None)
    if case == "shortcut-with-empty-pytest-args":
        pytest_check = planned_check(tmp_path, "pytest")
        return shortcut_report((pytest_check,), pytest_args=())
    if case == "shortcut-with-invalid-name":
        pytest_check = planned_check(tmp_path, "pytest")
        return shortcut_report((pytest_check,), test_shortcut="Unit")
    if case == "shortcut-with-non-tuple-string-pytest-args":
        pytest_check = planned_check(tmp_path, "pytest")
        return shortcut_report((pytest_check,), pytest_args=("tests/unit", 1))
    if case == "shortcut-with-non-partial-scope":
        pytest_check = planned_check(tmp_path, "pytest")
        return shortcut_report((pytest_check,), planned_test_scope="complete")
    if case == "selected-pytest-null-arguments":
        pytest_check = planned_check(tmp_path, "pytest")
        report = build_run_report(
            tmp_path,
            run_plan((pytest_check,)),
            ExecutionResult((executed_check(pytest_check, 0),), 0),
        )
        return replace(report, selection=replace(report.selection, pytest_args=None))
    if case == "selected-pytest-wrong-arguments":
        pytest_check = planned_check(tmp_path, "pytest")
        report = build_run_report(
            tmp_path,
            run_plan((pytest_check,), targets=("tests/unit",)),
            ExecutionResult((executed_check(pytest_check, 0),), 0),
        )
        return replace(report, selection=replace(report.selection, pytest_args=("other",)))
    if case == "selected-pytest-wrong-scope":
        pytest_check = planned_check(tmp_path, "pytest")
        report = build_run_report(
            tmp_path,
            run_plan((pytest_check,), targets=("tests/unit",)),
            ExecutionResult((executed_check(pytest_check, 0),), 0),
        )
        return replace(report, selection=replace(report.selection, planned_test_scope="complete"))
    if case == "unselected-pytest-has-arguments":
        selection = replace(passed.selection, pytest_args=("tests/unit",))
        return replace(passed, selection=selection)
    if case == "unselected-pytest-wrong-scope":
        selection = replace(passed.selection, planned_test_scope="complete")
        return replace(passed, selection=selection)
    if case == "non-b-planned-coverage-scope":
        selection = replace(passed.selection, planned_coverage_scope="partial")
        return replace(passed, selection=selection)
    if case == "ordinary-check-without-primary":
        result = replace(passed.checks[0], processes=())
        return replace(passed, checks=(result,))
    if case == "ordinary-check-with-two-primary-processes":
        result = replace(passed.checks[0], processes=(process, process))
        return replace(passed, checks=(result,))
    if case == "ordinary-check-with-c-only-role":
        return replace_process(passed, replace(process, role=cast(Any, "pytest_preflight")))
    if case == "positive-exit-claimed-passed":
        failed_process = failed.checks[0].processes[0]
        result = replace(failed.checks[0], status="passed", error=None)
        return replace(
            failed, overall_status="passed", checks=(replace(result, processes=(failed_process,)),)
        )
    if case == "zero-exit-claimed-failed":
        result = replace(passed.checks[0], status="failed", error=None)
        return replace(passed, overall_status="failed", checks=(result,))
    if case == "signal-with-wrong-error-code":
        result = replace(
            signaled.checks[0],
            error=CheckError("spawn_failed", "wrong evidence"),
        )
        return replace(signaled, checks=(result,))
    if case == "spawn-failure-with-wrong-error-code":
        result = replace(
            spawn_failed.checks[0],
            error=CheckError("terminated_by_signal", "wrong evidence"),
        )
        return replace(spawn_failed, checks=(result,))
    if case == "missing-primary-with-wrong-error-code":
        result = CheckResult(
            name="ruff",
            status="error",
            processes=(),
            error=CheckError("spawn_failed", "wrong evidence"),
        )
        return replace(passed, overall_status="error", complete=False, checks=(result,))
    raise AssertionError(f"unhandled invalid report case: {case}")


def replace_process(report: RunReportV1, process: ProcessResult) -> RunReportV1:
    result = replace(report.checks[0], processes=(process,))
    return replace(report, checks=(result,))
