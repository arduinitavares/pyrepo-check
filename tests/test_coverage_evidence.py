from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
import json
import math
import os
from pathlib import Path
from typing import Any, Literal

import pytest

from pyrepo_check.coverage_evidence import (
    CoverageCounts,
    CoverageError,
    CoverageErrorCode,
    CoverageFile,
    CoverageResult,
    CoverageThreshold,
    CoverageThresholdSkipReason,
    CoverageTotals,
    FileBranchCoverage,
    FileStatementCoverage,
    build_coverage_result,
    coverage_gate_policy,
    is_supported_coverage_version,
    validate_coverage_result,
    validate_coverage_json,
)
from pyrepo_check.coverage_execution import (
    CoverageArtifactObservation,
    CoverageArtifactState,
    CoverageExecutionObservation,
    CoveragePreflightClassification,
    CoveragePreflightObservation,
    CoveragePreflightRecord,
)
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


CoveragePreflightErrorClassification = Literal[
    "unsupported_python",
    "module_unavailable",
    "unsupported_version",
    "preflight_invalid",
    "spawn_failed",
    "terminated_by_signal",
]


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


def _coverage_project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    for relative_path in ("src/alpha.py", "src/beta.py", "src/gamma.py", "src/zero.py"):
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {relative_path}\n")
    return root


def _coverage_json_document() -> dict[str, Any]:
    return {
        "meta": {
            "format": 3,
            "version": "7.15.2",
            "timestamp": "2026-08-25T12:00:00Z",
            "branch_coverage": True,
            "show_contexts": False,
        },
        "files": {
            "src/alpha.py": {
                "executed_lines": [1, 3],
                "summary": {
                    "covered_lines": 2,
                    "num_statements": 4,
                    "percent_covered": 50.0,
                    "percent_covered_display": "50",
                    "missing_lines": 2,
                    "excluded_lines": 0,
                    "num_branches": 3,
                    "num_partial_branches": 1,
                    "covered_branches": 1,
                    "missing_branches": 2,
                },
                "missing_lines": [4, 2],
                "excluded_lines": [],
                "executed_branches": [[1, 2]],
                "missing_branches": [[5, -1], [-3, 4]],
            },
            "src/beta.py": {
                "executed_lines": [1, 2],
                "summary": {
                    "covered_lines": 2,
                    "num_statements": 3,
                    "percent_covered": 50.0,
                    "percent_covered_display": "50",
                    "missing_lines": 1,
                    "excluded_lines": 0,
                    "num_branches": 1,
                    "num_partial_branches": 0,
                    "covered_branches": 0,
                    "missing_branches": 1,
                },
                "missing_lines": [3],
                "excluded_lines": [],
                "executed_branches": [],
                "missing_branches": [[7, 8]],
            },
            "src/gamma.py": {
                "executed_lines": [],
                "summary": {
                    "covered_lines": 0,
                    "num_statements": 2,
                    "percent_covered": 0.0,
                    "percent_covered_display": "0",
                    "missing_lines": 2,
                    "excluded_lines": 0,
                    "num_branches": 0,
                    "num_partial_branches": 0,
                    "covered_branches": 0,
                    "missing_branches": 0,
                },
                "missing_lines": [2, 1],
                "excluded_lines": [],
                "executed_branches": [],
                "missing_branches": [],
            },
            "src/zero.py": {
                "executed_lines": [1],
                "summary": {
                    "covered_lines": 1,
                    "num_statements": 1,
                    "percent_covered": 100.0,
                    "percent_covered_display": "100",
                    "missing_lines": 0,
                    "excluded_lines": 0,
                    "num_branches": 0,
                    "num_partial_branches": 0,
                    "covered_branches": 0,
                    "missing_branches": 0,
                },
                "missing_lines": [],
                "excluded_lines": [],
                "executed_branches": [],
                "missing_branches": [],
            },
        },
        "totals": {
            "covered_lines": 5,
            "num_statements": 10,
            "percent_covered": 50.0,
            "percent_covered_display": "50",
            "missing_lines": 5,
            "excluded_lines": 0,
            "num_branches": 4,
            "num_partial_branches": 1,
            "covered_branches": 1,
            "missing_branches": 3,
        },
    }


def _coverage_json_bytes(document: object) -> bytes:
    return json.dumps(document, separators=(",", ":")).encode()


def test_coverage_json_builds_exact_sorted_gaps_and_file_guidance(tmp_path: Path) -> None:
    root = _coverage_project(tmp_path)

    totals, files = validate_coverage_json(
        _coverage_json_bytes(_coverage_json_document()),
        project_root=root,
        coverage_version="7.15.2",
    )

    assert totals == CoverageTotals(CoverageCounts(5, 5), CoverageCounts(1, 3))
    assert files == (
        CoverageFile(
            "src/alpha.py",
            FileStatementCoverage(2, 2, (2, 4)),
            FileBranchCoverage(1, 2, ((-3, 4), (5, -1))),
        ),
        CoverageFile(
            "src/beta.py",
            FileStatementCoverage(2, 1, (3,)),
            FileBranchCoverage(0, 1, ((7, 8),)),
        ),
        CoverageFile(
            "src/gamma.py",
            FileStatementCoverage(0, 2, (1, 2)),
            FileBranchCoverage(0, 0, ()),
        ),
        CoverageFile(
            "src/zero.py",
            FileStatementCoverage(1, 0, ()),
            FileBranchCoverage(0, 0, ()),
        ),
    )


