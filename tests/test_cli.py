from pathlib import Path

import pytest

from pyrepo_check.cli import main
from pyrepo_check.config import ProjectConfig
from pyrepo_check.execution import ExecutionResult, ProcessRunner
from pyrepo_check.planning import PlanningFacts, RunPlan, RunRequest
from tests.support import RecordingRunner


def test_cli_builds_request_and_executes_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_plan = RunPlan(mode="focused", targets=(), checks=())
    planned_requests: list[RunRequest] = []
    executed_plans: list[RunPlan] = []
    injected_runner = RecordingRunner()

    def fake_plan_run(
        request: RunRequest,
        config: ProjectConfig,
        facts: PlanningFacts,
    ) -> RunPlan:
        planned_requests.append(request)
        assert config.root == tmp_path.resolve()
        assert config.frozen is False
        assert facts == PlanningFacts(existing_positionals=frozenset())
        return expected_plan

    def fake_execute_plan(
        plan: RunPlan,
        *,
        runner: ProcessRunner,
    ) -> ExecutionResult:
        executed_plans.append(plan)
        assert runner is injected_runner
        return ExecutionResult(checks=(), exit_code=7)

    monkeypatch.setattr("pyrepo_check.cli.plan_run", fake_plan_run, raising=False)
    monkeypatch.setattr(
        "pyrepo_check.cli.execute_plan",
        fake_execute_plan,
        raising=False,
    )

    result = main(
        ["--root", str(tmp_path), "--no-frozen", "ty"],
        runner=injected_runner,
    )

    assert result == 7
    assert planned_requests == [
        RunRequest(
            root=tmp_path,
            positionals=("ty",),
            all_selected=False,
            no_frozen=True,
        )
    ]
    assert executed_plans == [expected_plan]


def test_cli_reports_planning_error_without_spawning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = RecordingRunner()

    result = main(["--root", str(tmp_path), "mypy"], runner=runner)

    captured = capsys.readouterr()
    assert result == 2
    assert captured.err == "Unknown check(s): mypy\n"
    assert runner.calls == []


def test_cli_propagates_runner_value_error_by_identity(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    error = ValueError("runner failed")
    runner = RecordingRunner(raise_on_call=1, exception=error)

    with pytest.raises(ValueError) as raised:
        main(["--root", str(tmp_path), "ruff"], runner=runner)

    assert raised.value is error
