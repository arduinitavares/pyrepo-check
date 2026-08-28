from __future__ import annotations

import os
from pathlib import Path

from pyrepo_check.controller_tools import resolve_controller_tools


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_resolution_skips_repository_and_unsafe_path_entries(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _executable(root / ".venv/bin/uv")
    _executable(root / "bin/git")
    external = tmp_path / "controller-bin"
    safe_uv = _executable(external / "uv").resolve()
    safe_git = _executable(external / "git").resolve()

    tools = resolve_controller_tools(
        root,
        path=os.pathsep.join(
            ("", ".", str(root / ".venv/bin"), str(root / "bin"), str(external))
        ),
    )

    assert tools.uv is not None and tools.uv.path == safe_uv
    assert tools.git is not None and tools.git.path == safe_git


def test_resolution_rejects_repository_resolving_alias(tmp_path: Path) -> None:
    root = tmp_path / "project"
    repository_bin = root / "bin"
    _executable(repository_bin / "uv")
    external_alias = tmp_path / "alias"
    external_alias.symlink_to(repository_bin, target_is_directory=True)

    tools = resolve_controller_tools(root, path=str(external_alias))

    assert tools.uv is None


def test_controller_executable_detects_identity_replacement(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    executable = _executable(tmp_path / "bin/uv")
    tools = resolve_controller_tools(root, path=str(executable.parent))
    assert tools.uv is not None
    assert tools.uv.path_for_use() == executable.resolve()

    replacement = _executable(tmp_path / "replacement")
    executable.unlink()
    replacement.rename(executable)

    try:
        tools.uv.path_for_use()
    except OSError as error:
        assert "identity changed" in str(error)
    else:
        raise AssertionError("replacement executable was accepted")
