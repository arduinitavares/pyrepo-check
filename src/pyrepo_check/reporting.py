from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import re
from typing import Literal, cast

from pyrepo_check.coverage_evidence import (
    CoverageError,
    CoverageResult,
    build_coverage_result,
    coverage_gate_policy_for_context,
    validate_coverage_result,
)
from pyrepo_check.execution import (
    CAPTURE_LIMIT_BYTES,
    CapturedBytes,
    CheckExecutionFailure,
    DependencyObservation,
    ExecutedCheck,
    ExecutedProcess,
    ExecutionResult,
    format_terminal_environment_line,
    PythonObservation,
    RepositoryCheckObservation,
    RepositoryEnvironmentObservation,
    ToolEnvironmentObservation,
)
from pyrepo_check.planning import (
    CheckName,
    OutputFormat,
    CheckInvocation,
    ExplicitRepositoryPython,
    PlanningErrorCode,
    RunPlan,
)
from pyrepo_check.pytest_evidence import (
    CollectionIssue,
    PytestCounts,
    PytestError,
    PytestEvidence,
    PytestResult,
    SlowTest,
    SpecialTestOutcome,
    build_pytest_result,
)
from pyrepo_check.repository_environment import dependency_name_for_check
from pyrepo_check.reporting_schema import (
    Advisory,
    AdvisoryCode as AdvisoryCode,
    AgentReportV2,
    AnalysisPythonAuthorityEvidence,
    CapturedText,
    CheckErrorV2,
    CheckResultV2,
    CheckStartEvidence,
    CheckStatus,
    DependencyEvidence,
    EnvironmentError,
    LockEvidence,
    OverallStatus,
    PlanningErrorReportV2,
    PlanningErrorV2,
    ProcessOutcome,
    ProcessResult,
    ProcessRole,
    PythonEvidence,
    ReportKind as ReportKind,
    RepositoryEnvironmentEvidence,
    RepositoryPythonSelectionEvidence,
    ReportingError,
    RunReportV2,
    Selection,
    ToolEnvironmentEvidence,
    validate_report_structure_v2,
)
from pyrepo_check.repository_environment import (
    SUPPORTED_DEPENDENCIES,
    dependency_version_supported,
)


_CSI_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CHECK_NAMES = frozenset(("ruff", "annotations", "annotations-fix", "ty", "bandit", "pytest"))
_PLANNING_ERROR_CODES = frozenset(
    (
        "invalid_arguments",
        "invalid_project_config",
        "invalid_test_shortcut",
        "unknown_check",
        "unknown_test_shortcut",
        "unknown_target",
        "coverage_configuration_required",
        "unsafe_unlocked_execution",
        "uv_project_required",
        "internal_planning_error",
    )
)
_RUN_MODES = frozenset(("focused", "strict_aggregate"))
_OVERALL_STATUSES = frozenset(("passed", "failed", "error"))
_CHECK_STATUSES = _OVERALL_STATUSES
_CHECK_ERROR_CODES = frozenset(
    (
        "spawn_failed",
        "terminated_by_signal",
        "pytest_preflight_failed",
        "pytest_evidence_error",
        "coverage_preflight_failed",
        "missing_primary_process",
        "cleanup_failed",
    )
)
_ADVISORY_CODES = frozenset(
    (
        "coverage_not_configured",
        "coverage_threshold_not_applied",
        "missing_test_reason",
        "output_truncated",
    )
)
_TERMINAL_COVERAGE_FILE_LIMIT = 3
_TERMINAL_COVERAGE_NAME_WIDTH = 48
_PYTEST_SCOPE_REASONS: tuple[str, ...] = (
    "planned_selector",
    "effective_narrowing_option",
    "unclassified_external_option",
    "deselected_tests",
    "collection_reduced",
    "incomplete_session",
)
_PYTEST_ERROR_CODES = frozenset(
    (
        "unsupported_python",
        "module_unavailable",
        "unsupported_version",
        "preflight_invalid",
        "unsupported_parallelism",
        "unsupported_retries",
        "exit_code_mismatch",
        "not_started",
        "spawn_failed",
        "terminated_by_signal",
        "artifact_missing",
        "artifact_invalid",
        "artifact_not_finalized",
        "session_incomplete",
        "interrupted",
        "internal_error",
        "usage_error",
        "unknown_exit_code",
    )
)
_PYTEST_PREFLIGHT_ERROR_CODES = frozenset(
    ("unsupported_python", "module_unavailable", "unsupported_version", "preflight_invalid")
)
_PYTEST_ARTIFACT_ERROR_CODES = frozenset(
    (
        "unsupported_parallelism",
        "unsupported_retries",
        "exit_code_mismatch",
        "artifact_missing",
        "artifact_invalid",
        "artifact_not_finalized",
    )
)
_COVERAGE_PREFLIGHT_ERROR_CODES = frozenset(
    (
        "unsupported_python",
        "module_unavailable",
        "unsupported_version",
        "preflight_invalid",
        "spawn_failed",
        "terminated_by_signal",
    )
)
_COVERAGE_PREPRIMARY_ERROR_CODES = frozenset(
    (
        "unsupported_python",
        "module_unavailable",
        "unsupported_version",
        "preflight_invalid",
    )
)
_COVERAGE_PREFLIGHT_ROLES: tuple[ProcessRole, ...] = (
    "pytest_preflight",
    "coverage_preflight",
)
_COVERAGE_PRIMARY_ROLES: tuple[ProcessRole, ...] = (*_COVERAGE_PREFLIGHT_ROLES, "primary")
_COVERAGE_COMPLETE_ROLES: tuple[ProcessRole, ...] = (*_COVERAGE_PRIMARY_ROLES, "coverage_json")
_COVERAGE_PREJSON_ARTIFACT_ERROR_CODES = frozenset(("data_missing", "unexpected_parallel_data"))


def validate_report_v2(report: AgentReportV2) -> None:
    validate_report_structure_v2(report)
    if isinstance(report, PlanningErrorReportV2):
        return
    _validate_run_report_v2(report)


def _validate_run_report_v2(report: RunReportV2) -> None:
    pytest_selected = "pytest" in report.selection.checks
    if report.mode == "focused" and report.selection.planned_coverage_scope == "unavailable":
        _invalid("focused runs cannot have unavailable planned coverage")
    if (
        report.mode == "strict_aggregate"
        and report.selection.planned_coverage_scope == "not_requested"
    ):
        _invalid("strict aggregate runs cannot omit planned coverage")
    emitted_names = tuple(check.name for check in report.checks)
    if report.selection.checks != emitted_names:
        _invalid("selection checks must exactly match emitted checks")

    if pytest_selected != (report.pytest is not None):
        _invalid("pytest must be present exactly when pytest is selected")
    if report.pytest is not None:
        _validate_pytest_result(report.pytest, report.selection)
    coverage_expected = report.selection.planned_coverage_scope in {"partial", "complete"}
    if coverage_expected != (report.coverage is not None):
        _invalid("coverage nullability contradicts planned coverage scope")
    if report.coverage is not None:
        try:
            validate_coverage_result(report.coverage)
        except ValueError as error:
            _invalid(f"invalid coverage result: {error}")
        if report.pytest is None:
            _invalid("coverage requires a pytest result")
        _validate_coverage_context_v2(report)

    environment = report.repository_environment
    _validate_environment_process_order_v2(environment.processes)
    _validate_environment_state_v2(environment, project_root=report.project_root)
    expected_dependencies = _expected_dependency_names_v2(report.selection)
    observed_dependencies = tuple(item.name for item in environment.dependencies)
    if observed_dependencies != expected_dependencies:
        _invalid("dependencies must use first-required order without duplicates")
    pre_execution_environment_error = (
        environment.error is not None
        and environment.error.code != "repository_state_changed"
    )
    if _unsafe_after_preparation_v2(environment):
        unattempted_dependency_seen = False
        for dependency in environment.dependencies:
            unattempted = dependency.status == "unobserved" and dependency.process is None
            if unattempted:
                unattempted_dependency_seen = True
            elif unattempted_dependency_seen:
                _invalid("unattempted dependencies must follow every attempted probe")
        unavailable_check_seen = False
        for check in report.checks:
            unavailable = (
                check.error is not None
                and check.error.code == "repository_environment_unavailable"
                and not check.processes
                and check.start_evidence is None
            )
            if unavailable:
                unavailable_check_seen = True
            elif unavailable_check_seen:
                _invalid("helper identity loss requires an unavailable Check suffix")
    for dependency in environment.dependencies:
        _validate_dependency_evidence_v2(
            dependency,
            pre_execution_environment_error=pre_execution_environment_error,
        )

    dependencies = {dependency.name: dependency for dependency in environment.dependencies}
    for check in report.checks:
        dependency = dependencies[dependency_name_for_check(check.name)]
        _validate_check_result_v2(
            check,
            environment,
            dependency,
            pytest_result=report.pytest if check.name == "pytest" else None,
            coverage_result=report.coverage if check.name == "pytest" else None,
            coverage_dependency=dependencies.get("coverage") if check.name == "pytest" else None,
            coverage_requested=coverage_expected if check.name == "pytest" else False,
        )
    expected_advisories = _build_advisories_v2(report)
    if report.advisories != expected_advisories:
        _invalid("advisories must exactly match v2 report evidence")

    expected_complete = _run_complete_v2(report)
    expected_status: OverallStatus
    if not expected_complete:
        expected_status = "error"
    elif (
        any(check.status == "failed" for check in report.checks)
        or (report.pytest is not None and report.pytest.status == "failed")
        or (report.coverage is not None and report.coverage.status == "failed")
    ):
        expected_status = "failed"
    else:
        expected_status = "passed"
    if report.complete is not expected_complete:
        _invalid("complete is inconsistent with v2 evidence")
    if report.overall_status != expected_status:
        _invalid("overall_status violates v2 evidence precedence")


