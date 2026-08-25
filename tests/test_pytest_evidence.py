from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import cast

import pytest

from pyrepo_check.execution import ExecutedCheck, ExecutedProcess
from pyrepo_check.planning import PlannedCheck, PytestExecutionPlan
from pyrepo_check.pytest_execution import (
    ArtifactState,
    PreflightClassification,
    PytestArtifactObservation,
    PytestExecutionObservation,
    PytestPreflightObservation,
    PytestPreflightRecord,
)
from pyrepo_check.pytest_evidence import PytestValidationFailure, validate_pytest_execution


def _check(
    *,
    preflight: PytestPreflightObservation | None = None,
    primary: ExecutedProcess | None = None,
    artifact: PytestArtifactObservation | None = None,
    cleanup_error: str | None = None,
) -> ExecutedCheck:
    cwd = Path("/consumer")
    planned = PlannedCheck(
        name="pytest",
        command=("consumer-python", "-m", "pytest", "tests"),
        cwd=cwd,
        pytest=PytestExecutionPlan(
            consumer_python=("consumer-python",), pytest_args=("tests",)
        ),
    )
    trusted_preflight = PytestPreflightObservation(
        "supported", PytestPreflightRecord((3, 13, 15), True, (8, 4, 2)), None
    )
    primary_process = ExecutedProcess(
        role="primary",
        command=planned.command,
        cwd=cwd,
        returncode=0,
        duration_ms=1,
        stdout=b"",
        stderr=b"",
        spawn_error=None,
    )
    raw_artifact = {
        "schema_version": 1,
        "state": "finalized",
        "writer_id": "writer-1",
        "pytest_version": "8.4.2",
        "session": {
            "starts": 1,
            "finishes": 1,
            "exit_code": 0,
            "collection_completed": True,
            "stopped_early": False,
        },
        "effective_args": ["tests"],
        "semantic_options": {
            "collection_paths": ["tests"],
            "keyword": "",
            "markexpr": "",
            "deselect": [],
            "ignore": [],
            "ignore_glob": [],
            "lf": False,
            "pyargs": False,
            "collectonly": False,
            "setuponly": False,
            "setupplan": False,
        },
        "collection": {
            "initial_nodeids": ["tests/test_ok.py::test_ok"],
            "final_nodeids": ["tests/test_ok.py::test_ok"],
            "deselected_nodeids": [],
            "uncovered_removed_nodeids": [],
            "errors": [],
            "skips": [],
        },
        "reports": [
            {
                "nodeid": "tests/test_ok.py::test_ok",
                "when": "setup",
                "outcome": "passed",
                "duration": 0.1,
                "wasxfail_present": False,
                "wasxfail_valid": True,
                "wasxfail": None,
                "longrepr": None,
            },
            {
                "nodeid": "tests/test_ok.py::test_ok",
                "when": "call",
                "outcome": "passed",
                "duration": 0.2,
                "wasxfail_present": False,
                "wasxfail_valid": True,
                "wasxfail": None,
                "longrepr": None,
            },
            {
                "nodeid": "tests/test_ok.py::test_ok",
                "when": "teardown",
                "outcome": "passed",
                "duration": 0.1,
                "wasxfail_present": False,
                "wasxfail_valid": True,
                "wasxfail": None,
                "longrepr": None,
            },
        ],
        "flags": {
            "unsupported_parallelism": False,
            "unsupported_retries": False,
            "worker_metadata": False,
        },
    }
    snapshot = PytestArtifactObservation(
        "snapshot", json.dumps(raw_artifact).encode(), ("writer-1",), None
    )
    return ExecutedCheck(
        planned=planned,
        processes=(primary if primary is not None else primary_process,),
        pytest=PytestExecutionObservation(
            preflight=preflight if preflight is not None else trusted_preflight,
            artifact=artifact if artifact is not None else snapshot,
            cleanup_error=cleanup_error,
        ),
    )


