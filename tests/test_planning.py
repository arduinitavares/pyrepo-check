from pathlib import Path

import pytest

from pyrepo_check.config import (
    CoverageConfig,
    ProjectConfig,
    TestShortcut as ConfigTestShortcut,
)
from pyrepo_check.planning import (
    CheckInvocation,
    CoverageExecutionPlan,
    ExplicitRepositoryPython,
    PlanningFacts,
    PlanningFailure,
    PytestExecutionPlan,
    RunPlan,
    RunRequest,
    build_checks as build_planned_checks,
    plan_run,
    select_check_names,
)


@pytest.mark.parametrize(
    "python_request",
    ("3.10", "3.10.19", "3.11", "3.12.11", "3.13", "3.13.15"),
)
def test_plans_one_explicit_repository_python(tmp_path: Path, python_request: str) -> None:
    plan = plan_run(
        RunRequest(tmp_path, ("ty",), False, False, repository_python=python_request),
        make_config(tmp_path),
        PlanningFacts(frozenset(), pyproject_exists=True),
    )

    assert plan.repository_python == ExplicitRepositoryPython(python_request)
    assert plan.root == tmp_path.resolve()


@pytest.mark.parametrize("python_request", ("3.9", "3.14", "3", "3.12.x", "pypy3.12"))
def test_rejects_unsupported_repository_python_without_a_plan(
    tmp_path: Path,
    python_request: str,
) -> None:
    with pytest.raises(PlanningFailure) as raised:
        plan_run(
            RunRequest(tmp_path, ("ty",), False, False, repository_python=python_request),
            make_config(tmp_path),
            PlanningFacts(frozenset(), pyproject_exists=True),
        )

    assert raised.value.code == "invalid_arguments"


def test_no_frozen_is_a_stable_zero_process_planning_error(tmp_path: Path) -> None:
    with pytest.raises(PlanningFailure) as raised:
        plan_run(
            RunRequest(tmp_path, ("ty",), False, True),
            make_config(tmp_path),
            PlanningFacts(frozenset(), pyproject_exists=True),
        )

    assert raised.value.code == "unsafe_unlocked_execution"
    assert str(raised.value) == ("--no-frozen is incompatible with repository-safe execution.")
    assert raised.value.hint == ("Update uv.lock explicitly, then rerun without --no-frozen.")


