from __future__ import annotations

import ast
from collections.abc import Callable
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess  # nosec B404
import sys
from types import ModuleType
from typing import Any

import pytest

from pyrepo_check.execution import PreparedRepositoryEnvironment
from pyrepo_check.planning import CheckInvocation
from pyrepo_check import _check_launcher as standalone_launcher
from pyrepo_check import repository_executor
from tests.support import (
    RecordingRunner,
    launcher_aware_runner,
    monotonic_clock,
    prepared_repository,
    RecordedCall,
    test_workspace,
)


def _launcher() -> ModuleType:
    return importlib.import_module("pyrepo_check.check_launcher")


def _arguments_sha256(arguments: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for argument in arguments:
        encoded = argument.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _marker_payload(
    prepared: PreparedRepositoryEnvironment,
    invocation: CheckInvocation,
    module: str,
) -> dict[str, Any]:
    python = prepared.python
    return {
        "schema_version": 1,
        "check": invocation.name,
        "module": module,
        "arguments_sha256": _arguments_sha256(invocation.arguments),
        "python": {
            "implementation": python.implementation,
            "version": list(python.version),
            "executable": str(python.executable),
        },
    }


def _write_marker(path: Path, payload: object) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(descriptor, json.dumps(payload, separators=(",", ":")).encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def test_standalone_launcher_is_python_310_stdlib_only() -> None:
    source_path = Path(__file__).parents[1] / "src/pyrepo_check/_check_launcher.py"
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 10))
    imported = {
        alias.name.partition(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.partition(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert "pyrepo_check" not in imported
    assert imported <= {
        "__future__",
        "hashlib",
        "json",
        "os",
        "pathlib",
        "runpy",
        "sys",
        "traceback",
    }


def test_standalone_argument_digest_is_length_prefixed() -> None:
    assert standalone_launcher.argument_digest(["ab", "c"]) == _arguments_sha256(
        ("ab", "c")
    )
    assert standalone_launcher.argument_digest(["ab", "c"]) != (
        standalone_launcher.argument_digest(["a", "bc"])
    )


@pytest.mark.parametrize(
    "arguments",
    (
        [],
        ["--evidence", "marker", "--wrong", "ruff", "--module", "ruff", "--"],
        ["--evidence", "", "--check", "ruff", "--module", "ruff", "--"],
        ["--evidence", "marker", "--check", "", "--module", "ruff", "--"],
        ["--evidence", "marker", "--check", "ruff", "--module", "", "--"],
        ["--evidence", "marker", "--check", "ruff", "--module", "ruff", "wrong"],
    ),
)
def test_standalone_parser_rejects_malformed_syntax(arguments: list[str]) -> None:
    assert standalone_launcher.parse_arguments(arguments) is None
    assert standalone_launcher.main(arguments) == 120


def test_standalone_parser_preserves_exact_module_arguments(tmp_path: Path) -> None:
    marker = tmp_path / "marker.json"
    assert standalone_launcher.parse_arguments(
        [
            "--evidence",
            str(marker),
            "--check",
            "pytest",
            "--module",
            "pytest",
            "--",
            "-q",
            "-k",
            "value",
        ]
    ) == (marker, "pytest", "pytest", ["-q", "-k", "value"])


def test_standalone_dispatch_publishes_before_module_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "marker.json"
    observed: list[tuple[object, ...]] = []
    original_path0 = sys.path[0]
    original_argv = sys.argv
    original_orig_argv = sys.orig_argv

    def run_module(module: str, *, run_name: str, alter_sys: bool) -> None:
        observed.append(
            (
                module,
                run_name,
                alter_sys,
                marker.exists(),
                sys.path[0],
                tuple(sys.argv),
                tuple(sys.orig_argv),
            )
        )

    monkeypatch.setattr(standalone_launcher.runpy, "run_module", run_module)
    try:
        result = standalone_launcher.dispatch(marker, "ruff", "probe", ["a", "b"])
    finally:
        sys.path[0] = original_path0
        sys.argv = original_argv
        sys.orig_argv = original_orig_argv

    assert result == 0
    assert observed == [
        (
            "probe",
            "__main__",
            True,
            True,
            os.getcwd(),
            ("probe", "a", "b"),
            (sys.executable, "-m", "probe", "a", "b"),
        )
    ]
    payload = json.loads(marker.read_bytes())
    assert payload["check"] == "ruff"
    assert payload["module"] == "probe"
    assert payload["arguments_sha256"] == _arguments_sha256(("a", "b"))
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600


def test_standalone_dispatch_maps_ordinary_exception_to_reserved_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "marker.json"
    original_path0 = sys.path[0]
    original_argv = sys.argv
    original_orig_argv = sys.orig_argv
    monkeypatch.setattr(
        standalone_launcher.runpy,
        "run_module",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("broken module")),
    )

    try:
        result = standalone_launcher.dispatch(marker, "ty", "probe", [])
    finally:
        sys.path[0] = original_path0
        sys.argv = original_argv
        sys.orig_argv = original_orig_argv

    assert result == 120
    assert marker.exists()


def test_standalone_dispatch_preserves_system_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "marker.json"
    original_path0 = sys.path[0]
    original_argv = sys.argv
    original_orig_argv = sys.orig_argv
    monkeypatch.setattr(
        standalone_launcher.runpy,
        "run_module",
        lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit(1)),
    )

    try:
        with pytest.raises(SystemExit) as raised:
            standalone_launcher.dispatch(marker, "bandit", "probe", [])
    finally:
        sys.path[0] = original_path0
        sys.argv = original_argv
        sys.orig_argv = original_orig_argv

    assert raised.value.code == 1
    assert marker.exists()


def test_standalone_main_maps_pre_dispatch_exception_without_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "marker.json"
    monkeypatch.setattr(
        standalone_launcher,
        "dispatch",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("blocked")),
    )

    assert standalone_launcher.main(
        [
            "--evidence",
            str(marker),
            "--check",
            "ruff",
            "--module",
            "ruff",
            "--",
        ]
    ) == 120
    assert not marker.exists()


