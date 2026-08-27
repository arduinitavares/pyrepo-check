"""Typed pytest execution observations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import time
from typing import Literal, cast

from pyrepo_check.artifact_safety import (
    _BoundedReadError,
    _MAX_JSON_NESTING as _ARTIFACT_MAX_JSON_NESTING,
    _READ_CHUNK_BYTES,
    _UnsafePathError,
    read_regular_file as _read_regular_file,
    load_bounded_json as _load_bounded_json,
)
from pyrepo_check import execution_workspace
from pyrepo_check.coverage_evidence import coverage_gate_policy
from pyrepo_check.execution import (
    CAPTURE_LIMIT_BYTES,
    CapturedBytes,
    ExecutedCheck,
    ExecutedProcess,
    ProcessRunner,
    execute_process,
)
from pyrepo_check.coverage_execution import (
    CoverageArtifactObservation,
    CoverageDataError,
    CoverageDataSnapshot,
    CoverageExecutionObservation,
    classify_coverage_preflight,
    coverage_environment,
    coverage_json_command,
    coverage_json_environment,
    coverage_preflight_command,
    coverage_primary_command,
    invalid_coverage_observation,
    prepare_coverage_data_snapshot,
    require_coverage_json_destination_absent,
    snapshot_coverage_json,
    verify_coverage_data_snapshot,
)
from pyrepo_check.planning import CoverageExecutionPlan, OutputFormat, PlannedCheck, RunPlan
from pyrepo_check.pytest_evidence import build_pytest_result



_MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
_MAX_WRITER_MARKER_BYTES = 4 * 1024
_MAX_JSON_NESTING = _ARTIFACT_MAX_JSON_NESTING
_MAX_WRITER_DIRECTORY_ENTRIES = 1024
_MINIMUM_PYTHON_VERSION = (3, 13, 15)

_PREFLIGHT_PROBE = """import json
import sys
record = {"schema_version": 1, "python_version": list(sys.version_info[:3]), "pytest_available": False, "pytest_version": None}
if tuple(sys.version_info[:3]) >= (3, 13, 15):
    try:
        import pytest
    except ImportError:
        pass
    else:
        record["pytest_available"] = True
        try:
            record["pytest_version"] = [int(piece) for piece in pytest.__version__.split(".")[:3]]
        except ValueError:
            record["pytest_version"] = []
