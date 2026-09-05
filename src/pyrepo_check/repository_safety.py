"""Capture immutable repository state without following repository aliases."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import os

from pyrepo_check import filesystem as fs
from pathlib import Path
import re
import stat
import subprocess  # nosec B404
from typing import Literal, cast

from pyrepo_check.controller_tools import ControllerExecutable

from pyrepo_check.execution import (
    CAPTURE_LIMIT_BYTES,
    EnvironmentFailureObservation,
    ExecutedProcess,
    MutationProtection,
    ProcessRunner,
    execute_process,
)


_INDEX_ENTRY_PATTERN = re.compile(rb"([0-7]{6}) ([0-9a-f]+) ([0-3])\t(.*)", re.DOTALL)
_READ_CHUNK_BYTES = 64 * 1024
_PROTECTED_NAMES = ("pyproject.toml", "uv.lock")


@dataclass(frozen=True)
class ProtectedFileSnapshot:
    path: str
    kind: Literal["regular", "missing", "unsafe"]
    mode: int | None
    sha256: str | None


@dataclass(frozen=True)
class TrackedFileSnapshot:
    path: str
    index_mode: str
    index_object: str
    working_tree_kind: Literal["regular", "symlink", "missing", "gitlink", "other"]
    working_tree_mode: int | None
    sha256: str | None


@dataclass(frozen=True)
class RepositoryStateSnapshot:
    git_root: Path | None
    protected_files: tuple[ProtectedFileSnapshot, ...]
    tracked_files: tuple[TrackedFileSnapshot, ...]


@dataclass(frozen=True)
class RepositoryBaselineResult:
    snapshot: RepositoryStateSnapshot | None
    processes: tuple[ExecutedProcess, ...]
    error: EnvironmentFailureObservation | None


@dataclass(frozen=True)
class RepositoryVerificationResult:
    processes: tuple[ExecutedProcess, ...]
    mutation_protection: MutationProtection
    error: EnvironmentFailureObservation | None


class _SnapshotError(Exception):
    pass


def capture_repository_baseline(
    root: Path,
    *,
    runner: ProcessRunner | None,
    clock_ns: Callable[[], int],
    controller_environment: Mapping[str, str] | None = None,
    expected_pyproject_sha256: str | None = None,
    git_executable: ControllerExecutable | str | None = "git",
) -> RepositoryBaselineResult:
    """Capture protected files and, for Git worktrees, every tracked entry."""
    project_root = _normalized_absolute(root)
    venv_error = _inspect_venv(project_root)
    if venv_error is not None:
        return RepositoryBaselineResult(None, (), venv_error)
    try:
        protected = _capture_protected_files(project_root)
    except _SnapshotError:
        return _baseline_failure([], "Protected files could not be safely captured.")
    if expected_pyproject_sha256 is not None and (
        protected[0].kind != "regular"
        or protected[0].sha256 != expected_pyproject_sha256
    ):
        return _baseline_failure(
            [],
            "pyproject.toml changed after configuration was parsed.",
        )

    environment = _sanitized_git_environment(
        os.environ if controller_environment is None else controller_environment
    )
    processes: list[ExecutedProcess] = []
    git_marker = any(
        _lexically_exists(directory / ".git")
        for directory in (project_root, *project_root.parents)
    )
    if git_executable is None:
        if git_marker:
            return _baseline_failure([], "Git metadata exists but Git is unavailable.")
        return _capture_non_git_baseline(project_root, [], protected=protected)
    root_process = _git_process(
        role="repository_git_root",
        root=project_root,
        arguments=("rev-parse", "--show-toplevel"),
        environment=environment,
        runner=runner,
        clock_ns=clock_ns,
        git_executable=git_executable,
    )
    processes.append(root_process)
    if root_process.spawn_error is not None or root_process.returncode is None:
        if git_marker:
            return _baseline_failure(processes, "Git metadata exists but Git is unavailable.")
        return _capture_non_git_baseline(project_root, processes, protected=protected)
    if root_process.returncode != 0:
        if git_marker:
            return _baseline_failure(
                processes,
                "Git metadata exists but the repository root could not be inspected.",
            )
        return _capture_non_git_baseline(project_root, processes, protected=protected)

    git_root = _parse_git_root(root_process)
    if git_root is None or not (
        project_root == git_root or git_root in project_root.parents
    ):
        return _baseline_failure(processes, "Git repository-root evidence is invalid.")

    tracked_venv = _git_process(
        role="repository_venv_tracked",
        root=project_root,
        arguments=("ls-files", "-z", "--", ".venv"),
        environment=environment,
        runner=runner,
        clock_ns=clock_ns,
        git_executable=git_executable,
    )
    processes.append(tracked_venv)
    tracked_output = _complete_stdout(tracked_venv)
    if tracked_venv.returncode != 0 or tracked_output is None:
        return _baseline_failure(processes, "Git could not prove that .venv is untracked.")
    if tracked_output:
        return _baseline_failure(processes, ".venv must be untracked.")

    ignored_venv = _git_process(
        role="repository_venv_ignored",
        root=project_root,
        arguments=(
            "check-ignore",
            "--no-index",
            "-q",
            "--",
            ".venv/",
        ),
        environment=environment,
        runner=runner,
        clock_ns=clock_ns,
        git_executable=git_executable,
    )
    processes.append(ignored_venv)
    if ignored_venv.spawn_error is not None or ignored_venv.returncode != 0:
        return _baseline_failure(processes, ".venv must be ignored by the repository.")

    stage_process = _tracked_stage_process(
        project_root,
        environment=environment,
        runner=runner,
        clock_ns=clock_ns,
        git_executable=git_executable,
    )
    processes.append(stage_process)
    try:
        entries, has_unmerged = _parse_tracked_entries(project_root, stage_process)
    except _SnapshotError:
        return _baseline_failure(processes, "Repository state could not be safely captured.")
    if has_unmerged:
        return _baseline_failure(processes, "The Git index contains unmerged entries.")
    if any(entry.kind != "regular" for entry in protected):
        return _baseline_failure(
            processes,
            "Protected dependency files must be regular non-symlink files.",
        )
    return RepositoryBaselineResult(
        snapshot=RepositoryStateSnapshot(
            git_root=git_root,
            protected_files=protected,
            tracked_files=entries,
        ),
        processes=tuple(processes),
        error=None,
    )


def verify_repository_state(
    snapshot: RepositoryStateSnapshot,
    *,
    annotations_fix_targets: tuple[str, ...] | None,
    runner: ProcessRunner | None,
    clock_ns: Callable[[], int],
    git_executable: ControllerExecutable | str | None = "git",
) -> RepositoryVerificationResult:
    """Rebuild the observable state and report any post-baseline mutation."""
    project_root = _snapshot_project_root(snapshot)
    if project_root is None:
        return RepositoryVerificationResult(
            (),
            "unobserved",
            _repository_changed("The repository baseline is invalid."),
        )
    try:
        protected = _capture_protected_files(project_root)
    except _SnapshotError:
        return RepositoryVerificationResult(
            (),
            "unobserved",
            _repository_changed("Protected files could not be safely verified."),
        )
    protected_valid = all(entry.kind == "regular" for entry in protected)
    baseline_protected_valid = all(
        entry.kind == "regular" for entry in snapshot.protected_files
    )

    if snapshot.git_root is None:
        if not protected_valid or not baseline_protected_valid:
            return RepositoryVerificationResult(
                (),
                "unobserved",
                _repository_changed("Protected-file evidence is incomplete."),
            )
        error = (
            None
            if protected == snapshot.protected_files
            else _repository_changed("A protected dependency file changed.")
        )
        return RepositoryVerificationResult((), "protected_files", error)

    if git_executable is None:
        return RepositoryVerificationResult(
            (),
            "unobserved",
            _repository_changed("Pinned Git is unavailable for final verification."),
        )

    environment = _sanitized_git_environment(os.environ)
    stage_process = _tracked_stage_process(
        project_root,
        environment=environment,
        runner=runner,
        clock_ns=clock_ns,
        git_executable=git_executable,
    )
    processes = (stage_process,)
    try:
        tracked, has_unmerged = _parse_tracked_entries(project_root, stage_process)
    except _SnapshotError:
        return RepositoryVerificationResult(
            processes,
            "unobserved",
            _repository_changed("Tracked repository state could not be safely verified."),
        )
    if not protected_valid or not baseline_protected_valid:
        return RepositoryVerificationResult(
            processes,
            "unobserved",
            _repository_changed("Protected-file evidence is incomplete."),
        )
    if protected != snapshot.protected_files:
        return RepositoryVerificationResult(
            processes,
            "tracked_files",
            _repository_changed("A protected dependency file changed."),
        )
    if has_unmerged:
        return RepositoryVerificationResult(
            processes,
            "tracked_files",
            _repository_changed("The Git index gained an unmerged entry."),
        )
    if not _tracked_files_match(
        snapshot.tracked_files,
        tracked,
        annotations_fix_targets=annotations_fix_targets,
    ):
        return RepositoryVerificationResult(
            processes,
            "tracked_files",
            _repository_changed("Tracked repository state changed."),
        )
    return RepositoryVerificationResult(processes, "tracked_files", None)


def _capture_non_git_baseline(
    root: Path,
    processes: list[ExecutedProcess],
    *,
    protected: tuple[ProtectedFileSnapshot, ...] | None = None,
) -> RepositoryBaselineResult:
    if protected is None:
        try:
            protected = _capture_protected_files(root)
        except _SnapshotError:
            return _baseline_failure(processes, "Protected files could not be safely captured.")
    if any(entry.kind != "regular" for entry in protected):
        return _baseline_failure(
            processes,
            "Protected dependency files must be regular non-symlink files.",
        )
    return RepositoryBaselineResult(
        RepositoryStateSnapshot(None, protected, ()),
        tuple(processes),
        None,
    )


def _snapshot_project_root(snapshot: RepositoryStateSnapshot) -> Path | None:
    if len(snapshot.protected_files) != len(_PROTECTED_NAMES):
        return None
    paths = tuple(Path(entry.path) for entry in snapshot.protected_files)
    if any(not path.is_absolute() for path in paths):
        return None
    normalized = tuple(_normalized_absolute(path) for path in paths)
    if any(str(path) != entry.path for path, entry in zip(normalized, snapshot.protected_files)):
        return None
    root = normalized[0].parent
    if normalized != tuple(root / name for name in _PROTECTED_NAMES):
        return None
    if snapshot.git_root is not None:
        git_root = _normalized_absolute(snapshot.git_root)
        if git_root != snapshot.git_root or not (
            root == git_root or git_root in root.parents
        ):
            return None
    return root


def _tracked_files_match(
    baseline: tuple[TrackedFileSnapshot, ...],
    current: tuple[TrackedFileSnapshot, ...],
    *,
    annotations_fix_targets: tuple[str, ...] | None,
) -> bool:
    if len(baseline) != len(current):
        return False
    for before, after in zip(baseline, current):
        if before == after:
            continue
        content_only_change = (
            before.path == after.path
            and before.index_mode == after.index_mode
            and before.index_object == after.index_object
            and before.working_tree_kind == after.working_tree_kind
            and before.working_tree_kind == "regular"
            and before.working_tree_mode == after.working_tree_mode
            and before.sha256 is not None
            and after.sha256 is not None
        )
        target_scoped = annotations_fix_targets is not None and any(
            target == "."
            or before.path == target.rstrip("/")
            or before.path.startswith(f"{target.rstrip('/')}/")
            for target in annotations_fix_targets
        )
        if not target_scoped or not content_only_change:
            return False
    return True


def _inspect_venv(root: Path) -> EnvironmentFailureObservation | None:
    path = root / ".venv"
    try:
        status = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return _unsafe_failure(".venv could not be safely inspected.")
    if not stat.S_ISDIR(status.st_mode) or _is_reparse_point(status):
        return _unsafe_failure(".venv must be a real directory or be absent.")
    return None


def _sanitized_git_environment(source: Mapping[str, str]) -> dict[str, str]:
    environment = {name: value for name, value in source.items() if not name.startswith("GIT_")}
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["LC_ALL"] = "C"
    return environment


def _git_process(
    *,
    role: str,
    root: Path,
    arguments: tuple[str, ...],
    environment: dict[str, str],
    runner: ProcessRunner | None,
    clock_ns: Callable[[], int],
    git_executable: ControllerExecutable | str,
) -> ExecutedProcess:
    pinned_path = (
        str(git_executable.path)
        if isinstance(git_executable, ControllerExecutable)
        else git_executable
    )
    try:
        executable = (
            str(git_executable.path_for_use())
            if isinstance(git_executable, ControllerExecutable)
            else git_executable
        )
    except OSError as error:
        executable = pinned_path
        failure = error

        def failed_runner(
            *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[tuple[str, ...]]:
            del args, kwargs
            raise failure

        selected_runner = cast(ProcessRunner, failed_runner)
    else:
        selected_runner = runner
    return execute_process(
        role=role,
        command=(executable, "-C", str(root), *arguments),
        cwd=root,
        capture_output=True,
        runner=selected_runner,  # type: ignore[arg-type]
        clock_ns=clock_ns,
        environment=environment,
    )


def _tracked_stage_process(
    root: Path,
    *,
    environment: dict[str, str],
    runner: ProcessRunner | None,
    clock_ns: Callable[[], int],
    git_executable: ControllerExecutable | str = "git",
) -> ExecutedProcess:
    return _git_process(
        role="repository_tracked_snapshot",
        root=root,
        arguments=("ls-files", "--stage", "-z", "--", "."),
        environment=environment,
        runner=runner,
        clock_ns=clock_ns,
        git_executable=git_executable,
    )


def _parse_git_root(process: ExecutedProcess) -> Path | None:
    output = _complete_stdout(process)
    if output is None or not output or b"\0" in output:
        return None
    raw = output[:-1] if output.endswith(b"\n") else output
    if not raw or b"\n" in raw or b"\r" in raw:
        return None
    candidate = Path(os.fsdecode(raw))
    if not candidate.is_absolute():
        return None
    return _normalized_absolute(candidate)


def _parse_tracked_entries(
    root: Path,
    process: ExecutedProcess,
) -> tuple[tuple[TrackedFileSnapshot, ...], bool]:
    if process.spawn_error is not None or process.returncode != 0:
        raise _SnapshotError
    output = _complete_stdout(process)
    if output is None:
        raise _SnapshotError
    records = output.split(b"\0")
    if records[-1] != b"":
        raise _SnapshotError
    entries: list[TrackedFileSnapshot] = []
    observed_paths: set[str] = set()
    has_unmerged = False
    for record in records[:-1]:
        match = _INDEX_ENTRY_PATTERN.fullmatch(record)
        if match is None:
            raise _SnapshotError
        index_mode = match.group(1).decode("ascii")
        index_object = match.group(2).decode("ascii")
        stage = match.group(3)
        path = os.fsdecode(match.group(4))
        if not path or Path(path).is_absolute() or ".." in Path(path).parts:
            raise _SnapshotError
        if stage != b"0":
            has_unmerged = True
            continue
        if path in observed_paths:
            raise _SnapshotError
        observed_paths.add(path)
        entries.append(_capture_tracked_file(root, path, index_mode, index_object))
    return tuple(entries), has_unmerged


def _capture_tracked_file(
    root: Path,
    relative_path: str,
    index_mode: str,
    index_object: str,
) -> TrackedFileSnapshot:
    if index_mode == "160000":
        return TrackedFileSnapshot(
            relative_path,
            index_mode,
            index_object,
            "gitlink",
            None,
            None,
        )
    if not _tracked_parent_exists_safely(root, relative_path):
        return TrackedFileSnapshot(
            relative_path,
            index_mode,
            index_object,
            "missing",
            None,
            None,
        )
    path = root / relative_path
    try:
        status = path.lstat()
    except FileNotFoundError:
        return TrackedFileSnapshot(
            relative_path,
            index_mode,
            index_object,
            "missing",
            None,
            None,
        )
    except OSError as error:
        raise _SnapshotError from error
    mode = stat.S_IMODE(status.st_mode)
    if stat.S_ISREG(status.st_mode):
        digest = _digest_regular_file(path, status)
        if not _tracked_parent_exists_safely(root, relative_path):
            raise _SnapshotError
        return TrackedFileSnapshot(
            relative_path,
            index_mode,
            index_object,
            "regular",
            mode,
            digest,
        )
    if stat.S_ISLNK(status.st_mode):
        digest = _digest_symlink_target(path, status)
        if not _tracked_parent_exists_safely(root, relative_path):
            raise _SnapshotError
        return TrackedFileSnapshot(
            relative_path,
            index_mode,
            index_object,
            "symlink",
            mode,
            digest,
        )
    return TrackedFileSnapshot(
        relative_path,
        index_mode,
        index_object,
        "other",
        mode,
        None,
    )


def _tracked_parent_exists_safely(root: Path, relative_path: str) -> bool:
    current = root
    for component in Path(relative_path).parts[:-1]:
        current /= component
        try:
            status = current.lstat()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise _SnapshotError from error
        if not stat.S_ISDIR(status.st_mode) or _is_reparse_point(status):
            raise _SnapshotError
    return True


def _capture_protected_files(root: Path) -> tuple[ProtectedFileSnapshot, ...]:
    snapshots: list[ProtectedFileSnapshot] = []
    for name in _PROTECTED_NAMES:
        path = _normalized_absolute(root / name)
        try:
            status = path.lstat()
        except FileNotFoundError:
            snapshots.append(ProtectedFileSnapshot(str(path), "missing", None, None))
            continue
        except OSError:
            snapshots.append(ProtectedFileSnapshot(str(path), "unsafe", None, None))
            continue
        mode = stat.S_IMODE(status.st_mode)
        if not stat.S_ISREG(status.st_mode):
            snapshots.append(ProtectedFileSnapshot(str(path), "unsafe", mode, None))
            continue
        try:
            digest = _digest_regular_file(path, status)
        except _SnapshotError:
            snapshots.append(ProtectedFileSnapshot(str(path), "unsafe", mode, None))
            continue
        snapshots.append(ProtectedFileSnapshot(str(path), "regular", mode, digest))
    return tuple(snapshots)


def _digest_regular_file(path: Path, expected: os.stat_result) -> str:
    no_follow = getattr(fs, "O_NOFOLLOW", None)
    non_blocking = getattr(fs, "O_NONBLOCK", None)
    if type(no_follow) is not int or type(non_blocking) is not int:
        raise _SnapshotError
    try:
        descriptor = fs.open(path, os.O_RDONLY | no_follow | non_blocking)
    except OSError as error:
        raise _SnapshotError from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not _same_file_observation(expected, before):
            raise _SnapshotError
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, _READ_CHUNK_BYTES):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if not _same_file_observation(before, after):
            raise _SnapshotError
        return digest.hexdigest()
    except OSError as error:
        raise _SnapshotError from error
    finally:
        os.close(descriptor)


def _digest_symlink_target(path: Path, expected: os.stat_result) -> str:
    try:
        target = os.readlink(path)
        after = path.lstat()
    except OSError as error:
        raise _SnapshotError from error
    if not stat.S_ISLNK(after.st_mode) or not _same_file_observation(expected, after):
        raise _SnapshotError
    return hashlib.sha256(os.fsencode(target)).hexdigest()


def _same_file_observation(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_size,
        left.st_mtime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
        right.st_mtime_ns,
    )


def _is_reparse_point(status: os.stat_result) -> bool:
    return bool(getattr(status, "st_file_attributes", 0) & 0x400)


def _complete_stdout(process: ExecutedProcess) -> bytes | None:
    if process.stdout is None or process.stdout.omitted_bytes != 0:
        return None
    if len(process.stdout.tail) > CAPTURE_LIMIT_BYTES:
        return None
    return process.stdout.tail


def _lexically_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _normalized_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(path)))


def _baseline_failure(
    processes: list[ExecutedProcess],
    message: str,
) -> RepositoryBaselineResult:
    return RepositoryBaselineResult(None, tuple(processes), _unsafe_failure(message))


def _unsafe_failure(message: str) -> EnvironmentFailureObservation:
    return EnvironmentFailureObservation(
        code="unsafe_repository_environment",
        message=message,
        hint="Repair repository safety state outside pyrepo-check, then retry.",
    )


def _repository_changed(message: str) -> EnvironmentFailureObservation:
    return EnvironmentFailureObservation(
        code="repository_state_changed",
        message=message,
        hint="Inspect repository changes before trusting this run.",
    )
