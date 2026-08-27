from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import subprocess  # nosec B404
import threading

import pytest

from pyrepo_check import execution, repository_executor, runner as legacy_runner
from pyrepo_check.execution import (
    CAPTURE_LIMIT_BYTES,
    CapturedBytes,
    ProcessRunner,
    ToolEnvironmentObservation,
    execute_legacy_commands,
    execute_plan,
    execute_process,
    observe_tool_environment,
)
from tests.support import RecordingRunner, focused_plan, monotonic_clock


class _VirtualPipe:
    def __init__(
        self,
        total_bytes: int,
        byte: int,
        *,
        barrier: threading.Barrier | None = None,
        fail_after_reads: int | None = None,
    ) -> None:
        self.total_bytes = total_bytes
        self.byte = byte
        self.barrier = barrier
        self.fail_after_reads = fail_after_reads
        self.reads = 0
        self.offset = 0
        self.reader_ident: int | None = None
        self.closed_by_reader = False
        self.close_idents: list[int] = []

    def readinto(self, buffer: bytearray | memoryview) -> int:
        if self.reader_ident is None:
            self.reader_ident = threading.get_ident()
            if self.barrier is not None:
                self.barrier.wait(timeout=1)
        self.reads += 1
        if self.fail_after_reads is not None and self.reads > self.fail_after_reads:
            raise OSError("synthetic drain failure")
        remaining = self.total_bytes - self.offset
        if remaining <= 0:
            return 0
        count = min(len(buffer), remaining)
        buffer[:count] = bytes((self.byte,)) * count
        self.offset += count
        return count

    def close(self) -> None:
        close_ident = threading.get_ident()
        self.close_idents.append(close_ident)
        self.closed_by_reader = self.closed_by_reader or close_ident == self.reader_ident


