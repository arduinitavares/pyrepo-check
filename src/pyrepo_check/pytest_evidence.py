"""Validate immutable pytest execution observations before reporting them."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
from types import MappingProxyType
from typing import Literal, cast

from pyrepo_check.execution import ExecutedCheck, ExecutedProcess


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
        return _failure("artifact_invalid", "pytest artifact snapshot is invalid", pytest_version, primary.returncode)
    try:
        document = json.loads(artifact.content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _failure("artifact_invalid", "pytest artifact is not valid JSON", pytest_version, primary.returncode)
    if not isinstance(document, dict):
        return _failure("artifact_invalid", "pytest artifact root must be an object", pytest_version, primary.returncode)
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
    reports, reports_have_retries = _validate_reports(_required(document, "reports"))
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
    return MappingProxyType(
        {
            "initial_nodeids": tuple(_strings(_required(value, "initial_nodeids"), "collection.initial_nodeids")),
            "final_nodeids": tuple(_strings(_required(value, "final_nodeids"), "collection.final_nodeids")),
            "deselected_nodeids": tuple(_strings(_required(value, "deselected_nodeids"), "collection.deselected_nodeids")),
            "uncovered_removed_nodeids": tuple(_strings(_required(value, "uncovered_removed_nodeids"), "collection.uncovered_removed_nodeids")),
            "errors": _issues(_required(value, "errors"), "collection.errors"),
            "skips": _issues(_required(value, "skips"), "collection.skips"),
        }
    )


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


def _validate_reports(value: object) -> tuple[tuple[ValidatedPhaseReport, ...], bool]:
    if not isinstance(value, list):
        raise _ArtifactInvalid("reports must be a list")
    raw_reports: list[tuple[str, Literal["setup", "call", "teardown"], Literal["passed", "failed", "skipped"], float, bool, str | None, str | None]] = []
    repeated_or_noncore = False
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(value):
        report = _object(item, f"reports[{index}]")
        nodeid = _string(_required(report, "nodeid"), f"reports[{index}].nodeid")
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
        raw_reports.append((nodeid, phase, outcome, duration, present, wasxfail, longrepr))
    return _normalize_expected_failures(raw_reports), repeated_or_noncore


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
