from __future__ import annotations

import os
from pathlib import Path
import threading
from typing import Callable, TypeVar, cast

import pytest

from pyrepo_check import filesystem
import pyrepo_check.artifact_safety as artifact_safety
from tests.support import symlink_or_skip


_T = TypeVar("_T")
_HAS_POSIX_FIFO = callable(getattr(os, "mkfifo", None))
_HAS_POSIX_PWRITE = callable(getattr(os, "pwrite", None))
_POSIX_FIFO = pytest.mark.skipif(
    not _HAS_POSIX_FIFO,
    reason="exercises POSIX FIFO behavior; native Windows file coverage is separate",
)
_POSIX_PWRITE = pytest.mark.skipif(
    not _HAS_POSIX_PWRITE,
    reason="exercises POSIX positional writes; Windows uses a native file handle backend",
)
_POSIX_RENAME_RACE = pytest.mark.skipif(
    os.name == "nt",
    reason="requires replacing an open POSIX directory; Windows handle sharing blocks that race",
)
_OS_NONBLOCK = filesystem.O_NONBLOCK
_MKFIFO = cast(Callable[[Path], None], getattr(os, "mkfifo", None))
_PWRITE = cast(Callable[[int, bytes, int], int], getattr(os, "pwrite", None))


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


@pytest.mark.parametrize(
    "limit",
    (128 * 1024 * 1024, 4 * 1024),
    ids=("artifact", "writer-marker"),
)
def test_regular_file_reader_accepts_exact_cap_and_rejects_one_over(
    tmp_path: Path,
    limit: int,
) -> None:
    exact = tmp_path / "artifact.json"
    with exact.open("wb") as file:
        file.truncate(limit)
    assert len(artifact_safety.read_regular_file(exact, max_bytes=limit)) == limit

    oversized = tmp_path / "oversized-artifact.json"
    with oversized.open("wb") as file:
        file.truncate(limit + 1)
    with pytest.raises(artifact_safety._BoundedReadError, match="exceeds"):
        artifact_safety.read_regular_file(oversized, max_bytes=limit)


def test_sparse_oversized_file_is_rejected_before_any_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "artifact.json"
    with artifact.open("wb") as file:
        file.truncate(2)

    def forbidden_read(_descriptor: int, _size: int) -> bytes:
        raise AssertionError("oversized sparse file must not be read")

    monkeypatch.setattr(artifact_safety.os, "read", forbidden_read)
    with pytest.raises(artifact_safety._BoundedReadError, match="exceeds"):
        artifact_safety.read_regular_file(artifact, max_bytes=1)


def test_regular_file_growth_after_fstat_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "marker.json"
    marker.write_bytes(b"x")
    original_read = artifact_safety.os.read
    grown = False

    def grow_before_first_read(descriptor: int, size: int) -> bytes:
        nonlocal grown
        if not grown:
            grown = True
            with marker.open("ab") as file:
                file.write(b"!")
        return original_read(descriptor, size)

    monkeypatch.setattr(artifact_safety.os, "read", grow_before_first_read)
    expected = PermissionError if os.name == "nt" else artifact_safety._BoundedReadError
    with pytest.raises(expected, match=None if os.name == "nt" else "exceeds"):
        artifact_safety.read_regular_file(marker, max_bytes=1)
    if os.name == "nt":
        assert marker.read_bytes() == b"x"


def test_regular_file_same_size_mutation_during_read_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "marker.json"
    marker.write_bytes(b"original")
    initial_status = marker.stat()
    original_read = artifact_safety.os.read
    mutated = False

    def mutate_after_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        content = original_read(descriptor, size)
        if content and not mutated:
            mutated = True
            marker.write_bytes(b"changed!")
            os.utime(
                marker,
                ns=(initial_status.st_atime_ns, initial_status.st_mtime_ns + 1_000_000),
            )
        return content

    monkeypatch.setattr(artifact_safety.os, "read", mutate_after_read)
    expected = PermissionError if os.name == "nt" else artifact_safety._UnsafePathError
    with pytest.raises(expected, match=None if os.name == "nt" else "changed during read"):
        artifact_safety.read_regular_file(marker, max_bytes=8)
    if os.name == "nt":
        assert marker.read_bytes() == b"original"


def test_regular_file_reader_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}")
    artifact = tmp_path / "artifact.json"
    symlink_or_skip(artifact, target)

    with pytest.raises(artifact_safety._UnsafePathError, match="not a regular file"):
        artifact_safety.read_regular_file(artifact, max_bytes=1024)


