from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest

from pyrepo_check.reporting import (
    ReportingError,
    capture_text,
    render_terminal,
    select_exit_code,
    serialize_json,
    strip_terminal_sequences,
)
from pyrepo_check.reporting import build_planning_error_report, build_run_report
from pyrepo_check.execution import (
    AnalysisPythonAuthorityObservation,
    CapturedBytes,
    CheckExecutionFailure,
    CheckStartObservation,
    DependencyObservation,
    EnvironmentFailureObservation,
    ExecutedProcess,
    PythonObservation,
    RepositoryCheckObservation,
    RepositoryEnvironmentObservation,
    RepositoryExecutionResult,
    ToolEnvironmentObservation,
)
from pyrepo_check.planning import (
    CheckInvocation,
    CoverageExecutionPlan,
    DefaultRepositoryPython,
    RunPlan,
)
from pyrepo_check.planning import PytestExecutionPlan
from pyrepo_check.pytest_execution import PytestArtifactObservation, PytestExecutionObservation
from pyrepo_check.pytest_evidence import (
    CollectionIssue,
    PytestCounts,
    PytestError,
    PytestResult,
    SlowTest,
    SpecialTestOutcome,
)
from pyrepo_check.coverage_evidence import (
    CoverageCounts,
    CoverageFile,
    CoverageThreshold,
    CoverageTotals,
    FileBranchCoverage,
    FileStatementCoverage,
)
from pyrepo_check.coverage_execution import (
    CoverageArtifactObservation,
    CoverageExecutionObservation,
    CoveragePreflightObservation,
    CoveragePreflightRecord,
)
from pyrepo_check.reporting_schema import (
    AgentReportV2,
    EnvironmentError,
    PlanningErrorReportV2,
    PlanningErrorV2,
    ProcessResult,
    CapturedText,
    RunReportV2,
)
from tests.test_reporting_schema_v2 import (
    dependency_failure_report,
    environment_failure_report,
    pytest_dependency_failure_report,
    pytest_no_primary_report,
    pytest_run_report,
    pytest_session_incomplete_report,
    pytest_workspace_failure_report,
    report_with_ty,
    tool_environment_evidence,
    valid_run_report,
)
from tests.test_pytest_evidence import _check as pytest_evidence_check
from tests.test_pytest_evidence import _with_exit as pytest_evidence_with_exit


def _observed_process(process: object) -> ExecutedProcess:
    assert isinstance(process, ProcessResult)
    assert process.outcome == "exited"
    assert process.exit_code is not None
    return ExecutedProcess(
        role=process.role,
        command=process.argv,
        cwd=Path(process.cwd),
        returncode=process.exit_code,
        duration_ms=process.duration_ms,
        stdout=CapturedBytes(process.stdout.text.encode(), process.stdout.omitted_bytes),
        stderr=CapturedBytes(process.stderr.text.encode(), process.stderr.omitted_bytes),
        spawn_error=None,
    )


def _ordinary_success_composition() -> tuple[Path, RunPlan, RepositoryExecutionResult]:
    expected = valid_run_report()
    root = Path(expected.project_root)
    planned = CheckInvocation("ruff", ("check", "src"))
    plan = RunPlan(
        root=root,
        repository_python=DefaultRepositoryPython(),
        mode="focused",
        targets=("src",),
        checks=(planned,),
        output_format="json",
        pytest_args=None,
        planned_test_scope="not_selected",
        planned_coverage_scope="not_requested",
    )
    tool_python = PythonObservation("cpython", (3, 12, 11), Path("/tool/bin/python"))
    repository_python = PythonObservation(
        "cpython", (3, 12, 11), Path("/repo/.venv/bin/python")
    )
    dependency = expected.repository_environment.dependencies[0]
    assert dependency.process is not None
    environment = RepositoryEnvironmentObservation(
        manager_version=expected.repository_environment.manager_version,
        path=Path("/repo/.venv"),
        python_selection=plan.repository_python,
        python=repository_python,
        lock_path=Path(expected.repository_environment.lock.path),
        lock_status=expected.repository_environment.lock.status,
        mutation_protection=expected.repository_environment.mutation_protection,
        dependencies=(
            DependencyObservation(
                name="ruff",
                module=dependency.module,
                required=dependency.required,
                status=dependency.status,
                version=dependency.version,
                origin=dependency.origin,
                process=_observed_process(dependency.process),
                error=None,
            ),
        ),
        processes=tuple(
            _observed_process(process)
            for process in expected.repository_environment.processes
        ),
        error=None,
    )
    check = expected.checks[0]
    assert check.start_evidence is not None
    execution = RepositoryExecutionResult(
        tool_environment=ToolEnvironmentObservation("0.1.0", tool_python),
        repository_environment=environment,
        checks=(
            RepositoryCheckObservation(
                invocation=planned,
                execution_environment="repository",
                analysis_python_authority=AnalysisPythonAuthorityObservation(),
                start=CheckStartObservation(
                    1,
                    "ruff",
                    "ruff",
                    check.start_evidence.arguments_sha256,
                    repository_python,
                ),
                processes=tuple(_observed_process(process) for process in check.processes),
                error=None,
            ),
        ),
    )
    return root, plan, execution


