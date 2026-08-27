from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import cast

import pytest

from pyrepo_check.execution import CapturedBytes, ExecutedCheck, ExecutedProcess
from pyrepo_check.planning import (
    CheckInvocation,
    DefaultRepositoryPython,
    PlannedTestScope,
    PytestExecutionPlan,
    RunPlan,
)
from pyrepo_check.pytest_execution import (
    ArtifactState,
    PreflightClassification,
    PytestArtifactObservation,
    PytestExecutionObservation,
    PytestPreflightObservation,
    PytestPreflightRecord,
)
from pyrepo_check.pytest_evidence import (
    CollectionIssue,
    PytestCounts,
    PytestError,
    PytestEvidence,
    PytestValidationFailure,
    PytestResult,
    SlowTest,
    SpecialTestOutcome,
    ValidatedExpectedFailure,
    ValidatedPhaseReport,
    ValidatedPytestSession,
    _build_evidence,
    _round_phase_durations,
    build_pytest_result,
    validate_pytest_execution,
)
from tests.test_pytest_report_plugin import run_plugin_project


class _CountingNodeId(str):
    comparisons = 0

    def __eq__(self, other: object) -> bool:
        type(self).comparisons += 1
        return super().__eq__(other)

    __hash__ = str.__hash__


@pytest.mark.parametrize(
    ("test_source", "project_sources", "invocation_args", "expected_complete"),
    (
        ("def test_case():\n    assert True\n", None, (), True),
        ("def test_case():\n    assert False\n", None, (), True),
        (
            "import pytest\n\n@pytest.mark.skip(reason='skip')\ndef test_case(): pass\n",
            None,
            (),
            True,
        ),
        (
            "import pytest\n\n@pytest.mark.xfail(reason='xfail')\ndef test_case(): assert False\n",
            None,
            (),
            True,
        ),
        (
            "def test_case(failing_setup): pass\n",
            {
                "conftest.py": (
                    "import pytest\n\n@pytest.fixture\ndef failing_setup():\n"
                    "    raise RuntimeError('setup')\n"
                )
            },
            (),
            True,
        ),
        (
            "def test_case(failing_teardown): pass\n",
            {
                "conftest.py": (
                    "import pytest\n\n@pytest.fixture\ndef failing_teardown():\n"
                    "    yield\n    raise RuntimeError('teardown')\n"
                )
            },
            (),
            True,
        ),
        ("def test_case():\n    assert True\n", None, ("--setup-only",), True),
        ("def test_case():\n    assert True\n", None, ("--setup-plan",), True),
        ("def test_case():\n    assert True\n", None, ("--collect-only",), False),
        (
            "def test_first(): assert False\n\ndef test_second(): assert True\n",
            None,
            ("-x",),
            False,
        ),
        (
            "def test_kept(): pass\n\ndef test_removed(): pass\n",
            None,
            ("-k", "kept"),
            True,
        ),
    ),
    ids=(
        "pass",
        "fail",
        "skip",
        "xfail",
        "setup-error",
        "teardown-error",
        "setup-only",
        "setup-plan",
        "collect-only",
        "early-stop",
        "deselection",
    ),
)
def test_real_plugin_artifact_matrix_passes_task_five_validation(
    tmp_path: Path,
    test_source: str,
    project_sources: dict[str, str] | None,
    invocation_args: tuple[str, ...],
    expected_complete: bool,
) -> None:
    run = run_plugin_project(
        tmp_path,
        test_source,
        project_sources=project_sources,
        invocation_args=invocation_args,
    )
    check = _check()
    version = tuple(int(piece) for piece in str(run.artifact["pytest_version"]).split(".")[:3])
    primary = replace(check.processes[0], returncode=run.completed.returncode)
    observation = PytestExecutionObservation(
        preflight=PytestPreflightObservation(
            "supported",
            PytestPreflightRecord((3, 13, 15), True, cast(tuple[int, int, int], version)),
            None,
        ),
        artifact=PytestArtifactObservation(
            "snapshot",
            run.artifact_path.read_bytes(),
            (cast(str, run.artifact["writer_id"]),),
            None,
        ),
        cleanup_error=None,
    )
    check = replace(check, processes=(primary,), pytest=observation)

    assert isinstance(validate_pytest_execution(check), ValidatedPytestSession)
    result = build_pytest_result(_plan(check), check)
    assert result.complete is expected_complete
    assert result.evidence is not None


