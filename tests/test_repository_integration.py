from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess  # nosec B404
import sys
import textwrap
from typing import Any

import pytest

from pyrepo_check import repository_executor
from pyrepo_check.planning import ExplicitRepositoryPython
from tests import support


pytestmark = pytest.mark.integration

_DEPENDENCIES = (
    "bandit>=1.9,<2",
    "coverage[toml]>=7.15,<8",
    "pytest>=8,<9",
    "ruff>=0.15,<1",
    "ty>=0.0.35,<0.1",
)
_UV_STORAGE_VARIABLES = (
    "UV_CACHE_DIR",
    "UV_PYTHON_INSTALL_DIR",
    "UV_PYTHON_CACHE_DIR",
    "UV_PYTHON_BIN_DIR",
)
_UV_STORAGE_BASES = (
    ("LOCALAPPDATA", "APPDATA", "USERPROFILE", "XDG_DATA_HOME", "XDG_BIN_HOME")
    if os.name == "nt"
    else ("HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_BIN_HOME")
)


def _run_repository_json(
    repository: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[bytes], dict[str, Any]]:
    child_environment = (
        support.repository_test_environment(repository.parent / "uv-storage")
        if environment is None
        else environment
    )
    controller_path = str(repository.parent / "controller-pythonpath")
    child_environment["PYTHONPATH"] = controller_path
    child_environment["PYREPO_CHECK_CONTROLLER_PATH_SENTINEL"] = controller_path
    completed = subprocess.run(  # nosec B603
        (
            sys.executable,
            "-m",
            "pyrepo_check.cli",
            "--root",
            str(repository),
            "--format",
            "json",
            *arguments,
        ),
        check=False,
        capture_output=True,
        env=child_environment,
    )
    assert completed.stdout, completed.stderr.decode()
    return completed, json.loads(completed.stdout)


def _resolved_executable(name: str) -> Path:
    executable = shutil.which(name)
    assert executable is not None, f"required executable is unavailable: {name}"
    return Path(executable).resolve()


def _link_executable(directory: Path, name: str) -> None:
    """Expose one executable through an isolated test-owned PATH."""
    executable = _resolved_executable(name)
    if os.name == "nt":
        shutil.copy2(executable, directory / executable.name)
        return
    (directory / name).symlink_to(executable)


