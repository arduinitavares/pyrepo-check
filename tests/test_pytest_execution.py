from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess  # nosec B404
import threading
from types import MappingProxyType
from typing import Callable, TypeVar, cast

import pytest

from pyrepo_check.check_launcher import stage_check_launcher
from pyrepo_check.coverage_evidence import build_coverage_result
from pyrepo_check.execution import (
    DependencyObservation,
    ExecutedCheck,
    PreparedRepositoryEnvironment,
    ProcessRunner,
)
from pyrepo_check.planning import (
    CoverageExecutionPlan,
    CheckInvocation,
    DefaultRepositoryPython,
    PytestExecutionPlan,
    RunPlan,
)
import pyrepo_check.execution_workspace as execution_workspace
import pyrepo_check.pytest_execution as pytest_execution
from pyrepo_check.pytest_evidence import (
    build_pytest_result,
)
import tests.support as test_support
from tests.support import (
    available_dependency,
    launcher_aware_runner,
    missing_dependency,
    monotonic_clock,
    prepared_repository,
    test_workspace,
)


_T = TypeVar("_T")
_OS_NONBLOCK = cast(int, getattr(os, "O_NONBLOCK"))
_OS_DIRECTORY = cast(int, getattr(os, "O_DIRECTORY"))
_OS_NOFOLLOW = cast(int, getattr(os, "O_NOFOLLOW"))
_MKFIFO = cast(Callable[[Path], None], getattr(os, "mkfifo"))


def run_prepared_pytest_fixture(
    *,
    prepared: PreparedRepositoryEnvironment,
    pytest_dependency: DependencyObservation,
    coverage_dependency: DependencyObservation | None,
    runner: ProcessRunner,
    coverage_requested: bool = False,
) -> pytest_execution.PreparedPytestExecution:
    coverage = (
        CoverageExecutionPlan(
            config_path=prepared.root / "pyproject.toml",
            fail_under=None,
        )
        if coverage_requested
        else None
    )
    pytest_plan = PytestExecutionPlan(pytest_args=("tests",), coverage=coverage)
    check = CheckInvocation(
        name="pytest",
        arguments=pytest_plan.pytest_args,
        pytest=pytest_plan,
    )
    plan = RunPlan(
        root=prepared.root,
        repository_python=DefaultRepositoryPython(),
        mode="focused",
        targets=(),
        checks=(check,),
        output_format="json",
        planned_coverage_scope="partial" if coverage_requested else "not_requested",
    )
    with test_workspace(prepared.root) as workspace:
        launcher = stage_check_launcher(workspace)
        return pytest_execution.execute_prepared_pytest(
            check,
            plan=plan,
            prepared=prepared,
            pytest_dependency=pytest_dependency,
            coverage_dependency=coverage_dependency,
            workspace=workspace,
            launcher=launcher,
            output_format="json",
            runner=runner,
            clock_ns=monotonic_clock(),
        )


def prepared_executed_check(
    prepared: PreparedRepositoryEnvironment,
    result: pytest_execution.PreparedPytestExecution,
    *,
    coverage_requested: bool,
) -> tuple[RunPlan, ExecutedCheck]:
    coverage = (
        CoverageExecutionPlan(
            config_path=prepared.root / "pyproject.toml",
            fail_under=None,
        )
        if coverage_requested
        else None
    )
    pytest_plan = PytestExecutionPlan(pytest_args=("tests",), coverage=coverage)
    check = CheckInvocation(
        name="pytest",
        arguments=pytest_plan.pytest_args,
        pytest=pytest_plan,
    )
    plan = RunPlan(
        root=prepared.root,
        repository_python=DefaultRepositoryPython(),
        mode="focused",
        targets=(),
        checks=(check,),
        output_format="json",
        planned_coverage_scope="partial" if coverage_requested else "not_requested",
    )
    return plan, ExecutedCheck(
        planned=check,
        processes=result.processes,
        pytest=result.pytest,
        coverage=result.coverage,
    )