@pytest.mark.parametrize(
    ("location", "member"),
    (
        ("root", "future_root"),
        ("meta", "future_meta"),
        ("file", "future_file"),
        ("summary", "future_summary"),
        ("totals", "future_totals"),
    ),
)
def test_coverage_json_ignores_one_unknown_additive_member(
    tmp_path: Path, location: str, member: str
) -> None:
    root = _coverage_project(tmp_path)
    document = _coverage_json_document()
    target: dict[str, Any]
    if location == "root":
        target = document
    elif location == "meta":
        target = document["meta"]
    elif location == "file":
        target = document["files"]["src/alpha.py"]
    elif location == "summary":
        target = document["files"]["src/alpha.py"]["summary"]
    else:
        target = document["totals"]
    target[member] = {"future": [1, 2, 3]}

    totals, files = validate_coverage_json(
        _coverage_json_bytes(document),
        project_root=root,
        coverage_version="7.15.2",
    )

    assert totals.statements == CoverageCounts(5, 5)
    assert files[0].path == "src/alpha.py"


@pytest.mark.parametrize("path_form", ("absolute", "dotdot", "internal_symlink"))
def test_coverage_json_accepts_paths_that_resolve_to_the_measured_file(
    tmp_path: Path, path_form: str
) -> None:
    root = _coverage_project(tmp_path)
    document = _coverage_json_document()
    alpha = document["files"].pop("src/alpha.py")
    if path_form == "absolute":
        key = str((root / "src/alpha.py").resolve())
    elif path_form == "dotdot":
        key = "src/../src/alpha.py"
    else:
        (root / "alias.py").symlink_to(root / "src/alpha.py")
        key = "alias.py"
    document["files"][key] = alpha

    _totals, files = validate_coverage_json(
        _coverage_json_bytes(document),
        project_root=root,
        coverage_version="7.15.2",
    )

    assert files[0].path == "src/alpha.py"


@pytest.mark.parametrize("alias_kind", ("hardlink", "case_alias"))
def test_coverage_json_rejects_distinct_keys_for_the_same_measured_file(
    tmp_path: Path, alias_kind: str
) -> None:
    """Different in-root names for one inode must not double-count evidence."""
    root = _coverage_project(tmp_path)
    document = _coverage_json_document()
    alias = root / "src" / "zero-alias.py"
    if alias_kind == "hardlink":
        os.link(root / "src" / "zero.py", alias)
    else:
        alias = root / "src" / "ZERO.py"
        if not alias.exists() or not os.path.samefile(alias, root / "src" / "zero.py"):
            pytest.skip()

    document["files"][str(alias.relative_to(root))] = deepcopy(
        document["files"]["src/zero.py"]
    )
    document["totals"]["covered_lines"] += 1
    document["totals"]["num_statements"] += 1

    with pytest.raises(ValueError, match="duplicate measured file"):
        validate_coverage_json(
            _coverage_json_bytes(document),
            project_root=root,
            coverage_version="7.15.2",
        )


