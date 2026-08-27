from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Literal, cast

from pyrepo_check.config import TEST_SHORTCUT_NAME_PATTERN
from pyrepo_check.coverage_evidence import (
    CoverageCounts,
    CoverageError,
    CoverageResult,
    build_coverage_result,
    coverage_gate_policy_for_context,
    is_supported_coverage_version,
    validate_coverage_result,
)
from pyrepo_check.execution import (
    CAPTURE_LIMIT_BYTES,
    CapturedBytes,
    ExecutedCheck,
    ExecutedProcess,
    ExecutionResult,
)
from pyrepo_check.planning import (
    CheckName,
    OutputFormat,
    CheckInvocation,
    PlannedTestScope,
    PlanningErrorCode,
    RunMode,
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
from pyrepo_check.reporting_schema import (
    Advisory,
    AdvisoryCode as AdvisoryCode,
    AgentReportV2,
    CapturedText,
    CheckErrorCode,
    CheckResultV2,
    CheckStatus,
    DependencyEvidence,
    OverallStatus,
    PlanningErrorReportV2,
    PlannedCoverageScope,
    ProcessOutcome,
    ProcessResult,
    ProcessRole,
    ReportKind as ReportKind,
    RepositoryEnvironmentEvidence,
    ReportingError,
    RunReportV2,
    Selection,
    validate_report_structure_v2,
)
from pyrepo_check.repository_environment import (
    SUPPORTED_DEPENDENCIES,
    dependency_version_supported,
)


_CSI_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CHECK_NAMES = frozenset(("ruff", "annotations", "annotations-fix", "ty", "bandit", "pytest"))
_B_CANONICAL_CHECK_ORDER: tuple[CheckName, ...] = (
    "ruff",
    "annotations",
    "ty",
    "bandit",
    "pytest",
    "annotations-fix",
)
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
_PLANNED_TEST_SCOPES = frozenset(("not_selected", "partial", "complete"))
_PLANNED_COVERAGE_SCOPES = frozenset(("not_requested", "unavailable", "partial", "complete"))
_PROCESS_ROLES = frozenset(("primary", "pytest_preflight", "coverage_preflight", "coverage_json"))
_PROCESS_OUTCOMES = frozenset(("exited", "signaled", "spawn_failed"))
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


@dataclass(frozen=True)
class PlanningError:
    code: PlanningErrorCode
    message: str
    hint: str | None


@dataclass(frozen=True)
class PlanningErrorReportV1:
    schema_version: int
    kind: Literal["planning_error"]
    overall_status: Literal["error"]
    complete: Literal[False]
    error: PlanningError


@dataclass(frozen=True)
class CheckError:
    code: CheckErrorCode
    message: str


@dataclass(frozen=True)
class CheckResult:
    name: CheckName
    status: CheckStatus
    processes: tuple[ProcessResult, ...]
    error: CheckError | None


@dataclass(frozen=True)
class RunReportV1:
    schema_version: int
    kind: Literal["run"]
    project_root: str
    mode: RunMode
    overall_status: OverallStatus
    complete: bool
    selection: Selection
    checks: tuple[CheckResult, ...]
    pytest: PytestResult | None
    coverage: CoverageResult | None
    advisories: tuple[Advisory, ...]


AgentReportV1 = PlanningErrorReportV1 | RunReportV1


def validate_report_v2(report: AgentReportV2) -> None:
    validate_report_structure_v2(report)
    if isinstance(report, PlanningErrorReportV2):
        return
    _validate_run_report_v2(report)


def _validate_run_report_v2(report: RunReportV2) -> None:
    pytest_selected = _validate_selection(report.selection)
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
    for dependency in environment.dependencies:
        _validate_dependency_evidence_v2(dependency, environment_error=environment.error is not None)

    dependencies = {dependency.name: dependency for dependency in environment.dependencies}
    for check in report.checks:
        dependency = dependencies[_dependency_for_check_v2(check.name)]
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
        expected_final_argv = (
            "git",
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
            or environment.processes[-1].argv != expected_final_argv
            or environment.processes[-1].cwd != project_root
        ):
            _invalid("tracked_files requires canonical successful final safety evidence")


def _validate_pre_execution_stage_v2(
    environment: RepositoryEnvironmentEvidence,
) -> None:
    error = environment.error
    if error is None or error.code == "repository_state_changed":
        return
    if any(
        dependency.status != "unobserved"
        or dependency.process is not None
        or dependency.error is not None
        for dependency in environment.dependencies
    ):
        _invalid("environment-wide failure cannot claim dependency probe evidence")
    core_roles = tuple(
        process.role
        for process in environment.processes
        if process.role != "repository_safety"
    )
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
            or environment.lock.status != "unverified"
            or environment.manager_version is not None
            or environment.path is not None
            or environment.python is not None
        ):
            _invalid("uv_unavailable contradicts preparation stage evidence")
        return
    if error.code == "repository_environment_failed" and (
        core_roles != ("uv_version", "environment_probe")
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
            and environment.lock.status == "unverified"
            and environment.manager_version is None
            and environment.path is None
            and environment.python is None
        )
        probe_evidence_failure = (
            core_roles == ("uv_version", "environment_probe")
            and environment.manager_version is not None
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


def _expected_dependency_names_v2(selection: Selection) -> tuple[str, ...]:
    names: list[str] = []
    for check in selection.checks:
        name = _dependency_for_check_v2(check)
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


def _dependency_for_check_v2(
    check: CheckName,
) -> Literal["ruff", "ty", "bandit", "pytest"]:
    if check in {"ruff", "annotations", "annotations-fix"}:
        return "ruff"
    return check


def _validate_dependency_evidence_v2(
    dependency: DependencyEvidence,
    *,
    environment_error: bool,
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
            if dependency.error is not None or not environment_error:
                _invalid("not-attempted dependency requires an environment error")
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
    if pre_execution_error:
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
    elif (
        environment.error is not None
        and environment.error.code == "repository_state_changed"
        and (
            not check.processes
            or check.error is not None
            and check.error.code == "repository_environment_unavailable"
        )
    ):
        _invalid("post-run repository change must preserve actual Check evidence")
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
    return _dependency_for_check_v2(check.name)


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
        marker_preparation = (
            check.error is not None
            and check.error.code == "check_start_evidence_invalid"
        )
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
            coverage,
            coverage_dependency,
            requested=coverage_requested,
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
    )


def _validate_unstarted_coverage_v2(
    check: CheckResultV2,
    coverage: CoverageResult | None,
    dependency: DependencyEvidence | None,
    *,
    requested: bool,
) -> None:
    if not requested:
        if coverage is not None or dependency is not None:
            _invalid("unrequested Coverage cannot have setup evidence")
        return
    if coverage is None or dependency is None:
        _invalid("requested Coverage requires setup evidence")
    coverage = cast(CoverageResult, coverage)
    dependency = cast(DependencyEvidence, dependency)
    expected_error = (
        "preflight_invalid"
        if dependency.status == "available"
        else {
            "missing": "module_unavailable",
            "incompatible": "unsupported_version",
            "shadowed": "preflight_invalid",
            "unusable": "preflight_invalid",
            "unobserved": "preflight_invalid",
        }[dependency.status]
    )
    if (
        coverage.status != "error"
        or coverage.coverage_version
        != (dependency.version if dependency.status == "incompatible" else None)
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
    if primary.outcome != "exited" or primary.exit_code not in {0, 1, 5}:
        if coverage_json is not None:
            _invalid("Coverage JSON cannot follow an incomplete pytest primary")
        if (
            coverage.status != "error"
            or coverage.error is None
            or coverage.error.code != "data_missing"
        ):
            _invalid("incomplete instrumented pytest requires Coverage error evidence")
        return
    session_incomplete = (
        pytest_result.error is not None
        and pytest_result.error.code == "session_incomplete"
    )
    if not pytest_result.complete and not session_incomplete and coverage_json is not None:
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
        if coverage_error.code not in {"data_missing", "unsupported_parallelism"}:
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
    hint: str | None = None,
) -> PlanningErrorReportV1:
    return PlanningErrorReportV1(
        schema_version=1,
        kind="planning_error",
        overall_status="error",
        complete=False,
        error=PlanningError(code=code, message=message, hint=hint),
    )


def build_run_report(
    project_root: Path,
    plan: RunPlan,
    execution: ExecutionResult,
) -> RunReportV1:
    observations = _match_observations(plan.checks, execution.checks)
    pytest_observation = next(
        (
            observations.get(index)
            for index, planned in enumerate(plan.checks)
            if planned.name == "pytest"
        ),
        None,
    )
    pytest_result = (
        _build_pytest_result(plan, pytest_observation)
        if any(planned.name == "pytest" for planned in plan.checks)
        else None
    )
    coverage = _build_coverage_result(project_root, plan, pytest_result, pytest_observation)
    checks = tuple(
        _build_check_result(
            planned,
            observations.get(index),
            output_format=plan.output_format,
            pytest_result=pytest_result if planned.name == "pytest" else None,
        )
        for index, planned in enumerate(plan.checks)
    )
    statuses = {check.status for check in checks}
    if (
        not _run_complete(checks, pytest_result, coverage)
        or "error" in statuses
        or (pytest_result is not None and pytest_result.status == "error")
        or (coverage is not None and coverage.status == "error")
    ):
        overall_status: OverallStatus = "error"
    elif (
        "failed" in statuses
        or (pytest_result is not None and pytest_result.status == "failed")
        or (coverage is not None and coverage.status == "failed")
    ):
        overall_status = "failed"
    else:
        overall_status = "passed"

    return RunReportV1(
        schema_version=1,
        kind="run",
        project_root=str(project_root.resolve()),
        mode=plan.mode,
        overall_status=overall_status,
        complete=_run_complete(checks, pytest_result, coverage),
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
        advisories=_build_advisories(checks, pytest_result, coverage, plan.planned_coverage_scope),
    )


def validate_report_v1(report: AgentReportV1) -> None:
    try:
        if not isinstance(report, (PlanningErrorReportV1, RunReportV1)):
            _invalid("unsupported producer model")
        _validate_exact_int(report.schema_version, "schema_version")
        if report.schema_version != 1:
            _invalid("schema_version must be 1")
        if isinstance(report, PlanningErrorReportV1):
            _validate_planning_error_report(report)
            return
        _validate_run_report(report)
    except (AttributeError, TypeError) as error:
        _invalid(f"malformed report model: {error}")


def render_terminal(report: AgentReportV1) -> str:
    """Render a validated report as a complete terminal-ready string."""
    validate_report_v1(report)
    if isinstance(report, PlanningErrorReportV1):
        lines = [report.error.message]
        if report.error.hint is not None:
            lines.append(f"Hint: {report.error.hint}")
        return "\n".join(lines) + "\n"

    run_context = report.mode.replace("_", " ")
    if not report.complete:
        run_context = f"{run_context}, incomplete"
    lines = ["", f"==> pyrepo-check summary: {report.overall_status} ({run_context})"]
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
        _append_failed_line(lines, check.name, _first_positive_exit_code(check.processes))
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
    check: CheckResult, pytest_result: PytestResult | None
) -> bool:
    if check.name != "pytest" or check.error is None or pytest_result is None:
        return False
    return check.error.code == _expected_pytest_check_error(pytest_result)


