from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess  # nosec B404
from typing import cast


_SUPPORTED_PYTEST_PREFLIGHT = (
    b'{"schema_version":1,"python_version":[3,13,15],'
    b'"pytest_available":true,"pytest_version":[8,4,2]}'
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
    ) -> None:
        self.returncodes = returncodes
        self.stdout = stdout
        self.stderr = stderr
        self.raise_on_call = raise_on_call
        self.exception = exception
        self.on_call = on_call
        self.publish_pytest_artifact = publish_pytest_artifact
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
        return cast(
            subprocess.CompletedProcess[tuple[str, ...]],
            subprocess.CompletedProcess(
                command,
                returncode=returncode,
                stdout=(
                    self.stdout[returncode_index]
                    if returncode_index < len(self.stdout)
                    else _SUPPORTED_PYTEST_PREFLIGHT
                    if "-c" in command
                    else None
                ),
                stderr=(
                    self.stderr[returncode_index]
                    if returncode_index < len(self.stderr)
                    else None
                ),
            ),
        )


def _publish_pytest_artifact(
    command: tuple[str, ...],
    environment: Mapping[str, str] | None,
    returncode: int,
) -> None:
    if environment is None or "-m" not in command:
        return
    module_index = command.index("-m")
    if command[module_index + 1] != "pytest":
        return
    plugin_index = command.index("-p", module_index + 2)
    artifact_path = Path(environment["PYREPO_CHECK_PYTEST_JSON"])
    writer_directory = Path(environment["PYREPO_CHECK_PYTEST_WRITER_DIR"])
    writer_id = "recording-runner"
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
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    (writer_directory / f"pytest-writer-{writer_id}.json").write_text(
        json.dumps({"schema_version": 1, "writer_id": writer_id, "pid": 1}),
        encoding="utf-8",
    )
