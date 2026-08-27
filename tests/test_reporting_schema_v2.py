from __future__ import annotations

from dataclasses import asdict, fields, make_dataclass, replace
from typing import Any, TypeVar, cast

import pytest

from pyrepo_check.coverage_evidence import (
    CoverageCounts,
    CoverageError,
    CoverageResult,
    CoverageThreshold,
    CoverageTotals,
)
from pyrepo_check.pytest_evidence import (
    PytestCounts,
    PytestError,
    PytestEvidence,
    PytestResult,
)
from pyrepo_check.reporting import ReportingError, validate_report_v2
from pyrepo_check.reporting_schema import (
    Advisory,
    AnalysisPythonAuthorityEvidence,
    CapturedText,
    CheckErrorV2,
    CheckResultV2,
    CheckStartEvidence,
    DependencyEvidence,
    EnvironmentError,
    LockEvidence,
    PlanningErrorReportV2,
    PlanningErrorV2,
    ProcessResult,
    PythonEvidence,
    RepositoryEnvironmentEvidence,
    RepositoryPythonSelectionEvidence,
    RunReportV2,
    Selection,
    ToolEnvironmentEvidence,
    validate_report_structure_v2,
)


def tool_environment_evidence(
    *, python: tuple[int, int, int] = (3, 12, 11)
) -> ToolEnvironmentEvidence:
    return ToolEnvironmentEvidence(
        pyrepo_check_version="0.1.0",
        python=PythonEvidence(
            implementation="cpython",
            version=python,
            executable="/tool/bin/python",
        ),
    )


def captured_text() -> CapturedText:
    return CapturedText(
        captured=True,
        text="",
        truncated=False,
        omitted_bytes=0,
    )


def pytest_error_result() -> PytestResult:
    return PytestResult(
        status="error",
        complete=False,
        scope="partial",
        scope_reasons=("incomplete_session",),
        pytest_version=None,
        exit_code=None,
        evidence=None,
        error=PytestError("preflight_invalid", "pytest did not run"),
    )


def coverage_error_result() -> CoverageResult:
    return CoverageResult(
        status="error",
        scope="partial",
        evidence_complete=False,
        coverage_version=None,
        gate_eligible=False,
        threshold=CoverageThreshold(
            configured=False,
            value=None,
            evaluated=False,
            passed=None,
            skipped_reason="evidence_error",
        ),
        totals=None,
        files=(),
        error=CoverageError("preflight_invalid", "coverage did not run"),
    )


def complete_pytest_result(*, exit_code: int = 0) -> PytestResult:
    return PytestResult(
        status="passed" if exit_code == 0 else "failed",
        complete=True,
        scope="complete",
        scope_reasons=(),
        pytest_version="8.4.2",
        exit_code=exit_code,
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
        error=None,
    )


def complete_coverage_result() -> CoverageResult:
    return CoverageResult(
        status="guidance",
        scope="complete",
        evidence_complete=True,
        coverage_version="7.15.0",
        gate_eligible=False,
        threshold=CoverageThreshold(
            configured=False,
            value=None,
            evaluated=False,
            passed=None,
            skipped_reason="not_configured",
        ),
        totals=CoverageTotals(
            statements=CoverageCounts(0, 0),
            branches=CoverageCounts(0, 0),
        ),
        files=(),
        error=None,
    )


def pytest_run_report(
    *,
    exit_code: int = 0,
    coverage: str = "not_requested",
    cleanup_failure: bool = False,
) -> RunReportV2:
    report = valid_run_report()
    environment = report.repository_environment
    pytest_dependency = replace(
        environment.dependencies[0],
        name="pytest",
        module="pytest",
        required=">=8,<9",
        version="8.4.2",
        origin="/repo/.venv/lib/python3.12/site-packages/pytest/__init__.py",
    )
    coverage_dependency: DependencyEvidence | None = None
    coverage_result: CoverageResult | None = None
    module = "pytest"
    processes: tuple[ProcessResult, ...]
    if coverage in {"available", "helper_failure", "primary_failure"}:
        module = "coverage"
        coverage_dependency = replace(
            pytest_dependency,
            name="coverage",
            module="coverage",
            required=">=7.15,<8",
            version="7.15.0",
            origin="/repo/.venv/lib/python3.12/site-packages/coverage/__init__.py",
        )
        if coverage == "helper_failure":
            coverage_result = replace(
                coverage_error_result(),
                coverage_version="7.15.0",
                error=CoverageError("generation_failed", "coverage json failed"),
            )
        elif coverage == "primary_failure":
            coverage_result = replace(
                coverage_error_result(),
                coverage_version="7.15.0",
                error=CoverageError("data_missing", "pytest did not complete"),
            )
        else:
            coverage_result = complete_coverage_result()
    elif coverage == "missing":
        coverage_dependency = replace(
            pytest_dependency,
            name="coverage",
            module="coverage",
            required=">=7.15,<8",
            status="missing",
            version=None,
            origin=None,
            error=CheckErrorV2(
                "check_dependency_missing",
                "coverage is missing",
                "Install coverage.",
            ),
        )
        coverage_result = coverage_error_result()
        coverage_result = replace(
            coverage_result,
            error=CoverageError("module_unavailable", "coverage is missing"),
        )
    primary = process_result(
        "primary",
        argv=(
            "uv",
            "run",
            "--locked",
            "python",
            "/repo/.pyrepo-check/check-launcher.py",
            "--evidence",
            "/repo/.pyrepo-check/start.json",
            "--check",
            "pytest",
            "--module",
            module,
            "--",
        ),
        exit_code=exit_code,
    )
    processes = (primary,)
    if coverage in {"available", "helper_failure"}:
        processes = (
            *processes,
            process_result(
                "coverage_json",
                exit_code=3 if coverage == "helper_failure" else 0,
            ),
        )
    check_error = (
        CheckErrorV2("cleanup_failed", "workspace cleanup failed", "Inspect workspace.")
        if cleanup_failure
        else CheckErrorV2(
            "check_execution_failed",
            "pytest launcher returned a reserved exit.",
            None,
        )
        if coverage == "primary_failure"
        else None
    )
    check = CheckResultV2(
        name="pytest",
        status=(
            "error"
            if cleanup_failure or coverage == "primary_failure"
            else "passed"
            if exit_code == 0
            else "failed"
        ),
        execution_environment="repository",
        analysis_python_authority=None,
        start_evidence=CheckStartEvidence(
            schema_version=1,
            check="pytest",
            module=cast(Any, module),
            arguments_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            python=repository_python(),
        ),
        processes=processes,
        error=check_error,
    )
    dependencies = (pytest_dependency,)
    if coverage_dependency is not None:
        dependencies = (*dependencies, coverage_dependency)
    complete = not cleanup_failure and coverage not in {
        "missing",
        "helper_failure",
        "primary_failure",
    }
    overall_status = (
        "error"
        if not complete
        else "failed"
        if exit_code != 0
        else "passed"
    )
    return replace(
        report,
        overall_status=overall_status,
        complete=complete,
        selection=Selection(
            checks=("pytest",),
            targets=(),
            test_shortcut=None,
            pytest_args=(),
            planned_test_scope="complete",
            planned_coverage_scope=(
                "complete"
                if coverage
                in {"available", "missing", "helper_failure", "primary_failure"}
                else "not_requested"
            ),
        ),
        repository_environment=replace(environment, dependencies=dependencies),
        checks=(check,),
        pytest=(
            PytestResult(
                status="error",
                complete=False,
                scope="partial",
                scope_reasons=("incomplete_session",),
                pytest_version="8.4.2",
                exit_code=exit_code,
                evidence=complete_pytest_result().evidence,
                error=PytestError("interrupted", "pytest was interrupted"),
            )
            if coverage == "primary_failure"
            else complete_pytest_result(exit_code=exit_code)
        ),
        coverage=coverage_result,
    )


def pytest_workspace_failure_report() -> RunReportV2:
    report = pytest_run_report(coverage="available")
    check = report.checks[0]
    return replace(
        report,
        overall_status="error",
        complete=False,
        checks=(
            replace(
                check,
                status="error",
                execution_environment=None,
                start_evidence=None,
                processes=(),
                error=CheckErrorV2(
                    "cleanup_failed",
                    "pytest workspace setup failed",
                    None,
                ),
            ),
        ),
        pytest=PytestResult(
            status="error",
            complete=False,
            scope="partial",
            scope_reasons=("incomplete_session",),
            pytest_version=None,
            exit_code=None,
            evidence=None,
            error=PytestError("not_started", "pytest workspace setup failed"),
        ),
        coverage=coverage_error_result(),
    )


