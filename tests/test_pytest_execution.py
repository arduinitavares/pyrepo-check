from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess  # nosec B404
import tempfile
import threading
from types import SimpleNamespace
from typing import Callable, Never, TypeVar, cast

import pytest

from pyrepo_check.execution import CapturedBytes, ExecutionResult, execute_plan
from pyrepo_check.planning import (
    DefaultRepositoryPython,
    OutputFormat,
    CheckInvocation,
    PytestExecutionPlan,
    RunPlan,
)
import pyrepo_check.execution_workspace as execution_workspace
import pyrepo_check.pytest_execution as pytest_execution
from pyrepo_check.pytest_execution import execute_pytest
from pyrepo_check.pytest_evidence import PytestValidationFailure, validate_pytest_execution
from pyrepo_check.reporting import build_run_report, validate_report_v1
from tests.support import RecordingRunner


_T = TypeVar("_T")
_OS_NONBLOCK = cast(int, getattr(os, "O_NONBLOCK"))
_OS_DIRECTORY = cast(int, getattr(os, "O_DIRECTORY"))
_OS_NOFOLLOW = cast(int, getattr(os, "O_NOFOLLOW"))
_MKFIFO = cast(Callable[[Path], None], getattr(os, "mkfifo"))


def _run_fifo_call_with_watchdog(call: Callable[[], _T], fifo: Path) -> _T:
    result: list[_T] = []
    errors: list[BaseException] = []
    completed = threading.Event()

    def invoke() -> None:
        try:
            result.append(call())
        except BaseException as error:
            errors.append(error)
        finally:
            completed.set()

    worker = threading.Thread(target=invoke)
    worker.start()
    returned_promptly = completed.wait(timeout=0.25)
    if not returned_promptly:
        writer = os.open(fifo, os.O_WRONLY | _OS_NONBLOCK)
        os.close(writer)
    worker.join(timeout=1)
    assert not worker.is_alive(), "FIFO evidence read could not be released"
    assert returned_promptly, "FIFO evidence read blocked instead of failing closed"
    if errors:
        raise errors[0]
    return result[0]


def test_stage_two_resource_limits_are_exact() -> None:
    assert pytest_execution._MAX_ARTIFACT_BYTES == 128 * 1024 * 1024
    assert pytest_execution._MAX_WRITER_MARKER_BYTES == 4 * 1024
    assert pytest_execution._MAX_JSON_NESTING == 64
    assert pytest_execution._MAX_WRITER_DIRECTORY_ENTRIES == 1024
    assert pytest_execution._READ_CHUNK_BYTES == 64 * 1024


def test_cleanup_resource_limits_are_exact() -> None:
    assert execution_workspace._MAX_CLEANUP_ENTRIES == 4096
    assert execution_workspace._MAX_CLEANUP_DEPTH == 64
    assert execution_workspace._MAX_CLEANUP_DURATION_NS == 5_000_000_000


def test_cleanup_budget_accepts_exact_boundaries_and_rejects_one_over() -> None:
    clock_values = iter((5_000_000_000, 5_000_000_001))
    budget = execution_workspace._CleanupBudget(started_ns=0, clock_ns=lambda: next(clock_values))

    for _ in range(4096):
        budget.observe_entry(depth=64)
    budget.check_deadline()

    with pytest.raises(execution_workspace._CleanupFailure) as entry_error:
        budget.observe_entry(depth=0)
    assert entry_error.value.kind == "budget_exceeded"
    assert entry_error.value.message == "cleanup entry limit exceeded (4096)"

    fresh_budget = execution_workspace._CleanupBudget(started_ns=0, clock_ns=lambda: 0)
    with pytest.raises(execution_workspace._CleanupFailure) as depth_error:
        fresh_budget.observe_entry(depth=65)
    assert depth_error.value.kind == "budget_exceeded"
    assert depth_error.value.message == "cleanup depth limit exceeded (64)"

    with pytest.raises(execution_workspace._CleanupFailure) as deadline_error:
        budget.check_deadline()
    assert deadline_error.value.kind == "budget_exceeded"
    assert deadline_error.value.message == "cleanup duration limit exceeded (5000000000 ns)"


def test_fifo_artifact_is_rejected_promptly_with_exact_diagnostic(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.json"
    _MKFIFO(artifact)

    observation = _run_fifo_call_with_watchdog(
        lambda: pytest_execution._snapshot_artifact(artifact, tmp_path),
        artifact,
    )

    assert observation.state == "unsafe_path"
    assert observation.content is None
    assert observation.diagnostic == "path is not a regular file: artifact.json"


def test_fifo_writer_marker_is_rejected_promptly_with_exact_diagnostic(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "pytest-writer-blocked.json"
    _MKFIFO(marker)

    writer_ids, diagnostic = _run_fifo_call_with_watchdog(
        lambda: pytest_execution._snapshot_writer_ids(tmp_path),
        marker,
    )

    assert writer_ids == ()
    assert diagnostic == (
        "writer marker is malformed: pytest-writer-blocked.json: "
        "path is not a regular file: pytest-writer-blocked.json"
    )


@pytest.mark.parametrize("constant", ("NaN", "Infinity", "-Infinity"))
def test_writer_marker_rejects_non_finite_ignored_metadata(
    tmp_path: Path,
    constant: str,
) -> None:
    marker = tmp_path / "pytest-writer-one.json"
    marker.write_text(f'{{"schema_version":1,"writer_id":"one","pid":1,"ignored":{constant}}}')

    writer_ids, diagnostic = pytest_execution._snapshot_writer_ids(tmp_path)

    assert writer_ids == ()
    assert diagnostic == (
        "writer marker is malformed: pytest-writer-one.json: "
        f"JSON constant {constant} is not permitted"
    )


@pytest.mark.parametrize("entry_count", (1024, 1025))
def test_writer_directory_entry_cap_is_exact(
    tmp_path: Path,
    entry_count: int,
) -> None:
    writer_directory = tmp_path / "writers"
    writer_directory.mkdir()
    for index in range(entry_count):
        (writer_directory / f"unrelated-{index}").touch()

    writer_ids, diagnostic = pytest_execution._snapshot_writer_ids(writer_directory)

    assert writer_ids == ()
    if entry_count == 1024:
        assert diagnostic is None
    else:
        assert diagnostic is not None
        assert "more than 1024 entries" in diagnostic


@pytest.mark.parametrize(
    "case",
    ("success", "entry-cap-early-return"),
)
def test_descriptor_relative_writer_snapshot_closes_inventory_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    writer_directory = tmp_path / "writers"
    writer_directory.mkdir()
    if case == "success":
        (writer_directory / "pytest-writer-one.json").write_text(
            '{"schema_version":1,"writer_id":"one","pid":1}'
        )
    else:
        for index in range(pytest_execution._MAX_WRITER_DIRECTORY_ENTRIES + 1):
            (writer_directory / f"unrelated-{index}").touch()

    original_open = os.open
    inventory_descriptors: list[int] = []
    run_descriptor = original_open(tmp_path, execution_workspace._secure_directory_open_flags())

    def capture_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if dir_fd == run_descriptor:
            inventory_descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(pytest_execution.os, "open", capture_open)
    try:
        writer_ids, diagnostic = pytest_execution._snapshot_writer_ids(
            writer_directory,
            run_descriptor=run_descriptor,
        )
        if case == "success":
            assert writer_ids == ("one",)
            assert diagnostic is None
        else:
            assert writer_ids == ()
            assert diagnostic == "writer directory contains more than 1024 entries"

        assert len(inventory_descriptors) == 1
        with pytest.raises(OSError):
            os.fstat(inventory_descriptors[0])
    finally:
        os.close(run_descriptor)


def test_second_writer_marker_stops_before_reading_any_later_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer_directory = tmp_path / "writers"
    writer_directory.mkdir()
    marker = b'{"schema_version":1,"writer_id":"%s","pid":1}'
    for writer_id in ("one", "two", "three"):
        (writer_directory / f"pytest-writer-{writer_id}.json").write_bytes(
            marker % writer_id.encode()
        )
    loads = 0
    original_load = pytest_execution._load_bounded_json

    def count_loads(content: bytes) -> object:
        nonlocal loads
        loads += 1
        return original_load(content)

    monkeypatch.setattr(pytest_execution, "_load_bounded_json", count_loads)

    writer_ids, diagnostic = pytest_execution._snapshot_writer_ids(writer_directory)

    assert len(writer_ids) == 1
    assert diagnostic == "multiple writer markers were found"
    assert loads == 1


def test_writer_scandir_iteration_error_retains_validated_writer_and_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "pytest-writer-one.json"
    marker.write_text('{"schema_version":1,"writer_id":"one","pid":1}')
    original_scandir = os.scandir
    with original_scandir(tmp_path) as iterator:
        entry = next(iterator)

    class FailingInventory:
        def __init__(self) -> None:
            self.yielded = False

        def __enter__(self) -> FailingInventory:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self) -> FailingInventory:
            return self

        def __next__(self) -> os.DirEntry[str]:
            if not self.yielded:
                self.yielded = True
                return entry
            raise PermissionError("synthetic writer iteration failure")

    monkeypatch.setattr(pytest_execution.os, "scandir", lambda _path: FailingInventory())

    writer_ids, diagnostic = pytest_execution._snapshot_writer_ids(tmp_path)

    assert writer_ids == ("one",)
    assert diagnostic == (
        "writer inventory failed after validated writer one: "
        "PermissionError: synthetic writer iteration failure"
    )


@pytest.mark.parametrize(
    "missing_capability",
    ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"),
)
def test_missing_platform_capability_fails_before_temp_or_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_capability: str,
) -> None:
    monkeypatch.setattr(pytest_execution.os, missing_capability, None)

    def forbid_temp(_root: Path) -> Never:
        raise AssertionError("temporary directory must not be created")

    def forbid_spawn(*_args: object, **_kwargs: object) -> Never:
        raise AssertionError("process must not spawn")

    monkeypatch.setattr(
        execution_workspace,
        "create_run_workspace",
        forbid_temp,
    )

    observation = execute_pytest(
        pytest_check(tmp_path),
        plan=pytest_run_plan(tmp_path),
        output_format="json",
        runner=forbid_spawn,
    )

    assert observation.processes == ()
    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == "not_started"
    assert "requires" in (observation.pytest.preflight.diagnostic or "")


