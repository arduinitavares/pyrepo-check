from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import threading
from typing import Callable, TypeVar, cast

import pytest

import pyrepo_check.execution_workspace as execution_workspace

from pyrepo_check.execution_workspace import (
    create_run_workspace,
    open_verified_workspace,
    remove_run_workspace,
)


_T = TypeVar("_T")
_OS_NONBLOCK = cast(int, getattr(os, "O_NONBLOCK"))
_OS_DIRECTORY = cast(int, getattr(os, "O_DIRECTORY"))
_OS_NOFOLLOW = cast(int, getattr(os, "O_NOFOLLOW"))
_MKFIFO = cast(Callable[[Path], None], getattr(os, "mkfifo"))


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
    if not returned_promptly:
        writer = os.open(fifo, os.O_WRONLY | _OS_NONBLOCK)
        os.close(writer)
    worker.join(timeout=1)
    assert not worker.is_alive(), "FIFO evidence read could not be released"
    assert returned_promptly, "FIFO evidence read blocked instead of failing closed"
    if errors:
        raise errors[0]
    return result[0]


def test_workspace_is_exclusive_and_outside_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    workspace = create_run_workspace(repository)
    verified = open_verified_workspace(workspace)
    try:
        verified.verify("test boundary")
        assert not workspace.path.is_relative_to(repository)
        assert workspace.path.is_dir()
    finally:
        verified.close()
        assert remove_run_workspace(
            workspace,
            repository_root=repository,
            clock_ns=lambda: 0,
        ) is None


