from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess  # nosec B404

import pytest

from pyrepo_check.repository_safety import (
    capture_repository_baseline,
    verify_repository_state,
)
from tests.support import RecordingRunner, monotonic_clock


def _run_git(
    root: Path,
    *arguments: str,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # nosec B603
        ("git", "-C", str(root), *arguments),
        input=input_bytes,
        check=True,
        capture_output=True,
    )


def initialize_git_fixture(root: Path) -> Path:
    resolved = root.resolve()
    (resolved / "src").mkdir(parents=True)
    (resolved / "src/example.py").write_text("value = 1\n", encoding="utf-8")
    (resolved / "pyproject.toml").write_text(
        "[project]\nname = 'fixture'\nversion = '0'\n",
        encoding="utf-8",
    )
    (resolved / "uv.lock").write_text(
        "version = 1\nrevision = 3\n",
        encoding="utf-8",
    )
    (resolved / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    _run_git(resolved, "init", "-q")
    _run_git(resolved, "add", ".")
    _commit_fixture(resolved, "fixture")
    return resolved


def _commit_fixture(root: Path, message: str) -> None:
    _run_git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        message,
    )


def _write_non_git_project(root: Path) -> Path:
    resolved = root.resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    (resolved / "pyproject.toml").write_bytes(b"[project]\nname='fixture'\n")
    (resolved / "uv.lock").write_bytes(b"version = 1\nrevision = 3\n")
    return resolved


def _scripted_stage(root: Path) -> bytes:
    object_id = "a" * 40
    return b"".join(
        f"100644 {object_id} 0\t{path}\0".encode()
        for path in (".gitignore", "pyproject.toml", "src/example.py", "uv.lock")
    )


def _set_unmerged_index(root: Path) -> None:
    blob = _run_git(root, "hash-object", "src/example.py").stdout.strip().decode()
    _run_git(
        root,
        "update-index",
        "--index-info",
        input_bytes=(
            f"0 {'0' * 40}\tsrc/example.py\n"
            f"100644 {blob} 1\tsrc/example.py\n"
        ).encode(),
    )


def _add_gitlink(root: Path) -> str:
    commit = _run_git(root, "rev-parse", "HEAD").stdout.strip().decode()
    _run_git(root, "update-index", "--add", "--cacheinfo", f"160000,{commit},vendor")
    return commit


def _new_commit_object(root: Path, parent: str) -> str:
    tree = _run_git(root, "rev-parse", f"{parent}^{{tree}}").stdout.strip().decode()
    return (
        _run_git(
            root,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit-tree",
            tree,
            "-p",
            parent,
            "-m",
            "alternate",
        )
        .stdout.strip()
        .decode()
    )


def test_ignored_untracked_venv_is_safe(tmp_path: Path) -> None:
    root = initialize_git_fixture(tmp_path)
    (root / ".venv").mkdir()

    result = capture_repository_baseline(
        root,
        runner=None,
        clock_ns=monotonic_clock(),
    )

    assert result.error is None
    assert result.snapshot is not None
    assert result.snapshot.git_root == root
    assert tuple(process.command for process in result.processes) == (
        ("git", "-C", str(root), "rev-parse", "--show-toplevel"),
        ("git", "-C", str(root), "ls-files", "-z", "--", ".venv"),
        (
            "git",
            "-C",
            str(root),
            "check-ignore",
            "--no-index",
            "-q",
            "--",
            ".venv/",
        ),
        ("git", "-C", str(root), "ls-files", "--stage", "-z", "--", "."),
    )
    assert tuple(entry.path for entry in result.snapshot.tracked_files) == (
        ".gitignore",
        "pyproject.toml",
        "src/example.py",
        "uv.lock",
    )


def test_tracked_venv_is_rejected(tmp_path: Path) -> None:
    root = initialize_git_fixture(tmp_path)
    (root / ".venv").mkdir()
    (root / ".venv/tracked.txt").write_text("tracked\n", encoding="utf-8")
    _run_git(root, "add", "-f", ".venv/tracked.txt")

    result = capture_repository_baseline(
        root,
        runner=None,
        clock_ns=monotonic_clock(),
    )

    assert result.snapshot is None
    assert result.error is not None
    assert result.error.code == "unsafe_repository_environment"
    assert len(result.processes) == 2


def test_unignored_venv_is_rejected(tmp_path: Path) -> None:
    root = initialize_git_fixture(tmp_path)
    (root / ".gitignore").write_text("", encoding="utf-8")

    result = capture_repository_baseline(
        root,
        runner=None,
        clock_ns=monotonic_clock(),
    )

    assert result.snapshot is None
    assert result.error is not None
    assert result.error.code == "unsafe_repository_environment"
    assert len(result.processes) == 3


