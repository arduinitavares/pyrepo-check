from pathlib import Path
from subprocess import CompletedProcess

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


def test_selects_all_when_requested_list_is_empty(tmp_path: Path) -> None:
    checks = build_checks(ProjectConfig(tmp_path, ("src",), ("src",), frozen=False))

    selected = select_checks(checks, requested=(), all_selected=False)

    assert tuple(check.name for check in selected) == ("ruff", "ty", "bandit", "pytest")


def test_rejects_unknown_check(tmp_path: Path) -> None:
    checks = build_checks(ProjectConfig(tmp_path, ("src",), ("src",), frozen=False))

    with pytest.raises(ValueError, match="Unknown check"):
        select_checks(checks, requested=("ruff", "mypy"), all_selected=False)


def test_stops_on_first_failing_check(tmp_path: Path) -> None:
    checks = build_checks(ProjectConfig(tmp_path, ("src",), ("src",), frozen=False))
    calls: list[tuple[str, ...]] = []

    def fake_runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
    ) -> CompletedProcess[tuple[str, ...]]:
        calls.append(command)
        return CompletedProcess(command, returncode=1)

    result = run_checks(
        (checks["ruff"], checks["pytest"]),
        cwd=tmp_path,
        runner=fake_runner,
    )

    assert result == 1
    assert calls == [checks["ruff"].command]