def test_standalone_rejects_oversized_marker_before_creation(tmp_path: Path) -> None:
    marker = tmp_path / "marker.json"

    with pytest.raises(RuntimeError, match="4096"):
        standalone_launcher.publish_start(marker, "x" * 5000, "probe", [])

    assert not marker.exists()


def test_staged_launcher_is_exclusive_regular_owner_only_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with test_workspace(tmp_path) as workspace:
        launcher = _launcher()
        monkeypatch.setattr(launcher.secrets, "token_hex", lambda size: "fixed")
        staged = launcher.stage_check_launcher(workspace)
        file_status = os.stat(
            staged.path.name,
            dir_fd=workspace.descriptor,
            follow_symlinks=False,
        )

        assert stat.S_ISREG(file_status.st_mode)
        assert stat.S_IMODE(file_status.st_mode) == 0o600
        assert file_status.st_uid == getattr(os, "geteuid")()
        assert staged.digest.size == staged.path.stat().st_size
        with pytest.raises(FileExistsError):
            launcher.stage_check_launcher(workspace)


def test_launcher_command_pins_observed_repository_python(tmp_path: Path) -> None:
    prepared = prepared_repository(tmp_path, (3, 12, 11))
    invocation = CheckInvocation("annotations", ("check", "--select", "ANN", "."))
    with test_workspace(tmp_path) as workspace:
        staged = _launcher().stage_check_launcher(workspace)
        marker = workspace.workspace.path / "marker.json"

        command = _launcher().build_launcher_command(
            prepared, staged, invocation, marker
        )

    assert command == (
        "uv",
        "run",
        "--locked",
        "--python",
        str(prepared.python.executable),
        "python",
        str(staged.path),
        "--evidence",
        str(marker),
        "--check",
        "annotations",
        "--module",
        "ruff",
        "--",
        "check",
        "--select",
        "ANN",
        ".",
    )


def test_staged_launcher_mutation_stops_before_spawn(tmp_path: Path) -> None:
    prepared = prepared_repository(tmp_path, (3, 12, 11))
    runner = RecordingRunner()
    with test_workspace(tmp_path) as workspace:
        staged = _launcher().stage_check_launcher(workspace)
        staged.path.write_bytes(b"raise SystemExit(0)\n")
        observation = repository_executor.execute_invocation(
            CheckInvocation("ruff", ("check", ".")),
            prepared=prepared,
            workspace=workspace,
            launcher=staged,
            runner=runner,
            clock_ns=monotonic_clock(),
        )

    assert observation.error is not None
    assert observation.error.code == "check_start_evidence_invalid"
    assert observation.processes == ()
    assert runner.calls == []