@pytest.mark.parametrize(
    ("preflight", "primary", "artifact", "expected_code"),
    (
        (
            PytestPreflightObservation("unsupported_version", None, "unsupported"),
            None,
            PytestArtifactObservation("missing", None, (), None),
            "unsupported_version",
        ),
        (
            PytestPreflightObservation("preflight_invalid", None, "invalid"),
            None,
            PytestArtifactObservation("missing", None, (), None),
            "preflight_invalid",
        ),
        (
            None,
            ExecutedProcess(
                role="primary",
                command=(),
                cwd=Path("/consumer"),
                returncode=None,
                duration_ms=0,
                stdout=None,
                stderr=None,
                spawn_error="FileNotFoundError: consumer-python",
            ),
            PytestArtifactObservation("missing", None, (), None),
            "spawn_failed",
        ),
        (None, None, PytestArtifactObservation("missing", None, (), None), "artifact_missing"),
    ),
    ids=("preflight-version", "preflight-invalid", "primary-spawn", "artifact-missing"),
)
def test_validation_precedence(
    preflight: PytestPreflightObservation | None,
    primary: ExecutedProcess | None,
    artifact: PytestArtifactObservation,
    expected_code: str,
) -> None:
    result = validate_pytest_execution(
        _check(preflight=preflight, primary=primary, artifact=artifact)
    )

    assert isinstance(result, PytestValidationFailure)
    assert result.code == expected_code


def test_validation_keeps_a_valid_snapshot_despite_cleanup_error() -> None:
    result = validate_pytest_execution(_check(cleanup_error="PermissionError: denied"))

    assert not isinstance(result, PytestValidationFailure)
    assert result.pytest_version == "8.4.2"
    assert result.exit_code == 0


def _artifact_document(check: ExecutedCheck) -> dict[str, object]:
    assert check.pytest is not None
    content = check.pytest.artifact.content
    assert content is not None
    document = cast(object, json.loads(content))
    assert isinstance(document, dict)
    return cast(dict[str, object], document)


def _report(document: dict[str, object], index: int) -> dict[str, object]:
    reports = document["reports"]
    assert isinstance(reports, list)
    item = reports[index]
    assert isinstance(item, dict)
    return cast(dict[str, object], item)


def _with_document(check: ExecutedCheck, document: dict[str, object]) -> ExecutedCheck:
    assert check.pytest is not None
    artifact = replace(
        check.pytest.artifact,
        content=json.dumps(document, separators=(",", ":")).encode(),
    )
    return replace(check, pytest=replace(check.pytest, artifact=artifact))


@pytest.mark.parametrize(
    ("defect", "expected_code"),
    (
        ("not-finalized", "artifact_not_finalized"),
        ("unsafe-path", "artifact_invalid"),
        ("writer-diagnostic", "artifact_invalid"),
        ("writer-mismatch", "artifact_invalid"),
        ("bad-duration", "artifact_invalid"),
        ("bad-xfail", "artifact_invalid"),
        ("parallel-over-retry-and-exit", "unsupported_parallelism"),
        ("retry-over-exit", "unsupported_retries"),
        ("exit-mismatch", "exit_code_mismatch"),
    ),
)
def test_artifact_validation_precedence(defect: str, expected_code: str) -> None:
    check = _check()
    document = _artifact_document(check)
    if defect == "not-finalized":
        document["state"] = "started"
    elif defect == "unsafe-path":
        assert check.pytest is not None
        check = replace(
            check,
            pytest=replace(
                check.pytest,
                artifact=replace(check.pytest.artifact, state="unsafe_path", content=None),
            ),
        )
    elif defect == "writer-diagnostic":
        assert check.pytest is not None
        check = replace(
            check,
            pytest=replace(
                check.pytest,
                artifact=replace(check.pytest.artifact, diagnostic="writer marker malformed"),
            ),
        )
    elif defect == "writer-mismatch":
        document["writer_id"] = "other-writer"
    elif defect == "bad-duration":
        _report(document, 0)["duration"] = float("nan")
    elif defect == "bad-xfail":
        report = _report(document, 1)
        report["outcome"] = "failed"
        report["wasxfail_present"] = True
        report["wasxfail_valid"] = True
        report["wasxfail"] = "reason"
    elif defect in {"parallel-over-retry-and-exit", "retry-over-exit"}:
        flags = cast(dict[str, object], document["flags"])
        session = cast(dict[str, object], document["session"])
        flags["unsupported_retries"] = True
        session["exit_code"] = 1
        if defect == "parallel-over-retry-and-exit":
            flags["unsupported_parallelism"] = True
    elif defect == "exit-mismatch":
        session = cast(dict[str, object], document["session"])
        session["exit_code"] = 1
    else:
        raise AssertionError(f"unhandled defect: {defect}")

    result = validate_pytest_execution(_with_document(check, document))

    assert isinstance(result, PytestValidationFailure)
    assert result.code == expected_code


