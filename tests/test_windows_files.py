"""Low-level regression coverage for the Windows filesystem adapter."""

from __future__ import annotations

from collections.abc import Callable
import ctypes
import errno
import os
from pathlib import Path
import subprocess  # nosec B404
from typing import cast
import pytest

from pyrepo_check import filesystem


pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows adapter coverage")

_set_last_error = cast(Callable[[int], None], getattr(ctypes, "set_last_error", None))
_O_BINARY = cast(int, getattr(os, "O_BINARY", 0))


def _junction(link: Path, target: Path) -> None:
    result = subprocess.run(  # nosec B603
        (
            "cmd.exe",
            "/d",
            "/c",
            "mklink",
            "/J",
            str(link),
            str(target),
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"junction creation failed: stdout={result.stdout!r}, stderr={result.stderr!r}"
    )


def _directory_flags() -> int:
    return os.O_RDONLY | filesystem.O_DIRECTORY | filesystem.O_NOFOLLOW


@pytest.mark.parametrize(
    ("information_class", "windows_error"),
    (
        (9, 1),  # FileAttributeTagInfo / ERROR_INVALID_FUNCTION
        (1, 50),  # FileStandardInfo / ERROR_NOT_SUPPORTED
        (18, 87),  # FileIdInfo / ERROR_INVALID_PARAMETER
    ),
)
def test_unsupported_native_file_information_is_a_platform_safety_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    information_class: int,
    windows_error: int,
) -> None:
    from pyrepo_check import _windows_files

    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"payload")
    original = _windows_files._GetFileInformationByHandleEx

    def fail_selected_information_class(
        handle: int,
        observed_class: int,
        buffer: object,
        size: int,
    ) -> int:
        if observed_class == information_class:
            _set_last_error(windows_error)
            return 0
        return int(original(handle, observed_class, buffer, size))

    monkeypatch.setattr(
        _windows_files,
        "_GetFileInformationByHandleEx",
        fail_selected_information_class,
    )

    with pytest.raises(filesystem.PlatformSafetyError, match="unsupported"):
        filesystem.open(artifact, os.O_RDONLY | filesystem.O_NOFOLLOW)


def test_native_file_information_access_failure_remains_an_io_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyrepo_check import _windows_files

    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"payload")

    def deny_file_information(
        handle: int,
        information_class: int,
        buffer: object,
        size: int,
    ) -> int:
        del handle, information_class, buffer, size
        _set_last_error(5)
        return 0

    monkeypatch.setattr(
        _windows_files,
        "_GetFileInformationByHandleEx",
        deny_file_information,
    )

    with pytest.raises(OSError) as raised:
        filesystem.open(artifact, os.O_RDONLY | filesystem.O_NOFOLLOW)

    assert not isinstance(raised.value, filesystem.PlatformSafetyError)


@pytest.mark.parametrize(
    ("information_class", "windows_error"),
    (
        (11, 120),  # FileIdBothDirectoryRestartInfo / ERROR_CALL_NOT_IMPLEMENTED
        (10, 124),  # FileIdBothDirectoryInfo / ERROR_INVALID_LEVEL
    ),
)
def test_unsupported_native_directory_information_is_a_platform_safety_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    information_class: int,
    windows_error: int,
) -> None:
    from pyrepo_check import _windows_files

    (tmp_path / "artifact.bin").write_bytes(b"payload")
    descriptor = filesystem.open(tmp_path, _directory_flags())
    original = _windows_files._GetFileInformationByHandleEx

    def fail_selected_information_class(
        handle: int,
        observed_class: int,
        buffer: object,
        size: int,
    ) -> int:
        if observed_class == information_class:
            _set_last_error(windows_error)
            return 0
        return int(original(handle, observed_class, buffer, size))

    monkeypatch.setattr(
        _windows_files,
        "_GetFileInformationByHandleEx",
        fail_selected_information_class,
    )
    try:
        with pytest.raises(filesystem.PlatformSafetyError, match="unsupported"):
            tuple(_windows_files._iter_directory_names(descriptor))
    finally:
        os.close(descriptor)


