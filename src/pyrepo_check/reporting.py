from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Literal

from pyrepo_check.execution import ExecutedCheck, ExecutionResult
from pyrepo_check.planning import (
    CheckName,
    OutputFormat,
    PlannedCheck,
    PlanningErrorCode,
    RunMode,
    RunPlan,
)


ReportKind = Literal["planning_error", "run"]
OverallStatus = Literal["passed", "failed", "error"]
CheckStatus = Literal["passed", "failed", "error"]
PlannedTestScope = Literal["not_selected", "partial", "complete"]
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
_OSC_PATTERN = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL)
_CHECK_NAMES = frozenset(("ruff", "annotations", "annotations-fix", "ty", "bandit", "pytest"))
_PLANNING_ERROR_CODES = frozenset(
    (
        "invalid_arguments",
        "invalid_project_config",
        "unknown_check",
        "unknown_target",
        "internal_planning_error",
    )
)
_RUN_MODES = frozenset(("focused", "strict_aggregate"))
_OVERALL_STATUSES = frozenset(("passed", "failed", "error"))
_CHECK_STATUSES = _OVERALL_STATUSES
_PLANNED_TEST_SCOPES = frozenset(("not_selected", "partial", "complete"))
_PLANNED_COVERAGE_SCOPES = frozenset(
    ("not_requested", "unavailable", "partial", "complete")
)
_PROCESS_ROLES = frozenset(
    ("primary", "pytest_preflight", "coverage_preflight", "coverage_json")
)
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
    test_shortcut: None
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
    pytest: None
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
    checks = tuple(
        _build_check_result(
            planned,
            observations.get(index),
            output_format=plan.output_format,
        )
        for index, planned in enumerate(plan.checks)
    )
    statuses = {check.status for check in checks}
    if "error" in statuses:
        overall_status: OverallStatus = "error"
    elif "failed" in statuses:
        overall_status = "failed"
    else:
        overall_status = "passed"

    pytest_selected = any(check.name == "pytest" for check in plan.checks)
    if not pytest_selected:
        planned_test_scope: PlannedTestScope = "not_selected"
        pytest_args = None
    elif plan.targets:
        planned_test_scope = "partial"
        pytest_args = plan.targets
    else:
        planned_test_scope = "complete"
        pytest_args = plan.targets

    return RunReportV1(
        schema_version=1,
        kind="run",
        project_root=str(project_root.resolve()),
        mode=plan.mode,
        overall_status=overall_status,
        complete="error" not in statuses,
        selection=Selection(
            checks=tuple(check.name for check in plan.checks),
            targets=plan.targets,
            test_shortcut=None,
            pytest_args=pytest_args,
            planned_test_scope=planned_test_scope,
            planned_coverage_scope="not_requested",
        ),
        checks=checks,
        pytest=None,
        coverage=None,
        advisories=_build_advisories(checks),
    )


def validate_report_v1(report: AgentReportV1) -> None:
    if report.schema_version != 1:
        _invalid("schema_version must be 1")
    if isinstance(report, PlanningErrorReportV1):
        _validate_planning_error_report(report)
        return
    if isinstance(report, RunReportV1):
        _validate_run_report(report)
        return
    _invalid("unsupported producer model")


def _validate_planning_error_report(report: PlanningErrorReportV1) -> None:
    if report.kind != "planning_error":
        _invalid("planning report kind must be planning_error")
    if report.overall_status != "error":
        _invalid("planning report overall_status must be error")
    if report.complete is not False:
        _invalid("planning report complete must be false")
    if report.error.code not in _PLANNING_ERROR_CODES:
        _invalid("unknown planning error code")