def _check(
    *,
    preflight: PytestPreflightObservation | None = None,
    primary: ExecutedProcess | None = None,
    artifact: PytestArtifactObservation | None = None,
    cleanup_error: str | None = None,
) -> ExecutedCheck:
    cwd = Path("/consumer")
    planned = CheckInvocation(
        name="pytest",
        arguments=("tests",),
        pytest=PytestExecutionPlan(pytest_args=("tests",)),
    )
    trusted_preflight = PytestPreflightObservation(
        "supported", PytestPreflightRecord((3, 13, 15), True, (8, 4, 2)), None
    )
    primary_process = ExecutedProcess(
        role="primary",
        command=("uv", "run", "--locked", "python", "-m", "pytest", *planned.arguments),
        cwd=cwd,
        returncode=0,
        duration_ms=1,
        stdout=CapturedBytes(b"", 0),
        stderr=CapturedBytes(b"", 0),
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


@pytest.mark.parametrize(("list_depth", "valid"), ((63, True), (64, False)))
def test_artifact_json_nesting_64_is_valid_and_65_is_typed_invalid(
    list_depth: int,
    valid: bool,
) -> None:
    check = _check()
    document = _artifact_document(check)
    nested: object = 0
    for _ in range(list_depth):
        nested = [nested]
    document["unknown_nested_metadata"] = nested

    result = validate_pytest_execution(_with_document(check, document))

    if valid:
        assert isinstance(result, ValidatedPytestSession)
    else:
        assert isinstance(result, PytestValidationFailure)
        assert result.code == "artifact_invalid"


def test_artifact_json_recursion_error_is_typed_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def recurse(_content: bytes, **_kwargs: object) -> object:
        raise RecursionError("too deep")

    monkeypatch.setattr("pyrepo_check.pytest_execution.json.loads", recurse)

    result = validate_pytest_execution(_check())

    assert isinstance(result, PytestValidationFailure)
    assert result.code == "artifact_invalid"


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


@pytest.mark.parametrize(
    ("state", "diagnostic"),
    (
        ("unsafe_path", "path is not a regular file: artifact.json"),
        ("read_failed", "artifact read failed: Permission denied"),
    ),
)
def test_artifact_observation_failure_preserves_exact_diagnostic(
    state: ArtifactState,
    diagnostic: str,
) -> None:
    check = _check()
    assert check.pytest is not None
    result = validate_pytest_execution(
        replace(
            check,
            pytest=replace(
                check.pytest,
                artifact=replace(
                    check.pytest.artifact,
                    state=state,
                    content=None,
                    diagnostic=diagnostic,
                ),
            ),
        )
    )

    assert isinstance(result, PytestValidationFailure)
    assert result.code == "artifact_invalid"
    assert result.message == diagnostic


@pytest.mark.parametrize("constant", ("NaN", "Infinity", "-Infinity"))
def test_artifact_rejects_non_finite_ignored_metadata(constant: str) -> None:
    check = _check()
    assert check.pytest is not None
    content = check.pytest.artifact.content
    assert content is not None
    injected = content[:-1] + f',"ignored":{constant}}}'.encode()
    check = replace(
        check,
        pytest=replace(
            check.pytest,
            artifact=replace(check.pytest.artifact, content=injected),
        ),
    )

    result = validate_pytest_execution(check)

    assert isinstance(result, PytestValidationFailure)
    assert result.code == "artifact_invalid"
    assert result.message == "pytest artifact is not valid JSON"


def test_malformed_artifact_preserves_writer_inventory_diagnostic() -> None:
    check = _check()
    assert check.pytest is not None
    check = replace(
        check,
        pytest=replace(
            check.pytest,
            artifact=replace(
                check.pytest.artifact,
                content=b"{malformed",
                diagnostic="writer marker iteration failed: PermissionError: denied",
            ),
        ),
    )

    result = validate_pytest_execution(check)

    assert isinstance(result, PytestValidationFailure)
    assert result.code == "artifact_invalid"
    assert result.message == (
        "pytest artifact is not valid JSON; writer marker iteration failed: PermissionError: denied"
    )


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
_CONTENT_BEARING_ARTIFACT_INVALID_DEFECTS = (
    "artifact-invalid-schema",
    "artifact-invalid-malformed-marker",
    "artifact-invalid-multiple-writers",
    "artifact-invalid-writer-mismatch",
    "artifact-invalid-expected-failure",
)
_MISSING_ARTIFACT_LATER_DEFECTS = (
    "artifact-invalid-malformed-marker",
    "artifact-invalid-multiple-writers",
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


def _assert_observable_defect(check: ExecutedCheck, defect: str) -> None:
    assert check.pytest is not None
    observation = check.pytest
    preflight_codes = {
        "preflight-unsupported-python": "unsupported_python",
        "preflight-module-unavailable": "module_unavailable",
        "preflight-unsupported-version": "unsupported_version",
        "preflight-invalid": "preflight_invalid",
        "preflight-spawn": "spawn_failed",
        "preflight-signal": "terminated_by_signal",
    }
    if defect in preflight_codes:
        assert observation.preflight.classification == preflight_codes[defect]
    elif defect == "primary-spawn":
        assert check.processes[0].spawn_error is not None
    elif defect == "primary-signal":
        assert check.processes[0].returncode is not None
        assert check.processes[0].returncode < 0
    elif defect == "artifact-missing":
        assert observation.artifact.state == "missing"
        assert observation.artifact.content is None
    elif defect == "artifact-not-finalized":
        assert observation.artifact.state == "snapshot"
        assert _observable_document(check, defect)["state"] == "started"
    elif defect == "artifact-invalid-schema":
        assert _observable_document(check, defect)["schema_version"] == 2
    elif defect == "artifact-invalid-unsafe-path":
        assert observation.artifact.state == "unsafe_path"
        assert observation.artifact.content is None
    elif defect == "artifact-invalid-read-failed":
        assert observation.artifact.state == "read_failed"
        assert observation.artifact.content is None
    elif defect == "artifact-invalid-malformed-marker":
        assert observation.artifact.diagnostic == "writer marker malformed"
    elif defect == "artifact-invalid-multiple-writers":
        assert observation.artifact.writer_ids == ("writer-1", "writer-2")
    elif defect == "artifact-invalid-writer-mismatch":
        assert _observable_document(check, defect)["writer_id"] == "other-writer"
    elif defect == "artifact-invalid-expected-failure":
        report = _report(_observable_document(check, defect), 1)
        assert report["outcome"] == "failed"
        assert report["wasxfail_present"] is True
    elif defect == "parallelism":
        flags = cast(dict[str, object], _observable_document(check, defect)["flags"])
        assert flags["unsupported_parallelism"] is True
    elif defect == "retry":
        flags = cast(dict[str, object], _observable_document(check, defect)["flags"])
        assert flags["unsupported_retries"] is True
    elif defect == "exit-mismatch":
        session = cast(dict[str, object], _observable_document(check, defect)["session"])
        assert session["exit_code"] != check.processes[0].returncode
    else:
        raise AssertionError(f"unhandled observable defect: {defect}")


def _observable_document(check: ExecutedCheck, defect: str) -> dict[str, object]:
    assert check.pytest is not None
    if check.pytest.artifact.content is None:
        raise AssertionError(f"{defect} requires artifact content")
    return _artifact_document(check)


def _validate_observable_combination(
    higher: str, lower: str
) -> ValidatedPytestSession | PytestValidationFailure:
    check = _check_with_defects(lower, higher)
    _assert_observable_defect(check, lower)
    _assert_observable_defect(check, higher)
    return validate_pytest_execution(check)


def test_fixture_guard_rejects_discarded_document_only_mutation() -> None:
    check = _check_with_defects("artifact-missing", "parallelism")

    with pytest.raises(AssertionError, match="parallelism requires artifact content"):
        _assert_observable_defect(check, "parallelism")


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
    result = _validate_observable_combination(preflight_defect, lower_defect)

    assert isinstance(result, PytestValidationFailure)
    assert result.code == expected_code


@pytest.mark.parametrize(
    ("higher", "lower", "expected_code"),
    (
        *(("preflight-spawn", lower, "spawn_failed") for lower in _PREFLIGHT_LATER_DEFECTS),
        *(
            ("preflight-signal", lower, "terminated_by_signal")
            for lower in _PREFLIGHT_LATER_DEFECTS
        ),
        *(("primary-spawn", lower, "spawn_failed") for lower in _PREFLIGHT_LATER_DEFECTS[2:]),
        *(
            ("primary-signal", lower, "terminated_by_signal")
            for lower in _PREFLIGHT_LATER_DEFECTS[2:]
        ),
        *(
            ("artifact-missing", lower, "artifact_missing")
            for lower in _MISSING_ARTIFACT_LATER_DEFECTS
        ),
        *(
            ("artifact-not-finalized", lower, "artifact_not_finalized")
            for lower in _NOT_FINALIZED_ARTIFACT_LATER_DEFECTS
        ),
        *(
            (artifact_invalid, lower, "artifact_invalid")
            for artifact_invalid in _CONTENT_BEARING_ARTIFACT_INVALID_DEFECTS
            for lower in ("parallelism", "retry", "exit-mismatch")
        ),
        ("parallelism", "retry", "unsupported_parallelism"),
        ("parallelism", "exit-mismatch", "unsupported_parallelism"),
        ("retry", "exit-mismatch", "unsupported_retries"),
    ),
)
def test_each_meaningful_higher_precedence_observation_wins(
    higher: str, lower: str, expected_code: str
) -> None:
    result = _validate_observable_combination(higher, lower)

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
            pytest=replace(
                check.pytest, artifact=replace(check.pytest.artifact, writer_ids=("a", "b"))
            ),
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


def _plan(check: ExecutedCheck, *, scope: str = "complete") -> RunPlan:
    pytest = check.planned.pytest
    assert pytest is not None
    return RunPlan(
        root=Path("/consumer"),
        repository_python=DefaultRepositoryPython(),
        mode="strict_aggregate",
        targets=(),
        checks=(check.planned,),
        output_format="json",
        pytest_args=pytest.pytest_args,
        planned_test_scope=cast(PlannedTestScope, scope),
    )


def _with_exit(
    check: ExecutedCheck, exit_code: int, *, stopped_early: bool = False
) -> ExecutedCheck:
    document = _artifact_document(check)
    session = cast(dict[str, object], document["session"])
    session["exit_code"] = exit_code
    session["stopped_early"] = stopped_early
    primary = replace(check.processes[0], returncode=exit_code)
    return replace(_with_document(check, document), processes=(primary,))


def test_evidence_null_on_validation_failure_keeps_only_planner_and_incomplete_scope() -> None:
    result = build_pytest_result(
        _plan(_check(), scope="partial"), _check_with_defects("artifact-missing")
    )

    assert result == PytestResult(
        status="error",
        complete=False,
        scope="partial",
        scope_reasons=("planned_selector", "incomplete_session"),
        pytest_version="8.4.2",
        exit_code=0,
        evidence=None,
        error=PytestError("artifact_missing", "pytest artifact is missing"),
    )


@pytest.mark.parametrize(
    ("exit_code", "stopped_early", "expected_status", "expected_complete", "expected_error"),
    (
        (0, False, "passed", True, None),
        (1, False, "failed", True, None),
        (1, True, "failed", False, "session_incomplete"),
        (2, False, "error", False, "interrupted"),
        (3, False, "error", False, "internal_error"),
        (4, False, "error", False, "usage_error"),
        (9, False, "error", False, "unknown_exit_code"),
    ),
)
def test_exit_matrix_retains_valid_evidence_when_allowed(
    exit_code: int,
    stopped_early: bool,
    expected_status: str,
    expected_complete: bool,
    expected_error: str | None,
) -> None:
    result = build_pytest_result(
        _plan(_check()), _with_exit(_check(), exit_code, stopped_early=stopped_early)
    )

    assert result.status == expected_status
    assert result.complete is expected_complete
    assert result.exit_code == exit_code
    assert result.evidence is not None
    assert (None if result.error is None else result.error.code) == expected_error


def test_exit_matrix_treats_valid_zero_collected_as_complete_failure() -> None:
    check = _check()
    document = _artifact_document(check)
    collection = cast(dict[str, object], document["collection"])
    collection["initial_nodeids"] = []
    collection["final_nodeids"] = []
    document["reports"] = []
    check = _with_document(check, document)
    result = build_pytest_result(_plan(check), _with_exit(check, 5))

    assert result.status == "failed"
    assert result.complete is True
    assert result.evidence is not None
    assert result.evidence.collected == 0
    assert result.error is None


def test_validation_rejects_false_pass_report_outside_empty_final_collection() -> None:
    check = _check()
    document = _artifact_document(check)
    collection = cast(dict[str, object], document["collection"])
    collection["initial_nodeids"] = []
    collection["final_nodeids"] = []
    document["reports"] = [_phase("not-selected", "call", "passed", 0.0, expected="none")]
    changed = _with_document(check, document)

    result = build_pytest_result(_plan(changed), changed)

    assert result.status == "error"
    assert result.evidence is None
    assert result.error is not None
    assert result.error.code == "artifact_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("initial_nodeids", ["node", "node"]),
        ("final_nodeids", ["node", "node"]),
        ("deselected_nodeids", ["gone", "gone"]),
        ("uncovered_removed_nodeids", ["gone", "gone"]),
        ("final_nodeids", ["outside"]),
        ("deselected_nodeids", ["node"]),
        ("uncovered_removed_nodeids", ["node"]),
    ),
)
def test_validation_rejects_duplicate_or_inconsistent_collection_sets(
    field: str,
    value: list[str],
) -> None:
    check = _check()
    document = _artifact_document(check)
    collection = cast(dict[str, object], document["collection"])
    if field in {"deselected_nodeids", "uncovered_removed_nodeids"} and value == ["gone", "gone"]:
        collection["initial_nodeids"] = ["tests/test_ok.py::test_ok", "gone"]
    collection[field] = value

    result = build_pytest_result(_plan(check), _with_document(check, document))

    assert result.error is not None
    assert result.error.code == "artifact_invalid"
    assert result.evidence is None