def environment_failure_report(*, coverage_requested: bool = False) -> RunReportV2:
    dependencies = [
        DependencyEvidence(
            name="pytest",
            module="pytest",
            required=">=8,<9",
            status="unobserved",
            version=None,
            origin=None,
            process=None,
            error=None,
        )
    ]
    if coverage_requested:
        dependencies.append(
            DependencyEvidence(
                name="coverage",
                module="coverage",
                required=">=7.15,<8",
                status="unobserved",
                version=None,
                origin=None,
                process=None,
                error=None,
            )
        )
    return RunReportV2(
        schema_version=2,
        kind="run",
        project_root="/repo",
        mode="focused",
        overall_status="error",
        complete=False,
        tool_environment=tool_environment_evidence(),
        repository_environment=RepositoryEnvironmentEvidence(
            manager="uv",
            manager_version=None,
            path=None,
            python_selection=RepositoryPythonSelectionEvidence(kind="default", request=None),
            python=None,
            lock=LockEvidence(path="/repo/uv.lock", status="missing"),
            dependency_selection="default",
            mutation_protection="unobserved",
            dependencies=tuple(dependencies),
            processes=(),
            error=EnvironmentError(
                code="repository_lock_missing",
                message="Repository lock is missing.",
                hint="Create uv.lock, then retry.",
            ),
        ),
        selection=Selection(
            checks=("pytest",),
            targets=(),
            test_shortcut=None,
            pytest_args=(),
            planned_test_scope="complete",
            planned_coverage_scope="complete" if coverage_requested else "not_requested",
        ),
        checks=(
            CheckResultV2(
                name="pytest",
                status="error",
                execution_environment=None,
                analysis_python_authority=None,
                start_evidence=None,
                processes=(),
                error=CheckErrorV2(
                    code="repository_environment_unavailable",
                    message="pytest did not run because the environment is unavailable.",
                    hint="Resolve the environment error, then retry.",
                ),
            ),
        ),
        pytest=pytest_error_result(),
        coverage=coverage_error_result() if coverage_requested else None,
        advisories=(),
    )


def report_with_ty() -> RunReportV2:
    report = valid_run_report()
    environment = report.repository_environment
    ruff = report.checks[0]
    start = ruff.start_evidence
    assert start is not None
    ty = replace(
        ruff,
        name="ty",
        start_evidence=replace(start, check="ty", module="ty"),
        processes=(
            replace(
                ruff.processes[0],
                argv=(
                    "uv",
                    "run",
                    "--locked",
                    "python",
                    "/repo/.pyrepo-check/check-launcher.py",
                    "--evidence",
                    "/repo/.pyrepo-check/start.json",
                    "--check",
                    "ty",
                    "--module",
                    "ty",
                    "--",
                    "check",
                    "src",
                ),
            ),
        ),
    )
    ty_dependency = replace(
        environment.dependencies[0],
        name="ty",
        module="ty",
        required=">=0.0.35,<0.1",
        version="0.0.35",
        origin="/repo/.venv/lib/python3.12/site-packages/ty/__init__.py",
    )
    return replace(
        report,
        selection=replace(report.selection, checks=("ruff", "ty")),
        checks=(ruff, ty),
        repository_environment=replace(
            environment,
            dependencies=(environment.dependencies[0], ty_dependency),
        ),
    )


def process_result(
    role: str,
    *,
    argv: tuple[str, ...] = ("uv", "--version"),
    exit_code: int = 0,
) -> ProcessResult:
    return ProcessResult(
        role=cast(Any, role),
        argv=argv,
        cwd="/repo",
        outcome="exited",
        exit_code=exit_code,
        signal=None,
        duration_ms=1,
        stdout=captured_text(),
        stderr=captured_text(),
        error_message=None,
    )


def repository_python() -> PythonEvidence:
    return PythonEvidence(
        implementation="cpython",
        version=(3, 12, 11),
        executable="/repo/.venv/bin/python",
    )


def valid_run_report() -> RunReportV2:
    python = repository_python()
    start = CheckStartEvidence(
        schema_version=1,
        check="ruff",
        module="ruff",
        arguments_sha256="d3fc572777ea5286ba40e5a116d16283eb8ab238c8a0f8c49170fdab2ca8c14f",
        python=python,
    )
    dependency = DependencyEvidence(
        name="ruff",
        module="ruff",
        required=">=0.15,<1",
        status="available",
        version="0.15.0",
        origin="/repo/.venv/lib/python3.12/site-packages/ruff/__init__.py",
        process=process_result("dependency_probe"),
        error=None,
    )
    environment = RepositoryEnvironmentEvidence(
        manager="uv",
        manager_version="0.8.13",
        path="/repo/.venv",
        python_selection=RepositoryPythonSelectionEvidence(kind="default", request=None),
        python=python,
        lock=LockEvidence(path="/repo/uv.lock", status="current"),
        dependency_selection="default",
        mutation_protection="tracked_files",
        dependencies=(dependency,),
        processes=(
            process_result("repository_safety"),
            process_result("uv_version"),
            process_result("environment_probe"),
            process_result(
                "repository_safety",
                argv=(
                    "git",
                    "-C",
                    "/repo",
                    "ls-files",
                    "--stage",
                    "-z",
                    "--",
                    ".",
                ),
            ),
        ),
        error=None,
    )
    check = CheckResultV2(
        name="ruff",
        status="passed",
        execution_environment="repository",
        analysis_python_authority=AnalysisPythonAuthorityEvidence(
            authority="repository_tool",
            pyrepo_check_override=None,
        ),
        start_evidence=start,
        processes=(
            process_result(
                "primary",
                argv=(
                    "uv",
                    "run",
                    "--locked",
                    "python",
                    "/repo/.pyrepo-check/check-launcher.py",
                    "--evidence",
                    "/repo/.pyrepo-check/start.json",
                    "--check",
                    "ruff",
                    "--module",
                    "ruff",
                    "--",
                    "check",
                    "src",
                ),
            ),
        ),
        error=None,
    )
    return RunReportV2(
        schema_version=2,
        kind="run",
        project_root="/repo",
        mode="focused",
        overall_status="passed",
        complete=True,
        tool_environment=tool_environment_evidence(),
        repository_environment=environment,
        selection=Selection(
            checks=("ruff",),
            targets=("src",),
            test_shortcut=None,
            pytest_args=None,
            planned_test_scope="not_selected",
            planned_coverage_scope="not_requested",
        ),
        checks=(check,),
        pytest=None,
        coverage=None,
        advisories=(),
    )


def test_schema_v2_planning_error_contains_tool_environment() -> None:
    report = PlanningErrorReportV2(
        schema_version=2,
        kind="planning_error",
        overall_status="error",
        complete=False,
        tool_environment=tool_environment_evidence(python=(3, 13, 15)),
        repository_environment=None,
        error=PlanningErrorV2(
            code="unsafe_unlocked_execution",
            message="--no-frozen is incompatible with repository-safe execution.",
            hint="Update uv.lock explicitly, then rerun without --no-frozen.",
        ),
    )

    validate_report_v2(report)
    payload = asdict(report)
    assert tuple(payload) == (
        "schema_version",
        "kind",
        "overall_status",
        "complete",
        "tool_environment",
        "repository_environment",
        "error",
    )
    assert payload["schema_version"] == 2
    assert payload["repository_environment"] is None
    assert payload["tool_environment"]["python"]["version"] == (3, 13, 15)


