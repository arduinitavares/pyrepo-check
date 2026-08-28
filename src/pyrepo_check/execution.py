from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import queue
import shlex
import subprocess  # nosec B404
import sys
import threading
import time
from typing import TYPE_CHECKING, Literal, Protocol, cast

from pyrepo_check.planning import (
    CheckInvocation,
    CheckName,
    RepositoryPythonSelection,
    RunPlan,
)
from pyrepo_check import __version__

if TYPE_CHECKING:
    from pyrepo_check.controller_tools import ControllerExecutable
    from pyrepo_check.coverage_execution import CoverageExecutionObservation
    from pyrepo_check.pytest_execution import PytestExecutionObservation


ProcessRunner = Callable[
    ...,
    subprocess.CompletedProcess[tuple[str, ...]],
]
TerminalWriter = Callable[[str], None]

CAPTURE_LIMIT_BYTES = 65_536
_PIPE_READ_BYTES = 64 * 1024
_FAILURE_CLEANUP_TIMEOUT_SECONDS = 0.2

_ExecutionFailurePhase = Literal[
    "stdout reader construction",
    "stderr reader construction",
    "stdout reader start",
    "stderr reader start",
    "stdout drain",
    "stderr drain",
    "wait",
]


class _ReadablePipe(Protocol):
    def readinto(self, buffer: bytearray | memoryview) -> int: ...

    def close(self) -> None: ...


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
class _ReaderResult:
    stream: Literal["stdout", "stderr"]
    error: BaseException | None


class _ProcessExecutionFailure(Exception):
    def __init__(self, phase: _ExecutionFailurePhase, error: OSError | RuntimeError) -> None:
        super().__init__(f"{phase} failed: {type(error).__name__}: {error}")
        self.phase = phase
        self.error = error


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


PythonVersion = tuple[int, int, int]
LockStatus = Literal["current", "missing", "unverified"]
MutationProtection = Literal["unobserved", "protected_files", "tracked_files"]
EnvironmentErrorCode = Literal[
    "repository_lock_missing",
    "uv_unavailable",
    "repository_environment_failed",
    "repository_python_unsupported",
    "unsafe_repository_environment",
    "environment_evidence_invalid",
    "repository_state_changed",
]
CheckExecutionErrorCode = Literal[
    "spawn_failed",
    "terminated_by_signal",
    "pytest_preflight_failed",
    "pytest_evidence_error",
    "coverage_preflight_failed",
    "missing_primary_process",
    "cleanup_failed",
    "repository_environment_unavailable",
    "check_dependency_missing",
    "check_dependency_incompatible",
    "check_dependency_shadowed",
    "check_dependency_unusable",
    "check_start_evidence_invalid",
    "check_execution_failed",
]
DependencyStatus = Literal[
    "available", "missing", "incompatible", "shadowed", "unusable", "unobserved"
]
CheckModule = Literal["ruff", "ty", "bandit", "pytest", "coverage"]


@dataclass(frozen=True)
class PythonObservation:
    implementation: str
    version: PythonVersion
    executable: Path


@dataclass(frozen=True)
class ToolEnvironmentObservation:
    pyrepo_check_version: str
    python: PythonObservation


@dataclass(frozen=True)
class EnvironmentFailureObservation:
    code: EnvironmentErrorCode
    message: str
    hint: str | None


@dataclass(frozen=True)
class CheckExecutionFailure:
    code: CheckExecutionErrorCode
    message: str
    hint: str | None


@dataclass(frozen=True)
class CheckStartObservation:
    schema_version: Literal[1]
    check: CheckName
    module: CheckModule
    arguments_sha256: str
    python: PythonObservation


@dataclass(frozen=True)
class AnalysisPythonAuthorityObservation:
    authority: Literal["repository_tool"] = "repository_tool"
    pyrepo_check_override: None = None


@dataclass(frozen=True)
class DependencyObservation:
    name: Literal["ruff", "ty", "bandit", "pytest", "coverage"]
    module: str
    required: str
    status: DependencyStatus
    version: str | None
    origin: str | None
    process: ExecutedProcess | None
    error: CheckExecutionFailure | None


@dataclass(frozen=True)
class PreparedRepositoryEnvironment:
    root: Path
    path: Path
    python: PythonObservation
    python_selection: RepositoryPythonSelection
    manager_version: str
    child_environment: Mapping[str, str]
    controller_uv: ControllerExecutable | None = None


@dataclass(frozen=True)
class RepositoryEnvironmentObservation:
    manager_version: str | None
    path: Path | None
    python_selection: RepositoryPythonSelection
    python: PythonObservation | None
    lock_path: Path
    lock_status: LockStatus
    mutation_protection: MutationProtection
    dependencies: tuple[DependencyObservation, ...]
    processes: tuple[ExecutedProcess, ...]
    error: EnvironmentFailureObservation | None