print(json.dumps(record, separators=(",", ":")))
"""


PreflightClassification = Literal[
    "supported",
    "unsupported_python",
    "module_unavailable",
    "unsupported_version",
    "preflight_invalid",
    "spawn_failed",
    "terminated_by_signal",
    "not_started",
]
ArtifactState = Literal[
    "not_attempted",
    "snapshot",
    "missing",
    "unsafe_path",
    "read_failed",
]


@dataclass(frozen=True)
class PytestPreflightRecord:
    python_version: tuple[int, int, int]
    pytest_available: bool
    pytest_version: tuple[int, int, int] | None


@dataclass(frozen=True)
class PytestPreflightObservation:
    classification: PreflightClassification
    record: PytestPreflightRecord | None
    diagnostic: str | None


@dataclass(frozen=True)
class PytestArtifactObservation:
    state: ArtifactState
    content: bytes | None
    writer_ids: tuple[str, ...]
    diagnostic: str | None


@dataclass(frozen=True)
class PytestExecutionObservation:
    preflight: PytestPreflightObservation
    artifact: PytestArtifactObservation
    cleanup_error: str | None



def execute_pytest(
    check: PlannedCheck,
    *,
    plan: RunPlan | None = None,
    output_format: OutputFormat,
    runner: ProcessRunner | None = None,
    clock_ns: Callable[[], int] = time.monotonic_ns,
) -> ExecutedCheck:
    """Run the consumer preflight probe and retain its typed observation."""
    pytest_plan = check.pytest
    if pytest_plan is None:
        raise ValueError("pytest execution requires PlannedCheck.pytest metadata")
    artifact = PytestArtifactObservation("not_attempted", None, (), None)
    coverage_plan = pytest_plan.coverage
    if coverage_plan is not None and plan is None:
        raise ValueError("planned coverage report generation requires RunPlan")
    coverage: CoverageExecutionObservation | None = (
        invalid_coverage_observation("coverage execution setup did not run")
        if coverage_plan is not None
        else None
    )
    processes: list[ExecutedProcess] = []
    preflight = PytestPreflightObservation(
        "not_started",
        None,
        "pytest execution setup did not run",
    )
    capability_error = execution_workspace._platform_capability_error()
    if capability_error is not None:
        if coverage_plan is not None:
            coverage = invalid_coverage_observation(capability_error)
        return ExecutedCheck(
            planned=check,
            processes=(),
            pytest=PytestExecutionObservation(
                preflight=PytestPreflightObservation(
                    "not_started",
                    None,
                    capability_error,
                ),
                artifact=artifact,
                cleanup_error=None,
            ),
            coverage=coverage,
        )
    try:
        run_directory = execution_workspace.create_run_workspace(check.cwd)
    except OSError as error:
        if coverage_plan is not None:
            coverage = invalid_coverage_observation(f"{type(error).__name__}: {error}")
        return ExecutedCheck(
            planned=check,
            processes=(),
            pytest=PytestExecutionObservation(
                preflight=PytestPreflightObservation(
                    "not_started",
                    None,
                    f"{type(error).__name__}: {error}",
                ),
                artifact=artifact,
                cleanup_error=None,
            ),
            coverage=coverage,
        )

    cleanup_error: str | None = None
    verified_run: execution_workspace.VerifiedRunWorkspace | None = None
    try:
        verified_run = execution_workspace.open_verified_workspace(run_directory)
        verified_run.verify("before preparation")
        plugin_module = f"_pyrepo_check_pytest_{secrets.token_hex(16)}"
        artifact_path, writer_directory = _prepare_run_directory(
            verified_run,
            plugin_module,
        )
        verified_run.verify("after preparation")
        environment = _isolated_environment(
            run_directory.path,
            artifact_path,
            writer_directory,
        )
        coverage_env = (
            coverage_environment(
                environment,
                run_directory=run_directory.path,
                config_path=coverage_plan.config_path.resolve(),
            )
            if coverage_plan is not None
            else None
        )
        verified_run.verify("immediately before preflight")
        process = _run_preflight(
            command=(*pytest_plan.consumer_python, "-c", _PREFLIGHT_PROBE),
            cwd=check.cwd,
            runner=runner,
            clock_ns=clock_ns,
            environment=environment,
        )
        preflight = _classify_preflight(process)
        processes.append(process)
        try:
            verified_run.verify("after pytest preflight")
        except OSError as error:
            preflight = PytestPreflightObservation(
                "preflight_invalid",
                None,
                str(error),
            )
        else:
            if coverage_plan is not None:
                if process.spawn_error is not None:
                    coverage = invalid_coverage_observation(
                        "pytest preflight launcher failed; coverage preflight was not attempted"
                    )
                else:
                    try:
                        verified_run.verify("immediately before coverage preflight")
                    except OSError as error:
                        coverage = invalid_coverage_observation(str(error))
                    else:
                        coverage_process = execute_process(
                            role="coverage_preflight",
                            command=coverage_preflight_command(
                                coverage_plan.consumer_python
                            ),
                            cwd=check.cwd,
                            capture_output=True,
                            runner=runner,
                            clock_ns=clock_ns,
                            environment=coverage_env,
                        )
                        processes.append(coverage_process)
                        coverage_preflight = classify_coverage_preflight(coverage_process)
                        coverage = CoverageExecutionObservation(
                            preflight=coverage_preflight,
                            artifact=CoverageArtifactObservation(
                                "not_attempted", None, None
                            ),
                        )
                        try:
                            verified_run.verify("after coverage preflight")
                        except OSError as error:
                            coverage = invalid_coverage_observation(str(error))
                if (
                    preflight.classification == "supported"
                    and coverage is not None
                    and coverage.preflight.classification == "supported"
                ):
                    if coverage_env is None:
                        raise AssertionError("coverage environment is unavailable")
                    processes.append(
                        _run_primary(
                            command=coverage_primary_command(
                                consumer_python=coverage_plan.consumer_python,
                                config_path=coverage_plan.config_path.resolve(),
                                run_directory=run_directory.path,
                                plugin_module=plugin_module,
                                pytest_args=pytest_plan.pytest_args,
                            ),
                            cwd=check.cwd,
                            runner=runner,
                            clock_ns=clock_ns,
                            environment=coverage_env,
                            capture_output=output_format == "json",
                        )
                    )
                    try:
                        verified_run.verify("after coverage pytest primary")
                    except OSError as error:
                        artifact = PytestArtifactObservation(
                            "unsafe_path", None, (), str(error)
                        )
                    else:
                        artifact = _snapshot_artifact(
                            artifact_path,
                            writer_directory,
                            run_descriptor=verified_run.descriptor,
                        )
                        interim_pytest = PytestExecutionObservation(
                            preflight=preflight,
                            artifact=artifact,
                            cleanup_error=None,
                        )
                        interim_check = ExecutedCheck(
                            planned=check,
                            processes=tuple(processes),
                            pytest=interim_pytest,
                            coverage=coverage,
                        )
                        if plan is None:
                            raise AssertionError("coverage RunPlan is unavailable")
                        pytest_result = build_pytest_result(plan, interim_check)
                        if (
                            pytest_result.error is not None
                            and pytest_result.error.code == "unsupported_parallelism"
                        ):
                            if coverage is None:
                                raise AssertionError("coverage observation is unavailable")
                            coverage = CoverageExecutionObservation(
                                preflight=coverage.preflight,
                                artifact=CoverageArtifactObservation(
                                    "unsupported_parallelism",
                                    None,
                                    "pytest artifact reports unsupported parallel execution",
                                ),
                            )
                        else:
                            policy = coverage_gate_policy(plan, pytest_result, True)
                            if coverage is None:
                                raise AssertionError("coverage observation is unavailable")
                            (
                                coverage_artifact,
                                coverage_json_process,
                                coverage_close_error,
                            ) = _generate_coverage_json(
                                verified_run=verified_run,
                                coverage_plan=coverage_plan,
                                check=check,
                                base_environment=coverage_env,
                                force_fail_under_zero=policy.force_fail_under_zero,
                                retain_threshold_exit_two=(
                                    policy.gate_eligible
                                    and policy.skipped_reason is None
                                ),
                                runner=runner,
                                clock_ns=clock_ns,
                            )
                            if coverage_json_process is not None:
                                processes.append(coverage_json_process)
                            if coverage_close_error is not None:
                                cleanup_error = _combine_diagnostic(
                                    cleanup_error,
                                    coverage_close_error,
                                )
                            coverage = CoverageExecutionObservation(
                                preflight=coverage.preflight,
                                artifact=coverage_artifact,
                                json_exit_code=(
                                    coverage_json_process.returncode
                                    if coverage_json_process is not None
                                    else None
                                ),
                            )
                elif (
                    coverage is not None
                    and coverage.preflight.classification == "supported"
                ):
                    coverage = CoverageExecutionObservation(
                        preflight=coverage.preflight,
                        artifact=CoverageArtifactObservation(
                            "data_missing",
                            None,
                            "coverage data was not produced because pytest primary did not run",
                        ),
                    )
            elif preflight.classification == "supported":
                processes.append(
                    _run_primary(
                        command=(
                            *pytest_plan.consumer_python,
                            "-m",
                            "pytest",
                            "-p",
                            plugin_module,
                            *pytest_plan.pytest_args,
                        ),
                        cwd=check.cwd,
                        runner=runner,
                        clock_ns=clock_ns,
                        environment=environment,
                        capture_output=output_format == "json",
                    )
                )
                try:
                    verified_run.verify("after pytest primary")
                except OSError as error:
                    artifact = PytestArtifactObservation(
                        "unsafe_path",
                        None,
                        (),
                        str(error),
                    )
                else:
                    artifact = _snapshot_artifact(
                        artifact_path,
                        writer_directory,
                        run_descriptor=verified_run.descriptor,
                    )
    except OSError as error:
        preflight = PytestPreflightObservation(
            "not_started",
            None,
            f"{type(error).__name__}: {error}",
        )
    finally:
        if verified_run is not None:
            try:
                verified_run.close()
            except OSError as error:
                if cleanup_error is None:
                    cleanup_error = f"{type(error).__name__}: {error}"
        try:
            cleanup_observation = execution_workspace.remove_run_workspace(
                run_directory,
                repository_root=check.cwd,
                clock_ns=clock_ns,
            )
        except OSError as error:
            cleanup_error = _combine_diagnostic(
                cleanup_error,
                f"{type(error).__name__}: {error}",
            )
        else:
            if cleanup_observation is not None:
                cleanup_error = _combine_diagnostic(
                    cleanup_error,
                    execution_workspace._cleanup_diagnostic(cleanup_observation),
                )
    return ExecutedCheck(
        planned=check,
        processes=tuple(processes),
        pytest=PytestExecutionObservation(
            preflight=preflight,
            artifact=artifact,
            cleanup_error=cleanup_error,
        ),
        coverage=coverage,
    )


def _run_preflight(
    *,
    command: tuple[str, ...],
    cwd: Path,
    runner: ProcessRunner | None,
    clock_ns: Callable[[], int],
    environment: Mapping[str, str],
) -> ExecutedProcess:
    return execute_process(
        role="pytest_preflight",
        command=command,
        cwd=cwd,
        capture_output=True,
        runner=runner,
        clock_ns=clock_ns,
        environment=dict(environment),
    )


def _run_primary(
    *,
    command: tuple[str, ...],
    cwd: Path,
    runner: ProcessRunner | None,
    clock_ns: Callable[[], int],
    environment: Mapping[str, str],
    capture_output: bool,
) -> ExecutedProcess:
    return execute_process(
        role="primary",
        command=command,
        cwd=cwd,
        capture_output=capture_output,
        runner=runner,
        clock_ns=clock_ns,
        environment=dict(environment),
    )


def _generate_coverage_json(
    *,
    verified_run: execution_workspace.VerifiedRunWorkspace,
    coverage_plan: CoverageExecutionPlan,
    check: PlannedCheck,
    base_environment: Mapping[str, str],
    force_fail_under_zero: bool,
    retain_threshold_exit_two: bool,
    runner: ProcessRunner | None,
    clock_ns: Callable[[], int],
) -> tuple[CoverageArtifactObservation, ExecutedProcess | None, str | None]:
    snapshot: CoverageDataSnapshot | None = None
    process: ExecutedProcess | None = None
    coverage_artifact: CoverageArtifactObservation | None = None
    close_error: str | None = None
    try:
        try:
            verified_run.verify("before coverage data snapshot")
            snapshot = prepare_coverage_data_snapshot(
                verified_run.workspace.path,
                run_descriptor=verified_run.descriptor,
            )
            verified_run.verify("before coverage JSON generation")
            verify_coverage_data_snapshot(snapshot)
            require_coverage_json_destination_absent(snapshot)
        except CoverageDataError as error:
            coverage_artifact = CoverageArtifactObservation(
                error.code,
                None,
                error.message,
            )
        except OSError as error:
            coverage_artifact = CoverageArtifactObservation(
                "unexpected_parallel_data",
                None,
                f"coverage workspace validation failed: {type(error).__name__}: {error}",
            )
        else:
            config_path = coverage_plan.config_path.resolve()
            process = execute_process(
                role="coverage_json",
                command=coverage_json_command(
                    consumer_python=coverage_plan.consumer_python,
                    config_path=config_path,
                    data_path=snapshot.data_path,
                    output_path=verified_run.workspace.path / "coverage.json",
                    force_fail_under_zero=force_fail_under_zero,
                ),
                cwd=check.cwd,
                capture_output=True,
                runner=runner,
                clock_ns=clock_ns,
                environment=coverage_json_environment(
                    base_environment,
                    data_path=snapshot.data_path,
                    config_path=config_path,
                ),
            )

            endpoint_error: str | None = None
            try:
                verified_run.verify("after coverage JSON generation")
                verify_coverage_data_snapshot(snapshot)
            except (CoverageDataError, OSError) as error:
                endpoint_error = f"{type(error).__name__}: {error}"

            if process.spawn_error is not None or process.returncode is None:
                coverage_artifact = CoverageArtifactObservation(
                    "spawn_failed",
                    None,
                    process.spawn_error or "coverage JSON process has no exit code",
                )
            elif process.returncode < 0:
                coverage_artifact = CoverageArtifactObservation(
                    "terminated_by_signal",
                    None,
                    f"coverage JSON process terminated by signal {-process.returncode}",
                )
            elif endpoint_error is not None:
                coverage_artifact = CoverageArtifactObservation(
                    "unexpected_parallel_data",
                    None,
                    endpoint_error,
                )
            elif process.returncode != 0 and not (
                process.returncode == 2 and retain_threshold_exit_two
            ):
                coverage_artifact = CoverageArtifactObservation(
                    "generation_failed",
                    None,
                    f"coverage JSON generation exited with code {process.returncode}",
                )
            else:
                coverage_artifact = snapshot_coverage_json(snapshot)
    finally:
        if snapshot is not None:
            try:
                snapshot.close()
            except OSError as error:
                close_error = f"coverage snapshot close failed: {type(error).__name__}: {error}"
    if coverage_artifact is None:
        raise AssertionError("coverage artifact observation is unavailable")
    return coverage_artifact, process, close_error


def _prepare_run_directory(
    verified_run: execution_workspace.VerifiedRunWorkspace,
    plugin_module: str,
) -> tuple[Path, Path]:
    run_directory = verified_run.workspace.path
    plugin_source = Path(__file__).with_name("_pytest_report_plugin.py")
    plugin_path = run_directory / f"{plugin_module}.py"
    _copy_plugin_source(
        plugin_source,
        plugin_path.name,
        run_descriptor=verified_run.descriptor,
    )
    writer_directory = run_directory / "writers"
    os.mkdir(writer_directory.name, mode=0o700, dir_fd=verified_run.descriptor)
    return run_directory / "artifact.json", writer_directory


def _copy_plugin_source(
    source: Path,
    destination_name: str,
    *,
    run_descriptor: int,
) -> None:
    no_follow = cast(int, getattr(os, "O_NOFOLLOW"))
    descriptor = os.open(
        destination_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow,
        0o600,
        dir_fd=run_descriptor,
    )
    try:
        os.set_inheritable(descriptor, False)
        with source.open("rb") as plugin_source:
            while chunk := plugin_source.read(_READ_CHUNK_BYTES):
                written = 0
                while written < len(chunk):
                    count = os.write(descriptor, chunk[written:])
                    if count <= 0:
                        raise OSError("plugin copy made no forward progress")
                    written += count
    finally:
        os.close(descriptor)



def _isolated_environment(
    run_directory: Path,
    artifact_path: Path,
    writer_directory: Path,
) -> dict[str, str]:
    environment = dict(os.environ)
    for name in tuple(environment):
        if name in {"COVERAGE_PROCESS_CONFIG", "COVERAGE_PROCESS_START"} or name.startswith(
            "COV_CORE_"
        ):
            del environment[name]
    existing_pythonpath = environment.get("PYTHONPATH")
    plugin_path = str(run_directory)
    environment["PYTHONPATH"] = (
        f"{existing_pythonpath}{os.pathsep}{plugin_path}"
        if existing_pythonpath
        else plugin_path
    )
    environment["PYREPO_CHECK_PYTEST_JSON"] = str(artifact_path)
    environment["PYREPO_CHECK_PYTEST_WRITER_DIR"] = str(writer_directory)
    return environment


def _snapshot_artifact(
    artifact_path: Path,
    writer_directory: Path,
    *,
    run_descriptor: int | None = None,
) -> PytestArtifactObservation:
    writer_ids, marker_diagnostic = _snapshot_writer_ids(
        writer_directory,
        run_descriptor=run_descriptor,
    )
    try:
        content = _read_regular_file(
            artifact_path,
            max_bytes=_MAX_ARTIFACT_BYTES,
            dir_fd=run_descriptor,
        )
    except FileNotFoundError:
        return PytestArtifactObservation("missing", None, writer_ids, marker_diagnostic)
    except _UnsafePathError as error:
        return PytestArtifactObservation(
            "unsafe_path",
            None,
            writer_ids,
            _combine_diagnostic(marker_diagnostic, str(error)),
        )
    except OSError as error:
        return PytestArtifactObservation(
            "read_failed",
            None,
            writer_ids,
            _combine_diagnostic(marker_diagnostic, f"artifact read failed: {error}"),
        )
    return PytestArtifactObservation("snapshot", content, writer_ids, marker_diagnostic)


def _snapshot_writer_ids(
    writer_directory: Path,
    *,
    run_descriptor: int | None = None,
) -> tuple[tuple[str, ...], str | None]:
    writer_id: str | None = None
    diagnostics: list[str] = []
    writer_descriptor: int | None = None
    try:
        try:
            if run_descriptor is None:
                entries = os.scandir(writer_directory)
            else:
                writer_descriptor = os.open(
                    writer_directory.name,
                    execution_workspace._secure_directory_open_flags(),
                    dir_fd=run_descriptor,
                )
                os.set_inheritable(writer_descriptor, False)
                entries = os.scandir(writer_descriptor)
        except OSError as error:
            return (), f"writer inventory failed: {error}"
        marker_seen = False
        try:
            with entries:
                for entry_count, entry in enumerate(entries, start=1):
                    retained_ids = (writer_id,) if writer_id is not None else ()
                    if entry_count > _MAX_WRITER_DIRECTORY_ENTRIES:
                        diagnostics.append(
                            f"writer directory contains more than "
                            f"{_MAX_WRITER_DIRECTORY_ENTRIES} entries"
                        )
                        return retained_ids, "; ".join(diagnostics)
                    marker_id = _marker_id(entry.name)
                    if marker_id is None:
                        continue
                    if marker_seen:
                        diagnostics.append("multiple writer markers were found")
                        return retained_ids, "; ".join(diagnostics)
                    marker_seen = True
                    try:
                        loaded_document = _load_bounded_json(
                            _read_regular_file(
                                Path(entry.path)
                                if writer_descriptor is None
                                else Path(entry.name),
                                max_bytes=_MAX_WRITER_MARKER_BYTES,
                                dir_fd=writer_descriptor,
                            )
                        )
                    except (
                        _UnsafePathError,
                        _BoundedReadError,
                        OSError,
                        UnicodeDecodeError,
                        json.JSONDecodeError,
                        ValueError,
                    ) as error:
                        diagnostics.append(f"writer marker is malformed: {entry.name}: {error}")
                        continue
                    if not isinstance(loaded_document, dict):
                        diagnostics.append(
                            f"writer marker is malformed: {entry.name}: root must be an object"
                        )
                        continue
                    document = cast(dict[object, object], loaded_document)
                    schema_version = document.get("schema_version")
                    document_writer_id = document.get("writer_id")
                    pid = document.get("pid")
                    if type(schema_version) is not int or schema_version != 1:
                        diagnostics.append(
                            f"writer marker is malformed: {entry.name}: "
                            "schema_version must be integer 1"
                        )
                        continue
                    if not isinstance(document_writer_id, str):
                        diagnostics.append(
                            f"writer marker is malformed: {entry.name}: "
                            "writer_id must be a string"
                        )
                        continue
                    if type(pid) is not int or pid < 0:
                        diagnostics.append(
                            f"writer marker is malformed: {entry.name}: "
                            "pid must be a non-negative integer"
                        )
                        continue
                    if document_writer_id != marker_id:
                        diagnostics.append(f"writer marker ID mismatch: {entry.name}")
                        continue
                    writer_id = marker_id
        except OSError as error:
            retained_ids = (writer_id,) if writer_id is not None else ()
            qualification = (
                f" after validated writer {writer_id}" if writer_id is not None else ""
            )
            diagnostics.append(
                f"writer inventory failed{qualification}: {type(error).__name__}: {error}"
            )
            return retained_ids, "; ".join(diagnostics)
        return ((writer_id,) if writer_id is not None else ()), "; ".join(diagnostics) or None
    finally:
        if writer_descriptor is not None:
            try:
                os.close(writer_descriptor)
            except OSError:
                pass


def _marker_id(name: str) -> str | None:
    prefix = "pytest-writer-"
    suffix = ".json"
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    marker_id = name[len(prefix) : -len(suffix)]
    if not marker_id or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
        for character in marker_id
    ):
        return None
    return marker_id



def _combine_diagnostic(first: str | None, second: str) -> str:
    return f"{first}; {second}" if first else second


def _classify_preflight(process: ExecutedProcess) -> PytestPreflightObservation:
    if process.spawn_error is not None:
        return PytestPreflightObservation("spawn_failed", None, process.spawn_error)
    if process.returncode is not None and process.returncode < 0:
        return PytestPreflightObservation(
            "terminated_by_signal",
            None,
            f"preflight terminated by signal {-process.returncode}",
        )
    if process.returncode != 0:
        return PytestPreflightObservation(
            "preflight_invalid",
            None,
            f"preflight exited with code {process.returncode}",
        )
    if any(stream is not None and stream.omitted_bytes > 0 for stream in (process.stdout, process.stderr)):
        return PytestPreflightObservation(
            "preflight_invalid",
            None,
            f"preflight output exceeds {CAPTURE_LIMIT_BYTES} bytes",
        )
    try:
        record = _parse_preflight_record(process.stdout)
    except ValueError as error:
        return PytestPreflightObservation("preflight_invalid", None, str(error))
    if record.python_version < _MINIMUM_PYTHON_VERSION:
        return PytestPreflightObservation("unsupported_python", record, None)
    if not record.pytest_available:
        return PytestPreflightObservation("module_unavailable", record, None)
    if record.pytest_version is None or record.pytest_version[0] != 8:
        return PytestPreflightObservation("unsupported_version", record, None)
    return PytestPreflightObservation("supported", record, None)


def _parse_preflight_record(output: CapturedBytes | None) -> PytestPreflightRecord:
    if output is None:
        raise ValueError("preflight emitted no output")
    if output.omitted_bytes > 0:
        raise ValueError(f"preflight output exceeds {CAPTURE_LIMIT_BYTES} bytes")
    try:
        lines = output.tail.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ValueError("preflight output is not valid UTF-8") from error
    if len(lines) != 1:
        raise ValueError("preflight must emit exactly one JSON line")
    try:
        document = json.loads(lines[0])
    except json.JSONDecodeError as error:
        raise ValueError("preflight JSON is malformed") from error
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "python_version",
        "pytest_available",
        "pytest_version",
    }:
        raise ValueError("preflight JSON does not match schema version 1")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("preflight JSON does not match schema version 1")
    python_version = _parse_version(document["python_version"])
    pytest_available = document["pytest_available"]
    if not isinstance(pytest_available, bool):
        raise ValueError("preflight JSON does not match schema version 1")
    pytest_version_value = document["pytest_version"]
    if pytest_available:
        pytest_version = _parse_version(pytest_version_value)
    elif pytest_version_value is None:
        pytest_version = None
    else:
        raise ValueError("preflight JSON does not match schema version 1")
    return PytestPreflightRecord(python_version, pytest_available, pytest_version)


def _parse_version(value: object) -> tuple[int, int, int]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("preflight JSON does not match schema version 1")
    version: list[int] = []
    for piece in value:
        if not isinstance(piece, int) or isinstance(piece, bool) or piece < 0:
            raise ValueError("preflight JSON does not match schema version 1")
        version.append(piece)
    return (version[0], version[1], version[2])


def _duration_ms(started_ns: int, ended_ns: int) -> int:
    return (max(0, ended_ns - started_ns) + 500_000) // 1_000_000