@pytest.mark.parametrize(
    ("model", "expected"),
    (
        (
            PlanningErrorV2,
            ("code", "message", "hint"),
        ),
        (PythonEvidence, ("implementation", "version", "executable")),
        (ToolEnvironmentEvidence, ("pyrepo_check_version", "python")),
        (RepositoryPythonSelectionEvidence, ("kind", "request")),
        (LockEvidence, ("path", "status")),
        (EnvironmentError, ("code", "message", "hint")),
        (CheckErrorV2, ("code", "message", "hint")),
        (
            DependencyEvidence,
            (
                "name",
                "module",
                "required",
                "status",
                "version",
                "origin",
                "process",
                "error",
            ),
        ),
        (CapturedText, ("captured", "text", "truncated", "omitted_bytes")),
        (
            ProcessResult,
            (
                "role",
                "argv",
                "cwd",
                "outcome",
                "exit_code",
                "signal",
                "duration_ms",
                "stdout",
                "stderr",
                "error_message",
            ),
        ),
        (
            Selection,
            (
                "checks",
                "targets",
                "test_shortcut",
                "pytest_args",
                "planned_test_scope",
                "planned_coverage_scope",
            ),
        ),
        (Advisory, ("code", "message", "hint")),
        (
            RepositoryEnvironmentEvidence,
            (
                "manager",
                "manager_version",
                "path",
                "python_selection",
                "python",
                "lock",
                "dependency_selection",
                "mutation_protection",
                "dependencies",
                "processes",
                "error",
            ),
        ),
        (
            CheckResultV2,
            (
                "name",
                "status",
                "execution_environment",
                "analysis_python_authority",
                "start_evidence",
                "processes",
                "error",
            ),
        ),
        (
            CheckStartEvidence,
            ("schema_version", "check", "module", "arguments_sha256", "python"),
        ),
        (
            AnalysisPythonAuthorityEvidence,
            ("authority", "pyrepo_check_override"),
        ),
        (
            PlanningErrorReportV2,
            (
                "schema_version",
                "kind",
                "overall_status",
                "complete",
                "tool_environment",
                "repository_environment",
                "error",
            ),
        ),
        (
            RunReportV2,
            (
                "schema_version",
                "kind",
                "project_root",
                "mode",
                "overall_status",
                "complete",
                "tool_environment",
                "repository_environment",
                "selection",
                "checks",
                "pytest",
                "coverage",
                "advisories",
            ),
        ),
    ),
)
def test_schema_v2_preserves_normative_field_order(
    model: type[object], expected: tuple[str, ...]
) -> None:
    assert tuple(field.name for field in fields(model)) == expected


def test_schema_v2_rejects_required_field_type_enum_and_nullability_mutations() -> None:
    report = valid_run_report()
    environment = report.repository_environment
    dependency = environment.dependencies[0]
    check = report.checks[0]
    start = check.start_evidence
    authority = check.analysis_python_authority
    process = check.processes[0]
    assert start is not None
    assert authority is not None
    mutations: tuple[tuple[str, RunReportV2], ...] = (
        ("schema_version", replace(report, schema_version=cast(Any, True))),
        ("kind", replace(report, kind=cast(Any, "planning_error"))),
        ("project_root_type", replace(report, project_root=cast(Any, 3))),
        ("project_root_absolute", replace(report, project_root="repo")),
        ("mode", replace(report, mode=cast(Any, "all"))),
        ("overall_status", replace(report, overall_status=cast(Any, "ok"))),
        ("complete", replace(report, complete=cast(Any, 1))),
        ("tool_environment", replace(report, tool_environment=cast(Any, object()))),
        (
            "tool_version",
            replace(
                report,
                tool_environment=replace(
                    report.tool_environment, pyrepo_check_version=cast(Any, 1)
                ),
            ),
        ),
        (
            "tool_python",
            replace(
                report,
                tool_environment=replace(report.tool_environment, python=cast(Any, object())),
            ),
        ),
        (
            "tool_python_implementation",
            replace(
                report,
                tool_environment=replace(
                    report.tool_environment,
                    python=replace(
                        report.tool_environment.python, implementation=cast(Any, 1)
                    ),
                ),
            ),
        ),
        (
            "tool_python_version",
            replace(
                report,
                tool_environment=replace(
                    report.tool_environment,
                    python=replace(
                        report.tool_environment.python, version=cast(Any, (3, 12))
                    ),
                ),
            ),
        ),
        (
            "tool_python_executable",
            replace(
                report,
                tool_environment=replace(
                    report.tool_environment,
                    python=replace(
                        report.tool_environment.python, executable=cast(Any, 1)
                    ),
                ),
            ),
        ),
        (
            "python_implementation",
            replace(
                report,
                repository_environment=replace(
                    environment,
                    python=replace(repository_python(), implementation=cast(Any, 1)),
                ),
            ),
        ),
        (
            "python_version",
            replace(
                report,
                repository_environment=replace(
                    environment,
                    python=replace(repository_python(), version=cast(Any, (3, 12))),
                ),
            ),
        ),
        (
            "python_executable",
            replace(
                report,
                repository_environment=replace(
                    environment,
                    python=replace(repository_python(), executable=cast(Any, 3)),
                ),
            ),
        ),
        (
            "repository_environment",
            replace(report, repository_environment=cast(Any, None)),
        ),
        (
            "manager",
            replace(
                report,
                repository_environment=replace(environment, manager=cast(Any, "venv")),
            ),
        ),
        (
            "manager_version",
            replace(
                report,
                repository_environment=replace(environment, manager_version=cast(Any, 1)),
            ),
        ),
        (
            "environment_path",
            replace(
                report,
                repository_environment=replace(environment, path=cast(Any, 1)),
            ),
        ),
        (
            "python_selection",
            replace(
                report,
                repository_environment=replace(
                    environment, python_selection=cast(Any, object())
                ),
            ),
        ),
        (
            "python_selection_kind",
            replace(
                report,
                repository_environment=replace(
                    environment,
                    python_selection=replace(
                        environment.python_selection, kind=cast(Any, "automatic")
                    ),
                ),
            ),
        ),
        (
            "default_python_request",
            replace(
                report,
                repository_environment=replace(
                    environment,
                    python_selection=replace(environment.python_selection, request="3.12"),
                ),
            ),
        ),
        (
            "explicit_python_request",
            replace(
                report,
                repository_environment=replace(
                    environment,
                    python_selection=RepositoryPythonSelectionEvidence(
                        kind="explicit", request=None
                    ),
                ),
            ),
        ),
        (
            "lock",
            replace(
                report,
                repository_environment=replace(environment, lock=cast(Any, object())),
            ),
        ),
        (
            "lock_path",
            replace(
                report,
                repository_environment=replace(
                    environment, lock=replace(environment.lock, path=cast(Any, 1))
                ),
            ),
        ),
        (
            "lock_status",
            replace(
                report,
                repository_environment=replace(
                    environment,
                    lock=replace(environment.lock, status=cast(Any, "present")),
                ),
            ),
        ),
        (
            "dependency_selection",
            replace(
                report,
                repository_environment=replace(
                    environment, dependency_selection=cast(Any, "all")
                ),
            ),
        ),
        (
            "mutation_protection",
            replace(
                report,
                repository_environment=replace(
                    environment, mutation_protection=cast(Any, "sandboxed")
                ),
            ),
        ),
        (
            "dependencies_tuple",
            replace(
                report,
                repository_environment=replace(
                    environment, dependencies=cast(Any, [dependency])
                ),
            ),
        ),
        (
            "dependency_type",
            replace(
                report,
                repository_environment=replace(
                    environment, dependencies=(cast(Any, object()),)
                ),
            ),
        ),
        (
            "dependency_name",
            replace(
                report,
                repository_environment=replace(
                    environment,
                    dependencies=(replace(dependency, name=cast(Any, "mypy")),),
                ),
            ),
        ),
        (
            "dependency_module",
            replace(
                report,
                repository_environment=replace(
                    environment,
                    dependencies=(replace(dependency, module=cast(Any, 1)),),
                ),
            ),
        ),
        (
            "dependency_required",
            replace(
                report,
                repository_environment=replace(
                    environment,
                    dependencies=(replace(dependency, required=cast(Any, 1)),),
                ),
            ),
        ),
        (
            "dependency_status",
            replace(
                report,
                repository_environment=replace(
                    environment,
                    dependencies=(replace(dependency, status=cast(Any, "ready")),),
                ),
            ),
        ),
        (
            "dependency_version",
            replace(
                report,
                repository_environment=replace(
                    environment,
                    dependencies=(replace(dependency, version=cast(Any, 1)),),
                ),
            ),
        ),
        (
            "dependency_origin",
            replace(
                report,
                repository_environment=replace(
                    environment,
                    dependencies=(replace(dependency, origin=cast(Any, 1)),),
                ),
            ),
        ),
        (
            "dependency_process",
            replace(
                report,
                repository_environment=replace(
                    environment,
                    dependencies=(replace(dependency, process=cast(Any, object())),),
                ),
            ),
        ),
        (
            "dependency_error",
            replace(
                report,
                repository_environment=replace(
                    environment,
                    dependencies=(replace(dependency, error=cast(Any, object())),),
                ),
            ),
        ),
        (
            "environment_processes_tuple",
            replace(
                report,
                repository_environment=replace(
                    environment, processes=cast(Any, list(environment.processes))
                ),
            ),
        ),
        (
            "environment_error",
            replace(
                report,
                repository_environment=replace(environment, error=cast(Any, object())),
            ),
        ),
        ("selection", replace(report, selection=cast(Any, object()))),
        (
            "selection_checks",
            replace(report, selection=replace(report.selection, checks=cast(Any, ["ruff"]))),
        ),
        (
            "selection_check_enum",
            replace(
                report,
                selection=replace(report.selection, checks=(cast(Any, "mypy"),)),
            ),
        ),
        (
            "selection_targets",
            replace(report, selection=replace(report.selection, targets=cast(Any, ["src"]))),
        ),
        (
            "selection_test_shortcut",
            replace(
                report,
                selection=replace(report.selection, test_shortcut=cast(Any, 1)),
            ),
        ),
        (
            "selection_pytest_args",
            replace(
                report,
                selection=replace(report.selection, pytest_args=cast(Any, ["src"])),
            ),
        ),
        (
            "planned_test_scope",
            replace(
                report,
                selection=replace(report.selection, planned_test_scope=cast(Any, "all")),
            ),
        ),
        (
            "planned_coverage_scope",
            replace(
                report,
                selection=replace(
                    report.selection, planned_coverage_scope=cast(Any, "all")
                ),
            ),
        ),
        ("checks_tuple", replace(report, checks=cast(Any, [check]))),
        ("check_type", replace(report, checks=(cast(Any, object()),))),
        ("check_name", replace(report, checks=(replace(check, name=cast(Any, "mypy")),))),
        (
            "check_status",
            replace(report, checks=(replace(check, status=cast(Any, "ok")),)),
        ),
        (
            "execution_environment",
            replace(
                report,
                checks=(replace(check, execution_environment=cast(Any, "tool")),),
            ),
        ),
        (
            "analysis_authority_type",
            replace(
                report,
                checks=(replace(check, analysis_python_authority=cast(Any, object())),),
            ),
        ),
        (
            "analysis_authority_enum",
            replace(
                report,
                checks=(
                    replace(
                        check,
                        analysis_python_authority=replace(
                            authority, authority=cast(Any, "controller")
                        ),
                    ),
                ),
            ),
        ),
        (
            "analysis_override",
            replace(
                report,
                checks=(
                    replace(
                        check,
                        analysis_python_authority=replace(
                            authority, pyrepo_check_override=cast(Any, "3.12")
                        ),
                    ),
                ),
            ),
        ),
        (
            "start_type",
            replace(report, checks=(replace(check, start_evidence=cast(Any, object())),)),
        ),
        (
            "start_schema",
            replace(
                report,
                checks=(replace(check, start_evidence=replace(start, schema_version=2)),),
            ),
        ),
        (
            "start_check",
            replace(
                report,
                checks=(
                    replace(check, start_evidence=replace(start, check=cast(Any, "mypy"))),
                ),
            ),
        ),
        (
            "start_module",
            replace(
                report,
                checks=(
                    replace(check, start_evidence=replace(start, module=cast(Any, "mypy"))),
                ),
            ),
        ),
        (
            "start_digest_type",
            replace(
                report,
                checks=(
                    replace(
                        check,
                        start_evidence=replace(start, arguments_sha256=cast(Any, 1)),
                    ),
                ),
            ),
        ),
        (
            "start_digest_shape",
            replace(
                report,
                checks=(replace(check, start_evidence=replace(start, arguments_sha256="abc")),),
            ),
        ),
        (
            "start_python",
            replace(
                report,
                checks=(replace(check, start_evidence=replace(start, python=cast(Any, object()))),),
            ),
        ),
        (
            "check_processes_tuple",
            replace(report, checks=(replace(check, processes=cast(Any, [process])),)),
        ),
        (
            "check_error_type",
            replace(report, checks=(replace(check, error=cast(Any, object())),)),
        ),
        ("pytest_type", replace(report, pytest=cast(Any, object()))),
        ("coverage_type", replace(report, coverage=cast(Any, object()))),
        ("advisories_tuple", replace(report, advisories=cast(Any, []))),
        ("advisory_type", replace(report, advisories=(cast(Any, object()),))),
    )

    for case, malformed in mutations:
        with pytest.raises(ReportingError, match=r"^invalid report:") as error:
            validate_report_structure_v2(malformed)
        assert case and str(error.value)


