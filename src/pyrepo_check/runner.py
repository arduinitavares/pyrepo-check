from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess
import shlex
import subprocess

from pyrepo_check.config import ProjectConfig


CHECK_ORDER = ("ruff", "ty", "bandit", "pytest")


@dataclass(frozen=True)
class Check:
    name: str
    command: tuple[str, ...]


def build_checks(config: ProjectConfig) -> dict[str, Check]:
    prefix = _uv_python_prefix(config)
    return {
        "ruff": Check(
            "ruff",
            (*prefix, "ruff", "check", *config.ruff_targets),
        ),
        "ty": Check("ty", (*prefix, "ty", "check")),
        "bandit": Check(
            "bandit",
            (*prefix, "bandit", "-c", "pyproject.toml", "-r", *config.bandit_targets),
        ),
        "pytest": Check("pytest", (*prefix, "pytest")),
    }


def select_checks(
    checks: Mapping[str, Check],
    *,
    requested: Sequence[str],
    all_selected: bool,
) -> tuple[Check, ...]:
    if all_selected or not requested:
        return tuple(checks[name] for name in CHECK_ORDER)

    unknown = sorted(set(requested) - set(checks))
    if unknown:
        names = ", ".join(unknown)
        raise ValueError(f"Unknown check(s): {names}")

    requested_names = set(requested)
    return tuple(checks[name] for name in CHECK_ORDER if name in requested_names)


def run_checks(
    checks: Sequence[Check],
    *,
    cwd: Path,
    runner: Callable[..., CompletedProcess[tuple[str, ...]]] = subprocess.run,
) -> int:
    for check in checks:
        print(f"\n==> {check.name}: {shlex.join(check.command)}", flush=True)
        completed = runner(check.command, cwd=cwd, check=False)
        if completed.returncode != 0:
            return completed.returncode
    return 0


def _uv_python_prefix(config: ProjectConfig) -> tuple[str, ...]:
    if config.frozen:
        return ("uv", "run", "--frozen", "python", "-m")
    return ("uv", "run", "python", "-m")