def _validate_environment_process_order_v2(
    processes: tuple[ProcessResult, ...],
) -> None:
    phase = "safety"
    trailing_safety = False
    for index, process in enumerate(processes):
        role = process.role
        if role == "repository_safety":
            if phase == "safety":
                continue
            if phase in {"uv", "environment"} and index == len(processes) - 1 and not trailing_safety:
                trailing_safety = True
                continue
            _invalid("repository safety processes use an invalid order")
        if role == "uv_version":
            if phase != "safety":
                _invalid("uv version process uses an invalid order")
            phase = "uv"
            continue
        if role == "environment_probe":
            if phase != "uv":
                _invalid("environment probe must follow uv version")
            phase = "environment"
            continue
        _invalid("repository environment contains a non-environment process role")


def _validate_environment_state_v2(
    environment: RepositoryEnvironmentEvidence,
    *,
    project_root: str,
) -> None:
    _validate_pre_execution_stage_v2(environment)
    uv_process = next((item for item in environment.processes if item.role == "uv_version"), None)
    probe = next(
        (item for item in environment.processes if item.role == "environment_probe"), None
    )
    if environment.manager_version is not None and not _successful_process(uv_process):
        _invalid("manager version requires a successful uv version process")
    observed = environment.path is not None and environment.python is not None
    if (environment.path is None) != (environment.python is None):
        _invalid("repository path and Python must be observed together")
    if observed and not _successful_process(probe):
        _invalid("observed repository environment requires a successful probe")
    if observed:
        python = environment.python
        supported_python = _supported_repository_python_v2(python)
        if environment.error is not None and environment.error.code == "repository_python_unsupported":
            if supported_python:
                _invalid("repository_python_unsupported contradicts supported Python")
        elif not supported_python:
            _invalid("observed Repository Python is outside the supported set")
    if environment.lock.status == "current":
        if not observed or not _successful_process(probe):
            _invalid("current lock requires successful environment evidence")
    elif environment.lock.status == "missing":
        if (
            observed
            or probe is not None
            or environment.error is None
            or environment.error.code != "repository_lock_missing"
        ):
            _invalid("missing lock evidence contradicts repository environment state")
    if environment.error is None:
        if (
            environment.lock.status != "current"
            or not observed
            or environment.manager_version is None
        ):
            _invalid("successful repository environment requires current observed evidence")
    elif environment.lock.status == "current" and environment.error.code == "repository_lock_missing":
        _invalid("current lock cannot report repository_lock_missing")
    if environment.error is None and environment.mutation_protection == "unobserved":
        _invalid("successful repository environment requires mutation protection")
    if environment.mutation_protection == "tracked_files":
        expected_final_tail = (
            "-C",
            project_root,
            "ls-files",
            "--stage",
            "-z",
            "--",
            ".",
        )
        if (
            not environment.processes
            or environment.processes[-1].role != "repository_safety"
            or not _successful_process(environment.processes[-1])
            or len(environment.processes[-1].argv) != len(expected_final_tail) + 1
            or not Path(environment.processes[-1].argv[0]).is_absolute()
            or environment.processes[-1].argv[1:] != expected_final_tail
            or environment.processes[-1].cwd != project_root
        ):
            _invalid("tracked_files requires canonical successful final safety evidence")


def _validate_pre_execution_stage_v2(
    environment: RepositoryEnvironmentEvidence,
) -> None:
    error = environment.error
    if error is None or error.code == "repository_state_changed":
        return
    core_roles = tuple(
        process.role
        for process in environment.processes
        if process.role != "repository_safety"
    )
    uv_process = next(
        (process for process in environment.processes if process.role == "uv_version"),
        None,
    )
    probe = next(
        (process for process in environment.processes if process.role == "environment_probe"),
        None,
    )
    if not _unsafe_after_preparation_v2(environment) and any(
        dependency.status != "unobserved"
        or dependency.process is not None
        or dependency.error is not None
        for dependency in environment.dependencies
    ):
        _invalid("environment-wide failure cannot claim dependency probe evidence")
    if error.code == "repository_lock_missing":
        if (
            environment.lock.status != "missing"
            or environment.processes
            or environment.manager_version is not None
            or environment.path is not None
            or environment.python is not None
            or environment.mutation_protection != "unobserved"
        ):
            _invalid("repository_lock_missing must stop before every process")
        return
    if error.code == "uv_unavailable":
        if (
            core_roles != ("uv_version",)
            or not _failed_process(uv_process)
            or environment.lock.status != "unverified"
            or environment.manager_version is not None
            or environment.path is not None
            or environment.python is not None
        ):
            _invalid("uv_unavailable contradicts preparation stage evidence")
        return
    if error.code == "repository_environment_failed" and (
        core_roles != ("uv_version", "environment_probe")
        or not _failed_process(probe)
        or environment.lock.status != "unverified"
        or environment.manager_version is None
        or environment.path is not None
        or environment.python is not None
    ):
        _invalid("repository_environment_failed contradicts probe-stage evidence")
    if error.code == "repository_python_unsupported" and (
        core_roles != ("uv_version", "environment_probe")
        or environment.lock.status != "current"
        or environment.manager_version is None
        or environment.path is None
        or environment.python is None
    ):
        _invalid("repository_python_unsupported requires observed probe evidence")
    if error.code == "environment_evidence_invalid":
        uv_evidence_failure = (
            core_roles == ("uv_version",)
            and _zero_exit_process(uv_process)
            and environment.lock.status == "unverified"
            and environment.manager_version is None
            and environment.path is None
            and environment.python is None
        )
        probe_evidence_failure = (
            core_roles == ("uv_version", "environment_probe")
            and environment.manager_version is not None
            and _zero_exit_process(probe)
            and (
                environment.lock.status == "unverified"
                and environment.path is None
                and environment.python is None
                or environment.lock.status == "current"
                and environment.path is not None
                and environment.python is not None
            )
        )
        if not (uv_evidence_failure or probe_evidence_failure):
            _invalid("environment_evidence_invalid contradicts preparation stage")
    if error.code == "unsafe_repository_environment":
        before_process = (
            not core_roles
            and environment.lock.status == "unverified"
            and environment.manager_version is None
            and environment.path is None
            and environment.python is None
        )
        after_probe = (
            core_roles == ("uv_version", "environment_probe")
            and environment.lock.status == "current"
            and environment.manager_version is not None
            and environment.path is not None
            and environment.python is not None
        )
        if not (before_process or after_probe):
            _invalid("unsafe_repository_environment contradicts preparation stage")


def _unsafe_after_preparation_v2(
    environment: RepositoryEnvironmentEvidence,
) -> bool:
    error = environment.error
    core_roles = tuple(
        process.role
        for process in environment.processes
        if process.role != "repository_safety"
    )
    return (
        error is not None
        and error.code == "unsafe_repository_environment"
        and core_roles == ("uv_version", "environment_probe")
        and environment.lock.status == "current"
        and environment.manager_version is not None
        and environment.path is not None
        and environment.python is not None
    )


def _expected_dependency_names_v2(selection: Selection) -> tuple[str, ...]:
    names: list[str] = []
    for check in selection.checks:
        name = dependency_name_for_check(check)
        if name not in names:
            names.append(name)
    if selection.planned_coverage_scope in {"partial", "complete"}:
        if "pytest" not in names:
            _invalid("Coverage dependency requires selected pytest")
        names.append("coverage")
    return tuple(names)


def _validate_coverage_context_v2(report: RunReportV2) -> None:
    coverage = cast(CoverageResult, report.coverage)
    pytest_result = cast(PytestResult, report.pytest)
    expected_scope = (
        "complete"
        if (
            report.selection.planned_coverage_scope == "complete"
            and pytest_result.scope == "complete"
            and coverage.evidence_complete
        )
        else "partial"
    )
    if coverage.scope != expected_scope:
        _invalid("Coverage scope contradicts report evidence")
    policy = coverage_gate_policy_for_context(
        mode=report.mode,
        targets=report.selection.targets,
        test_shortcut=report.selection.test_shortcut,
        pytest_result=pytest_result,
        evidence_complete=coverage.evidence_complete,
        configured=coverage.threshold.configured,
    )
    if coverage.gate_eligible is not policy.gate_eligible:
        _invalid("Coverage gate eligibility contradicts report context")
    if coverage.status == "error":
        if policy.skipped_reason != "evidence_error":
            _invalid("Coverage error requires incomplete evidence")
        return
    if coverage.threshold.skipped_reason != policy.skipped_reason:
        _invalid("Coverage threshold skip reason contradicts report context")
    expected_status = (
        "guidance"
        if not policy.gate_eligible
        else "failed"
        if coverage.threshold.passed is False
        else "passed"
    )
    if coverage.status != expected_status:
        _invalid("Coverage status contradicts report context")


def _validate_dependency_evidence_v2(
    dependency: DependencyEvidence,
    *,
    pre_execution_environment_error: bool,
) -> None:
    expected_module = dependency.name
    if dependency.module != expected_module:
        _invalid("dependency module must match dependency name")
    supported = SUPPORTED_DEPENDENCIES[dependency.name]
    minimum = ".".join(str(part) for part in supported.minimum)
    maximum = ".".join(str(part) for part in supported.maximum)
    if dependency.required != f">={minimum},<{maximum}":
        _invalid("dependency required range does not match the supported contract")
    process = dependency.process
    if process is not None and process.role != "dependency_probe":
        _invalid("dependency process role must be dependency_probe")
    if dependency.status == "available":
        if (
            dependency.version is None
            or dependency.origin is None
            or not _successful_process(process)
            or dependency.error is not None
            or not dependency_version_supported(
                SUPPORTED_DEPENDENCIES[dependency.name],
                dependency.version,
            )
        ):
            _invalid("available dependency evidence is incomplete")
        return
    if dependency.status == "unobserved":
        if dependency.version is not None or dependency.origin is not None:
            _invalid("unobserved dependency cannot claim version or origin")
        if process is None:
            if dependency.error is not None or not pre_execution_environment_error:
                _invalid(
                    "not-attempted dependency requires a pre-execution environment error"
                )
        elif dependency.error is None or dependency.error.code != "check_dependency_unusable":
            _invalid("attempted unobserved dependency requires unusable evidence error")
        return
    expected_error = {
        "missing": "check_dependency_missing",
        "incompatible": "check_dependency_incompatible",
        "shadowed": "check_dependency_shadowed",
        "unusable": "check_dependency_unusable",
    }[dependency.status]
    if not _successful_process(process):
        _invalid("typed dependency status requires a successful probe")
    if dependency.error is None or dependency.error.code != expected_error:
        _invalid("dependency status contradicts its typed error")
    if dependency.status == "incompatible" and dependency.version is None:
        _invalid("incompatible dependency requires a known version")
    if dependency.status == "shadowed" and dependency.origin is None:
        _invalid("shadowed dependency requires a conflicting origin")


