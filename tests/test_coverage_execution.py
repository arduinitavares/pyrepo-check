from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import threading
from typing import Callable, TypeVar, cast

import pytest

from pyrepo_check import coverage_execution
from pyrepo_check import filesystem
from pyrepo_check.artifact_safety import FileDigest, RegularFileCopy
from tests.support import symlink_or_skip


_T = TypeVar("_T")
_HAS_POSIX_FIFO = callable(getattr(os, "mkfifo", None))
_POSIX_FIFO = pytest.mark.skipif(
    not _HAS_POSIX_FIFO,
    reason="exercises POSIX FIFO behavior; native Windows file coverage is separate",
)
_POSIX_RENAME_RACE = pytest.mark.skipif(
    os.name == "nt",
    reason="requires replacing an open POSIX directory; Windows handle sharing blocks that race",
)
_MKFIFO = cast(Callable[[Path], None], getattr(os, "mkfifo", None))


def _run_fifo_call_with_watchdog(call: Callable[[], _T], fifo: Path) -> _T:
    result: list[_T] = []
    errors: list[BaseException] = []
    completed = threading.Event()

    def invoke() -> None:
        try:
            result.append(call())
        except BaseException as error:
            errors.append(error)
        finally:
            completed.set()

    worker = threading.Thread(target=invoke)
    worker.start()
    returned_promptly = completed.wait(timeout=0.25)
    worker.join(timeout=1)
    assert returned_promptly, "coverage FIFO inspection blocked"
    assert not worker.is_alive(), "coverage FIFO inspection did not terminate"
    if errors:
        raise errors[0]
    return result[0]


def _open_directory(directory: Path) -> int:
    return filesystem.open(
        directory,
        os.O_RDONLY
        | filesystem.O_DIRECTORY
        | filesystem.O_NOFOLLOW
        | filesystem.O_NONBLOCK,
    )


def _prepare_coverage_data(run_directory: Path) -> coverage_execution.CoverageDataSnapshot:
    descriptor = _open_directory(run_directory)
    try:
        return coverage_execution.prepare_coverage_data_snapshot(
            run_directory,
            run_descriptor=descriptor,
        )
    finally:
        os.close(descriptor)


def test_coverage_data_base_is_copied_to_an_immutable_descriptor_held_snapshot(
    tmp_path: Path,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / ".coverage").write_bytes(b"sqlite-evidence")

    snapshot = _prepare_coverage_data(run_directory)
    try:
        assert snapshot.data_path == run_directory / "report-input" / "coverage-data"
        assert snapshot.data_path.read_bytes() == b"sqlite-evidence"
        assert snapshot.digest.size == len(b"sqlite-evidence")
        assert len(snapshot.digest.sha256) == 64
        assert all(not isinstance(value, bytes) for value in vars(snapshot).values())
        coverage_execution.verify_coverage_data_snapshot(snapshot)
    finally:
        snapshot.close()


