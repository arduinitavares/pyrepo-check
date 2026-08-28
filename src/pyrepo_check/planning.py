from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast
import re

from pyrepo_check.config import (
    CoverageConfig,
    ProjectConfig,
    validate_project_target_syntax,
)


CheckName = Literal[
    "ruff",
    "annotations",
    "annotations-fix",
    "ty",
    "bandit",
    "pytest",
]
RunMode = Literal["focused", "strict_aggregate"]
OutputFormat = Literal["terminal", "json"]
PlannedTestScope = Literal["not_selected", "partial", "complete"]
PlannedCoverageScope = Literal["not_requested", "unavailable", "partial", "complete"]
PlanningErrorCode = Literal[
    "invalid_arguments",
    "invalid_project_config",
    "invalid_test_shortcut",
    "unknown_check",
    "unknown_test_shortcut",
    "unknown_target",
    "coverage_configuration_required",
    "unsafe_unlocked_execution",
    "uv_project_required",
    "internal_planning_error",
]


_REPOSITORY_PYTHON_PATTERN = re.compile(r"^3\.(?:10|11|12|13)(?:\.(?:0|[1-9][0-9]*))?$")


class PlanningFailure(ValueError):
    def __init__(
        self,
        code: PlanningErrorCode,
        message: str,
        *,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


CHECK_ORDER: tuple[CheckName, ...] = (
    "ruff",
    "annotations",
    "ty",
    "bandit",
    "pytest",
)
SELECTABLE_CHECK_ORDER: tuple[CheckName, ...] = (*CHECK_ORDER, "annotations-fix")
TARGET_DEFAULT_CHECKS: tuple[CheckName, ...] = (
    "ruff",
    "annotations",
    "ty",
    "bandit",
)


@dataclass(frozen=True)
class RunRequest:
    root: Path
    positionals: tuple[str, ...]
    all_selected: bool
    no_frozen: bool
    output_format: OutputFormat = "terminal"
    test_shortcut: str | None = None
    coverage_requested: bool = False
    repository_python: str | None = None


@dataclass(frozen=True)
class PlanningFacts:
    existing_positionals: frozenset[str]
    pyproject_exists: bool


@dataclass(frozen=True)
class DefaultRepositoryPython:
    kind: Literal["default"] = "default"


@dataclass(frozen=True)
class ExplicitRepositoryPython:
    request: str
    kind: Literal["explicit"] = "explicit"


RepositoryPythonSelection = DefaultRepositoryPython | ExplicitRepositoryPython


@dataclass(frozen=True)
class CoverageExecutionPlan:
    config_path: Path
    fail_under: int | float | None
    artifact_protocol: Literal["coverage_v1"] = "coverage_v1"


@dataclass(frozen=True)
class PytestExecutionPlan:
    pytest_args: tuple[str, ...]
    artifact_protocol: Literal["pytest_v1"] = "pytest_v1"
    coverage: CoverageExecutionPlan | None = None


@dataclass(frozen=True)
class CheckInvocation:
    name: CheckName
    arguments: tuple[str, ...]
    pytest: PytestExecutionPlan | None = None
    targets: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunPlan:
    root: Path
    repository_python: RepositoryPythonSelection
    mode: RunMode
    targets: tuple[str, ...]
    checks: tuple[CheckInvocation, ...]
    output_format: OutputFormat = "terminal"
    test_shortcut: str | None = None
    pytest_args: tuple[str, ...] | None = None
    planned_test_scope: PlannedTestScope = "not_selected"
    planned_coverage_scope: PlannedCoverageScope = "not_requested"
    pyproject_sha256: str | None = None


def plan_run(
    request: RunRequest,
    config: ProjectConfig,
    facts: PlanningFacts,
) -> RunPlan:
    if request.no_frozen:
        raise PlanningFailure(
            "unsafe_unlocked_execution",
            "--no-frozen is incompatible with repository-safe execution.",
            hint="Update uv.lock explicitly, then rerun without --no-frozen.",
        )
    if not facts.pyproject_exists:
        raise PlanningFailure(
            "uv_project_required",
            "Repository root must contain pyproject.toml.",
            hint="Run pyrepo-check from a uv project root or pass --root.",
        )
    repository_python = _select_repository_python(request.repository_python)
    requested, targets = _split_positionals(request.positionals)
    shortcut_args = _resolve_test_shortcut(
        request,
        config,
        requested=requested,
        targets=targets,
    )
    targets = _validate_direct_targets(targets, facts=facts)
    if targets and not requested and not request.all_selected:
        missing = tuple(target for target in targets if target not in facts.existing_positionals)
        if missing:
            names = ", ".join(sorted(missing))
            code: PlanningErrorCode = (
                "unknown_target"
                if all(_is_path_like(token) for token in missing)
                else "unknown_check"
            )
            hint = (
                "Check the target path or select a check name."
                if code == "unknown_target"
                else "Available checks: ruff, annotations, annotations-fix, ty, bandit, pytest"
            )
            raise PlanningFailure(code, f"Unknown check(s): {names}", hint=hint)
        requested = TARGET_DEFAULT_CHECKS

    strict_all = not targets and (request.all_selected or not request.positionals)
    available = build_checks(
        config,
        targets=targets,
        strict_all=strict_all,
        pytest_args=shortcut_args,
    )
    selected = select_checks(
        available,
        requested=requested,
        all_selected=request.all_selected,
    )
    if any(check.name == "annotations-fix" for check in selected) and len(selected) != 1:
        raise PlanningFailure(
            "invalid_arguments",
            "annotations-fix must be selected alone.",
            hint="Run annotations-fix without --all or another check name.",
        )
    pytest_selected = any(check.name == "pytest" for check in selected)
    if request.coverage_requested and not pytest_selected:
        raise PlanningFailure(
            "invalid_arguments",
            "--coverage requires pytest to be selected.",
            hint="Use: pyrepo-check pytest --coverage",
        )
    coverage_enabled = request.coverage_requested or (strict_all and config.coverage is not None)
    if request.coverage_requested and config.coverage is None:
        raise PlanningFailure(
            "coverage_configuration_required",
            "--coverage requires a valid [tool.coverage.run] configuration.",
            hint="Configure native Coverage.py settings in pyproject.toml.",
        )
    elif strict_all and config.coverage is None:
        planned_coverage_scope = "unavailable"
    elif coverage_enabled:
        planned_coverage_scope = "partial" if targets or shortcut_args is not None else "complete"
    else:
        planned_coverage_scope = "not_requested"
    if coverage_enabled and config.coverage is not None:
        selected = _attach_coverage_plan(selected, config.coverage)
    if not pytest_selected:
        planned_pytest_args = None
        planned_test_scope: PlannedTestScope = "not_selected"
    elif shortcut_args is not None:
        planned_pytest_args = shortcut_args
        planned_test_scope = "partial"
    elif targets:
        planned_pytest_args = targets
        planned_test_scope = "partial"
    else:
        planned_pytest_args = ()
        planned_test_scope = "complete"
    return RunPlan(
        root=config.root,
        repository_python=repository_python,
        mode="strict_aggregate" if strict_all else "focused",
        targets=targets,
        checks=selected,
        output_format=request.output_format,
        test_shortcut=request.test_shortcut if shortcut_args is not None else None,
        pytest_args=planned_pytest_args,
        planned_test_scope=planned_test_scope,
        planned_coverage_scope=planned_coverage_scope,
        pyproject_sha256=config.pyproject_sha256,
    )


def _attach_coverage_plan(
    selected: tuple[CheckInvocation, ...], coverage: CoverageConfig
) -> tuple[CheckInvocation, ...]:
    return tuple(
        check
        if check.pytest is None
        else replace(
            check,
            pytest=replace(
                check.pytest,
                coverage=CoverageExecutionPlan(
                    config_path=coverage.config_path,
                    fail_under=coverage.fail_under,
                ),
            ),
        )
        for check in selected
    )


def _resolve_test_shortcut(
    request: RunRequest,
    config: ProjectConfig,
    *,
    requested: tuple[CheckName, ...],
    targets: tuple[str, ...],
) -> tuple[str, ...] | None:
    name = request.test_shortcut
    if name is None:
        return None

    if request.all_selected or targets or set(requested) != {"pytest"}:
        raise PlanningFailure(
            "invalid_arguments",
            "--shortcut requires an explicit pytest-only run with no direct targets or --all.",
            hint="Use: pyrepo-check pytest --shortcut NAME",
        )

    shortcuts = {shortcut.name: shortcut.pytest_args for shortcut in config.test_shortcuts}
    try:
        return shortcuts[name]
    except KeyError as error:
        available = sorted(shortcuts)
        hint = (
            "Available Test Shortcuts: " + ", ".join(available)
            if available
            else "No Test Shortcuts are configured."
        )
        raise PlanningFailure(
            "unknown_test_shortcut",
            f"Unknown Test Shortcut: {name}",
            hint=hint,
        ) from error


def _split_positionals(
    positionals: Sequence[str],
) -> tuple[tuple[CheckName, ...], tuple[str, ...]]:
    check_names = set(SELECTABLE_CHECK_ORDER)
    requested = tuple(cast(CheckName, token) for token in positionals if token in check_names)
    targets = tuple(token for token in positionals if token not in check_names)
    return requested, targets


def _validate_direct_targets(
    targets: tuple[str, ...],
    *,
    facts: PlanningFacts,
) -> tuple[str, ...]:
    invalid_syntax: set[str] = set()
    for target in targets:
        try:
            validate_project_target_syntax(target)
        except ValueError:
            invalid_syntax.add(target)
    missing = tuple(
        target
        for target in targets
        if target in invalid_syntax or target not in facts.existing_positionals
    )
    if missing:
        names = ", ".join(sorted(missing))
        code: PlanningErrorCode = (
            "unknown_target"
            if all(_is_path_like(token) or token in invalid_syntax for token in missing)
            else "unknown_check"
        )
        hint = (
            "Check the target path or select a check name."
            if code == "unknown_target"
            else "Available checks: ruff, annotations, annotations-fix, ty, bandit, pytest"
        )
        raise PlanningFailure(code, f"Unknown check(s): {names}", hint=hint)
    return targets


def build_checks(
    config: ProjectConfig,
    *,
    targets: Sequence[str] = (),
    strict_all: bool = False,
    pytest_args: Sequence[str] | None = None,
) -> dict[str, CheckInvocation]:
    explicit_targets = tuple(targets)
    effective_pytest_args = explicit_targets if pytest_args is None else tuple(pytest_args)
    strict_targets = (".",) if strict_all and not explicit_targets else ()
    ruff_targets = explicit_targets or strict_targets or config.ruff_targets
    bandit_targets = explicit_targets or strict_targets or config.bandit_targets
    pytest = PytestExecutionPlan(
        pytest_args=effective_pytest_args,
    )

    return {
        "ruff": CheckInvocation(
            name="ruff",
            arguments=("check", *ruff_targets),
            targets=ruff_targets,
        ),
        "annotations": CheckInvocation(
            name="annotations",
            arguments=(
                "check",
                *ruff_targets,
                "--select",
                "ANN",
                "--output-format",
                "concise",
            ),
            targets=ruff_targets,
        ),
        "annotations-fix": CheckInvocation(
            name="annotations-fix",
            arguments=(
                "check",
                *ruff_targets,
                "--select",
                "ANN",
                "--fix",
                "--unsafe-fixes",
            ),
            targets=ruff_targets,
        ),
        "ty": CheckInvocation(
            name="ty",
            arguments=("check", *explicit_targets),
        ),
        "bandit": CheckInvocation(
            name="bandit",
            arguments=(
                "-c",
                "pyproject.toml",
                *_bandit_target_args(
                    bandit_targets,
                    recursive=not explicit_targets,
                ),
            ),
        ),
        "pytest": CheckInvocation(
            name="pytest",
            arguments=pytest.pytest_args,
            pytest=pytest,
        ),
    }


def select_checks(
    checks: Mapping[str, CheckInvocation],
    *,
    requested: Sequence[str],
    all_selected: bool,
) -> tuple[CheckInvocation, ...]:
    selected_names = select_check_names(
        checks.keys(),
        requested=requested,
        all_selected=all_selected,
    )
    return tuple(checks[name] for name in selected_names)


def select_check_names(
    available_names: Collection[str],
    *,
    requested: Sequence[str],
    all_selected: bool,
) -> tuple[CheckName, ...]:
    if all_selected or not requested:
        return CHECK_ORDER

    unknown = sorted(set(requested) - set(available_names))
    if unknown:
        names = ", ".join(unknown)
        code: PlanningErrorCode = (
            "unknown_target" if all(_is_path_like(token) for token in unknown) else "unknown_check"
        )
        hint = (
            "Check the target path or select a check name."
            if code == "unknown_target"
            else "Available checks: ruff, annotations, annotations-fix, ty, bandit, pytest"
        )
        raise PlanningFailure(code, f"Unknown check(s): {names}", hint=hint)

    requested_names = set(requested)
    return tuple(name for name in SELECTABLE_CHECK_ORDER if name in requested_names)


def _is_path_like(token: str) -> bool:
    return any(marker in token for marker in ("/", "\\", "::")) or "." in Path(token).name


def _select_repository_python(request: str | None) -> RepositoryPythonSelection:
    if request is None:
        return DefaultRepositoryPython()
    if _REPOSITORY_PYTHON_PATTERN.fullmatch(request) is None:
        raise PlanningFailure(
            "invalid_arguments",
            f"Unsupported Repository Python request: {request}",
            hint="Use 3.10 through 3.13, optionally with an exact patch version.",
        )
    return ExplicitRepositoryPython(request)


def _bandit_target_args(
    targets: Sequence[str],
    *,
    recursive: bool,
) -> tuple[str, ...]:
    if recursive:
        return ("-r", *targets)
    return tuple(targets)