@pytest.mark.parametrize(
    "phases",
    (
        ("call", "setup", "teardown"),
        ("setup", "teardown", "call"),
        ("call",),
        ("teardown",),
    ),
)
def test_validation_rejects_impossible_phase_order(phases: tuple[str, ...]) -> None:
    check = _check()
    document = _artifact_document(check)
    document["reports"] = [
        _phase("tests/test_ok.py::test_ok", phase, "passed", 0.0, expected="none")
        for phase in phases
    ]

    result = build_pytest_result(_plan(check), _with_document(check, document))

    assert result.error is not None
    assert result.error.code == "artifact_invalid"


def test_setup_and_passing_call_without_teardown_is_trusted_but_incomplete() -> None:
    check = _check()
    document = _artifact_document(check)
    reports = cast(list[dict[str, object]], document["reports"])
    document["reports"] = reports[:2]
    document["effective_args"] = []
    options = cast(dict[str, object], document["semantic_options"])
    options["collection_paths"] = []
    changed = _with_document(check, document)

    validated = validate_pytest_execution(changed)
    result = build_pytest_result(_plan(changed), changed)

    assert isinstance(validated, ValidatedPytestSession)
    assert result.status == "passed"
    assert result.complete is False
    assert result.scope == "partial"
    assert result.scope_reasons == ("incomplete_session",)
    assert result.pytest_version == "8.4.2"
    assert result.exit_code == 0
    assert result.evidence is not None
    assert result.evidence.counts == PytestCounts(1, 0, 0, 0, 0, 0)
    assert result.error is None