def _validate_check_result_v2(
    check: CheckResultV2,
    environment: RepositoryEnvironmentEvidence,
    dependency: DependencyEvidence,
    *,
    pytest_result: PytestResult | None,
    coverage_result: CoverageResult | None,
    coverage_dependency: DependencyEvidence | None,
    coverage_requested: bool,
) -> None:
    roles = tuple(process.role for process in check.processes)
    allowed_roles = {(), ("primary",), ("primary", "coverage_json")} if check.name == "pytest" else {(), ("primary",)}
    if roles not in allowed_roles:
        _invalid("check processes use an invalid attempted-command order")
    primary = check.processes[0] if check.processes else None
    start = check.start_evidence
    if (start is not None) != (check.execution_environment == "repository"):
        _invalid("repository execution attribution must match start evidence")
    if start is not None:
        if primary is None:
            _invalid("start evidence requires a primary process")
        primary = cast(ProcessResult, primary)
        expected_module = _expected_check_module_v2(
            check,
            coverage_dependency=coverage_dependency,
            coverage_requested=coverage_requested,
        )
        if start.check != check.name or start.module != expected_module:
            _invalid("start evidence does not match the Check invocation")
        if environment.python is None or start.python != environment.python:
            _invalid("start evidence Python does not match Repository Python")
        if start.arguments_sha256 != _process_argument_digest_v2(
            primary,
            check=check.name,
            module=expected_module,
        ):
            _invalid("start evidence argument digest does not match the primary process")

    pre_execution_error = (
        environment.error is not None
        and environment.error.code != "repository_state_changed"
    )
    synthesized_environment_unavailable = (
        check.error is not None
        and check.error.code == "repository_environment_unavailable"
        and not check.processes
        and start is None
        and check.execution_environment is None
        and check.analysis_python_authority is None
    )
    helper_identity_loss_after_preparation = _unsafe_after_preparation_v2(environment)
    if pre_execution_error and not helper_identity_loss_after_preparation:
        if (
            check.error is None
            or check.error.code != "repository_environment_unavailable"
            or check.processes
            or start is not None
            or check.execution_environment is not None
            or check.analysis_python_authority is not None
        ):
            _invalid("environment failure requires synthesized unavailable Checks")
        _validate_synthesized_pytest_v2(
            check,
            pytest_result,
            coverage_result,
            environment_failure=True,
            dependency=None,
            coverage_dependency=coverage_dependency,
            coverage_requested=coverage_requested,
        )
        return
    if helper_identity_loss_after_preparation and synthesized_environment_unavailable:
        _validate_synthesized_pytest_v2(
            check,
            pytest_result,
            coverage_result,
            environment_failure=True,
            dependency=None,
            coverage_dependency=coverage_dependency,
            coverage_requested=coverage_requested,
        )
        return
    elif (
        environment.error is not None
        and environment.error.code == "repository_state_changed"
        and check.error is not None
        and check.error.code == "repository_environment_unavailable"
    ):
        _invalid("post-run repository change must preserve actual Check evidence")
    elif (
        check.name == "pytest"
        and dependency.status != "available"
        and primary is None
        and check.error is not None
        and check.error.code == "cleanup_failed"
    ):
        _validate_pytest_check_v2(
            check,
            primary,
            pytest_result,
            dependency,
            coverage_result,
            coverage_dependency,
            coverage_requested=coverage_requested,
            helper_identity_loss_after_preparation=(
                helper_identity_loss_after_preparation
            ),
        )
        return
    elif dependency.status != "available":
        if (
            check.error is None
            or dependency.error is None
            or check.error.code != dependency.error.code
            or check.processes
            or start is not None
            or check.execution_environment is not None
            or check.analysis_python_authority is not None
        ):
            _invalid("dependency failure requires a matching synthesized Check")
        _validate_synthesized_pytest_v2(
            check,
            pytest_result,
            coverage_result,
            environment_failure=False,
            dependency=dependency,
            coverage_dependency=coverage_dependency,
            coverage_requested=coverage_requested,
        )
        return

    if check.name == "pytest":
        _validate_pytest_check_v2(
            check,
            primary,
            pytest_result,
            dependency,
            coverage_result,
            coverage_dependency,
            coverage_requested=coverage_requested,
            helper_identity_loss_after_preparation=(
                helper_identity_loss_after_preparation
            ),
        )
    else:
        _validate_ordinary_check_v2(check, primary)


def _expected_check_module_v2(
    check: CheckResultV2,
    *,
    coverage_dependency: DependencyEvidence | None,
    coverage_requested: bool,
) -> str:
    if (
        check.name == "pytest"
        and coverage_requested
        and coverage_dependency is not None
        and coverage_dependency.status == "available"
    ):
        return "coverage"
    return dependency_name_for_check(check.name)


def _validate_synthesized_pytest_v2(
    check: CheckResultV2,
    pytest_result: PytestResult | None,
    coverage_result: CoverageResult | None,
    *,
    environment_failure: bool,
    dependency: DependencyEvidence | None,
    coverage_dependency: DependencyEvidence | None,
    coverage_requested: bool,
) -> None:
    if check.name != "pytest":
        return
    if pytest_result is None:
        _invalid("pytest Check requires nested pytest evidence")
    expected_pytest_error = (
        "preflight_invalid"
        if environment_failure or dependency is None
        else {
            "missing": "module_unavailable",
            "incompatible": "unsupported_version",
            "shadowed": "preflight_invalid",
            "unusable": "preflight_invalid",
            "unobserved": "preflight_invalid",
        }[dependency.status]
    )
    result = cast(PytestResult, pytest_result)
    if (
        result.status != "error"
        or result.complete
        or result.exit_code is not None
        or result.evidence is not None
        or result.error is None
        or result.error.code != expected_pytest_error
    ):
        _invalid("synthesized pytest evidence contradicts preparation failure")
    expected_version = (
        dependency.version
        if dependency is not None and dependency.status == "incompatible"
        else None
    )
    if result.pytest_version != expected_version:
        _invalid("synthesized pytest version contradicts dependency evidence")
    if not coverage_requested:
        return
    if coverage_result is None:
        _invalid("requested Coverage requires nested evidence")
    coverage_result = cast(CoverageResult, coverage_result)
    coverage_error = coverage_result.error
    if (
        coverage_result.status != "error"
        or coverage_error is None
        or coverage_error.code != "preflight_invalid"
    ):
        _invalid("synthesized Coverage evidence contradicts pytest preparation")
    if coverage_dependency is not None and coverage_dependency.status == "available":
        if coverage_result.coverage_version not in {None, coverage_dependency.version}:
            _invalid("Coverage version contradicts dependency evidence")


def _validate_ordinary_check_v2(
    check: CheckResultV2,
    primary: ProcessResult | None,
) -> None:
    start = check.start_evidence
    cleanup_failed = check.error is not None and check.error.code == "cleanup_failed"
    if primary is None:
        if (
            check.status != "error"
            or check.error is None
            or check.error.code
            not in {
                "missing_primary_process",
                "cleanup_failed",
                "check_start_evidence_invalid",
            }
            or start is not None
            or check.execution_environment is not None
            or check.analysis_python_authority is not None
        ):
            _invalid("ordinary Check without a process requires a setup error")
        return
    if primary.outcome == "spawn_failed":
        expected_error = "cleanup_failed" if cleanup_failed else "spawn_failed"
        if (
            check.status != "error"
            or check.error is None
            or check.error.code != expected_error
            or start is not None
            or check.execution_environment is not None
            or check.analysis_python_authority is not None
        ):
            _invalid("spawn failure contradicts Check attribution")
        return
    if start is None:
        if (
            check.status != "error"
            or check.error is None
            or check.error.code
            != ("cleanup_failed" if cleanup_failed else "check_start_evidence_invalid")
            or check.analysis_python_authority is not None
        ):
            _invalid("unattributed ordinary Check requires invalid start evidence")
        return
    expected_authority = (
        check.name in {"ruff", "annotations", "annotations-fix", "ty"}
        and primary.outcome == "exited"
        and primary.exit_code in {0, 1}
    )
    if expected_authority != (check.analysis_python_authority is not None):
        _invalid("analysis Python authority contradicts static primary evidence")
    if cleanup_failed:
        if check.status != "error":
            _invalid("cleanup failure requires an error Check")
        return
    if primary.outcome == "signaled":
        if (
            check.status != "error"
            or check.error is None
            or check.error.code != "terminated_by_signal"
        ):
            _invalid("signaled primary contradicts Check error")
        return
    if primary.exit_code == 0:
        expected_status: CheckStatus = "passed"
        expected_error = None
    elif primary.exit_code == 1:
        expected_status = "failed"
        expected_error = None
    else:
        expected_status = "error"
        expected_error = "check_execution_failed"
    if check.status != expected_status or (
        (expected_error is None and check.error is not None)
        or (
            expected_error is not None
            and (check.error is None or check.error.code != expected_error)
        )
    ):
        _invalid("ordinary Check outcome contradicts trusted primary evidence")


