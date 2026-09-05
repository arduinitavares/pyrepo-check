from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess  # nosec B404
import sys

import pytest

from tests.support import symlink_or_skip

from pyrepo_check.coverage_dependency import (
    coverage_json_staged_command,
    ensure_staged_coverage_dependency,
    stage_coverage_dependency,
)
from pyrepo_check.coverage_execution import coverage_json_environment
from tests.support import test_workspace


def _coverage_package(
    environment: Path,
    marker: str,
    *,
    main_source: str | None = None,
) -> Path:
    package = environment / "site-packages/coverage"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        (
            "import json, pathlib, sys\n"
            "output = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
            f"output.write_text(json.dumps({{'producer': {marker!r}}}))\n"
            if main_source is None
            else main_source
        ),
        encoding="utf-8",
    )
    return package / "__init__.py"


def _run_staged(
    root: Path,
    command: tuple[str, ...],
    output: Path,
    *,
    environment: dict[str, str] | None = None,
) -> dict[str, str]:
    subprocess.run(  # nosec B603
        command,
        cwd=root,
        env=environment,
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


@pytest.mark.parametrize("module_name", ("os", "json", "runpy"))
def test_workspace_stdlib_shadow_cannot_run_in_coverage_json_launcher(
    tmp_path: Path,
    module_name: str,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    environment_root = root / ".venv"
    origin = _coverage_package(environment_root, "locked")
    shadow_witness = tmp_path / f"{module_name}-shadow-ran"
    output = tmp_path / "coverage.json"

    with test_workspace(root) as workspace:
        staged = stage_coverage_dependency(
            origin=origin,
            environment_root=environment_root,
            workspace=workspace,
        )
        shadow_directory = root if module_name == "os" else workspace.workspace.path
        (shadow_directory / f"{module_name}.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(shadow_witness)!r}).write_text('ran', encoding='utf-8')\n"
            "raise RuntimeError('workspace stdlib shadow imported')\n",
            encoding="utf-8",
        )
        command = coverage_json_staged_command(
            python_prefix=(sys.executable, "-S", "-X", "frozen_modules=off"),
            staged=staged,
            coverage_arguments=("json", "-o", str(output)),
        )
        document = _run_staged(
            root,
            command,
            output,
            environment={**os.environ, "PYTHONPATH": str(shadow_directory)},
        )

    assert document == {"producer": "locked"}
    assert not shadow_witness.exists()


def test_real_staged_helper_does_not_mutate_its_verified_dependency(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    environment_root = root / ".venv"
    origin = _coverage_package(environment_root, "locked")
    output = tmp_path / "coverage.json"

    with test_workspace(root) as workspace:
        staged = stage_coverage_dependency(
            origin=origin,
            environment_root=environment_root,
            workspace=workspace,
        )
        command = coverage_json_staged_command(
            python_prefix=(sys.executable,),
            staged=staged,
            coverage_arguments=("json", "-o", str(output)),
        )
        document = _run_staged(
            root,
            command,
            output,
            environment=coverage_json_environment(
                os.environ,
                data_path=tmp_path / "coverage-data",
                config_path=root / "pyproject.toml",
            ),
        )
        ensure_staged_coverage_dependency(staged, workspace=workspace)

    assert document == {"producer": "locked"}


def test_repository_venv_sitecustomize_cannot_run_before_coverage_launcher(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    environment_root = root / ".venv"
    subprocess.run(  # nosec B603
        (sys.executable, "-m", "venv", str(environment_root)),
        check=True,
        capture_output=True,
    )
    repository_python = environment_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    purelib = Path(
        subprocess.run(  # nosec B603
            (
                str(repository_python),
                "-c",
                "import sysconfig; print(sysconfig.get_paths()['purelib'])",
            ),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    origin = _coverage_package(purelib.parent, "locked")
    startup_witness = tmp_path / "sitecustomize-ran"
    (purelib / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(startup_witness)!r}).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    output = tmp_path / "coverage.json"

    with test_workspace(root) as workspace:
        staged = stage_coverage_dependency(
            origin=origin,
            environment_root=environment_root,
            workspace=workspace,
        )
        document = _run_staged(
            root,
            coverage_json_staged_command(
                python_prefix=(str(repository_python),),
                staged=staged,
                coverage_arguments=("json", "-o", str(output)),
            ),
            output,
        )

    assert document == {"producer": "locked"}
    assert not startup_witness.exists()


def test_coverage_launcher_retains_python310_tomli_and_installed_plugin_imports(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    environment_root = root / ".venv"
    import_root = environment_root / "site-packages"
    import_root.mkdir(parents=True)
    (import_root / "tomli.py").write_text("VALUE = 'tomli'\n", encoding="utf-8")
    (import_root / "installed_coverage_plugin.py").write_text(
        "VALUE = 'plugin'\n",
        encoding="utf-8",
    )
    origin = _coverage_package(
        environment_root,
        "unused",
        main_source=(
            "import json, pathlib, sys\n"
            "import installed_coverage_plugin, tomli\n"
            "output = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
            "output.write_text(json.dumps({\n"
            "    'producer': 'locked',\n"
            "    'tomli': tomli.VALUE,\n"
            "    'plugin': installed_coverage_plugin.VALUE,\n"
            "}))\n"
        ),
    )
    output = tmp_path / "coverage.json"
    helper_python = os.environ.get("PYREPO_CHECK_COVERAGE_HELPER_PYTHON", sys.executable)

    with test_workspace(root) as workspace:
        staged = stage_coverage_dependency(
            origin=origin,
            environment_root=environment_root,
            workspace=workspace,
        )
        document = _run_staged(
            root,
            coverage_json_staged_command(
                python_prefix=(helper_python,),
                staged=staged,
                coverage_arguments=("json", "-o", str(output)),
            ),
            output,
        )

    assert document == {"producer": "locked", "tomli": "tomli", "plugin": "plugin"}


def test_project_local_coverage_plugin_is_not_silently_loaded(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    environment_root = root / ".venv"
    witness = tmp_path / "project-plugin-ran"
    (root / "project_coverage_plugin.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(witness)!r}).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    origin = _coverage_package(
        environment_root,
        "unused",
        main_source="import project_coverage_plugin\n",
    )

    with test_workspace(root) as workspace:
        staged = stage_coverage_dependency(
            origin=origin,
            environment_root=environment_root,
            workspace=workspace,
        )
        completed = subprocess.run(  # nosec B603
            coverage_json_staged_command(
                python_prefix=(sys.executable,),
                staged=staged,
                coverage_arguments=("json",),
            ),
            cwd=root,
            check=False,
            capture_output=True,
        )

    assert completed.returncode > 0
    assert not witness.exists()


def test_coverage_json_launcher_parses_with_python_310_grammar() -> None:
    source = (
        Path(__file__).parents[1]
        / "src/pyrepo_check/_coverage_json_launcher.py"
    ).read_text(encoding="utf-8")

    ast.parse(source, feature_version=(3, 10))


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
        symlink_or_skip(staged.module_root, external, target_is_directory=True)
        with pytest.raises(OSError):
            ensure_staged_coverage_dependency(staged, workspace=workspace)
