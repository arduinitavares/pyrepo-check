from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import io
from pathlib import Path
import shlex
import subprocess  # nosec B404
import threading
import time
from typing import TYPE_CHECKING, cast

from pyrepo_check.planning import PlannedCheck, RunPlan

if TYPE_CHECKING:
    from pyrepo_check.pytest_execution import PytestExecutionObservation


ProcessRunner = Callable[
    ...,
    subprocess.CompletedProcess[tuple[str, ...]],
]

CAPTURE_LIMIT_BYTES = 65_536
_PIPE_READ_BYTES = 64 * 1024


@dataclass(frozen=True)
class CapturedBytes:
    tail: bytes
    omitted_bytes: int


class _TailAccumulator:
    def __init__(self) -> None:
        self._tail = bytearray()
        self._total_bytes = 0

    def feed(self, chunk: bytes | bytearray | memoryview) -> None:
        chunk_size = len(chunk)
        self._total_bytes += chunk_size
        if chunk_size >= CAPTURE_LIMIT_BYTES:
            self._tail[:] = chunk[-CAPTURE_LIMIT_BYTES:]
            return
        overflow = len(self._tail) + chunk_size - CAPTURE_LIMIT_BYTES
        if overflow > 0:
            del self._tail[:overflow]
        self._tail.extend(chunk)

    def finish(self) -> CapturedBytes:
        tail = bytes(self._tail)
        return CapturedBytes(tail, self._total_bytes - len(tail))


@dataclass(frozen=True)
class ExecutedProcess:
    role: str
    command: tuple[str, ...]
    cwd: Path
    returncode: int | None
    duration_ms: int
    stdout: CapturedBytes | None
    stderr: CapturedBytes | None
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
    runner: ProcessRunner | None = None,
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

        executed.append(
            ExecutedCheck(
                planned=check,
                processes=(
                    execute_process(
                        role="primary",
                        command=check.command,
                        cwd=check.cwd,
                        capture_output=is_json,
                        runner=runner,
                        clock_ns=clock_ns,
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


def execute_process(
    *,
    role: str,
    command: tuple[str, ...],
    cwd: Path,
    capture_output: bool,
    runner: ProcessRunner | None,
    clock_ns: Callable[[], int],
    environment: dict[str, str] | None = None,
) -> ExecutedProcess:
    started_ns: int | None = None
    returncode: int | None = None
    stdout: CapturedBytes | None = None
    stderr: CapturedBytes | None = None
    spawn_error: str | None = None
    try:
        if runner is None:
            started_ns = clock_ns()
            returncode, stdout, stderr = _run_bounded_process(
                command,
                cwd=cwd,
                capture_output=capture_output,
                environment=environment,
            )
        else:
            runner_kwargs: dict[str, object] = {
                "cwd": cwd,
                "check": False,
                "capture_output": capture_output,
            }
            if environment is not None:
                runner_kwargs["env"] = environment
            started_ns = clock_ns()
            completed = runner(command, **runner_kwargs)
            returncode = completed.returncode
            if capture_output:
                stdout = _normalize_buffered_output(
                    cast(bytes | str | None, completed.stdout)
                )
                stderr = _normalize_buffered_output(
                    cast(bytes | str | None, completed.stderr)
                )
    except OSError as error:
        spawn_error = f"{type(error).__name__}: {error}"
    if started_ns is None:
        raise RuntimeError("process clock did not start")
    return ExecutedProcess(
        role=role,
        command=command,
        cwd=cwd,
        returncode=returncode,
        duration_ms=_duration_ms(started_ns, clock_ns()),
        stdout=stdout,
        stderr=stderr,
        spawn_error=spawn_error,
    )


def _run_bounded_process(
    command: tuple[str, ...],
    *,
    cwd: Path,
    capture_output: bool,
    environment: dict[str, str] | None,
) -> tuple[int, CapturedBytes | None, CapturedBytes | None]:
    process = subprocess.Popen(  # nosec B603
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
    )
    if not capture_output:
        return process.wait(), None, None

    stdout_accumulator = _TailAccumulator()
    stderr_accumulator = _TailAccumulator()
    stdout_thread = threading.Thread(
        target=_drain_pipe,
        args=(process.stdout, stdout_accumulator),
        name="pyrepo-check-stdout",
    )
    stderr_thread = threading.Thread(
        target=_drain_pipe,
        args=(process.stderr, stderr_accumulator),
        name="pyrepo-check-stderr",
    )
    stdout_thread.start()
    stderr_thread.start()
    returncode = process.wait()
    stdout_thread.join()
    stderr_thread.join()
    return returncode, stdout_accumulator.finish(), stderr_accumulator.finish()


def _drain_pipe(
    pipe: io.BufferedReader | None,
    accumulator: _TailAccumulator,
) -> None:
    if pipe is None:
        return
    read_buffer = bytearray(_PIPE_READ_BYTES)
    with pipe:
        while (read_bytes := pipe.readinto(read_buffer)) != 0:
            accumulator.feed(memoryview(read_buffer)[:read_bytes])


def _normalize_buffered_output(output: bytes | str | None) -> CapturedBytes:
    raw = output.encode() if isinstance(output, str) else output or b""
    retained = raw[-CAPTURE_LIMIT_BYTES:]
    return CapturedBytes(retained, len(raw) - len(retained))


def _duration_ms(started_ns: int, ended_ns: int) -> int:
    elapsed_ns = max(0, ended_ns - started_ns)
    return (elapsed_ns + 500_000) // 1_000_000