def test_workspace_cleanup_never_traverses_replaced_root(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "protected.txt"
    protected.write_text("keep", encoding="utf-8")
    workspace = create_run_workspace(repository)
    original = workspace.path.with_name(f"{workspace.path.name}-original")
    workspace.path.rename(original)
    workspace.path.symlink_to(outside, target_is_directory=True)

    observation = remove_run_workspace(
        workspace,
        repository_root=repository,
        clock_ns=lambda: 0,
    )

    assert observation is not None
    assert observation.kind == "unsafe_tree"
    assert protected.read_text(encoding="utf-8") == "keep"
def _cleanup_record(run_directory: Path) -> execution_workspace.RunWorkspace:
    return execution_workspace.RunWorkspace(
        run_directory,
        execution_workspace._directory_identity(run_directory),
        execution_workspace._directory_identity(run_directory.parent),
    )


def test_cleanup_validation_stops_after_4097_lazy_entries_without_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    sentinel = run_directory / "sentinel"
    sentinel.write_text("keep")
    sentinel_status = os.stat(sentinel)
    pulls = 0
    unlink_calls: list[str] = []
    original_stat = execution_workspace.os.stat

    class FakeEntry:
        def __init__(self, name: str) -> None:
            self.name = name

    class LazyInventory:
        def __enter__(self) -> LazyInventory:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self) -> LazyInventory:
            return self

        def __next__(self) -> FakeEntry:
            nonlocal pulls
            pulls += 1
            if pulls > 100_000:
                raise AssertionError("cleanup materialized or over-pulled lazy inventory")
            return FakeEntry(f"entry-{pulls}")

        def close(self) -> None:
            return None

    def fake_stat(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        if dir_fd is not None and os.fsdecode(path).startswith("entry-"):
            assert follow_symlinks is False
            return sentinel_status
        return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    def forbid_unlink(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
    ) -> None:
        del dir_fd
        unlink_calls.append(os.fsdecode(path))
        raise AssertionError("validation overflow must not delete entries")

    monkeypatch.setattr(execution_workspace.os, "scandir", lambda _fd: LazyInventory())
    monkeypatch.setattr(execution_workspace.os, "stat", fake_stat)
    monkeypatch.setattr(execution_workspace.os, "unlink", forbid_unlink)

    observation = execution_workspace.remove_run_workspace(
        _cleanup_record(run_directory),
        repository_root=consumer_root,
    )

    assert observation is not None
    assert observation.kind == "budget_exceeded"
    assert pulls == 4097
    assert unlink_calls == []
    assert run_directory.exists() and sentinel.exists()
    assert observation.retained_path == run_directory


def test_cleanup_rejects_unvalidated_directory_injected_between_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    sentinel = victim / "sentinel"
    sentinel.write_text("keep")
    injected_sentinel = run_directory / "victim" / sentinel.name
    victim_identity = execution_workspace._directory_identity(victim)
    original_walk = execution_workspace._walk_cleanup_tree
    original_scandir = execution_workspace.os.scandir
    traversed_victim = False
    injected = False

    def track_scandir(descriptor: int) -> execution_workspace._ScandirIterator:
        nonlocal traversed_victim
        if execution_workspace._status_identity(os.fstat(descriptor)) == victim_identity:
            traversed_victim = True
        return cast(execution_workspace._ScandirIterator, original_scandir(descriptor))

    def inject_after_validation(
        parent_descriptor: int,
        root_name: str,
        root_identity: tuple[int, int],
        *,
        budget: execution_workspace._CleanupBudget,
        delete: bool,
        manifest: execution_workspace._CleanupManifest | None = None,
    ) -> execution_workspace._CleanupManifest:
        nonlocal injected
        result = original_walk(
            parent_descriptor,
            root_name,
            root_identity,
            budget=budget,
            delete=delete,
            manifest=manifest,
        )
        if not delete and not injected:
            victim.rename(run_directory / "victim")
            injected = True
        return result

    monkeypatch.setattr(execution_workspace.os, "scandir", track_scandir)
    monkeypatch.setattr(execution_workspace, "_walk_cleanup_tree", inject_after_validation)

    observation = execution_workspace.remove_run_workspace(
        _cleanup_record(run_directory),
        repository_root=consumer_root,
    )

    assert injected
    assert injected_sentinel.exists(), "cleanup deleted an entry absent from its validation pass"
    assert not traversed_victim
    assert observation is not None
    assert observation.kind == "unsafe_tree"
    assert observation.retained_path == run_directory


def test_cleanup_rejects_leaf_substituted_between_validation_and_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    leaf = run_directory / "leaf"
    leaf.write_text("validated")
    displaced = tmp_path / "displaced-leaf"
    original_walk = execution_workspace._walk_cleanup_tree
    substituted = False

    def substitute_after_validation(
        parent_descriptor: int,
        root_name: str,
        root_identity: tuple[int, int],
        *,
        budget: execution_workspace._CleanupBudget,
        delete: bool,
        manifest: execution_workspace._CleanupManifest | None = None,
    ) -> execution_workspace._CleanupManifest:
        nonlocal substituted
        result = original_walk(
            parent_descriptor,
            root_name,
            root_identity,
            budget=budget,
            delete=delete,
            manifest=manifest,
        )
        if not delete and not substituted:
            leaf.rename(displaced)
            leaf.write_text("replacement")
            substituted = True
        return result

    monkeypatch.setattr(execution_workspace, "_walk_cleanup_tree", substitute_after_validation)

    observation = execution_workspace.remove_run_workspace(
        _cleanup_record(run_directory),
        repository_root=consumer_root,
    )

    assert substituted
    assert leaf.exists(), "cleanup unlinked a substituted leaf"
    assert leaf.read_text() == "replacement"
    assert displaced.read_text() == "validated"
    assert observation is not None
    assert observation.kind == "unsafe_tree"
    assert observation.retained_path == run_directory


@pytest.mark.parametrize("leaf_type", ("regular", "symlink", "fifo"))
def test_cleanup_quarantines_leaf_replaced_immediately_before_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    leaf_type: str,
) -> None:
    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_sentinel = outside / "sentinel"
    outside_sentinel.write_text("keep")
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    leaf = run_directory / "leaf"
    displaced = tmp_path / "displaced-leaf"

    def create_leaf(path: Path, *, replacement: bool) -> None:
        if leaf_type == "regular":
            path.write_text("replacement" if replacement else "validated")
        elif leaf_type == "symlink":
            path.symlink_to(outside_sentinel if replacement else outside)
        else:
            _MKFIFO(path)

    create_leaf(leaf, replacement=False)
    original_rename = execution_workspace.os.rename
    original_unlink = execution_workspace.os.unlink
    quarantine_destination: tuple[str, int] | None = None
    quarantine_unlinks: list[tuple[str, int | None]] = []
    swapped = False

    def swap_before_quarantine_rename(
        source: str | bytes | os.PathLike[str],
        destination: str | bytes | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal quarantine_destination, swapped
        if os.fsdecode(source) == leaf.name and src_dir_fd is not None:
            assert dst_dir_fd is not None
            quarantine_destination = (os.fsdecode(destination), dst_dir_fd)
            leaf.rename(displaced)
            create_leaf(leaf, replacement=True)
            swapped = True
        original_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    def track_unlink(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
    ) -> None:
        quarantine_unlinks.append((os.fsdecode(path), dir_fd))
        original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(execution_workspace.os, "rename", swap_before_quarantine_rename)
    monkeypatch.setattr(execution_workspace.os, "unlink", track_unlink)

    def cleanup() -> execution_workspace.CleanupObservation | None:
        return execution_workspace.remove_run_workspace(
            _cleanup_record(run_directory),
            repository_root=consumer_root,
        )
    observation = (
        _run_fifo_call_with_watchdog(cleanup, leaf)
        if leaf_type == "fifo"
        else cleanup()
    )

    assert swapped
    assert quarantine_destination is not None
    quarantine_name, quarantine_descriptor = quarantine_destination
    assert observation is not None
    assert observation.kind == "unsafe_tree"
    assert observation.retained_run_path == run_directory
    assert observation.retained_quarantine_path is not None
    quarantined_leaf = observation.retained_quarantine_path / quarantine_name
    assert quarantined_leaf.exists() or quarantined_leaf.is_symlink()
    assert displaced.exists() or displaced.is_symlink()
    assert not any(
        name == quarantine_name and descriptor == quarantine_descriptor
        for name, descriptor in quarantine_unlinks
    )
    assert outside_sentinel.read_text() == "keep"


def test_cleanup_deadline_after_quarantine_rename_retains_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / "leaf").write_text("validated")
    original_rename = execution_workspace.os.rename
    renamed = False

    def track_rename(
        source: str | bytes | os.PathLike[str],
        destination: str | bytes | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal renamed
        original_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        renamed = True

    monkeypatch.setattr(execution_workspace.os, "rename", track_rename)

    observation = execution_workspace.remove_run_workspace(
        _cleanup_record(run_directory),
        repository_root=consumer_root,
        clock_ns=lambda: 5_000_000_001 if renamed else 0,
    )

    assert observation is not None
    assert observation.kind == "budget_exceeded"
    assert observation.retained_quarantine_path is not None
    assert tuple(observation.retained_quarantine_path.iterdir())


def test_successful_leaf_cleanup_unlinks_only_from_quarantine_and_removes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_sentinel = outside / "sentinel"
    outside_sentinel.write_text("keep")
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / "regular").write_text("delete")
    (run_directory / "symlink").symlink_to(outside_sentinel)
    _MKFIFO(run_directory / "fifo")
    original_mkdir = execution_workspace.os.mkdir
    original_rename = execution_workspace.os.rename
    original_unlink = execution_workspace.os.unlink
    quarantine_name: str | None = None
    quarantine_moves: dict[str, int] = {}
    quarantine_unlinks: list[tuple[str, int | None]] = []

    def track_mkdir(
        path: str | bytes | os.PathLike[str],
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal quarantine_name
        name = os.fsdecode(path)
        if name.startswith(".pyrepo-check-quarantine-"):
            quarantine_name = name
            assert mode == 0o700
            assert dir_fd is not None
        original_mkdir(path, mode, dir_fd=dir_fd)

    def track_rename(
        source: str | bytes | os.PathLike[str],
        destination: str | bytes | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        assert os.fsdecode(source) in {"regular", "symlink", "fifo"}
        assert src_dir_fd is not None and dst_dir_fd is not None
        quarantine_status = os.fstat(dst_dir_fd)
        assert quarantine_status.st_dev == os.stat(run_directory.parent).st_dev
        assert quarantine_status.st_uid == execution_workspace._effective_uid()
        assert stat.S_IMODE(quarantine_status.st_mode) == 0o700
        assert not os.get_inheritable(dst_dir_fd)
        quarantine_moves[os.fsdecode(destination)] = dst_dir_fd
        original_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    def track_unlink(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
    ) -> None:
        name = os.fsdecode(path)
        quarantine_unlinks.append((name, dir_fd))
        assert quarantine_moves.get(name) == dir_fd
        original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(execution_workspace.os, "mkdir", track_mkdir)
    monkeypatch.setattr(execution_workspace.os, "rename", track_rename)
    monkeypatch.setattr(execution_workspace.os, "unlink", track_unlink)

    observation = _run_fifo_call_with_watchdog(
        lambda: execution_workspace.remove_run_workspace(
            _cleanup_record(run_directory),
            repository_root=consumer_root,
        ),
        run_directory / "fifo",
    )

    assert observation is None
    assert len(quarantine_moves) == 3
    assert len(quarantine_unlinks) == 3
    assert quarantine_name is not None
    assert not (tmp_path / quarantine_name).exists()
    assert not run_directory.exists()
    assert outside_sentinel.read_text() == "keep"


def test_quarantine_open_identity_failure_retains_truthful_empty_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / "leaf").write_text("keep")
    original_open = execution_workspace.os.open
    original_fstat = execution_workspace.os.fstat
    quarantine_descriptors: set[int] = set()

    def track_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        *args: int,
        **kwargs: int | None,
    ) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)
        if os.fsdecode(path).startswith(".pyrepo-check-quarantine-"):
            quarantine_descriptors.add(descriptor)
        return descriptor

    def replace_quarantine_identity(descriptor: int) -> os.stat_result:
        file_status = original_fstat(descriptor)
        if descriptor not in quarantine_descriptors:
            return file_status
        values = list(file_status)
        values[1] = file_status.st_ino + 1
        return os.stat_result(values)

    monkeypatch.setattr(execution_workspace.os, "open", track_open)
    monkeypatch.setattr(execution_workspace.os, "fstat", replace_quarantine_identity)

    observation = execution_workspace.remove_run_workspace(
        _cleanup_record(run_directory),
        repository_root=consumer_root,
    )

    assert observation is not None
    assert observation.kind == "unsafe_tree"
    assert observation.retained_run_path == run_directory
    assert observation.retained_quarantine_path is not None
    assert observation.retained_quarantine_path.parent == tmp_path
    assert observation.retained_quarantine_path.is_dir()
    assert not tuple(observation.retained_quarantine_path.iterdir())
    assert (run_directory / "leaf").read_text() == "keep"
    observation.retained_quarantine_path.rmdir()