def test_valid_marker_binds_all_fields_and_is_cleaned(tmp_path: Path) -> None:
    prepared = prepared_repository(tmp_path, (3, 12, 11))
    invocation = CheckInvocation("ty", ("check",))
    with test_workspace(tmp_path) as workspace:
        marker = workspace.workspace.path / "marker.json"
        _write_marker(marker, _marker_payload(prepared, invocation, "ty"))

        observation = _launcher().validate_start_marker(
            marker,
            workspace=workspace,
            invocation=invocation,
            module="ty",
            prepared=prepared,
        )

        assert observation.check == "ty"
        assert observation.module == "ty"
        assert observation.arguments_sha256 == _arguments_sha256(("check",))
        assert observation.python == prepared.python
        assert not marker.exists()


@pytest.mark.parametrize(
    ("mutation", "diagnostic"),
    (
        (lambda payload: payload.update(extra=True), "unknown"),
        (lambda payload: payload.update(schema_version=2), "schema"),
        (lambda payload: payload.update(check="ruff"), "check"),
        (lambda payload: payload.update(module="ruff"), "module"),
        (lambda payload: payload.update(arguments_sha256="0" * 64), "argument"),
        (
            lambda payload: payload["python"].update(implementation="pypy"),
            "Python",
        ),
        (lambda payload: payload["python"].update(version=[3, 11, 9]), "Python"),
        (
            lambda payload: payload["python"].update(executable="/other/python"),
            "Python",
        ),
    ),
)
def test_marker_rejects_unknown_or_contradictory_fields(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
    diagnostic: str,
) -> None:
    prepared = prepared_repository(tmp_path, (3, 12, 11))
    invocation = CheckInvocation("ty", ("check",))
    payload = _marker_payload(prepared, invocation, "ty")
    mutation(payload)
    with test_workspace(tmp_path) as workspace:
        marker = workspace.workspace.path / "marker.json"
        _write_marker(marker, payload)

        with pytest.raises(OSError, match=diagnostic):
            _launcher().validate_start_marker(
                marker,
                workspace=workspace,
                invocation=invocation,
                module="ty",
                prepared=prepared,
            )


@pytest.mark.parametrize(
    ("remove", "diagnostic"),
    (
        (lambda payload: payload.pop("module"), "missing fields"),
        (lambda payload: payload["python"].pop("executable"), "Python fields"),
    ),
)
def test_marker_rejects_missing_outer_and_nested_fields(
    tmp_path: Path,
    remove: Callable[[dict[str, Any]], object],
    diagnostic: str,
) -> None:
    prepared = prepared_repository(tmp_path, (3, 12, 11))
    invocation = CheckInvocation("ty", ("check",))
    payload = _marker_payload(prepared, invocation, "ty")
    removed = remove(payload)
    assert removed is not None
    with test_workspace(tmp_path) as workspace:
        marker = workspace.workspace.path / "marker.json"
        _write_marker(marker, payload)

        with pytest.raises(OSError, match=diagnostic):
            _launcher().validate_start_marker(
                marker,
                workspace=workspace,
                invocation=invocation,
                module="ty",
                prepared=prepared,
            )


def test_marker_rejects_nesting_depth_nine(tmp_path: Path) -> None:
    prepared = prepared_repository(tmp_path, (3, 12, 11))
    invocation = CheckInvocation("ruff", ("check", "."))
    with test_workspace(tmp_path) as workspace:
        marker = workspace.workspace.path / "nested.json"
        marker.write_bytes(b"[" * 9 + b"0" + b"]" * 9)
        marker.chmod(0o600)

        with pytest.raises(OSError, match="nesting exceeds the 8-level limit"):
            _launcher().validate_start_marker(
                marker,
                workspace=workspace,
                invocation=invocation,
                module="ruff",
                prepared=prepared,
            )


