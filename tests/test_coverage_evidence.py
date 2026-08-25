from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pyrepo_check.coverage_evidence import coverage_gate_policy
from pyrepo_check.planning import (
    CoverageExecutionPlan,
    PlannedCheck,
    PytestExecutionPlan,
    RunMode,
    RunPlan,
)
from pyrepo_check.pytest_evidence import (
    PytestCounts,
    PytestEvidence,
    PytestResult,
    TestScope,
    TestScopeReason,
)


def _plan(
    *,
    mode: RunMode = "strict_aggregate",
    targets: tuple[str, ...] = (),
    shortcut: str | None = None,
    fail_under: int | float | None = 90,
) -> RunPlan:
    coverage = CoverageExecutionPlan(("consumer-python",), Path("pyproject.toml"), fail_under)
    pytest = PytestExecutionPlan(("consumer-python",), (), coverage=coverage)
    check = PlannedCheck("pytest", ("consumer-python", "-m", "pytest"), Path("."), pytest)
    return RunPlan(
        mode=mode,
        targets=targets,
        checks=(check,),
        test_shortcut=shortcut,
        pytest_args=(),
        planned_test_scope="complete" if shortcut is None and not targets else "partial",
        planned_coverage_scope="complete" if shortcut is None and not targets else "partial",
    )


def _pytest_result(
    *,
    exit_code: int = 0,
    complete: bool = True,
    scope: TestScope = "complete",
    scope_reasons: tuple[TestScopeReason, ...] = (),
    evidence: PytestEvidence | None = None,
) -> PytestResult:
    return PytestResult(
        status="passed" if exit_code == 0 else "failed",
        complete=complete,
        scope=scope,
        scope_reasons=scope_reasons,
        pytest_version="8.4.2",
        exit_code=exit_code,
        evidence=evidence
        if evidence is not None
        else PytestEvidence((), 1, 0, PytestCounts(1, 0, 0, 0, 0, 0), (), (), (), ()),
        error=None,
    )


@pytest.mark.parametrize(
    ("plan", "pytest_result", "evidence_complete", "expected"),
    (
        (_plan(), _pytest_result(), True, (True, None, False)),
        (_plan(fail_under=None), _pytest_result(), True, (True, "not_configured", False)),
        (_plan(mode="focused"), _pytest_result(), True, (False, "focused_run", True)),
        (_plan(mode="focused", targets=("src",)), _pytest_result(), True, (False, "partial_run", True)),
        (_plan(mode="focused", shortcut="unit"), _pytest_result(), True, (False, "partial_run", True)),
        (_plan(), _pytest_result(scope="partial", scope_reasons=("deselected_tests",)), True, (False, "partial_run", True)),
        (_plan(), _pytest_result(exit_code=0), True, (True, None, False)),
        (_plan(), _pytest_result(exit_code=1), True, (False, "pytest_failed", True)),
        (_plan(), _pytest_result(exit_code=2, complete=False, scope="partial", scope_reasons=("incomplete_session",)), True, (False, "pytest_incomplete", True)),
        (_plan(), _pytest_result(exit_code=3, complete=False, scope="partial", scope_reasons=("incomplete_session",)), True, (False, "pytest_incomplete", True)),
        (_plan(), _pytest_result(exit_code=4, complete=False, scope="partial", scope_reasons=("incomplete_session",)), True, (False, "pytest_incomplete", True)),
        (_plan(), _pytest_result(exit_code=5), True, (False, "no_tests_collected", True)),
        (_plan(), replace(_pytest_result(), evidence=None), True, (False, "evidence_error", True)),
        (_plan(), _pytest_result(complete=False, scope="partial", scope_reasons=("incomplete_session",)), True, (False, "pytest_incomplete", True)),
        (_plan(), _pytest_result(), False, (False, "evidence_error", True)),
    ),
    ids=(
        "eligible-strict",
        "unconfigured",
        "focused-target-free",
        "direct-target",
        "shortcut",
        "observed-partial",
        "exit-zero",
        "exit-one",
        "exit-two",
        "exit-three",
        "exit-four",
        "exit-five",
        "missing-pytest-evidence",
        "incomplete-session",
        "incomplete-coverage-evidence",
    ),
)
def test_coverage_gate_policy_matrix(
    plan: RunPlan,
    pytest_result: PytestResult,
    evidence_complete: bool,
    expected: tuple[bool, str | None, bool],
) -> None:
    policy = coverage_gate_policy(plan, pytest_result, evidence_complete)

    assert (policy.gate_eligible, policy.skipped_reason, policy.force_fail_under_zero) == expected


