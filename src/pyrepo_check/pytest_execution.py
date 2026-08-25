"""Typed pytest execution observations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess  # nosec B404
import tempfile
import time
from typing import Literal, cast

from pyrepo_check.execution import ExecutedCheck, ExecutedProcess, ProcessRunner
from pyrepo_check.planning import OutputFormat, PlannedCheck


_PREFLIGHT_LIMIT_BYTES = 65_536
_MINIMUM_PYTHON_VERSION = (3, 13, 15)
PYTEST_PLUGIN_MODULE = "pyrepo_check_pytest_evidence_plugin"
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
    output_format: OutputFormat,
    runner: ProcessRunner = subprocess.run,
    clock_ns: Callable[[], int] = time.monotonic_ns,
) -> ExecutedCheck:
    """Run the consumer preflight probe and retain its typed observation."""
    pytest_plan = check.pytest
    if pytest_plan is None:
        raise ValueError("pytest execution requires PlannedCheck.pytest metadata")
    run_directory = Path(tempfile.mkdtemp(prefix="pyrepo-check-pytest-"))
    artifact = PytestArtifactObservation("not_attempted", None, (), None)
    cleanup_error: str | None = None
    processes: list[ExecutedProcess] = []
    preflight = PytestPreflightObservation(
        "spawn_failed",
        None,
        "pytest execution setup did not run",
    )
    try:
        artifact_path, writer_directory = _prepare_run_directory(run_directory)
        environment = _isolated_environment(
            run_directory,
            artifact_path,
            writer_directory,
        )
        process = _run_preflight(
            command=(*pytest_plan.consumer_python, "-c", _PREFLIGHT_PROBE),
            cwd=check.cwd,
            runner=runner,
            clock_ns=clock_ns,
            environment=environment,
        )
        preflight = _classify_preflight(process)
        processes.append(process)
        if preflight.classification == "supported":
            processes.append(
                _run_primary(
                    command=(
                        *pytest_plan.consumer_python,
                        "-m",
                        "pytest",
                        "-p",
                        PYTEST_PLUGIN_MODULE,
                        *pytest_plan.pytest_args,
                    ),
                    cwd=check.cwd,
                    runner=runner,
                    clock_ns=clock_ns,
                    environment=environment,
                    capture_output=output_format == "json",
                )
            )
            artifact = _snapshot_artifact(artifact_path, writer_directory)
    except OSError as error:
        preflight = PytestPreflightObservation(
            "spawn_failed",
            None,
            f"{type(error).__name__}: {error}",
        )
    finally:
        try:
            _remove_run_directory(run_directory, consumer_root=check.cwd)
        except OSError as error:
            cleanup_error = f"{type(error).__name__}: {error}"
    return ExecutedCheck(
        planned=check,
        processes=tuple(processes),
        pytest=PytestExecutionObservation(
            preflight=preflight,
            artifact=artifact,
            cleanup_error=cleanup_error,
        ),
    )


def _run_preflight(
    *,
    command: tuple[str, ...],
    cwd: Path,
    runner: ProcessRunner,
    clock_ns: Callable[[], int],
    environment: Mapping[str, str],
) -> ExecutedProcess:
    started_ns = clock_ns()
    returncode: int | None = None
    stdout: bytes | None = None
    stderr: bytes | None = None
    spawn_error: str | None = None
    try:
        completed = runner(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            env=environment,
        )
        returncode = completed.returncode
        stdout = _as_bytes(cast(bytes | str | None, completed.stdout))
        stderr = _as_bytes(cast(bytes | str | None, completed.stderr))
    except OSError as error:
        spawn_error = f"{type(error).__name__}: {error}"
    return ExecutedProcess(
        role="pytest_preflight",
        command=command,
        cwd=cwd,
        returncode=returncode,
        duration_ms=_duration_ms(started_ns, clock_ns()),
        stdout=stdout,
        stderr=stderr,
        spawn_error=spawn_error,
    )


def _run_primary(
    *,
    command: tuple[str, ...],
    cwd: Path,
    runner: ProcessRunner,
    clock_ns: Callable[[], int],
    environment: Mapping[str, str],
    capture_output: bool,
) -> ExecutedProcess:
    started_ns = clock_ns()
    returncode: int | None = None
    stdout: bytes | None = None
    stderr: bytes | None = None
    spawn_error: str | None = None
    try:
        completed = runner(
            command,
            cwd=cwd,
            check=False,
            capture_output=capture_output,
            env=environment,
        )
        returncode = completed.returncode
        if capture_output:
            stdout = _as_bytes(cast(bytes | str | None, completed.stdout))
            stderr = _as_bytes(cast(bytes | str | None, completed.stderr))
    except OSError as error:
        spawn_error = f"{type(error).__name__}: {error}"
    return ExecutedProcess(
        role="primary",
        command=command,
        cwd=cwd,
        returncode=returncode,
        duration_ms=_duration_ms(started_ns, clock_ns()),
        stdout=stdout,
        stderr=stderr,
        spawn_error=spawn_error,
    )


def _prepare_run_directory(run_directory: Path) -> tuple[Path, Path]:
    plugin_source = Path(__file__).with_name("_pytest_report_plugin.py")
    plugin_path = run_directory / f"{PYTEST_PLUGIN_MODULE}.py"
    shutil.copyfile(plugin_source, plugin_path)
    os.chmod(plugin_path, 0o600)
    writer_directory = run_directory / "writers"
    writer_directory.mkdir(mode=0o700)
    return run_directory / "artifact.json", writer_directory


def _isolated_environment(
    run_directory: Path,
    artifact_path: Path,
    writer_directory: Path,
) -> dict[str, str]:
    environment = dict(os.environ)
    for name in tuple(environment):
        if name == "COVERAGE_PROCESS_START" or name.startswith("COV_CORE_"):
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
) -> PytestArtifactObservation:
    writer_ids, marker_diagnostic = _snapshot_writer_ids(writer_directory)
    try:
        artifact_stat = artifact_path.lstat()
    except FileNotFoundError:
        return PytestArtifactObservation("missing", None, writer_ids, marker_diagnostic)
    except OSError as error:
        return PytestArtifactObservation(
            "read_failed",
            None,
            writer_ids,
            _combine_diagnostic(marker_diagnostic, f"artifact lstat failed: {error}"),
        )
    if not stat.S_ISREG(artifact_stat.st_mode):
        return PytestArtifactObservation(
            "unsafe_path",
            None,
            writer_ids,
            _combine_diagnostic(marker_diagnostic, "artifact is not a regular file"),
        )
    try:
        content = artifact_path.read_bytes()
    except OSError as error:
        return PytestArtifactObservation(
            "read_failed",
            None,
            writer_ids,
            _combine_diagnostic(marker_diagnostic, f"artifact read failed: {error}"),
        )
    return PytestArtifactObservation("snapshot", content, writer_ids, marker_diagnostic)


def _snapshot_writer_ids(writer_directory: Path) -> tuple[tuple[str, ...], str | None]:
    writer_ids: list[str] = []
    diagnostics: list[str] = []
    try:
        marker_paths = tuple(writer_directory.iterdir())
    except OSError as error:
        return (), f"writer inventory failed: {error}"
    for marker_path in marker_paths:
        marker_id = _marker_id(marker_path.name)
        if marker_id is None:
            continue
        try:
            marker_stat = marker_path.lstat()
        except OSError as error:
            diagnostics.append(f"writer marker lstat failed: {marker_path.name}: {error}")
            continue
        if not stat.S_ISREG(marker_stat.st_mode):
            diagnostics.append(f"writer marker is not a regular file: {marker_path.name}")
            continue
        try:
            document = json.loads(marker_path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            diagnostics.append(f"writer marker is malformed: {marker_path.name}: {error}")
            continue
        if not isinstance(document, dict) or document.get("writer_id") != marker_id:
            diagnostics.append(f"writer marker ID mismatch: {marker_path.name}")
            continue
        writer_ids.append(marker_id)
    return tuple(sorted(writer_ids)), "; ".join(diagnostics) or None


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


def _remove_run_directory(run_directory: Path, *, consumer_root: Path) -> None:
    if run_directory.resolve() == consumer_root.resolve():
        raise OSError("refusing to remove consumer root")
    shutil.rmtree(run_directory)


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


def _parse_preflight_record(output: bytes | None) -> PytestPreflightRecord:
    if output is None:
        raise ValueError("preflight emitted no output")
    if len(output) > _PREFLIGHT_LIMIT_BYTES:
        raise ValueError("preflight output exceeds 65536 bytes")
    try:
        lines = output.decode("utf-8").splitlines()
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
    if document["schema_version"] != 1:
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


def _as_bytes(output: bytes | str | None) -> bytes | None:
    return output.encode() if isinstance(output, str) else output


def _duration_ms(started_ns: int, ended_ns: int) -> int:
    return (max(0, ended_ns - started_ns) + 500_000) // 1_000_000