@pytest.mark.parametrize(
    "capability",
    (
        "_SCANDIR_SUPPORTS_FD",
        "_OPEN_SUPPORTS_DIR_FD",
        "_STAT_SUPPORTS_DIR_FD",
        "_STAT_SUPPORTS_FOLLOW_SYMLINKS",
        "_UNLINK_SUPPORTS_DIR_FD",
        "_RMDIR_SUPPORTS_DIR_FD",
        "_MKDIR_SUPPORTS_DIR_FD",
        "_RENAME_SUPPORTS_DIR_FD",
        "_POST_RMDIR_UNLINK_PROOF",
    ),
)
def test_missing_descriptor_operation_fails_before_temp_or_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capability: str,
) -> None:
    monkeypatch.setattr(execution_workspace, capability, False)
    monkeypatch.setattr(
        execution_workspace,
        "create_run_workspace",
        lambda _root: (_ for _ in ()).throw(
            AssertionError("temporary directory must not be created")
        ),
    )

    observation = execute_pytest(
        pytest_check(tmp_path),
        plan=pytest_run_plan(tmp_path),
        output_format="json",
        runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("process must not spawn")
        ),
    )

    assert observation.processes == ()
    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == "not_started"
    assert observation.pytest.artifact.state == "not_attempted"


def test_preflight_rejects_a_normalized_omitted_tail_before_json_parse(
    tmp_path: Path,
) -> None:
    calls = 0

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        nonlocal calls
        del cwd, check, capture_output, env
        calls += 1
        return completed(
            command,
            0,
            stdout=b"x" + b" " * (65_536 - len(preflight_document())) + preflight_document(),
            stderr=b"",
        )

    observation = execute_pytest(
        pytest_check(tmp_path), plan=pytest_run_plan(tmp_path), output_format="json", runner=runner
    )

    assert calls == 1
    assert observation.processes[0].stdout == CapturedBytes(
        tail=b" " * (65_536 - len(preflight_document())) + preflight_document(),
        omitted_bytes=1,
    )
    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == "preflight_invalid"


def pytest_check(tmp_path: Path) -> CheckInvocation:
    pytest = PytestExecutionPlan(pytest_args=("tests",))
    return CheckInvocation(
        name="pytest",
        arguments=pytest.pytest_args,
        pytest=pytest,
    )


def pytest_run_plan(tmp_path: Path, check: CheckInvocation | None = None) -> RunPlan:
    invocation = pytest_check(tmp_path) if check is None else check
    return RunPlan(
        root=tmp_path,
        repository_python=DefaultRepositoryPython(),
        mode="focused",
        targets=(),
        checks=(invocation,),
        output_format="json",
    )


def preflight_document(
    *,
    python_version: object = [3, 13, 15],
    pytest_available: object = True,
    pytest_version: object = [8, 4, 2],
    schema_version: object = 1,
) -> bytes:
    return json.dumps(
        {
            "schema_version": schema_version,
            "python_version": python_version,
            "pytest_available": pytest_available,
            "pytest_version": pytest_version,
        },
        separators=(",", ":"),
    ).encode()


def completed(
    command: tuple[str, ...],
    returncode: int,
    *,
    stdout: bytes | None = None,
    stderr: bytes | None = None,
) -> subprocess.CompletedProcess[tuple[str, ...]]:
    return cast(
        subprocess.CompletedProcess[tuple[str, ...]],
        subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr),
    )


def safe_run_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    run_directory = Path(tempfile.mkdtemp(prefix="pyrepo-check-test-"))
    assert not run_directory.is_relative_to(tmp_path)
    created_run_directory = execution_workspace.RunWorkspace(
        run_directory,
        execution_workspace._directory_identity(run_directory),
        execution_workspace._directory_identity(run_directory.parent),
    )
    monkeypatch.setattr(
        execution_workspace,
        "create_run_workspace",
        lambda _consumer_root: created_run_directory,
    )
    return run_directory


def _cleanup_record(run_directory: Path) -> execution_workspace.RunWorkspace:
    return execution_workspace.RunWorkspace(
        run_directory,
        execution_workspace._directory_identity(run_directory),
        execution_workspace._directory_identity(run_directory.parent),
    )


@pytest.mark.parametrize(
    ("stdout", "returncode", "error", "classification"),
    [
        (preflight_document(), 0, None, "supported"),
        (preflight_document(python_version=[3, 13, 14]), 0, None, "unsupported_python"),
        (
            preflight_document(pytest_available=False, pytest_version=None),
            0,
            None,
            "module_unavailable",
        ),
        (preflight_document(pytest_version=[7, 4, 4]), 0, None, "unsupported_version"),
        (preflight_document(pytest_version=[9, 0, 0]), 0, None, "unsupported_version"),
        (preflight_document() + b"\\nextra", 0, None, "preflight_invalid"),
        (b"not-json", 0, None, "preflight_invalid"),
        (preflight_document(python_version="3.13.15"), 0, None, "preflight_invalid"),
        (preflight_document(schema_version=2), 0, None, "preflight_invalid"),
        (b"\\xff", 0, None, "preflight_invalid"),
        (b"x" * 65_537, 0, None, "preflight_invalid"),
        (preflight_document(), 1, None, "preflight_invalid"),
        (preflight_document(), -9, None, "terminated_by_signal"),
        (b"", 0, FileNotFoundError("consumer-python"), "spawn_failed"),
    ],
    ids=(
        "supported",
        "unsupported-python",
        "missing-pytest",
        "pytest-7",
        "pytest-9",
        "extra-output",
        "malformed-json",
        "wrong-types",
        "wrong-schema",
        "invalid-utf8",
        "oversized",
        "nonzero",
        "signal",
        "spawn-failure",
    ),
)
def test_preflight_classification_stops_before_plugin_on_non_supported_result(
    tmp_path: Path,
    stdout: bytes,
    returncode: int,
    error: OSError | None,
    classification: str,
) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        del cwd, check, env
        calls.append(command)
        assert capture_output
        if error is not None:
            raise error
        return completed(command, returncode, stdout=stdout, stderr=b"")

    observation = execute_pytest(
        pytest_check(tmp_path), plan=pytest_run_plan(tmp_path), output_format="json", runner=runner
    )

    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == classification
    assert observation.processes[0].role == "pytest_preflight"
    if classification != "supported":
        assert all(process.role != "primary" for process in observation.processes)
    assert len(calls) == (2 if classification == "supported" else 1)


