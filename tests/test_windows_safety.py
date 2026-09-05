"""Native Windows regressions for filesystem safety boundaries."""

from __future__ import annotations

import hashlib
from itertools import count
import os
from pathlib import Path
import subprocess  # nosec B404

import pytest

from pyrepo_check.artifact_safety import (
    copy_regular_file,
    digest_regular_file,
    read_regular_file,
)
from pyrepo_check.config import load_project_config
from pyrepo_check import execution_workspace, filesystem
from pyrepo_check.execution_workspace import (
    create_run_workspace,
    open_verified_workspace,
    remove_run_workspace,
)
from pyrepo_check.repository_safety import (
    capture_repository_baseline,
    verify_repository_state,
)
from tests.support import monotonic_clock


pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows regression coverage")


def _powershell_literal(path: Path) -> str:
    return str(path).replace("'", "''")


def _create_junction(link: Path, target: Path) -> None:
    command = (
        "New-Item -ItemType Junction -Path "
        f"'{_powershell_literal(link)}' -Target '{_powershell_literal(target)}' "
        "-ErrorAction Stop | Out-Null"
    )
    result = subprocess.run(  # nosec B603
        ("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _write_non_git_project(root: Path) -> None:
    (root / "pyproject.toml").write_bytes(b"[project]\r\nname = 'fixture'\r\n")
    (root / "uv.lock").write_bytes(b"version = 1\r\nrevision = 3\r\n")


def test_regular_reader_preserves_windows_binary_bytes_and_exact_limit(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.bin"
    payload = b"line-one\r\n\x1aline-two\r\n\x00"
    artifact.write_bytes(payload)

    assert read_regular_file(artifact, max_bytes=len(payload)) == payload


def test_regular_reader_rejects_one_byte_over_windows_binary_limit(tmp_path: Path) -> None:
    artifact = tmp_path / "oversized.bin"
    artifact.write_bytes(b"a\r\n\x1ab")

    with pytest.raises(OSError, match="exceeds"):
        read_regular_file(artifact, max_bytes=4)


def test_digest_regular_file_hashes_windows_binary_bytes_without_text_translation(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "digest.bin"
    payload = b"first\r\n\x1asecond\r\n"
    artifact.write_bytes(payload)

    observed = digest_regular_file(artifact, max_bytes=len(payload))

    assert observed.size == len(payload)
    assert observed.sha256 == hashlib.sha256(payload).hexdigest()


def test_regular_reader_rejects_windows_file_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.bin"
    target.write_bytes(b"protected")
    link = tmp_path / "artifact.bin"
    try:
        link.symlink_to(target)
    except OSError as error:
        if getattr(error, "winerror", None) != 1314:
            raise
        raise pytest.skip.Exception(f"Windows file symlink privilege is unavailable: {error}")

    with pytest.raises(OSError):
        read_regular_file(link, max_bytes=64)


def test_digest_regular_file_rejects_windows_directory_junction(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "artifact.bin").write_bytes(b"protected")
    junction = tmp_path / "junction"
    _create_junction(junction, outside)

    with pytest.raises(OSError):
        digest_regular_file(junction / "artifact.bin", max_bytes=64)


def test_copy_regular_file_uses_exclusive_destination_without_clobbering(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"replacement\r\n\x1a")
    destination = tmp_path / "destination.bin"
    destination.write_bytes(b"keep this")

    with pytest.raises(FileExistsError):
        copy_regular_file(source, destination, max_bytes=64)

    assert destination.read_bytes() == b"keep this"


def test_project_config_rejects_auto_detected_windows_junction_target(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = repository / "src"
    _create_junction(junction, outside)

    with pytest.raises(ValueError, match="target must remain beneath the project root"):
        load_project_config(repository)


def test_windows_workspace_lifecycle_is_verified_and_cleanup_removes_only_its_run_tree(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    workspace = create_run_workspace(repository)
    verified = open_verified_workspace(workspace)
    try:
        verified.verify("Windows lifecycle")
        (workspace.path / "evidence.bin").write_bytes(b"evidence\r\n\x1a")
    finally:
        verified.close()

    assert remove_run_workspace(workspace, repository_root=repository, clock_ns=lambda: 0) is None
    assert not workspace.path.exists()


def test_windows_workspace_root_replacement_fails_closed_and_preserves_external_sentinel(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    workspace = create_run_workspace(repository)
    original = workspace.path.with_name(f"{workspace.path.name}-original")
    verified = open_verified_workspace(workspace)
    replacement_prevented = False

    try:
        try:
            workspace.path.rename(original)
        except OSError:
            replacement_prevented = True
            verified.verify("held Windows directory handle")
        else:
            _create_junction(workspace.path, outside)
            with pytest.raises(OSError, match="identity mismatch"):
                verified.verify("after root replacement")
    finally:
        verified.close()

    try:
        if replacement_prevented:
            assert remove_run_workspace(
                workspace, repository_root=repository, clock_ns=lambda: 0
            ) is None
        else:
            observation = remove_run_workspace(
                workspace, repository_root=repository, clock_ns=lambda: 0
            )
            assert observation is not None
            assert observation.kind == "unsafe_tree"
            assert sentinel.read_text(encoding="utf-8") == "keep"
    finally:
        if workspace.path.exists():
            workspace.path.rmdir()
        if original.exists():
            original.rmdir()


def test_windows_protected_repository_snapshot_detects_mutation(tmp_path: Path) -> None:
    _write_non_git_project(tmp_path)
    baseline = capture_repository_baseline(
        tmp_path,
        runner=None,
        clock_ns=monotonic_clock(),
        git_executable=None,
    )

    assert baseline.error is None
    assert baseline.snapshot is not None
    (tmp_path / "uv.lock").write_bytes(b"version = 2\r\nrevision = 3\r\n")

    result = verify_repository_state(
        baseline.snapshot,
        annotations_fix_targets=None,
        runner=None,
        clock_ns=monotonic_clock(),
        git_executable=None,
    )

    assert result.mutation_protection == "protected_files"
    assert result.error is not None


def test_windows_cleanup_preserves_a_leaf_replaced_after_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = create_run_workspace(tmp_path)
    leaf = workspace.path / "evidence.bin"
    leaf.write_bytes(b"original")
    original_open = filesystem.open_for_cleanup

    def replace_before_open(path: str | Path, *, dir_fd: int | None = None) -> int:
        if str(path) == leaf.name:
            leaf.unlink()
            leaf.write_bytes(b"replacement must survive")
        return original_open(path, dir_fd=dir_fd)

    with monkeypatch.context() as patch:
        patch.setattr(filesystem, "open_for_cleanup", replace_before_open)
        observation = remove_run_workspace(workspace, repository_root=tmp_path)
    try:
        assert observation is not None
        assert observation.kind == "unsafe_tree"
        assert leaf.read_bytes() == b"replacement must survive"
    finally:
        if workspace.path.exists():
            assert remove_run_workspace(workspace, repository_root=tmp_path) is None


@pytest.mark.parametrize("limit", ("entries", "depth", "deadline"))
def test_windows_cleanup_budget_failure_precedes_any_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    workspace = create_run_workspace(tmp_path)
    leaves = [workspace.path / name for name in ("one", "two")]
    for leaf in leaves:
        leaf.write_bytes(b"keep")
    with monkeypatch.context() as patch:
        if limit == "entries":
            patch.setattr(execution_workspace, "_MAX_CLEANUP_ENTRIES", 1)
        elif limit == "depth":
            patch.setattr(execution_workspace, "_MAX_CLEANUP_DEPTH", 0)
        ticks = count(step=6_000_000_000 if limit == "deadline" else 0)
        observation = remove_run_workspace(
            workspace, repository_root=tmp_path, clock_ns=lambda: next(ticks)
        )
    try:
        assert observation is not None
        assert observation.kind == "budget_exceeded"
        assert all(leaf.read_bytes() == b"keep" for leaf in leaves)
    finally:
        assert remove_run_workspace(workspace, repository_root=tmp_path) is None


def test_windows_workspace_cleanup_removes_nested_junction_without_following_it(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"keep outside")
    workspace = create_run_workspace(tmp_path)
    nested = workspace.path / "nested"
    nested.mkdir()
    _create_junction(nested / "junction", outside)

    assert remove_run_workspace(workspace, repository_root=tmp_path) is None
    assert not workspace.path.exists()
    assert sentinel.read_bytes() == b"keep outside"


def test_windows_cleanup_preserves_children_created_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = create_run_workspace(tmp_path)
    original = workspace.path / "original.bin"
    original.write_bytes(b"known entry")
    added = workspace.path / "concurrent.bin"
    delete_open_file = filesystem.delete_open_file
    added_once = False

    def add_before_deletion(descriptor: int) -> None:
        nonlocal added_once
        if not added_once:
            added_once = True
            added.write_bytes(b"not in manifest")
        delete_open_file(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(filesystem, "delete_open_file", add_before_deletion)
        observation = remove_run_workspace(workspace, repository_root=tmp_path)
    try:
        assert observation is not None
        assert observation.kind == "io_failed"
        assert observation.retained_run_path == workspace.path
        assert added.read_bytes() == b"not in manifest"
    finally:
        assert remove_run_workspace(workspace, repository_root=tmp_path) is None


def test_windows_cleanup_preserves_tree_when_an_open_reader_denies_deletion(
    tmp_path: Path,
) -> None:
    workspace = create_run_workspace(tmp_path)
    leaf = workspace.path / "busy.bin"
    leaf.write_bytes(b"keep while busy")
    with leaf.open("rb"):
        observation = remove_run_workspace(workspace, repository_root=tmp_path)
    try:
        assert observation is not None
        assert observation.kind == "io_failed"
        assert observation.retained_run_path == workspace.path
        assert leaf.read_bytes() == b"keep while busy"
    finally:
        assert remove_run_workspace(workspace, repository_root=tmp_path) is None


@pytest.mark.parametrize("populated", (False, True), ids=("empty", "concurrent-child"))
def test_windows_workspace_creation_failure_removes_only_its_empty_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, populated: bool
) -> None:
    from pyrepo_check import _windows_workspace

    base = tmp_path / "temporary"
    base.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()

    def unsupported_acl(descriptor: int) -> None:
        if populated:
            created = next(base.iterdir())
            (created / "concurrent.bin").write_bytes(b"keep")
        raise filesystem.PlatformSafetyError("private ACL unsupported")

    with monkeypatch.context() as patch:
        patch.setattr(_windows_workspace.tempfile, "gettempdir", lambda: str(base))
        patch.setattr(filesystem, "verify_private", unsupported_acl)
        with pytest.raises(filesystem.PlatformSafetyError, match="private ACL unsupported") as raised:
            create_run_workspace(repository)

    if populated:
        retained = next(base.iterdir())
        child = retained / "concurrent.bin"
        assert child.read_bytes() == b"keep"
        assert "rejected workspace cleanup failed" in " ".join(raised.value.__notes__)
        child.unlink()
        retained.rmdir()
    assert tuple(base.iterdir()) == ()
