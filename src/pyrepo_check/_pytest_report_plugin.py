"""Collect raw pytest-session evidence for pyrepo-check."""

from __future__ import annotations

import atexit
from collections.abc import Generator
import json
import math
import os
from pathlib import Path
from typing import cast
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
        self._deselected_during_collection_set: set[str] | None = None
        self._final_nodeid_set: set[str] = set()
        self._observed_phase_keys: set[tuple[str, str | None]] = set()
        self._terminal_nodeids: set[str] = set()
        self._args: list[str] | None = None
        self._config: pytest.Config | None = None
        self._args_observed = False
        self._semantic_options_observed = False
        self._session_started = False
        self._finish_seen = False
        self._forced_finish = False
        self._closed = False
        self._terminal_published = False
        self._invalidated = False

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

    @staticmethod
    def _semantic_options(config: pytest.Config) -> dict[str, object]:
        option = config.option
        return {
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

    def observe_hook(self) -> bool:
        terminal_was_published = self._terminal_published
        if self._closed or terminal_was_published:
            self._invalidated = True
            if self._session_started:
                try:
                    self.publish("started")
                except OSError:
                    if terminal_was_published:
                        os._exit(int(pytest.ExitCode.INTERNAL_ERROR))
                    raise
            return False
        return not self._invalidated

    def remember_args(self, args: list[str]) -> None:
        self._args = args
        self._merge_args(_remove_owned_plugin_pair(list(args)))

    def remember_config(self, config: pytest.Config) -> None:
        self._config = config
        self._merge_semantic_options(self._semantic_options(config))

    def refresh_scope(self) -> None:
        if self._args is not None:
            self._merge_args(_remove_owned_plugin_pair(list(self._args)))
        if self._config is not None:
            self._merge_semantic_options(self._semantic_options(self._config))

    def _merge_args(self, observed: list[str]) -> None:
        if not self._args_observed:
            self.effective_args = observed
            self._args_observed = True
        elif _is_subsequence(self.effective_args, observed):
            self.effective_args = observed
        elif not _is_subsequence(observed, self.effective_args):
            self._invalidated = True

    def _merge_semantic_options(self, observed: dict[str, object]) -> None:
        if not self._semantic_options_observed:
            self.semantic_options = observed
            self._semantic_options_observed = True
            return
        for name in ("collection_paths", "deselect", "ignore", "ignore_glob"):
            retained = list(cast(list[str], self.semantic_options[name]))
            for value in cast(list[str], observed[name]):
                if value not in retained:
                    retained.append(value)
            self.semantic_options[name] = retained
        for name in ("lf", "pyargs", "collectonly", "setuponly", "setupplan"):
            self.semantic_options[name] = bool(self.semantic_options[name] or observed[name])
        for name in ("keyword", "markexpr"):
            retained = self.semantic_options[name]
            current = observed[name]
            if retained and current and retained != current:
                self._invalidated = True
            elif current:
                self.semantic_options[name] = current

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

    def start_session(self) -> None:
        self._session_started = True
        self.starts += 1

    def record_finish(
        self,
        exit_code: int,
        *,
        stopped_early: bool,
        forced: bool = False,
    ) -> None:
        if self._finish_seen:
            if self._forced_finish and exit_code == self.exit_code:
                self.stopped_early = self.stopped_early or stopped_early
                return
            self._invalidated = True
            return
        self._finish_seen = True
        self._forced_finish = forced
        self.exit_code = exit_code
        self.stopped_early = stopped_early

    def close(self) -> None:
        self._closed = True

    def finalize_at_exit(self) -> None:
        if not self._session_started or self._terminal_published:
            return
        self.refresh_scope()
        if self._invalidated or not self._finish_seen or not self._closed:
            self.publish("started")
            return
        self.finishes = 1
        self.publish("finalized")
        self._terminal_published = True


_EVIDENCE = _Evidence()


def _finalize_at_exit() -> None:
    _EVIDENCE.finalize_at_exit()


atexit.register(_finalize_at_exit)


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


def _is_subsequence(candidate: list[str], sequence: list[str]) -> bool:
    candidate_index = 0
    for value in sequence:
        if candidate_index < len(candidate) and value == candidate[candidate_index]:
            candidate_index += 1
    return candidate_index == len(candidate)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_load_initial_conftests(
    early_config: pytest.Config,
    parser: pytest.Parser,
    args: list[str],
) -> Generator[None, object, None]:
    if _EVIDENCE.observe_hook():
        _EVIDENCE.remember_args(args)
    yield
    if _EVIDENCE.observe_hook():
        _EVIDENCE.remember_args(args)


def pytest_sessionstart(session: pytest.Session) -> None:
    if not _EVIDENCE.observe_hook():
        return
    _register_writer()
    _EVIDENCE.remember_config(session.config)
    _EVIDENCE.start_session()
    _EVIDENCE.publish("started")


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_modifyitems(
    session: pytest.Session,
    config: pytest.Config,
    items: list[pytest.Item],
) -> Generator[None, object, None]:
    active = _EVIDENCE.observe_hook()
    if not active:
        yield
        return
    _EVIDENCE.remember_config(config)
    _EVIDENCE.initial_nodeids = [item.nodeid for item in items]
    _EVIDENCE._deselected_during_collection_set = set()
    yield
    _EVIDENCE.refresh_scope()
    _EVIDENCE.final_nodeids = [item.nodeid for item in items]
    _EVIDENCE._final_nodeid_set = set(_EVIDENCE.final_nodeids)
    deselected_nodeids = _EVIDENCE._deselected_during_collection_set or set()
    _EVIDENCE._deselected_during_collection_set = None
    _EVIDENCE.uncovered_removed_nodeids = [
        nodeid
        for nodeid in _EVIDENCE.initial_nodeids
        if nodeid not in _EVIDENCE._final_nodeid_set and nodeid not in deselected_nodeids
    ]


def pytest_deselected(items: list[pytest.Item]) -> None:
    if not _EVIDENCE.observe_hook():
        return
    nodeids = [item.nodeid for item in items]
    _EVIDENCE.deselected_nodeids.extend(nodeids)
    if _EVIDENCE._deselected_during_collection_set is not None:
        _EVIDENCE._deselected_during_collection_set.update(nodeids)


def pytest_collectreport(report: pytest.CollectReport) -> None:
    if not _EVIDENCE.observe_hook():
        return
    if report.failed:
        _EVIDENCE.collection_errors.append(
            {"nodeid": report.nodeid, "message": str(report.longrepr)}
        )
    elif report.skipped:
        _EVIDENCE.collection_skips.append(
            {"nodeid": report.nodeid, "message": str(report.longrepr)}
        )


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_finish(session: pytest.Session) -> Generator[None, object, None]:
    active = _EVIDENCE.observe_hook()
    if active:
        _EVIDENCE.remember_config(session.config)
    yield
    if active:
        _EVIDENCE.refresh_scope()
        _EVIDENCE.collection_completed = True


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if not _EVIDENCE.observe_hook():
        return
    duration = report.duration if math.isfinite(report.duration) and report.duration >= 0 else 0.0
    wasxfail = getattr(report, "wasxfail", None)
    phase_key = (report.nodeid, report.when)
    if phase_key in _EVIDENCE._observed_phase_keys:
        _EVIDENCE.unsupported_retries = True
    _EVIDENCE._observed_phase_keys.add(phase_key)
    if report.outcome not in {"passed", "failed", "skipped"}:
        _EVIDENCE.unsupported_retries = True
    if report.when == "teardown":
        _EVIDENCE._terminal_nodeids.add(report.nodeid)
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


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(
    session: pytest.Session,
    exitstatus: int,
) -> Generator[None, object, None]:
    active = _EVIDENCE.observe_hook()
    if active:
        _EVIDENCE.remember_config(session.config)
    yield
    if not active:
        return
    _EVIDENCE.refresh_scope()
    stopped_early = bool(
        session.shouldstop
        or session.shouldfail
        or (
            not _EVIDENCE.collection_errors
            and not _EVIDENCE._final_nodeid_set.issubset(_EVIDENCE._terminal_nodeids)
        )
    )
    _EVIDENCE.record_finish(exitstatus, stopped_early=stopped_early)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_unconfigure(config: pytest.Config) -> Generator[None, object, None]:
    active = _EVIDENCE.observe_hook()
    if active:
        _EVIDENCE.remember_config(config)
    yield
    if active:
        _EVIDENCE.refresh_scope()
        _EVIDENCE.close()


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_setupnodes(config: pytest.Config, specs: list[object]) -> None:
    if specs and _EVIDENCE.observe_hook():
        _EVIDENCE.remember_config(config)
        _EVIDENCE.unsupported_parallelism = True
        _EVIDENCE.record_finish(4, stopped_early=True, forced=True)
        pytest.exit(returncode=4)


def pytest_configure(config: pytest.Config) -> None:
    if not _EVIDENCE.observe_hook():
        return
    _EVIDENCE.remember_config(config)
    _EVIDENCE.worker_metadata = hasattr(config, "workerinput")