def test_coverage_json_rejects_a_valid_utf16_document(tmp_path: Path) -> None:
    root = _coverage_project(tmp_path)

    with pytest.raises(UnicodeDecodeError):
        validate_coverage_json(
            json.dumps(_coverage_json_document(), separators=(",", ":")).encode("utf-16"),
            project_root=root,
            coverage_version="7.15.2",
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "root_not_object",
        "meta_missing",
        "meta_not_object",
        "files_not_object",
        "totals_not_object",
        "format_bool",
        "format_wrong",
        "version_not_string",
        "version_mismatch",
        "branch_coverage_false",
        "file_name_empty",
        "file_record_not_object",
        "summary_not_object",
        "summary_field_missing",
        "count_bool",
        "count_float",
        "count_negative",
        "missing_lines_not_array",
        "missing_line_bool",
        "missing_line_zero",
        "missing_line_negative",
        "duplicate_line",
        "missing_branches_not_array",
        "branch_arc_not_pair",
        "branch_endpoint_bool",
        "branch_endpoint_zero",
        "duplicate_arc",
        "statement_arithmetic",
        "statement_gap_count",
        "branch_arithmetic",
        "branch_gap_count",
        "totals_arithmetic",
        "totals_disagree",
        "unknown_nonfinite",
    ),
)
def test_coverage_json_rejects_one_schema_or_arithmetic_defect(
    tmp_path: Path, mutation: str
) -> None:
    root = _coverage_project(tmp_path)
    document: Any = _coverage_json_document()
    if mutation == "root_not_object":
        document = []
    elif mutation == "meta_missing":
        del document["meta"]
    elif mutation == "meta_not_object":
        document["meta"] = []
    elif mutation == "files_not_object":
        document["files"] = []
    elif mutation == "totals_not_object":
        document["totals"] = []
    elif mutation == "format_bool":
        document["meta"]["format"] = True
    elif mutation == "format_wrong":
        document["meta"]["format"] = 2
    elif mutation == "version_not_string":
        document["meta"]["version"] = 7
    elif mutation == "version_mismatch":
        document["meta"]["version"] = "7.15.1"
    elif mutation == "branch_coverage_false":
        document["meta"]["branch_coverage"] = False
    elif mutation == "file_name_empty":
        document["files"][""] = document["files"].pop("src/alpha.py")
    elif mutation == "file_record_not_object":
        document["files"]["src/alpha.py"] = []
    elif mutation == "summary_not_object":
        document["files"]["src/alpha.py"]["summary"] = []
    elif mutation == "summary_field_missing":
        del document["files"]["src/alpha.py"]["summary"]["covered_lines"]
    elif mutation == "count_bool":
        document["files"]["src/alpha.py"]["summary"]["covered_lines"] = True
    elif mutation == "count_float":
        document["files"]["src/alpha.py"]["summary"]["covered_lines"] = 2.0
    elif mutation == "count_negative":
        document["files"]["src/alpha.py"]["summary"]["covered_lines"] = -1
    elif mutation == "missing_lines_not_array":
        document["files"]["src/alpha.py"]["missing_lines"] = {}
    elif mutation == "missing_line_bool":
        document["files"]["src/alpha.py"]["missing_lines"][0] = True
    elif mutation == "missing_line_zero":
        document["files"]["src/alpha.py"]["missing_lines"][0] = 0
    elif mutation == "missing_line_negative":
        document["files"]["src/alpha.py"]["missing_lines"][0] = -1
    elif mutation == "duplicate_line":
        document["files"]["src/alpha.py"]["missing_lines"] = [2, 2]
    elif mutation == "missing_branches_not_array":
        document["files"]["src/alpha.py"]["missing_branches"] = {}
    elif mutation == "branch_arc_not_pair":
        document["files"]["src/alpha.py"]["missing_branches"][0] = [5]
    elif mutation == "branch_endpoint_bool":
        document["files"]["src/alpha.py"]["missing_branches"][0][0] = True
    elif mutation == "branch_endpoint_zero":
        document["files"]["src/alpha.py"]["missing_branches"][0][0] = 0
    elif mutation == "duplicate_arc":
        document["files"]["src/alpha.py"]["missing_branches"] = [[5, -1], [5, -1]]
    elif mutation == "statement_arithmetic":
        document["files"]["src/alpha.py"]["summary"]["num_statements"] = 5
    elif mutation == "statement_gap_count":
        document["files"]["src/alpha.py"]["summary"]["missing_lines"] = 1
    elif mutation == "branch_arithmetic":
        document["files"]["src/alpha.py"]["summary"]["num_branches"] = 4
    elif mutation == "branch_gap_count":
        document["files"]["src/alpha.py"]["summary"]["missing_branches"] = 1
    elif mutation == "totals_arithmetic":
        document["totals"]["num_statements"] = 11
    elif mutation == "totals_disagree":
        document["totals"]["covered_lines"] = 4
    else:
        document["meta"]["future"] = math.nan

    with pytest.raises(ValueError):
        validate_coverage_json(
            _coverage_json_bytes(document),
            project_root=root,
            coverage_version="7.15.2",
        )