@pytest.mark.parametrize(
    "base_kind",
    (
        "missing",
        "symlink",
        pytest.param("fifo", marks=_POSIX_FIFO),
        "oversized",
        pytest.param(
            "unreadable",
            marks=pytest.mark.skipif(
                os.name == "nt",
                reason="POSIX permission-bit fixture; Windows ACL checks have native coverage",
            ),
        ),
    ),
)
def test_coverage_data_missing_or_unusable_base_fails_closed(
    tmp_path: Path,
    base_kind: str,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    base = run_directory / ".coverage"
    if base_kind == "symlink":
        target = tmp_path / "outside.coverage"
        target.write_bytes(b"outside")
        symlink_or_skip(base, target)
    elif base_kind == "fifo":
        _MKFIFO(base)
    elif base_kind == "oversized":
        with base.open("wb") as file:
            file.truncate(coverage_execution.MAX_COVERAGE_DATA_BYTES + 1)
    elif base_kind == "unreadable":
        base.write_bytes(b"private")
        base.chmod(0)

    def call() -> coverage_execution.CoverageDataSnapshot:
        return _prepare_coverage_data(run_directory)

    try:
        with pytest.raises(coverage_execution.CoverageDataError) as raised:
            if base_kind == "fifo":
                _run_fifo_call_with_watchdog(call, base)
            else:
                call()
    finally:
        if base_kind == "unreadable":
            base.chmod(0o600)
    assert raised.value.code == "data_missing"


@pytest.mark.parametrize(
    ("entry_name", "rejected"),
    (
        pytest.param(
            ".coverage.",
            True,
            marks=pytest.mark.skipif(
                os.name == "nt",
                reason="Windows normalizes a trailing dot, so this fixture overwrites .coverage",
            ),
        ),
        (".coverage.worker", True),
        (".coveragex.worker", False),
        ("coverage.worker", False),
    ),
)
def test_coverage_data_scans_only_literal_run_root_shards(
    tmp_path: Path,
    entry_name: str,
    rejected: bool,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / ".coverage").write_bytes(b"base")
    (run_directory / entry_name).write_bytes(b"entry")

    if rejected:
        with pytest.raises(coverage_execution.CoverageDataError) as raised:
            _prepare_coverage_data(run_directory)
        assert raised.value.code == "unexpected_parallel_data"
    else:
        snapshot = _prepare_coverage_data(run_directory)
        snapshot.close()


def test_coverage_data_scan_does_not_inspect_consumer_siblings(tmp_path: Path) -> None:
    consumer_shard = tmp_path / ".coverage.worker"
    consumer_shard.write_bytes(b"consumer-owned")
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / ".coverage").write_bytes(b"base")

    snapshot = _prepare_coverage_data(run_directory)
    snapshot.close()

    assert consumer_shard.read_bytes() == b"consumer-owned"


def test_coverage_data_rejects_report_destination_collision(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / ".coverage").write_bytes(b"base")
    (run_directory / "report-input").mkdir()

    with pytest.raises(coverage_execution.CoverageDataError) as raised:
        _prepare_coverage_data(run_directory)

    assert raised.value.code == "unexpected_parallel_data"

def test_coverage_data_copy_or_source_change_is_unexpected_parallel_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    base = run_directory / ".coverage"
    base.write_bytes(b"original")

    def mutate_instead_of_copy(*_args: object, **_kwargs: object) -> object:
        base.write_bytes(b"changed!")
        raise coverage_execution._DigestMismatchError("coverage-data digest mismatch")

    monkeypatch.setattr(
        coverage_execution,
        "copy_regular_file",
        mutate_instead_of_copy,
    )

    with pytest.raises(coverage_execution.CoverageDataError) as raised:
        _prepare_coverage_data(run_directory)

    assert raised.value.code == "unexpected_parallel_data"


def test_coverage_data_rejects_same_byte_snapshot_replacement_at_copy_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / ".coverage").write_bytes(b"same-bytes")
    real_copy = coverage_execution.copy_regular_file

    def replace_after_copy(
        source_path: Path,
        destination_path: Path,
        *,
        max_bytes: int,
        source_dir_fd: int | None = None,
        destination_dir_fd: int | None = None,
    ) -> object:
        copied = real_copy(
            source_path,
            destination_path,
            max_bytes=max_bytes,
            source_dir_fd=source_dir_fd,
            destination_dir_fd=destination_dir_fd,
        )
        snapshot = run_directory / "report-input" / "coverage-data"
        replacement = snapshot.with_name("coverage-data-replacement")
        replacement.write_bytes(b"same-bytes")
        replacement.replace(snapshot)
        return copied

    monkeypatch.setattr(
        coverage_execution,
        "copy_regular_file",
        replace_after_copy,
    )

    with pytest.raises(coverage_execution.CoverageDataError) as raised:
        _prepare_coverage_data(run_directory)

    assert raised.value.code == "unexpected_parallel_data"


