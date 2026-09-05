"""Windows workspace cleanup bound to exclusively held deletion handles.

Every entry is opened and checked before deletion starts. The retained handles
deny rename/delete sharing, so the deletion manifest consists of the actual
objects that were inspected. Disposition operates on those handles, not names.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import os
from pathlib import Path
import secrets
import stat
import tempfile

from pyrepo_check import filesystem as fs
from pyrepo_check.execution_workspace import (
    CleanupObservation,
    RunWorkspace,
    _CleanupBudget,
    _CleanupFailure,
    _is_within,
    _status_identity,
)


@dataclass
class _HeldEntry:
    descriptor: int
    children: list[_HeldEntry] = field(default_factory=list)
    closed: bool = False

    def close(self) -> None:
        for child in self.children:
            child.close()
        if not self.closed:
            self.closed = True
            os.close(self.descriptor)


def create_workspace(repository_root: Path) -> RunWorkspace:
    base = Path(tempfile.gettempdir()).resolve(strict=True)
    if _is_within(base, repository_root.resolve()):
        raise OSError("no safe operating-system temporary directory is available")
    parent = fs.open(base, os.O_RDONLY | fs.O_DIRECTORY | fs.O_NOFOLLOW)
    try:
        parent_identity = _status_identity(os.fstat(parent))
        name = f"pyrepo-check-pytest-{secrets.token_hex(16)}"
        descriptor = fs.open(
            name,
            os.O_RDONLY | fs.O_DIRECTORY | fs.O_NOFOLLOW | os.O_CREAT | os.O_EXCL,
            0o700,
            dir_fd=parent,
        )
        try:
            fs.verify_private(descriptor)
            identity = _status_identity(os.fstat(descriptor))
            if _status_identity(fs.stat(base, follow_symlinks=False)) != parent_identity:
                raise OSError("created run directory parent identity mismatch")
            return RunWorkspace(base / name, identity, parent_identity)
        except BaseException as error:
            try:
                # Exclusive directory creation grants deletion access to this
                # exact object. No name is reopened on the failure path.
                fs.delete_open_file(descriptor)
            except OSError as cleanup_error:
                error.add_note(f"rejected workspace cleanup failed for {base / name}: {cleanup_error}")
            raise
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def _hold_tree(
    descriptor: int,
    *,
    device: int,
    budget: _CleanupBudget,
    depth: int,
) -> _HeldEntry:
    held = _HeldEntry(descriptor)
    try:
        budget.check_deadline()
        status = os.fstat(descriptor)
        if status.st_dev != device:
            raise _CleanupFailure("unsafe_tree", "cross-device cleanup entry rejected")
        # Reparse points are terminal objects, including directory junctions.
        if getattr(status, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            return held
        if stat.S_ISDIR(status.st_mode):
            with fs.scandir(descriptor) as entries:
                for entry in entries:
                    budget.check_deadline()
                    budget.observe_entry(depth=depth + 1)
                    observed = entry.stat(follow_symlinks=False)
                    child = fs.open_for_cleanup(entry.name, dir_fd=descriptor)
                    try:
                        opened = os.fstat(child)
                        if (
                            _status_identity(opened) != _status_identity(observed)
                            or stat.S_IFMT(opened.st_mode) != stat.S_IFMT(observed.st_mode)
                            or (getattr(opened, "st_file_attributes", 0)
                                ^ getattr(observed, "st_file_attributes", 0))
                            & stat.FILE_ATTRIBUTE_REPARSE_POINT
                        ):
                            raise _CleanupFailure(
                                "unsafe_tree", "cleanup entry changed after enumeration"
                            )
                    except BaseException:
                        os.close(child)
                        raise
                    held.children.append(
                        _hold_tree(child, device=device, budget=budget, depth=depth + 1)
                    )
        elif not stat.S_ISREG(status.st_mode):
            raise _CleanupFailure("unsafe_tree", "non-disk cleanup entry rejected")
        return held
    except BaseException:
        held.close()
        raise


def _delete_tree(held: _HeldEntry, budget: _CleanupBudget) -> None:
    for child in held.children:
        _delete_tree(child, budget)
    budget.check_deadline()
    # A concurrent new child makes directory disposition fail; it is never
    # recursively discovered and deleted outside the preflight manifest.
    fs.delete_open_file(held.descriptor)
    held.close()


def remove_workspace(
    workspace: RunWorkspace,
    *,
    repository_root: Path,
    clock_ns: Callable[[], int],
) -> CleanupObservation | None:
    parent: int | None = None
    root: _HeldEntry | None = None
    budget = _CleanupBudget(clock_ns(), clock_ns)
    try:
        if _is_within(workspace.path.absolute(), repository_root.resolve()):
            raise _CleanupFailure("unsafe_tree", "refusing to remove consumer root or its contents")
        parent = fs.open(
            workspace.path.parent, os.O_RDONLY | fs.O_DIRECTORY | fs.O_NOFOLLOW
        )
        if _status_identity(os.fstat(parent)) != workspace.parent_identity:
            raise _CleanupFailure("unsafe_tree", "run directory parent identity mismatch")
        descriptor = fs.open_for_cleanup(workspace.path.name, dir_fd=parent)
        try:
            status = os.fstat(descriptor)
            if (
                _status_identity(status) != workspace.identity
                or not stat.S_ISDIR(status.st_mode)
                or getattr(status, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise _CleanupFailure("unsafe_tree", "run directory identity mismatch")
        except BaseException:
            os.close(descriptor)
            raise
        root = _hold_tree(descriptor, device=workspace.identity[0], budget=budget, depth=0)
        _delete_tree(root, budget)
        return None
    except OSError as error:
        kind = error.kind if isinstance(error, _CleanupFailure) else "io_failed"
        retained: Path | None = None
        try:
            current = fs.stat(workspace.path, follow_symlinks=False)
            if _status_identity(current) == workspace.identity:
                retained = workspace.path
        except OSError:
            pass
        return CleanupObservation(kind, str(error), retained, None)
    finally:
        if root is not None:
            root.close()
        if parent is not None:
            os.close(parent)
