"""Low-level regression coverage for the Windows filesystem adapter."""

from __future__ import annotations

from collections.abc import Callable
import ctypes
from ctypes import wintypes
import errno
import os
from pathlib import Path
import subprocess  # nosec B404
import sys
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


class _TokenTracker:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _windows_files: object,
        *,
        deny_access_mask: int = 0,
        deny_error: int = 5,
    ) -> None:
        self.opened_handles: list[int] = []
        self.closed_handles: list[int] = []
        self.live_handles: set[int] = set()
        self.requested_access: list[int] = []
        self.denied_access: list[int] = []

        real_open_token = getattr(_windows_files, "_OpenProcessToken")
        real_close_handle = getattr(_windows_files, "_CloseHandle")

        def tracking_open_token(
            process: int,
            access: int,
            token_ref: ctypes.c_void_p,
        ) -> int:
            self.requested_access.append(access)
            if deny_access_mask and (access & deny_access_mask):
                self.denied_access.append(access)
                _set_last_error(deny_error)
                return 0
            result = int(real_open_token(process, access, token_ref))
            if result:
                handle_ptr = ctypes.cast(token_ref, ctypes.POINTER(wintypes.HANDLE))
                handle = handle_ptr.contents.value
                if handle:
                    handle_val = cast(int, handle)
                    self.opened_handles.append(handle_val)
                    self.live_handles.add(handle_val)
            return result

        def tracking_close_handle(handle: int) -> int:
            if handle in self.live_handles:
                self.live_handles.remove(handle)
                self.closed_handles.append(handle)
            return int(real_close_handle(handle))

        monkeypatch.setattr(_windows_files, "_OpenProcessToken", tracking_open_token)
        monkeypatch.setattr(_windows_files, "_CloseHandle", tracking_close_handle)


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
    creator = filesystem.open(
        artifact,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | filesystem.O_NOFOLLOW,
        0o600,
    )
    try:
        assert os.write(creator, b"payload") == 7
    finally:
        os.close(creator)
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


def test_private_directory_creation_owner_already_user_is_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyrepo_check import _windows_files

    tracker = _TokenTracker(monkeypatch, _windows_files)
    set_token_info_called = False

    def trap_set_token_info(*args: object) -> int:
        nonlocal set_token_info_called
        set_token_info_called = True
        return 1

    monkeypatch.setattr(_windows_files, "_SetTokenInformation", trap_set_token_info)

    equal_sid_called = False

    def reporting_equal_sid(sid1: int, sid2: int) -> int:
        nonlocal equal_sid_called
        equal_sid_called = True
        return 1

    monkeypatch.setattr(_windows_files, "_EqualSid", reporting_equal_sid)

    target_dir = tmp_path / "private_dir"
    filesystem.mkdir(target_dir, mode=0o700)

    assert target_dir.is_dir()
    assert equal_sid_called
    assert not set_token_info_called
    assert tracker.opened_handles
    assert not tracker.live_handles
    assert tracker.opened_handles == tracker.closed_handles
    assert all(access == _windows_files._TOKEN_QUERY for access in tracker.requested_access)