def test_native_directory_information_access_failure_remains_an_io_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyrepo_check import _windows_files

    descriptor = filesystem.open(tmp_path, _directory_flags())

    def deny_directory_information(
        handle: int,
        information_class: int,
        buffer: object,
        size: int,
    ) -> int:
        del handle, information_class, buffer, size
        _set_last_error(5)
        return 0

    monkeypatch.setattr(
        _windows_files,
        "_GetFileInformationByHandleEx",
        deny_directory_information,
    )
    try:
        with pytest.raises(OSError) as raised:
            tuple(_windows_files._iter_directory_names(descriptor))
    finally:
        os.close(descriptor)

    assert not isinstance(raised.value, filesystem.PlatformSafetyError)


def test_held_regular_reader_blocks_a_concurrent_writer(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"original")
    reader = filesystem.open(artifact, os.O_RDONLY | filesystem.O_NOFOLLOW)
    try:
        with pytest.raises(OSError):
            writer = os.open(artifact, os.O_WRONLY | _O_BINARY)
            os.close(writer)
    finally:
        os.close(reader)

    writer = os.open(artifact, os.O_WRONLY | _O_BINARY)
    try:
        assert os.write(writer, b"changed!") == 8
    finally:
        os.close(writer)


def test_native_identity_mismatch_fails_closed_and_releases_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyrepo_check import _windows_files

    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"original")
    original_file_id_info = _windows_files._file_id_info

    def mismatched_file_id(handle: int) -> object:
        information = original_file_id_info(handle)
        information.VolumeSerialNumber ^= 1
        return information

    monkeypatch.setattr(_windows_files, "_file_id_info", mismatched_file_id)

    with pytest.raises(filesystem.PlatformSafetyError, match="identity does not match"):
        filesystem.open(artifact, os.O_RDONLY | filesystem.O_NOFOLLOW)

    writer = os.open(artifact, os.O_WRONLY | _O_BINARY)
    try:
        assert os.write(writer, b"changed!") == 8
    finally:
        os.close(writer)


def test_deletion_without_delete_authority_fails_and_preserves_target(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"preserve")
    descriptor = filesystem.open(artifact, os.O_RDONLY | filesystem.O_NOFOLLOW)
    try:
        with pytest.raises(OSError):
            filesystem.delete_open_file(descriptor)
    finally:
        os.close(descriptor)

    assert artifact.read_bytes() == b"preserve"


def test_private_relative_file_open_is_binary_and_descriptor_bound(tmp_path: Path) -> None:
    parent = filesystem.open(tmp_path, _directory_flags())
    try:
        descriptor = filesystem.open(
            "artifact.bin",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | filesystem.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        try:
            payload = b"line-one\r\n\x1aline-two\x00"
            assert os.write(descriptor, payload) == len(payload)
            os.fsync(descriptor)
            filesystem.verify_private(descriptor)
            descriptor_status = os.fstat(descriptor)
            path_status = filesystem.stat("artifact.bin", dir_fd=parent)
            assert (descriptor_status.st_dev, descriptor_status.st_ino) == (
                path_status.st_dev,
                path_status.st_ino,
            )
            assert descriptor_status.st_ino != 0
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)

    assert (tmp_path / "artifact.bin").read_bytes() == payload


def test_unsupported_security_retrieval_is_a_platform_safety_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyrepo_check import _windows_files

    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"payload")
    descriptor = filesystem.open(artifact, os.O_RDONLY | filesystem.O_NOFOLLOW)

    def unsupported_security_info(*args: object) -> int:
        del args
        return 120

    monkeypatch.setattr(_windows_files, "_GetSecurityInfo", unsupported_security_info)
    try:
        with pytest.raises(filesystem.PlatformSafetyError, match="unsupported"):
            filesystem.verify_private(descriptor)
    finally:
        os.close(descriptor)


def test_unsupported_security_descriptor_control_is_a_platform_safety_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyrepo_check import _windows_files

    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"payload")
    descriptor = filesystem.open(artifact, os.O_RDONLY | filesystem.O_NOFOLLOW)

    def unsupported_descriptor_control(*args: object) -> int:
        del args
        _set_last_error(87)
        return 0

    monkeypatch.setattr(
        _windows_files,
        "_GetSecurityDescriptorControl",
        unsupported_descriptor_control,
    )
    try:
        with pytest.raises(filesystem.PlatformSafetyError, match="unsupported"):
            filesystem.verify_private(descriptor)
    finally:
        os.close(descriptor)


