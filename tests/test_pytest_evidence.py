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
