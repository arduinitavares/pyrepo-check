from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess  # nosec B404
from subprocess import CompletedProcess  # nosec B404
import threading
from typing import Callable, TypeVar, cast

import pytest

from pyrepo_check.execution import CapturedBytes, ExecutedCheck, ExecutedProcess, execute_plan
from pyrepo_check.planning import (
    CoverageExecutionPlan,
    OutputFormat,
    PlannedCheck,
    PytestExecutionPlan,
    RunPlan,
    RunMode,
)
from pyrepo_check import coverage_execution


_T = TypeVar("_T")
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
    worker.join(timeout=1)
    assert returned_promptly, "coverage FIFO inspection blocked"
    assert not worker.is_alive(), "coverage FIFO inspection did not terminate"
    if errors:
        raise errors[0]
    return result[0]


def _open_directory(directory: Path) -> int:
    return os.open(
        directory,
        os.O_RDONLY
        | cast(int, getattr(os, "O_DIRECTORY"))
        | cast(int, getattr(os, "O_NOFOLLOW"))
        | cast(int, getattr(os, "O_NONBLOCK")),
    )


def _prepare_coverage_data(run_directory: Path) -> coverage_execution.CoverageDataSnapshot:
    descriptor = _open_directory(run_directory)
    try:
        return coverage_execution.prepare_coverage_data_snapshot(
            run_directory,
            run_descriptor=descriptor,
        )
    finally:
        os.close(descriptor)


def test_coverage_data_base_is_copied_to_an_immutable_descriptor_held_snapshot(
    tmp_path: Path,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / ".coverage").write_bytes(b"sqlite-evidence")

    snapshot = _prepare_coverage_data(run_directory)
    try:
        assert snapshot.data_path == run_directory / "report-input" / "coverage-data"
        assert snapshot.data_path.read_bytes() == b"sqlite-evidence"
        assert snapshot.digest.size == len(b"sqlite-evidence")
        assert len(snapshot.digest.sha256) == 64
        assert all(not isinstance(value, bytes) for value in vars(snapshot).values())
        coverage_execution.verify_coverage_data_snapshot(snapshot)
    finally:
        snapshot.close()


@pytest.mark.parametrize(
    "base_kind",
    ("missing", "symlink", "fifo", "oversized", "unreadable"),
)
def test_coverage_data_missing_or_unusable_base_fails_closed(
    tmp_path: Path,
    base_kind: str,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    base = run_directory / ".coverage"
    if base_kind == "symlink":
        target = tmp_path / "outside.coverage"
        target.write_bytes(b"outside")
        base.symlink_to(target)
    elif base_kind == "fifo":
        _MKFIFO(base)
    elif base_kind == "oversized":
        with base.open("wb") as file:
            file.truncate(coverage_execution.MAX_COVERAGE_DATA_BYTES + 1)
    elif base_kind == "unreadable":
        base.write_bytes(b"private")
        base.chmod(0)

    def call() -> coverage_execution.CoverageDataSnapshot:
        return _prepare_coverage_data(run_directory)
    try:
        with pytest.raises(coverage_execution.CoverageDataError) as raised:
            if base_kind == "fifo":
                _run_fifo_call_with_watchdog(call, base)
            else:
                call()
    finally:
        if base_kind == "unreadable":
            base.chmod(0o600)
    assert raised.value.code == "data_missing"


@pytest.mark.parametrize(
    ("entry_name", "rejected"),
    (
        (".coverage.", True),
        (".coverage.worker", True),
        (".coveragex.worker", False),
        ("coverage.worker", False),
    ),
)
def test_coverage_data_scans_only_literal_run_root_shards(
    tmp_path: Path,
    entry_name: str,
    rejected: bool,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / ".coverage").write_bytes(b"base")
    (run_directory / entry_name).write_bytes(b"entry")

    if rejected:
        with pytest.raises(coverage_execution.CoverageDataError) as raised:
            _prepare_coverage_data(run_directory)
        assert raised.value.code == "unexpected_parallel_data"
    else:
        snapshot = _prepare_coverage_data(run_directory)
        snapshot.close()


def test_coverage_data_scan_does_not_inspect_consumer_siblings(tmp_path: Path) -> None:
    consumer_shard = tmp_path / ".coverage.worker"
    consumer_shard.write_bytes(b"consumer-owned")
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / ".coverage").write_bytes(b"base")

    snapshot = _prepare_coverage_data(run_directory)
    snapshot.close()

    assert consumer_shard.read_bytes() == b"consumer-owned"


def test_coverage_data_rejects_report_destination_collision(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / ".coverage").write_bytes(b"base")
    (run_directory / "report-input").mkdir()

    with pytest.raises(coverage_execution.CoverageDataError) as raised:
        _prepare_coverage_data(run_directory)

    assert raised.value.code == "unexpected_parallel_data"