def test_quarantine_removal_error_reports_verified_retained_quarantine_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / "leaf").write_text("delete")
    original_rmdir = execution_workspace.os.rmdir
    quarantine_rmdir_calls = 0

    def deny_quarantine_rmdir(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal quarantine_rmdir_calls
        if os.fsdecode(path).startswith(".pyrepo-check-quarantine-"):
            quarantine_rmdir_calls += 1
            raise PermissionError("quarantine removal denied")
        original_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(execution_workspace.os, "rmdir", deny_quarantine_rmdir)

    observation = execution_workspace.remove_run_workspace(
        _cleanup_record(run_directory),
        repository_root=consumer_root,
    )

    assert observation is not None
    assert observation.kind == "io_failed"
    assert observation.retained_run_path is None
    assert observation.retained_quarantine_path is not None
    assert quarantine_rmdir_calls == 1
    assert observation.retained_quarantine_path.is_dir()
    assert not tuple(observation.retained_quarantine_path.iterdir())
    original_rmdir(observation.retained_quarantine_path)


def test_quarantine_name_replacement_fails_closed_without_stale_path_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / "leaf").write_text("delete")
    original_rmdir = execution_workspace.os.rmdir
    displaced_quarantine: Path | None = None

    def swap_quarantine_during_rmdir(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal displaced_quarantine
        name = os.fsdecode(path)
        if name.startswith(".pyrepo-check-quarantine-"):
            live_quarantine = tmp_path / name
            displaced_quarantine = tmp_path / f"displaced-{name}"
            live_quarantine.rename(displaced_quarantine)
            live_quarantine.mkdir(mode=0o700)
        original_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(execution_workspace.os, "rmdir", swap_quarantine_during_rmdir)

    observation = execution_workspace.remove_run_workspace(
        _cleanup_record(run_directory),
        repository_root=consumer_root,
    )

    assert observation is not None
    assert observation.kind == "unsafe_tree"
    assert observation.retained_run_path is None
    assert observation.retained_quarantine_path is None
    assert displaced_quarantine is not None and displaced_quarantine.is_dir()
    displaced_quarantine.rmdir()


def test_cleanup_rejects_validated_entry_missing_during_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    leaf = run_directory / "leaf"
    leaf.write_text("validated")
    displaced = tmp_path / "displaced-leaf"
    original_walk = execution_workspace._walk_cleanup_tree
    removed = False

    def remove_after_validation(
        parent_descriptor: int,
        root_name: str,
        root_identity: tuple[int, int],
        *,
        budget: execution_workspace._CleanupBudget,
        delete: bool,
        manifest: execution_workspace._CleanupManifest | None = None,
    ) -> execution_workspace._CleanupManifest:
        nonlocal removed
        result = original_walk(
            parent_descriptor,
            root_name,
            root_identity,
            budget=budget,
            delete=delete,
            manifest=manifest,
        )
        if not delete and not removed:
            leaf.rename(displaced)
            removed = True
        return result

    monkeypatch.setattr(execution_workspace, "_walk_cleanup_tree", remove_after_validation)

    observation = execution_workspace.remove_run_workspace(
        _cleanup_record(run_directory),
        repository_root=consumer_root,
    )

    assert removed
    assert run_directory.exists(), "cleanup removed a root with a missing manifest entry"
    assert displaced.read_text() == "validated"
    assert observation is not None
    assert observation.kind == "unsafe_tree"
    assert observation.retained_path == run_directory


def test_cleanup_validation_manifest_accepts_exactly_4096_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    sentinel = run_directory / "sentinel"
    sentinel.write_text("keep")
    sentinel_status = os.stat(sentinel)
    original_stat = execution_workspace.os.stat
    pulls = 0

    class FakeEntry:
        def __init__(self, name: str) -> None:
            self.name = name

    class ExactInventory:
        def __iter__(self) -> ExactInventory:
            return self

        def __next__(self) -> FakeEntry:
            nonlocal pulls
            if pulls == 4096:
                raise StopIteration
            pulls += 1
            return FakeEntry(f"entry-{pulls}")

        def close(self) -> None:
            return None

    def fake_stat(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        if dir_fd is not None and os.fsdecode(path).startswith("entry-"):
            assert follow_symlinks is False
            return sentinel_status
        return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    parent_descriptor = execution_workspace._open_verified_parent(
        _cleanup_record(run_directory)
    )
    monkeypatch.setattr(execution_workspace.os, "scandir", lambda _fd: ExactInventory())
    monkeypatch.setattr(execution_workspace.os, "stat", fake_stat)
    try:
        manifest = execution_workspace._walk_cleanup_tree(
            parent_descriptor,
            run_directory.name,
            execution_workspace._directory_identity(run_directory),
            budget=execution_workspace._CleanupBudget(0, lambda: 0),
            delete=False,
        )
    finally:
        os.close(parent_descriptor)

    assert pulls == 4096
    assert manifest is not None
    assert len(manifest.entries) == 4096
    manifest_key, manifest_entry = next(iter(manifest.entries.items()))
    with pytest.raises(TypeError):
        cast(
            dict[execution_workspace.CleanupManifestKey, execution_workspace._CleanupManifestEntry],
            manifest.entries,
        )[manifest_key] = manifest_entry


@pytest.mark.parametrize(("depth", "removed"), ((64, True), (65, False)))
def test_cleanup_depth_boundary_is_exact(
    tmp_path: Path,
    depth: int,
    removed: bool,
) -> None:
    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    run_directory = tmp_path / f"run-{depth}"
    run_directory.mkdir()
    nested = run_directory
    for index in range(depth):
        nested = nested / f"d{index}"
        nested.mkdir()

    observation = execution_workspace.remove_run_workspace(
        _cleanup_record(run_directory),
        repository_root=consumer_root,
    )

    assert (not run_directory.exists()) is removed
    if removed:
        assert observation is None
    else:
        assert observation is not None
        assert observation.kind == "budget_exceeded"
        assert observation.retained_path == run_directory


def test_cleanup_unlinks_symlink_without_touching_outside_and_never_uses_rmtree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("keep")
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / "outside-link").symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(
        shutil,
        "rmtree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("production cleanup must not call shutil.rmtree")
        ),
    )

    observation = execution_workspace.remove_run_workspace(
        _cleanup_record(run_directory),
        repository_root=consumer_root,
    )

    assert observation is None
    assert not run_directory.exists()
    assert sentinel.read_text() == "keep"


