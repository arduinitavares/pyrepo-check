"""Collect raw pytest-session evidence for pyrepo-check."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import uuid

import pytest


_ARTIFACT_PATH = Path(os.environ["PYREPO_CHECK_PYTEST_JSON"])
_WRITER_DIR = Path(os.environ["PYREPO_CHECK_PYTEST_WRITER_DIR"])
_WRITER_ID = f"{__name__}-{os.getpid()}"


class _Evidence:
    def __init__(self) -> None:
        self.starts = 0
        self.finishes = 0
        self.exit_code = 0
        self.collection_completed = False
        self.stopped_early = False
        self.effective_args: list[str] = []
        self.semantic_options: dict[str, object] = self.empty_semantic_options()
        self.initial_nodeids: list[str] = []
        self.final_nodeids: list[str] = []
        self.deselected_nodeids: list[str] = []
        self.uncovered_removed_nodeids: list[str] = []
        self.collection_errors: list[dict[str, str]] = []
        self.collection_skips: list[dict[str, str]] = []
        self.reports: list[dict[str, object]] = []
        self.unsupported_parallelism = False
        self.unsupported_retries = False
        self.worker_metadata = False
        self._deselected_during_collection: list[str] | None = None

    @staticmethod
    def empty_semantic_options() -> dict[str, object]:
        return {
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
        }

    def snapshot_semantic_options(self, config: pytest.Config) -> None:
        option = config.option
        self.semantic_options = {
            "collection_paths": list(config.getoption("file_or_dir", default=[])),
            "keyword": option.keyword,
            "markexpr": option.markexpr,
            "deselect": list(option.deselect or []),
            "ignore": list(option.ignore or []),
            "ignore_glob": list(option.ignore_glob or []),
            "lf": option.lf,
            "pyargs": option.pyargs,
            "collectonly": option.collectonly,
            "setuponly": option.setuponly,
            "setupplan": option.setupplan,
        }

    def publish(self, state: str) -> None:
        document = {
            "schema_version": 1,
            "state": state,
            "writer_id": _WRITER_ID,
            "pytest_version": pytest.__version__,
            "session": {
                "starts": self.starts,
                "finishes": self.finishes,
                "exit_code": self.exit_code,
                "collection_completed": self.collection_completed,
                "stopped_early": self.stopped_early,
            },
            "effective_args": self.effective_args,
            "semantic_options": self.semantic_options,
            "collection": {
                "initial_nodeids": self.initial_nodeids,
                "final_nodeids": self.final_nodeids,
                "deselected_nodeids": self.deselected_nodeids,
                "uncovered_removed_nodeids": self.uncovered_removed_nodeids,
                "errors": self.collection_errors,
                "skips": self.collection_skips,
            },
            "reports": self.reports,
            "flags": {
                "unsupported_parallelism": self.unsupported_parallelism,
                "unsupported_retries": self.unsupported_retries,
                "worker_metadata": self.worker_metadata,
            },
        }
        temporary_path = _ARTIFACT_PATH.with_name(
            f".{_ARTIFACT_PATH.name}.{_WRITER_ID}.{uuid.uuid4().hex}.tmp"
        )
        with open(temporary_path, "x", encoding="utf-8") as temporary_file:
            os.chmod(temporary_path, 0o600)
            json.dump(document, temporary_file, separators=(",", ":"))
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, _ARTIFACT_PATH)


_EVIDENCE = _Evidence()


def _register_writer() -> None:
    marker_path = _WRITER_DIR / f"pytest-writer-{_WRITER_ID}.json"

    def open_owner_only(path: str, flags: int) -> int:
        return os.open(path, flags, 0o600)

    with open(marker_path, "x", encoding="utf-8", opener=open_owner_only) as marker_file:
        json.dump(
            {"schema_version": 1, "writer_id": _WRITER_ID, "pid": os.getpid()},
            marker_file,
            separators=(",", ":"),
        )
        marker_file.flush()
        os.fsync(marker_file.fileno())


def _remove_owned_plugin_pair(args: list[str]) -> list[str]:
    effective_args: list[str] = []
    index = 0
    while index < len(args):
        if args[index : index + 2] == ["-p", __name__]:
            index += 2
        else:
            effective_args.append(args[index])
            index += 1
    return effective_args


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_load_initial_conftests(
    early_config: pytest.Config,
    parser: pytest.Parser,
    args: list[str],
) -> object:
    yield
    _EVIDENCE.effective_args = _remove_owned_plugin_pair(list(args))


def pytest_sessionstart(session: pytest.Session) -> None:
    _register_writer()
    _EVIDENCE.starts += 1
    _EVIDENCE.publish("started")


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_collection_modifyitems(
    session: pytest.Session,
    config: pytest.Config,
    items: list[pytest.Item],
) -> object:
    _EVIDENCE.snapshot_semantic_options(config)
    _EVIDENCE.initial_nodeids = [item.nodeid for item in items]
    _EVIDENCE._deselected_during_collection = []
    yield
    _EVIDENCE.final_nodeids = [item.nodeid for item in items]
    deselected = _EVIDENCE._deselected_during_collection
    _EVIDENCE._deselected_during_collection = None
    deselected_nodeids = set(deselected or [])
    _EVIDENCE.uncovered_removed_nodeids = [
        nodeid
        for nodeid in _EVIDENCE.initial_nodeids
        if nodeid not in _EVIDENCE.final_nodeids and nodeid not in deselected_nodeids
    ]


def pytest_deselected(items: list[pytest.Item]) -> None:
    nodeids = [item.nodeid for item in items]
    _EVIDENCE.deselected_nodeids.extend(nodeids)
    if _EVIDENCE._deselected_during_collection is not None:
        _EVIDENCE._deselected_during_collection.extend(nodeids)


def pytest_collectreport(report: pytest.CollectReport) -> None:
    if report.failed:
        _EVIDENCE.collection_errors.append(
            {"nodeid": report.nodeid, "message": str(report.longrepr)}
        )
    elif report.skipped:
        _EVIDENCE.collection_skips.append(
            {"nodeid": report.nodeid, "message": str(report.longrepr)}
        )


def pytest_collection_finish(session: pytest.Session) -> None:
    _EVIDENCE.collection_completed = True


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    duration = report.duration if math.isfinite(report.duration) and report.duration >= 0 else 0.0
    wasxfail = getattr(report, "wasxfail", None)
    phase_key = (report.nodeid, report.when)
    if any((item["nodeid"], item["when"]) == phase_key for item in _EVIDENCE.reports):
        _EVIDENCE.unsupported_retries = True
    if report.outcome not in {"passed", "failed", "skipped"}:
        _EVIDENCE.unsupported_retries = True
    _EVIDENCE.reports.append(
        {
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "duration": duration,
            "wasxfail_present": hasattr(report, "wasxfail"),
            "wasxfail_valid": not hasattr(report, "wasxfail") or isinstance(wasxfail, str),
            "wasxfail": wasxfail if isinstance(wasxfail, str) else None,
            "longrepr": None if report.longrepr is None else str(report.longrepr),
        }
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    _EVIDENCE.finishes += 1
    _EVIDENCE.exit_code = exitstatus
    _EVIDENCE.stopped_early = bool(session.shouldstop or session.shouldfail)
    _EVIDENCE.publish("finalized")


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_setupnodes(config: pytest.Config, specs: list[object]) -> None:
    if specs:
        _EVIDENCE.unsupported_parallelism = True


def pytest_configure(config: pytest.Config) -> None:
    _EVIDENCE.worker_metadata = hasattr(config, "workerinput")
