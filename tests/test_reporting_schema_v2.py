from __future__ import annotations

from dataclasses import asdict, fields, replace
from typing import Any, cast

import pytest

from pyrepo_check.coverage_evidence import (
    CoverageError,
    CoverageResult,
    CoverageThreshold,
)
from pyrepo_check.pytest_evidence import PytestError, PytestResult
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
            process_result("repository_safety"),
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
            process_result("repository_safety"),
        ),
        error=EnvironmentError(
            code="uv_unavailable",
            message="uv --version failed.",
            hint="Repair uv, then retry.",
        ),
    )

    validate_report_v2(replace(report, repository_environment=failed_environment))


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