def test_marker_rejects_duplicate_keys_and_4097_bytes(tmp_path: Path) -> None:
    prepared = prepared_repository(tmp_path, (3, 12, 11))
    invocation = CheckInvocation("ruff", ("check", "."))
    with test_workspace(tmp_path) as workspace:
        duplicate = workspace.workspace.path / "duplicate.json"
        content = json.dumps(_marker_payload(prepared, invocation, "ruff"))
        duplicate.write_text(content[:-1] + ',"check":"ruff"}', encoding="utf-8")
        os.chmod(duplicate, 0o600)
        with pytest.raises(OSError, match="duplicate"):
            _launcher().validate_start_marker(
                duplicate,
                workspace=workspace,
                invocation=invocation,
                module="ruff",
                prepared=prepared,
            )

        oversized = workspace.workspace.path / "oversized.json"
        oversized.write_bytes(b" " * 4097)
        os.chmod(oversized, 0o600)
        with pytest.raises(OSError, match="4096"):
            _launcher().validate_start_marker(
                oversized,
                workspace=workspace,
                invocation=invocation,
                module="ruff",
                prepared=prepared,
            )


def test_marker_rejects_symlink_and_broad_permissions(tmp_path: Path) -> None:
    prepared = prepared_repository(tmp_path, (3, 12, 11))
    invocation = CheckInvocation("ruff", ("check", "."))
    with test_workspace(tmp_path) as workspace:
        target = workspace.workspace.path / "target.json"
        _write_marker(target, _marker_payload(prepared, invocation, "ruff"))
        symlink = workspace.workspace.path / "symlink.json"
        symlink.symlink_to(target.name)
        with pytest.raises(OSError, match="regular|symbolic|marker"):
            _launcher().validate_start_marker(
                symlink,
                workspace=workspace,
                invocation=invocation,
                module="ruff",
                prepared=prepared,
            )

        broad = workspace.workspace.path / "broad.json"
        _write_marker(broad, _marker_payload(prepared, invocation, "ruff"))
        broad.chmod(0o644)
        with pytest.raises(OSError, match="0600"):
            _launcher().validate_start_marker(
                broad,
                workspace=workspace,
                invocation=invocation,
                module="ruff",
                prepared=prepared,
            )


def test_marker_rejects_wrong_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = prepared_repository(tmp_path, (3, 12, 11))
    invocation = CheckInvocation("ruff", ("check", "."))
    with test_workspace(tmp_path) as workspace:
        marker = workspace.workspace.path / "wrong-owner.json"
        _write_marker(marker, _marker_payload(prepared, invocation, "ruff"))
        actual_owner = marker.stat(follow_symlinks=False).st_uid
        launcher = _launcher()
        marker_os = ModuleType("marker_os")
        marker_os.__dict__.update(os.__dict__)
        setattr(marker_os, "geteuid", lambda: actual_owner + 1)
        monkeypatch.setattr(launcher, "os", marker_os)

        with pytest.raises(OSError, match="owner"):
            launcher.validate_start_marker(
                marker,
                workspace=workspace,
                invocation=invocation,
                module="ruff",
                prepared=prepared,
            )


def test_marker_rejects_hard_link_count_two(tmp_path: Path) -> None:
    prepared = prepared_repository(tmp_path, (3, 12, 11))
    invocation = CheckInvocation("ruff", ("check", "."))
    with test_workspace(tmp_path) as workspace:
        marker = workspace.workspace.path / "linked.json"
        other_link = workspace.workspace.path / "other-link.json"
        _write_marker(marker, _marker_payload(prepared, invocation, "ruff"))
        os.link(marker, other_link)
        assert marker.stat(follow_symlinks=False).st_nlink == 2

        with pytest.raises(OSError, match="link count"):
            _launcher().validate_start_marker(
                marker,
                workspace=workspace,
                invocation=invocation,
                module="ruff",
                prepared=prepared,
            )


def test_marker_open_closes_descriptor_when_inheritability_setup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepared_repository(tmp_path, (3, 12, 11))
    invocation = CheckInvocation("ruff", ("check", "."))
    with test_workspace(tmp_path) as workspace:
        marker = workspace.workspace.path / "marker.json"
        _write_marker(marker, _marker_payload(prepared, invocation, "ruff"))
        launcher = _launcher()
        original_close = launcher.os.close
        closed: list[int] = []

        def close(descriptor: int) -> None:
            closed.append(descriptor)
            original_close(descriptor)

        with monkeypatch.context() as marker_patch:
            marker_patch.setattr(launcher.os, "close", close)
            marker_patch.setattr(
                launcher.os,
                "set_inheritable",
                lambda descriptor, inheritable: (_ for _ in ()).throw(
                    OSError("blocked")
                ),
            )
            with pytest.raises(OSError, match="blocked"):
                launcher.validate_start_marker(
                    marker,
                    workspace=workspace,
                    invocation=invocation,
                    module="ruff",
                    prepared=prepared,
                )

            assert len(closed) == 1


