"""Typed pytest execution observations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import secrets
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
from pyrepo_check.check_launcher import (
    StagedCheckLauncher,
    build_launcher_command,
    ensure_staged_launcher,
    ensure_start_marker_absent,
    validate_start_marker,
)
from pyrepo_check.coverage_evidence import coverage_gate_policy
from pyrepo_check.execution import (
    CheckExecutionFailure,
    CheckModule,
    CheckStartObservation,
    DependencyObservation,
    ExecutedCheck,
    ExecutedProcess,
    PreparedRepositoryEnvironment,
    ProcessRunner,
    TerminalWriter,
    execute_process,
    format_terminal_check_banner,
)
from pyrepo_check.coverage_execution import (
    CoverageArtifactObservation,
    CoverageDataError,
    CoverageDataSnapshot,
    CoverageExecutionObservation,
    CoveragePreflightObservation,
    CoveragePreflightRecord,
    coverage_environment,
    coverage_json_command,
    coverage_json_environment,
    coverage_primary_arguments,
    invalid_coverage_observation,
    prepare_coverage_data_snapshot,
    require_coverage_json_destination_absent,
    snapshot_coverage_json,
    verify_coverage_data_snapshot,
)
from pyrepo_check.execution_workspace import VerifiedRunWorkspace
from pyrepo_check.planning import CheckInvocation, CoverageExecutionPlan, OutputFormat, RunPlan
from pyrepo_check.pytest_evidence import build_pytest_result
from pyrepo_check.repository_environment import (
    SUPPORTED_DEPENDENCIES,
    dependency_version_supported,
    locked_repository_prefix,
)


_MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
_MAX_WRITER_MARKER_BYTES = 4 * 1024
_MAX_JSON_NESTING = _ARTIFACT_MAX_JSON_NESTING
_MAX_WRITER_DIRECTORY_ENTRIES = 1024

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


@dataclass(frozen=True)
class PreparedPytestExecution:
    processes: tuple[ExecutedProcess, ...]
    start: CheckStartObservation | None
    error: CheckExecutionFailure | None
    pytest: PytestExecutionObservation | None
    coverage: CoverageExecutionObservation | None


def execute_prepared_pytest(
    check: CheckInvocation,
    *,
    plan: RunPlan,
    prepared: PreparedRepositoryEnvironment,
    pytest_dependency: DependencyObservation,
    coverage_dependency: DependencyObservation | None,
    workspace: VerifiedRunWorkspace,
    launcher: StagedCheckLauncher,
    output_format: OutputFormat,
    runner: ProcessRunner | None,
    clock_ns: Callable[[], int],
    terminal_writer: TerminalWriter | None = None,
) -> PreparedPytestExecution:
    """Run pytest through one already prepared Repository Environment."""
    pytest_plan = check.pytest
    if pytest_plan is None:
        raise ValueError("pytest execution requires CheckInvocation.pytest metadata")

    pytest_preflight = _prepared_pytest_preflight(prepared, pytest_dependency)
    artifact = PytestArtifactObservation("not_attempted", None, (), None)
    coverage_plan = pytest_plan.coverage
    coverage = _prepared_coverage_observation(
        prepared,
        coverage_dependency,
        requested=coverage_plan is not None,
    )
    if pytest_preflight.classification != "supported":
        if coverage is not None:
            coverage = invalid_coverage_observation(
                "coverage was not attempted because pytest is unavailable"
            )
        return PreparedPytestExecution(
            processes=(),
            start=None,
            error=pytest_dependency.error
            or CheckExecutionFailure(
                "pytest_preflight_failed",
                pytest_preflight.diagnostic or "pytest dependency is unavailable",
                None,
            ),
            pytest=PytestExecutionObservation(pytest_preflight, artifact, None),
            coverage=coverage,
        )

    try:
        workspace.verify("before prepared pytest reporter staging")
        plugin_module = f"_pyrepo_check_pytest_{secrets.token_hex(16)}"
        artifact_path, writer_directory = _prepare_run_directory(workspace, plugin_module)
        workspace.verify("after prepared pytest reporter staging")
    except OSError as error:
        diagnostic = f"{type(error).__name__}: {error}"
        return PreparedPytestExecution(
            processes=(),
            start=None,
            error=CheckExecutionFailure("pytest_evidence_error", diagnostic, None),
            pytest=PytestExecutionObservation(
                PytestPreflightObservation("not_started", None, diagnostic),
                artifact,
                None,
            ),
            coverage=invalid_coverage_observation(diagnostic) if coverage is not None else None,
        )

    environment = _prepared_pytest_environment(
        prepared.child_environment,
        workspace.workspace.path,
        artifact_path,
        writer_directory,
    )
    instrumented = (
        coverage_plan is not None
        and coverage is not None
        and coverage.preflight.classification == "supported"
    )
    if instrumented:
        if coverage_plan is None:
            raise AssertionError("instrumented pytest requires a Coverage execution plan")
        primary_arguments = coverage_primary_arguments(
            config_path=coverage_plan.config_path.resolve(),
            run_directory=workspace.workspace.path,
            plugin_module=plugin_module,
            pytest_args=pytest_plan.pytest_args,
        )
        module: CheckModule = "coverage"
        primary_environment = coverage_environment(
            environment,
            run_directory=workspace.workspace.path,
            config_path=coverage_plan.config_path.resolve(),
        )
    else:
        primary_arguments = ("-p", plugin_module, *pytest_plan.pytest_args)
        module = "pytest"
        primary_environment = environment

    primary_invocation = replace(check, arguments=primary_arguments)
    process, start, execution_error = _run_prepared_primary(
        primary_invocation,
        module=module,
        prepared=prepared,
        workspace=workspace,
        launcher=launcher,
        environment=primary_environment,
        capture_output=output_format == "json",
        runner=runner,
        clock_ns=clock_ns,
        terminal_writer=terminal_writer,
        banner_arguments=pytest_plan.pytest_args,
    )
    if process is None:
        return PreparedPytestExecution(
            processes=(),
            start=None,
            error=execution_error,
            pytest=PytestExecutionObservation(pytest_preflight, artifact, None),
            coverage=coverage,
        )
    processes = [process]
    try:
        workspace.verify("after prepared pytest primary")
    except OSError as error:
        artifact = PytestArtifactObservation("unsafe_path", None, (), str(error))
    else:
        artifact = _snapshot_artifact(
            artifact_path,
            writer_directory,
            run_descriptor=workspace.descriptor,
        )

    pytest_observation = PytestExecutionObservation(pytest_preflight, artifact, None)
    if instrumented and coverage is not None:
        reserved_pytest_exit = (
            start is not None
            and process.spawn_error is None
            and process.returncode in {2, 3, 4}
        )
        if execution_error is not None and not reserved_pytest_exit:
            coverage = replace(
                coverage,
                artifact=CoverageArtifactObservation(
                    "data_missing",
                    None,
                    "coverage data was not produced by a trusted pytest primary",
                ),
            )
        else:
            interim = ExecutedCheck(
                planned=check,
                processes=tuple(processes),
                pytest=pytest_observation,
                coverage=coverage,
            )
            pytest_result = build_pytest_result(
                plan,
                interim,
                dependency_version=pytest_dependency.version,
            )
            if (
                pytest_result.error is not None
                and pytest_result.error.code == "unsupported_parallelism"
            ):
                coverage = replace(
                    coverage,
                    artifact=CoverageArtifactObservation(
                        "unsupported_parallelism",
                        None,
                        "pytest artifact reports unsupported parallel execution",
                    ),
                )
            else:
                if coverage_plan is None:
                    raise AssertionError("coverage plan is unavailable")
                policy = coverage_gate_policy(plan, pytest_result, True)
                coverage_artifact, coverage_process, close_error = _generate_coverage_json(
                    verified_run=workspace,
                    plan=plan,
                    coverage_plan=coverage_plan,
                    check=check,
                    base_environment=primary_environment,
                    python_prefix=locked_repository_prefix(prepared),
                    force_fail_under_zero=(
                        policy.force_fail_under_zero or reserved_pytest_exit
                    ),
                    retain_threshold_exit_two=(
                        not reserved_pytest_exit
                        and policy.gate_eligible
                        and policy.skipped_reason is None
                    ),
                    runner=runner,
                    clock_ns=clock_ns,
                )
                if coverage_process is not None:
                    processes.append(coverage_process)
                if close_error is not None:
                    pytest_observation = replace(
                        pytest_observation,
                        cleanup_error=close_error,
                    )
                coverage = replace(
                    coverage,
                    artifact=coverage_artifact,
                    json_exit_code=(
                        coverage_process.returncode if coverage_process is not None else None
                    ),
                )

    return PreparedPytestExecution(
        processes=tuple(processes),
        start=start,
        error=execution_error,
        pytest=pytest_observation,
        coverage=coverage,
    )


def _generate_coverage_json(
    *,
    verified_run: execution_workspace.VerifiedRunWorkspace,
    plan: RunPlan,
    coverage_plan: CoverageExecutionPlan,
    check: CheckInvocation,
    base_environment: Mapping[str, str],
    python_prefix: tuple[str, ...],
    force_fail_under_zero: bool,
    retain_threshold_exit_two: bool,
    runner: ProcessRunner | None,
    clock_ns: Callable[[], int],
) -> tuple[CoverageArtifactObservation, ExecutedProcess | None, str | None]:
    del check
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
                    python_prefix=python_prefix,
                    config_path=config_path,
                    data_path=snapshot.data_path,
                    output_path=verified_run.workspace.path / "coverage.json",
                    force_fail_under_zero=force_fail_under_zero,
                ),
                cwd=plan.root,
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
                close_error = (
                    f"coverage snapshot close failed: {type(error).__name__}: {error}"
                )
    if coverage_artifact is None:
        raise AssertionError("coverage artifact observation is unavailable")
    return coverage_artifact, process, close_error


def _prepared_pytest_preflight(
    prepared: PreparedRepositoryEnvironment,
    dependency: DependencyObservation,
) -> PytestPreflightObservation:
    diagnostic = dependency.error.message if dependency.error is not None else None
    version = _dependency_numeric_version(dependency.version)
    if dependency.status == "available" and dependency_version_supported(
        SUPPORTED_DEPENDENCIES["pytest"], dependency.version
    ):
        if version is None:
            raise AssertionError("supported pytest dependency has no legacy version transport")
        return PytestPreflightObservation(
            "supported",
            PytestPreflightRecord(prepared.python.version, True, version),
            None,
        )
    if dependency.status == "missing":
        return PytestPreflightObservation(
            "module_unavailable",
            PytestPreflightRecord(prepared.python.version, False, None),
            diagnostic,
        )
    if dependency.status == "incompatible" and version is not None:
        return PytestPreflightObservation(
            "unsupported_version",
            PytestPreflightRecord(prepared.python.version, True, version),
            diagnostic,
        )
    return PytestPreflightObservation(
        "preflight_invalid",
        None,
        diagnostic or "pytest dependency evidence is unusable",
    )


def _prepared_coverage_observation(
    prepared: PreparedRepositoryEnvironment,
    dependency: DependencyObservation | None,
    *,
    requested: bool,
) -> CoverageExecutionObservation | None:
    if not requested:
        return None
    if dependency is None:
        return invalid_coverage_observation("coverage dependency evidence is unavailable")
    diagnostic = dependency.error.message if dependency.error is not None else None
    if dependency.status == "available" and dependency_version_supported(
        SUPPORTED_DEPENDENCIES["coverage"], dependency.version
    ):
        if dependency.version is None:
            raise AssertionError("supported coverage dependency has no version")
        preflight = CoveragePreflightObservation(
            "supported",
            CoveragePreflightRecord(prepared.python.version, True, dependency.version),
            None,
        )
    elif dependency.status == "missing":
        preflight = CoveragePreflightObservation(
            "module_unavailable",
            CoveragePreflightRecord(prepared.python.version, False, None),
            diagnostic,
        )
    elif dependency.status == "incompatible" and dependency.version is not None:
        preflight = CoveragePreflightObservation(
            "unsupported_version",
            CoveragePreflightRecord(prepared.python.version, True, dependency.version),
            diagnostic,
        )
    else:
        preflight = CoveragePreflightObservation(
            "preflight_invalid",
            None,
            diagnostic or "coverage dependency evidence is unusable",
        )
    return CoverageExecutionObservation(
        preflight=preflight,
        artifact=CoverageArtifactObservation("not_attempted", None, None),
    )


def _dependency_numeric_version(version: str | None) -> tuple[int, int, int] | None:
    if version is None:
        return None
    try:
        parts = tuple(int(part) for part in version.split(".")[:3])
    except ValueError:
        return None
    if not parts or any(part < 0 for part in parts):
        return None
    return cast(tuple[int, int, int], (*parts, *(0 for _ in range(3 - len(parts)))))


def _prepared_pytest_environment(
    base_environment: Mapping[str, str],
    run_directory: Path,
    artifact_path: Path,
    writer_directory: Path,
) -> dict[str, str]:
    environment = dict(base_environment)
    environment.pop("PYTHONPATH", None)
    for name in tuple(environment):
        if name in {"COVERAGE_PROCESS_CONFIG", "COVERAGE_PROCESS_START"} or name.startswith(
            "COV_CORE_"
        ):
            del environment[name]
    environment["PYTHONPATH"] = str(run_directory)
    environment["PYREPO_CHECK_PYTEST_JSON"] = str(artifact_path)
    environment["PYREPO_CHECK_PYTEST_WRITER_DIR"] = str(writer_directory)
    return environment


def _run_prepared_primary(
    invocation: CheckInvocation,
    *,
    module: CheckModule,
    prepared: PreparedRepositoryEnvironment,
    workspace: VerifiedRunWorkspace,
    launcher: StagedCheckLauncher,
    environment: Mapping[str, str],
    capture_output: bool,
    runner: ProcessRunner | None,
    clock_ns: Callable[[], int],
    terminal_writer: TerminalWriter | None,
    banner_arguments: tuple[str, ...],
) -> tuple[ExecutedProcess | None, CheckStartObservation | None, CheckExecutionFailure | None]:
    marker_path = workspace.workspace.path / f"check-start-{secrets.token_hex(16)}.json"
    try:
        ensure_staged_launcher(launcher, workspace=workspace)
        ensure_start_marker_absent(marker_path, workspace=workspace)
    except OSError as error:
        return (
            None,
            None,
            CheckExecutionFailure(
                "check_start_evidence_invalid",
                f"Check start evidence could not be prepared: {error}",
                None,
            ),
        )

    if terminal_writer is not None:
        terminal_writer(format_terminal_check_banner(invocation.name, "pytest", banner_arguments))
    process = execute_process(
        role="primary",
        command=build_launcher_command(
            prepared,
            launcher,
            invocation,
            marker_path,
            module=module,
            use_observed_python_executable=True,
        ),
        cwd=prepared.root,
        capture_output=capture_output,
        runner=runner,
        clock_ns=clock_ns,
        environment=dict(environment),
    )
    start: CheckStartObservation | None = None
    marker_error: OSError | None = None
    try:
        start = validate_start_marker(
            marker_path,
            workspace=workspace,
            invocation=invocation,
            module=module,
            prepared=prepared,
        )
    except OSError as error:
        marker_error = error

    if process.spawn_error is not None or process.returncode is None:
        return (
            process,
            None,
            CheckExecutionFailure(
                "spawn_failed",
                f"Check process could not be spawned: {process.spawn_error}",
                None,
            ),
        )
    if start is None:
        return (
            process,
            None,
            CheckExecutionFailure(
                "check_start_evidence_invalid",
                f"Check start evidence is invalid: {marker_error}",
                "Retry after verifying the locked Repository Environment.",
            ),
        )
    if process.returncode < 0:
        return (
            process,
            start,
            CheckExecutionFailure(
                "terminated_by_signal",
                f"Check process terminated by signal {-process.returncode}.",
                None,
            ),
        )
    if process.returncode not in {0, 1, 5}:
        return (
            process,
            start,
            CheckExecutionFailure(
                "check_execution_failed",
                f"Check process exited with reserved error status {process.returncode}.",
                None,
            ),
        )
    return process, start, None



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
                                Path(entry.path) if writer_descriptor is None else Path(entry.name),
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
                            f"writer marker is malformed: {entry.name}: writer_id must be a string"
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
            qualification = f" after validated writer {writer_id}" if writer_id is not None else ""
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



def _duration_ms(started_ns: int, ended_ns: int) -> int:
    return (max(0, ended_ns - started_ns) + 500_000) // 1_000_000