def test_schema_v2_rejects_retained_process_and_advisory_structural_mutations() -> None:
    report = valid_run_report()
    check = report.checks[0]
    process = check.processes[0]
    stream = process.stdout
    advisory = Advisory("output_truncated", "output omitted", None)
    mutations = (
        replace(process, role=cast(Any, "setup")),
        replace(process, argv=cast(Any, ["uv"])),
        replace(process, cwd=cast(Any, 1)),
        replace(process, cwd="repo"),
        replace(process, outcome=cast(Any, "completed")),
        replace(process, duration_ms=-1),
        replace(process, stdout=cast(Any, object())),
        replace(process, stdout=replace(stream, captured=cast(Any, 1))),
        replace(process, stdout=replace(stream, text=cast(Any, 1))),
        replace(process, stdout=replace(stream, truncated=True, omitted_bytes=0)),
        replace(process, stdout=replace(stream, truncated=False, omitted_bytes=1)),
        replace(process, error_message=cast(Any, 1)),
        replace(process, exit_code=None),
        replace(process, signal=9),
    )
    for malformed_process in mutations:
        malformed = replace(report, checks=(replace(check, processes=(malformed_process,)),))
        with pytest.raises(ReportingError, match=r"^invalid report:"):
            validate_report_structure_v2(malformed)

    advisory_mutations = (
        replace(advisory, code=cast(Any, "warning")),
        replace(advisory, message=cast(Any, 1)),
        replace(advisory, hint=cast(Any, 1)),
    )
    for malformed_advisory in advisory_mutations:
        with pytest.raises(ReportingError, match=r"^invalid report:"):
            validate_report_structure_v2(replace(report, advisories=(malformed_advisory,)))


def test_schema_v2_process_outcome_nullability_is_exact() -> None:
    report = valid_run_report()
    check = report.checks[0]
    exited = check.processes[0]
    signaled = replace(
        exited,
        outcome="signaled",
        exit_code=None,
        signal=9,
        error_message="terminated",
    )
    spawn_failed = replace(
        exited,
        outcome="spawn_failed",
        exit_code=None,
        signal=None,
        error_message="could not spawn",
    )
    for valid_process in (signaled, spawn_failed):
        validate_report_structure_v2(
            replace(report, checks=(replace(check, processes=(valid_process,)),))
        )

    malformed_processes = (
        replace(signaled, exit_code=1),
        replace(signaled, signal=None),
        replace(signaled, error_message=None),
        replace(spawn_failed, exit_code=1),
        replace(spawn_failed, signal=9),
        replace(spawn_failed, error_message=None),
    )
    for malformed_process in malformed_processes:
        with pytest.raises(ReportingError):
            validate_report_structure_v2(
                replace(report, checks=(replace(check, processes=(malformed_process,)),))
            )


def test_schema_v2_selection_order_local_nullability_is_exact() -> None:
    report = valid_run_report()
    malformed_unselected = (
        replace(report.selection, test_shortcut="unit"),
        replace(report.selection, pytest_args=("src",)),
        replace(report.selection, planned_test_scope="complete"),
        replace(report.selection, planned_coverage_scope="complete"),
    )
    for malformed_selection in malformed_unselected:
        with pytest.raises(ReportingError):
            validate_report_structure_v2(replace(report, selection=malformed_selection))


_ModelT = TypeVar("_ModelT")


def extended_dataclass(value: _ModelT) -> _ModelT:
    model = type(value)
    extended = make_dataclass(
        f"Extended{model.__name__}",
        (("unexpected", str),),
        bases=(model,),
        frozen=True,
    )
    return cast(
        _ModelT,
        extended(
            *(getattr(value, field.name) for field in fields(cast(Any, value))),
            "unexpected",
        ),
    )