def test_coverage_data_copy_or_source_change_is_unexpected_parallel_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    base = run_directory / ".coverage"
    base.write_bytes(b"original")

    def mutate_instead_of_copy(*_args: object, **_kwargs: object) -> object:
        base.write_bytes(b"changed!")
        raise coverage_execution._DigestMismatchError("coverage-data digest mismatch")

    monkeypatch.setattr(
        coverage_execution,
        "copy_regular_file",
        mutate_instead_of_copy,
    )

    with pytest.raises(coverage_execution.CoverageDataError) as raised:
        _prepare_coverage_data(run_directory)

    assert raised.value.code == "unexpected_parallel_data"


def test_coverage_data_rejects_same_byte_snapshot_replacement_at_copy_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / ".coverage").write_bytes(b"same-bytes")
    real_copy = coverage_execution.copy_regular_file

    def replace_after_copy(
        source_path: Path,
        destination_path: Path,
        *,
        max_bytes: int,
        source_dir_fd: int | None = None,
        destination_dir_fd: int | None = None,
    ) -> object:
        copied = real_copy(
            source_path,
            destination_path,
            max_bytes=max_bytes,
            source_dir_fd=source_dir_fd,
            destination_dir_fd=destination_dir_fd,
        )
        snapshot = run_directory / "report-input" / "coverage-data"
        replacement = snapshot.with_name("coverage-data-replacement")
        replacement.write_bytes(b"same-bytes")
        replacement.replace(snapshot)
        return copied

    monkeypatch.setattr(
        coverage_execution,
        "copy_regular_file",
        replace_after_copy,
    )

    with pytest.raises(coverage_execution.CoverageDataError) as raised:
        _prepare_coverage_data(run_directory)

    assert raised.value.code == "unexpected_parallel_data"


@pytest.mark.parametrize(
    ("namespace", "entry_name"),
    (
        ("run", ".coverage.worker"),
        ("snapshot", "coverage-data.worker"),
        ("snapshot", "coverage-data."),
    ),
)
def test_coverage_data_rejects_shards_created_after_snapshot(
    tmp_path: Path,
    namespace: str,
    entry_name: str,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / ".coverage").write_bytes(b"base")
    snapshot = _prepare_coverage_data(run_directory)
    try:
        parent = run_directory if namespace == "run" else snapshot.data_path.parent
        (parent / entry_name).write_bytes(b"shard")

        with pytest.raises(coverage_execution.CoverageDataError) as raised:
            coverage_execution.verify_coverage_data_snapshot(snapshot)
    finally:
        snapshot.close()

    assert raised.value.code == "unexpected_parallel_data"


@pytest.mark.parametrize("target", ("original", "snapshot"))
def test_coverage_data_rejects_original_or_snapshot_mutation(
    tmp_path: Path,
    target: str,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    original = run_directory / ".coverage"
    original.write_bytes(b"original")
    snapshot = _prepare_coverage_data(run_directory)
    try:
        path = original if target == "original" else snapshot.data_path
        path.write_bytes(b"changed!")

        with pytest.raises(coverage_execution.CoverageDataError) as raised:
            coverage_execution.verify_coverage_data_snapshot(snapshot)
    finally:
        snapshot.close()

    assert raised.value.code == "unexpected_parallel_data"


@pytest.mark.parametrize("target", ("original", "snapshot"))
def test_coverage_data_rejects_same_byte_original_or_snapshot_replacement(
    tmp_path: Path,
    target: str,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    original = run_directory / ".coverage"
    original.write_bytes(b"same-bytes")
    snapshot = _prepare_coverage_data(run_directory)
    try:
        path = original if target == "original" else snapshot.data_path
        replacement = path.with_name(f"{path.name}-replacement")
        replacement.write_bytes(b"same-bytes")
        replacement.replace(path)

        with pytest.raises(coverage_execution.CoverageDataError) as raised:
            coverage_execution.verify_coverage_data_snapshot(snapshot)
    finally:
        snapshot.close()

    assert raised.value.code == "unexpected_parallel_data"


def test_coverage_data_preparation_cleanup_attempts_both_closes_and_preserves_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / ".coverage").write_bytes(b"base")
    run_descriptor = _open_directory(run_directory)
    duplicated: list[int] = []
    report_descriptors: list[int] = []
    close_attempts: list[int] = []
    original_dup = coverage_execution.os.dup
    original_open = coverage_execution.os.open
    original_close = coverage_execution.os.close

    def tracked_dup(descriptor: int) -> int:
        duplicate = original_dup(descriptor)
        duplicated.append(duplicate)
        return duplicate

    def tracked_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        *args: int,
        **kwargs: int | None,
    ) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)
        if os.fsdecode(path) == "report-input":
            report_descriptors.append(descriptor)
        return descriptor

    def fail_report_close(descriptor: int) -> None:
        close_attempts.append(descriptor)
        if report_descriptors and descriptor == report_descriptors[0]:
            raise PermissionError("report descriptor close denied")
        original_close(descriptor)

    def fail_verification(_snapshot: object) -> None:
        raise coverage_execution.CoverageDataError(
            "unexpected_parallel_data",
            "initiating verification failure",
        )

    monkeypatch.setattr(coverage_execution.os, "dup", tracked_dup)
    monkeypatch.setattr(coverage_execution.os, "open", tracked_open)
    monkeypatch.setattr(coverage_execution.os, "close", fail_report_close)
    monkeypatch.setattr(coverage_execution, "verify_coverage_data_snapshot", fail_verification)
    try:
        with pytest.raises(
            coverage_execution.CoverageDataError,
            match="initiating verification failure",
        ):
            coverage_execution.prepare_coverage_data_snapshot(
                run_directory,
                run_descriptor=run_descriptor,
            )

        assert report_descriptors
        assert duplicated
        owned_attempts = [
            descriptor
            for descriptor in close_attempts
            if descriptor in {report_descriptors[0], duplicated[0]}
        ]
        assert owned_attempts[-2:] == [report_descriptors[0], duplicated[0]]
    finally:
        for descriptor in (*report_descriptors, *duplicated, run_descriptor):
            try:
                original_close(descriptor)
            except OSError:
                pass


