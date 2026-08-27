from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pyrepo_check.config import ProjectConfig
from pyrepo_check.execution import CHECK_MODULES, ProcessRunner, execute_legacy_commands
from pyrepo_check.planning import (
    CHECK_ORDER as CHECK_ORDER,
    SELECTABLE_CHECK_ORDER as SELECTABLE_CHECK_ORDER,
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
        name: Check(
            name=check.name,
            command=(
                "uv",
                "run",
                "--locked",
                "python",
                "-m",
                CHECK_MODULES[check.name],
                *check.arguments,
            ),
        )
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
    return execute_legacy_commands(
        tuple(check.command for check in checks),
        cwd=cwd,
        runner=runner,
    )