def test_supported_preflight_records_typed_version_data(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        del cwd, check, env
        calls.append(command)
        assert capture_output
        return completed(
            command,
            0,
            stdout=preflight_document(),
            stderr=b"",
        )

    observation = execute_pytest(
        pytest_check(tmp_path), plan=pytest_run_plan(tmp_path), output_format="json", runner=runner
    )

    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == "supported"
    assert observation.pytest.preflight.record is not None
    assert observation.pytest.preflight.record.python_version == (3, 13, 15)
    assert observation.pytest.preflight.record.pytest_available is True
    assert observation.pytest.preflight.record.pytest_version == (8, 4, 2)


@pytest.mark.parametrize("schema_version", [True, 1.0], ids=("boolean", "float"))
def test_preflight_rejects_non_integer_schema_version(
    tmp_path: Path,
    schema_version: object,
) -> None:
    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        del cwd, check, env
        assert capture_output
        return completed(command, 0, stdout=preflight_document(schema_version=schema_version))

    observation = execute_pytest(
        pytest_check(tmp_path), plan=pytest_run_plan(tmp_path), output_format="json", runner=runner
    )

    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == "preflight_invalid"
    assert len(observation.processes) == 1


def test_preflight_rejects_oversized_stderr(tmp_path: Path) -> None:
    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        del cwd, check, env
        assert capture_output
        return completed(
            command,
            0,
            stdout=preflight_document(),
            stderr=b"x" * 65_537,
        )

    observation = execute_pytest(
        pytest_check(tmp_path), plan=pytest_run_plan(tmp_path), output_format="json", runner=runner
    )

    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == "preflight_invalid"
    assert len(observation.processes) == 1


@pytest.mark.parametrize(
    ("output_format", "primary_capture"), [("json", True), ("terminal", False)]
)
def test_supported_preflight_launches_isolated_primary_from_planner_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    output_format: OutputFormat,
    primary_capture: bool,
) -> None:
    monkeypatch.setenv("PYTHONPATH", "consumer-path")
    monkeypatch.setenv("COVERAGE_PROCESS_CONFIG", "consumer-coverage-config")
    monkeypatch.setenv("COVERAGE_PROCESS_START", "consumer-coverage")
    monkeypatch.setenv("COV_CORE_SOURCE", "consumer-source")
    calls: list[tuple[tuple[str, ...], Path, bool, dict[str, str] | None]] = []

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        del check
        calls.append((command, cwd, capture_output, env))
        if "pytest" in command:
            assert env is not None
            plugin_name = command[command.index("-p") + 1]
            run_directory = Path(env["PYREPO_CHECK_PYTEST_JSON"]).parent
            assert plugin_name.isidentifier()
            assert plugin_name != "pyrepo_check_pytest_evidence_plugin"
            assert (run_directory / f"{plugin_name}.py").is_file()
        stdout = preflight_document() if len(calls) == 1 else b"primary-output"
        return completed(command, 0, stdout=stdout, stderr=b"")

    check = pytest_check(tmp_path)
    result = execute_plan(
        RunPlan(
            root=tmp_path,
            repository_python=DefaultRepositoryPython(),
            mode="focused",
            targets=(),
            checks=(check,),
            output_format=output_format,
        ),
        runner=runner,
    )

    plugin_name = calls[1][0][calls[1][0].index("-p") + 1]
    assert [call[0] for call in calls] == [
        ("uv", "run", "--locked", "python", "-c", calls[0][0][-1]),
        ("uv", "run", "--locked", "python", "-m", "pytest", "-p", plugin_name, "tests"),
    ]
    assert [call[1] for call in calls] == [tmp_path, tmp_path]
    assert [call[2] for call in calls] == [True, primary_capture]
    assert all(call[3] is not None for call in calls)
    environments = [call[3] for call in calls]
    assert all(environment is not None for environment in environments)
    assert all(
        "COVERAGE_PROCESS_CONFIG" not in environment
        and "COVERAGE_PROCESS_START" not in environment
        and "COV_CORE_SOURCE" not in environment
        for environment in environments
        if environment is not None
    )
    environment = calls[1][3]
    assert environment is not None
    assert environment["PYTHONPATH"].split(":")[0] == "consumer-path"
    assert not Path(environment["PYREPO_CHECK_PYTEST_JSON"]).is_relative_to(tmp_path)
    assert not Path(environment["PYREPO_CHECK_PYTEST_WRITER_DIR"]).is_relative_to(tmp_path)
    assert [process.role for process in result.checks[0].processes] == [
        "pytest_preflight",
        "primary",
    ]
    assert result.checks[0].pytest is not None
    assert result.checks[0].pytest.preflight.classification == "supported"
    expected_banner = "\n==> pytest: uv run --locked python -m pytest tests\n"
    assert capsys.readouterr().out == ("" if output_format == "json" else expected_banner)


def test_plugin_module_name_is_fresh_for_each_pytest_execution(tmp_path: Path) -> None:
    plugin_names: list[str] = []

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        del cwd, check, capture_output, env
        if "pytest" in command:
            plugin_names.append(command[command.index("-p") + 1])
            return completed(command, 0, stdout=b"", stderr=b"")
        return completed(command, 0, stdout=preflight_document(), stderr=b"")

    execute_pytest(
        pytest_check(tmp_path), plan=pytest_run_plan(tmp_path), output_format="json", runner=runner
    )
    execute_pytest(
        pytest_check(tmp_path), plan=pytest_run_plan(tmp_path), output_format="json", runner=runner
    )

    assert len(plugin_names) == 2
    assert plugin_names[0] != plugin_names[1]
    assert all(name.isidentifier() for name in plugin_names)
    assert "pyrepo_check_pytest_evidence_plugin" not in plugin_names


def test_duplicate_simulated_primaries_fail_closed_with_multiple_writers(
    tmp_path: Path,
) -> None:
    recording_runner = RecordingRunner(publish_pytest_artifact=True)

    def duplicate_primary_runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        completed_process = recording_runner(
            command,
            cwd=cwd,
            check=check,
            capture_output=capture_output,
            env=env,
        )
        if "pytest" in command:
            recording_runner(
                command,
                cwd=cwd,
                check=check,
                capture_output=capture_output,
                env=env,
            )
        return completed_process

    observation = execute_pytest(
        pytest_check(tmp_path),
        plan=pytest_run_plan(tmp_path),
        output_format="json",
        runner=duplicate_primary_runner,
    )
    validation = validate_pytest_execution(observation)

    assert observation.pytest is not None
    assert len(observation.pytest.artifact.writer_ids) == 1
    assert observation.pytest.artifact.diagnostic == "multiple writer markers were found"
    assert isinstance(validation, PytestValidationFailure)
    assert validation.code == "artifact_invalid"


def test_preflight_runs_without_primary_when_consumer_is_unsupported(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        del cwd, check, env
        calls.append(command)
        assert capture_output
        return completed(
            command,
            0,
            stdout=preflight_document(python_version=[3, 13, 14]),
            stderr=b"",
        )

    result = execute_plan(
        RunPlan(
            root=tmp_path,
            repository_python=DefaultRepositoryPython(),
            mode="focused",
            targets=(),
            checks=(pytest_check(tmp_path),),
        ),
        runner=runner,
    )

    assert len(calls) == 1
    assert [process.role for process in result.checks[0].processes] == ["pytest_preflight"]


def test_primary_artifact_and_sorted_writer_snapshot_are_retained_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = safe_run_directory(tmp_path, monkeypatch)

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        del cwd, check, capture_output
        if env is not None and "pytest" in command:
            artifact_path = Path(env["PYREPO_CHECK_PYTEST_JSON"])
            writer_directory = Path(env["PYREPO_CHECK_PYTEST_WRITER_DIR"])
            artifact_path.write_bytes(b'{"raw":true}')
            (writer_directory / "pytest-writer-z.json").write_text(
                '{"schema_version":1,"writer_id":"z","pid":2}'
            )
            (writer_directory / "pytest-writer-a.json").write_text(
                '{"schema_version":1,"writer_id":"a","pid":1}'
            )
        return completed(
            command,
            0,
            stdout=preflight_document() if "-c" in command else b"",
            stderr=b"",
        )

    observation = execute_pytest(
        pytest_check(tmp_path), plan=pytest_run_plan(tmp_path), output_format="json", runner=runner
    )

    assert observation.pytest is not None
    assert observation.pytest.artifact.state == "snapshot"
    assert observation.pytest.artifact.content == b'{"raw":true}'
    assert observation.pytest.artifact.writer_ids in {("a",), ("z",)}
    assert observation.pytest.artifact.diagnostic == "multiple writer markers were found"
    assert observation.pytest.cleanup_error is None
    assert not run_directory.exists()


@pytest.mark.parametrize(
    ("marker_payloads", "writer_ids", "diagnostic"),
    [
        ({}, (), None),
        (
            {"pytest-writer-one.json": ('{"schema_version":1,"writer_id":"one","pid":1}')},
            ("one",),
            None,
        ),
        (
            {
                "pytest-writer-b.json": ('{"schema_version":1,"writer_id":"b","pid":2}'),
                "pytest-writer-a.json": ('{"schema_version":1,"writer_id":"a","pid":1}'),
            },
            ("a", "b"),
            "multiple writer markers",
        ),
        ({"pytest-writer-bad.json": "not-json"}, (), "malformed"),
        (
            {"pytest-writer-one.json": ('{"schema_version":1,"writer_id":"other","pid":1}')},
            (),
            "ID mismatch",
        ),
    ],
    ids=("zero", "one", "multiple", "malformed", "mismatched-id"),
)
def test_writer_inventory_records_only_regular_valid_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker_payloads: dict[str, str],
    writer_ids: tuple[str, ...],
    diagnostic: str | None,
) -> None:
    safe_run_directory(tmp_path, monkeypatch)

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        del cwd, check, capture_output
        if env is not None and "pytest" in command:
            Path(env["PYREPO_CHECK_PYTEST_JSON"]).write_bytes(b"artifact")
            writer_directory = Path(env["PYREPO_CHECK_PYTEST_WRITER_DIR"])
            for name, payload in marker_payloads.items():
                (writer_directory / name).write_text(payload)
        return completed(
            command,
            0,
            stdout=preflight_document() if "-c" in command else b"",
            stderr=b"",
        )

    observation = execute_pytest(
        pytest_check(tmp_path), plan=pytest_run_plan(tmp_path), output_format="json", runner=runner
    )

    assert observation.pytest is not None
    assert observation.pytest.artifact.state == "snapshot"
    if len(writer_ids) == 2:
        assert observation.pytest.artifact.writer_ids in {(writer_ids[0],), (writer_ids[1],)}
    else:
        assert observation.pytest.artifact.writer_ids == writer_ids
    if diagnostic is None:
        assert observation.pytest.artifact.diagnostic is None
    else:
        assert diagnostic in (observation.pytest.artifact.diagnostic or "")