@dataclass(frozen=True)
class RepositoryPreparation:
    prepared: PreparedRepositoryEnvironment | None
    observation: RepositoryEnvironmentObservation


@dataclass(frozen=True)
class RepositoryLockPresence:
    path: Path
    state: Literal["missing", "present", "unsafe"]
    diagnostic: str | None


@dataclass(frozen=True)
class RepositoryCheckObservation:
    invocation: CheckInvocation
    execution_environment: Literal["repository"] | None
    analysis_python_authority: AnalysisPythonAuthorityObservation | None
    start: CheckStartObservation | None
    processes: tuple[ExecutedProcess, ...]
    error: CheckExecutionFailure | None
    pytest: PytestExecutionObservation | None = None
    coverage: CoverageExecutionObservation | None = None
    environment_error: EnvironmentFailureObservation | None = None


@dataclass(frozen=True)
class RepositoryExecutionResult:
    tool_environment: ToolEnvironmentObservation
    repository_environment: RepositoryEnvironmentObservation
    checks: tuple[RepositoryCheckObservation, ...]


ExecutionResult = RepositoryExecutionResult


def observe_tool_environment() -> ToolEnvironmentObservation:
    executable = Path(os.path.abspath(os.path.normpath(sys.executable)))
    return ToolEnvironmentObservation(
        pyrepo_check_version=__version__,
        python=PythonObservation(
            implementation=sys.implementation.name,
            version=cast(PythonVersion, tuple(sys.version_info[:3])),
            executable=executable,
        ),
    )


@dataclass(frozen=True)
class ExecutedCheck:
    planned: CheckInvocation
    processes: tuple[ExecutedProcess, ...]
    pytest: PytestExecutionObservation | None = None
    coverage: CoverageExecutionObservation | None = None


CHECK_MODULES: Mapping[CheckName, str] = {
    "ruff": "ruff",
    "annotations": "ruff",
    "annotations-fix": "ruff",
    "ty": "ty",
    "bandit": "bandit",
    "pytest": "pytest",
}


def execute_plan(
    plan: RunPlan,
    *,
    tool_environment: ToolEnvironmentObservation | None = None,
    runner: ProcessRunner | None = None,
    clock_ns: Callable[[], int] = time.monotonic_ns,
    terminal_writer: TerminalWriter | None = None,
) -> ExecutionResult:
    from pyrepo_check.repository_executor import execute_repository_plan

    return execute_repository_plan(
        plan,
        tool_environment=tool_environment,
        runner=runner,
        clock_ns=clock_ns,
        terminal_writer=terminal_writer,
    )


def format_terminal_environment_line(
    tool_python: tuple[int, int, int],
    repository_python: tuple[int, int, int],
) -> str:
    tool_version = ".".join(str(piece) for piece in tool_python)
    repository_version = ".".join(str(piece) for piece in repository_python)
    return (
        f"==> environment: tool Python {tool_version} -> "
        f"repository Python {repository_version} (uv, locked)\n"
    )


def format_terminal_check_banner(
    name: CheckName,
    module: CheckModule,
    arguments: tuple[str, ...],
) -> str:
    command = shlex.join(("python", "-m", module, *arguments))
    return f"\n==> {name}: {command}\n"


def execute_legacy_commands(
    commands: Sequence[tuple[str, ...]],
    *,
    cwd: Path,
    runner: ProcessRunner | None = None,
    clock_ns: Callable[[], int] = time.monotonic_ns,
) -> int:
    """Run raw compatibility commands without producing an Agent Report."""
    executed = tuple(
        execute_process(
            role="legacy",
            command=command,
            cwd=cwd,
            capture_output=False,
            runner=runner,
            clock_ns=clock_ns,
        )
        for command in commands
    )
    first_positive = next(
        (
            process.returncode
            for process in executed
            if process.returncode is not None and process.returncode > 0
        ),
        None,
    )
    if first_positive is not None:
        return first_positive
    return (
        2
        if any(process.returncode is None or process.returncode < 0 for process in executed)
        else 0
    )


