"""Stage and revalidate the probed Coverage package before repository code runs."""

from __future__ import annotations

from dataclasses import dataclass
import os

from pyrepo_check import filesystem as fs
from pathlib import Path
import secrets
import stat
from typing import cast

from pyrepo_check.artifact_safety import FileDigest, copy_regular_file, digest_regular_file
from pyrepo_check.execution_workspace import VerifiedRunWorkspace


_MAX_FILE_BYTES = 32 * 1024 * 1024
_MAX_PACKAGE_BYTES = 128 * 1024 * 1024
_MAX_ENTRIES = 4096
_MAX_DEPTH = 32
_MAX_LAUNCHER_BYTES = 65_536


@dataclass(frozen=True)
class StagedCoverageDependency:
    module_root: Path
    dependency_root: Path
    module_root_identity: tuple[int, int]
    package_name: str
    files: tuple[tuple[Path, FileDigest], ...]
    directories: tuple[Path, ...]
    launcher_path: Path
    launcher_digest: FileDigest


def stage_coverage_dependency(
    *,
    origin: Path,
    environment_root: Path,
    workspace: VerifiedRunWorkspace,
) -> StagedCoverageDependency:
    """Copy the exact probed Coverage package into the held run workspace."""
    normalized_origin = _normalized_absolute(origin)
    normalized_environment = _normalized_absolute(environment_root)
    try:
        relative_origin = normalized_origin.relative_to(normalized_environment)
    except ValueError as error:
        raise OSError("Coverage origin escapes the Repository Environment") from error
    if relative_origin.name != "__init__.py" or relative_origin.parent.name != "coverage":
        raise OSError("Coverage origin is not coverage/__init__.py")

    workspace.verify("before Coverage dependency staging")
    source_descriptor = _open_relative_directory(
        normalized_environment,
        relative_origin.parent,
    )
    module_root_name = f"coverage-dependency-{secrets.token_hex(16)}"
    package_name = "coverage"
    module_root = workspace.workspace.path / module_root_name
    launcher_name = f"coverage-json-launcher-{secrets.token_hex(16)}.py"
    launcher_path = workspace.workspace.path / launcher_name
    destination_descriptor: int | None = None
    package_descriptor: int | None = None
    module_root_identity: tuple[int, int] | None = None
    try:
        fs.mkdir(module_root_name, mode=0o700, dir_fd=workspace.descriptor)
        destination_descriptor = fs.open(
            module_root_name,
            _directory_open_flags(),
            dir_fd=workspace.descriptor,
        )
        module_root_identity = _directory_identity(destination_descriptor)
        fs.mkdir(package_name, mode=0o700, dir_fd=destination_descriptor)
        package_descriptor = fs.open(
            package_name,
            _directory_open_flags(),
            dir_fd=destination_descriptor,
        )
        files: list[tuple[Path, FileDigest]] = []
        directories: list[Path] = [Path(package_name)]
        counts = [0, 0]
        _copy_tree(
            source_descriptor,
            package_descriptor,
            relative=Path(package_name),
            depth=0,
            files=files,
            directories=directories,
            counts=counts,
        )
        copied_launcher = copy_regular_file(
            Path(__file__).with_name("_coverage_json_launcher.py"),
            Path(launcher_name),
            max_bytes=_MAX_LAUNCHER_BYTES,
            destination_dir_fd=workspace.descriptor,
        )
    finally:
        os.close(source_descriptor)
        if package_descriptor is not None:
            os.close(package_descriptor)
        if destination_descriptor is not None:
            os.close(destination_descriptor)
    if module_root_identity is None:
        raise AssertionError("Coverage module-root identity is unavailable")
    staged = StagedCoverageDependency(
        module_root=module_root,
        dependency_root=normalized_origin.parent.parent,
        module_root_identity=module_root_identity,
        package_name=package_name,
        files=tuple(files),
        directories=tuple(directories),
        launcher_path=launcher_path,
        launcher_digest=copied_launcher.digest,
    )
    ensure_staged_coverage_dependency(staged, workspace=workspace)
    workspace.verify("after Coverage dependency staging")
    return staged


def ensure_staged_coverage_dependency(
    staged: StagedCoverageDependency,
    *,
    workspace: VerifiedRunWorkspace,
) -> None:
    """Reject any post-staging package or launcher mutation before JSON execution."""
    workspace.verify("before staged Coverage dependency validation")
    module_root_descriptor = fs.open(
        staged.module_root.name,
        _directory_open_flags(),
        dir_fd=workspace.descriptor,
    )
    try:
        if _directory_identity(module_root_descriptor) != staged.module_root_identity:
            raise OSError("staged Coverage module-root identity changed")
        actual_files: dict[Path, FileDigest] = {}
        actual_directories: set[Path] = set()
        _snapshot_tree(
            module_root_descriptor,
            relative=Path(),
            depth=0,
            files=actual_files,
            directories=actual_directories,
        )
    finally:
        os.close(module_root_descriptor)
    expected_files = dict(staged.files)
    if actual_directories != set(staged.directories) or set(actual_files) != set(
        expected_files
    ):
        raise OSError("staged Coverage dependency shape changed")
    for relative, expected in expected_files.items():
        if actual_files[relative] != expected:
            raise OSError(f"staged Coverage dependency changed: {relative}")
    launcher = digest_regular_file(
        Path(staged.launcher_path.name),
        max_bytes=_MAX_LAUNCHER_BYTES,
        dir_fd=workspace.descriptor,
    )
    if launcher != staged.launcher_digest:
        raise OSError("staged Coverage JSON launcher changed")
    workspace.verify("after staged Coverage dependency validation")


