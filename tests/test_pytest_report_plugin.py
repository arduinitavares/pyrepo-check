from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import shutil
from stat import S_IMODE
import subprocess  # nosec B404
import sys
from types import ModuleType
from typing import cast
import uuid

import pytest


class _CountingNodeId(str):
    comparisons = 0

    def __eq__(self, other: object) -> bool:
        type(self).comparisons += 1
        return super().__eq__(other)

    __hash__ = str.__hash__


@dataclass(frozen=True)
class PluginProjectRun:
    completed: subprocess.CompletedProcess[str]
    artifact: dict[str, object]
    artifact_path: Path
    markers: list[dict[str, object]]
    marker_paths: list[Path]
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
    marker_paths = sorted(writer_dir.glob("pytest-writer-*.json"))
    markers = [json.loads(path.read_text(encoding="utf-8")) for path in marker_paths]
    return PluginProjectRun(
        completed,
        artifact,
        artifact_path,
        markers,
        marker_paths,
        project,
    )


def _load_plugin_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> ModuleType:
    artifact_directory = tmp_path / "direct-plugin-artifacts"
    artifact_directory.mkdir()
    writer_directory = artifact_directory / "writers"
    writer_directory.mkdir()
    monkeypatch.setenv(
        "PYREPO_CHECK_PYTEST_JSON",
        str(artifact_directory / "artifact.json"),
    )
    monkeypatch.setenv("PYREPO_CHECK_PYTEST_WRITER_DIR", str(writer_directory))
    module_path = Path(__file__).parents[1] / "src/pyrepo_check/_pytest_report_plugin.py"
    spec = importlib.util.spec_from_file_location(
        f"direct_pytest_report_plugin_{uuid.uuid4().hex}",
        module_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugin_collection_and_phase_tracking_use_linear_membership_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_plugin_module(tmp_path, monkeypatch)
    evidence = module._Evidence()
    setattr(module, "_EVIDENCE", evidence)
    nodeids = [_CountingNodeId(f"node-{index}") for index in range(2_000)]

    class Item:
        def __init__(self, nodeid: str) -> None:
            self.nodeid = nodeid

    class Option:
        keyword = ""
        markexpr = ""
        deselect: list[str] = []
        ignore: list[str] = []
        ignore_glob: list[str] = []
        lf = False
        pyargs = False
        collectonly = False
        setuponly = False
        setupplan = False

    class Config:
        option = Option()

        @staticmethod
        def getoption(_name: str, *, default: object) -> object:
            return default

    items = [Item(nodeid) for nodeid in nodeids]
    wrapper = module.pytest_collection_modifyitems(None, Config(), items)
    next(wrapper)
    deselected = items[1::4]
    module.pytest_deselected(deselected)
    items[:] = items[::2]
    _CountingNodeId.comparisons = 0
    with pytest.raises(StopIteration):
        next(wrapper)

    class Report:
        duration = 0.0
        outcome = "passed"
        when = "call"
        longrepr = None

        def __init__(self, nodeid: str) -> None:
            self.nodeid = nodeid

    for nodeid in reversed(nodeids):
        module.pytest_runtest_logreport(Report(nodeid))

    assert evidence.final_nodeids == nodeids[::2]
    assert evidence.deselected_nodeids == nodeids[1::4]
    assert evidence.uncovered_removed_nodeids == nodeids[3::4]
    assert [report["nodeid"] for report in evidence.reports] == list(reversed(nodeids))
    assert _CountingNodeId.comparisons < len(nodeids) * 10


def test_plugin_finalization_is_idempotent_after_an_early_abort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_plugin_module(tmp_path, monkeypatch)
    evidence = module._Evidence()
    evidence.start_session()

    evidence.record_finish(4, stopped_early=True, forced=True)
    evidence.close()
    evidence.finalize_at_exit()
    evidence.finalize_at_exit()

    assert evidence.finishes == 1
    assert evidence.exit_code == 4
    assert evidence.stopped_early is True


def test_plugin_exit_handler_is_noop_without_a_started_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_plugin_module(tmp_path, monkeypatch)

    module._finalize_at_exit()

    artifact_path = tmp_path / "direct-plugin-artifacts" / "artifact.json"
    assert not artifact_path.exists()


def test_plugin_terminal_replace_failure_leaves_started_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_plugin_module(tmp_path, monkeypatch)
    evidence = module._Evidence()
    evidence.start_session()
    evidence.publish("started")
    evidence.record_finish(0, stopped_early=False)
    evidence.close()
    original_replace = module.os.replace

    def fail_terminal_replace(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("terminal replace failed")

    monkeypatch.setattr(module.os, "replace", fail_terminal_replace)

    with pytest.raises(OSError, match="terminal replace failed"):
        evidence.finalize_at_exit()
    monkeypatch.setattr(module.os, "replace", original_replace)
    artifact = json.loads(
        (tmp_path / "direct-plugin-artifacts" / "artifact.json").read_text()
    )

    assert artifact["state"] == "started"
    assert artifact["session"]["finishes"] == 0


def test_plugin_failed_post_terminal_invalidation_forces_internal_error_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_plugin_module(tmp_path, monkeypatch)
    evidence = module._Evidence()
    evidence.start_session()
    evidence.publish("started")
    evidence.record_finish(0, stopped_early=False)
    evidence.close()
    evidence.finalize_at_exit()
    forced_exit_codes: list[int] = []

    class ForcedExit(RuntimeError):
        pass

    def fail_restore_replace(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("invalidation replace failed")

    def force_exit(exit_code: int) -> None:
        forced_exit_codes.append(exit_code)
        raise ForcedExit(str(exit_code))

    monkeypatch.setattr(module.os, "replace", fail_restore_replace)
    monkeypatch.setattr(module.os, "_exit", force_exit)

    with pytest.raises(ForcedExit, match="^3$"):
        evidence.observe_hook()

    artifact = json.loads(
        (tmp_path / "direct-plugin-artifacts" / "artifact.json").read_text()
    )
    assert forced_exit_codes == [3]
    assert artifact["state"] == "finalized"
    assert artifact["session"]["finishes"] == 1


def test_plugin_conflicting_scope_observations_stay_non_finalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_plugin_module(tmp_path, monkeypatch)
    evidence = module._Evidence()
    evidence.start_session()
    evidence.remember_args(["--first"])
    evidence.remember_args(["--second"])
    evidence.publish("started")
    evidence.record_finish(0, stopped_early=False)
    evidence.close()

    evidence.finalize_at_exit()
    artifact = json.loads(
        (tmp_path / "direct-plugin-artifacts" / "artifact.json").read_text()
    )

    assert artifact["state"] == "started"
    assert artifact["session"]["finishes"] == 0


def test_plugin_finalized_exit_zero_requires_teardown_for_terminal_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_plugin_module(tmp_path, monkeypatch)
    evidence = module._Evidence()
    setattr(module, "_EVIDENCE", evidence)
    nodeid = "test_sample.py::test_case"
    evidence.start_session()
    evidence.collection_completed = True
    evidence.initial_nodeids = [nodeid]
    evidence.final_nodeids = [nodeid]
    evidence._final_nodeid_set = {nodeid}

    class Report:
        duration = 0.0
        outcome = "passed"
        longrepr = None

        def __init__(self, when: str) -> None:
            self.nodeid = nodeid
            self.when = when

    module.pytest_runtest_logreport(Report("setup"))
    module.pytest_runtest_logreport(Report("call"))

    class Option:
        keyword = ""
        markexpr = ""
        deselect: list[str] = []
        ignore: list[str] = []
        ignore_glob: list[str] = []
        lf = False
        pyargs = False
        collectonly = False
        setuponly = False
        setupplan = False

    class Config:
        option = Option()

        @staticmethod
        def getoption(_name: str, *, default: object) -> object:
            return default

    class Session:
        config = Config()
        shouldstop = False
        shouldfail = False

    wrapper = module.pytest_sessionfinish(Session(), 0)
    next(wrapper)
    with pytest.raises(StopIteration):
        next(wrapper)
    unconfigure = module.pytest_unconfigure(Session.config)
    next(unconfigure)
    with pytest.raises(StopIteration):
        next(unconfigure)
    evidence.finalize_at_exit()
    artifact = json.loads(
        (tmp_path / "direct-plugin-artifacts" / "artifact.json").read_text()
    )

    assert artifact["state"] == "finalized"
    assert artifact["session"] == {
        "starts": 1,
        "finishes": 1,
        "exit_code": 0,
        "collection_completed": True,
        "stopped_early": True,
    }
    assert [report["when"] for report in artifact["reports"]] == ["setup", "call"]


def test_plugin_finalizes_one_atomic_session_for_a_passing_test(tmp_path: Path) -> None:
    run = run_plugin_project(tmp_path, "def test_ok():\n    assert True\n")

    assert run.completed.returncode == 0
    assert run.artifact["state"] == "finalized"
    assert len(run.markers) == 1
    assert set(run.markers[0]) == {"schema_version", "writer_id", "pid"}
    assert run.markers[0]["schema_version"] == 1
    assert isinstance(run.markers[0]["pid"], int)
    assert run.artifact["writer_id"] == run.markers[0]["writer_id"]
    assert S_IMODE(run.artifact_path.stat().st_mode) == 0o600
    assert S_IMODE(run.marker_paths[0].stat().st_mode) == 0o600
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


def test_plugin_invalidates_a_consumer_atexit_hook_relay(tmp_path: Path) -> None:
    run = run_plugin_project(
        tmp_path,
        "def test_ok():\n    assert True\n",
        invocation_args=("-p", "late_relay"),
        plugin_sources={
            "late_relay": """
import atexit
from _pytest.reports import TestReport


_CONFIG = None


def relay_after_unconfigure():
    if _CONFIG is None:
        return
    _CONFIG.hook.pytest_runtest_logreport(
        report=TestReport(
            nodeid="test_sample.py::test_ok",
            location=("test_sample.py", 0, "test_ok"),
            keywords={},
            outcome="passed",
            longrepr=None,
            when="call",
            sections=(),
            duration=0.0,
            start=0.0,
            stop=0.0,
            user_properties=[],
        )
    )


atexit.register(relay_after_unconfigure)


def pytest_configure(config):
    global _CONFIG
    _CONFIG = config
""",
        },
    )

    session = cast(dict[str, object], run.artifact["session"])
    assert run.completed.returncode == 0
    assert run.artifact["state"] == "started"
    assert session["finishes"] == 0


def test_plugin_fatal_exit_keeps_started_artifact(tmp_path: Path) -> None:
    run = run_plugin_project(
        tmp_path,
        "import os\n\ndef test_abort():\n    os._exit(7)\n",  # nosec B404
    )

    session = cast(dict[str, object], run.artifact["session"])
    assert run.completed.returncode == 7
    assert run.artifact["state"] == "started"
    assert session["finishes"] == 0


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


@pytest.mark.parametrize(
    ("test_source", "expected_reports"),
    [
        (
            "import pytest\n\ndef test_case():\n    pytest.skip('skip reason')\n",
            [
                ("setup", "passed", False, None),
                ("call", "skipped", False, None),
                ("teardown", "passed", False, None),
            ],
        ),
        (
            "import pytest\n\ndef test_case():\n    pytest.xfail('imperative reason')\n",
            [
                ("setup", "passed", False, None),
                ("call", "skipped", True, "imperative reason"),
                ("teardown", "passed", False, None),
            ],
        ),
        (
            "import pytest\n\n@pytest.mark.xfail(reason='marked reason')\ndef test_case():\n    assert False\n",
            [
                ("setup", "passed", False, None),
                ("call", "skipped", True, "marked reason"),
                ("teardown", "passed", False, None),
            ],
        ),
        (
            "import pytest\n\n@pytest.mark.xfail(reason='not run', run=False)\ndef test_case():\n    assert False\n",
            [
                ("setup", "skipped", True, "[NOTRUN] not run"),
                ("teardown", "passed", False, None),
            ],
        ),
        (
            "import pytest\n\n@pytest.mark.xfail(reason='strict reason', strict=True)\ndef test_case():\n    assert True\n",
            [
                ("setup", "passed", False, None),
                ("call", "failed", False, None),
                ("teardown", "passed", False, None),
            ],
        ),
        (
            "import pytest\n\n@pytest.mark.xfail(reason='non-strict reason')\ndef test_case():\n    assert True\n",
            [
                ("setup", "passed", False, None),
                ("call", "passed", True, "non-strict reason"),
                ("teardown", "passed", False, None),
            ],
        ),
    ],
)
def test_plugin_records_pytest_8_expected_failure_shapes(
    tmp_path: Path,
    test_source: str,
    expected_reports: list[tuple[str, str, bool, str | None]],
) -> None:
    """Catch loss or coercion of pytest 8 skip/XFAIL/XPASS metadata."""
    run = run_plugin_project(tmp_path, test_source)

    reports = cast(list[dict[str, object]], run.artifact["reports"])
    assert [
        (
            report["when"],
            report["outcome"],
            report["wasxfail_present"],
            report["wasxfail"],
        )
        for report in reports
    ] == expected_reports
    assert all(report["wasxfail_valid"] is True for report in reports)
    assert all(report["longrepr"] is None or isinstance(report["longrepr"], str) for report in reports)


@pytest.mark.parametrize(
    ("fixture_source", "test_source", "expected_reports"),
    [
        (
            """import pytest


@pytest.fixture
def xfail_in_setup():
    pytest.xfail("setup reason")
""",
            "def test_case(xfail_in_setup):\n    assert True\n",
            [
                ("setup", "skipped", True, "setup reason"),
                ("teardown", "passed", False, None),
            ],
        ),
        (
            """import pytest


@pytest.fixture
def xfail_in_teardown():
    yield
    pytest.xfail("teardown reason")
""",
            "def test_case(xfail_in_teardown):\n    assert True\n",
            [
                ("setup", "passed", False, None),
                ("call", "passed", False, None),
                ("teardown", "skipped", True, "teardown reason"),
            ],
        ),
    ],
)
def test_plugin_records_fixture_xfail_phases_without_early_stop(
    tmp_path: Path,
    fixture_source: str,
    test_source: str,
    expected_reports: list[tuple[str, str, bool, str | None]],
) -> None:
    """Catch losing setup or teardown XFAIL metadata and terminal coverage."""
    run = run_plugin_project(
        tmp_path,
        test_source,
        project_sources={"conftest.py": fixture_source},
    )

    collection = cast(dict[str, object], run.artifact["collection"])
    reports = cast(list[dict[str, object]], run.artifact["reports"])
    session = cast(dict[str, object], run.artifact["session"])
    assert run.completed.returncode == 0
    assert collection["final_nodeids"] == ["test_sample.py::test_case"]
    assert [
        (
            report["when"],
            report["outcome"],
            report["wasxfail_present"],
            report["wasxfail"],
        )
        for report in reports
    ] == expected_reports
    assert {report["nodeid"] for report in reports} == {"test_sample.py::test_case"}
    assert session["stopped_early"] is False


@pytest.mark.parametrize("invocation_args", (("-x",), ("--maxfail=1",)))
def test_plugin_marks_early_stop_when_a_collected_node_lacks_terminal_outcome(
    tmp_path: Path,
    invocation_args: tuple[str, ...],
) -> None:
    """Catch treating a truncated session as complete after collection."""
    run = run_plugin_project(
        tmp_path,
        "def test_first():\n    assert False\n\ndef test_never_runs():\n    assert True\n",
        invocation_args=invocation_args,
    )

    collection = cast(dict[str, object], run.artifact["collection"])
    reports = cast(list[dict[str, object]], run.artifact["reports"])
    assert run.completed.returncode == 1
    assert collection["final_nodeids"] == [
        "test_sample.py::test_first",
        "test_sample.py::test_never_runs",
    ]
    assert {report["nodeid"] for report in reports} == {"test_sample.py::test_first"}
    assert run.artifact["session"] == {
        "starts": 1,
        "finishes": 1,
        "exit_code": 1,
        "collection_completed": True,
        "stopped_early": True,
    }


def test_plugin_does_not_treat_executed_failures_as_early_stop(tmp_path: Path) -> None:
    """Catch deriving early-stop status solely from a nonzero failed-test count."""
    run = run_plugin_project(
        tmp_path,
        "def test_one():\n    assert False\n\ndef test_two():\n    assert False\n",
    )

    assert run.completed.returncode == 1
    assert run.artifact["session"] == {
        "starts": 1,
        "finishes": 1,
        "exit_code": 1,
        "collection_completed": True,
        "stopped_early": False,
    }


def test_plugin_marks_a_final_node_without_any_phase_report_as_early_stop(
    tmp_path: Path,
) -> None:
    """Catch a plugin silently preventing one collected node from running."""
    run = run_plugin_project(
        tmp_path,
        "def test_runs():\n    assert True\n\ndef test_has_no_report():\n    assert True\n",
        invocation_args=("-p", "suppress_node"),
        plugin_sources={
            "suppress_node": """
def pytest_runtest_protocol(item, nextitem):
    if item.name == \"test_has_no_report\":
        return True
    return None
""",
        },
    )

    reports = cast(list[dict[str, object]], run.artifact["reports"])
    session = cast(dict[str, object], run.artifact["session"])
    assert run.completed.returncode == 0
    assert {report["nodeid"] for report in reports} == {"test_sample.py::test_runs"}
    assert session["stopped_early"] is True


def test_plugin_allows_inactive_xdist_without_parallelism_flag(tmp_path: Path) -> None:
    """Catch flagging installed xdist when it is explicitly inactive."""
    run = run_plugin_project(
        tmp_path,
        "def test_ok():\n    assert True\n",
        invocation_args=("-n", "0"),
    )

    assert run.completed.returncode == 0
    assert run.artifact["flags"] == {
        "unsupported_parallelism": False,
        "unsupported_retries": False,
        "worker_metadata": False,
    }


def test_plugin_rejects_xdist_before_a_worker_can_import_tests(tmp_path: Path) -> None:
    """Catch allowing a parallel worker to create test-side effects."""
    worker_sentinel = tmp_path / "worker-imported"
    run = run_plugin_project(
        tmp_path,
        (
            "import os\n"
            "from pathlib import Path\n\n"
            "if os.environ.get('PYTEST_XDIST_WORKER'):\n"
            f"    Path({str(worker_sentinel)!r}).write_text('worker started', encoding='utf-8')\n\n"
            "def test_ok():\n"
            "    assert True\n"
        ),
        invocation_args=("-n", "1"),
    )

    assert run.completed.returncode == 4
    assert run.artifact["state"] == "finalized"
    assert run.artifact["session"] == {
        "starts": 1,
        "finishes": 1,
        "exit_code": 4,
        "collection_completed": False,
        "stopped_early": True,
    }
    assert run.artifact["flags"] == {
        "unsupported_parallelism": True,
        "unsupported_retries": False,
        "worker_metadata": False,
    }
    assert not worker_sentinel.exists()


def test_plugin_rejects_real_reruns(tmp_path: Path) -> None:
    """Catch accepting a session whose passed result required a retry."""
    run = run_plugin_project(
        tmp_path,
        "attempts = 0\n\ndef test_flaky():\n    global attempts\n    attempts += 1\n    assert attempts == 2\n",
        invocation_args=("--reruns", "1"),
    )

    assert run.completed.returncode == 0
    flags = cast(dict[str, object], run.artifact["flags"])
    assert flags["unsupported_retries"] is True


@pytest.mark.parametrize(
    ("nodeid", "when", "outcome"),
    (
        ("test_sample.py::test_ok", "setup", "passed"),
        ("test_sample.py::test_ok", "teardown", "passed"),
        ("synthetic.py::test_rerun", "call", "rerun"),
    ),
)
def test_plugin_rejects_synthetic_repeated_phases_and_noncore_outcomes(
    tmp_path: Path,
    nodeid: str,
    when: str,
    outcome: str,
) -> None:
    """Catch retry-like reports injected by another pytest plugin."""
    run = run_plugin_project(
        tmp_path,
        "def test_ok():\n    assert True\n",
        invocation_args=("-p", "inject_report"),
        plugin_sources={
            "inject_report": f"""
from _pytest.reports import TestReport


def pytest_sessionfinish(session, exitstatus):
    session.config.hook.pytest_runtest_logreport(
        report=TestReport(
            nodeid={nodeid!r},
            location=(\"synthetic.py\", 0, \"test_rerun\"),
            keywords={{}},
            outcome={outcome!r},
            longrepr=None,
            when={when!r},
            sections=(),
            duration=0.0,
            start=0.0,
            stop=0.0,
            user_properties=[],
        )
    )
""",
        },
    )

    assert run.completed.returncode == 0
    flags = cast(dict[str, object], run.artifact["flags"])
    assert flags["unsupported_retries"] is True