def _pytest_composition(
    pytest_observation: PytestExecutionObservation,
    *,
    primary_exit: int,
    observation_error: CheckExecutionFailure | None = None,
    coverage_observation: CoverageExecutionObservation | None = None,
    environment_error: EnvironmentFailureObservation | None = None,
    include_coverage_json: bool = True,
) -> tuple[Path, RunPlan, RepositoryExecutionResult]:
    coverage_requested = coverage_observation is not None
    expected = pytest_run_report(
        exit_code=primary_exit,
        coverage="available" if coverage_requested else "not_requested",
    )
    root = Path(expected.project_root)
    planned = CheckInvocation(
        "pytest",
        (),
        pytest=PytestExecutionPlan(
            pytest_args=(),
            coverage=(
                CoverageExecutionPlan(Path("/repo/pyproject.toml"), None)
                if coverage_requested
                else None
            ),
        ),
    )
    plan = RunPlan(
        root=root,
        repository_python=DefaultRepositoryPython(),
        mode=expected.mode,
        targets=(),
        checks=(planned,),
        output_format="json",
        pytest_args=(),
        planned_test_scope="complete",
        planned_coverage_scope="complete" if coverage_requested else "not_requested",
    )
    environment = expected.repository_environment
    python = environment.python
    assert python is not None
    repository_python = PythonObservation(
        python.implementation,
        python.version,
        Path(python.executable),
    )
    dependencies = tuple(
        DependencyObservation(
            name=dependency.name,
            module=dependency.module,
            required=dependency.required,
            status=dependency.status,
            version=dependency.version,
            origin=dependency.origin,
            process=(
                None
                if dependency.process is None
                else _observed_process(dependency.process)
            ),
            error=(
                None
                if dependency.error is None
                else CheckExecutionFailure(
                    dependency.error.code,
                    dependency.error.message,
                    dependency.error.hint,
                )
            ),
        )
        for dependency in environment.dependencies
    )
    environment_observation = RepositoryEnvironmentObservation(
        manager_version=environment.manager_version,
        path=None if environment.path is None else Path(environment.path),
        python_selection=plan.repository_python,
        python=repository_python,
        lock_path=Path(environment.lock.path),
        lock_status=environment.lock.status,
        mutation_protection=environment.mutation_protection,
        dependencies=dependencies,
        processes=tuple(_observed_process(process) for process in environment.processes),
        error=environment_error,
    )
    expected_check = expected.checks[0]
    expected_processes = expected_check.processes
    if coverage_requested and not include_coverage_json:
        expected_processes = expected_processes[:-1]
    start = expected_check.start_evidence
    assert start is not None
    tool_python = expected.tool_environment.python
    execution = RepositoryExecutionResult(
        tool_environment=ToolEnvironmentObservation(
            expected.tool_environment.pyrepo_check_version,
            PythonObservation(
                tool_python.implementation,
                tool_python.version,
                Path(tool_python.executable),
            ),
        ),
        repository_environment=environment_observation,
        checks=(
            RepositoryCheckObservation(
                invocation=planned,
                execution_environment="repository",
                analysis_python_authority=None,
                start=CheckStartObservation(
                    start.schema_version,
                    start.check,
                    start.module,
                    start.arguments_sha256,
                    repository_python,
                ),
                processes=tuple(
                    _observed_process(process) for process in expected_processes
                ),
                error=observation_error,
                pytest=pytest_observation,
                coverage=coverage_observation,
            ),
        ),
    )
    return root, plan, execution