def coverage_json_staged_command(
    *,
    python_prefix: tuple[str, ...],
    staged: StagedCoverageDependency,
    coverage_arguments: tuple[str, ...],
) -> tuple[str, ...]:
    return (
        *python_prefix,
        "-S",
        str(staged.launcher_path),
        "--module-root",
        str(staged.module_root),
        "--dependency-root",
        str(staged.dependency_root),
        "--",
        *coverage_arguments,
    )


def _copy_tree(
    source_descriptor: int,
    destination_descriptor: int,
    *,
    relative: Path,
    depth: int,
    files: list[tuple[Path, FileDigest]],
    directories: list[Path],
    counts: list[int],
) -> None:
    if depth > _MAX_DEPTH:
        raise OSError("Coverage package exceeds staging depth limit")
    initial_identity = _directory_identity(source_descriptor)
    entries = sorted(fs.scandir(source_descriptor), key=lambda entry: entry.name)
    for entry in entries:
        counts[0] += 1
        if counts[0] > _MAX_ENTRIES:
            raise OSError("Coverage package exceeds staging entry limit")
        status = entry.stat(follow_symlinks=False)
        child_relative = relative / entry.name
        if stat.S_ISDIR(status.st_mode):
            fs.mkdir(entry.name, mode=0o700, dir_fd=destination_descriptor)
            source_child = fs.open(
                entry.name,
                _directory_open_flags(),
                dir_fd=source_descriptor,
            )
            destination_child = fs.open(
                entry.name,
                _directory_open_flags(),
                dir_fd=destination_descriptor,
            )
            try:
                directories.append(child_relative)
                _copy_tree(
                    source_child,
                    destination_child,
                    relative=child_relative,
                    depth=depth + 1,
                    files=files,
                    directories=directories,
                    counts=counts,
                )
            finally:
                os.close(source_child)
                os.close(destination_child)
        elif stat.S_ISREG(status.st_mode):
            counts[1] += status.st_size
            if status.st_size > _MAX_FILE_BYTES or counts[1] > _MAX_PACKAGE_BYTES:
                raise OSError("Coverage package exceeds staging byte limit")
            copied = copy_regular_file(
                Path(entry.name),
                Path(entry.name),
                max_bytes=_MAX_FILE_BYTES,
                source_dir_fd=source_descriptor,
                destination_dir_fd=destination_descriptor,
            )
            files.append((child_relative, copied.digest))
        else:
            raise OSError("Coverage package contains a non-regular entry")
    if _directory_identity(source_descriptor) != initial_identity:
        raise OSError("Coverage package directory identity changed")
    final_names = tuple(sorted(entry.name for entry in fs.scandir(source_descriptor)))
    if final_names != tuple(entry.name for entry in entries):
        raise OSError("Coverage package directory entries changed")


def _snapshot_tree(
    descriptor: int,
    *,
    relative: Path,
    depth: int,
    files: dict[Path, FileDigest],
    directories: set[Path],
) -> None:
    if depth > _MAX_DEPTH:
        raise OSError("staged Coverage dependency exceeds validation depth limit")
    entries = sorted(fs.scandir(descriptor), key=lambda entry: entry.name)
    if len(files) + len(directories) + len(entries) > _MAX_ENTRIES:
        raise OSError("staged Coverage dependency exceeds validation entry limit")
    for entry in entries:
        status = entry.stat(follow_symlinks=False)
        child_relative = relative / entry.name
        if stat.S_ISDIR(status.st_mode):
            child_descriptor = fs.open(
                entry.name,
                _directory_open_flags(),
                dir_fd=descriptor,
            )
            try:
                directories.add(child_relative)
                _snapshot_tree(
                    child_descriptor,
                    relative=child_relative,
                    depth=depth + 1,
                    files=files,
                    directories=directories,
                )
            finally:
                os.close(child_descriptor)
        elif stat.S_ISREG(status.st_mode):
            files[child_relative] = digest_regular_file(
                Path(entry.name),
                max_bytes=_MAX_FILE_BYTES,
                dir_fd=descriptor,
            )
        else:
            raise OSError("staged Coverage dependency contains a non-regular entry")


def _open_relative_directory(root: Path, relative: Path) -> int:
    descriptor = fs.open(root, _directory_open_flags())
    try:
        for component in relative.parts:
            next_descriptor = fs.open(
                component,
                _directory_open_flags(),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | cast(int, getattr(fs, "O_DIRECTORY"))
        | cast(int, getattr(fs, "O_NOFOLLOW"))
        | cast(int, getattr(fs, "O_NONBLOCK"))
    )


def _directory_identity(descriptor: int) -> tuple[int, int]:
    status = os.fstat(descriptor)
    if not stat.S_ISDIR(status.st_mode):
        raise OSError("Coverage package directory is not regular")
    return status.st_dev, status.st_ino


def _normalized_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(str(path))))