def test_cleanup_final_root_removal_is_descriptor_relative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / "leaf").write_text("delete")
    original_rmdir = execution_workspace.os.rmdir
    final_calls: list[tuple[str, int | None]] = []

    def track_rmdir(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
    ) -> None:
        name = os.fsdecode(path)
        if name == run_directory.name:
            final_calls.append((name, dir_fd))
            assert dir_fd is not None
        original_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(execution_workspace.os, "rmdir", track_rmdir)

    observation = execution_workspace.remove_run_workspace(
        _cleanup_record(run_directory),
        repository_root=consumer_root,
    )

    assert observation is None
    assert len(final_calls) == 1
    assert final_calls[0][0] == "run"
    assert final_calls[0][1] is not None


def test_cleanup_directory_opens_use_exact_secure_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    run_directory = tmp_path / "run"
    child = run_directory / "child"
    child.mkdir(parents=True)
    original_open = execution_workspace.os.open
    directory_open_flags: list[int] = []

    def track_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        *args: int,
        **kwargs: int | None,
    ) -> int:
        directory_open_flags.append(flags)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(execution_workspace.os, "open", track_open)

    observation = execution_workspace.remove_run_workspace(
        _cleanup_record(run_directory),
        repository_root=consumer_root,
    )

    expected = os.O_RDONLY | _OS_DIRECTORY | _OS_NOFOLLOW | _OS_NONBLOCK
    assert observation is None
    assert directory_open_flags
    assert all(flags == expected for flags in directory_open_flags)


