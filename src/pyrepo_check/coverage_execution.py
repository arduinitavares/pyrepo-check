"""Coverage.py preflight and invocation helpers for pytest execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Literal, cast

from pyrepo_check.execution import CAPTURE_LIMIT_BYTES, CapturedBytes, ExecutedProcess


_MINIMUM_PYTHON_VERSION = (3, 13, 15)
_STABLE_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*))?$")

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
CoverageArtifactState = Literal["not_attempted"]


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
    diagnostic: str | None


@dataclass(frozen=True)
class CoverageExecutionObservation:
    preflight: CoveragePreflightObservation
    artifact: CoverageArtifactObservation


def coverage_environment(
    base_environment: Mapping[str, str],
    *,
    run_directory: Path,
    config_path: Path,
) -> dict[str, str]:
    """Give Coverage.py run-owned output paths without consumer startup hooks."""
    environment = dict(base_environment)
    for name in tuple(environment):
        if name in {
            "COVERAGE_PROCESS_CONFIG",
            "COVERAGE_PROCESS_START",
            "COVERAGE_FILE",
            "COVERAGE_RCFILE",
        } or name.startswith("COV_CORE_"):
            del environment[name]
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
        artifact=CoverageArtifactObservation("not_attempted", None),
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