@pytest.mark.parametrize(
    ("namespace", "entry_name"),
    (
        ("run", ".coverage.worker"),
        ("snapshot", "coverage-data.worker"),
        ("snapshot", "coverage-data."),
    ),
)
def test_coverage_data_rejects_shards_created_after_snapshot(
    tmp_path: Path,
    namespace: str,
    entry_name: str,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / ".coverage").write_bytes(b"base")
    snapshot = _prepare_coverage_data(run_directory)
    try:
        parent = run_directory if namespace == "run" else snapshot.data_path.parent
        (parent / entry_name).write_bytes(b"shard")

        with pytest.raises(coverage_execution.CoverageDataError) as raised:
            coverage_execution.verify_coverage_data_snapshot(snapshot)
    finally:
        snapshot.close()

    assert raised.value.code == "unexpected_parallel_data"


@pytest.mark.parametrize("target", ("original", "snapshot"))
def test_coverage_data_rejects_original_or_snapshot_mutation(
    tmp_path: Path,
    target: str,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    original = run_directory / ".coverage"
    original.write_bytes(b"original")
    snapshot = _prepare_coverage_data(run_directory)
    try:
        path = original if target == "original" else snapshot.data_path
        path.write_bytes(b"changed!")

        with pytest.raises(coverage_execution.CoverageDataError) as raised:
            coverage_execution.verify_coverage_data_snapshot(snapshot)
    finally:
        snapshot.close()

    assert raised.value.code == "unexpected_parallel_data"


@pytest.mark.parametrize("target", ("original", "snapshot"))
def test_coverage_data_rejects_same_byte_original_or_snapshot_replacement(
    tmp_path: Path,
    target: str,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    original = run_directory / ".coverage"
    original.write_bytes(b"same-bytes")
    snapshot = _prepare_coverage_data(run_directory)
    try:
        path = original if target == "original" else snapshot.data_path
        replacement = path.with_name(f"{path.name}-replacement")
        replacement.write_bytes(b"same-bytes")
        replacement.replace(path)

        with pytest.raises(coverage_execution.CoverageDataError) as raised:
            coverage_execution.verify_coverage_data_snapshot(snapshot)
    finally:
        snapshot.close()

    assert raised.value.code == "unexpected_parallel_data"


def test_coverage_data_preparation_cleanup_attempts_both_closes_and_preserves_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / ".coverage").write_bytes(b"base")
    run_descriptor = _open_directory(run_directory)
    duplicated: list[int] = []
    report_descriptors: list[int] = []
    close_attempts: list[int] = []
    original_dup = coverage_execution.os.dup
    original_open = coverage_execution.fs.open
    original_close = coverage_execution.os.close

    def tracked_dup(descriptor: int) -> int:
        duplicate = original_dup(descriptor)
        duplicated.append(duplicate)
        return duplicate

    def tracked_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        *args: int,
        **kwargs: int | None,
    ) -> int:
        descriptor = original_open(os.fsdecode(path), flags, *args, **kwargs)
        if os.fsdecode(path) == "report-input":
            report_descriptors.append(descriptor)
        return descriptor

    def fail_report_close(descriptor: int) -> None:
        close_attempts.append(descriptor)
        if report_descriptors and descriptor == report_descriptors[0]:
            raise PermissionError("report descriptor close denied")
        original_close(descriptor)

    def fail_verification(_snapshot: object) -> None:
        raise coverage_execution.CoverageDataError(
            "unexpected_parallel_data",
            "initiating verification failure",
        )

    monkeypatch.setattr(coverage_execution.os, "dup", tracked_dup)
    monkeypatch.setattr(coverage_execution.fs, "open", tracked_open)
    monkeypatch.setattr(coverage_execution.os, "close", fail_report_close)
    monkeypatch.setattr(coverage_execution, "verify_coverage_data_snapshot", fail_verification)
    try:
        with pytest.raises(
            coverage_execution.CoverageDataError,
            match="initiating verification failure",
        ):
            coverage_execution.prepare_coverage_data_snapshot(
                run_directory,
                run_descriptor=run_descriptor,
            )

        assert report_descriptors
        assert duplicated
        owned_attempts = [
            descriptor
            for descriptor in close_attempts
            if descriptor in {report_descriptors[0], duplicated[0]}
        ]
        assert owned_attempts[-2:] == [report_descriptors[0], duplicated[0]]
    finally:
        for descriptor in (*report_descriptors, *duplicated, run_descriptor):
            try:
                original_close(descriptor)
            except OSError:
                pass