@pytest.mark.parametrize(
    "path_defect",
    ("outside", "symlink_escape", "missing", "directory", "duplicate_normalized"),
)
def test_coverage_json_rejects_one_measured_path_defect(
    tmp_path: Path, path_defect: str
) -> None:
    root = _coverage_project(tmp_path)
    document = _coverage_json_document()
    if path_defect == "outside":
        outside = tmp_path / "outside.py"
        outside.write_text("# outside\n")
        document["files"][str(outside)] = document["files"].pop("src/alpha.py")
    elif path_defect == "symlink_escape":
        outside = tmp_path / "outside.py"
        outside.write_text("# outside\n")
        (root / "escape.py").symlink_to(outside)
        document["files"]["escape.py"] = document["files"].pop("src/alpha.py")
    elif path_defect == "missing":
        document["files"]["missing.py"] = document["files"].pop("src/alpha.py")
    elif path_defect == "directory":
        document["files"]["src"] = document["files"].pop("src/alpha.py")
    else:
        document["files"]["src/../src/alpha.py"] = deepcopy(
            document["files"]["src/alpha.py"]
        )

    with pytest.raises(ValueError):
        validate_coverage_json(
            _coverage_json_bytes(document),
            project_root=root,
            coverage_version="7.15.2",
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX permits literal backslashes and drive-like names")
@pytest.mark.parametrize(
    ("raw_path", "path_parts"),
    (
        ("src\\alpha.py", ("src\\alpha.py",)),
        ("C:/alpha.py", ("C:", "alpha.py")),
    ),
    ids=("literal-backslash", "drive-like-prefix"),
)
def test_coverage_json_rejects_non_posix_measured_paths_before_result_construction(
    tmp_path: Path,
    raw_path: str,
    path_parts: tuple[str, ...],
) -> None:
    root = _coverage_project(tmp_path)
    measured = root.joinpath(*path_parts)
    measured.parent.mkdir(parents=True, exist_ok=True)
    measured.write_text("# literal POSIX name\n")
    document = _coverage_json_document()
    document["files"][raw_path] = document["files"].pop("src/alpha.py")

    with pytest.raises(ValueError, match="project-relative POSIX text"):
        validate_coverage_json(
            _coverage_json_bytes(document),
            project_root=root,
            coverage_version="7.15.2",
        )


@pytest.mark.parametrize(
    "content",
    (
        b"\xff",
        b'{"meta":',
        (b'{"future":' + b"[" * 65 + b"0" + b"]" * 65 + b"}"),
    ),
    ids=("invalid-utf8", "malformed-json", "excessive-nesting"),
)
def test_coverage_json_rejects_malformed_or_excessively_nested_bytes(
    tmp_path: Path, content: bytes
) -> None:
    root = _coverage_project(tmp_path)

    with pytest.raises((UnicodeDecodeError, ValueError)):
        validate_coverage_json(
            content,
            project_root=root,
            coverage_version="7.15.2",
        )


def _coverage_observation(
    *,
    preflight: CoveragePreflightClassification = "supported",
    artifact: CoverageArtifactState = "snapshot",
    content: bytes | None = None,
    diagnostic: str | None = None,
    version: str | None = "7.15.2",
    json_exit_code: int | None = 0,
) -> CoverageExecutionObservation:
    if preflight == "supported":
        record = CoveragePreflightRecord((3, 13, 15), True, version)
    elif preflight == "module_unavailable":
        record = CoveragePreflightRecord((3, 13, 15), False, None)
    elif preflight in {"unsupported_python", "unsupported_version"}:
        record = CoveragePreflightRecord((3, 13, 14), True, version)
    else:
        record = None
    return CoverageExecutionObservation(
        preflight=CoveragePreflightObservation(preflight, record, diagnostic),
        artifact=CoverageArtifactObservation(
            artifact,
            _coverage_json_bytes(_coverage_json_document()) if content is None else content,
            diagnostic,
        ),
        json_exit_code=json_exit_code,
    )


def _plan_without_coverage(scope: str) -> RunPlan:
    plan = _plan()
    check = plan.checks[0]
    assert check.pytest is not None
    return replace(
        plan,
        checks=(replace(check, pytest=replace(check.pytest, coverage=None)),),
        planned_coverage_scope=scope,
    )


@pytest.mark.parametrize("scope", ("not_requested", "unavailable"))
def test_coverage_result_is_absent_only_when_coverage_was_not_planned(
    tmp_path: Path, scope: str
) -> None:
    root = _coverage_project(tmp_path)

    result = build_coverage_result(
        root,
        _plan_without_coverage(scope),
        _pytest_result(),
        None,
    )

    assert result is None


def test_coverage_result_builds_strict_configured_threshold_pass(tmp_path: Path) -> None:
    root = _coverage_project(tmp_path)

    result = build_coverage_result(
        root,
        _plan(),
        _pytest_result(),
        _coverage_observation(),
    )

    assert result is not None
    assert result == CoverageResult(
        status="passed",
        scope="complete",
        evidence_complete=True,
        coverage_version="7.15.2",
        gate_eligible=True,
        threshold=CoverageThreshold(True, 90, True, True, None),
        totals=CoverageTotals(CoverageCounts(5, 5), CoverageCounts(1, 3)),
        files=(
            CoverageFile(
                "src/alpha.py",
                FileStatementCoverage(2, 2, (2, 4)),
                FileBranchCoverage(1, 2, ((-3, 4), (5, -1))),
            ),
            CoverageFile(
                "src/beta.py",
                FileStatementCoverage(2, 1, (3,)),
                FileBranchCoverage(0, 1, ((7, 8),)),
            ),
            CoverageFile(
                "src/gamma.py",
                FileStatementCoverage(0, 2, (1, 2)),
                FileBranchCoverage(0, 0, ()),
            ),
            CoverageFile(
                "src/zero.py",
                FileStatementCoverage(1, 0, ()),
                FileBranchCoverage(0, 0, ()),
            ),
        ),
        error=None,
    )
    assert tuple(file.path for file in result.files) == (
        "src/alpha.py",
        "src/beta.py",
        "src/gamma.py",
        "src/zero.py",
    )
    validate_coverage_result(result)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda result: replace(result, coverage_version=1),
        lambda result: replace(result, coverage_version=None),
        lambda result: replace(result, coverage_version=""),
        lambda result: replace(result, coverage_version="7.15.2-dev"),
        lambda result: replace(result, evidence_complete=1),
        lambda result: replace(result, gate_eligible=0),
        lambda result: replace(result, totals=CoverageTotals(CoverageCounts(True, 5), result.totals.branches)),
        lambda result: replace(result, totals=CoverageTotals(CoverageCounts(6, 5), result.totals.branches)),
        lambda result: replace(result, threshold=replace(result.threshold, value=float("nan"))),
        lambda result: replace(result, threshold=replace(result.threshold, passed=False)),
        lambda result: replace(result, files=tuple(reversed(result.files))),
        lambda result: replace(
            result, files=(replace(result.files[0], path="src/../alpha.py"), *result.files[1:])
        ),
        lambda result: replace(
            result, files=(replace(result.files[0], path="src//alpha.py"), *result.files[1:])
        ),
        lambda result: replace(
            result, files=(replace(result.files[0], path="src/"), *result.files[1:])
        ),
        lambda result: replace(
            result, files=(replace(result.files[0], path="src/\x00alpha.py"), *result.files[1:])
        ),
        lambda result: replace(
            result, files=(replace(result.files[0], path="C:/alpha.py"), *result.files[1:])
        ),
        lambda result: replace(
            result,
            files=(
                replace(
                    result.files[0],
                    statements=replace(result.files[0].statements, missing_lines=(4, 2)),
                ),
                *result.files[1:],
            ),
        ),
    ),
    ids=(
        "version-type",
        "missing-version",
        "empty-version",
        "unstable-version",
        "evidence-bool",
        "eligible-bool",
        "count-bool",
        "totals-do-not-match-files",
        "threshold-nan",
        "evaluated-passed",
        "file-order",
        "noncanonical-path",
        "double-separator-path",
        "trailing-separator-path",
        "nul-path",
        "windows-absolute-path",
        "missing-lines-order",
    ),
)
def test_public_coverage_model_rejects_noncanonical_values(
    tmp_path: Path,
    mutate: Callable[[CoverageResult], CoverageResult],
) -> None:
    root = _coverage_project(tmp_path)
    result = build_coverage_result(root, _plan(), _pytest_result(), _coverage_observation())
    assert result is not None

    with pytest.raises(ValueError):
        validate_coverage_result(mutate(result))


