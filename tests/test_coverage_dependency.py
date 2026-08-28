from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess  # nosec B404
import sys

import pytest

from pyrepo_check.coverage_dependency import (
    coverage_json_staged_command,
    ensure_staged_coverage_dependency,
    stage_coverage_dependency,
)
from tests.support import test_workspace


def _coverage_package(environment: Path, marker: str) -> Path:
    package = environment / "site-packages/coverage"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import json, pathlib, sys\n"
        "output = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
        f"output.write_text(json.dumps({{'producer': {marker!r}}}))\n",
        encoding="utf-8",
    )
    return package / "__init__.py"


def _run_staged(root: Path, command: tuple[str, ...], output: Path) -> dict[str, str]:
    subprocess.run(  # nosec B603
        command,
        cwd=root,
        check=True,
        capture_output=True,
    )
    return json.loads(output.read_text(encoding="utf-8"))


def test_pytest_created_repository_coverage_shadow_cannot_produce_json(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    environment = root / ".venv"
    origin = _coverage_package(environment, "locked")
    shadow_witness = root / "shadow-ran"
    output = tmp_path / "coverage.json"

    with test_workspace(root) as workspace:
        staged = stage_coverage_dependency(
            origin=origin,
            environment_root=environment,
            workspace=workspace,
        )
        (root / "coverage.py").write_text(
            f"from pathlib import Path\nPath({str(shadow_witness)!r}).write_text('ran')\n",
            encoding="utf-8",
        )
        command = coverage_json_staged_command(
            python_prefix=(sys.executable,),
            staged=staged,
            coverage_arguments=("json", "-o", str(output)),
        )
        document = _run_staged(root, command, output)

    assert document == {"producer": "locked"}
    assert not shadow_witness.exists()


def test_same_origin_venv_coverage_mutation_after_staging_cannot_run(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    environment = root / ".venv"
    origin = _coverage_package(environment, "locked")
    output = tmp_path / "coverage.json"

    with test_workspace(root) as workspace:
        staged = stage_coverage_dependency(
            origin=origin,
            environment_root=environment,
            workspace=workspace,
        )
        _coverage_package(environment, "mutated")
        command = coverage_json_staged_command(
            python_prefix=(sys.executable,),
            staged=staged,
            coverage_arguments=("json", "-o", str(output)),
        )
        document = _run_staged(root, command, output)

    assert document == {"producer": "locked"}


def test_pytest_mutation_of_staged_coverage_is_rejected_before_json(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    environment = root / ".venv"
    origin = _coverage_package(environment, "locked")

    with test_workspace(root) as workspace:
        staged = stage_coverage_dependency(
            origin=origin,
            environment_root=environment,
            workspace=workspace,
        )
        (staged.module_root / "coverage/__main__.py").write_text(
            "raise SystemExit('mutated')\n",
            encoding="utf-8",
        )
        with pytest.raises(OSError, match="staged Coverage dependency changed"):
            ensure_staged_coverage_dependency(staged, workspace=workspace)


def test_pytest_module_root_alias_is_rejected_before_json(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    environment = root / ".venv"
    origin = _coverage_package(environment, "locked")

    with test_workspace(root) as workspace:
        staged = stage_coverage_dependency(
            origin=origin,
            environment_root=environment,
            workspace=workspace,
        )
        external = tmp_path / "aliased-module-root"
        shutil.copytree(staged.module_root, external)
        shutil.rmtree(staged.module_root)
        staged.module_root.symlink_to(external, target_is_directory=True)
        with pytest.raises(OSError):
            ensure_staged_coverage_dependency(staged, workspace=workspace)
