from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess  # nosec B404
from typing import cast
import uuid

from pyrepo_check.config import ProjectConfig
from pyrepo_check.planning import (
    CheckName,
    DefaultRepositoryPython,
    ExplicitRepositoryPython,
    RunPlan,
    build_checks,
)


_SUPPORTED_PYTEST_PREFLIGHT = (
    b'{"schema_version":1,"python_version":[3,13,15],'
    b'"pytest_available":true,"pytest_version":[8,4,2]}'
)
_SUPPORTED_COVERAGE_PREFLIGHT = (
    b'{"schema_version":1,"python_version":[3,13,15],'
    b'"coverage_available":true,"coverage_version":"7.15.2"}'
)


@dataclass(frozen=True)
class RecordedCall:
    command: tuple[str, ...]
    cwd: Path
    check: bool
    capture_output: bool
    env: Mapping[str, str] | None = None


class RecordingRunner:
    def __init__(
        self,
        *,
        returncodes: tuple[int, ...] = (),
        stdout: tuple[bytes | str | None, ...] = (),
        stderr: tuple[bytes | str | None, ...] = (),
        raise_on_call: int | None = None,
        exception: Exception | None = None,
        on_call: Callable[[RecordedCall], None] | None = None,
        publish_pytest_artifact: bool = False,
        publish_coverage_artifact: bool = False,
    ) -> None:
        self.returncodes = returncodes
        self.stdout = stdout
        self.stderr = stderr
        self.raise_on_call = raise_on_call
        self.exception = exception
        self.on_call = on_call
        self.publish_pytest_artifact = publish_pytest_artifact
        self.publish_coverage_artifact = publish_coverage_artifact
        self.calls: list[RecordedCall] = []

    def __call__(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[tuple[str, ...]]:
        recorded = RecordedCall(
            command=command,
            cwd=cwd,
            check=check,
            capture_output=capture_output,
            env=env,
        )
        self.calls.append(recorded)
        if self.on_call is not None:
            self.on_call(recorded)
        call_number = len(self.calls)
        if self.raise_on_call == call_number:
            if self.exception is None:
                raise FileNotFoundError(command[0])
            raise self.exception

        returncode_index = call_number - 1
        returncode = (
            self.returncodes[returncode_index]
            if returncode_index < len(self.returncodes)
            else 0
        )
        if self.publish_pytest_artifact:
            _publish_pytest_artifact(command, env, returncode)
        if self.publish_coverage_artifact:
            _publish_coverage_artifact(command)
        return cast(
            subprocess.CompletedProcess[tuple[str, ...]],
            subprocess.CompletedProcess(
                command,
                returncode=returncode,
                stdout=(
                    self.stdout[returncode_index]
                    if returncode_index < len(self.stdout)
                    else _default_stdout(command, self.publish_coverage_artifact)
                ),
                stderr=(
                    self.stderr[returncode_index]
                    if returncode_index < len(self.stderr)
                    else None
                ),
            ),
        )


def monotonic_clock() -> Callable[[], int]:
    values = iter(range(0, 10_000_000_000, 1_000_000))
    return lambda: next(values)


def environment_probe_bytes(
    *,
    version: tuple[int, int, int],
    executable: Path,
    environment_root: Path,
) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "implementation": "cpython",
            "version": list(version),
            "executable": str(executable),
            "environment_root": str(environment_root),
        },
        separators=(",", ":"),
    ).encode()


def write_minimal_uv_project(root: Path) -> None:
    (root / "pyproject.toml").write_text(
        "[project]\nname='fixture'\nversion='0'\nrequires-python='>=3.10'\n",
        encoding="utf-8",
    )
    (root / "uv.lock").write_text("version = 1\nrevision = 3\n", encoding="utf-8")
    (root / ".venv/bin").mkdir(parents=True)
    (root / ".venv/bin/python").write_bytes(b"")


def focused_plan(
    root: Path,
    check: CheckName,
    repository_python: str | None = None,
) -> RunPlan:
    resolved_root = root.resolve()
    checks = build_checks(
        ProjectConfig(root=resolved_root, ruff_targets=(), bandit_targets=())
    )
    selection = (
        DefaultRepositoryPython()
        if repository_python is None
        else ExplicitRepositoryPython(repository_python)
    )
    return RunPlan(
        root=resolved_root,
        repository_python=selection,
        mode="focused",
        targets=(),
        checks=(checks[check],),
        output_format="json",
    )


def _default_stdout(
    command: tuple[str, ...],
    publish_coverage_artifact: bool,
) -> bytes | None:
    if "-c" not in command:
        return None
    if publish_coverage_artifact and any(
        "coverage_available" in argument for argument in command
    ):
        return _SUPPORTED_COVERAGE_PREFLIGHT
    return _SUPPORTED_PYTEST_PREFLIGHT


