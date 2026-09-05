"""Native controller executable selection."""

import os
from pathlib import Path

import pytest

from pyrepo_check.controller_tools import resolve_controller_tools


@pytest.mark.skipif(os.name != "nt", reason="Windows executable suffix")
def test_controller_resolves_external_native_executables(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    external = tmp_path / "tools"
    external.mkdir()
    for name in ("uv.exe", "git.exe"):
        (external / name).write_bytes(b"fixture")
    tools = resolve_controller_tools(repository, path=str(external))
    assert tools.uv is not None
    assert tools.uv.path == external / "uv.exe"
    assert tools.git is not None
    assert tools.git.path == external / "git.exe"
