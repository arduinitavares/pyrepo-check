"""Native Windows descriptor-relative filesystem operations.

This module is imported only on Windows.  It uses NT relative opens so a held
directory, rather than a reconstructed path, remains the authority for every
child operation.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import errno
import msvcrt
import os
from pathlib import Path
import stat as stat_module
from types import TracebackType
from typing import NoReturn, Protocol, cast

from pyrepo_check.filesystem import (
    O_DIRECTORY,
    O_NOFOLLOW,
    O_NONBLOCK,
    PlatformSafetyError,
    ScandirEntry,
    ScandirIterator,
)


class _NativeFunction(Protocol):
    argtypes: object
    restype: object

    def __call__(self, *args: object) -> int: ...


class _WindowsLibrary(Protocol):
    def __getattr__(self, name: str) -> _NativeFunction: ...


class _WindowsCtypes(Protocol):
    def WinDLL(self, name: str, *, use_last_error: bool = False) -> _WindowsLibrary: ...

    def get_last_error(self) -> int: ...

    def set_last_error(self, error: int) -> None: ...

    def FormatError(self, error: int) -> str: ...


class _WindowsMsvcrt(Protocol):
    def get_osfhandle(self, descriptor: int) -> int: ...

    def open_osfhandle(self, handle: int, flags: int) -> int: ...


_windows_ctypes = cast(_WindowsCtypes, ctypes)
_windows_msvcrt = cast(_WindowsMsvcrt, msvcrt)

try:
    _binary_flag = getattr(os, "O_BINARY")
    if type(_binary_flag) is not int or _binary_flag <= 0:
        raise AttributeError("os.O_BINARY is unavailable")
    _O_BINARY = cast(int, _binary_flag)
    _kernel32 = _windows_ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = _windows_ctypes.WinDLL("ntdll")
    _advapi32 = _windows_ctypes.WinDLL("advapi32", use_last_error=True)
    for _library, _api_names in (
        (
            _kernel32,
            (
                "CloseHandle",
                "CreateFileW",
                "GetCurrentProcess",
                "GetFileInformationByHandleEx",
                "GetFileType",
                "LocalFree",
                "SetFileInformationByHandle",
            ),
        ),
        (_ntdll, ("NtCreateFile", "RtlNtStatusToDosError")),
        (
            _advapi32,
            (
                "ConvertSidToStringSidW",
                "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                "EqualSid",
                "GetAce",
                "GetAclInformation",
                "GetSecurityDescriptorControl",
                "GetSecurityInfo",
                "GetTokenInformation",
                "OpenProcessToken",
            ),
        ),
    ):
        for _api_name in _api_names:
            getattr(_library, _api_name)
except (AttributeError, OSError) as error:
    raise PlatformSafetyError(
        "required native Windows filesystem APIs are unavailable"
    ) from error

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_OBJ_CASE_INSENSITIVE = 0x0000_0040
_OBJ_DONT_REPARSE = 0x0000_1000

_FILE_READ_DATA = 0x0000_0001
_FILE_LIST_DIRECTORY = _FILE_READ_DATA
_FILE_WRITE_DATA = 0x0000_0002
_FILE_APPEND_DATA = 0x0000_0004
_FILE_READ_ATTRIBUTES = 0x0000_0080
_DELETE = 0x0001_0000
_READ_CONTROL = 0x0002_0000
_SYNCHRONIZE = 0x0010_0000

_FILE_SHARE_READ = 0x0000_0001
_FILE_SHARE_WRITE = 0x0000_0002
_FILE_SHARE_DELETE = 0x0000_0004

_FILE_OPEN = 1
_FILE_CREATE = 2
_FILE_OPEN_IF = 3
_FILE_OVERWRITE = 4
_FILE_OVERWRITE_IF = 5

_FILE_DIRECTORY_FILE = 0x0000_0001
_FILE_SYNCHRONOUS_IO_NONALERT = 0x0000_0020
_FILE_NON_DIRECTORY_FILE = 0x0000_0040
_FILE_OPEN_REPARSE_POINT = 0x0020_0000

_FILE_ATTRIBUTE_DIRECTORY = 0x0000_0010
_FILE_ATTRIBUTE_NORMAL = 0x0000_0080
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0000_0400
_FILE_FLAG_BACKUP_SEMANTICS = 0x0200_0000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x0020_0000
_OPEN_EXISTING = 3

_FILE_TYPE_DISK = 0x0001
_FILE_STANDARD_INFO_CLASS = 1
_FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
_FILE_ID_BOTH_DIR_INFO_CLASS = 10
_FILE_ID_BOTH_DIR_RESTART_INFO_CLASS = 11
_FILE_ID_INFO_CLASS = 18
_FILE_DISPOSITION_INFO_CLASS = 4
_ERROR_NO_MORE_FILES = 18
_UNSUPPORTED_CAPABILITY_ERRORS = frozenset({1, 50, 87, 120, 124})

_TOKEN_QUERY = 0x0008
_TOKEN_USER_CLASS = 1
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x0000_0001
_DACL_SECURITY_INFORMATION = 0x0000_0004
_SDDL_REVISION_1 = 1
_ACL_SIZE_INFORMATION_CLASS = 2
_ACCESS_ALLOWED_ACE_TYPE = 0
_SE_DACL_PRESENT = 0x0004

_RESERVED_DEVICE_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = (
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    )


class _OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = (
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(_UNICODE_STRING)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", wintypes.LPVOID),
        ("SecurityQualityOfService", wintypes.LPVOID),
    )


class _IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = (("Status", ctypes.c_ssize_t), ("Information", ctypes.c_size_t))


class _FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _fields_ = (("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD))


class _FILE_STANDARD_INFO(ctypes.Structure):
    _fields_ = (
        ("AllocationSize", ctypes.c_longlong),
        ("EndOfFile", ctypes.c_longlong),
        ("NumberOfLinks", wintypes.DWORD),
        ("DeletePending", ctypes.c_ubyte),
        ("Directory", ctypes.c_ubyte),
    )


class _FILE_ID_128(ctypes.Structure):
    _fields_ = (("Identifier", ctypes.c_ubyte * 16),)


class _FILE_ID_INFO(ctypes.Structure):
    _fields_ = (("VolumeSerialNumber", ctypes.c_ulonglong), ("FileId", _FILE_ID_128))


class _FILE_DISPOSITION_INFO(ctypes.Structure):
    _fields_ = (("DeleteFile", wintypes.BOOL),)


class _ACL_SIZE_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    )


class _ACE_HEADER(ctypes.Structure):
    _fields_ = (
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", wintypes.USHORT),
    )


class _FILE_ID_BOTH_DIR_INFO_HEADER(ctypes.Structure):
    _fields_ = (
        ("NextEntryOffset", wintypes.DWORD),
        ("FileIndex", wintypes.DWORD),
        ("CreationTime", ctypes.c_longlong),
        ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong),
        ("ChangeTime", ctypes.c_longlong),
        ("EndOfFile", ctypes.c_longlong),
        ("AllocationSize", ctypes.c_longlong),
        ("FileAttributes", wintypes.DWORD),
        ("FileNameLength", wintypes.DWORD),
        ("EaSize", wintypes.DWORD),
        ("ShortNameLength", ctypes.c_byte),
        ("ShortName", wintypes.WCHAR * 12),
        ("FileId", ctypes.c_longlong),
    )


_NtCreateFile = _ntdll.NtCreateFile
_NtCreateFile.argtypes = (
    ctypes.POINTER(wintypes.HANDLE),
    wintypes.DWORD,
    ctypes.POINTER(_OBJECT_ATTRIBUTES),
    ctypes.POINTER(_IO_STATUS_BLOCK),
    ctypes.POINTER(ctypes.c_longlong),
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
)
_NtCreateFile.restype = ctypes.c_long

_RtlNtStatusToDosError = _ntdll.RtlNtStatusToDosError
_RtlNtStatusToDosError.argtypes = (ctypes.c_long,)
_RtlNtStatusToDosError.restype = wintypes.ULONG

_CreateFileW = _kernel32.CreateFileW
_CreateFileW.argtypes = (
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
)
_CreateFileW.restype = wintypes.HANDLE

_CloseHandle = _kernel32.CloseHandle
_CloseHandle.argtypes = (wintypes.HANDLE,)
_CloseHandle.restype = wintypes.BOOL

_GetFileType = _kernel32.GetFileType
_GetFileType.argtypes = (wintypes.HANDLE,)
_GetFileType.restype = wintypes.DWORD

_GetFileInformationByHandleEx = _kernel32.GetFileInformationByHandleEx
_GetFileInformationByHandleEx.argtypes = (
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
)
_GetFileInformationByHandleEx.restype = wintypes.BOOL

_SetFileInformationByHandle = _kernel32.SetFileInformationByHandle
_SetFileInformationByHandle.argtypes = (
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
)
_SetFileInformationByHandle.restype = wintypes.BOOL

_GetCurrentProcess = _kernel32.GetCurrentProcess
_GetCurrentProcess.restype = wintypes.HANDLE

_LocalFree = _kernel32.LocalFree
_LocalFree.argtypes = (wintypes.HLOCAL,)
_LocalFree.restype = wintypes.HLOCAL

_OpenProcessToken = _advapi32.OpenProcessToken
_OpenProcessToken.argtypes = (
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
)
_OpenProcessToken.restype = wintypes.BOOL

_GetTokenInformation = _advapi32.GetTokenInformation
_GetTokenInformation.argtypes = (
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
)
_GetTokenInformation.restype = wintypes.BOOL

_ConvertSidToStringSidW = _advapi32.ConvertSidToStringSidW
_ConvertSidToStringSidW.argtypes = (wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR))
_ConvertSidToStringSidW.restype = wintypes.BOOL

_ConvertStringSecurityDescriptorToSecurityDescriptorW = (
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
)
_ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.LPVOID),
    ctypes.POINTER(wintypes.ULONG),
)
_ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL

_GetSecurityInfo = _advapi32.GetSecurityInfo
_GetSecurityInfo.argtypes = (
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.LPVOID),
    ctypes.POINTER(wintypes.LPVOID),
    ctypes.POINTER(wintypes.LPVOID),
    ctypes.POINTER(wintypes.LPVOID),
    ctypes.POINTER(wintypes.LPVOID),
)
_GetSecurityInfo.restype = wintypes.DWORD

_EqualSid = _advapi32.EqualSid
_EqualSid.argtypes = (wintypes.LPVOID, wintypes.LPVOID)
_EqualSid.restype = wintypes.BOOL

_GetAclInformation = _advapi32.GetAclInformation
_GetAclInformation.argtypes = (
    wintypes.LPVOID,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.c_int,
)
_GetAclInformation.restype = wintypes.BOOL

_GetAce = _advapi32.GetAce
_GetAce.argtypes = (wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID))
_GetAce.restype = wintypes.BOOL

_GetSecurityDescriptorControl = _advapi32.GetSecurityDescriptorControl
_GetSecurityDescriptorControl.argtypes = (
    wintypes.LPVOID,
    ctypes.POINTER(wintypes.USHORT),
    ctypes.POINTER(wintypes.DWORD),
)
_GetSecurityDescriptorControl.restype = wintypes.BOOL


def _raise_last_error(message: str, path: str | None = None) -> NoReturn:
    error = _windows_ctypes.get_last_error()
    detail = _windows_ctypes.FormatError(error).strip()
    raise OSError(error, f"{message}: {detail}", path)


def _raise_capability_error(message: str, error: int | None = None) -> NoReturn:
    if error is None:
        error = _windows_ctypes.get_last_error()
    detail = _windows_ctypes.FormatError(error).strip()
    if error in _UNSUPPORTED_CAPABILITY_ERRORS:
        raise PlatformSafetyError(
            error,
            f"{message}: required Windows safety capability is unsupported: {detail}",
        )
    raise OSError(error, f"{message}: {detail}")


def _raise_ntstatus(status: int, path: str) -> NoReturn:
    unsigned = status & 0xFFFF_FFFF
    if unsigned in {0xC000_050B, 0xC000_0275, 0xC000_0280}:
        raise OSError(errno.ELOOP, "path contains a reparse point", path)
    windows_error = int(_RtlNtStatusToDosError(status))
    detail = _windows_ctypes.FormatError(windows_error).strip()
    if windows_error in {80, 183}:
        raise FileExistsError(errno.EEXIST, detail or "filesystem object already exists", path)
    if windows_error in {2, 3}:
        raise FileNotFoundError(errno.ENOENT, detail or "filesystem object does not exist", path)
    if windows_error == 267:
        raise NotADirectoryError(errno.ENOTDIR, detail or "path component is not a directory", path)
    if windows_error in {4390, 4392, 4393}:
        raise OSError(errno.ELOOP, "path contains a reparse point", path)
    raise OSError(windows_error, detail or "native Windows open failed", path)


def _safe_component(path: str | Path) -> str:
    component = os.fspath(path)
    if not isinstance(component, str):
        raise TypeError("filesystem paths must be text")
    base_name = component.split(".", maxsplit=1)[0].upper()
    if (
        not component
        or component in {".", ".."}
        or "\x00" in component
        or "/" in component
        or "\\" in component
        or ":" in component
        or component.endswith((".", " "))
        or base_name in _RESERVED_DEVICE_NAMES
    ):
        raise OSError(errno.EINVAL, "path must be a safe relative component", component)
    return component


def _absolute_parts(path: str | Path) -> tuple[str, tuple[str, ...]]:
    raw_path = os.fspath(path)
    if not isinstance(raw_path, str):
        raise TypeError("filesystem paths must be text")
    normalized_path = raw_path.replace("/", "\\")
    if normalized_path.startswith(("\\\\", "\\??\\", "\\Device\\")):
        raise OSError(errno.EINVAL, "NT, UNC, and device namespaces are unavailable", raw_path)
    absolute = os.path.abspath(raw_path)
    drive, tail = os.path.splitdrive(absolute)
    if (
        len(drive) != 2
        or drive[1] != ":"
        or not drive[0].isalpha()
        or not tail.startswith(("\\", "/"))
    ):
        raise OSError(errno.EINVAL, "a local absolute Windows path is required", raw_path)
    parts = tuple(_safe_component(part) for part in tail.replace("/", "\\").split("\\") if part)
    return f"{drive}\\", parts


def _handle_value(descriptor: int) -> int:
    try:
        handle = _windows_msvcrt.get_osfhandle(descriptor)
    except OSError as error:
        raise OSError(errno.EBADF, "invalid filesystem descriptor") from error
    if handle == -1:
        raise OSError(errno.EBADF, "invalid filesystem descriptor")
    return handle


def _close_handle(handle: int) -> None:
    if handle not in {0, _INVALID_HANDLE_VALUE}:
        _CloseHandle(handle)


def _open_volume_root(root: str) -> int:
    handle = _CreateFileW(
        root,
        _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _READ_CONTROL | _SYNCHRONIZE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    value = handle
    if value == _INVALID_HANDLE_VALUE:
        _raise_last_error("cannot open volume root", root)
    try:
        _validate_handle(value, expected_directory=True, reject_reparse=True)
    except BaseException:
        _close_handle(value)
        raise
    return value


def _current_user_sid() -> tuple[ctypes.Array[ctypes.c_char], int]:
    token = wintypes.HANDLE()
    if not _OpenProcessToken(_GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        _raise_capability_error("cannot inspect the current process token")
    try:
        required = wintypes.DWORD()
        _GetTokenInformation(token, _TOKEN_USER_CLASS, None, 0, ctypes.byref(required))
        if required.value == 0:
            _raise_capability_error("cannot size the current process token")
        buffer = ctypes.create_string_buffer(required.value)
        if not _GetTokenInformation(
            token,
            _TOKEN_USER_CLASS,
            buffer,
            required,
            ctypes.byref(required),
        ):
            _raise_capability_error("cannot inspect the current user SID")
        sid = ctypes.c_void_p.from_buffer(buffer).value
        if sid is None:
            raise PlatformSafetyError("the current process token has no user SID")
        return buffer, sid
    finally:
        _close_handle(cast(int, token.value))


def _private_security_descriptor(*, directory: bool) -> int:
    sid_buffer, sid = _current_user_sid()
    string_sid = wintypes.LPWSTR()
    if not _ConvertSidToStringSidW(sid, ctypes.byref(string_sid)):
        _raise_capability_error("cannot format the current user SID")
    try:
        inheritance = "OICI" if directory else ""
        sddl = f"O:{string_sid.value}D:P(A;{inheritance};GA;;;{string_sid.value})"
        security_descriptor = wintypes.LPVOID()
        if not _ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl,
            _SDDL_REVISION_1,
            ctypes.byref(security_descriptor),
            None,
        ):
            _raise_capability_error("cannot build a private security descriptor")
        value = cast(int, security_descriptor.value)
        if not value:
            raise PlatformSafetyError("Windows returned an empty security descriptor")
        return value
    finally:
        del sid_buffer
        _LocalFree(string_sid)


def _nt_open_component(
    parent_handle: int,
    component: str,
    *,
    desired_access: int,
    share_access: int,
    disposition: int,
    options: int,
    file_attributes: int,
    security_descriptor: int | None,
    reject_reparse: bool,
) -> int:
    name_buffer = ctypes.create_unicode_buffer(component)
    name = _UNICODE_STRING(
        len(component.encode("utf-16-le")),
        len(name_buffer) * ctypes.sizeof(wintypes.WCHAR),
        ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    attributes = _OBJECT_ATTRIBUTES(
        ctypes.sizeof(_OBJECT_ATTRIBUTES),
        parent_handle,
        ctypes.pointer(name),
        _OBJ_CASE_INSENSITIVE | (_OBJ_DONT_REPARSE if reject_reparse else 0),
        security_descriptor,
        None,
    )
    handle = wintypes.HANDLE()
    io_status = _IO_STATUS_BLOCK()
    status = int(
        _NtCreateFile(
            ctypes.byref(handle),
            desired_access,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            None,
            file_attributes,
            share_access,
            disposition,
            options,
            None,
            0,
        )
    )
    if status < 0:
        _raise_ntstatus(status, component)
    value = cast(int, handle.value)
    if not value or value == _INVALID_HANDLE_VALUE:
        raise PlatformSafetyError("NtCreateFile returned an invalid handle")
    return value


def _attribute_info(handle: int) -> _FILE_ATTRIBUTE_TAG_INFO:
    info = _FILE_ATTRIBUTE_TAG_INFO()
    if not _GetFileInformationByHandleEx(
        handle,
        _FILE_ATTRIBUTE_TAG_INFO_CLASS,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        _raise_capability_error("cannot inspect Windows file attributes")
    return info


def _standard_info(handle: int) -> _FILE_STANDARD_INFO:
    info = _FILE_STANDARD_INFO()
    if not _GetFileInformationByHandleEx(
        handle,
        _FILE_STANDARD_INFO_CLASS,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        _raise_capability_error("cannot inspect Windows file type")
    return info


def _file_id_info(handle: int) -> _FILE_ID_INFO:
    info = _FILE_ID_INFO()
    if not _GetFileInformationByHandleEx(
        handle,
        _FILE_ID_INFO_CLASS,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        _raise_capability_error("cannot inspect stable Windows file identity")
    return info


def _validate_handle(
    handle: int,
    *,
    expected_directory: bool | None,
    reject_reparse: bool,
) -> None:
    if _GetFileType(handle) != _FILE_TYPE_DISK:
        raise OSError(errno.ENODEV, "filesystem handle is not a regular disk object")
    attributes = _attribute_info(handle)
    if reject_reparse and attributes.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise OSError(errno.ELOOP, "filesystem object is a reparse point")
    standard = _standard_info(handle)
    is_directory = bool(standard.Directory)
    if expected_directory is True and not is_directory:
        raise NotADirectoryError(errno.ENOTDIR, "filesystem object is not a directory")
    if expected_directory is False and is_directory:
        raise IsADirectoryError(errno.EISDIR, "filesystem object is a directory")


def _open_native_handle(
    path: str | Path,
    *,
    dir_fd: int | None,
    desired_access: int,
    share_access: int,
    disposition: int,
    options: int,
    file_attributes: int,
    expected_directory: bool | None,
    create_private: bool,
    allow_leaf_reparse: bool,
) -> int:
    if dir_fd is not None:
        parent_handle = _handle_value(dir_fd)
        _validate_handle(parent_handle, expected_directory=True, reject_reparse=True)
        components = (_safe_component(path),)
        owns_parent = False
    else:
        root, components = _absolute_parts(path)
        parent_handle = _open_volume_root(root)
        owns_parent = True

    if not components:
        if disposition != _FILE_OPEN:
            if owns_parent:
                _close_handle(parent_handle)
            raise FileExistsError(errno.EEXIST, "volume root already exists", os.fspath(path))
        try:
            _validate_handle(
                parent_handle,
                expected_directory=expected_directory,
                reject_reparse=not allow_leaf_reparse,
            )
        except BaseException:
            if owns_parent:
                _close_handle(parent_handle)
            raise
        return parent_handle

    try:
        for component in components[:-1]:
            child = _nt_open_component(
                parent_handle,
                component,
                desired_access=(
                    _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _READ_CONTROL | _SYNCHRONIZE
                ),
                share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
                disposition=_FILE_OPEN,
                options=(
                    _FILE_DIRECTORY_FILE
                    | _FILE_SYNCHRONOUS_IO_NONALERT
                    | _FILE_OPEN_REPARSE_POINT
                ),
                file_attributes=_FILE_ATTRIBUTE_NORMAL,
                security_descriptor=None,
                reject_reparse=True,
            )
            try:
                _validate_handle(child, expected_directory=True, reject_reparse=True)
            except BaseException:
                _close_handle(child)
                raise
            if owns_parent:
                _close_handle(parent_handle)
            parent_handle = child
            owns_parent = True

        security_descriptor = (
            _private_security_descriptor(directory=expected_directory is True)
            if create_private
            else None
        )
        try:
            leaf = _nt_open_component(
                parent_handle,
                components[-1],
                desired_access=desired_access,
                share_access=share_access,
                disposition=disposition,
                options=options | _FILE_OPEN_REPARSE_POINT,
                file_attributes=file_attributes,
                security_descriptor=security_descriptor,
                reject_reparse=not allow_leaf_reparse,
            )
        finally:
            if security_descriptor is not None:
                _LocalFree(security_descriptor)
        try:
            _validate_handle(
                leaf,
                expected_directory=expected_directory,
                reject_reparse=not allow_leaf_reparse,
            )
        except BaseException:
            _close_handle(leaf)
            raise
        return leaf
    finally:
        if owns_parent:
            _close_handle(parent_handle)


def _to_descriptor(handle: int, crt_flags: int) -> int:
    try:
        descriptor = _windows_msvcrt.open_osfhandle(handle, crt_flags | _O_BINARY)
    except BaseException:
        _close_handle(handle)
        raise
    try:
        os.set_inheritable(descriptor, False)
        native_identity = _file_id_info(_handle_value(descriptor))
        descriptor_status = os.fstat(descriptor)
        native_inode = int.from_bytes(bytes(native_identity.FileId.Identifier), "little")
        if (
            native_identity.VolumeSerialNumber != descriptor_status.st_dev
            or native_inode != descriptor_status.st_ino
            or native_inode == 0
        ):
            raise PlatformSafetyError(
                "Windows native identity does not match the Python descriptor identity"
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _creation_disposition(flags: int) -> tuple[int, bool]:
    create = bool(flags & os.O_CREAT)
    exclusive = bool(flags & os.O_EXCL)
    truncate = bool(flags & os.O_TRUNC)
    if exclusive and not create:
        raise OSError(errno.EINVAL, "O_EXCL requires O_CREAT")
    if create and exclusive:
        return _FILE_CREATE, True
    if create and truncate:
        return _FILE_OVERWRITE_IF, True
    if create:
        return _FILE_OPEN_IF, True
    if truncate:
        return _FILE_OVERWRITE, False
    return _FILE_OPEN, False


def open(
    path: str | Path,
    flags: int,
    mode: int = 0o777,
    *,
    dir_fd: int | None = None,
) -> int:
    """Open a regular disk file or directory without traversing reparse points."""
    del mode
    access_mode = flags & 0x3
    directory = bool(flags & O_DIRECTORY)
    if access_mode == os.O_RDONLY:
        data_access = _FILE_LIST_DIRECTORY if directory else _FILE_READ_DATA
    elif access_mode == os.O_WRONLY:
        data_access = _FILE_WRITE_DATA
    elif access_mode == os.O_RDWR:
        data_access = (_FILE_LIST_DIRECTORY if directory else _FILE_READ_DATA) | _FILE_WRITE_DATA
    else:
        raise OSError(errno.EINVAL, "unsupported Windows access mode", os.fspath(path))
    if flags & os.O_APPEND:
        data_access |= _FILE_APPEND_DATA
    disposition, create_private = _creation_disposition(flags)
    delete_created_directory = directory and disposition == _FILE_CREATE
    options = _FILE_SYNCHRONOUS_IO_NONALERT | (
        _FILE_DIRECTORY_FILE if directory else _FILE_NON_DIRECTORY_FILE
    )
    share_access = _FILE_SHARE_READ
    if directory:
        share_access |= _FILE_SHARE_WRITE
    else:
        share_access |= _FILE_SHARE_DELETE
    handle = _open_native_handle(
        path,
        dir_fd=dir_fd,
        desired_access=(
            data_access
            | _FILE_READ_ATTRIBUTES
            | _READ_CONTROL
            | _SYNCHRONIZE
            | (_DELETE if delete_created_directory else 0)
        ),
        share_access=share_access,
        disposition=disposition,
        options=options,
        file_attributes=_FILE_ATTRIBUTE_DIRECTORY if directory else _FILE_ATTRIBUTE_NORMAL,
        expected_directory=directory,
        create_private=create_private,
        allow_leaf_reparse=False,
    )
    crt_flags = access_mode | (os.O_APPEND if flags & os.O_APPEND else 0)
    return _to_descriptor(handle, crt_flags)


def stat(
    path: str | Path,
    *,
    dir_fd: int | None = None,
    follow_symlinks: bool = False,
) -> os.stat_result:
    """Return descriptor-derived metadata for a no-follow metadata handle."""
    if follow_symlinks:
        raise PlatformSafetyError("following Windows reparse points is outside the safe adapter")
    handle = _open_native_handle(
        path,
        dir_fd=dir_fd,
        desired_access=_FILE_READ_ATTRIBUTES | _READ_CONTROL | _SYNCHRONIZE,
        share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        disposition=_FILE_OPEN,
        options=_FILE_SYNCHRONOUS_IO_NONALERT,
        file_attributes=_FILE_ATTRIBUTE_NORMAL,
        expected_directory=None,
        create_private=False,
        allow_leaf_reparse=True,
    )
    descriptor = _to_descriptor(handle, os.O_RDONLY)
    try:
        return os.fstat(descriptor)
    finally:
        os.close(descriptor)


def mkdir(path: str | Path, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
    """Create a directory with a protected inheritable current-user DACL."""
    descriptor = open(
        path,
        os.O_RDONLY | os.O_CREAT | os.O_EXCL | O_DIRECTORY | O_NOFOLLOW | O_NONBLOCK,
        mode,
        dir_fd=dir_fd,
    )
    os.close(descriptor)


def _iter_directory_names(descriptor: int) -> Generator[str, None, None]:
    handle = _handle_value(descriptor)
    buffer = ctypes.create_string_buffer(64 * 1024)
    info_class = _FILE_ID_BOTH_DIR_RESTART_INFO_CLASS
    while True:
        _windows_ctypes.set_last_error(0)
        if not _GetFileInformationByHandleEx(
            handle,
            info_class,
            buffer,
            len(buffer),
        ):
            error = _windows_ctypes.get_last_error()
            if error == _ERROR_NO_MORE_FILES:
                break
            _raise_capability_error("cannot enumerate a held Windows directory")
        offset = 0
        while True:
            header = _FILE_ID_BOTH_DIR_INFO_HEADER.from_buffer(buffer, offset)
            name_offset = offset + ctypes.sizeof(_FILE_ID_BOTH_DIR_INFO_HEADER)
            name = ctypes.wstring_at(
                ctypes.addressof(buffer) + name_offset,
                header.FileNameLength // ctypes.sizeof(wintypes.WCHAR),
            )
            if name not in {".", ".."}:
                yield name
            if header.NextEntryOffset == 0:
                break
            offset += header.NextEntryOffset
        info_class = _FILE_ID_BOTH_DIR_INFO_CLASS


@dataclass(frozen=True, slots=True)
class _WindowsDirEntry:
    name: str
    path: str
    _status: os.stat_result

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        if follow_symlinks:
            raise PlatformSafetyError("following Windows reparse points is unavailable")
        return self._status


class _WindowsScandir(Iterator[ScandirEntry]):
    def __init__(self, path: int | str | Path) -> None:
        if isinstance(path, int):
            self._descriptor = path
            self._owns_descriptor = False
            self._entry_parent: str | None = None
        else:
            self._descriptor = open(path, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_NONBLOCK)
            self._owns_descriptor = True
            self._entry_parent = os.fspath(path)
        self._names = _iter_directory_names(self._descriptor)
        self._closed = False

    def __iter__(self) -> Iterator[ScandirEntry]:
        return self

    def __enter__(self) -> _WindowsScandir:
        return self

    def __next__(self) -> ScandirEntry:
        if self._closed:
            raise StopIteration
        try:
            name = next(self._names)
        except StopIteration:
            self.close()
            raise
        entry_path = name if self._entry_parent is None else os.path.join(self._entry_parent, name)
        return _WindowsDirEntry(
            name=name,
            path=entry_path,
            _status=stat(name, dir_fd=self._descriptor, follow_symlinks=False),
        )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._names.close()
        if self._owns_descriptor:
            os.close(self._descriptor)
            self._owns_descriptor = False


def scandir(path: int | str | Path) -> ScandirIterator:
    """Enumerate names and no-follow metadata through a held directory handle."""
    return _WindowsScandir(path)


def verify_private(descriptor: int) -> None:
    """Validate actual ownership and every effective DACL trustee on a handle."""
    handle = _handle_value(descriptor)
    sid_buffer, current_sid = _current_user_sid()
    owner = wintypes.LPVOID()
    dacl = wintypes.LPVOID()
    security_descriptor = wintypes.LPVOID()
    result = _GetSecurityInfo(
        handle,
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(security_descriptor),
    )
    if result:
        _raise_capability_error("cannot inspect Windows file security", result)
    try:
        if not owner.value or not _EqualSid(owner, current_sid):
            raise PermissionError(errno.EACCES, "filesystem object is not owned by the current user")
        control = wintypes.USHORT()
        revision = wintypes.DWORD()
        if not _GetSecurityDescriptorControl(
            security_descriptor,
            ctypes.byref(control),
            ctypes.byref(revision),
        ):
            _raise_capability_error("cannot inspect Windows DACL control flags")
        if not control.value & _SE_DACL_PRESENT or not dacl.value:
            raise PermissionError(errno.EACCES, "filesystem object has an unrestricted DACL")
        acl_info = _ACL_SIZE_INFORMATION()
        if not _GetAclInformation(
            dacl,
            ctypes.byref(acl_info),
            ctypes.sizeof(acl_info),
            _ACL_SIZE_INFORMATION_CLASS,
        ):
            _raise_capability_error("cannot inspect Windows DACL entries")
        if acl_info.AceCount == 0:
            raise PermissionError(errno.EACCES, "filesystem object grants no current-user access")
        for index in range(acl_info.AceCount):
            ace = wintypes.LPVOID()
            if not _GetAce(dacl, index, ctypes.byref(ace)):
                _raise_capability_error("cannot inspect a Windows DACL entry")
            header = _ACE_HEADER.from_address(cast(int, ace.value))
            ace_sid = cast(int, ace.value) + 8
            if header.AceType != _ACCESS_ALLOWED_ACE_TYPE or not _EqualSid(ace_sid, current_sid):
                raise PermissionError(
                    errno.EACCES,
                    "filesystem object grants access beyond the current user",
                )
    finally:
        del sid_buffer
        if security_descriptor.value:
            _LocalFree(security_descriptor)


def open_for_cleanup(path: str | Path, *, dir_fd: int | None = None) -> int:
    """Open a leaf itself with DELETE access and no delete sharing."""
    handle = _open_native_handle(
        path,
        dir_fd=dir_fd,
        desired_access=(
            _DELETE
            | _FILE_LIST_DIRECTORY
            | _FILE_READ_ATTRIBUTES
            | _READ_CONTROL
            | _SYNCHRONIZE
        ),
        share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
        disposition=_FILE_OPEN,
        options=_FILE_SYNCHRONOUS_IO_NONALERT,
        file_attributes=_FILE_ATTRIBUTE_NORMAL,
        expected_directory=None,
        create_private=False,
        allow_leaf_reparse=True,
    )
    return _to_descriptor(handle, os.O_RDONLY)


def delete_open_file(descriptor: int) -> None:
    """Mark precisely the held handle for deletion."""
    disposition = _FILE_DISPOSITION_INFO(True)
    if not _SetFileInformationByHandle(
        _handle_value(descriptor),
        _FILE_DISPOSITION_INFO_CLASS,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        _raise_last_error("cannot delete the held Windows filesystem object")


def unlink(path: str | Path, *, dir_fd: int | None = None) -> None:
    """Delete a non-directory leaf through the exact held handle."""
    descriptor = open_for_cleanup(path, dir_fd=dir_fd)
    try:
        if stat_module.S_ISDIR(os.fstat(descriptor).st_mode):
            raise IsADirectoryError(errno.EISDIR, "filesystem object is a directory", os.fspath(path))
        delete_open_file(descriptor)
    finally:
        os.close(descriptor)