class _FakePopen:
    def __init__(
        self,
        stdout: _VirtualPipe,
        stderr: _VirtualPipe,
        *,
        returncode: int = 0,
        cleanup_errors: bool = False,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.cleanup_errors = cleanup_errors
        self.wait_calls: list[float | None] = []
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self.cleanup_errors and timeout is not None:
            raise OSError("synthetic cleanup wait failure")
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if self.cleanup_errors:
            raise OSError("synthetic terminate failure")

    def kill(self) -> None:
        self.killed = True
        if self.cleanup_errors:
            raise OSError("synthetic kill failure")


def _install_fake_popen(
    monkeypatch: pytest.MonkeyPatch,
    process: _FakePopen,
    *,
    capture_output: bool = True,
) -> None:
    def fake_popen(*_args: object, **kwargs: object) -> _FakePopen:
        expected_pipe = subprocess.PIPE if capture_output else None
        assert kwargs["stdout"] is expected_pipe
        assert kwargs["stderr"] is expected_pipe
        return process

    monkeypatch.setattr(execution.subprocess, "Popen", fake_popen)


def test_tool_environment_observation_uses_controller_without_a_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(execution.sys, "executable", "/tool/../tool/bin/python")
    monkeypatch.setattr(execution.sys, "version_info", (3, 13, 15, "final", 0))

    observation = observe_tool_environment()

    assert observation.python.implementation == execution.sys.implementation.name
    assert observation.python.version == (3, 13, 15)
    assert observation.python.executable == Path("/tool/bin/python")


def test_public_execute_plan_delegates_once_without_legacy_adapters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = focused_plan(tmp_path, "ty")
    tool_environment = observe_tool_environment()
    sentinel = object()
    calls: list[tuple[object, object, object]] = []

    def delegate(
        delegated_plan: object,
        *,
        tool_environment: ToolEnvironmentObservation | None,
        runner: ProcessRunner | None,
        clock_ns: object,
    ) -> object:
        calls.append((delegated_plan, tool_environment, runner))
        assert clock_ns is clock
        return sentinel

    def legacy_must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("legacy execution adapter was reached")

    clock = monotonic_clock()
    runner = RecordingRunner()
    monkeypatch.setattr(repository_executor, "execute_repository_plan", delegate)
    monkeypatch.setattr(execution, "execute_legacy_commands", legacy_must_not_run)
    monkeypatch.setattr(legacy_runner, "run_checks", legacy_must_not_run)

    result = execute_plan(
        plan,
        tool_environment=tool_environment,
        runner=runner,
        clock_ns=clock,
    )

    assert result is sentinel
    assert calls == [(plan, tool_environment, runner)]


def test_execution_result_is_repository_execution_result_alias() -> None:
    assert execution.ExecutionResult is execution.RepositoryExecutionResult


@pytest.mark.parametrize(
    ("returncodes", "expected"),
    (((0, 0), 0), ((1, 0), 1), ((0, 7), 7), ((-15, 0), 2)),
)
def test_raw_command_compatibility_helper_retains_isolated_exit_contract(
    tmp_path: Path,
    returncodes: tuple[int, ...],
    expected: int,
) -> None:
    commands = tuple(("tool", str(index)) for index in range(len(returncodes)))

    assert execute_legacy_commands(
        commands,
        cwd=tmp_path,
        runner=RecordingRunner(returncodes=returncodes),
        clock_ns=monotonic_clock(),
    ) == expected


def test_execute_process_normalizes_injected_json_streams(tmp_path: Path) -> None:
    process = execute_process(
        role="primary",
        command=("tool",),
        cwd=tmp_path,
        capture_output=True,
        runner=RecordingRunner(stdout=("snowman: ☃",), stderr=(b"warning",)),
        clock_ns=monotonic_clock(),
    )

    assert process.stdout == CapturedBytes("snowman: ☃".encode(), 0)
    assert process.stderr == CapturedBytes(b"warning", 0)
    assert process.returncode == 0


def test_execute_process_bounds_each_injected_stream_independently(tmp_path: Path) -> None:
    stdout = b"x" * (CAPTURE_LIMIT_BYTES + 3)
    stderr = b"y" * (CAPTURE_LIMIT_BYTES + 5)

    process = execute_process(
        role="primary",
        command=("tool",),
        cwd=tmp_path,
        capture_output=True,
        runner=RecordingRunner(stdout=(stdout,), stderr=(stderr,)),
        clock_ns=monotonic_clock(),
    )

    assert process.stdout == CapturedBytes(b"x" * CAPTURE_LIMIT_BYTES, 3)
    assert process.stderr == CapturedBytes(b"y" * CAPTURE_LIMIT_BYTES, 5)


@pytest.mark.parametrize("error", (FileNotFoundError("tool"), OSError("spawn blocked")))
def test_execute_process_records_spawn_failure(
    tmp_path: Path,
    error: OSError,
) -> None:
    process = execute_process(
        role="primary",
        command=("tool",),
        cwd=tmp_path,
        capture_output=True,
        runner=RecordingRunner(raise_on_call=1, exception=error),
        clock_ns=monotonic_clock(),
    )

    assert process.returncode is None
    assert process.spawn_error == f"{type(error).__name__}: {error}"


def test_execute_process_propagates_non_os_programming_error(tmp_path: Path) -> None:
    marker = ValueError("runner bug")

    with pytest.raises(ValueError) as raised:
        execute_process(
            role="primary",
            command=("tool",),
            cwd=tmp_path,
            capture_output=True,
            runner=RecordingRunner(raise_on_call=1, exception=marker),
            clock_ns=monotonic_clock(),
        )

    assert raised.value is marker


def test_production_process_capture_is_bounded(tmp_path: Path) -> None:
    source = (
        "import os; "
        f"os.write(1, b'a' * {CAPTURE_LIMIT_BYTES + 9}); "
        f"os.write(2, b'b' * {CAPTURE_LIMIT_BYTES + 11})"
    )
    process = execute_process(
        role="primary",
        command=(execution.sys.executable, "-c", source),
        cwd=tmp_path,
        capture_output=True,
        runner=None,
        clock_ns=execution.time.monotonic_ns,
    )

    assert process.returncode == 0
    assert process.stdout == CapturedBytes(b"a" * CAPTURE_LIMIT_BYTES, 9)
    assert process.stderr == CapturedBytes(b"b" * CAPTURE_LIMIT_BYTES, 11)


def test_terminal_process_inherits_streams(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    process = execute_process(
        role="primary",
        command=(execution.sys.executable, "-c", "print('visible')"),
        cwd=tmp_path,
        capture_output=False,
        runner=None,
        clock_ns=execution.time.monotonic_ns,
    )

    assert process.returncode == 0
    assert process.stdout is None
    assert process.stderr is None
    assert capfd.readouterr().out == "visible\n"


def test_production_capture_uses_distinct_readers_and_exact_bounded_tails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = threading.Barrier(2)
    stdout = _VirtualPipe(CAPTURE_LIMIT_BYTES + 11, ord("o"), barrier=barrier)
    stderr = _VirtualPipe(CAPTURE_LIMIT_BYTES + 17, ord("e"), barrier=barrier)
    popen = _FakePopen(stdout, stderr)
    _install_fake_popen(monkeypatch, popen)

    process = execute_process(
        role="primary",
        command=("tool",),
        cwd=tmp_path,
        capture_output=True,
        runner=None,
        clock_ns=monotonic_clock(),
    )

    assert stdout.reader_ident not in {None, threading.get_ident()}
    assert stderr.reader_ident not in {None, threading.get_ident(), stdout.reader_ident}
    assert process.stdout == CapturedBytes(b"o" * CAPTURE_LIMIT_BYTES, 11)
    assert process.stderr == CapturedBytes(b"e" * CAPTURE_LIMIT_BYTES, 17)
    assert stdout.closed_by_reader and stderr.closed_by_reader


def test_pipe_drain_error_is_typed_and_reaps_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    popen = _FakePopen(
        _VirtualPipe(1, ord("x"), fail_after_reads=0),
        _VirtualPipe(0, ord("y")),
    )
    _install_fake_popen(monkeypatch, popen)

    process = execute_process(
        role="primary",
        command=("tool",),
        cwd=tmp_path,
        capture_output=True,
        runner=None,
        clock_ns=monotonic_clock(),
    )

    assert process.returncode is None
    assert process.stdout is None and process.stderr is None
    assert process.spawn_error == "stdout drain failed: OSError: synthetic drain failure"
    assert popen.terminated


def test_second_reader_start_failure_closes_and_reaps_without_masking_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = _VirtualPipe(0, ord("x"))
    stderr = _VirtualPipe(0, ord("y"))
    popen = _FakePopen(stdout, stderr, cleanup_errors=True)
    _install_fake_popen(monkeypatch, popen)
    real_thread = threading.Thread
    created_threads: list[threading.Thread] = []
    start_calls = 0

    class FailSecondStartThread(real_thread):
        def __init__(
            self,
            *,
            target: Callable[..., object] | None = None,
            args: tuple[object, ...] = (),
            name: str | None = None,
            daemon: bool | None = None,
        ) -> None:
            super().__init__(target=target, args=args, name=name, daemon=daemon)
            created_threads.append(self)

        def start(self) -> None:
            nonlocal start_calls
            start_calls += 1
            if start_calls == 2:
                raise RuntimeError("synthetic stderr reader start failure")
            super().start()

    monkeypatch.setattr(execution.threading, "Thread", FailSecondStartThread)

    process = execute_process(
        role="primary",
        command=("tool",),
        cwd=tmp_path,
        capture_output=True,
        runner=None,
        clock_ns=monotonic_clock(),
    )

    assert process.returncode is None
    assert process.spawn_error == (
        "stderr reader start failed: RuntimeError: synthetic stderr reader start failure"
    )
    assert stdout.closed_by_reader
    assert stderr.close_idents == [threading.get_ident()]
    assert len(created_threads) == 2
    assert all(reader.daemon for reader in created_threads)
    assert popen.terminated and popen.killed


def test_second_reader_construction_failure_reaps_first_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = _VirtualPipe(0, ord("x"))
    stderr = _VirtualPipe(0, ord("y"))
    popen = _FakePopen(stdout, stderr)
    _install_fake_popen(monkeypatch, popen)
    real_thread = threading.Thread
    constructions = 0

    def construct_thread(
        *,
        target: Callable[..., object] | None = None,
        args: tuple[object, ...] = (),
        name: str | None = None,
        daemon: bool | None = None,
    ) -> threading.Thread:
        nonlocal constructions
        constructions += 1
        if constructions == 2:
            raise RuntimeError("synthetic stderr reader construction failure")
        return real_thread(target=target, args=args, name=name, daemon=daemon)

    monkeypatch.setattr(execution.threading, "Thread", construct_thread)

    process = execute_process(
        role="primary",
        command=("tool",),
        cwd=tmp_path,
        capture_output=True,
        runner=None,
        clock_ns=monotonic_clock(),
    )

    assert process.returncode is None
    assert process.spawn_error == (
        "stderr reader construction failed: RuntimeError: "
        "synthetic stderr reader construction failure"
    )
    assert popen.terminated
    assert stdout.closed_by_reader
    assert stderr.close_idents == [threading.get_ident()]


def test_wait_failure_is_typed_after_both_readers_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class WaitFailurePopen(_FakePopen):
        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls.append(timeout)
            if timeout is None:
                assert self.stdout.closed_by_reader and self.stderr.closed_by_reader
                raise OSError("synthetic wait failure")
            return 0

    popen = WaitFailurePopen(_VirtualPipe(0, ord("x")), _VirtualPipe(0, ord("y")))
    _install_fake_popen(monkeypatch, popen)

    process = execute_process(
        role="primary",
        command=("tool",),
        cwd=tmp_path,
        capture_output=True,
        runner=None,
        clock_ns=monotonic_clock(),
    )

    assert process.returncode is None
    assert process.spawn_error == "wait failed: OSError: synthetic wait failure"
    assert popen.terminated
    assert popen.wait_calls[0] is None
    assert popen.wait_calls[1] is not None


def test_reader_programming_error_cleans_up_then_reraises_by_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = ValueError("synthetic reader bug")

    class ProgrammingFailurePipe(_VirtualPipe):
        def readinto(self, buffer: bytearray | memoryview) -> int:
            del buffer
            self.reader_ident = threading.get_ident()
            raise marker

    stdout = ProgrammingFailurePipe(0, ord("x"))
    stderr = _VirtualPipe(0, ord("y"))
    popen = _FakePopen(stdout, stderr)
    _install_fake_popen(monkeypatch, popen)

    with pytest.raises(ValueError) as raised:
        execute_process(
            role="primary",
            command=("tool",),
            cwd=tmp_path,
            capture_output=True,
            runner=None,
            clock_ns=monotonic_clock(),
        )

    assert raised.value is marker
    assert popen.terminated
    assert stdout.closed_by_reader and stderr.closed_by_reader
