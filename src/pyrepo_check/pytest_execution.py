"""Typed pytest execution observations."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile
import time
from types import MappingProxyType, ModuleType
from typing import Literal, Never, Protocol, cast

from pyrepo_check.execution import (
    CAPTURE_LIMIT_BYTES,
    CapturedBytes,
    ExecutedCheck,
    ExecutedProcess,
    ProcessRunner,
    execute_process,
)
from pyrepo_check.planning import OutputFormat, PlannedCheck

if sys.platform == "darwin":
    import fcntl as _fcntl
else:
    _fcntl: ModuleType | None = None


_MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
_MAX_WRITER_MARKER_BYTES = 4 * 1024
_MAX_JSON_NESTING = 64
_MAX_WRITER_DIRECTORY_ENTRIES = 1024
_READ_CHUNK_BYTES = 64 * 1024
_MAX_CLEANUP_ENTRIES = 4096
_MAX_CLEANUP_DEPTH = 64
_MAX_CLEANUP_DURATION_NS = 5_000_000_000
_MINIMUM_PYTHON_VERSION = (3, 13, 15)
_SCANDIR_SUPPORTS_FD = os.scandir in os.supports_fd
_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd
_STAT_SUPPORTS_DIR_FD = os.stat in os.supports_dir_fd
_STAT_SUPPORTS_FOLLOW_SYMLINKS = os.stat in os.supports_follow_symlinks
_UNLINK_SUPPORTS_DIR_FD = os.unlink in os.supports_dir_fd
_RMDIR_SUPPORTS_DIR_FD = os.rmdir in os.supports_dir_fd
_MKDIR_SUPPORTS_DIR_FD = os.mkdir in os.supports_dir_fd
_RENAME_SUPPORTS_DIR_FD = os.rename in os.supports_dir_fd
_DARWIN_GETPATH_UNLINK_PROOF = (
    sys.platform == "darwin"
    and _fcntl is not None
    and callable(getattr(_fcntl, "fcntl", None))
    and type(getattr(_fcntl, "F_GETPATH", None)) is int
)
_POST_RMDIR_UNLINK_PROOF = sys.platform.startswith("linux") or _DARWIN_GETPATH_UNLINK_PROOF
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
    parent_identity: tuple[int, int]


@dataclass
class _VerifiedRunDirectory:
    run_directory: _RunDirectory
    parent_descriptor: int
    descriptor: int

    def verify(self, gate: str) -> None:
        message = f"run directory identity mismatch {gate}"
        try:
            parent_status = os.fstat(self.parent_descriptor)
            lexical_parent_status = os.stat(
                self.run_directory.path.parent,
                follow_symlinks=False,
            )
            relative_status = os.stat(
                self.run_directory.path.name,
                dir_fd=self.parent_descriptor,
                follow_symlinks=False,
            )
            descriptor_status = os.fstat(self.descriptor)
            lexical_status = os.stat(
                self.run_directory.path,
                follow_symlinks=False,
            )
        except OSError as error:
            raise OSError(message) from error
        if (
            _status_identity(parent_status) != self.run_directory.parent_identity
            or not stat.S_ISDIR(parent_status.st_mode)
            or _status_identity(lexical_parent_status)
            != self.run_directory.parent_identity
            or not stat.S_ISDIR(lexical_parent_status.st_mode)
            or _status_identity(relative_status) != self.run_directory.identity
            or not stat.S_ISDIR(relative_status.st_mode)
            or _status_identity(descriptor_status) != self.run_directory.identity
            or not stat.S_ISDIR(descriptor_status.st_mode)
            or _status_identity(lexical_status) != self.run_directory.identity
            or not stat.S_ISDIR(lexical_status.st_mode)
        ):
            raise OSError(message)

    def close(self) -> None:
        run_error: OSError | None = None
        try:
            os.close(self.descriptor)
        except OSError as error:
            run_error = error
        try:
            os.close(self.parent_descriptor)
        except OSError:
            if run_error is None:
                raise
        if run_error is not None:
            raise run_error


CleanupFailureKind = Literal["budget_exceeded", "unsafe_tree", "io_failed"]
CleanupEntryType = Literal["directory", "symlink", "regular", "other"]
CleanupManifestKey = tuple[tuple[int, int], str]


class _ScandirIterator(Iterator[os.DirEntry[str]], Protocol):
    def close(self) -> None: ...


@dataclass(frozen=True)
class _CleanupObservation:
    kind: CleanupFailureKind
    message: str
    retained_run_path: Path | None
    retained_quarantine_path: Path | None

    @property
    def retained_path(self) -> Path | None:
        """Compatibility alias for the original private cleanup observation."""
        return self.retained_run_path


class _CleanupFailure(OSError):
    def __init__(self, kind: CleanupFailureKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class _QuarantineSetupFailure(_CleanupFailure):
    def __init__(
        self,
        kind: CleanupFailureKind,
        message: str,
        quarantine: _QuarantineDirectory,
    ) -> None:
        super().__init__(kind, message)
        self.quarantine = quarantine


@dataclass(frozen=True)
class _CleanupManifestEntry:
    identity: tuple[int, int]
    file_type: CleanupEntryType


@dataclass(frozen=True)
class _CleanupManifest:
    entries: Mapping[CleanupManifestKey, _CleanupManifestEntry]


@dataclass
class _QuarantineDirectory:
    name: str
    identity: tuple[int, int]
    descriptor: int | None
    may_contain_data: bool = False
    ever_contained_data: bool = False
    removed: bool = False
    cleanup_allowed: bool = True


@dataclass
class _CleanupBudget:
    started_ns: int
    clock_ns: Callable[[], int]
    entries: int = 0
    quarantine: _QuarantineDirectory | None = None

    def observe_entry(self, *, depth: int) -> None:
        self.entries += 1
        if self.entries > _MAX_CLEANUP_ENTRIES:
            raise _CleanupFailure(
                "budget_exceeded",
                f"cleanup entry limit exceeded ({_MAX_CLEANUP_ENTRIES})",
            )
        if depth > _MAX_CLEANUP_DEPTH:
            raise _CleanupFailure(
                "budget_exceeded",
                f"cleanup depth limit exceeded ({_MAX_CLEANUP_DEPTH})",
            )

    def check_deadline(self) -> None:
        if self.clock_ns() - self.started_ns > _MAX_CLEANUP_DURATION_NS:
            raise _CleanupFailure(
                "budget_exceeded",
                f"cleanup duration limit exceeded ({_MAX_CLEANUP_DURATION_NS} ns)",
            )


@dataclass
class _CleanupFrame:
    descriptor: int
    entries: _ScandirIterator
    depth: int
    name: str | None
    identity: tuple[int, int]
    parent_descriptor: int


def execute_pytest(
    check: PlannedCheck,
    *,
    output_format: OutputFormat,
    runner: ProcessRunner | None = None,
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
    capability_error = _platform_capability_error()
    if capability_error is not None:
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
    verified_run: _VerifiedRunDirectory | None = None
    try:
        verified_run = _open_verified_run_directory(run_directory)
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
            if preflight.classification == "supported":
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
            cleanup_observation = _remove_run_directory(
                run_directory,
                consumer_root=check.cwd,
                clock_ns=clock_ns,
            )
        except OSError as error:
            cleanup_error = f"{type(error).__name__}: {error}"
        else:
            if cleanup_observation is not None:
                cleanup_error = _cleanup_diagnostic(cleanup_observation)
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


def _prepare_run_directory(
    verified_run: _VerifiedRunDirectory,
    plugin_module: str,
) -> tuple[Path, Path]:
    run_directory = verified_run.run_directory.path
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
            parent_identity = _directory_identity(base)
        except OSError as error:
            last_error = error
            continue
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
            resolved_parent = resolved_run_directory.parent
            if (
                resolved_parent != base
                or _directory_identity(resolved_parent) != parent_identity
            ):
                raise OSError("created run directory parent identity mismatch")
            if _is_within(resolved_run_directory, resolved_root):
                raise OSError("refusing run directory inside consumer root")
            record = _RunDirectory(run_directory, identity, parent_identity)
        except OSError as error:
            cleanup_error = _remove_empty_created_run_directory(
                run_directory,
                identity,
                parent_identity,
            )
            last_error = _with_cleanup_error(error, cleanup_error)
            if cleanup_error is not None:
                raise last_error
            continue
        if identity is None:
            error = OSError("created run directory identity is unavailable")
            cleanup_error = _remove_empty_created_run_directory(
                run_directory,
                identity,
                parent_identity,
            )
            last_error = _with_cleanup_error(error, cleanup_error)
            if cleanup_error is not None:
                raise last_error
            continue
        return record
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
                    _secure_directory_open_flags(),
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


def _remove_run_directory(
    run_directory: _RunDirectory,
    *,
    consumer_root: Path,
    clock_ns: Callable[[], int] = time.monotonic_ns,
) -> _CleanupObservation | None:
    parent_descriptor: int | None = None
    parent_verified = False
    quarantine: _QuarantineDirectory | None = None
    observation: _CleanupObservation | None = None
    close_error: OSError | None = None
    started_ns = clock_ns()
    try:
        if _is_within(run_directory.path.absolute(), consumer_root.resolve()):
            raise _CleanupFailure(
                "unsafe_tree",
                "refusing to remove consumer root or its contents",
            )
        parent_descriptor = _open_verified_parent(run_directory)
        parent_verified = True
        quarantine = _create_quarantine_directory(
            parent_descriptor,
            expected_device=run_directory.identity[0],
            budget=_CleanupBudget(started_ns, clock_ns),
        )
        manifest = _walk_cleanup_tree(
            parent_descriptor,
            run_directory.path.name,
            run_directory.identity,
            budget=_CleanupBudget(started_ns, clock_ns),
            delete=False,
        )
        deletion_budget = _CleanupBudget(
            started_ns,
            clock_ns,
            quarantine=quarantine,
        )
        _walk_cleanup_tree(
            parent_descriptor,
            run_directory.path.name,
            run_directory.identity,
            budget=deletion_budget,
            delete=True,
            manifest=manifest,
        )
        _remove_verified_relative_directory(
            parent_descriptor,
            run_directory.path.name,
            run_directory.identity,
            expected_device=run_directory.identity[0],
            budget=deletion_budget,
            identity_mismatch_message="run directory identity mismatch before root removal",
        )
        _remove_held_relative_directory(
            parent_descriptor,
            quarantine,
            budget=deletion_budget,
            identity_mismatch_message=(
                "quarantine directory identity mismatch before removal"
            ),
        )
    except _CleanupFailure as error:
        if isinstance(error, _QuarantineSetupFailure):
            quarantine = error.quarantine
        cleanup_message = error.message
        if (
            quarantine is not None
            and not quarantine.removed
            and not quarantine.may_contain_data
            and not quarantine.ever_contained_data
            and quarantine.cleanup_allowed
            and parent_descriptor is not None
        ):
            try:
                _remove_held_relative_directory(
                    parent_descriptor,
                    quarantine,
                    budget=_CleanupBudget(started_ns, clock_ns),
                    identity_mismatch_message=(
                        "quarantine directory identity mismatch before failure cleanup"
                    ),
                )
            except OSError as cleanup_error:
                cleanup_message = (
                    f"{cleanup_message}; empty quarantine cleanup failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
        retained_run_path = _verified_retained_path(
            run_directory,
            parent_descriptor if parent_verified else None,
        )
        retained_quarantine_path = _verified_quarantine_path(
            run_directory,
            quarantine,
            parent_descriptor if parent_verified else None,
        )
        observation = _CleanupObservation(
            error.kind,
            cleanup_message,
            retained_run_path,
            retained_quarantine_path,
        )
    except OSError as error:
        cleanup_message = f"{type(error).__name__}: {error}"
        if (
            quarantine is not None
            and not quarantine.removed
            and not quarantine.may_contain_data
            and not quarantine.ever_contained_data
            and quarantine.cleanup_allowed
            and parent_descriptor is not None
        ):
            try:
                _remove_held_relative_directory(
                    parent_descriptor,
                    quarantine,
                    budget=_CleanupBudget(started_ns, clock_ns),
                    identity_mismatch_message=(
                        "quarantine directory identity mismatch before failure cleanup"
                    ),
                )
            except OSError as cleanup_error:
                cleanup_message = (
                    f"{cleanup_message}; empty quarantine cleanup failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
        retained_run_path = _verified_retained_path(
            run_directory,
            parent_descriptor if parent_verified else None,
        )
        retained_quarantine_path = _verified_quarantine_path(
            run_directory,
            quarantine,
            parent_descriptor if parent_verified else None,
        )
        observation = _CleanupObservation(
            "io_failed",
            cleanup_message,
            retained_run_path,
            retained_quarantine_path,
        )
    finally:
        if quarantine is not None and quarantine.descriptor is not None:
            try:
                os.close(quarantine.descriptor)
            except OSError as error:
                close_error = error
        if parent_descriptor is not None:
            try:
                os.close(parent_descriptor)
            except OSError as error:
                if close_error is None:
                    close_error = error
    if observation is not None:
        return observation
    if close_error is not None:
        return _CleanupObservation(
            "io_failed",
            f"{type(close_error).__name__}: {close_error}",
            None,
            None,
        )
    return None


def _walk_cleanup_tree(
    parent_descriptor: int,
    root_name: str,
    root_identity: tuple[int, int],
    *,
    budget: _CleanupBudget,
    delete: bool,
    manifest: _CleanupManifest | None = None,
) -> _CleanupManifest:
    if delete and manifest is None:
        raise _CleanupFailure("unsafe_tree", "cleanup deletion manifest is unavailable")
    if not delete and manifest is not None:
        raise _CleanupFailure("unsafe_tree", "cleanup validation received a deletion manifest")
    manifest_entries: dict[CleanupManifestKey, _CleanupManifestEntry] = {}
    remaining = set(manifest.entries) if manifest is not None else set()
    stack: list[_CleanupFrame] = []
    try:
        root_descriptor, root_status = _open_verified_relative_directory(
            parent_descriptor,
            root_name,
            root_identity,
            expected_device=root_identity[0],
            budget=budget,
        )
        try:
            budget.check_deadline()
            root_entries = os.scandir(root_descriptor)
        except BaseException:
            try:
                os.close(root_descriptor)
            except BaseException:
                pass
            raise
        stack.append(
            _CleanupFrame(
                root_descriptor,
                root_entries,
                0,
                None,
                _status_identity(root_status),
                parent_descriptor,
            )
        )
        while stack:
            frame = stack[-1]
            budget.check_deadline()
            try:
                entry = next(frame.entries)
            except StopIteration:
                frame.entries.close()
                if frame.name is None:
                    os.close(frame.descriptor)
                    stack.pop()
                    continue
                if delete:
                    budget.check_deadline()
                    _verify_relative_identity(
                        frame.parent_descriptor,
                        frame.name,
                        frame.identity,
                        f"directory identity mismatch before removal: {frame.name}",
                    )
                if delete:
                    budget.check_deadline()
                    os.rmdir(frame.name, dir_fd=frame.parent_descriptor)
                    budget.check_deadline()
                    if _opened_directory_remains_linked(frame.descriptor):
                        raise _CleanupFailure(
                            "unsafe_tree",
                            f"directory remained linked after removal: {frame.name}",
                        )
                os.close(frame.descriptor)
                stack.pop()
                continue
            child_depth = frame.depth + 1
            budget.observe_entry(depth=child_depth)
            budget.check_deadline()
            key = (frame.identity, entry.name)
            if delete and key not in remaining:
                raise _CleanupFailure(
                    "unsafe_tree",
                    f"cleanup entry absent from validation manifest: {entry.name}",
                )
            try:
                child_status = os.stat(
                    entry.name,
                    dir_fd=frame.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError as error:
                if delete:
                    raise _CleanupFailure(
                        "unsafe_tree",
                        f"validated cleanup entry is missing: {entry.name}",
                    ) from error
                raise
            observed_entry = _CleanupManifestEntry(
                _status_identity(child_status),
                _cleanup_entry_type(child_status.st_mode),
            )
            if delete:
                if manifest is None or manifest.entries[key] != observed_entry:
                    raise _CleanupFailure(
                        "unsafe_tree",
                        f"cleanup entry identity or type mismatch: {entry.name}",
                    )
            else:
                if key in manifest_entries:
                    raise _CleanupFailure(
                        "unsafe_tree",
                        f"duplicate cleanup manifest entry: {entry.name}",
                    )
                manifest_entries[key] = observed_entry
            if stat.S_ISDIR(child_status.st_mode):
                if delete:
                    remaining.remove(key)
                child_identity = _status_identity(child_status)
                child_descriptor, verified_status = _open_verified_relative_directory(
                    frame.descriptor,
                    entry.name,
                    child_identity,
                    expected_device=root_identity[0],
                    budget=budget,
                )
                try:
                    budget.check_deadline()
                    child_entries = os.scandir(child_descriptor)
                except BaseException:
                    os.close(child_descriptor)
                    raise
                stack.append(
                    _CleanupFrame(
                        child_descriptor,
                        child_entries,
                        child_depth,
                        entry.name,
                        _status_identity(verified_status),
                        frame.descriptor,
                    )
                )
                continue
            if delete:
                quarantine = budget.quarantine
                if quarantine is None:
                    raise _CleanupFailure(
                        "unsafe_tree",
                        "cleanup leaf quarantine is unavailable",
                    )
                _quarantine_and_remove_leaf(
                    frame.descriptor,
                    entry.name,
                    manifest.entries[key] if manifest is not None else observed_entry,
                    key=key,
                    remaining=remaining,
                    quarantine=quarantine,
                    budget=budget,
                )
        if delete and remaining:
            raise _CleanupFailure(
                "unsafe_tree",
                "validated cleanup entries are missing during deletion",
            )
        if manifest is not None:
            return manifest
        return _CleanupManifest(MappingProxyType(manifest_entries))
    finally:
        for frame in reversed(stack):
            try:
                frame.entries.close()
            except BaseException:
                pass
            try:
                os.close(frame.descriptor)
            except BaseException:
                pass


def _create_quarantine_directory(
    parent_descriptor: int,
    *,
    expected_device: int,
    budget: _CleanupBudget,
) -> _QuarantineDirectory:
    budget.check_deadline()
    name = f".pyrepo-check-quarantine-{secrets.token_hex(16)}"
    os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
    descriptor: int | None = None
    quarantine: _QuarantineDirectory | None = None
    try:
        budget.check_deadline()
        file_status = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        identity = _status_identity(file_status)
        quarantine = _QuarantineDirectory(name, identity, None)
        if (
            not stat.S_ISDIR(file_status.st_mode)
            or file_status.st_dev != expected_device
            or file_status.st_uid != _effective_uid()
            or stat.S_IMODE(file_status.st_mode) != 0o700
        ):
            quarantine.cleanup_allowed = False
            raise _QuarantineSetupFailure(
                "unsafe_tree",
                "created quarantine directory is not private or trusted",
                quarantine,
            )
        descriptor = os.open(
            name,
            _secure_directory_open_flags(),
            dir_fd=parent_descriptor,
        )
        quarantine.descriptor = descriptor
        os.set_inheritable(descriptor, False)
        budget.check_deadline()
        verified_status = os.fstat(descriptor)
        if (
            _status_identity(verified_status) != identity
            or not stat.S_ISDIR(verified_status.st_mode)
            or verified_status.st_dev != expected_device
            or verified_status.st_uid != _effective_uid()
            or stat.S_IMODE(verified_status.st_mode) != 0o700
        ):
            quarantine.cleanup_allowed = False
            raise _QuarantineSetupFailure(
                "unsafe_tree",
                "opened quarantine directory identity or privacy mismatch",
                quarantine,
            )
        return quarantine
    except _QuarantineSetupFailure:
        raise
    except BaseException as error:
        if quarantine is not None:
            quarantine.cleanup_allowed = False
            raise _QuarantineSetupFailure(
                "unsafe_tree",
                f"could not securely open quarantine directory: {type(error).__name__}: {error}",
                quarantine,
            ) from error
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException:
                pass
        raise


def _quarantine_and_remove_leaf(
    source_descriptor: int,
    source_name: str,
    expected_entry: _CleanupManifestEntry,
    *,
    key: CleanupManifestKey,
    remaining: set[CleanupManifestKey],
    quarantine: _QuarantineDirectory,
    budget: _CleanupBudget,
) -> None:
    if quarantine.descriptor is None:
        raise _CleanupFailure(
            "unsafe_tree",
            "cleanup leaf quarantine descriptor is unavailable",
        )
    quarantine_name = f"leaf-{secrets.token_hex(16)}"
    budget.check_deadline()
    quarantine.may_contain_data = True
    quarantine.ever_contained_data = True
    os.rename(
        source_name,
        quarantine_name,
        src_dir_fd=source_descriptor,
        dst_dir_fd=quarantine.descriptor,
    )
    budget.check_deadline()
    quarantined_status = os.stat(
        quarantine_name,
        dir_fd=quarantine.descriptor,
        follow_symlinks=False,
    )
    quarantined_entry = _CleanupManifestEntry(
        _status_identity(quarantined_status),
        _cleanup_entry_type(quarantined_status.st_mode),
    )
    if quarantined_entry != expected_entry:
        raise _CleanupFailure(
            "unsafe_tree",
            f"quarantined cleanup entry identity or type mismatch: {source_name}",
        )
    remaining.remove(key)
    budget.check_deadline()
    os.unlink(quarantine_name, dir_fd=quarantine.descriptor)
    quarantine.may_contain_data = False
    budget.check_deadline()


def _open_verified_parent(run_directory: _RunDirectory) -> int:
    parent_identity = run_directory.parent_identity
    if parent_identity is None:
        raise _CleanupFailure("unsafe_tree", "run directory parent identity is unavailable")
    descriptor = os.open(run_directory.path.parent, _secure_directory_open_flags())
    try:
        os.set_inheritable(descriptor, False)
        if _status_identity(os.fstat(descriptor)) != parent_identity:
            raise _CleanupFailure("unsafe_tree", "run directory parent identity mismatch")
    except BaseException:
        try:
            os.close(descriptor)
        except BaseException:
            pass
        raise
    return descriptor


def _open_verified_run_directory(
    run_directory: _RunDirectory,
) -> _VerifiedRunDirectory:
    parent_descriptor = _open_verified_parent(run_directory)
    run_descriptor: int | None = None
    try:
        run_descriptor = os.open(
            run_directory.path.name,
            _secure_directory_open_flags(),
            dir_fd=parent_descriptor,
        )
        os.set_inheritable(run_descriptor, False)
        verified = _VerifiedRunDirectory(
            run_directory,
            parent_descriptor,
            run_descriptor,
        )
        verified.verify("before preparation")
    except BaseException:
        if run_descriptor is not None:
            try:
                os.close(run_descriptor)
            except BaseException:
                pass
        try:
            os.close(parent_descriptor)
        except BaseException:
            pass
        raise
    return verified


def _remove_held_relative_directory(
    parent_descriptor: int,
    directory: _QuarantineDirectory,
    *,
    budget: _CleanupBudget,
    identity_mismatch_message: str,
) -> None:
    if directory.descriptor is None:
        raise _CleanupFailure(
            "unsafe_tree",
            "quarantine directory descriptor is unavailable for removal",
        )
    budget.check_deadline()
    _verify_relative_identity(
        parent_descriptor,
        directory.name,
        directory.identity,
        identity_mismatch_message,
    )
    budget.check_deadline()
    os.rmdir(directory.name, dir_fd=parent_descriptor)
    budget.check_deadline()
    if _opened_directory_remains_linked(directory.descriptor):
        raise _CleanupFailure(
            "unsafe_tree",
            f"directory remained linked after removal: {directory.name}",
        )
    directory.removed = True


def _open_verified_relative_directory(
    parent_descriptor: int,
    name: str,
    identity: tuple[int, int],
    *,
    expected_device: int,
    budget: _CleanupBudget,
) -> tuple[int, os.stat_result]:
    budget.check_deadline()
    try:
        descriptor = os.open(
            name,
            _secure_directory_open_flags(),
            dir_fd=parent_descriptor,
        )
    except OSError as error:
        kind: CleanupFailureKind = (
            "unsafe_tree"
            if error.errno in {errno.ELOOP, errno.ENOTDIR}
            else "io_failed"
        )
        raise _CleanupFailure(
            kind,
            f"could not safely open directory {name}: {type(error).__name__}: {error}",
        ) from error
    try:
        os.set_inheritable(descriptor, False)
        budget.check_deadline()
        file_status = os.fstat(descriptor)
        if file_status.st_dev != expected_device:
            raise _CleanupFailure("unsafe_tree", f"cross-device directory rejected: {name}")
        if _status_identity(file_status) != identity:
            raise _CleanupFailure("unsafe_tree", f"directory identity mismatch: {name}")
    except BaseException:
        try:
            os.close(descriptor)
        except BaseException:
            pass
        raise
    return descriptor, file_status


def _verify_relative_identity(
    parent_descriptor: int,
    name: str,
    identity: tuple[int, int],
    message: str,
) -> None:
    file_status = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if _status_identity(file_status) != identity or not stat.S_ISDIR(file_status.st_mode):
        raise _CleanupFailure("unsafe_tree", message)


def _remove_verified_relative_directory(
    parent_descriptor: int,
    name: str,
    identity: tuple[int, int],
    *,
    expected_device: int,
    budget: _CleanupBudget,
    identity_mismatch_message: str,
) -> None:
    descriptor, _status = _open_verified_relative_directory(
        parent_descriptor,
        name,
        identity,
        expected_device=expected_device,
        budget=budget,
    )
    try:
        budget.check_deadline()
        _verify_relative_identity(
            parent_descriptor,
            name,
            identity,
            identity_mismatch_message,
        )
        budget.check_deadline()
        os.rmdir(name, dir_fd=parent_descriptor)
        budget.check_deadline()
        if _opened_directory_remains_linked(descriptor):
            raise _CleanupFailure(
                "unsafe_tree",
                f"directory remained linked after removal: {name}",
            )
    finally:
        os.close(descriptor)


def _opened_directory_remains_linked(descriptor: int) -> bool:
    file_status = os.fstat(descriptor)
    if file_status.st_nlink == 0:
        return False
    if _fcntl is None:
        return True
    fcntl_call = getattr(_fcntl, "fcntl", None)
    get_path = getattr(_fcntl, "F_GETPATH", None)
    if not callable(fcntl_call) or type(get_path) is not int:
        return True
    try:
        raw_path = fcntl_call(descriptor, get_path, b"\0" * 1024)
        if not isinstance(raw_path, bytes):
            return True
        path_bytes, separator, _remainder = raw_path.partition(b"\0")
        if not separator or not path_bytes:
            return True
        live_path = os.fsdecode(path_bytes)
        if not os.path.isabs(live_path):
            return True
        os.stat(live_path, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _verified_retained_path(
    run_directory: _RunDirectory,
    parent_descriptor: int | None,
) -> Path | None:
    if parent_descriptor is None:
        return None
    try:
        parent_identity = run_directory.parent_identity
        if _status_identity(os.fstat(parent_descriptor)) != parent_identity:
            return None
        lexical_parent_status = os.stat(
            run_directory.path.parent,
            follow_symlinks=False,
        )
        if (
            _status_identity(lexical_parent_status) != parent_identity
            or not stat.S_ISDIR(lexical_parent_status.st_mode)
        ):
            return None
        file_status = os.stat(
            run_directory.path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        lexical_child_status = os.stat(run_directory.path, follow_symlinks=False)
    except OSError:
        return None
    if (
        _status_identity(file_status) != run_directory.identity
        or not stat.S_ISDIR(file_status.st_mode)
        or _status_identity(lexical_child_status) != run_directory.identity
        or not stat.S_ISDIR(lexical_child_status.st_mode)
    ):
        return None
    return run_directory.path


def _verified_quarantine_path(
    run_directory: _RunDirectory,
    quarantine: _QuarantineDirectory | None,
    parent_descriptor: int | None,
) -> Path | None:
    if quarantine is None or quarantine.removed or parent_descriptor is None:
        return None
    try:
        parent_identity = run_directory.parent_identity
        if _status_identity(os.fstat(parent_descriptor)) != parent_identity:
            return None
        lexical_parent_status = os.stat(
            run_directory.path.parent,
            follow_symlinks=False,
        )
        if (
            _status_identity(lexical_parent_status) != parent_identity
            or not stat.S_ISDIR(lexical_parent_status.st_mode)
        ):
            return None
        relative_status = os.stat(
            quarantine.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        lexical_path = run_directory.path.parent / quarantine.name
        lexical_status = os.stat(lexical_path, follow_symlinks=False)
    except OSError:
        return None
    if (
        _status_identity(relative_status) != quarantine.identity
        or not stat.S_ISDIR(relative_status.st_mode)
        or _status_identity(lexical_status) != quarantine.identity
        or not stat.S_ISDIR(lexical_status.st_mode)
    ):
        return None
    return lexical_path


def _secure_directory_open_flags() -> int:
    directory_only = cast(int, getattr(os, "O_DIRECTORY"))
    no_follow = cast(int, getattr(os, "O_NOFOLLOW"))
    non_blocking = cast(int, getattr(os, "O_NONBLOCK"))
    return os.O_RDONLY | directory_only | no_follow | non_blocking


def _effective_uid() -> int:
    get_effective_uid = getattr(os, "geteuid", None)
    if not callable(get_effective_uid):
        raise _CleanupFailure(
            "unsafe_tree",
            "effective user identity is unavailable",
        )
    return cast(Callable[[], int], get_effective_uid)()


def _cleanup_diagnostic(observation: _CleanupObservation) -> str:
    diagnostic = observation.message
    if observation.retained_run_path is not None:
        diagnostic = (
            f"{diagnostic}; retained run path: {observation.retained_run_path}"
        )
    if observation.retained_quarantine_path is not None:
        diagnostic = (
            f"{diagnostic}; retained quarantine path: "
            f"{observation.retained_quarantine_path}"
        )
    return diagnostic


def _remove_empty_created_run_directory(
    run_directory: Path,
    identity: tuple[int, int] | None,
    parent_identity: tuple[int, int],
) -> OSError | None:
    if identity is None:
        return OSError("created run directory identity is unavailable")
    parent_descriptor: int | None = None
    cleanup_error: OSError | None = None
    try:
        parent_descriptor = os.open(
            run_directory.parent,
            _secure_directory_open_flags(),
        )
        os.set_inheritable(parent_descriptor, False)
        if _status_identity(os.fstat(parent_descriptor)) != parent_identity:
            raise _CleanupFailure(
                "unsafe_tree",
                "created run directory parent identity mismatch",
            )
        started_ns = time.monotonic_ns()
        _remove_verified_relative_directory(
            parent_descriptor,
            run_directory.name,
            identity,
            expected_device=identity[0],
            budget=_CleanupBudget(started_ns, time.monotonic_ns),
            identity_mismatch_message="created run directory identity mismatch",
        )
    except OSError as error:
        cleanup_error = error
    finally:
        if parent_descriptor is not None:
            try:
                os.close(parent_descriptor)
            except OSError as error:
                if cleanup_error is None:
                    cleanup_error = error
    return cleanup_error


def _directory_identity(path: Path) -> tuple[int, int]:
    file_status = os.lstat(path)
    if not stat.S_ISDIR(file_status.st_mode):
        raise OSError("run directory is not a directory")
    return _status_identity(file_status)


def _status_identity(file_status: os.stat_result) -> tuple[int, int]:
    return file_status.st_dev, file_status.st_ino


def _cleanup_entry_type(mode: int) -> CleanupEntryType:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISREG(mode):
        return "regular"
    return "other"


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


class _BoundedReadError(OSError):
    """Raised when a regular evidence file exceeds its byte budget."""


def _read_regular_file(
    path: Path,
    *,
    max_bytes: int,
    dir_fd: int | None = None,
) -> bytes:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    non_blocking = getattr(os, "O_NONBLOCK", None)
    if type(no_follow) is not int or type(non_blocking) is not int:
        raise _UnsafePathError("safe no-follow file opening is unavailable")
    try:
        if dir_fd is None:
            descriptor = os.open(path, os.O_RDONLY | no_follow | non_blocking)
        else:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | no_follow | non_blocking,
                dir_fd=dir_fd,
            )
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise _UnsafePathError(f"path is not a regular file: {path.name}") from error
        raise
    try:
        file_status = os.fstat(descriptor)
        if not stat.S_ISREG(file_status.st_mode):
            raise _UnsafePathError(f"path is not a regular file: {path.name}")
        if file_status.st_size > max_bytes:
            raise _BoundedReadError(f"{path.name} exceeds the {max_bytes}-byte limit")
        content = bytearray()
        while len(content) <= max_bytes:
            remaining = max_bytes + 1 - len(content)
            chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > max_bytes:
            raise _BoundedReadError(f"{path.name} exceeds the {max_bytes}-byte limit")
        return bytes(content)
    finally:
        os.close(descriptor)


def _load_bounded_json(content: bytes) -> object:
    depth = 0
    in_string = False
    escaped = False
    for byte in content:
        if in_string:
            if escaped:
                escaped = False
            elif byte == ord("\\"):
                escaped = True
            elif byte == ord('"'):
                in_string = False
            continue
        if byte == ord('"'):
            in_string = True
        elif byte in {ord("{"), ord("[")}:
            depth += 1
            if depth > _MAX_JSON_NESTING:
                raise ValueError(
                    f"JSON nesting exceeds the {_MAX_JSON_NESTING}-level limit"
                )
        elif byte in {ord("}"), ord("]")}:
            depth -= 1
    try:
        return json.loads(content, parse_constant=_reject_json_constant)
    except RecursionError as error:
        raise ValueError("JSON parsing exceeded the recursion limit") from error


def _reject_json_constant(constant: str) -> Never:
    raise ValueError(f"JSON constant {constant} is not permitted")


def _platform_capability_error() -> str | None:
    if (
        type(getattr(os, "O_NOFOLLOW", None)) is not int
        or type(getattr(os, "O_DIRECTORY", None)) is not int
        or type(getattr(os, "O_NONBLOCK", None)) is not int
        or not _SCANDIR_SUPPORTS_FD
        or not _OPEN_SUPPORTS_DIR_FD
        or not _STAT_SUPPORTS_DIR_FD
        or not _STAT_SUPPORTS_FOLLOW_SYMLINKS
        or not _UNLINK_SUPPORTS_DIR_FD
        or not _RMDIR_SUPPORTS_DIR_FD
        or not _MKDIR_SUPPORTS_DIR_FD
        or not _RENAME_SUPPORTS_DIR_FD
        or not callable(getattr(os, "geteuid", None))
        or not _POST_RMDIR_UNLINK_PROOF
    ):
        return (
            "Structured pytest evidence requires descriptor-safe no-follow file opening "
            "and bounded descriptor-relative recursive removal."
        )
    return None


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