def test_coverage_result_builds_native_threshold_failure_from_exit_two(
    tmp_path: Path,
) -> None:
    root = _coverage_project(tmp_path)

    result = build_coverage_result(
        root,
        _plan(),
        _pytest_result(),
        _coverage_observation(json_exit_code=2),
    )

    assert result is not None
    assert result.status == "failed"
    assert result.gate_eligible is True
    assert result.threshold == CoverageThreshold(True, 90, True, False, None)
    assert result.error is None


def test_coverage_result_builds_strict_unconfigured_pass_without_evaluation(
    tmp_path: Path,
) -> None:
    root = _coverage_project(tmp_path)

    result = build_coverage_result(
        root,
        _plan(fail_under=None),
        _pytest_result(),
        _coverage_observation(),
    )

    assert result is not None
    assert result.status == "passed"
    assert result.gate_eligible is True
    assert result.threshold == CoverageThreshold(
        False, None, False, None, "not_configured"
    )
    validate_coverage_result(result)


@pytest.mark.parametrize(
    ("plan", "expected_scope"),
    (
        (_plan(mode="focused", fail_under=None), "complete"),
        (_plan(targets=("tests/test_alpha.py",), fail_under=None), "partial"),
    ),
    ids=("focused", "direct-target"),
)
def test_coverage_result_validates_unconfigured_guidance_from_builder(
    tmp_path: Path,
    plan: RunPlan,
    expected_scope: str,
) -> None:
    root = _coverage_project(tmp_path)

    result = build_coverage_result(root, plan, _pytest_result(), _coverage_observation())

    assert result is not None
    assert result.status == "guidance"
    assert result.scope == expected_scope
    assert result.threshold == CoverageThreshold(False, None, False, None, "not_configured")
    assert validate_coverage_result(result) is None