def execute_process(
    *,
    role: str,
    command: tuple[str, ...],
    cwd: Path,
    capture_output: bool,
    runner: ProcessRunner | None,
    clock_ns: Callable[[], int],
    environment: Mapping[str, str] | None = None,
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
                stdout = _normalize_buffered_output(cast(bytes | str | None, completed.stdout))
                stderr = _normalize_buffered_output(cast(bytes | str | None, completed.stderr))
    except _ProcessExecutionFailure as error:
        spawn_error = str(error)
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
    environment: Mapping[str, str] | None,
) -> tuple[int, CapturedBytes | None, CapturedBytes | None]:
    process = subprocess.Popen(  # nosec B603
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
    )
    if not capture_output:
        try:
            try:
                returncode = process.wait()
            except (OSError, RuntimeError) as error:
                raise _ProcessExecutionFailure("wait", error) from error
        except BaseException:
            _cleanup_failed_process(process, (), [])
            raise
        return returncode, None, None

    stdout_accumulator = _TailAccumulator()
    stderr_accumulator = _TailAccumulator()
    results: queue.SimpleQueue[_ReaderResult] = queue.SimpleQueue()
    started_threads: list[threading.Thread] = []
    streams: tuple[
        tuple[Literal["stdout", "stderr"], _ReadablePipe | None, _TailAccumulator],
        ...,
    ] = (
        ("stdout", cast(_ReadablePipe | None, process.stdout), stdout_accumulator),
        ("stderr", cast(_ReadablePipe | None, process.stderr), stderr_accumulator),
    )
    try:
        for stream, pipe, accumulator in streams:
            try:
                reader = threading.Thread(
                    target=_run_pipe_reader,
                    args=(stream, pipe, accumulator, results),
                    name=f"pyrepo-check-{stream}",
                    daemon=True,
                )
            except (OSError, RuntimeError) as error:
                raise _ProcessExecutionFailure(
                    cast(_ExecutionFailurePhase, f"{stream} reader construction"),
                    error,
                ) from error
            try:
                reader.start()
            except (OSError, RuntimeError) as error:
                raise _ProcessExecutionFailure(
                    cast(_ExecutionFailurePhase, f"{stream} reader start"),
                    error,
                ) from error
            started_threads.append(reader)

        for _ in streams:
            result = results.get()
            if result.error is None:
                continue
            if isinstance(result.error, (OSError, RuntimeError)):
                raise _ProcessExecutionFailure(
                    cast(_ExecutionFailurePhase, f"{result.stream} drain"),
                    result.error,
                ) from result.error
            raise result.error
        for reader in started_threads:
            reader.join()
        try:
            returncode = process.wait()
        except (OSError, RuntimeError) as error:
            raise _ProcessExecutionFailure("wait", error) from error
    except BaseException:
        _cleanup_failed_process(process, streams, started_threads)
        raise
    return returncode, stdout_accumulator.finish(), stderr_accumulator.finish()


def _run_pipe_reader(
    stream: Literal["stdout", "stderr"],
    pipe: _ReadablePipe | None,
    accumulator: _TailAccumulator,
    results: queue.SimpleQueue[_ReaderResult],
) -> None:
    error: BaseException | None = None
    try:
        _drain_pipe(pipe, accumulator)
    except BaseException as caught:
        error = caught
    finally:
        if pipe is not None:
            try:
                pipe.close()
            except BaseException as caught:
                if error is None:
                    error = caught
        results.put(_ReaderResult(stream, error))


def _cleanup_failed_process(
    process: subprocess.Popen[bytes],
    streams: tuple[
        tuple[Literal["stdout", "stderr"], _ReadablePipe | None, _TailAccumulator],
        ...,
    ],
    started_threads: list[threading.Thread],
) -> None:
    deadline = time.monotonic() + _FAILURE_CLEANUP_TIMEOUT_SECONDS
    reaped = False
    try:
        process.terminate()
    except BaseException:
        pass
    remaining = max(0.0, deadline - time.monotonic())
    if remaining > 0:
        try:
            process.wait(timeout=remaining)
            reaped = True
        except BaseException:
            pass
    if not reaped:
        try:
            process.kill()
        except BaseException:
            pass
        remaining = max(0.0, deadline - time.monotonic())
        if remaining > 0:
            try:
                process.wait(timeout=remaining)
            except BaseException:
                pass
    for _stream, pipe, _accumulator in streams[len(started_threads) :]:
        if pipe is not None:
            try:
                pipe.close()
            except BaseException:
                pass
    for reader in started_threads:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            break
        try:
            reader.join(timeout=remaining)
        except BaseException:
            pass


def _drain_pipe(
    pipe: _ReadablePipe | None,
    accumulator: _TailAccumulator,
) -> None:
    if pipe is None:
        return
    read_buffer = bytearray(_PIPE_READ_BYTES)
    while (read_bytes := pipe.readinto(read_buffer)) != 0:
        accumulator.feed(memoryview(read_buffer)[:read_bytes])


def _normalize_buffered_output(output: bytes | str | None) -> CapturedBytes:
    raw = output.encode() if isinstance(output, str) else output or b""
    retained = raw[-CAPTURE_LIMIT_BYTES:]
    return CapturedBytes(retained, len(raw) - len(retained))


def _duration_ms(started_ns: int, ended_ns: int) -> int:
    elapsed_ns = max(0, ended_ns - started_ns)
    return (elapsed_ns + 500_000) // 1_000_000
