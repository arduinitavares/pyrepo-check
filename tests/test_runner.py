from pathlib import Path
from subprocess import CompletedProcess  # nosec B404

import pytest

from pyrepo_check.config import ProjectConfig
from pyrepo_check.runner import build_checks, run_checks, select_checks


def test_builds_uv_frozen_commands(tmp_path: Path) -> None:
    config = ProjectConfig(
        root=tmp_path,
        ruff_targets=("src/pkg", "tests"),
        bandit_targets=("src/pkg",),
        frozen=True,
    )

    checks = build_checks(config)

    assert checks["ruff"].command == (
        "uv",
        "run",
        "--frozen",
        "python",
        "-m",
        "ruff",
        "check",
        "src/pkg",
        "tests",
    )
    assert checks["annotations"].command == (
        "uv",
        "run",
        "--frozen",
        "python",
        "-m",
        "ruff",
        "check",
        "src/pkg",
        "tests",
        "--select",
        "ANN",
        "--output-format",
        "concise",
    )
    assert checks["annotations-fix"].command == (
        "uv",
        "run",
        "--frozen",
        "python",
        "-m",
        "ruff",
        "check",
        "src/pkg",
        "tests",
        "--select",
        "ANN",
        "--fix",
        "--unsafe-fixes",
    )
    assert checks["ty"].command == ("uv", "run", "--frozen", "python", "-m", "ty", "check")
    assert checks["bandit"].command == (
        "uv",
        "run",
        "--frozen",
        "python",
        "-m",
        "bandit",
        "-c",
        "pyproject.toml",
        "-r",
        "src/pkg",
    )
    assert checks["pytest"].command == (
        "uv",
        "run",
        "--frozen",
        "python",
        "-m",
        "pytest",
    )


def test_builds_unfrozen_commands(tmp_path: Path) -> None:
    config = ProjectConfig(
        root=tmp_path,
        ruff_targets=("src",),
        bandit_targets=("src",),
        frozen=False,
    )

    checks = build_checks(config)

    assert checks["ruff"].command[:3] == ("uv", "run", "python")


def test_builds_strict_all_commands_against_repository_root(tmp_path: Path) -> None:
    config = ProjectConfig(
        root=tmp_path,
        ruff_targets=("tests", "scripts"),
        bandit_targets=("src",),
        frozen=False,
    )

    checks = build_checks(config, strict_all=True)

    assert checks["ruff"].command == ("uv", "run", "python", "-m", "ruff", "check", ".")
    assert checks["annotations"].command == (
        "uv",
        "run",
        "python",
        "-m",
        "ruff",
        "check",
        ".",
        "--select",
        "ANN",
        "--output-format",
        "concise",
    )
    assert checks["bandit"].command == (
        "uv",
        "run",
        "python",
        "-m",
        "bandit",
        "-c",
        "pyproject.toml",
        "-r",
        ".",
    )
    assert checks["ty"].command == ("uv", "run", "python", "-m", "ty", "check")
    assert checks["pytest"].command == ("uv", "run", "python", "-m", "pytest")


def test_explicit_targets_override_strict_all_targets(tmp_path: Path) -> None:
    config = ProjectConfig(
        root=tmp_path,
        ruff_targets=("tests", "scripts"),
        bandit_targets=("src",),
        frozen=False,
    )

    checks = build_checks(config, targets=("api.py",), strict_all=True)

    assert checks["ruff"].command == ("uv", "run", "python", "-m", "ruff", "check", "api.py")
    assert checks["bandit"].command == (
        "uv",
        "run",
        "python",
        "-m",
        "bandit",
        "-c",
        "pyproject.toml",
        "api.py",
    )


def test_selects_all_when_requested_list_is_empty(tmp_path: Path) -> None:
    checks = build_checks(ProjectConfig(tmp_path, ("src",), ("src",), frozen=False))

    selected = select_checks(checks, requested=(), all_selected=False)

    assert tuple(check.name for check in selected) == (
        "ruff",
        "annotations",
        "ty",
        "bandit",
        "pytest",
    )


def test_selects_annotations_fix_when_requested(tmp_path: Path) -> None:
    checks = build_checks(ProjectConfig(tmp_path, ("src",), ("src",), frozen=False))

    selected = select_checks(checks, requested=("annotations-fix",), all_selected=False)

    assert tuple(check.name for check in selected) == ("annotations-fix",)


def test_selects_all_without_annotations_fix(tmp_path: Path) -> None:
    checks = build_checks(ProjectConfig(tmp_path, ("src",), ("src",), frozen=False))

    selected = select_checks(checks, requested=(), all_selected=True)

    assert "annotations-fix" not in tuple(check.name for check in selected)


def test_rejects_unknown_check(tmp_path: Path) -> None:
    checks = build_checks(ProjectConfig(tmp_path, ("src",), ("src",), frozen=False))

    with pytest.raises(ValueError, match="Unknown check"):
        select_checks(checks, requested=("ruff", "mypy"), all_selected=False)


def test_runs_all_checks_and_returns_first_failing_exit_code(tmp_path: Path) -> None:
    checks = build_checks(ProjectConfig(tmp_path, ("src",), ("src",), frozen=False))
    calls: list[tuple[str, ...]] = []
    returncodes = {
        checks["ruff"].command: 1,
        checks["ty"].command: 2,
    }

    def fake_runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
    ) -> CompletedProcess[tuple[str, ...]]:
        calls.append(command)
        return CompletedProcess(command, returncode=returncodes[command])

    result = run_checks(
        (checks["ruff"], checks["ty"]),
        cwd=tmp_path,
        runner=fake_runner,
    )

    assert result == 1
    assert calls == [checks["ruff"].command, checks["ty"].command]