def test_symlinked_venv_is_rejected_without_following_it(tmp_path: Path) -> None:
    root = initialize_git_fixture(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (root / ".venv").symlink_to(outside, target_is_directory=True)

    result = capture_repository_baseline(
        root,
        runner=None,
        clock_ns=monotonic_clock(),
    )

    assert result.snapshot is None
    assert result.error is not None
    assert result.error.code == "unsafe_repository_environment"
    assert result.processes == ()
    assert list(outside.iterdir()) == []


def test_git_marker_with_missing_git_is_rejected(tmp_path: Path) -> None:
    root = initialize_git_fixture(tmp_path)
    runner = RecordingRunner(raise_on_call=1)

    result = capture_repository_baseline(
        root,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert result.snapshot is None
    assert result.error is not None
    assert result.error.code == "unsafe_repository_environment"
    assert len(result.processes) == 1
    assert result.processes[0].spawn_error is not None


def test_ancestor_git_marker_with_missing_git_is_rejected(tmp_path: Path) -> None:
    outer = initialize_git_fixture(tmp_path)
    root = outer / "nested-project"
    root.mkdir()
    _write_non_git_project(root)
    runner = RecordingRunner(raise_on_call=1)

    result = capture_repository_baseline(
        root,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert result.snapshot is None
    assert result.error is not None
    assert result.error.code == "unsafe_repository_environment"
    assert len(result.processes) == 1
    assert result.processes[0].spawn_error is not None


def test_non_git_root_records_only_protected_files(tmp_path: Path) -> None:
    root = _write_non_git_project(tmp_path)

    result = capture_repository_baseline(
        root,
        runner=None,
        clock_ns=monotonic_clock(),
    )

    assert result.error is None
    assert result.snapshot is not None
    assert result.snapshot.git_root is None
    assert result.snapshot.tracked_files == ()
    assert tuple(Path(entry.path) for entry in result.snapshot.protected_files) == (
        root / "pyproject.toml",
        root / "uv.lock",
    )
    assert all(entry.kind == "regular" for entry in result.snapshot.protected_files)
    assert all(entry.sha256 is not None for entry in result.snapshot.protected_files)
    assert len(result.processes) == 1


def test_initial_unmerged_index_is_rejected(tmp_path: Path) -> None:
    root = initialize_git_fixture(tmp_path)
    _set_unmerged_index(root)

    result = capture_repository_baseline(
        root,
        runner=None,
        clock_ns=monotonic_clock(),
    )

    assert result.snapshot is None
    assert result.error is not None
    assert result.error.code == "unsafe_repository_environment"
    assert result.processes[-1].command[-5:] == (
        "ls-files",
        "--stage",
        "-z",
        "--",
        ".",
    )


def test_git_probes_ignore_all_inherited_git_redirection(tmp_path: Path) -> None:
    root = initialize_git_fixture(tmp_path)
    runner = RecordingRunner(
        stdout=(
            os.fsencode(root) + b"\n",
            b"",
            b"",
            _scripted_stage(root),
        )
    )
    controller = {
        "PATH": "/bin:/usr/bin",
        "GIT_DIR": "/wrong/git-dir",
        "GIT_WORK_TREE": "/wrong/worktree",
        "GIT_INDEX_FILE": "/wrong/index",
        "GIT_CONFIG_COUNT": "1",
    }

    capture_repository_baseline(
        root,
        runner=runner,
        clock_ns=monotonic_clock(),
        controller_environment=controller,
    )

    git_environments = [call.env for call in runner.calls if call.command[0] == "git"]
    assert git_environments
    assert all(environment is not None for environment in git_environments)
    assert all(
        environment["GIT_OPTIONAL_LOCKS"] == "0"
        for environment in git_environments
        if environment is not None
    )
    assert all(
        environment["LC_ALL"] == "C"
        for environment in git_environments
        if environment is not None
    )
    assert all(
        "GIT_DIR" not in environment
        for environment in git_environments
        if environment is not None
    )
    assert all(
        "GIT_WORK_TREE" not in environment
        for environment in git_environments
        if environment is not None
    )
    assert all(
        "GIT_INDEX_FILE" not in environment
        for environment in git_environments
        if environment is not None
    )
    assert all(
        "GIT_CONFIG_COUNT" not in environment
        for environment in git_environments
        if environment is not None
    )


def test_git_root_evidence_must_contain_the_project_root(tmp_path: Path) -> None:
    root = initialize_git_fixture(tmp_path)
    unrelated = (tmp_path.parent / f"{tmp_path.name}-unrelated-root").resolve()
    runner = RecordingRunner(stdout=(os.fsencode(unrelated) + b"\n",))

    result = capture_repository_baseline(
        root,
        runner=runner,
        clock_ns=monotonic_clock(),
    )

    assert result.snapshot is None
    assert result.error is not None
    assert result.error.code == "unsafe_repository_environment"
    assert len(result.processes) == 1


def test_unchanged_clean_git_state_is_verified(tmp_path: Path) -> None:
    root = initialize_git_fixture(tmp_path)
    baseline = capture_repository_baseline(
        root,
        runner=None,
        clock_ns=monotonic_clock(),
    )
    assert baseline.snapshot is not None

    result = verify_repository_state(
        baseline.snapshot,
        annotations_fix_selected=False,
        runner=None,
        clock_ns=monotonic_clock(),
    )

    assert result.error is None
    assert result.mutation_protection == "tracked_files"
    assert len(result.processes) == 1
    assert result.processes[-1].command[-5:] == (
        "ls-files",
        "--stage",
        "-z",
        "--",
        ".",
    )


def test_unchanged_already_dirty_state_is_verified(tmp_path: Path) -> None:
    root = initialize_git_fixture(tmp_path)
    (root / "src/example.py").write_text("dirty = True\n", encoding="utf-8")
    baseline = capture_repository_baseline(
        root,
        runner=None,
        clock_ns=monotonic_clock(),
    )
    assert baseline.snapshot is not None

    result = verify_repository_state(
        baseline.snapshot,
        annotations_fix_selected=False,
        runner=None,
        clock_ns=monotonic_clock(),
    )

    assert result.error is None
    assert result.mutation_protection == "tracked_files"


def test_initially_dirty_file_changed_again_is_rejected(tmp_path: Path) -> None:
    root = initialize_git_fixture(tmp_path)
    source = root / "src/example.py"
    source.write_text("dirty = True\n", encoding="utf-8")
    baseline = capture_repository_baseline(
        root,
        runner=None,
        clock_ns=monotonic_clock(),
    )
    assert baseline.snapshot is not None
    source.write_text("dirtier = True\n", encoding="utf-8")

    result = verify_repository_state(
        baseline.snapshot,
        annotations_fix_selected=False,
        runner=None,
        clock_ns=monotonic_clock(),
    )

    assert result.error is not None
    assert result.error.code == "repository_state_changed"
    assert result.mutation_protection == "tracked_files"


def test_tracked_file_cannot_be_read_through_an_external_symlink_ancestor(
    tmp_path: Path,
) -> None:
    root = initialize_git_fixture(tmp_path)
    package = root / "pkg"
    package.mkdir()
    tracked = package / "module.py"
    tracked.write_text("value = 1\n", encoding="utf-8")
    _run_git(root, "add", "pkg/module.py")
    _commit_fixture(root, "track nested source")
    baseline = capture_repository_baseline(
        root,
        runner=None,
        clock_ns=monotonic_clock(),
    )
    assert baseline.snapshot is not None

    outside = tmp_path.parent / f"{tmp_path.name}-external-package"
    outside.mkdir()
    (outside / "module.py").write_text("value = 1\n", encoding="utf-8")
    tracked.unlink()
    package.rmdir()
    package.symlink_to(outside, target_is_directory=True)

    result = verify_repository_state(
        baseline.snapshot,
        annotations_fix_selected=False,
        runner=None,
        clock_ns=monotonic_clock(),
    )

    assert result.error is not None
    assert result.error.code == "repository_state_changed"


@pytest.mark.parametrize("protected_name", ["pyproject.toml", "uv.lock"])
def test_non_git_protected_file_change_is_rejected(
    tmp_path: Path,
    protected_name: str,
) -> None:
    root = _write_non_git_project(tmp_path)
    baseline = capture_repository_baseline(
        root,
        runner=None,
        clock_ns=monotonic_clock(),
    )
    assert baseline.snapshot is not None
    (root / protected_name).write_text("changed\n", encoding="utf-8")

    result = verify_repository_state(
        baseline.snapshot,
        annotations_fix_selected=False,
        runner=None,
        clock_ns=monotonic_clock(),
    )

    assert result.processes == ()
    assert result.mutation_protection == "protected_files"
    assert result.error is not None
    assert result.error.code == "repository_state_changed"


def test_gitlink_contents_are_not_descended_into(tmp_path: Path) -> None:
    root = initialize_git_fixture(tmp_path)
    _add_gitlink(root)
    baseline = capture_repository_baseline(
        root,
        runner=None,
        clock_ns=monotonic_clock(),
    )
    assert baseline.snapshot is not None
    gitlink = next(entry for entry in baseline.snapshot.tracked_files if entry.path == "vendor")
    assert gitlink.working_tree_kind == "gitlink"
    assert gitlink.sha256 is None
    (root / "vendor").mkdir()
    (root / "vendor/nested.txt").write_text("unobserved\n", encoding="utf-8")

    result = verify_repository_state(
        baseline.snapshot,
        annotations_fix_selected=False,
        runner=None,
        clock_ns=monotonic_clock(),
    )

    assert result.error is None
    assert result.mutation_protection == "tracked_files"


def test_annotations_fix_exempts_source_bytes_but_never_dependency_files(
    tmp_path: Path,
) -> None:
    root = initialize_git_fixture(tmp_path)
    baseline = capture_repository_baseline(root, runner=None, clock_ns=monotonic_clock())
    assert baseline.snapshot is not None
    (root / "src/example.py").write_text("fixed = True\n", encoding="utf-8")

    allowed = verify_repository_state(
        baseline.snapshot,
        annotations_fix_selected=True,
        runner=None,
        clock_ns=monotonic_clock(),
    )
    assert allowed.error is None

    (root / "uv.lock").write_text("changed\n", encoding="utf-8")
    rejected = verify_repository_state(
        baseline.snapshot,
        annotations_fix_selected=True,
        runner=None,
        clock_ns=monotonic_clock(),
    )
    assert rejected.error is not None
    assert rejected.error.code == "repository_state_changed"


@pytest.mark.parametrize(
    "mutation",
    [
        "mode",
        "regular_to_symlink",
        "symlink_to_regular",
        "deletion",
        "added_tracked_path",
        "unmerged_stage",
        "gitlink_object",
        "pyproject_content",
        "lock_content",
    ],
)
def test_annotations_fix_never_exempts_structural_or_dependency_changes(
    tmp_path: Path,
    mutation: str,
) -> None:
    root = initialize_git_fixture(tmp_path)
    gitlink_object: str | None = None
    if mutation == "symlink_to_regular":
        source = root / "src/example.py"
        source.unlink()
        source.symlink_to("target.py")
        _run_git(root, "add", "src/example.py")
        _commit_fixture(root, "track symlink")
    if mutation == "gitlink_object":
        gitlink_object = _add_gitlink(root)
    baseline = capture_repository_baseline(
        root,
        runner=None,
        clock_ns=monotonic_clock(),
    )
    assert baseline.snapshot is not None

    source = root / "src/example.py"
    if mutation == "mode":
        source.chmod(stat.S_IMODE(source.stat().st_mode) | stat.S_IXUSR)
    elif mutation == "regular_to_symlink":
        source.unlink()
        source.symlink_to("target.py")
    elif mutation == "symlink_to_regular":
        source.unlink()
        source.write_text("regular = True\n", encoding="utf-8")
    elif mutation == "deletion":
        source.unlink()
    elif mutation == "added_tracked_path":
        added = root / "src/added.py"
        added.write_text("added = True\n", encoding="utf-8")
        _run_git(root, "add", "src/added.py")
    elif mutation == "unmerged_stage":
        _set_unmerged_index(root)
    elif mutation == "gitlink_object":
        assert gitlink_object is not None
        replacement = _new_commit_object(root, gitlink_object)
        _run_git(
            root,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{replacement},vendor",
        )
    elif mutation == "pyproject_content":
        (root / "pyproject.toml").write_text("changed\n", encoding="utf-8")
    elif mutation == "lock_content":
        (root / "uv.lock").write_text("changed\n", encoding="utf-8")
    else:
        raise AssertionError(f"unknown mutation {mutation}")

    result = verify_repository_state(
        baseline.snapshot,
        annotations_fix_selected=True,
        runner=None,
        clock_ns=monotonic_clock(),
    )

    assert result.processes
    assert result.processes[-1].command[-5:] == (
        "ls-files",
        "--stage",
        "-z",
        "--",
        ".",
    )
    assert result.error is not None
    assert result.error.code == "repository_state_changed"


def test_annotations_fix_allows_existing_symlink_target_content_change(
    tmp_path: Path,
) -> None:
    root = initialize_git_fixture(tmp_path)
    source = root / "src/example.py"
    source.unlink()
    source.symlink_to("target-a.py")
    _run_git(root, "add", "src/example.py")
    _commit_fixture(root, "track symlink")
    baseline = capture_repository_baseline(
        root,
        runner=None,
        clock_ns=monotonic_clock(),
    )
    assert baseline.snapshot is not None
    source.unlink()
    source.symlink_to("target-b.py")

    result = verify_repository_state(
        baseline.snapshot,
        annotations_fix_selected=True,
        runner=None,
        clock_ns=monotonic_clock(),
    )

    assert result.error is None
    assert result.mutation_protection == "tracked_files"