def test_cleanup_rejects_cross_device_child_and_retains_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    run_directory = tmp_path / "run"
    child = run_directory / "child"
    child.mkdir(parents=True)
    original_open = execution_workspace.os.open
    original_fstat = execution_workspace.os.fstat
    child_descriptors: set[int] = set()

    def track_child_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        *args: int,
        **kwargs: int | None,
    ) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)
        if os.fsdecode(path) == "child" and kwargs.get("dir_fd") is not None:
            child_descriptors.add(descriptor)
        return descriptor

    def cross_device_fstat(descriptor: int) -> os.stat_result:
        file_status = original_fstat(descriptor)
        if descriptor not in child_descriptors:
            return file_status
        values = list(file_status)
        values[2] = file_status.st_dev + 1
        return os.stat_result(values)

    monkeypatch.setattr(execution_workspace.os, "open", track_child_open)
    monkeypatch.setattr(execution_workspace.os, "fstat", cross_device_fstat)

    observation = execution_workspace.remove_run_workspace(
        _cleanup_record(run_directory),
        repository_root=consumer_root,
    )

    assert observation is not None
    assert observation.kind == "unsafe_tree"
    assert observation.message == "cross-device directory rejected: child"
    assert observation.retained_path == run_directory
    assert child.exists()


def test_cleanup_rejects_child_substitution_between_stat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    run_directory = tmp_path / "run"
    child = run_directory / "child"
    child.mkdir(parents=True)
    displaced = tmp_path / "displaced-child"
    replacement_sentinel = child / "replacement"
    original_open = execution_workspace.os.open
    substituted = False

    def substitute_before_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        *args: int,
        **kwargs: int | None,
    ) -> int:
        nonlocal substituted
        if (
            not substituted
            and os.fsdecode(path) == "child"
            and kwargs.get("dir_fd") is not None
        ):
            child.rename(displaced)
            child.mkdir()
            replacement_sentinel.write_text("keep")
            substituted = True
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(execution_workspace.os, "open", substitute_before_open)

    observation = execution_workspace.remove_run_workspace(
        _cleanup_record(run_directory),
        repository_root=consumer_root,
    )

    assert observation is not None
    assert observation.kind == "unsafe_tree"
    assert observation.message == "directory identity mismatch: child"
    assert observation.retained_path == run_directory
    assert replacement_sentinel.read_text() == "keep"
    assert displaced.exists()


