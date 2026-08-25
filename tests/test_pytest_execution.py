from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess  # nosec B404
import tempfile
from typing import cast

import pytest

from pyrepo_check.execution import execute_plan
from pyrepo_check.planning import OutputFormat, PlannedCheck, PytestExecutionPlan, RunPlan
import pyrepo_check.pytest_execution as pytest_execution
from pyrepo_check.pytest_execution import PYTEST_PLUGIN_MODULE, execute_pytest


def pytest_check(tmp_path: Path) -> PlannedCheck:
    pytest = PytestExecutionPlan(
        consumer_python=("consumer-python",),
        pytest_args=("tests",),
    )
    return PlannedCheck(
        name="pytest",
        command=(*pytest.consumer_python, "-m", "pytest", *pytest.pytest_args),
        cwd=tmp_path,
        pytest=pytest,
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
    created_run_directory = pytest_execution._RunDirectory(
        run_directory,
        pytest_execution._directory_identity(run_directory),
    )
    monkeypatch.setattr(
        pytest_execution,
        "_create_run_directory",
        lambda _consumer_root: created_run_directory,
    )
    return run_directory


@pytest.mark.parametrize(
    ("stdout", "returncode", "error", "classification"),
    [
        (preflight_document(), 0, None, "supported"),
        (preflight_document(python_version=[3, 13, 14]), 0, None, "unsupported_python"),
        (preflight_document(pytest_available=False, pytest_version=None), 0, None, "module_unavailable"),
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

    observation = execute_pytest(pytest_check(tmp_path), output_format="json", runner=runner)

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

    observation = execute_pytest(pytest_check(tmp_path), output_format="json", runner=runner)

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

    observation = execute_pytest(pytest_check(tmp_path), output_format="json", runner=runner)

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

    observation = execute_pytest(pytest_check(tmp_path), output_format="json", runner=runner)

    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == "preflight_invalid"
    assert len(observation.processes) == 1


@pytest.mark.parametrize(("output_format", "primary_capture"), [("json", True), ("terminal", False)])
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
        stdout = preflight_document() if len(calls) == 1 else b"primary-output"
        return completed(command, 0, stdout=stdout, stderr=b"")

    check = pytest_check(tmp_path)
    result = execute_plan(
        RunPlan(mode="focused", targets=(), checks=(check,), output_format=output_format),
        runner=runner,
    )

    assert [call[0] for call in calls] == [
        ("consumer-python", "-c", calls[0][0][-1]),
        ("consumer-python", "-m", "pytest", "-p", PYTEST_PLUGIN_MODULE, "tests"),
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
    expected_banner = "\n==> pytest: consumer-python -m pytest tests\n"
    assert capsys.readouterr().out == ("" if output_format == "json" else expected_banner)


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
        RunPlan(mode="focused", targets=(), checks=(pytest_check(tmp_path),)),
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
        if env is not None and command[1:3] == ("-m", "pytest"):
            artifact_path = Path(env["PYREPO_CHECK_PYTEST_JSON"])
            writer_directory = Path(env["PYREPO_CHECK_PYTEST_WRITER_DIR"])
            artifact_path.write_bytes(b'{"raw":true}')
            (writer_directory / "pytest-writer-z.json").write_text('{"writer_id":"z"}')
            (writer_directory / "pytest-writer-a.json").write_text('{"writer_id":"a"}')
        return completed(
            command,
            0,
            stdout=preflight_document() if command[1] == "-c" else b"",
            stderr=b"",
        )

    observation = execute_pytest(pytest_check(tmp_path), output_format="json", runner=runner)

    assert observation.pytest is not None
    assert observation.pytest.artifact.state == "snapshot"
    assert observation.pytest.artifact.content == b'{"raw":true}'
    assert observation.pytest.artifact.writer_ids == ("a", "z")
    assert observation.pytest.cleanup_error is None
    assert not run_directory.exists()


@pytest.mark.parametrize(
    ("marker_payloads", "writer_ids", "diagnostic"),
    [
        ({}, (), None),
        ({"pytest-writer-one.json": '{"writer_id":"one"}'}, ("one",), None),
        (
            {
                "pytest-writer-b.json": '{"writer_id":"b"}',
                "pytest-writer-a.json": '{"writer_id":"a"}',
            },
            ("a", "b"),
            None,
        ),
        ({"pytest-writer-bad.json": "not-json"}, (), "malformed"),
        ({"pytest-writer-one.json": '{"writer_id":"other"}'}, (), "ID mismatch"),
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
        if env is not None and command[1:3] == ("-m", "pytest"):
            Path(env["PYREPO_CHECK_PYTEST_JSON"]).write_bytes(b"artifact")
            writer_directory = Path(env["PYREPO_CHECK_PYTEST_WRITER_DIR"])
            for name, payload in marker_payloads.items():
                (writer_directory / name).write_text(payload)
        return completed(
            command,
            0,
            stdout=preflight_document() if command[1] == "-c" else b"",
            stderr=b"",
        )

    observation = execute_pytest(pytest_check(tmp_path), output_format="json", runner=runner)

    assert observation.pytest is not None
    assert observation.pytest.artifact.state == "snapshot"
    assert observation.pytest.artifact.writer_ids == writer_ids
    if diagnostic is None:
        assert observation.pytest.artifact.diagnostic is None
    else:
        assert diagnostic in (observation.pytest.artifact.diagnostic or "")


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
        if env is not None and command[1:3] == ("-m", "pytest") and make_artifact:
            Path(env["PYREPO_CHECK_PYTEST_JSON"]).symlink_to(outside_artifact)
        if env is not None and command[1:3] == ("-m", "pytest") and spawn:
            raise FileNotFoundError("consumer-python")
        return completed(
            command,
            0 if command[1] == "-c" else primary_returncode,
            stdout=preflight_document() if command[1] == "-c" else b"",
            stderr=b"",
        )

    observation = execute_pytest(pytest_check(tmp_path), output_format="json", runner=runner)

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
        if not args and not kwargs and Path(os.fsdecode(path)).name == "artifact.json":
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
        if env is not None and command[1:3] == ("-m", "pytest"):
            Path(env["PYREPO_CHECK_PYTEST_JSON"]).write_bytes(b"artifact")
        return completed(
            command,
            0,
            stdout=preflight_document() if command[1] == "-c" else b"",
            stderr=b"",
        )

    observation = execute_pytest(pytest_check(tmp_path), output_format="json", runner=runner)

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

    def replacement_race(
        path: str | bytes | os.PathLike[str],
        flags: int,
        *args: int,
        **kwargs: int | None,
    ) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)
        target = Path(os.fsdecode(path))
        if not args and not kwargs and target.name == "artifact.json":
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
        del cwd, check, capture_output
        if env is not None and command[1:3] == ("-m", "pytest"):
            Path(env["PYREPO_CHECK_PYTEST_JSON"]).write_bytes(b"captured-content")
        return completed(
            command,
            0,
            stdout=preflight_document() if command[1] == "-c" else b"",
        )

    observation = execute_pytest(pytest_check(tmp_path), output_format="json", runner=runner)

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
    replacement.write_text('{"writer_id":"attacker"}')
    open_calls: list[Path] = []
    original_open = pytest_execution.os.open

    def replacement_race(
        path: str | bytes | os.PathLike[str],
        flags: int,
        *args: int,
        **kwargs: int | None,
    ) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)
        target = Path(os.fsdecode(path))
        if not args and not kwargs and target.name == "pytest-writer-safe.json":
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
        del cwd, check, capture_output
        if env is not None and command[1:3] == ("-m", "pytest"):
            Path(env["PYREPO_CHECK_PYTEST_JSON"]).write_bytes(b"artifact")
            writer_directory = Path(env["PYREPO_CHECK_PYTEST_WRITER_DIR"])
            (writer_directory / "pytest-writer-safe.json").write_text('{"writer_id":"safe"}')
        return completed(
            command,
            0,
            stdout=preflight_document() if command[1] == "-c" else b"",
        )

    observation = execute_pytest(pytest_check(tmp_path), output_format="json", runner=runner)

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
        run_directory: pytest_execution._RunDirectory,
        *,
        consumer_root: Path,
    ) -> None:
        del run_directory, consumer_root
        raise PermissionError("cleanup denied")

    monkeypatch.setattr(pytest_execution, "_remove_run_directory", failing_cleanup)

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        del cwd, check, capture_output
        if env is not None and command[1:3] == ("-m", "pytest"):
            Path(env["PYREPO_CHECK_PYTEST_JSON"]).write_bytes(b"artifact")
        return completed(
            command,
            0,
            stdout=preflight_document() if command[1] == "-c" else b"",
            stderr=b"",
        )

    try:
        observation = execute_pytest(pytest_check(tmp_path), output_format="json", runner=runner)
    finally:
        shutil.rmtree(run_directory, ignore_errors=True)

    assert observation.pytest is not None
    assert observation.pytest.artifact.content == b"artifact"
    assert observation.pytest.cleanup_error == "PermissionError: cleanup denied"


def test_setup_failure_is_observed_and_the_created_run_directory_is_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = safe_run_directory(tmp_path, monkeypatch)

    def fail_copy(source: Path, destination: Path) -> None:
        del source, destination
        raise PermissionError("plugin copy denied")

    monkeypatch.setattr(pytest_execution.shutil, "copyfile", fail_copy)

    observation = execute_pytest(pytest_check(tmp_path), output_format="json")

    assert observation.processes == ()
    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == "spawn_failed"
    assert observation.pytest.artifact.state == "not_attempted"
    assert not run_directory.exists()


def test_consumer_tmpdir_is_not_used_for_the_run_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pytest_execution.tempfile, "gettempdir", lambda: str(tmp_path))
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
            stdout=preflight_document() if command[1] == "-c" else b"",
        )

    observation = execute_pytest(pytest_check(tmp_path), output_format="json", runner=runner)

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

    monkeypatch.setattr(pytest_execution.tempfile, "mkdtemp", fail_mkdtemp)

    observation = execute_pytest(pytest_check(tmp_path), output_format="json")

    assert observation.processes == ()
    assert observation.pytest is not None
    assert observation.pytest.preflight.classification == "not_started"
    assert observation.pytest.preflight.diagnostic == "PermissionError: temporary directory denied"
    assert observation.pytest.artifact.state == "not_attempted"


def test_rejected_consumer_root_run_directories_are_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_directories: list[Path] = []

    def create_in_consumer_root(**_kwargs: object) -> str:
        run_directory = tmp_path / f"run-directory-{len(created_directories)}"
        run_directory.mkdir()
        created_directories.append(run_directory)
        return str(run_directory)

    monkeypatch.setattr(pytest_execution.tempfile, "mkdtemp", create_in_consumer_root)

    with pytest.raises(OSError, match="inside consumer root"):
        pytest_execution._create_run_directory(tmp_path)

    assert created_directories
    assert all(not directory.exists() for directory in created_directories)


def test_rejected_run_directory_cleanup_failure_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_directories: list[Path] = []
    original_rmdir = pytest_execution.os.rmdir

    def create_in_consumer_root(**_kwargs: object) -> str:
        run_directory = tmp_path / f"run-directory-{len(created_directories)}"
        run_directory.mkdir()
        created_directories.append(run_directory)
        return str(run_directory)

    def deny_rmdir(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
    ) -> None:
        if Path(os.fsdecode(path)).name.startswith("run-directory-"):
            raise PermissionError("run directory cleanup denied")
        original_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(pytest_execution.tempfile, "mkdtemp", create_in_consumer_root)
    monkeypatch.setattr(pytest_execution.os, "rmdir", deny_rmdir)

    with pytest.raises(OSError, match="cleanup failed: PermissionError: run directory cleanup denied"):
        pytest_execution._create_run_directory(tmp_path)

    for directory in created_directories:
        original_rmdir(directory)


def test_rejected_run_directory_cleanup_failure_stops_before_later_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_directories: list[Path] = []
    original_mkdtemp = tempfile.mkdtemp
    original_rmdir = pytest_execution.os.rmdir

    def create_candidate(
        *,
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | os.PathLike[str] | None = None,
    ) -> str:
        if not created_directories:
            run_directory = tmp_path / "rejected-run-directory"
            run_directory.mkdir()
        else:
            run_directory = Path(original_mkdtemp(suffix=suffix, prefix=prefix, dir=dir))
        created_directories.append(run_directory)
        return str(run_directory)

    def deny_rejected_rmdir(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
    ) -> None:
        if Path(os.fsdecode(path)) == created_directories[0]:
            raise PermissionError("rejected candidate cleanup denied")
        original_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(pytest_execution.tempfile, "mkdtemp", create_candidate)
    monkeypatch.setattr(pytest_execution.os, "rmdir", deny_rejected_rmdir)

    try:
        with pytest.raises(
            OSError,
            match="cleanup failed: PermissionError: rejected candidate cleanup denied",
        ):
            pytest_execution._create_run_directory(tmp_path)
    finally:
        for directory in created_directories:
            if directory.exists():
                original_rmdir(directory)

    assert created_directories == [tmp_path / "rejected-run-directory"]


def test_open_run_directory_closes_descriptor_when_identity_check_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = tmp_path / "run-directory"
    run_directory.mkdir()
    record = pytest_execution._RunDirectory(
        run_directory,
        pytest_execution._directory_identity(run_directory),
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
            pytest_execution._open_run_directory(record)
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
        if env is not None and command[1:3] == ("-m", "pytest"):
            shutil.rmtree(run_directory)
            run_directory.mkdir()
            replacement_file.write_text("do not delete")
        return completed(
            command,
            0,
            stdout=preflight_document() if command[1] == "-c" else b"",
        )

    try:
        observation = execute_pytest(pytest_check(tmp_path), output_format="json", runner=runner)
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
    original_verify = pytest_execution._verify_directory_identity
    replaced = False

    def replace_after_verification(path: Path, identity: tuple[int, int]) -> None:
        nonlocal replaced
        original_verify(path, identity)
        if path == run_directory and not replaced:
            replaced = True
            path.rename(displaced_directory)
            path.mkdir()
            replacement_file.write_text("do not delete")

    monkeypatch.setattr(
        pytest_execution,
        "_verify_directory_identity",
        replace_after_verification,
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
            stdout=preflight_document() if command[1] == "-c" else b"",
        )

    try:
        observation = execute_pytest(
            pytest_check(tmp_path),
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

    def deny_plugin_unlink(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal descriptor_relative_failure
        if os.fsdecode(path) == f"{PYTEST_PLUGIN_MODULE}.py":
            descriptor_relative_failure = dir_fd is not None
            raise PermissionError("plugin deletion denied")
        original_unlink(path, dir_fd=dir_fd)

    def track_rmdir(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
    ) -> None:
        if dir_fd is None:
            top_level_removals.append(Path(os.fsdecode(path)))
        original_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(pytest_execution.os, "unlink", deny_plugin_unlink)
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
            stdout=preflight_document() if command[1] == "-c" else b"",
        )

    try:
        observation = execute_pytest(
            pytest_check(tmp_path),
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
    assert observation.pytest.cleanup_error == "PermissionError: plugin deletion denied"