def _validate_pytest_check_v2(
    check: CheckResultV2,
    primary: ProcessResult | None,
    result: PytestResult | None,
    dependency: DependencyEvidence,
    coverage: CoverageResult | None,
    coverage_dependency: DependencyEvidence | None,
    *,
    coverage_requested: bool,
    helper_identity_loss_after_preparation: bool,
) -> None:
    if result is None:
        _invalid("pytest Check requires nested pytest evidence")
    result = cast(PytestResult, result)
    if check.analysis_python_authority is not None:
        _invalid("pytest cannot claim static analysis Python authority")
    if (
        result.pytest_version is not None
        and result.pytest_version != dependency.version
    ):
        _invalid("pytest version must exactly match dependency evidence")
    roles = tuple(process.role for process in check.processes)
    expected_instrumented = (
        coverage_requested
        and coverage_dependency is not None
        and coverage_dependency.status == "available"
    )
    if not expected_instrumented and "coverage_json" in roles:
        _invalid("Coverage JSON requires authoritative instrumented pytest")
    cleanup_failed = check.error is not None and check.error.code == "cleanup_failed"
    if primary is None:
        if (
            check.status != "error"
            or check.error is None
            or check.error.code
            not in {
                "missing_primary_process",
                "cleanup_failed",
                "check_start_evidence_invalid",
                "pytest_evidence_error",
            }
            or check.start_evidence is not None
        ):
            _invalid("pytest without a primary process requires a setup error")
        if result.exit_code is not None or result.evidence is not None or result.complete:
            _invalid("pytest setup error cannot claim primary evidence")
        if dependency.status != "available":
            _validate_unavailable_pytest_cleanup_v2(result, dependency)
            _validate_unstarted_coverage_v2(
                check,
                result,
                coverage,
                coverage_dependency,
                requested=coverage_requested,
                pytest_available=False,
            )
            return
        marker_preparation = result.pytest_version == dependency.version
        error = check.error
        setup_error = "missing_primary_process" if error is None else error.code
        if error is None:
            _invalid("pytest setup error is unavailable")
        if marker_preparation and setup_error not in {
            "check_start_evidence_invalid",
            "cleanup_failed",
        }:
            _invalid("pytest marker preparation contradicts its Check error")
        if not marker_preparation and setup_error == "check_start_evidence_invalid":
            _invalid("pytest setup error contradicts its observed phase")
        if (
            result.status != "error"
            or result.pytest_version
            != (dependency.version if marker_preparation else None)
            or result.error is None
            or result.error.code != "not_started"
        ):
            _invalid("pytest setup failure requires not_started evidence")
        _validate_unstarted_coverage_v2(
            check,
            result,
            coverage,
            coverage_dependency,
            requested=coverage_requested,
            pytest_available=True,
        )
        return
    if primary.outcome == "spawn_failed":
        if result.pytest_version != dependency.version:
            _invalid("started pytest requires the dependency version")
        expected_error = "cleanup_failed" if cleanup_failed else "spawn_failed"
        if (
            check.status != "error"
            or check.error is None
            or check.error.code != expected_error
            or check.start_evidence is not None
            or result.error is None
            or result.error.code != "spawn_failed"
        ):
            _invalid("pytest spawn evidence is inconsistent")
        _validate_pytest_no_exit_error(result, "spawn_failed")
        _validate_coverage_correlation_v2(
            check,
            result,
            coverage,
            coverage_dependency,
            requested=coverage_requested,
            instrumented=expected_instrumented,
            helper_identity_loss_after_preparation=(
                helper_identity_loss_after_preparation
            ),
        )
        return
    if primary.outcome == "signaled":
        if result.pytest_version != dependency.version:
            _invalid("started pytest requires the dependency version")
        _validate_pytest_no_exit_error(result, "terminated_by_signal")
        if check.start_evidence is None:
            expected_error = "cleanup_failed" if cleanup_failed else "check_start_evidence_invalid"
        else:
            expected_error = "cleanup_failed" if cleanup_failed else "terminated_by_signal"
        if check.status != "error" or check.error is None or check.error.code != expected_error:
            _invalid("pytest signal evidence contradicts Check error")
        _validate_coverage_correlation_v2(
            check,
            result,
            coverage,
            coverage_dependency,
            requested=coverage_requested,
            instrumented=expected_instrumented,
            helper_identity_loss_after_preparation=(
                helper_identity_loss_after_preparation
            ),
        )
        return
    exit_code = cast(int, primary.exit_code)
    if result.pytest_version != dependency.version:
        _invalid("started pytest requires the dependency version")
    if result.exit_code != exit_code:
        _invalid("pytest exit_code must match primary process evidence")
    _validate_pytest_primary_outcome(result, exit_code)
    if check.start_evidence is None:
        expected_error = "cleanup_failed" if cleanup_failed else "check_start_evidence_invalid"
        if check.status != "error" or check.error is None or check.error.code != expected_error:
            _invalid("unattributed pytest requires invalid start evidence")
    elif cleanup_failed:
        if check.status != "error":
            _invalid("pytest cleanup failure requires error status")
    elif exit_code not in {0, 1, 5}:
        if (
            check.status != "error"
            or check.error is None
            or check.error.code != "check_execution_failed"
        ):
            _invalid("pytest launcher failure requires check_execution_failed")
    elif result.error is None or result.error.code == "session_incomplete":
        if check.status != result.status or check.error is not None:
            _invalid("pytest Check must exactly match completed pytest evidence")
    else:
        if (
            check.status != "error"
            or check.error is None
            or check.error.code != "pytest_evidence_error"
        ):
            _invalid("pytest artifact error contradicts Check evidence")
    _validate_coverage_correlation_v2(
        check,
        result,
        coverage,
        coverage_dependency,
        requested=coverage_requested,
        instrumented=expected_instrumented,
        helper_identity_loss_after_preparation=(
            helper_identity_loss_after_preparation
        ),
    )


def _validate_unavailable_pytest_cleanup_v2(
    result: PytestResult,
    dependency: DependencyEvidence,
) -> None:
    error = result.error
    expected_error = {
        "missing": "module_unavailable",
        "incompatible": "unsupported_version",
        "shadowed": "preflight_invalid",
        "unusable": "preflight_invalid",
        "unobserved": "preflight_invalid",
    }[dependency.status]
    expected_version = (
        dependency.version if dependency.status == "incompatible" else None
    )
    workspace_creation_failed = (
        result.pytest_version is None
        and error is not None
        and error.code == "not_started"
    )
    dependency_preflight_retained = (
        result.pytest_version == expected_version
        and error is not None
        and error.code == expected_error
    )
    if (
        result.status != "error"
        or not (workspace_creation_failed or dependency_preflight_retained)
    ):
        _invalid("unavailable pytest cleanup contradicts its observed setup phase")


def _validate_unstarted_coverage_v2(
    check: CheckResultV2,
    pytest_result: PytestResult,
    coverage: CoverageResult | None,
    dependency: DependencyEvidence | None,
    *,
    requested: bool,
    pytest_available: bool,
) -> None:
    if not requested:
        if coverage is not None or dependency is not None:
            _invalid("unrequested Coverage cannot have setup evidence")
        return
    if coverage is None or dependency is None:
        _invalid("requested Coverage requires setup evidence")
    coverage = cast(CoverageResult, coverage)
    dependency = cast(DependencyEvidence, dependency)
    setup_failure_owns_coverage = (
        not pytest_available
        or (
            check.error is not None
            and (
                check.error.code == "pytest_evidence_error"
                or check.error.code == "cleanup_failed"
                and pytest_result.pytest_version is None
            )
        )
    )
    if setup_failure_owns_coverage:
        expected_error = "preflight_invalid"
        expected_version = None
    elif dependency.status == "available":
        expected_error = "data_missing"
        expected_version = dependency.version
    else:
        expected_error = {
            "missing": "module_unavailable",
            "incompatible": "unsupported_version",
            "shadowed": "preflight_invalid",
            "unusable": "preflight_invalid",
            "unobserved": "preflight_invalid",
        }[dependency.status]
        expected_version = dependency.version if dependency.status == "incompatible" else None
    if (
        coverage.status != "error"
        or coverage.coverage_version != expected_version
        or coverage.error is None
        or coverage.error.code != expected_error
        or any(process.role == "coverage_json" for process in check.processes)
    ):
        _invalid("unstarted Coverage evidence contradicts dependency state")


def _validate_coverage_correlation_v2(
    check: CheckResultV2,
    pytest_result: PytestResult,
    coverage: CoverageResult | None,
    dependency: DependencyEvidence | None,
    *,
    requested: bool,
    instrumented: bool,
    helper_identity_loss_after_preparation: bool,
) -> None:
    coverage_json = (
        check.processes[-1]
        if check.processes and check.processes[-1].role == "coverage_json"
        else None
    )
    if not requested:
        if coverage is not None or dependency is not None or coverage_json is not None:
            _invalid("unrequested Coverage cannot have evidence")
        return
    if coverage is None or dependency is None:
        _invalid("requested Coverage requires dependency and result evidence")
    coverage = cast(CoverageResult, coverage)
    dependency = cast(DependencyEvidence, dependency)
    if coverage.coverage_version is not None and coverage.coverage_version != dependency.version:
        _invalid("Coverage version must exactly match dependency evidence")
    if dependency.status != "available":
        expected_error = {
            "missing": "module_unavailable",
            "incompatible": "unsupported_version",
            "shadowed": "preflight_invalid",
            "unusable": "preflight_invalid",
            "unobserved": "preflight_invalid",
        }[dependency.status]
        if (
            coverage.status != "error"
            or coverage.error is None
            or coverage.error.code != expected_error
            or coverage_json is not None
        ):
            _invalid("Coverage fallback contradicts dependency evidence")
        return
    if not instrumented:
        _invalid("available requested Coverage must instrument pytest")
    primary = check.processes[0]
    reserved_pytest_exit = (
        primary.outcome == "exited" and primary.exit_code in {2, 3, 4}
    )
    if primary.outcome != "exited" or primary.exit_code not in {0, 1, 2, 3, 4, 5}:
        if coverage_json is not None:
            _invalid("Coverage JSON cannot follow an incomplete pytest primary")
        if (
            coverage.status != "error"
            or coverage.error is None
            or coverage.error.code != "data_missing"
        ):
            _invalid("incomplete instrumented pytest requires Coverage error evidence")
        return
    if reserved_pytest_exit and (
        check.error is None or check.error.code != "check_execution_failed"
    ):
        _invalid("reserved pytest exit requires Check-level execution error")
    session_incomplete = (
        pytest_result.error is not None
        and pytest_result.error.code == "session_incomplete"
    )
    if (
        not pytest_result.complete
        and not session_incomplete
        and not reserved_pytest_exit
        and coverage_json is not None
    ):
        _invalid("Coverage JSON cannot follow incomplete pytest evidence")
    if coverage.status != "error":
        if coverage_json is None:
            _invalid("complete Coverage requires the JSON helper process")
        coverage_json = cast(ProcessResult, coverage_json)
        expected_exit = 2 if coverage.status == "failed" else 0
        if coverage_json.outcome != "exited" or coverage_json.exit_code != expected_exit:
            _invalid("Coverage JSON outcome contradicts complete Coverage evidence")
        return
    if coverage.error is None:
        _invalid("Coverage error result requires a typed error")
    coverage_error = cast(CoverageError, coverage.error)
    if coverage_json is None:
        if (
            helper_identity_loss_after_preparation
            and coverage_error.code == "generation_failed"
        ):
            return
        if coverage_error.code not in {
            "data_missing",
            "unexpected_parallel_data",
            "unsupported_parallelism",
        }:
            _invalid("post-primary Coverage error requires its attempted JSON helper")
        return
    expected_outcome = {
        "spawn_failed": "spawn_failed",
        "terminated_by_signal": "signaled",
    }.get(coverage_error.code)
    if expected_outcome is not None:
        if coverage_json.outcome != expected_outcome:
            _invalid("Coverage helper process contradicts Coverage error")
    elif coverage_error.code == "generation_failed":
        if (
            coverage_json.outcome != "exited"
            or coverage_json.exit_code is None
            or coverage_json.exit_code <= 0
        ):
            _invalid("Coverage generation failure requires positive helper exit")
    elif coverage_json.outcome != "exited":
        _invalid("Coverage artifact error requires an exited helper")
    elif coverage_error.code not in {
        "artifact_missing",
        "artifact_invalid",
        "unexpected_parallel_data",
    }:
        _invalid("Coverage helper evidence contradicts its error stage")


