from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pyrepo_check.config import ProjectConfig


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
PlanningErrorCode = Literal[
    "invalid_arguments",
    "invalid_project_config",
    "invalid_test_shortcut",
    "unknown_check",
    "unknown_test_shortcut",
    "unknown_target",
    "internal_planning_error",
]


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


@dataclass(frozen=True)
class PlanningFacts:
    existing_positionals: frozenset[str]


@dataclass(frozen=True)
class PlannedCheck:
    name: CheckName
    command: tuple[str, ...]
    cwd: Path


@dataclass(frozen=True)
class RunPlan:
    mode: RunMode
    targets: tuple[str, ...]
    checks: tuple[PlannedCheck, ...]
    output_format: OutputFormat = "terminal"
    test_shortcut: str | None = None
    pytest_args: tuple[str, ...] | None = None
    planned_test_scope: PlannedTestScope = "not_selected"


def plan_run(
    request: RunRequest,
    config: ProjectConfig,
    facts: PlanningFacts,
) -> RunPlan:
    requested, targets = _split_positionals(request.positionals)
    shortcut_args = _resolve_test_shortcut(
        request,
        config,
        requested=requested,
        targets=targets,
    )
    if targets and not requested and not request.all_selected:
        missing = tuple(
            target for target in targets if target not in facts.existing_positionals
        )
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
    pytest_selected = any(check.name == "pytest" for check in selected)
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
        mode="strict_aggregate" if strict_all else "focused",
        targets=targets,
        checks=selected,
        output_format=request.output_format,
        test_shortcut=request.test_shortcut if shortcut_args is not None else None,
        pytest_args=planned_pytest_args,
        planned_test_scope=planned_test_scope,
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
    requested = tuple(
        cast(CheckName, token) for token in positionals if token in check_names
    )
    targets = tuple(token for token in positionals if token not in check_names)
    return requested, targets


def build_checks(
    config: ProjectConfig,
    *,
    targets: Sequence[str] = (),
    strict_all: bool = False,
    pytest_args: Sequence[str] | None = None,
) -> dict[str, PlannedCheck]:
    prefix = _uv_python_prefix(config)
    explicit_targets = tuple(targets)
    effective_pytest_args = (
        explicit_targets if pytest_args is None else tuple(pytest_args)
    )
    strict_targets = (".",) if strict_all and not explicit_targets else ()
    ruff_targets = explicit_targets or strict_targets or config.ruff_targets
    bandit_targets = explicit_targets or strict_targets or config.bandit_targets

    return {
        "ruff": PlannedCheck(
            name="ruff",
            command=(*prefix, "ruff", "check", *ruff_targets),
            cwd=config.root,
        ),
        "annotations": PlannedCheck(
            name="annotations",
            command=(
                *prefix,
                "ruff",
                "check",
                *ruff_targets,
                "--select",
                "ANN",
                "--output-format",
                "concise",
            ),
            cwd=config.root,
        ),
        "annotations-fix": PlannedCheck(
            name="annotations-fix",
            command=(
                *prefix,
                "ruff",
                "check",
                *ruff_targets,
                "--select",
                "ANN",
                "--fix",
                "--unsafe-fixes",
            ),
            cwd=config.root,
        ),
        "ty": PlannedCheck(
            name="ty",
            command=(*prefix, "ty", "check", *explicit_targets),
            cwd=config.root,
        ),
        "bandit": PlannedCheck(
            name="bandit",
            command=(
                *prefix,
                "bandit",
                "-c",
                "pyproject.toml",
                *_bandit_target_args(
                    bandit_targets,
                    recursive=not explicit_targets,
                ),
            ),
            cwd=config.root,
        ),
        "pytest": PlannedCheck(
            name="pytest",
            command=(*prefix, "pytest", *effective_pytest_args),
            cwd=config.root,
        ),
    }


def select_checks(
    checks: Mapping[str, PlannedCheck],
    *,
    requested: Sequence[str],
    all_selected: bool,
) -> tuple[PlannedCheck, ...]:
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
            "unknown_target" if all(_is_path_like(token) for token in unknown)
            else "unknown_check"
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


def _uv_python_prefix(config: ProjectConfig) -> tuple[str, ...]:
    if config.frozen:
        return ("uv", "run", "--frozen", "python", "-m")
    return ("uv", "run", "python", "-m")


def _bandit_target_args(
    targets: Sequence[str],
    *,
    recursive: bool,
) -> tuple[str, ...]:
    if recursive:
        return ("-r", *targets)
    return tuple(targets)