def test_coverage_data_rejects_report_directory_substitution(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / ".coverage").write_bytes(b"base")
    snapshot = _prepare_coverage_data(run_directory)
    displaced = run_directory / "displaced-report-input"
    snapshot.data_path.parent.rename(displaced)
    snapshot.data_path.parent.mkdir()
    try:
        with pytest.raises(coverage_execution.CoverageDataError) as raised:
            coverage_execution.verify_coverage_data_snapshot(snapshot)
    finally:
        snapshot.close()

    assert raised.value.code == "unexpected_parallel_data"


def _process(
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int | None = 0,
    spawn_error: str | None = None,
) -> ExecutedProcess:
    return ExecutedProcess(
        role="coverage_preflight",
        command=("consumer-python", "-c", "probe"),
        cwd=Path("."),
        returncode=returncode,
        duration_ms=0,
        stdout=CapturedBytes(stdout, 0),
        stderr=CapturedBytes(stderr, 0),
        spawn_error=spawn_error,
    )


def _document(
    *,
    python_version: object = [3, 13, 15],
    coverage_available: object = True,
    coverage_version: object = "7.15.2",
) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "python_version": python_version,
            "coverage_available": coverage_available,
            "coverage_version": coverage_version,
        },
        separators=(",", ":"),
    ).encode()


@pytest.mark.parametrize(
    ("stdout", "returncode", "spawn_error", "classification", "version"),
    [
        (_document(python_version=[3, 13, 14], coverage_available=False, coverage_version=None), 0, None, "unsupported_python", None),
        (_document(coverage_available=False, coverage_version=None), 0, None, "module_unavailable", None),
        (_document(coverage_version="7.15"), 0, None, "supported", "7.15"),
        (_document(coverage_version="7.15.2"), 0, None, "supported", "7.15.2"),
        (_document(coverage_version="7.99.0"), 0, None, "supported", "7.99.0"),
        (_document(coverage_version="7.14.9"), 0, None, "unsupported_version", "7.14.9"),
        (_document(coverage_version="8.0.0"), 0, None, "unsupported_version", "8.0.0"),
        (_document(coverage_version="7.15rc1"), 0, None, "unsupported_version", None),
        (_document(coverage_version="7.15.dev0"), 0, None, "unsupported_version", None),
        (_document(coverage_version="7.15.post1"), 0, None, "unsupported_version", None),
        (_document(coverage_version="7.15+local"), 0, None, "unsupported_version", None),
        (_document() + b"\nextra", 0, None, "preflight_invalid", None),
        (b"not-json", 0, None, "preflight_invalid", None),
        (b"\xff", 0, None, "preflight_invalid", None),
        (_document(), 1, None, "preflight_invalid", None),
        (_document(), -9, None, "terminated_by_signal", None),
        (b"", None, "FileNotFoundError: consumer-python", "spawn_failed", None),
    ],
    ids=(
        "unsupported-python",
        "missing-coverage",
        "minimum-stable",
        "stable-patch",
        "stable-high-minor",
        "too-old",
        "too-new",
        "prerelease",
        "dev",
        "post",
        "local",
        "extra-output",
        "malformed-json",
        "invalid-utf8",
        "nonzero",
        "signal",
        "spawn-failure",
    ),
)
def test_coverage_preflight_classifies_the_supported_consumer_contract(
    stdout: bytes,
    returncode: int | None,
    spawn_error: str | None,
    classification: str,
    version: str | None,
) -> None:
    observation = coverage_execution.classify_coverage_preflight(
        _process(stdout=stdout, returncode=returncode, spawn_error=spawn_error)
    )

    assert observation.classification == classification
    assert observation.record is not None or classification in {
        "preflight_invalid",
        "spawn_failed",
        "terminated_by_signal",
    }
    if observation.record is None:
        assert version is None
    else:
        assert observation.record.coverage_version == version