def test_unsupported_private_security_construction_creates_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyrepo_check import _windows_files

    parent = filesystem.open(tmp_path, _directory_flags())

    def unsupported_security_descriptor(*args: object) -> int:
        del args
        _set_last_error(120)
        return 0

    monkeypatch.setattr(
        _windows_files,
        "_ConvertStringSecurityDescriptorToSecurityDescriptorW",
        unsupported_security_descriptor,
    )
    try:
        with pytest.raises(filesystem.PlatformSafetyError, match="unsupported"):
            filesystem.open(
                "artifact.bin",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | filesystem.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
    finally:
        os.close(parent)

    assert not (tmp_path / "artifact.bin").exists()


def test_exclusive_relative_create_reports_file_exists(tmp_path: Path) -> None:
    (tmp_path / "artifact.bin").write_bytes(b"keep")
    parent = filesystem.open(tmp_path, _directory_flags())
    try:
        with pytest.raises(FileExistsError):
            filesystem.open(
                "artifact.bin",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | filesystem.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
    finally:
        os.close(parent)

    assert (tmp_path / "artifact.bin").read_bytes() == b"keep"


def test_private_relative_directory_is_openable_and_verifiable(tmp_path: Path) -> None:
    parent = filesystem.open(tmp_path, _directory_flags())
    try:
        filesystem.mkdir("child", mode=0o700, dir_fd=parent)
        child = filesystem.open("child", _directory_flags(), dir_fd=parent)
        try:
            filesystem.verify_private(child)
            assert os.fstat(child).st_ino != 0
        finally:
            os.close(child)
    finally:
        os.close(parent)


def test_verify_private_rejects_an_extra_everyone_ace(tmp_path: Path) -> None:
    parent = filesystem.open(tmp_path, _directory_flags())
    try:
        descriptor = filesystem.open(
            "artifact.bin",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | filesystem.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        os.close(descriptor)
        artifact = tmp_path / "artifact.bin"
        result = subprocess.run(  # nosec B603
            ("icacls.exe", str(artifact), "/grant", "*S-1-1-0:(R)"),
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"ACL mutation failed: stdout={result.stdout!r}, stderr={result.stderr!r}"
        )
        descriptor = filesystem.open(artifact, os.O_RDONLY | filesystem.O_NOFOLLOW)
        try:
            with pytest.raises(PermissionError, match="beyond the current user"):
                filesystem.verify_private(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def test_private_directory_makes_plain_exclusive_child_private(tmp_path: Path) -> None:
    parent = filesystem.open(tmp_path, _directory_flags())
    try:
        filesystem.mkdir("private", mode=0o700, dir_fd=parent)
    finally:
        os.close(parent)

    marker = tmp_path / "private" / "marker.bin"
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
    os.close(descriptor)
    descriptor = filesystem.open(marker, os.O_RDONLY | filesystem.O_NOFOLLOW)
    try:
        filesystem.verify_private(descriptor)
    finally:
        os.close(descriptor)


def test_relative_operations_reject_unsafe_components(tmp_path: Path) -> None:
    parent = filesystem.open(tmp_path, _directory_flags())
    unsafe_names = (
        "",
        ".",
        "..",
        "child/name",
        "child\\name",
        "stream:name",
        "trailing.",
        "trailing ",
        "NUL",
        "COM1.txt",
        "name\x00suffix",
    )
    try:
        for name in unsafe_names:
            with pytest.raises(OSError, match="safe relative component"):
                filesystem.stat(name, dir_fd=parent)
    finally:
        os.close(parent)


def test_absolute_open_rejects_junction_ancestor(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "artifact.bin").write_bytes(b"outside")
    junction = tmp_path / "junction"
    _junction(junction, outside)

    with pytest.raises(OSError) as raised:
        filesystem.open(
            junction / "artifact.bin",
            os.O_RDONLY | filesystem.O_NOFOLLOW | filesystem.O_NONBLOCK,
        )

    assert raised.value.errno in {errno.ELOOP, errno.ENOTDIR}


def test_open_rejects_leaf_reparse_point(tmp_path: Path) -> None:
    target = tmp_path / "target.bin"
    target.write_bytes(b"protected")
    link = tmp_path / "link.bin"
    try:
        link.symlink_to(target)
    except OSError as error:
        if getattr(error, "winerror", None) != 1314:
            raise
        raise pytest.skip.Exception(f"Windows file symlink privilege is unavailable: {error}")

    with pytest.raises(OSError) as raised:
        filesystem.open(link, os.O_RDONLY | filesystem.O_NOFOLLOW)

    assert raised.value.errno == errno.ELOOP


def test_open_rejects_leaf_directory_junction(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    _junction(junction, target)

    with pytest.raises(OSError) as raised:
        filesystem.open(junction, _directory_flags())

    assert raised.value.errno == errno.ELOOP


def test_stat_is_no_follow_and_matches_open_descriptor(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"payload")
    descriptor = filesystem.open(artifact, os.O_RDONLY | filesystem.O_NOFOLLOW)
    try:
        assert filesystem.stat(artifact) == os.fstat(descriptor)
    finally:
        os.close(descriptor)


def test_scandir_enumerates_held_directory_descriptor_not_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "first.bin").write_bytes(b"first")
    descriptor = filesystem.open(directory, _directory_flags())
    other = tmp_path / "other"
    other.mkdir()
    (other / "replacement.bin").write_bytes(b"replacement")
    monkeypatch.chdir(other)
    try:
        with filesystem.scandir(descriptor) as iterator:
            entries = tuple(iterator)
        assert tuple(entry.name for entry in entries) == ("first.bin",)
        assert entries[0].path == "first.bin"
        assert entries[0].stat(follow_symlinks=False) == filesystem.stat(
            "first.bin", dir_fd=descriptor
        )
    finally:
        os.close(descriptor)


def test_scandir_path_entries_keep_the_supplied_parent_path(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    artifact = directory / "artifact.bin"
    artifact.write_bytes(b"payload")

    with filesystem.scandir(directory) as iterator:
        entry = next(iterator)

    assert Path(entry.path) == artifact


def test_scandir_close_stops_iteration_without_closing_borrowed_descriptor(
    tmp_path: Path,
) -> None:
    (tmp_path / "artifact.bin").write_bytes(b"payload")
    descriptor = filesystem.open(tmp_path, _directory_flags())
    try:
        iterator = filesystem.scandir(descriptor)
        iterator.close()

        assert tuple(iterator) == ()
        assert os.fstat(descriptor).st_ino != 0
    finally:
        os.close(descriptor)


def test_cleanup_deletes_the_exact_open_file(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"payload")
    descriptor = filesystem.open_for_cleanup(artifact)
    try:
        with pytest.raises(OSError):
            artifact.rename(tmp_path / "replacement.bin")
        filesystem.delete_open_file(descriptor)
    finally:
        os.close(descriptor)

    assert not artifact.exists()


def test_cleanup_directory_handle_can_enumerate_children(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "child.bin").write_bytes(b"payload")
    descriptor = filesystem.open_for_cleanup(directory)
    try:
        with filesystem.scandir(descriptor) as entries:
            assert tuple(entry.name for entry in entries) == ("child.bin",)
    finally:
        os.close(descriptor)


def test_cleanup_can_delete_leaf_symlink_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target.bin"
    target.write_bytes(b"protected")
    link = tmp_path / "link.bin"
    try:
        link.symlink_to(target)
    except OSError as error:
        if getattr(error, "winerror", None) != 1314:
            raise
        raise pytest.skip.Exception(f"Windows file symlink privilege is unavailable: {error}")

    descriptor = filesystem.open_for_cleanup(link)
    try:
        filesystem.delete_open_file(descriptor)
    finally:
        os.close(descriptor)

    assert not link.exists()
    assert target.read_bytes() == b"protected"


def test_cleanup_can_delete_leaf_junction_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    sentinel = target / "sentinel.bin"
    sentinel.write_bytes(b"protected")
    junction = tmp_path / "junction"
    _junction(junction, target)

    descriptor = filesystem.open_for_cleanup(junction)
    try:
        filesystem.delete_open_file(descriptor)
    finally:
        os.close(descriptor)

    assert not junction.exists()
    assert sentinel.read_bytes() == b"protected"


def test_absolute_paths_reject_device_namespaces_and_streams(tmp_path: Path) -> None:
    unsafe_paths = (
        r"\\.\NUL",
        r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1",
        "//server/share/artifact.bin",
        "//?/GLOBALROOT/Device/HarddiskVolumeShadowCopy1",
        "//./C:/Windows/System32",
        tmp_path / "artifact.bin:stream",
    )
    for path in unsafe_paths:
        with pytest.raises(OSError) as raised:
            filesystem.open(path, os.O_RDONLY | filesystem.O_NOFOLLOW)
        assert raised.value.errno == errno.EINVAL