@pytest.mark.parametrize(
    ("plan", "pytest_result", "evidence_complete", "expected_reason"),
    (
        (_plan(fail_under=None), _pytest_result(exit_code=5), False, "evidence_error"),
        (_plan(), _pytest_result(exit_code=5), False, "evidence_error"),
        (_plan(), _pytest_result(complete=False), False, "evidence_error"),
        (_plan(), _pytest_result(exit_code=1), False, "evidence_error"),
        (_plan(), _pytest_result(scope="partial"), False, "evidence_error"),
        (_plan(mode="focused"), _pytest_result(), False, "evidence_error"),
        (_plan(fail_under=None), _pytest_result(exit_code=5), True, "not_configured"),
        (_plan(fail_under=None), _pytest_result(complete=False), True, "not_configured"),
        (_plan(fail_under=None), _pytest_result(exit_code=1), True, "not_configured"),
        (_plan(fail_under=None), _pytest_result(scope="partial"), True, "not_configured"),
        (_plan(mode="focused", fail_under=None), _pytest_result(), True, "not_configured"),
        (_plan(), _pytest_result(exit_code=5, complete=False, scope="partial", scope_reasons=("incomplete_session",)), True, "no_tests_collected"),
        (_plan(), _pytest_result(exit_code=5, scope="partial"), True, "no_tests_collected"),
        (_plan(mode="focused"), _pytest_result(exit_code=5), True, "no_tests_collected"),
        (_plan(), _pytest_result(exit_code=1, complete=False, scope="partial", scope_reasons=("incomplete_session",)), True, "pytest_incomplete"),
        (_plan(), _pytest_result(complete=False, scope="partial"), True, "pytest_incomplete"),
        (_plan(mode="focused"), _pytest_result(complete=False), True, "pytest_incomplete"),
        (_plan(), _pytest_result(exit_code=1, scope="partial", scope_reasons=("deselected_tests",)), True, "pytest_failed"),
        (_plan(mode="focused"), _pytest_result(exit_code=1), True, "pytest_failed"),
        (_plan(mode="focused"), _pytest_result(scope="partial", scope_reasons=("deselected_tests",)), True, "partial_run"),
    ),
    ids=(
        "evidence-over-not-configured-and-no-tests",
        "evidence-over-no-tests",
        "evidence-over-incomplete",
        "evidence-over-failed",
        "evidence-over-partial",
        "evidence-over-focused",
        "not-configured-over-no-tests",
        "not-configured-over-incomplete",
        "not-configured-over-failed",
        "not-configured-over-partial",
        "not-configured-over-focused",
        "no-tests-over-incomplete",
        "no-tests-over-partial",
        "no-tests-over-focused",
        "incomplete-over-failed",
        "incomplete-over-partial",
        "incomplete-over-focused",
        "failed-over-partial",
        "failed-over-focused",
        "partial-over-focused",
    ),
)
def test_coverage_gate_policy_uses_exact_skip_reason_precedence(
    plan: RunPlan,
    pytest_result: PytestResult,
    evidence_complete: bool,
    expected_reason: str,
) -> None:
    policy = coverage_gate_policy(plan, pytest_result, evidence_complete)

    assert policy.gate_eligible is False
    assert policy.skipped_reason == expected_reason
    assert policy.force_fail_under_zero is True


def test_coverage_gate_policy_treats_invalid_pytest_evidence_as_an_evidence_error() -> None:
    result = replace(_pytest_result(), evidence=None)

    policy = coverage_gate_policy(_plan(), result, True)

    assert (policy.gate_eligible, policy.skipped_reason, policy.force_fail_under_zero) == (
        False,
        "evidence_error",
        True,
    )


def test_coverage_gate_policy_keeps_unconfigured_strict_evidence_eligible_but_not_failed_pytest() -> None:
    unconfigured = _plan(fail_under=None)

    complete = coverage_gate_policy(unconfigured, _pytest_result(), True)
    failed = coverage_gate_policy(unconfigured, _pytest_result(exit_code=1), True)

    assert (complete.gate_eligible, complete.skipped_reason, complete.force_fail_under_zero) == (
        True,
        "not_configured",
        False,
    )
    assert (failed.gate_eligible, failed.skipped_reason, failed.force_fail_under_zero) == (
        False,
        "not_configured",
        True,
    )
