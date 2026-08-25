from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Literal, cast

from pyrepo_check.config import TEST_SHORTCUT_NAME_PATTERN
from pyrepo_check.execution import ExecutedCheck, ExecutedProcess, ExecutionResult
from pyrepo_check.planning import (
    CheckName,
    OutputFormat,
    PlannedCheck,
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


ReportKind = Literal["planning_error", "run"]
OverallStatus = Literal["passed", "failed", "error"]
CheckStatus = Literal["passed", "failed", "error"]
PlannedCoverageScope = Literal["not_requested", "unavailable", "partial", "complete"]
ProcessRole = Literal["primary", "pytest_preflight", "coverage_preflight", "coverage_json"]
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
AdvisoryCode = Literal[
    "coverage_not_configured",
    "coverage_threshold_not_applied",
    "missing_test_reason",
    "output_truncated",
]


CAPTURE_LIMIT_BYTES = 65_536
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


class ReportingError(RuntimeError):
    """Raised when execution observations cannot form a valid report."""


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
    coverage: None
    advisories: tuple[Advisory, ...]


AgentReportV1 = PlanningErrorReportV1 | RunReportV1


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
        not _run_complete(checks, pytest_result)
        or "error" in statuses
        or (pytest_result is not None and pytest_result.status == "error")
    ):
        overall_status: OverallStatus = "error"
    elif "failed" in statuses or (pytest_result is not None and pytest_result.status == "failed"):
        overall_status = "failed"
    else:
        overall_status = "passed"

    return RunReportV1(
        schema_version=1,
        kind="run",
        project_root=str(project_root.resolve()),
        mode=plan.mode,
        overall_status=overall_status,
        complete=_run_complete(checks, pytest_result),
        selection=Selection(
            checks=tuple(check.name for check in plan.checks),
            targets=plan.targets,
            test_shortcut=plan.test_shortcut,
            pytest_args=plan.pytest_args,
            planned_test_scope=plan.planned_test_scope,
            planned_coverage_scope="not_requested",
        ),
        checks=checks,
        pytest=pytest_result,
        coverage=None,
        advisories=_build_advisories(checks, pytest_result),
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

    completeness = "complete" if report.complete else "incomplete"
    lines = ["", f"==> pyrepo-check summary: {report.overall_status} ({completeness})"]
    if report.pytest is not None and (
        not report.pytest.complete or report.pytest.status == "error"
    ):
        if report.pytest.error is None:
            lines.append("    error: pytest evidence is incomplete.")
        else:
            lines.append(f"    error: pytest evidence: {report.pytest.error.message}")
    for check in report.checks:
        if check.status != "error":
            continue
        error = check.error
        if error is not None:
            lines.append(f"    error: {check.name}: {error.message}")
        _append_helper_diagnostics(lines, check)
    for check in report.checks:
        if check.status != "failed":
            continue
        _append_failed_line(lines, check.name, _first_positive_exit_code(check.processes))
    pytest_check = next((check for check in report.checks if check.name == "pytest"), None)
    if (
        report.pytest is not None
        and report.pytest.status == "failed"
        and (pytest_check is None or pytest_check.status != "failed")
    ):
        _append_failed_line(lines, "pytest", report.pytest.exit_code)
    if report.pytest is not None and report.pytest.evidence is not None:
        for outcome in report.pytest.evidence.special_outcomes:
            reason = f" ({outcome.reason})" if outcome.reason else ""
            lines.append(f"    special: pytest {outcome.outcome}: {outcome.nodeid}{reason}")
        for slow_test in report.pytest.evidence.slowest:
            lines.append(f"    slow: pytest {slow_test.nodeid} ({slow_test.duration_ms} ms)")
    for advisory in report.advisories:
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


def _append_helper_diagnostics(lines: list[str], check: CheckResult) -> None:
    for process in check.processes:
        if process.role not in {"pytest_preflight", "coverage_preflight", "coverage_json"}:
            continue
        for stream_name, captured in (("stdout", process.stdout), ("stderr", process.stderr)):
            if not captured.captured or not captured.text:
                continue
            for line in captured.text.rstrip("\n").splitlines() or [captured.text]:
                lines.append(
                    f"    diagnostic: {check.name} {process.role} {stream_name}: {line}"
                )


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
        "coverage": report.coverage,
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
    if report.coverage is not None:
        _invalid("coverage must be null before coverage execution")

    pytest_selected = _validate_selection(report.selection)
    selection = report.selection
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
        _validate_check_result(check, report.pytest if check.name == "pytest" else None)
        for check in report.checks
    }
    _validate_advisories(report.advisories, report.checks, report.pytest)

    expected_complete = _run_complete(report.checks, report.pytest)
    if (
        not expected_complete
        or "error" in statuses
        or (report.pytest is not None and report.pytest.status == "error")
    ):
        expected_status: OverallStatus = "error"
    elif "failed" in statuses or (report.pytest is not None and report.pytest.status == "failed"):
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
    if selection.planned_coverage_scope != "not_requested":
        _invalid("planned_coverage_scope must be not_requested before coverage planning")

    pytest_selected = "pytest" in selection.checks
    if not pytest_selected:
        if selection.test_shortcut is not None:
            _invalid("test_shortcut requires pytest selection")
        if selection.pytest_args is not None:
            _invalid("pytest_args must be null when pytest is not selected")
        if selection.planned_test_scope != "not_selected":
            _invalid("planned_test_scope must be not_selected when pytest is not selected")
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