def test_marker_in_place_mode_mutation_during_read_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepared_repository(tmp_path, (3, 12, 11))
    invocation = CheckInvocation("ruff", ("check", "."))
    with test_workspace(tmp_path) as workspace:
        marker = workspace.workspace.path / "marker.json"
        _write_marker(marker, _marker_payload(prepared, invocation, "ruff"))
        launcher = _launcher()
        original_read = launcher.os.read
        mutated = False

        def mutate_mode_after_read(descriptor: int, size: int) -> bytes:
            nonlocal mutated
            chunk = original_read(descriptor, size)
            if chunk and not mutated:
                mutated = True
                os.fchmod(descriptor, 0o640)
            return chunk

        monkeypatch.setattr(launcher.os, "read", mutate_mode_after_read)
        with pytest.raises(OSError, match="changed|metadata"):
            launcher.validate_start_marker(
                marker,
                workspace=workspace,
                invocation=invocation,
                module="ruff",
                prepared=prepared,
            )
        assert mutated


def test_marker_in_place_size_mutation_during_read_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepared_repository(tmp_path, (3, 12, 11))
    invocation = CheckInvocation("ruff", ("check", "."))
    with test_workspace(tmp_path) as workspace:
        marker = workspace.workspace.path / "marker.json"
        _write_marker(marker, _marker_payload(prepared, invocation, "ruff"))
        launcher = _launcher()
        original_read = launcher.os.read
        mutated = False

        def mutate_size_after_read(descriptor: int, size: int) -> bytes:
            nonlocal mutated
            chunk = original_read(descriptor, size)
            if chunk and not mutated:
                mutated = True
                writer = os.open(marker, os.O_WRONLY | os.O_APPEND)
                try:
                    os.write(writer, b" ")
                    os.fsync(writer)
                finally:
                    os.close(writer)
            return chunk

        monkeypatch.setattr(launcher.os, "read", mutate_size_after_read)
        with pytest.raises(OSError, match="changed|metadata"):
            launcher.validate_start_marker(
                marker,
                workspace=workspace,
                invocation=invocation,
                module="ruff",
                prepared=prepared,
            )
        assert mutated


def test_marker_path_replacement_during_read_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepared_repository(tmp_path, (3, 12, 11))
    invocation = CheckInvocation("ruff", ("check", "."))
    with test_workspace(tmp_path) as workspace:
        marker = workspace.workspace.path / "marker.json"
        _write_marker(marker, _marker_payload(prepared, invocation, "ruff"))
        launcher = _launcher()
        original_read = launcher.os.read
        replaced = False

        def replace_after_read(descriptor: int, size: int) -> bytes:
            nonlocal replaced
            chunk = original_read(descriptor, size)
            if chunk and not replaced:
                replaced = True
                marker.rename(marker.with_suffix(".old"))
                _write_marker(marker, _marker_payload(prepared, invocation, "ruff"))
            return chunk

        monkeypatch.setattr(launcher.os, "read", replace_after_read)
        with pytest.raises(OSError, match="changed|identity"):
            launcher.validate_start_marker(
                marker,
                workspace=workspace,
                invocation=invocation,
                module="ruff",
                prepared=prepared,
            )


@pytest.mark.parametrize("returncode", (0, 1, 2, 120, -9))
def test_outer_uv_exit_without_marker_is_invalid_start_evidence(
    tmp_path: Path,
    returncode: int,
) -> None:
    prepared = prepared_repository(tmp_path, (3, 12, 11))
    runner = RecordingRunner(returncodes=(returncode,))

    with test_workspace(tmp_path) as workspace:
        launcher = _launcher().stage_check_launcher(workspace)
        observation = repository_executor.execute_invocation(
            CheckInvocation("ruff", ("check", ".")),
            prepared=prepared,
            workspace=workspace,
            launcher=launcher,
            runner=runner,
            clock_ns=monotonic_clock(),
        )
        marker = Path(runner.calls[0].command[runner.calls[0].command.index("--evidence") + 1])
        assert not marker.exists()

    assert observation.start is None
    assert observation.execution_environment is None
    assert observation.analysis_python_authority is None
    assert len(observation.processes) == 1
    assert observation.processes[0].returncode == returncode
    assert observation.error is not None
    assert observation.error.code == "check_start_evidence_invalid"