@_POSIX_RENAME_RACE
def test_coverage_data_rejects_report_directory_substitution(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / ".coverage").write_bytes(b"base")
    snapshot = _prepare_coverage_data(run_directory)
    displaced = run_directory / "displaced-report-input"
    snapshot.data_path.parent.rename(displaced)
    snapshot.data_path.parent.mkdir()
    try:
        with pytest.raises(coverage_execution.CoverageDataError) as raised:
            coverage_execution.verify_coverage_data_snapshot(snapshot)
    finally:
        snapshot.close()

    assert raised.value.code == "unexpected_parallel_data"


def test_coverage_environments_scrub_ambient_hooks_and_set_owned_paths(
    tmp_path: Path,
) -> None:
    base = {
        "PATH": "/bin",
        "PYTHONPATH": "/run/reporter",
        "COVERAGE_PROCESS_START": "ambient.toml",
        "COVERAGE_PROCESS_CONFIG": "ambient",
        "COVERAGE_FILE": "ambient.data",
        "COVERAGE_RCFILE": "ambient.rc",
        "COV_CORE_SOURCE": "ambient",
    }
    config = tmp_path / "pyproject.toml"
    run = tmp_path / "run"
    data = run / "report-input/coverage-data"

    primary = coverage_execution.coverage_environment(
        base,
        run_directory=run,
        config_path=config,
    )
    helper = coverage_execution.coverage_json_environment(
        base,
        data_path=data,
        config_path=config,
    )

    assert primary == {
        "PATH": "/bin",
        "PYTHONPATH": "/run/reporter",
        "COVERAGE_FILE": str(run / ".coverage"),
        "COVERAGE_RCFILE": str(config),
    }
    assert helper == {
        "PATH": "/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "COVERAGE_FILE": str(data),
        "COVERAGE_RCFILE": str(config),
    }


def test_coverage_commands_are_exact_and_threshold_override_is_explicit(
    tmp_path: Path,
) -> None:
    config = tmp_path / "pyproject.toml"
    run = tmp_path / "run"
    data = run / "report-input/coverage-data"
    output = run / "coverage.json"

    assert coverage_execution.coverage_primary_arguments(
        config_path=config,
        run_directory=run,
        plugin_module="_isolated_reporter",
        pytest_args=("tests", "-q"),
    ) == (
        "run",
        f"--rcfile={config}",
        f"--data-file={run / '.coverage'}",
        "-m",
        "pytest",
        "-p",
        "_isolated_reporter",
        "tests",
        "-q",
    )
    base = coverage_execution.coverage_json_command(
        python_prefix=("/repo/.venv/bin/python",),
        config_path=config,
        data_path=data,
        output_path=output,
        force_fail_under_zero=False,
    )
    assert base[-1] == "--keep-combined"
    assert coverage_execution.coverage_json_command(
        python_prefix=("/repo/.venv/bin/python",),
        config_path=config,
        data_path=data,
        output_path=output,
        force_fail_under_zero=True,
    ) == (*base, "--fail-under=0")


def test_coverage_json_destination_must_be_absent(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / ".coverage").write_bytes(b"data")
    snapshot = _prepare_coverage_data(run)
    try:
        coverage_execution.require_coverage_json_destination_absent(snapshot)
        (run / "coverage.json").write_text("{}", encoding="utf-8")
        with pytest.raises(coverage_execution.CoverageDataError) as raised:
            coverage_execution.require_coverage_json_destination_absent(snapshot)
    finally:
        snapshot.close()

    assert raised.value.code == "unexpected_parallel_data"


@pytest.mark.parametrize(
    ("artifact", "expected_state"),
    ((None, "artifact_missing"), (b"not json", "artifact_invalid"), (b'{"ok":true}', "snapshot")),
)
def test_coverage_json_snapshot_retains_only_bounded_valid_json(
    tmp_path: Path,
    artifact: bytes | None,
    expected_state: str,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / ".coverage").write_bytes(b"data")
    snapshot = _prepare_coverage_data(run)
    try:
        if artifact is not None:
            (run / "coverage.json").write_bytes(artifact)
        observation = coverage_execution.snapshot_coverage_json(snapshot)
    finally:
        snapshot.close()

    assert observation.state == expected_state
    assert observation.content == (artifact if expected_state == "snapshot" else None)