def test_evidence_consolidation_uses_linear_membership_operations() -> None:
    nodeids = tuple(_CountingNodeId(f"node-{index}") for index in range(2_000))
    reports = tuple(
        ValidatedPhaseReport(
            nodeid,
            "call",
            "passed",
            0.0,
            None,
            ValidatedExpectedFailure("none", None, None, False),
        )
        for nodeid in reversed(nodeids)
    )
    validated = ValidatedPytestSession(
        pytest_version="8.4.2",
        exit_code=0,
        effective_args=(),
        semantic_options={"setuponly": False, "setupplan": False},
        collection={
            "final_nodeids": nodeids,
            "deselected_nodeids": (),
            "errors": (),
            "skips": (),
        },
        reports=reports,
        flags={},
        session={},
    )
    _CountingNodeId.comparisons = 0

    evidence, terminal = _build_evidence(validated)

    assert evidence.counts.passed == len(nodeids)
    assert terminal == set(nodeids)
    assert _CountingNodeId.comparisons < len(nodeids) * 10


@pytest.mark.parametrize(
    ("defect", "expected_code"),
    (("primary-spawn", "spawn_failed"), ("primary-signal", "terminated_by_signal")),
)
def test_exit_matrix_maps_spawn_and_signal_to_incomplete_evidence_null(
    defect: str, expected_code: str
) -> None:
    check = _check_with_defects(defect)

    result = build_pytest_result(_plan(check), check)

    assert result.status == "error"
    assert result.complete is False
    assert result.exit_code is None
    assert result.evidence is None
    assert result.error is not None
    assert result.error.code == expected_code