def test_coverage_preflight_rejects_truncated_stderr() -> None:
    process = _process(stdout=_document())
    object.__setattr__(process, "stderr", CapturedBytes(b"tail", 1))

    observation = coverage_execution.classify_coverage_preflight(process)

    assert observation.classification == "preflight_invalid"


def test_coverage_preflight_fails_closed_on_an_oversized_stable_version() -> None:
    observation = coverage_execution.classify_coverage_preflight(
        _process(stdout=_document(coverage_version=f"7.{'9' * 5000}"))
    )

    assert observation.classification == "unsupported_version"


def test_coverage_probe_is_python_37_compatible_and_reads_python_first() -> None:
    probe = coverage_execution.COVERAGE_PREFLIGHT_PROBE

    assert "f\"" not in probe
    assert probe.index("sys.version_info") < probe.index("import coverage")
    assert 'separators=(",", ":")' in probe


def _pytest_document() -> bytes:
    return b'{"schema_version":1,"python_version":[3,13,15],"pytest_available":true,"pytest_version":[8,4,2]}'


def _coverage_check(
    tmp_path: Path,
    *,
    fail_under: int | float | None = None,
) -> PlannedCheck:
    config_path = tmp_path / "pyproject.toml"
    pytest_plan = PytestExecutionPlan(
        consumer_python=("consumer-python",),
        pytest_args=("tests",),
        coverage=CoverageExecutionPlan(
            consumer_python=("consumer-python",),
            config_path=config_path,
            fail_under=fail_under,
        ),
    )
    return PlannedCheck(
        name="pytest",
        command=("consumer-python", "-m", "pytest", "tests"),
        cwd=tmp_path,
        pytest=pytest_plan,
    )


def test_coverage_execution_orders_preflights_then_runs_one_instrumented_pytest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", "consumer-path")
    monkeypatch.setenv("COVERAGE_PROCESS_START", "consumer-startup")
    monkeypatch.setenv("COVERAGE_FILE", "consumer-data")
    calls: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> CompletedProcess[tuple[str, ...]]:
        del cwd, check, capture_output
        calls.append((command, env))
        stdout = (
            _coverage_document()
            if command[-1] == coverage_execution.COVERAGE_PREFLIGHT_PROBE
            else _pytest_document()
            if "-c" in command
            else b""
        )
        return cast(
            CompletedProcess[tuple[str, ...]],
            CompletedProcess(command, 0, stdout=stdout, stderr=b""),
        )

    result = execute_plan(
        RunPlan(mode="focused", targets=(), checks=(_coverage_check(tmp_path),), output_format="json"),
        runner=runner,
    )

    observation = result.checks[0]
    assert [process.role for process in observation.processes] == [
        "pytest_preflight",
        "coverage_preflight",
        "primary",
    ]
    assert [command for command, _environment in calls][2].count("-m") == 2
    assert calls[2][0][1:5] == ("-m", "coverage", "run", f"--rcfile={tmp_path / 'pyproject.toml'}")
    coverage_environment = calls[2][1]
    assert coverage_environment is not None
    assert calls[2][0][5] == f"--data-file={coverage_environment['COVERAGE_FILE']}"
    assert calls[2][0][6:10] == ("-m", "pytest", "-p", calls[2][0][9])
    assert sum(command.count("pytest") for command, _environment in calls) == 1
    assert calls[0][1] is not None and "COVERAGE_PROCESS_START" not in calls[0][1]
    for _command, environment in calls[1:]:
        assert environment is not None
        assert environment["COVERAGE_FILE"].endswith("/.coverage")
        assert environment["COVERAGE_RCFILE"] == str(tmp_path / "pyproject.toml")
        assert "COVERAGE_PROCESS_START" not in environment
    assert observation.coverage is not None
    assert observation.coverage.preflight.classification == "supported"
    assert observation.coverage.artifact.state == "data_missing"


def _finalized_pytest_document(
    *,
    exit_code: int = 0,
    stopped_early: bool = False,
    narrowed: bool = False,
) -> bytes:
    collected = exit_code != 5
    failed = exit_code == 1
    nodeids = ["tests/test_ok.py::test_ok"] if collected else []
    reports = (
        [
            {
                "nodeid": nodeids[0],
                "when": phase,
                "outcome": "failed" if failed and phase == "call" else "passed",
                "duration": 0.1,
                "wasxfail_present": False,
                "wasxfail_valid": True,
                "wasxfail": None,
                "longrepr": "assert false" if failed and phase == "call" else None,
            }
            for phase in ("setup", "call", "teardown")
        ]
        if collected
        else []
    )
    return json.dumps(
        {
            "schema_version": 1,
            "state": "finalized",
            "writer_id": "writer-1",
            "pytest_version": "8.4.2",
            "session": {
                "starts": 1,
                "finishes": 1,
                "exit_code": exit_code,
                "collection_completed": not stopped_early,
                "stopped_early": stopped_early,
            },
            "effective_args": ["tests"] if narrowed else [],
            "semantic_options": {
                "collection_paths": ["tests"] if narrowed else [],
                "keyword": "",
                "markexpr": "",
                "deselect": [],
                "ignore": [],
                "ignore_glob": [],
                "lf": False,
                "pyargs": False,
                "collectonly": False,
                "setuponly": False,
                "setupplan": False,
            },
            "collection": {
                "initial_nodeids": nodeids,
                "final_nodeids": nodeids,
                "deselected_nodeids": [],
                "uncovered_removed_nodeids": [],
                "errors": [],
                "skips": [],
            },
            "reports": reports,
            "flags": {
                "unsupported_parallelism": False,
                "unsupported_retries": False,
                "worker_metadata": False,
            },
        },
        separators=(",", ":"),
    ).encode()


