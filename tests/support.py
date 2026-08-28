from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess  # nosec B404
from typing import cast
import uuid

from pyrepo_check.config import ProjectConfig
from pyrepo_check.execution import (
    CapturedBytes,
    CheckExecutionFailure,
    DependencyObservation,
    ExecutedProcess,
    PreparedRepositoryEnvironment,
    PythonObservation,
)
from pyrepo_check import execution_workspace
from pyrepo_check.planning import (
    CheckName,
    DefaultRepositoryPython,
    ExplicitRepositoryPython,
    RunPlan,
    build_checks,
)
from pyrepo_check.repository_environment import DependencyName, SUPPORTED_DEPENDENCIES


_SUPPORTED_PYTEST_PREFLIGHT = (
    b'{"schema_version":1,"python_version":[3,13,15],'
    b'"pytest_available":true,"pytest_version":[8,4,2]}'
)
_SUPPORTED_COVERAGE_PREFLIGHT = (
    b'{"schema_version":1,"python_version":[3,13,15],'
    b'"coverage_available":true,"coverage_version":"7.15.2"}'
)

_REPOSITORY_FIXTURE_DEPENDENCIES = (
    "bandit>=1.9,<2",
    "coverage[toml]>=7.15,<8",
    "pytest>=8,<9",
    "ruff>=0.15,<1",
    "ty>=0.0.35,<0.1",
)


def repository_test_environment(storage_root: Path) -> dict[str, str]:
    """Return a child environment whose writable uv cache stays test-owned."""
    resolved_storage = storage_root.resolve()
    return {
        **os.environ,
        "UV_CACHE_DIR": str(resolved_storage / "cache"),
        "UV_PYTHON_CACHE_DIR": str(resolved_storage / "python-cache"),
        "UV_PYTHON_BIN_DIR": str(resolved_storage / "python-bin"),
        "UV_NO_PROGRESS": "1",
    }


def write_locked_repository_fixture(
    tmp_path: Path,
    *,
    python: str,
    dependencies: tuple[str, ...] = _REPOSITORY_FIXTURE_DEPENDENCIES,
) -> Path:
    """Create one committed uv repository without installing pyrepo-check."""
    repository = tmp_path / "repository"
    package = repository / "src" / "fixture_package"
    tests = repository / "tests"
    package.mkdir(parents=True)
    tests.mkdir()
    rendered_dependencies = "\n".join(f'    "{item}",' for item in dependencies)
    (repository / "pyproject.toml").write_text(
        f'''[project]
name = "repository-environment-fixture"
version = "0.0.0"
requires-python = ">=3.10,<3.14"
dependencies = []

[dependency-groups]
dev = [
{rendered_dependencies}
]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.coverage.run]
branch = true
source = ["src/fixture_package"]
parallel = false

[tool.coverage.report]
fail_under = 100

[tool.ruff]
target-version = "py310"

[tool.ty.environment]
python-version = "3.10"

[tool.bandit]
exclude_dirs = [".venv", "tests"]
''',
        encoding="utf-8",
    )
    (repository / ".gitignore").write_text(
        ".venv/\n.pytest_cache/\n.ruff_cache/\n__pycache__/\n",
        encoding="utf-8",
    )
    (package / "__init__.py").write_text(
        '''"""A fully annotated repository fixture package."""


def classify(value: int) -> str:
    """Classify one integer by sign."""
    if value > 0:
        return "positive"
    return "nonpositive"
''',
        encoding="utf-8",
    )
    (tests / "test_fixture_package.py").write_text(
        '''"""Repository-owned tests and environment assertions."""

import importlib.util
import json
import os
import sys
from pathlib import Path

import __main__
from src.fixture_package import classify


def test_classify_covers_both_branches() -> None:
    """Exercise every branch for the fixture's strict Coverage policy."""
    assert classify(1) == "positive"
    assert classify(0) == "nonpositive"


def test_repository_process_has_native_isolated_startup() -> None:
    """Prove repository ownership without importing controller code."""
    controller_path = os.environ["PYREPO_CHECK_CONTROLLER_PATH_SENTINEL"]
    pythonpath = os.environ.get("PYTHONPATH", "")
    spec = __main__.__spec__
    assert Path.cwd() == Path(__file__).parents[1]
    assert controller_path not in pythonpath.split(os.pathsep)
    assert importlib.util.find_spec("pyrepo_check") is None
    assert spec is not None
    assert sys.orig_argv[1] == "-m"
    assert sys.orig_argv[2] in {"coverage", "pytest"}
    witness = os.environ.get("PYREPO_CHECK_NATIVE_STARTUP_WITNESS")
    if witness is not None:
        Path(witness).write_text(
            json.dumps(
                {
                    "path0": sys.path[0],
                    "argv": sys.argv,
                    "orig_argv": sys.orig_argv,
                    "spec": {
                        "name": spec.name,
                        "parent": spec.parent,
                        "origin": spec.origin,
                        "has_location": spec.has_location,
                    },
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
''',
        encoding="utf-8",
    )
    uv = shutil.which("uv")
    git = shutil.which("git")
    if uv is None or git is None:
        raise RuntimeError("real uv and Git executables are required for integration fixtures")
    environment = repository_test_environment(tmp_path / "uv-storage")
    subprocess.run(  # nosec B603
        (str(Path(uv).resolve()), "lock", "--python", python),
        cwd=repository,
        env=environment,
        check=True,
        capture_output=True,
    )
    subprocess.run((str(Path(git).resolve()), "init", "-q"), cwd=repository, check=True)  # nosec B603
    subprocess.run(  # nosec B603
        (str(Path(git).resolve()), "config", "user.name", "pyrepo-check fixture"),
        cwd=repository,
        check=True,
    )
    subprocess.run(  # nosec B603
        (str(Path(git).resolve()), "config", "user.email", "fixture@example.invalid"),
        cwd=repository,
        check=True,
    )
    subprocess.run((str(Path(git).resolve()), "add", "."), cwd=repository, check=True)  # nosec B603
    subprocess.run(  # nosec B603
        (str(Path(git).resolve()), "commit", "-q", "-m", "fixture"),
        cwd=repository,
        check=True,
    )
    return repository