def _process_argument_digest_v2(
    process: ProcessResult,
    *,
    check: CheckName,
    module: str,
) -> str:
    try:
        separator = process.argv.index("--")
    except ValueError:
        _invalid("attributed primary command requires an argument separator")
        raise AssertionError("unreachable")
    header = process.argv[:separator]
    if _launcher_option_v2(header, "--check") != check:
        _invalid("primary launcher Check does not match start evidence")
    if _launcher_option_v2(header, "--module") != module:
        _invalid("primary launcher module does not match start evidence")
    arguments = process.argv[separator + 1 :]
    digest = hashlib.sha256()
    for argument in arguments:
        encoded = argument.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _launcher_option_v2(header: tuple[str, ...], option: str) -> str:
    positions = tuple(index for index, argument in enumerate(header) if argument == option)
    if len(positions) != 1 or positions[0] + 1 >= len(header):
        _invalid(f"primary launcher requires one {option} option")
    return header[positions[0] + 1]


def _successful_process(process: ProcessResult | None) -> bool:
    return (
        process is not None
        and process.outcome == "exited"
        and process.exit_code == 0
        and _complete_captured_output(process.stdout)
        and _complete_captured_output(process.stderr)
    )


def _zero_exit_process(process: ProcessResult | None) -> bool:
    return process is not None and process.outcome == "exited" and process.exit_code == 0


def _failed_process(process: ProcessResult | None) -> bool:
    return process is not None and (
        process.outcome in {"spawn_failed", "signaled"}
        or process.outcome == "exited"
        and process.exit_code is not None
        and process.exit_code != 0
    )


def _supported_repository_python_v2(python: PythonEvidence) -> bool:
    return (
        python.implementation == "cpython"
        and python.version[0] == 3
        and 10 <= python.version[1] <= 13
    )


def _complete_captured_output(captured: CapturedText) -> bool:
    return captured.captured and not captured.truncated and captured.omitted_bytes == 0


def _run_complete_v2(report: RunReportV2) -> bool:
    environment = report.repository_environment
    return (
        environment.error is None
        and environment.lock.status == "current"
        and all(dependency.status == "available" for dependency in environment.dependencies)
        and all(
            dependency.process is not None and _successful_process(dependency.process)
            for dependency in environment.dependencies
        )
        and all(check.status != "error" for check in report.checks)
        and (report.pytest is None or report.pytest.complete)
        and (report.coverage is None or report.coverage.evidence_complete)
    )


def build_planning_error_report(
    code: PlanningErrorCode,
    message: str,
    *,
    tool_environment: ToolEnvironmentObservation,
    hint: str | None = None,
) -> PlanningErrorReportV2:
    report = PlanningErrorReportV2(
        schema_version=2,
        kind="planning_error",
        overall_status="error",
        complete=False,
        tool_environment=_tool_environment_evidence(tool_environment),
        repository_environment=None,
        error=PlanningErrorV2(code=code, message=message, hint=hint),
    )
    validate_report_v2(report)
    return report


def build_run_report(
    project_root: Path,
    plan: RunPlan,
    execution: ExecutionResult,
) -> RunReportV2:
    observations = _match_repository_observations(plan.checks, execution.checks)
    pytest_observation = next(
        (
            observations.get(index)
            for index, planned in enumerate(plan.checks)
            if planned.name == "pytest"
        ),
        None,
    )
    pytest_result = (
        _build_pytest_result_v2(plan, execution.repository_environment, pytest_observation)
        if any(planned.name == "pytest" for planned in plan.checks)
        else None
    )
    coverage = _build_coverage_result_v2(
        project_root,
        plan,
        execution.repository_environment,
        pytest_result,
        pytest_observation,
    )
    checks = tuple(
        _build_check_result_v2(
            planned,
            observations.get(index),
            output_format=plan.output_format,
            pytest_result=pytest_result if planned.name == "pytest" else None,
        )
        for index, planned in enumerate(plan.checks)
    )
    report = RunReportV2(
        schema_version=2,
        kind="run",
        project_root=str(project_root.resolve()),
        mode=plan.mode,
        overall_status="passed",
        complete=True,
        tool_environment=_tool_environment_evidence(execution.tool_environment),
        repository_environment=_repository_environment_evidence(
            execution.repository_environment,
            output_format=plan.output_format,
        ),
        selection=Selection(
            checks=tuple(check.name for check in plan.checks),
            targets=plan.targets,
            test_shortcut=plan.test_shortcut,
            pytest_args=plan.pytest_args,
            planned_test_scope=plan.planned_test_scope,
            planned_coverage_scope=plan.planned_coverage_scope,
        ),
        checks=checks,
        pytest=pytest_result,
        coverage=coverage,
        advisories=(),
    )
    complete = _run_complete_v2(report)
    overall_status: OverallStatus = (
        "error"
        if not complete
        else "failed"
        if any(check.status == "failed" for check in checks)
        or (pytest_result is not None and pytest_result.status == "failed")
        or (coverage is not None and coverage.status == "failed")
        else "passed"
    )
    report = replace(report, complete=complete, overall_status=overall_status)
    report = replace(report, advisories=_build_advisories_v2(report))
    validate_report_v2(report)
    return report


def _tool_environment_evidence(
    observation: ToolEnvironmentObservation,
) -> ToolEnvironmentEvidence:
    return ToolEnvironmentEvidence(
        pyrepo_check_version=observation.pyrepo_check_version,
        python=_python_evidence(observation.python),
    )


def _python_evidence(observation: PythonObservation) -> PythonEvidence:
    return PythonEvidence(
        implementation=observation.implementation,
        version=observation.version,
        executable=str(observation.executable),
    )


def _repository_environment_evidence(
    observation: RepositoryEnvironmentObservation,
    *,
    output_format: OutputFormat,
) -> RepositoryEnvironmentEvidence:
    selection = observation.python_selection
    return RepositoryEnvironmentEvidence(
        manager="uv",
        manager_version=observation.manager_version,
        path=None if observation.path is None else str(observation.path),
        python_selection=RepositoryPythonSelectionEvidence(
            kind=selection.kind,
            request=selection.request if isinstance(selection, ExplicitRepositoryPython) else None,
        ),
        python=None if observation.python is None else _python_evidence(observation.python),
        lock=LockEvidence(str(observation.lock_path), observation.lock_status),
        dependency_selection="default",
        mutation_protection=observation.mutation_protection,
        dependencies=tuple(
            _dependency_evidence(dependency, output_format=output_format)
            for dependency in observation.dependencies
        ),
        processes=tuple(
            _process_evidence(process, output_format=output_format)
            for process in observation.processes
        ),
        error=(
            None
            if observation.error is None
            else EnvironmentError(
                observation.error.code,
                observation.error.message,
                observation.error.hint,
            )
        ),
    )


def _dependency_evidence(
    observation: DependencyObservation,
    *,
    output_format: OutputFormat,
) -> DependencyEvidence:
    return DependencyEvidence(
        name=observation.name,
        module=observation.module,
        required=observation.required,
        status=observation.status,
        version=observation.version,
        origin=observation.origin,
        process=(
            None
            if observation.process is None
            else _process_evidence(observation.process, output_format=output_format)
        ),
        error=_check_error_evidence(observation.error),
    )


def _check_error_evidence(error: CheckExecutionFailure | None) -> CheckErrorV2 | None:
    return None if error is None else CheckErrorV2(error.code, error.message, error.hint)


def _process_evidence(
    observation: ExecutedProcess,
    *,
    output_format: OutputFormat,
) -> ProcessResult:
    returncode = observation.returncode
    if returncode is None:
        outcome: ProcessOutcome = "spawn_failed"
        exit_code = None
        signal = None
        error_message = observation.spawn_error or "Process failed to spawn."
    elif returncode < 0:
        outcome = "signaled"
        exit_code = None
        signal = abs(returncode)
        error_message = f"Process terminated by signal {signal}."
    else:
        outcome = "exited"
        exit_code = returncode
        signal = None
        error_message = None
    return ProcessResult(
        role=_report_process_role(observation.role),
        argv=observation.command,
        cwd=str(observation.cwd.resolve()),
        outcome=outcome,
        exit_code=exit_code,
        signal=signal,
        duration_ms=observation.duration_ms,
        stdout=_captured_stream(observation.stdout, output_format=output_format),
        stderr=_captured_stream(observation.stderr, output_format=output_format),
        error_message=error_message,
    )