def _coverage_run_plan(
    tmp_path: Path,
    *,
    mode: str = "strict_aggregate",
    targets: tuple[str, ...] = (),
    fail_under: int | float | None = 80,
) -> RunPlan:
    check = _coverage_check(tmp_path, fail_under=fail_under)
    if not targets:
        if check.pytest is None:
            raise AssertionError("pytest plan is unavailable")
        check = replace(
            check,
            command=("consumer-python", "-m", "pytest"),
            pytest=replace(check.pytest, pytest_args=()),
        )
    return RunPlan(
        mode=cast(RunMode, mode),
        targets=targets,
        checks=(check,),
        output_format="json",
        pytest_args=("tests",) if targets else (),
        planned_test_scope="partial" if targets else "complete",
        planned_coverage_scope="partial" if targets else "complete",
    )


def _run_coverage_json_case(
    tmp_path: Path,
    *,
    plan: RunPlan,
    pytest_exit: int = 0,
    pytest_stopped_early: bool = False,
    coverage_json_exit: int = 0,
    coverage_json_content: bytes | None = b'{"meta":{"format":3}}',
    coverage_json_error: OSError | None = None,
    primary_mutation: Callable[[Path], None] | None = None,
    during_coverage_json: Callable[[Path, Path], None] | None = None,
) -> tuple[ExecutedCheck, list[tuple[tuple[str, ...], bool, dict[str, str] | None]]]:
    calls: list[tuple[tuple[str, ...], bool, dict[str, str] | None]] = []

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> CompletedProcess[tuple[str, ...]]:
        del cwd, check
        calls.append((command, capture_output, env))
        if command[-1] == coverage_execution.COVERAGE_PREFLIGHT_PROBE:
            return cast(
                CompletedProcess[tuple[str, ...]],
                CompletedProcess(command, 0, stdout=_coverage_document(), stderr=b""),
            )
        if "-c" in command:
            return cast(
                CompletedProcess[tuple[str, ...]],
                CompletedProcess(command, 0, stdout=_pytest_document(), stderr=b""),
            )
        if "run" in command:
            assert env is not None
            Path(env["COVERAGE_FILE"]).write_bytes(b"sqlite-evidence")
            Path(env["PYREPO_CHECK_PYTEST_JSON"]).write_bytes(
                _finalized_pytest_document(
                    exit_code=pytest_exit,
                    stopped_early=pytest_stopped_early,
                    narrowed=bool(plan.targets),
                )
            )
            writer_directory = Path(env["PYREPO_CHECK_PYTEST_WRITER_DIR"])
            (writer_directory / "pytest-writer-writer-1.json").write_text(
                '{"schema_version":1,"writer_id":"writer-1","pid":1}'
            )
            if primary_mutation is not None:
                primary_mutation(Path(env["COVERAGE_FILE"]).parent)
            return cast(
                CompletedProcess[tuple[str, ...]],
                CompletedProcess(command, pytest_exit, stdout=b"", stderr=b""),
            )
        output_path = Path(command[command.index("-o") + 1])
        if coverage_json_content is not None:
            output_path.write_bytes(coverage_json_content)
        data_path = Path(
            next(argument for argument in command if argument.startswith("--data-file="))
            .removeprefix("--data-file=")
        )
        if during_coverage_json is not None:
            during_coverage_json(output_path.parent, data_path)
        if coverage_json_error is not None:
            raise coverage_json_error
        return cast(
            CompletedProcess[tuple[str, ...]],
            CompletedProcess(
                command,
                coverage_json_exit,
                stdout=b"coverage stdout",
                stderr=b"coverage stderr",
            ),
        )

    result = execute_plan(plan, runner=runner)
    return result.checks[0], calls