def test_private_directory_creation_adjusts_unequal_owner_and_verifies_postcondition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyrepo_check import _windows_files

    expected_buffer, expected_user_sid = _windows_files._current_user_sid()
    tracker = _TokenTracker(monkeypatch, _windows_files)

    real_equal_sid = _windows_files._EqualSid
    real_get_token_info = _windows_files._GetTokenInformation

    operations: list[tuple[str, int]] = []
    set_info_calls: list[tuple[int, int, bool]] = []

    def tracking_get_token_info(
        token: int,
        info_class: int,
        buffer: object,
        length: int,
        return_length: object,
    ) -> int:
        result = int(
            real_get_token_info(token, info_class, buffer, length, return_length)
        )
        if buffer is not None and result:
            operations.append(("QUERY", info_class))
        return result

    def recording_set_token_info(
        token: int,
        info_class: int,
        info_ptr: ctypes.c_void_p,
        info_length: int,
    ) -> int:
        operations.append(("SET", info_class))
        owner_struct = ctypes.cast(
            info_ptr,
            ctypes.POINTER(_windows_files._TOKEN_OWNER),
        ).contents
        owner_sid = cast(int, owner_struct.Owner) if owner_struct.Owner else 0
        is_expected_user = bool(
            owner_sid and real_equal_sid(owner_sid, expected_user_sid)
        )
        set_info_calls.append((info_class, info_length, is_expected_user))
        return 1

    equal_sid_call_count = 0

    def sequence_equal_sid(sid1: int, sid2: int) -> int:
        nonlocal equal_sid_call_count
        equal_sid_call_count += 1
        if equal_sid_call_count == 1:
            return 0
        return 1

    monkeypatch.setattr(
        _windows_files, "_GetTokenInformation", tracking_get_token_info
    )
    monkeypatch.setattr(
        _windows_files, "_SetTokenInformation", recording_set_token_info
    )
    monkeypatch.setattr(_windows_files, "_EqualSid", sequence_equal_sid)

    target_dir = tmp_path / "private_dir"
    filesystem.mkdir(target_dir, mode=0o700)

    assert target_dir.is_dir()
    assert len(set_info_calls) == 1
    info_class, info_length, is_expected_user = set_info_calls[0]
    assert info_class == _windows_files._TOKEN_OWNER_CLASS
    assert info_length == ctypes.sizeof(_windows_files._TOKEN_OWNER)
    assert is_expected_user is True
    assert equal_sid_call_count == 2
    assert operations[:4] == [
        ("QUERY", _windows_files._TOKEN_USER_CLASS),
        ("QUERY", _windows_files._TOKEN_OWNER_CLASS),
        ("SET", _windows_files._TOKEN_OWNER_CLASS),
        ("QUERY", _windows_files._TOKEN_OWNER_CLASS),
    ]
    adjust_access = _windows_files._TOKEN_ADJUST_DEFAULT | _windows_files._TOKEN_QUERY
    assert tracker.requested_access.count(adjust_access) == 1
    assert not tracker.live_handles
    assert tracker.opened_handles == tracker.closed_handles
    # Keep the SID's backing storage alive through all native comparisons above.
    del expected_buffer


@pytest.mark.parametrize("adjust_owner", (False, True), ids=("query", "adjust"))
@pytest.mark.parametrize("invalid_handle", (None, -1), ids=("null", "invalid"))
def test_private_directory_creation_rejects_invalid_token_without_closing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adjust_owner: bool,
    invalid_handle: int | None,
) -> None:
    from pyrepo_check import _windows_files

    tracker = _TokenTracker(monkeypatch, _windows_files)
    tracked_open_token = _windows_files._OpenProcessToken
    tracked_close_handle = _windows_files._CloseHandle
    failing_access = _windows_files._TOKEN_QUERY
    if adjust_owner:
        failing_access |= _windows_files._TOKEN_ADJUST_DEFAULT
    injected_access: list[int] = []

    def return_invalid_token(
        process: int,
        access: int,
        token_ref: ctypes.c_void_p,
    ) -> int:
        if access == failing_access:
            injected_access.append(access)
            handle_ptr = ctypes.cast(token_ref, ctypes.POINTER(wintypes.HANDLE))
            handle_ptr.contents.value = invalid_handle
            return 1
        return int(tracked_open_token(process, access, token_ref))

    def reject_invalid_close(handle: int | None) -> int:
        assert handle not in {None, 0, _windows_files._INVALID_HANDLE_VALUE}, (
            "invalid token reached native CloseHandle"
        )
        return int(tracked_close_handle(handle))

    def reject_owner_adjustment(*args: object) -> int:
        raise AssertionError("an invalid token must not reach owner adjustment")

    monkeypatch.setattr(_windows_files, "_OpenProcessToken", return_invalid_token)
    monkeypatch.setattr(_windows_files, "_CloseHandle", reject_invalid_close)
    monkeypatch.setattr(_windows_files, "_EqualSid", lambda *args: 0)
    monkeypatch.setattr(_windows_files, "_SetTokenInformation", reject_owner_adjustment)

    target_dir = tmp_path / "private_dir"
    with pytest.raises(filesystem.PlatformSafetyError, match="invalid handle"):
        filesystem.mkdir(target_dir, mode=0o700)

    assert injected_access == [failing_access]
    assert not target_dir.exists()
    assert len(tracker.opened_handles) == int(adjust_owner)
    assert tracker.opened_handles == tracker.closed_handles
    assert not tracker.live_handles


def test_private_directory_creation_fails_closed_when_query_token_open_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyrepo_check import _windows_files

    tracker = _TokenTracker(
        monkeypatch,
        _windows_files,
        deny_access_mask=_windows_files._TOKEN_QUERY,
    )

    target_dir = tmp_path / "private_dir"
    with pytest.raises(
        filesystem.PlatformSafetyError,
        match="cannot open the process token for query",
    ):
        filesystem.mkdir(target_dir, mode=0o700)

    assert tracker.denied_access
    assert not target_dir.exists()
    assert not tracker.live_handles
    assert tracker.opened_handles == tracker.closed_handles