def _directory_alias(alias: Path, target: Path) -> None:
    """Create a directory alias without requiring Windows symlink privilege."""
    if os.name != "nt":
        support.symlink_or_skip(alias, target, target_is_directory=True)
        return
    completed = subprocess.run(  # nosec B603
        ("cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(target)),
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr.decode()


def _run_git(
    repository: Path,
    *arguments: str,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # nosec B603
        (str(_resolved_executable("git")), *arguments),
        cwd=repository,
        input=input_bytes,
        check=True,
        capture_output=True,
    )


def _commit_fixture_change(repository: Path, message: str) -> None:
    _run_git(repository, "add", "-A", "--force")
    _run_git(repository, "commit", "-q", "-m", message)


def _tracked_bytes(repository: Path) -> dict[str, bytes]:
    completed = _run_git(repository, "ls-files", "-z")
    paths = [path for path in completed.stdout.decode().split("\0") if path]
    return {
        path: (
            os.readlink(repository / path).encode()
            if (repository / path).is_symlink()
            else (repository / path).read_bytes()
        )
        for path in paths
    }


def _repository_process_roles(report: dict[str, Any]) -> list[str]:
    return [
        process["role"]
        for process in report["repository_environment"]["processes"]
    ]


def _assert_no_uv_or_check_start(report: dict[str, Any]) -> None:
    assert "uv_version" not in _repository_process_roles(report)
    assert "environment_probe" not in _repository_process_roles(report)
    assert all(check["processes"] == [] for check in report["checks"])
    assert all(check["start_evidence"] is None for check in report["checks"])


def _without_dependency(name: str) -> tuple[str, ...]:
    return tuple(dependency for dependency in _DEPENDENCIES if not dependency.startswith(name))


def _overridden_uv_storage_variables(base_variable: str) -> tuple[str, ...]:
    if os.name == "nt":
        return {
            "LOCALAPPDATA": ("UV_CACHE_DIR",),
            "APPDATA": ("UV_PYTHON_INSTALL_DIR",),
            "USERPROFILE": ("UV_PYTHON_BIN_DIR",),
            "XDG_DATA_HOME": ("UV_PYTHON_BIN_DIR",),
            "XDG_BIN_HOME": ("UV_PYTHON_BIN_DIR",),
        }[base_variable]
    return {
        "HOME": ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_PYTHON_BIN_DIR"),
        "XDG_CACHE_HOME": ("UV_CACHE_DIR",),
        "XDG_DATA_HOME": ("UV_PYTHON_INSTALL_DIR", "UV_PYTHON_BIN_DIR"),
        "XDG_BIN_HOME": ("UV_PYTHON_BIN_DIR",),
    }[base_variable]


def _write_uv_proxy(tmp_path: Path, mode: str) -> Path:
    proxy_directory = tmp_path / "uv-proxy"
    proxy_directory.mkdir()
    proxy = proxy_directory / "uv-proxy.py"
    proxy.write_text(
        f'''#!{sys.executable}
import json
import os
import subprocess
import sys

REAL_UV = {str(_resolved_executable("uv"))!r}
MODE = {mode!r}
arguments = sys.argv[1:]
if MODE == "outer-exit" and "--evidence" in arguments:
    raise SystemExit(1)
completed = subprocess.run(
    (REAL_UV, *arguments),
    check=False,
    capture_output=True,
    env=os.environ,
)
stdout = completed.stdout
source = arguments[arguments.index("-c") + 1] if "-c" in arguments else ""
target = "dependency" if "distribution_name" in source else (
    "environment" if "environment_root" in source else None
)
if MODE.startswith("environment-") and target == "environment":
    operation = MODE.removeprefix("environment-")
elif MODE.startswith("dependency-") and target == "dependency":
    operation = MODE.removeprefix("dependency-")
else:
    operation = None
if operation == "append":
    stdout += b"\\ntrailing-evidence"
elif operation == "truncate":
    stdout = stdout[: max(1, len(stdout) // 2)]
elif operation == "duplicate":
    stdout = stdout.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1', 1)
elif operation == "unknown":
    payload = json.loads(stdout)
    payload["unknown"] = True
    stdout = json.dumps(payload, separators=(",", ":")).encode()
elif operation == "contradict":
    payload = json.loads(stdout)
    if target == "environment":
        payload["implementation"] = "pypy"
    else:
        payload["distribution"] = "contradictory-distribution"
    stdout = json.dumps(payload, separators=(",", ":")).encode()
sys.stdout.buffer.write(stdout)
sys.stderr.buffer.write(completed.stderr)
raise SystemExit(completed.returncode)
''',
        encoding="utf-8",
    )
    if os.name == "nt":
        package = proxy_directory / "package"
        package.mkdir()
        (package / "pyproject.toml").write_text(
            '''[project]
name = "test-uv-proxy"
version = "0.0.0"

[project.scripts]
uv = "test_uv_proxy:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["test_uv_proxy"]
''',
            encoding="utf-8",
        )
        payload = proxy.read_text(encoding="utf-8").split("\n", maxsplit=1)[1]
        module = package / "test_uv_proxy"
        module.mkdir()
        (module / "__init__.py").write_text(
            "def main() -> None:\n" + textwrap.indent(payload, "    "),
            encoding="utf-8",
        )
        environment = proxy_directory / "venv"
        created = subprocess.run(  # nosec B603
            (_resolved_executable("uv"), "venv", "--python", sys.executable, str(environment)),
            check=False,
            capture_output=True,
        )
        assert created.returncode == 0, created.stderr.decode()
        installed = subprocess.run(  # nosec B603
            (
                _resolved_executable("uv"),
                "pip",
                "install",
                "--python",
                str(environment / "Scripts" / "python.exe"),
                str(package),
            ),
            check=False,
            capture_output=True,
        )
        assert installed.returncode == 0, installed.stderr.decode()
        shutil.copy2(environment / "Scripts" / "uv.exe", proxy_directory / "uv.exe")
    else:
        launcher = proxy_directory / "uv"
        shutil.copy2(proxy, launcher)
        launcher.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return proxy_directory


def _add_artifact_attack(repository: Path) -> None:
    (repository / "conftest.py").write_text(
        '''"""Corrupt repository-controlled pytest evidence after plugin finalization."""

import atexit
import json
import os
from pathlib import Path
import sys

def _attack_after_finalization():
    artifact = Path(os.environ["PYREPO_CHECK_PYTEST_JSON"])
    mode = os.environ["PYREPO_CHECK_ARTIFACT_ATTACK"]
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    if payload.get("state") != "finalized":
        raise RuntimeError("pytest artifact was not finalized before attack")
    if mode == "replace":
        replacement = artifact.with_name("replacement.json")
        replacement.write_text('{"schema_version":1}', encoding="utf-8")
        os.replace(replacement, artifact)
    elif mode == "truncate":
        artifact.write_bytes(b"{")
    else:
        payload["session"]["starts"] = 2
        artifact.write_text(json.dumps(payload), encoding="utf-8")
    print(f"PYREPO_CHECK_ATTACK_COMPLETE:{mode}", file=sys.stderr)

def pytest_unconfigure(config):
    plugin = next(
        module
        for name, module in sys.modules.items()
        if name.startswith("_pyrepo_check_pytest_")
    )
    atexit.unregister(plugin._finalize_at_exit)
    atexit.register(_attack_after_finalization)
    atexit.register(plugin._finalize_at_exit)
''',
        encoding="utf-8",
    )
    _commit_fixture_change(repository, "add hostile artifact fixture")


def test_missing_environment_is_rebuilt_from_the_current_lock(
    tmp_path: Path,
) -> None:
    repository = support.write_locked_repository_fixture(tmp_path, python="3.13")

    completed, report = _run_repository_json(repository, "--all")

    assert completed.returncode == 0, completed.stderr.decode()
    assert report["schema_version"] == 2
    assert report["repository_environment"]["lock"]["status"] == "current"
    assert report["repository_environment"]["mutation_protection"] == "tracked_files"
    assert (repository / ".venv").is_dir()
    repository_python = Path(report["repository_environment"]["python"]["executable"])
    assert repository_python.is_relative_to(repository / ".venv")
    if os.name == "nt":
        assert repository_python.resolve().is_relative_to(repository / ".venv")
    else:
        assert repository_python.resolve().is_relative_to(repository) is False
    assert [dependency["name"] for dependency in report["repository_environment"]["dependencies"]] == [
        "ruff",
        "ty",
        "bandit",
        "pytest",
        "coverage",
    ]
    assert all(
        dependency["status"] == "available"
        for dependency in report["repository_environment"]["dependencies"]
    )
    assert all(check["execution_environment"] == "repository" for check in report["checks"])
    assert all(check["start_evidence"] is not None for check in report["checks"])
    support.assert_repository_startup_parity(
        repository,
        workspace=tmp_path / "plain-pytest-startup",
        coverage=False,
    )
    support.assert_repository_startup_parity(
        repository,
        workspace=tmp_path / "coverage-pytest-startup",
        coverage=True,
    )


def test_missing_lock_stops_before_uv_or_any_check(tmp_path: Path) -> None:
    repository = support.write_locked_repository_fixture(tmp_path, python="3.13")
    (repository / "uv.lock").unlink()

    completed, report = _run_repository_json(repository, "ty")

    assert completed.returncode == 2
    assert report["repository_environment"]["lock"]["status"] == "missing"
    assert report["repository_environment"]["error"]["code"] == "repository_lock_missing"
    _assert_no_uv_or_check_start(report)


def test_noncurrent_lock_retains_bounded_uv_diagnostics_without_check_start(
    tmp_path: Path,
) -> None:
    repository = support.write_locked_repository_fixture(tmp_path, python="3.13")
    pyproject = repository / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            "dependencies = []",
            'dependencies = ["typing-extensions>=4"]',
        ),
        encoding="utf-8",
    )

    completed, report = _run_repository_json(repository, "ty")

    environment = report["repository_environment"]
    assert completed.returncode == 2
    assert environment["lock"]["status"] == "unverified"
    assert environment["error"]["code"] == "repository_environment_failed"
    probe = next(process for process in environment["processes"] if process["role"] == "environment_probe")
    assert probe["stderr"]["text"]
    assert len(probe["stderr"]["text"].encode()) <= 65_536
    assert all(check["processes"] == [] for check in report["checks"])


