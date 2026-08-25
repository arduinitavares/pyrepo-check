"""Safe bounded reads for invocation-owned evidence artifacts."""

from __future__ import annotations

import errno
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Never, cast


_MAX_JSON_NESTING = 64
_READ_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class FileDigest:
    """The size and SHA-256 digest of one safely observed regular file."""

    size: int
    sha256: str


@dataclass(frozen=True)
class FileIdentity:
    """The stable filesystem identity of one regular file."""

    device: int
    inode: int


@dataclass(frozen=True)
class RegularFileCopy:
    """Digest and held-descriptor identity verified by one exclusive copy."""

    digest: FileDigest
    destination_identity: FileIdentity


@dataclass(frozen=True)
class _DirectoryIdentity:
    device: int
    inode: int


@dataclass(frozen=True)
class _DestinationMetadata:
    device: int
    inode: int
    owner: int
    mode: int
    link_count: int


class _UnsafePathError(OSError):
    """Raised when descriptor-safe regular-file reading is unavailable or fails."""


class _BoundedReadError(OSError):
    """Raised when a regular evidence file exceeds its byte budget."""


class _DigestMismatchError(OSError):
    """Raised when a copied artifact does not match its source digest."""


def read_regular_file(
    path: Path,
    *,
    max_bytes: int,
    dir_fd: int | None = None,
) -> bytes:
    """Read one regular file without following links and within its byte budget."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    non_blocking = getattr(os, "O_NONBLOCK", None)
    if type(no_follow) is not int or type(non_blocking) is not int:
        raise _UnsafePathError("safe no-follow file opening is unavailable")
    try:
        if dir_fd is None:
            descriptor = os.open(path, os.O_RDONLY | no_follow | non_blocking)
        else:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | no_follow | non_blocking,
                dir_fd=dir_fd,
            )
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise _UnsafePathError(f"path is not a regular file: {path.name}") from error
        raise
    try:
        file_status = os.fstat(descriptor)
        if not stat.S_ISREG(file_status.st_mode):
            raise _UnsafePathError(f"path is not a regular file: {path.name}")
        if file_status.st_size > max_bytes:
            raise _BoundedReadError(f"{path.name} exceeds the {max_bytes}-byte limit")
        content = bytearray()
        while len(content) <= max_bytes:
            remaining = max_bytes + 1 - len(content)
            chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > max_bytes:
            raise _BoundedReadError(f"{path.name} exceeds the {max_bytes}-byte limit")
        _verify_unchanged(descriptor, path.name, file_status)
        return bytes(content)
    finally:
        os.close(descriptor)


def load_bounded_json(content: bytes, *, max_nesting: int = _MAX_JSON_NESTING) -> object:
    """Parse JSON with a bounded nesting level and no non-finite constants."""
    depth = 0
    in_string = False
    escaped = False
    for byte in content:
        if in_string:
            if escaped:
                escaped = False
            elif byte == ord("\\"):
                escaped = True
            elif byte == ord('"'):
                in_string = False
            continue
        if byte == ord('"'):
            in_string = True
        elif byte in {ord("{"), ord("[")}:
            depth += 1
            if depth > max_nesting:
                raise ValueError(f"JSON nesting exceeds the {max_nesting}-level limit")
        elif byte in {ord("}"), ord("]")}:
            depth -= 1
    try:
        return json.loads(content, parse_constant=_reject_json_constant)
    except RecursionError as error:
        raise ValueError("JSON parsing exceeded the recursion limit") from error


def _reject_json_constant(constant: str) -> Never:
    raise ValueError(f"JSON constant {constant} is not permitted")


def digest_regular_file(
    path: Path,
    *,
    max_bytes: int,
    dir_fd: int | None = None,
) -> FileDigest:
    """Stream and digest one bounded no-follow regular file."""
    descriptor, initial_status = _open_regular_file(path, max_bytes=max_bytes, dir_fd=dir_fd)
    try:
        digest, size = _stream_digest(descriptor, path.name, initial_status, max_bytes)
    finally:
        os.close(descriptor)
    return FileDigest(size=size, sha256=digest.hexdigest())


def copy_regular_file_with_digest(
    source_path: Path,
    destination_path: Path,
    *,
    max_bytes: int,
    source_dir_fd: int | None = None,
    destination_dir_fd: int | None = None,
) -> FileDigest:
    """Copy one bounded regular file, then verify the destination digest."""
    return copy_regular_file(
        source_path,
        destination_path,
        max_bytes=max_bytes,
        source_dir_fd=source_dir_fd,
        destination_dir_fd=destination_dir_fd,
    ).digest


def copy_regular_file(
    source_path: Path,
    destination_path: Path,
    *,
    max_bytes: int,
    source_dir_fd: int | None = None,
    destination_dir_fd: int | None = None,
) -> RegularFileCopy:
    """Copy a bounded regular file and return its verified digest and identity."""
    source_descriptor, initial_status = _open_regular_file(
        source_path,
        max_bytes=max_bytes,
        dir_fd=source_dir_fd,
    )
    destination_descriptor: int | None = None
    parent_descriptor: int | None = None
    close_parent_descriptor = False
    verify_parent_path = destination_dir_fd is None
    try:
        (
            destination_descriptor,
            parent_descriptor,
            close_parent_descriptor,
        ) = _create_destination(
            destination_path,
            destination_dir_fd=destination_dir_fd,
        )
        parent_identity = _directory_identity(parent_descriptor)
        destination_metadata = _destination_metadata(
            os.fstat(destination_descriptor),
            destination_path.name,
        )
        digest, size = _stream_copy(
            source_descriptor,
            destination_descriptor,
            source_path.name,
            initial_status,
            max_bytes,
        )
        os.fsync(destination_descriptor)

        _verify_destination_metadata(
            os.fstat(destination_descriptor),
            destination_metadata,
            destination_path.name,
        )

        _verify_parent_binding(
            destination_path,
            parent_descriptor,
            parent_identity,
            require_path_match=verify_parent_path,
        )
        verification_descriptor, verification_status = _open_regular_file(
            Path(destination_path.name),
            max_bytes=max_bytes,
            dir_fd=parent_descriptor,
        )
        try:
            _verify_destination_metadata(
                verification_status,
                destination_metadata,
                destination_path.name,
            )
            destination_hash, destination_size = _stream_digest(
                verification_descriptor,
                destination_path.name,
                verification_status,
                max_bytes,
            )
        finally:
            os.close(verification_descriptor)

        _verify_parent_binding(
            destination_path,
            parent_descriptor,
            parent_identity,
            require_path_match=verify_parent_path,
        )
        source_digest = FileDigest(size=size, sha256=digest.hexdigest())
        destination_digest = FileDigest(
            size=destination_size,
            sha256=destination_hash.hexdigest(),
        )
        if destination_digest != source_digest:
            raise _DigestMismatchError(f"{destination_path.name} digest mismatch after copy")
        return RegularFileCopy(
            digest=source_digest,
            destination_identity=FileIdentity(
                device=destination_metadata.device,
                inode=destination_metadata.inode,
            ),
        )
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        if close_parent_descriptor and parent_descriptor is not None:
            os.close(parent_descriptor)
        os.close(source_descriptor)


def _open_regular_file(
    path: Path,
    *,
    max_bytes: int,
    dir_fd: int | None,
) -> tuple[int, os.stat_result]:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    non_blocking = getattr(os, "O_NONBLOCK", None)
    if type(no_follow) is not int or type(non_blocking) is not int:
        raise _UnsafePathError("safe no-follow file opening is unavailable")
    try:
        if dir_fd is None:
            descriptor = os.open(path, os.O_RDONLY | no_follow | non_blocking)
        else:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | no_follow | non_blocking,
                dir_fd=dir_fd,
            )
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise _UnsafePathError(f"path is not a regular file: {path.name}") from error
        raise
    try:
        file_status = os.fstat(descriptor)
        if not stat.S_ISREG(file_status.st_mode):
            raise _UnsafePathError(f"path is not a regular file: {path.name}")
        if file_status.st_size > max_bytes:
            raise _BoundedReadError(f"{path.name} exceeds the {max_bytes}-byte limit")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, file_status


def _stream_digest(
    descriptor: int,
    filename: str,
    initial_status: os.stat_result,
    max_bytes: int,
) -> tuple[hashlib._Hash, int]:
    digest = hashlib.sha256()
    size = 0
    while size < initial_status.st_size:
        chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, initial_status.st_size - size))
        if not chunk:
            raise _UnsafePathError(f"{filename} changed during read")
        size += len(chunk)
        if size > max_bytes:
            raise _BoundedReadError(f"{filename} exceeds the {max_bytes}-byte limit")
        digest.update(chunk)
    _verify_unchanged(descriptor, filename, initial_status)
    return digest, size


def _stream_copy(
    source_descriptor: int,
    destination_descriptor: int,
    filename: str,
    initial_status: os.stat_result,
    max_bytes: int,
) -> tuple[hashlib._Hash, int]:
    digest = hashlib.sha256()
    size = 0
    while size < initial_status.st_size:
        chunk = os.read(
            source_descriptor,
            min(_READ_CHUNK_BYTES, initial_status.st_size - size),
        )
        if not chunk:
            raise _UnsafePathError(f"{filename} changed during read")
        size += len(chunk)
        if size > max_bytes:
            raise _BoundedReadError(f"{filename} exceeds the {max_bytes}-byte limit")
        digest.update(chunk)
        _write_all(destination_descriptor, chunk)
    _verify_unchanged(source_descriptor, filename, initial_status)
    return digest, size


def _verify_unchanged(
    descriptor: int,
    filename: str,
    initial_status: os.stat_result,
) -> None:
    final_status = os.fstat(descriptor)
    if (
        final_status.st_dev != initial_status.st_dev
        or final_status.st_ino != initial_status.st_ino
        or final_status.st_size != initial_status.st_size
        or final_status.st_mtime_ns != initial_status.st_mtime_ns
        or final_status.st_ctime_ns != initial_status.st_ctime_ns
    ):
        raise _UnsafePathError(f"{filename} changed during read")


def _create_destination(
    destination_path: Path,
    *,
    destination_dir_fd: int | None,
) -> tuple[int, int, bool]:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    non_blocking = getattr(os, "O_NONBLOCK", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if (
        type(no_follow) is not int
        or type(non_blocking) is not int
        or type(directory) is not int
    ):
        raise _UnsafePathError("safe no-follow file opening is unavailable")
    parent_descriptor = destination_dir_fd
    close_parent_descriptor = False
    try:
        if destination_dir_fd is None:
            parent_descriptor = os.open(
                destination_path.parent,
                os.O_RDONLY | directory | no_follow | non_blocking,
            )
            close_parent_descriptor = True
        if parent_descriptor is None:
            raise _UnsafePathError("safe destination directory opening is unavailable")
        _directory_identity(parent_descriptor)
        descriptor = os.open(
            destination_path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow | non_blocking,
            0o600,
            dir_fd=parent_descriptor,
        )
    except BaseException:
        if close_parent_descriptor and parent_descriptor is not None:
            os.close(parent_descriptor)
        raise
    return descriptor, parent_descriptor, close_parent_descriptor


def _directory_identity(descriptor: int) -> _DirectoryIdentity:
    file_status = os.fstat(descriptor)
    if not stat.S_ISDIR(file_status.st_mode):
        raise _UnsafePathError("destination parent is not a directory")
    return _DirectoryIdentity(file_status.st_dev, file_status.st_ino)


def _destination_metadata(
    file_status: os.stat_result,
    filename: str,
) -> _DestinationMetadata:
    if not stat.S_ISREG(file_status.st_mode):
        raise _UnsafePathError(f"{filename} destination is not a regular file")
    if file_status.st_uid != _effective_user_id():
        raise _UnsafePathError(f"{filename} destination owner is not the effective user")
    mode = stat.S_IMODE(file_status.st_mode)
    if mode & ~0o600:
        raise _UnsafePathError(f"{filename} destination permissions are broader than 0600")
    if file_status.st_nlink != 1:
        raise _UnsafePathError(f"{filename} destination link count is not one")
    return _DestinationMetadata(
        device=file_status.st_dev,
        inode=file_status.st_ino,
        owner=file_status.st_uid,
        mode=mode,
        link_count=file_status.st_nlink,
    )


def _effective_user_id() -> int:
    get_effective_user_id = getattr(os, "geteuid", None)
    if not callable(get_effective_user_id):
        raise _UnsafePathError("effective-user ownership validation is unavailable")
    return cast(Callable[[], int], get_effective_user_id)()


def _verify_destination_metadata(
    file_status: os.stat_result,
    expected: _DestinationMetadata,
    filename: str,
) -> None:
    observed = _destination_metadata(file_status, filename)
    if observed != expected:
        raise _UnsafePathError(f"{filename} destination changed during verification")


def _verify_parent_binding(
    destination_path: Path,
    parent_descriptor: int,
    expected: _DirectoryIdentity,
    *,
    require_path_match: bool,
) -> None:
    if _directory_identity(parent_descriptor) != expected:
        raise _UnsafePathError("destination parent changed during verification")
    if not require_path_match:
        return
    try:
        current = os.stat(destination_path.parent, follow_symlinks=False)
    except OSError as error:
        raise _UnsafePathError("destination parent changed during verification") from error
    if (
        not stat.S_ISDIR(current.st_mode)
        or _DirectoryIdentity(current.st_dev, current.st_ino) != expected
    ):
        raise _UnsafePathError("destination parent changed during verification")


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("artifact destination write made no progress")
        offset += written


_read_regular_file = read_regular_file
_load_bounded_json = load_bounded_json