def test_valid_marker_and_exit_one_is_a_completed_finding(tmp_path: Path) -> None:
    prepared = prepared_repository(tmp_path, (3, 12, 11))
    runner = launcher_aware_runner(returncode=1, publish_valid_marker=True)

    with test_workspace(tmp_path) as workspace:
        launcher = _launcher().stage_check_launcher(workspace)
        observation = repository_executor.execute_invocation(
            CheckInvocation("ty", ("check",)),
            prepared=prepared,
            workspace=workspace,
            launcher=launcher,
            runner=runner,
            clock_ns=monotonic_clock(),
        )

    assert observation.start is not None
    assert observation.error is None
    assert observation.processes[0].returncode == 1
    assert observation.execution_environment == "repository"
    assert observation.analysis_python_authority is not None


@pytest.mark.parametrize(
    ("returncode", "expected_error"),
    ((0, None), (1, None), (2, "check_execution_failed"), (120, "check_execution_failed"), (-9, "terminated_by_signal")),
)
def test_valid_marker_exit_matrix(
    tmp_path: Path,
    returncode: int,
    expected_error: str | None,
) -> None:
    prepared = prepared_repository(tmp_path, (3, 12, 11))
    runner = launcher_aware_runner(
        returncode=returncode,
        publish_valid_marker=True,
    )
    with test_workspace(tmp_path) as workspace:
        staged = _launcher().stage_check_launcher(workspace)
        observation = repository_executor.execute_invocation(
            CheckInvocation("bandit", ("-r", ".")),
            prepared=prepared,
            workspace=workspace,
            launcher=staged,
            runner=runner,
            clock_ns=monotonic_clock(),
        )

    assert observation.start is not None
    assert observation.execution_environment == "repository"
    assert (None if observation.error is None else observation.error.code) == expected_error
    assert observation.analysis_python_authority is None


def test_marker_is_absent_immediately_before_spawn(tmp_path: Path) -> None:
    prepared = prepared_repository(tmp_path, (3, 12, 11))
    observed_absent = False

    def inspect(call: RecordedCall) -> None:
        nonlocal observed_absent
        marker = Path(call.command[call.command.index("--evidence") + 1])
        observed_absent = not marker.exists()

    runner = RecordingRunner(on_call=inspect)
    with test_workspace(tmp_path) as workspace:
        staged = _launcher().stage_check_launcher(workspace)
        repository_executor.execute_invocation(
            CheckInvocation("ruff", ("check", ".")),
            prepared=prepared,
            workspace=workspace,
            launcher=staged,
            runner=runner,
            clock_ns=monotonic_clock(),
        )

    assert observed_absent


def test_preexisting_marker_stops_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepared_repository(tmp_path, (3, 12, 11))
    monkeypatch.setattr(repository_executor.secrets, "token_hex", lambda size: "fixed")
    runner = RecordingRunner()
    with test_workspace(tmp_path) as workspace:
        marker = workspace.workspace.path / "check-start-fixed.json"
        _write_marker(
            marker,
            _marker_payload(
                prepared,
                CheckInvocation("ruff", ("check", ".")),
                "ruff",
            ),
        )
        staged = _launcher().stage_check_launcher(workspace)
        observation = repository_executor.execute_invocation(
            CheckInvocation("ruff", ("check", ".")),
            prepared=prepared,
            workspace=workspace,
            launcher=staged,
            runner=runner,
            clock_ns=monotonic_clock(),
        )

    assert observation.error is not None
    assert observation.error.code == "check_start_evidence_invalid"
    assert observation.processes == ()
    assert runner.calls == []


