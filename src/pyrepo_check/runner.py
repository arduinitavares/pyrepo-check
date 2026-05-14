from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess
import shlex
import subprocess  # nosec B404

from pyrepo_check.config import ProjectConfig


CHECK_ORDER = ("ruff", "annotations", "ty", "bandit", "pytest")
SELECTABLE_CHECK_ORDER = (*CHECK_ORDER, "annotations-fix")


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
    prefix = _uv_python_prefix(config)
    explicit_targets = tuple(targets)
    strict_targets = (".",) if strict_all and not explicit_targets else ()
    ruff_targets = explicit_targets or strict_targets or config.ruff_targets
    bandit_targets = explicit_targets or strict_targets or config.bandit_targets
    return {
        "ruff": Check(
            "ruff",
            (*prefix, "ruff", "check", *ruff_targets),
        ),
        "annotations": Check(
            "annotations",
            (
                *prefix,
                "ruff",
                "check",
                *ruff_targets,
                "--select",
                "ANN",
                "--output-format",
                "concise",
            ),
        ),
        "annotations-fix": Check(
            "annotations-fix",
            (
                *prefix,
                "ruff",
                "check",
                *ruff_targets,
                "--select",
                "ANN",
                "--fix",
                "--unsafe-fixes",
            ),
        ),
        "ty": Check("ty", (*prefix, "ty", "check", *explicit_targets)),
        "bandit": Check(
            "bandit",
            (
                *prefix,
                "bandit",
                "-c",
                "pyproject.toml",
                *_bandit_target_args(bandit_targets, recursive=not explicit_targets),
            ),
        ),
        "pytest": Check("pytest", (*prefix, "pytest", *explicit_targets)),
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
    return tuple(checks[name] for name in SELECTABLE_CHECK_ORDER if name in requested_names)


def run_checks(
    checks: Sequence[Check],
    *,
    cwd: Path,
    runner: Callable[..., CompletedProcess[tuple[str, ...]]] = subprocess.run,
) -> int:
    exit_code = 0
    for check in checks:
        print(f"\n==> {check.name}: {shlex.join(check.command)}", flush=True)
        completed = runner(check.command, cwd=cwd, check=False)
        if completed.returncode != 0 and exit_code == 0:
            exit_code = completed.returncode
    return exit_code


def _uv_python_prefix(config: ProjectConfig) -> tuple[str, ...]:
    if config.frozen:
        return ("uv", "run", "--frozen", "python", "-m")
    return ("uv", "run", "python", "-m")


def _bandit_target_args(targets: Sequence[str], *, recursive: bool) -> tuple[str, ...]:
    if recursive:
        return ("-r", *targets)
    return tuple(targets)