@pytest.mark.parametrize(
    ("windows_error", "return_size"),
    (
        (87, 0),
        (122, 0),
    ),
)
def test_private_directory_creation_fails_closed_on_token_sizing_fault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows_error: int,
    return_size: int,
) -> None:
    from pyrepo_check import _windows_files

    tracker = _TokenTracker(monkeypatch, _windows_files)
    injection_executed = False
    real_get_token_info = _windows_files._GetTokenInformation

    def mock_sizing_get_token_info(
        token: int,
        info_class: int,
        buffer: object,
        length: int,
        return_length: ctypes.c_void_p,
    ) -> int:
        nonlocal injection_executed
        if buffer is None and info_class == _windows_files._TOKEN_USER_CLASS:
            injection_executed = True
            _set_last_error(windows_error)
            ctypes.cast(
                return_length,
                ctypes.POINTER(wintypes.DWORD),
            ).contents.value = return_size
            return 0
        return int(
            real_get_token_info(token, info_class, buffer, length, return_length)
        )

    monkeypatch.setattr(_windows_files, "_GetTokenInformation", mock_sizing_get_token_info)

    target_dir = tmp_path / "private_dir"
    with pytest.raises(filesystem.PlatformSafetyError, match="cannot size process token user"):
        filesystem.mkdir(target_dir, mode=0o700)

    assert injection_executed
    assert not target_dir.exists()
    assert not tracker.live_handles
    assert tracker.opened_handles == tracker.closed_handles


def test_private_directory_creation_fails_closed_when_user_query_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyrepo_check import _windows_files

    tracker = _TokenTracker(monkeypatch, _windows_files)
    injection_executed = False
    real_get_token_info = _windows_files._GetTokenInformation

    def fail_user_token_info(
        token: int,
        info_class: int,
        buffer: object,
        length: int,
        return_length: object,
    ) -> int:
        nonlocal injection_executed
        if info_class == _windows_files._TOKEN_USER_CLASS and buffer is not None:
            injection_executed = True
            _set_last_error(87)
            return 0
        return int(real_get_token_info(token, info_class, buffer, length, return_length))

    monkeypatch.setattr(_windows_files, "_GetTokenInformation", fail_user_token_info)

    target_dir = tmp_path / "private_dir"
    with pytest.raises(
        filesystem.PlatformSafetyError,
        match="cannot query process token user",
    ):
        filesystem.mkdir(target_dir, mode=0o700)

    assert injection_executed
    assert not target_dir.exists()
    assert not tracker.live_handles
    assert tracker.opened_handles == tracker.closed_handles


def test_private_directory_creation_fails_closed_when_owner_query_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyrepo_check import _windows_files

    tracker = _TokenTracker(monkeypatch, _windows_files)
    injection_executed = False
    real_get_token_info = _windows_files._GetTokenInformation

    def fail_owner_token_info(
        token: int,
        info_class: int,
        buffer: object,
        length: int,
        return_length: object,
    ) -> int:
        nonlocal injection_executed
        if info_class == _windows_files._TOKEN_OWNER_CLASS and buffer is not None:
            injection_executed = True
            _set_last_error(87)
            return 0
        return int(real_get_token_info(token, info_class, buffer, length, return_length))

    monkeypatch.setattr(_windows_files, "_GetTokenInformation", fail_owner_token_info)

    target_dir = tmp_path / "private_dir"
    with pytest.raises(
        filesystem.PlatformSafetyError,
        match="cannot query process token owner",
    ):
        filesystem.mkdir(target_dir, mode=0o700)

    assert injection_executed
    assert not target_dir.exists()
    assert not tracker.live_handles
    assert tracker.opened_handles == tracker.closed_handles


def test_private_directory_creation_fails_closed_when_adjust_token_open_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyrepo_check import _windows_files

    tracker = _TokenTracker(
        monkeypatch,
        _windows_files,
        deny_access_mask=_windows_files._TOKEN_ADJUST_DEFAULT,
    )
    monkeypatch.setattr(_windows_files, "_EqualSid", lambda s1, s2: 0)

    target_dir = tmp_path / "private_dir"
    with pytest.raises(
        filesystem.PlatformSafetyError,
        match="cannot open the process token for owner adjustment",
    ):
        filesystem.mkdir(target_dir, mode=0o700)

    assert tracker.denied_access
    assert not target_dir.exists()
    assert not tracker.live_handles
    assert tracker.opened_handles == tracker.closed_handles


