import dataclasses
import inspect
from pathlib import Path

import pytest

from pyrepo_check.config import ProjectConfig
from pyrepo_check.execution import ExecutionResult
from pyrepo_check.planning import PlannedCheck, RunPlan
from pyrepo_check.runner import Check, build_checks, run_checks, select_checks
from tests.support import RecordingRunner


def test_legacy_runner_names_and_check_shape_are_preserved(tmp_path: Path) -> None:
    config = ProjectConfig(tmp_path, ("src",), ("src",), frozen=False)

    assert tuple(inspect.signature(Check).parameters) == ("name", "command")
    assert tuple(field.name for field in dataclasses.fields(Check)) == (
        "name",
        "command",
    )
    assert repr(Check("ruff", ("uv",))) == "Check(name='ruff', command=('uv',))"
    assert isinstance(build_checks(config)["ruff"], Check)
    assert callable(select_checks)
    assert callable(run_checks)


def test_legacy_select_returns_original_checks_in_canonical_order() -> None:
    ruff = Check("ruff", ("ruff",))
    ty = Check("ty", ("ty",))
    checks = {"ty": ty, "ruff": ruff}

    selected = select_checks(checks, requested=("ty", "ruff"), all_selected=False)

    assert selected == (ruff, ty)
    assert selected[0] is ruff
    assert selected[1] is ty


def test_legacy_run_checks_delegates_to_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check = Check("ruff", ("uv", "run", "ruff"))
    expected = ExecutionResult(checks=(), exit_code=7)
    received: list[RunPlan] = []
    injected_runner = RecordingRunner()

    def fake_execute_plan(
        plan: RunPlan,
        *,
        runner: object,
    ) -> ExecutionResult:
        received.append(plan)
        assert runner is injected_runner
        return expected

    monkeypatch.setattr("pyrepo_check.runner.execute_plan", fake_execute_plan)

    result = run_checks((check,), cwd=tmp_path, runner=injected_runner)

    assert result == 7
    assert received == [
        RunPlan(
            mode="focused",
            targets=(),
            checks=(
                PlannedCheck(
                    name="ruff",
                    command=("uv", "run", "ruff"),
                    cwd=tmp_path,
                ),
            ),
        )
    ]
