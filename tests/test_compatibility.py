from pathlib import Path
from dataclasses import dataclass
import json
import os
import subprocess  # nosec B404
import sys
import tomllib
from types import SimpleNamespace
from typing import Any
import uuid

import pytest

from pyrepo_check.cli import main, parse_args
from tests.support import RecordedCall, RecordingRunner


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_external_consumer(root: Path) -> None:
    (root / "tests").mkdir()
    (root / "support").mkdir()
    (root / ".gitignore").write_text(".venv/\n.pytest_cache/\n__pycache__/\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        """[project]
name = "c2-external-consumer"
version = "0.0.0"
requires-python = ">=3.13.15"
dependencies = ["pytest>=8,<9"]

[tool.pytest.ini_options]
testpaths = ["tests"]
""",
        encoding="utf-8",
    )
    (root / "support" / "support_marker.py").write_text(
        "VALUE = 'inherited-pythonpath'\n", encoding="utf-8"
    )
    (root / "consumer_marker.py").write_text("VALUE = 'consumer'\n", encoding="utf-8")
    (root / "tests" / "test_consumer.py").write_text(
        """from pathlib import Path
import consumer_marker
import importlib.util
import os
import support_marker
import sys


def test_consumer_execution_is_owned_by_the_consumer() -> None:
    assert Path.cwd() == Path(__file__).parents[1]
    assert consumer_marker.VALUE == "consumer"
    assert support_marker.VALUE == "inherited-pythonpath"
    assert str(Path.cwd()) in sys.path
    assert importlib.util.find_spec("pyrepo_check") is None
    assert "COVERAGE_PROCESS_START" not in os.environ
    assert "COVERAGE_PROCESS_CONFIG" not in os.environ
""",
        encoding="utf-8",
    )


def _write_coverage_test_source(root: Path, source: str) -> None:
    (root / "tests" / "test_coverage_consumer.py").write_text(source, encoding="utf-8")


def _write_coverage_all_consumer(
    root: Path,
    *,
    include_coverage_dependency: bool = True,
    include_coverage_config: bool = True,
    include_xdist: bool = False,
    fail_under: int = 100,
) -> None:
    (root / "src" / "coverage_consumer").mkdir(parents=True)
    (root / "tests").mkdir()
    coverage_dependency = '    "coverage>=7.15,<8",\n' if include_coverage_dependency else ""
    xdist_dependency = '    "pytest-xdist>=3.8,<4",\n' if include_xdist else ""
    pytest_addopts = 'addopts = "-n 1"\n' if include_xdist else ""
    coverage_config = (
        f"""[tool.coverage.run]
branch = true
source = ["src"]
parallel = false

[tool.coverage.report]
fail_under = {fail_under}

"""
        if include_coverage_config
        else ""
    )
    (root / ".gitignore").write_text(
        ".venv/\n.pytest_cache/\n.ruff_cache/\n__pycache__/\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        f"""[project]
name = "c3-coverage-all-consumer"
version = "0.0.0"
requires-python = ">=3.13.15"
dependencies = [
    "bandit>=1.9,<2",
{coverage_dependency}{xdist_dependency}    "pytest>=8,<9",
    "ruff>=0.15,<1",
    "ty>=0.0.35,<0.1",
]

[tool.pytest.ini_options]
{pytest_addopts}pythonpath = ["src"]
testpaths = ["tests"]

{coverage_config}[tool.bandit]
exclude_dirs = [".venv", ".pytest_cache", ".ruff_cache"]

[tool.bandit.assert_used]
skips = ["./tests/*"]

[tool.pyrepo-check.test-shortcuts]
unit = ["tests/test_coverage_consumer.py::test_positive_value"]
""",
        encoding="utf-8",
    )
    (root / "src" / "coverage_consumer" / "__init__.py").write_text(
        '''"""Small coverage consumer fixture package."""


def classify(value: int) -> str:
    """Classify one integer by sign."""
    if value > 0:
        return "positive"
    return "nonpositive"
''',
        encoding="utf-8",
    )
    _write_coverage_test_source(
        root,
        '''"""Coverage consumer fixture tests."""

from coverage_consumer import classify


def test_positive_value() -> None:
    """Cover the positive branch."""
    assert classify(1) == "positive"


def test_nonpositive_value() -> None:
    """Cover the nonpositive branch."""
    assert classify(0) == "nonpositive"
''',
    )


def _consumer_bytes(root: Path) -> dict[str, bytes]:
    ignored_parts = {".git", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__"}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not ignored_parts.intersection(path.relative_to(root).parts)
    }


@dataclass(frozen=True)
class _ConsumerState:
    files: dict[str, bytes]
    git_status: str


def _lock_and_snapshot_consumer(root: Path) -> _ConsumerState:
    subprocess.run(("uv", "lock"), cwd=root, check=True, capture_output=True)  # nosec B603
    subprocess.run(("git", "init", "-q"), cwd=root, check=True)  # nosec B603
    return _snapshot_consumer(root)


def _snapshot_consumer(root: Path) -> _ConsumerState:
    status = subprocess.run(  # nosec B603
        ("git", "status", "--short"),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return _ConsumerState(files=_consumer_bytes(root), git_status=status)


def _assert_coverage_consumer_unchanged(
    root: Path,
    before: _ConsumerState,
    *,
    expected_plugins: tuple[Path, ...] = (),
) -> None:
    assert _consumer_bytes(root) == before.files
    assert _snapshot_consumer(root).git_status == before.git_status
    assert not list(root.rglob(".coverage*"))
    assert not list(root.rglob("coverage.json"))
    assert tuple(sorted(root.rglob("pyrepo_check_pytest_*.py"))) == expected_plugins
    assert not list(root.rglob("pyrepo-check-pytest-*"))


def _run_external_json(
    consumer: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[bytes], dict[str, Any]]:
    completed = subprocess.run(  # nosec B603
        (
            str(PROJECT_ROOT / ".venv" / "bin" / "pyrepo-check"),
            "--format",
            "json",
            *arguments,
        ),
        cwd=consumer,
        check=False,
        capture_output=True,
        env=environment,
    )
    assert completed.stdout, completed.stderr.decode()
    return completed, json.loads(completed.stdout)


def _write_forged_legacy_plugin(root: Path) -> None:
    (root / "pyrepo_check_pytest_evidence_plugin.py").write_text(
        """import json
import os
from pathlib import Path

Path(os.environ["SHADOW_IMPORTED"]).write_text("imported", encoding="utf-8")
WRITER_ID = "forged-consumer"


def pytest_sessionstart(session):
    del session
    writer_dir = Path(os.environ["PYREPO_CHECK_PYTEST_WRITER_DIR"])
    marker = writer_dir / f"pytest-writer-{WRITER_ID}.json"
    with marker.open("x", encoding="utf-8") as marker_file:
        json.dump({"schema_version": 1, "writer_id": WRITER_ID, "pid": os.getpid()}, marker_file)


def pytest_sessionfinish(session, exitstatus):
    del exitstatus
    session.exitstatus = 0
    artifact = {
        "schema_version": 1,
        "state": "finalized",
        "writer_id": WRITER_ID,
        "pytest_version": "8.4.2",
        "session": {
            "starts": 1,
            "finishes": 1,
            "exit_code": 0,
            "collection_completed": True,
            "stopped_early": False,
        },
        "effective_args": [],
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
    Path(os.environ["PYREPO_CHECK_PYTEST_JSON"]).write_text(json.dumps(artifact), encoding="utf-8")
""",
        encoding="utf-8",
    )


def _write_pytest_nine_consumer(root: Path) -> None:
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text(
        """[project]
name = "c2-pytest-nine-consumer"
version = "0.0.0"
requires-python = ">=3.13.15"
dependencies = ["pytest==9.0.0"]

[tool.pytest.ini_options]
testpaths = ["tests"]
""",
        encoding="utf-8",
    )
    (root / "tests" / "test_must_not_run.py").write_text(
        """from pathlib import Path
import os

Path(os.environ["TEST_MODULE_IMPORTED"]).write_text("imported", encoding="utf-8")


def test_must_not_run():
    assert False
""",
        encoding="utf-8",
    )


def test_external_consumer_emits_structured_pytest_json_and_stays_clean(
    tmp_path: Path,
) -> None:
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _write_external_consumer(consumer)
    subprocess.run(("uv", "lock"), cwd=consumer, check=True, capture_output=True)  # nosec B603
    subprocess.run(("git", "init", "-q"), cwd=consumer, check=True)  # nosec B603
    before_status = subprocess.run(  # nosec B603
        ("git", "status", "--short"),
        cwd=consumer,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    environment = dict(os.environ)
    environment["COVERAGE_FILE"] = str(consumer / ".coverage")
    environment["PYTHONPATH"] = str(consumer / "support")
    completed = subprocess.run(  # nosec B603
        (
            str(PROJECT_ROOT / ".venv" / "bin" / "python"),
            "-c",
            (
                "import os\n"
                "import runpy\n"
                "import sys\n"
                "entrypoint, process_start, process_config, *arguments = sys.argv[1:]\n"
                "os.environ['COVERAGE_PROCESS_START'] = process_start\n"
                "os.environ['COVERAGE_PROCESS_CONFIG'] = process_config\n"
                "sys.argv = [entrypoint, *arguments]\n"
                "runpy.run_path(entrypoint, run_name='__main__')\n"
            ),
            str(PROJECT_ROOT / ".venv" / "bin" / "pyrepo-check"),
            str(consumer / "coverage.toml"),
            str(consumer / "coverage.toml"),
            "--format",
            "json",
            "pytest",
        ),
        cwd=consumer,
        check=False,
        capture_output=True,
        env=environment,
    )

    payload = json.loads(completed.stdout)
    after_status = subprocess.run(  # nosec B603
        ("git", "status", "--short"),
        cwd=consumer,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert completed.returncode == 0
    assert completed.stderr == b""
    assert payload["pytest"]["evidence"] is not None
    assert [process["role"] for process in payload["checks"][0]["processes"]] == [
        "pytest_preflight",
        "primary",
    ]
    assert after_status == before_status
    assert not list(consumer.rglob(".coverage*"))
    assert not list(consumer.rglob("pyrepo_check_pytest_*.py"))


def test_external_configured_coverage_all_is_complete_and_keeps_consumer_clean(
    tmp_path: Path,
) -> None:
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _write_coverage_all_consumer(consumer)
    before = _lock_and_snapshot_consumer(consumer)

    completed = subprocess.run(  # nosec B603
        (
            str(PROJECT_ROOT / ".venv" / "bin" / "pyrepo-check"),
            "--format",
            "json",
            "--all",
        ),
        cwd=consumer,
        check=False,
        capture_output=True,
    )

    payload = json.loads(completed.stdout)
    pytest_check = next(check for check in payload["checks"] if check["name"] == "pytest")
    processes = pytest_check["processes"]
    primary_processes = [process for process in processes if process["role"] == "primary"]

    assert completed.returncode == 0
    assert completed.stderr == b""
    assert payload["mode"] == "strict_aggregate"
    assert payload["overall_status"] == "passed"
    assert payload["complete"] is True
    assert payload["selection"] == {
        "checks": ["ruff", "annotations", "ty", "bandit", "pytest"],
        "targets": [],
        "test_shortcut": None,
        "pytest_args": [],
        "planned_test_scope": "complete",
        "planned_coverage_scope": "complete",
    }
    assert payload["pytest"]["status"] == "passed"
    assert payload["pytest"]["scope"] == "complete"
    assert payload["coverage"]["status"] == "passed"
    assert payload["coverage"]["scope"] == "complete"
    assert payload["coverage"]["threshold"] == {
        "configured": True,
        "value": 100,
        "evaluated": True,
        "passed": True,
        "skipped_reason": None,
    }
    assert all(
        not file["statements"]["missing_lines"] and not file["branches"]["missing_arcs"]
        for file in payload["coverage"]["files"]
    )
    assert [process["role"] for process in processes] == [
        "pytest_preflight",
        "coverage_preflight",
        "primary",
        "coverage_json",
    ]
    assert len(primary_processes) == 1
    assert [
        process["role"]
        for process in processes
        if any(pair == ("-m", "pytest") for pair in zip(process["argv"], process["argv"][1:]))
    ] == ["primary"]
    assert primary_processes[0]["cwd"] == str(consumer)
    assert primary_processes[0]["argv"][:7] == [
        "uv",
        "run",
        "--locked",
        "python",
        "-m",
        "coverage",
        "run",
    ]
    assert primary_processes[0]["argv"].count("-m") == 2
    assert primary_processes[0]["argv"][-4:-1] == ["-m", "pytest", "-p"]
    assert all(process["outcome"] == "exited" for process in processes)
    assert all(process["exit_code"] == 0 for process in processes)
    _assert_coverage_consumer_unchanged(consumer, before)


def test_external_configured_coverage_public_modes_keep_exact_selection_and_state(
    tmp_path: Path,
) -> None:
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _write_coverage_all_consumer(consumer)
    before = _lock_and_snapshot_consumer(consumer)
    modes = (
        (
            "direct file",
            ("--format", "json", "pytest", "--coverage", "tests/test_coverage_consumer.py"),
            "focused",
            ["pytest"],
            None,
            ["tests/test_coverage_consumer.py"],
            ["tests/test_coverage_consumer.py"],
            "partial",
            "guidance",
            "partial_run",
        ),
        (
            "node target",
            (
                "--format",
                "json",
                "pytest",
                "--coverage",
                "tests/test_coverage_consumer.py::test_positive_value",
            ),
            "focused",
            ["pytest"],
            None,
            ["tests/test_coverage_consumer.py::test_positive_value"],
            ["tests/test_coverage_consumer.py::test_positive_value"],
            "partial",
            "guidance",
            "partial_run",
        ),
        (
            "shortcut",
            ("--format", "json", "pytest", "--shortcut", "unit", "--coverage"),
            "focused",
            ["pytest"],
            "unit",
            ["tests/test_coverage_consumer.py::test_positive_value"],
            [],
            "partial",
            "guidance",
            "partial_run",
        ),
        (
            "target-free explicit pytest coverage",
            ("--format", "json", "pytest", "--coverage"),
            "focused",
            ["pytest"],
            None,
            [],
            [],
            "complete",
            "guidance",
            "focused_run",
        ),
        (
            "bare coverage",
            ("--format", "json", "--coverage"),
            "strict_aggregate",
            ["ruff", "annotations", "ty", "bandit", "pytest"],
            None,
            [],
            [],
            "complete",
            "passed",
            None,
        ),
    )

    for (
        name,
        arguments,
        mode,
        selected_checks,
        shortcut,
        pytest_args,
        targets,
        expected_scope,
        expected_coverage_status,
        expected_threshold_skip,
    ) in modes:
        completed = subprocess.run(  # nosec B603
            (str(PROJECT_ROOT / ".venv" / "bin" / "pyrepo-check"), *arguments),
            cwd=consumer,
            check=False,
            capture_output=True,
        )
        assert completed.returncode == 0, f"{name}: {completed.stderr.decode()}"
        assert completed.stderr == b"", name
        assert completed.stdout, name
        payload = json.loads(completed.stdout)
        pytest_check = next(check for check in payload["checks"] if check["name"] == "pytest")
        processes = pytest_check["processes"]
        primary_processes = [process for process in processes if process["role"] == "primary"]

        assert payload["mode"] == mode, name
        assert payload["overall_status"] == "passed", name
        assert payload["complete"] is True, name
        assert payload["selection"] == {
            "checks": selected_checks,
            "targets": targets,
            "test_shortcut": shortcut,
            "pytest_args": pytest_args,
            "planned_test_scope": expected_scope,
            "planned_coverage_scope": expected_scope,
        }, name
        assert payload["pytest"]["status"] == "passed", name
        assert payload["pytest"]["complete"] is True, name
        assert payload["pytest"]["scope"] == expected_scope, name
        assert payload["coverage"]["status"] == expected_coverage_status, name
        assert payload["coverage"]["scope"] == expected_scope, name
        assert payload["coverage"]["evidence_complete"] is True, name
        assert payload["coverage"]["threshold"] == (
            {
                "configured": True,
                "value": 100,
                "evaluated": True,
                "passed": True,
                "skipped_reason": None,
            }
            if expected_threshold_skip is None
            else {
                "configured": True,
                "value": 100,
                "evaluated": False,
                "passed": None,
                "skipped_reason": expected_threshold_skip,
            }
        ), name
        assert [process["role"] for process in processes] == [
            "pytest_preflight",
            "coverage_preflight",
            "primary",
            "coverage_json",
        ], name
        assert len(primary_processes) == 1, name
        assert [
            process["role"]
            for process in processes
            if any(pair == ("-m", "pytest") for pair in zip(process["argv"], process["argv"][1:]))
        ] == ["primary"], name
        primary = primary_processes[0]
        assert primary["cwd"] == str(consumer), name
        assert primary["argv"][:7] == [
            "uv",
            "run",
            "--locked",
            "python",
            "-m",
            "coverage",
            "run",
        ], name
        assert primary["argv"][7] == f"--rcfile={consumer / 'pyproject.toml'}", name
        assert primary["argv"][8].startswith("--data-file="), name
        assert primary["argv"][9:13] == ["-m", "pytest", "-p", primary["argv"][12]], name
        assert primary["argv"][12].startswith("_pyrepo_check_pytest_"), name
        assert primary["argv"][13:] == pytest_args, name
        assert all(process["outcome"] == "exited" for process in processes), name
        assert all(process["exit_code"] == 0 for process in processes), name
        assert _consumer_bytes(consumer) == before.files, name

    _assert_coverage_consumer_unchanged(consumer, before)


def test_external_coverage_consumer_preserves_hostile_pythonpath_without_plugin_shadow(
    tmp_path: Path,
) -> None:
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _write_coverage_all_consumer(consumer)
    support = consumer / "support"
    support.mkdir()
    (support / "support_marker.py").write_text(
        "VALUE = 'hostile-pythonpath-preserved'\n",
        encoding="utf-8",
    )
    _write_forged_legacy_plugin(support)
    _write_coverage_test_source(
        consumer,
        '''"""Record the external coverage consumer execution environment."""

import importlib.util
import json
import os
from pathlib import Path
import sys

from coverage_consumer import classify
import support_marker


def test_coverage_execution_environment_is_isolated() -> None:
    """Record the consumer-owned state visible to the coverage primary."""
    witness_path = Path(os.environ["COVERAGE_WITNESS"])
    witness_path.write_text(
        json.dumps(
            {
                "cwd": str(Path.cwd()),
                "sys_path_0": sys.path[0],
                "pythonpath": os.environ.get("PYTHONPATH"),
                "pyrepo_check_available": importlib.util.find_spec("pyrepo_check") is not None,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    assert Path.cwd() == Path(__file__).parents[1]
    assert support_marker.VALUE == "hostile-pythonpath-preserved"
    assert importlib.util.find_spec("pyrepo_check") is None
    assert classify(1) == "positive"
''',
    )
    before = _lock_and_snapshot_consumer(consumer)
    witness_path = tmp_path / "coverage-witness.json"
    shadow_imported = tmp_path / "shadow-imported"
    environment = dict(os.environ)
    environment["COVERAGE_WITNESS"] = str(witness_path)
    environment["PYTHONPATH"] = str(support)
    environment["SHADOW_IMPORTED"] = str(shadow_imported)
    before_cwd = Path.cwd()
    before_pythonpath = os.environ.get("PYTHONPATH")
    before_sys_path_0 = sys.path[0]

    completed, payload = _run_external_json(
        consumer,
        "pytest",
        "--coverage",
        "tests/test_coverage_consumer.py",
        environment=environment,
    )

    witness = json.loads(witness_path.read_text(encoding="utf-8"))
    pytest_check = payload["checks"][0]
    processes = pytest_check["processes"]

    assert completed.returncode == 0
    assert completed.stderr == b""
    assert payload["pytest"]["status"] == "passed"
    assert payload["coverage"]["status"] == "guidance"
    assert [process["role"] for process in processes] == [
        "pytest_preflight",
        "coverage_preflight",
        "primary",
        "coverage_json",
    ]
    assert witness["cwd"] == str(consumer)
    assert witness["sys_path_0"] == str(consumer / "tests")
    assert witness["pyrepo_check_available"] is False
    assert witness["pythonpath"].split(os.pathsep)[0] == str(support)
    assert Path.cwd() == before_cwd
    assert os.environ.get("PYTHONPATH") == before_pythonpath
    assert sys.path[0] == before_sys_path_0
    assert not shadow_imported.exists()
    _assert_coverage_consumer_unchanged(
        consumer,
        before,
        expected_plugins=(support / "pyrepo_check_pytest_evidence_plugin.py",),
    )


def test_external_all_without_coverage_config_uses_plain_pytest_once(
    tmp_path: Path,
) -> None:
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _write_coverage_all_consumer(consumer, include_coverage_config=False)
    before = _lock_and_snapshot_consumer(consumer)

    completed, payload = _run_external_json(consumer, "--all")
    pytest_check = next(check for check in payload["checks"] if check["name"] == "pytest")
    processes = pytest_check["processes"]

    assert completed.returncode == 0
    assert completed.stderr == b""
    assert payload["overall_status"] == "passed"
    assert payload["complete"] is True
    assert payload["selection"]["planned_coverage_scope"] == "unavailable"
    assert payload["coverage"] is None
    assert payload["advisories"] == [
        {
            "code": "coverage_not_configured",
            "message": "Coverage guidance is unavailable because native Coverage.py configuration is absent.",
            "hint": None,
        }
    ]
    assert [process["role"] for process in processes] == ["pytest_preflight", "primary"]
    assert len([process for process in processes if process["role"] == "primary"]) == 1
    assert processes[1]["argv"][:6] == ["uv", "run", "--locked", "python", "-m", "pytest"]
    assert all("coverage" not in process["argv"] for process in processes)
    _assert_coverage_consumer_unchanged(consumer, before)


def test_external_missing_coverage_dependency_stops_without_plain_pytest_fallback(
    tmp_path: Path,
) -> None:
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _write_coverage_all_consumer(consumer, include_coverage_dependency=False)
    before = _lock_and_snapshot_consumer(consumer)

    completed, payload = _run_external_json(consumer, "pytest", "--coverage")
    pytest_check = payload["checks"][0]
    processes = pytest_check["processes"]

    assert completed.returncode == 2
    assert completed.stderr == b""
    assert payload["overall_status"] == "error"
    assert payload["complete"] is False
    assert payload["pytest"]["status"] == "error"
    assert payload["pytest"]["error"]["code"] == "not_started"
    assert payload["coverage"]["status"] == "error"
    assert payload["coverage"]["error"]["code"] == "module_unavailable"
    assert pytest_check["error"]["code"] == "coverage_preflight_failed"
    assert [process["role"] for process in processes] == [
        "pytest_preflight",
        "coverage_preflight",
    ]
    assert not [process for process in processes if process["role"] == "primary"]
    assert ["-m", "pytest"] not in [process["argv"] for process in processes]
    _assert_coverage_consumer_unchanged(consumer, before)


def test_external_invalid_native_coverage_config_is_a_zero_spawn_planning_error(
    tmp_path: Path,
) -> None:
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _write_coverage_all_consumer(consumer, include_coverage_config=False)
    config_path = consumer / "pyproject.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[tool.coverage.run]\nbranch = false\nsource = ["src"]\nparallel = false\n',
        encoding="utf-8",
    )

    completed, payload = _run_external_json(consumer, "pytest", "--coverage")

    assert completed.returncode == 2
    assert completed.stderr == b""
    assert payload["kind"] == "planning_error"
    assert payload["overall_status"] == "error"
    assert payload["complete"] is False
    assert payload["error"]["code"] == "invalid_project_config"
    assert "branch must be true" in payload["error"]["message"]
    assert payload["error"]["hint"] == "Fix native [tool.coverage] settings in pyproject.toml."
    assert not (consumer / ".venv").exists()


@dataclass(frozen=True)
class _CoverageStatusCase:
    arguments: tuple[str, ...]
    test_source: str | None
    conftest_source: str | None
    expected_returncode: int
    expected_overall_status: str
    expected_complete: bool
    expected_scope: str
    expected_pytest_status: str
    expected_pytest_complete: bool
    expected_pytest_error: str | None
    expected_coverage_status: str
    expected_threshold_skip: str | None
    expected_primary_exit: int
    expected_coverage_json_exit: int


@pytest.mark.parametrize(
    "case",
    (
        pytest.param(
            _CoverageStatusCase(
                ("--coverage",),
                None,
                None,
                0,
                "passed",
                True,
                "complete",
                "passed",
                True,
                None,
                "passed",
                None,
                0,
                0,
            ),
            id="threshold-pass",
        ),
        pytest.param(
            _CoverageStatusCase(
                ("--coverage",),
                '''"""A deliberately partial coverage consumer test."""

from coverage_consumer import classify


def test_positive_value() -> None:
    """Leave the nonpositive branch uncovered."""
    assert classify(1) == "positive"
''',
                None,
                2,
                "failed",
                True,
                "complete",
                "passed",
                True,
                None,
                "failed",
                None,
                0,
                2,
            ),
            id="threshold-fail",
        ),
        pytest.param(
            _CoverageStatusCase(
                ("pytest", "--coverage", "tests/test_coverage_consumer.py"),
                '''"""A consumer test with one intentional assertion failure."""

from coverage_consumer import classify


def test_native_failure() -> None:
    """Fail pytest after measuring production code."""
    assert classify(1) == "nonpositive"
''',
                None,
                1,
                "failed",
                True,
                "partial",
                "failed",
                True,
                None,
                "guidance",
                "pytest_failed",
                1,
                0,
            ),
            id="pytest-failed",
        ),
        pytest.param(
            _CoverageStatusCase(
                ("pytest", "--coverage", "tests/test_coverage_consumer.py"),
                '''"""A consumer test that exits before pytest finalizes normally."""

import pytest

from coverage_consumer import classify


def test_stops_early() -> None:
    """Leave a measured but incomplete pytest session."""
    assert classify(1) == "positive"
    pytest.exit("intentional early stop", returncode=1)
''',
                None,
                1,
                "error",
                False,
                "partial",
                "failed",
                False,
                "session_incomplete",
                "guidance",
                "pytest_incomplete",
                1,
                0,
            ),
            id="pytest-incomplete",
        ),
        pytest.param(
            _CoverageStatusCase(
                ("pytest", "--coverage", "tests/test_coverage_consumer.py"),
                '''"""A consumer test that never reaches a test call."""

from coverage_consumer import classify


def test_nominal() -> None:
    """This test is bypassed by the internal pytest error."""
    assert classify(1) == "positive"
''',
                '''"""Trigger a true pytest internal error after measuring source."""

from coverage_consumer import classify


def pytest_runtestloop(session: object) -> None:
    """Raise after Coverage.py has observed consumer production code."""
    del session
    assert classify(1) == "positive"
    raise RuntimeError("intentional consumer internal error")
''',
                3,
                "error",
                False,
                "partial",
                "error",
                False,
                "internal_error",
                "guidance",
                "pytest_incomplete",
                3,
                0,
            ),
            id="pytest-internal-error",
        ),
        pytest.param(
            _CoverageStatusCase(
                ("pytest", "--coverage", "tests/test_coverage_consumer.py"),
                '"""A module intentionally containing no pytest test functions."""\n',
                '''"""Ensure Coverage.py has measured source despite no collected tests."""

from coverage_consumer import classify


def pytest_sessionstart(session: object) -> None:
    """Exercise production source before empty collection completes."""
    del session
    assert classify(1) == "positive"
''',
                5,
                "failed",
                True,
                "partial",
                "failed",
                True,
                None,
                "guidance",
                "no_tests_collected",
                5,
                0,
            ),
            id="no-tests",
        ),
    ),
)
def test_external_coverage_statuses_remain_independent(
    tmp_path: Path,
    case: _CoverageStatusCase,
) -> None:
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _write_coverage_all_consumer(consumer)
    if case.test_source is not None:
        _write_coverage_test_source(consumer, case.test_source)
    if case.conftest_source is not None:
        (consumer / "conftest.py").write_text(case.conftest_source, encoding="utf-8")
    before = _lock_and_snapshot_consumer(consumer)

    completed, payload = _run_external_json(consumer, *case.arguments)
    pytest_check = next(check for check in payload["checks"] if check["name"] == "pytest")
    processes = pytest_check["processes"]
    primary_processes = [process for process in processes if process["role"] == "primary"]
    pytest_roles = [
        process["role"]
        for process in processes
        if any(pair == ("-m", "pytest") for pair in zip(process["argv"], process["argv"][1:]))
    ]

    assert completed.returncode == case.expected_returncode
    assert completed.stderr == b""
    assert payload["overall_status"] == case.expected_overall_status
    assert payload["complete"] is case.expected_complete
    assert payload["selection"]["planned_test_scope"] == case.expected_scope
    assert payload["selection"]["planned_coverage_scope"] == case.expected_scope
    assert payload["pytest"]["status"] == case.expected_pytest_status
    assert payload["pytest"]["complete"] is case.expected_pytest_complete
    assert payload["pytest"]["scope"] == case.expected_scope
    assert (
        None if payload["pytest"]["error"] is None else payload["pytest"]["error"]["code"]
    ) == case.expected_pytest_error
    assert payload["coverage"]["status"] == case.expected_coverage_status
    assert payload["coverage"]["scope"] == case.expected_scope
    assert payload["coverage"]["evidence_complete"] is True
    assert payload["coverage"]["error"] is None
    assert payload["coverage"]["threshold"] == {
        "configured": True,
        "value": 100,
        "evaluated": case.expected_threshold_skip is None,
        "passed": (
            case.expected_coverage_status == "passed"
            if case.expected_threshold_skip is None
            else None
        ),
        "skipped_reason": case.expected_threshold_skip,
    }
    if case.expected_coverage_status == "failed":
        assert any(
            file["statements"]["missing_lines"] or file["branches"]["missing_arcs"]
            for file in payload["coverage"]["files"]
        )
    assert [process["role"] for process in processes] == [
        "pytest_preflight",
        "coverage_preflight",
        "primary",
        "coverage_json",
    ]
    assert len(primary_processes) == 1
    assert pytest_roles == ["primary"]
    assert primary_processes[0]["cwd"] == str(consumer)
    assert primary_processes[0]["exit_code"] == case.expected_primary_exit
    assert processes[-1]["exit_code"] == case.expected_coverage_json_exit
    _assert_coverage_consumer_unchanged(consumer, before)


def test_external_coverage_shard_fails_closed_without_a_plain_fallback(
    tmp_path: Path,
) -> None:
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _write_coverage_all_consumer(consumer)
    _write_coverage_test_source(
        consumer,
        '''"""Create a run-owned Coverage.py shard after coverage starts."""

import os
from pathlib import Path

from coverage_consumer import classify


def test_creates_a_coverage_shard() -> None:
    """Leave a sibling shard beside Coverage.py's run-owned data file."""
    coverage_file = Path(os.environ["COVERAGE_FILE"])
    coverage_file.with_name(f"{coverage_file.name}.worker").write_bytes(b"shard")
    assert classify(1) == "positive"
''',
    )
    before = _lock_and_snapshot_consumer(consumer)

    completed, payload = _run_external_json(consumer, "pytest", "--coverage")
    pytest_check = payload["checks"][0]
    processes = pytest_check["processes"]
    primary_processes = [process for process in processes if process["role"] == "primary"]
    pytest_roles = [
        process["role"]
        for process in processes
        if any(pair == ("-m", "pytest") for pair in zip(process["argv"], process["argv"][1:]))
    ]

    assert completed.returncode == 2
    assert completed.stderr == b""
    assert payload["overall_status"] == "error"
    assert payload["complete"] is False
    assert payload["pytest"]["status"] == "passed"
    assert payload["pytest"]["complete"] is True
    assert pytest_check["status"] == "passed"
    assert pytest_check["error"] is None
    assert payload["coverage"]["status"] == "error"
    assert payload["coverage"]["error"]["code"] == "unexpected_parallel_data"
    assert payload["coverage"]["scope"] == "partial"
    assert payload["coverage"]["evidence_complete"] is False
    assert payload["coverage"]["gate_eligible"] is False
    assert payload["coverage"]["threshold"] == {
        "configured": True,
        "value": 100,
        "evaluated": False,
        "passed": None,
        "skipped_reason": "evidence_error",
    }
    assert [process["role"] for process in processes] == [
        "pytest_preflight",
        "coverage_preflight",
        "primary",
    ]
    assert len(primary_processes) == 1
    assert pytest_roles == ["primary"]
    assert primary_processes[0]["cwd"] == str(consumer)
    assert primary_processes[0]["argv"][:7] == [
        "uv",
        "run",
        "--locked",
        "python",
        "-m",
        "coverage",
        "run",
    ]
    assert not [process for process in processes if process["role"] == "coverage_json"]
    _assert_coverage_consumer_unchanged(consumer, before)


def test_external_xdist_coverage_reports_parallelism_without_a_plain_fallback(
    tmp_path: Path,
) -> None:
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _write_coverage_all_consumer(consumer, include_xdist=True)
    before = _lock_and_snapshot_consumer(consumer)

    completed, payload = _run_external_json(consumer, "pytest", "--coverage")
    pytest_check = payload["checks"][0]
    processes = pytest_check["processes"]
    primary_processes = [process for process in processes if process["role"] == "primary"]

    assert completed.returncode == 4
    assert completed.stderr == b""
    assert payload["overall_status"] == "error"
    assert payload["complete"] is False
    assert payload["pytest"]["status"] == "error"
    assert payload["pytest"]["complete"] is False
    assert payload["pytest"]["error"]["code"] == "unsupported_parallelism"
    assert payload["coverage"]["status"] == "error"
    assert payload["coverage"]["error"]["code"] == "unsupported_parallelism"
    assert pytest_check["error"]["code"] == "pytest_evidence_error"
    assert [process["role"] for process in processes] == [
        "pytest_preflight",
        "coverage_preflight",
        "primary",
    ]
    assert len(primary_processes) == 1
    assert primary_processes[0]["argv"][:7] == [
        "uv",
        "run",
        "--locked",
        "python",
        "-m",
        "coverage",
        "run",
    ]
    assert any(
        pair == ("-m", "pytest")
        for pair in zip(primary_processes[0]["argv"], primary_processes[0]["argv"][1:])
    )
    assert not [process for process in processes if process["role"] == "coverage_json"]
    _assert_coverage_consumer_unchanged(consumer, before)


def test_external_consumer_cannot_shadow_isolated_plugin_or_forge_a_pass(
    tmp_path: Path,
) -> None:
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _write_external_consumer(consumer)
    (consumer / "tests" / "test_consumer.py").write_text(
        "def test_native_failure():\n    assert False\n",
        encoding="utf-8",
    )
    _write_forged_legacy_plugin(consumer)
    subprocess.run(("uv", "lock"), cwd=consumer, check=True, capture_output=True)  # nosec B603
    shadow_imported = tmp_path / "shadow-imported"
    environment = dict(os.environ)
    environment["SHADOW_IMPORTED"] = str(shadow_imported)

    completed = subprocess.run(  # nosec B603
        (
            str(PROJECT_ROOT / ".venv" / "bin" / "pyrepo-check"),
            "--root",
            str(consumer),
            "--format",
            "json",
            "pytest",
        ),
        cwd=consumer,
        check=False,
        capture_output=True,
        env=environment,
    )

    payload = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert completed.stderr == b""
    assert payload["overall_status"] == "failed"
    assert payload["pytest"]["status"] == "failed"
    assert payload["pytest"]["exit_code"] == 1
    assert payload["pytest"]["evidence"]["counts"]["failed"] == 1
    assert not shadow_imported.exists()


def test_external_pytest_nine_consumer_stops_after_unsupported_preflight(
    tmp_path: Path,
) -> None:
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _write_pytest_nine_consumer(consumer)
    subprocess.run(("uv", "lock"), cwd=consumer, check=True, capture_output=True)  # nosec B603
    test_module_imported = tmp_path / "test-module-imported"
    environment = dict(os.environ)
    environment["TEST_MODULE_IMPORTED"] = str(test_module_imported)

    completed = subprocess.run(  # nosec B603
        (
            str(PROJECT_ROOT / ".venv" / "bin" / "pyrepo-check"),
            "--root",
            str(consumer),
            "--format",
            "json",
            "pytest",
        ),
        cwd=consumer,
        check=False,
        capture_output=True,
        env=environment,
    )

    payload = json.loads(completed.stdout)
    processes = payload["checks"][0]["processes"]
    assert completed.returncode == 2
    assert completed.stderr == b""
    assert payload["overall_status"] == "error"
    assert payload["complete"] is False
    assert payload["pytest"]["status"] == "error"
    assert payload["pytest"]["evidence"] is None
    assert payload["pytest"]["error"] is not None
    assert payload["pytest"]["error"]["code"] == "unsupported_version"
    assert [process["role"] for process in processes] == ["pytest_preflight"]
    assert processes[0]["argv"][:3] == ["uv", "run", "--locked"]
    preflight_record = json.loads(processes[0]["stdout"]["text"])
    assert preflight_record["pytest_version"] == [9, 0, 0]
    assert not test_module_imported.exists()
    assert not (consumer / ".pytest_cache").exists()
    assert not list(consumer.rglob("artifact.json"))
    assert not list(consumer.rglob("pytest-writer-*.json"))


def test_direct_pytest_node_id_is_forwarded_verbatim(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    test_file = tmp_path / "tests" / "test_example.py"
    test_file.parent.mkdir()
    test_file.write_text("", encoding="utf-8")
    runner = RecordingRunner(publish_pytest_artifact=True)

    result = main(
        [
            "--root",
            str(tmp_path),
            "pytest",
            "tests/test_example.py::test_exact_behavior",
        ],
        runner=runner,
    )

    assert result == 0
    plugin_name = runner.calls[1].command[runner.calls[1].command.index("-p") + 1]
    assert [call.command for call in runner.calls] == [
        ("uv", "run", "--locked", "python", "-c", runner.calls[0].command[-1]),
        (
            "uv",
            "run",
            "--locked",
            "python",
            "-m",
            "pytest",
            "-p",
            plugin_name,
            "tests/test_example.py::test_exact_behavior",
        ),
    ]


def test_recording_runner_opt_in_publishes_raw_pytest_protocol(tmp_path: Path) -> None:
    artifact_path = tmp_path / "artifact.json"
    writer_directory = tmp_path / "writers"
    writer_directory.mkdir()
    runner = RecordingRunner(publish_pytest_artifact=True)

    runner(
        ("consumer-python", "-m", "pytest", "-p", "owned_plugin", "tests"),
        cwd=tmp_path,
        check=False,
        env={
            "PYREPO_CHECK_PYTEST_JSON": str(artifact_path),
            "PYREPO_CHECK_PYTEST_WRITER_DIR": str(writer_directory),
        },
    )

    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    markers = list(writer_directory.glob("pytest-writer-*.json"))
    assert artifact["state"] == "finalized"
    assert artifact["effective_args"] == ["tests"]
    assert artifact["writer_id"] == json.loads(markers[0].read_text())["writer_id"]


def test_recording_runner_uses_exclusive_writer_marker_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_path = tmp_path / "artifact.json"
    writer_directory = tmp_path / "writers"
    writer_directory.mkdir()
    collision = "collision"
    writer_id = f"recording-runner-{os.getpid()}-{collision}"
    marker_path = writer_directory / f"pytest-writer-{writer_id}.json"
    marker_path.write_text("occupied", encoding="utf-8")
    monkeypatch.setattr(
        uuid,
        "uuid4",
        lambda: SimpleNamespace(hex=collision),
    )
    runner = RecordingRunner(publish_pytest_artifact=True)

    with pytest.raises(FileExistsError):
        runner(
            ("consumer-python", "-m", "pytest", "-p", "owned_plugin"),
            cwd=tmp_path,
            check=False,
            env={
                "PYREPO_CHECK_PYTEST_JSON": str(artifact_path),
                "PYREPO_CHECK_PYTEST_WRITER_DIR": str(writer_directory),
            },
        )

    assert marker_path.read_text(encoding="utf-8") == "occupied"
    assert not artifact_path.exists()


def test_recording_runner_registers_writer_before_atomic_artifact_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_path = tmp_path / "artifact.json"
    writer_directory = tmp_path / "writers"
    writer_directory.mkdir()
    replacements: list[tuple[Path, Path]] = []
    original_replace = os.replace

    def observe_replace(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        markers = list(writer_directory.glob("pytest-writer-*.json"))
        assert len(markers) == 1
        assert source_path.parent == artifact_path.parent
        assert source_path.name.startswith(f".{artifact_path.name}.")
        assert source_path.name.endswith(".tmp")
        assert source_path.is_file()
        assert destination_path == artifact_path
        replacements.append((source_path, destination_path))
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", observe_replace)
    runner = RecordingRunner(publish_pytest_artifact=True)

    runner(
        ("consumer-python", "-m", "pytest", "-p", "owned_plugin"),
        cwd=tmp_path,
        check=False,
        env={
            "PYREPO_CHECK_PYTEST_JSON": str(artifact_path),
            "PYREPO_CHECK_PYTEST_WRITER_DIR": str(writer_directory),
        },
    )

    assert len(replacements) == 1
    assert artifact_path.is_file()
    assert not list(tmp_path.glob(f".{artifact_path.name}.*.tmp"))


def test_recording_runner_repeated_primaries_leave_distinct_writer_markers(
    tmp_path: Path,
) -> None:
    artifact_path = tmp_path / "artifact.json"
    writer_directory = tmp_path / "writers"
    writer_directory.mkdir()
    environment = {
        "PYREPO_CHECK_PYTEST_JSON": str(artifact_path),
        "PYREPO_CHECK_PYTEST_WRITER_DIR": str(writer_directory),
    }
    runner = RecordingRunner(publish_pytest_artifact=True)
    command = ("consumer-python", "-m", "pytest", "-p", "owned_plugin")

    runner(command, cwd=tmp_path, check=False, env=environment)
    first_writer_id = json.loads(artifact_path.read_text(encoding="utf-8"))["writer_id"]
    runner(command, cwd=tmp_path, check=False, env=environment)
    second_writer_id = json.loads(artifact_path.read_text(encoding="utf-8"))["writer_id"]

    marker_ids = {
        json.loads(marker.read_text(encoding="utf-8"))["writer_id"]
        for marker in writer_directory.glob("pytest-writer-*.json")
    }
    assert first_writer_id != second_writer_id
    assert marker_ids == {first_writer_id, second_writer_id}


@pytest.mark.parametrize(
    ("returncodes", "raise_on_call", "expected"),
    [
        ((-15, 7, 0, 0, 0), None, 7),
        ((-15, 0, 0, 0, 0), None, 2),
        ((0, 0, 0, 0, 0), 1, 2),
        ((0, 7, 0, 0, 0), 1, 7),
    ],
)
def test_legacy_exit_code_classifies_spawn_and_negative_outcomes(
    tmp_path: Path,
    returncodes: tuple[int, ...],
    raise_on_call: int | None,
    expected: int,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    runner = RecordingRunner(returncodes=returncodes, raise_on_call=raise_on_call)

    result = main(["--root", str(tmp_path), "--all"], runner=runner)

    assert result == expected
    assert len(runner.calls) == 6


def test_spawn_exception_is_recorded_and_later_checks_continue(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    error = FileNotFoundError("uv")
    stdout_at_spawn: list[str] = []

    def record_spawn(call: RecordedCall) -> None:
        stdout_at_spawn.append(capsys.readouterr().out)

    runner = RecordingRunner(
        raise_on_call=2,
        exception=error,
        on_call=record_spawn,
        publish_pytest_artifact=True,
    )

    result = main(["--root", str(tmp_path), "--all"], runner=runner)

    assert result == 2
    assert len(runner.calls) == 6
    assert stdout_at_spawn == [
        "\n==> ruff: uv run --locked python -m ruff check .\n",
        (
            "\n==> annotations: uv run --locked python -m ruff check . "
            "--select ANN --output-format concise\n"
        ),
        "\n==> ty: uv run --locked python -m ty check\n",
        "\n==> bandit: uv run --locked python -m bandit -c pyproject.toml -r .\n",
        "\n==> pytest: uv run --locked python -m pytest\n",
        "",
    ]
    assert capsys.readouterr().out == (
        "\n==> pyrepo-check summary: error (strict aggregate, incomplete)\n"
        "    error: annotations: Could not start process: FileNotFoundError: uv\n"
        "    advisory: Coverage guidance is unavailable because native Coverage.py configuration is absent.\n"
        "    passed: ruff, ty, bandit, pytest\n"
    )


def test_runner_value_error_is_not_a_planning_error(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    error = ValueError("runner failed")
    runner = RecordingRunner(raise_on_call=1, exception=error)

    with pytest.raises(ValueError) as captured:
        main(["--root", str(tmp_path), "ruff"], runner=runner)

    assert captured.value is error


def test_banner_is_printed_before_each_spawn(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    stdout_at_spawn: list[str] = []
    runner = RecordingRunner(on_call=lambda _call: stdout_at_spawn.append(capsys.readouterr().out))

    result = main(["--root", str(tmp_path), "ruff"], runner=runner)

    assert result == 0
    assert stdout_at_spawn == ["\n==> ruff: uv run --locked python -m ruff check src\n"]


def test_help_surface_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["pyrepo-check"])

    with pytest.raises(SystemExit) as captured:
        parse_args(["--help"])

    output = capsys.readouterr()
    assert captured.value.code == 0
    assert (
        output.out
        == """usage: pyrepo-check [-h] [--all] [--root ROOT] [--no-frozen]
                    [--python REQUEST] [--format {terminal,json}]
                    [--shortcut NAME] [--coverage]
                    [checks ...]

Run Python repository quality checks.

positional arguments:
  checks                Optional check names and target paths. Checks: ruff,
                        annotations, annotations-fix, ty, bandit, pytest.

options:
  -h, --help            show this help message and exit
  --all                 Run all checks.
  --root ROOT           Project root to check. Defaults to the current working
                        directory.
  --no-frozen           Recognized for compatibility; repository-safe
                        execution rejects it.
  --python REQUEST      Request a Repository Python from 3.10 through 3.13.
  --format {terminal,json}
                        Output terminal diagnostics or one JSON document.
  --shortcut NAME       Run a configured Test Shortcut in a pytest-only
                        focused run.
  --coverage            Plan Coverage.py collection for the selected pytest
                        run.
"""
    )
    assert output.err == ""


def test_format_defaults_to_terminal() -> None:
    assert parse_args([]).format == "terminal"


def test_json_format_is_public_syntax_before_checks() -> None:
    args = parse_args(["--format", "json", "ty"])

    assert args.format == "json"
    assert args.checks == ["ty"]


@pytest.mark.parametrize(
    "argv",
    (
        ("--shortcut", "unit", "pytest"),
        ("pytest", "--shortcut", "unit"),
    ),
)
def test_shortcut_is_public_syntax_in_both_supported_placements(
    argv: tuple[str, ...],
) -> None:
    args = parse_args(argv)

    assert args.checks == ["pytest"]
    assert args.shortcut == "unit"


def test_missing_shortcut_operand_remains_argparse_owned(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as captured:
        parse_args(["--shortcut"])

    output = capsys.readouterr()
    assert captured.value.code == 2
    assert output.out == ""
    assert "argument --shortcut: expected one argument" in output.err


def test_invalid_format_remains_argparse_owned(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as captured:
        parse_args(["--format", "xml"])

    output = capsys.readouterr()
    assert captured.value.code == 2
    assert output.out == ""
    assert "invalid choice: 'xml'" in output.err
    assert "choose from 'terminal', 'json'" in output.err


def test_python_requirement_is_consistent_across_active_contracts() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lockfile = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))
    reporting_design = (
        PROJECT_ROOT
        / "docs"
        / "superpowers"
        / "specs"
        / "2026-08-24-agent-guidance-reporting-design.md"
    ).read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert pyproject["project"]["requires-python"] == ">=3.13.15"
    assert pyproject["tool"]["ruff"]["target-version"] == "py313"
    assert lockfile["requires-python"] == ">=3.13.15"
    assert (PROJECT_ROOT / ".python-version").read_text(encoding="utf-8") == "3.13.15\n"
    assert "consumer Python `>=3.13.15`" in reporting_design
    assert "consumer Python `>=3.9`" not in reporting_design
    assert "consumer Python `>=3.10`" not in reporting_design
    assert "consumer Python below 3.9" not in reporting_design
    assert "consumer Python below 3.10" not in reporting_design
    assert "Python 3.13.15 or newer is required" in readme


def test_pytest_fixture_dependencies_are_development_only() -> None:
    """Reject moving plugin integration fixtures into runtime dependencies."""
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["dependencies"] == []
    assert "pytest-xdist>=3.8,<4" in pyproject["dependency-groups"]["dev"]
    assert "pytest-rerunfailures>=16.6,<17" in pyproject["dependency-groups"]["dev"]


def test_repository_coverage_dependency_is_development_only_and_locked() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lockfile = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))

    assert pyproject["project"]["dependencies"] == []
    assert "coverage[toml]>=7.15,<8" in pyproject["dependency-groups"]["dev"]
    coverage_packages = [
        package for package in lockfile["package"] if package["name"] == "coverage"
    ]
    assert len(coverage_packages) == 1
    major, minor, *_ = (int(piece) for piece in coverage_packages[0]["version"].split("."))
    assert major == 7
    assert minor >= 15


def test_repository_native_coverage_measurement_policy_is_explicit() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["tool"]["coverage"]["run"] == {
        "branch": True,
        "source": ["src/pyrepo_check"],
        "parallel": False,
    }


def test_repository_coverage_threshold_is_explicit() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["tool"]["coverage"]["report"] == {
        "fail_under": 86.01,
        "precision": 2,
    }