def _exact_json(report: AgentReportV2) -> bytes:
    return json.dumps(
        asdict(report),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"


def _incomplete_coverage_observation(
    state: str,
    *,
    json_exit_code: int | None,
) -> CoverageExecutionObservation:
    return CoverageExecutionObservation(
        preflight=CoveragePreflightObservation(
            "supported",
            CoveragePreflightRecord((3, 12, 11), True, "7.15.0"),
            None,
        ),
        artifact=CoverageArtifactObservation(
            cast(Any, state),
            None,
            "Coverage integrity was lost.",
        ),
        json_exit_code=json_exit_code,
    )


def _dependency_error_with_independent_failure() -> RunReportV2:
    report = report_with_ty()
    dependency_failure = dependency_failure_report("missing")
    ty = report.checks[1]
    failed_ty = replace(
        ty,
        status="failed",
        processes=(replace(ty.processes[0], exit_code=1),),
    )
    return replace(
        report,
        overall_status="error",
        complete=False,
        repository_environment=replace(
            report.repository_environment,
            dependencies=(
                dependency_failure.repository_environment.dependencies[0],
                report.repository_environment.dependencies[1],
            ),
        ),
        checks=(dependency_failure.checks[0], failed_ty),
    )


def _strict_aggregate_success() -> RunReportV2:
    report = pytest_run_report(coverage="available")
    assert report.coverage is not None
    coverage = replace(
        report.coverage,
        status="passed",
        gate_eligible=True,
        threshold=CoverageThreshold(
            configured=True,
            value=90.0,
            evaluated=True,
            passed=True,
            skipped_reason=None,
        ),
    )
    return replace(report, mode="strict_aggregate", coverage=coverage)


def _repository_state_changed() -> RunReportV2:
    report = valid_run_report()
    return replace(
        report,
        overall_status="error",
        complete=False,
        repository_environment=replace(
            report.repository_environment,
            error=EnvironmentError(
                "repository_state_changed",
                "Repository state changed after Check execution.",
                "Restore the repository state, then retry.",
            ),
        ),
    )


def test_exact_planning_error_json_uses_schema_v2_field_order() -> None:
    report = PlanningErrorReportV2(
        schema_version=2,
        kind="planning_error",
        overall_status="error",
        complete=False,
        tool_environment=tool_environment_evidence(python=(3, 13, 15)),
        repository_environment=None,
        error=PlanningErrorV2(
            "unsafe_unlocked_execution",
            "--no-frozen is incompatible with repository-safe execution.",
            "Update uv.lock explicitly, then rerun without --no-frozen.",
        ),
    )

    assert serialize_json(report) == (
        b'{"schema_version":2,"kind":"planning_error","overall_status":"error",'
        b'"complete":false,"tool_environment":{"pyrepo_check_version":"0.1.0",'
        b'"python":{"implementation":"cpython","version":[3,13,15],'
        b'"executable":"/tool/bin/python"}},"repository_environment":null,'
        b'"error":{"code":"unsafe_unlocked_execution","message":"--no-frozen is '
        b'incompatible with repository-safe execution.","hint":"Update uv.lock '
        b'explicitly, then rerun without --no-frozen."}}\n'
    )


def test_public_composition_builds_strict_schema_v2_from_execution_evidence() -> None:
    root, plan, execution = _ordinary_success_composition()

    report = build_run_report(root, plan, execution)

    assert report == valid_run_report()


def test_public_planning_error_composition_retains_observed_tool_environment() -> None:
    tool = ToolEnvironmentObservation(
        "0.1.0",
        PythonObservation("cpython", (3, 13, 15), Path("/tool/bin/python")),
    )

    report = build_planning_error_report(
        "invalid_arguments",
        "bad arguments",
        tool_environment=tool,
        hint="retry with valid arguments",
    )

    assert report.tool_environment == tool_environment_evidence(python=(3, 13, 15))
    assert report.repository_environment is None
    assert select_exit_code(report) == 2


@pytest.mark.parametrize(
    ("report", "sha256"),
    (
        (
            environment_failure_report(),
            "3c74d76a595d5f6d0b7c78433b0a3d98973d9a5f78d8a2c9c08b3d392faf3b30",
        ),
        (
            _dependency_error_with_independent_failure(),
            "19049c098fee29a5ebb77c8b2a1a150b621e081a2c19db5569a550d8d89c1fc8",
        ),
        (
            report_with_ty(),
            "a7555492c528f0d42fe10dc6104670d1f00dd5b6a15cb916cf4fc8c103c1d773",
        ),
        (
            _strict_aggregate_success(),
            "350118a3e11a7f403097cdcf9d13a65c7fc5ef863560e7f65900608e18a91f1e",
        ),
        (
            pytest_run_report(coverage="missing"),
            "52af495c3afefe79317644acd0eb8062491c7ffe04eb65769f9bcf6d4c77300a",
        ),
        (
            _repository_state_changed(),
            "823a8d706382e28869f7f49b0845a043e75738787b84abcd7572360655dbb953",
        ),
    ),
    ids=(
        "missing-lock",
        "dependency-error-plus-independent-failure",
        "focused-ty",
        "strict-aggregate-success",
        "pytest-without-coverage-dependency",
        "repository-state-changed",
    ),
)
def test_exact_schema_v2_json_is_one_utf8_document_in_dataclass_order(
    report: AgentReportV2,
    sha256: str,
) -> None:
    payload = serialize_json(report)

    assert payload == _exact_json(report)
    assert hashlib.sha256(payload).hexdigest() == sha256
    assert payload.endswith(b"\n")
    assert not payload.endswith(b"\n\n")
    assert json.loads(payload)["schema_version"] == 2


def test_exact_terminal_environment_line_precedes_summary() -> None:
    report = report_with_ty()

    assert render_terminal(report).splitlines()[:3] == [
        "==> environment: tool Python 3.12.11 -> repository Python 3.12.11 (uv, locked)",
        "",
        "==> pyrepo-check summary: passed (focused)",
    ]


def test_terminal_dependency_error_names_check_range_install_and_remediation() -> None:
    output = render_terminal(dependency_failure_report("missing"))

    assert "error: ruff: dependency ruff requires >=0.15,<1; installed: not installed." in output
    assert "remediation:" in output


def test_select_exit_code_is_stable() -> None:
    passed = valid_run_report()
    failed_process = replace(passed.checks[0].processes[0], exit_code=1)
    failed_check = replace(passed.checks[0], status="failed", processes=(failed_process,))
    failed = replace(passed, checks=(failed_check,), overall_status="failed")

    assert select_exit_code(passed) == 0
    assert select_exit_code(failed) == 1
    assert select_exit_code(environment_failure_report()) == 2


def test_public_projections_validate_before_emitting() -> None:
    malformed = replace(valid_run_report(), schema_version=1)

    with pytest.raises(ReportingError):
        serialize_json(malformed)
    with pytest.raises(ReportingError):
        render_terminal(malformed)
    with pytest.raises(ReportingError):
        select_exit_code(malformed)


@pytest.mark.parametrize(
    "report",
    (
        valid_run_report(),
        report_with_ty(),
        environment_failure_report(),
        environment_failure_report(coverage_requested=True),
        dependency_failure_report("missing"),
        dependency_failure_report("incompatible"),
        dependency_failure_report("shadowed"),
        dependency_failure_report("unusable"),
        pytest_dependency_failure_report("missing"),
        pytest_dependency_failure_report("incompatible"),
        pytest_dependency_failure_report("shadowed"),
        pytest_dependency_failure_report("unusable"),
        pytest_run_report(exit_code=0),
        pytest_run_report(exit_code=1),
        pytest_run_report(exit_code=5),
        pytest_run_report(coverage="available"),
        pytest_run_report(coverage="missing"),
        pytest_run_report(coverage="helper_failure"),
        pytest_run_report(exit_code=2, coverage="primary_failure"),
        pytest_run_report(exit_code=2, coverage="reserved_evidence"),
        pytest_run_report(cleanup_failure=True),
        pytest_no_primary_report(stage="marker_preparation"),
        pytest_no_primary_report(stage="reporter_staging"),
        pytest_session_incomplete_report(instrumented=False),
        pytest_session_incomplete_report(instrumented=True),
        pytest_workspace_failure_report(),
        _dependency_error_with_independent_failure(),
        _strict_aggregate_success(),
        _repository_state_changed(),
    ),
)
def test_public_projections_cover_every_schema_v2_producer_family(
    report: RunReportV2,
) -> None:
    terminal = render_terminal(report)
    document = serialize_json(report)

    assert terminal.endswith("\n")
    assert document.endswith(b"\n")
    assert select_exit_code(report) in {0, 1, 2}


def test_terminal_renders_bounded_coverage_table_and_gap_count() -> None:
    report = pytest_run_report(coverage="available")
    assert report.coverage is not None
    files = tuple(
        CoverageFile(
            path=(
                f"src/pyrepo_check/a_very_long_module_name_that_needs_compaction_{index}.py"
            ),
            statements=FileStatementCoverage(90, 10, tuple(range(1, 11))),
            branches=FileBranchCoverage(18, 2, ((1, 2), (3, 4))),
        )
        for index in range(5)
    )
    coverage = replace(
        report.coverage,
        totals=CoverageTotals(CoverageCounts(450, 50), CoverageCounts(90, 10)),
        files=files,
    )

    output = render_terminal(replace(report, coverage=coverage))

    assert "coverage: guidance (complete); no minimum configured" in output
    assert "... 2 more files with gaps" in output
    assert "coverage details: use --format json for exact missing lines and branches" in output
    assert "...y_long_module_name_that_needs_compaction_0.py" in output
    assert "TOTAL" in output
    assert "90.00%" in output


def test_terminal_renders_each_coverage_helper_stream_line() -> None:
    report = pytest_run_report(coverage="helper_failure")
    check = report.checks[0]
    primary, helper = check.processes
    helper = replace(
        helper,
        stdout=CapturedText(True, "first\nsecond\n", False, 0),
        stderr=CapturedText(True, "warning", False, 0),
    )
    report = replace(report, checks=(replace(check, processes=(primary, helper)),))

    output = render_terminal(report)

    assert "diagnostic: pytest coverage_json stdout: first" in output
    assert "diagnostic: pytest coverage_json stdout: second" in output
    assert "diagnostic: pytest coverage_json stderr: warning" in output


def test_capture_text_bounds_decodes_and_strips_terminal_sequences() -> None:
    raw = b"prefix\x1b[31mred\x1b[0m\xff"

    captured = capture_text(CapturedBytes(raw, 7))

    assert captured == CapturedText(True, "prefixred\ufffd", True, 7)
    assert strip_terminal_sequences("\x1b[1mplain\x1b[0m") == "plain"
    assert capture_text(b"x" * (65_536 + 3)) == CapturedText(
        True,
        "x" * 65_536,
        True,
        3,
    )


@pytest.mark.parametrize(
    ("returncode", "failure", "expected_outcome", "expected_status"),
    (
        (1, None, "exited", "failed"),
        (
            -15,
            CheckExecutionFailure(
                "terminated_by_signal",
                "Check process terminated by signal 15.",
                None,
            ),
            "signaled",
            "error",
        ),
        (
            None,
            CheckExecutionFailure(
                "spawn_failed",
                "Check process could not be spawned: executable missing",
                None,
            ),
            "spawn_failed",
            "error",
        ),
    ),
)
def test_public_composition_projects_ordinary_process_outcomes(
    returncode: int | None,
    failure: CheckExecutionFailure | None,
    expected_outcome: str,
    expected_status: str,
) -> None:
    root, plan, execution = _ordinary_success_composition()
    observed = execution.checks[0]
    primary = observed.processes[0]
    spawn_error = "executable missing" if returncode is None else None
    observed = replace(
        observed,
        start=None if returncode is None else observed.start,
        execution_environment=None if returncode is None else observed.execution_environment,
        analysis_python_authority=(
            observed.analysis_python_authority if returncode in {0, 1} else None
        ),
        processes=(replace(primary, returncode=returncode, spawn_error=spawn_error),),
        error=failure,
    )

    report = build_run_report(root, plan, replace(execution, checks=(observed,)))

    assert report.checks[0].processes[0].outcome == expected_outcome
    assert report.checks[0].status == expected_status
    assert report.overall_status == ("failed" if expected_status == "failed" else "error")


def test_public_composition_projects_terminal_streams_without_reobservation() -> None:
    root, plan, execution = _ordinary_success_composition()
    plan = replace(plan, output_format="terminal")
    check = execution.checks[0]
    primary = replace(check.processes[0], stdout=None, stderr=None)

    report = build_run_report(
        root,
        plan,
        replace(execution, checks=(replace(check, processes=(primary,)),)),
    )

    assert report.checks[0].processes[0].stdout == CapturedText(False, "", False, 0)
    assert report.checks[0].processes[0].stderr == CapturedText(False, "", False, 0)


def test_public_composition_projects_exact_truncation_advisory_once() -> None:
    root, plan, execution = _ordinary_success_composition()
    check = execution.checks[0]
    primary = replace(
        check.processes[0],
        stdout=CapturedBytes(b"tail", 9),
        stderr=CapturedBytes(b"tail", 9),
    )

    report = build_run_report(
        root,
        plan,
        replace(execution, checks=(replace(check, processes=(primary,)),)),
    )

    assert [advisory.code for advisory in report.advisories] == [
        "output_truncated",
        "output_truncated",
    ]
    messages = {advisory.message for advisory in report.advisories}
    assert any("stdout omitted 9 byte(s)" in message for message in messages)
    assert any("stderr omitted 9 byte(s)" in message for message in messages)


def _pytest_evidence_error_cases() -> tuple[
    tuple[str, PytestExecutionObservation, int],
    ...,
]:
    observed = pytest_evidence_check()
    pytest_observation = observed.pytest
    assert pytest_observation is not None
    return (
        (
            "invalid",
            replace(
                pytest_observation,
                artifact=PytestArtifactObservation(
                    "snapshot",
                    b"{",
                    pytest_observation.artifact.writer_ids,
                    None,
                ),
            ),
            0,
        ),
        (
            "missing",
            replace(
                pytest_observation,
                artifact=PytestArtifactObservation("missing", None, (), None),
            ),
            0,
        ),
        ("exit-mismatch", pytest_observation, 1),
    )


@pytest.mark.parametrize(
    ("_case", "pytest_observation", "primary_exit"),
    _pytest_evidence_error_cases(),
)
def test_public_composition_projects_nested_pytest_evidence_errors_to_the_check(
    _case: str,
    pytest_observation: PytestExecutionObservation,
    primary_exit: int,
) -> None:
    root, plan, execution = _pytest_composition(
        pytest_observation,
        primary_exit=primary_exit,
    )

    report = build_run_report(root, plan, execution)

    nested_error = report.pytest.error if report.pytest is not None else None
    assert nested_error is not None
    assert report.checks[0].status == "error"
    assert report.checks[0].error is not None
    assert report.checks[0].error.code == "pytest_evidence_error"
    assert report.checks[0].error.message == nested_error.message
    assert report.checks[0].error.hint is None


def test_public_composition_keeps_session_incomplete_as_a_failed_check() -> None:
    observed = pytest_evidence_with_exit(
        pytest_evidence_check(),
        1,
        stopped_early=True,
    )
    pytest_observation = observed.pytest
    assert pytest_observation is not None
    root, plan, execution = _pytest_composition(
        pytest_observation,
        primary_exit=1,
    )

    report = build_run_report(root, plan, execution)

    assert report.pytest is not None
    assert report.pytest.error is not None
    assert report.pytest.error.code == "session_incomplete"
    assert report.checks[0].status == "failed"
    assert report.checks[0].error is None


def test_public_composition_serializes_post_helper_coverage_integrity_loss() -> None:
    observed = pytest_evidence_check()
    pytest_observation = observed.pytest
    assert pytest_observation is not None
    root, plan, execution = _pytest_composition(
        pytest_observation,
        primary_exit=0,
        coverage_observation=_incomplete_coverage_observation(
            "unexpected_parallel_data",
            json_exit_code=0,
        ),
    )

    payload = json.loads(serialize_json(build_run_report(root, plan, execution)))

    assert payload["coverage"]["error"]["code"] == "unexpected_parallel_data"
    assert payload["checks"][0]["processes"][-1]["exit_code"] == 0


def test_public_composition_serializes_helper_identity_loss_after_pytest() -> None:
    observed = pytest_evidence_check()
    pytest_observation = observed.pytest
    assert pytest_observation is not None
    root, plan, execution = _pytest_composition(
        pytest_observation,
        primary_exit=0,
        coverage_observation=_incomplete_coverage_observation(
            "generation_failed",
            json_exit_code=None,
        ),
        environment_error=EnvironmentFailureObservation(
            "unsafe_repository_environment",
            "A pinned controller helper changed during execution.",
            "Restore the controller uv and Git installations, then retry.",
        ),
        include_coverage_json=False,
    )

    payload = json.loads(serialize_json(build_run_report(root, plan, execution)))

    assert payload["repository_environment"]["error"]["code"] == (
        "unsafe_repository_environment"
    )
    assert [process["role"] for process in payload["checks"][0]["processes"]] == [
        "primary"
    ]
    assert payload["coverage"]["error"]["code"] == "generation_failed"


def test_authoritative_pytest_observation_error_precedes_nested_evidence_error() -> None:
    observed = pytest_evidence_check()
    pytest_observation = observed.pytest
    assert pytest_observation is not None
    invalid = replace(
        pytest_observation,
        artifact=PytestArtifactObservation(
            "snapshot",
            b"{",
            pytest_observation.artifact.writer_ids,
            None,
        ),
    )
    authoritative = CheckExecutionFailure(
        "cleanup_failed",
        "pytest workspace cleanup failed",
        "Inspect the run workspace.",
    )
    root, plan, execution = _pytest_composition(
        invalid,
        primary_exit=0,
        observation_error=authoritative,
    )

    report = build_run_report(root, plan, execution)

    assert report.checks[0].error is not None
    assert report.checks[0].error.code == "cleanup_failed"
    assert report.checks[0].error.message == authoritative.message
    assert report.checks[0].error.hint == authoritative.hint


def test_public_composition_rejects_missing_mismatched_and_extra_observations() -> None:
    root, plan, execution = _ordinary_success_composition()
    extra_invocation = CheckInvocation("ty", ("check",))
    extra = replace(execution.checks[0], invocation=extra_invocation)

    with pytest.raises(ReportingError, match="every planned Check requires"):
        build_run_report(root, plan, replace(execution, checks=()))
    with pytest.raises(ReportingError, match="unexpected, mismatched, or out-of-order"):
        build_run_report(root, plan, replace(execution, checks=(extra,)))


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("before\x1b]0;title\x07after", "beforeafter"),
        ("before\x1b]0;title\x1b\\after", "beforeafter"),
        ("before\x1b]unfinished\x1b[31m", "before\x1b]unfinished\x1b[31m"),
    ),
)
def test_terminal_sequence_sanitization_handles_osc_terminators_and_incomplete_tail(
    text: str,
    expected: str,
) -> None:
    assert strip_terminal_sequences(text) == expected