def _report_process_role(role: str) -> ProcessRole:
    if role in {
        "repository_git_root",
        "repository_venv_tracked",
        "repository_venv_ignored",
        "repository_tracked_snapshot",
    }:
        return "repository_safety"
    return cast(ProcessRole, role)


def _match_repository_observations(
    planned_checks: tuple[CheckInvocation, ...],
    executed_checks: tuple[RepositoryCheckObservation, ...],
) -> dict[int, RepositoryCheckObservation]:
    matched: dict[int, RepositoryCheckObservation] = {}
    next_index = 0
    for observation in executed_checks:
        match = next(
            (
                index
                for index in range(next_index, len(planned_checks))
                if planned_checks[index] == observation.invocation
            ),
            None,
        )
        if match is None:
            raise ReportingError(
                f"unexpected, mismatched, or out-of-order observation for "
                f"check {observation.invocation.name}"
            )
        matched[match] = observation
        next_index = match + 1
    if len(matched) != len(planned_checks):
        raise ReportingError("every planned Check requires one execution observation")
    return matched


def _pytest_evidence_input(observation: RepositoryCheckObservation) -> ExecutedCheck:
    return ExecutedCheck(
        planned=observation.invocation,
        processes=observation.processes,
        pytest=observation.pytest,
        coverage=observation.coverage,
    )


def _dependency_observation(
    environment: RepositoryEnvironmentObservation,
    name: str,
) -> DependencyObservation | None:
    return next((item for item in environment.dependencies if item.name == name), None)


def _build_pytest_result_v2(
    plan: RunPlan,
    environment: RepositoryEnvironmentObservation,
    observation: RepositoryCheckObservation | None,
) -> PytestResult:
    dependency = _dependency_observation(environment, "pytest")
    if observation is not None and observation.pytest is not None:
        return build_pytest_result(
            plan,
            _pytest_evidence_input(observation),
            dependency_version=None if dependency is None else dependency.version,
        )
    reasons = (
        ("planned_selector", "incomplete_session")
        if plan.planned_test_scope == "partial"
        else ("incomplete_session",)
    )
    environment_failed = (
        environment.error is not None
        and environment.error.code != "repository_state_changed"
    )
    return PytestResult(
        status="error",
        complete=False,
        scope="partial",
        scope_reasons=reasons,
        pytest_version=None,
        exit_code=None,
        evidence=None,
        error=PytestError(
            "preflight_invalid" if environment_failed else "not_started",
            "pytest did not run because the Repository Environment is unavailable."
            if environment_failed
            else "pytest execution was not observed",
        ),
    )


def _build_coverage_result_v2(
    project_root: Path,
    plan: RunPlan,
    environment: RepositoryEnvironmentObservation,
    pytest_result: PytestResult | None,
    observation: RepositoryCheckObservation | None,
) -> CoverageResult | None:
    if plan.planned_coverage_scope in {"not_requested", "unavailable"}:
        return None
    if pytest_result is None:
        raise ReportingError("planned coverage requires pytest selection")
    dependency = _dependency_observation(environment, "coverage")
    return build_coverage_result(
        project_root.resolve(),
        plan,
        pytest_result,
        None if observation is None else observation.coverage,
        dependency_version=None if dependency is None else dependency.version,
    )


def _build_check_result_v2(
    planned: CheckInvocation,
    observation: RepositoryCheckObservation | None,
    *,
    output_format: OutputFormat,
    pytest_result: PytestResult | None,
) -> CheckResultV2:
    if observation is None:
        raise ReportingError(f"selected Check {planned.name} has no execution observation")
    processes = tuple(
        _process_evidence(process, output_format=output_format)
        for process in observation.processes
    )
    error = _check_error_evidence(observation.error)
    if error is not None:
        status: CheckStatus = "error"
    elif planned.name == "pytest":
        if pytest_result is None:
            raise ReportingError("selected pytest requires nested evidence")
        status = pytest_result.status
        nested_error = pytest_result.error
        if (
            status == "error"
            and nested_error is not None
            and nested_error.code != "session_incomplete"
        ):
            error = CheckErrorV2(
                "pytest_evidence_error",
                nested_error.message,
                None,
            )
    else:
        primary = next((process for process in observation.processes if process.role == "primary"), None)
        if primary is None:
            raise ReportingError(f"selected Check {planned.name} has no primary process")
        status = "passed" if primary.returncode == 0 else "failed"
    start = observation.start
    return CheckResultV2(
        name=planned.name,
        status=status,
        execution_environment=observation.execution_environment,
        analysis_python_authority=(
            None
            if observation.analysis_python_authority is None
            else AnalysisPythonAuthorityEvidence(
                observation.analysis_python_authority.authority,
                observation.analysis_python_authority.pyrepo_check_override,
            )
        ),
        start_evidence=(
            None
            if start is None
            else CheckStartEvidence(
                start.schema_version,
                start.check,
                start.module,
                start.arguments_sha256,
                _python_evidence(start.python),
            )
        ),
        processes=processes,
        error=error,
    )


def render_terminal(
    report: AgentReportV2,
    *,
    include_environment: bool = True,
) -> str:
    """Render a validated report as a complete terminal-ready string."""
    validate_report_v2(report)
    if isinstance(report, PlanningErrorReportV2):
        lines = [report.error.message]
        if report.error.hint is not None:
            lines.append(f"Hint: {report.error.hint}")
        return "\n".join(lines) + "\n"

    run_context = report.mode.replace("_", " ")
    if not report.complete:
        run_context = f"{run_context}, incomplete"
    lines: list[str] = []
    environment = report.repository_environment
    if (
        include_environment
        and
        environment.lock.status == "current"
        and environment.python is not None
        and environment.manager_version is not None
    ):
        lines.append(
            format_terminal_environment_line(
                report.tool_environment.python.version,
                environment.python.version,
            ).removesuffix("\n")
        )
    lines.extend(("", f"==> pyrepo-check summary: {report.overall_status} ({run_context})"))
    if environment.error is not None:
        lines.append(f"    error: repository environment: {environment.error.message}")
        if environment.error.hint is not None:
            lines.append(f"    remediation: {environment.error.hint}")
    dependent_checks = {
        "ruff": ("ruff", "annotations", "annotations-fix"),
        "ty": ("ty",),
        "bandit": ("bandit",),
        "pytest": ("pytest",),
        "coverage": ("pytest",),
    }
    for dependency in environment.dependencies:
        if dependency.status == "available":
            continue
        installed = "not installed"
        if dependency.version is not None:
            installed = dependency.version
            if dependency.origin is not None:
                installed = f"{installed} from {dependency.origin}"
        checks = ", ".join(
            name for name in dependent_checks[dependency.name] if name in report.selection.checks
        )
        label = checks or dependency.name
        lines.append(
            f"    error: {label}: dependency {dependency.name} requires "
            f"{dependency.required}; installed: {installed}."
        )
        if dependency.error is not None and dependency.error.hint is not None:
            lines.append(f"    remediation: {dependency.error.hint}")
    if report.pytest is not None and (
        not report.pytest.complete or report.pytest.status == "error"
    ):
        if report.pytest.error is None:
            lines.append("    error: pytest evidence is incomplete.")
        else:
            lines.append(f"    error: pytest evidence: {report.pytest.error.message}")
    if report.coverage is not None and report.coverage.status == "error":
        if report.coverage.error is None:
            lines.append("    error: coverage evidence is incomplete.")
        else:
            lines.append(f"    error: coverage evidence: {report.coverage.error.message}")
    pytest_check = next((check for check in report.checks if check.name == "pytest"), None)
    for check in report.checks:
        if check.status != "error":
            continue
        error = check.error
        if error is not None and not _is_duplicate_pytest_result_error(check, report.pytest):
            lines.append(f"    error: {check.name}: {error.message}")
        _append_helper_diagnostics(lines, check)
    if (
        report.coverage is not None
        and report.coverage.status == "error"
        and pytest_check is not None
        and pytest_check.status != "error"
    ):
        _append_process_diagnostics(lines, pytest_check, frozenset(("coverage_json",)))
    for check in report.checks:
        if check.status != "failed":
            continue
        primary_exit = next(
            (
                process.exit_code
                for process in check.processes
                if process.role == "primary"
            ),
            None,
        )
        _append_failed_line(lines, check.name, primary_exit)
    if (
        report.pytest is not None
        and report.pytest.status == "failed"
        and (pytest_check is None or pytest_check.status != "failed")
    ):
        _append_failed_line(lines, "pytest", report.pytest.exit_code)
    if report.coverage is not None and report.coverage.status == "failed":
        _append_failed_line(lines, "coverage", None)
    if report.coverage is not None and report.coverage.totals is not None:
        _append_coverage_summary(lines, report.coverage)
    if report.pytest is not None and report.pytest.evidence is not None:
        for outcome in report.pytest.evidence.special_outcomes:
            reason = f" ({outcome.reason})" if outcome.reason else ""
            lines.append(f"    special: pytest {outcome.outcome}: {outcome.nodeid}{reason}")
        for slow_test in report.pytest.evidence.slowest:
            lines.append(f"    slow: pytest {slow_test.nodeid} ({slow_test.duration_ms} ms)")
    for advisory in report.advisories:
        if (
            advisory.code == "coverage_threshold_not_applied"
            and report.coverage is not None
            and report.coverage.totals is not None
        ):
            continue
        lines.append(f"    advisory: {advisory.message}")
    passed_checks = [check.name for check in report.checks if check.status == "passed"]
    if passed_checks:
        lines.append(f"    passed: {', '.join(passed_checks)}")
    return "\n".join(lines) + "\n"


def _append_failed_line(lines: list[str], name: str, exit_code: int | None) -> None:
    if exit_code is None:
        lines.append(f"    failed: {name}")
    else:
        lines.append(f"    failed: {name} (exit {exit_code})")


