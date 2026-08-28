"""Resolve controller-owned helper executables outside the selected project."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat


@dataclass(frozen=True)
class _ExecutableIdentity:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int


@dataclass(frozen=True)
class ControllerExecutable:
    path: Path
    identity: _ExecutableIdentity

    def path_for_use(self) -> Path:
        """Return the pinned path only while its stable identity still matches."""
        try:
            current = _identity(self.path.stat())
        except OSError as error:
            raise OSError(f"controller helper identity changed: {self.path}") from error
        if current != self.identity:
            raise OSError(f"controller helper identity changed: {self.path}")
        return self.path


@dataclass(frozen=True)
class ControllerTools:
    uv: ControllerExecutable | None
    git: ControllerExecutable | None


def resolve_controller_tools(
    root: Path,
    *,
    path: str | None = None,
) -> ControllerTools:
    """Resolve safe canonical helpers once from absolute external PATH entries."""
    project_root = _normalized_absolute(root)
    search_path = os.environ.get("PATH", "") if path is None else path
    return ControllerTools(
        uv=_resolve_executable("uv", project_root, search_path),
        git=_resolve_executable("git", project_root, search_path),
    )


def _resolve_executable(
    name: str,
    project_root: Path,
    search_path: str,
) -> ControllerExecutable | None:
    for raw_entry in search_path.split(os.pathsep):
        if not raw_entry:
            continue
        entry = Path(raw_entry)
        if not entry.is_absolute():
            continue
        lexical_candidate = _normalized_absolute(entry / name)
        if _contained_by(lexical_candidate, project_root):
            continue
        try:
            canonical = lexical_candidate.resolve(strict=True)
            status = canonical.stat()
        except (OSError, RuntimeError):
            continue
        if _contained_by(canonical, project_root):
            continue
        if not stat.S_ISREG(status.st_mode) or not os.access(canonical, os.X_OK):
            continue
        return ControllerExecutable(canonical, _identity(status))
    return None


def _identity(status: os.stat_result) -> _ExecutableIdentity:
    return _ExecutableIdentity(
        device=status.st_dev,
        inode=status.st_ino,
        mode=status.st_mode,
        size=status.st_size,
        modified_ns=status.st_mtime_ns,
    )


def _normalized_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(str(path))))


def _contained_by(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True
