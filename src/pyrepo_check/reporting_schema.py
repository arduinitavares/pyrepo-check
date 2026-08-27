"""Immutable report models shared by schema v1 and internal schema v2."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Literal

from pyrepo_check.coverage_evidence import CoverageResult
from pyrepo_check.planning import CheckName, PlannedTestScope, PlanningErrorCode, RunMode
from pyrepo_check.pytest_evidence import PytestResult


ReportKind = Literal["planning_error", "run"]
OverallStatus = Literal["passed", "failed", "error"]
CheckStatus = Literal["passed", "failed", "error"]
PlannedCoverageScope = Literal["not_requested", "unavailable", "partial", "complete"]
ProcessRole = Literal[
    "primary",
    "pytest_preflight",
    "coverage_preflight",
    "coverage_json",
    "repository_safety",
    "uv_version",
    "environment_probe",
    "dependency_probe",
]
ProcessOutcome = Literal["exited", "signaled", "spawn_failed"]
CheckErrorCode = Literal[
    "spawn_failed",
    "terminated_by_signal",
    "pytest_preflight_failed",
    "pytest_evidence_error",
    "coverage_preflight_failed",
    "missing_primary_process",
    "cleanup_failed",
]
CheckErrorCodeV2 = Literal[
    "spawn_failed",
    "terminated_by_signal",
    "pytest_preflight_failed",
    "pytest_evidence_error",
    "coverage_preflight_failed",
    "missing_primary_process",
    "cleanup_failed",
    "repository_environment_unavailable",
    "check_dependency_missing",
    "check_dependency_incompatible",
    "check_dependency_shadowed",
    "check_dependency_unusable",
    "check_start_evidence_invalid",
    "check_execution_failed",
]
AdvisoryCode = Literal[
    "coverage_not_configured",
    "coverage_threshold_not_applied",
    "missing_test_reason",
    "output_truncated",
]
PlanningErrorCodeV2 = PlanningErrorCode
EnvironmentErrorCode = Literal[
    "repository_lock_missing",
    "uv_unavailable",
    "repository_environment_failed",
    "repository_python_unsupported",
    "unsafe_repository_environment",
    "environment_evidence_invalid",
    "repository_state_changed",
]
DependencyStatus = Literal[
    "available",
    "missing",
    "incompatible",
    "shadowed",
    "unusable",
    "unobserved",
]
MutationProtection = Literal["unobserved", "protected_files", "tracked_files"]
PythonVersion = tuple[int, int, int]


_CHECK_NAMES = frozenset(("ruff", "annotations", "annotations-fix", "ty", "bandit", "pytest"))
_CHECK_ORDER: tuple[CheckName, ...] = (
    "ruff",
    "annotations",
    "ty",
    "bandit",
    "pytest",
    "annotations-fix",
)
_RUN_MODES = frozenset(("focused", "strict_aggregate"))
_STATUSES = frozenset(("passed", "failed", "error"))
_PLANNED_TEST_SCOPES = frozenset(("not_selected", "partial", "complete"))
_PLANNED_COVERAGE_SCOPES = frozenset(
    ("not_requested", "unavailable", "partial", "complete")
)
_PROCESS_ROLES = frozenset(
    (
        "primary",
        "pytest_preflight",
        "coverage_preflight",
        "coverage_json",
        "repository_safety",
        "uv_version",
        "environment_probe",
        "dependency_probe",
    )
)
_PROCESS_OUTCOMES = frozenset(("exited", "signaled", "spawn_failed"))
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
_CHECK_ERROR_CODES_V2 = frozenset(
    (
        "spawn_failed",
        "terminated_by_signal",
        "pytest_preflight_failed",
        "pytest_evidence_error",
        "coverage_preflight_failed",
        "missing_primary_process",
        "cleanup_failed",
        "repository_environment_unavailable",
        "check_dependency_missing",
        "check_dependency_incompatible",
        "check_dependency_shadowed",
        "check_dependency_unusable",
        "check_start_evidence_invalid",
        "check_execution_failed",
    )
)
_ENVIRONMENT_ERROR_CODES = frozenset(
    (
        "repository_lock_missing",
        "uv_unavailable",
        "repository_environment_failed",
        "repository_python_unsupported",
        "unsafe_repository_environment",
        "environment_evidence_invalid",
        "repository_state_changed",
    )
)
_DEPENDENCY_NAMES = frozenset(("ruff", "ty", "bandit", "pytest", "coverage"))
_DEPENDENCY_STATUSES = frozenset(
    ("available", "missing", "incompatible", "shadowed", "unusable", "unobserved")
)
_MODULES = frozenset(("ruff", "ty", "bandit", "pytest", "coverage"))
_MUTATION_PROTECTION = frozenset(("unobserved", "protected_files", "tracked_files"))
_ADVISORY_CODES = frozenset(
    (
        "coverage_not_configured",
        "coverage_threshold_not_applied",
        "missing_test_reason",
        "output_truncated",
    )
)
_SHORTCUT_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReportingError(RuntimeError):
    """Raised when execution observations cannot form a valid report."""


@dataclass(frozen=True)
class CapturedText:
    captured: bool
    text: str
    truncated: bool
    omitted_bytes: int


@dataclass(frozen=True)
class ProcessResult:
    role: ProcessRole
    argv: tuple[str, ...]
    cwd: str
    outcome: ProcessOutcome
    exit_code: int | None
    signal: int | None
    duration_ms: int
    stdout: CapturedText
    stderr: CapturedText
    error_message: str | None


@dataclass(frozen=True)
class Selection:
    checks: tuple[CheckName, ...]
    targets: tuple[str, ...]
    test_shortcut: str | None
    pytest_args: tuple[str, ...] | None
    planned_test_scope: PlannedTestScope
    planned_coverage_scope: PlannedCoverageScope


@dataclass(frozen=True)
class Advisory:
    code: AdvisoryCode
    message: str
    hint: str | None


@dataclass(frozen=True)
class PythonEvidence:
    implementation: str
    version: PythonVersion
    executable: str


@dataclass(frozen=True)
class ToolEnvironmentEvidence:
    pyrepo_check_version: str
    python: PythonEvidence


@dataclass(frozen=True)
class PlanningErrorV2:
    code: PlanningErrorCodeV2
    message: str
    hint: str | None


@dataclass(frozen=True)
class RepositoryPythonSelectionEvidence:
    kind: Literal["default", "explicit"]
    request: str | None


@dataclass(frozen=True)
class LockEvidence:
    path: str
    status: Literal["current", "missing", "unverified"]


@dataclass(frozen=True)
class EnvironmentError:
    code: EnvironmentErrorCode
    message: str
    hint: str | None


@dataclass(frozen=True)
class CheckErrorV2:
    code: CheckErrorCodeV2
    message: str
    hint: str | None


@dataclass(frozen=True)
class DependencyEvidence:
    name: Literal["ruff", "ty", "bandit", "pytest", "coverage"]
    module: str
    required: str
    status: DependencyStatus
    version: str | None
    origin: str | None
    process: ProcessResult | None
    error: CheckErrorV2 | None


@dataclass(frozen=True)
class RepositoryEnvironmentEvidence:
    manager: Literal["uv"]
    manager_version: str | None
    path: str | None
    python_selection: RepositoryPythonSelectionEvidence
    python: PythonEvidence | None
    lock: LockEvidence
    dependency_selection: Literal["default"]
    mutation_protection: MutationProtection
    dependencies: tuple[DependencyEvidence, ...]
    processes: tuple[ProcessResult, ...]
    error: EnvironmentError | None


@dataclass(frozen=True)
class AnalysisPythonAuthorityEvidence:
    authority: Literal["repository_tool"]
    pyrepo_check_override: None


@dataclass(frozen=True)
class CheckStartEvidence:
    schema_version: Literal[1]
    check: CheckName
    module: Literal["ruff", "ty", "bandit", "pytest", "coverage"]
    arguments_sha256: str
    python: PythonEvidence


@dataclass(frozen=True)
class CheckResultV2:
    name: CheckName
    status: CheckStatus
    execution_environment: Literal["repository"] | None
    analysis_python_authority: AnalysisPythonAuthorityEvidence | None
    start_evidence: CheckStartEvidence | None
    processes: tuple[ProcessResult, ...]
    error: CheckErrorV2 | None


@dataclass(frozen=True)
class PlanningErrorReportV2:
    schema_version: Literal[2]
    kind: Literal["planning_error"]
    overall_status: Literal["error"]
    complete: Literal[False]
    tool_environment: ToolEnvironmentEvidence
    repository_environment: None
    error: PlanningErrorV2


@dataclass(frozen=True)
class RunReportV2:
    schema_version: Literal[2]
    kind: Literal["run"]
    project_root: str
    mode: RunMode
    overall_status: OverallStatus
    complete: bool
    tool_environment: ToolEnvironmentEvidence
    repository_environment: RepositoryEnvironmentEvidence
    selection: Selection
    checks: tuple[CheckResultV2, ...]
    pytest: PytestResult | None
    coverage: CoverageResult | None
    advisories: tuple[Advisory, ...]


AgentReportV2 = PlanningErrorReportV2 | RunReportV2


def validate_report_structure_v2(report: AgentReportV2) -> None:
    """Validate schema-v2 model structure without execution dependencies."""
    try:
        if not isinstance(report, (PlanningErrorReportV2, RunReportV2)):
            _invalid("unsupported producer model")
        if type(report.schema_version) is not int or report.schema_version != 2:
            _invalid("schema_version must be 2")
        if isinstance(report, PlanningErrorReportV2):
            _validate_planning_report(report)
            return
        _validate_run_report(report)
    except (AttributeError, TypeError) as error:
        _invalid(f"malformed report model: {error}")


def _validate_planning_report(report: PlanningErrorReportV2) -> None:
    if report.kind != "planning_error":
        _invalid("planning report kind must be planning_error")
    if report.overall_status != "error":
        _invalid("planning report overall_status must be error")
    if report.complete is not False:
        _invalid("planning report complete must be false")
    if report.repository_environment is not None:
        _invalid("planning report repository_environment must be null")
    if not isinstance(report.tool_environment, ToolEnvironmentEvidence):
        _invalid("tool_environment must be ToolEnvironmentEvidence")
    _validate_tool_environment(report.tool_environment)
    if not isinstance(report.error, PlanningErrorV2):
        _invalid("planning report error must be PlanningErrorV2")
    _validate_planning_error(report.error)


def _validate_run_report(report: RunReportV2) -> None:
    if report.kind != "run":
        _invalid("run report kind must be run")
    if not isinstance(report.project_root, str) or not Path(report.project_root).is_absolute():
        _invalid("project_root must be an absolute string")
    if report.mode not in _RUN_MODES:
        _invalid("unknown run mode")
    if report.overall_status not in _STATUSES:
        _invalid("unknown overall status")
    if type(report.complete) is not bool:
        _invalid("run complete must be boolean")
    if not isinstance(report.tool_environment, ToolEnvironmentEvidence):
        _invalid("tool_environment must be ToolEnvironmentEvidence")
    _validate_tool_environment(report.tool_environment)
    if not isinstance(report.repository_environment, RepositoryEnvironmentEvidence):
        _invalid("repository_environment must be RepositoryEnvironmentEvidence")
    _validate_repository_environment(report.repository_environment)
    _validate_selection(report.selection)
    if not isinstance(report.checks, tuple):
        _invalid("checks must be a tuple")
    for check in report.checks:
        _validate_check(check)
    if report.pytest is not None and not isinstance(report.pytest, PytestResult):
        _invalid("pytest must be null or PytestResult")
    if report.coverage is not None and not isinstance(report.coverage, CoverageResult):
        _invalid("coverage must be null or CoverageResult")
    if not isinstance(report.advisories, tuple):
        _invalid("advisories must be a tuple")
    for advisory in report.advisories:
        _validate_advisory(advisory)
    keys = tuple((advisory.code, advisory.message) for advisory in report.advisories)
    if len(set(keys)) != len(keys) or tuple(
        sorted(report.advisories, key=lambda item: (item.code, item.message))
    ) != report.advisories:
        _invalid("advisories must be unique and ordered by code then message")


def _validate_tool_environment(environment: ToolEnvironmentEvidence) -> None:
    if not isinstance(environment.pyrepo_check_version, str):
        _invalid("pyrepo_check_version must be a string")
    python = environment.python
    if not isinstance(python, PythonEvidence):
        _invalid("tool environment python must be PythonEvidence")
    _validate_python(python)


def _validate_python(python: PythonEvidence) -> None:
    if not isinstance(python.implementation, str):
        _invalid("Python implementation must be a string")
    if (
        not isinstance(python.version, tuple)
        or len(python.version) != 3
        or any(type(part) is not int or part < 0 for part in python.version)
    ):
        _invalid("Python version must be three non-negative integers")
    if not isinstance(python.executable, str):
        _invalid("Python executable must be a string")


def _validate_planning_error(error: PlanningErrorV2) -> None:
    if error.code not in _PLANNING_ERROR_CODES:
        _invalid("unknown planning error code")
    _validate_message_hint(error.message, error.hint, "planning error")


def _validate_repository_environment(environment: RepositoryEnvironmentEvidence) -> None:
    if environment.manager != "uv":
        _invalid("repository environment manager must be uv")
    _validate_optional_string(environment.manager_version, "manager_version")
    _validate_optional_string(environment.path, "repository environment path")
    if environment.path is not None and not Path(environment.path).is_absolute():
        _invalid("repository environment path must be absolute")
    selection = environment.python_selection
    if not isinstance(selection, RepositoryPythonSelectionEvidence):
        _invalid("python_selection must be RepositoryPythonSelectionEvidence")
    if selection.kind not in {"default", "explicit"}:
        _invalid("unknown repository Python selection kind")
    if selection.kind == "default":
        if selection.request is not None:
            _invalid("default repository Python selection request must be null")
    elif not isinstance(selection.request, str):
        _invalid("explicit repository Python selection requires a string request")
    if environment.python is not None:
        if not isinstance(environment.python, PythonEvidence):
            _invalid("repository Python must be null or PythonEvidence")
        _validate_python(environment.python)
    if not isinstance(environment.lock, LockEvidence):
        _invalid("lock must be LockEvidence")
    if not isinstance(environment.lock.path, str):
        _invalid("lock path must be a string")
    if environment.lock.status not in {"current", "missing", "unverified"}:
        _invalid("unknown lock status")
    if environment.dependency_selection != "default":
        _invalid("dependency_selection must be default")
    if environment.mutation_protection not in _MUTATION_PROTECTION:
        _invalid("unknown mutation protection")
    if not isinstance(environment.dependencies, tuple):
        _invalid("dependencies must be a tuple")
    for dependency in environment.dependencies:
        _validate_dependency(dependency)
    if not isinstance(environment.processes, tuple):
        _invalid("repository environment processes must be a tuple")
    for process in environment.processes:
        _validate_process(process)
    if environment.error is not None:
        if not isinstance(environment.error, EnvironmentError):
            _invalid("repository environment error must be null or EnvironmentError")
        if environment.error.code not in _ENVIRONMENT_ERROR_CODES:
            _invalid("unknown environment error code")
        _validate_message_hint(
            environment.error.message,
            environment.error.hint,
            "environment error",
        )


def _validate_dependency(dependency: DependencyEvidence) -> None:
    if not isinstance(dependency, DependencyEvidence):
        _invalid("dependencies must contain DependencyEvidence values")
    if dependency.name not in _DEPENDENCY_NAMES:
        _invalid("unknown dependency name")
    if not isinstance(dependency.module, str):
        _invalid("dependency module must be a string")
    if not isinstance(dependency.required, str):
        _invalid("dependency required range must be a string")
    if dependency.status not in _DEPENDENCY_STATUSES:
        _invalid("unknown dependency status")
    _validate_optional_string(dependency.version, "dependency version")
    _validate_optional_string(dependency.origin, "dependency origin")
    if dependency.process is not None:
        if not isinstance(dependency.process, ProcessResult):
            _invalid("dependency process must be null or ProcessResult")
        _validate_process(dependency.process)
    if dependency.error is not None:
        _validate_check_error(dependency.error)


def _validate_selection(selection: Selection) -> None:
    if not isinstance(selection, Selection):
        _invalid("selection must be Selection")
    if not isinstance(selection.checks, tuple) or any(
        name not in _CHECK_NAMES for name in selection.checks
    ):
        _invalid("selection contains an unknown check name")
    if tuple(name for name in _CHECK_ORDER if name in selection.checks) != selection.checks:
        _invalid("selection checks must use fixed unique order")
    if not isinstance(selection.targets, tuple) or any(
        not isinstance(target, str) for target in selection.targets
    ):
        _invalid("selection targets must be a tuple of strings")
    if selection.test_shortcut is not None and (
        not isinstance(selection.test_shortcut, str)
        or _SHORTCUT_NAME.fullmatch(selection.test_shortcut) is None
    ):
        _invalid("test_shortcut must be null or a valid name")
    if selection.pytest_args is not None and (
        not isinstance(selection.pytest_args, tuple)
        or any(not isinstance(argument, str) for argument in selection.pytest_args)
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
            _invalid("planned_test_scope must be not_selected without pytest")
        if selection.planned_coverage_scope != "not_requested":
            _invalid("planned coverage requires pytest selection")
    elif selection.test_shortcut is not None:
        if selection.checks != ("pytest",):
            _invalid("test_shortcut requires pytest-only selection")
        if selection.targets:
            _invalid("test_shortcut cannot coexist with direct targets")
        if not selection.pytest_args:
            _invalid("test_shortcut requires non-empty pytest_args")
        if selection.planned_test_scope != "partial":
            _invalid("test_shortcut requires partial planned test scope")
    else:
        if selection.pytest_args != selection.targets:
            _invalid("pytest_args must exactly match targets without a Test Shortcut")
        expected_scope = "partial" if selection.targets else "complete"
        if selection.planned_test_scope != expected_scope:
            _invalid("planned_test_scope contradicts pytest targets")


def _validate_check(check: CheckResultV2) -> None:
    if not isinstance(check, CheckResultV2):
        _invalid("checks must contain CheckResultV2 values")
    if check.name not in _CHECK_NAMES:
        _invalid("unknown check name")
    if check.status not in _STATUSES:
        _invalid("unknown check status")
    if check.execution_environment not in {None, "repository"}:
        _invalid("unknown execution environment")
    if check.analysis_python_authority is not None:
        authority = check.analysis_python_authority
        if not isinstance(authority, AnalysisPythonAuthorityEvidence):
            _invalid("analysis authority must be null or AnalysisPythonAuthorityEvidence")
        if authority.authority != "repository_tool" or authority.pyrepo_check_override is not None:
            _invalid("invalid analysis Python authority")
    if check.start_evidence is not None:
        _validate_start_evidence(check.start_evidence)
    if not isinstance(check.processes, tuple):
        _invalid("check processes must be a tuple")
    for process in check.processes:
        _validate_process(process)
    if check.error is not None:
        _validate_check_error(check.error)


def _validate_start_evidence(evidence: CheckStartEvidence) -> None:
    if not isinstance(evidence, CheckStartEvidence):
        _invalid("start evidence must be null or CheckStartEvidence")
    if type(evidence.schema_version) is not int or evidence.schema_version != 1:
        _invalid("start evidence schema_version must be 1")
    if evidence.check not in _CHECK_NAMES:
        _invalid("unknown start evidence check")
    if evidence.module not in _MODULES:
        _invalid("unknown start evidence module")
    if not isinstance(evidence.arguments_sha256, str) or _SHA256.fullmatch(
        evidence.arguments_sha256
    ) is None:
        _invalid("start evidence digest must be 64 lowercase hexadecimal characters")
    if not isinstance(evidence.python, PythonEvidence):
        _invalid("start evidence Python must be PythonEvidence")
    _validate_python(evidence.python)


def _validate_process(process: ProcessResult) -> None:
    if not isinstance(process, ProcessResult):
        _invalid("process must be ProcessResult")
    if process.role not in _PROCESS_ROLES:
        _invalid("unknown process role")
    if not isinstance(process.argv, tuple) or any(
        not isinstance(argument, str) for argument in process.argv
    ):
        _invalid("process argv must be a tuple of strings")
    if not isinstance(process.cwd, str) or not Path(process.cwd).is_absolute():
        _invalid("process cwd must be an absolute string")
    if process.outcome not in _PROCESS_OUTCOMES:
        _invalid("unknown process outcome")
    if type(process.duration_ms) is not int or process.duration_ms < 0:
        _invalid("process duration_ms must be a non-negative integer")
    _validate_captured_text(process.stdout)
    _validate_captured_text(process.stderr)
    _validate_optional_string(process.error_message, "process error_message")
    if process.outcome == "exited":
        if type(process.exit_code) is not int or process.exit_code < 0:
            _invalid("exited process requires a non-negative integer exit_code")
        if process.signal is not None or process.error_message is not None:
            _invalid("exited process requires null signal and error_message")
    elif process.outcome == "signaled":
        if process.exit_code is not None or type(process.signal) is not int or process.signal <= 0:
            _invalid("signaled process requires positive signal and null exit_code")
        if process.error_message is None:
            _invalid("signaled process requires error_message")
    elif (
        process.exit_code is not None
        or process.signal is not None
        or process.error_message is None
    ):
        _invalid("spawn_failed process requires null codes and error_message")


def _validate_captured_text(captured: CapturedText) -> None:
    if not isinstance(captured, CapturedText):
        _invalid("captured stream must be CapturedText")
    if type(captured.captured) is not bool or type(captured.truncated) is not bool:
        _invalid("captured stream flags must be boolean")
    if not isinstance(captured.text, str):
        _invalid("captured stream text must be a string")
    if type(captured.omitted_bytes) is not int or captured.omitted_bytes < 0:
        _invalid("captured omitted_bytes must be a non-negative integer")
    if not captured.captured and (
        captured.text or captured.truncated or captured.omitted_bytes != 0
    ):
        _invalid("uncaptured stream must be empty")
    if captured.captured and captured.truncated is not (captured.omitted_bytes > 0):
        _invalid("captured truncation must match omitted bytes")


def _validate_check_error(error: CheckErrorV2) -> None:
    if not isinstance(error, CheckErrorV2):
        _invalid("check error must be CheckErrorV2")
    if error.code not in _CHECK_ERROR_CODES_V2:
        _invalid("unknown check error code")
    _validate_message_hint(error.message, error.hint, "check error")


def _validate_advisory(advisory: Advisory) -> None:
    if not isinstance(advisory, Advisory):
        _invalid("advisories must contain Advisory values")
    if advisory.code not in _ADVISORY_CODES:
        _invalid("unknown advisory code")
    _validate_message_hint(advisory.message, advisory.hint, "advisory")


def _validate_message_hint(message: object, hint: object, field: str) -> None:
    if not isinstance(message, str):
        _invalid(f"{field} message must be a string")
    if hint is not None and not isinstance(hint, str):
        _invalid(f"{field} hint must be null or a string")


def _validate_optional_string(value: object, field: str) -> None:
    if value is not None and not isinstance(value, str):
        _invalid(f"{field} must be null or a string")


def _invalid(message: str) -> None:
    raise ReportingError(f"invalid report: {message}")
