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
_PLANNED_COVERAGE_SCOPES = frozenset(
    ("not_requested", "unavailable", "partial", "complete")
)
_PROCESS_ROLES = frozenset(("primary",))
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
            test_shortcut=plan.test_shortcut,
            pytest_args=plan.pytest_args,
            planned_test_scope=plan.planned_test_scope,
            planned_coverage_scope="not_requested",
        ),
        checks=checks,
        pytest=None,
        coverage=None,
        advisories=_build_advisories(checks),
    )


def validate_report_v1(report: AgentReportV1) -> None:
    _validate_exact_int(report.schema_version, "schema_version")
    if report.schema_version != 1:
        _invalid("schema_version must be 1")
    if isinstance(report, PlanningErrorReportV1):
        _validate_planning_error_report(report)
        return
    if isinstance(report, RunReportV1):
        _validate_run_report(report)
        return
    _invalid("unsupported producer model")


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
    for check in report.checks:
        if check.status != "error":
            continue
        error = check.error
        if error is not None:
            lines.append(f"    error: {check.name}: {error.message}")
    for check in report.checks:
        if check.status != "failed":
            continue
        exit_code = _first_positive_exit_code(check.processes)
        if exit_code is None:
            lines.append(f"    failed: {check.name}")
        else:
            lines.append(f"    failed: {check.name} (exit {exit_code})")
    for advisory in report.advisories:
        lines.append(f"    advisory: {advisory.message}")
    passed_checks = [check.name for check in report.checks if check.status == "passed"]
    if passed_checks:
        lines.append(f"    passed: {', '.join(passed_checks)}")
    return "\n".join(lines) + "\n"


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
        "pytest": report.pytest,
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
        _invalid("pytest must be null before structured pytest evidence")
    if report.coverage is not None:
        _invalid("coverage must be null before coverage execution")

    selection = report.selection
    if any(name not in _CHECK_NAMES for name in selection.checks):
        _invalid("selection contains an unknown check name")
    if not isinstance(selection.targets, tuple) or any(
        not isinstance(target, str) for target in selection.targets
    ):
        _invalid("targets must be a tuple of strings")
    if selection.planned_test_scope not in _PLANNED_TEST_SCOPES:
        _invalid("unknown planned test scope")
    if selection.planned_coverage_scope not in _PLANNED_COVERAGE_SCOPES:
        _invalid("unknown planned coverage scope")
    if selection.planned_coverage_scope != "not_requested":
        _invalid("planned_coverage_scope must be not_requested before coverage planning")

    emitted_names = tuple(check.name for check in report.checks)
    if selection.checks != emitted_names:
        _invalid("selection checks must exactly match emitted checks")
    if len(set(selection.checks)) != len(selection.checks):
        _invalid("selection checks must be unique")
    canonical_selection = tuple(
        name for name in _B_CANONICAL_CHECK_ORDER if name in selection.checks
    )
    if selection.checks != canonical_selection:
        _invalid("selection checks must use canonical order")

    shortcut = selection.test_shortcut
    if shortcut is not None and (
        not isinstance(shortcut, str)
        or TEST_SHORTCUT_NAME_PATTERN.fullmatch(shortcut) is None
    ):
        _invalid("test_shortcut must be null or a valid Test Shortcut name")

    if selection.pytest_args is not None and (
        not isinstance(selection.pytest_args, tuple)
        or any(not isinstance(arg, str) for arg in selection.pytest_args)
    ):
        _invalid("pytest_args must be null or a tuple of strings")

    pytest_selected = "pytest" in selection.checks
    if not pytest_selected:
        if shortcut is not None:
            _invalid("test_shortcut requires pytest selection")
        if selection.pytest_args is not None:
            _invalid("pytest_args must be null when pytest is not selected")
        if selection.planned_test_scope != "not_selected":
            _invalid("planned_test_scope must be not_selected when pytest is not selected")
    elif shortcut is not None:
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
        expected_scope: PlannedTestScope = (
            "partial" if selection.targets else "complete"
        )
        if selection.planned_test_scope != expected_scope:
            _invalid("planned_test_scope is inconsistent with pytest selection")

    statuses = {_validate_check_result(check) for check in report.checks}
    for advisory in report.advisories:
        if advisory.code not in _ADVISORY_CODES:
            _invalid("unknown advisory code")

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


def _validate_check_result(check: CheckResult) -> CheckStatus:
    if check.name not in _CHECK_NAMES:
        _invalid("unknown check name")
    if check.status not in _CHECK_STATUSES:
        _invalid("unknown check status")
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


def _validate_process_result(process: ProcessResult) -> None:
    if process.role not in _PROCESS_ROLES:
        _invalid("unknown process role")
    if process.outcome not in _PROCESS_OUTCOMES:
        _invalid("unknown process outcome")
    if not Path(process.cwd).is_absolute():
        _invalid("process cwd must be absolute")
    _validate_exact_int(process.duration_ms, "process duration_ms")
    if process.duration_ms < 0:
        _invalid("process duration_ms must be non-negative")
    _validate_captured_text(process.stdout)
    _validate_captured_text(process.stderr)

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
    _validate_exact_int(captured.omitted_bytes, "captured omitted_bytes")
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

    if len(observation.processes) != 1:
        raise ReportingError("ordinary check must contain exactly one primary process")
    process, status, error = _build_process_result(
        observation.processes[0],
        output_format=output_format,
    )
    return CheckResult(
        name=planned.name,
        status=status,
        processes=(process,),
        error=error,
    )


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