def test_carries_internal_output_intent(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    facts = PlanningFacts(frozenset(), pyproject_exists=True)
    terminal = RunRequest(tmp_path, ("ty",), False, False)
    machine = RunRequest(tmp_path, ("ty",), False, False, "json")

    assert terminal.output_format == "terminal"
    assert plan_run(machine, config, facts).output_format == "json"
    assert plan_run(terminal, config, facts).output_format == "terminal"


def make_config(
    root: Path,
    *,
    ruff_targets: tuple[str, ...] = ("src", "tests"),
    bandit_targets: tuple[str, ...] = ("src",),
    test_shortcuts: tuple[ConfigTestShortcut, ...] = (),
    coverage: CoverageConfig | None = None,
) -> ProjectConfig:
    return ProjectConfig(
        root=root,
        ruff_targets=ruff_targets,
        bandit_targets=bandit_targets,
        test_shortcuts=test_shortcuts,
        coverage=coverage,
    )


def command_names(plan: RunPlan) -> tuple[str, ...]:
    return tuple(check.name for check in plan.checks)


def commands(plan: RunPlan) -> tuple[tuple[str, ...], ...]:
    return tuple(check.arguments for check in plan.checks)


def expected_arguments(command: tuple[str, ...]) -> tuple[str, ...]:
    return command[command.index("-m") + 2 :]


def test_plans_coverage_matrix_without_changing_plain_pytest_commands(tmp_path: Path) -> None:
    coverage = CoverageConfig(tmp_path / "pyproject.toml", 87.5)
    shortcut = ConfigTestShortcut("unit", ("tests/unit",))
    cases = (
        (("pytest",), False, True, True, (), None, (), "focused", "complete"),
        (
            ("pytest", "tests/unit.py"),
            False,
            True,
            True,
            (),
            None,
            ("tests/unit.py",),
            "focused",
            "partial",
        ),
        (("pytest",), False, True, True, (), shortcut, ("tests/unit",), "focused", "partial"),
        ((), False, True, True, (), None, (), "strict_aggregate", "complete"),
        ((), False, False, True, (), None, (), "strict_aggregate", "complete"),
        ((), False, False, False, (), None, (), "strict_aggregate", "unavailable"),
        (("pytest",), False, False, True, (), None, (), "focused", "not_requested"),
        (
            ("tests/unit.py",),
            True,
            True,
            True,
            ("tests/unit.py",),
            None,
            ("tests/unit.py",),
            "focused",
            "partial",
        ),
    )
    for (
        positionals,
        all_selected,
        requested,
        configured,
        existing,
        test_shortcut,
        expected_pytest_args,
        mode,
        scope,
    ) in cases:
        plan = plan_run(
            RunRequest(
                tmp_path,
                positionals,
                all_selected,
                False,
                coverage_requested=requested,
                test_shortcut=test_shortcut.name if test_shortcut else None,
            ),
            make_config(
                tmp_path,
                coverage=coverage if configured else None,
                test_shortcuts=(test_shortcut,) if test_shortcut else (),
            ),
            PlanningFacts(frozenset(existing), pyproject_exists=True),
        )
        pytest_check = next(check for check in plan.checks if check.name == "pytest")
        pytest_plan = pytest_check.pytest
        assert pytest_plan is not None
        assert plan.mode == mode
        assert plan.planned_coverage_scope == scope
        assert pytest_plan.pytest_args == expected_pytest_args
        assert pytest_check.arguments == expected_pytest_args
        if scope in {"not_requested", "unavailable"}:
            assert pytest_plan.coverage is None
        else:
            assert pytest_plan.coverage == CoverageExecutionPlan(tmp_path / "pyproject.toml", 87.5)


@pytest.mark.parametrize(
    ("positionals", "all_selected"),
    ((("ty",), False), (("tests/unit.py",), False)),
)
def test_rejects_explicit_coverage_before_checking_configuration(
    tmp_path: Path, positionals: tuple[str, ...], all_selected: bool
) -> None:
    with pytest.raises(PlanningFailure) as raised:
        plan_run(
            RunRequest(tmp_path, positionals, all_selected, False, coverage_requested=True),
            make_config(tmp_path),
            PlanningFacts(frozenset(positionals), pyproject_exists=True),
        )

    assert raised.value.code == "invalid_arguments"


def test_requires_native_configuration_for_explicit_pytest_coverage(tmp_path: Path) -> None:
    with pytest.raises(PlanningFailure) as raised:
        plan_run(
            RunRequest(tmp_path, ("pytest",), False, False, coverage_requested=True),
            make_config(tmp_path),
            PlanningFacts(frozenset(), pyproject_exists=True),
        )

    assert raised.value.code == "coverage_configuration_required"


def test_plans_named_pytest_shortcut_with_exact_configured_tokens(
    tmp_path: Path,
) -> None:
    shortcut = ConfigTestShortcut("unit", ("tests/unit", "-m", "not slow", "tests/unit"))
    plan = plan_run(
        RunRequest(
            root=tmp_path,
            positionals=("pytest",),
            all_selected=False,
            no_frozen=False,
            output_format="json",
            test_shortcut="unit",
        ),
        make_config(tmp_path, test_shortcuts=(shortcut,)),
        PlanningFacts(frozenset(), pyproject_exists=True),
    )

    assert plan.mode == "focused"
    assert plan.targets == ()
    assert plan.test_shortcut == "unit"
    assert plan.pytest_args == ("tests/unit", "-m", "not slow", "tests/unit")
    assert plan.planned_test_scope == "partial"
    assert plan.checks[0].arguments == plan.pytest_args


@pytest.mark.parametrize(
    ("positionals", "all_selected"),
    (
        (("pytest", "tests/a.py"), False),
        (("pytest", "ruff"), False),
        ((), False),
        (("tests/a.py",), False),
        (("pytest",), True),
    ),
)
def test_shortcut_conflicts_are_rejected_before_name_lookup(
    tmp_path: Path,
    positionals: tuple[str, ...],
    all_selected: bool,
) -> None:
    with pytest.raises(PlanningFailure) as raised:
        plan_run(
            RunRequest(tmp_path, positionals, all_selected, False, test_shortcut="unknown"),
            make_config(tmp_path, test_shortcuts=(ConfigTestShortcut("unit", ("tests/unit",)),)),
            PlanningFacts(frozenset(("tests/a.py",)), pyproject_exists=True),
        )

    assert raised.value.code == "invalid_arguments"
    assert str(raised.value) == (
        "--shortcut requires an explicit pytest-only run with no direct targets or --all."
    )
    assert raised.value.hint == "Use: pyrepo-check pytest --shortcut NAME"


def test_repeated_pytest_allows_named_shortcut(tmp_path: Path) -> None:
    plan = plan_run(
        RunRequest(tmp_path, ("pytest", "pytest"), False, False, test_shortcut="unit"),
        make_config(tmp_path, test_shortcuts=(ConfigTestShortcut("unit", ("tests/unit",)),)),
        PlanningFacts(frozenset(), pyproject_exists=True),
    )

    assert command_names(plan) == ("pytest",)
    assert plan.pytest_args == ("tests/unit",)


def test_unknown_shortcut_lists_sorted_configured_names(tmp_path: Path) -> None:
    with pytest.raises(PlanningFailure) as raised:
        plan_run(
            RunRequest(tmp_path, ("pytest",), False, False, test_shortcut="smoke"),
            make_config(
                tmp_path,
                test_shortcuts=(
                    ConfigTestShortcut("unit", ("tests/unit",)),
                    ConfigTestShortcut("cli", ("tests/test_cli.py",)),
                    ConfigTestShortcut("integration", ("-m", "integration")),
                ),
            ),
            PlanningFacts(frozenset(), pyproject_exists=True),
        )

    assert raised.value.code == "unknown_test_shortcut"
    assert str(raised.value) == "Unknown Test Shortcut: smoke"
    assert raised.value.hint == "Available Test Shortcuts: cli, integration, unit"


def test_unknown_shortcut_reports_empty_configuration(tmp_path: Path) -> None:
    with pytest.raises(PlanningFailure) as raised:
        plan_run(
            RunRequest(tmp_path, ("pytest",), False, False, test_shortcut="smoke"),
            make_config(tmp_path),
            PlanningFacts(frozenset(), pyproject_exists=True),
        )

    assert raised.value.code == "unknown_test_shortcut"
    assert raised.value.hint == "No Test Shortcuts are configured."


@pytest.mark.parametrize(
    ("positionals", "shortcut", "expected_args", "expected_scope"),
    (
        (("ruff",), None, None, "not_selected"),
        (("pytest",), None, (), "complete"),
        (
            ("pytest", "tests/test_cli.py::test_name"),
            None,
            ("tests/test_cli.py::test_name",),
            "partial",
        ),
        (("pytest",), "unit", ("tests/unit",), "partial"),
    ),
)
def test_plans_authoritative_pytest_scope_metadata(
    tmp_path: Path,
    positionals: tuple[str, ...],
    shortcut: str | None,
    expected_args: tuple[str, ...] | None,
    expected_scope: str,
) -> None:
    plan = plan_run(
        RunRequest(tmp_path, positionals, False, False, test_shortcut=shortcut),
        make_config(tmp_path, test_shortcuts=(ConfigTestShortcut("unit", ("tests/unit",)),)),
        PlanningFacts(frozenset(), pyproject_exists=True),
    )

    assert plan.pytest_args == expected_args
    assert plan.planned_test_scope == expected_scope


@pytest.mark.parametrize(
    ("positionals", "shortcut", "expected_args"),
    (
        (("pytest", "tests/test_cli.py::test_name"), None, ("tests/test_cli.py::test_name",)),
        (("pytest",), "unit", ("tests/unit", "-m", "not slow")),
    ),
)
def test_pytest_execution_plan_exposes_consumer_command_and_pytest_args(
    tmp_path: Path,
    positionals: tuple[str, ...],
    shortcut: str | None,
    expected_args: tuple[str, ...],
) -> None:
    plan = plan_run(
        RunRequest(tmp_path, positionals, False, False, test_shortcut=shortcut),
        make_config(
            tmp_path,
            test_shortcuts=(ConfigTestShortcut("unit", ("tests/unit", "-m", "not slow")),),
        ),
        PlanningFacts(frozenset(), pyproject_exists=True),
    )
    pytest_check = plan.checks[0]

    assert isinstance(pytest_check.pytest, PytestExecutionPlan)
    assert pytest_check.pytest.pytest_args == expected_args
    assert pytest_check.pytest.artifact_protocol == "pytest_v1"
    assert pytest_check.arguments == pytest_check.pytest.pytest_args


def test_pytest_execution_plan_is_not_attached_to_ordinary_checks(tmp_path: Path) -> None:
    checks = build_planned_checks(make_config(tmp_path))

    assert isinstance(checks["pytest"].pytest, PytestExecutionPlan)
    assert all(check.pytest is None for name, check in checks.items() if name != "pytest")


def test_builds_environment_neutral_check_arguments(tmp_path: Path) -> None:
    checks = build_planned_checks(
        make_config(
            tmp_path,
            ruff_targets=("src/pkg", "tests"),
            bandit_targets=("src/pkg",),
        )
    )

    assert {name: check.arguments for name, check in checks.items()} == {
        "ruff": ("check", "src/pkg", "tests"),
        "annotations": (
            "check",
            "src/pkg",
            "tests",
            "--select",
            "ANN",
            "--output-format",
            "concise",
        ),
        "annotations-fix": (
            "check",
            "src/pkg",
            "tests",
            "--select",
            "ANN",
            "--fix",
            "--unsafe-fixes",
        ),
        "ty": ("check",),
        "bandit": ("-c", "pyproject.toml", "-r", "src/pkg"),
        "pytest": (),
    }


def test_selects_legacy_names_in_canonical_order() -> None:
    available = ("ruff", "annotations", "annotations-fix", "ty", "bandit", "pytest")

    assert select_check_names(available, requested=(), all_selected=False) == (
        "ruff",
        "annotations",
        "ty",
        "bandit",
        "pytest",
    )
    assert select_check_names(
        available,
        requested=("annotations-fix",),
        all_selected=False,
    ) == ("annotations-fix",)
    assert select_check_names(available, requested=(), all_selected=True) == (
        "ruff",
        "annotations",
        "ty",
        "bandit",
        "pytest",
    )
    with pytest.raises(ValueError) as raised:
        select_check_names(available, requested=("ruff", "mypy"), all_selected=False)

    assert str(raised.value) == "Unknown check(s): mypy"


@pytest.mark.parametrize(
    (
        "positionals",
        "all_selected",
        "existing",
        "ruff_targets",
        "bandit_targets",
        "expected",
    ),
    (
        pytest.param(
            ("ruff",),
            False,
            frozenset(),
            ("src",),
            ("src",),
            (("uv", "run", "python", "-m", "ruff", "check", "src"),),
            id="focused-ruff-from-root",
        ),
        pytest.param(
            ("annotations",),
            False,
            frozenset(),
            ("src",),
            ("src",),
            (
                (
                    "uv",
                    "run",
                    "python",
                    "-m",
                    "ruff",
                    "check",
                    "src",
                    "--select",
                    "ANN",
                    "--output-format",
                    "concise",
                ),
            ),
            id="focused-annotations-from-root",
        ),
        pytest.param(
            ("annotations-fix",),
            False,
            frozenset(),
            ("src",),
            ("src",),
            (
                (
                    "uv",
                    "run",
                    "python",
                    "-m",
                    "ruff",
                    "check",
                    "src",
                    "--select",
                    "ANN",
                    "--fix",
                    "--unsafe-fixes",
                ),
            ),
            id="focused-annotations-fix-from-root",
        ),
        pytest.param(
            ("ruff", "api.py"),
            False,
            frozenset(),
            ("src",),
            ("src",),
            (("uv", "run", "python", "-m", "ruff", "check", "api.py"),),
            id="ruff-with-direct-target",
        ),
        pytest.param(
            ("annotations", "api.py"),
            False,
            frozenset(),
            ("src",),
            ("src",),
            (
                (
                    "uv",
                    "run",
                    "python",
                    "-m",
                    "ruff",
                    "check",
                    "api.py",
                    "--select",
                    "ANN",
                    "--output-format",
                    "concise",
                ),
            ),
            id="annotations-with-direct-target",
        ),
        pytest.param(
            ("annotations-fix", "api.py"),
            False,
            frozenset(),
            ("src",),
            ("src",),
            (
                (
                    "uv",
                    "run",
                    "python",
                    "-m",
                    "ruff",
                    "check",
                    "api.py",
                    "--select",
                    "ANN",
                    "--fix",
                    "--unsafe-fixes",
                ),
            ),
            id="annotations-fix-with-direct-target",
        ),
        pytest.param(
            ("api.py",),
            False,
            frozenset(("api.py",)),
            ("src",),
            ("src",),
            (
                ("uv", "run", "python", "-m", "ruff", "check", "api.py"),
                (
                    "uv",
                    "run",
                    "python",
                    "-m",
                    "ruff",
                    "check",
                    "api.py",
                    "--select",
                    "ANN",
                    "--output-format",
                    "concise",
                ),
                ("uv", "run", "python", "-m", "ty", "check", "api.py"),
                ("uv", "run", "python", "-m", "bandit", "-c", "pyproject.toml", "api.py"),
            ),
            id="target-only-four-check-order",
        ),
        pytest.param(
            ("api.py",),
            True,
            frozenset(("api.py",)),
            ("src",),
            ("src",),
            (
                ("uv", "run", "python", "-m", "ruff", "check", "api.py"),
                (
                    "uv",
                    "run",
                    "python",
                    "-m",
                    "ruff",
                    "check",
                    "api.py",
                    "--select",
                    "ANN",
                    "--output-format",
                    "concise",
                ),
                ("uv", "run", "python", "-m", "ty", "check", "api.py"),
                ("uv", "run", "python", "-m", "bandit", "-c", "pyproject.toml", "api.py"),
                ("uv", "run", "python", "-m", "pytest", "api.py"),
            ),
            id="all-with-direct-target-five-check-order",
        ),
        pytest.param(
            (),
            True,
            frozenset(),
            ("tests", "scripts"),
            ("tests",),
            (
                ("uv", "run", "python", "-m", "ruff", "check", "."),
                (
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
                ),
                ("uv", "run", "python", "-m", "ty", "check"),
                ("uv", "run", "python", "-m", "bandit", "-c", "pyproject.toml", "-r", "."),
                ("uv", "run", "python", "-m", "pytest"),
            ),
            id="strict-all-ignores-configured-narrow-targets",
        ),
        pytest.param(
            (),
            False,
            frozenset(),
            ("tests", "scripts"),
            ("tests",),
            (
                ("uv", "run", "python", "-m", "ruff", "check", "."),
                (
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
                ),
                ("uv", "run", "python", "-m", "ty", "check"),
                ("uv", "run", "python", "-m", "bandit", "-c", "pyproject.toml", "-r", "."),
                ("uv", "run", "python", "-m", "pytest"),
            ),
            id="no-positionals-use-strict-repository-targets",
        ),
    ),
)
def test_preserves_legacy_cli_command_contracts(
    tmp_path: Path,
    positionals: tuple[str, ...],
    all_selected: bool,
    existing: frozenset[str],
    ruff_targets: tuple[str, ...],
    bandit_targets: tuple[str, ...],
    expected: tuple[tuple[str, ...], ...],
) -> None:
    plan = plan_run(
        RunRequest(tmp_path, positionals, all_selected, no_frozen=False),
        make_config(
            tmp_path,
            ruff_targets=ruff_targets,
            bandit_targets=bandit_targets,
        ),
        PlanningFacts(existing, pyproject_exists=True),
    )

    assert commands(plan) == tuple(expected_arguments(command) for command in expected)
    assert plan.root == tmp_path


@pytest.mark.parametrize(
    ("positionals", "all_selected", "existing", "mode", "expected"),
    (
        (
            (),
            False,
            frozenset(),
            "strict_aggregate",
            ("ruff", "annotations", "ty", "bandit", "pytest"),
        ),
        (
            (),
            True,
            frozenset(),
            "strict_aggregate",
            ("ruff", "annotations", "ty", "bandit", "pytest"),
        ),
        (("ty",), False, frozenset(), "focused", ("ty",)),
        (("bandit", "ruff", "ruff"), False, frozenset(), "focused", ("ruff", "bandit")),
        (
            ("api.py",),
            False,
            frozenset(("api.py",)),
            "focused",
            ("ruff", "annotations", "ty", "bandit"),
        ),
        (
            ("api.py",),
            True,
            frozenset(("api.py",)),
            "focused",
            ("ruff", "annotations", "ty", "bandit", "pytest"),
        ),
        (("annotations-fix",), False, frozenset(), "focused", ("annotations-fix",)),
        (("pytest", "tests/test_cli.py::test_name"), False, frozenset(), "focused", ("pytest",)),
        (("pytest", "missing.py"), False, frozenset(), "focused", ("pytest",)),
        (("ruff", "missing.py"), False, frozenset(), "focused", ("ruff",)),
        (
            ("missing.py",),
            True,
            frozenset(),
            "focused",
            ("ruff", "annotations", "ty", "bandit", "pytest"),
        ),
        (
            ("ty",),
            True,
            frozenset(),
            "strict_aggregate",
            ("ruff", "annotations", "ty", "bandit", "pytest"),
        ),
        (
            ("a.py", "b.py"),
            False,
            frozenset(("a.py", "b.py")),
            "focused",
            ("ruff", "annotations", "ty", "bandit"),
        ),
        (
            ("a.py", "a.py"),
            False,
            frozenset(("a.py",)),
            "focused",
            ("ruff", "annotations", "ty", "bandit"),
        ),
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
        PlanningFacts(existing, pyproject_exists=True),
    )

    assert plan.mode == mode
    assert command_names(plan) == expected


def test_preserves_existing_absolute_target(tmp_path: Path) -> None:
    target = tmp_path / "outside.py"
    token = str(target)

    plan = plan_run(
        RunRequest(tmp_path, (token,), all_selected=False, no_frozen=False),
        make_config(tmp_path),
        PlanningFacts(frozenset((token,)), pyproject_exists=True),
    )

    assert plan.mode == "focused"
    assert plan.targets == (token,)
    assert command_names(plan) == ("ruff", "annotations", "ty", "bandit")


def test_preserves_direct_target_order_and_duplicates(tmp_path: Path) -> None:
    plan = plan_run(
        RunRequest(tmp_path, ("a.py", "a.py", "b.py"), False, no_frozen=False),
        make_config(tmp_path),
        PlanningFacts(frozenset(("a.py", "b.py")), pyproject_exists=True),
    )

    assert plan.targets == ("a.py", "a.py", "b.py")


def test_rejects_unknown_target_only_request(tmp_path: Path) -> None:
    with pytest.raises(PlanningFailure) as raised:
        plan_run(
            RunRequest(tmp_path, ("z.py", "a.py"), False, no_frozen=False),
            make_config(tmp_path),
            PlanningFacts(frozenset(), pyproject_exists=True),
        )

    assert str(raised.value) == "Unknown check(s): a.py, z.py"
    assert raised.value.code == "unknown_target"
    assert raised.value.hint == "Check the target path or select a check name."


@pytest.mark.parametrize(
    ("positionals", "expected_code"),
    (("mypy", "unknown_check"), ("a.py", "unknown_target"), (("a.py", "mypy"), "unknown_check")),
)
def test_plan_run_classifies_unknown_target_only_tokens(
    tmp_path: Path,
    positionals: tuple[str, ...] | str,
    expected_code: str,
) -> None:
    tokens = (positionals,) if isinstance(positionals, str) else positionals
    with pytest.raises(PlanningFailure) as raised:
        plan_run(
            RunRequest(tmp_path, tokens, False, no_frozen=False),
            make_config(tmp_path),
            PlanningFacts(frozenset(), pyproject_exists=True),
        )

    assert raised.value.code == expected_code


def test_unknown_check_has_typed_failure_and_hint() -> None:
    available = ("ruff", "annotations", "annotations-fix", "ty", "bandit", "pytest")

    with pytest.raises(PlanningFailure) as raised:
        select_check_names(available, requested=("mypy",), all_selected=False)

    assert isinstance(raised.value, ValueError)
    assert raised.value.code == "unknown_check"
    assert str(raised.value) == "Unknown check(s): mypy"
    assert (
        raised.value.hint
        == "Available checks: ruff, annotations, annotations-fix, ty, bandit, pytest"
    )


@pytest.mark.parametrize("token", ("a.py", "src/foo", "tests::test_name"))
def test_unknown_path_like_target_has_unknown_target_code(token: str) -> None:
    with pytest.raises(PlanningFailure) as raised:
        select_check_names((), requested=(token,), all_selected=False)

    assert raised.value.code == "unknown_target"


def test_mixed_unknown_tokens_choose_check_code() -> None:
    with pytest.raises(PlanningFailure) as raised:
        select_check_names((), requested=("a.py", "mypy"), all_selected=False)

    assert raised.value.code == "unknown_check"


def test_known_check_with_missing_target_remains_allowed(tmp_path: Path) -> None:
    plan = plan_run(
        RunRequest(tmp_path, ("ruff", "missing.py"), False, False),
        make_config(tmp_path),
        PlanningFacts(frozenset(), pyproject_exists=True),
    )

    assert command_names(plan) == ("ruff",)


def test_plans_default_repository_python_without_execution_details(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    plan = plan_run(
        RunRequest(tmp_path, (), False, no_frozen=False),
        config,
        PlanningFacts(frozenset(), pyproject_exists=True),
    )

    assert plan.repository_python.kind == "default"
    assert all("uv" not in check.arguments for check in plan.checks)


def test_builds_strict_commands_against_repository_root(tmp_path: Path) -> None:
    plan = plan_run(
        RunRequest(tmp_path, (), False, no_frozen=False),
        make_config(tmp_path, ruff_targets=("tests",), bandit_targets=("src",)),
        PlanningFacts(frozenset(), pyproject_exists=True),
    )
    checks = {check.name: check for check in plan.checks}

    assert checks["ruff"].arguments[-1:] == (".",)
    assert checks["annotations"].arguments[1:2] == (".",)
    assert checks["bandit"].arguments[-2:] == ("-r", ".")
    assert checks["ty"].arguments == ("check",)
    assert checks["pytest"].arguments == ()


def test_builds_focused_commands_with_configured_targets(tmp_path: Path) -> None:
    plan = plan_run(
        RunRequest(tmp_path, ("ruff", "bandit"), False, no_frozen=False),
        make_config(tmp_path, ruff_targets=("src/pkg",), bandit_targets=("src/pkg",)),
        PlanningFacts(frozenset(), pyproject_exists=True),
    )
    checks = {check.name: check for check in plan.checks}

    assert checks["ruff"].arguments[-1:] == ("src/pkg",)
    assert checks["bandit"].arguments[-2:] == ("-r", "src/pkg")


def test_direct_targets_override_configured_targets_and_bandit_is_not_recursive(
    tmp_path: Path,
) -> None:
    plan = plan_run(
        RunRequest(tmp_path, ("bandit", "ruff", "api.py"), False, no_frozen=False),
        make_config(tmp_path),
        PlanningFacts(frozenset(), pyproject_exists=True),
    )
    checks: dict[str, CheckInvocation] = {check.name: check for check in plan.checks}

    assert checks["ruff"].arguments[-1:] == ("api.py",)
    assert checks["bandit"].arguments[-1:] == ("api.py",)
    assert "-r" not in checks["bandit"].arguments


def test_explicit_targets_override_strict_all_targets(tmp_path: Path) -> None:
    checks = build_planned_checks(
        make_config(
            tmp_path,
            ruff_targets=("tests", "scripts"),
            bandit_targets=("src",),
        ),
        targets=("api.py",),
        strict_all=True,
    )

    assert checks["ruff"].arguments == ("check", "api.py")
    assert checks["bandit"].arguments == ("-c", "pyproject.toml", "api.py")
