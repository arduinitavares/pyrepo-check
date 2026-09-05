"""Stage the standalone Check launcher and validate trusted start evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import os

from pyrepo_check import filesystem as fs
from pathlib import Path
import re
import secrets
import stat
from typing import cast

from pyrepo_check.artifact_safety import (
    FileDigest,
    copy_regular_file,
    digest_regular_file,
    load_bounded_json,
)
from pyrepo_check.execution import (
    CheckModule,
    CheckStartObservation,
    PreparedRepositoryEnvironment,
    PythonObservation,
)
from pyrepo_check.execution_workspace import VerifiedRunWorkspace
from pyrepo_check.planning import CheckInvocation, CheckName
from pyrepo_check.repository_environment import locked_repository_prefix


_MAX_LAUNCHER_BYTES = 65_536
_MAX_MARKER_BYTES = 4_096
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_MARKER_FIELDS = {
    "schema_version",
    "check",
    "module",
    "arguments_sha256",
    "python",
}
_PYTHON_FIELDS = {"implementation", "version", "executable"}

CHECK_MODULE: Mapping[CheckName, CheckModule] = {
    "ruff": "ruff",
    "annotations": "ruff",
    "annotations-fix": "ruff",
    "ty": "ty",
    "bandit": "bandit",
    "pytest": "pytest",
}


@dataclass(frozen=True)
class StagedCheckLauncher:
    path: Path
    digest: FileDigest


def stage_check_launcher(workspace: VerifiedRunWorkspace) -> StagedCheckLauncher:
    """Copy the packaged launcher into the held workspace with verified bytes."""
    workspace.verify("immediately before Check launcher staging")
    source = Path(__file__).with_name("_check_launcher.py")
    name = f"check-launcher-{secrets.token_hex(16)}.py"
    destination = workspace.workspace.path / name
    copied = copy_regular_file(
        source,
        Path(name),
        max_bytes=_MAX_LAUNCHER_BYTES,
        destination_dir_fd=workspace.descriptor,
    )
    workspace.verify("immediately after Check launcher staging")
    return StagedCheckLauncher(destination, copied.digest)


def ensure_staged_launcher(
    staged: StagedCheckLauncher,
    *,
    workspace: VerifiedRunWorkspace,
) -> None:
    """Revalidate staged launcher identity-bound bytes immediately pre-spawn."""
    _require_workspace_child(staged.path, workspace)
    workspace.verify("immediately before staged Check launcher validation")
    digest = digest_regular_file(
        Path(staged.path.name),
        max_bytes=_MAX_LAUNCHER_BYTES,
        dir_fd=workspace.descriptor,
    )
    if digest != staged.digest:
        raise OSError("staged Check launcher digest changed before spawn")
    workspace.verify("immediately after staged Check launcher validation")


def build_launcher_command(
    prepared: PreparedRepositoryEnvironment,
    staged: StagedCheckLauncher,
    invocation: CheckInvocation,
    marker_path: Path,
    *,
    module: CheckModule | None = None,
    use_observed_python_executable: bool = False,
) -> tuple[str, ...]:
    """Build the exact locked Repository Python launcher command."""
    selected_module = CHECK_MODULE[invocation.name] if module is None else module
    prefix = locked_repository_prefix(prepared)
    if use_observed_python_executable:
        prefix = (*prefix[:-1], str(prepared.python.executable))
    return (
        *prefix,
        str(staged.path),
        "--evidence",
        str(marker_path),
        "--check",
        invocation.name,
        "--module",
        selected_module,
        "--",
        *invocation.arguments,
    )


def argument_digest(arguments: tuple[str, ...]) -> str:
    """Hash exact length-prefixed arguments independently from the launcher."""
    digest = hashlib.sha256()
    for argument in arguments:
        encoded = argument.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def ensure_start_marker_absent(
    marker_path: Path,
    *,
    workspace: VerifiedRunWorkspace,
) -> None:
    """Prove an invocation-owned marker leaf is absent immediately pre-spawn."""
    _require_workspace_child(marker_path, workspace)
    workspace.verify("immediately before Check spawn")
    try:
        fs.stat(
            marker_path.name,
            dir_fd=workspace.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    except OSError as error:
        raise OSError(f"Check start marker absence could not be proved: {error}") from error
    raise OSError("Check start marker existed before spawn")


def validate_start_marker(
    marker_path: Path,
    *,
    workspace: VerifiedRunWorkspace,
    invocation: CheckInvocation,
    module: CheckModule,
    prepared: PreparedRepositoryEnvironment,
) -> CheckStartObservation:
    """Read, revalidate, bind, and clean one descriptor-relative start marker."""
    _require_workspace_child(marker_path, workspace)
    workspace.verify("before Check start marker validation")
    descriptor: int | None = None
    try:
        descriptor = _open_marker(marker_path, workspace)
        initial = os.fstat(descriptor)
        _validate_marker_metadata(initial, marker_path.name, descriptor=descriptor)
        if initial.st_size > _MAX_MARKER_BYTES:
            raise OSError(f"Check start marker exceeds the 4096-byte limit: {marker_path.name}")
        content = bytearray()
        while len(content) <= _MAX_MARKER_BYTES:
            chunk = os.read(descriptor, _MAX_MARKER_BYTES + 1 - len(content))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > _MAX_MARKER_BYTES:
            raise OSError(f"Check start marker exceeds the 4096-byte limit: {marker_path.name}")
        final = os.fstat(descriptor)
        lexical = fs.stat(
            marker_path.name,
            dir_fd=workspace.descriptor,
            follow_symlinks=False,
        )
        if not _same_marker_snapshot(initial, final) or not _same_marker_snapshot(
            initial, lexical
        ):
            raise OSError("Check start marker identity or metadata changed during read")
        workspace.verify("after Check start marker read")
        try:
            payload = load_bounded_json(bytes(content), max_nesting=8)
        except (UnicodeDecodeError, ValueError) as error:
            raise OSError(f"Check start marker JSON is invalid: {error}") from error
        return _bind_marker(payload, invocation, module, prepared)
    except FileNotFoundError as error:
        raise OSError("Check start marker is missing") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            fs.unlink(marker_path.name, dir_fd=workspace.descriptor)
        except FileNotFoundError:
            pass


def _open_marker(path: Path, workspace: VerifiedRunWorkspace) -> int:
    no_follow = getattr(fs, "O_NOFOLLOW", None)
    non_blocking = getattr(fs, "O_NONBLOCK", None)
    if type(no_follow) is not int or type(non_blocking) is not int:
        raise OSError("safe no-follow Check start marker opening is unavailable")
    try:
        descriptor = fs.open(
            path.name,
            os.O_RDONLY | no_follow | non_blocking,
            dir_fd=workspace.descriptor,
        )
    except FileNotFoundError:
        raise
    except OSError as error:
        raise OSError(f"Check start marker is not a regular no-follow file: {error}") from error
    try:
        os.set_inheritable(descriptor, False)
    except BaseException:
        try:
            os.close(descriptor)
        except BaseException:
            pass
        raise
    return descriptor


def _validate_marker_metadata(
    file_status: os.stat_result, name: str, *, descriptor: int | None = None
) -> None:
    if not stat.S_ISREG(file_status.st_mode):
        raise OSError(f"Check start marker is not a regular file: {name}")
    if file_status.st_nlink != 1:
        raise OSError("Check start marker link count is not one")
    if os.name == "nt":
        if descriptor is None:
            raise OSError("Check start marker handle is unavailable for ACL validation")
        fs.verify_private(descriptor)
        return
    get_effective_uid = getattr(os, "geteuid", None)
    if not callable(get_effective_uid):
        raise OSError("effective user identity is unavailable for Check start marker")
    if file_status.st_uid != get_effective_uid():
        raise OSError("Check start marker owner is not the effective user")
    if stat.S_IMODE(file_status.st_mode) != 0o600:
        raise OSError("Check start marker mode is not exactly 0600")
    if file_status.st_nlink != 1:
        raise OSError("Check start marker link count is not one")


def _same_marker_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_uid,
        stat.S_IMODE(left.st_mode),
        left.st_nlink,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_uid,
        stat.S_IMODE(right.st_mode),
        right.st_nlink,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _bind_marker(
    raw: object,
    invocation: CheckInvocation,
    module: CheckModule,
    prepared: PreparedRepositoryEnvironment,
) -> CheckStartObservation:
    if not isinstance(raw, dict):
        raise OSError("Check start marker must be a JSON object")
    marker = cast(dict[str, object], raw)
    unknown = set(marker) - _MARKER_FIELDS
    missing = _MARKER_FIELDS - set(marker)
    if unknown:
        raise OSError(f"Check start marker has unknown fields: {sorted(unknown)}")
    if missing:
        raise OSError(f"Check start marker is missing fields: {sorted(missing)}")
    if type(marker["schema_version"]) is not int or marker["schema_version"] != 1:
        raise OSError("Check start marker schema version is invalid")
    if marker["check"] != invocation.name:
        raise OSError("Check start marker check does not match the invocation")
    if marker["module"] != module:
        raise OSError("Check start marker module does not match the invocation")
    expected_digest = argument_digest(invocation.arguments)
    digest = marker["arguments_sha256"]
    if (
        type(digest) is not str
        or _SHA256_PATTERN.fullmatch(digest) is None
        or digest != expected_digest
    ):
        raise OSError("Check start marker argument digest does not match the invocation")
    python = _parse_python(marker["python"])
    if python != prepared.python:
        raise OSError("Check start marker Python does not match Repository Python")
    return CheckStartObservation(1, invocation.name, module, digest, python)


def _parse_python(raw: object) -> PythonObservation:
    if not isinstance(raw, dict) or set(raw) != _PYTHON_FIELDS:
        raise OSError("Check start marker Python fields are invalid")
    evidence = cast(dict[str, object], raw)
    implementation = evidence["implementation"]
    version = evidence["version"]
    executable = evidence["executable"]
    if type(implementation) is not str or not implementation:
        raise OSError("Check start marker Python implementation is invalid")
    if (
        not isinstance(version, list)
        or len(version) != 3
        or any(type(part) is not int or part < 0 for part in version)
    ):
        raise OSError("Check start marker Python version is invalid")
    if type(executable) is not str or not executable:
        raise OSError("Check start marker Python executable is invalid")
    normalized = os.path.abspath(os.path.normpath(executable))
    if executable != normalized:
        raise OSError("Check start marker Python executable is not normalized")
    return PythonObservation(
        implementation,
        cast(tuple[int, int, int], tuple(version)),
        Path(executable),
    )


def _require_workspace_child(path: Path, workspace: VerifiedRunWorkspace) -> None:
    if path.parent != workspace.workspace.path or path.name in {"", ".", ".."}:
        raise OSError("Check start marker is outside the held run workspace")