def test_controller_python_and_uv_selectors_are_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = support.write_locked_repository_fixture(tmp_path, python="3.13")
    environment = support.repository_test_environment(tmp_path / "uv-storage")
    environment.update(
        {
            "CONDA_PREFIX": str(repository / "hostile-conda"),
            "PYTHONHOME": str(repository / "hostile-home"),
            "PYTHONEXECUTABLE": str(repository / "hostile-python"),
            "UV_MANAGED_PYTHON": "0",
            "UV_NO_MANAGED_PYTHON": "1",
            "UV_PYTHON": "3.10",
            "UV_PYTHON_PREFERENCE": "only-system",
            "UV_PYTHON_SEARCH_PATH": str(repository / "hostile-search"),
            "VIRTUAL_ENV": str(repository / "hostile-venv"),
            "__PYVENV_LAUNCHER__": str(repository / "hostile-launcher"),
        }
    )
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    result = repository_executor.execute_repository_plan(
        support.focused_plan(repository, "ty", "3.13")
    )

    assert result.repository_environment.error is None
    selection = result.repository_environment.python_selection
    assert isinstance(selection, ExplicitRepositoryPython)
    assert selection.request == "3.13"
    assert result.repository_environment.python is not None
    assert result.repository_environment.python.version[:2] == (3, 13)
    assert result.checks[0].error is None


