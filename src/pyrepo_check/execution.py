from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import shlex
import subprocess  # nosec B404

from pyrepo_check.planning import PlannedCheck, RunPlan


ProcessRunner = Callable[
    ...,
    subprocess.CompletedProcess[tuple[str, ...]],
]


@dataclass(frozen=True)
class ExecutedCheck:
    planned: PlannedCheck
    returncode: int


@dataclass(frozen=True)
class ExecutionResult:
    checks: tuple[ExecutedCheck, ...]
    exit_code: int


def execute_plan(
    plan: RunPlan,
    *,
    runner: ProcessRunner = subprocess.run,
) -> ExecutionResult:
    executed: list[ExecutedCheck] = []
    exit_code = 0

    for check in plan.checks:
        print(f"\n==> {check.name}: {shlex.join(check.command)}", flush=True)
        completed = runner(check.command, cwd=check.cwd, check=False)
        executed.append(ExecutedCheck(planned=check, returncode=completed.returncode))
        if completed.returncode != 0 and exit_code == 0:
            exit_code = completed.returncode

    return ExecutionResult(checks=tuple(executed), exit_code=exit_code)