@pytest.mark.parametrize(
    (
        "mode",
        "targets",
        "pytest_exit",
        "stopped_early",
        "expect_fail_under_zero",
    ),
    (
        ("strict_aggregate", (), 0, False, False),
        ("focused", (), 0, False, True),
        ("strict_aggregate", ("tests",), 0, False, True),
        ("strict_aggregate", (), 1, False, True),
        ("strict_aggregate", (), 2, True, True),
        ("strict_aggregate", (), 5, False, True),
    ),
    ids=("eligible", "focused", "partial", "failed", "incomplete", "no-tests"),
)
def test_coverage_json_fail_under_policy_uses_finalized_pytest_result(
    tmp_path: Path,
    mode: str,
    targets: tuple[str, ...],
    pytest_exit: int,
    stopped_early: bool,
    expect_fail_under_zero: bool,
) -> None:
    plan = _coverage_run_plan(tmp_path, mode=mode, targets=targets)

    observation, calls = _run_coverage_json_case(
        tmp_path,
        plan=plan,
        pytest_exit=pytest_exit,
        pytest_stopped_early=stopped_early,
    )

    roles = [process.role for process in observation.processes]
    assert roles == ["pytest_preflight", "coverage_preflight", "primary", "coverage_json"]
    command, captured, environment = calls[-1]
    assert command[:4] == ("consumer-python", "-m", "coverage", "json")
    assert ("--fail-under=0" in command) is expect_fail_under_zero
    assert "--keep-combined" in command
    data_argument = next(argument for argument in command if argument.startswith("--data-file="))
    assert data_argument.endswith("/report-input/coverage-data")
    assert captured is True
    assert environment is not None
    assert environment["COVERAGE_FILE"] == data_argument.removeprefix("--data-file=")
    assert observation.coverage is not None
    assert observation.coverage.artifact.state == "snapshot"
    assert observation.coverage.artifact.content == b'{"meta":{"format":3}}'


@pytest.mark.parametrize(
    ("mode", "fail_under", "coverage_json_exit", "expected_state"),
    (
        ("strict_aggregate", 80, 2, "snapshot"),
        ("strict_aggregate", 80, 1, "generation_failed"),
        ("strict_aggregate", None, 2, "generation_failed"),
        ("focused", 80, 2, "generation_failed"),
    ),
    ids=("eligible-threshold", "eligible-other", "unconfigured-exit-2", "focused-exit-2"),
)
def test_coverage_json_exit_classification_retains_only_eligible_threshold_exit_two(
    tmp_path: Path,
    mode: str,
    fail_under: int | float | None,
    coverage_json_exit: int,
    expected_state: str,
) -> None:
    plan = _coverage_run_plan(tmp_path, mode=mode, fail_under=fail_under)

    observation, _calls = _run_coverage_json_case(
        tmp_path,
        plan=plan,
        coverage_json_exit=coverage_json_exit,
    )

    assert observation.coverage is not None
    assert observation.coverage.artifact.state == expected_state
    process = observation.processes[-1]
    assert process.role == "coverage_json"
    assert process.returncode == coverage_json_exit


@pytest.mark.parametrize(
    ("returncode", "error", "expected_state"),
    (
        (-9, None, "terminated_by_signal"),
        (0, FileNotFoundError("consumer-python"), "spawn_failed"),
    ),
)
def test_coverage_json_process_errors_remain_typed(
    tmp_path: Path,
    returncode: int,
    error: OSError | None,
    expected_state: str,
) -> None:
    plan = _coverage_run_plan(tmp_path)

    observation, _calls = _run_coverage_json_case(
        tmp_path,
        plan=plan,
        coverage_json_exit=returncode,
        coverage_json_error=error,
    )

    assert observation.coverage is not None
    assert observation.coverage.artifact.state == expected_state


@pytest.mark.parametrize(
    ("content", "expected_state"),
    (
        (None, "artifact_missing"),
        (b"not-json", "artifact_invalid"),
        (b"\xff", "artifact_invalid"),
    ),
    ids=("missing", "malformed", "invalid-utf8"),
)
def test_coverage_json_artifact_failures_are_typed(
    tmp_path: Path,
    content: bytes | None,
    expected_state: str,
) -> None:
    observation, _calls = _run_coverage_json_case(
        tmp_path,
        plan=_coverage_run_plan(tmp_path),
        coverage_json_content=content,
    )

    assert observation.coverage is not None
    assert observation.coverage.artifact.state == expected_state
    assert observation.coverage.artifact.content is None