@pytest.mark.parametrize("variable", _UV_STORAGE_VARIABLES)
@pytest.mark.parametrize("destination_kind", ("lexical", "resolved"))
def test_unsafe_uv_storage_override_stops_before_processes(
    tmp_path: Path,
    variable: str,
    destination_kind: str,
) -> None:
    repository = support.write_locked_repository_fixture(tmp_path, python="3.13")
    environment = support.repository_test_environment(tmp_path / "safe-storage")
    if destination_kind == "lexical":
        destination = repository / "storage"
    else:
        alias = tmp_path / "repository-alias"
        _directory_alias(alias, repository)
        destination = alias / "storage"
    environment[variable] = str(destination)

    completed, report = _run_repository_json(repository, "ty", environment=environment)

    assert completed.returncode == 2
    assert report["repository_environment"]["error"]["code"] == "unsafe_repository_environment"
    _assert_no_uv_or_check_start(report)


@pytest.mark.parametrize("base_variable", _UV_STORAGE_BASES)
@pytest.mark.parametrize("destination_kind", ("lexical", "resolved"))
def test_unsafe_uv_default_storage_base_stops_before_processes(
    tmp_path: Path,
    base_variable: str,
    destination_kind: str,
) -> None:
    repository = support.write_locked_repository_fixture(tmp_path, python="3.13")
    environment = support.repository_test_environment(tmp_path / "safe-storage")
    suffix = "data" if os.name == "nt" and base_variable == "XDG_DATA_HOME" else ""
    if destination_kind == "lexical":
        environment[base_variable] = str(repository / suffix)
    else:
        alias = tmp_path / "repository-alias"
        _directory_alias(alias, repository)
        environment[base_variable] = str(alias / suffix)
    for variable in _overridden_uv_storage_variables(base_variable):
        environment.pop(variable, None)

    completed, report = _run_repository_json(repository, "ty", environment=environment)

    assert completed.returncode == 2
    assert report["repository_environment"]["error"]["code"] == "unsafe_repository_environment"
    _assert_no_uv_or_check_start(report)