def test_schema_v2_rejects_subclasses_for_every_schema_owned_model() -> None:
    report = valid_run_report()
    environment = report.repository_environment
    dependency = environment.dependencies[0]
    check = report.checks[0]
    start = cast(CheckStartEvidence, check.start_evidence)
    authority = cast(AnalysisPythonAuthorityEvidence, check.analysis_python_authority)
    process = check.processes[0]
    advisory = Advisory("output_truncated", "truncated", None)
    environment_error = EnvironmentError("repository_state_changed", "changed", None)
    check_error = CheckErrorV2("check_execution_failed", "failed", None)
    mutations = (
        replace(report, tool_environment=extended_dataclass(report.tool_environment)),
        replace(
            report,
            tool_environment=replace(
                report.tool_environment,
                python=extended_dataclass(report.tool_environment.python),
            ),
        ),
        replace(
            report,
            repository_environment=extended_dataclass(environment),
        ),
        replace(
            report,
            repository_environment=replace(
                environment,
                python_selection=extended_dataclass(environment.python_selection),
            ),
        ),
        replace(
            report,
            repository_environment=replace(environment, lock=extended_dataclass(environment.lock)),
        ),
        replace(
            report,
            repository_environment=replace(
                environment,
                dependencies=(extended_dataclass(dependency),),
            ),
        ),
        replace(
            report,
            repository_environment=replace(
                environment,
                dependencies=(
                    replace(dependency, error=extended_dataclass(check_error)),
                ),
            ),
        ),
        replace(
            report,
            repository_environment=replace(
                environment,
                processes=(extended_dataclass(environment.processes[0]), *environment.processes[1:]),
            ),
        ),
        replace(
            report,
            checks=(replace(check, processes=(replace(process, stdout=extended_dataclass(process.stdout)),)),),
        ),
        replace(report, selection=extended_dataclass(report.selection)),
        replace(report, checks=(extended_dataclass(check),)),
        replace(
            report,
            checks=(replace(check, start_evidence=extended_dataclass(start)),),
        ),
        replace(
            report,
            checks=(replace(check, analysis_python_authority=extended_dataclass(authority)),),
        ),
        replace(report, advisories=(extended_dataclass(advisory),)),
        extended_dataclass(report),
        replace(
            report,
            repository_environment=replace(
                environment, error=extended_dataclass(environment_error)
            ),
        ),
    )
    for malformed in mutations:
        with pytest.raises(ReportingError):
            validate_report_structure_v2(malformed)

    planning = PlanningErrorReportV2(
        2,
        "planning_error",
        "error",
        False,
        tool_environment_evidence(),
        None,
        PlanningErrorV2("unknown_check", "unknown", None),
    )
    for malformed in (
        extended_dataclass(planning),
        replace(planning, error=extended_dataclass(planning.error)),
    ):
        with pytest.raises(ReportingError):
            validate_report_structure_v2(malformed)


def test_schema_v2_rejects_relative_and_lexically_non_normal_paths() -> None:
    report = valid_run_report()
    environment = report.repository_environment
    check = report.checks[0]
    start = cast(CheckStartEvidence, check.start_evidence)
    process = check.processes[0]
    malformed = (
        replace(report, project_root="repo"),
        replace(report, project_root="/repo/../repo"),
        replace(
            report,
            tool_environment=replace(
                report.tool_environment,
                python=replace(report.tool_environment.python, executable="tool/python"),
            ),
        ),
        replace(
            report,
            tool_environment=replace(
                report.tool_environment,
                python=replace(
                    report.tool_environment.python,
                    executable="/tool/../tool/bin/python",
                ),
            ),
        ),
        replace(
            report,
            repository_environment=replace(environment, path="repo/.venv"),
        ),
        replace(
            report,
            repository_environment=replace(environment, path="/repo/./.venv"),
        ),
        replace(
            report,
            repository_environment=replace(
                environment,
                python=replace(repository_python(), executable="repo/.venv/bin/python"),
            ),
        ),
        replace(
            report,
            repository_environment=replace(
                environment,
                lock=replace(environment.lock, path="/repo/../repo/uv.lock"),
            ),
        ),
        replace(
            report,
            checks=(
                replace(
                    check,
                    start_evidence=replace(
                        start,
                        python=replace(start.python, executable="/repo/.venv/../bin/python"),
                    ),
                ),
            ),
        ),
        replace(
            report,
            checks=(replace(check, processes=(replace(process, cwd="/repo/../repo"),)),),
        ),
    )
    for invalid in malformed:
        with pytest.raises(ReportingError):
            validate_report_structure_v2(invalid)

    pytest_selection = Selection(
        checks=("pytest",),
        targets=("tests",),
        test_shortcut=None,
        pytest_args=("tests",),
        planned_test_scope="partial",
        planned_coverage_scope="not_requested",
    )
    validate_report_structure_v2(replace(report, selection=pytest_selection))
    malformed_selected = (
        replace(pytest_selection, pytest_args=None),
        replace(pytest_selection, pytest_args=("other",)),
        replace(pytest_selection, planned_test_scope="complete"),
        replace(pytest_selection, test_shortcut="Bad", targets=(), pytest_args=("tests",)),
        replace(
            pytest_selection,
            test_shortcut="unit",
            targets=("tests",),
            pytest_args=("tests",),
        ),
    )
    for malformed_selection in malformed_selected:
        with pytest.raises(ReportingError):
            validate_report_structure_v2(replace(report, selection=malformed_selection))


def test_schema_v2_rejects_planning_and_typed_error_structural_mutations() -> None:
    planning = PlanningErrorReportV2(
        schema_version=2,
        kind="planning_error",
        overall_status="error",
        complete=False,
        tool_environment=tool_environment_evidence(),
        repository_environment=None,
        error=PlanningErrorV2("unknown_check", "unknown", None),
    )
    planning_mutations = (
        replace(planning, schema_version=cast(Any, True)),
        replace(planning, kind=cast(Any, "run")),
        replace(planning, overall_status=cast(Any, "passed")),
        replace(planning, complete=cast(Any, True)),
        replace(planning, tool_environment=cast(Any, object())),
        replace(planning, repository_environment=cast(Any, object())),
        replace(planning, error=cast(Any, object())),
        replace(planning, error=replace(planning.error, code=cast(Any, "bad"))),
        replace(planning, error=replace(planning.error, message=cast(Any, 1))),
        replace(planning, error=replace(planning.error, hint=cast(Any, 1))),
    )
    for malformed in planning_mutations:
        with pytest.raises(ReportingError, match=r"^invalid report:"):
            validate_report_structure_v2(malformed)

    report = environment_failure_report()
    environment = report.repository_environment
    assert environment.error is not None
    check = report.checks[0]
    assert check.error is not None
    error_mutations = (
        replace(environment.error, code=cast(Any, "bad")),
        replace(environment.error, message=cast(Any, 1)),
        replace(environment.error, hint=cast(Any, 1)),
    )
    for malformed_error in error_mutations:
        malformed_environment = replace(environment, error=malformed_error)
        with pytest.raises(ReportingError, match=r"^invalid report:"):
            validate_report_structure_v2(
                replace(report, repository_environment=malformed_environment)
            )
    check_error_mutations = (
        replace(check.error, code=cast(Any, "bad")),
        replace(check.error, message=cast(Any, 1)),
        replace(check.error, hint=cast(Any, 1)),
    )
    for malformed_error in check_error_mutations:
        with pytest.raises(ReportingError, match=r"^invalid report:"):
            validate_report_structure_v2(
                replace(report, checks=(replace(check, error=malformed_error),))
            )


def test_schema_v2_accepts_valid_success_and_environment_failure_baselines() -> None:
    validate_report_v2(valid_run_report())
    validate_report_v2(report_with_ty())
    validate_report_v2(environment_failure_report())
    validate_report_v2(environment_failure_report(coverage_requested=True))


def test_schema_v2_accepts_final_safety_after_uv_failure() -> None:
    report = environment_failure_report()
    environment = report.repository_environment
    uv_failure = process_result("uv_version", exit_code=1)
    failed_environment = replace(
        environment,
        lock=replace(environment.lock, status="unverified"),
        mutation_protection="tracked_files",
        processes=(
            process_result("repository_safety"),
            uv_failure,
            process_result(
                "repository_safety",
                argv=(
                    "git",
                    "-C",
                    "/repo",
                    "ls-files",
                    "--stage",
                    "-z",
                    "--",
                    ".",
                ),
            ),
        ),
        error=EnvironmentError(
            code="uv_unavailable",
            message="uv --version failed.",
            hint="Repair uv, then retry.",
        ),
    )

    validate_report_v2(replace(report, repository_environment=failed_environment))


