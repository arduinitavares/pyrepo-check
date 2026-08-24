from pathlib import Path

import pytest

from pyrepo_check.config import ProjectConfig
from pyrepo_check.planning import (
    PlannedCheck,
    PlanningFacts,
    RunPlan,
    RunRequest,
    plan_run,
)


def make_config(
    root: Path,
    *,
    ruff_targets: tuple[str, ...] = ("src", "tests"),
    bandit_targets: tuple[str, ...] = ("src",),
    frozen: bool = False,
) -> ProjectConfig:
    return ProjectConfig(
        root=root,
        ruff_targets=ruff_targets,
        bandit_targets=bandit_targets,
        frozen=frozen,
    )


def command_names(plan: RunPlan) -> tuple[str, ...]:
    return tuple(check.name for check in plan.checks)


@pytest.mark.parametrize(
    ("positionals", "all_selected", "existing", "mode", "expected"),
    (
        ((), False, frozenset(), "strict_aggregate", ("ruff", "annotations", "ty", "bandit", "pytest")),
        ((), True, frozenset(), "strict_aggregate", ("ruff", "annotations", "ty", "bandit", "pytest")),
        (("ty",), False, frozenset(), "focused", ("ty",)),
        (("bandit", "ruff", "ruff"), False, frozenset(), "focused", ("ruff", "bandit")),
        (("api.py",), False, frozenset(("api.py",)), "focused", ("ruff", "annotations", "ty", "bandit")),
        (("api.py",), True, frozenset(("api.py",)), "focused", ("ruff", "annotations", "ty", "bandit", "pytest")),
        (("annotations-fix",), False, frozenset(), "focused", ("annotations-fix",)),
        (("pytest", "tests/test_cli.py::test_name"), False, frozenset(), "focused", ("pytest",)),
        (("pytest", "missing.py"), False, frozenset(), "focused", ("pytest",)),
        (("ruff", "missing.py"), False, frozenset(), "focused", ("ruff",)),
        (("missing.py",), True, frozenset(), "focused", ("ruff", "annotations", "ty", "bandit", "pytest")),
        (("ty",), True, frozenset(), "strict_aggregate", ("ruff", "annotations", "ty", "bandit", "pytest")),
        (("a.py", "b.py"), False, frozenset(("a.py", "b.py")), "focused", ("ruff", "annotations", "ty", "bandit")),
        (("a.py", "a.py"), False, frozenset(("a.py",)), "focused", ("ruff", "annotations", "ty", "bandit")),
    ),
)
def test_plans_requested_checks(
    tmp_path: Path,
    positionals: tuple[str, ...],
    all_selected: bool,
    existing: frozenset[str],
    mode: str,
    expected: tuple[str, ...],
) -> None:
    plan = plan_run(
        RunRequest(tmp_path, positionals, all_selected, no_frozen=False),
        make_config(tmp_path),
        PlanningFacts(existing),
    )

    assert plan.mode == mode
    assert command_names(plan) == expected


def test_preserves_existing_absolute_target(tmp_path: Path) -> None:
    target = tmp_path / "outside.py"
    token = str(target)

    plan = plan_run(
        RunRequest(tmp_path, (token,), all_selected=False, no_frozen=False),
        make_config(tmp_path),
        PlanningFacts(frozenset((token,))),
    )

    assert plan.mode == "focused"
    assert plan.targets == (token,)
    assert command_names(plan) == ("ruff", "annotations", "ty", "bandit")


def test_preserves_direct_target_order_and_duplicates(tmp_path: Path) -> None:
    plan = plan_run(
        RunRequest(tmp_path, ("a.py", "a.py", "b.py"), False, no_frozen=False),
        make_config(tmp_path),
        PlanningFacts(frozenset(("a.py", "b.py"))),
    )

    assert plan.targets == ("a.py", "a.py", "b.py")


def test_rejects_unknown_target_only_request(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"Unknown check\(s\): a.py, z.py"):
        plan_run(
            RunRequest(tmp_path, ("z.py", "a.py"), False, no_frozen=False),
            make_config(tmp_path),
            PlanningFacts(frozenset()),
        )


@pytest.mark.parametrize("frozen", (False, True))
def test_builds_commands_with_effective_frozen_setting(tmp_path: Path, frozen: bool) -> None:
    config = make_config(tmp_path, frozen=frozen)
    plan = plan_run(
        RunRequest(tmp_path, (), False, no_frozen=not frozen),
        config,
        PlanningFacts(frozenset()),
    )
    prefix = ("uv", "run", "--frozen", "python", "-m") if frozen else ("uv", "run", "python", "-m")

    assert all(check.command[: len(prefix)] == prefix for check in plan.checks)
    assert all(check.cwd == tmp_path for check in plan.checks)


def test_builds_strict_commands_against_repository_root(tmp_path: Path) -> None:
    plan = plan_run(
        RunRequest(tmp_path, (), False, no_frozen=False),
        make_config(tmp_path, ruff_targets=("tests",), bandit_targets=("src",)),
        PlanningFacts(frozenset()),
    )
    checks = {check.name: check for check in plan.checks}

    assert checks["ruff"].command[-1:] == (".",)
    assert checks["annotations"].command[6:7] == (".",)
    assert checks["bandit"].command[-2:] == ("-r", ".")
    assert checks["ty"].command[-2:] == ("ty", "check")
    assert checks["pytest"].command[-1:] == ("pytest",)


def test_builds_focused_commands_with_configured_targets(tmp_path: Path) -> None:
    plan = plan_run(
        RunRequest(tmp_path, ("ruff", "bandit"), False, no_frozen=False),
        make_config(tmp_path, ruff_targets=("src/pkg",), bandit_targets=("src/pkg",)),
        PlanningFacts(frozenset()),
    )
    checks = {check.name: check for check in plan.checks}

    assert checks["ruff"].command[-1:] == ("src/pkg",)
    assert checks["bandit"].command[-2:] == ("-r", "src/pkg")


def test_direct_targets_override_configured_targets_and_bandit_is_not_recursive(tmp_path: Path) -> None:
    plan = plan_run(
        RunRequest(tmp_path, ("bandit", "ruff", "api.py"), False, no_frozen=False),
        make_config(tmp_path),
        PlanningFacts(frozenset()),
    )
    checks: dict[str, PlannedCheck] = {check.name: check for check in plan.checks}

    assert checks["ruff"].command[-1:] == ("api.py",)
    assert checks["bandit"].command[-1:] == ("api.py",)
    assert "-r" not in checks["bandit"].command