def _append_helper_diagnostics(lines: list[str], check: CheckResult) -> None:
    _append_process_diagnostics(
        lines,
        check,
        frozenset(("pytest_preflight", "coverage_preflight", "coverage_json")),
    )


def _append_process_diagnostics(
    lines: list[str],
    check: CheckResult,
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


def serialize_json(report: AgentReportV1) -> bytes:
    """Serialize a validated report as one compact UTF-8 JSON document."""
    validate_report_v1(report)
    payload = _report_payload(report)
    text = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return text.encode("utf-8") + b"\n"


def select_exit_code(report: AgentReportV1) -> int:
    """Select the public exit code from report evidence in execution order."""
    validate_report_v1(report)
    if isinstance(report, PlanningErrorReportV1):
        return 2
    for check in report.checks:
        exit_code = _first_positive_exit_code(check.processes)
        if exit_code is not None:
            return exit_code
    if report.overall_status == "failed":
        return 1
    if report.overall_status == "error":
        return 2
    return 0


def _report_payload(report: AgentReportV1) -> dict[str, object]:
    if isinstance(report, PlanningErrorReportV1):
        return {
            "schema_version": report.schema_version,
            "kind": report.kind,
            "overall_status": report.overall_status,
            "complete": report.complete,
            "error": {
                "code": report.error.code,
                "message": report.error.message,
                "hint": report.error.hint,
            },
        }
    return {
        "schema_version": report.schema_version,
        "kind": report.kind,
        "project_root": report.project_root,
        "mode": report.mode,
        "overall_status": report.overall_status,
        "complete": report.complete,
        "selection": _selection_payload(report.selection),
        "checks": [_check_result_payload(check) for check in report.checks],
        "pytest": _pytest_result_payload(report.pytest),
        "coverage": _coverage_result_payload(report.coverage),
        "advisories": [_advisory_payload(advisory) for advisory in report.advisories],
    }


def _selection_payload(selection: Selection) -> dict[str, object]:
    return {
        "checks": list(selection.checks),
        "targets": list(selection.targets),
        "test_shortcut": selection.test_shortcut,
        "pytest_args": list(selection.pytest_args) if selection.pytest_args is not None else None,
        "planned_test_scope": selection.planned_test_scope,
        "planned_coverage_scope": selection.planned_coverage_scope,
    }


def _check_result_payload(check: CheckResult) -> dict[str, object]:
    return {
        "name": check.name,
        "status": check.status,
        "processes": [_process_result_payload(process) for process in check.processes],
        "error": _check_error_payload(check.error),
    }


def _process_result_payload(process: ProcessResult) -> dict[str, object]:
    return {
        "role": process.role,
        "argv": list(process.argv),
        "cwd": process.cwd,
        "outcome": process.outcome,
        "exit_code": process.exit_code,
        "signal": process.signal,
        "duration_ms": process.duration_ms,
        "stdout": _captured_text_payload(process.stdout),
        "stderr": _captured_text_payload(process.stderr),
        "error_message": process.error_message,
    }


def _captured_text_payload(captured: CapturedText) -> dict[str, object]:
    return {
        "captured": captured.captured,
        "text": captured.text,
        "truncated": captured.truncated,
        "omitted_bytes": captured.omitted_bytes,
    }


def _check_error_payload(error: CheckError | None) -> dict[str, object] | None:
    if error is None:
        return None
    return {
        "code": error.code,
        "message": error.message,
    }


def _advisory_payload(advisory: Advisory) -> dict[str, object]:
    return {
        "code": advisory.code,
        "message": advisory.message,
        "hint": advisory.hint,
    }


def _pytest_result_payload(result: PytestResult | None) -> dict[str, object] | None:
    if result is None:
        return None
    return {
        "status": result.status,
        "complete": result.complete,
        "scope": result.scope,
        "scope_reasons": list(result.scope_reasons),
        "pytest_version": result.pytest_version,
        "exit_code": result.exit_code,
        "evidence": _pytest_evidence_payload(result.evidence),
        "error": _pytest_error_payload(result.error),
    }


def _pytest_evidence_payload(evidence: PytestEvidence | None) -> dict[str, object] | None:
    if evidence is None:
        return None
    return {
        "effective_args": list(evidence.effective_args),
        "collected": evidence.collected,
        "deselected": evidence.deselected,
        "counts": {
            "passed": evidence.counts.passed,
            "failed": evidence.counts.failed,
            "errors": evidence.counts.errors,
            "skipped": evidence.counts.skipped,
            "xfailed": evidence.counts.xfailed,
            "xpassed": evidence.counts.xpassed,
        },
        "collection_errors": [
            _collection_issue_payload(item) for item in evidence.collection_errors
        ],
        "collection_skips": [_collection_issue_payload(item) for item in evidence.collection_skips],
        "slowest": [
            {"nodeid": item.nodeid, "duration_ms": item.duration_ms} for item in evidence.slowest
        ],
        "special_outcomes": [
            {
                "nodeid": item.nodeid,
                "outcome": item.outcome,
                "reason": item.reason,
                "strict": item.strict,
                "affects_exit": item.affects_exit,
                "duration_ms": item.duration_ms,
            }
            for item in evidence.special_outcomes
        ],
    }


def _collection_issue_payload(issue: CollectionIssue) -> dict[str, object]:
    return {"nodeid": issue.nodeid, "message": issue.message}


def _pytest_error_payload(error: PytestError | None) -> dict[str, object] | None:
    if error is None:
        return None
    return {"code": error.code, "message": error.message}


def _coverage_result_payload(result: CoverageResult | None) -> dict[str, object] | None:
    if result is None:
        return None
    return {
        "status": result.status,
        "scope": result.scope,
        "evidence_complete": result.evidence_complete,
        "coverage_version": result.coverage_version,
        "gate_eligible": result.gate_eligible,
        "threshold": {
            "configured": result.threshold.configured,
            "value": result.threshold.value,
            "evaluated": result.threshold.evaluated,
            "passed": result.threshold.passed,
            "skipped_reason": result.threshold.skipped_reason,
        },
        "totals": (
            None
            if result.totals is None
            else {
                "statements": _coverage_counts_payload(result.totals.statements),
                "branches": _coverage_counts_payload(result.totals.branches),
            }
        ),
        "files": [
            {
                "path": file.path,
                "statements": {
                    "covered": file.statements.covered,
                    "missing": file.statements.missing,
                    "missing_lines": list(file.statements.missing_lines),
                },
                "branches": {
                    "covered": file.branches.covered,
                    "missing": file.branches.missing,
                    "missing_arcs": [list(arc) for arc in file.branches.missing_arcs],
                },
            }
            for file in result.files
        ],
        "error": (
            None
            if result.error is None
            else {"code": result.error.code, "message": result.error.message}
        ),
    }


def _coverage_counts_payload(counts: CoverageCounts) -> dict[str, object]:
    return {"covered": counts.covered, "missing": counts.missing}


def _first_positive_exit_code(processes: tuple[ProcessResult, ...]) -> int | None:
    for process in processes:
        if process.exit_code is not None and process.exit_code > 0:
            return process.exit_code
    return None


def _validate_planning_error_report(report: PlanningErrorReportV1) -> None:
    if report.kind != "planning_error":
        _invalid("planning report kind must be planning_error")
    if report.overall_status != "error":
        _invalid("planning report overall_status must be error")
    if report.complete is not False:
        _invalid("planning report complete must be false")
    if not isinstance(report.error, PlanningError):
        _invalid("planning report error must be a PlanningError")
    if report.error.code not in _PLANNING_ERROR_CODES:
        _invalid("unknown planning error code")
    if not isinstance(report.error.message, str):
        _invalid("planning error message must be a string")
    if report.error.hint is not None and not isinstance(report.error.hint, str):
        _invalid("planning error hint must be null or a string")


def _validate_run_report(report: RunReportV1) -> None:
    if report.kind != "run":
        _invalid("run report kind must be run")
    if report.mode not in _RUN_MODES:
        _invalid("unknown run mode")
    if report.overall_status not in _OVERALL_STATUSES:
        _invalid("unknown overall status")
    if type(report.complete) is not bool:
        _invalid("run complete must be boolean")
    if not isinstance(report.project_root, str):
        _invalid("project_root must be a string")
    if not Path(report.project_root).is_absolute():
        _invalid("project_root must be absolute")
    pytest_selected = _validate_selection(report.selection)
    selection = report.selection
    if report.mode == "focused" and selection.planned_coverage_scope == "unavailable":
        _invalid("focused runs cannot have unavailable planned coverage")
    if report.mode == "strict_aggregate" and selection.planned_coverage_scope == "not_requested":
        _invalid("strict aggregate runs cannot omit planned coverage")
    if not isinstance(report.checks, tuple):
        _invalid("checks must be a tuple")
    for check in report.checks:
        if not isinstance(check, CheckResult):
            _invalid("checks must contain CheckResult values")
    emitted_names = tuple(check.name for check in report.checks)
    if selection.checks != emitted_names:
        _invalid("selection checks must exactly match emitted checks")
    if pytest_selected != (report.pytest is not None):
        _invalid("pytest must be present exactly when pytest is selected")
    if report.pytest is not None:
        _validate_pytest_result(report.pytest, selection)

    statuses = {
        _validate_check_result(
            check,
            report.pytest if check.name == "pytest" else None,
            selection=selection,
            coverage=report.coverage if check.name == "pytest" else None,
        )
        for check in report.checks
    }
    pytest_check = next((check for check in report.checks if check.name == "pytest"), None)
    _validate_coverage_projection(
        report.mode,
        selection,
        report.pytest,
        report.coverage,
        pytest_check,
    )
    _validate_advisories(
        report.advisories, report.checks, report.pytest, report.coverage, selection
    )

    expected_complete = _run_complete(report.checks, report.pytest, report.coverage)
    if (
        not expected_complete
        or "error" in statuses
        or (report.pytest is not None and report.pytest.status == "error")
        or (report.coverage is not None and report.coverage.status == "error")
    ):
        expected_status: OverallStatus = "error"
    elif (
        "failed" in statuses
        or (report.pytest is not None and report.pytest.status == "failed")
        or (report.coverage is not None and report.coverage.status == "failed")
    ):
        expected_status = "failed"
    else:
        expected_status = "passed"
    if report.overall_status != expected_status:
        _invalid("overall_status violates check-status precedence")
    if report.complete is not expected_complete:
        _invalid("complete is inconsistent with evidence")


def _validate_selection(selection: Selection) -> bool:
    if not isinstance(selection, Selection):
        _invalid("selection must be a Selection")
    if not isinstance(selection.checks, tuple) or any(
        not isinstance(name, str) or name not in _CHECK_NAMES for name in selection.checks
    ):
        _invalid("selection contains an unknown check name")
    if len(set(selection.checks)) != len(selection.checks):
        _invalid("selection checks must be unique")
    canonical_selection = tuple(
        name for name in _B_CANONICAL_CHECK_ORDER if name in selection.checks
    )
    if selection.checks != canonical_selection:
        _invalid("selection checks must use canonical order")
    if not isinstance(selection.targets, tuple) or any(
        not isinstance(target, str) for target in selection.targets
    ):
        _invalid("targets must be a tuple of strings")
    if selection.test_shortcut is not None and (
        not isinstance(selection.test_shortcut, str)
        or TEST_SHORTCUT_NAME_PATTERN.fullmatch(selection.test_shortcut) is None
    ):
        _invalid("test_shortcut must be null or a valid Test Shortcut name")
    if selection.pytest_args is not None and (
        not isinstance(selection.pytest_args, tuple)
        or any(not isinstance(arg, str) for arg in selection.pytest_args)
    ):
        _invalid("pytest_args must be null or a tuple of strings")
    if selection.planned_test_scope not in _PLANNED_TEST_SCOPES:
        _invalid("unknown planned test scope")
    if selection.planned_coverage_scope not in _PLANNED_COVERAGE_SCOPES:
        _invalid("unknown planned coverage scope")
    pytest_selected = "pytest" in selection.checks
    if not pytest_selected:
        if selection.test_shortcut is not None:
            _invalid("test_shortcut requires pytest selection")
        if selection.pytest_args is not None:
            _invalid("pytest_args must be null when pytest is not selected")
        if selection.planned_test_scope != "not_selected":
            _invalid("planned_test_scope must be not_selected when pytest is not selected")
        if selection.planned_coverage_scope != "not_requested":
            _invalid("planned coverage requires pytest selection")
    elif selection.test_shortcut is not None:
        if selection.checks != ("pytest",):
            _invalid("test_shortcut requires a pytest-only selection")
        if selection.targets:
            _invalid("test_shortcut cannot coexist with direct targets")
        if not selection.pytest_args:
            _invalid("test_shortcut requires non-empty pytest_args")
        if selection.planned_test_scope != "partial":
            _invalid("test_shortcut requires partial planned test scope")
    else:
        if selection.pytest_args != selection.targets:
            _invalid("pytest_args must exactly match targets without a Test Shortcut")
        expected_scope: PlannedTestScope = "partial" if selection.targets else "complete"
        if selection.planned_test_scope != expected_scope:
            _invalid("planned_test_scope is inconsistent with pytest selection")
    return pytest_selected


def _validate_coverage_projection(
    mode: RunMode,
    selection: Selection,
    pytest_result: PytestResult | None,
    coverage: CoverageResult | None,
    pytest_check: CheckResult | None,
) -> None:
    expected_null = selection.planned_coverage_scope in {"not_requested", "unavailable"}
    if (coverage is None) != expected_null:
        _invalid("coverage nullability contradicts planned coverage scope")
    if coverage is not None:
        try:
            validate_coverage_result(coverage)
        except ValueError as error:
            _invalid(f"invalid coverage result: {error}")
        if pytest_result is None or pytest_check is None:
            _invalid("planned coverage requires a pytest report")
            return
        _validate_coverage_report_context(
            mode,
            selection,
            pytest_result,
            coverage,
            pytest_check,
        )


def _validate_coverage_report_context(
    mode: RunMode,
    selection: Selection,
    pytest_result: PytestResult,
    coverage: CoverageResult,
    pytest_check: CheckResult,
) -> None:
    expected_scope = (
        "complete"
        if (
            selection.planned_coverage_scope == "complete"
            and pytest_result.scope == "complete"
            and coverage.evidence_complete
        )
        else "partial"
    )
    if coverage.scope != expected_scope:
        _invalid("coverage scope contradicts observed pytest and coverage evidence")
    policy = coverage_gate_policy_for_context(
        mode=mode,
        targets=selection.targets,
        test_shortcut=selection.test_shortcut,
        pytest_result=pytest_result,
        evidence_complete=coverage.evidence_complete,
        configured=coverage.threshold.configured,
    )
    if coverage.gate_eligible is not policy.gate_eligible:
        _invalid("coverage gate eligibility contradicts report context")
    pytest_parallelism = (
        pytest_result.error is not None and pytest_result.error.code == "unsupported_parallelism"
    )
    coverage_parallelism = (
        coverage.status == "error"
        and coverage.error is not None
        and coverage.error.code == "unsupported_parallelism"
        and tuple(process.role for process in pytest_check.processes) == _COVERAGE_PRIMARY_ROLES
    )
    if pytest_parallelism is not coverage_parallelism:
        _invalid("pytest and coverage parallelism evidence must match exactly")
    if coverage.status == "error":
        if policy.skipped_reason != "evidence_error":
            _invalid("coverage error must be backed by incomplete evidence")
        _validate_coverage_error_processes(
            mode,
            selection,
            pytest_result,
            coverage,
            pytest_check,
        )
        return
    if coverage.threshold.skipped_reason != policy.skipped_reason:
        _invalid("coverage threshold skip reason contradicts report context")
    expected_status = (
        "guidance"
        if not policy.gate_eligible
        else "failed"
        if coverage.threshold.passed is False
        else "passed"
    )
    if coverage.status != expected_status:
        _invalid("coverage status contradicts report context")
    _validate_complete_coverage_processes(pytest_check, coverage)


def _validate_complete_coverage_processes(
    pytest_check: CheckResult,
    coverage: CoverageResult,
) -> None:
    if tuple(process.role for process in pytest_check.processes) != _COVERAGE_COMPLETE_ROLES:
        _invalid("complete coverage requires every attempted coverage process")
    coverage_preflight = pytest_check.processes[1]
    if coverage_preflight.outcome != "exited" or coverage_preflight.exit_code != 0:
        _invalid("coverage primary requires a successful coverage preflight")
    coverage_json = pytest_check.processes[-1]
    expected_exit_code = 2 if coverage.status == "failed" else 0
    if coverage_json.outcome != "exited" or coverage_json.exit_code != expected_exit_code:
        _invalid("coverage JSON process contradicts coverage result")


def _validate_coverage_error_processes(
    mode: RunMode,
    selection: Selection,
    pytest_result: PytestResult,
    coverage: CoverageResult,
    pytest_check: CheckResult,
) -> None:
    error = coverage.error
    if error is None:
        _invalid("coverage error result requires an error")
        return
    roles = tuple(process.role for process in pytest_check.processes)
    if roles in {(), ("pytest_preflight",)}:
        if error.code != "preflight_invalid" or coverage.coverage_version is not None:
            _invalid("coverage setup error contradicts attempted processes")
        return

    if roles == _COVERAGE_PREFLIGHT_ROLES:
        coverage_preflight = pytest_check.processes[-1]
        if error.code in _COVERAGE_PREPRIMARY_ERROR_CODES:
            if coverage_preflight.outcome != "exited" or (
                error.code != "preflight_invalid" and coverage_preflight.exit_code != 0
            ):
                _invalid("typed coverage preflight error contradicts preflight exit")
            return
        if error.code in {"spawn_failed", "terminated_by_signal"}:
            expected_outcome: ProcessOutcome = (
                "spawn_failed" if error.code == "spawn_failed" else "signaled"
            )
            if (
                coverage_preflight.outcome != expected_outcome
                or coverage.coverage_version is not None
            ):
                _invalid("coverage preflight process contradicts coverage error")
            return
        if error.code == "data_missing":
            _validate_supported_prejson_coverage_error(coverage, coverage_preflight)
            return
        _invalid("coverage error requires a later attempted process")
        return

    if roles == _COVERAGE_PRIMARY_ROLES:
        coverage_preflight = pytest_check.processes[1]
        if error.code == "unsupported_parallelism":
            _validate_supported_prejson_coverage_error(coverage, coverage_preflight)
            if pytest_result.error is None or pytest_result.error.code != "unsupported_parallelism":
                _invalid("coverage parallelism error requires matching pytest evidence")
            return
        if (
            pytest_result.error is not None
            and pytest_result.error.code == "unsupported_parallelism"
        ):
            _invalid("pytest parallelism evidence requires matching coverage error")
        if error.code in _COVERAGE_PREJSON_ARTIFACT_ERROR_CODES:
            _validate_supported_prejson_coverage_error(coverage, coverage_preflight)
            return
        _invalid("coverage error contradicts primary and JSON process evidence")
        return

    if roles != _COVERAGE_COMPLETE_ROLES:
        _invalid("coverage error uses an invalid attempted-command order")
        return

    coverage_preflight = pytest_check.processes[1]
    coverage_json = pytest_check.processes[-1]
    if (
        coverage_preflight.outcome != "exited"
        or coverage_preflight.exit_code != 0
        or not is_supported_coverage_version(coverage.coverage_version)
    ):
        _invalid("post-JSON coverage error requires a supported coverage preflight")
    if error.code in {"spawn_failed", "terminated_by_signal"}:
        expected_outcome = "spawn_failed" if error.code == "spawn_failed" else "signaled"
        if coverage_json.outcome != expected_outcome:
            _invalid("coverage JSON process contradicts coverage error")
        return
    if error.code == "generation_failed":
        if (
            coverage_json.outcome != "exited"
            or coverage_json.exit_code is None
            or coverage_json.exit_code <= 0
        ):
            _invalid("coverage generation failure requires a positive JSON exit")
        if coverage_json.exit_code == 2 and _coverage_threshold_exit_two_is_eligible(
            mode,
            selection,
            pytest_result,
            coverage,
        ):
            _invalid("eligible coverage threshold exit cannot be a generation failure")
        return
    if error.code in {"artifact_missing", "artifact_invalid"}:
        if coverage_json.outcome != "exited":
            _invalid("coverage artifact error requires an exited JSON process")
        if coverage_json.exit_code == 0:
            return
        if coverage_json.exit_code != 2 or not _coverage_threshold_exit_two_is_eligible(
            mode,
            selection,
            pytest_result,
            coverage,
        ):
            _invalid("coverage artifact error contradicts the JSON exit")
        return
    if error.code == "unexpected_parallel_data":
        if coverage_json.outcome != "exited":
            _invalid("post-JSON parallel-data error requires an exited JSON process")
        return
    _invalid("coverage error contradicts complete process evidence")


def _validate_supported_prejson_coverage_error(
    coverage: CoverageResult,
    coverage_preflight: ProcessResult,
) -> None:
    if (
        coverage_preflight.outcome != "exited"
        or coverage_preflight.exit_code != 0
        or not is_supported_coverage_version(coverage.coverage_version)
    ):
        _invalid("post-preflight coverage error requires a supported coverage preflight")


def _coverage_threshold_exit_two_is_eligible(
    mode: RunMode,
    selection: Selection,
    pytest_result: PytestResult,
    coverage: CoverageResult,
) -> bool:
    return (
        coverage.threshold.configured
        and coverage_gate_policy_for_context(
            mode=mode,
            targets=selection.targets,
            test_shortcut=selection.test_shortcut,
            pytest_result=pytest_result,
            evidence_complete=True,
            configured=coverage.threshold.configured,
        ).gate_eligible
    )


def _validate_check_result(
    check: CheckResult,
    pytest_result: PytestResult | None,
    *,
    selection: Selection,
    coverage: CoverageResult | None,
) -> CheckStatus:
    if not isinstance(check, CheckResult):
        _invalid("check must be a CheckResult")
    if check.name not in _CHECK_NAMES:
        _invalid("unknown check name")
    if check.status not in _CHECK_STATUSES:
        _invalid("unknown check status")
    if not isinstance(check.processes, tuple):
        _invalid("check processes must be a tuple")
    _validate_check_error(check.error)
    if check.name == "pytest":
        return _validate_pytest_check_result(check, pytest_result, selection, coverage)
    if not check.processes:
        if (
            check.status != "error"
            or check.error is None
            or check.error.code != "missing_primary_process"
        ):
            _invalid("no-process check must be missing_primary_process error")
        return "error"
    if len(check.processes) != 1:
        _invalid("ordinary check must contain exactly one primary process")

    process = check.processes[0]
    _validate_process_result(process)
    if process.role != "primary":
        _invalid("ordinary check process role must be primary")

    if process.outcome == "exited":
        expected_status: CheckStatus = "passed" if process.exit_code == 0 else "failed"
        expected_error_code: CheckErrorCode | None = None
    elif process.outcome == "signaled":
        expected_status = "error"
        expected_error_code = "terminated_by_signal"
    else:
        expected_status = "error"
        expected_error_code = "spawn_failed"

    if check.status != expected_status:
        _invalid("check status contradicts primary process evidence")
    if expected_error_code is None:
        if check.error is not None:
            _invalid("exited check must not have an error")
    elif check.error is None or check.error.code != expected_error_code:
        _invalid("check error contradicts primary process evidence")
    return expected_status


def _validate_pytest_check_result(
    check: CheckResult,
    result: PytestResult | None,
    selection: Selection,
    coverage: CoverageResult | None,
) -> CheckStatus:
    if result is None:
        _invalid("pytest check requires a pytest result")
        return check.status
    processes = check.processes
    for process in processes:
        _validate_process_result(process)
    preflight = processes[0] if processes else None
    coverage_preflight = next(
        (process for process in processes if process.role == "coverage_preflight"), None
    )
    primary = next((process for process in processes if process.role == "primary"), None)
    if preflight is not None and preflight.role != "pytest_preflight":
        _invalid("pytest processes must start with pytest_preflight")
    _validate_pytest_process_order(processes, coverage_planned=coverage is not None)
    if (
        coverage_preflight is not None
        and primary is not None
        and (coverage_preflight.outcome != "exited" or coverage_preflight.exit_code != 0)
    ):
        _invalid("pytest primary cannot follow a failed coverage preflight")
    if (
        primary is not None
        and coverage is not None
        and coverage.status == "error"
        and coverage.error is not None
        and coverage.error.code in _COVERAGE_PREPRIMARY_ERROR_CODES
    ):
        _invalid("pytest primary cannot follow a typed coverage preflight failure")
    _validate_pytest_execution_shape(result, preflight, primary)

    if check.error is not None and check.error.code == "cleanup_failed":
        if check.status != "error":
            _invalid("cleanup failure requires pytest check error")
        return "error"
    expected_error = _expected_pytest_check_error(result)
    if _coverage_preflight_owns_pytest_check(
        result,
        coverage,
        preflight,
        coverage_preflight,
        primary,
    ):
        expected_error = "coverage_preflight_failed"
    if expected_error is None:
        if check.status != result.status or check.error is not None:
            _invalid("pytest check must match successful pytest evidence")
    elif check.status != "error" or check.error is None or check.error.code != expected_error:
        _invalid("pytest check error contradicts pytest evidence")
    return check.status


def _validate_pytest_process_order(
    processes: tuple[ProcessResult, ...], *, coverage_planned: bool
) -> None:
    roles = tuple(process.role for process in processes)
    allowed = (
        {
            (),
            ("pytest_preflight",),
            ("pytest_preflight", "primary"),
        }
        if not coverage_planned
        else {
            (),
            ("pytest_preflight",),
            ("pytest_preflight", "coverage_preflight"),
            ("pytest_preflight", "coverage_preflight", "primary"),
            ("pytest_preflight", "coverage_preflight", "primary", "coverage_json"),
        }
    )
    if roles not in allowed:
        _invalid("pytest processes use an invalid attempted-command order")


def _expected_pytest_check_error(result: PytestResult) -> CheckErrorCode | None:
    if result.error is None or result.status != "error":
        return None
    if result.error.code == "not_started":
        return "missing_primary_process"
    if result.error.code == "spawn_failed":
        return "spawn_failed"
    if result.error.code == "terminated_by_signal":
        return "terminated_by_signal"
    if result.error.code in _PYTEST_PREFLIGHT_ERROR_CODES:
        return "pytest_preflight_failed"
    return "pytest_evidence_error"


def _coverage_preflight_owns_pytest_check(
    result: PytestResult,
    coverage: CoverageResult | None,
    pytest_preflight: ProcessResult | None,
    coverage_preflight: ProcessResult | None,
    primary: ProcessResult | None,
) -> bool:
    return (
        result.status == "error"
        and result.error is not None
        and result.error.code == "not_started"
        and pytest_preflight is not None
        and pytest_preflight.outcome == "exited"
        and pytest_preflight.exit_code == 0
        and coverage_preflight is not None
        and primary is None
        and coverage is not None
        and coverage.status == "error"
        and coverage.error is not None
        and coverage.error.code in _COVERAGE_PREFLIGHT_ERROR_CODES
    )


def _validate_pytest_execution_shape(
    result: PytestResult,
    preflight: ProcessResult | None,
    primary: ProcessResult | None,
) -> None:
    error_code = result.error.code if result.error is not None else None
    if primary is None:
        if result.exit_code is not None:
            _invalid("pytest without a primary process must not have an exit_code")
        if result.evidence is not None:
            _invalid("pytest without a primary process must not have evidence")
        if result.complete or result.status != "error" or error_code is None:
            _invalid("pytest without a primary process must be a typed error")
        if error_code == "not_started":
            if preflight is None:
                if result.pytest_version is not None:
                    _invalid("pytest without preflight cannot have a trusted version")
                return
            if preflight.outcome == "exited" and preflight.exit_code == 0:
                if result.pytest_version is None:
                    _invalid("successful pytest preflight requires a trusted version")
                return
            _invalid("not_started requires absent or successful pytest preflight")
            return
        if preflight is None:
            _invalid("selected pytest requires preflight before a non-started error")
            return
        if preflight.outcome == "spawn_failed":
            if result.pytest_version is not None:
                _invalid("failed pytest preflight cannot have a trusted version")
            if error_code != "spawn_failed":
                _invalid("pytest preflight spawn failure contradicts pytest error")
            return
        if preflight.outcome == "signaled":
            if result.pytest_version is not None:
                _invalid("failed pytest preflight cannot have a trusted version")
            if error_code != "terminated_by_signal":
                _invalid("pytest preflight signal contradicts pytest error")
            return
        if error_code not in _PYTEST_PREFLIGHT_ERROR_CODES:
            _invalid("pytest without a primary process requires a preflight error")
        if preflight.exit_code != 0 and error_code != "preflight_invalid":
            _invalid("failed pytest preflight must be preflight_invalid")
        if error_code == "unsupported_version":
            if result.pytest_version is None:
                _invalid("unsupported pytest version requires a trusted version")
        elif result.pytest_version is not None:
            _invalid("untrusted pytest preflight cannot have a version")
        return

    if preflight is None:
        _invalid("pytest primary requires a successful preflight")
        return
    if preflight.outcome != "exited" or preflight.exit_code != 0:
        _invalid("pytest primary requires a successful preflight")
        return
    if result.pytest_version is None:
        _invalid("pytest primary requires a trusted preflight version")
        return
    if primary.outcome == "spawn_failed":
        _validate_pytest_no_exit_error(result, "spawn_failed")
        return
    if primary.outcome == "signaled":
        _validate_pytest_no_exit_error(result, "terminated_by_signal")
        return

    exit_code = primary.exit_code
    if exit_code is None:
        _invalid("exited pytest primary requires an exit_code")
        return
    if result.exit_code != exit_code:
        _invalid("pytest exit_code must match the primary process")
    _validate_pytest_primary_outcome(result, exit_code)


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


def _validate_process_result(process: ProcessResult) -> None:
    if not isinstance(process, ProcessResult):
        _invalid("process must be a ProcessResult")
    if process.role not in _PROCESS_ROLES:
        _invalid("unknown process role")
    if process.outcome not in _PROCESS_OUTCOMES:
        _invalid("unknown process outcome")
    if not isinstance(process.argv, tuple) or any(not isinstance(arg, str) for arg in process.argv):
        _invalid("process argv must be a tuple of strings")
    if not isinstance(process.cwd, str):
        _invalid("process cwd must be a string")
    if not Path(process.cwd).is_absolute():
        _invalid("process cwd must be absolute")
    _validate_exact_int(process.duration_ms, "process duration_ms")
    if process.duration_ms < 0:
        _invalid("process duration_ms must be non-negative")
    _validate_captured_text(process.stdout)
    _validate_captured_text(process.stderr)
    if process.error_message is not None and not isinstance(process.error_message, str):
        _invalid("process error_message must be null or a string")

    if process.outcome == "exited":
        exit_code = process.exit_code
        if exit_code is None:
            _invalid("exited process requires exit_code and null signal")
        if process.signal is not None:
            _invalid("exited process requires exit_code and null signal")
        exit_code = _validate_exact_int(exit_code, "exited process exit_code")
        if exit_code < 0:
            _invalid("exited process exit_code must be non-negative")
        if process.error_message is not None:
            _invalid("exited process requires null error_message")
    elif process.outcome == "signaled":
        signal = process.signal
        if process.exit_code is not None:
            _invalid("signaled process requires signal and null exit_code")
        if signal is None:
            _invalid("signaled process requires signal and null exit_code")
        signal = _validate_exact_int(signal, "signaled process signal")
        if signal <= 0:
            _invalid("signaled process signal must be positive")
        if process.error_message is None:
            _invalid("signaled process requires error_message")
    elif process.outcome == "spawn_failed":
        if process.exit_code is not None or process.signal is not None:
            _invalid("spawn_failed process requires null exit_code and signal")
        if process.error_message is None:
            _invalid("spawn_failed process requires error_message")


def _validate_captured_text(captured: CapturedText) -> None:
    if not isinstance(captured, CapturedText):
        _invalid("captured stream must be CapturedText")
    if type(captured.captured) is not bool:
        _invalid("captured flag must be boolean")
    if not isinstance(captured.text, str):
        _invalid("captured text must be a string")
    if type(captured.truncated) is not bool:
        _invalid("captured truncated must be boolean")
    _validate_exact_int(captured.omitted_bytes, "captured omitted_bytes")
    if captured.omitted_bytes < 0:
        _invalid("captured omitted_bytes must be non-negative")
    if not captured.captured:
        if captured.text or captured.truncated or captured.omitted_bytes != 0:
            _invalid("uncaptured text must be empty, untruncated, and omit zero bytes")
        return
    if captured.truncated is not (captured.omitted_bytes > 0):
        _invalid("captured truncation must match a positive omitted-byte count")


def _validate_check_error(error: CheckError | None) -> None:
    if error is None:
        return
    if not isinstance(error, CheckError):
        _invalid("check error must be a CheckError")
    if error.code not in _CHECK_ERROR_CODES:
        _invalid("unknown check error code")
    if not isinstance(error.message, str):
        _invalid("check error message must be a string")


def _validate_advisories(
    advisories: tuple[Advisory, ...],
    checks: tuple[CheckResult, ...],
    pytest_result: PytestResult | None,
    coverage: CoverageResult | None,
    selection: Selection,
) -> None:
    if not isinstance(advisories, tuple):
        _invalid("advisories must be a tuple")
    for advisory in advisories:
        if not isinstance(advisory, Advisory):
            _invalid("advisories must contain Advisory values")
        if advisory.code not in _ADVISORY_CODES:
            _invalid("unknown advisory code")
        if not isinstance(advisory.message, str):
            _invalid("advisory message must be a string")
        if advisory.hint is not None and not isinstance(advisory.hint, str):
            _invalid("advisory hint must be null or a string")
    keys = tuple((advisory.code, advisory.message) for advisory in advisories)
    if len(set(keys)) != len(keys):
        _invalid("advisories must be unique by code and message")
    if tuple(sorted(advisories, key=lambda item: (item.code, item.message))) != advisories:
        _invalid("advisories must use code then message order")
    if advisories != _build_advisories(
        checks, pytest_result, coverage, selection.planned_coverage_scope
    ):
        _invalid("advisories must exactly match report evidence")


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


def _run_complete(
    checks: tuple[CheckResult, ...],
    pytest_result: PytestResult | None,
    coverage: CoverageResult | None,
) -> bool:
    return (
        all(check.status != "error" for check in checks)
        and (pytest_result is None or pytest_result.complete)
        and (coverage is None or coverage.evidence_complete)
    )


def _build_advisories(
    checks: tuple[CheckResult, ...],
    pytest_result: PytestResult | None,
    coverage: CoverageResult | None = None,
    planned_coverage_scope: PlannedCoverageScope = "not_requested",
) -> tuple[Advisory, ...]:
    advisories: list[Advisory] = []
    for check in checks:
        for process_index, process in enumerate(check.processes, start=1):
            for stream_name, captured in (
                ("stdout", process.stdout),
                ("stderr", process.stderr),
            ):
                if not captured.truncated:
                    continue
                advisories.append(
                    Advisory(
                        code="output_truncated",
                        message=(
                            f"{check.name} process {process_index} ({process.role}) "
                            f"{stream_name} omitted {captured.omitted_bytes} byte(s); "
                            f"only the final {CAPTURE_LIMIT_BYTES} bytes are included."
                        ),
                        hint=None,
                    )
                )
    if pytest_result is not None and pytest_result.evidence is not None:
        for outcome in pytest_result.evidence.special_outcomes:
            if outcome.reason is None or outcome.reason == "":
                advisories.append(
                    Advisory(
                        code="missing_test_reason",
                        message=(f"pytest {outcome.outcome} has no reason: {outcome.nodeid}."),
                        hint=None,
                    )
                )
    if planned_coverage_scope == "unavailable":
        advisories.append(
            Advisory(
                "coverage_not_configured",
                "Coverage guidance is unavailable because native Coverage.py configuration is absent.",
                None,
            )
        )
    if coverage is not None and coverage.threshold.configured and not coverage.threshold.evaluated:
        advisories.append(
            Advisory(
                "coverage_threshold_not_applied",
                "Configured coverage threshold was not applied to this run.",
                None,
            )
        )
    unique = {(advisory.code, advisory.message): advisory for advisory in advisories}
    return tuple(sorted(unique.values(), key=lambda advisory: (advisory.code, advisory.message)))


def _build_advisories_v2(report: RunReportV2) -> tuple[Advisory, ...]:
    advisories = list(
        _build_advisories(
            (),
            report.pytest,
            report.coverage,
            report.selection.planned_coverage_scope,
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


def _match_observations(
    planned_checks: tuple[CheckInvocation, ...],
    executed_checks: tuple[ExecutedCheck, ...],
) -> dict[int, ExecutedCheck]:
    matched: dict[int, ExecutedCheck] = {}
    seen: set[CheckInvocation] = set()
    next_index = 0

    for observation in executed_checks:
        if observation.planned in seen:
            raise ReportingError(
                f"duplicate execution observation for check {observation.planned.name}"
            )
        seen.add(observation.planned)

        match = next(
            (
                index
                for index in range(next_index, len(planned_checks))
                if planned_checks[index] == observation.planned
            ),
            None,
        )
        if match is None:
            if any(check.name == observation.planned.name for check in planned_checks):
                raise ReportingError(
                    f"mismatched or out-of-order observation for check {observation.planned.name}"
                )
            raise ReportingError(
                f"unexpected execution observation for check {observation.planned.name}"
            )

        matched[match] = observation
        next_index = match + 1

    return matched


def _build_check_result(
    planned: CheckInvocation,
    observation: ExecutedCheck | None,
    *,
    output_format: OutputFormat,
    pytest_result: PytestResult | None,
) -> CheckResult:
    if planned.name == "pytest":
        if pytest_result is None:
            raise ReportingError("selected pytest requires a pytest result")
        processes = (
            ()
            if observation is None
            else tuple(
                _build_process_result(process, output_format=output_format)[0]
                for process in _project_pytest_processes(observation)
            )
        )
        error = _pytest_check_error(pytest_result, observation)
        status: CheckStatus = "error" if error is not None else pytest_result.status
        return CheckResult(planned.name, status, processes, error)
    if observation is None:
        return CheckResult(
            name=planned.name,
            status="error",
            processes=(),
            error=CheckError(
                code="missing_primary_process",
                message="No primary process observation was recorded.",
            ),
        )

    if len(observation.processes) == 1 and observation.processes[0].role == "primary":
        primary = observation.processes[0]
    else:
        raise ReportingError("ordinary check must contain exactly one primary process")
    process, status, error = _build_process_result(
        primary,
        output_format=output_format,
    )
    return CheckResult(
        name=planned.name,
        status=status,
        processes=(process,),
        error=error,
    )


def _project_pytest_processes(observation: ExecutedCheck) -> tuple[ExecutedProcess, ...]:
    processes = observation.processes
    if not processes:
        pytest_observation = observation.pytest
        if (
            pytest_observation is not None
            and pytest_observation.preflight.classification == "not_started"
        ):
            return ()
        raise ReportingError("pytest execution process order must start with preflight")
    if processes[0].role != "pytest_preflight":
        raise ReportingError("pytest execution process order must start with preflight")
    roles = tuple(process.role for process in processes)
    if roles in {
        ("pytest_preflight",),
        ("pytest_preflight", "primary"),
        ("pytest_preflight", "coverage_preflight"),
        ("pytest_preflight", "coverage_preflight", "primary"),
        ("pytest_preflight", "coverage_preflight", "primary", "coverage_json"),
    }:
        return processes
    raise ReportingError("pytest execution process order is invalid")


def _build_coverage_result(
    project_root: Path,
    plan: RunPlan,
    pytest_result: PytestResult | None,
    observation: ExecutedCheck | None,
) -> CoverageResult | None:
    if plan.planned_coverage_scope in {"not_requested", "unavailable"}:
        return None
    if pytest_result is None:
        raise ReportingError("planned coverage requires pytest selection")
    return build_coverage_result(
        project_root.resolve(),
        plan,
        pytest_result,
        observation.coverage if observation is not None else None,
    )


def _build_pytest_result(plan: RunPlan, observation: ExecutedCheck | None) -> PytestResult:
    if observation is not None:
        return build_pytest_result(plan, observation)
    reasons = (
        ("planned_selector", "incomplete_session")
        if plan.planned_test_scope == "partial"
        else ("incomplete_session",)
    )
    return PytestResult(
        status="error",
        complete=False,
        scope="partial",
        scope_reasons=reasons,
        pytest_version=None,
        exit_code=None,
        evidence=None,
        error=PytestError("not_started", "pytest execution was not observed"),
    )


def _pytest_check_error(
    result: PytestResult, observation: ExecutedCheck | None
) -> CheckError | None:
    cleanup_error = (
        observation.pytest.cleanup_error
        if observation is not None and observation.pytest is not None
        else None
    )
    if cleanup_error is not None:
        return CheckError("cleanup_failed", f"Could not clean up pytest evidence: {cleanup_error}")
    if _coverage_preflight_prevented_primary(result, observation):
        if observation is None or observation.coverage is None:
            raise AssertionError("coverage preflight observation is unavailable")
        coverage = observation.coverage
        return CheckError(
            "coverage_preflight_failed",
            coverage.preflight.diagnostic
            or f"coverage preflight: {coverage.preflight.classification}",
        )
    if result.error is None:
        return None
    if result.status != "error":
        return None
    if result.error.code == "not_started":
        return CheckError("missing_primary_process", "No primary process observation was recorded.")
    if result.error.code == "spawn_failed":
        prefix = (
            "Pytest execution failed"
            if _process_started_before_failure(result.error.message)
            else "Could not start pytest"
        )
        return CheckError("spawn_failed", f"{prefix}: {result.error.message}")
    if result.error.code == "terminated_by_signal":
        return CheckError("terminated_by_signal", result.error.message)
    if result.error.code in {
        "unsupported_python",
        "module_unavailable",
        "unsupported_version",
        "preflight_invalid",
    }:
        return CheckError("pytest_preflight_failed", result.error.message)
    return CheckError("pytest_evidence_error", result.error.message)


def _coverage_preflight_prevented_primary(
    result: PytestResult,
    observation: ExecutedCheck | None,
) -> bool:
    if observation is None or observation.coverage is None:
        return False
    if result.status != "error" or result.error is None or result.error.code != "not_started":
        return False
    pytest_preflight = next(
        (process for process in observation.processes if process.role == "pytest_preflight"),
        None,
    )
    if pytest_preflight is None or pytest_preflight.returncode != 0:
        return False
    if observation.pytest is None or observation.pytest.preflight.classification != "supported":
        return False
    if not any(process.role == "coverage_preflight" for process in observation.processes):
        return False
    if any(process.role == "primary" for process in observation.processes):
        return False
    return observation.coverage.preflight.classification != "supported"


def _build_process_result(
    observation: ExecutedProcess,
    *,
    output_format: OutputFormat,
) -> tuple[ProcessResult, CheckStatus, CheckError | None]:
    returncode = observation.returncode
    if returncode is None:
        outcome: ProcessOutcome = "spawn_failed"
        exit_code = None
        signal = None
        status: CheckStatus = "error"
        error_message = observation.spawn_error or "Process failed to spawn."
        prefix = (
            "Process execution failed"
            if _process_started_before_failure(error_message)
            else "Could not start process"
        )
        error = CheckError("spawn_failed", f"{prefix}: {error_message}")
    elif returncode < 0:
        outcome = "signaled"
        exit_code = None
        signal = abs(returncode)
        status = "error"
        error_message = f"Process terminated by signal {signal}."
        error = CheckError(
            "terminated_by_signal",
            f"Primary process terminated by signal {signal}.",
        )
    else:
        outcome = "exited"
        exit_code = returncode
        signal = None
        status = "passed" if returncode == 0 else "failed"
        error_message = None
        error = None

    return (
        ProcessResult(
            role=cast(ProcessRole, observation.role),
            argv=observation.command,
            cwd=str(observation.cwd.resolve()),
            outcome=outcome,
            exit_code=exit_code,
            signal=signal,
            duration_ms=observation.duration_ms,
            stdout=_captured_stream(observation.stdout, output_format=output_format),
            stderr=_captured_stream(observation.stderr, output_format=output_format),
            error_message=error_message,
        ),
        status,
        error,
    )


def _process_started_before_failure(diagnostic: str) -> bool:
    return diagnostic.startswith(
        (
            "stdout reader construction failed:",
            "stderr reader construction failed:",
            "stdout reader start failed:",
            "stderr reader start failed:",
            "stdout drain failed:",
            "stderr drain failed:",
            "wait failed:",
        )
    )


def _captured_stream(
    raw: CapturedBytes | None,
    *,
    output_format: OutputFormat,
) -> CapturedText:
    if raw is None:
        return CapturedText(output_format == "json", "", False, 0)
    return capture_text(raw)