@_POSIX_FIFO
def test_regular_file_reader_rejects_fifo_promptly(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.json"
    _MKFIFO(artifact)

    with pytest.raises(artifact_safety._UnsafePathError, match="not a regular file"):
        _run_fifo_call_with_watchdog(
            lambda: artifact_safety.read_regular_file(artifact, max_bytes=1024), artifact
        )


@pytest.mark.parametrize(
    ("payload", "accepted"),
    (
        (b"[" * 64 + b"0" + b"]" * 64, True),
        (b"[" * 65 + b"0" + b"]" * 65, False),
        (b'{"value":"[[[\\\"{[]}\\\"]]]"}', True),
    ),
    ids=("depth-64", "depth-65", "braces-and-escapes-in-string"),
)
def test_json_nesting_limit_respects_strings_and_escapes(
    payload: bytes,
    accepted: bool,
) -> None:
    if accepted:
        artifact_safety.load_bounded_json(payload)
    else:
        with pytest.raises(ValueError, match="nesting"):
            artifact_safety.load_bounded_json(payload)


def test_bounded_json_converts_recursion_error_to_invalid_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def recurse(_payload: bytes, **_kwargs: object) -> object:
        raise RecursionError("too deep")

    monkeypatch.setattr(artifact_safety.json, "loads", recurse)
    with pytest.raises(ValueError, match="recursion"):
        artifact_safety.load_bounded_json(b"{}")


@pytest.mark.parametrize("constant", ("NaN", "Infinity", "-Infinity"))
def test_bounded_json_rejects_non_finite_constants_with_stable_diagnostic(
    constant: str,
) -> None:
    payload = f'{{"ignored":{constant}}}'.encode()

    with pytest.raises(
        ValueError,
        match=rf"^JSON constant {constant} is not permitted$",
    ):
        artifact_safety.load_bounded_json(payload)


@pytest.mark.parametrize("number", ("1e999", "-1e999"))
def test_bounded_json_rejects_overflow_numbers_in_ignored_members(number: str) -> None:
    """An overflow float must not survive in data the caller will ignore."""
    with pytest.raises(ValueError, match="non-finite"):
        artifact_safety.load_bounded_json(f'{{"ignored":{number}}}'.encode())


def test_bounded_json_rejects_utf16_json() -> None:
    """Evidence bytes are an explicitly UTF-8 boundary, not auto-detected JSON."""
    with pytest.raises(UnicodeDecodeError):
        artifact_safety.load_bounded_json('{"ignored":1}'.encode("utf-16"))


def test_bounded_json_rejects_duplicate_raw_object_members() -> None:
    """Duplicate members make the immutable evidence document ambiguous."""
    with pytest.raises(ValueError, match="duplicate JSON object member"):
        artifact_safety.load_bounded_json(b'{"ignored":1,"ignored":2}')


@pytest.mark.parametrize(("size", "accepted"), ((8, True), (9, False)))
def test_digest_regular_file_respects_exact_size_boundary(
    tmp_path: Path,
    size: int,
    accepted: bool,
) -> None:
    source = tmp_path / "source.data"
    source.write_bytes(b"x" * size)

    if accepted:
        digest = artifact_safety.digest_regular_file(source, max_bytes=8)
        assert digest.size == size
        assert len(digest.sha256) == 64
    else:
        with pytest.raises(artifact_safety._BoundedReadError, match="exceeds"):
            artifact_safety.digest_regular_file(source, max_bytes=8)


@pytest.mark.parametrize(
    "leaf_type",
    ("symlink", pytest.param("fifo", marks=_POSIX_FIFO)),
)
def test_digest_regular_file_rejects_unsafe_source_types(
    tmp_path: Path,
    leaf_type: str,
) -> None:
    source = tmp_path / "source.data"
    if leaf_type == "symlink":
        target = tmp_path / "target.data"
        target.write_bytes(b"data")
        symlink_or_skip(source, target)
    else:
        _MKFIFO(source)

    with pytest.raises(artifact_safety._UnsafePathError, match="not a regular file"):
        if leaf_type == "fifo":
            _run_fifo_call_with_watchdog(
                lambda: artifact_safety.digest_regular_file(source, max_bytes=8), source
            )
        else:
            artifact_safety.digest_regular_file(source, max_bytes=8)


def test_copy_regular_file_with_digest_creates_destination_exclusively(tmp_path: Path) -> None:
    source = tmp_path / "source.data"
    source.write_bytes(b"source")
    destination = tmp_path / "destination.data"
    destination.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        artifact_safety.copy_regular_file_with_digest(source, destination, max_bytes=8)

    assert destination.read_bytes() == b"existing"


def test_copy_regular_file_with_digest_completes_partial_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.data"
    content = b"partial-write" * 100
    source.write_bytes(content)
    destination = tmp_path / "destination.data"
    original_write = artifact_safety.os.write

    def partial_write(descriptor: int, data: bytes) -> int:
        return original_write(descriptor, data[:3])

    monkeypatch.setattr(artifact_safety.os, "write", partial_write)
    digest = artifact_safety.copy_regular_file_with_digest(
        source,
        destination,
        max_bytes=len(content),
    )

    assert destination.read_bytes() == content
    assert digest == artifact_safety.digest_regular_file(destination, max_bytes=len(content))


def test_copy_regular_file_with_digest_rejects_source_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.data"
    source.write_bytes(b"original")
    destination = tmp_path / "destination.data"
    original_read = artifact_safety.os.read
    changed = False

    def mutate_before_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        if not changed:
            changed = True
            with source.open("ab") as file:
                file.write(b"!")
        return original_read(descriptor, size)

    monkeypatch.setattr(artifact_safety.os, "read", mutate_before_read)
    expected = PermissionError if os.name == "nt" else artifact_safety._UnsafePathError
    with pytest.raises(expected, match=None if os.name == "nt" else "changed during read"):
        artifact_safety.copy_regular_file_with_digest(source, destination, max_bytes=8)
    if os.name == "nt":
        assert source.read_bytes() == b"original"


@_POSIX_PWRITE
def test_copy_regular_file_with_digest_rejects_destination_digest_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.data"
    source.write_bytes(b"source")
    destination = tmp_path / "destination.data"
    original_write = artifact_safety.os.write
    changed = False

    def corrupt_destination(descriptor: int, data: bytes) -> int:
        nonlocal changed
        written = original_write(descriptor, data)
        if not changed:
            changed = True
            _PWRITE(descriptor, b"!", 0)
        return written

    monkeypatch.setattr(artifact_safety.os, "write", corrupt_destination)
    with pytest.raises(artifact_safety._DigestMismatchError, match="digest mismatch"):
        artifact_safety.copy_regular_file_with_digest(source, destination, max_bytes=8)


def test_copy_regular_file_with_digest_rejects_same_byte_leaf_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.data"
    source.write_bytes(b"source")
    destination = tmp_path / "destination.data"
    original_fsync = artifact_safety.os.fsync
    replaced = False

    def replace_after_sync(descriptor: int) -> None:
        nonlocal replaced
        original_fsync(descriptor)
        if not replaced:
            replaced = True
            destination.unlink()
            destination.write_bytes(b"source")
            destination.chmod(0o666)

    monkeypatch.setattr(artifact_safety.os, "fsync", replace_after_sync)
    with pytest.raises(artifact_safety._UnsafePathError, match="destination"):
        artifact_safety.copy_regular_file_with_digest(source, destination, max_bytes=8)


@_POSIX_RENAME_RACE
def test_copy_regular_file_with_digest_rejects_parent_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.data"
    source.write_bytes(b"source")
    parent = tmp_path / "destination-parent"
    parent.mkdir()
    destination = parent / "destination.data"
    displaced_parent = tmp_path / "displaced-parent"
    original_fsync = artifact_safety.os.fsync
    replaced = False

    def replace_after_sync(descriptor: int) -> None:
        nonlocal replaced
        original_fsync(descriptor)
        if not replaced:
            replaced = True
            parent.rename(displaced_parent)
            parent.mkdir()
            (parent / destination.name).write_bytes(b"source")

    monkeypatch.setattr(artifact_safety.os, "fsync", replace_after_sync)
    with pytest.raises(artifact_safety._UnsafePathError, match="destination parent changed"):
        artifact_safety.copy_regular_file_with_digest(source, destination, max_bytes=8)


def test_copy_digest_observation_never_retains_destination_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source.data"
    source.write_bytes(b"source")
    destination = tmp_path / "destination.data"

    digest = artifact_safety.copy_regular_file_with_digest(source, destination, max_bytes=8)

    assert all(not isinstance(value, bytes) for value in vars(digest).values())
    assert destination.read_bytes() == b"source"


def test_regular_file_copy_returns_verified_destination_identity(tmp_path: Path) -> None:
    source = tmp_path / "source.data"
    source.write_bytes(b"source")
    destination = tmp_path / "destination.data"

    copied = artifact_safety.copy_regular_file(source, destination, max_bytes=8)
    destination_status = destination.stat()

    assert copied.digest == artifact_safety.digest_regular_file(destination, max_bytes=8)
    assert copied.destination_identity == artifact_safety.FileIdentity(
        device=destination_status.st_dev,
        inode=destination_status.st_ino,
    )