@pytest.mark.parametrize(
    ("plan", "pytest_result", "expected_scope", "expected_reason"),
    (
        (_plan(mode="focused"), _pytest_result(), "complete", "focused_run"),
        (
            _plan(mode="focused", targets=("src",)),
            _pytest_result(),
            "partial",
            "partial_run",
        ),
        (
            _plan(mode="focused", shortcut="unit"),
            _pytest_result(),
            "partial",
            "partial_run",
        ),
        (_plan(), _pytest_result(exit_code=1), "complete", "pytest_failed"),
        (
            _plan(),
            _pytest_result(
                exit_code=2,
                complete=False,
                scope="partial",
                scope_reasons=("incomplete_session",),
            ),
            "partial",
            "pytest_incomplete",
        ),
        (
            _plan(),
            _pytest_result(
                exit_code=3,
                complete=False,
                scope="partial",
                scope_reasons=("incomplete_session",),
            ),
            "partial",
            "pytest_incomplete",
        ),
        (
            _plan(),
            _pytest_result(
                exit_code=4,
                complete=False,
                scope="partial",
                scope_reasons=("incomplete_session",),
            ),
            "partial",
            "pytest_incomplete",
        ),
        (_plan(), _pytest_result(exit_code=5), "complete", "no_tests_collected"),
        (
            _plan(),
            _pytest_result(
                scope="partial",
                scope_reasons=("effective_narrowing_option",),
            ),
            "partial",
            "partial_run",
        ),
    ),
    ids=(
        "focused-complete",
        "direct-target",
        "shortcut",
        "pytest-exit-one",
        "pytest-exit-two",
        "pytest-exit-three",
        "pytest-exit-four",
        "pytest-exit-five",
        "external-narrowing",
    ),
)
def test_coverage_result_builds_valid_non_gating_guidance(
    tmp_path: Path,
    plan: RunPlan,
    pytest_result: PytestResult,
    expected_scope: str,
    expected_reason: CoverageThresholdSkipReason,
) -> None:
    root = _coverage_project(tmp_path)

    result = build_coverage_result(root, plan, pytest_result, _coverage_observation())

    assert result is not None
    assert result.status == "guidance"
    assert result.scope == expected_scope
    assert result.evidence_complete is True
    assert result.gate_eligible is False
    assert result.threshold == CoverageThreshold(
        True, 90, False, None, expected_reason
    )
    assert result.totals is not None
    assert result.error is None


@pytest.mark.parametrize(
    ("preflight", "artifact", "expected_code"),
    (
        ("unsupported_python", "not_attempted", "unsupported_python"),
        ("module_unavailable", "not_attempted", "module_unavailable"),
        ("unsupported_version", "not_attempted", "unsupported_version"),
        ("preflight_invalid", "not_attempted", "preflight_invalid"),
        ("spawn_failed", "not_attempted", "spawn_failed"),
        ("terminated_by_signal", "not_attempted", "terminated_by_signal"),
        ("supported", "spawn_failed", "spawn_failed"),
        ("supported", "terminated_by_signal", "terminated_by_signal"),
        ("supported", "unsupported_parallelism", "unsupported_parallelism"),
        ("supported", "unexpected_parallel_data", "unexpected_parallel_data"),
        ("supported", "data_missing", "data_missing"),
        ("supported", "not_attempted", "data_missing"),
        ("supported", "generation_failed", "generation_failed"),
        ("supported", "artifact_missing", "artifact_missing"),
        ("supported", "artifact_invalid", "artifact_invalid"),
    ),
)
def test_coverage_result_maps_every_preflight_and_artifact_error(
    tmp_path: Path,
    preflight: CoveragePreflightClassification,
    artifact: CoverageArtifactState,
    expected_code: CoverageErrorCode,
) -> None:
    root = _coverage_project(tmp_path)

    result = build_coverage_result(
        root,
        _plan(),
        _pytest_result(),
        _coverage_observation(
            preflight=preflight,
            artifact=artifact,
            diagnostic=f"{expected_code} diagnostic",
            version="7.14.9" if preflight == "unsupported_version" else "7.15.2",
        ),
    )

    assert result is not None
    assert result.status == "error"
    assert result.scope == "partial"
    assert result.evidence_complete is False
    assert result.gate_eligible is False
    assert result.threshold == CoverageThreshold(
        True, 90, False, None, "evidence_error"
    )
    assert result.totals is None
    assert result.files == ()
    assert result.error == CoverageError(expected_code, f"{expected_code} diagnostic")
    assert result.coverage_version == (
        "7.14.9"
        if preflight == "unsupported_version"
        else "7.15.2"
        if preflight == "supported"
        else None
    )
    validate_coverage_result(result)


@pytest.mark.parametrize(
    "version",
    ("7.15.2rc1", "7.15.2.dev0", "7.15.2.post1", "7.15.2+local"),
)
def test_coverage_result_keeps_untrusted_unsupported_version_as_unsupported(
    tmp_path: Path,
    version: str,
) -> None:
    root = _coverage_project(tmp_path)

    result = build_coverage_result(
        root,
        _plan(),
        _pytest_result(),
        _coverage_observation(
            preflight="unsupported_version",
            artifact="not_attempted",
            version=version,
            diagnostic="unsupported coverage version",
        ),
    )

    assert result is not None
    assert result.error == CoverageError("unsupported_version", "unsupported coverage version")
    assert result.coverage_version is None
    validate_coverage_result(result)


def test_coverage_result_retains_only_stable_out_of_range_unsupported_version(
    tmp_path: Path,
) -> None:
    root = _coverage_project(tmp_path)
    result = build_coverage_result(
        root,
        _plan(),
        _pytest_result(),
        _coverage_observation(
            preflight="unsupported_version",
            artifact="not_attempted",
            version="7.14.9",
            diagnostic="unsupported coverage version",
        ),
    )

    assert result is not None
    assert result.error is not None
    assert result.error.code == "unsupported_version"
    assert result.coverage_version == "7.14.9"
    validate_coverage_result(result)

    with pytest.raises(ValueError):
        validate_coverage_result(replace(result, coverage_version="7.15.2"))