def assert_repository_startup_parity(
    repository: Path,
    *,
    workspace: Path,
    coverage: bool,
) -> None:
    """Compare real native and standalone-launcher startup inside a repository."""
    workspace.mkdir()
    repository_python = repository / ".venv" / "bin" / "python"
    launcher = Path(__file__).parents[1] / "src" / "pyrepo_check" / "_check_launcher.py"
    coverage_data = workspace / ".coverage"
    module = "coverage" if coverage else "pytest"
    module_arguments = (
        (
            "run",
            f"--rcfile={repository / 'pyproject.toml'}",
            f"--data-file={coverage_data}",
            "-m",
            "pytest",
            "-q",
        )
        if coverage
        else ("-q",)
    )
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYREPO_CHECK_CONTROLLER_PATH_SENTINEL"] = str(
        workspace / "controller-pythonpath"
    )
    snapshots: dict[str, dict[str, object]] = {}
    for invocation in ("direct", "launched"):
        witness = workspace / f"{invocation}.json"
        environment["PYREPO_CHECK_NATIVE_STARTUP_WITNESS"] = str(witness)
        if invocation == "direct":
            command = (str(repository_python), "-m", module, *module_arguments)
        else:
            coverage_data.unlink(missing_ok=True)
            command = (
                str(repository_python),
                str(launcher),
                "--evidence",
                str(workspace / "start.json"),
                "--check",
                "pytest",
                "--module",
                module,
                "--",
                *module_arguments,
            )
        completed = subprocess.run(  # nosec B603
            command,
            cwd=repository,
            env=environment,
            check=False,
            capture_output=True,
        )
        assert completed.returncode == 0, completed.stderr.decode()
        snapshots[invocation] = cast(
            dict[str, object], json.loads(witness.read_text(encoding="utf-8"))
        )

    direct = snapshots["direct"]
    launched = snapshots["launched"]
    assert launched["path0"] == direct["path0"]
    assert launched["argv"] == direct["argv"]
    assert launched["orig_argv"] == direct["orig_argv"]
    assert launched["spec"] == direct["spec"]


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


def prepared_repository(
    root: Path,
    python: tuple[int, int, int],
) -> PreparedRepositoryEnvironment:
    """Build one complete prepared Repository Environment test observation."""
    resolved = root.resolve()
    environment_root = resolved / ".venv"
    executable = environment_root / "bin" / f"python-{'.'.join(map(str, python))}"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"")
    return PreparedRepositoryEnvironment(
        root=resolved,
        path=environment_root,
        python=PythonObservation("cpython", python, executable),
        python_selection=DefaultRepositoryPython(),
        manager_version="0.10.12",
        child_environment={},
    )


