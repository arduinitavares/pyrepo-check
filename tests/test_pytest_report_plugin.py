from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess  # nosec B404
import sys
from typing import cast
import uuid


@dataclass(frozen=True)
class PluginProjectRun:
    completed: subprocess.CompletedProcess[str]
    artifact: dict[str, object]
    markers: list[dict[str, object]]
    project: Path


def run_plugin_project(
    tmp_path: Path,
    test_source: str,
    *,
    invocation_args: tuple[str, ...] = (),
    project_sources: dict[str, str] | None = None,
    plugin_sources: dict[str, str] | None = None,
    pytest_addopts: str | None = None,
) -> PluginProjectRun:
    project = tmp_path / "project"
    project.mkdir(parents=True)
    (project / "test_sample.py").write_text(test_source, encoding="utf-8")
    for relative_path, source in (project_sources or {}).items():
        destination = project / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source, encoding="utf-8")

    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    artifact_path = artifact_dir / "pytest.json"
    writer_dir = artifact_dir / "writers"
    writer_dir.mkdir()

    module_dir = tmp_path / "plugin"
    module_dir.mkdir()
    module_name = f"pyrepo_check_pytest_{uuid.uuid4().hex}"
    shutil.copyfile(
        Path(__file__).parents[1] / "src/pyrepo_check/_pytest_report_plugin.py",
        module_dir / f"{module_name}.py",
    )
    for plugin_name, source in (plugin_sources or {}).items():
        (module_dir / f"{plugin_name}.py").write_text(source, encoding="utf-8")
    environment = os.environ | {
        "PYREPO_CHECK_PYTEST_JSON": str(artifact_path),
        "PYREPO_CHECK_PYTEST_WRITER_DIR": str(writer_dir),
        "PYTHONPATH": os.pathsep.join(
            filter(None, (os.environ.get("PYTHONPATH"), str(module_dir)))
        ),
    }
    if pytest_addopts is not None:
        environment["PYTEST_ADDOPTS"] = pytest_addopts
    completed = subprocess.run(  # nosec B603
        (sys.executable, "-m", "pytest", "-p", module_name, *invocation_args),
        cwd=project,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    markers = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(writer_dir.glob("pytest-writer-*.json"))
    ]
    return PluginProjectRun(completed, artifact, markers, project)


def test_plugin_finalizes_one_atomic_session_for_a_passing_test(tmp_path: Path) -> None:
    run = run_plugin_project(tmp_path, "def test_ok():\n    assert True\n")

    assert run.completed.returncode == 0
    assert run.artifact["state"] == "finalized"
    assert run.artifact["writer_id"] == run.markers[0]["writer_id"]
    assert run.artifact["session"] == {
        "starts": 1,
        "finishes": 1,
        "exit_code": 0,
        "collection_completed": True,
        "stopped_early": False,
    }
    reports = cast(list[dict[str, object]], run.artifact["reports"])
    assert [item["when"] for item in reports] == [
        "setup",
        "call",
        "teardown",
    ]


def test_plugin_records_effective_args_after_all_public_sources(tmp_path: Path) -> None:
    run = run_plugin_project(
        tmp_path,
        "def test_ok():\n    assert True\n",
        invocation_args=("-p", "mutate_args", "-vv"),
        project_sources={
            "pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-q'\n",
        },
        plugin_sources={
            "mutate_args": """
import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_load_initial_conftests(early_config, parser, args):
    yield
    args.append("--disable-warnings")
""",
        },
        pytest_addopts="-ra",
    )

    effective_args = cast(list[str], run.artifact["effective_args"])
    assert "-q" in effective_args
    assert "-ra" in effective_args
    assert "-p" in effective_args
    assert "mutate_args" in effective_args
    assert "-vv" in effective_args
    assert "--disable-warnings" in effective_args
    assert not any(argument.startswith("pyrepo_check_pytest_") for argument in effective_args)


def test_plugin_snapshots_final_semantic_options(tmp_path: Path) -> None:
    run = run_plugin_project(
        tmp_path,
        "def test_kept():\n    assert True\n\ndef test_removed():\n    assert True\n",
        invocation_args=("-k", "kept"),
    )

    semantic_options = cast(dict[str, object], run.artifact["semantic_options"])
    assert semantic_options == {
        "collection_paths": [],
        "keyword": "kept",
        "markexpr": "",
        "deselect": [],
        "ignore": [],
        "ignore_glob": [],
        "lf": False,
        "pyargs": False,
        "collectonly": False,
        "setuponly": False,
        "setupplan": False,
    }


def test_plugin_separates_reported_deselection_from_silent_collection_removal(
    tmp_path: Path,
) -> None:
    deselected = run_plugin_project(
        tmp_path / "deselected",
        "def test_a():\n    assert True\n\ndef test_b():\n    assert True\n",
        invocation_args=("--deselect", "test_sample.py::test_b"),
    )
    silently_removed = run_plugin_project(
        tmp_path / "silently-removed",
        "def test_a():\n    assert True\n\ndef test_b():\n    assert True\n",
        invocation_args=("-p", "remove_item"),
        plugin_sources={
            "remove_item": """
def pytest_collection_modifyitems(items):
    items[:] = [item for item in items if item.name != "test_b"]
""",
        },
    )

    deselected_collection = cast(dict[str, object], deselected.artifact["collection"])
    silently_removed_collection = cast(dict[str, object], silently_removed.artifact["collection"])
    assert deselected_collection["deselected_nodeids"] == ["test_sample.py::test_b"]
    assert deselected_collection["uncovered_removed_nodeids"] == []
    assert silently_removed_collection["deselected_nodeids"] == []
    assert silently_removed_collection["uncovered_removed_nodeids"] == ["test_sample.py::test_b"]


def test_plugin_keeps_collection_errors_and_skips_separate(tmp_path: Path) -> None:
    run = run_plugin_project(
        tmp_path,
        "def test_ok():\n    assert True\n",
        project_sources={
            "test_error.py": "raise RuntimeError('collection failed')\n",
            "test_skip.py": "import pytest\npytest.skip('not applicable', allow_module_level=True)\n",
        },
    )

    collection = cast(dict[str, object], run.artifact["collection"])
    errors = cast(list[dict[str, str]], collection["errors"])
    skips = cast(list[dict[str, str]], collection["skips"])
    assert run.artifact["session"] == {
        "starts": 1,
        "finishes": 1,
        "exit_code": 2,
        "collection_completed": True,
        "stopped_early": False,
    }
    assert [issue["nodeid"] for issue in errors] == ["test_error.py"]
    assert [issue["nodeid"] for issue in skips] == ["test_skip.py"]