def test_expected_failure_normalization_and_immutable_snapshots() -> None:
    check = _check()
    document = _artifact_document(check)
    report = _report(document, 1)
    report["outcome"] = "failed"
    report["longrepr"] = "[XPASS(strict)] strict reason"

    result = validate_pytest_execution(_with_document(check, document))

    assert not isinstance(result, PytestValidationFailure)
    call = result.reports[1]
    assert call.expected_failure.kind == "xpass_strict"
    assert call.expected_failure.reason == "strict reason"
    assert call.expected_failure.strict is True
    assert call.expected_failure.affects_exit is True
    with pytest.raises(TypeError):
        cast(dict[str, bool], result.flags)["worker_metadata"] = True


@pytest.mark.parametrize(
    ("classification", "expected_code"),
    (
        ("unsupported_python", "unsupported_python"),
        ("module_unavailable", "module_unavailable"),
        ("spawn_failed", "spawn_failed"),
        ("terminated_by_signal", "terminated_by_signal"),
    ),
)
def test_preflight_failures_override_all_lower_observations(
    classification: PreflightClassification, expected_code: str
) -> None:
    preflight = PytestPreflightObservation(
        classification,
        PytestPreflightRecord((3, 13, 15), True, (8, 4, 2)),
        "preflight failure",
    )
    result = validate_pytest_execution(
        _check(
            preflight=preflight,
            artifact=PytestArtifactObservation("missing", None, (), None),
        )
    )

    assert isinstance(result, PytestValidationFailure)
    assert result.code == expected_code


@pytest.mark.parametrize("state", ("unsafe_path", "read_failed"))
def test_finalized_artifact_observation_failures_are_invalid(state: ArtifactState) -> None:
    check = _check()
    assert check.pytest is not None
    result = validate_pytest_execution(
        replace(
            check,
            pytest=replace(
                check.pytest,
                artifact=replace(check.pytest.artifact, state=state, content=None),
            ),
        )
    )

    assert isinstance(result, PytestValidationFailure)
    assert result.code == "artifact_invalid"


def test_expected_failure_shape_beats_parallelism_and_repeated_reports_are_retries() -> None:
    invalid = _artifact_document(_check())
    invalid_report = _report(invalid, 1)
    invalid_flags = cast(dict[str, object], invalid["flags"])
    invalid_report["outcome"] = "failed"
    invalid_report["wasxfail_present"] = True
    invalid_report["wasxfail"] = "reason"
    invalid_flags["unsupported_parallelism"] = True
    invalid_result = validate_pytest_execution(_with_document(_check(), invalid))

    repeated = _artifact_document(_check())
    repeated_reports = cast(list[object], repeated["reports"])
    assert isinstance(repeated_reports, list)
    repeated_reports.append(dict(_report(repeated, 1)))
    repeated_result = validate_pytest_execution(_with_document(_check(), repeated))

    assert isinstance(invalid_result, PytestValidationFailure)
    assert invalid_result.code == "artifact_invalid"
    assert isinstance(repeated_result, PytestValidationFailure)
    assert repeated_result.code == "unsupported_retries"


def test_started_artifact_precedes_schema_validation() -> None:
    document = _artifact_document(_check())
    document["state"] = "started"
    document["schema_version"] = 2

    result = validate_pytest_execution(_with_document(_check(), document))

    assert isinstance(result, PytestValidationFailure)
    assert result.code == "artifact_not_finalized"


_PREFLIGHT_SPECIFIC_CODES = {
    "preflight-unsupported-python": "unsupported_python",
    "preflight-module-unavailable": "module_unavailable",
    "preflight-unsupported-version": "unsupported_version",
    "preflight-invalid": "preflight_invalid",
}
_PREFLIGHT_LATER_DEFECTS = (
    "primary-spawn",
    "primary-signal",
    "artifact-missing",
    "artifact-not-finalized",
    "artifact-invalid-schema",
    "artifact-invalid-unsafe-path",
    "artifact-invalid-read-failed",
    "artifact-invalid-malformed-marker",
    "artifact-invalid-multiple-writers",
    "artifact-invalid-writer-mismatch",
    "artifact-invalid-expected-failure",
    "parallelism",
    "retry",
    "exit-mismatch",
)
_ARTIFACT_INVALID_DEFECTS = (
    "artifact-invalid-schema",
    "artifact-invalid-unsafe-path",
    "artifact-invalid-read-failed",
    "artifact-invalid-malformed-marker",
    "artifact-invalid-multiple-writers",
    "artifact-invalid-writer-mismatch",
    "artifact-invalid-expected-failure",
)
_MISSING_ARTIFACT_LATER_DEFECTS = (
    "artifact-invalid-malformed-marker",
    "artifact-invalid-multiple-writers",
    "parallelism",
    "retry",
    "exit-mismatch",
)
_NOT_FINALIZED_ARTIFACT_LATER_DEFECTS = (
    "artifact-invalid-schema",
    "artifact-invalid-malformed-marker",
    "artifact-invalid-multiple-writers",
    "artifact-invalid-writer-mismatch",
    "artifact-invalid-expected-failure",
    "parallelism",
    "retry",
    "exit-mismatch",
)