@contextmanager
def test_workspace(root: Path) -> Iterator[execution_workspace.VerifiedRunWorkspace]:
    """Hold one real Task 1 workspace and clean it with the production seam."""
    workspace = execution_workspace.create_run_workspace(root.resolve())
    verified = execution_workspace.open_verified_workspace(workspace)
    try:
        yield verified
    finally:
        verified.close()
        observation = execution_workspace.remove_run_workspace(
            workspace,
            repository_root=root.resolve(),
        )
        if observation is not None:
            raise OSError(execution_workspace._cleanup_diagnostic(observation))


setattr(test_workspace, "__test__", False)


def launcher_aware_runner(
    *,
    returncode: int = 0,
    publish_valid_marker: bool,
    returncodes: tuple[int, ...] = (),
    stdout: tuple[bytes | str | None, ...] = (),
    stderr: tuple[bytes | str | None, ...] = (),
    raise_on_call: int | None = None,
    exception: Exception | None = None,
) -> RecordingRunner:
    """Return a scripted runner that publishes exact launcher start evidence."""

    def publish(call: RecordedCall) -> None:
        if not publish_valid_marker or "--evidence" not in call.command:
            return
        evidence_index = call.command.index("--evidence")
        check_index = call.command.index("--check")
        module_index = call.command.index("--module")
        separator_index = call.command.index("--", module_index + 2)
        python_index = call.command.index("--python")
        executable = Path(call.command[python_index + 1])
        version = tuple(
            int(piece) for piece in executable.name.removeprefix("python-").split(".")
        )
        arguments = call.command[separator_index + 1 :]
        digest = hashlib.sha256()
        for argument in arguments:
            encoded = argument.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        payload = {
            "schema_version": 1,
            "check": call.command[check_index + 1],
            "module": call.command[module_index + 1],
            "arguments_sha256": digest.hexdigest(),
            "python": {
                "implementation": "cpython",
                "version": list(version),
                "executable": str(executable),
            },
        }
        marker = Path(call.command[evidence_index + 1])
        descriptor = os.open(
            marker,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            content = json.dumps(payload, separators=(",", ":")).encode()
            os.write(descriptor, content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    selected_returncodes = returncodes or (returncode,)
    return RecordingRunner(
        returncodes=selected_returncodes,
        stdout=stdout,
        stderr=stderr,
        raise_on_call=raise_on_call,
        exception=exception,
        on_call=publish,
    )


def available_dependency(name: DependencyName, version: str) -> DependencyObservation:
    dependency = SUPPORTED_DEPENDENCIES[name]
    return DependencyObservation(
        name=dependency.name,
        module=dependency.module,
        required=_dependency_required(dependency.minimum, dependency.maximum),
        status="available",
        version=version,
        origin=f"/repository/.venv/site-packages/{dependency.module}/__init__.py",
        process=_dependency_process(name),
        error=None,
    )


def missing_dependency(name: DependencyName) -> DependencyObservation:
    dependency = SUPPORTED_DEPENDENCIES[name]
    required = _dependency_required(dependency.minimum, dependency.maximum)
    return DependencyObservation(
        name=dependency.name,
        module=dependency.module,
        required=required,
        status="missing",
        version=None,
        origin=None,
        process=_dependency_process(name),
        error=CheckExecutionFailure(
            code="check_dependency_missing",
            message=f"Repository dependency {name} is missing.",
            hint=(
                f"Add {name} {required} to the locked Repository Environment, "
                "then retry."
            ),
        ),
    )


def _dependency_process(name: DependencyName) -> ExecutedProcess:
    return ExecutedProcess(
        role="dependency_probe",
        command=("uv", "run", "dependency-probe", name),
        cwd=Path("/repository"),
        returncode=0,
        duration_ms=1,
        stdout=CapturedBytes(b"", 0),
        stderr=CapturedBytes(b"", 0),
        spawn_error=None,
    )


def _dependency_required(
    minimum: tuple[int, ...],
    maximum: tuple[int, ...],
) -> str:
    lower = ".".join(str(part) for part in minimum)
    upper = ".".join(str(part) for part in maximum)
    return f">={lower},<{upper}"


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
