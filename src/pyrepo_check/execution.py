from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import shlex
import subprocess  # nosec B404
import time
from typing import cast

from pyrepo_check.planning import PlannedCheck, RunPlan


ProcessRunner = Callable[
    ...,
    subprocess.CompletedProcess[tuple[str, ...]],
]


@dataclass(frozen=True)
class ExecutedCheck:
    planned: PlannedCheck
    returncode: int | None
    duration_ms: int
    stdout: bytes | None
    stderr: bytes | None
    spawn_error: str | None


@dataclass(frozen=True)
class ExecutionResult:
    checks: tuple[ExecutedCheck, ...]
    exit_code: int


def execute_plan(
    plan: RunPlan,
    *,
    runner: ProcessRunner = subprocess.run,
    clock_ns: Callable[[], int] = time.monotonic_ns,
) -> ExecutionResult:
    executed: list[ExecutedCheck] = []

    for check in plan.checks:
        is_json = plan.output_format == "json"
        if not is_json:
            print(f"\n==> {check.name}: {shlex.join(check.command)}", flush=True)

        started_ns = clock_ns()
        returncode: int | None = None
        stdout: bytes | None = None
        stderr: bytes | None = None
        spawn_error: str | None = None
        try:
            if is_json:
                completed = runner(
                    check.command,
                    cwd=check.cwd,
                    check=False,
                    capture_output=True,
                )
                stdout = _as_bytes(cast(bytes | str | None, completed.stdout))
                stderr = _as_bytes(cast(bytes | str | None, completed.stderr))
            else:
                completed = runner(check.command, cwd=check.cwd, check=False)
            returncode = completed.returncode
        except OSError as error:
            spawn_error = f"{type(error).__name__}: {error}"
        finally:
            duration_ms = (clock_ns() - started_ns + 500_000) // 1_000_000

        executed.append(
            ExecutedCheck(
                planned=check,
                returncode=returncode,
                duration_ms=duration_ms,
                stdout=stdout,
                stderr=stderr,
                spawn_error=spawn_error,
            )
        )

    first_positive = next(
        (
            check.returncode
            for check in executed
            if check.returncode is not None and check.returncode > 0
        ),
        None,
    )
    if first_positive is not None:
        exit_code = first_positive
    elif any(check.returncode is None or check.returncode < 0 for check in executed):
        exit_code = 2
    else:
        exit_code = 0

    return ExecutionResult(checks=tuple(executed), exit_code=exit_code)


def _as_bytes(output: bytes | str | None) -> bytes | None:
    if isinstance(output, str):
        return output.encode()
    return output
