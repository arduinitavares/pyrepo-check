"""Immutable Coverage.py evidence types and threshold gate policy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from pyrepo_check.artifact_safety import load_bounded_json

if TYPE_CHECKING:
    from pyrepo_check.coverage_execution import CoverageExecutionObservation
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


def validate_coverage_json(
    content: bytes,
    *,
    project_root: Path,
    coverage_version: str,
) -> tuple[CoverageTotals, tuple[CoverageFile, ...]]:
    """Validate one immutable Coverage JSON snapshot and normalize exact gaps."""
    loaded = load_bounded_json(content)
    document = _object(loaded, "coverage JSON root")
    meta = _member_object(document, "meta", "coverage JSON")
    files = _member_object(document, "files", "coverage JSON")
    totals_summary = _member_object(document, "totals", "coverage JSON")

    if _exact_integer(_member(meta, "format", "coverage meta"), "meta.format") != 3:
        raise ValueError("coverage JSON meta.format must be 3")
    version = _member(meta, "version", "coverage meta")
    if not isinstance(version, str) or version != coverage_version:
        raise ValueError("coverage JSON meta.version differs from the trusted preflight")
    if _member(meta, "branch_coverage", "coverage meta") is not True:
        raise ValueError("coverage JSON meta.branch_coverage must be true")

    try:
        root = project_root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError("project root cannot be resolved") from error
    if not root.is_dir():
        raise ValueError("project root must be a directory")

    normalized_files: list[CoverageFile] = []
    normalized_paths: set[str] = set()
    measured_file_identities: set[tuple[int, int]] = set()
    statement_covered = 0
    statement_missing = 0
    branch_covered = 0
    branch_missing = 0
    for raw_path, raw_record in files.items():
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("coverage JSON file keys must be non-empty strings")
        normalized_path, identity = _normalize_measured_path(raw_path, root)
        if normalized_path in normalized_paths:
            raise ValueError("coverage JSON contains duplicate normalized file paths")
        if identity in measured_file_identities:
            raise ValueError("coverage JSON contains duplicate measured file identities")
        normalized_paths.add(normalized_path)
        measured_file_identities.add(identity)

        record = _object(raw_record, f"coverage file {raw_path!r}")
        summary = _member_object(record, "summary", f"coverage file {raw_path!r}")
        counts = _summary_counts(summary, f"coverage file {raw_path!r} summary")
        missing_lines = _missing_lines(
            _member(record, "missing_lines", f"coverage file {raw_path!r}"),
            raw_path,
        )
        missing_arcs = _missing_arcs(
            _member(record, "missing_branches", f"coverage file {raw_path!r}"),
            raw_path,
        )
        _validate_summary_arithmetic(counts, missing_lines, missing_arcs, raw_path)

        statement_covered += counts["covered_lines"]
        statement_missing += counts["missing_lines"]
        branch_covered += counts["covered_branches"]
        branch_missing += counts["missing_branches"]
        normalized_files.append(
            CoverageFile(
                normalized_path,
                FileStatementCoverage(
                    counts["covered_lines"],
                    counts["missing_lines"],
                    missing_lines,
                ),
                FileBranchCoverage(
                    counts["covered_branches"],
                    counts["missing_branches"],
                    missing_arcs,
                ),
            )
        )

    totals_counts = _summary_counts(totals_summary, "coverage totals")
    _validate_totals_arithmetic(totals_counts)
    observed_totals = (
        statement_covered,
        statement_missing,
        branch_covered,
        branch_missing,
    )
    declared_totals = (
        totals_counts["covered_lines"],
        totals_counts["missing_lines"],
        totals_counts["covered_branches"],
        totals_counts["missing_branches"],
    )
    if declared_totals != observed_totals:
        raise ValueError("coverage JSON totals do not equal the sum of file summaries")

    normalized_files.sort(
        key=lambda file: (
            -(file.statements.missing + file.branches.missing),
            file.path,
        )
    )
    return (
        CoverageTotals(
            CoverageCounts(statement_covered, statement_missing),
            CoverageCounts(branch_covered, branch_missing),
        ),
        tuple(normalized_files),
    )


def build_coverage_result(
    project_root: Path,
    plan: RunPlan,
    pytest_result: PytestResult,
    observation: CoverageExecutionObservation | None,
) -> CoverageResult | None:
    """Build the sole public coverage result from immutable execution evidence."""
    if plan.planned_coverage_scope in {"not_requested", "unavailable"}:
        return None

    threshold_value = _coverage_threshold_value(plan)
    configured = threshold_value is not None
    coverage_version = _trusted_coverage_version(observation)
    observed_error = _coverage_observation_error(observation)
    if observed_error is not None:
        return _coverage_error_result(
            configured=configured,
            threshold_value=threshold_value,
            coverage_version=coverage_version,
            error=observed_error,
        )
    if observation is None:
        raise AssertionError("validated coverage observation is unavailable")

    policy = coverage_gate_policy(plan, pytest_result, True)
    exit_code = observation.json_exit_code
    threshold_exit = exit_code == 2 and configured and policy.gate_eligible
    if exit_code != 0 and not threshold_exit:
        return _coverage_error_result(
            configured=configured,
            threshold_value=threshold_value,
            coverage_version=coverage_version,
            error=CoverageError(
                "generation_failed",
                (
                    "coverage JSON process has no exit code"
                    if exit_code is None
                    else f"coverage JSON generation exited with code {exit_code}"
                ),
            ),
        )

    artifact = observation.artifact
    if artifact.content is None:
        return _coverage_error_result(
            configured=configured,
            threshold_value=threshold_value,
            coverage_version=coverage_version,
            error=CoverageError("artifact_invalid", "coverage JSON snapshot has no content"),
        )

    try:
        totals, files = validate_coverage_json(
            artifact.content,
            project_root=project_root,
            coverage_version=coverage_version or "",
        )
    except (UnicodeDecodeError, ValueError) as error:
        return _coverage_error_result(
            configured=configured,
            threshold_value=threshold_value,
            coverage_version=coverage_version,
            error=CoverageError(
                "artifact_invalid",
                f"coverage JSON is invalid: {error}",
            ),
        )

    if policy.skipped_reason == "evidence_error":
        return _coverage_error_result(
            configured=configured,
            threshold_value=threshold_value,
            coverage_version=coverage_version,
            error=CoverageError(
                "artifact_invalid",
                "coverage evidence cannot be matched to valid pytest evidence",
            ),
        )

    evaluated = configured and policy.gate_eligible
    threshold_passed = exit_code == 0 if evaluated else None
    threshold = CoverageThreshold(
        configured=configured,
        value=threshold_value,
        evaluated=evaluated,
        passed=threshold_passed,
        skipped_reason=None if evaluated else policy.skipped_reason,
    )
    scope: CoverageScope = (
        "complete"
        if plan.planned_coverage_scope == "complete" and pytest_result.scope == "complete"
        else "partial"
    )
    if policy.gate_eligible:
        status: CoverageStatus = "passed" if threshold_passed is not False else "failed"
    else:
        status = "guidance"
    return CoverageResult(
        status=status,
        scope=scope,
        evidence_complete=True,
        coverage_version=coverage_version,
        gate_eligible=policy.gate_eligible,
        threshold=threshold,
        totals=totals,
        files=files,
        error=None,
    )


def _trusted_coverage_version(
    observation: CoverageExecutionObservation | None,
) -> str | None:
    if observation is None or observation.preflight.classification != "supported":
        return None
    record = observation.preflight.record
    return record.coverage_version if record is not None else None


def _coverage_observation_error(
    observation: CoverageExecutionObservation | None,
) -> CoverageError | None:
    if observation is None:
        return CoverageError(
            "preflight_invalid",
            "coverage execution setup prevented the coverage preflight",
        )
    preflight = observation.preflight
    if preflight.classification != "supported":
        return CoverageError(
            cast(CoverageErrorCode, preflight.classification),
            preflight.diagnostic or f"coverage preflight: {preflight.classification}",
        )
    if preflight.record is None or preflight.record.coverage_version is None:
        return CoverageError(
            "preflight_invalid",
            "supported coverage preflight has no trusted version",
        )
    artifact = observation.artifact
    if artifact.state == "snapshot":
        return None
    code: CoverageErrorCode = (
        "data_missing" if artifact.state == "not_attempted" else artifact.state
    )
    return CoverageError(
        code,
        artifact.diagnostic or f"coverage evidence: {code}",
    )


def _coverage_error_result(
    *,
    configured: bool,
    threshold_value: int | float | None,
    coverage_version: str | None,
    error: CoverageError,
) -> CoverageResult:
    return CoverageResult(
        status="error",
        scope="partial",
        evidence_complete=False,
        coverage_version=coverage_version,
        gate_eligible=False,
        threshold=CoverageThreshold(
            configured=configured,
            value=threshold_value,
            evaluated=False,
            passed=None,
            skipped_reason="evidence_error",
        ),
        totals=None,
        files=(),
        error=error,
    )


def _normalize_measured_path(raw_path: str, root: Path) -> tuple[str, tuple[int, int]]:
    try:
        candidate = Path(raw_path)
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve(
            strict=True
        )
        relative = resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError(f"coverage JSON file path is invalid: {raw_path!r}") from error
    if not resolved.is_file():
        raise ValueError(f"coverage JSON file path is not a regular file: {raw_path!r}")
    try:
        status = resolved.stat()
    except OSError as error:
        raise ValueError(f"coverage JSON file path is invalid: {raw_path!r}") from error
    return relative.as_posix(), (status.st_dev, status.st_ino)


def _summary_counts(summary: dict[object, object], name: str) -> dict[str, int]:
    names = (
        "covered_lines",
        "missing_lines",
        "num_statements",
        "covered_branches",
        "missing_branches",
        "num_branches",
    )
    return {
        member: _non_negative_integer(_member(summary, member, name), f"{name}.{member}")
        for member in names
    }


def _missing_lines(value: object, path: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ValueError(f"coverage file {path!r} missing_lines must be an array")
    lines = tuple(
        _exact_integer(line, f"coverage file {path!r} missing line") for line in value
    )
    if any(line <= 0 for line in lines):
        raise ValueError(f"coverage file {path!r} missing lines must be positive")
    if len(set(lines)) != len(lines):
        raise ValueError(f"coverage file {path!r} contains duplicate missing lines")
    return tuple(sorted(lines))


def _missing_arcs(value: object, path: str) -> tuple[tuple[int, int], ...]:
    if not isinstance(value, list):
        raise ValueError(f"coverage file {path!r} missing_branches must be an array")
    arcs: list[tuple[int, int]] = []
    for raw_arc in value:
        if not isinstance(raw_arc, list) or len(raw_arc) != 2:
            raise ValueError(f"coverage file {path!r} branch arcs must contain two integers")
        source = _exact_integer(raw_arc[0], f"coverage file {path!r} branch source")
        destination = _exact_integer(
            raw_arc[1], f"coverage file {path!r} branch destination"
        )
        if source == 0 or destination == 0:
            raise ValueError(f"coverage file {path!r} branch endpoints must be nonzero")
        arcs.append((source, destination))
    if len(set(arcs)) != len(arcs):
        raise ValueError(f"coverage file {path!r} contains duplicate missing branch arcs")
    return tuple(sorted(arcs))


def _validate_summary_arithmetic(
    counts: dict[str, int],
    missing_lines: tuple[int, ...],
    missing_arcs: tuple[tuple[int, int], ...],
    path: str,
) -> None:
    if counts["covered_lines"] + counts["missing_lines"] != counts["num_statements"]:
        raise ValueError(f"coverage file {path!r} statement summary is inconsistent")
    if counts["covered_branches"] + counts["missing_branches"] != counts["num_branches"]:
        raise ValueError(f"coverage file {path!r} branch summary is inconsistent")
    if counts["missing_lines"] != len(missing_lines):
        raise ValueError(f"coverage file {path!r} missing-line count is inconsistent")
    if counts["missing_branches"] != len(missing_arcs):
        raise ValueError(f"coverage file {path!r} missing-branch count is inconsistent")


def _validate_totals_arithmetic(counts: dict[str, int]) -> None:
    if counts["covered_lines"] + counts["missing_lines"] != counts["num_statements"]:
        raise ValueError("coverage totals statement summary is inconsistent")
    if counts["covered_branches"] + counts["missing_branches"] != counts["num_branches"]:
        raise ValueError("coverage totals branch summary is inconsistent")


def _member(value: dict[object, object], key: str, name: str) -> object:
    try:
        return value[key]
    except KeyError as error:
        raise ValueError(f"{name} is missing required member {key!r}") from error


def _member_object(value: dict[object, object], key: str, name: str) -> dict[object, object]:
    return _object(_member(value, key, name), f"{name}.{key}")


def _object(value: object, name: str) -> dict[object, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return cast(dict[object, object], value)


def _exact_integer(value: object, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    return value


def _non_negative_integer(value: object, name: str) -> int:
    integer = _exact_integer(value, name)
    if integer < 0:
        raise ValueError(f"{name} must be non-negative")
    return integer


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
    return _coverage_threshold_value(plan) is not None


def _coverage_threshold_value(plan: RunPlan) -> int | float | None:
    return next(
        (
            check.pytest.coverage.fail_under
            for check in plan.checks
            if check.pytest is not None
            and check.pytest.coverage is not None
            and check.pytest.coverage.fail_under is not None
        ),
        None,
    )
