from __future__ import annotations

import inspect
import os
from pathlib import Path
import subprocess  # nosec B404
import sys
import signal
import threading
import tracemalloc
from collections.abc import Callable

import pytest

from pyrepo_check import execution
from pyrepo_check.execution import (
    CAPTURE_LIMIT_BYTES,
    CapturedBytes,
    ExecutedCheck,
    ExecutedProcess,
    ExecutionResult,
    execute_plan,
    observe_tool_environment,
)
from pyrepo_check.planning import (
    CheckInvocation,
    DefaultRepositoryPython,
    ExplicitRepositoryPython,
    OutputFormat,
    PytestExecutionPlan,
    RunPlan,
)
from tests.support import RecordingRunner


def test_tool_environment_observation_uses_controller_without_a_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbid_process(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("tool environment observation must not spawn a process")

    monkeypatch.setattr(subprocess, "Popen", forbid_process)

    observation = observe_tool_environment()

    assert observation.pyrepo_check_version == "0.1.0"
    assert observation.python.implementation == sys.implementation.name
    assert observation.python.version == tuple(sys.version_info[:3])
    assert observation.python.executable == Path(
        os.path.abspath(os.path.normpath(sys.executable))
    )


def make_plan(tmp_path: Path) -> RunPlan:
    return RunPlan(
        root=tmp_path,
        repository_python=DefaultRepositoryPython(),
        mode="focused",
        targets=(),
        checks=(
            CheckInvocation(
                name="ruff",
                arguments=("check", "src"),
            ),
            CheckInvocation(
                name="ty",
                arguments=("check",),
            ),
        ),
    )


def test_executor_expands_environment_neutral_invocation_into_locked_command(
    tmp_path: Path,
) -> None:
    plan = RunPlan(
        root=tmp_path,
        repository_python=ExplicitRepositoryPython("3.12"),
        mode="focused",
        targets=("src", "src"),
        checks=(CheckInvocation("ruff", ("check", "src", "src")),),
    )

    assert execution.locked_module_command(plan, plan.checks[0]) == (
        "uv",
        "run",
        "--locked",
        "--python",
        "3.12",
        "python",
        "-m",
        "ruff",
        "check",
        "src",
        "src",
    )


def make_json_plan(tmp_path: Path) -> RunPlan:
    plan = make_plan(tmp_path)
    return RunPlan(
        root=plan.root,
        repository_python=plan.repository_python,
        mode=plan.mode,
        targets=plan.targets,
        checks=plan.checks,
        output_format="json",
    )


def make_single_check_plan(
    tmp_path: Path,
    *,
    output_format: OutputFormat = "terminal",
) -> RunPlan:
    plan = make_plan(tmp_path)
    return RunPlan(
        root=plan.root,
        repository_python=plan.repository_python,
        mode=plan.mode,
        targets=plan.targets,
        checks=plan.checks[:1],
        output_format=output_format,
    )


def make_python_check_plan(tmp_path: Path, source: str) -> RunPlan:
    return RunPlan(
        root=tmp_path,
        repository_python=DefaultRepositoryPython(),
        mode="focused",
        targets=(),
        checks=(
            CheckInvocation(
                name="ruff",
                arguments=("check", source),
            ),
        ),
        output_format="json",
    )


def test_execute_plan_passes_authoritative_plan_to_pytest_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyrepo_check import pytest_execution

    pytest_plan = PytestExecutionPlan(
        pytest_args=(),
    )
    check = CheckInvocation(
        name="pytest",
        arguments=(),
        pytest=pytest_plan,
    )
    plan = RunPlan(
        root=tmp_path,
        repository_python=DefaultRepositoryPython(),
        mode="strict_aggregate",
        targets=(),
        checks=(check,),
    )

    def fake_execute_pytest(
        observed_check: CheckInvocation,
        *,
        plan: RunPlan | None,
        output_format: OutputFormat,
        runner: object,
        clock_ns: object,
    ) -> ExecutedCheck:
        del output_format, runner, clock_ns
        assert observed_check is check
        assert plan is not None
        assert plan is expected_plan
        return ExecutedCheck(planned=check, processes=())

    expected_plan = plan
    monkeypatch.setattr(pytest_execution, "execute_pytest", fake_execute_pytest)

    result = execute_plan(plan)

    assert result.checks == (ExecutedCheck(planned=check, processes=()),)


class _VirtualPipe:
    def __init__(
        self,
        total_bytes: int,
        byte: int,
        *,
        barrier: threading.Barrier | None = None,
        fail_after_reads: int | None = None,
        blocked_after_failure: threading.Event | None = None,
    ) -> None:
        self.total_bytes = total_bytes
        self.byte = byte
        self.barrier = barrier
        self.fail_after_reads = fail_after_reads
        self.blocked_after_failure = blocked_after_failure
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
        if self.blocked_after_failure is not None:
            self.blocked_after_failure.wait(timeout=2)
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
        if self.blocked_after_failure is not None:
            self.blocked_after_failure.set()


class _FakePopen:
    def __init__(
        self,
        stdout: _VirtualPipe,
        stderr: _VirtualPipe,
        *,
        returncode: int = 0,
        wait_release: threading.Event | None = None,
        cleanup_errors: bool = False,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.wait_release = wait_release
        self.cleanup_errors = cleanup_errors
        self.wait_calls: list[float | None] = []
        self.terminated = False
        self.killed = False

    def communicate(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("communicate must not buffer captured output")

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        effective_timeout = 2.0 if timeout is None else timeout
        if self.wait_release is not None and not self.wait_release.wait(timeout=effective_timeout):
            raise subprocess.TimeoutExpired(("fake",), effective_timeout)
        if self.cleanup_errors and timeout is not None:
            raise OSError("synthetic cleanup wait failure")
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if self.cleanup_errors:
            raise OSError("synthetic terminate failure")
        if self.wait_release is not None:
            self.wait_release.set()

    def kill(self) -> None:
        self.killed = True
        if self.cleanup_errors:
            raise OSError("synthetic kill failure")
        if self.wait_release is not None:
            self.wait_release.set()


def _install_fake_popen(
    monkeypatch: pytest.MonkeyPatch,
    processes: list[_FakePopen],
    *,
    capture_output: bool = True,
) -> None:
    remaining = iter(processes)

    def fake_popen(*_args: object, **kwargs: object) -> _FakePopen:
        expected_pipe = subprocess.PIPE if capture_output else None
        assert kwargs["stdout"] is expected_pipe
        assert kwargs["stderr"] is expected_pipe
        assert "text" not in kwargs and "encoding" not in kwargs
        return next(remaining)

    monkeypatch.setattr(execution.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        execution.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("subprocess.run must not buffer captured output")
        ),
    )


def test_production_capture_drains_both_pipes_and_retains_exact_raw_tails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout_size = 3 * 1024 * 1024 + 17
    stderr_size = 2 * 1024 * 1024 + 29
    source = (
        "import os\n"
        f"os.write(1, b'A' * {stdout_size - CAPTURE_LIMIT_BYTES} + "
        f"b'B' * {CAPTURE_LIMIT_BYTES})\n"
        f"os.write(2, b'C' * {stderr_size - CAPTURE_LIMIT_BYTES} + "
        f"b'D' * {CAPTURE_LIMIT_BYTES})\n"
    )

    monkeypatch.setattr(
        execution,
        "locked_module_command",
        lambda _plan, _check: (sys.executable, "-c", source),
    )
    result = execute_plan(make_python_check_plan(tmp_path, source), runner=None)

    process = result.checks[0].processes[0]
    assert process.returncode == 0
    assert process.stdout == CapturedBytes(
        tail=b"B" * CAPTURE_LIMIT_BYTES,
        omitted_bytes=stdout_size - CAPTURE_LIMIT_BYTES,
    )
    assert process.stderr == CapturedBytes(
        tail=b"D" * CAPTURE_LIMIT_BYTES,
        omitted_bytes=stderr_size - CAPTURE_LIMIT_BYTES,
    )


def test_capture_uses_concurrent_distinct_readers_and_exact_bounded_tails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = threading.Barrier(2)
    stdout = _VirtualPipe(CAPTURE_LIMIT_BYTES + 11, ord("o"), barrier=barrier)
    stderr = _VirtualPipe(CAPTURE_LIMIT_BYTES + 17, ord("e"), barrier=barrier)
    process = _FakePopen(stdout, stderr)
    _install_fake_popen(monkeypatch, [process])

    result = execute_plan(make_python_check_plan(tmp_path, "unused"), runner=None)

    captured = result.checks[0].processes[0]
    assert stdout.reader_ident not in {None, threading.get_ident()}
    assert stderr.reader_ident not in {None, threading.get_ident(), stdout.reader_ident}
    assert captured.stdout == CapturedBytes(b"o" * CAPTURE_LIMIT_BYTES, 11)
    assert captured.stderr == CapturedBytes(b"e" * CAPTURE_LIMIT_BYTES, 17)
    assert stdout.closed_by_reader and stderr.closed_by_reader


def test_capture_streams_virtual_multimegabyte_output_with_bounded_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    total_bytes = 24 * 1024 * 1024
    accumulators: list[execution._TailAccumulator] = []
    accumulator_type = execution._TailAccumulator

    def make_accumulator() -> execution._TailAccumulator:
        accumulator = accumulator_type()
        accumulators.append(accumulator)
        return accumulator

    process = _FakePopen(
        _VirtualPipe(total_bytes, ord("x")),
        _VirtualPipe(total_bytes, ord("y")),
    )
    _install_fake_popen(monkeypatch, [process])
    monkeypatch.setattr(execution, "_TailAccumulator", make_accumulator)

    tracemalloc.start()
    try:
        result = execute_plan(make_python_check_plan(tmp_path, "unused"), runner=None)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    captured = result.checks[0].processes[0]
    assert len(accumulators) == 2
    assert peak_bytes < 4 * 1024 * 1024
    assert captured.stdout == CapturedBytes(
        b"x" * CAPTURE_LIMIT_BYTES, total_bytes - CAPTURE_LIMIT_BYTES
    )
    assert captured.stderr == CapturedBytes(
        b"y" * CAPTURE_LIMIT_BYTES, total_bytes - CAPTURE_LIMIT_BYTES
    )


def test_stdout_drain_error_becomes_typed_process_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakePopen(
        _VirtualPipe(1, ord("x"), fail_after_reads=0),
        _VirtualPipe(0, ord("y")),
    )
    _install_fake_popen(monkeypatch, [process])

    result = execute_plan(make_python_check_plan(tmp_path, "unused"), runner=None)

    captured = result.checks[0].processes[0]
    assert captured.returncode is None
    assert captured.stdout is None and captured.stderr is None
    assert captured.spawn_error == "stdout drain failed: OSError: synthetic drain failure"
    assert result.exit_code == 2


def test_drain_error_aborts_before_blocking_wait_and_reaps_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wait_release = threading.Event()
    stderr = _VirtualPipe(
        32 * 1024 * 1024,
        ord("y"),
        blocked_after_failure=wait_release,
    )
    process = _FakePopen(
        _VirtualPipe(1, ord("x"), fail_after_reads=0),
        stderr,
        wait_release=wait_release,
    )
    _install_fake_popen(monkeypatch, [process])
    completed = threading.Event()
    result_holder: list[ExecutionResult] = []

    def invoke() -> None:
        result_holder.append(execute_plan(make_python_check_plan(tmp_path, "unused"), runner=None))
        completed.set()

    watchdog = threading.Thread(target=invoke)
    watchdog.start()
    try:
        assert completed.wait(timeout=1), "capture failure hung before child cleanup"
    finally:
        wait_release.set()
        watchdog.join(timeout=1)

    assert not watchdog.is_alive()
    assert process.terminated
    assert process.wait_calls and process.wait_calls[0] is not None
    assert result_holder[0].checks[0].processes[0].returncode is None


def test_reader_start_failure_returns_promptly_when_descendant_retains_pipe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready_path = tmp_path / "descendant-ready"
    pid_path = tmp_path / "descendant-pid"
    source = (
        "from pathlib import Path\n"
        "import subprocess, sys, time\n"
        f"pid_path = Path({str(pid_path)!r})\n"
        f"ready_path = Path({str(ready_path)!r})\n"
        "descendant = subprocess.Popen(\n"  # nosec B603
        "    [sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "    stdout=sys.stdout, stderr=sys.stderr,\n"
        ")\n"
        "pid_path.write_text(str(descendant.pid))\n"
        "ready_path.write_text('ready')\n"
        "time.sleep(30)\n"
    )
    real_thread = threading.Thread
    start_calls = 0

    class FailSecondStartThread(real_thread):
        def start(self) -> None:
            nonlocal start_calls
            start_calls += 1
            if start_calls == 2:
                raise RuntimeError("synthetic stderr reader start failure")
            super().start()
            deadline = execution.time.monotonic() + 2
            while not ready_path.exists() and execution.time.monotonic() < deadline:
                threading.Event().wait(0.01)
            assert ready_path.exists(), "child/descendant readiness handshake failed"

    monkeypatch.setattr(execution.threading, "Thread", FailSecondStartThread)
    monkeypatch.setattr(
        execution,
        "locked_module_command",
        lambda _plan, _check: (sys.executable, "-c", source),
    )
    completed = threading.Event()
    result_holder: list[ExecutionResult] = []

    def invoke() -> None:
        result_holder.append(execute_plan(make_python_check_plan(tmp_path, source), runner=None))
        completed.set()

    watchdog = real_thread(target=invoke, daemon=True)
    watchdog.start()
    returned_promptly = completed.wait(timeout=0.8)
    try:
        descendant_pid = int(pid_path.read_text())
        os.kill(descendant_pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except (FileNotFoundError, ProcessLookupError):
        pass
    watchdog.join(timeout=2)

    assert returned_promptly, "failure cleanup blocked on a pipe retained by a descendant"
    assert not watchdog.is_alive(), "descendant pipe probe could not be released"
    captured = result_holder[0].checks[0].processes[0]
    assert captured.spawn_error == (
        "stderr reader start failed: RuntimeError: synthetic stderr reader start failure"
    )


def test_second_reader_start_failure_closes_and_reaps_without_masking_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = _VirtualPipe(0, ord("x"))
    stderr = _VirtualPipe(0, ord("y"))
    process = _FakePopen(stdout, stderr, cleanup_errors=True)
    _install_fake_popen(monkeypatch, [process])
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

    result = execute_plan(make_python_check_plan(tmp_path, "unused"), runner=None)

    captured = result.checks[0].processes[0]
    assert captured.returncode is None
    assert captured.spawn_error == (
        "stderr reader start failed: RuntimeError: synthetic stderr reader start failure"
    )
    assert stdout.closed_by_reader
    assert stdout.close_idents == [stdout.reader_ident]
    assert not stderr.closed_by_reader
    assert stderr.close_idents == [threading.get_ident()]
    assert len(created_threads) == 2
    assert all(reader.daemon for reader in created_threads)
    assert not created_threads[0].is_alive()
    assert created_threads[1].ident is None
    assert process.terminated and process.killed


def test_second_reader_construction_failure_is_typed_and_reaps_first_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = _VirtualPipe(0, ord("x"))
    stderr = _VirtualPipe(0, ord("y"))
    process = _FakePopen(stdout, stderr)
    _install_fake_popen(monkeypatch, [process])
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

    result = execute_plan(make_python_check_plan(tmp_path, "unused"), runner=None)

    captured = result.checks[0].processes[0]
    assert captured.returncode is None
    assert captured.spawn_error == (
        "stderr reader construction failed: RuntimeError: "
        "synthetic stderr reader construction failure"
    )
    assert process.terminated
    assert stdout.closed_by_reader
    assert stdout.close_idents == [stdout.reader_ident]
    assert not stderr.closed_by_reader
    assert stderr.close_idents == [threading.get_ident()]


def test_cleanup_failures_return_with_only_daemon_blocked_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    reader_started = threading.Event()

    class BlockingPipe(_VirtualPipe):
        def readinto(self, buffer: bytearray | memoryview) -> int:
            del buffer
            if self.reader_ident is None:
                self.reader_ident = threading.get_ident()
                reader_started.set()
            release.wait()
            return 0

        def close(self) -> None:
            if threading.get_ident() != self.reader_ident:
                release.wait()
            super().close()

    blocked = BlockingPipe(0, ord("y"))
    process = _FakePopen(
        _VirtualPipe(0, ord("x"), fail_after_reads=0),
        blocked,
        cleanup_errors=True,
    )
    _install_fake_popen(monkeypatch, [process])
    real_thread = threading.Thread
    readers: list[threading.Thread] = []

    class RecordingThread(real_thread):
        def __init__(
            self,
            *,
            target: Callable[..., object] | None = None,
            args: tuple[object, ...] = (),
            name: str | None = None,
            daemon: bool | None = None,
        ) -> None:
            super().__init__(target=target, args=args, name=name, daemon=daemon)
            readers.append(self)

    monkeypatch.setattr(execution.threading, "Thread", RecordingThread)
    completed = threading.Event()
    result_holder: list[ExecutionResult] = []

    def invoke() -> None:
        result_holder.append(execute_plan(make_python_check_plan(tmp_path, "unused"), runner=None))
        completed.set()

    watchdog = real_thread(target=invoke, daemon=True)
    watchdog.start()
    assert reader_started.wait(timeout=1)
    returned_promptly = completed.wait(timeout=0.6)
    try:
        assert returned_promptly, "failure cleanup blocked on the reader-owned pipe lock"
        assert process.terminated and process.killed
        assert len(readers) == 2
        assert all(reader.daemon for reader in readers)
        assert readers[1].is_alive()
        assert blocked.close_idents == []
        assert result_holder[0].checks[0].processes[0].spawn_error == (
            "stdout drain failed: OSError: synthetic drain failure"
        )
    finally:
        release.set()
        watchdog.join(timeout=1)
        for reader in readers:
            reader.join(timeout=1)

    assert not watchdog.is_alive()
    assert all(not reader.is_alive() for reader in readers)


def test_daemon_blocked_reader_does_not_prevent_interpreter_shutdown() -> None:
    probe = inspect.cleandoc(
        """
        from pathlib import Path
        import threading

        from pyrepo_check import execution


        class ErrorPipe:
            def readinto(self, _buffer):
                raise OSError("synthetic drain failure")

            def close(self):
                return None


        class BlockingPipe:
            def readinto(self, _buffer):
                threading.Event().wait()
                return 0

            def close(self):
                threading.Event().wait()


        class FailedCleanupProcess:
            def __init__(self):
                self.stdout = ErrorPipe()
                self.stderr = BlockingPipe()

            def terminate(self):
                raise OSError("synthetic terminate failure")

            def kill(self):
                raise OSError("synthetic kill failure")

            def wait(self, timeout=None):
                del timeout
                raise OSError("synthetic wait failure")


        execution.subprocess.Popen = lambda *_args, **_kwargs: FailedCleanupProcess()
        try:
            execution._run_bounded_process(
                ("unused",),
                cwd=Path.cwd(),
                capture_output=True,
                environment=None,
            )
        except execution._ProcessExecutionFailure as error:
            assert str(error) == (
                "stdout drain failed: OSError: synthetic drain failure"
            )
        else:
            raise AssertionError("expected typed execution failure")
        print("finished", flush=True)
        """
    )

    completed = subprocess.run(  # nosec B603
        (sys.executable, "-c", probe),
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )

    assert completed.returncode == 0
    assert completed.stdout == "finished\n"
    assert completed.stderr == ""


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

    process = WaitFailurePopen(_VirtualPipe(0, ord("x")), _VirtualPipe(0, ord("y")))
    _install_fake_popen(monkeypatch, [process])

    result = execute_plan(make_python_check_plan(tmp_path, "unused"), runner=None)

    captured = result.checks[0].processes[0]
    assert captured.returncode is None
    assert captured.spawn_error == "wait failed: OSError: synthetic wait failure"
    assert process.terminated
    assert process.wait_calls[0] is None
    assert process.wait_calls[1] is not None
    assert 0 < process.wait_calls[1] <= execution._FAILURE_CLEANUP_TIMEOUT_SECONDS


@pytest.mark.parametrize("error_type", (OSError, RuntimeError))
def test_terminal_wait_failure_is_contained_and_later_check_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[OSError] | type[RuntimeError],
) -> None:
    class WaitFailurePopen(_FakePopen):
        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls.append(timeout)
            if timeout is None:
                raise error_type("synthetic terminal wait failure")
            return self.returncode

    failed = WaitFailurePopen(_VirtualPipe(0, ord("x")), _VirtualPipe(0, ord("y")))
    later = _FakePopen(_VirtualPipe(0, ord("x")), _VirtualPipe(0, ord("y")), returncode=7)
    _install_fake_popen(monkeypatch, [failed, later], capture_output=False)

    result = execute_plan(make_plan(tmp_path), runner=None)

    first, second = (check.processes[0] for check in result.checks)
    assert first.returncode is None
    assert first.spawn_error == (
        f"wait failed: {error_type.__name__}: synthetic terminal wait failure"
    )
    assert second.returncode == 7
    assert failed.terminated
    assert failed.wait_calls[0] is None
    assert failed.wait_calls[1] is not None
    assert 0 < failed.wait_calls[1] <= execution._FAILURE_CLEANUP_TIMEOUT_SECONDS
    assert result.exit_code == 7


def test_unexpected_reader_programming_error_cleans_up_then_reraises_by_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = ValueError("synthetic reader bug")

    class ProgrammingFailurePipe(_VirtualPipe):
        def readinto(self, buffer: bytearray | memoryview) -> int:
            del buffer
            self.reader_ident = threading.get_ident()
            raise error

    stdout = ProgrammingFailurePipe(0, ord("x"))
    stderr = _VirtualPipe(0, ord("y"))
    process = _FakePopen(stdout, stderr)
    _install_fake_popen(monkeypatch, [process])

    with pytest.raises(ValueError) as raised:
        execute_plan(make_python_check_plan(tmp_path, "unused"), runner=None)

    assert raised.value is error
    assert process.terminated
    assert stdout.closed_by_reader and stderr.closed_by_reader


def test_capture_failure_continues_and_later_positive_exit_still_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = _FakePopen(
        _VirtualPipe(1, ord("x"), fail_after_reads=0),
        _VirtualPipe(0, ord("y")),
    )
    later = _FakePopen(_VirtualPipe(0, ord("x")), _VirtualPipe(0, ord("y")), returncode=7)
    _install_fake_popen(monkeypatch, [failed, later])

    result = execute_plan(make_json_plan(tmp_path), runner=None)

    assert tuple(check.processes[0].returncode for check in result.checks) == (None, 7)
    assert result.checks[0].processes[0].spawn_error == (
        "stdout drain failed: OSError: synthetic drain failure"
    )
    assert result.exit_code == 7


def test_injected_buffered_runner_output_is_normalized_immediately(
    tmp_path: Path,
) -> None:
    stdout = b"prefix" + b"x" * CAPTURE_LIMIT_BYTES
    stderr = b"error"

    result = execute_plan(
        make_single_check_plan(tmp_path, output_format="json"),
        runner=RecordingRunner(stdout=(stdout,), stderr=(stderr,)),
    )

    process = result.checks[0].processes[0]
    assert process.stdout == CapturedBytes(
        tail=b"x" * CAPTURE_LIMIT_BYTES,
        omitted_bytes=len(b"prefix"),
    )
    assert process.stderr == CapturedBytes(tail=stderr, omitted_bytes=0)


def test_production_spawn_failure_and_signal_preserve_continuation_and_exit_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = CheckInvocation(
        name="ruff",
        arguments=("check",),
    )
    signaled = CheckInvocation(
        name="ty",
        arguments=("check",),
    )
    completed = CheckInvocation(
        name="bandit",
        arguments=("-c", "pyproject.toml"),
    )

    raw_commands = {
        "ruff": (str(tmp_path / "missing-executable"),),
        "ty": (sys.executable, "-c", f"import os; os.kill(os.getpid(), {signal.SIGTERM})"),
        "bandit": (sys.executable, "-c", "raise SystemExit(7)"),
    }
    monkeypatch.setattr(
        execution,
        "locked_module_command",
        lambda _plan, check: raw_commands[check.name],
    )
    result = execute_plan(
        RunPlan(
            root=tmp_path,
            repository_python=DefaultRepositoryPython(),
            mode="focused",
            targets=(),
            checks=(missing, signaled, completed),
            output_format="json",
        ),
        runner=None,
    )

    processes = tuple(check.processes[0] for check in result.checks)
    assert processes[0].returncode is None
    assert processes[0].spawn_error is not None
    assert processes[1].returncode == -signal.SIGTERM
    assert processes[2].returncode == 7
    assert result.exit_code == 7


def test_production_terminal_output_remains_inherited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    check = CheckInvocation(
        name="ruff",
        arguments=("check",),
    )

    monkeypatch.setattr(
        execution,
        "locked_module_command",
        lambda _plan, _check: (
            sys.executable,
            "-c",
            "import os; os.write(1, b'inherited-output\\n')",
        ),
    )
    result = execute_plan(
        RunPlan(
            root=tmp_path,
            repository_python=DefaultRepositoryPython(),
            mode="focused",
            targets=(),
            checks=(check,),
        ),
        runner=None,
    )

    process = result.checks[0].processes[0]
    captured = capfd.readouterr()
    assert process.returncode == 0
    assert process.stdout is None
    assert process.stderr is None
    assert "inherited-output\n" in captured.out


def test_zero_return_codes_produce_exit_zero(tmp_path: Path) -> None:
    result = execute_plan(make_plan(tmp_path), runner=RecordingRunner(returncodes=(0, 0)))

    assert isinstance(result, ExecutionResult)
    assert result.exit_code == 0
    assert tuple(process.returncode for check in result.checks for process in check.processes) == (
        0,
        0,
    )
    assert all(
        len(check.processes) == 1
        and check.pytest is None
        and isinstance(check.processes[0], ExecutedProcess)
        and check.processes[0].role == "primary"
        and check.processes[0].command
        == execution.locked_module_command(make_plan(tmp_path), check.planned)
        and check.processes[0].cwd == tmp_path
        for check in result.checks
    )


def test_terminal_commands_run_in_plan_order_with_exact_arguments(tmp_path: Path) -> None:
    runner = RecordingRunner(returncodes=(0, 0))

    execute_plan(make_plan(tmp_path), runner=runner)

    assert runner.calls == [
        runner.calls[0].__class__(
            command=("uv", "run", "--locked", "python", "-m", "ruff", "check", "src"),
            cwd=tmp_path,
            check=False,
            capture_output=False,
        ),
        runner.calls[1].__class__(
            command=("uv", "run", "--locked", "python", "-m", "ty", "check"),
            cwd=tmp_path,
            check=False,
            capture_output=False,
        ),
    ]


def test_terminal_banner_is_printed_before_spawn(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []

    def on_call(_call: object) -> None:
        events.append(capsys.readouterr().out)

    execute_plan(make_plan(tmp_path), runner=RecordingRunner(on_call=on_call))

    assert events == [
        "\n==> ruff: uv run --locked python -m ruff check src\n",
        "\n==> ty: uv run --locked python -m ty check\n",
    ]


def test_json_commands_capture_output_without_printing_banner(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = RecordingRunner(stdout=(b"first", "second"), stderr=(b"", b"error"))

    result = execute_plan(make_json_plan(tmp_path), runner=runner)

    assert [call.capture_output for call in runner.calls] == [True, True]
    assert capsys.readouterr().out == ""
    assert [(check.processes[0].stdout, check.processes[0].stderr) for check in result.checks] == [
        (CapturedBytes(b"first", 0), CapturedBytes(b"", 0)),
        (CapturedBytes(b"second", 0), CapturedBytes(b"error", 0)),
    ]


def test_terminal_observations_do_not_claim_uncaptured_output(tmp_path: Path) -> None:
    result = execute_plan(
        make_plan(tmp_path),
        runner=RecordingRunner(stdout=(b"ignored",), stderr=(b"ignored",)),
    )

    assert [(check.processes[0].stdout, check.processes[0].stderr) for check in result.checks] == [
        (None, None),
        (None, None),
    ]


@pytest.mark.parametrize(
    ("elapsed_ns", "duration_ms"),
    [(499_999, 0), (500_000, 1), (1_500_000, 2)],
)
def test_duration_rounds_to_nearest_millisecond(
    tmp_path: Path,
    elapsed_ns: int,
    duration_ms: int,
) -> None:
    clock_values = iter((1_000_000_000, 1_000_000_000 + elapsed_ns))

    result = execute_plan(
        make_single_check_plan(tmp_path),
        runner=RecordingRunner(),
        clock_ns=lambda: next(clock_values),
    )

    assert result.checks[0].processes[0].duration_ms == duration_ms


def test_backwards_clock_clamps_duration_to_zero(tmp_path: Path) -> None:
    clock_values = iter((1_500_000, 0))

    result = execute_plan(
        make_single_check_plan(tmp_path),
        runner=RecordingRunner(),
        clock_ns=lambda: next(clock_values),
    )

    assert result.checks[0].processes[0].duration_ms == 0


@pytest.mark.parametrize("output_format", ["terminal", "json"])
def test_start_clock_immediately_precedes_runner_invocation(
    tmp_path: Path,
    output_format: OutputFormat,
) -> None:
    call_lines: list[tuple[str, int]] = []
    clock_values = iter((0, 0))

    def clock_ns() -> int:
        frame = inspect.currentframe()
        assert frame is not None
        caller = frame.f_back
        assert caller is not None
        call_lines.append(("clock", caller.f_lineno))
        return next(clock_values)

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        del cwd, check
        frame = inspect.currentframe()
        assert frame is not None
        caller = frame.f_back
        assert caller is not None
        call_lines.append(("runner", caller.f_lineno))
        assert capture_output is (output_format == "json")
        return subprocess.CompletedProcess(command, returncode=0)

    execute_plan(
        make_single_check_plan(tmp_path, output_format=output_format),
        runner=runner,
        clock_ns=clock_ns,
    )

    assert [kind for kind, _ in call_lines] == ["clock", "runner", "clock"]
    assert call_lines[1][1] == call_lines[0][1] + 1


def test_ordinary_failures_continue_and_first_positive_nonzero_wins(tmp_path: Path) -> None:
    runner = RecordingRunner(returncodes=(3, 7))

    result = execute_plan(make_plan(tmp_path), runner=runner)

    assert result.exit_code == 3
    assert len(runner.calls) == 2


def test_negative_return_codes_are_recorded_and_later_checks_run(tmp_path: Path) -> None:
    runner = RecordingRunner(returncodes=(-9, 4))

    result = execute_plan(make_plan(tmp_path), runner=runner)

    assert tuple(process.returncode for check in result.checks for process in check.processes) == (
        -9,
        4,
    )
    assert result.exit_code == 4
    assert len(runner.calls) == 2


@pytest.mark.parametrize("error", [FileNotFoundError("missing"), PermissionError("denied")])
def test_spawn_errors_are_recorded_and_later_checks_run(
    tmp_path: Path,
    error: OSError,
) -> None:
    runner = RecordingRunner(raise_on_call=1, exception=error)
    clock_values = iter((0, 500_000, 1_000_000, 1_500_000))

    result = execute_plan(
        make_plan(tmp_path),
        runner=runner,
        clock_ns=lambda: next(clock_values),
    )

    assert result.checks[0].processes[0].returncode is None
    assert result.checks[0].processes[0].duration_ms == 1
    assert result.checks[0].processes[0].spawn_error == f"{type(error).__name__}: {error}"
    assert result.checks[1].processes[0].returncode == 0
    assert len(runner.calls) == 2


def test_injected_valueerror_is_propagated_by_identity(tmp_path: Path) -> None:
    error = ValueError("injected")
    runner = RecordingRunner(raise_on_call=1, exception=error)

    with pytest.raises(ValueError) as raised:
        execute_plan(make_plan(tmp_path), runner=runner)

    assert raised.value is error