def _check_with_defects(*defects: str) -> ExecutedCheck:
    check = _check()
    document = _artifact_document(check)
    artifact_state: ArtifactState = "snapshot"
    writer_ids: tuple[str, ...] = ("writer-1",)
    artifact_diagnostic: str | None = None
    preflight: PytestPreflightObservation | None = None
    primary: ExecutedProcess | None = None
    for defect in defects:
        if defect == "preflight-unsupported-python":
            preflight = PytestPreflightObservation("unsupported_python", None, "unsupported")
        elif defect == "preflight-module-unavailable":
            preflight = PytestPreflightObservation("module_unavailable", None, "unavailable")
        elif defect == "preflight-unsupported-version":
            preflight = PytestPreflightObservation("unsupported_version", None, "unsupported")
        elif defect == "preflight-invalid":
            preflight = PytestPreflightObservation("preflight_invalid", None, "invalid")
        elif defect == "preflight-spawn":
            preflight = PytestPreflightObservation("spawn_failed", None, "spawn failed")
        elif defect == "preflight-signal":
            preflight = PytestPreflightObservation("terminated_by_signal", None, "signal")
        elif defect == "primary-spawn":
            primary = replace(_check().processes[0], returncode=None, spawn_error="spawn failed")
        elif defect == "primary-signal":
            primary = replace(_check().processes[0], returncode=-9)
        elif defect == "artifact-missing":
            artifact_state = "missing"
        elif defect == "artifact-not-finalized":
            document["state"] = "started"
        elif defect == "artifact-invalid-schema":
            document["state"] = "finalized"
            document["schema_version"] = 2
        elif defect == "artifact-invalid-unsafe-path":
            artifact_state = "unsafe_path"
        elif defect == "artifact-invalid-read-failed":
            artifact_state = "read_failed"
        elif defect == "artifact-invalid-malformed-marker":
            artifact_diagnostic = "writer marker malformed"
        elif defect == "artifact-invalid-multiple-writers":
            writer_ids = ("writer-1", "writer-2")
        elif defect == "artifact-invalid-writer-mismatch":
            document["writer_id"] = "other-writer"
        elif defect == "artifact-invalid-expected-failure":
            report = _report(document, 1)
            report["outcome"] = "failed"
            report["wasxfail_present"] = True
            report["wasxfail"] = "contradictory"
        elif defect == "parallelism":
            flags = cast(dict[str, object], document["flags"])
            flags["unsupported_parallelism"] = True
        elif defect == "retry":
            flags = cast(dict[str, object], document["flags"])
            flags["unsupported_retries"] = True
        elif defect == "exit-mismatch":
            session = cast(dict[str, object], document["session"])
            session["exit_code"] = 1
        else:
            raise AssertionError(f"unhandled defect: {defect}")
    check = _with_document(check, document)
    assert check.pytest is not None
    artifact = replace(
        check.pytest.artifact,
        state=artifact_state,
        content=(
            None
            if artifact_state in {"missing", "unsafe_path", "read_failed"}
            else check.pytest.artifact.content
        ),
        writer_ids=writer_ids,
        diagnostic=artifact_diagnostic,
    )
    return replace(
        check,
        processes=(primary,) if primary is not None else check.processes,
        pytest=replace(
            check.pytest,
            preflight=preflight if preflight is not None else check.pytest.preflight,
            artifact=artifact,
        ),
    )


@pytest.mark.parametrize(
    ("preflight_defect", "lower_defect", "expected_code"),
    tuple(
        (preflight_defect, lower_defect, expected_code)
        for preflight_defect, expected_code in _PREFLIGHT_SPECIFIC_CODES.items()
        for lower_defect in _PREFLIGHT_LATER_DEFECTS
    ),
)
def test_each_preflight_specific_code_wins_over_every_lower_observation(
    preflight_defect: str, lower_defect: str, expected_code: str
) -> None:
    result = validate_pytest_execution(_check_with_defects(lower_defect, preflight_defect))

    assert isinstance(result, PytestValidationFailure)
    assert result.code == expected_code