def _append_coverage_summary(lines: list[str], coverage: CoverageResult) -> None:
    totals = coverage.totals
    if totals is None:
        return

    lines.append(
        f"    coverage: {coverage.status} ({coverage.scope}); "
        f"{_coverage_threshold_summary(coverage)}"
    )
    files_with_gaps = tuple(
        file for file in coverage.files if file.statements.missing or file.branches.missing
    )
    focus_files = files_with_gaps[:_TERMINAL_COVERAGE_FILE_LIMIT]
    rows = [
        (
            file.path,
            file.statements.covered + file.statements.missing,
            file.statements.missing,
            file.branches.covered + file.branches.missing,
            file.branches.missing,
            _coverage_percentage(
                file.statements.covered + file.branches.covered,
                file.statements.missing + file.branches.missing,
            ),
        )
        for file in focus_files
    ]
    total_row = (
        "TOTAL",
        totals.statements.covered + totals.statements.missing,
        totals.statements.missing,
        totals.branches.covered + totals.branches.missing,
        totals.branches.missing,
        _coverage_percentage(
            totals.statements.covered + totals.branches.covered,
            totals.statements.missing + totals.branches.missing,
        ),
    )
    _append_coverage_table(lines, rows, total_row)
    omitted = len(files_with_gaps) - len(focus_files)
    if omitted:
        noun = "file" if omitted == 1 else "files"
        lines.append(f"      ... {omitted} more {noun} with gaps")
    if files_with_gaps:
        lines.append("    coverage details: use --format json for exact missing lines and branches")


def _coverage_threshold_summary(coverage: CoverageResult) -> str:
    threshold = coverage.threshold
    if not threshold.configured or threshold.value is None:
        return "no minimum configured"
    value = f"{threshold.value:g}%"
    if not threshold.evaluated:
        return f"minimum {value} not applied"
    outcome = "passed" if threshold.passed else "failed"
    return f"minimum {value} {outcome}"


def _append_coverage_table(
    lines: list[str],
    rows: list[tuple[str, int, int, int, int, str]],
    total_row: tuple[str, int, int, int, int, str],
) -> None:
    display_rows = [(_coverage_display_name(row[0]), *row[1:]) for row in rows]
    name_width = max(len("Name"), *(len(row[0]) for row in (*display_rows, total_row)))
    header = (
        f"      {'Name':<{name_width}}  {'Stmts':>5}  {'Miss':>4}  "
        f"{'Branch':>6}  {'BrMiss':>6}  {'Cover':>7}"
    )
    separator = "      " + "-" * (len(header) - 6)
    lines.extend(("    coverage:", header, separator))
    for name, statements, missing, branches, branch_missing, cover in display_rows:
        lines.append(
            f"      {name:<{name_width}}  {statements:>5}  {missing:>4}  "
            f"{branches:>6}  {branch_missing:>6}  {cover:>7}"
        )
    if rows:
        lines.append(separator)
    name, statements, missing, branches, branch_missing, cover = total_row
    lines.append(
        f"      {name:<{name_width}}  {statements:>5}  {missing:>4}  "
        f"{branches:>6}  {branch_missing:>6}  {cover:>7}"
    )


def _coverage_display_name(path: str) -> str:
    if len(path) <= _TERMINAL_COVERAGE_NAME_WIDTH:
        return path
    return "..." + path[-(_TERMINAL_COVERAGE_NAME_WIDTH - 3) :]


def _coverage_percentage(covered: int, missing: int) -> str:
    total = covered + missing
    percentage = 100.0 if total == 0 else covered * 100 / total
    if 0 < percentage < 0.01:
        percentage = 0.01
    elif 99.99 < percentage < 100:
        percentage = 99.99
    else:
        percentage = round(percentage, 2)
    return f"{percentage:.2f}%"


def _is_duplicate_pytest_result_error(
    check: CheckResultV2, pytest_result: PytestResult | None
) -> bool:
    if (
        check.name != "pytest"
        or check.error is None
        or pytest_result is None
        or pytest_result.error is None
    ):
        return False
    return check.error.message == pytest_result.error.message


def _append_helper_diagnostics(lines: list[str], check: CheckResultV2) -> None:
    _append_process_diagnostics(
        lines,
        check,
        frozenset(("pytest_preflight", "coverage_preflight", "coverage_json")),
    )


def _append_process_diagnostics(
    lines: list[str],
    check: CheckResultV2,
    roles: frozenset[ProcessRole],
) -> None:
    for process in check.processes:
        if process.role not in roles:
            continue
        for stream_name, captured in (("stdout", process.stdout), ("stderr", process.stderr)):
            if not captured.captured or not captured.text:
                continue
            for line in captured.text.rstrip("\n").splitlines() or [captured.text]:
                lines.append(f"    diagnostic: {check.name} {process.role} {stream_name}: {line}")


def serialize_json(report: AgentReportV2) -> bytes:
    """Serialize a validated report as one compact UTF-8 JSON document."""
    validate_report_v2(report)
    payload = asdict(report)
    text = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return text.encode("utf-8") + b"\n"


def select_exit_code(report: AgentReportV2) -> Literal[0, 1, 2]:
    """Select stable public status from complete report evidence."""
    validate_report_v2(report)
    if isinstance(report, PlanningErrorReportV2):
        return 2
    if report.overall_status == "error":
        return 2
    return 1 if report.overall_status == "failed" else 0


def _validate_pytest_no_exit_error(result: PytestResult, expected_code: str) -> None:
    if (
        result.status != "error"
        or result.complete
        or result.exit_code is not None
        or result.evidence is not None
        or result.error is None
        or result.error.code != expected_code
    ):
        _invalid("pytest process failure contradicts pytest result")


def _validate_pytest_primary_outcome(result: PytestResult, exit_code: int) -> None:
    if result.error is None:
        if exit_code == 0:
            expected_status: CheckStatus = "passed"
        elif exit_code in {1, 5}:
            expected_status = "failed"
        else:
            _invalid("pytest exit requires an error result")
            return
        if result.status != expected_status:
            _invalid("pytest status contradicts the primary exit_code")
        if exit_code == 5 and result.complete:
            evidence = result.evidence
            if evidence is None or evidence.collected != 0:
                _invalid("complete pytest exit 5 requires zero collected tests")
        return

    code = result.error.code
    if code == "session_incomplete":
        if (
            exit_code != 1
            or result.status != "failed"
            or result.complete
            or result.evidence is None
        ):
            _invalid("session_incomplete must retain failed pytest exit 1 evidence")
        return
    expected_exit_codes = {
        "interrupted": {2},
        "internal_error": {3},
        "usage_error": {4},
    }
    if code in expected_exit_codes:
        if (
            exit_code not in expected_exit_codes[code]
            or result.status != "error"
            or result.complete
            or result.evidence is None
        ):
            _invalid("pytest exit-matrix error contradicts process evidence")
        return
    if code == "unknown_exit_code":
        if (
            exit_code in {0, 1, 2, 3, 4, 5}
            or result.status != "error"
            or result.complete
            or result.evidence is None
        ):
            _invalid("unknown pytest exit_code contradicts process evidence")
        return
    if code in _PYTEST_ARTIFACT_ERROR_CODES:
        if result.status != "error" or result.complete or result.evidence is not None:
            _invalid("invalid pytest artifact must produce incomplete error evidence")
        return
    _invalid("pytest primary exit contradicts pytest error")


def _validate_pytest_result(result: PytestResult, selection: Selection) -> None:
    if not isinstance(result, PytestResult):
        _invalid("pytest must be a PytestResult")
    if result.status not in _CHECK_STATUSES:
        _invalid("unknown pytest status")
    if type(result.complete) is not bool:
        _invalid("pytest complete must be boolean")
    if result.scope not in {"partial", "complete"}:
        _invalid("unknown pytest scope")
    if not isinstance(result.scope_reasons, tuple) or any(
        not isinstance(reason, str) or reason not in _PYTEST_SCOPE_REASONS
        for reason in result.scope_reasons
    ):
        _invalid("unknown pytest scope reason")
    if (
        tuple(reason for reason in _PYTEST_SCOPE_REASONS if reason in result.scope_reasons)
        != result.scope_reasons
    ):
        _invalid("pytest scope reasons must use fixed unique order")
    if result.scope != ("complete" if not result.scope_reasons else "partial"):
        _invalid("pytest scope contradicts scope reasons")
    if result.pytest_version is not None and not isinstance(result.pytest_version, str):
        _invalid("pytest_version must be null or a string")
    if result.exit_code is not None:
        _validate_exact_int(result.exit_code, "pytest exit_code")
        if result.exit_code < 0:
            _invalid("pytest exit_code must be non-negative")
    if result.error is not None:
        if not isinstance(result.error, PytestError):
            _invalid("pytest error must be a PytestError")
        if result.error.code not in _PYTEST_ERROR_CODES:
            _invalid("unknown pytest error code")
        if not isinstance(result.error.message, str):
            _invalid("pytest error message must be a string")
    if result.status == "error" and result.error is None:
        _invalid("error pytest result requires an error")
    if (
        result.status != "error"
        and result.error is not None
        and result.error.code != "session_incomplete"
    ):
        _invalid("non-error pytest result has an invalid error")
    if result.complete and (result.status == "error" or result.error is not None):
        _invalid("complete pytest result cannot contain an error")
    if result.complete and result.evidence is None:
        _invalid("complete pytest result requires evidence")
    if ("planned_selector" in result.scope_reasons) != (selection.planned_test_scope == "partial"):
        _invalid("pytest planned_selector must match planned test scope")
    if ("incomplete_session" in result.scope_reasons) != (not result.complete):
        _invalid("pytest incomplete_session must match completeness")
    if (
        result.error is not None
        and result.error.code == "session_incomplete"
        and (result.status != "failed" or result.complete or result.evidence is None)
    ):
        _invalid("session_incomplete requires incomplete failed evidence")
    _validate_pytest_evidence(result.evidence, complete=result.complete)
    if result.evidence is None:
        if result.complete or result.error is None or result.status != "error":
            _invalid("null pytest evidence requires an incomplete error result")
        if any(
            reason not in {"planned_selector", "incomplete_session"}
            for reason in result.scope_reasons
        ):
            _invalid("null pytest evidence cannot claim artifact-derived scope reasons")
    else:
        if ("deselected_tests" in result.scope_reasons) != (result.evidence.deselected > 0):
            _invalid("pytest deselected_tests must match evidence")