def _validate_check_result(check: CheckResult, pytest_result: PytestResult | None) -> CheckStatus:
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
        return _validate_pytest_check_result(check, pytest_result)
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


def _validate_pytest_check_result(check: CheckResult, result: PytestResult | None) -> CheckStatus:
    if result is None:
        _invalid("pytest check requires a pytest result")
        return check.status
    processes = check.processes
    if len(processes) > 2:
        _invalid("pytest processes must be preflight then optional primary")
    for process in processes:
        _validate_process_result(process)
    preflight = processes[0] if processes else None
    primary = processes[1] if len(processes) == 2 else None
    if preflight is not None and preflight.role != "pytest_preflight":
        _invalid("pytest processes must start with pytest_preflight")
    if primary is not None and primary.role != "primary":
        _invalid("pytest processes must be preflight then optional primary")
    _validate_pytest_execution_shape(result, preflight, primary)

    if check.error is not None and check.error.code == "cleanup_failed":
        if check.status != "error":
            _invalid("cleanup failure requires pytest check error")
        return "error"
    expected_error = _expected_pytest_check_error(result)
    if expected_error is None:
        if check.status != result.status or check.error is not None:
            _invalid("pytest check must match successful pytest evidence")
    elif check.status != "error" or check.error is None or check.error.code != expected_error:
        _invalid("pytest check error contradicts pytest evidence")
    return check.status


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
    if ("planned_selector" in result.scope_reasons) != (
        selection.planned_test_scope == "partial"
    ):
        _invalid("pytest planned_selector must match planned test scope")
    if ("incomplete_session" in result.scope_reasons) != (not result.complete):
        _invalid("pytest incomplete_session must match completeness")
    if result.error is not None and result.error.code == "session_incomplete" and (
        result.status != "failed" or result.complete or result.evidence is None
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
    if len({item.nodeid for item in evidence.special_outcomes}) != len(
        evidence.special_outcomes
    ):
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
    if advisories != _build_advisories(checks, pytest_result):
        _invalid("advisories must exactly match report evidence")


def _invalid(message: str) -> None:
    raise ReportingError(f"invalid report: {message}")


def _validate_exact_int(value: object, field: str) -> int:
    if type(value) is not int:
        _invalid(f"{field} must be an integer")
    return cast(int, value)


def capture_text(raw: bytes) -> CapturedText:
    retained = raw[-CAPTURE_LIMIT_BYTES:]
    omitted = len(raw) - len(retained)
    text = strip_terminal_sequences(retained.decode("utf-8", errors="replace"))
    return CapturedText(True, text, omitted > 0, omitted)


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


def _run_complete(checks: tuple[CheckResult, ...], pytest_result: PytestResult | None) -> bool:
    return all(check.status != "error" for check in checks) and (
        pytest_result is None or pytest_result.complete
    )


def _build_advisories(
    checks: tuple[CheckResult, ...], pytest_result: PytestResult | None
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
    unique = {(advisory.code, advisory.message): advisory for advisory in advisories}
    return tuple(sorted(unique.values(), key=lambda advisory: (advisory.code, advisory.message)))


def _match_observations(
    planned_checks: tuple[PlannedCheck, ...],
    executed_checks: tuple[ExecutedCheck, ...],
) -> dict[int, ExecutedCheck]:
    matched: dict[int, ExecutedCheck] = {}
    seen: set[PlannedCheck] = set()
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
    planned: PlannedCheck,
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
    if len(processes) == 1:
        return processes
    if len(processes) == 2 and processes[1].role == "primary":
        return processes
    raise ReportingError("pytest execution process order must be preflight then primary")


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
    if result.error is None:
        return None
    if result.status != "error":
        return None
    if result.error.code == "not_started":
        return CheckError("missing_primary_process", "No primary process observation was recorded.")
    if result.error.code == "spawn_failed":
        return CheckError("spawn_failed", f"Could not start pytest: {result.error.message}")
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
        error = CheckError("spawn_failed", f"Could not start process: {error_message}")
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


def _captured_stream(
    raw: bytes | None,
    *,
    output_format: OutputFormat,
) -> CapturedText:
    if raw is None:
        return CapturedText(output_format == "json", "", False, 0)
    return capture_text(raw)
