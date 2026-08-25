"""Immutable Coverage.py evidence types and threshold gate policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pyrepo_check.planning import RunPlan
    from pyrepo_check.pytest_evidence import PytestResult


CoverageStatus = Literal["passed", "failed", "guidance", "error"]
CoverageScope = Literal["partial", "complete"]
CoverageThresholdSkipReason = Literal[
    "evidence_error",
    "not_configured",
    "focused_run",
    "partial_run",
    "pytest_failed",
    "pytest_incomplete",
    "no_tests_collected",
]
CoverageErrorCode = Literal[
    "unsupported_python",
    "module_unavailable",
    "unsupported_version",
    "preflight_invalid",
    "spawn_failed",
    "terminated_by_signal",
    "unsupported_parallelism",
    "data_missing",
    "unexpected_parallel_data",
    "generation_failed",
    "artifact_missing",
    "artifact_invalid",
]


@dataclass(frozen=True)
class CoverageCounts:
    covered: int
    missing: int


@dataclass(frozen=True)
class CoverageTotals:
    statements: CoverageCounts
    branches: CoverageCounts


@dataclass(frozen=True)
class FileStatementCoverage:
    covered: int
    missing: int
    missing_lines: tuple[int, ...]


@dataclass(frozen=True)
class FileBranchCoverage:
    covered: int
    missing: int
    missing_arcs: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class CoverageFile:
    path: str
    statements: FileStatementCoverage
    branches: FileBranchCoverage


@dataclass(frozen=True)
class CoverageError:
    code: CoverageErrorCode
    message: str


@dataclass(frozen=True)
class CoverageThreshold:
    configured: bool
    value: int | float | None
    evaluated: bool
    passed: bool | None
    skipped_reason: CoverageThresholdSkipReason | None


@dataclass(frozen=True)
class CoverageResult:
    status: CoverageStatus
    scope: CoverageScope
    evidence_complete: bool
    coverage_version: str | None
    gate_eligible: bool
    threshold: CoverageThreshold
    totals: CoverageTotals | None
    files: tuple[CoverageFile, ...]
    error: CoverageError | None


@dataclass(frozen=True)
class CoverageGatePolicy:
    gate_eligible: bool
    skipped_reason: CoverageThresholdSkipReason | None
    force_fail_under_zero: bool


def coverage_gate_policy(
    plan: RunPlan,
    pytest_result: PytestResult,
    evidence_complete: bool,
) -> CoverageGatePolicy:
    """Select native threshold eligibility from finalized public evidence."""
    configured = _coverage_threshold_is_configured(plan)
    gate_eligible = _gate_is_eligible(plan, pytest_result, evidence_complete)
    skipped_reason = _threshold_skipped_reason(
        plan,
        pytest_result,
        evidence_complete,
        configured,
    )
    return CoverageGatePolicy(gate_eligible, skipped_reason, not gate_eligible)


def _gate_is_eligible(
    plan: RunPlan,
    pytest_result: PytestResult,
    evidence_complete: bool,
) -> bool:
    return (
        evidence_complete
        and pytest_result.evidence is not None
        and pytest_result.exit_code == 0
        and pytest_result.complete
        and pytest_result.scope == "complete"
        and not pytest_result.scope_reasons
        and not plan.targets
        and plan.test_shortcut is None
        and plan.mode == "strict_aggregate"
    )


def _threshold_skipped_reason(
    plan: RunPlan,
    pytest_result: PytestResult,
    evidence_complete: bool,
    configured: bool,
) -> CoverageThresholdSkipReason | None:
    if not evidence_complete or pytest_result.evidence is None:
        return "evidence_error"
    if not configured:
        return "not_configured"
    if pytest_result.exit_code == 5:
        return "no_tests_collected"
    if not pytest_result.complete:
        return "pytest_incomplete"
    if pytest_result.exit_code != 0:
        return "pytest_failed"
    if (
        pytest_result.scope != "complete"
        or pytest_result.scope_reasons
        or plan.targets
        or plan.test_shortcut is not None
    ):
        return "partial_run"
    if plan.mode != "strict_aggregate":
        return "focused_run"
    return None

def _coverage_threshold_is_configured(plan: RunPlan) -> bool:
    return any(
        check.pytest is not None
        and check.pytest.coverage is not None
        and check.pytest.coverage.fail_under is not None
        for check in plan.checks
    )