@pytest.mark.parametrize(
    "payload",
    (
        "[]",
        '{"writer_id":"one","pid":1}',
        '{"schema_version":2,"writer_id":"one","pid":1}',
        '{"schema_version":true,"writer_id":"one","pid":1}',
        '{"schema_version":1,"writer_id":1,"pid":1}',
        '{"schema_version":1,"writer_id":"other","pid":1}',
        '{"schema_version":1,"writer_id":"one"}',
        '{"schema_version":1,"writer_id":"one","pid":true}',
        '{"schema_version":1,"writer_id":"one","pid":-1}',
    ),
    ids=(
        "not-object",
        "missing-version",
        "wrong-version",
        "boolean-version",
        "non-string-id",
        "identity-mismatch",
        "missing-pid",
        "boolean-pid",
        "negative-pid",
    ),
)
def test_writer_inventory_rejects_malformed_marker_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
) -> None:
    safe_run_directory(tmp_path, monkeypatch)

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        del cwd, check, capture_output
        if env is not None and "pytest" in command:
            Path(env["PYREPO_CHECK_PYTEST_JSON"]).write_bytes(b"artifact")
            writer_directory = Path(env["PYREPO_CHECK_PYTEST_WRITER_DIR"])
            (writer_directory / "pytest-writer-one.json").write_text(payload)
        return completed(
            command,
            0,
            stdout=preflight_document() if "-c" in command else b"",
            stderr=b"",
        )

    observation = execute_pytest(
        pytest_check(tmp_path), plan=pytest_run_plan(tmp_path), output_format="json", runner=runner
    )

    assert observation.pytest is not None
    assert observation.pytest.artifact.writer_ids == ()
    assert "writer marker" in (observation.pytest.artifact.diagnostic or "")


@pytest.mark.parametrize(
    ("primary_returncode", "make_artifact", "spawn", "state"),
    [
        (0, False, False, "missing"),
        (-9, False, False, "missing"),
        (0, True, False, "unsafe_path"),
        (0, False, True, "missing"),
    ],
    ids=("missing", "signal", "symlink", "spawn-failure"),
)
def test_artifact_snapshot_handles_missing_signal_and_unsafe_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primary_returncode: int,
    make_artifact: bool,
    spawn: bool,
    state: str,
) -> None:
    run_directory = safe_run_directory(tmp_path, monkeypatch)
    outside_artifact = tmp_path / "outside-artifact"
    outside_artifact.write_bytes(b"outside")

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        del cwd, check, capture_output
        if env is not None and "pytest" in command and make_artifact:
            Path(env["PYREPO_CHECK_PYTEST_JSON"]).symlink_to(outside_artifact)
        if env is not None and "pytest" in command and spawn:
            raise FileNotFoundError("consumer-python")
        return completed(
            command,
            0 if "-c" in command else primary_returncode,
            stdout=preflight_document() if "-c" in command else b"",
            stderr=b"",
        )

    observation = execute_pytest(
        pytest_check(tmp_path), plan=pytest_run_plan(tmp_path), output_format="json", runner=runner
    )

    assert observation.pytest is not None
    assert observation.pytest.artifact.state == state
    assert observation.pytest.artifact.content is None
    assert observation.pytest.cleanup_error is None
    assert not run_directory.exists()


def test_artifact_read_failure_is_observed_and_cleanup_still_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = safe_run_directory(tmp_path, monkeypatch)
    original_open = pytest_execution.os.open

    def deny_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        *args: int,
        **kwargs: int | None,
    ) -> int:
        if kwargs.get("dir_fd") is not None and os.fsdecode(path) == "artifact.json":
            raise PermissionError("artifact denied")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(pytest_execution.os, "open", deny_open)

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        del cwd, check, capture_output
        if env is not None and "pytest" in command:
            Path(env["PYREPO_CHECK_PYTEST_JSON"]).write_bytes(b"artifact")
        return completed(
            command,
            0,
            stdout=preflight_document() if "-c" in command else b"",
            stderr=b"",
        )

    observation = execute_pytest(
        pytest_check(tmp_path), plan=pytest_run_plan(tmp_path), output_format="json", runner=runner
    )

    assert observation.pytest is not None
    assert observation.pytest.artifact.state == "read_failed"
    assert observation.pytest.artifact.content is None
    assert not run_directory.exists()


def test_artifact_snapshot_reads_open_descriptor_when_path_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe_run_directory(tmp_path, monkeypatch)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"attacker-content")
    open_calls: list[Path] = []
    original_open = pytest_execution.os.open
    artifact_path: Path | None = None

    def replacement_race(
        path: str | bytes | os.PathLike[str],
        flags: int,
        *args: int,
        **kwargs: int | None,
    ) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)
        if kwargs.get("dir_fd") is not None and os.fsdecode(path) == "artifact.json":
            assert artifact_path is not None
            target = artifact_path
            open_calls.append(target)
            target.unlink()
            target.symlink_to(replacement)
        return descriptor

    monkeypatch.setattr(pytest_execution.os, "open", replacement_race)

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        nonlocal artifact_path
        del cwd, check, capture_output
        if env is not None and "pytest" in command:
            artifact_path = Path(env["PYREPO_CHECK_PYTEST_JSON"])
            artifact_path.write_bytes(b"captured-content")
        return completed(
            command,
            0,
            stdout=preflight_document() if "-c" in command else b"",
        )

    observation = execute_pytest(
        pytest_check(tmp_path), plan=pytest_run_plan(tmp_path), output_format="json", runner=runner
    )

    assert open_calls
    assert observation.pytest is not None
    assert observation.pytest.artifact.state == "snapshot"
    assert observation.pytest.artifact.content == b"captured-content"


def test_writer_snapshot_reads_open_descriptor_when_marker_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe_run_directory(tmp_path, monkeypatch)
    replacement = tmp_path / "replacement"
    replacement.write_text('{"schema_version":1,"writer_id":"attacker","pid":999}')
    open_calls: list[Path] = []
    original_open = pytest_execution.os.open
    marker_path: Path | None = None

    def replacement_race(
        path: str | bytes | os.PathLike[str],
        flags: int,
        *args: int,
        **kwargs: int | None,
    ) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)
        if kwargs.get("dir_fd") is not None and os.fsdecode(path) == "pytest-writer-safe.json":
            assert marker_path is not None
            target = marker_path
            open_calls.append(target)
            target.unlink()
            target.symlink_to(replacement)
        return descriptor

    monkeypatch.setattr(pytest_execution.os, "open", replacement_race)

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        nonlocal marker_path
        del cwd, check, capture_output
        if env is not None and "pytest" in command:
            Path(env["PYREPO_CHECK_PYTEST_JSON"]).write_bytes(b"artifact")
            writer_directory = Path(env["PYREPO_CHECK_PYTEST_WRITER_DIR"])
            marker_path = writer_directory / "pytest-writer-safe.json"
            marker_path.write_text('{"schema_version":1,"writer_id":"safe","pid":1}')
        return completed(
            command,
            0,
            stdout=preflight_document() if "-c" in command else b"",
        )

    observation = execute_pytest(
        pytest_check(tmp_path), plan=pytest_run_plan(tmp_path), output_format="json", runner=runner
    )

    assert open_calls
    assert observation.pytest is not None
    assert observation.pytest.artifact.state == "snapshot"
    assert observation.pytest.artifact.writer_ids == ("safe",)