def test_coverage_result_retains_a_giant_stable_unsupported_version(tmp_path: Path) -> None:
    root = _coverage_project(tmp_path)
    version = f"{'9' * 10_000}.0.0"

    assert is_supported_coverage_version(version) is False
    result = build_coverage_result(
        root,
        _plan(),
        _pytest_result(),
        _coverage_observation(
            preflight="unsupported_version",
            artifact="not_attempted",
            version=version,
            diagnostic="unsupported coverage version",
        ),
    )

    assert result is not None
    assert result.error == CoverageError("unsupported_version", "unsupported coverage version")
    assert result.coverage_version == version
    validate_coverage_result(result)


@pytest.mark.parametrize("version", ("7.14.9", "8.0.0", "0.0"))
def test_public_coverage_model_rejects_non_supported_success_version(
    tmp_path: Path,
    version: str,
) -> None:
    root = _coverage_project(tmp_path)
    result = build_coverage_result(root, _plan(), _pytest_result(), _coverage_observation())

    assert result is not None
    with pytest.raises(ValueError):
        validate_coverage_result(replace(result, coverage_version=version))


@pytest.mark.parametrize("version", (None, "7.14.9", "8.0.0"))
def test_public_coverage_model_requires_supported_version_after_supported_preflight(
    tmp_path: Path,
    version: str | None,
) -> None:
    root = _coverage_project(tmp_path)
    result = build_coverage_result(
        root,
        _plan(),
        _pytest_result(),
        _coverage_observation(artifact="data_missing", diagnostic="coverage data missing"),
    )

    assert result is not None
    assert result.error is not None
    assert result.error.code == "data_missing"
    with pytest.raises(ValueError):
        validate_coverage_result(replace(result, coverage_version=version))


@pytest.mark.parametrize(
    ("earlier", "later"),
    (
        ("unsupported_python", "unsupported_parallelism"),
        ("module_unavailable", "unexpected_parallel_data"),
        ("unsupported_version", "data_missing"),
        ("preflight_invalid", "generation_failed"),
        ("spawn_failed", "artifact_missing"),
        ("terminated_by_signal", "artifact_invalid"),
    ),
)
def test_coverage_result_preflight_error_precedes_every_later_artifact_error(
    tmp_path: Path,
    earlier: CoveragePreflightErrorClassification,
    later: CoverageArtifactState,
) -> None:
    root = _coverage_project(tmp_path)

    result = build_coverage_result(
        root,
        _plan(),
        _pytest_result(),
        _coverage_observation(
            preflight=earlier,
            artifact=later,
            diagnostic=f"{earlier} wins",
        ),
    )

    assert result is not None
    assert result.error == CoverageError(earlier, f"{earlier} wins")


@pytest.mark.parametrize(
    ("plan", "exit_code"),
    (
        (_plan(), 1),
        (_plan(mode="focused"), 2),
        (_plan(fail_under=None), 2),
        (_plan(), 3),
    ),
)
def test_coverage_result_rejects_non_threshold_json_exit(
    tmp_path: Path, plan: RunPlan, exit_code: int
) -> None:
    root = _coverage_project(tmp_path)

    result = build_coverage_result(
        root,
        plan,
        _pytest_result(),
        _coverage_observation(json_exit_code=exit_code),
    )

    assert result is not None
    assert result.error is not None
    assert result.error.code == "generation_failed"


@pytest.mark.parametrize("content", (None, b'{"meta":'))
def test_coverage_result_ordinary_json_exit_precedes_missing_or_invalid_snapshot(
    tmp_path: Path, content: bytes | None
) -> None:
    root = _coverage_project(tmp_path)
    observation = _coverage_observation(content=content or b'{"meta":')
    if content is None:
        observation = replace(
            observation,
            artifact=replace(observation.artifact, content=None),
        )

    result = build_coverage_result(
        root,
        _plan(),
        _pytest_result(),
        replace(observation, json_exit_code=1),
    )

    assert result is not None
    assert result.error == CoverageError(
        "generation_failed", "coverage JSON generation exited with code 1"
    )


def test_coverage_result_eligible_exit_two_still_requires_snapshot_content(
    tmp_path: Path,
) -> None:
    root = _coverage_project(tmp_path)
    observation = _coverage_observation()
    observation = replace(
        observation,
        artifact=replace(observation.artifact, content=None),
        json_exit_code=2,
    )

    result = build_coverage_result(root, _plan(), _pytest_result(), observation)

    assert result is not None
    assert result.error == CoverageError(
        "artifact_invalid", "coverage JSON snapshot has no content"
    )


