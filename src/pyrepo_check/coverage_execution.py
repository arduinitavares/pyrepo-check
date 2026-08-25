"""Coverage.py preflight and invocation helpers for pytest execution."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
from typing import Literal, cast

from pyrepo_check.artifact_safety import (
    FileDigest,
    FileIdentity,
    _BoundedReadError,
    _DigestMismatchError,
    _UnsafePathError,
    copy_regular_file,
    digest_regular_file,
    load_bounded_json,
    read_regular_file,
)
from pyrepo_check.execution import CAPTURE_LIMIT_BYTES, CapturedBytes, ExecutedProcess


_MINIMUM_PYTHON_VERSION = (3, 13, 15)
_STABLE_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*))?$")
MAX_COVERAGE_DATA_BYTES = 512 * 1024 * 1024
MAX_COVERAGE_JSON_BYTES = 128 * 1024 * 1024
_BASE_DATA_NAME = ".coverage"
_BASE_SHARD_PREFIX = ".coverage."
_REPORT_INPUT_NAME = "report-input"
_SNAPSHOT_DATA_NAME = "coverage-data"
_SNAPSHOT_SHARD_PREFIX = "coverage-data."

COVERAGE_PREFLIGHT_PROBE = """import json
import sys
record = {"schema_version": 1, "python_version": list(sys.version_info[:3]), "coverage_available": False, "coverage_version": None}
if tuple(sys.version_info[:3]) >= (3, 13, 15):
    try:
        import coverage
    except ImportError:
        pass
    else:
        record["coverage_available"] = True
        record["coverage_version"] = getattr(coverage, "__version__", None)