def test_cleanup_failure_is_observed_without_losing_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = safe_run_directory(tmp_path, monkeypatch)

    def failing_cleanup(
        run_directory: execution_workspace.RunWorkspace,
        *,
        repository_root: Path,
        clock_ns: Callable[[], int],
    ) -> None:
        del run_directory, repository_root, clock_ns
        raise PermissionError("cleanup denied")

    monkeypatch.setattr(execution_workspace, "remove_run_workspace", failing_cleanup)

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        del cwd, check, capture_output
        if env is not None and "pytest" in command:
            Path(env["PYREPO_CHECK_PYTEST_JSON"]).write_bytes(b"artifact")
        return completed(
            command,
            0,
            stdout=preflight_document() if "-c" in command else b"",
            stderr=b"",
        )

    try:
        observation = execute_pytest(
            pytest_check(tmp_path),
            plan=pytest_run_plan(tmp_path),
            output_format="json",
            runner=runner,
        )
    finally:
        shutil.rmtree(run_directory, ignore_errors=True)

    assert observation.pytest is not None
    assert observation.pytest.artifact.content == b"artifact"
    assert observation.pytest.cleanup_error == "PermissionError: cleanup denied"


def test_setup_failure_is_typed_not_started_and_the_created_run_directory_is_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = safe_run_directory(tmp_path, monkeypatch)

    def fail_copy(
        source: Path,
        destination_name: str,
        *,
        run_descriptor: int,
    ) -> None:
        del source, destination_name, run_descriptor
        raise PermissionError("plugin copy denied")

    monkeypatch.setattr(pytest_execution, "_copy_plugin_source", fail_copy)

    observation = execute_pytest(
        pytest_check(tmp_path), plan=pytest_run_plan(tmp_path), output_format="json"
    )

    assert observation.processes == ()
    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == "not_started"
    assert observation.pytest.artifact.state == "not_attempted"
    assert not run_directory.exists()