@pytest.mark.parametrize("publish_valid_marker", (False, True))
def test_spawn_failure_discards_start_authority_but_retains_process(
    tmp_path: Path,
    publish_valid_marker: bool,
) -> None:
    prepared = prepared_repository(tmp_path, (3, 12, 11))
    runner = launcher_aware_runner(
        publish_valid_marker=publish_valid_marker,
        raise_on_call=1,
        exception=FileNotFoundError("uv"),
    )
    with test_workspace(tmp_path) as workspace:
        staged = _launcher().stage_check_launcher(workspace)
        observation = repository_executor.execute_invocation(
            CheckInvocation("ty", ("check",)),
            prepared=prepared,
            workspace=workspace,
            launcher=staged,
            runner=runner,
            clock_ns=monotonic_clock(),
        )
        marker = Path(runner.calls[0].command[runner.calls[0].command.index("--evidence") + 1])
        assert not marker.exists()

    assert observation.error is not None
    assert observation.error.code == "spawn_failed"
    assert observation.start is None
    assert observation.execution_environment is None
    assert observation.analysis_python_authority is None
    assert len(observation.processes) == 1
    assert observation.processes[0].spawn_error == "FileNotFoundError: uv"


def test_malformed_launcher_syntax_exits_120_without_marker(tmp_path: Path) -> None:
    launcher = Path(__file__).parents[1] / "src/pyrepo_check/_check_launcher.py"
    marker = tmp_path / "marker.json"

    completed = subprocess.run(  # nosec B603
        (sys.executable, str(launcher), "--evidence", str(marker), "--wrong"),
        check=False,
        capture_output=True,
    )

    assert completed.returncode == 120
    assert not marker.exists()


def _parity_pythons() -> tuple[str, ...]:
    candidates = [sys.executable]
    candidates.extend(
        executable
        for name in ("python3.10", "python3.11", "python3.12", "python3.13")
        if (executable := shutil.which(name)) is not None
    )
    return tuple(dict.fromkeys(candidates))


@pytest.mark.parametrize("python_executable", _parity_pythons())
@pytest.mark.parametrize(
    "arguments",
    (
        ("-q", "tests/example.py", "-k", "value"),
        ("run", "-m", "pytest", "-q", "tests/example.py"),
    ),
)
def test_launcher_matches_native_python_module_startup(
    tmp_path: Path,
    python_executable: str,
    arguments: tuple[str, ...],
) -> None:
    launcher = Path(__file__).parents[1] / "src/pyrepo_check/_check_launcher.py"
    probe = tmp_path / "startup_probe.py"
    probe.write_text(
        "import __main__\n"
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "import sys\n"
        "spec = __main__.__spec__\n"
        "payload = {\n"
        "    'path0': sys.path[0],\n"
        "    'argv': sys.argv,\n"
        "    'orig_argv': sys.orig_argv,\n"
        "    'spec': None if spec is None else {\n"
        "        'name': spec.name,\n"
        "        'parent': spec.parent,\n"
        "        'origin': spec.origin,\n"
        "        'has_location': spec.has_location,\n"
        "    },\n"
        "}\n"
        "Path(os.environ['PROBE_OUTPUT']).write_text(json.dumps(payload))\n",
        encoding="utf-8",
    )
    direct_output = tmp_path / "direct.json"
    launched_output = tmp_path / "launched.json"
    marker = tmp_path / "marker.json"
    direct_environment = {**os.environ, "PROBE_OUTPUT": str(direct_output)}
    launched_environment = {**os.environ, "PROBE_OUTPUT": str(launched_output)}

    direct = subprocess.run(  # nosec B603
        (python_executable, "-m", "startup_probe", *arguments),
        cwd=tmp_path,
        env=direct_environment,
        check=False,
        capture_output=True,
    )
    launched = subprocess.run(  # nosec B603
        (
            python_executable,
            str(launcher),
            "--evidence",
            str(marker),
            "--check",
            "pytest",
            "--module",
            "startup_probe",
            "--",
            *arguments,
        ),
        cwd=tmp_path,
        env=launched_environment,
        check=False,
        capture_output=True,
    )

    assert direct.returncode == 0, direct.stderr.decode()
    assert launched.returncode == 0, launched.stderr.decode()
    assert json.loads(launched_output.read_bytes()) == json.loads(
        direct_output.read_bytes()
    )
    marker_status = marker.stat(follow_symlinks=False)
    assert stat.S_ISREG(marker_status.st_mode)
    assert stat.S_IMODE(marker_status.st_mode) == 0o600
