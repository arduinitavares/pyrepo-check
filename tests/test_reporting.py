from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, cast

import pytest

from pyrepo_check.execution import ExecutedCheck, ExecutionResult
from pyrepo_check.planning import CheckName, OutputFormat, PlannedCheck, RunMode, RunPlan
from pyrepo_check.reporting import (
    Advisory,
    AgentReportV1,
    CapturedText,
    CheckError,
    CheckResult,
    PlanningError,
    PlanningErrorReportV1,
    ProcessResult,
    ReportingError,
    RunReportV1,
    Selection,
    build_planning_error_report,
    build_run_report,
    validate_report_v1,
)


def planned_check(root: Path, name: CheckName) -> PlannedCheck:
    return PlannedCheck(
        name=name,
        command=("uv", "run", "python", "-m", name),
        cwd=root,
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
    return ExecutedCheck(
        planned=planned,
        returncode=returncode,
        duration_ms=duration_ms,
        stdout=stdout,
        stderr=stderr,
        spawn_error=spawn_error,
    )


def run_plan(
    checks: tuple[PlannedCheck, ...],
    *,
    targets: tuple[str, ...] = (),
    mode: RunMode = "focused",
    output_format: OutputFormat = "json",
) -> RunPlan:
    return RunPlan(
        mode=mode,
        targets=targets,
        checks=checks,
        output_format=output_format,
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

    assert report == RunReportV1(
        schema_version=1,
        kind="run",
        project_root=str(unresolved_root.resolve()),
        mode="focused",
        overall_status="error",
        complete=False,
        selection=Selection(
            checks=("ruff", "ty", "bandit", "pytest"),
            targets=("tests/a.py", "tests/a.py"),
            test_shortcut=None,
            pytest_args=("tests/a.py", "tests/a.py"),
            planned_test_scope="partial",
            planned_coverage_scope="not_requested",
        ),
        checks=(
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
            CheckResult(
                name="pytest",
                status="error",
                processes=(
                    ProcessResult(
                        role="primary",
                        argv=pytest_check.command,
                        cwd=str(unresolved_root.resolve()),
                        outcome="spawn_failed",
                        exit_code=None,
                        signal=None,
                        duration_ms=14,
                        stdout=CapturedText(True, "", False, 0),
                        stderr=CapturedText(True, "", False, 0),
                        error_message="FileNotFoundError: uv",
                    ),
                ),
                error=CheckError(
                    "spawn_failed",
                    "Could not start process: FileNotFoundError: uv",
                ),
            ),
        ),
        pytest=None,
        coverage=None,
        advisories=(),
    )


def test_terminal_observations_are_explicitly_uncaptured(tmp_path: Path) -> None:
    check = planned_check(tmp_path, "ruff")
    plan = run_plan((check,), output_format="terminal")
    execution = ExecutionResult(
        checks=(
            executed_check(check, 0, stdout=None, stderr=None),
        ),
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
    assert report.pytest is None
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
            b"discarded-prefix" + b"tail" * 16_384,
            "tail" * 16_384,
            True,
            len(b"discarded-prefix"),
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
    "negative-duration",
    "negative-omitted-bytes",
    "relative-project-root",
    "relative-process-cwd",
    "exited-missing-exit-code",
    "exited-unexpected-signal",
    "exited-unexpected-error-message",
    "signaled-unexpected-exit-code",
    "signaled-missing-signal",
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
)


@pytest.mark.parametrize("case", INVALID_REPORT_CASES)
def test_rejects_invalid_producer_report(tmp_path: Path, case: str) -> None:
    invalid_report = make_invalid_report(tmp_path, case)

    with pytest.raises(ReportingError, match=r"^invalid report:"):
        validate_report_v1(invalid_report)


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
    if case == "negative-duration":
        return replace_process(passed, replace(process, duration_ms=-1))
    if case == "negative-omitted-bytes":
        stdout = replace(process.stdout, omitted_bytes=-1)
        return replace_process(passed, replace(process, stdout=stdout))
    if case == "relative-project-root":
        return replace(passed, project_root="relative/project")
    if case == "relative-process-cwd":
        return replace_process(passed, replace(process, cwd="relative/project"))
    if case == "exited-missing-exit-code":
        return replace_process(passed, replace(process, exit_code=None))
    if case == "exited-unexpected-signal":
        return replace_process(passed, replace(process, signal=9))
    if case == "exited-unexpected-error-message":
        return replace_process(passed, replace(process, error_message="unexpected"))
    if case == "signaled-unexpected-exit-code":
        return replace_process(signaled, replace(signal_process, exit_code=1))
    if case == "signaled-missing-signal":
        return replace_process(signaled, replace(signal_process, signal=None))
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
    raise AssertionError(f"unhandled invalid report case: {case}")


def replace_process(report: RunReportV1, process: ProcessResult) -> RunReportV1:
    result = replace(report.checks[0], processes=(process,))
    return replace(report, checks=(result,))