def test_plugin_preparation_completes_partial_descriptor_writes(
    tmp_path: Path,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    verified = execution_workspace.open_verified_workspace(_cleanup_record(run_directory))
    original_write = pytest_execution.os.write

    def partial_write(descriptor: int, content: bytes) -> int:
        return original_write(descriptor, content[: max(1, len(content) // 3)])

    try:
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(pytest_execution.os, "write", partial_write)
            _artifact_path, writer_directory = pytest_execution._prepare_run_directory(
                verified,
                "_pyrepo_check_pytest_partial_write",
            )
        plugin = run_directory / "_pyrepo_check_pytest_partial_write.py"
        assert (
            plugin.read_bytes()
            == Path(pytest_execution.__file__).with_name("_pytest_report_plugin.py").read_bytes()
        )
        assert stat.S_IMODE(plugin.stat().st_mode) == 0o600
        assert writer_directory.is_dir()
        assert stat.S_IMODE(writer_directory.stat().st_mode) == 0o700
    finally:
        verified.close()
        shutil.rmtree(run_directory, ignore_errors=True)


@pytest.mark.parametrize("setup_failure", (False, True), ids=("success", "failure"))
def test_verified_run_descriptors_close_on_all_execution_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup_failure: bool,
) -> None:
    run_directory = safe_run_directory(tmp_path, monkeypatch)
    original_open_verified = execution_workspace.open_verified_workspace
    original_close = pytest_execution.os.close
    verified_descriptors: set[int] = set()
    closed: set[int] = set()

    def track_open_verified(
        record: execution_workspace.RunWorkspace,
    ) -> execution_workspace.VerifiedRunWorkspace:
        verified = original_open_verified(record)
        verified_descriptors.update({verified.parent_descriptor, verified.descriptor})
        return verified

    def track_close(descriptor: int) -> None:
        closed.add(descriptor)
        original_close(descriptor)

    def fail_copy(
        source: Path,
        destination_name: str,
        *,
        run_descriptor: int,
    ) -> Never:
        del source, destination_name, run_descriptor
        raise PermissionError("plugin copy denied")

    monkeypatch.setattr(
        execution_workspace,
        "open_verified_workspace",
        track_open_verified,
    )
    monkeypatch.setattr(pytest_execution.os, "close", track_close)
    if setup_failure:
        monkeypatch.setattr(pytest_execution, "_copy_plugin_source", fail_copy)

    observation = execute_pytest(
        pytest_check(tmp_path),
        plan=pytest_run_plan(tmp_path),
        output_format="json",
        runner=lambda command, **_kwargs: completed(
            command,
            0,
            stdout=preflight_document(
                pytest_available=False,
                pytest_version=None,
            ),
        ),
    )

    assert verified_descriptors
    assert verified_descriptors <= closed
    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == (
        "not_started" if setup_failure else "module_unavailable"
    )
    assert not run_directory.exists()


def test_run_swap_after_create_stops_before_preparation_or_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = safe_run_directory(tmp_path, monkeypatch)
    record = _cleanup_record(run_directory)
    displaced = run_directory.with_name(f"{run_directory.name}-displaced")
    replacement_sentinel = run_directory / "replacement-sentinel"
    runner_calls: list[tuple[str, ...]] = []

    def create_then_swap(_consumer_root: Path) -> execution_workspace.RunWorkspace:
        run_directory.rename(displaced)
        run_directory.mkdir()
        replacement_sentinel.write_text("keep")
        return record

    def runner(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        runner_calls.append(command)
        return completed(command, 0, stdout=preflight_document())

    monkeypatch.setattr(execution_workspace, "create_run_workspace", create_then_swap)
    try:
        observation = execute_pytest(
            pytest_check(tmp_path),
            plan=pytest_run_plan(tmp_path),
            output_format="json",
            runner=runner,
        )
        assert replacement_sentinel.read_text() == "keep"
        assert tuple(path.name for path in run_directory.iterdir()) == ("replacement-sentinel",)
    finally:
        shutil.rmtree(run_directory, ignore_errors=True)
        shutil.rmtree(displaced, ignore_errors=True)

    assert runner_calls == []
    assert observation.processes == ()
    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == "not_started"
    assert observation.pytest.preflight.diagnostic == (
        "OSError: run directory identity mismatch before preparation"
    )
    assert observation.pytest.artifact.state == "not_attempted"


def test_run_swap_during_preparation_stays_fd_bound_and_post_gate_stops_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = safe_run_directory(tmp_path, monkeypatch)
    displaced = run_directory.with_name(f"{run_directory.name}-displaced")
    replacement_sentinel = run_directory / "replacement-sentinel"
    original_write = pytest_execution.os.write
    swapped = False
    runner_calls: list[tuple[str, ...]] = []

    def swap_on_first_plugin_write(descriptor: int, content: bytes) -> int:
        nonlocal swapped
        if not swapped:
            run_directory.rename(displaced)
            run_directory.mkdir()
            replacement_sentinel.write_text("keep")
            swapped = True
        return original_write(descriptor, content)

    def runner(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        runner_calls.append(command)
        return completed(command, 0, stdout=preflight_document())

    monkeypatch.setattr(pytest_execution.os, "write", swap_on_first_plugin_write)
    try:
        observation = execute_pytest(
            pytest_check(tmp_path),
            plan=pytest_run_plan(tmp_path),
            output_format="json",
            runner=runner,
        )
        assert replacement_sentinel.read_text() == "keep"
        assert tuple(path.name for path in run_directory.iterdir()) == ("replacement-sentinel",)
        assert any(path.name.startswith("_pyrepo_check_pytest_") for path in displaced.iterdir())
        assert (displaced / "writers").is_dir()
    finally:
        shutil.rmtree(run_directory, ignore_errors=True)
        shutil.rmtree(displaced, ignore_errors=True)

    assert swapped
    assert runner_calls == []
    assert observation.processes == ()
    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == "not_started"
    assert observation.pytest.preflight.diagnostic == (
        "OSError: run directory identity mismatch after preparation"
    )


def test_run_swap_before_preflight_stops_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = safe_run_directory(tmp_path, monkeypatch)
    displaced = run_directory.with_name(f"{run_directory.name}-displaced")
    replacement_sentinel = run_directory / "replacement-sentinel"
    original_environment = pytest_execution._isolated_environment
    runner_calls: list[tuple[str, ...]] = []

    def environment_then_swap(
        run_path: Path,
        artifact_path: Path,
        writer_directory: Path,
    ) -> dict[str, str]:
        environment = original_environment(run_path, artifact_path, writer_directory)
        run_directory.rename(displaced)
        run_directory.mkdir()
        replacement_sentinel.write_text("keep")
        return environment

    def runner(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        runner_calls.append(command)
        return completed(command, 0, stdout=preflight_document())

    monkeypatch.setattr(pytest_execution, "_isolated_environment", environment_then_swap)
    try:
        observation = execute_pytest(
            pytest_check(tmp_path),
            plan=pytest_run_plan(tmp_path),
            output_format="json",
            runner=runner,
        )
        assert replacement_sentinel.read_text() == "keep"
    finally:
        shutil.rmtree(run_directory, ignore_errors=True)
        shutil.rmtree(displaced, ignore_errors=True)

    assert runner_calls == []
    assert observation.processes == ()
    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == "not_started"
    assert observation.pytest.preflight.diagnostic == (
        "OSError: run directory identity mismatch immediately before preflight"
    )


def test_run_swap_after_supported_preflight_retains_real_process_and_stops_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = safe_run_directory(tmp_path, monkeypatch)
    displaced = run_directory.with_name(f"{run_directory.name}-displaced")
    replacement_sentinel = run_directory / "replacement-sentinel"
    calls = 0

    def runner(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            run_directory.rename(displaced)
            run_directory.mkdir()
            replacement_sentinel.write_text("keep")
        return completed(command, 0, stdout=preflight_document())

    try:
        observation = execute_pytest(
            pytest_check(tmp_path),
            plan=pytest_run_plan(tmp_path),
            output_format="json",
            runner=runner,
        )
        assert replacement_sentinel.read_text() == "keep"
    finally:
        shutil.rmtree(run_directory, ignore_errors=True)
        shutil.rmtree(displaced, ignore_errors=True)

    assert calls == 1
    assert len(observation.processes) == 1
    assert observation.processes[0].role == "pytest_preflight"
    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == "preflight_invalid"
    assert observation.pytest.preflight.record is None
    assert observation.pytest.preflight.diagnostic == (
        "run directory identity mismatch after pytest preflight"
    )
    validation = validate_pytest_execution(observation)
    assert isinstance(validation, PytestValidationFailure)
    assert validation.code == "preflight_invalid"
    plan = RunPlan(
        root=tmp_path,
        repository_python=DefaultRepositoryPython(),
        mode="focused",
        targets=("tests",),
        checks=(observation.planned,),
        output_format="json",
        pytest_args=("tests",),
        planned_test_scope="partial",
    )
    report = build_run_report(
        tmp_path,
        plan,
        ExecutionResult((observation,), 2),
    )
    validate_report_v1(report)


def test_run_swap_after_unsupported_preflight_still_applies_identity_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = safe_run_directory(tmp_path, monkeypatch)
    displaced = run_directory.with_name(f"{run_directory.name}-displaced")

    def runner(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        run_directory.rename(displaced)
        run_directory.mkdir()
        return completed(
            command,
            0,
            stdout=preflight_document(pytest_version=[9, 0, 0]),
        )

    try:
        observation = execute_pytest(
            pytest_check(tmp_path),
            plan=pytest_run_plan(tmp_path),
            output_format="json",
            runner=runner,
        )
    finally:
        shutil.rmtree(run_directory, ignore_errors=True)
        shutil.rmtree(displaced, ignore_errors=True)

    assert len(observation.processes) == 1
    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == "preflight_invalid"
    assert observation.pytest.preflight.record is None
    assert observation.pytest.preflight.diagnostic == (
        "run directory identity mismatch after pytest preflight"
    )


def test_run_swap_inside_primary_retains_processes_without_snapshotting_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = safe_run_directory(tmp_path, monkeypatch)
    displaced = run_directory.with_name(f"{run_directory.name}-displaced")
    replacement_artifact = run_directory / "artifact.json"
    calls = 0

    def runner(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        nonlocal calls
        calls += 1
        if calls == 2:
            run_directory.rename(displaced)
            run_directory.mkdir()
            replacement_artifact.write_bytes(b'"untrusted replacement"')
        return completed(
            command,
            0,
            stdout=preflight_document() if calls == 1 else b"",
        )

    try:
        observation = execute_pytest(
            pytest_check(tmp_path),
            plan=pytest_run_plan(tmp_path),
            output_format="json",
            runner=runner,
        )
        assert replacement_artifact.read_bytes() == b'"untrusted replacement"'
    finally:
        shutil.rmtree(run_directory, ignore_errors=True)
        shutil.rmtree(displaced, ignore_errors=True)

    assert calls == 2
    assert [process.role for process in observation.processes] == [
        "pytest_preflight",
        "primary",
    ]
    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == "supported"
    assert observation.pytest.artifact.state == "unsafe_path"
    assert observation.pytest.artifact.content is None
    assert observation.pytest.artifact.diagnostic == (
        "run directory identity mismatch after pytest primary"
    )
    assert observation.pytest.cleanup_error is not None
    validation = validate_pytest_execution(observation)
    assert isinstance(validation, PytestValidationFailure)
    assert validation.code == "artifact_invalid"
    plan = RunPlan(
        root=tmp_path,
        repository_python=DefaultRepositoryPython(),
        mode="focused",
        targets=("tests",),
        checks=(observation.planned,),
        output_format="json",
        pytest_args=("tests",),
        planned_test_scope="partial",
    )
    report = build_run_report(
        tmp_path,
        plan,
        ExecutionResult((observation,), 2),
    )
    validate_report_v1(report)


def test_snapshot_uses_held_run_descriptor_after_run_basename_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = safe_run_directory(tmp_path, monkeypatch)
    displaced = run_directory.with_name(f"{run_directory.name}-displaced")
    original_snapshot = pytest_execution._snapshot_artifact
    trusted_content: bytes | None = None

    def swap_run_at_snapshot_start(
        artifact_path: Path,
        writer_directory: Path,
        *,
        run_descriptor: int | None = None,
    ) -> pytest_execution.PytestArtifactObservation:
        nonlocal trusted_content
        trusted_content = artifact_path.read_bytes()
        trusted_document = json.loads(trusted_content)
        trusted_document["forged"] = True
        marker_path = next(writer_directory.glob("pytest-writer-*.json"))
        marker_content = marker_path.read_bytes()

        run_directory.rename(displaced)
        run_directory.mkdir()
        replacement_writer_directory = run_directory / "writers"
        replacement_writer_directory.mkdir()
        (run_directory / "artifact.json").write_text(
            json.dumps(trusted_document, separators=(",", ":")),
            encoding="utf-8",
        )
        (replacement_writer_directory / marker_path.name).write_bytes(marker_content)

        if run_descriptor is None:
            return original_snapshot(artifact_path, writer_directory)
        return original_snapshot(
            artifact_path,
            writer_directory,
            run_descriptor=run_descriptor,
        )

    monkeypatch.setattr(pytest_execution, "_snapshot_artifact", swap_run_at_snapshot_start)

    try:
        observation = execute_pytest(
            pytest_check(tmp_path),
            plan=pytest_run_plan(tmp_path),
            output_format="json",
            runner=RecordingRunner(publish_pytest_artifact=True),
        )
    finally:
        shutil.rmtree(run_directory, ignore_errors=True)
        shutil.rmtree(displaced, ignore_errors=True)

    assert trusted_content is not None
    assert [process.role for process in observation.processes] == [
        "pytest_preflight",
        "primary",
    ]
    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == "supported"
    assert observation.pytest.artifact.state == "snapshot"
    assert observation.pytest.artifact.content == trusted_content
    assert observation.pytest.cleanup_error is not None
    assert "identity mismatch" in observation.pytest.cleanup_error

    plan = RunPlan(
        root=tmp_path,
        repository_python=DefaultRepositoryPython(),
        mode="focused",
        targets=("tests",),
        checks=(observation.planned,),
        output_format="json",
        pytest_args=("tests",),
        planned_test_scope="partial",
    )
    report = build_run_report(tmp_path, plan, ExecutionResult((observation,), 0))
    assert report.pytest is not None
    assert report.pytest.status == "passed"
    assert report.pytest.complete is True
    assert report.pytest.evidence is not None
    assert report.pytest.error is None
    assert report.checks[0].error is not None
    assert report.checks[0].error.code == "cleanup_failed"
    assert report.complete is False


def test_consumer_tmpdir_is_not_used_for_the_run_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(execution_workspace.tempfile, "gettempdir", lambda: str(tmp_path))
    run_directories: list[Path] = []

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        del cwd, check, capture_output
        if env is not None:
            run_directories.append(Path(env["PYREPO_CHECK_PYTEST_JSON"]).parent)
        return completed(
            command,
            0,
            stdout=preflight_document() if "-c" in command else b"",
        )

    observation = execute_pytest(
        pytest_check(tmp_path), plan=pytest_run_plan(tmp_path), output_format="json", runner=runner
    )

    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == "supported"
    assert run_directories
    assert all(not directory.is_relative_to(tmp_path) for directory in run_directories)


def test_run_directory_creation_failure_is_typed_not_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_mkdtemp(**_kwargs: object) -> str:
        raise PermissionError("temporary directory denied")

    monkeypatch.setattr(execution_workspace.tempfile, "mkdtemp", fail_mkdtemp)

    observation = execute_pytest(
        pytest_check(tmp_path), plan=pytest_run_plan(tmp_path), output_format="json"
    )

    assert observation.processes == ()
    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == "not_started"
    assert observation.pytest.preflight.diagnostic == "PermissionError: temporary directory denied"
    assert observation.pytest.artifact.state == "not_attempted"


def test_parent_identity_failure_precedes_mkdtemp_and_leaves_no_run_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_parents = {
        Path(tempfile.gettempdir()).resolve(strict=True),
        Path("/tmp").resolve(strict=True),  # nosec B108
        Path("/var/tmp").resolve(strict=True),  # nosec B108
    }
    original_identity = execution_workspace._directory_identity
    original_mkdtemp = tempfile.mkdtemp
    created_directories: list[Path] = []

    def deny_candidate_parent_identity(directory: Path) -> tuple[int, int]:
        if directory.resolve(strict=True) in candidate_parents:
            raise PermissionError("candidate parent identity denied")
        return original_identity(directory)

    def track_mkdtemp(
        *,
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | os.PathLike[str] | None = None,
    ) -> str:
        directory = Path(original_mkdtemp(suffix=suffix, prefix=prefix, dir=dir))
        created_directories.append(directory)
        return str(directory)

    monkeypatch.setattr(execution_workspace, "_directory_identity", deny_candidate_parent_identity)
    monkeypatch.setattr(execution_workspace.tempfile, "mkdtemp", track_mkdtemp)

    try:
        with pytest.raises(PermissionError, match="candidate parent identity denied"):
            execution_workspace.create_run_workspace(Path.cwd())
    finally:
        for directory in created_directories:
            if directory.exists():
                directory.rmdir()

    assert created_directories == []


def test_parent_replacement_after_mkdtemp_stops_before_preparation_and_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    temp_parent = tmp_path / "temp-parent"
    temp_parent.mkdir()
    displaced_parent = tmp_path / "displaced-temp-parent"
    original_mkdtemp = tempfile.mkdtemp
    original_prepare = pytest_execution._prepare_run_directory
    prepared: list[Path] = []
    runner_calls: list[tuple[str, ...]] = []
    replacement_run_directories: list[Path] = []

    def swap_parent_after_creation(
        *,
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | os.PathLike[str] | None = None,
    ) -> str:
        created = Path(original_mkdtemp(suffix=suffix, prefix=prefix, dir=dir))
        temp_parent.rename(displaced_parent)
        temp_parent.mkdir()
        replacement = temp_parent / created.name
        replacement.mkdir()
        replacement_run_directories.append(replacement)
        return str(replacement)

    def track_prepare(
        verified_run: execution_workspace.VerifiedRunWorkspace,
        plugin_module: str,
    ) -> tuple[Path, Path]:
        prepared.append(verified_run.workspace.path)
        return original_prepare(verified_run, plugin_module)

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        del cwd, check, capture_output, env
        runner_calls.append(command)
        return completed(command, 0, stdout=preflight_document())

    monkeypatch.setattr(execution_workspace.tempfile, "gettempdir", lambda: str(temp_parent))
    monkeypatch.setattr(execution_workspace.tempfile, "mkdtemp", swap_parent_after_creation)
    monkeypatch.setattr(pytest_execution, "_prepare_run_directory", track_prepare)

    try:
        observation = execute_pytest(
            pytest_check(consumer_root),
            plan=pytest_run_plan(consumer_root),
            output_format="json",
            runner=runner,
        )
        assert replacement_run_directories
        assert all(directory.exists() for directory in replacement_run_directories)
    finally:
        shutil.rmtree(temp_parent, ignore_errors=True)
        shutil.rmtree(displaced_parent, ignore_errors=True)

    assert prepared == []
    assert runner_calls == []
    assert observation.processes == ()
    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == "not_started"
    assert observation.pytest.preflight.diagnostic == (
        "OSError: created run directory parent identity mismatch; cleanup failed: "
        "_CleanupFailure: created run directory parent identity mismatch"
    )


def test_empty_created_cleanup_reports_swap_during_rmdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    identity = execution_workspace._directory_identity(run_directory)
    parent_identity = execution_workspace._directory_identity(tmp_path)
    displaced = tmp_path / "displaced-run"
    original_rmdir = pytest_execution.os.rmdir
    swapped = False

    def swap_during_rmdir(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if os.fsdecode(path) == run_directory.name and not swapped:
            run_directory.rename(displaced)
            run_directory.mkdir()
            swapped = True
        original_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(pytest_execution.os, "rmdir", swap_during_rmdir)

    cleanup_error = execution_workspace._remove_empty_created_run_directory(
        run_directory,
        identity,
        parent_identity,
    )

    assert swapped
    assert isinstance(cleanup_error, execution_workspace._CleanupFailure)
    assert cleanup_error.kind == "unsafe_tree"
    assert cleanup_error.message == f"directory remained linked after removal: {run_directory.name}"
    assert displaced.exists()


def test_darwin_getpath_live_identity_mismatch_does_not_prove_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = tmp_path / "opened"
    opened.mkdir()
    different_live_target = tmp_path / "different-live-target"
    different_live_target.mkdir()
    descriptor = os.open(opened, _OS_DIRECTORY | os.O_RDONLY)

    def get_path(_descriptor: int, _command: int, _buffer: bytes) -> bytes:
        return os.fsencode(different_live_target) + b"\0"

    monkeypatch.setattr(
        execution_workspace,
        "_fcntl",
        SimpleNamespace(fcntl=get_path, F_GETPATH=50),
    )
    try:
        remains_linked = execution_workspace._opened_directory_remains_linked(descriptor)
    finally:
        os.close(descriptor)

    assert remains_linked is True


def test_darwin_getpath_same_identity_does_not_prove_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = tmp_path / "opened"
    opened.mkdir()
    descriptor = os.open(opened, _OS_DIRECTORY | os.O_RDONLY)

    def get_path(_descriptor: int, _command: int, _buffer: bytes) -> bytes:
        return os.fsencode(opened) + b"\0"

    monkeypatch.setattr(
        execution_workspace,
        "_fcntl",
        SimpleNamespace(fcntl=get_path, F_GETPATH=50),
    )
    try:
        remains_linked = execution_workspace._opened_directory_remains_linked(descriptor)
    finally:
        os.close(descriptor)

    assert remains_linked is True


def test_darwin_getpath_missing_target_proves_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = tmp_path / "opened"
    opened.mkdir()
    missing = tmp_path / "missing"
    descriptor = os.open(opened, _OS_DIRECTORY | os.O_RDONLY)

    def get_path(_descriptor: int, _command: int, _buffer: bytes) -> bytes:
        return os.fsencode(missing) + b"\0"

    monkeypatch.setattr(
        execution_workspace,
        "_fcntl",
        SimpleNamespace(fcntl=get_path, F_GETPATH=50),
    )
    try:
        remains_linked = execution_workspace._opened_directory_remains_linked(descriptor)
    finally:
        os.close(descriptor)

    assert remains_linked is False


def test_darwin_getpath_empty_target_does_not_prove_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = tmp_path / "opened"
    opened.mkdir()
    descriptor = os.open(opened, _OS_DIRECTORY | os.O_RDONLY)

    def get_path(_descriptor: int, _command: int, _buffer: bytes) -> bytes:
        return b"\0"

    monkeypatch.setattr(
        execution_workspace,
        "_fcntl",
        SimpleNamespace(fcntl=get_path, F_GETPATH=50),
    )
    try:
        remains_linked = execution_workspace._opened_directory_remains_linked(descriptor)
    finally:
        os.close(descriptor)

    assert remains_linked is True


def test_rejected_consumer_root_run_directories_are_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_directories: list[Path] = []
    original_mkdtemp = tempfile.mkdtemp
    original_is_within = execution_workspace._is_within

    def create_candidate(
        *,
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | os.PathLike[str] | None = None,
    ) -> str:
        run_directory = Path(original_mkdtemp(suffix=suffix, prefix=prefix, dir=dir))
        created_directories.append(run_directory)
        return str(run_directory)

    def reject_created(directory: Path, root: Path) -> bool:
        return directory in created_directories or original_is_within(directory, root)

    monkeypatch.setattr(execution_workspace.tempfile, "mkdtemp", create_candidate)
    monkeypatch.setattr(execution_workspace, "_is_within", reject_created)

    with pytest.raises(OSError, match="inside consumer root"):
        execution_workspace.create_run_workspace(tmp_path)

    assert created_directories
    assert all(not directory.exists() for directory in created_directories)


def test_rejected_run_directory_cleanup_failure_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_directories: list[Path] = []
    original_mkdtemp = tempfile.mkdtemp
    original_rmdir = pytest_execution.os.rmdir
    original_is_within = execution_workspace._is_within

    def create_candidate(
        *,
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | os.PathLike[str] | None = None,
    ) -> str:
        run_directory = Path(original_mkdtemp(suffix=suffix, prefix=prefix, dir=dir))
        created_directories.append(run_directory)
        return str(run_directory)

    def reject_created(directory: Path, root: Path) -> bool:
        return directory in created_directories or original_is_within(directory, root)

    def deny_rmdir(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
    ) -> None:
        if Path(os.fsdecode(path)).name.startswith("pyrepo-check-pytest-"):
            raise PermissionError("run directory cleanup denied")
        original_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(execution_workspace.tempfile, "mkdtemp", create_candidate)
    monkeypatch.setattr(execution_workspace, "_is_within", reject_created)
    monkeypatch.setattr(pytest_execution.os, "rmdir", deny_rmdir)

    with pytest.raises(
        OSError, match="cleanup failed: PermissionError: run directory cleanup denied"
    ):
        execution_workspace.create_run_workspace(tmp_path)

    for directory in created_directories:
        original_rmdir(directory)


def test_rejected_run_directory_cleanup_failure_stops_before_later_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_directories: list[Path] = []
    original_mkdtemp = tempfile.mkdtemp
    original_rmdir = pytest_execution.os.rmdir
    original_is_within = execution_workspace._is_within

    def create_candidate(
        *,
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | os.PathLike[str] | None = None,
    ) -> str:
        run_directory = Path(original_mkdtemp(suffix=suffix, prefix=prefix, dir=dir))
        created_directories.append(run_directory)
        return str(run_directory)

    def reject_created(directory: Path, root: Path) -> bool:
        return directory in created_directories or original_is_within(directory, root)

    def deny_rejected_rmdir(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
    ) -> None:
        if Path(os.fsdecode(path)).name == created_directories[0].name:
            raise PermissionError("rejected candidate cleanup denied")
        original_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(execution_workspace.tempfile, "mkdtemp", create_candidate)
    monkeypatch.setattr(execution_workspace, "_is_within", reject_created)
    monkeypatch.setattr(pytest_execution.os, "rmdir", deny_rejected_rmdir)

    try:
        with pytest.raises(
            OSError,
            match="cleanup failed: PermissionError: rejected candidate cleanup denied",
        ):
            execution_workspace.create_run_workspace(tmp_path)
    finally:
        for directory in created_directories:
            if directory.exists():
                original_rmdir(directory)

    assert len(created_directories) == 1


def test_open_verified_parent_closes_descriptor_when_identity_check_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = tmp_path / "run-directory"
    run_directory.mkdir()
    record = execution_workspace.RunWorkspace(
        run_directory,
        execution_workspace._directory_identity(run_directory),
        execution_workspace._directory_identity(run_directory.parent),
    )
    opened: list[int] = []
    closed: list[int] = []
    original_open = pytest_execution.os.open
    original_close = pytest_execution.os.close

    def tracked_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        *args: int,
        **kwargs: int | None,
    ) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def deny_fstat(_descriptor: int) -> os.stat_result:
        raise PermissionError("directory identity denied")

    def tracked_close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(pytest_execution.os, "open", tracked_open)
    monkeypatch.setattr(pytest_execution.os, "fstat", deny_fstat)
    monkeypatch.setattr(pytest_execution.os, "close", tracked_close)

    try:
        with pytest.raises(PermissionError, match="directory identity denied"):
            execution_workspace._open_verified_parent(record)
    finally:
        for descriptor in opened:
            if descriptor not in closed:
                original_close(descriptor)
        run_directory.rmdir()

    assert len(opened) == 1
    assert closed == opened


def test_cleanup_does_not_delete_replaced_run_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = safe_run_directory(tmp_path, monkeypatch)
    replacement_file = run_directory / "replacement"

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        del cwd, check, capture_output
        if env is not None and "pytest" in command:
            shutil.rmtree(run_directory)
            run_directory.mkdir()
            replacement_file.write_text("do not delete")
        return completed(
            command,
            0,
            stdout=preflight_document() if "-c" in command else b"",
        )

    try:
        observation = execute_pytest(
            pytest_check(tmp_path),
            plan=pytest_run_plan(tmp_path),
            output_format="json",
            runner=runner,
        )
        assert replacement_file.exists()
    finally:
        shutil.rmtree(run_directory, ignore_errors=True)

    assert observation.pytest is not None
    assert observation.pytest.cleanup_error is not None
    assert "identity mismatch" in observation.pytest.cleanup_error


def test_cleanup_does_not_traverse_replacement_after_identity_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = safe_run_directory(tmp_path, monkeypatch)
    displaced_directory = tmp_path / "displaced-run-directory"
    replacement_file = run_directory / "replacement"
    original_walk = execution_workspace._walk_cleanup_tree
    replaced = False

    def replace_after_validation(
        parent_descriptor: int,
        root_name: str,
        root_identity: tuple[int, int],
        *,
        budget: execution_workspace._CleanupBudget,
        delete: bool,
        manifest: execution_workspace._CleanupManifest | None = None,
    ) -> execution_workspace._CleanupManifest:
        nonlocal replaced
        result = original_walk(
            parent_descriptor,
            root_name,
            root_identity,
            budget=budget,
            delete=delete,
            manifest=manifest,
        )
        if not delete and not replaced:
            run_directory.rename(displaced_directory)
            run_directory.mkdir()
            replacement_file.write_text("do not delete")
            replaced = True
        return result

    monkeypatch.setattr(
        execution_workspace,
        "_walk_cleanup_tree",
        replace_after_validation,
    )

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        del cwd, check, capture_output, env
        return completed(
            command,
            0,
            stdout=preflight_document() if "-c" in command else b"",
        )

    try:
        observation = execute_pytest(
            pytest_check(tmp_path),
            plan=pytest_run_plan(tmp_path),
            output_format="json",
            runner=runner,
        )
        assert replaced
        assert replacement_file.exists()
    finally:
        shutil.rmtree(run_directory, ignore_errors=True)
        shutil.rmtree(displaced_directory, ignore_errors=True)

    assert observation.pytest is not None
    assert observation.pytest.cleanup_error is not None
    assert "identity mismatch" in observation.pytest.cleanup_error


def test_cleanup_preserves_inner_descriptor_relative_deletion_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = safe_run_directory(tmp_path, monkeypatch)
    original_unlink = pytest_execution.os.unlink
    original_rmdir = pytest_execution.os.rmdir
    descriptor_relative_failure = False
    top_level_removals: list[Path] = []

    def deny_quarantine_unlink(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal descriptor_relative_failure
        filename = os.fsdecode(path)
        if filename.startswith("leaf-"):
            descriptor_relative_failure = dir_fd is not None
            raise PermissionError("quarantine deletion denied")
        original_unlink(path, dir_fd=dir_fd)

    def track_rmdir(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
    ) -> None:
        if dir_fd is None:
            top_level_removals.append(Path(os.fsdecode(path)))
        original_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(pytest_execution.os, "unlink", deny_quarantine_unlink)
    monkeypatch.setattr(pytest_execution.os, "rmdir", track_rmdir)

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        del cwd, check, capture_output, env
        return completed(
            command,
            0,
            stdout=preflight_document() if "-c" in command else b"",
        )

    try:
        observation = execute_pytest(
            pytest_check(tmp_path),
            plan=pytest_run_plan(tmp_path),
            output_format="json",
            runner=runner,
        )
    finally:
        monkeypatch.setattr(pytest_execution.os, "unlink", original_unlink)
        monkeypatch.setattr(pytest_execution.os, "rmdir", original_rmdir)
        shutil.rmtree(run_directory, ignore_errors=True)

    assert descriptor_relative_failure
    assert run_directory not in top_level_removals
    assert observation.pytest is not None
    assert observation.pytest.cleanup_error is not None
    assert observation.pytest.cleanup_error.startswith(
        f"PermissionError: quarantine deletion denied; retained run path: {run_directory}; "
        "retained quarantine path: "
    )