def test_private_directory_creation_fails_closed_when_set_token_information_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyrepo_check import _windows_files

    tracker = _TokenTracker(monkeypatch, _windows_files)
    injection_executed = False

    def fail_set_token_info(*args: object) -> int:
        nonlocal injection_executed
        injection_executed = True
        _set_last_error(87)
        return 0

    monkeypatch.setattr(_windows_files, "_EqualSid", lambda s1, s2: 0)
    monkeypatch.setattr(_windows_files, "_SetTokenInformation", fail_set_token_info)

    target_dir = tmp_path / "private_dir"
    with pytest.raises(
        filesystem.PlatformSafetyError,
        match="cannot set process token owner",
    ):
        filesystem.mkdir(target_dir, mode=0o700)

    assert injection_executed
    assert not target_dir.exists()
    assert not tracker.live_handles
    assert tracker.opened_handles == tracker.closed_handles


def test_private_directory_creation_fails_closed_when_owner_postcondition_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyrepo_check import _windows_files

    tracker = _TokenTracker(monkeypatch, _windows_files)
    set_info_called = False
    equal_sid_calls = 0

    def mock_set_token_info(*args: object) -> int:
        nonlocal set_info_called
        set_info_called = True
        return 1

    def always_unequal_sid(sid1: int, sid2: int) -> int:
        nonlocal equal_sid_calls
        equal_sid_calls += 1
        return 0

    monkeypatch.setattr(_windows_files, "_SetTokenInformation", mock_set_token_info)
    monkeypatch.setattr(_windows_files, "_EqualSid", always_unequal_sid)

    target_dir = tmp_path / "private_dir"
    with pytest.raises(
        filesystem.PlatformSafetyError,
        match="process token owner could not be synchronized",
    ):
        filesystem.mkdir(target_dir, mode=0o700)

    assert set_info_called
    assert equal_sid_calls >= 2
    assert not target_dir.exists()
    assert not tracker.live_handles
    assert tracker.opened_handles == tracker.closed_handles


def test_private_directory_inherited_by_spawned_process_writers(tmp_path: Path) -> None:
    parent = filesystem.open(tmp_path, _directory_flags())
    try:
        filesystem.mkdir("private", mode=0o700, dir_fd=parent)
    finally:
        os.close(parent)

    private_dir = tmp_path / "private"

    child_code = (
        "import os, sys, pathlib\n"
        "target_dir = pathlib.Path(sys.argv[1])\n"
        "o_binary = getattr(os, 'O_BINARY', 0)\n"
        "fd = os.open(target_dir / 'child_exclusive.bin', os.O_WRONLY | os.O_CREAT | os.O_EXCL | o_binary, 0o600)\n"
        "assert os.write(fd, b'child-exclusive-payload') == 23\n"
        "os.close(fd)\n"
        "with open(target_dir / 'child_open_x.bin', 'xb') as f:\n"
        "    assert f.write(b'child-open-x-payload') == 20\n"
        "temp_file = target_dir / 'child_temp.bin'\n"
        "with open(temp_file, 'xb') as f:\n"
        "    assert f.write(b'child-atomic-replace-payload') == 28\n"
        "os.replace(temp_file, target_dir / 'child_atomic.bin')\n"
    )

    result = subprocess.run(  # nosec B603
        (sys.executable, "-c", child_code, str(private_dir)),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"child process failed: stdout={result.stdout!r}, stderr={result.stderr!r}"
    )

    expected_files = (
        ("child_exclusive.bin", b"child-exclusive-payload"),
        ("child_open_x.bin", b"child-open-x-payload"),
        ("child_atomic.bin", b"child-atomic-replace-payload"),
    )
    for filename, expected_content in expected_files:
        filepath = private_dir / filename
        descriptor = filesystem.open(filepath, os.O_RDONLY | filesystem.O_NOFOLLOW)
        try:
            filesystem.verify_private(descriptor)
            content = os.read(descriptor, len(expected_content) + 10)
            assert content == expected_content
        finally:
            os.close(descriptor)


def test_verify_private_rejects_owner_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyrepo_check import _windows_files

    artifact = tmp_path / "artifact.bin"
    creator = filesystem.open(
        artifact,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | filesystem.O_NOFOLLOW,
        0o600,
    )
    try:
        assert os.write(creator, b"payload") == 7
    finally:
        os.close(creator)

    descriptor = filesystem.open(artifact, os.O_RDONLY | filesystem.O_NOFOLLOW)
    injection_executed = False

    def mismatched_owner_equal_sid(sid1: int, sid2: int) -> int:
        nonlocal injection_executed
        injection_executed = True
        return 0

    monkeypatch.setattr(_windows_files, "_EqualSid", mismatched_owner_equal_sid)
    try:
        with pytest.raises(PermissionError, match="not owned by the current user"):
            filesystem.verify_private(descriptor)
    finally:
        os.close(descriptor)

    assert injection_executed


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