def test_coverage_result_treats_invalid_exit_two_json_as_artifact_invalid(
    tmp_path: Path,
) -> None:
    root = _coverage_project(tmp_path)
    document = _coverage_json_document()
    document["meta"]["format"] = 2

    result = build_coverage_result(
        root,
        _plan(),
        _pytest_result(),
        _coverage_observation(
            content=_coverage_json_bytes(document),
            json_exit_code=2,
        ),
    )

    assert result is not None
    assert result.error is not None
    assert result.error.code == "artifact_invalid"


def test_coverage_result_treats_missing_snapshot_content_as_artifact_invalid(
    tmp_path: Path,
) -> None:
    root = _coverage_project(tmp_path)
    observation = _coverage_observation()
    observation = replace(
        observation,
        artifact=replace(observation.artifact, content=None),
    )

    result = build_coverage_result(root, _plan(), _pytest_result(), observation)

    assert result is not None
    assert result.error is not None
    assert result.error.code == "artifact_invalid"


def test_coverage_result_retains_trusted_version_on_later_error(tmp_path: Path) -> None:
    root = _coverage_project(tmp_path)

    result = build_coverage_result(
        root,
        _plan(),
        _pytest_result(),
        _coverage_observation(artifact="data_missing", diagnostic="no base data"),
    )

    assert result is not None
    assert result.coverage_version == "7.15.2"
    assert result.error == CoverageError("data_missing", "no base data")


def test_coverage_result_keeps_pytest_and_coverage_preflight_failures_independent(
    tmp_path: Path,
) -> None:
    root = _coverage_project(tmp_path)
    pytest_result = replace(
        _pytest_result(),
        status="error",
        complete=False,
        scope="partial",
        scope_reasons=("incomplete_session",),
        evidence=None,
    )

    data_missing = build_coverage_result(
        root,
        _plan(),
        pytest_result,
        _coverage_observation(artifact="data_missing", diagnostic="primary did not run"),
    )
    coverage_preflight_failed = build_coverage_result(
        root,
        _plan(),
        pytest_result,
        _coverage_observation(
            preflight="module_unavailable",
            artifact="not_attempted",
            diagnostic="coverage unavailable",
        ),
    )

    assert data_missing is not None and data_missing.error is not None
    assert data_missing.error.code == "data_missing"
    assert coverage_preflight_failed is not None
    assert coverage_preflight_failed.error is not None
    assert coverage_preflight_failed.error.code == "module_unavailable"


def test_coverage_result_maps_setup_before_coverage_preflight_to_preflight_invalid(
    tmp_path: Path,
) -> None:
    root = _coverage_project(tmp_path)

    result = build_coverage_result(root, _plan(), _pytest_result(), None)

    assert result is not None
    assert result.error is not None
    assert result.error.code == "preflight_invalid"


@pytest.mark.parametrize(
    ("plan", "pytest_result", "expected_eligible", "expected_reason"),
    (
        (_plan(), _pytest_result(), True, None),
        (_plan(fail_under=None), _pytest_result(), True, "not_configured"),
        (_plan(mode="focused"), _pytest_result(), False, "focused_run"),
        (
            _plan(mode="focused", targets=("src",)),
            _pytest_result(),
            False,
            "partial_run",
        ),
        (
            _plan(mode="focused", shortcut="unit"),
            _pytest_result(),
            False,
            "partial_run",
        ),
        (
            _plan(),
            _pytest_result(scope="partial", scope_reasons=("deselected_tests",)),
            False,
            "partial_run",
        ),
        (_plan(), _pytest_result(exit_code=1), False, "pytest_failed"),
        (
            _plan(),
            _pytest_result(
                exit_code=2,
                complete=False,
                scope="partial",
                scope_reasons=("incomplete_session",),
            ),
            False,
            "pytest_incomplete",
        ),
        (
            _plan(),
            _pytest_result(
                exit_code=3,
                complete=False,
                scope="partial",
                scope_reasons=("incomplete_session",),
            ),
            False,
            "pytest_incomplete",
        ),
        (
            _plan(),
            _pytest_result(
                exit_code=4,
                complete=False,
                scope="partial",
                scope_reasons=("incomplete_session",),
            ),
            False,
            "pytest_incomplete",
        ),
        (_plan(), _pytest_result(exit_code=5), False, "no_tests_collected"),
        (_plan(), replace(_pytest_result(), evidence=None), False, "evidence_error"),
    ),
)
def test_coverage_result_agrees_with_every_gate_policy_row(
    tmp_path: Path,
    plan: RunPlan,
    pytest_result: PytestResult,
    expected_eligible: bool,
    expected_reason: CoverageThresholdSkipReason | None,
) -> None:
    root = _coverage_project(tmp_path)

    result = build_coverage_result(root, plan, pytest_result, _coverage_observation())

    assert result is not None
    assert result.gate_eligible is expected_eligible
    assert result.threshold.skipped_reason == expected_reason
    if expected_reason == "evidence_error":
        assert result.status == "error"
        assert result.evidence_complete is False
    else:
        assert result.evidence_complete is True