@pytest.mark.parametrize("leaf_kind", ("symlink", "fifo", "oversized"))
def test_coverage_json_rejects_unsafe_or_oversized_artifact(
    tmp_path: Path,
    leaf_kind: str,
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_bytes(b'{"outside":true}')

    def replace_json(run_directory: Path, _data_path: Path) -> None:
        output = run_directory / "coverage.json"
        output.unlink()
        if leaf_kind == "symlink":
            output.symlink_to(outside)
        elif leaf_kind == "fifo":
            _MKFIFO(output)
        else:
            with output.open("wb") as file:
                file.truncate(coverage_execution.MAX_COVERAGE_JSON_BYTES + 1)

    observation, _calls = _run_coverage_json_case(
        tmp_path,
        plan=_coverage_run_plan(tmp_path),
        during_coverage_json=replace_json,
    )

    assert observation.coverage is not None
    assert observation.coverage.artifact.state == "artifact_invalid"
    assert outside.read_bytes() == b'{"outside":true}'


@pytest.mark.parametrize(
    "mutation",
    ("original", "snapshot", "run-shard", "snapshot-shard"),
)
def test_coverage_json_rejects_data_mutation_or_retained_shards_during_reporting(
    tmp_path: Path,
    mutation: str,
) -> None:
    def mutate(run_directory: Path, data_path: Path) -> None:
        if mutation == "original":
            (run_directory / ".coverage").write_bytes(b"changed-evidence")
        elif mutation == "snapshot":
            data_path.write_bytes(b"changed-evidence")
        elif mutation == "run-shard":
            (run_directory / ".coverage.worker").write_bytes(b"shard")
        else:
            (data_path.parent / "coverage-data.worker").write_bytes(b"shard")

    observation, _calls = _run_coverage_json_case(
        tmp_path,
        plan=_coverage_run_plan(tmp_path),
        during_coverage_json=mutate,
    )

    assert observation.coverage is not None
    assert observation.coverage.artifact.state == "unexpected_parallel_data"


@pytest.mark.parametrize(
    ("returncode", "error", "expected_state"),
    (
        (-9, None, "terminated_by_signal"),
        (0, FileNotFoundError("consumer-python"), "spawn_failed"),
    ),
)
def test_coverage_json_process_error_precedes_concurrent_snapshot_shard(
    tmp_path: Path,
    returncode: int,
    error: OSError | None,
    expected_state: str,
) -> None:
    def add_shard(_run_directory: Path, data_path: Path) -> None:
        (data_path.parent / "coverage-data.worker").write_bytes(b"shard")

    observation, _calls = _run_coverage_json_case(
        tmp_path,
        plan=_coverage_run_plan(tmp_path),
        coverage_json_exit=returncode,
        coverage_json_error=error,
        during_coverage_json=add_shard,
    )

    assert observation.coverage is not None
    assert observation.coverage.artifact.state == expected_state


def test_coverage_json_endpoint_does_not_claim_transient_shard_detection(
    tmp_path: Path,
) -> None:
    def create_then_remove_shard(_run_directory: Path, data_path: Path) -> None:
        shard = data_path.parent / "coverage-data.worker"
        shard.write_bytes(b"transient")
        shard.unlink()

    observation, _calls = _run_coverage_json_case(
        tmp_path,
        plan=_coverage_run_plan(tmp_path),
        during_coverage_json=create_then_remove_shard,
    )

    assert observation.coverage is not None
    assert observation.coverage.artifact.state == "snapshot"


@pytest.mark.parametrize("collision", ("report-input", "coverage-json", "root-shard"))
def test_coverage_json_rejects_pre_report_collisions_before_process_spawn(
    tmp_path: Path,
    collision: str,
) -> None:
    def collide(run_directory: Path) -> None:
        if collision == "report-input":
            (run_directory / "report-input").mkdir()
        elif collision == "coverage-json":
            (run_directory / "coverage.json").write_bytes(b"existing")
        else:
            (run_directory / ".coverage.worker").write_bytes(b"shard")

    observation, calls = _run_coverage_json_case(
        tmp_path,
        plan=_coverage_run_plan(tmp_path),
        primary_mutation=collide,
    )

    assert observation.coverage is not None
    assert observation.coverage.artifact.state == "unexpected_parallel_data"
    assert len(calls) == 3


@pytest.mark.parametrize("output_format", ("json", "terminal"))
def test_coverage_json_helper_streams_are_always_captured(
    tmp_path: Path,
    output_format: str,
) -> None:
    plan = replace(
        _coverage_run_plan(tmp_path),
        output_format=cast(OutputFormat, output_format),
    )

    observation, _calls = _run_coverage_json_case(tmp_path, plan=plan)

    process = observation.processes[-1]
    assert process.role == "coverage_json"
    assert process.stdout is not None and process.stdout.tail == b"coverage stdout"
    assert process.stderr is not None and process.stderr.tail == b"coverage stderr"


def test_coverage_run_cleanup_preserves_consumer_files_and_git_status(
    tmp_path: Path,
) -> None:
    consumer_coverage = tmp_path / ".coverage"
    consumer_json = tmp_path / "coverage.json"
    consumer_coverage.write_bytes(b"consumer coverage bytes")
    consumer_json.write_bytes(b"consumer JSON bytes")
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True)  # nosec B603
    subprocess.run(  # nosec B603
        ("git", "add", ".coverage", "coverage.json"), cwd=tmp_path, check=True
    )
    before = subprocess.run(  # nosec B603
        ("git", "status", "--short"),
        cwd=tmp_path,
        check=True,
        capture_output=True,
    ).stdout

    observation, calls = _run_coverage_json_case(
        tmp_path,
        plan=_coverage_run_plan(tmp_path),
    )

    after = subprocess.run(  # nosec B603
        ("git", "status", "--short"),
        cwd=tmp_path,
        check=True,
        capture_output=True,
    ).stdout
    primary_environment = calls[2][2]
    assert primary_environment is not None
    run_directory = Path(primary_environment["COVERAGE_FILE"]).parent
    assert observation.pytest is not None and observation.pytest.cleanup_error is None
    assert not run_directory.exists()
    assert consumer_coverage.read_bytes() == b"consumer coverage bytes"
    assert consumer_json.read_bytes() == b"consumer JSON bytes"
    assert after == before