def test_schema_v2_repository_integrity_state_families_are_exact() -> None:
    tracked = valid_run_report()
    validate_report_v2(tracked)

    protected = replace(
        tracked,
        repository_environment=replace(
            tracked.repository_environment,
            mutation_protection="protected_files",
            processes=tracked.repository_environment.processes[:-1],
        ),
    )
    validate_report_v2(protected)

    environment = tracked.repository_environment
    post_run = replace(
        tracked,
        overall_status="error",
        complete=False,
        repository_environment=replace(
            environment,
            error=EnvironmentError(
                "repository_state_changed",
                "Repository state changed after Check execution.",
                "Restore the repository state, then retry.",
            ),
        ),
    )
    validate_report_v2(post_run)

    pre_execution = replace(
        tracked,
        overall_status="error",
        complete=False,
        repository_environment=replace(
            environment,
            error=EnvironmentError(
                "environment_evidence_invalid",
                "Repository Environment evidence is incomplete.",
                "Inspect the preparation evidence.",
            ),
        ),
        checks=(
            CheckResultV2(
                name="ruff",
                status="error",
                execution_environment=None,
                analysis_python_authority=None,
                start_evidence=None,
                processes=(),
                error=CheckErrorV2(
                    "repository_environment_unavailable",
                    "Repository Environment is unavailable.",
                    None,
                ),
            ),
        ),
    )
    validate_report_v2(pre_execution)

    malformed = (
        replace(
            tracked,
            repository_environment=replace(environment, mutation_protection="unobserved"),
        ),
        replace(
            tracked,
            repository_environment=replace(
                environment,
                processes=(
                    *environment.processes[:-1],
                    replace(environment.processes[-1], argv=("git", "status")),
                ),
            ),
        ),
        replace(post_run, checks=pre_execution.checks),
        replace(pre_execution, checks=tracked.checks),
    )
    for invalid in malformed:
        with pytest.raises(ReportingError):
            validate_report_v2(invalid)


def test_schema_v2_accepts_exact_ordinary_check_outcome_families() -> None:
    passed = valid_run_report()
    check = passed.checks[0]
    primary = check.processes[0]

    failed = replace(
        passed,
        overall_status="failed",
        checks=(replace(check, status="failed", processes=(replace(primary, exit_code=1),)),),
    )
    execution_error = replace(
        passed,
        overall_status="error",
        complete=False,
        checks=(
            replace(
                check,
                status="error",
                analysis_python_authority=None,
                processes=(replace(primary, exit_code=2),),
                error=CheckErrorV2(
                    "check_execution_failed",
                    "ruff exited before producing completed findings.",
                    None,
                ),
            ),
        ),
    )
    missing_start = replace(
        passed,
        overall_status="error",
        complete=False,
        checks=(
            replace(
                check,
                status="error",
                execution_environment=None,
                analysis_python_authority=None,
                start_evidence=None,
                error=CheckErrorV2(
                    "check_start_evidence_invalid",
                    "ruff start evidence was not observed.",
                    None,
                ),
            ),
        ),
    )
    signaled = replace(
        missing_start,
        checks=(
            replace(
                missing_start.checks[0],
                processes=(
                    replace(
                        primary,
                        outcome="signaled",
                        exit_code=None,
                        signal=15,
                        error_message="terminated by signal 15",
                    ),
                ),
            ),
        ),
    )
    spawn_failed = replace(
        missing_start,
        checks=(
            replace(
                missing_start.checks[0],
                processes=(
                    replace(
                        primary,
                        outcome="spawn_failed",
                        exit_code=None,
                        error_message="could not spawn uv",
                    ),
                ),
                error=CheckErrorV2("spawn_failed", "could not spawn uv", None),
            ),
        ),
    )
    spawn_cleanup_failed = replace(
        spawn_failed,
        checks=(
            replace(
                spawn_failed.checks[0],
                error=CheckErrorV2(
                    "cleanup_failed",
                    "spawn and later workspace cleanup both failed",
                    None,
                ),
            ),
        ),
    )
    cleanup_failed = replace(
        passed,
        overall_status="error",
        complete=False,
        checks=(
            replace(
                check,
                status="error",
                error=CheckErrorV2(
                    "cleanup_failed",
                    "could not remove launcher evidence",
                    None,
                ),
            ),
        ),
    )
    workspace_setup_failed = replace(
        passed,
        overall_status="error",
        complete=False,
        checks=(
            replace(
                check,
                status="error",
                execution_environment=None,
                analysis_python_authority=None,
                start_evidence=None,
                processes=(),
                error=CheckErrorV2(
                    "cleanup_failed",
                    "could not create the isolated Check workspace",
                    None,
                ),
            ),
        ),
    )
    for report in (
        passed,
        failed,
        execution_error,
        missing_start,
        signaled,
        spawn_failed,
        spawn_cleanup_failed,
        cleanup_failed,
        workspace_setup_failed,
    ):
        validate_report_v2(report)


def test_schema_v2_rejects_ordinary_check_single_contradictions() -> None:
    passed = valid_run_report()
    check = passed.checks[0]
    primary = check.processes[0]
    malformed = (
        replace(
            passed,
            overall_status="failed",
            checks=(replace(check, status="failed", processes=(replace(primary, exit_code=2),)),),
        ),
        replace(
            passed,
            overall_status="error",
            complete=False,
            checks=(
                replace(
                    check,
                    status="error",
                    execution_environment=None,
                    analysis_python_authority=None,
                    start_evidence=None,
                    processes=(replace(primary, exit_code=2),),
                    error=CheckErrorV2(
                        "check_execution_failed",
                        "ruff failed without trusted start evidence.",
                        None,
                    ),
                ),
            ),
        ),
        replace(
            passed,
            overall_status="error",
            complete=False,
            checks=(
                replace(
                    check,
                    status="error",
                    analysis_python_authority=None,
                    processes=(replace(primary, exit_code=1),),
                    error=CheckErrorV2("check_execution_failed", "ruff failed", None),
                ),
            ),
        ),
        replace(
            passed,
            overall_status="error",
            complete=False,
            checks=(
                replace(
                    check,
                    status="error",
                    execution_environment=None,
                    analysis_python_authority=None,
                    start_evidence=None,
                    processes=(),
                    error=CheckErrorV2(
                        "check_execution_failed",
                        "no primary process exists",
                        None,
                    ),
                ),
            ),
        ),
    )
    for report in malformed:
        with pytest.raises(ReportingError):
            validate_report_v2(report)


def test_schema_v2_accepts_exact_pytest_coverage_producer_families() -> None:
    reports = (
        pytest_run_report(),
        pytest_run_report(exit_code=5),
        pytest_run_report(coverage="available"),
        pytest_run_report(coverage="missing"),
        pytest_run_report(coverage="helper_failure"),
        pytest_run_report(exit_code=2, coverage="primary_failure"),
        pytest_run_report(cleanup_failure=True),
        pytest_workspace_failure_report(),
    )
    for report in reports:
        validate_report_v2(report)