def test_consolidation_orders_special_outcomes_slowest_and_collection_issues() -> None:
    check = _check()
    document = _artifact_document(check)
    document["collection"] = {
        "initial_nodeids": ["z", "a", "b", "c", "d"],
        "final_nodeids": ["z", "a", "b", "c", "d"],
        "deselected_nodeids": [],
        "uncovered_removed_nodeids": [],
        "errors": [{"nodeid": "z", "message": "later"}, {"nodeid": "a", "message": "first"}],
        "skips": [{"nodeid": "z", "message": "skip"}, {"nodeid": "a", "message": "other"}],
    }
    reports: list[dict[str, object]] = []
    for nodeid, outcome, expected, duration in (
        ("z", "passed", "none", 0.0045),
        ("a", "failed", "none", 0.001),
        ("b", "failed", "strict", 0.002),
        ("c", "skipped", "xfail", 0.003),
        ("d", "passed", "xpass", 0.004),
    ):
        reports.extend(_phases(nodeid, outcome, expected, duration))
    reports[3]["outcome"] = "failed"
    document["reports"] = reports
    check = _with_document(check, document)

    result = build_pytest_result(_plan(check), _with_exit(check, 1))

    assert result.evidence == PytestEvidence(
        effective_args=("tests",),
        collected=5,
        deselected=0,
        counts=PytestCounts(passed=1, failed=0, errors=1, skipped=0, xfailed=1, xpassed=2),
        collection_errors=(CollectionIssue("a", "first"), CollectionIssue("z", "later")),
        collection_skips=(CollectionIssue("a", "other"), CollectionIssue("z", "skip")),
        slowest=(
            SlowTest("z", 14),
            SlowTest("d", 12),
            SlowTest("c", 9),
            SlowTest("b", 6),
            SlowTest("a", 3),
        ),
        special_outcomes=(
            SpecialTestOutcome("b", "xpassed", None, True, True, 6),
            SpecialTestOutcome("c", "xfailed", "xfail reason", None, False, 9),
            SpecialTestOutcome("d", "xpassed", "xpass reason", False, False, 12),
        ),
    )