def _validate_pytest_evidence(evidence: PytestEvidence | None, *, complete: bool) -> None:
    if evidence is None:
        return
    if not isinstance(evidence, PytestEvidence):
        _invalid("pytest evidence must be a PytestEvidence")
    if not isinstance(evidence.counts, PytestCounts):
        _invalid("pytest counts must be a PytestCounts")
    for value, field in (
        (evidence.collected, "pytest collected"),
        (evidence.deselected, "pytest deselected"),
    ):
        _validate_exact_int(value, field)
        if value < 0:
            _invalid(f"{field} must be non-negative")
    if not isinstance(evidence.effective_args, tuple) or any(
        not isinstance(arg, str) for arg in evidence.effective_args
    ):
        _invalid("pytest effective_args must be a tuple of strings")
    for value, field in (
        (evidence.counts.passed, "pytest passed"),
        (evidence.counts.failed, "pytest failed"),
        (evidence.counts.errors, "pytest errors"),
        (evidence.counts.skipped, "pytest skipped"),
        (evidence.counts.xfailed, "pytest xfailed"),
        (evidence.counts.xpassed, "pytest xpassed"),
    ):
        _validate_exact_int(value, field)
        if value < 0:
            _invalid(f"{field} must be non-negative")
    if not isinstance(evidence.collection_errors, tuple) or not isinstance(
        evidence.collection_skips, tuple
    ):
        _invalid("pytest collection issues must be tuples")
    _validate_issue_order(evidence.collection_errors, "collection errors")
    _validate_issue_order(evidence.collection_skips, "collection skips")
    if not isinstance(evidence.slowest, tuple) or not isinstance(evidence.special_outcomes, tuple):
        _invalid("pytest result lists must be tuples")
    for item in evidence.slowest:
        if not isinstance(item, SlowTest):
            _invalid("pytest slow test must be a SlowTest")
        if not isinstance(item.nodeid, str):
            _invalid("slow test nodeid must be a string")
        _validate_exact_int(item.duration_ms, "slow test duration_ms")
        if item.duration_ms < 0:
            _invalid("slow test duration_ms must be non-negative")
    if len({item.nodeid for item in evidence.slowest}) != len(evidence.slowest):
        _invalid("pytest slow tests must use unique nodeids")
    if (
        tuple(sorted(evidence.slowest, key=lambda item: (-item.duration_ms, item.nodeid)))
        != evidence.slowest
        or len(evidence.slowest) > 10
    ):
        _invalid("pytest slowest tests must use deterministic order")
    for item in evidence.special_outcomes:
        if not isinstance(item, SpecialTestOutcome):
            _invalid("pytest special outcome must be a SpecialTestOutcome")
        if item.outcome not in {"skipped", "xfailed", "xpassed"} or not isinstance(
            item.nodeid, str
        ):
            _invalid("invalid pytest special outcome")
        if item.reason is not None and not isinstance(item.reason, str):
            _invalid("pytest special reason must be null or a string")
        if item.strict is not None and type(item.strict) is not bool:
            _invalid("pytest special strict must be null or a boolean")
        if type(item.affects_exit) is not bool:
            _invalid("pytest special affects_exit must be boolean")
        if item.outcome in {"skipped", "xfailed"} and (
            item.strict is not None or item.affects_exit
        ):
            _invalid("skip and xfail special outcomes cannot be strict or affect exit")
        if item.outcome == "xpassed" and (
            item.strict is None or item.affects_exit is not item.strict
        ):
            _invalid("xpass special outcomes must match strict exit effect")
        _validate_exact_int(item.duration_ms, "pytest special duration_ms")
        if item.duration_ms < 0:
            _invalid("pytest special duration_ms must be non-negative")
    if len({item.nodeid for item in evidence.special_outcomes}) != len(evidence.special_outcomes):
        _invalid("pytest special outcomes must use unique nodeids")
    if (
        tuple(sorted(evidence.special_outcomes, key=lambda item: item.nodeid))
        != evidence.special_outcomes
    ):
        _invalid("pytest special outcomes must use nodeid order")

    count_total = (
        evidence.counts.passed
        + evidence.counts.failed
        + evidence.counts.errors
        + evidence.counts.skipped
        + evidence.counts.xfailed
        + evidence.counts.xpassed
    )
    if count_total > evidence.collected:
        _invalid("pytest outcome counts cannot exceed collected")
    if len(evidence.slowest) != min(10, count_total):
        _invalid("pytest slow-test cardinality must match terminal outcomes")
    if len(evidence.special_outcomes) != (
        evidence.counts.skipped + evidence.counts.xfailed + evidence.counts.xpassed
    ):
        _invalid("pytest special-outcome cardinality must match counts")
    slow_durations = {item.nodeid: item.duration_ms for item in evidence.slowest}
    for item in evidence.special_outcomes:
        if item.nodeid in slow_durations and slow_durations[item.nodeid] != item.duration_ms:
            _invalid("pytest special duration must match the slow-test duration")
    if complete:
        if evidence.collection_errors:
            _invalid("complete pytest evidence cannot contain collection errors")
        if count_total != evidence.collected:
            _invalid("complete pytest counts must equal collected")


def _validate_issue_order(issues: tuple[CollectionIssue, ...], field: str) -> None:
    for issue in issues:
        if not isinstance(issue, CollectionIssue):
            _invalid(f"pytest {field} must contain CollectionIssue values")
        if not isinstance(issue.nodeid, str) or not isinstance(issue.message, str):
            _invalid(f"pytest {field} require strings")
    pairs = tuple((issue.nodeid, issue.message) for issue in issues)
    if len(set(pairs)) != len(pairs):
        _invalid(f"pytest {field} must be unique")
    if tuple(sorted(issues, key=lambda item: (item.nodeid, item.message))) != issues:
        _invalid(f"pytest {field} must use deterministic order")


def _invalid(message: str) -> None:
    raise ReportingError(f"invalid report: {message}")


def _validate_exact_int(value: object, field: str) -> int:
    if type(value) is not int:
        _invalid(f"{field} must be an integer")
    return cast(int, value)


def capture_text(raw: CapturedBytes | bytes) -> CapturedText:
    if isinstance(raw, bytes):
        retained = raw[-CAPTURE_LIMIT_BYTES:]
        captured = CapturedBytes(retained, len(raw) - len(retained))
    else:
        captured = raw
    text = strip_terminal_sequences(captured.tail.decode("utf-8", errors="replace"))
    return CapturedText(
        True,
        text,
        captured.omitted_bytes > 0,
        captured.omitted_bytes,
    )


def strip_terminal_sequences(text: str) -> str:
    pieces: list[str] = []
    cursor = 0
    while True:
        osc_start = text.find("\x1b]", cursor)
        if osc_start == -1:
            pieces.append(_CSI_PATTERN.sub("", text[cursor:]))
            return "".join(pieces)

        pieces.append(_CSI_PATTERN.sub("", text[cursor:osc_start]))
        bel_end = text.find("\x07", osc_start + 2)
        st_start = text.find("\x1b\\", osc_start + 2)
        terminators = [end for end in (bel_end, st_start) if end != -1]
        if not terminators:
            # An unfinished OSC consumes its remaining tail; preserve it intact,
            # including escape-like diagnostic bytes nested within that tail.
            pieces.append(text[osc_start:])
            return "".join(pieces)
        terminator_start = min(terminators)
        cursor = terminator_start + (1 if terminator_start == bel_end else 2)


def _build_advisories_v2(report: RunReportV2) -> tuple[Advisory, ...]:
    advisories: list[Advisory] = []
    if report.pytest is not None and report.pytest.evidence is not None:
        for outcome in report.pytest.evidence.special_outcomes:
            if outcome.reason is None or outcome.reason == "":
                advisories.append(
                    Advisory(
                        "missing_test_reason",
                        f"pytest {outcome.outcome} has no reason: {outcome.nodeid}.",
                        None,
                    )
                )
    if report.selection.planned_coverage_scope == "unavailable":
        advisories.append(
            Advisory(
                "coverage_not_configured",
                "Coverage guidance is unavailable because native Coverage.py configuration is absent.",
                None,
            )
        )
    if (
        report.coverage is not None
        and report.coverage.threshold.configured
        and not report.coverage.threshold.evaluated
    ):
        advisories.append(
            Advisory(
                "coverage_threshold_not_applied",
                "Configured coverage threshold was not applied to this run.",
                None,
            )
        )
    seen: set[ProcessResult] = set()

    def append_process(process: ProcessResult, label: str) -> None:
        if process in seen:
            return
        seen.add(process)
        for stream_name, captured in (
            ("stdout", process.stdout),
            ("stderr", process.stderr),
        ):
            if not captured.truncated:
                continue
            advisories.append(
                Advisory(
                    "output_truncated",
                    (
                        f"{label} ({process.role}) {stream_name} omitted "
                        f"{captured.omitted_bytes} byte(s); only the final "
                        f"{CAPTURE_LIMIT_BYTES} bytes are included."
                    ),
                    None,
                )
            )

    for index, process in enumerate(report.repository_environment.processes, start=1):
        append_process(process, f"repository environment process {index}")
    for dependency in report.repository_environment.dependencies:
        if dependency.process is not None:
            append_process(dependency.process, f"dependency {dependency.name} process")
    for check in report.checks:
        for index, process in enumerate(check.processes, start=1):
            append_process(process, f"{check.name} process {index}")
    unique = {(advisory.code, advisory.message): advisory for advisory in advisories}
    return tuple(sorted(unique.values(), key=lambda advisory: (advisory.code, advisory.message)))


def _captured_stream(
    raw: CapturedBytes | None,
    *,
    output_format: OutputFormat,
) -> CapturedText:
    if raw is None:
        return CapturedText(output_format == "json", "", False, 0)
    return capture_text(raw)