def test_schema_v2_rejects_pytest_coverage_single_contradictions() -> None:
    instrumented = pytest_run_report(coverage="available")
    check = instrumented.checks[0]
    start = cast(CheckStartEvidence, check.start_evidence)
    pytest_result = cast(PytestResult, instrumented.pytest)
    coverage_result = cast(CoverageResult, instrumented.coverage)
    coverage_dependency = instrumented.repository_environment.dependencies[1]

    missing_coverage = pytest_run_report(coverage="missing")
    missing_check = missing_coverage.checks[0]
    missing_start = cast(CheckStartEvidence, missing_check.start_evidence)

    helper_failure = pytest_run_report(coverage="helper_failure")
    helper_check = helper_failure.checks[0]

    primary_failure = pytest_run_report(exit_code=2, coverage="primary_failure")
    primary_check = primary_failure.checks[0]

    workspace_failure = pytest_workspace_failure_report()
    workspace_pytest = cast(PytestResult, workspace_failure.pytest)

    exit_five = pytest_run_report(exit_code=5)
    exit_five_check = exit_five.checks[0]
    exit_five_result = cast(PytestResult, exit_five.pytest)

    malformed = (
        replace(instrumented, checks=(replace(check, start_evidence=replace(start, module="pytest")),)),
        replace(
            instrumented,
            checks=(
                replace(
                    check,
                    processes=(
                        replace(
                            check.processes[0],
                            argv=tuple(
                                "pytest" if argument == "coverage" else argument
                                for argument in check.processes[0].argv
                            ),
                        ),
                        check.processes[1],
                    ),
                ),
            ),
        ),
        replace(
            instrumented,
            checks=(replace(check, status="failed"),),
            pytest=replace(pytest_result, status="failed"),
            overall_status="failed",
        ),
        replace(instrumented, pytest=replace(pytest_result, pytest_version="8.4.1")),
        replace(instrumented, coverage=replace(coverage_result, coverage_version="7.15.1")),
        replace(
            instrumented,
            coverage=replace(coverage_result, status="passed", gate_eligible=True),
        ),
        replace(
            instrumented,
            repository_environment=replace(
                instrumented.repository_environment,
                dependencies=(
                    instrumented.repository_environment.dependencies[0],
                    replace(coverage_dependency, version="7.15.1"),
                ),
            ),
        ),
        replace(instrumented, checks=(replace(check, processes=(check.processes[0],)),)),
        replace(
            instrumented,
            checks=(
                replace(
                    check,
                    processes=(
                        check.processes[0],
                        replace(check.processes[1], exit_code=2),
                    ),
                ),
            ),
        ),
        replace(exit_five, checks=(replace(exit_five_check, status="passed"),)),
        replace(exit_five, pytest=replace(exit_five_result, exit_code=1)),
        replace(
            missing_coverage,
            checks=(
                replace(
                    missing_check,
                    start_evidence=replace(missing_start, module="coverage"),
                ),
            ),
        ),
        replace(
            helper_failure,
            checks=(
                replace(
                    helper_check,
                    processes=(
                        helper_check.processes[0],
                        replace(helper_check.processes[1], exit_code=0),
                    ),
                ),
            ),
        ),
        replace(
            primary_failure,
            checks=(
                replace(
                    primary_check,
                    processes=(*primary_check.processes, process_result("coverage_json")),
                ),
            ),
        ),
        replace(
            workspace_failure,
            pytest=replace(
                workspace_pytest,
                error=PytestError("preflight_invalid", "wrong setup failure"),
            ),
        ),
    )
    for report in malformed:
        with pytest.raises(ReportingError):
            validate_report_v2(report)


def test_schema_v2_rejects_repository_attribution_without_start() -> None:
    report = valid_run_report()
    check = replace(
        report.checks[0],
        execution_environment="repository",
        start_evidence=None,
    )
    malformed = replace(report, checks=(check, *report.checks[1:]))

    with pytest.raises(ReportingError):
        validate_report_v2(malformed)


def test_schema_v2_rejects_current_lock_without_successful_probe() -> None:
    report = valid_run_report()
    environment = report.repository_environment
    malformed_environment = replace(
        environment,
        lock=replace(environment.lock, status="current"),
        processes=tuple(
            process
            for process in environment.processes
            if process.role != "environment_probe"
        ),
    )

    with pytest.raises(ReportingError):
        validate_report_v2(
            replace(report, repository_environment=malformed_environment)
        )


def test_schema_v2_rejects_environment_process_role_and_order_mutations() -> None:
    report = valid_run_report()
    environment = report.repository_environment
    dependency = environment.dependencies[0]
    malformed_environments = (
        replace(
            environment,
            processes=(
                environment.processes[1],
                environment.processes[0],
                *environment.processes[2:],
            ),
        ),
        replace(
            environment,
            processes=(*environment.processes, process_result("uv_version")),
        ),
        replace(
            environment,
            dependencies=(
                replace(
                    dependency,
                    process=replace(
                        cast(ProcessResult, dependency.process), role="environment_probe"
                    ),
                ),
            ),
        ),
    )
    for malformed_environment in malformed_environments:
        with pytest.raises(ReportingError):
            validate_report_v2(
                replace(report, repository_environment=malformed_environment)
            )


def test_schema_v2_rejects_dependency_order_deduplication_and_selection_mismatch() -> None:
    report = report_with_ty()
    environment = report.repository_environment
    malformed_reports = (
        replace(
            report,
            repository_environment=replace(
                environment, dependencies=tuple(reversed(environment.dependencies))
            ),
        ),
        replace(
            report,
            repository_environment=replace(
                environment,
                dependencies=(environment.dependencies[0], environment.dependencies[0]),
            ),
        ),
        replace(
            report,
            repository_environment=replace(
                environment,
                dependencies=(
                    replace(environment.dependencies[0], required=">=0"),
                    environment.dependencies[1],
                ),
            ),
        ),
        replace(report, selection=replace(report.selection, checks=("ruff",))),
    )
    for malformed in malformed_reports:
        with pytest.raises(ReportingError):
            validate_report_v2(malformed)


def test_schema_v2_rejects_start_digest_check_module_and_python_binding_mutations() -> None:
    report = valid_run_report()
    check = report.checks[0]
    start = check.start_evidence
    assert start is not None
    malformed_starts = (
        replace(start, arguments_sha256="0" * 64),
        replace(start, check="ty"),
        replace(start, module="ty"),
        replace(
            start,
            python=replace(start.python, executable="/repo/.venv/bin/other-python"),
        ),
    )
    for malformed_start in malformed_starts:
        with pytest.raises(ReportingError):
            validate_report_v2(
                replace(report, checks=(replace(check, start_evidence=malformed_start),))
            )


def test_schema_v2_rejects_launcher_check_module_and_argument_binding_mutations() -> None:
    report = valid_run_report()
    check = report.checks[0]
    argv = check.processes[0].argv
    malformed_argvs = (
        (*argv[:8], "ty", *argv[9:]),
        (*argv[:10], "ty", *argv[11:]),
        (*argv, "--"),
    )
    for malformed_argv in malformed_argvs:
        with pytest.raises(ReportingError):
            validate_report_v2(
                replace(
                    report,
                    checks=(
                        replace(
                            check,
                            processes=(replace(check.processes[0], argv=malformed_argv),),
                        ),
                    ),
                )
            )


def test_schema_v2_rejects_lock_probe_and_observed_environment_contradictions() -> None:
    report = valid_run_report()
    environment = report.repository_environment
    failed_probe = replace(
        environment.processes[2],
        exit_code=1,
    )
    malformed_environments = (
        replace(
            environment,
            processes=(
                *environment.processes[:2],
                failed_probe,
                *environment.processes[3:],
            ),
        ),
        replace(environment, path=None),
        replace(environment, python=None),
        replace(environment, lock=replace(environment.lock, status="missing")),
        replace(environment, error=EnvironmentError("repository_lock_missing", "missing", None)),
    )
    for malformed_environment in malformed_environments:
        with pytest.raises(ReportingError):
            validate_report_v2(
                replace(report, repository_environment=malformed_environment)
            )


def test_schema_v2_rejects_execution_and_analysis_attribution_mutations() -> None:
    report = valid_run_report()
    check = report.checks[0]
    malformed_checks = (
        replace(check, execution_environment=None),
        replace(check, analysis_python_authority=None),
        replace(check, start_evidence=None),
        replace(check, processes=(replace(check.processes[0], role="environment_probe"),)),
        replace(
            check,
            processes=(replace(check.processes[0], exit_code=2),),
        ),
    )
    for malformed_check in malformed_checks:
        with pytest.raises(ReportingError):
            validate_report_v2(replace(report, checks=(malformed_check,)))

    bandit_start = cast(CheckStartEvidence, check.start_evidence)
    bandit = replace(
        check,
        name="bandit",
        start_evidence=replace(bandit_start, check="bandit", module="bandit"),
        processes=(
            replace(
                check.processes[0],
                argv=(
                    *check.processes[0].argv[:8],
                    "bandit",
                    "--module",
                    "bandit",
                    "--",
                    "check",
                    "src",
                ),
            ),
        ),
    )
    dependency = replace(
        report.repository_environment.dependencies[0],
        name="bandit",
        module="bandit",
        required=">=1.9,<2",
        version="1.9.0",
        origin="/repo/.venv/lib/python3.12/site-packages/bandit/__init__.py",
    )
    bandit_report = replace(
        report,
        selection=replace(report.selection, checks=("bandit",)),
        checks=(bandit,),
        repository_environment=replace(
            report.repository_environment, dependencies=(dependency,)
        ),
    )
    validate_report_v2(
        replace(
            bandit_report,
            checks=(replace(bandit, analysis_python_authority=None),),
        )
    )
    with pytest.raises(ReportingError):
        validate_report_v2(bandit_report)