@pytest.mark.parametrize(
    ("args", "mutate", "expected_reasons"),
    (
        (("-ra",), None, ()),
        (("-k", "fast"), None, ("effective_narrowing_option",)),
        (("--tb", "short"), None, ()),
        (("--mystery",), None, ("unclassified_external_option",)),
        ((), "deselected", ("deselected_tests",)),
        ((), "reduced", ("collection_reduced",)),
    ),
)
def test_scope_classifies_known_neutral_narrowing_unknown_and_collection_reasons(
    args: tuple[str, ...], mutate: str | None, expected_reasons: tuple[str, ...]
) -> None:
    check = _check()
    document = _artifact_document(check)
    document["effective_args"] = list(args)
    options = cast(dict[str, object], document["semantic_options"])
    options["collection_paths"] = []
    collection = cast(dict[str, object], document["collection"])
    if mutate == "deselected":
        collection["initial_nodeids"] = [
            "tests/test_ok.py::test_ok",
            "gone",
        ]
        collection["deselected_nodeids"] = ["gone"]
    elif mutate == "reduced":
        collection["initial_nodeids"] = [
            "tests/test_ok.py::test_ok",
            "gone",
        ]
        collection["uncovered_removed_nodeids"] = ["gone"]
    result = build_pytest_result(
        _plan(_with_document(check, document)), _with_document(check, document)
    )

    assert result.scope_reasons == expected_reasons
    assert result.scope == ("complete" if not expected_reasons else "partial")


def _phases(
    nodeid: str, call_outcome: str, expected: str, duration: float
) -> list[dict[str, object]]:
    phases = [
        _phase(nodeid, "setup", "passed", duration, expected="none"),
        _phase(nodeid, "call", call_outcome, duration, expected=expected),
        _phase(nodeid, "teardown", "passed", duration, expected="none"),
    ]
    return phases


def _phase(
    nodeid: str, when: str, outcome: str, duration: float, *, expected: str
) -> dict[str, object]:
    report: dict[str, object] = {
        "nodeid": nodeid,
        "when": when,
        "outcome": outcome,
        "duration": duration,
        "wasxfail_present": False,
        "wasxfail_valid": True,
        "wasxfail": None,
        "longrepr": None,
    }
    if expected == "xfail":
        report.update(wasxfail_present=True, wasxfail="xfail reason")
    elif expected == "xpass":
        report.update(wasxfail_present=True, wasxfail="xpass reason")
    elif expected == "strict":
        report["longrepr"] = "[XPASS(strict)] "
    return report