def test_later_coverage_run_never_reuses_cleaned_workspace(tmp_path: Path) -> None:
    plan = _coverage_run_plan(tmp_path)

    first, first_calls = _run_coverage_json_case(tmp_path, plan=plan)
    second, second_calls = _run_coverage_json_case(tmp_path, plan=plan)

    first_environment = first_calls[2][2]
    second_environment = second_calls[2][2]
    assert first_environment is not None
    assert second_environment is not None
    first_run = Path(first_environment["COVERAGE_FILE"]).parent
    second_run = Path(second_environment["COVERAGE_FILE"]).parent
    assert first.pytest is not None and first.pytest.cleanup_error is None
    assert second.pytest is not None and second.pytest.cleanup_error is None
    assert first_run != second_run
    assert not first_run.exists()
    assert not second_run.exists()


def _coverage_document(*, version: str = "7.15.2") -> bytes:
    return _document(coverage_version=version)


def test_coverage_preflight_runs_after_a_non_spawn_pytest_preflight_failure(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> CompletedProcess[tuple[str, ...]]:
        del cwd, check, capture_output, env
        calls.append(command)
        stdout = _coverage_document() if command[-1] == coverage_execution.COVERAGE_PREFLIGHT_PROBE else b"bad"
        return cast(
            CompletedProcess[tuple[str, ...]],
            CompletedProcess(command, 0, stdout=stdout, stderr=b""),
        )

    result = execute_plan(
        RunPlan(mode="focused", targets=(), checks=(_coverage_check(tmp_path),), output_format="json"),
        runner=runner,
    )

    observation = result.checks[0]
    assert [process.role for process in observation.processes] == [
        "pytest_preflight",
        "coverage_preflight",
    ]
    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == "preflight_invalid"
    assert observation.coverage is not None
    assert observation.coverage.preflight.classification == "supported"
    assert observation.coverage.artifact.state == "data_missing"


def test_coverage_preflight_is_not_attempted_after_pytest_preflight_spawn_failure(
    tmp_path: Path,
) -> None:
    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> CompletedProcess[tuple[str, ...]]:
        del command, cwd, check, capture_output, env
        raise FileNotFoundError("consumer-python")

    result = execute_plan(
        RunPlan(mode="focused", targets=(), checks=(_coverage_check(tmp_path),), output_format="json"),
        runner=runner,
    )

    observation = result.checks[0]
    assert [process.role for process in observation.processes] == ["pytest_preflight"]
    assert observation.coverage is not None
    assert observation.coverage.preflight.classification == "preflight_invalid"


def test_unsupported_coverage_preflight_prevents_a_primary_fallback(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> CompletedProcess[tuple[str, ...]]:
        del cwd, check, capture_output, env
        calls.append(command)
        stdout = _coverage_document(version="7.14.9") if command[-1] == coverage_execution.COVERAGE_PREFLIGHT_PROBE else _pytest_document()
        return cast(
            CompletedProcess[tuple[str, ...]],
            CompletedProcess(command, 0, stdout=stdout, stderr=b""),
        )

    result = execute_plan(
        RunPlan(mode="focused", targets=(), checks=(_coverage_check(tmp_path),), output_format="json"),
        runner=runner,
    )

    observation = result.checks[0]
    assert [process.role for process in observation.processes] == [
        "pytest_preflight",
        "coverage_preflight",
    ]
    assert len(calls) == 2
    assert observation.coverage is not None
    assert observation.coverage.preflight.classification == "unsupported_version"


def test_workspace_capability_failure_attempts_no_preflight_and_retains_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pyrepo_check import pytest_execution

    monkeypatch.setattr(pytest_execution, "_platform_capability_error", lambda: "unsupported")
    calls: list[tuple[str, ...]] = []

    def runner(*args: object, **kwargs: object) -> CompletedProcess[tuple[str, ...]]:
        del args, kwargs
        calls.append(("unexpected",))
        raise AssertionError("runner must not be called")

    result = execute_plan(
        RunPlan(mode="focused", targets=(), checks=(_coverage_check(tmp_path),), output_format="json"),
        runner=runner,
    )

    observation = result.checks[0]
    assert calls == []
    assert observation.processes == ()
    assert observation.coverage is not None
    assert observation.coverage.preflight.classification == "preflight_invalid"
    assert observation.coverage.artifact.state == "not_attempted"