def _publish_pytest_artifact(
    command: tuple[str, ...],
    environment: Mapping[str, str] | None,
    returncode: int,
) -> None:
    if environment is None:
        return
    module_index = _module_index(command, "pytest")
    if module_index is None:
        return
    plugin_index = command.index("-p", module_index + 2)
    artifact_path = Path(environment["PYREPO_CHECK_PYTEST_JSON"])
    writer_directory = Path(environment["PYREPO_CHECK_PYTEST_WRITER_DIR"])
    writer_id = f"recording-runner-{os.getpid()}-{uuid.uuid4().hex}"
    marker_path = writer_directory / f"pytest-writer-{writer_id}.json"

    def open_owner_only(path: str, flags: int) -> int:
        return os.open(path, flags, 0o600)

    with open(  # noqa: PTH123
        marker_path,
        "x",
        encoding="utf-8",
        opener=open_owner_only,
    ) as marker_file:
        json.dump(
            {"schema_version": 1, "writer_id": writer_id, "pid": os.getpid()},
            marker_file,
            separators=(",", ":"),
        )
        marker_file.flush()
        os.fsync(marker_file.fileno())
    artifact = {
        "schema_version": 1,
        "state": "finalized",
        "writer_id": writer_id,
        "pytest_version": "8.4.2",
        "session": {
            "starts": 1,
            "finishes": 1,
            "exit_code": returncode,
            "collection_completed": True,
            "stopped_early": False,
        },
        "effective_args": list(command[plugin_index + 2 :]),
        "semantic_options": {
            "collection_paths": [],
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
            "initial_nodeids": [],
            "final_nodeids": [],
            "deselected_nodeids": [],
            "uncovered_removed_nodeids": [],
            "errors": [],
            "skips": [],
        },
        "reports": [],
        "flags": {
            "unsupported_parallelism": False,
            "unsupported_retries": False,
            "worker_metadata": False,
        },
    }
    temporary_path = artifact_path.with_name(
        f".{artifact_path.name}.{writer_id}.{uuid.uuid4().hex}.tmp"
    )
    with open(  # noqa: PTH123
        temporary_path,
        "x",
        encoding="utf-8",
        opener=open_owner_only,
    ) as temporary_file:
        json.dump(artifact, temporary_file, separators=(",", ":"))
        temporary_file.flush()
        os.fsync(temporary_file.fileno())
    os.replace(temporary_path, artifact_path)


def _publish_coverage_artifact(command: tuple[str, ...]) -> None:
    module_index = _module_index(command, "coverage")
    if module_index is None:
        return
    coverage_subcommand = command[module_index + 2]
    if coverage_subcommand == "run":
        data_argument = next(
            (argument for argument in command if argument.startswith("--data-file=")),
            None,
        )
        if data_argument is not None:
            Path(data_argument.removeprefix("--data-file=")).write_bytes(b"coverage-data")
        return
    if coverage_subcommand != "json":
        return
    output_index = command.index("-o")
    output_path = Path(command[output_index + 1])
    output_path.write_text(
        json.dumps(
            {
                "meta": {
                    "format": 3,
                    "version": "7.15.2",
                    "timestamp": "2026-08-25T12:00:00Z",
                    "branch_coverage": True,
                    "show_contexts": False,
                },
                "files": {
                    "src/example.py": {
                        "executed_lines": [1],
                        "summary": {
                            "covered_lines": 1,
                            "num_statements": 1,
                            "percent_covered": 100.0,
                            "percent_covered_display": "100",
                            "missing_lines": 0,
                            "excluded_lines": 0,
                            "num_branches": 0,
                            "num_partial_branches": 0,
                            "covered_branches": 0,
                            "missing_branches": 0,
                        },
                        "missing_lines": [],
                        "excluded_lines": [],
                        "executed_branches": [],
                        "missing_branches": [],
                    }
                },
                "totals": {
                    "covered_lines": 1,
                    "num_statements": 1,
                    "percent_covered": 100.0,
                    "percent_covered_display": "100",
                    "missing_lines": 0,
                    "excluded_lines": 0,
                    "num_branches": 0,
                    "num_partial_branches": 0,
                    "covered_branches": 0,
                    "missing_branches": 0,
                },
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def _module_index(command: tuple[str, ...], module: str) -> int | None:
    return next(
        (
            index
            for index, pair in enumerate(zip(command, command[1:]))
            if pair == ("-m", module)
        ),
        None,
    )
