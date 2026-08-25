"""Typed pytest execution observations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import errno
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
class _RunDirectory:
    path: Path
    identity: tuple[int, int]


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
    artifact = PytestArtifactObservation("not_attempted", None, (), None)
    processes: list[ExecutedProcess] = []
    preflight = PytestPreflightObservation(
        "not_started",
        None,
        "pytest execution setup did not run",
    )
    try:
        run_directory = _create_run_directory(check.cwd)
    except OSError as error:
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
        )

    cleanup_error: str | None = None
    try:
        artifact_path, writer_directory = _prepare_run_directory(run_directory.path)
        environment = _isolated_environment(
            run_directory.path,
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


def _create_run_directory(consumer_root: Path) -> _RunDirectory:
    resolved_root = consumer_root.resolve()
    candidates = (Path(tempfile.gettempdir()), Path("/tmp"), Path("/var/tmp"))  # nosec B108
    last_error: OSError | None = None
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            base = candidate.resolve(strict=True)
        except OSError as error:
            last_error = error
            continue
        if base in seen or not base.is_dir() or _is_within(base, resolved_root):
            continue
        seen.add(base)
        try:
            run_directory = Path(
                tempfile.mkdtemp(prefix="pyrepo-check-pytest-", dir=base)
            )
        except OSError as error:
            last_error = error
            continue
        identity: tuple[int, int] | None = None
        try:
            identity = _directory_identity(run_directory)
            resolved_run_directory = run_directory.resolve(strict=True)
            if _is_within(resolved_run_directory, resolved_root):
                raise OSError("refusing run directory inside consumer root")
        except OSError as error:
            cleanup_error = _remove_empty_created_run_directory(run_directory, identity)
            last_error = _with_cleanup_error(error, cleanup_error)
            if cleanup_error is not None:
                raise last_error
            continue
        if identity is None:
            error = OSError("created run directory identity is unavailable")
            cleanup_error = _remove_empty_created_run_directory(run_directory, identity)
            last_error = _with_cleanup_error(error, cleanup_error)
            if cleanup_error is not None:
                raise last_error
            continue
        return _RunDirectory(run_directory, identity)
    if last_error is not None:
        raise last_error
    raise OSError("no safe operating-system temporary directory is available")


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
) -> PytestArtifactObservation:
    writer_ids, marker_diagnostic = _snapshot_writer_ids(writer_directory)
    try:
        content = _read_regular_file(artifact_path)
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
            document = json.loads(_read_regular_file(marker_path).decode("utf-8"))
        except (_UnsafePathError, OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
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


def _remove_run_directory(run_directory: _RunDirectory, *, consumer_root: Path) -> None:
    descriptor = _open_run_directory(run_directory)
    try:
        resolved_run_directory = run_directory.path.resolve(strict=True)
        if _is_within(resolved_run_directory, consumer_root.resolve()):
            raise OSError("refusing to remove consumer root or its contents")
        if not shutil.rmtree.avoids_symlink_attacks:
            raise OSError("symlink-safe recursive removal is unavailable")
        _verify_directory_identity(run_directory.path, run_directory.identity)
        try:
            shutil.rmtree(
                ".",
                dir_fd=descriptor,
                onexc=_retain_open_run_directory,
            )
        except _RecursiveCleanupFailure as failure:
            raise failure.error from failure
        _verify_directory_identity(run_directory.path, run_directory.identity)
        os.rmdir(run_directory.path)
    finally:
        os.close(descriptor)


def _open_run_directory(run_directory: _RunDirectory) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory_only = getattr(os, "O_DIRECTORY", None)
    if type(no_follow) is not int or type(directory_only) is not int:
        raise OSError("safe directory opening is unavailable")
    descriptor = os.open(
        run_directory.path,
        os.O_RDONLY | no_follow | directory_only,
    )
    try:
        os.set_inheritable(descriptor, False)
        identity = _status_identity(os.fstat(descriptor))
    except BaseException:
        os.close(descriptor)
        raise
    if identity != run_directory.identity:
        os.close(descriptor)
        raise OSError("run directory identity mismatch")
    return descriptor


def _retain_open_run_directory(
    function: Callable[..., object],
    path: str,
    error: BaseException,
) -> None:
    if function is os.rmdir and path == "." and isinstance(error, OSError):
        if error.errno == errno.EINVAL:
            return
    if isinstance(error, OSError):
        raise _RecursiveCleanupFailure(error)
    raise error


def _remove_empty_created_run_directory(
    run_directory: Path,
    identity: tuple[int, int] | None,
) -> OSError | None:
    if identity is None:
        return OSError("created run directory identity is unavailable")
    try:
        _verify_directory_identity(run_directory, identity)
        os.rmdir(run_directory)
    except OSError as error:
        return error
    return None


def _directory_identity(path: Path) -> tuple[int, int]:
    file_status = os.lstat(path)
    if not stat.S_ISDIR(file_status.st_mode):
        raise OSError("run directory is not a directory")
    return _status_identity(file_status)


def _status_identity(file_status: os.stat_result) -> tuple[int, int]:
    return file_status.st_dev, file_status.st_ino


def _verify_directory_identity(path: Path, identity: tuple[int, int]) -> None:
    if _directory_identity(path) != identity:
        raise OSError("run directory identity mismatch")


def _with_cleanup_error(error: OSError, cleanup_error: OSError | None) -> OSError:
    if cleanup_error is None:
        return error
    return OSError(
        f"{error}; cleanup failed: {type(cleanup_error).__name__}: {cleanup_error}"
    )


class _UnsafePathError(OSError):
    """Raised when descriptor-safe regular-file reading is unavailable or fails."""


class _RecursiveCleanupFailure(Exception):
    """Carry an inner cleanup error past shutil's root-path error rewriting."""

    def __init__(self, error: OSError) -> None:
        super().__init__(str(error))
        self.error = error


def _read_regular_file(path: Path) -> bytes:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if type(no_follow) is not int:
        raise _UnsafePathError("safe no-follow file opening is unavailable")
    try:
        descriptor = os.open(path, os.O_RDONLY | no_follow)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise _UnsafePathError(f"path is not a regular file: {path.name}") from error
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _UnsafePathError(f"path is not a regular file: {path.name}")
        with os.fdopen(descriptor, "rb") as file:
            descriptor = -1
            return file.read()
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _is_within(path: Path, root: Path) -> bool:
    return path.is_relative_to(root)


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
    if any(
        stream is not None and len(stream) > _PREFLIGHT_LIMIT_BYTES
        for stream in (process.stdout, process.stderr)
    ):
        return PytestPreflightObservation(
            "preflight_invalid",
            None,
            "preflight output exceeds 65536 bytes",
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


def _as_bytes(output: bytes | str | None) -> bytes | None:
    return output.encode() if isinstance(output, str) else output


def _duration_ms(started_ns: int, ended_ns: int) -> int:
    return (max(0, ended_ns - started_ns) + 500_000) // 1_000_000