def dependency_failure_report(status: str) -> RunReportV2:
    report = valid_run_report()
    dependency = report.repository_environment.dependencies[0]
    code = {
        "missing": "check_dependency_missing",
        "incompatible": "check_dependency_incompatible",
        "shadowed": "check_dependency_shadowed",
        "unusable": "check_dependency_unusable",
        "unobserved": "check_dependency_unusable",
    }[status]
    error = CheckErrorV2(cast(Any, code), f"ruff is {status}", "Repair ruff.")
    failed_dependency = replace(
        dependency,
        status=cast(Any, status),
        version="0.14.0" if status in {"incompatible", "shadowed", "unusable"} else None,
        origin=(
            "/repo/.venv/lib/python3.12/site-packages/ruff/__init__.py"
            if status in {"incompatible", "shadowed"}
            else None
        ),
        error=error,
    )
    failed_check = replace(
        report.checks[0],
        status="error",
        execution_environment=None,
        analysis_python_authority=None,
        start_evidence=None,
        processes=(),
        error=error,
    )
    return replace(
        report,
        overall_status="error",
        complete=False,
        repository_environment=replace(
            report.repository_environment,
            dependencies=(failed_dependency,),
        ),
        checks=(failed_check,),
    )


def pytest_dependency_failure_report(status: str) -> RunReportV2:
    report = pytest_run_report()
    dependency = report.repository_environment.dependencies[0]
    check_code = {
        "missing": "check_dependency_missing",
        "incompatible": "check_dependency_incompatible",
        "shadowed": "check_dependency_shadowed",
        "unusable": "check_dependency_unusable",
        "unobserved": "check_dependency_unusable",
    }[status]
    nested_code = {
        "missing": "module_unavailable",
        "incompatible": "unsupported_version",
        "shadowed": "preflight_invalid",
        "unusable": "preflight_invalid",
        "unobserved": "preflight_invalid",
    }[status]
    version = "7.4.0" if status == "incompatible" else "8.4.2" if status == "shadowed" else None
    origin = (
        "/outside/repo/site-packages/pytest/__init__.py"
        if status == "shadowed"
        else dependency.origin
        if status == "incompatible"
        else None
    )
    error = CheckErrorV2(cast(Any, check_code), f"pytest is {status}", "Repair pytest.")
    failed_dependency = replace(
        dependency,
        status=cast(Any, status),
        version=version,
        origin=origin,
        error=error,
    )
    failed_check = replace(
        report.checks[0],
        status="error",
        execution_environment=None,
        start_evidence=None,
        processes=(),
        error=error,
    )
    nested = PytestResult(
        status="error",
        complete=False,
        scope="partial",
        scope_reasons=("incomplete_session",),
        pytest_version=version if status == "incompatible" else None,
        exit_code=None,
        evidence=None,
        error=PytestError(cast(Any, nested_code), f"pytest is {status}"),
    )
    return replace(
        report,
        overall_status="error",
        complete=False,
        repository_environment=replace(
            report.repository_environment,
            dependencies=(failed_dependency,),
        ),
        checks=(failed_check,),
        pytest=nested,
    )


def test_schema_v2_rejects_dependency_status_evidence_mutations() -> None:
    valid = valid_run_report()
    available = valid.repository_environment.dependencies[0]
    available_mutations = (
        replace(available, version=None),
        replace(available, origin=None),
        replace(available, process=None),
        replace(available, error=CheckErrorV2("check_dependency_unusable", "bad", None)),
    )
    for malformed_dependency in available_mutations:
        with pytest.raises(ReportingError):
            validate_report_v2(
                replace(
                    valid,
                    repository_environment=replace(
                        valid.repository_environment,
                        dependencies=(malformed_dependency,),
                    ),
                )
            )

    for status in ("missing", "incompatible", "shadowed", "unusable", "unobserved"):
        report = dependency_failure_report(status)
        validate_report_v2(report)
        dependency = report.repository_environment.dependencies[0]
        mutations = [replace(dependency, error=None)]
        if status != "unobserved":
            mutations.append(replace(dependency, process=None))
        if status == "incompatible":
            mutations.append(replace(dependency, version=None))
        if status == "shadowed":
            mutations.append(replace(dependency, origin=None))
        for malformed_dependency in mutations:
            with pytest.raises(ReportingError):
                validate_report_v2(
                    replace(
                        report,
                        repository_environment=replace(
                            report.repository_environment,
                            dependencies=(malformed_dependency,),
                        ),
                    )
                )


@pytest.mark.parametrize(
    "status",
    ("missing", "incompatible", "shadowed", "unusable", "unobserved"),
)
def test_schema_v2_correlates_pytest_dependency_failure_evidence(status: str) -> None:
    report = pytest_dependency_failure_report(status)
    validate_report_v2(report)
    result = cast(PytestResult, report.pytest)
    check = report.checks[0]
    wrong_nested_code = (
        "preflight_invalid"
        if result.error is not None and result.error.code != "preflight_invalid"
        else "module_unavailable"
    )
    mutations = (
        replace(
            report,
            pytest=replace(
                result,
                error=PytestError(cast(Any, wrong_nested_code), "wrong dependency state"),
            ),
        ),
        replace(
            report,
            pytest=replace(
                result,
                pytest_version=None if result.pytest_version is not None else "8.4.2",
            ),
        ),
        replace(
            report,
            checks=(
                replace(
                    check,
                    error=CheckErrorV2(
                        "check_dependency_missing"
                        if check.error is not None
                        and check.error.code == "check_dependency_unusable"
                        else "check_dependency_unusable",
                        "wrong dependency state",
                        None,
                    ),
                ),
            ),
        ),
    )
    for malformed in mutations:
        with pytest.raises(ReportingError):
            validate_report_v2(malformed)


def test_schema_v2_rejects_pytest_and_coverage_selection_nullability_mutations() -> None:
    unselected = valid_run_report()
    with pytest.raises(ReportingError):
        validate_report_v2(replace(unselected, pytest=pytest_error_result()))
    with pytest.raises(ReportingError):
        validate_report_v2(replace(unselected, coverage=coverage_error_result()))

    pytest_selected = environment_failure_report()
    validate_report_v2(pytest_selected)
    with pytest.raises(ReportingError):
        validate_report_v2(replace(pytest_selected, pytest=None))

    coverage_selected = environment_failure_report(coverage_requested=True)
    validate_report_v2(coverage_selected)
    with pytest.raises(ReportingError):
        validate_report_v2(replace(coverage_selected, coverage=None))
    with pytest.raises(ReportingError):
        validate_report_v2(replace(coverage_selected, pytest=None))


def test_schema_v2_rejects_complete_overall_status_and_error_relation_mutations() -> None:
    passed = valid_run_report()
    check = passed.checks[0]
    malformed_reports = (
        replace(passed, complete=False),
        replace(passed, overall_status="failed"),
        replace(passed, overall_status="error"),
        replace(passed, checks=(replace(check, status="failed"),), overall_status="failed"),
        replace(
            passed,
            checks=(
                replace(
                    check,
                    error=CheckErrorV2("check_execution_failed", "failed", None),
                ),
            ),
        ),
    )
    for malformed in malformed_reports:
        with pytest.raises(ReportingError):
            validate_report_v2(malformed)

    environment_error = environment_failure_report()
    malformed_error_reports = (
        replace(environment_error, complete=True),
        replace(environment_error, overall_status="passed"),
        replace(environment_error, overall_status="failed"),
        replace(
            environment_error,
            repository_environment=replace(
                environment_error.repository_environment, error=None
            ),
        ),
        replace(
            environment_error,
            checks=(replace(environment_error.checks[0], error=None),),
        ),
    )
    for malformed in malformed_error_reports:
        with pytest.raises(ReportingError):
            validate_report_v2(malformed)


def test_schema_v2_requires_exact_evidence_derived_advisories() -> None:
    report = valid_run_report()
    check = report.checks[0]
    process = check.processes[0]
    truncated = replace(
        process,
        stdout=CapturedText(True, "tail", True, 1),
    )
    expected = Advisory(
        code="output_truncated",
        message=(
            "ruff process 1 (primary) stdout omitted 1 byte(s); "
            "only the final 65536 bytes are included."
        ),
        hint=None,
    )
    projected = replace(
        report,
        checks=(replace(check, processes=(truncated,)),),
        advisories=(expected,),
    )
    validate_report_v2(projected)

    for malformed in (
        replace(projected, advisories=()),
        replace(report, advisories=(expected,)),
        replace(projected, advisories=(replace(expected, message="invented"),)),
    ):
        with pytest.raises(ReportingError):
            validate_report_v2(malformed)
