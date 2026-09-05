"""Descriptor-oriented filesystem operations with native Windows safety."""

from __future__ import annotations

from collections.abc import Iterator
import os
from pathlib import Path
from types import TracebackType
from typing import Protocol, cast


class PlatformSafetyError(OSError):
    """The platform cannot provide a required filesystem safety guarantee."""


class ScandirEntry(Protocol):
    """The subset of ``os.DirEntry`` consumed by pyrepo-check."""

    @property
    def name(self) -> str: ...

    @property
    def path(self) -> str: ...

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result: ...


class ScandirIterator(Iterator[ScandirEntry], Protocol):
    """A closeable, context-managed directory iterator."""

    def __enter__(self) -> ScandirIterator: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...

    def close(self) -> None: ...


if os.name == "nt":
    # These bits are outside the CRT open-flag range and are consumed by the adapter.
    O_NOFOLLOW = 0x1000_0000
    O_NONBLOCK = 0x2000_0000
    O_DIRECTORY = 0x4000_0000
else:
    O_NOFOLLOW = os.O_NOFOLLOW
    O_NONBLOCK = os.O_NONBLOCK
    O_DIRECTORY = os.O_DIRECTORY


def open(
    path: str | Path,
    flags: int,
    mode: int = 0o777,
    *,
    dir_fd: int | None = None,
) -> int:
    """Open a path while preserving no-follow and descriptor-relative semantics."""
    if os.name == "nt":
        from pyrepo_check import _windows_files

        return _windows_files.open(path, flags, mode, dir_fd=dir_fd)
    if dir_fd is None:
        return os.open(path, flags, mode)
    return os.open(path, flags, mode, dir_fd=dir_fd)


def stat(
    path: str | Path,
    *,
    dir_fd: int | None = None,
    follow_symlinks: bool = False,
) -> os.stat_result:
    """Stat a path relative to a held directory without following its leaf."""
    if os.name == "nt":
        from pyrepo_check import _windows_files

        return _windows_files.stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
    if dir_fd is None:
        return os.stat(path, follow_symlinks=follow_symlinks)
    return os.stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)


def mkdir(
    path: str | Path,
    mode: int = 0o777,
    *,
    dir_fd: int | None = None,
) -> None:
    """Create a private directory exclusively."""
    if os.name == "nt":
        from pyrepo_check import _windows_files

        _windows_files.mkdir(path, mode, dir_fd=dir_fd)
        return
    if dir_fd is None:
        os.mkdir(path, mode)
    else:
        os.mkdir(path, mode, dir_fd=dir_fd)


def scandir(path: int | str | Path) -> ScandirIterator:
    """Enumerate a directory, remaining bound to a supplied descriptor."""
    if os.name == "nt":
        from pyrepo_check import _windows_files

        return _windows_files.scandir(path)
    return cast(ScandirIterator, os.scandir(path))


def unlink(path: str | Path, *, dir_fd: int | None = None) -> None:
    """Unlink a file relative to a held directory."""
    if os.name == "nt":
        from pyrepo_check import _windows_files

        _windows_files.unlink(path, dir_fd=dir_fd)
        return
    if dir_fd is None:
        os.unlink(path)
    else:
        os.unlink(path, dir_fd=dir_fd)


def verify_private(descriptor: int) -> None:
    """Verify that only the current Windows user owns and can access a handle."""
    if os.name != "nt":
        return
    from pyrepo_check import _windows_files

    _windows_files.verify_private(descriptor)


def open_for_cleanup(path: str | Path, *, dir_fd: int | None = None) -> int:
    """Open the exact leaf for deletion while preventing its replacement."""
    if os.name != "nt":
        if dir_fd is None:
            return os.open(path, O_NOFOLLOW | O_NONBLOCK | os.O_RDONLY)
        return os.open(path, O_NOFOLLOW | O_NONBLOCK | os.O_RDONLY, dir_fd=dir_fd)
    from pyrepo_check import _windows_files

    return _windows_files.open_for_cleanup(path, dir_fd=dir_fd)


def delete_open_file(descriptor: int) -> None:
    """Mark the exact held Windows handle for deletion."""
    if os.name != "nt":
        raise PlatformSafetyError("deletion by an open descriptor is unavailable")
    from pyrepo_check import _windows_files

    _windows_files.delete_open_file(descriptor)
