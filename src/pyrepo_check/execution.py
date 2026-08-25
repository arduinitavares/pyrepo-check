from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import shlex
import subprocess  # nosec B404
import time
from typing import TYPE_CHECKING, cast

from pyrepo_check.planning import PlannedCheck, RunPlan

if TYPE_CHECKING:
    from pyrepo_check.pytest_execution import PytestExecutionObservation


ProcessRunner = Callable[
    ...,
    subprocess.CompletedProcess[tuple[str, ...]],
]


@dataclass(frozen=True)
class ExecutedProcess:
    role: str
    command: tuple[str, ...]
    cwd: Path
    returncode: int | None
    duration_ms: int
    stdout: bytes | None
    stderr: bytes | None
    spawn_error: str | None


@dataclass(frozen=True)
class ExecutedCheck:
    planned: PlannedCheck
    processes: tuple[ExecutedProcess, ...]
    pytest: PytestExecutionObservation | None = None


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
        if check.pytest is not None:
            from pyrepo_check.pytest_execution import execute_pytest

            executed.append(
                execute_pytest(
                    check,
                    output_format=plan.output_format,
                    runner=runner,
                    clock_ns=clock_ns,
                )
            )
            continue

        returncode: int | None = None
        stdout: bytes | None = None
        stderr: bytes | None = None
        spawn_error: str | None = None
        started_ns: int | None = None
        duration_ms = 0
        try:
            if is_json:
                started_ns = clock_ns()
                completed = runner(
                    check.command,
                    cwd=check.cwd,
                    check=False,
                    capture_output=True,
                )
                stdout = _as_bytes(cast(bytes | str | None, completed.stdout))
                stderr = _as_bytes(cast(bytes | str | None, completed.stderr))
                returncode = completed.returncode
            else:
                started_ns = clock_ns()
                completed = runner(check.command, cwd=check.cwd, check=False)
                returncode = completed.returncode
        except OSError as error:
            if started_ns is None:
                raise
            spawn_error = f"{type(error).__name__}: {error}"
        finally:
            if started_ns is not None:
                duration_ms = _duration_ms(started_ns, clock_ns())

        executed.append(
            ExecutedCheck(
                planned=check,
                processes=(
                    ExecutedProcess(
                        role="primary",
                        command=check.command,
                        cwd=check.cwd,
                        returncode=returncode,
                        duration_ms=duration_ms,
                        stdout=stdout,
                        stderr=stderr,
                        spawn_error=spawn_error,
                    ),
                ),
            )
        )

    first_positive = next(
        (
            process.returncode
            for check in executed
            for process in check.processes
            if process.returncode is not None and process.returncode > 0
        ),
        None,
    )
    if first_positive is not None:
        exit_code = first_positive
    elif any(
        process.returncode is None or process.returncode < 0
        for check in executed
        for process in check.processes
    ):
        exit_code = 2
    else:
        exit_code = 0

    return ExecutionResult(checks=tuple(executed), exit_code=exit_code)


def _as_bytes(output: bytes | str | None) -> bytes | None:
    if isinstance(output, str):
        return output.encode()
    return output


def _duration_ms(started_ns: int, ended_ns: int) -> int:
    elapsed_ns = max(0, ended_ns - started_ns)
    return (elapsed_ns + 500_000) // 1_000_000
