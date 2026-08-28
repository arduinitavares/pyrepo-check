"""Secure, descriptor-verified execution workspace lifecycle."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile
import time
from types import MappingProxyType, ModuleType
from typing import Literal, Protocol, cast

if sys.platform == "darwin":
    import fcntl as _fcntl
else:
    _fcntl: ModuleType | None = None

_MAX_CLEANUP_ENTRIES = 4096
_MAX_CLEANUP_DEPTH = 64
_MAX_CLEANUP_DURATION_NS = 5_000_000_000
_SCANDIR_SUPPORTS_FD = os.scandir in os.supports_fd
_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd
_STAT_SUPPORTS_DIR_FD = os.stat in os.supports_dir_fd
_STAT_SUPPORTS_FOLLOW_SYMLINKS = os.stat in os.supports_follow_symlinks
_UNLINK_SUPPORTS_DIR_FD = os.unlink in os.supports_dir_fd
_RMDIR_SUPPORTS_DIR_FD = os.rmdir in os.supports_dir_fd
_MKDIR_SUPPORTS_DIR_FD = os.mkdir in os.supports_dir_fd
_RENAME_SUPPORTS_DIR_FD = os.rename in os.supports_dir_fd
_DARWIN_GETPATH_UNLINK_PROOF = (
    sys.platform == "darwin"
    and _fcntl is not None
    and callable(getattr(_fcntl, "fcntl", None))
    and type(getattr(_fcntl, "F_GETPATH", None)) is int
)
_POST_RMDIR_UNLINK_PROOF = sys.platform.startswith("linux") or _DARWIN_GETPATH_UNLINK_PROOF

@dataclass(frozen=True)
class RunWorkspace:
    path: Path
    identity: tuple[int, int]
    parent_identity: tuple[int, int]


@dataclass
class VerifiedRunWorkspace:
    workspace: RunWorkspace
    parent_descriptor: int
    descriptor: int

    def verify(self, gate: str) -> None:
        message = f"run directory identity mismatch {gate}"
        try:
            parent_status = os.fstat(self.parent_descriptor)
            lexical_parent_status = os.stat(
                self.workspace.path.parent,
                follow_symlinks=False,
            )
            relative_status = os.stat(
                self.workspace.path.name,
                dir_fd=self.parent_descriptor,
                follow_symlinks=False,
            )
            descriptor_status = os.fstat(self.descriptor)
            lexical_status = os.stat(
                self.workspace.path,
                follow_symlinks=False,
            )
        except OSError as error:
            raise OSError(message) from error
        if (
            _status_identity(parent_status) != self.workspace.parent_identity
            or not stat.S_ISDIR(parent_status.st_mode)
            or _status_identity(lexical_parent_status)
            != self.workspace.parent_identity
            or not stat.S_ISDIR(lexical_parent_status.st_mode)
            or _status_identity(relative_status) != self.workspace.identity
            or not stat.S_ISDIR(relative_status.st_mode)
            or _status_identity(descriptor_status) != self.workspace.identity
            or not stat.S_ISDIR(descriptor_status.st_mode)
            or _status_identity(lexical_status) != self.workspace.identity
            or not stat.S_ISDIR(lexical_status.st_mode)
        ):
            raise OSError(message)

    def close(self) -> None:
        run_error: OSError | None = None
        try:
            os.close(self.descriptor)
        except OSError as error:
            run_error = error
        try:
            os.close(self.parent_descriptor)
        except OSError:
            if run_error is None:
                raise
        if run_error is not None:
            raise run_error


CleanupFailureKind = Literal["budget_exceeded", "unsafe_tree", "io_failed"]
CleanupEntryType = Literal["directory", "symlink", "regular", "other"]
CleanupManifestKey = tuple[tuple[int, int], str]


class _ScandirIterator(Iterator[os.DirEntry[str]], Protocol):
    def close(self) -> None: ...


@dataclass(frozen=True)
class CleanupObservation:
    kind: CleanupFailureKind
    message: str
    retained_run_path: Path | None
    retained_quarantine_path: Path | None

    @property
    def retained_path(self) -> Path | None:
        """Compatibility alias for the original private cleanup observation."""
        return self.retained_run_path


class _CleanupFailure(OSError):
    def __init__(self, kind: CleanupFailureKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class _QuarantineSetupFailure(_CleanupFailure):
    def __init__(
        self,
        kind: CleanupFailureKind,
        message: str,
        quarantine: _QuarantineDirectory,
    ) -> None:
        super().__init__(kind, message)
        self.quarantine = quarantine


@dataclass(frozen=True)
class _CleanupManifestEntry:
    identity: tuple[int, int]
    file_type: CleanupEntryType


@dataclass(frozen=True)
class _CleanupManifest:
    entries: Mapping[CleanupManifestKey, _CleanupManifestEntry]


@dataclass
class _QuarantineDirectory:
    name: str
    identity: tuple[int, int]
    descriptor: int | None
    may_contain_data: bool = False
    ever_contained_data: bool = False
    removed: bool = False
    cleanup_allowed: bool = True


@dataclass
class _CleanupBudget:
    started_ns: int
    clock_ns: Callable[[], int]
    entries: int = 0
    quarantine: _QuarantineDirectory | None = None

    def observe_entry(self, *, depth: int) -> None:
        self.entries += 1
        if self.entries > _MAX_CLEANUP_ENTRIES:
            raise _CleanupFailure(
                "budget_exceeded",
                f"cleanup entry limit exceeded ({_MAX_CLEANUP_ENTRIES})",
            )
        if depth > _MAX_CLEANUP_DEPTH:
            raise _CleanupFailure(
                "budget_exceeded",
                f"cleanup depth limit exceeded ({_MAX_CLEANUP_DEPTH})",
            )

    def check_deadline(self) -> None:
        if self.clock_ns() - self.started_ns > _MAX_CLEANUP_DURATION_NS:
            raise _CleanupFailure(
                "budget_exceeded",
                f"cleanup duration limit exceeded ({_MAX_CLEANUP_DURATION_NS} ns)",
            )


@dataclass
class _CleanupFrame:
    descriptor: int
    entries: _ScandirIterator
    depth: int
    name: str | None
    identity: tuple[int, int]
    parent_descriptor: int



def create_run_workspace(repository_root: Path) -> RunWorkspace:
    resolved_root = repository_root.resolve()
    candidates = (Path(tempfile.gettempdir()), Path("/tmp"), Path("/var/tmp"))  # nosec B108
    last_error: OSError | None = None
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            base = candidate.resolve(strict=True)
        except OSError as error:
            last_error = error
            continue
        if base in seen or not base.is_dir() or _is_within(base, resolved_root):
            continue
        seen.add(base)
        try:
            parent_identity = _directory_identity(base)
        except OSError as error:
            last_error = error
            continue
        try:
            run_directory = Path(
                tempfile.mkdtemp(prefix="pyrepo-check-pytest-", dir=base)
            )
        except OSError as error:
            last_error = error
            continue
        identity: tuple[int, int] | None = None
        try:
            identity = _directory_identity(run_directory)
            resolved_run_directory = run_directory.resolve(strict=True)
            resolved_parent = resolved_run_directory.parent
            if (
                resolved_parent != base
                or _directory_identity(resolved_parent) != parent_identity
            ):
                raise OSError("created run directory parent identity mismatch")
            if _is_within(resolved_run_directory, resolved_root):
                raise OSError("refusing run directory inside consumer root")
            record = RunWorkspace(run_directory, identity, parent_identity)
        except OSError as error:
            cleanup_error = _remove_empty_created_run_directory(
                run_directory,
                identity,
                parent_identity,
            )
            last_error = _with_cleanup_error(error, cleanup_error)
            if cleanup_error is not None:
                raise last_error
            continue
        if identity is None:
            error = OSError("created run directory identity is unavailable")
            cleanup_error = _remove_empty_created_run_directory(
                run_directory,
                identity,
                parent_identity,
            )
            last_error = _with_cleanup_error(error, cleanup_error)
            if cleanup_error is not None:
                raise last_error
            continue
        return record
    if last_error is not None:
        raise last_error
    raise OSError("no safe operating-system temporary directory is available")



def remove_run_workspace(
    run_directory: RunWorkspace,
    *,
    repository_root: Path,
    clock_ns: Callable[[], int] = time.monotonic_ns,
) -> CleanupObservation | None:
    parent_descriptor: int | None = None
    parent_verified = False
    quarantine: _QuarantineDirectory | None = None
    observation: CleanupObservation | None = None
    close_error: OSError | None = None
    started_ns = clock_ns()
    try:
        if _is_within(run_directory.path.absolute(), repository_root.resolve()):
            raise _CleanupFailure(
                "unsafe_tree",
                "refusing to remove consumer root or its contents",
            )
        parent_descriptor = _open_verified_parent(run_directory)
        parent_verified = True
        quarantine = _create_quarantine_directory(
            parent_descriptor,
            expected_device=run_directory.identity[0],
            budget=_CleanupBudget(started_ns, clock_ns),
        )
        manifest = _walk_cleanup_tree(
            parent_descriptor,
            run_directory.path.name,
            run_directory.identity,
            budget=_CleanupBudget(started_ns, clock_ns),
            delete=False,
        )
        deletion_budget = _CleanupBudget(
            started_ns,
            clock_ns,
            quarantine=quarantine,
        )
        _walk_cleanup_tree(
            parent_descriptor,
            run_directory.path.name,
            run_directory.identity,
            budget=deletion_budget,
            delete=True,
            manifest=manifest,
        )
        _remove_verified_relative_directory(
            parent_descriptor,
            run_directory.path.name,
            run_directory.identity,
            expected_device=run_directory.identity[0],
            budget=deletion_budget,
            identity_mismatch_message="run directory identity mismatch before root removal",
        )
        _remove_held_relative_directory(
            parent_descriptor,
            quarantine,
            budget=deletion_budget,
            identity_mismatch_message=(
                "quarantine directory identity mismatch before removal"
            ),
        )
    except _CleanupFailure as error:
        if isinstance(error, _QuarantineSetupFailure):
            quarantine = error.quarantine
        cleanup_message = error.message
        if (
            quarantine is not None
            and not quarantine.removed
            and not quarantine.may_contain_data
            and not quarantine.ever_contained_data
            and quarantine.cleanup_allowed
            and parent_descriptor is not None
        ):
            try:
                _remove_held_relative_directory(
                    parent_descriptor,
                    quarantine,
                    budget=_CleanupBudget(started_ns, clock_ns),
                    identity_mismatch_message=(
                        "quarantine directory identity mismatch before failure cleanup"
                    ),
                )
            except OSError as cleanup_error:
                cleanup_message = (
                    f"{cleanup_message}; empty quarantine cleanup failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
        retained_run_path = _verified_retained_path(
            run_directory,
            parent_descriptor if parent_verified else None,
        )
        retained_quarantine_path = _verified_quarantine_path(
            run_directory,
            quarantine,
            parent_descriptor if parent_verified else None,
        )
        observation = CleanupObservation(
            error.kind,
            cleanup_message,
            retained_run_path,
            retained_quarantine_path,
        )
    except OSError as error:
        cleanup_message = f"{type(error).__name__}: {error}"
        if (
            quarantine is not None
            and not quarantine.removed
            and not quarantine.may_contain_data
            and not quarantine.ever_contained_data
            and quarantine.cleanup_allowed
            and parent_descriptor is not None
        ):
            try:
                _remove_held_relative_directory(
                    parent_descriptor,
                    quarantine,
                    budget=_CleanupBudget(started_ns, clock_ns),
                    identity_mismatch_message=(
                        "quarantine directory identity mismatch before failure cleanup"
                    ),
                )
            except OSError as cleanup_error:
                cleanup_message = (
                    f"{cleanup_message}; empty quarantine cleanup failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
        retained_run_path = _verified_retained_path(
            run_directory,
            parent_descriptor if parent_verified else None,
        )
        retained_quarantine_path = _verified_quarantine_path(
            run_directory,
            quarantine,
            parent_descriptor if parent_verified else None,
        )
        observation = CleanupObservation(
            "io_failed",
            cleanup_message,
            retained_run_path,
            retained_quarantine_path,
        )
    finally:
        if quarantine is not None and quarantine.descriptor is not None:
            try:
                os.close(quarantine.descriptor)
            except OSError as error:
                close_error = error
        if parent_descriptor is not None:
            try:
                os.close(parent_descriptor)
            except OSError as error:
                if close_error is None:
                    close_error = error
    if observation is not None:
        return observation
    if close_error is not None:
        return CleanupObservation(
            "io_failed",
            f"{type(close_error).__name__}: {close_error}",
            None,
            None,
        )
    return None


def _walk_cleanup_tree(
    parent_descriptor: int,
    root_name: str,
    root_identity: tuple[int, int],
    *,
    budget: _CleanupBudget,
    delete: bool,
    manifest: _CleanupManifest | None = None,
) -> _CleanupManifest:
    if delete and manifest is None:
        raise _CleanupFailure("unsafe_tree", "cleanup deletion manifest is unavailable")
    if not delete and manifest is not None:
        raise _CleanupFailure("unsafe_tree", "cleanup validation received a deletion manifest")
    manifest_entries: dict[CleanupManifestKey, _CleanupManifestEntry] = {}
    remaining = set(manifest.entries) if manifest is not None else set()
    stack: list[_CleanupFrame] = []
    try:
        root_descriptor, root_status = _open_verified_relative_directory(
            parent_descriptor,
            root_name,
            root_identity,
            expected_device=root_identity[0],
            budget=budget,
        )
        try:
            budget.check_deadline()
            root_entries = os.scandir(root_descriptor)
        except BaseException:
            try:
                os.close(root_descriptor)
            except BaseException:
                pass
            raise
        stack.append(
            _CleanupFrame(
                root_descriptor,
                root_entries,
                0,
                None,
                _status_identity(root_status),
                parent_descriptor,
            )
        )
        while stack:
            frame = stack[-1]
            budget.check_deadline()
            try:
                entry = next(frame.entries)
            except StopIteration:
                frame.entries.close()
                if frame.name is None:
                    os.close(frame.descriptor)
                    stack.pop()
                    continue
                if delete:
                    budget.check_deadline()
                    _verify_relative_identity(
                        frame.parent_descriptor,
                        frame.name,
                        frame.identity,
                        f"directory identity mismatch before removal: {frame.name}",
                    )
                if delete:
                    budget.check_deadline()
                    os.rmdir(frame.name, dir_fd=frame.parent_descriptor)
                    budget.check_deadline()
                    if _opened_directory_remains_linked(frame.descriptor):
                        raise _CleanupFailure(
                            "unsafe_tree",
                            f"directory remained linked after removal: {frame.name}",
                        )
                os.close(frame.descriptor)
                stack.pop()
                continue
            child_depth = frame.depth + 1
            budget.observe_entry(depth=child_depth)
            budget.check_deadline()
            key = (frame.identity, entry.name)
            if delete and key not in remaining:
                raise _CleanupFailure(
                    "unsafe_tree",
                    f"cleanup entry absent from validation manifest: {entry.name}",
                )
            try:
                child_status = os.stat(
                    entry.name,
                    dir_fd=frame.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError as error:
                if delete:
                    raise _CleanupFailure(
                        "unsafe_tree",
                        f"validated cleanup entry is missing: {entry.name}",
                    ) from error
                raise
            observed_entry = _CleanupManifestEntry(
                _status_identity(child_status),
                _cleanup_entry_type(child_status.st_mode),
            )
            if delete:
                if manifest is None or manifest.entries[key] != observed_entry:
                    raise _CleanupFailure(
                        "unsafe_tree",
                        f"cleanup entry identity or type mismatch: {entry.name}",
                    )
            else:
                if key in manifest_entries:
                    raise _CleanupFailure(
                        "unsafe_tree",
                        f"duplicate cleanup manifest entry: {entry.name}",
                    )
                manifest_entries[key] = observed_entry
            if stat.S_ISDIR(child_status.st_mode):
                if delete:
                    remaining.remove(key)
                child_identity = _status_identity(child_status)
                child_descriptor, verified_status = _open_verified_relative_directory(
                    frame.descriptor,
                    entry.name,
                    child_identity,
                    expected_device=root_identity[0],
                    budget=budget,
                )
                try:
                    budget.check_deadline()
                    child_entries = os.scandir(child_descriptor)
                except BaseException:
                    os.close(child_descriptor)
                    raise
                stack.append(
                    _CleanupFrame(
                        child_descriptor,
                        child_entries,
                        child_depth,
                        entry.name,
                        _status_identity(verified_status),
                        frame.descriptor,
                    )
                )
                continue
            if delete:
                quarantine = budget.quarantine
                if quarantine is None:
                    raise _CleanupFailure(
                        "unsafe_tree",
                        "cleanup leaf quarantine is unavailable",
                    )
                _quarantine_and_remove_leaf(
                    frame.descriptor,
                    entry.name,
                    manifest.entries[key] if manifest is not None else observed_entry,
                    key=key,
                    remaining=remaining,
                    quarantine=quarantine,
                    budget=budget,
                )
        if delete and remaining:
            raise _CleanupFailure(
                "unsafe_tree",
                "validated cleanup entries are missing during deletion",
            )
        if manifest is not None:
            return manifest
        return _CleanupManifest(MappingProxyType(manifest_entries))
    finally:
        for frame in reversed(stack):
            try:
                frame.entries.close()
            except BaseException:
                pass
            try:
                os.close(frame.descriptor)
            except BaseException:
                pass


def _create_quarantine_directory(
    parent_descriptor: int,
    *,
    expected_device: int,
    budget: _CleanupBudget,
) -> _QuarantineDirectory:
    budget.check_deadline()
    name = f".pyrepo-check-quarantine-{secrets.token_hex(16)}"
    os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
    descriptor: int | None = None
    quarantine: _QuarantineDirectory | None = None
    try:
        budget.check_deadline()
        file_status = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        identity = _status_identity(file_status)
        quarantine = _QuarantineDirectory(name, identity, None)
        if (
            not stat.S_ISDIR(file_status.st_mode)
            or file_status.st_dev != expected_device
            or file_status.st_uid != _effective_uid()
            or stat.S_IMODE(file_status.st_mode) != 0o700
        ):
            quarantine.cleanup_allowed = False
            raise _QuarantineSetupFailure(
                "unsafe_tree",
                "created quarantine directory is not private or trusted",
                quarantine,
            )
        descriptor = os.open(
            name,
            _secure_directory_open_flags(),
            dir_fd=parent_descriptor,
        )
        quarantine.descriptor = descriptor
        os.set_inheritable(descriptor, False)
        budget.check_deadline()
        verified_status = os.fstat(descriptor)
        if (
            _status_identity(verified_status) != identity
            or not stat.S_ISDIR(verified_status.st_mode)
            or verified_status.st_dev != expected_device
            or verified_status.st_uid != _effective_uid()
            or stat.S_IMODE(verified_status.st_mode) != 0o700
        ):
            quarantine.cleanup_allowed = False
            raise _QuarantineSetupFailure(
                "unsafe_tree",
                "opened quarantine directory identity or privacy mismatch",
                quarantine,
            )
        return quarantine
    except _QuarantineSetupFailure:
        raise
    except BaseException as error:
        if quarantine is not None:
            quarantine.cleanup_allowed = False
            raise _QuarantineSetupFailure(
                "unsafe_tree",
                f"could not securely open quarantine directory: {type(error).__name__}: {error}",
                quarantine,
            ) from error
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException:
                pass
        raise


def _quarantine_and_remove_leaf(
    source_descriptor: int,
    source_name: str,
    expected_entry: _CleanupManifestEntry,
    *,
    key: CleanupManifestKey,
    remaining: set[CleanupManifestKey],
    quarantine: _QuarantineDirectory,
    budget: _CleanupBudget,
) -> None:
    if quarantine.descriptor is None:
        raise _CleanupFailure(
            "unsafe_tree",
            "cleanup leaf quarantine descriptor is unavailable",
        )
    quarantine_name = f"leaf-{secrets.token_hex(16)}"
    budget.check_deadline()
    quarantine.may_contain_data = True
    quarantine.ever_contained_data = True
    os.rename(
        source_name,
        quarantine_name,
        src_dir_fd=source_descriptor,
        dst_dir_fd=quarantine.descriptor,
    )
    budget.check_deadline()
    quarantined_status = os.stat(
        quarantine_name,
        dir_fd=quarantine.descriptor,
        follow_symlinks=False,
    )
    quarantined_entry = _CleanupManifestEntry(
        _status_identity(quarantined_status),
        _cleanup_entry_type(quarantined_status.st_mode),
    )
    if quarantined_entry != expected_entry:
        raise _CleanupFailure(
            "unsafe_tree",
            f"quarantined cleanup entry identity or type mismatch: {source_name}",
        )
    remaining.remove(key)
    budget.check_deadline()
    os.unlink(quarantine_name, dir_fd=quarantine.descriptor)
    quarantine.may_contain_data = False
    budget.check_deadline()


def _open_verified_parent(run_directory: RunWorkspace) -> int:
    parent_identity = run_directory.parent_identity
    if parent_identity is None:
        raise _CleanupFailure("unsafe_tree", "run directory parent identity is unavailable")
    descriptor = os.open(run_directory.path.parent, _secure_directory_open_flags())
    try:
        os.set_inheritable(descriptor, False)
        if _status_identity(os.fstat(descriptor)) != parent_identity:
            raise _CleanupFailure("unsafe_tree", "run directory parent identity mismatch")
    except BaseException:
        try:
            os.close(descriptor)
        except BaseException:
            pass
        raise
    return descriptor


def open_verified_workspace(
    workspace: RunWorkspace,
) -> VerifiedRunWorkspace:
    parent_descriptor = _open_verified_parent(workspace)
    run_descriptor: int | None = None
    try:
        run_descriptor = os.open(
            workspace.path.name,
            _secure_directory_open_flags(),
            dir_fd=parent_descriptor,
        )
        os.set_inheritable(run_descriptor, False)
        verified = VerifiedRunWorkspace(
            workspace,
            parent_descriptor,
            run_descriptor,
        )
        verified.verify("before preparation")
    except BaseException:
        if run_descriptor is not None:
            try:
                os.close(run_descriptor)
            except BaseException:
                pass
        try:
            os.close(parent_descriptor)
        except BaseException:
            pass
        raise
    return verified


def _remove_held_relative_directory(
    parent_descriptor: int,
    directory: _QuarantineDirectory,
    *,
    budget: _CleanupBudget,
    identity_mismatch_message: str,
) -> None:
    if directory.descriptor is None:
        raise _CleanupFailure(
            "unsafe_tree",
            "quarantine directory descriptor is unavailable for removal",
        )
    budget.check_deadline()
    _verify_relative_identity(
        parent_descriptor,
        directory.name,
        directory.identity,
        identity_mismatch_message,
    )
    budget.check_deadline()
    os.rmdir(directory.name, dir_fd=parent_descriptor)
    budget.check_deadline()
    if _opened_directory_remains_linked(directory.descriptor):
        raise _CleanupFailure(
            "unsafe_tree",
            f"directory remained linked after removal: {directory.name}",
        )
    directory.removed = True


def _open_verified_relative_directory(
    parent_descriptor: int,
    name: str,
    identity: tuple[int, int],
    *,
    expected_device: int,
    budget: _CleanupBudget,
) -> tuple[int, os.stat_result]:
    budget.check_deadline()
    try:
        descriptor = os.open(
            name,
            _secure_directory_open_flags(),
            dir_fd=parent_descriptor,
        )
    except OSError as error:
        kind: CleanupFailureKind = (
            "unsafe_tree"
            if error.errno in {errno.ELOOP, errno.ENOTDIR}
            else "io_failed"
        )
        raise _CleanupFailure(
            kind,
            f"could not safely open directory {name}: {type(error).__name__}: {error}",
        ) from error
    try:
        os.set_inheritable(descriptor, False)
        budget.check_deadline()
        file_status = os.fstat(descriptor)
        if file_status.st_dev != expected_device:
            raise _CleanupFailure("unsafe_tree", f"cross-device directory rejected: {name}")
        if _status_identity(file_status) != identity:
            raise _CleanupFailure("unsafe_tree", f"directory identity mismatch: {name}")
    except BaseException:
        try:
            os.close(descriptor)
        except BaseException:
            pass
        raise
    return descriptor, file_status


def _verify_relative_identity(
    parent_descriptor: int,
    name: str,
    identity: tuple[int, int],
    message: str,
) -> None:
    file_status = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if _status_identity(file_status) != identity or not stat.S_ISDIR(file_status.st_mode):
        raise _CleanupFailure("unsafe_tree", message)


def _remove_verified_relative_directory(
    parent_descriptor: int,
    name: str,
    identity: tuple[int, int],
    *,
    expected_device: int,
    budget: _CleanupBudget,
    identity_mismatch_message: str,
) -> None:
    descriptor, _status = _open_verified_relative_directory(
        parent_descriptor,
        name,
        identity,
        expected_device=expected_device,
        budget=budget,
    )
    try:
        budget.check_deadline()
        _verify_relative_identity(
            parent_descriptor,
            name,
            identity,
            identity_mismatch_message,
        )
        budget.check_deadline()
        os.rmdir(name, dir_fd=parent_descriptor)
        budget.check_deadline()
        if _opened_directory_remains_linked(descriptor):
            raise _CleanupFailure(
                "unsafe_tree",
                f"directory remained linked after removal: {name}",
            )
    finally:
        os.close(descriptor)


def _opened_directory_remains_linked(descriptor: int) -> bool:
    file_status = os.fstat(descriptor)
    if file_status.st_nlink == 0:
        return False
    if _fcntl is None:
        return True
    fcntl_call = getattr(_fcntl, "fcntl", None)
    get_path = getattr(_fcntl, "F_GETPATH", None)
    if not callable(fcntl_call) or type(get_path) is not int:
        return True
    try:
        raw_path = fcntl_call(descriptor, get_path, b"\0" * 1024)
        if not isinstance(raw_path, bytes):
            return True
        path_bytes, separator, _remainder = raw_path.partition(b"\0")
        if not separator or not path_bytes:
            return True
        live_path = os.fsdecode(path_bytes)
        if not os.path.isabs(live_path):
            return True
        os.stat(live_path, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _verified_retained_path(
    run_directory: RunWorkspace,
    parent_descriptor: int | None,
) -> Path | None:
    if parent_descriptor is None:
        return None
    try:
        parent_identity = run_directory.parent_identity
        if _status_identity(os.fstat(parent_descriptor)) != parent_identity:
            return None
        lexical_parent_status = os.stat(
            run_directory.path.parent,
            follow_symlinks=False,
        )
        if (
            _status_identity(lexical_parent_status) != parent_identity
            or not stat.S_ISDIR(lexical_parent_status.st_mode)
        ):
            return None
        file_status = os.stat(
            run_directory.path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        lexical_child_status = os.stat(run_directory.path, follow_symlinks=False)
    except OSError:
        return None
    if (
        _status_identity(file_status) != run_directory.identity
        or not stat.S_ISDIR(file_status.st_mode)
        or _status_identity(lexical_child_status) != run_directory.identity
        or not stat.S_ISDIR(lexical_child_status.st_mode)
    ):
        return None
    return run_directory.path


def _verified_quarantine_path(
    run_directory: RunWorkspace,
    quarantine: _QuarantineDirectory | None,
    parent_descriptor: int | None,
) -> Path | None:
    if quarantine is None or quarantine.removed or parent_descriptor is None:
        return None
    try:
        parent_identity = run_directory.parent_identity
        if _status_identity(os.fstat(parent_descriptor)) != parent_identity:
            return None
        lexical_parent_status = os.stat(
            run_directory.path.parent,
            follow_symlinks=False,
        )
        if (
            _status_identity(lexical_parent_status) != parent_identity
            or not stat.S_ISDIR(lexical_parent_status.st_mode)
        ):
            return None
        relative_status = os.stat(
            quarantine.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        lexical_path = run_directory.path.parent / quarantine.name
        lexical_status = os.stat(lexical_path, follow_symlinks=False)
    except OSError:
        return None
    if (
        _status_identity(relative_status) != quarantine.identity
        or not stat.S_ISDIR(relative_status.st_mode)
        or _status_identity(lexical_status) != quarantine.identity
        or not stat.S_ISDIR(lexical_status.st_mode)
    ):
        return None
    return lexical_path


def _secure_directory_open_flags() -> int:
    directory_only = cast(int, getattr(os, "O_DIRECTORY"))
    no_follow = cast(int, getattr(os, "O_NOFOLLOW"))
    non_blocking = cast(int, getattr(os, "O_NONBLOCK"))
    return os.O_RDONLY | directory_only | no_follow | non_blocking


def _effective_uid() -> int:
    get_effective_uid = getattr(os, "geteuid", None)
    if not callable(get_effective_uid):
        raise _CleanupFailure(
            "unsafe_tree",
            "effective user identity is unavailable",
        )
    return cast(Callable[[], int], get_effective_uid)()


def format_cleanup_diagnostic(observation: CleanupObservation) -> str:
    diagnostic = observation.message
    if observation.retained_run_path is not None:
        diagnostic = (
            f"{diagnostic}; retained run path: {observation.retained_run_path}"
        )
    if observation.retained_quarantine_path is not None:
        diagnostic = (
            f"{diagnostic}; retained quarantine path: "
            f"{observation.retained_quarantine_path}"
        )
    return diagnostic


def _remove_empty_created_run_directory(
    run_directory: Path,
    identity: tuple[int, int] | None,
    parent_identity: tuple[int, int],
) -> OSError | None:
    if identity is None:
        return OSError("created run directory identity is unavailable")
    parent_descriptor: int | None = None
    cleanup_error: OSError | None = None
    try:
        parent_descriptor = os.open(
            run_directory.parent,
            _secure_directory_open_flags(),
        )
        os.set_inheritable(parent_descriptor, False)
        if _status_identity(os.fstat(parent_descriptor)) != parent_identity:
            raise _CleanupFailure(
                "unsafe_tree",
                "created run directory parent identity mismatch",
            )
        started_ns = time.monotonic_ns()
        _remove_verified_relative_directory(
            parent_descriptor,
            run_directory.name,
            identity,
            expected_device=identity[0],
            budget=_CleanupBudget(started_ns, time.monotonic_ns),
            identity_mismatch_message="created run directory identity mismatch",
        )
    except OSError as error:
        cleanup_error = error
    finally:
        if parent_descriptor is not None:
            try:
                os.close(parent_descriptor)
            except OSError as error:
                if cleanup_error is None:
                    cleanup_error = error
    return cleanup_error


def _directory_identity(path: Path) -> tuple[int, int]:
    file_status = os.lstat(path)
    if not stat.S_ISDIR(file_status.st_mode):
        raise OSError("run directory is not a directory")
    return _status_identity(file_status)


def _status_identity(file_status: os.stat_result) -> tuple[int, int]:
    return file_status.st_dev, file_status.st_ino


def _cleanup_entry_type(mode: int) -> CleanupEntryType:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISREG(mode):
        return "regular"
    return "other"


def _verify_directory_identity(path: Path, identity: tuple[int, int]) -> None:
    if _directory_identity(path) != identity:
        raise OSError("run directory identity mismatch")


def _with_cleanup_error(error: OSError, cleanup_error: OSError | None) -> OSError:
    if cleanup_error is None:
        return error
    return OSError(
        f"{error}; cleanup failed: {type(cleanup_error).__name__}: {cleanup_error}"
    )


def _platform_capability_error() -> str | None:
    if (
        type(getattr(os, "O_NOFOLLOW", None)) is not int
        or type(getattr(os, "O_DIRECTORY", None)) is not int
        or type(getattr(os, "O_NONBLOCK", None)) is not int
        or not _SCANDIR_SUPPORTS_FD
        or not _OPEN_SUPPORTS_DIR_FD
        or not _STAT_SUPPORTS_DIR_FD
        or not _STAT_SUPPORTS_FOLLOW_SYMLINKS
        or not _UNLINK_SUPPORTS_DIR_FD
        or not _RMDIR_SUPPORTS_DIR_FD
        or not _MKDIR_SUPPORTS_DIR_FD
        or not _RENAME_SUPPORTS_DIR_FD
        or not callable(getattr(os, "geteuid", None))
        or not _POST_RMDIR_UNLINK_PROOF
    ):
        return (
            "Structured pytest evidence requires descriptor-safe no-follow file opening "
            "and bounded descriptor-relative recursive removal."
        )
    return None


def _is_within(path: Path, root: Path) -> bool:
    return path.is_relative_to(root)