class PreparedPytestRunner:
    def __init__(
        self,
        *,
        returncodes: tuple[int, ...] = (),
        raise_on_call: int | None = None,
        exception: Exception | None = None,
        pytest_version: str = "8.4.2",
        coverage_version: str = "7.15.2",
    ) -> None:
        self._runner = launcher_aware_runner(
            returncodes=returncodes,
            publish_valid_marker=True,
            raise_on_call=raise_on_call,
            exception=exception,
        )
        self.pytest_version = pytest_version
        self.coverage_version = coverage_version
        self.calls = self._runner.calls

    def __call__(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        completed_process = self._runner(
            command,
            cwd=cwd,
            check=check,
            capture_output=capture_output,
            env=env,
        )
        logical = self._logical_module_command(command)
        if logical is not None:
            test_support._publish_pytest_artifact(  # noqa: SLF001
                logical,
                env,
                completed_process.returncode,
            )
            test_support._publish_coverage_artifact(logical)  # noqa: SLF001
            self._replace_artifact_versions(logical, env)
        return completed_process

    def _replace_artifact_versions(
        self,
        command: tuple[str, ...],
        environment: dict[str, str] | None,
    ) -> None:
        if "pytest" in command and environment is not None:
            artifact_path = Path(environment["PYREPO_CHECK_PYTEST_JSON"])
            document = json.loads(artifact_path.read_text(encoding="utf-8"))
            document["pytest_version"] = self.pytest_version
            artifact_path.write_text(json.dumps(document), encoding="utf-8")
        if "coverage" in command and "json" in command:
            output_path = Path(command[command.index("-o") + 1])
            document = json.loads(output_path.read_text(encoding="utf-8"))
            document["meta"]["version"] = self.coverage_version
            output_path.write_text(json.dumps(document), encoding="utf-8")

    @staticmethod
    def _logical_module_command(command: tuple[str, ...]) -> tuple[str, ...] | None:
        if "--module" not in command:
            return command if "-m" in command else None
        module_index = command.index("--module")
        separator_index = command.index("--", module_index + 2)
        return (
            "python",
            "-m",
            command[module_index + 1],
            *command[separator_index + 1 :],
        )


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


def test_pytest_uses_prepared_repository_python_and_no_controller_pythonpath(
    tmp_path: Path,
) -> None:
    prepared = prepared_repository(tmp_path, python=(3, 10, 19))
    environment = dict(prepared.child_environment)
    environment["PYTHONPATH"] = "/controller/source"
    runner = launcher_aware_runner(returncode=0, publish_valid_marker=True)

    result = run_prepared_pytest_fixture(
        prepared=replace(prepared, child_environment=MappingProxyType(environment)),
        pytest_dependency=available_dependency("pytest", "8.4.2"),
        coverage_dependency=None,
        runner=runner,
    )

    primary = next(process for process in result.processes if process.role == "primary")
    assert primary.command[:6] == (
        "uv",
        "run",
        "--locked",
        "--python",
        str(prepared.python.executable),
        str(prepared.python.executable),
    )
    assert result.start is not None
    assert result.start.python == prepared.python
    primary_call = next(call for call in runner.calls if call.command == primary.command)
    assert primary_call.env is not None
    assert "/controller/source" not in primary_call.env["PYTHONPATH"]


def test_missing_coverage_runs_plain_pytest_once_and_keeps_coverage_error(
    tmp_path: Path,
) -> None:
    runner = launcher_aware_runner(returncode=0, publish_valid_marker=True)

    result = run_prepared_pytest_fixture(
        prepared=prepared_repository(tmp_path, python=(3, 12, 11)),
        pytest_dependency=available_dependency("pytest", "8.4.2"),
        coverage_dependency=missing_dependency("coverage"),
        coverage_requested=True,
        runner=runner,
    )

    primaries = [process for process in result.processes if process.role == "primary"]
    assert len(primaries) == 1
    assert "pytest" in primaries[0].command
    assert "coverage" not in primaries[0].command
    assert result.coverage is not None
    assert result.coverage.artifact.state == "not_attempted"


def test_prepared_pytest_pythonpath_contains_only_the_reporter_directory(
    tmp_path: Path,
) -> None:
    runner = launcher_aware_runner(returncode=0, publish_valid_marker=True)

    result = run_prepared_pytest_fixture(
        prepared=prepared_repository(tmp_path, python=(3, 11, 12)),
        pytest_dependency=available_dependency("pytest", "8.4.2"),
        coverage_dependency=None,
        runner=runner,
    )

    primary = next(process for process in result.processes if process.role == "primary")
    primary_call = next(call for call in runner.calls if call.command == primary.command)
    assert primary_call.env is not None
    reporter_directory = Path(primary_call.env["PYREPO_CHECK_PYTEST_JSON"]).parent
    assert primary_call.env["PYTHONPATH"] == str(reporter_directory)
    assert os.pathsep not in primary_call.env["PYTHONPATH"]
    assert result.start is not None
    assert result.start.module == "pytest"


def test_prepared_coverage_primary_uses_launcher_but_json_helper_does_not(
    tmp_path: Path,
) -> None:
    prepared = prepared_repository(tmp_path, python=(3, 12, 11))
    runner = PreparedPytestRunner()

    result = run_prepared_pytest_fixture(
        prepared=prepared,
        pytest_dependency=available_dependency("pytest", "8.4.2"),
        coverage_dependency=available_dependency("coverage", "7.15.2"),
        coverage_requested=True,
        runner=runner,
    )

    assert [process.role for process in result.processes] == ["primary", "coverage_json"]
    primary, helper = result.processes
    assert primary.command[:6] == (
        "uv",
        "run",
        "--locked",
        "--python",
        str(prepared.python.executable),
        str(prepared.python.executable),
    )
    assert "--evidence" in primary.command
    assert primary.command[primary.command.index("--module") + 1] == "coverage"
    separator = primary.command.index("--")
    assert primary.command[separator + 1 : separator + 5] == (
        "run",
        f"--rcfile={tmp_path / 'pyproject.toml'}",
        f"--data-file={Path(primary.command[primary.command.index('--evidence') + 1]).parent / '.coverage'}",
        "-m",
    )
    assert helper.command[:6] == (
        "uv",
        "run",
        "--locked",
        "--python",
        str(prepared.python.executable),
        "python",
    )
    assert helper.command[6:9] == ("-m", "coverage", "json")
    assert "--evidence" not in helper.command
    assert result.start is not None
    assert result.start.module == "coverage"
    assert result.pytest is not None
    assert result.pytest.artifact.state == "snapshot"
    assert result.coverage is not None
    assert result.coverage.artifact.state == "snapshot"


def test_missing_pytest_prevents_both_pytest_and_coverage(
    tmp_path: Path,
) -> None:
    runner = PreparedPytestRunner()

    result = run_prepared_pytest_fixture(
        prepared=prepared_repository(tmp_path, python=(3, 12, 11)),
        pytest_dependency=missing_dependency("pytest"),
        coverage_dependency=available_dependency("coverage", "7.15.2"),
        coverage_requested=True,
        runner=runner,
    )

    assert result.processes == ()
    assert result.start is None
    assert result.error == missing_dependency("pytest").error
    assert result.pytest is not None
    assert result.pytest.preflight.classification == "module_unavailable"
    assert result.coverage is not None
    assert result.coverage.preflight.classification == "preflight_invalid"
    assert result.coverage.artifact.state == "not_attempted"
    assert runner.calls == []


def test_coverage_json_helper_failure_keeps_primary_start_evidence(
    tmp_path: Path,
) -> None:
    runner = PreparedPytestRunner(returncodes=(0, 3))

    result = run_prepared_pytest_fixture(
        prepared=prepared_repository(tmp_path, python=(3, 12, 11)),
        pytest_dependency=available_dependency("pytest", "8.4.2"),
        coverage_dependency=available_dependency("coverage", "7.15.2"),
        coverage_requested=True,
        runner=runner,
    )

    assert result.start is not None
    assert result.start.module == "coverage"
    assert result.error is None
    assert [process.returncode for process in result.processes] == [0, 3]
    assert result.coverage is not None
    assert result.coverage.artifact.state == "generation_failed"
    assert result.coverage.json_exit_code == 3


def test_coverage_primary_spawn_failure_discards_start_and_skips_json_helper(
    tmp_path: Path,
) -> None:
    runner = PreparedPytestRunner(
        raise_on_call=1,
        exception=FileNotFoundError("repository python"),
    )

    result = run_prepared_pytest_fixture(
        prepared=prepared_repository(tmp_path, python=(3, 12, 11)),
        pytest_dependency=available_dependency("pytest", "8.4.2"),
        coverage_dependency=available_dependency("coverage", "7.15.2"),
        coverage_requested=True,
        runner=runner,
    )

    assert len(result.processes) == 1
    assert result.processes[0].role == "primary"
    assert result.processes[0].spawn_error == "FileNotFoundError: repository python"
    assert result.start is None
    assert result.error is not None
    assert result.error.code == "spawn_failed"
    assert result.coverage is not None
    assert result.coverage.artifact.state == "data_missing"
    assert not [process for process in result.processes if process.role == "coverage_json"]


@pytest.mark.parametrize("returncode", (2, 120))
def test_prepared_plain_pytest_rejects_reserved_launcher_exit(
    tmp_path: Path,
    returncode: int,
) -> None:
    result = run_prepared_pytest_fixture(
        prepared=prepared_repository(tmp_path, python=(3, 12, 11)),
        pytest_dependency=available_dependency("pytest", "8.4.2"),
        coverage_dependency=None,
        runner=PreparedPytestRunner(returncodes=(returncode,)),
    )

    assert [process.returncode for process in result.processes] == [returncode]
    assert result.start is not None
    assert result.error is not None
    assert result.error.code == "check_execution_failed"


@pytest.mark.parametrize("returncode", (2, 120))
def test_prepared_instrumented_pytest_rejects_reserved_launcher_exit_without_helper(
    tmp_path: Path,
    returncode: int,
) -> None:
    result = run_prepared_pytest_fixture(
        prepared=prepared_repository(tmp_path, python=(3, 12, 11)),
        pytest_dependency=available_dependency("pytest", "8.4.2"),
        coverage_dependency=available_dependency("coverage", "7.15.2"),
        coverage_requested=True,
        runner=PreparedPytestRunner(returncodes=(returncode,)),
    )

    assert [process.returncode for process in result.processes] == [returncode]
    assert result.start is not None
    assert result.error is not None
    assert result.error.code == "check_execution_failed"
    assert result.coverage is not None
    assert result.coverage.artifact.state == "data_missing"


def test_prepared_pytest_exit_five_remains_completed(tmp_path: Path) -> None:
    result = run_prepared_pytest_fixture(
        prepared=prepared_repository(tmp_path, python=(3, 12, 11)),
        pytest_dependency=available_dependency("pytest", "8.4.2"),
        coverage_dependency=None,
        runner=PreparedPytestRunner(returncodes=(5,)),
    )

    assert [process.returncode for process in result.processes] == [5]
    assert result.start is not None
    assert result.error is None


def test_prepared_four_component_dependency_versions_remain_exact(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "example.py").write_text("value = 1\n", encoding="utf-8")
    prepared = prepared_repository(tmp_path, python=(3, 12, 11))
    result = run_prepared_pytest_fixture(
        prepared=prepared,
        pytest_dependency=available_dependency("pytest", "8.4.2.0"),
        coverage_dependency=available_dependency("coverage", "7.15.2.0"),
        coverage_requested=True,
        runner=PreparedPytestRunner(
            pytest_version="8.4.2.0",
            coverage_version="7.15.2.0",
        ),
    )
    plan, check = prepared_executed_check(prepared, result, coverage_requested=True)

    pytest_result = build_pytest_result(
        plan,
        check,
        dependency_version="8.4.2.0",
    )
    coverage_result = build_coverage_result(
        prepared.root,
        plan,
        pytest_result,
        result.coverage,
        dependency_version="7.15.2.0",
    )

    assert pytest_result.pytest_version == "8.4.2.0"
    assert pytest_result.error is None
    assert coverage_result is not None
    assert coverage_result.coverage_version == "7.15.2.0"
    assert coverage_result.error is None


@pytest.mark.parametrize(
    ("pytest_artifact_version", "coverage_artifact_version", "expected_error"),
    (
        ("8.4.2", "7.15.2.0", "pytest"),
        ("8.4.2.0", "7.15.2", "coverage"),
    ),
)
def test_prepared_four_component_dependency_version_mismatch_is_rejected(
    tmp_path: Path,
    pytest_artifact_version: str,
    coverage_artifact_version: str,
    expected_error: str,
) -> None:
    prepared = prepared_repository(tmp_path, python=(3, 12, 11))
    result = run_prepared_pytest_fixture(
        prepared=prepared,
        pytest_dependency=available_dependency("pytest", "8.4.2.0"),
        coverage_dependency=available_dependency("coverage", "7.15.2.0"),
        coverage_requested=True,
        runner=PreparedPytestRunner(
            pytest_version=pytest_artifact_version,
            coverage_version=coverage_artifact_version,
        ),
    )
    plan, check = prepared_executed_check(prepared, result, coverage_requested=True)
    pytest_result = build_pytest_result(
        plan,
        check,
        dependency_version="8.4.2.0",
    )

    if expected_error == "pytest":
        assert pytest_result.error is not None
        assert pytest_result.error.code == "artifact_invalid"
        return
    coverage_result = build_coverage_result(
        prepared.root,
        plan,
        pytest_result,
        result.coverage,
        dependency_version="7.15.2.0",
    )
    assert coverage_result is not None
    assert coverage_result.error is not None
    assert coverage_result.error.code == "artifact_invalid"


def test_prepared_pytest_requires_planner_metadata_before_execution(tmp_path: Path) -> None:
    prepared = prepared_repository(tmp_path, python=(3, 12, 11))
    check = CheckInvocation("pytest", (), pytest=None)
    plan = RunPlan(
        root=tmp_path,
        repository_python=DefaultRepositoryPython(),
        mode="focused",
        targets=(),
        checks=(check,),
        output_format="json",
    )
    with test_workspace(tmp_path) as workspace:
        launcher = stage_check_launcher(workspace)
        with pytest.raises(ValueError, match="requires CheckInvocation.pytest metadata"):
            pytest_execution.execute_prepared_pytest(
                check,
                plan=plan,
                prepared=prepared,
                pytest_dependency=available_dependency("pytest", "8.4.2"),
                coverage_dependency=None,
                workspace=workspace,
                launcher=launcher,
                output_format="json",
                runner=PreparedPytestRunner(),
                clock_ns=monotonic_clock(),
            )


@pytest.mark.parametrize(
    ("version", "expected"),
    (
        (None, None),
        ("bad", None),
        ("-1.2.3", None),
        ("8", (8, 0, 0)),
        ("8.4", (8, 4, 0)),
        ("8.4.2.1", (8, 4, 2)),
    ),
)
def test_dependency_numeric_version_is_bounded_and_fail_closed(
    version: str | None,
    expected: tuple[int, int, int] | None,
) -> None:
    assert pytest_execution._dependency_numeric_version(version) == expected


def test_prepared_coverage_observation_handles_unrequested_missing_and_unusable(
    tmp_path: Path,
) -> None:
    prepared = prepared_repository(tmp_path, python=(3, 12, 11))

    assert (
        pytest_execution._prepared_coverage_observation(
            prepared,
            None,
            requested=False,
        )
        is None
    )
    missing = pytest_execution._prepared_coverage_observation(
        prepared,
        None,
        requested=True,
    )
    assert missing is not None
    assert missing.preflight.classification == "preflight_invalid"
    unusable_dependency = replace(
        available_dependency("coverage", "7.15.2"),
        status="unusable",
    )
    unusable = pytest_execution._prepared_coverage_observation(
        prepared,
        unusable_dependency,
        requested=True,
    )
    assert unusable is not None
    assert unusable.preflight.classification == "preflight_invalid"


def test_prepared_pytest_environment_scrubs_consumer_startup_hooks(tmp_path: Path) -> None:
    environment = pytest_execution._prepared_pytest_environment(
        {
            "PATH": "/bin",
            "PYTHONPATH": "/hostile",
            "COVERAGE_PROCESS_CONFIG": "ambient",
            "COVERAGE_PROCESS_START": "ambient.toml",
            "COV_CORE_SOURCE": "ambient",
        },
        tmp_path,
        tmp_path / "artifact.json",
        tmp_path / "writers",
    )

    assert environment == {
        "PATH": "/bin",
        "PYTHONPATH": str(tmp_path),
        "PYREPO_CHECK_PYTEST_JSON": str(tmp_path / "artifact.json"),
        "PYREPO_CHECK_PYTEST_WRITER_DIR": str(tmp_path / "writers"),
    }


@pytest.mark.parametrize(
    ("name", "expected"),
    (
        ("plain", None),
        ("pytest-writer-.json", None),
        ("pytest-writer-bad/marker.json", None),
        ("pytest-writer-valid_1-.json", "valid_1-"),
    ),
)
def test_writer_marker_name_parser_is_exact(name: str, expected: str | None) -> None:
    assert pytest_execution._marker_id(name) == expected


def test_pytest_small_helpers_preserve_diagnostics_and_duration() -> None:
    assert pytest_execution._combine_diagnostic(None, "second") == "second"
    assert pytest_execution._combine_diagnostic("first", "second") == "first; second"
    assert pytest_execution._duration_ms(2_000_000, 1_000_000) == 0
    assert pytest_execution._duration_ms(0, 1_500_000) == 2