@pytest.mark.parametrize("state", ("tracked", "unignored", "symlink"))
def test_unsafe_repository_environment_path_stops_before_uv(
    tmp_path: Path,
    state: str,
) -> None:
    repository = support.write_locked_repository_fixture(tmp_path, python="3.13")
    environment_path = repository / ".venv"
    if state == "tracked":
        environment_path.mkdir()
        (environment_path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        _commit_fixture_change(repository, "track repository environment")
    elif state == "unignored":
        (repository / ".gitignore").write_text(
            ".pytest_cache/\n.ruff_cache/\n__pycache__/\n",
            encoding="utf-8",
        )
        _commit_fixture_change(repository, "remove environment ignore")
    else:
        external = tmp_path / "external-environment"
        external.mkdir()
        support.symlink_or_skip(environment_path, external, target_is_directory=True)

    completed, report = _run_repository_json(repository, "ty")

    assert completed.returncode == 2
    assert report["repository_environment"]["error"]["code"] == "unsafe_repository_environment"
    assert "uv_version" not in _repository_process_roles(report)
    assert all(check["processes"] == [] for check in report["checks"])


def test_unavailable_git_is_bounded_and_stops_before_uv(tmp_path: Path) -> None:
    repository = support.write_locked_repository_fixture(tmp_path, python="3.13")
    executable_directory = tmp_path / "uv-only-bin"
    executable_directory.mkdir()
    _link_executable(executable_directory, "uv")
    environment = support.repository_test_environment(tmp_path / "uv-storage")
    environment["PATH"] = str(executable_directory)

    completed, report = _run_repository_json(repository, "ty", environment=environment)

    repository_environment = report["repository_environment"]
    assert completed.returncode == 2
    assert repository_environment["error"]["code"] == "unsafe_repository_environment"
    assert _repository_process_roles(report) == []
    assert repository_environment["processes"] == []
    _assert_no_uv_or_check_start(report)


def test_unmerged_index_stops_before_uv_without_a_merge_race(tmp_path: Path) -> None:
    repository = support.write_locked_repository_fixture(tmp_path, python="3.13")
    object_ids = [
        _run_git(repository, "hash-object", "-w", "--stdin", input_bytes=content).stdout
        .decode()
        .strip()
        for content in (b"base\n", b"ours\n", b"theirs\n")
    ]
    index_records = "".join(
        f"100644 {object_id} {stage}\tconflict.py\n"
        for stage, object_id in enumerate(object_ids, start=1)
    ).encode()
    _run_git(repository, "update-index", "--index-info", input_bytes=index_records)
    unmerged_entries: dict[int, str] = {}
    for line in _run_git(repository, "ls-files", "-u").stdout.decode().splitlines():
        metadata, path = line.split("\t", maxsplit=1)
        mode, object_id, stage = metadata.split()
        assert mode == "100644"
        assert path == "conflict.py"
        unmerged_entries[int(stage)] = object_id
    assert unmerged_entries == {
        stage: object_id for stage, object_id in enumerate(object_ids, start=1)
    }

    completed, report = _run_repository_json(repository, "ty")

    assert completed.returncode == 2
    assert report["repository_environment"]["error"]["code"] == "unsafe_repository_environment"
    assert _repository_process_roles(report) == ["repository_safety"] * 4
    _assert_no_uv_or_check_start(report)


def test_initially_dirty_tracked_bytes_remain_accepted_when_unchanged(
    tmp_path: Path,
) -> None:
    repository = support.write_locked_repository_fixture(tmp_path, python="3.13")
    package = repository / "src" / "fixture_package" / "__init__.py"
    package.write_text(package.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    before = _tracked_bytes(repository)
    before_status = _run_git(repository, "status", "--short").stdout

    completed, report = _run_repository_json(repository, "ty")

    assert completed.returncode == 0, completed.stderr.decode()
    assert report["repository_environment"]["error"] is None
    assert report["repository_environment"]["mutation_protection"] == "tracked_files"
    assert _tracked_bytes(repository) == before
    assert _run_git(repository, "status", "--short").stdout == before_status


def test_tracked_mutation_is_reported_without_rewriting_check_evidence(
    tmp_path: Path,
) -> None:
    repository = support.write_locked_repository_fixture(tmp_path, python="3.13")
    (repository / "tests" / "test_mutation.py").write_text(
        '''"""Deliberately mutate one tracked file during repository execution."""

from pathlib import Path


def test_mutates_tracked_source() -> None:
    """Exercise post-execution mutation detection."""
    package = Path(__file__).parents[1] / "src" / "fixture_package" / "__init__.py"
    package.write_text(package.read_text(encoding="utf-8") + "# mutated\\n", encoding="utf-8")
''',
        encoding="utf-8",
    )
    _commit_fixture_change(repository, "add mutation fixture")
    before = _tracked_bytes(repository)

    completed, report = _run_repository_json(repository, "pytest")

    pytest_check = report["checks"][0]
    assert completed.returncode == 2
    assert report["repository_environment"]["error"]["code"] == "repository_state_changed"
    assert pytest_check["status"] == "passed"
    assert pytest_check["execution_environment"] == "repository"
    assert pytest_check["start_evidence"] is not None
    assert _tracked_bytes(repository) != before


def test_missing_ruff_does_not_suppress_independent_checks(tmp_path: Path) -> None:
    repository = support.write_locked_repository_fixture(
        tmp_path,
        python="3.13",
        dependencies=_without_dependency("ruff"),
    )

    completed, report = _run_repository_json(repository, "--all")

    statuses = {check["name"]: check["status"] for check in report["checks"]}
    assert completed.returncode == 2
    assert statuses == {
        "ruff": "error",
        "annotations": "error",
        "ty": "passed",
        "bandit": "passed",
        "pytest": "passed",
    }
    assert next(
        dependency
        for dependency in report["repository_environment"]["dependencies"]
        if dependency["name"] == "ruff"
    )["status"] == "missing"
    assert all(
        next(check for check in report["checks"] if check["name"] == name)["start_evidence"]
        is not None
        for name in ("ty", "bandit", "pytest")
    )


def test_missing_coverage_runs_one_plain_pytest_and_retains_error(
    tmp_path: Path,
) -> None:
    repository = support.write_locked_repository_fixture(
        tmp_path,
        python="3.13",
        dependencies=_without_dependency("coverage"),
    )

    completed, report = _run_repository_json(repository, "pytest", "--coverage")

    pytest_check = report["checks"][0]
    assert completed.returncode == 2
    assert report["pytest"]["status"] == "passed"
    assert report["coverage"]["status"] == "error"
    assert report["coverage"]["error"]["code"] == "module_unavailable"
    assert [process["role"] for process in pytest_check["processes"]] == ["primary"]
    primary = pytest_check["processes"][0]
    module_index = primary["argv"].index("--module")
    assert primary["argv"][module_index + 1] == "pytest"
    assert pytest_check["start_evidence"] is not None


def test_outer_uv_exit_one_without_a_marker_is_an_execution_error(
    tmp_path: Path,
) -> None:
    repository = support.write_locked_repository_fixture(tmp_path, python="3.13")
    proxy_directory = _write_uv_proxy(tmp_path, "outer-exit")
    environment = support.repository_test_environment(tmp_path / "uv-storage")
    environment["PATH"] = os.pathsep.join((str(proxy_directory), environment["PATH"]))

    completed, report = _run_repository_json(repository, "ruff", environment=environment)

    check = report["checks"][0]
    assert completed.returncode == 2
    assert report["repository_environment"]["lock"]["status"] == "current"
    assert check["status"] == "error"
    assert check["error"]["code"] == "check_start_evidence_invalid"
    assert check["execution_environment"] is None
    assert check["start_evidence"] is None
    assert len(check["processes"]) == 1
    assert check["processes"][0]["exit_code"] == 1


@pytest.mark.parametrize(
    "operation",
    ("append", "truncate", "duplicate", "unknown", "contradict"),
)
def test_hostile_environment_probe_stdout_is_rejected_with_bounded_evidence(
    tmp_path: Path,
    operation: str,
) -> None:
    repository = support.write_locked_repository_fixture(tmp_path, python="3.13")
    proxy_directory = _write_uv_proxy(tmp_path, f"environment-{operation}")
    environment = support.repository_test_environment(tmp_path / "uv-storage")
    environment["PATH"] = os.pathsep.join((str(proxy_directory), environment["PATH"]))

    completed, report = _run_repository_json(repository, "ruff", environment=environment)

    repository_environment = report["repository_environment"]
    probe = next(
        process
        for process in repository_environment["processes"]
        if process["role"] == "environment_probe"
    )
    assert completed.returncode == 2
    assert repository_environment["error"]["code"] in {
        "environment_evidence_invalid",
        "repository_python_unsupported",
    }
    assert probe["stdout"]["text"]
    assert len(probe["stdout"]["text"].encode()) <= 65_536
    assert all(check["processes"] == [] for check in report["checks"])


@pytest.mark.parametrize("operation", ("append", "truncate", "contradict"))
def test_hostile_dependency_probe_stdout_isolated_to_the_dependent_check(
    tmp_path: Path,
    operation: str,
) -> None:
    repository = support.write_locked_repository_fixture(tmp_path, python="3.13")
    proxy_directory = _write_uv_proxy(tmp_path, f"dependency-{operation}")
    environment = support.repository_test_environment(tmp_path / "uv-storage")
    environment["PATH"] = os.pathsep.join((str(proxy_directory), environment["PATH"]))

    completed, report = _run_repository_json(repository, "ruff", environment=environment)

    dependency = report["repository_environment"]["dependencies"][0]
    check = report["checks"][0]
    assert completed.returncode == 2
    assert report["repository_environment"]["error"] is None
    assert dependency["status"] == "unobserved"
    assert dependency["error"]["code"] == "check_dependency_unusable"
    assert dependency["process"]["stdout"]["text"]
    assert len(dependency["process"]["stdout"]["text"].encode()) <= 65_536
    assert check["status"] == "error"
    assert check["processes"] == []
    assert check["start_evidence"] is None


@pytest.mark.parametrize("mode", ("replace", "truncate", "contradict"))
def test_repository_controlled_pytest_artifact_attacks_are_rejected(
    tmp_path: Path,
    mode: str,
) -> None:
    repository = support.write_locked_repository_fixture(tmp_path, python="3.13")
    _add_artifact_attack(repository)
    environment = support.repository_test_environment(tmp_path / "uv-storage")
    environment["PYREPO_CHECK_ARTIFACT_ATTACK"] = mode

    completed, report = _run_repository_json(repository, "pytest", environment=environment)

    repository_environment = report["repository_environment"]
    check = report["checks"][0]
    primary = check["processes"][0]
    pytest_result = report["pytest"]
    assert completed.returncode == 2
    assert repository_environment["error"] is None
    assert check["status"] == "error"
    assert check["error"]["code"] == "pytest_evidence_error"
    assert check["execution_environment"] == "repository"
    assert check["start_evidence"] is not None
    assert primary["exit_code"] == 0
    assert f"PYREPO_CHECK_ATTACK_COMPLETE:{mode}" in primary["stderr"]["text"]
    assert pytest_result["status"] == "error"
    assert pytest_result["error"]["code"] == "artifact_invalid"
    assert primary["stdout"]["text"]
    assert len(primary["stdout"]["text"].encode()) <= 1_048_576


def _isolated_acquisition_environment(
    root: Path,
    *,
    include_git: bool,
    downloads: str,
) -> dict[str, str]:
    executable_directory = root / "bin"
    executable_directory.mkdir(parents=True)
    _link_executable(executable_directory, "uv")
    path_entries = [str(executable_directory)]
    if include_git:
        if os.name == "nt":
            path_entries.append(str(_resolved_executable("git").parent))
        else:
            _link_executable(executable_directory, "git")
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("UV_")
        and name
        not in {
            "CONDA_PREFIX",
            "PYTHONHOME",
            "PYTHONPATH",
            "PYTHONEXECUTABLE",
            "VIRTUAL_ENV",
            "__PYVENV_LAUNCHER__",
        }
    }
    environment.update(
        {
            "HOME": str(root / "home"),
            "PATH": os.pathsep.join(path_entries),
            "UV_CACHE_DIR": str(root / "cache"),
            "UV_PYTHON_BIN_DIR": str(root / "python-bin"),
            "UV_PYTHON_CACHE_DIR": str(root / "python-cache"),
            "UV_PYTHON_DOWNLOADS": downloads,
            "UV_PYTHON_INSTALL_DIR": str(root / "python-install"),
            "XDG_CACHE_HOME": str(root / "xdg-cache"),
            "XDG_DATA_HOME": str(root / "xdg-data"),
        }
    )
    return environment


def _missing_download_candidate(tmp_path: Path) -> tuple[str, list[str]]:
    candidate_root = tmp_path / "candidate-storage"
    environment = _isolated_acquisition_environment(
        candidate_root,
        include_git=False,
        downloads="automatic",
    )
    evidence: list[str] = []
    for candidate in ("3.12.12", "3.12.11", "3.12.10"):
        listed = subprocess.run(  # nosec B603
            ("uv", "python", "list", "--all-versions", candidate),
            check=False,
            capture_output=True,
            env=environment,
        )
        listing = listed.stdout.decode(errors="replace")
        find_environment = {**environment, "UV_PYTHON_DOWNLOADS": "never"}
        found = subprocess.run(  # nosec B603
            ("uv", "python", "find", candidate),
            check=False,
            capture_output=True,
            env=find_environment,
        )
        evidence.append(
            f"{candidate}: list={listed.returncode}:{listing[:512]!r}; "
            f"find={found.returncode}:{found.stderr.decode(errors='replace')[:512]!r}"
        )
        advertised = any(
            line.startswith(f"cpython-{candidate}-") and "<download available>" in line
            for line in listing.splitlines()
        )
        if listed.returncode == 0 and advertised and found.returncode != 0:
            return candidate, evidence
    raise AssertionError(
        "no deterministic missing download candidate; " + " | ".join(evidence)
    )


def test_isolated_python_acquisition_obeys_allowed_and_forbidden_policy(
    tmp_path: Path,
) -> None:
    candidate, precondition_evidence = _missing_download_candidate(tmp_path)
    assert precondition_evidence
    allowed_root = tmp_path / "allowed"
    forbidden_root = tmp_path / "forbidden"
    allowed_root.mkdir()
    forbidden_root.mkdir()
    allowed_repository = support.write_locked_repository_fixture(
        allowed_root,
        python="3.13",
    )
    forbidden_repository = support.write_locked_repository_fixture(
        forbidden_root,
        python="3.13",
    )
    allowed_environment = _isolated_acquisition_environment(
        allowed_root / "external-storage",
        include_git=True,
        downloads="automatic",
    )
    forbidden_environment = _isolated_acquisition_environment(
        forbidden_root / "external-storage",
        include_git=True,
        downloads="never",
    )
    allowed_install = Path(allowed_environment["UV_PYTHON_INSTALL_DIR"])
    forbidden_install = Path(forbidden_environment["UV_PYTHON_INSTALL_DIR"])
    allowed_before = _tracked_bytes(allowed_repository)
    forbidden_before = _tracked_bytes(forbidden_repository)

    allowed, allowed_report = _run_repository_json(
        allowed_repository,
        "--python",
        candidate,
        "ty",
        environment=allowed_environment,
    )
    forbidden, forbidden_report = _run_repository_json(
        forbidden_repository,
        "--python",
        candidate,
        "ty",
        environment=forbidden_environment,
    )

    assert allowed.returncode == 0, allowed.stderr.decode()
    assert allowed_report["repository_environment"]["python"]["version"] == [
        int(piece) for piece in candidate.split(".")
    ]
    assert allowed_install.is_dir()
    assert any(allowed_install.iterdir())
    assert _tracked_bytes(allowed_repository) == allowed_before
    forbidden_environment_evidence = forbidden_report["repository_environment"]
    assert forbidden.returncode == 2
    assert forbidden_environment_evidence["lock"]["status"] == "unverified"
    assert forbidden_environment_evidence["error"]["code"] == "repository_environment_failed"
    forbidden_probe = next(
        process
        for process in forbidden_environment_evidence["processes"]
        if process["role"] == "environment_probe"
    )
    assert len(forbidden_probe["stderr"]["text"].encode()) <= 65_536
    assert not forbidden_install.exists() or not any(forbidden_install.iterdir())
    assert _tracked_bytes(forbidden_repository) == forbidden_before
    assert all(check["processes"] == [] for check in forbidden_report["checks"])