@pytest.mark.parametrize(
    ("higher", "lower", "expected_code"),
    (
        *(("preflight-spawn", lower, "spawn_failed") for lower in _PREFLIGHT_LATER_DEFECTS),
        *(("preflight-signal", lower, "terminated_by_signal") for lower in _PREFLIGHT_LATER_DEFECTS),
        *(("primary-spawn", lower, "spawn_failed") for lower in _PREFLIGHT_LATER_DEFECTS[2:]),
        *(("primary-signal", lower, "terminated_by_signal") for lower in _PREFLIGHT_LATER_DEFECTS[2:]),
        *(("artifact-missing", lower, "artifact_missing") for lower in _MISSING_ARTIFACT_LATER_DEFECTS),
        *(("artifact-not-finalized", lower, "artifact_not_finalized") for lower in _NOT_FINALIZED_ARTIFACT_LATER_DEFECTS),
        *((artifact_invalid, lower, "artifact_invalid") for artifact_invalid in _ARTIFACT_INVALID_DEFECTS for lower in ("parallelism", "retry", "exit-mismatch")),
        ("parallelism", "retry", "unsupported_parallelism"),
        ("parallelism", "exit-mismatch", "unsupported_parallelism"),
        ("retry", "exit-mismatch", "unsupported_retries"),
    ),
)
def test_each_meaningful_higher_precedence_observation_wins(
    higher: str, lower: str, expected_code: str
) -> None:
    result = validate_pytest_execution(_check_with_defects(lower, higher))

    assert isinstance(result, PytestValidationFailure)
    assert result.code == expected_code


@pytest.mark.parametrize(
    "defect",
    ("unsafe_path", "read_failed", "malformed-marker", "multiple-writers", "writer-mismatch"),
)
def test_artifact_observations_map_to_artifact_invalid(defect: str) -> None:
    check = _check()
    assert check.pytest is not None
    if defect in {"unsafe_path", "read_failed"}:
        check = replace(
            check,
            pytest=replace(
                check.pytest,
                artifact=replace(check.pytest.artifact, state=cast(ArtifactState, defect)),
            ),
        )
    elif defect == "malformed-marker":
        check = replace(
            check,
            pytest=replace(
                check.pytest,
                artifact=replace(check.pytest.artifact, diagnostic="writer marker malformed"),
            ),
        )
    elif defect == "multiple-writers":
        check = replace(
            check,
            pytest=replace(check.pytest, artifact=replace(check.pytest.artifact, writer_ids=("a", "b"))),
        )
    elif defect == "writer-mismatch":
        document = _artifact_document(check)
        document["writer_id"] = "other"
        check = _with_document(check, document)
    else:
        raise AssertionError(f"unhandled defect: {defect}")

    result = validate_pytest_execution(check)

    assert isinstance(result, PytestValidationFailure)
    assert result.code == "artifact_invalid"


@pytest.mark.parametrize(
    ("kind", "expected_kind", "expected_reason", "strict", "affects_exit"),
    (
        ("none", "none", None, None, False),
        ("xfail", "xfail", "xfail reason", None, False),
        ("xpass", "xpass_non_strict", "xpass reason", False, False),
        ("strict-empty", "xpass_strict", None, True, True),
    ),
)
def test_expected_failure_normalization_positive_shapes(
    kind: str,
    expected_kind: str,
    expected_reason: str | None,
    strict: bool | None,
    affects_exit: bool,
) -> None:
    document = _artifact_document(_check())
    report = _report(document, 1)
    if kind == "xfail":
        report["outcome"] = "skipped"
        report["wasxfail_present"] = True
        report["wasxfail"] = "xfail reason"
    elif kind == "xpass":
        report["wasxfail_present"] = True
        report["wasxfail"] = "xpass reason"
    elif kind == "strict-empty":
        report["outcome"] = "failed"
        report["longrepr"] = "[XPASS(strict)] "

    result = validate_pytest_execution(_with_document(_check(), document))

    assert not isinstance(result, PytestValidationFailure)
    expected = result.reports[1].expected_failure
    assert expected.kind == expected_kind
    assert expected.reason == expected_reason
    assert expected.strict is strict
    assert expected.affects_exit is affects_exit
