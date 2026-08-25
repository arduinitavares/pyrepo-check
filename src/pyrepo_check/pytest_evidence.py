"""Validate immutable pytest execution observations before reporting them."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
import math
from types import MappingProxyType
from typing import Literal, cast

from pyrepo_check.execution import ExecutedCheck, ExecutedProcess
from pyrepo_check.planning import RunPlan
from pyrepo_check.pytest_execution import _load_bounded_json


PytestErrorCode = Literal[
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
]
ExpectedFailureKind = Literal["none", "xfail", "xpass_non_strict", "xpass_strict"]
PytestStatus = Literal["passed", "failed", "error"]
TestScope = Literal["partial", "complete"]
TestScopeReason = Literal[
    "planned_selector",
    "effective_narrowing_option",
    "unclassified_external_option",
    "deselected_tests",
    "collection_reduced",
    "incomplete_session",
]
SpecialOutcome = Literal["skipped", "xfailed", "xpassed"]


@dataclass(frozen=True)
class PytestCounts:
    passed: int
    failed: int
    errors: int
    skipped: int
    xfailed: int
    xpassed: int


@dataclass(frozen=True)
class CollectionIssue:
    nodeid: str
    message: str


@dataclass(frozen=True)
class SlowTest:
    nodeid: str
    duration_ms: int


@dataclass(frozen=True)
class SpecialTestOutcome:
    nodeid: str
    outcome: SpecialOutcome
    reason: str | None
    strict: bool | None
    affects_exit: bool
    duration_ms: int


@dataclass(frozen=True)
class PytestEvidence:
    effective_args: tuple[str, ...]
    collected: int
    deselected: int
    counts: PytestCounts
    collection_errors: tuple[CollectionIssue, ...]
    collection_skips: tuple[CollectionIssue, ...]
    slowest: tuple[SlowTest, ...]
    special_outcomes: tuple[SpecialTestOutcome, ...]


@dataclass(frozen=True)
class PytestError:
    code: PytestErrorCode
    message: str


@dataclass(frozen=True)
class PytestResult:
    status: PytestStatus
    complete: bool
    scope: TestScope
    scope_reasons: tuple[TestScopeReason, ...]
    pytest_version: str | None
    exit_code: int | None
    evidence: PytestEvidence | None
    error: PytestError | None


@dataclass(frozen=True)
class ValidatedExpectedFailure:
    kind: ExpectedFailureKind
    reason: str | None
    strict: bool | None
    affects_exit: bool


@dataclass(frozen=True)
class ValidatedPhaseReport:
    nodeid: str
    when: Literal["setup", "call", "teardown"]
    outcome: Literal["passed", "failed", "skipped"]
    duration: float
    longrepr: str | None
    expected_failure: ValidatedExpectedFailure


@dataclass(frozen=True)
class ValidatedPytestSession:
    pytest_version: str
    exit_code: int
    effective_args: tuple[str, ...]
    semantic_options: Mapping[str, object]
    collection: Mapping[str, object]
    reports: tuple[ValidatedPhaseReport, ...]
    flags: Mapping[str, bool]
    session: Mapping[str, object]


@dataclass(frozen=True)
class PytestValidationFailure:
    code: PytestErrorCode
    message: str
    pytest_version: str | None
    exit_code: int | None


class _ArtifactInvalid(ValueError):
    """An artifact cannot be trusted as schema-version-one evidence."""


def validate_pytest_execution(
    check: ExecutedCheck,
) -> ValidatedPytestSession | PytestValidationFailure:
    """Trust one preflight, primary process, and finalized plugin artifact."""
    observation = check.pytest
    if observation is None:
        return _failure("not_started", "pytest execution was not observed", None, None)

    preflight = observation.preflight
    if preflight.classification != "supported":
        return _preflight_failure(check)
    if preflight.record is None or preflight.record.pytest_version is None:
        return _failure("preflight_invalid", "supported preflight has no pytest version", None, None)
    pytest_version = ".".join(str(piece) for piece in preflight.record.pytest_version)

    primary = _primary_process(check)
    if primary is None:
        return _failure("not_started", "pytest primary process was not observed", pytest_version, None)
    if primary.spawn_error is not None:
        return _failure("spawn_failed", primary.spawn_error, pytest_version, None)
    if primary.returncode is None:
        return _failure("spawn_failed", "pytest primary process has no exit code", pytest_version, None)
    if primary.returncode < 0:
        return _failure(
            "terminated_by_signal",
            f"pytest primary process terminated by signal {-primary.returncode}",
            pytest_version,
            None,
        )

    artifact = observation.artifact
    if artifact.state == "missing":
        return _failure("artifact_missing", "pytest artifact is missing", pytest_version, primary.returncode)
    if artifact.state != "snapshot" or artifact.content is None:
        return _failure(
            "artifact_invalid",
            artifact.diagnostic or "pytest artifact snapshot is invalid",
            pytest_version,
            primary.returncode,
        )
    try:
        loaded_document = _load_bounded_json(artifact.content)
    except (UnicodeDecodeError, ValueError):
        message = "pytest artifact is not valid JSON"
        if artifact.diagnostic is not None:
            message = f"{message}; {artifact.diagnostic}"
        return _failure("artifact_invalid", message, pytest_version, primary.returncode)
    if not isinstance(loaded_document, dict):
        return _failure("artifact_invalid", "pytest artifact root must be an object", pytest_version, primary.returncode)
    document = cast(dict[object, object], loaded_document)
    try:
        state = _string(_required(document, "state"), "state")
    except _ArtifactInvalid as error:
        return _failure("artifact_invalid", str(error), pytest_version, primary.returncode)
    if state not in {"started", "finalized"}:
        return _failure("artifact_invalid", "pytest artifact state is invalid", pytest_version, primary.returncode)
    if state != "finalized":
        return _failure("artifact_not_finalized", "pytest artifact is not finalized", pytest_version, primary.returncode)
    try:
        _schema_version(document)
    except _ArtifactInvalid as error:
        return _failure("artifact_invalid", str(error), pytest_version, primary.returncode)
    if artifact.diagnostic is not None:
        return _failure("artifact_invalid", artifact.diagnostic, pytest_version, primary.returncode)
    try:
        validated, reports_have_retries = _validate_artifact(
            document, artifact.writer_ids, pytest_version
        )
    except _ArtifactInvalid as error:
        return _failure("artifact_invalid", str(error), pytest_version, primary.returncode)
    if validated.flags["unsupported_parallelism"] or validated.flags["worker_metadata"]:
        return _failure(
            "unsupported_parallelism",
            "pytest artifact reports unsupported parallel execution",
            pytest_version,
            primary.returncode,
        )
    if validated.flags["unsupported_retries"] or reports_have_retries:
        return _failure(
            "unsupported_retries",
            "pytest artifact reports unsupported retries",
            pytest_version,
            primary.returncode,
        )
    if validated.session["exit_code"] != primary.returncode:
        return _failure(
            "exit_code_mismatch",
            "pytest artifact exit code differs from the primary process",
            pytest_version,
            primary.returncode,
        )
    return validated


def build_pytest_result(plan: RunPlan, check: ExecutedCheck) -> PytestResult:
    """Consolidate trusted pytest observations into the public schema-v1 result."""
    validated = validate_pytest_execution(check)
    if isinstance(validated, PytestValidationFailure):
        return _result_from_validation_failure(plan, validated)

    evidence, terminal_nodeids = _build_evidence(validated)
    collection_errors = evidence.collection_errors
    final_nodeids = cast(tuple[str, ...], validated.collection["final_nodeids"])
    final_nodeid_set = set(final_nodeids)
    final_phase_nodeids = {
        report.nodeid for report in validated.reports if report.when == "teardown"
    }
    collection_completed = cast(bool, validated.session["collection_completed"])
    stopped_early = cast(bool, validated.session["stopped_early"])
    complete = (
        collection_completed
        and not collection_errors
        and not stopped_early
        and final_nodeid_set.issubset(terminal_nodeids)
        and final_nodeid_set.issubset(final_phase_nodeids)
        and _count_total(evidence.counts) == evidence.collected
        and validated.exit_code in {0, 1, 5}
        and (validated.exit_code != 5 or evidence.collected == 0)
    )
    status, error = _exit_result(validated.exit_code, complete, stopped_early)
    reasons = _scope_reasons(plan, validated, evidence, complete)
    return PytestResult(
        status=status,
        complete=complete,
        scope="complete" if not reasons else "partial",
        scope_reasons=reasons,
        pytest_version=validated.pytest_version,
        exit_code=validated.exit_code,
        evidence=evidence,
        error=error,
    )


def _result_from_validation_failure(
    plan: RunPlan, failure: PytestValidationFailure
) -> PytestResult:
    reasons: list[TestScopeReason] = []
    if plan.planned_test_scope == "partial":
        reasons.append("planned_selector")
    reasons.append("incomplete_session")
    return PytestResult(
        status="error",
        complete=False,
        scope="partial",
        scope_reasons=tuple(reasons),
        pytest_version=failure.pytest_version,
        exit_code=failure.exit_code,
        evidence=None,
        error=PytestError(failure.code, failure.message),
    )


def _build_evidence(
    validated: ValidatedPytestSession,
) -> tuple[PytestEvidence, set[str]]:
    final_nodeids = cast(tuple[str, ...], validated.collection["final_nodeids"])
    final_nodeid_set = set(final_nodeids)
    phases_by_nodeid: dict[str, list[ValidatedPhaseReport]] = {}
    for report in validated.reports:
        if report.nodeid in final_nodeid_set:
            phases_by_nodeid.setdefault(report.nodeid, []).append(report)

    counts = PytestCounts(0, 0, 0, 0, 0, 0)
    slowest: list[SlowTest] = []
    special: list[SpecialTestOutcome] = []
    terminal_nodeids: set[str] = set()
    setup_only = bool(
        validated.semantic_options["setuponly"]
        or validated.semantic_options["setupplan"]
    )
    for nodeid in final_nodeids:
        reports = phases_by_nodeid.get(nodeid, [])
        outcome = _consolidate_node(reports, setup_only=setup_only)
        if outcome is None:
            continue
        terminal_nodeids.add(nodeid)
        counts = _increment_count(counts, outcome[0])
        duration_ms = _round_phase_durations(reports)
        slowest.append(SlowTest(nodeid, duration_ms))
        if outcome[0] in {"skipped", "xfailed", "xpassed"}:
            special.append(
                SpecialTestOutcome(
                    nodeid=nodeid,
                    outcome=cast(SpecialOutcome, outcome[0]),
                    reason=outcome[1],
                    strict=outcome[2],
                    affects_exit=outcome[3],
                    duration_ms=duration_ms,
                )
            )

    collection_errors = _collection_issues(validated.collection["errors"])
    collection_skips = _collection_issues(validated.collection["skips"])
    return (
        PytestEvidence(
            effective_args=validated.effective_args,
            collected=len(final_nodeids),
            deselected=len(cast(tuple[str, ...], validated.collection["deselected_nodeids"])),
            counts=counts,
            collection_errors=collection_errors,
            collection_skips=collection_skips,
            slowest=tuple(sorted(slowest, key=lambda item: (-item.duration_ms, item.nodeid))[:10]),
            special_outcomes=tuple(sorted(special, key=lambda item: item.nodeid)),
        ),
        terminal_nodeids,
    )


def _consolidate_node(
    reports: list[ValidatedPhaseReport],
    *,
    setup_only: bool,
) -> tuple[Literal["passed", "failed", "errors", "skipped", "xfailed", "xpassed"], str | None, bool | None, bool] | None:
    if not reports:
        return None
    setup_or_teardown = [report for report in reports if report.when in {"setup", "teardown"}]
    if any(report.outcome == "failed" for report in setup_or_teardown):
        return ("errors", None, None, False)
    strict_xpass = next(
        (report for report in reports if report.expected_failure.kind == "xpass_strict"),
        None,
    )
    if strict_xpass is not None:
        return ("xpassed", strict_xpass.expected_failure.reason, True, True)
    if any(report.when == "call" and report.outcome == "failed" for report in reports):
        return ("failed", None, None, False)
    xfail = next(
        (report for report in reports if report.expected_failure.kind == "xfail"),
        None,
    )
    if xfail is not None:
        return ("xfailed", xfail.expected_failure.reason, None, False)
    non_strict_xpass = next(
        (report for report in reports if report.expected_failure.kind == "xpass_non_strict"),
        None,
    )
    if non_strict_xpass is not None:
        return ("xpassed", non_strict_xpass.expected_failure.reason, False, False)
    skipped = next((report for report in reports if report.outcome == "skipped"), None)
    if skipped is not None:
        return ("skipped", None, None, False)
    if not _has_terminal_phase(reports, setup_only=setup_only):
        return None
    return ("passed", None, None, False)


def _has_terminal_phase(
    reports: list[ValidatedPhaseReport],
    *,
    setup_only: bool,
) -> bool:
    return any(
        report.when == "call"
        or (report.when == "setup" and report.outcome != "passed")
        or (
            report.when == "teardown"
            and (report.outcome != "passed" or setup_only)
        )
        for report in reports
    )


def _increment_count(
    counts: PytestCounts,
    outcome: Literal["passed", "failed", "errors", "skipped", "xfailed", "xpassed"],
) -> PytestCounts:
    values = {
        "passed": counts.passed,
        "failed": counts.failed,
        "errors": counts.errors,
        "skipped": counts.skipped,
        "xfailed": counts.xfailed,
        "xpassed": counts.xpassed,
    }
    values[outcome] += 1
    return PytestCounts(**values)


def _collection_issues(value: object) -> tuple[CollectionIssue, ...]:
    issues = cast(tuple[Mapping[str, str], ...], value)
    return tuple(
        sorted(
            (CollectionIssue(issue["nodeid"], issue["message"]) for issue in issues),
            key=lambda issue: (issue.nodeid, issue.message),
        )
    )


def _round_phase_durations(reports: list[ValidatedPhaseReport]) -> int:
    milliseconds = sum(
        (Fraction(str(report.duration)) * 1000 for report in reports),
        Fraction(),
    )
    quotient, remainder = divmod(milliseconds.numerator, milliseconds.denominator)
    return quotient + int(remainder * 2 >= milliseconds.denominator)


def _count_total(counts: PytestCounts) -> int:
    return (
        counts.passed
        + counts.failed
        + counts.errors
        + counts.skipped
        + counts.xfailed
        + counts.xpassed
    )


def _exit_result(
    exit_code: int,
    complete: bool,
    stopped_early: bool,
) -> tuple[PytestStatus, PytestError | None]:
    if exit_code == 0:
        return ("passed", None)
    if exit_code == 1:
        if not complete and stopped_early:
            return (
                "failed",
                PytestError("session_incomplete", "pytest session stopped before all selected tests completed"),
            )
        return ("failed", None)
    error_by_exit: dict[int, tuple[PytestErrorCode, str]] = {
        2: ("interrupted", "pytest execution was interrupted"),
        3: ("internal_error", "pytest encountered an internal error"),
        4: ("usage_error", "pytest reported a usage error"),
    }
    if exit_code in error_by_exit:
        code, message = error_by_exit[exit_code]
        return ("error", PytestError(code, message))
    if exit_code == 5:
        return ("failed", None)
    return ("error", PytestError("unknown_exit_code", f"pytest exited with unknown code {exit_code}"))


def _scope_reasons(
    plan: RunPlan,
    validated: ValidatedPytestSession,
    evidence: PytestEvidence,
    complete: bool,
) -> tuple[TestScopeReason, ...]:
    reasons: list[TestScopeReason] = []
    if plan.planned_test_scope == "partial":
        reasons.append("planned_selector")
    if _has_known_narrowing(validated):
        reasons.append("effective_narrowing_option")
    if _has_unclassified_option(validated.effective_args):
        reasons.append("unclassified_external_option")
    if evidence.deselected > 0:
        reasons.append("deselected_tests")
    if cast(tuple[str, ...], validated.collection["uncovered_removed_nodeids"]):
        reasons.append("collection_reduced")
    if not complete:
        reasons.append("incomplete_session")
    return tuple(reasons)


def _has_known_narrowing(validated: ValidatedPytestSession) -> bool:
    options = validated.semantic_options
    return bool(
        cast(tuple[str, ...], options["collection_paths"])
        or cast(str, options["keyword"])
        or cast(str, options["markexpr"])
        or cast(tuple[str, ...], options["deselect"])
        or cast(tuple[str, ...], options["ignore"])
        or cast(tuple[str, ...], options["ignore_glob"])
        or cast(bool, options["lf"])
        or cast(bool, options["pyargs"])
        or cast(bool, options["collectonly"])
        or cast(bool, options["setuponly"])
        or cast(bool, options["setupplan"])
        or _has_known_narrowing_argument(validated.effective_args)
    )


def _has_known_narrowing_argument(args: tuple[str, ...]) -> bool:
    known = {
        "-k",
        "-m",
        "--deselect",
        "--ignore",
        "--ignore-glob",
        "--lf",
        "--last-failed",
        "--pyargs",
        "--collect-only",
        "--setup-only",
        "--setup-plan",
    }
    neutral_with_operand = {
        "-r",
        "--tb",
        "--color",
        "--code-highlight",
        "--capture",
        "--durations",
        "--durations-min",
    }
    index = 0
    while index < len(args):
        argument = args[index]
        if argument == "--":
            return index + 1 < len(args)
        if argument in neutral_with_operand:
            index += 2
            continue
        if argument in known or argument.startswith(
            ("-k", "-m", "--deselect=", "--ignore=", "--ignore-glob=")
        ):
            return True
        if not argument.startswith("-"):
            return True
        index += 1
    return False


def _has_unclassified_option(args: tuple[str, ...]) -> bool:
    index = 0
    neutral_with_operand = {
        "-r",
        "--tb",
        "--color",
        "--code-highlight",
        "--capture",
        "--durations",
        "--durations-min",
    }
    known_narrowing_without_operand = {
        "--lf",
        "--last-failed",
        "--pyargs",
        "--collect-only",
        "--setup-only",
        "--setup-plan",
    }
    while index < len(args):
        argument = args[index]
        if argument == "--":
            return False
        if argument in neutral_with_operand:
            index += 2
            continue
        if argument in known_narrowing_without_operand:
            index += 1
            continue
        if argument.startswith("-r") and argument != "-r":
            index += 1
            continue
        if argument and set(argument) <= {"-", "q"} and argument.startswith("-"):
            index += 1
            continue
        if argument and set(argument) <= {"-", "v"} and argument.startswith("-"):
            index += 1
            continue
        if argument in {"--quiet", "--verbose", "-l", "--showlocals", "--no-showlocals", "-s", "--disable-warnings", "--strict-config", "--strict-markers"}:
            index += 1
            continue
        if argument.startswith(("--tb=", "--color=", "--code-highlight=", "--capture=", "--durations=", "--durations-min=")):
            index += 1
            continue
        if argument.startswith(("-k", "-m", "--deselect=", "--ignore=", "--ignore-glob=")):
            index += 1
            continue
        if argument.startswith("-"):
            if argument in {"-k", "-m", "--deselect", "--ignore", "--ignore-glob"}:
                index += 2
                continue
            return True
        index += 1
    return False


def _validate_artifact(
    document: dict[object, object], writer_ids: tuple[str, ...], pytest_version: str
) -> tuple[ValidatedPytestSession, bool]:
    _schema_version(document)
    writer_id = _string(_required(document, "writer_id"), "writer_id")
    if len(writer_ids) != 1 or writer_ids[0] != writer_id:
        raise _ArtifactInvalid("pytest artifact writer identity is not trusted")
    if _string(_required(document, "pytest_version"), "pytest_version") != pytest_version:
        raise _ArtifactInvalid("pytest artifact pytest version differs from preflight")
    session = _validate_session(_object(_required(document, "session"), "session"))
    effective_args = tuple(_strings(_required(document, "effective_args"), "effective_args"))
    semantic_options = _validate_semantic_options(
        _object(_required(document, "semantic_options"), "semantic_options")
    )
    collection = _validate_collection(_object(_required(document, "collection"), "collection"))
    reports, reports_have_retries = _validate_reports(
        _required(document, "reports"),
        cast(tuple[str, ...], collection["final_nodeids"]),
    )
    flags = _validate_flags(_object(_required(document, "flags"), "flags"))
    return (
        ValidatedPytestSession(
            pytest_version=pytest_version,
            exit_code=cast(int, session["exit_code"]),
            effective_args=effective_args,
            semantic_options=semantic_options,
            collection=collection,
            reports=reports,
            flags=flags,
            session=session,
        ),
        reports_have_retries,
    )


def _schema_version(document: dict[object, object]) -> None:
    if _integer(_required(document, "schema_version"), "schema_version") != 1:
        raise _ArtifactInvalid("pytest artifact schema version is unsupported")


def _validate_session(value: dict[object, object]) -> Mapping[str, object]:
    starts = _integer(_required(value, "starts"), "session.starts")
    finishes = _integer(_required(value, "finishes"), "session.finishes")
    exit_code = _integer(_required(value, "exit_code"), "session.exit_code")
    collection_completed = _boolean(
        _required(value, "collection_completed"), "session.collection_completed"
    )
    stopped_early = _boolean(_required(value, "stopped_early"), "session.stopped_early")
    if starts != 1 or finishes != 1:
        raise _ArtifactInvalid("pytest artifact has unsupported session cardinality")
    return MappingProxyType(
        {
            "starts": starts,
            "finishes": finishes,
            "exit_code": exit_code,
            "collection_completed": collection_completed,
            "stopped_early": stopped_early,
        }
    )


def _validate_semantic_options(value: dict[object, object]) -> Mapping[str, object]:
    return MappingProxyType(
        {
            "collection_paths": tuple(_strings(_required(value, "collection_paths"), "semantic_options.collection_paths")),
            "keyword": _string(_required(value, "keyword"), "semantic_options.keyword"),
            "markexpr": _string(_required(value, "markexpr"), "semantic_options.markexpr"),
            "deselect": tuple(_strings(_required(value, "deselect"), "semantic_options.deselect")),
            "ignore": tuple(_strings(_required(value, "ignore"), "semantic_options.ignore")),
            "ignore_glob": tuple(_strings(_required(value, "ignore_glob"), "semantic_options.ignore_glob")),
            "lf": _boolean(_required(value, "lf"), "semantic_options.lf"),
            "pyargs": _boolean(_required(value, "pyargs"), "semantic_options.pyargs"),
            "collectonly": _boolean(_required(value, "collectonly"), "semantic_options.collectonly"),
            "setuponly": _boolean(_required(value, "setuponly"), "semantic_options.setuponly"),
            "setupplan": _boolean(_required(value, "setupplan"), "semantic_options.setupplan"),
        }
    )


def _validate_collection(value: dict[object, object]) -> Mapping[str, object]:
    initial_nodeids = tuple(
        _strings(_required(value, "initial_nodeids"), "collection.initial_nodeids")
    )
    final_nodeids = tuple(
        _strings(_required(value, "final_nodeids"), "collection.final_nodeids")
    )
    deselected_nodeids = tuple(
        _strings(
            _required(value, "deselected_nodeids"),
            "collection.deselected_nodeids",
        )
    )
    uncovered_removed_nodeids = tuple(
        _strings(
            _required(value, "uncovered_removed_nodeids"),
            "collection.uncovered_removed_nodeids",
        )
    )
    initial = _unique_nodeids(initial_nodeids, "collection.initial_nodeids")
    final = _unique_nodeids(final_nodeids, "collection.final_nodeids")
    deselected = _unique_nodeids(
        deselected_nodeids, "collection.deselected_nodeids"
    )
    uncovered = _unique_nodeids(
        uncovered_removed_nodeids, "collection.uncovered_removed_nodeids"
    )
    if not final.issubset(initial):
        raise _ArtifactInvalid("collection.final_nodeids must be a subset of initial_nodeids")
    if not deselected.issubset(initial):
        raise _ArtifactInvalid(
            "collection.deselected_nodeids must be a subset of initial_nodeids"
        )
    if not uncovered.issubset(initial):
        raise _ArtifactInvalid(
            "collection.uncovered_removed_nodeids must be a subset of initial_nodeids"
        )
    if final & deselected or final & uncovered or deselected & uncovered:
        raise _ArtifactInvalid("pytest collection node sets must be pairwise disjoint")
    if final | deselected | uncovered != initial:
        raise _ArtifactInvalid("pytest collection node sets must account for initial_nodeids")
    return MappingProxyType(
        {
            "initial_nodeids": initial_nodeids,
            "final_nodeids": final_nodeids,
            "deselected_nodeids": deselected_nodeids,
            "uncovered_removed_nodeids": uncovered_removed_nodeids,
            "errors": _issues(_required(value, "errors"), "collection.errors"),
            "skips": _issues(_required(value, "skips"), "collection.skips"),
        }
    )


def _unique_nodeids(nodeids: tuple[str, ...], name: str) -> set[str]:
    unique = set(nodeids)
    if len(unique) != len(nodeids):
        raise _ArtifactInvalid(f"{name} must contain unique node IDs")
    return unique


def _issues(value: object, name: str) -> tuple[Mapping[str, str], ...]:
    if not isinstance(value, list):
        raise _ArtifactInvalid(f"{name} must be a list")
    issues: list[Mapping[str, str]] = []
    for index, item in enumerate(value):
        issue = _object(item, f"{name}[{index}]")
        issues.append(
            MappingProxyType(
                {
                    "nodeid": _string(_required(issue, "nodeid"), f"{name}[{index}].nodeid"),
                    "message": _string(_required(issue, "message"), f"{name}[{index}].message"),
                }
            )
        )
    return tuple(issues)


def _validate_reports(
    value: object,
    final_nodeids: tuple[str, ...],
) -> tuple[tuple[ValidatedPhaseReport, ...], bool]:
    if not isinstance(value, list):
        raise _ArtifactInvalid("reports must be a list")
    raw_reports: list[tuple[str, Literal["setup", "call", "teardown"], Literal["passed", "failed", "skipped"], float, bool, str | None, str | None]] = []
    repeated_or_noncore = False
    seen: set[tuple[str, str]] = set()
    phases_by_nodeid: dict[str, list[str]] = {}
    final_nodeid_set = set(final_nodeids)
    for index, item in enumerate(value):
        report = _object(item, f"reports[{index}]")
        nodeid = _string(_required(report, "nodeid"), f"reports[{index}].nodeid")
        if nodeid not in final_nodeid_set:
            raise _ArtifactInvalid(f"reports[{index}].nodeid is not in final_nodeids")
        when_value = _string(_required(report, "when"), f"reports[{index}].when")
        outcome_value = _string(_required(report, "outcome"), f"reports[{index}].outcome")
        if when_value not in {"setup", "call", "teardown"}:
            raise _ArtifactInvalid(f"reports[{index}].when is invalid")
        if outcome_value not in {"passed", "failed", "skipped"}:
            repeated_or_noncore = True
        duration = _duration(_required(report, "duration"), f"reports[{index}].duration")
        present = _boolean(_required(report, "wasxfail_present"), f"reports[{index}].wasxfail_present")
        valid = _boolean(_required(report, "wasxfail_valid"), f"reports[{index}].wasxfail_valid")
        wasxfail_value = _required(report, "wasxfail")
        longrepr_value = _required(report, "longrepr")
        wasxfail = wasxfail_value if isinstance(wasxfail_value, str) else None
        longrepr = longrepr_value if isinstance(longrepr_value, str) else None
        if (present and (not valid or wasxfail is None)) or (not present and (not valid or wasxfail_value is not None)):
            raise _ArtifactInvalid(f"reports[{index}] has invalid expected-failure metadata")
        if longrepr_value is not None and longrepr is None:
            raise _ArtifactInvalid(f"reports[{index}].longrepr must be a string or null")
        phase = cast(Literal["setup", "call", "teardown"], when_value)
        outcome = cast(Literal["passed", "failed", "skipped"], outcome_value)
        if (nodeid, when_value) in seen:
            repeated_or_noncore = True
        seen.add((nodeid, when_value))
        phases_by_nodeid.setdefault(nodeid, []).append(when_value)
        raw_reports.append((nodeid, phase, outcome, duration, present, wasxfail, longrepr))
    if not repeated_or_noncore:
        _validate_phase_sequences(phases_by_nodeid)
    return _normalize_expected_failures(raw_reports), repeated_or_noncore


def _validate_phase_sequences(phases_by_nodeid: dict[str, list[str]]) -> None:
    allowed = {
        ("setup",),
        ("setup", "call"),
        ("setup", "teardown"),
        ("setup", "call", "teardown"),
    }
    for nodeid, phases in phases_by_nodeid.items():
        if tuple(phases) not in allowed:
            raise _ArtifactInvalid(
                f"pytest reports for {nodeid} have an impossible phase sequence"
            )


def _normalize_expected_failures(
    reports: list[tuple[str, Literal["setup", "call", "teardown"], Literal["passed", "failed", "skipped"], float, bool, str | None, str | None]]
) -> tuple[ValidatedPhaseReport, ...]:
    grouped: dict[str, list[int]] = {}
    for index, report in enumerate(reports):
        grouped.setdefault(report[0], []).append(index)
    normalized = [
        ValidatedPhaseReport(nodeid, when, outcome, duration, longrepr, _none_expected_failure())
        for nodeid, when, outcome, duration, _present, _wasxfail, longrepr in reports
    ]
    for indices in grouped.values():
        expected_indices = [index for index in indices if reports[index][4]]
        strict_indices = [
            index
            for index in indices
            if _is_strict_xpass_marker(reports[index])
        ]
        if strict_indices:
            if len(strict_indices) != 1 or expected_indices:
                raise _ArtifactInvalid("pytest artifact has contradictory strict XPASS metadata")
            strict_index = strict_indices[0]
            _nodeid, when, outcome, _duration_value, _present, _wasxfail, longrepr = reports[strict_index]
            if when != "call" or outcome != "failed" or longrepr is None:
                raise _ArtifactInvalid("pytest artifact has malformed strict XPASS metadata")
            normalized[strict_index] = replace_report(
                normalized[strict_index],
                ValidatedExpectedFailure(
                    "xpass_strict", _normalized_reason(longrepr.removeprefix("[XPASS(strict)] ")), True, True
                ),
            )
        elif expected_indices:
            if len(expected_indices) != 1:
                raise _ArtifactInvalid("pytest artifact has contradictory expected-failure metadata")
            expected_index = expected_indices[0]
            _nodeid, when, outcome, _duration_value, _present, reason, _longrepr = reports[expected_index]
            if reason is None:
                raise _ArtifactInvalid("pytest artifact has malformed expected-failure metadata")
            if outcome == "skipped":
                expected = ValidatedExpectedFailure("xfail", _normalized_reason(reason), None, False)
            elif when == "call" and outcome == "passed":
                expected = ValidatedExpectedFailure("xpass_non_strict", _normalized_reason(reason), False, False)
            else:
                raise _ArtifactInvalid("pytest artifact has malformed expected-failure metadata")
            normalized[expected_index] = replace_report(normalized[expected_index], expected)
    return tuple(normalized)


def replace_report(
    report: ValidatedPhaseReport, expected_failure: ValidatedExpectedFailure
) -> ValidatedPhaseReport:
    return ValidatedPhaseReport(
        report.nodeid,
        report.when,
        report.outcome,
        report.duration,
        report.longrepr,
        expected_failure,
    )


def _none_expected_failure() -> ValidatedExpectedFailure:
    return ValidatedExpectedFailure("none", None, None, False)


def _is_strict_xpass_marker(
    report: tuple[
        str,
        Literal["setup", "call", "teardown"],
        Literal["passed", "failed", "skipped"],
        float,
        bool,
        str | None,
        str | None,
    ],
) -> bool:
    longrepr = report[6]
    return longrepr is not None and longrepr.startswith("[XPASS(strict)] ")


def _normalized_reason(value: str) -> str | None:
    return value or None


def _validate_flags(value: dict[object, object]) -> Mapping[str, bool]:
    return MappingProxyType(
        {
            "unsupported_parallelism": _boolean(_required(value, "unsupported_parallelism"), "flags.unsupported_parallelism"),
            "unsupported_retries": _boolean(_required(value, "unsupported_retries"), "flags.unsupported_retries"),
            "worker_metadata": _boolean(_required(value, "worker_metadata"), "flags.worker_metadata"),
        }
    )


def _required(value: dict[object, object], key: str) -> object:
    if key not in value:
        raise _ArtifactInvalid(f"pytest artifact is missing {key}")
    return value[key]


def _object(value: object, name: str) -> dict[object, object]:
    if not isinstance(value, dict):
        raise _ArtifactInvalid(f"{name} must be an object")
    return cast(dict[object, object], value)


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise _ArtifactInvalid(f"{name} must be a string")
    return value


def _strings(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _ArtifactInvalid(f"{name} must be a list of strings")
    return cast(list[str], value)


def _integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _ArtifactInvalid(f"{name} must be an integer")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise _ArtifactInvalid(f"{name} must be a boolean")
    return value


def _duration(value: object, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise _ArtifactInvalid(f"{name} must be a finite non-negative number")
    return float(value)


def _primary_process(check: ExecutedCheck) -> ExecutedProcess | None:
    return next((process for process in check.processes if process.role == "primary"), None)


def _preflight_failure(check: ExecutedCheck) -> PytestValidationFailure:
    observation = check.pytest
    if observation is None:
        return _failure("not_started", "pytest execution was not observed", None, None)
    preflight = observation.preflight
    code: PytestErrorCode
    if preflight.classification in {
        "unsupported_python",
        "module_unavailable",
        "unsupported_version",
        "preflight_invalid",
        "spawn_failed",
        "terminated_by_signal",
        "not_started",
    }:
        code = cast(PytestErrorCode, preflight.classification)
    else:
        code = "preflight_invalid"
    version = (
        None
        if preflight.record is None or preflight.record.pytest_version is None
        else ".".join(str(piece) for piece in preflight.record.pytest_version)
    )
    return _failure(code, preflight.diagnostic or "pytest preflight failed", version, None)


def _failure(
    code: PytestErrorCode,
    message: str,
    pytest_version: str | None,
    exit_code: int | None,
) -> PytestValidationFailure:
    return PytestValidationFailure(code, message, pytest_version, exit_code)