def test_cleanup_rejects_child_substitution_before_rmdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    run_directory = tmp_path / "run"
    child = run_directory / "child"
    child.mkdir(parents=True)
    displaced = tmp_path / "displaced-child"
    replacement_sentinel = child / "replacement"
    original_stat = execution_workspace.os.stat
    child_stats = 0

    def substitute_on_preremoval_stat(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal child_stats
        if os.fsdecode(path) == "child" and dir_fd is not None:
            child_stats += 1
            if child_stats == 3:
                child.rename(displaced)
                child.mkdir()
                replacement_sentinel.write_text("keep")
        return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(execution_workspace.os, "stat", substitute_on_preremoval_stat)

    observation = execution_workspace.remove_run_workspace(
        _cleanup_record(run_directory),
        repository_root=consumer_root,
    )

    assert child_stats == 3
    assert observation is not None
    assert observation.kind == "unsafe_tree"
    assert observation.message == "directory identity mismatch before removal: child"
    assert observation.retained_path == run_directory
    assert replacement_sentinel.read_text() == "keep"
    assert displaced.exists()


def test_cleanup_reports_child_swap_during_rmdir_as_unsafe_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    run_directory = tmp_path / "run"
    child = run_directory / "child"
    child.mkdir(parents=True)
    displaced = tmp_path / "displaced-child"
    original_rmdir = execution_workspace.os.rmdir
    swapped = False

    def swap_during_rmdir(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if os.fsdecode(path) == "child" and not swapped:
            child.rename(displaced)
            child.mkdir()
            swapped = True
        original_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(execution_workspace.os, "rmdir", swap_during_rmdir)

    observation = execution_workspace.remove_run_workspace(
        _cleanup_record(run_directory),
        repository_root=consumer_root,
    )

    assert swapped
    assert observation is not None
    assert observation.kind == "unsafe_tree"
    assert observation.message == "directory remained linked after removal: child"
    assert displaced.exists()


def test_cleanup_reports_root_swap_during_rmdir_as_unsafe_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    displaced = tmp_path / "displaced-run"
    original_rmdir = execution_workspace.os.rmdir
    swapped = False

    def swap_during_rmdir(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if os.fsdecode(path) == run_directory.name and not swapped:
            run_directory.rename(displaced)
            run_directory.mkdir()
            swapped = True
        original_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(execution_workspace.os, "rmdir", swap_during_rmdir)

    observation = execution_workspace.remove_run_workspace(
        _cleanup_record(run_directory),
        repository_root=consumer_root,
    )

    assert swapped
    assert observation is not None
    assert observation.kind == "unsafe_tree"
    assert observation.message == f"directory remained linked after removal: {run_directory.name}"
    assert displaced.exists()


def test_parent_rename_prevents_stale_retained_path_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    parent = tmp_path / "parent"
    run_directory = parent / "run"
    run_directory.mkdir(parents=True)
    moved_parent = tmp_path / "moved-parent"

    def rename_parent_then_fail(*_args: object, **_kwargs: object) -> None:
        parent.rename(moved_parent)
        raise execution_workspace._CleanupFailure("unsafe_tree", "synthetic cleanup failure")

    monkeypatch.setattr(execution_workspace, "_walk_cleanup_tree", rename_parent_then_fail)

    observation = execution_workspace.remove_run_workspace(
        _cleanup_record(run_directory),
        repository_root=consumer_root,
    )

    assert observation is not None
    assert observation.retained_path is None
    assert (moved_parent / "run").exists()


def test_cleanup_concurrent_growth_reports_enotempty_and_retains_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    run_directory = tmp_path / "run"
    child = run_directory / "child"
    child.mkdir(parents=True)
    original_rmdir = execution_workspace.os.rmdir
    grew = False

    def grow_before_child_rmdir(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal grew
        if os.fsdecode(path) == "child" and not grew:
            (child / "late").write_text("keep")
            grew = True
        original_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(execution_workspace.os, "rmdir", grow_before_child_rmdir)

    observation = execution_workspace.remove_run_workspace(
        _cleanup_record(run_directory),
        repository_root=consumer_root,
    )

    assert observation is not None
    assert observation.kind == "io_failed"
    assert "Directory not empty" in observation.message
    assert observation.retained_path == run_directory
    assert (child / "late").read_text() == "keep"
