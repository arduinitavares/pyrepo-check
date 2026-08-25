from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pyrepo_check.config import ProjectConfig
from pyrepo_check.execution import ProcessRunner, execute_plan
from pyrepo_check.planning import (
    CHECK_ORDER as CHECK_ORDER,
    SELECTABLE_CHECK_ORDER as SELECTABLE_CHECK_ORDER,
    CheckName,
    PlannedCheck,
    RunPlan,
    build_checks as build_planned_checks,
    select_check_names,
)


@dataclass(frozen=True)
class Check:
    name: str
    command: tuple[str, ...]


def build_checks(
    config: ProjectConfig,
    *,
    targets: Sequence[str] = (),
    strict_all: bool = False,
) -> dict[str, Check]:
    planned = build_planned_checks(
        config,
        targets=targets,
        strict_all=strict_all,
    )
    return {
        name: Check(name=check.name, command=check.command)
        for name, check in planned.items()
    }


def select_checks(
    checks: Mapping[str, Check],
    *,
    requested: Sequence[str],
    all_selected: bool,
) -> tuple[Check, ...]:
    selected_names = select_check_names(
        checks.keys(),
        requested=requested,
        all_selected=all_selected,
    )
    return tuple(checks[name] for name in selected_names)


def run_checks(
    checks: Sequence[Check],
    *,
    cwd: Path,
    runner: ProcessRunner | None = None,
) -> int:
    prepared = tuple(
        PlannedCheck(
            name=cast(CheckName, check.name),
            command=check.command,
            cwd=cwd,
        )
        for check in checks
    )
    plan = RunPlan(mode="focused", targets=(), checks=prepared)
    return execute_plan(plan, runner=runner).exit_code