def test_coverage_json_snapshot_rejects_unsafe_leaf(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / ".coverage").write_bytes(b"data")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    symlink_or_skip(run / "coverage.json", outside)
    snapshot = _prepare_coverage_data(run)
    try:
        observation = coverage_execution.snapshot_coverage_json(snapshot)
    finally:
        snapshot.close()

    assert observation.state == "artifact_invalid"
    assert observation.content is None
    assert "unsafe or unreadable" in cast(str, observation.diagnostic)


def test_coverage_snapshot_close_surfaces_first_descriptor_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / ".coverage").write_bytes(b"data")
    snapshot = _prepare_coverage_data(run)
    real_close = coverage_execution._close_snapshot_descriptors
    monkeypatch.setattr(
        coverage_execution,
        "_close_snapshot_descriptors",
        lambda *_args: PermissionError("close denied"),
    )
    try:
        with pytest.raises(PermissionError, match="close denied"):
            snapshot.close()
    finally:
        real_close(snapshot.report_descriptor, snapshot.run_descriptor)


def test_coverage_snapshot_rejects_nonempty_report_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / ".coverage").write_bytes(b"data")
    monkeypatch.setattr(coverage_execution, "_directory_is_empty", lambda _descriptor: False)

    with pytest.raises(coverage_execution.CoverageDataError) as raised:
        _prepare_coverage_data(run)

    assert raised.value.code == "unexpected_parallel_data"


def test_coverage_snapshot_rejects_copy_digest_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / ".coverage").write_bytes(b"data")
    real_copy = coverage_execution.copy_regular_file

    def mismatched_copy(
        source_path: Path,
        destination_path: Path,
        *,
        max_bytes: int,
        source_dir_fd: int | None = None,
        destination_dir_fd: int | None = None,
    ) -> RegularFileCopy:
        copied = real_copy(
            source_path,
            destination_path,
            max_bytes=max_bytes,
            source_dir_fd=source_dir_fd,
            destination_dir_fd=destination_dir_fd,
        )
        return replace(copied, digest=FileDigest(copied.digest.size, "0" * 64))

    monkeypatch.setattr(coverage_execution, "copy_regular_file", mismatched_copy)

    with pytest.raises(coverage_execution.CoverageDataError) as raised:
        _prepare_coverage_data(run)

    assert raised.value.code == "unexpected_parallel_data"


def test_coverage_snapshot_does_not_mask_programming_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / ".coverage").write_bytes(b"data")
    marker = ValueError("copy bug")
    monkeypatch.setattr(
        coverage_execution,
        "copy_regular_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(marker),
    )

    with pytest.raises(ValueError) as raised:
        _prepare_coverage_data(run)

    assert raised.value is marker


def test_coverage_destination_and_shard_scan_io_errors_are_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / ".coverage").write_bytes(b"data")
    snapshot = _prepare_coverage_data(run)
    with monkeypatch.context() as patch:
        patch.setattr(
            coverage_execution.fs,
            "stat",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("stat denied")),
        )
        with pytest.raises(coverage_execution.CoverageDataError) as raised:
            coverage_execution.require_coverage_json_destination_absent(snapshot)
    assert raised.value.code == "unexpected_parallel_data"
    with monkeypatch.context() as patch:
        patch.setattr(
            coverage_execution,
            "_find_prefixed_entry",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("scan denied")),
        )
        with pytest.raises(coverage_execution.CoverageDataError) as raised:
            coverage_execution._reject_shards(
                snapshot.run_descriptor,
                prefix=".coverage.",
                namespace="run coverage-data",
            )
    snapshot.close()
    assert raised.value.code == "unexpected_parallel_data"


def test_coverage_secure_directory_capabilities_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coverage_execution.fs, "O_NOFOLLOW", None)

    with pytest.raises(OSError, match="safe no-follow directory opening is unavailable"):
        coverage_execution._secure_directory_open_flags()