@pytest.mark.parametrize(
    "args",
    (
        ("-r", "a"),
        ("-q", "-q"),
        ("-v", "-v"),
        ("--tb=short",),
        ("-l", "--no-showlocals"),
        ("--color", "yes"),
        ("--code-highlight=no",),
        ("-s", "--disable-warnings"),
        ("--strict-config", "--strict-markers"),
        ("--durations", "5", "--durations-min=1.5"),
    ),
)
def test_scope_accepts_every_frozen_neutral_option_form(args: tuple[str, ...]) -> None:
    check = _check()
    document = _artifact_document(check)
    document["effective_args"] = list(args)
    options = cast(dict[str, object], document["semantic_options"])
    options["collection_paths"] = []

    result = build_pytest_result(
        _plan(_with_document(check, document)), _with_document(check, document)
    )

    assert result.scope == "complete"
    assert result.scope_reasons == ()


@pytest.mark.parametrize(
    "args",
    (
        ("-k", "fast"),
        ("-m", "slow"),
        ("--deselect", "test_a.py::test_a"),
        ("--ignore=test_a.py",),
        ("--ignore-glob", "*_generated.py"),
        ("--lf",),
        ("--last-failed",),
        ("--pyargs",),
        ("--collect-only",),
        ("--setup-only",),
        ("--setup-plan",),
        ("test_direct.py",),
    ),
)
def test_scope_marks_every_known_narrowing_argument(args: tuple[str, ...]) -> None:
    check = _check()
    document = _artifact_document(check)
    document["effective_args"] = list(args)
    options = cast(dict[str, object], document["semantic_options"])
    options["collection_paths"] = []

    result = build_pytest_result(
        _plan(_with_document(check, document)), _with_document(check, document)
    )

    assert result.scope_reasons == ("effective_narrowing_option",)


def test_scope_uses_fixed_reason_order_for_semantic_mutation_and_incomplete_session() -> None:
    check = _check()
    document = _artifact_document(check)
    document["effective_args"] = ["--unknown"]
    options = cast(dict[str, object], document["semantic_options"])
    options["collection_paths"] = []
    options["keyword"] = "injected"
    collection = cast(dict[str, object], document["collection"])
    collection["initial_nodeids"] = [
        "tests/test_ok.py::test_ok",
        "gone",
        "missing",
    ]
    collection["deselected_nodeids"] = ["gone"]
    collection["uncovered_removed_nodeids"] = ["missing"]
    changed = _with_document(check, document)

    result = build_pytest_result(
        _plan(changed, scope="partial"), _with_exit(changed, 1, stopped_early=True)
    )

    assert result.scope_reasons == (
        "planned_selector",
        "effective_narrowing_option",
        "unclassified_external_option",
        "deselected_tests",
        "collection_reduced",
        "incomplete_session",
    )


def test_teardown_failure_beats_a_normalized_xfail_without_reparsing_raw_metadata() -> None:
    check = _check()
    document = _artifact_document(check)
    reports = cast(list[dict[str, object]], document["reports"])
    reports[1].update(outcome="skipped", wasxfail_present=True, wasxfail="expected")
    reports[2]["outcome"] = "failed"
    changed = _with_document(check, document)

    result = build_pytest_result(_plan(changed), _with_exit(changed, 1))

    assert result.evidence is not None
    assert result.evidence.counts == PytestCounts(0, 0, 1, 0, 0, 0)
    assert result.evidence.special_outcomes == ()


def test_exit_five_with_collected_evidence_is_failed_and_incomplete() -> None:
    check = _check()

    result = build_pytest_result(_plan(check), _with_exit(check, 5))

    assert result.status == "failed"
    assert result.complete is False
    assert result.error is None
    assert result.scope_reasons == ("effective_narrowing_option", "incomplete_session")


@pytest.mark.parametrize(
    "args",
    (("--", "tests"), ("--", "tests", "--option-looking", "more-tests")),
)
def test_scope_treats_every_argument_after_delimiter_as_a_known_positional(
    args: tuple[str, ...],
) -> None:
    check = _check()
    document = _artifact_document(check)
    document["effective_args"] = list(args)
    options = cast(dict[str, object], document["semantic_options"])
    options["collection_paths"] = []
    changed = _with_document(check, document)

    result = build_pytest_result(_plan(changed), changed)

    assert result.scope_reasons == ("effective_narrowing_option",)