def _validate_run_report(report: RunReportV1) -> None:
    if report.kind != "run":
        _invalid("run report kind must be run")
    if report.mode not in _RUN_MODES:
        _invalid("unknown run mode")
    if report.overall_status not in _OVERALL_STATUSES:
        _invalid("unknown overall status")
    if not Path(report.project_root).is_absolute():
        _invalid("project_root must be absolute")
    if report.pytest is not None:
        _invalid("pytest must be null in milestone B")
    if report.coverage is not None:
        _invalid("coverage must be null in milestone B")

    selection = report.selection
    if any(name not in _CHECK_NAMES for name in selection.checks):
        _invalid("selection contains an unknown check name")
    if selection.test_shortcut is not None:
        _invalid("test_shortcut must be null in milestone B")
    if selection.planned_test_scope not in _PLANNED_TEST_SCOPES:
        _invalid("unknown planned test scope")
    if selection.planned_coverage_scope not in _PLANNED_COVERAGE_SCOPES:
        _invalid("unknown planned coverage scope")

    for check in report.checks:
        _validate_check_result(check)
    for advisory in report.advisories:
        if advisory.code not in _ADVISORY_CODES:
            _invalid("unknown advisory code")

    statuses = {check.status for check in report.checks}
    if "error" in statuses:
        expected_status: OverallStatus = "error"
    elif "failed" in statuses:
        expected_status = "failed"
    else:
        expected_status = "passed"
    if report.overall_status != expected_status:
        _invalid("overall_status violates check-status precedence")
    if report.complete is not ("error" not in statuses):
        _invalid("complete is inconsistent with check errors")


def _validate_check_result(check: CheckResult) -> None:
    if check.name not in _CHECK_NAMES:
        _invalid("unknown check name")
    if check.status not in _CHECK_STATUSES:
        _invalid("unknown check status")
    if (check.error is not None) is not (check.status == "error"):
        _invalid("check error must be present exactly for error status")
    if check.error is not None and check.error.code not in _CHECK_ERROR_CODES:
        _invalid("unknown check error code")
    for process in check.processes:
        _validate_process_result(process)


def _validate_process_result(process: ProcessResult) -> None:
    if process.role not in _PROCESS_ROLES:
        _invalid("unknown process role")
    if process.outcome not in _PROCESS_OUTCOMES:
        _invalid("unknown process outcome")
    if not Path(process.cwd).is_absolute():
        _invalid("process cwd must be absolute")
    if process.duration_ms < 0:
        _invalid("process duration_ms must be non-negative")
    _validate_captured_text(process.stdout)
    _validate_captured_text(process.stderr)

    if process.outcome == "exited":
        if process.exit_code is None or process.signal is not None:
            _invalid("exited process requires exit_code and null signal")
        if process.error_message is not None:
            _invalid("exited process requires null error_message")
    elif process.outcome == "signaled":
        if process.exit_code is not None or process.signal is None:
            _invalid("signaled process requires signal and null exit_code")
        if process.error_message is None:
            _invalid("signaled process requires error_message")
    elif process.outcome == "spawn_failed":
        if process.exit_code is not None or process.signal is not None:
            _invalid("spawn_failed process requires null exit_code and signal")
        if process.error_message is None:
            _invalid("spawn_failed process requires error_message")


def _validate_captured_text(captured: CapturedText) -> None:
    if captured.omitted_bytes < 0:
        _invalid("captured omitted_bytes must be non-negative")
    if not captured.captured:
        if captured.text or captured.truncated or captured.omitted_bytes != 0:
            _invalid("uncaptured text must be empty, untruncated, and omit zero bytes")
        return
    if captured.truncated is not (captured.omitted_bytes > 0):
        _invalid("captured truncation must match a positive omitted-byte count")


def _invalid(message: str) -> None:
    raise ReportingError(f"invalid report: {message}")


def capture_text(raw: bytes) -> CapturedText:
    retained = raw[-CAPTURE_LIMIT_BYTES:]
    omitted = len(raw) - len(retained)
    text = strip_terminal_sequences(retained.decode("utf-8", errors="replace"))
    return CapturedText(True, text, omitted > 0, omitted)


def strip_terminal_sequences(text: str) -> str:
    without_osc = _OSC_PATTERN.sub("", text)
    return _CSI_PATTERN.sub("", without_osc)


def _build_advisories(checks: tuple[CheckResult, ...]) -> tuple[Advisory, ...]:
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
    return tuple(sorted(advisories, key=lambda advisory: (advisory.code, advisory.message)))


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
                    f"mismatched or out-of-order observation for check "
                    f"{observation.planned.name}"
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
) -> CheckResult:
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

    process, status, error = _build_process_result(
        observation,
        output_format=output_format,
    )
    return CheckResult(
        name=planned.name,
        status=status,
        processes=(process,),
        error=error,
    )


def _build_process_result(
    observation: ExecutedCheck,
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
            role="primary",
            argv=observation.planned.command,
            cwd=str(observation.planned.cwd.resolve()),
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