print(json.dumps(record, separators=(",", ":")))
"""


CoveragePreflightClassification = Literal[
    "supported",
    "unsupported_python",
    "module_unavailable",
    "unsupported_version",
    "preflight_invalid",
    "spawn_failed",
    "terminated_by_signal",
]
CoverageArtifactState = Literal[
    "not_attempted",
    "snapshot",
    "unsupported_parallelism",
    "data_missing",
    "unexpected_parallel_data",
    "generation_failed",
    "artifact_missing",
    "artifact_invalid",
    "spawn_failed",
    "terminated_by_signal",
]


@dataclass(frozen=True)
class CoveragePreflightRecord:
    python_version: tuple[int, int, int]
    coverage_available: bool
    coverage_version: str | None


@dataclass(frozen=True)
class CoveragePreflightObservation:
    classification: CoveragePreflightClassification
    record: CoveragePreflightRecord | None
    diagnostic: str | None


@dataclass(frozen=True)
class CoverageArtifactObservation:
    state: CoverageArtifactState
    content: bytes | None
    diagnostic: str | None


@dataclass(frozen=True)
class CoverageExecutionObservation:
    preflight: CoveragePreflightObservation
    artifact: CoverageArtifactObservation
    json_exit_code: int | None = None


CoverageDataErrorCode = Literal["data_missing", "unexpected_parallel_data"]


class CoverageDataError(OSError):
    def __init__(self, code: CoverageDataErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class CoverageDataSnapshot:
    """Descriptor-held identity and digest for one run-owned coverage copy."""

    run_directory: Path
    run_descriptor: int
    report_descriptor: int
    run_identity: tuple[int, int]
    report_identity: tuple[int, int]
    original_identity: FileIdentity
    snapshot_identity: FileIdentity
    digest: FileDigest

    @property
    def data_path(self) -> Path:
        return self.run_directory / _REPORT_INPUT_NAME / _SNAPSHOT_DATA_NAME

    def close(self) -> None:
        close_error = _close_snapshot_descriptors(
            self.report_descriptor,
            self.run_descriptor,
        )
        if close_error is not None:
            raise close_error


def prepare_coverage_data_snapshot(
    run_directory: Path,
    *,
    run_descriptor: int,
) -> CoverageDataSnapshot:
    """Validate exact base data and copy it into an isolated held directory."""
    owned_run_descriptor: int | None = os.dup(run_descriptor)
    report_descriptor: int | None = None
    try:
        os.set_inheritable(owned_run_descriptor, False)
        run_identity = _directory_identity(owned_run_descriptor, "run directory")
        _reject_shards(
            owned_run_descriptor,
            prefix=_BASE_SHARD_PREFIX,
            namespace="run coverage-data",
        )
        try:
            original_identity = _regular_leaf_identity(
                owned_run_descriptor,
                _BASE_DATA_NAME,
            )
            original_digest = digest_regular_file(
                Path(_BASE_DATA_NAME),
                max_bytes=MAX_COVERAGE_DATA_BYTES,
                dir_fd=owned_run_descriptor,
            )
        except (_BoundedReadError, _UnsafePathError, OSError) as error:
            code: CoverageDataErrorCode = (
                "unexpected_parallel_data"
                if "changed during read" in str(error)
                else "data_missing"
            )
            raise CoverageDataError(code, _data_error_message(error)) from error

        try:
            os.mkdir(_REPORT_INPUT_NAME, mode=0o700, dir_fd=owned_run_descriptor)
            report_descriptor = os.open(
                _REPORT_INPUT_NAME,
                _secure_directory_open_flags(),
                dir_fd=owned_run_descriptor,
            )
            os.set_inheritable(report_descriptor, False)
            report_identity = _directory_identity(report_descriptor, "report-input")
            _verify_report_directory_metadata(report_descriptor)
            if not _directory_is_empty(report_descriptor):
                raise _UnsafePathError("report-input directory is not empty")
            copied = copy_regular_file(
                Path(_BASE_DATA_NAME),
                Path(_SNAPSHOT_DATA_NAME),
                max_bytes=MAX_COVERAGE_DATA_BYTES,
                source_dir_fd=owned_run_descriptor,
                destination_dir_fd=report_descriptor,
            )
            if copied.digest != original_digest:
                raise _DigestMismatchError(
                    "coverage data changed between validation and snapshot copy"
                )
            snapshot_identity = copied.destination_identity
            _verify_leaf_identity(
                report_descriptor,
                _SNAPSHOT_DATA_NAME,
                snapshot_identity,
            )
        except BaseException as error:
            if isinstance(error, CoverageDataError):
                raise
            if not isinstance(error, OSError):
                raise
            raise CoverageDataError(
                "unexpected_parallel_data",
                f"coverage snapshot failed: {type(error).__name__}: {error}",
            ) from error

        snapshot = CoverageDataSnapshot(
            run_directory=run_directory,
            run_descriptor=owned_run_descriptor,
            report_descriptor=report_descriptor,
            run_identity=run_identity,
            report_identity=report_identity,
            original_identity=original_identity,
            snapshot_identity=snapshot_identity,
            digest=original_digest,
        )
        verify_coverage_data_snapshot(snapshot)
        owned_run_descriptor = None
        report_descriptor = None
        return snapshot
    except BaseException:
        _close_snapshot_descriptors(report_descriptor, owned_run_descriptor)
        raise


def verify_coverage_data_snapshot(snapshot: CoverageDataSnapshot) -> None:
    """Revalidate both shard namespaces, directory identities, and digests."""
    try:
        if _status_identity(os.fstat(snapshot.run_descriptor)) != snapshot.run_identity:
            raise _UnsafePathError("run directory identity changed")
        report_status = os.stat(
            _REPORT_INPUT_NAME,
            dir_fd=snapshot.run_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(report_status.st_mode)
            or _status_identity(report_status) != snapshot.report_identity
            or _directory_identity(snapshot.report_descriptor, "report-input")
            != snapshot.report_identity
        ):
            raise _UnsafePathError("report-input directory identity changed")
        _verify_report_directory_metadata(snapshot.report_descriptor)
        _reject_shards(
            snapshot.run_descriptor,
            prefix=_BASE_SHARD_PREFIX,
            namespace="run coverage-data",
        )
        _reject_shards(
            snapshot.report_descriptor,
            prefix=_SNAPSHOT_SHARD_PREFIX,
            namespace="snapshot coverage-data",
        )
        _verify_leaf_identity(
            snapshot.run_descriptor,
            _BASE_DATA_NAME,
            snapshot.original_identity,
        )
        _verify_leaf_identity(
            snapshot.report_descriptor,
            _SNAPSHOT_DATA_NAME,
            snapshot.snapshot_identity,
        )
        original_digest = digest_regular_file(
            Path(_BASE_DATA_NAME),
            max_bytes=MAX_COVERAGE_DATA_BYTES,
            dir_fd=snapshot.run_descriptor,
        )
        copied_digest = digest_regular_file(
            Path(_SNAPSHOT_DATA_NAME),
            max_bytes=MAX_COVERAGE_DATA_BYTES,
            dir_fd=snapshot.report_descriptor,
        )
        if original_digest != snapshot.digest or copied_digest != snapshot.digest:
            raise _DigestMismatchError("coverage original or snapshot digest changed")
        _verify_leaf_identity(
            snapshot.run_descriptor,
            _BASE_DATA_NAME,
            snapshot.original_identity,
        )
        _verify_leaf_identity(
            snapshot.report_descriptor,
            _SNAPSHOT_DATA_NAME,
            snapshot.snapshot_identity,
        )
    except CoverageDataError:
        raise
    except OSError as error:
        raise CoverageDataError(
            "unexpected_parallel_data",
            f"coverage data validation failed: {type(error).__name__}: {error}",
        ) from error


def require_coverage_json_destination_absent(snapshot: CoverageDataSnapshot) -> None:
    """Refuse to let Coverage.py replace an existing run-owned JSON leaf."""
    try:
        os.stat(
            "coverage.json",
            dir_fd=snapshot.run_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    except OSError as error:
        raise CoverageDataError(
            "unexpected_parallel_data",
            f"coverage JSON destination inspection failed: {type(error).__name__}: {error}",
        ) from error
    raise CoverageDataError(
        "unexpected_parallel_data",
        "coverage JSON destination already exists",
    )


def _reject_shards(descriptor: int, *, prefix: str, namespace: str) -> None:
    try:
        shard = _find_prefixed_entry(descriptor, prefix)
    except OSError as error:
        raise CoverageDataError(
            "unexpected_parallel_data",
            f"{namespace} scan failed: {type(error).__name__}: {error}",
        ) from error
    if shard is not None:
        raise CoverageDataError(
            "unexpected_parallel_data",
            f"unexpected parallel coverage data: {shard}",
        )


def _directory_is_empty(descriptor: int) -> bool:
    with os.scandir(descriptor) as entries:
        return next(entries, None) is None


def _find_prefixed_entry(descriptor: int, prefix: str) -> str | None:
    with os.scandir(descriptor) as entries:
        return next((entry.name for entry in entries if entry.name.startswith(prefix)), None)


def _directory_identity(descriptor: int, name: str) -> tuple[int, int]:
    file_status = os.fstat(descriptor)
    if not stat.S_ISDIR(file_status.st_mode):
        raise _UnsafePathError(f"{name} is not a directory")
    return _status_identity(file_status)


def _regular_leaf_identity(descriptor: int, name: str) -> FileIdentity:
    file_status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    if not stat.S_ISREG(file_status.st_mode):
        raise _UnsafePathError(f"{name} is not a regular file")
    return FileIdentity(file_status.st_dev, file_status.st_ino)


def _verify_leaf_identity(
    descriptor: int,
    name: str,
    expected_identity: FileIdentity,
) -> None:
    if _regular_leaf_identity(descriptor, name) != expected_identity:
        raise _UnsafePathError(f"{name} identity changed")


def _close_snapshot_descriptors(
    report_descriptor: int | None,
    run_descriptor: int | None,
) -> OSError | None:
    close_error: OSError | None = None
    for descriptor in (report_descriptor, run_descriptor):
        if descriptor is None:
            continue
        try:
            os.close(descriptor)
        except OSError as error:
            if close_error is None:
                close_error = error
    return close_error


def _verify_report_directory_metadata(descriptor: int) -> None:
    file_status = os.fstat(descriptor)
    get_effective_user_id = getattr(os, "geteuid", None)
    if not callable(get_effective_user_id):
        raise _UnsafePathError("effective-user ownership validation is unavailable")
    if file_status.st_uid != cast(Callable[[], int], get_effective_user_id)():
        raise _UnsafePathError("report-input owner is not the effective user")
    if stat.S_IMODE(file_status.st_mode) & ~0o700:
        raise _UnsafePathError("report-input permissions are broader than 0700")


def _secure_directory_open_flags() -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    non_blocking = getattr(os, "O_NONBLOCK", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if (
        type(no_follow) is not int
        or type(non_blocking) is not int
        or type(directory) is not int
    ):
        raise _UnsafePathError("safe no-follow directory opening is unavailable")
    return os.O_RDONLY | no_follow | non_blocking | directory


def _status_identity(file_status: os.stat_result) -> tuple[int, int]:
    return file_status.st_dev, file_status.st_ino


def _data_error_message(error: OSError) -> str:
    return f"coverage data is missing or unusable: {type(error).__name__}: {error}"


def coverage_environment(
    base_environment: Mapping[str, str],
    *,
    run_directory: Path,
    config_path: Path,
) -> dict[str, str]:
    """Give Coverage.py run-owned output paths without consumer startup hooks."""
    environment = _scrub_coverage_environment(base_environment)
    environment["COVERAGE_FILE"] = str(run_directory / ".coverage")
    environment["COVERAGE_RCFILE"] = str(config_path)
    return environment


def coverage_preflight_command(consumer_python: tuple[str, ...]) -> tuple[str, ...]:
    return (*consumer_python, "-c", COVERAGE_PREFLIGHT_PROBE)


def coverage_primary_command(
    *,
    consumer_python: tuple[str, ...],
    config_path: Path,
    run_directory: Path,
    plugin_module: str,
    pytest_args: tuple[str, ...],
) -> tuple[str, ...]:
    return (
        *consumer_python,
        "-m",
        "coverage",
        "run",
        f"--rcfile={config_path}",
        f"--data-file={run_directory / '.coverage'}",
        "-m",
        "pytest",
        "-p",
        plugin_module,
        *pytest_args,
    )


def coverage_json_command(
    *,
    consumer_python: tuple[str, ...],
    config_path: Path,
    data_path: Path,
    output_path: Path,
    force_fail_under_zero: bool,
) -> tuple[str, ...]:
    command = (
        *consumer_python,
        "-m",
        "coverage",
        "json",
        f"--rcfile={config_path}",
        f"--data-file={data_path}",
        "-o",
        str(output_path),
        "--keep-combined",
    )
    return (*command, "--fail-under=0") if force_fail_under_zero else command


def coverage_json_environment(
    base_environment: Mapping[str, str],
    *,
    data_path: Path,
    config_path: Path,
) -> dict[str, str]:
    environment = _scrub_coverage_environment(base_environment)
    environment["COVERAGE_FILE"] = str(data_path)
    environment["COVERAGE_RCFILE"] = str(config_path)
    return environment


def _scrub_coverage_environment(base_environment: Mapping[str, str]) -> dict[str, str]:
    environment = dict(base_environment)
    for name in tuple(environment):
        if name in {
            "COVERAGE_PROCESS_CONFIG",
            "COVERAGE_PROCESS_START",
            "COVERAGE_FILE",
            "COVERAGE_RCFILE",
        } or name.startswith("COV_CORE_"):
            del environment[name]
    return environment


def snapshot_coverage_json(snapshot: CoverageDataSnapshot) -> CoverageArtifactObservation:
    """Retain syntactically valid bounded JSON bytes, without semantic projection."""
    try:
        content = read_regular_file(
            Path("coverage.json"),
            max_bytes=MAX_COVERAGE_JSON_BYTES,
            dir_fd=snapshot.run_descriptor,
        )
    except FileNotFoundError:
        return CoverageArtifactObservation("artifact_missing", None, "coverage JSON is missing")
    except (_UnsafePathError, _BoundedReadError, OSError) as error:
        return CoverageArtifactObservation(
            "artifact_invalid",
            None,
            f"coverage JSON is unsafe or unreadable: {type(error).__name__}: {error}",
        )
    try:
        load_bounded_json(content)
    except (UnicodeDecodeError, ValueError) as error:
        return CoverageArtifactObservation(
            "artifact_invalid",
            None,
            f"coverage JSON is malformed: {type(error).__name__}: {error}",
        )
    return CoverageArtifactObservation("snapshot", content, None)


def classify_coverage_preflight(process: ExecutedProcess) -> CoveragePreflightObservation:
    if process.spawn_error is not None:
        return CoveragePreflightObservation("spawn_failed", None, process.spawn_error)
    if process.returncode is not None and process.returncode < 0:
        return CoveragePreflightObservation(
            "terminated_by_signal",
            None,
            f"preflight terminated by signal {-process.returncode}",
        )
    if process.returncode != 0:
        return CoveragePreflightObservation(
            "preflight_invalid", None, f"preflight exited with code {process.returncode}"
        )
    if any(
        stream is not None and stream.omitted_bytes > 0
        for stream in (process.stdout, process.stderr)
    ):
        return CoveragePreflightObservation(
            "preflight_invalid", None, f"preflight output exceeds {CAPTURE_LIMIT_BYTES} bytes"
        )
    try:
        record = _parse_coverage_preflight_record(process.stdout)
    except ValueError as error:
        return CoveragePreflightObservation("preflight_invalid", None, str(error))
    if record.python_version < _MINIMUM_PYTHON_VERSION:
        return CoveragePreflightObservation("unsupported_python", record, None)
    if not record.coverage_available:
        return CoveragePreflightObservation("module_unavailable", record, None)
    if record.coverage_version is None or not _is_supported_version(record.coverage_version):
        return CoveragePreflightObservation("unsupported_version", record, None)
    return CoveragePreflightObservation("supported", record, None)


def invalid_coverage_observation(diagnostic: str) -> CoverageExecutionObservation:
    return CoverageExecutionObservation(
        preflight=CoveragePreflightObservation("preflight_invalid", None, diagnostic),
        artifact=CoverageArtifactObservation("not_attempted", None, None),
    )


def _parse_coverage_preflight_record(output: CapturedBytes | None) -> CoveragePreflightRecord:
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
        "coverage_available",
        "coverage_version",
    }:
        raise ValueError("preflight JSON does not match schema version 1")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("preflight JSON does not match schema version 1")
    coverage_available = document["coverage_available"]
    if not isinstance(coverage_available, bool):
        raise ValueError("preflight JSON does not match schema version 1")
    raw_version = document["coverage_version"]
    if coverage_available:
        if not isinstance(raw_version, str):
            raise ValueError("preflight JSON does not match schema version 1")
        coverage_version = raw_version if _stable_version(raw_version) else None
    elif raw_version is None:
        coverage_version = None
    else:
        raise ValueError("preflight JSON does not match schema version 1")
    return CoveragePreflightRecord(
        _parse_python_version(document["python_version"]), coverage_available, coverage_version
    )


def _parse_python_version(value: object) -> tuple[int, int, int]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("preflight JSON does not match schema version 1")
    version: list[int] = []
    for piece in value:
        if not isinstance(piece, int) or isinstance(piece, bool) or piece < 0:
            raise ValueError("preflight JSON does not match schema version 1")
        version.append(piece)
    return cast(tuple[int, int, int], tuple(version))


def _stable_version(value: str) -> bool:
    return _STABLE_VERSION.fullmatch(value) is not None


def _is_supported_version(value: str) -> bool:
    match = _STABLE_VERSION.fullmatch(value)
    if match is None:
        return False
    try:
        major, minor, patch = (int(piece or "0") for piece in match.groups())
    except ValueError:
        return False
    return major == 7 and (minor, patch) >= (15, 0)