def test_large_finite_phase_duration_rounds_without_rejecting_valid_evidence() -> None:
    check = _check()
    document = _artifact_document(check)
    reports = cast(list[dict[str, object]], document["reports"])
    reports[1]["duration"] = 1e30
    changed = _with_document(check, document)

    result = build_pytest_result(_plan(changed), changed)

    assert result.evidence is not None
    assert result.evidence.slowest == (SlowTest("tests/test_ok.py::test_ok", 10**33 + 200),)


def test_phase_durations_sum_exactly_before_the_final_half_up_rounding() -> None:
    check = _check()
    document = _artifact_document(check)
    reports = cast(list[dict[str, object]], document["reports"])
    reports[0]["duration"] = 1e30
    reports[1]["duration"] = 0.0005
    reports[2]["duration"] = 0
    changed = _with_document(check, document)

    result = build_pytest_result(_plan(changed), changed)

    assert result.evidence is not None
    assert result.evidence.slowest == (SlowTest("tests/test_ok.py::test_ok", 10**33 + 1),)


@pytest.mark.parametrize(
    ("durations", "expected_duration_ms"),
    (
        ((0.125, 0.225, 0.325), 675),
        ((0.9995, 0.0005, 0), 1000),
    ),
    ids=("common-exponent", "carry"),
)
def test_phase_duration_rounding_preserves_exact_small_value_boundaries(
    durations: tuple[float, float, float], expected_duration_ms: int
) -> None:
    check = _check()
    document = _artifact_document(check)
    reports = cast(list[dict[str, object]], document["reports"])
    for report, duration in zip(reports, durations, strict=True):
        report["duration"] = duration
    changed = _with_document(check, document)

    result = build_pytest_result(_plan(changed), changed)

    assert result.evidence is not None
    assert result.evidence.slowest == (SlowTest("tests/test_ok.py::test_ok", expected_duration_ms),)


def test_phase_duration_rounding_carries_many_submillisecond_phases() -> None:
    check = _check()
    validated = validate_pytest_execution(check)
    assert isinstance(validated, ValidatedPytestSession)

    reports = [replace(validated.reports[0], duration=0.0005) for _ in range(2001)]

    assert _round_phase_durations(reports) == 1001


@pytest.mark.parametrize(
    ("exit_code", "stopped_early"),
    ((1, True), (2, False)),
)
def test_incomplete_early_stop_or_interruption_keeps_partial_counts(
    exit_code: int, stopped_early: bool
) -> None:
    check = _check()
    document = _artifact_document(check)
    reports = cast(list[dict[str, object]], document["reports"])
    document["reports"] = [reports[0]]
    changed = _with_document(check, document)

    result = build_pytest_result(
        _plan(changed), _with_exit(changed, exit_code, stopped_early=stopped_early)
    )

    assert result.complete is False
    assert result.evidence is not None
    assert result.evidence.collected == 1
    assert result.evidence.counts == PytestCounts(0, 0, 0, 0, 0, 0)
    assert result.scope_reasons[-1] == "incomplete_session"


def test_slowest_keeps_ten_deterministic_entries_at_a_duration_tie_boundary() -> None:
    check = _check()
    document = _artifact_document(check)
    nodeids = [f"test_{index:02d}" for index in range(12)]
    collection = cast(dict[str, object], document["collection"])
    collection["initial_nodeids"] = nodeids
    collection["final_nodeids"] = nodeids
    reports: list[dict[str, object]] = []
    for nodeid in reversed(nodeids):
        reports.extend(_phases(nodeid, "passed", "none", 0.001))
    document["reports"] = reports
    changed = _with_document(check, document)

    result = build_pytest_result(_plan(changed), changed)

    assert result.evidence is not None
    assert result.evidence.slowest == tuple(SlowTest(f"test_{index:02d}", 3) for index in range(10))


def test_collection_issue_order_breaks_nodeid_ties_by_message() -> None:
    check = _check()
    document = _artifact_document(check)
    collection = cast(dict[str, object], document["collection"])
    collection["errors"] = [
        {"nodeid": "same", "message": "zeta"},
        {"nodeid": "same", "message": "alpha"},
    ]
    collection["skips"] = [
        {"nodeid": "same", "message": "later"},
        {"nodeid": "same", "message": "earlier"},
    ]
    changed = _with_document(check, document)

    result = build_pytest_result(_plan(changed), changed)

    assert result.evidence is not None
    assert result.evidence.collection_errors == (
        CollectionIssue("same", "alpha"),
        CollectionIssue("same", "zeta"),
    )
    assert result.evidence.collection_skips == (
        CollectionIssue("same", "earlier"),
        CollectionIssue("same", "later"),
    )