def _pytest_semantic_mutations() -> tuple[object, ...]:
    result = cast(object, pytest_run_report().pytest)
    assert isinstance(result, PytestResult)
    evidence = result.evidence
    assert evidence is not None
    one_pass = PytestCounts(1, 0, 0, 0, 0, 0)
    two_pass = PytestCounts(2, 0, 0, 0, 0, 0)
    slow_a = SlowTest("tests/test_a.py::test_a", 1)
    slow_b = SlowTest("tests/test_b.py::test_b", 2)
    skip_a = SpecialTestOutcome(
        "tests/test_a.py::test_a", "skipped", "reason", None, False, 1
    )
    skip_b = SpecialTestOutcome(
        "tests/test_b.py::test_b", "skipped", "reason", None, False, 2
    )
    issue_a = CollectionIssue("tests/test_a.py", "failed")
    issue_b = CollectionIssue("tests/test_b.py", "failed")
    return (
        replace(result, status=cast(Any, "unknown")),
        replace(result, complete=cast(Any, 1)),
        replace(result, scope=cast(Any, "unknown")),
        replace(result, scope_reasons=(cast(Any, "unknown"),)),
        replace(result, scope_reasons=("planned_selector", "planned_selector")),
        replace(result, scope="partial", scope_reasons=()),
        replace(result, pytest_version=cast(Any, 1)),
        replace(result, exit_code=-1),
        replace(
            result,
            status="error",
            complete=False,
            scope="partial",
            scope_reasons=("incomplete_session",),
            evidence=None,
            error=PytestError(cast(Any, "unknown"), "bad code"),
        ),
        replace(
            result,
            status="error",
            complete=False,
            scope="partial",
            scope_reasons=("incomplete_session",),
            evidence=None,
            error=PytestError("preflight_invalid", cast(Any, 1)),
        ),
        replace(result, error=PytestError("preflight_invalid", "unexpected error")),
        replace(result, evidence=None),
        replace(
            result,
            complete=False,
            scope="partial",
            scope_reasons=("incomplete_session",),
        ),
        replace(
            result,
            scope="partial",
            scope_reasons=("planned_selector",),
        ),
        replace(
            result,
            evidence=replace(evidence, deselected=1),
        ),
        replace(result, status="error", error=None),
        replace(result, complete=False),
        replace(result, evidence=replace(evidence, collected=-1)),
        replace(result, evidence=replace(evidence, effective_args=cast(Any, ["tests"]))),
        replace(
            result,
            evidence=replace(evidence, collected=1, counts=PytestCounts(-1, 0, 0, 0, 0, 0)),
        ),
        replace(result, evidence=replace(evidence, collection_errors=cast(Any, [issue_a]))),
        replace(result, evidence=replace(evidence, slowest=cast(Any, [slow_a]))),
        replace(
            result,
            evidence=replace(
                evidence,
                collected=1,
                counts=one_pass,
                slowest=(SlowTest(cast(Any, 1), 1),),
            ),
        ),
        replace(
            result,
            evidence=replace(
                evidence,
                collected=1,
                counts=one_pass,
                slowest=(SlowTest("tests/test_a.py::test_a", -1),),
            ),
        ),
        replace(
            result,
            evidence=replace(
                evidence,
                collected=2,
                counts=two_pass,
                slowest=(slow_a, slow_a),
            ),
        ),
        replace(
            result,
            evidence=replace(
                evidence,
                collected=2,
                counts=two_pass,
                slowest=(slow_a, slow_b),
            ),
        ),
        replace(
            result,
            evidence=replace(
                evidence,
                collected=1,
                counts=PytestCounts(0, 0, 0, 1, 0, 0),
                slowest=(slow_a,),
                special_outcomes=(replace(skip_a, outcome=cast(Any, "passed")),),
            ),
        ),
        replace(
            result,
            evidence=replace(
                evidence,
                collected=1,
                counts=PytestCounts(0, 0, 0, 1, 0, 0),
                slowest=(slow_a,),
                special_outcomes=(replace(skip_a, reason=cast(Any, 1)),),
            ),
        ),
        replace(
            result,
            evidence=replace(
                evidence,
                collected=1,
                counts=PytestCounts(0, 0, 0, 1, 0, 0),
                slowest=(slow_a,),
                special_outcomes=(replace(skip_a, strict=True),),
            ),
        ),
        replace(
            result,
            evidence=replace(
                evidence,
                collected=2,
                counts=PytestCounts(0, 0, 0, 2, 0, 0),
                slowest=(slow_b, slow_a),
                special_outcomes=(skip_a, skip_a),
            ),
        ),
        replace(
            result,
            evidence=replace(
                evidence,
                collected=2,
                counts=PytestCounts(0, 0, 0, 2, 0, 0),
                slowest=(slow_b, slow_a),
                special_outcomes=(skip_b, skip_a),
            ),
        ),
        replace(
            result,
            evidence=replace(evidence, collected=0, counts=one_pass, slowest=(slow_a,)),
        ),
        replace(
            result,
            evidence=replace(evidence, collected=1, counts=one_pass, slowest=()),
        ),
        replace(
            result,
            evidence=replace(
                evidence,
                collected=1,
                counts=PytestCounts(0, 0, 0, 1, 0, 0),
                slowest=(replace(slow_a, duration_ms=2),),
                special_outcomes=(skip_a,),
            ),
        ),
        replace(
            result,
            evidence=replace(evidence, collection_errors=(issue_a, issue_a)),
        ),
        replace(
            result,
            evidence=replace(evidence, collection_errors=(issue_b, issue_a)),
        ),
        replace(
            result,
            evidence=replace(
                evidence,
                collected=1,
                counts=PytestCounts(0, 0, 0, 1, 0, 0),
                slowest=(slow_a,),
                special_outcomes=(replace(skip_a, affects_exit=cast(Any, 1)),),
            ),
        ),
        replace(
            result,
            evidence=replace(
                evidence,
                collected=1,
                counts=PytestCounts(0, 0, 0, 0, 0, 1),
                slowest=(slow_a,),
                special_outcomes=(
                    SpecialTestOutcome(
                        slow_a.nodeid,
                        "xpassed",
                        None,
                        None,
                        False,
                        slow_a.duration_ms,
                    ),
                ),
            ),
        ),
        replace(
            result,
            evidence=replace(
                evidence,
                collected=1,
                counts=PytestCounts(0, 0, 0, 1, 0, 0),
                slowest=(slow_a,),
                special_outcomes=(replace(skip_a, duration_ms=-1),),
            ),
        ),
        replace(
            result,
            evidence=replace(
                evidence,
                collected=1,
                counts=PytestCounts(0, 0, 0, 1, 0, 0),
                slowest=(slow_a,),
                special_outcomes=(),
            ),
        ),
        replace(
            result,
            evidence=replace(evidence, collection_errors=(issue_a,)),
        ),
        replace(result, evidence=replace(evidence, collected=1)),
        replace(
            result,
            evidence=replace(
                evidence,
                collection_errors=(CollectionIssue(cast(Any, 1), "failed"),),
            ),
        ),
    )


@pytest.mark.parametrize("mutation", _pytest_semantic_mutations())
def test_strict_schema_v2_rejects_pytest_semantic_single_contradictions(
    mutation: object,
) -> None:
    report = pytest_run_report()

    with pytest.raises(ReportingError, match="^invalid report:"):
        render_terminal(replace(report, pytest=cast(Any, mutation)))
